//! Native-generation P2P v2 client. This transport has no origin reader or
//! fallback path. Range SHA checks protect transport bytes, not content identity.

use std::io;
use std::net::SocketAddr;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use thiserror::Error;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use uuid::Uuid;

use super::native::{
    NativeCoverageRequest, NativeCoverageResult, NativeLayerMetadata, NativeReadRangeRequest,
    NativeSnapshotMetadata, MAX_NATIVE_COVERAGE_UNITS, NATIVE_P2P_METADATA_VERSION,
};
use super::native_wire::{
    validate_coverage, MAX_NATIVE_METADATA_LAYERS, NATIVE_METADATA_PAGE_LAYERS,
};
use super::protocol::{
    P2PTcpErrorCode, P2PTcpRequest, P2PTcpResponse, P2P_NATIVE_PROTOCOL_VERSION, P2P_TCP_MAGIC,
};
use super::tcp::{read_tcp_frame, write_tcp_frame};
use crate::snapshot::metadata::{sha256_identity, LayerKind};

const MAX_READ_BYTES: u64 = 16 * 1024 * 1024;

#[derive(Debug, Error)]
pub enum NativeP2PError {
    #[error("Native peer miss: {0}")]
    Miss(String),
    #[error("Native peer protocol is unsupported: {0}")]
    Unsupported(String),
    #[error("Native peer identity differs: {0}")]
    Identity(String),
    #[error("Native peer transport failed: {0}")]
    Transport(String),
    #[error("Native peer integrity check failed: {0}")]
    Integrity(String),
}

/// One strictly negotiated native-generation session. A failed or cancelled
/// exchange consumes its stream so later calls cannot parse leftover payload.
#[derive(Debug)]
pub struct NativeP2PClient {
    stream: Option<TcpStream>,
    timeout: Duration,
    metadata: NativeSnapshotMetadata,
}

impl NativeP2PClient {
    /// Bootstrap all bounded descriptor pages over v2 only. The optional source
    /// generation pins a known LOCAL_READY incarnation; no v1 probe is sent.
    pub async fn connect(
        peer: SocketAddr,
        timeout: Duration,
        snapshot_id: impl Into<String>,
        expected_generation: Option<&str>,
    ) -> Result<(Self, NativeSnapshotMetadata), NativeP2PError> {
        let snapshot_id = snapshot_id.into();
        if timeout.is_zero() {
            return Err(NativeP2PError::Transport("timeout must be positive".into()));
        }
        if snapshot_id.is_empty() || snapshot_id.len() > 4096 {
            return Err(NativeP2PError::Identity("snapshot ID is invalid".into()));
        }
        if expected_generation.is_some_and(|id| Uuid::parse_str(id).is_err()) {
            return Err(NativeP2PError::Identity("generation is not a UUID".into()));
        }
        let (stream, metadata) = tokio::time::timeout(timeout, async {
            let mut stream = TcpStream::connect(peer).await.map_err(wire_io)?;
            stream.set_nodelay(true).map_err(wire_io)?;
            stream.write_all(P2P_TCP_MAGIC).await.map_err(wire_io)?;
            let request_id = request_id();
            let response = exchange(
                &mut stream,
                P2PTcpRequest::Open {
                    protocol_version: P2P_NATIVE_PROTOCOL_VERSION,
                    request_id: request_id.clone(),
                    deadline_unix_ms: deadline(timeout),
                },
                &request_id,
            )
            .await?;
            match response {
                P2PTcpResponse::Ready {
                    protocol_version, ..
                } if protocol_version == P2P_NATIVE_PROTOCOL_VERSION => {}
                P2PTcpResponse::Ready {
                    protocol_version, ..
                } => {
                    return Err(NativeP2PError::Unsupported(format!(
                        "expected v2, peer selected v{protocol_version}"
                    )));
                }
                _ => return Err(NativeP2PError::Identity("expected a v2 Ready reply".into())),
            }
            let metadata =
                bootstrap(&mut stream, timeout, &snapshot_id, expected_generation).await?;
            Ok((stream, metadata))
        })
        .await
        .map_err(|_| NativeP2PError::Transport("bootstrap deadline expired".into()))??;
        Ok((
            Self {
                stream: Some(stream),
                timeout,
                metadata: metadata.clone(),
            },
            metadata,
        ))
    }

