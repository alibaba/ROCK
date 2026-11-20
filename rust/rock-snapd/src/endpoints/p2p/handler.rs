//! Snapd-to-snapd decoded DATA endpoint, independent of the Admin HTTP boundary.

use std::io;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use async_trait::async_trait;
use thiserror::Error;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;

use super::protocol::{
    NativeCoverageRequest, NativeCoverageResult, NativeReadRangeRequest, NativeSnapshotMetadata,
    P2PAvailabilityResult, P2PReadRangeRequest, P2PTcpErrorCode, P2PTcpRequest, P2PTcpResponse,
    P2P_NATIVE_PROTOCOL_VERSION, P2P_PROTOCOL_VERSION, P2P_TCP_MAGIC,
};
use super::tcp::{read_tcp_frame, write_tcp_frame};
use crate::observability::metrics::SnapdMetrics;
use crate::observability::structured_log::{
    StructuredComponent, StructuredEvent, StructuredEventName, StructuredLogLevel,
    StructuredOutcome,
};
use crate::snapshot::services::p2p::range_sha256;
use crate::vm::error::OperationError as SnapshotControlError;
use crate::vm::repository::VmService;

const MAX_REQUEST_ID_BYTES: usize = 128;

#[async_trait]
pub trait P2pApi: Send + Sync + 'static {
    fn supports_native_p2p(&self) -> bool {
        false
    }
    async fn p2p_native_metadata(
        &self,
        _snapshot_id: &str,
        _generation_id: Option<&str>,
    ) -> Result<NativeSnapshotMetadata, SnapshotControlError> {
        Err(SnapshotControlError::InvalidState(
            "native P2P is unsupported".into(),
        ))
    }
    async fn p2p_native_coverage(
        &self,
        _snapshot_id: &str,
        _request: &NativeCoverageRequest,
    ) -> Result<NativeCoverageResult, SnapshotControlError> {
        Err(SnapshotControlError::InvalidState(
            "native P2P is unsupported".into(),
        ))
    }
    async fn p2p_native_read_range(
        &self,
        _snapshot_id: &str,
        _request: &NativeReadRangeRequest,
    ) -> Result<Vec<u8>, SnapshotControlError> {
        Err(SnapshotControlError::InvalidState(
            "native P2P is unsupported".into(),
        ))
    }
    async fn p2p_availability(
        &self,
        snapshot_id: &str,
        spec_checksum: &str,
    ) -> Result<P2PAvailabilityResult, SnapshotControlError>;

    async fn p2p_read_range(
        &self,
        snapshot_id: &str,
        request: &P2PReadRangeRequest,
    ) -> Result<Vec<u8>, SnapshotControlError>;

    async fn p2p_selection_availability(
        &self,
        snapshot_id: &str,
        spec_checksum: &str,
    ) -> Result<P2PAvailabilityResult, SnapshotControlError> {
        self.p2p_availability(snapshot_id, spec_checksum).await
    }
}

#[async_trait]
impl P2pApi for VmService {
    fn supports_native_p2p(&self) -> bool {
        true
    }
    async fn p2p_native_metadata(
        &self,
        snapshot_id: &str,
        generation_id: Option<&str>,
    ) -> Result<NativeSnapshotMetadata, SnapshotControlError> {
        VmService::p2p_native_metadata(self, snapshot_id, generation_id).await
    }
    async fn p2p_native_coverage(
        &self,
        snapshot_id: &str,
        request: &NativeCoverageRequest,
    ) -> Result<NativeCoverageResult, SnapshotControlError> {
        VmService::p2p_native_coverage(self, snapshot_id, request).await
    }
    async fn p2p_native_read_range(
        &self,
        snapshot_id: &str,
        request: &NativeReadRangeRequest,
    ) -> Result<Vec<u8>, SnapshotControlError> {
        VmService::p2p_native_read_range(self, snapshot_id, request).await
    }
    async fn p2p_selection_availability(
        &self,
        snapshot_id: &str,
        spec_checksum: &str,
    ) -> Result<P2PAvailabilityResult, SnapshotControlError> {
        VmService::p2p_selection_availability(self, snapshot_id, spec_checksum).await
    }

    async fn p2p_availability(
        &self,
        snapshot_id: &str,
        spec_checksum: &str,
    ) -> Result<P2PAvailabilityResult, SnapshotControlError> {
        VmService::p2p_availability(self, snapshot_id, spec_checksum).await
    }

    async fn p2p_read_range(
        &self,
        snapshot_id: &str,
        request: &P2PReadRangeRequest,
    ) -> Result<Vec<u8>, SnapshotControlError> {
        VmService::p2p_read_range(self, snapshot_id, request).await
    }
}

pub struct P2pService<A: P2pApi> {
    api: Arc<A>,
    metrics: Option<Arc<SnapdMetrics>>,
}

impl<A: P2pApi> P2pService<A> {
    pub fn new(api: Arc<A>, metrics: Option<Arc<SnapdMetrics>>) -> Arc<Self> {
        Arc::new(Self { api, metrics })
    }

    pub async fn serve_connection(&self, mut stream: TcpStream) -> io::Result<()> {
        let mut magic = [0_u8; P2P_TCP_MAGIC.len()];
        stream.read_exact(&mut magic).await?;
        if magic != *P2P_TCP_MAGIC {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "invalid P2P TCP protocol preface",
            ));
        }
        let opening = read_tcp_frame::<_, P2PTcpRequest>(&mut stream).await?;
        let (request_id, deadline_unix_ms, protocol_version) = match opening {
            P2PTcpRequest::Open {
                protocol_version,
                request_id,
                deadline_unix_ms,
            } if protocol_version == P2P_PROTOCOL_VERSION
                || (protocol_version == P2P_NATIVE_PROTOCOL_VERSION
                    && self.api.supports_native_p2p()) =>
            {
                (request_id, deadline_unix_ms, protocol_version)
            }
            P2PTcpRequest::Open { request_id, .. } => {
                write_p2p_tcp_error(
                    &mut stream,
                    request_id,
                    P2PTcpErrorCode::InvalidArgument,
                    false,
                    "unsupported P2P TCP protocol version",
                )
                .await?;
                return Ok(());
            }
            _ => {
                write_p2p_tcp_error(
                    &mut stream,
                    "unknown".into(),
                    P2PTcpErrorCode::InvalidArgument,
                    false,
                    "P2P TCP open must be the first frame",
                )
                .await?;
                return Ok(());
            }
        };
        if let Err(error) = validate_p2p_tcp_metadata(&request_id, deadline_unix_ms) {
            write_p2p_boundary_error(&mut stream, request_id, error).await?;
            return Ok(());
        }
        write_tcp_frame(
            &mut stream,
            &P2PTcpResponse::Ready {
                request_id,
                protocol_version,
            },
        )
        .await?;
        if let Some(metrics) = &self.metrics {
            metrics.emit_structured(
                StructuredEvent::new(
                    StructuredLogLevel::Info,
                    StructuredEventName::P2PProbeCompleted,
                    StructuredComponent::P2P,
                    StructuredOutcome::Success,
                )
                .operation("p2p_tcp_ready"),
            );
        }

        let mut selected_snapshot = None::<(String, String)>;
        let mut selected_native = None::<(String, String)>;
        loop {
            let request = match read_tcp_frame::<_, P2PTcpRequest>(&mut stream).await {
                Ok(request) => request,
                Err(error) if error.kind() == io::ErrorKind::UnexpectedEof => return Ok(()),
                Err(error) => return Err(error),
            };
            match request {
                P2PTcpRequest::NativeMetadata {
                    request_id,
                    deadline_unix_ms,
                    snapshot_id,
                    generation_id,
                    cursor,
                } => {
                    if let Err(error) = validate_p2p_tcp_metadata(&request_id, deadline_unix_ms) {
                        write_p2p_boundary_error(&mut stream, request_id, error).await?;
                        continue;
                    }
                    if protocol_version != P2P_NATIVE_PROTOCOL_VERSION {
                        write_p2p_tcp_error(
                            &mut stream,
                            request_id,
                            P2PTcpErrorCode::InvalidArgument,
                            false,
                            "native operations require negotiated P2P v2",
                        )
                        .await?;
                        continue;
                    }
                    match self
                        .api
                        .p2p_native_metadata(&snapshot_id, generation_id.as_deref())
                        .await
                    {
                        Ok(mut metadata) => {
                            let total_layers = metadata.layers.len();
                            if cursor as usize >= total_layers
                                && !(cursor == 0 && total_layers == 0)
                            {
                                write_p2p_tcp_error(
                                    &mut stream,
                                    request_id,
                                    P2PTcpErrorCode::InvalidArgument,
                                    false,
                                    "native metadata cursor is outside its descriptor set",
                                )
                                .await?;
                                continue;
                            }
                            selected_native = Some((
                                metadata.snapshot_id.clone(),
                                metadata.generation_id.clone(),
                            ));
                            metadata.layers=metadata.layers.into_iter().skip(cursor as usize)
                                .take(crate::snapshot::transport::p2p::native_wire::NATIVE_METADATA_PAGE_LAYERS).collect();
                            write_tcp_frame(
                                &mut stream,
                                &P2PTcpResponse::NativeMetadata {
                                    request_id,
                                    cursor,
                                    total_layers: total_layers as u32,
                                    metadata,
                                },
                            )
                            .await?;
                        }
                        Err(error) => {
                            write_p2p_control_error(&mut stream, request_id, error).await?
                        }
                    }
                }
                P2PTcpRequest::NativeCoverage {
                    request_id,
                    deadline_unix_ms,
                    snapshot_id,
                    request,
                } => {
                    if let Err(error) = validate_p2p_tcp_metadata(&request_id, deadline_unix_ms) {
                        write_p2p_boundary_error(&mut stream, request_id, error).await?;
                        continue;
                    }
                    if protocol_version != P2P_NATIVE_PROTOCOL_VERSION
                        || !native_scope_matches(
                            &selected_native,
                            &snapshot_id,
                            &request.generation_id,
                        )
                    {
                        write_p2p_tcp_error(
                            &mut stream,
                            request_id,
                            P2PTcpErrorCode::NotFound,
                            false,
                            "native coverage requires matching v2 metadata discovery",
                        )
                        .await?;
                        continue;
                    }
                    match self.api.p2p_native_coverage(&snapshot_id, &request).await {
                        Ok(coverage) => {
                            write_tcp_frame(
                                &mut stream,
                                &P2PTcpResponse::NativeCoverage {
                                    request_id,
                                    coverage,
                                },
                            )
                            .await?
                        }
                        Err(error) => {
                            write_p2p_control_error(&mut stream, request_id, error).await?
                        }
                    }
                }
                P2PTcpRequest::NativeReadRange {
                    request_id,
                    deadline_unix_ms,
                    snapshot_id,
                    request,
                } => {
                    if let Err(error) = validate_p2p_tcp_metadata(&request_id, deadline_unix_ms) {
                        write_p2p_boundary_error(&mut stream, request_id, error).await?;
                        continue;
                    }
                    if protocol_version != P2P_NATIVE_PROTOCOL_VERSION
                        || !native_scope_matches(
                            &selected_native,
                            &snapshot_id,
                            &request.generation_id,
                        )
                    {
                        write_p2p_tcp_error(
                            &mut stream,
                            request_id,
                            P2PTcpErrorCode::NotFound,
                            false,
                            "native read requires matching v2 metadata discovery",
                        )
                        .await?;
                        continue;
                    }
                    match self.api.p2p_native_read_range(&snapshot_id, &request).await {
                        Ok(bytes) if bytes.len() as u64 == request.length => {
                            write_tcp_frame(
                                &mut stream,
                                &P2PTcpResponse::Range {
                                    request_id,
                                    length: request.length,
                                    range_sha256: range_sha256(&bytes),
                                },
                            )
                            .await?;
                            stream.write_all(&bytes).await?;
                        }
                        Ok(_) => {
                            write_p2p_tcp_error(
                                &mut stream,
                                request_id,
                                P2PTcpErrorCode::Internal,
                                false,
                                "native source returned an invalid range length",
                            )
                            .await?
                        }
                        Err(error) => {
                            write_p2p_control_error(&mut stream, request_id, error).await?
                        }
                    }
                }
                P2PTcpRequest::Open { request_id, .. } => {
                    write_p2p_tcp_error(
                        &mut stream,
                        request_id,
                        P2PTcpErrorCode::InvalidArgument,
                        false,
                        "P2P TCP connection is already open",
                    )
                    .await?;
                    return Ok(());
                }
                P2PTcpRequest::Availability {
                    request_id,
                    deadline_unix_ms,
                    snapshot_id,
                    spec_checksum,
                    selection_only,
                } => {
                    if let Err(error) = validate_p2p_tcp_metadata(&request_id, deadline_unix_ms) {
                        write_p2p_boundary_error(&mut stream, request_id, error).await?;
                        continue;
                    }
                    let result = if selection_only {
                        self.api
                            .p2p_selection_availability(&snapshot_id, &spec_checksum)
                            .await
                    } else {
                        self.api
                            .p2p_availability(&snapshot_id, &spec_checksum)
                            .await
                    };
                    match result {
                        Ok(availability) => {
                            if availability.available {
                                selected_snapshot = Some((
                                    availability.snapshot_id.clone(),
                                    availability.spec_checksum.clone(),
                                ));
                            } else {
                                selected_snapshot = None;
                            }
                            write_tcp_frame(
                                &mut stream,
                                &P2PTcpResponse::Availability {
                                    request_id,
                                    availability,
                                },
                            )
                            .await?;
                        }
                        Err(error) => {
                            write_p2p_control_error(&mut stream, request_id, error).await?;
                        }
                    }
                }
                P2PTcpRequest::ReadRange {
                    request_id,
                    deadline_unix_ms,
                    snapshot_id,
                    request,
                } => {
                    if let Err(error) = validate_p2p_tcp_metadata(&request_id, deadline_unix_ms) {
                        write_p2p_boundary_error(&mut stream, request_id, error).await?;
                        continue;
                    }
                    if !selected_snapshot
                        .as_ref()
                        .is_some_and(|(selected_id, checksum)| {
                            selected_id == &snapshot_id && checksum == &request.spec_checksum
                        })
                    {
                        write_p2p_tcp_error(
                            &mut stream,
                            request_id,
                            P2PTcpErrorCode::NotFound,
                            false,
                            "P2P TCP range requires an available snapshot probe on this connection",
                        )
                        .await?;
                        continue;
                    }
                    match self.api.p2p_read_range(&snapshot_id, &request).await {
                        Ok(payload) if payload.len() as u64 == request.length => {
                            let range_sha256 = range_sha256(&payload);
                            write_tcp_frame(
                                &mut stream,
                                &P2PTcpResponse::Range {
                                    request_id,
                                    length: payload.len() as u64,
                                    range_sha256,
                                },
                            )
                            .await?;
                            stream.write_all(&payload).await?;
                        }
                        Ok(_) => {
                            write_p2p_tcp_error(
                                &mut stream,
                                request_id,
                                P2PTcpErrorCode::Internal,
                                false,
                                "P2P source returned a range with invalid length",
                            )
                            .await?;
                        }
                        Err(error) => {
                            write_p2p_control_error(&mut stream, request_id, error).await?;
                        }
                    }
                }
            }
        }
    }
}