    pub async fn coverage(
        &mut self,
        request: &NativeCoverageRequest,
    ) -> Result<NativeCoverageResult, NativeP2PError> {
        let unit = self
            .layer(
                &request.generation_id,
                (
                    request.kind,
                    request.ordinal,
                    &request.layer_id,
                    &request.layer_generation_id,
                ),
                (request.offset, request.length),
            )?
            .coverage_unit_size;
        if !request.offset.is_multiple_of(unit)
            || !request.length.is_multiple_of(unit)
            || request.length / unit > MAX_NATIVE_COVERAGE_UNITS
        {
            return Err(NativeP2PError::Identity(
                "coverage geometry is outside its bounded units".into(),
            ));
        }
        let mut stream = self.take_stream()?;
        let result = tokio::time::timeout(self.timeout, async {
            let request_id = request_id();
            match exchange(
                &mut stream,
                P2PTcpRequest::NativeCoverage {
                    request_id: request_id.clone(),
                    deadline_unix_ms: deadline(self.timeout),
                    snapshot_id: self.metadata.snapshot_id.clone(),
                    request: request.clone(),
                },
                &request_id,
            )
            .await?
            {
                P2PTcpResponse::NativeCoverage { coverage, .. } => {
                    validate_coverage(&coverage).map_err(wire_io)?;
                    if (coverage.offset, coverage.length, coverage.unit_size)
                        != (request.offset, request.length, unit)
                    {
                        return Err(NativeP2PError::Identity(
                            "coverage reply geometry differs".into(),
                        ));
                    }
                    Ok(coverage)
                }
                _ => Err(NativeP2PError::Identity(
                    "expected a native coverage reply".into(),
                )),
            }
        })
        .await
        .unwrap_or_else(|_| {
            Err(NativeP2PError::Transport(
                "coverage deadline expired".into(),
            ))
        });
        self.finish(stream, &result);
        result
    }

    /// Read raw logical bytes from the selected generation. A returned digest
    /// authenticates this transfer only; it never upgrades a native ID to SHA.
    pub async fn read_range(
        &mut self,
        request: &NativeReadRangeRequest,
    ) -> Result<Vec<u8>, NativeP2PError> {
        self.layer(
            &request.generation_id,
            (
                request.kind,
                request.ordinal,
                &request.layer_id,
                &request.layer_generation_id,
            ),
            (request.offset, request.length),
        )?;
        if request.length > MAX_READ_BYTES {
            return Err(NativeP2PError::Identity(
                "read exceeds the 16 MiB bound".into(),
            ));
        }
        let mut stream = self.take_stream()?;
        let result = tokio::time::timeout(self.timeout, async {
            let request_id = request_id();
            let response = exchange(
                &mut stream,
                P2PTcpRequest::NativeReadRange {
                    request_id: request_id.clone(),
                    deadline_unix_ms: deadline(self.timeout),
                    snapshot_id: self.metadata.snapshot_id.clone(),
                    request: request.clone(),
                },
                &request_id,
            )
            .await?;
            let P2PTcpResponse::Range {
                length,
                range_sha256,
                ..
            } = response
            else {
                return Err(NativeP2PError::Identity(
                    "expected a native range reply".into(),
                ));
            };
            if length != request.length || length > MAX_READ_BYTES {
                return Err(NativeP2PError::Integrity(
                    "range length differs from request".into(),
                ));
            }
            crate::snapshot::metadata::validate_sha256(&range_sha256)
                .map_err(NativeP2PError::Integrity)?;
            let mut bytes = vec![0; length as usize];
            stream.read_exact(&mut bytes).await.map_err(wire_io)?;
            if sha256_identity(&bytes) != range_sha256 {
                return Err(NativeP2PError::Integrity(
                    "range transfer checksum differs".into(),
                ));
            }
            Ok(bytes)
        })
        .await
        .unwrap_or_else(|_| Err(NativeP2PError::Transport("read deadline expired".into())));
        self.finish(stream, &result);
        result
    }

    fn layer(
        &self,
        generation: &str,
        (kind, ordinal, layer_id, layer_generation): (LayerKind, u32, &str, &str),
        (offset, length): (u64, u64),
    ) -> Result<&NativeLayerMetadata, NativeP2PError> {
        let layer = self.metadata.layers.iter().find(|layer| {
            layer.kind == kind
                && layer.ordinal == ordinal
                && layer.layer_id == layer_id
                && layer.layer_generation_id == layer_generation
        });
        if generation != self.metadata.generation_id {
            return Err(NativeP2PError::Identity(
                "snapshot generation differs".into(),
            ));
        }
        layer
            .filter(|layer| {
                length != 0
                    && offset
                        .checked_add(length)
                        .is_some_and(|end| end <= layer.logical_bytes)
            })
            .ok_or_else(|| {
                NativeP2PError::Identity("layer identity or logical range differs".into())
            })
    }

    fn take_stream(&mut self) -> Result<TcpStream, NativeP2PError> {
        self.stream
            .take()
            .ok_or_else(|| NativeP2PError::Transport("native peer session is closed".into()))
    }

    fn finish<T>(&mut self, stream: TcpStream, result: &Result<T, NativeP2PError>) {
        if result.is_ok() || matches!(result, Err(NativeP2PError::Miss(_))) {
            self.stream = Some(stream);
        }
    }
}

async fn bootstrap(
    stream: &mut TcpStream,
    timeout: Duration,
    snapshot_id: &str,
    expected_generation: Option<&str>,
) -> Result<NativeSnapshotMetadata, NativeP2PError> {
    let mut assembled: Option<NativeSnapshotMetadata> = None;
    let mut cursor = 0_u32;
    let mut expected_total = None;
    loop {
        let request_id = request_id();
        let response = exchange(
            stream,
            P2PTcpRequest::NativeMetadata {
                request_id: request_id.clone(),
                deadline_unix_ms: deadline(timeout),
                snapshot_id: snapshot_id.into(),
                generation_id: assembled
                    .as_ref()
                    .map(|m| m.generation_id.clone())
                    .or_else(|| expected_generation.map(str::to_owned)),
                cursor,
            },
            &request_id,
        )
        .await?;
        let P2PTcpResponse::NativeMetadata {
            cursor: received_cursor,
            total_layers,
            mut metadata,
            ..
        } = response
        else {
            return Err(NativeP2PError::Identity(
                "expected a native metadata page".into(),
            ));
        };
        if received_cursor != cursor
            || total_layers == 0
            || total_layers as usize > MAX_NATIVE_METADATA_LAYERS
            || expected_total.is_some_and(|total| total != total_layers)
            || metadata.layers.is_empty()
            || metadata.layers.len() > NATIVE_METADATA_PAGE_LAYERS
            || u64::from(cursor) + metadata.layers.len() as u64 > u64::from(total_layers)
        {
            return Err(NativeP2PError::Identity(
                "metadata page cursor or total differs".into(),
            ));
        }
        if metadata.snapshot_id != snapshot_id
            || metadata.metadata_version != NATIVE_P2P_METADATA_VERSION
            || Uuid::parse_str(&metadata.generation_id).is_err()
            || expected_generation.is_some_and(|expected| expected != metadata.generation_id)
        {
            return Err(NativeP2PError::Identity(
                "metadata source generation differs".into(),
            ));
        }
        if let Some(previous) = &assembled {
            if previous.snapshot_kind != metadata.snapshot_kind
                || previous.generation_id != metadata.generation_id
                || previous.metadata_sha256 != metadata.metadata_sha256
            {
                return Err(NativeP2PError::Identity(
                    "metadata page header changed".into(),
                ));
            }
        }
        cursor += metadata.layers.len() as u32;
        expected_total = Some(total_layers);
        if let Some(previous) = &mut assembled {
            previous.layers.append(&mut metadata.layers);
        } else {
            assembled = Some(metadata);
        }
        if cursor == total_layers {
            let metadata = assembled.expect("a nonempty page was received");
            metadata.validate().map_err(NativeP2PError::Integrity)?;
            return Ok(metadata);
        }
    }
}