// Keep error text wire-compatible with the original combined endpoint.
fn native_scope_matches(
    selected: &Option<(String, String)>,
    snapshot_id: &str,
    generation_id: &str,
) -> bool {
    selected.as_ref().is_some_and(|(selected_id, generation)| {
        selected_id == snapshot_id && generation == generation_id
    })
}

#[derive(Debug, Error)]
enum P2pBoundaryError {
    #[error("invalid HTTP request: {0}")]
    Invalid(String),
    #[error("request deadline has expired")]
    DeadlineExpired,
}

fn validate_p2p_tcp_metadata(
    request_id: &str,
    deadline_unix_ms: u64,
) -> Result<(), P2pBoundaryError> {
    if !valid_request_id(request_id) {
        return Err(P2pBoundaryError::Invalid("request ID is invalid".into()));
    }
    if deadline_unix_ms <= now_unix_ms() {
        return Err(P2pBoundaryError::DeadlineExpired);
    }
    Ok(())
}

async fn write_p2p_boundary_error(
    stream: &mut TcpStream,
    request_id: String,
    error: P2pBoundaryError,
) -> io::Result<()> {
    let code = match &error {
        P2pBoundaryError::DeadlineExpired => P2PTcpErrorCode::DeadlineExceeded,
        _ => P2PTcpErrorCode::InvalidArgument,
    };
    write_p2p_tcp_error(stream, request_id, code, false, &error.to_string()).await
}