async fn exchange(
    stream: &mut TcpStream,
    request: P2PTcpRequest,
    request_id: &str,
) -> Result<P2PTcpResponse, NativeP2PError> {
    write_tcp_frame(stream, &request).await.map_err(wire_io)?;
    let response = read_tcp_frame::<_, P2PTcpResponse>(stream)
        .await
        .map_err(wire_io)?;
    let response_id = match &response {
        P2PTcpResponse::Ready { request_id, .. }
        | P2PTcpResponse::Availability { request_id, .. }
        | P2PTcpResponse::Range { request_id, .. }
        | P2PTcpResponse::Error { request_id, .. }
        | P2PTcpResponse::NativeMetadata { request_id, .. }
        | P2PTcpResponse::NativeCoverage { request_id, .. } => request_id,
    };
    if response_id != request_id {
        return Err(NativeP2PError::Identity("reply correlation differs".into()));
    }
    match response {
        P2PTcpResponse::Error { code, message, .. } => Err(match code {
            P2PTcpErrorCode::NotFound => NativeP2PError::Miss(message),
            P2PTcpErrorCode::InvalidArgument => NativeP2PError::Unsupported(message),
            P2PTcpErrorCode::IdentityConflict => NativeP2PError::Identity(message),
            P2PTcpErrorCode::DeadlineExceeded | P2PTcpErrorCode::Internal => {
                NativeP2PError::Transport(message)
            }
        }),
        response => Ok(response),
    }
}

fn wire_io(error: io::Error) -> NativeP2PError {
    if error.kind() == io::ErrorKind::InvalidData {
        NativeP2PError::Integrity(error.to_string())
    } else {
        NativeP2PError::Transport(error.to_string())
    }
}

fn request_id() -> String {
    Uuid::new_v4().to_string()
}

fn deadline(timeout: Duration) -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .saturating_add(timeout.as_millis())
        .min(u128::from(u64::MAX)) as u64
}

#[cfg(test)]
mod tests {
    use std::future::Future;

    use super::super::native::NativeLayerIdentityKind;
    use super::super::protocol::{P2PBinaryFrame, P2P_TCP_MAX_FRAME_BYTES};
    use super::*;
    use crate::snapshot::metadata::SnapshotKind;
    use tokio::net::TcpListener;
    use tokio::task::JoinHandle;

    const TIMEOUT: Duration = Duration::from_secs(1);

    fn metadata(count: u32) -> NativeSnapshotMetadata {
        let mut metadata = NativeSnapshotMetadata {
            metadata_version: NATIVE_P2P_METADATA_VERSION,
            snapshot_id: "local-ready-source".into(),
            snapshot_kind: SnapshotKind::Full,
            generation_id: Uuid::new_v4().to_string(),
            metadata_sha256: String::new(),
            layers: (0..count)
                .map(|ordinal| {
                    let generation = Uuid::new_v4().to_string();
                    NativeLayerMetadata {
                        kind: LayerKind::Memory,
                        ordinal,
                        layer_id: format!("memory-{ordinal}"),
                        layer_generation_id: generation.clone(),
                        identity_kind: NativeLayerIdentityKind::NativeGeneration,
                        content_identity: format!("native-generation:{generation}"),
                        logical_bytes: 32 * 1024 * 1024,
                        coverage_unit_size: 4096,
                    }
                })
                .collect(),
        };
        metadata.metadata_sha256 = metadata.descriptor_checksum().unwrap();
        metadata
    }

    fn read_request(metadata: &NativeSnapshotMetadata) -> NativeReadRangeRequest {
        let layer = &metadata.layers[0];
        NativeReadRangeRequest {
            generation_id: metadata.generation_id.clone(),
            kind: layer.kind,
            ordinal: layer.ordinal,
            layer_id: layer.layer_id.clone(),
            layer_generation_id: layer.layer_generation_id.clone(),
            offset: 0,
            length: 4096,
        }
    }

    fn coverage_request(metadata: &NativeSnapshotMetadata) -> NativeCoverageRequest {
        let read = read_request(metadata);
        NativeCoverageRequest {
            generation_id: read.generation_id,
            kind: read.kind,
            ordinal: read.ordinal,
            layer_id: read.layer_id,
            layer_generation_id: read.layer_generation_id,
            offset: read.offset,
            length: read.length,
        }
    }

    async fn peer<F, Fut>(handler: F) -> (SocketAddr, JoinHandle<()>)
    where
        F: FnOnce(TcpStream) -> Fut + Send + 'static,
        Fut: Future<Output = ()> + Send + 'static,
    {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let task = tokio::spawn(async move {
            tokio::time::timeout(Duration::from_secs(3), async move {
                let (stream, _) = listener.accept().await.unwrap();
                handler(stream).await;
            })
            .await
            .unwrap();
        });
        (address, task)
    }

    async fn open_request(stream: &mut TcpStream) -> String {
        let mut magic = [0; 8];
        stream.read_exact(&mut magic).await.unwrap();
        assert_eq!(&magic, P2P_TCP_MAGIC);
        let P2PTcpRequest::Open {
            protocol_version,
            request_id,
            deadline_unix_ms,
        } = read_tcp_frame(stream).await.unwrap()
        else {
            panic!("expected Open");
        };
        assert_eq!(protocol_version, P2P_NATIVE_PROTOCOL_VERSION);
        assert!(deadline_unix_ms > 0);
        request_id
    }

    async fn open(stream: &mut TcpStream) {
        let request_id = open_request(stream).await;
        write_tcp_frame(
            stream,
            &P2PTcpResponse::Ready {
                request_id,
                protocol_version: P2P_NATIVE_PROTOCOL_VERSION,
            },
        )
        .await
        .unwrap();
    }

    async fn metadata_request(stream: &mut TcpStream) -> (String, u32, Option<String>) {
        let P2PTcpRequest::NativeMetadata {
            request_id,
            cursor,
            generation_id,
            ..
        } = read_tcp_frame(stream).await.unwrap()
        else {
            panic!("expected native metadata");
        };
        (request_id, cursor, generation_id)
    }

    async fn page(
        stream: &mut TcpStream,
        metadata: &NativeSnapshotMetadata,
        request_id: String,
        cursor: u32,
        total: u32,
    ) {
        let mut selected = metadata.clone();
        selected.layers = metadata
            .layers
            .iter()
            .skip(cursor as usize)
            .take(NATIVE_METADATA_PAGE_LAYERS)
            .cloned()
            .collect();
        write_tcp_frame(
            stream,
            &P2PTcpResponse::NativeMetadata {
                request_id,
                cursor,
                total_layers: total,
                metadata: selected,
            },
        )
        .await
        .unwrap();
    }

    async fn bootstrap_peer(stream: &mut TcpStream, metadata: &NativeSnapshotMetadata) {
        open(stream).await;
        let mut cursor = 0;
        while cursor < metadata.layers.len() as u32 {
            let (request_id, requested, generation) = metadata_request(stream).await;
            assert_eq!(requested, cursor);
            if cursor > 0 {
                assert_eq!(generation.as_deref(), Some(metadata.generation_id.as_str()));
            }
            page(
                stream,
                metadata,
                request_id,
                cursor,
                metadata.layers.len() as u32,
            )
            .await;
            cursor =
                (cursor + NATIVE_METADATA_PAGE_LAYERS as u32).min(metadata.layers.len() as u32);
        }
    }