async fn write_p2p_control_error(
    stream: &mut TcpStream,
    request_id: String,
    error: SnapshotControlError,
) -> io::Result<()> {
    let code = match &error {
        SnapshotControlError::Invalid(_) => P2PTcpErrorCode::InvalidArgument,
        SnapshotControlError::NotFound(_) => P2PTcpErrorCode::NotFound,
        SnapshotControlError::IdentityConflict(_)
        | SnapshotControlError::RetryableIdentityConflict(_)
        | SnapshotControlError::InvalidState(_)
        | SnapshotControlError::UploadDisabled
        | SnapshotControlError::RetryableInvalidState(_) => P2PTcpErrorCode::IdentityConflict,
        SnapshotControlError::Deadline(_) => P2PTcpErrorCode::DeadlineExceeded,
        SnapshotControlError::Runtime(_) => P2PTcpErrorCode::Internal,
    };
    write_p2p_tcp_error(
        stream,
        request_id,
        code,
        error.retryable(),
        &error.to_string(),
    )
    .await
}

async fn write_p2p_tcp_error(
    stream: &mut TcpStream,
    request_id: String,
    code: P2PTcpErrorCode,
    retryable: bool,
    message: &str,
) -> io::Result<()> {
    let request_id = if valid_request_id(&request_id) {
        request_id
    } else {
        "unknown".into()
    };
    let message = message.chars().take(1024).collect();
    write_tcp_frame(
        stream,
        &P2PTcpResponse::Error {
            request_id,
            code,
            retryable,
            message,
        },
    )
    .await
}

fn valid_request_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= MAX_REQUEST_ID_BYTES
        && value.bytes().all(|byte| byte.is_ascii_graphic())
}

fn now_unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .try_into()
        .unwrap_or(u64::MAX)
}

#[cfg(test)]
mod tests {
    use super::*;

    use std::sync::atomic::{AtomicUsize, Ordering};
    use tokio::net::TcpListener;

    #[derive(Default)]
    struct PeerApi {
        reads: AtomicUsize,
        selections: AtomicUsize,
    }

    #[async_trait]
    impl P2pApi for PeerApi {
        async fn p2p_availability(
            &self,
            snapshot_id: &str,
            spec_checksum: &str,
        ) -> Result<P2PAvailabilityResult, SnapshotControlError> {
            Ok(P2PAvailabilityResult {
                snapshot_id: snapshot_id.into(),
                spec_checksum: spec_checksum.into(),
                available: true,
                complete: false,
                servable_objects: 1,
            })
        }

        async fn p2p_selection_availability(
            &self,
            snapshot_id: &str,
            spec_checksum: &str,
        ) -> Result<P2PAvailabilityResult, SnapshotControlError> {
            self.selections.fetch_add(1, Ordering::SeqCst);
            self.p2p_availability(snapshot_id, spec_checksum).await
        }

        async fn p2p_read_range(
            &self,
            _snapshot_id: &str,
            request: &P2PReadRangeRequest,
        ) -> Result<Vec<u8>, SnapshotControlError> {
            assert_eq!((request.offset, request.length), (4096, 4));
            self.reads.fetch_add(1, Ordering::SeqCst);
            Ok(vec![1, 2, 3, 4])
        }
    }

    async fn connection(api: Arc<PeerApi>) -> (TcpStream, tokio::task::JoinHandle<io::Result<()>>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let client = TcpStream::connect(listener.local_addr().unwrap())
            .await
            .unwrap();
        let (server, _) = listener.accept().await.unwrap();
        let service = P2pService::new(api, None);
        let task = tokio::spawn(async move { service.serve_connection(server).await });
        (client, task)
    }

    fn range(spec_checksum: &str) -> P2PTcpRequest {
        P2PTcpRequest::ReadRange {
            request_id: "range".into(),
            deadline_unix_ms: u64::MAX,
            snapshot_id: "snapshot".into(),
            request: P2PReadRangeRequest {
                spec_checksum: spec_checksum.into(),
                object_key: "logical.pack".into(),
                object_sha256: "sha256:source".into(),
                object_version: None,
                offset: 4096,
                length: 4,
            },
        }
    }