    #[tokio::test]
    async fn pages_native_descriptors_and_reads_after_an_explicit_miss_without_fallback() {
        let expected = metadata(5);
        let source = expected.clone();
        let (address, task) = peer(move |mut stream| async move {
            bootstrap_peer(&mut stream, &source).await;
            let P2PTcpRequest::NativeCoverage {
                request_id,
                request,
                ..
            } = read_tcp_frame(&mut stream).await.unwrap()
            else {
                panic!("expected native coverage");
            };
            write_tcp_frame(
                &mut stream,
                &P2PTcpResponse::NativeCoverage {
                    request_id,
                    coverage: NativeCoverageResult {
                        offset: request.offset,
                        length: request.length,
                        unit_size: 4096,
                        packed_states: vec![1],
                    },
                },
            )
            .await
            .unwrap();
            let P2PTcpRequest::NativeReadRange { request_id, .. } =
                read_tcp_frame(&mut stream).await.unwrap()
            else {
                panic!("expected native read");
            };
            write_tcp_frame(
                &mut stream,
                &P2PTcpResponse::Error {
                    request_id,
                    code: P2PTcpErrorCode::NotFound,
                    retryable: false,
                    message: "INHERIT has no native source".into(),
                },
            )
            .await
            .unwrap();
            // The next request is an explicit caller retry on this v2 session,
            // never an automatic canonical or provider fallback.
            let P2PTcpRequest::NativeReadRange {
                request_id,
                request,
                ..
            } = read_tcp_frame(&mut stream).await.unwrap()
            else {
                panic!("expected caller's native retry");
            };
            let bytes = vec![0x5a; request.length as usize];
            write_tcp_frame(
                &mut stream,
                &P2PTcpResponse::Range {
                    request_id,
                    length: request.length,
                    range_sha256: sha256_identity(&bytes),
                },
            )
            .await
            .unwrap();
            stream.write_all(&bytes).await.unwrap();
        })
        .await;
        let (mut client, received) =
            NativeP2PClient::connect(address, TIMEOUT, &expected.snapshot_id, None)
                .await
                .unwrap();
        assert_eq!(received, expected);
        assert_eq!(
            client
                .coverage(&coverage_request(&received))
                .await
                .unwrap()
                .packed_states,
            vec![1]
        );
        assert!(matches!(
            client.read_range(&read_request(&received)).await,
            Err(NativeP2PError::Miss(_))
        ));
        assert_eq!(
            client.read_range(&read_request(&received)).await.unwrap(),
            vec![0x5a; 4096]
        );
        task.await.unwrap();
    }

    #[tokio::test]
    async fn rejects_v1_ready_without_retrying_a_canonical_session() {
        let (address, task) = peer(|mut stream| async move {
            let request_id = open_request(&mut stream).await;
            write_tcp_frame(
                &mut stream,
                &P2PTcpResponse::Ready {
                    request_id,
                    protocol_version: 1,
                },
            )
            .await
            .unwrap();
            assert_eq!(stream.read(&mut [0; 1]).await.unwrap(), 0);
        })
        .await;
        assert!(matches!(
            NativeP2PClient::connect(address, TIMEOUT, "local-ready-source", None).await,
            Err(NativeP2PError::Unsupported(_))
        ));
        task.await.unwrap();
    }

    #[tokio::test]
    async fn rejects_forged_snapshot_generation_and_descriptor_checksum() {
        for mutation in 0..3 {
            let expected = metadata(1);
            let mut source = expected.clone();
            match mutation {
                0 => source.snapshot_id = "different-snapshot".into(),
                1 => source.generation_id = Uuid::new_v4().to_string(),
                _ => {}
            }
            source.metadata_sha256 = source.descriptor_checksum().unwrap();
            if mutation == 2 {
                source.metadata_sha256 = sha256_identity(b"forged descriptors");
            }
            let (address, task) = peer(move |mut stream| async move {
                open(&mut stream).await;
                let (id, _, _) = metadata_request(&mut stream).await;
                page(&mut stream, &source, id, 0, 1).await;
            })
            .await;
            let result = NativeP2PClient::connect(
                address,
                TIMEOUT,
                &expected.snapshot_id,
                Some(&expected.generation_id),
            )
            .await;
            if mutation == 2 {
                assert!(matches!(result, Err(NativeP2PError::Integrity(_))));
            } else {
                assert!(matches!(result, Err(NativeP2PError::Identity(_))));
            }
            task.await.unwrap();
        }
    }

    #[tokio::test]
    async fn rejects_inconsistent_metadata_page_cursor_total_or_header() {
        for mutation in 0..3 {
            let source = metadata(5);
            let snapshot = source.snapshot_id.clone();
            let (address, task) = peer(move |mut stream| async move {
                open(&mut stream).await;
                let (id, _, _) = metadata_request(&mut stream).await;
                page(&mut stream, &source, id, 0, 5).await;
                let (request_id, cursor, _) = metadata_request(&mut stream).await;
                assert_eq!(cursor, 4);
                let mut changed = source.clone();
                changed.layers = source.layers[4..].to_vec();
                if mutation == 2 {
                    changed.metadata_sha256 = sha256_identity(b"changed page header");
                }
                write_tcp_frame(
                    &mut stream,
                    &P2PTcpResponse::NativeMetadata {
                        request_id,
                        cursor: if mutation == 0 { 3 } else { 4 },
                        total_layers: if mutation == 1 { 6 } else { 5 },
                        metadata: changed,
                    },
                )
                .await
                .unwrap();
            })
            .await;
            assert!(matches!(
                NativeP2PClient::connect(address, TIMEOUT, snapshot, None).await,
                Err(NativeP2PError::Identity(_))
            ));
            task.await.unwrap();
        }
    }

    #[tokio::test]
    async fn rejects_bad_coverage_states_and_geometry() {
        for mutation in 0..2 {
            let source = metadata(1);
            let snapshot = source.snapshot_id.clone();
            let (address, task) = peer(move |mut stream| async move {
                bootstrap_peer(&mut stream, &source).await;
                let P2PTcpRequest::NativeCoverage { request_id, .. } =
                    read_tcp_frame(&mut stream).await.unwrap()
                else {
                    panic!("expected coverage");
                };
                let coverage = if mutation == 0 {
                    NativeCoverageResult {
                        offset: 0,
                        length: 4096,
                        unit_size: 512,
                        packed_states: vec![0; 2],
                    }
                } else {
                    NativeCoverageResult {
                        offset: 0,
                        length: 4096,
                        unit_size: 4096,
                        packed_states: vec![1],
                    }
                };
                let mut bytes = P2PTcpResponse::NativeCoverage {
                    request_id,
                    coverage,
                }
                .to_binary()
                .unwrap();
                if mutation == 1 {
                    *bytes.last_mut().unwrap() = 3;
                }
                stream.write_u32(bytes.len() as u32).await.unwrap();
                stream.write_all(&bytes).await.unwrap();
            })
            .await;
            let (mut client, metadata) = NativeP2PClient::connect(address, TIMEOUT, snapshot, None)
                .await
                .unwrap();
            let result = client.coverage(&coverage_request(&metadata)).await;
            assert!(matches!(
                result,
                Err(NativeP2PError::Identity(_) | NativeP2PError::Integrity(_))
            ));
            assert!(client.stream.is_none());
            task.await.unwrap();
        }
    }

    #[tokio::test]
    async fn rejects_truncated_payload_bad_hash_length_and_correlation() {
        for mutation in 0..4 {
            let source = metadata(1);
            let snapshot = source.snapshot_id.clone();
            let (address, task) = peer(move |mut stream| async move {
                bootstrap_peer(&mut stream, &source).await;
                let P2PTcpRequest::NativeReadRange {
                    mut request_id,
                    request,
                    ..
                } = read_tcp_frame(&mut stream).await.unwrap()
                else {
                    panic!("expected native read");
                };
                let bytes = vec![0x42; request.length as usize];
                if mutation == 3 {
                    request_id = "wrong-correlation".into();
                }
                write_tcp_frame(
                    &mut stream,
                    &P2PTcpResponse::Range {
                        request_id,
                        length: if mutation == 2 {
                            MAX_READ_BYTES + 1
                        } else {
                            request.length
                        },
                        range_sha256: if mutation == 1 {
                            sha256_identity(b"wrong bytes")
                        } else {
                            sha256_identity(&bytes)
                        },
                    },
                )
                .await
                .unwrap();
                if mutation == 0 {
                    stream.write_all(&bytes[..8]).await.unwrap();
                }
                if mutation == 1 {
                    stream.write_all(&bytes).await.unwrap();
                }
            })
            .await;
            let (mut client, metadata) = NativeP2PClient::connect(address, TIMEOUT, snapshot, None)
                .await
                .unwrap();
            let result = client.read_range(&read_request(&metadata)).await;
            match mutation {
                0 => assert!(matches!(result, Err(NativeP2PError::Transport(_)))),
                3 => assert!(matches!(result, Err(NativeP2PError::Identity(_)))),
                _ => assert!(matches!(result, Err(NativeP2PError::Integrity(_)))),
            }
            assert!(client.stream.is_none());
            task.await.unwrap();
        }
    }