    #[tokio::test]
    async fn standalone_peer_handler_preserves_probe_binding_before_serving_data() {
        let api = Arc::new(PeerApi::default());
        let (mut client, task) = connection(Arc::clone(&api)).await;
        client.write_all(P2P_TCP_MAGIC).await.unwrap();
        write_tcp_frame(
            &mut client,
            &P2PTcpRequest::Open {
                request_id: "open".into(),
                protocol_version: 1,
                deadline_unix_ms: u64::MAX,
            },
        )
        .await
        .unwrap();
        assert!(matches!(
            read_tcp_frame::<_, P2PTcpResponse>(&mut client)
                .await
                .unwrap(),
            P2PTcpResponse::Ready {
                protocol_version: 1,
                ..
            }
        ));
        write_tcp_frame(&mut client, &range("spec")).await.unwrap();
        assert!(matches!(
            read_tcp_frame::<_, P2PTcpResponse>(&mut client)
                .await
                .unwrap(),
            P2PTcpResponse::Error {
                code: P2PTcpErrorCode::NotFound,
                ..
            }
        ));
        write_tcp_frame(
            &mut client,
            &P2PTcpRequest::Availability {
                request_id: "probe".into(),
                deadline_unix_ms: u64::MAX,
                snapshot_id: "snapshot".into(),
                spec_checksum: "spec".into(),
                selection_only: true,
            },
        )
        .await
        .unwrap();
        assert!(matches!(
            read_tcp_frame::<_, P2PTcpResponse>(&mut client).await.unwrap(),
            P2PTcpResponse::Availability { availability, .. }
                if availability.available && availability.spec_checksum == "spec"
        ));
        write_tcp_frame(&mut client, &range("different-spec"))
            .await
            .unwrap();
        assert!(matches!(
            read_tcp_frame::<_, P2PTcpResponse>(&mut client)
                .await
                .unwrap(),
            P2PTcpResponse::Error {
                code: P2PTcpErrorCode::NotFound,
                ..
            }
        ));
        write_tcp_frame(&mut client, &range("spec")).await.unwrap();
        assert!(matches!(
            read_tcp_frame::<_, P2PTcpResponse>(&mut client).await.unwrap(),
            P2PTcpResponse::Range { length: 4, range_sha256: checksum, .. }
                if checksum == range_sha256(&[1, 2, 3, 4])
        ));
        let mut data = [0_u8; 4];
        client.read_exact(&mut data).await.unwrap();
        assert_eq!(data, [1, 2, 3, 4]);
        assert_eq!(api.reads.load(Ordering::SeqCst), 1);
        assert_eq!(api.selections.load(Ordering::SeqCst), 1);
        drop(client);
        tokio::time::timeout(std::time::Duration::from_secs(2), task)
            .await
            .unwrap()
            .unwrap()
            .unwrap();
    }

    #[tokio::test]
    async fn standalone_peer_handler_rejects_wrong_version_before_business_calls() {
        let api = Arc::new(PeerApi::default());
        let (mut client, task) = connection(Arc::clone(&api)).await;
        client.write_all(P2P_TCP_MAGIC).await.unwrap();
        write_tcp_frame(
            &mut client,
            &P2PTcpRequest::Open {
                request_id: "bad-version".into(),
                protocol_version: 2,
                deadline_unix_ms: u64::MAX,
            },
        )
        .await
        .unwrap();
        assert!(matches!(
            read_tcp_frame::<_, P2PTcpResponse>(&mut client).await.unwrap(),
            P2PTcpResponse::Error {
                request_id, code: P2PTcpErrorCode::InvalidArgument, retryable: false, message
            } if request_id == "bad-version" && message == "unsupported P2P TCP protocol version"
        ));
        assert_eq!(api.reads.load(Ordering::SeqCst), 0);
        assert_eq!(api.selections.load(Ordering::SeqCst), 0);
        tokio::time::timeout(std::time::Duration::from_secs(2), task)
            .await
            .unwrap()
            .unwrap()
            .unwrap();
    }

    #[test]
    fn peer_metadata_validation_retains_existing_error_contract() {
        assert_eq!(P2P_PROTOCOL_VERSION, 1);
        assert_eq!(
            validate_p2p_tcp_metadata("bad id", u64::MAX)
                .unwrap_err()
                .to_string(),
            "invalid HTTP request: request ID is invalid"
        );
        assert_eq!(
            validate_p2p_tcp_metadata("request", 0)
                .unwrap_err()
                .to_string(),
            "request deadline has expired"
        );
    }
}