    #[tokio::test]
    async fn bounds_bootstrap_time_and_frame_allocation() {
        let (address, task) = peer(|mut stream| async move {
            open_request(&mut stream).await;
            tokio::time::sleep(Duration::from_millis(100)).await;
        })
        .await;
        assert!(matches!(
            NativeP2PClient::connect(
                address,
                Duration::from_millis(20),
                "local-ready-source",
                None
            )
            .await,
            Err(NativeP2PError::Transport(_))
        ));
        task.await.unwrap();
        let (address, task) = peer(|mut stream| async move {
            open_request(&mut stream).await;
            stream
                .write_u32(P2P_TCP_MAX_FRAME_BYTES as u32 + 1)
                .await
                .unwrap();
        })
        .await;
        assert!(matches!(
            NativeP2PClient::connect(address, TIMEOUT, "local-ready-source", None).await,
            Err(NativeP2PError::Integrity(_))
        ));
        task.await.unwrap();
    }

    #[tokio::test]
    async fn rejects_unpinned_requests_before_sending_and_honors_descriptor_units() {
        let mut source = metadata(1);
        source.layers[0].kind = LayerKind::Block;
        source.layers[0].coverage_unit_size = 512;
        source.layers[0].identity_kind = NativeLayerIdentityKind::CanonicalContent;
        source.layers[0].content_identity = sha256_identity(b"canonical compressed blob");
        source.layers[0].layer_generation_id = source.layers[0].content_identity.clone();
        source.metadata_sha256 = source.descriptor_checksum().unwrap();
        let snapshot = source.snapshot_id.clone();
        let (address, task) = peer(move |mut stream| async move {
            bootstrap_peer(&mut stream, &source).await;
            let P2PTcpRequest::NativeCoverage {
                request_id,
                request,
                ..
            } = read_tcp_frame(&mut stream).await.unwrap()
            else {
                panic!("invalid local requests must never reach the peer");
            };
            assert_eq!(request.length, 512);
            write_tcp_frame(
                &mut stream,
                &P2PTcpResponse::NativeCoverage {
                    request_id,
                    coverage: NativeCoverageResult {
                        offset: 0,
                        length: 512,
                        unit_size: 512,
                        packed_states: vec![2],
                    },
                },
            )
            .await
            .unwrap();
        })
        .await;
        let (mut client, metadata) = NativeP2PClient::connect(address, TIMEOUT, snapshot, None)
            .await
            .unwrap();
        let mut read = read_request(&metadata);
        read.generation_id = Uuid::new_v4().to_string();
        assert!(matches!(
            client.read_range(&read).await,
            Err(NativeP2PError::Identity(_))
        ));
        read = read_request(&metadata);
        read.length = MAX_READ_BYTES + 1;
        assert!(matches!(
            client.read_range(&read).await,
            Err(NativeP2PError::Identity(_))
        ));
        let mut coverage = coverage_request(&metadata);
        coverage.length = 512 * (MAX_NATIVE_COVERAGE_UNITS + 1);
        assert!(matches!(
            client.coverage(&coverage).await,
            Err(NativeP2PError::Identity(_))
        ));
        coverage.length = 512;
        assert_eq!(
            client.coverage(&coverage).await.unwrap().packed_states,
            vec![2]
        );
        task.await.unwrap();
    }
}
