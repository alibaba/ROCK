//! Direct-TCP transport for independently authenticated decoded peer DATA.
//! Source fallback is owned by RangeIO; this client never calls another reader.

pub mod native;
pub mod protocol;
pub(crate) use crate::snapshot::metadata::kinds as kind_models;
pub mod native_client;
pub use native_client::{NativeP2PClient, NativeP2PError};
pub(crate) mod native_wire;
pub mod tcp;

use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

#[cfg(test)]
use async_trait::async_trait;
use sha2::{Digest, Sha256};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::Mutex;
use uuid::Uuid;

use self::protocol::{
    P2PReadRangeRequest, P2PTcpErrorCode, P2PTcpRequest, P2PTcpResponse, P2P_PROTOCOL_VERSION,
    P2P_TCP_MAGIC,
};
use self::tcp::{read_tcp_frame, write_tcp_frame};
use crate::observability::metrics::{DataKind, P2PProbeOutcome, P2PRangeOutcome, SnapdMetrics};
use crate::observability::structured_log::{
    StructuredComponent, StructuredEvent, StructuredEventName, StructuredLogLevel,
    StructuredOutcome,
};
use crate::snapshot::io::range_io::{P2PSource, RangeReadRequest};
use crate::snapshot::transport::{RangeReadError, RangeReadFailureClass, RangeReadFallbackClass};
use crate::RuntimeIdentity;

#[derive(Debug, Clone, Copy)]
pub struct P2PClientConfig {
    pub enabled: bool,
    pub port: u16,
    pub probe_timeout: Duration,
    pub range_timeout: Duration,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ProbeState {
    Unknown,
    Available,
    ProbeMiss,
    ProbeTimeout,
    ProbeTransport,
    RangeTimeoutDisabled,
    RangeTransportDisabled,
    IntegrityDisabled,
}

#[derive(Debug)]
enum P2PReadError {
    Miss(String),
    Transport(String),
    Integrity(String),
}

#[derive(Clone)]
pub struct P2PClient {
    config: P2PClientConfig,
    metrics: Arc<SnapdMetrics>,
    peers: Arc<Mutex<HashMap<RuntimeIdentity, Arc<Mutex<RuntimePeer>>>>>,
}

#[derive(Debug)]
struct RuntimePeer {
    source: P2PSource,
    state: ProbeState,
    stream: Option<TcpStream>,
}

impl std::fmt::Debug for P2PClient {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("P2PClient")
            .field("config", &self.config)
            .finish_non_exhaustive()
    }
}

impl P2PClient {
    pub fn new(
        config: P2PClientConfig,
        metrics: Arc<SnapdMetrics>,
    ) -> Result<Arc<Self>, RangeReadError> {
        if config.port == 0 || config.probe_timeout.is_zero() || config.range_timeout.is_zero() {
            return Err(RangeReadError::failure(
                RangeReadFailureClass::Permanent,
                "P2P port and timeouts must be positive",
            ));
        }
        Ok(Arc::new(Self {
            config,
            metrics,
            peers: Arc::new(Mutex::new(HashMap::new())),
        }))
    }

    pub async fn forget_runtime(&self, runtime: &RuntimeIdentity) {
        self.peers.lock().await.remove(runtime);
    }

    async fn probe_state(&self, runtime: &RuntimeIdentity, source: &P2PSource) -> ProbeState {
        let peer = {
            let mut peers = self.peers.lock().await;
            Arc::clone(peers.entry(runtime.clone()).or_insert_with(|| {
                Arc::new(Mutex::new(RuntimePeer {
                    source: source.clone(),
                    state: ProbeState::Unknown,
                    stream: None,
                }))
            }))
        };
        let mut peer = peer.lock().await;
        if peer.source != *source {
            peer.state = ProbeState::IntegrityDisabled;
            peer.stream = None;
            self.metrics.add_integrity_violation();
            return peer.state;
        }
        if peer.state != ProbeState::Unknown {
            return peer.state;
        }
        let started = Instant::now();
        let (state, stream) =
            match tokio::time::timeout(self.config.probe_timeout, self.connect_and_probe(source))
                .await
            {
                Ok(Ok(Some(stream))) => {
                    self.metrics.add_p2p_probe(P2PProbeOutcome::Available);
                    (ProbeState::Available, Some(stream))
                }
                Ok(Ok(None)) => {
                    self.metrics.add_p2p_probe(P2PProbeOutcome::Unavailable);
                    (ProbeState::ProbeMiss, None)
                }
                Ok(Err(P2PReadError::Integrity(_))) => {
                    self.metrics.add_p2p_probe(P2PProbeOutcome::Error);
                    self.metrics.add_integrity_violation();
                    (ProbeState::IntegrityDisabled, None)
                }
                Ok(Err(_)) => {
                    self.metrics.add_p2p_probe(P2PProbeOutcome::Error);
                    (ProbeState::ProbeTransport, None)
                }
                Err(_) => {
                    self.metrics.add_p2p_probe(P2PProbeOutcome::Error);
                    (ProbeState::ProbeTimeout, None)
                }
            };
        peer.state = state;
        peer.stream = stream;
        self.emit_probe(runtime, source, state, started.elapsed());
        state
    }

    async fn disable_runtime_peer(&self, runtime: &RuntimeIdentity, disabled: ProbeState) {
        debug_assert!(matches!(
            disabled,
            ProbeState::RangeTimeoutDisabled
                | ProbeState::RangeTransportDisabled
                | ProbeState::IntegrityDisabled
        ));
        let peer = self.peers.lock().await.get(runtime).cloned();
        if let Some(peer) = peer {
            let mut peer = peer.lock().await;
            if peer.state == ProbeState::Available {
                peer.state = disabled;
                peer.stream = None;
            }
        }
    }

    async fn connect_and_probe(
        &self,
        source: &P2PSource,
    ) -> Result<Option<TcpStream>, P2PReadError> {
        let mut stream = TcpStream::connect(SocketAddr::new(source.worker_ip, self.config.port))
            .await
            .map_err(|error| P2PReadError::Transport(error.to_string()))?;
        stream
            .set_nodelay(true)
            .map_err(|error| P2PReadError::Transport(error.to_string()))?;
        stream
            .write_all(P2P_TCP_MAGIC)
            .await
            .map_err(|error| P2PReadError::Transport(error.to_string()))?;
        let opening_id = request_id();
        write_tcp_frame(
            &mut stream,
            &P2PTcpRequest::Open {
                protocol_version: P2P_PROTOCOL_VERSION,
                request_id: opening_id.clone(),
                deadline_unix_ms: deadline(self.config.probe_timeout),
            },
        )
        .await
        .map_err(|error| P2PReadError::Transport(error.to_string()))?;
        match read_tcp_frame::<_, P2PTcpResponse>(&mut stream)
            .await
            .map_err(|error| P2PReadError::Transport(error.to_string()))?
        {
            P2PTcpResponse::Ready {
                request_id,
                protocol_version,
            } if request_id == opening_id && protocol_version == P2P_PROTOCOL_VERSION => {}
            P2PTcpResponse::Error { code, message, .. } => return Err(wire_error(code, message)),
            _ => {
                return Err(P2PReadError::Integrity(
                    "peer open response identity differs".into(),
                ));
            }
        }
        let probe_id = request_id();
        write_tcp_frame(
            &mut stream,
            &P2PTcpRequest::Availability {
                request_id: probe_id.clone(),
                deadline_unix_ms: deadline(self.config.probe_timeout),
                snapshot_id: source.snapshot_id.clone(),
                spec_checksum: source.spec_checksum.clone(),
                selection_only: true,
            },
        )
        .await
        .map_err(|error| P2PReadError::Transport(error.to_string()))?;
        match read_tcp_frame::<_, P2PTcpResponse>(&mut stream)
            .await
            .map_err(|error| P2PReadError::Transport(error.to_string()))?
        {
            P2PTcpResponse::Availability {
                request_id,
                availability,
            } if request_id == probe_id
                && availability.snapshot_id == source.snapshot_id
                && availability.spec_checksum == source.spec_checksum =>
            {
                // A partial source can serve resident ranges. A normal range
                // miss does not invalidate this authenticated session.
                Ok(availability.available.then_some(stream))
            }
            P2PTcpResponse::Error { code, message, .. } => Err(wire_error(code, message)),
            _ => Err(P2PReadError::Integrity(
                "peer availability response identity differs".into(),
            )),
        }
    }

    async fn p2p_read(
        &self,
        request: RangeReadRequest<'_>,
        source: &P2PSource,
    ) -> Result<Vec<u8>, P2PReadError> {
        let expected_range_checksum = prefixed_sha256(request.expected_range_sha256);
        let peer = self
            .peers
            .lock()
            .await
            .get(request.runtime)
            .cloned()
            .ok_or_else(|| P2PReadError::Transport("peer session is missing".into()))?;
        let mut peer = peer.lock().await;
        if peer.source != *source || peer.state != ProbeState::Available {
            return Err(P2PReadError::Integrity(
                "peer session identity or routing state differs".into(),
            ));
        }
        let mut stream = peer
            .stream
            .take()
            .ok_or_else(|| P2PReadError::Transport("peer TCP stream is unavailable".into()))?;
        let range_request_id = request_id();
        let frame = P2PTcpRequest::ReadRange {
            request_id: range_request_id.clone(),
            deadline_unix_ms: deadline(self.config.range_timeout),
            snapshot_id: source.snapshot_id.clone(),
            request: P2PReadRangeRequest {
                spec_checksum: source.spec_checksum.clone(),
                object_key: request.object_key.to_owned(),
                object_sha256: prefixed_sha256(request.object_sha256),
                object_version: request.object_version.map(str::to_owned),
                offset: request.offset,
                length: request.length,
            },
        };
        write_tcp_frame(&mut stream, &frame)
            .await
            .map_err(|error| P2PReadError::Transport(error.to_string()))?;
        let response = read_tcp_frame::<_, P2PTcpResponse>(&mut stream)
            .await
            .map_err(|error| P2PReadError::Transport(error.to_string()))?;
        let (declared, peer_checksum) = match response {
            P2PTcpResponse::Range {
                request_id,
                length,
                range_sha256,
            } if request_id == range_request_id => (length, range_sha256),
            P2PTcpResponse::Error {
                request_id,
                code,
                message,
                ..
            } if request_id == range_request_id => {
                if code == P2PTcpErrorCode::NotFound {
                    peer.stream = Some(stream);
                }
                return Err(wire_error(code, message));
            }
            _ => {
                return Err(P2PReadError::Integrity(
                    "peer range response identity differs".into(),
                ));
            }
        };
        if declared != request.length || peer_checksum != expected_range_checksum {
            return Err(P2PReadError::Integrity(
                "peer range length or checksum differs from trusted snapshot metadata".into(),
            ));
        }
        let mut payload = vec![
            0_u8;
            usize::try_from(declared).map_err(|_| {
                P2PReadError::Integrity("peer range length exceeds address space".into())
            })?
        ];
        stream
            .read_exact(&mut payload)
            .await
            .map_err(|error| P2PReadError::Transport(error.to_string()))?;
        if prefixed_sha256(&format!("{:x}", Sha256::digest(&payload))) != expected_range_checksum {
            return Err(P2PReadError::Integrity(
                "peer range payload checksum differs from trusted snapshot metadata".into(),
            ));
        }
        peer.stream = Some(stream);
        Ok(payload)
    }

    fn emit_probe(
        &self,
        runtime: &RuntimeIdentity,
        source: &P2PSource,
        state: ProbeState,
        elapsed: Duration,
    ) {
        let (level, outcome, error_code) = match state {
            ProbeState::Available => (StructuredLogLevel::Info, StructuredOutcome::Success, None),
            ProbeState::ProbeMiss => (
                StructuredLogLevel::Info,
                StructuredOutcome::Rejected,
                Some("unavailable"),
            ),
            ProbeState::ProbeTimeout => (
                StructuredLogLevel::Warning,
                StructuredOutcome::Error,
                Some("timeout"),
            ),
            ProbeState::ProbeTransport => (
                StructuredLogLevel::Warning,
                StructuredOutcome::Error,
                Some("transport"),
            ),
            ProbeState::IntegrityDisabled => (
                StructuredLogLevel::Error,
                StructuredOutcome::Error,
                Some("integrity_disabled"),
            ),
            ProbeState::Unknown
            | ProbeState::RangeTimeoutDisabled
            | ProbeState::RangeTransportDisabled => return,
        };
        let mut event = StructuredEvent::new(
            level,
            StructuredEventName::P2PProbeCompleted,
            StructuredComponent::P2P,
            outcome,
        )
        .operation("p2p_probe")
        .snapshot_id(&source.snapshot_id)
        .sandbox_id(&runtime.sandbox_id)
        .duration(elapsed);
        if let Some(error_code) = error_code {
            event = event.error_code(error_code);
        }
        self.metrics.emit_structured(event);
    }

    fn emit_range(
        &self,
        runtime: &RuntimeIdentity,
        source: &P2PSource,
        request: RangeReadRequest<'_>,
        outcome: StructuredOutcome,
        error_code: Option<&'static str>,
        elapsed: Duration,
    ) {
        let mut event = StructuredEvent::new(
            if error_code.is_some() {
                StructuredLogLevel::Warning
            } else {
                StructuredLogLevel::Info
            },
            StructuredEventName::P2PRangeCompleted,
            StructuredComponent::P2P,
            outcome,
        )
        .operation("p2p_range_read")
        .snapshot_id(&source.snapshot_id)
        .object_sha256(request.object_sha256)
        .sandbox_id(&runtime.sandbox_id)
        .phase(match request.kind {
            crate::snapshot::io::range_io::RangeDataKind::Memory => "memory",
            crate::snapshot::io::range_io::RangeDataKind::Block => "block",
        })
        .range(request.offset, request.length)
        .duration(elapsed);
        if let Some(error_code) = error_code {
            event = event.error_code(error_code);
        }
        self.metrics.emit_structured(event);
    }
}

#[derive(Debug)]
pub(crate) struct PeerReadFailure {
    pub class: RangeReadFallbackClass,
    pub message: String,
}

impl PeerReadFailure {
    fn new(class: RangeReadFallbackClass, message: impl Into<String>) -> Self {
        Self {
            class,
            message: message.into(),
        }
    }
}

impl P2PClient {
    pub(crate) async fn allows_provider_coalescing(
        &self,
        runtime: &RuntimeIdentity,
        source: &P2PSource,
    ) -> bool {
        !self.config.enabled || self.probe_state(runtime, source).await != ProbeState::Available
    }

    pub(crate) fn metrics(&self) -> Arc<SnapdMetrics> {
        Arc::clone(&self.metrics)
    }

    pub(crate) async fn read_decoded(
        &self,
        request: RangeReadRequest<'_>,
    ) -> Result<Vec<u8>, PeerReadFailure> {
        let kind = match request.kind {
            crate::snapshot::io::range_io::RangeDataKind::Memory => DataKind::Memory,
            crate::snapshot::io::range_io::RangeDataKind::Block => DataKind::Block,
        };
        let source = request.preferred_p2p_source.ok_or_else(|| {
            PeerReadFailure::new(
                RangeReadFallbackClass::Unavailable,
                "no peer source was selected",
            )
        })?;
        if !self.config.enabled {
            return Err(PeerReadFailure::new(
                RangeReadFallbackClass::Unavailable,
                "P2P is disabled",
            ));
        }
        let state = self.probe_state(request.runtime, source).await;
        if state != ProbeState::Available {
            return Err(PeerReadFailure::new(
                fallback_class_for_probe_state(state),
                probe_failure(state),
            ));
        }
        let started = Instant::now();
        match tokio::time::timeout(self.config.range_timeout, self.p2p_read(request, source)).await
        {
            Ok(Ok(payload)) => {
                self.metrics
                    .observe_p2p_range(kind, P2PRangeOutcome::Hit, started.elapsed());
                Ok(payload)
            }
            Ok(Err(P2PReadError::Miss(message))) => {
                self.metrics
                    .observe_p2p_range(kind, P2PRangeOutcome::Miss, started.elapsed());

                Err(PeerReadFailure::new(
                    RangeReadFallbackClass::NotFound,
                    message,
                ))
            }
            Ok(Err(P2PReadError::Integrity(message))) => {
                self.metrics.observe_p2p_range(
                    kind,
                    P2PRangeOutcome::IntegrityError,
                    started.elapsed(),
                );

                self.metrics.add_integrity_violation();
                self.emit_range(
                    request.runtime,
                    source,
                    request,
                    StructuredOutcome::Error,
                    Some("integrity"),
                    started.elapsed(),
                );
                self.disable_runtime_peer(request.runtime, ProbeState::IntegrityDisabled)
                    .await;
                Err(PeerReadFailure::new(
                    RangeReadFallbackClass::Integrity,
                    message,
                ))
            }
            Ok(Err(P2PReadError::Transport(message))) => {
                self.metrics.observe_p2p_range(
                    kind,
                    P2PRangeOutcome::TransportError,
                    started.elapsed(),
                );

                self.emit_range(
                    request.runtime,
                    source,
                    request,
                    StructuredOutcome::Error,
                    Some("transport"),
                    started.elapsed(),
                );
                self.disable_runtime_peer(request.runtime, ProbeState::RangeTransportDisabled)
                    .await;
                Err(PeerReadFailure::new(
                    RangeReadFallbackClass::Transport,
                    message,
                ))
            }
            Err(_) => {
                self.metrics
                    .observe_p2p_range(kind, P2PRangeOutcome::Timeout, started.elapsed());

                self.emit_range(
                    request.runtime,
                    source,
                    request,
                    StructuredOutcome::Error,
                    Some("timeout"),
                    started.elapsed(),
                );
                self.disable_runtime_peer(request.runtime, ProbeState::RangeTimeoutDisabled)
                    .await;
                Err(PeerReadFailure::new(
                    RangeReadFallbackClass::Timeout,
                    "P2P range read timed out",
                ))
            }
        }
    }
}

fn prefixed_sha256(value: &str) -> String {
    if value.starts_with("sha256:") {
        value.to_owned()
    } else {
        format!("sha256:{value}")
    }
}

fn now_unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

fn deadline(timeout: Duration) -> u64 {
    now_unix_ms().saturating_add(timeout.as_millis() as u64)
}

fn request_id() -> String {
    format!("p2p:{}", Uuid::new_v4())
}

fn wire_error(code: P2PTcpErrorCode, message: String) -> P2PReadError {
    match code {
        P2PTcpErrorCode::NotFound => P2PReadError::Miss(message),
        P2PTcpErrorCode::InvalidArgument | P2PTcpErrorCode::IdentityConflict => {
            P2PReadError::Integrity(message)
        }
        P2PTcpErrorCode::DeadlineExceeded | P2PTcpErrorCode::Internal => {
            P2PReadError::Transport(message)
        }
    }
}

fn probe_failure(state: ProbeState) -> &'static str {
    match state {
        ProbeState::ProbeMiss => "P2P probe reported unavailable",
        ProbeState::ProbeTimeout => "P2P probe timed out",
        ProbeState::ProbeTransport => "P2P probe transport failed",
        ProbeState::RangeTimeoutDisabled => "P2P was disabled after a range timeout",
        ProbeState::RangeTransportDisabled => "P2P was disabled after a range transport failure",
        ProbeState::IntegrityDisabled => "P2P was disabled after an integrity failure",
        ProbeState::Unknown | ProbeState::Available => {
            "P2P probe did not produce a fallback reason"
        }
    }
}

fn fallback_class_for_probe_state(state: ProbeState) -> RangeReadFallbackClass {
    match state {
        ProbeState::ProbeMiss => RangeReadFallbackClass::Unavailable,
        ProbeState::ProbeTimeout | ProbeState::RangeTimeoutDisabled => {
            RangeReadFallbackClass::Timeout
        }
        ProbeState::ProbeTransport | ProbeState::RangeTransportDisabled => {
            RangeReadFallbackClass::Transport
        }
        ProbeState::IntegrityDisabled => RangeReadFallbackClass::Integrity,
        ProbeState::Unknown | ProbeState::Available => {
            unreachable!("available/unknown P2P state does not fall back here")
        }
    }
}

#[cfg(test)]
mod physical_fallback_tests {
    use super::*;

    use std::sync::atomic::{AtomicUsize, Ordering};

    use crate::snapshot::io::range_io::{RangeDataKind, RangeIO, RangeReadTransport, RangeReader};
    use crate::snapshot::transport::{PhysicalRangeReader, PhysicalRangeRequest};

    struct PhysicalProvider {
        calls: AtomicUsize,
        error: Option<RangeReadError>,
    }

    #[async_trait]
    impl PhysicalRangeReader for PhysicalProvider {
        fn source_id(&self) -> &str {
            "test-object-store"
        }

        async fn read_physical_range(
            &self,
            request: PhysicalRangeRequest<'_>,
        ) -> Result<Vec<u8>, RangeReadError> {
            self.calls.fetch_add(1, Ordering::Relaxed);
            assert_eq!(request.object_key, "raw.pack");
            assert_eq!(request.object_sha256, "object-identity");
            assert_eq!(request.object_version, Some("version-1"));
            assert_eq!((request.offset, request.length), (4096, 4));
            match &self.error {
                Some(error) => Err(error.clone()),
                None => Ok(vec![1, 2, 3, 4]),
            }
        }
    }

    fn request<'a>(runtime: &'a RuntimeIdentity, peer: &'a P2PSource) -> RangeReadRequest<'a> {
        RangeReadRequest {
            physical: None,
            authority: None,
            runtime,
            kind: RangeDataKind::Block,
            object_key: "raw.pack",
            object_sha256: "object-identity",
            expected_range_sha256: "unit-identity",
            offset: 4096,
            length: 4,
            object_version: Some("version-1"),
            preferred_p2p_source: Some(peer),
        }
    }

    async fn fixture(
        state: Option<ProbeState>,
        error: Option<RangeReadError>,
    ) -> (
        Arc<RangeIO>,
        Arc<PhysicalProvider>,
        RuntimeIdentity,
        P2PSource,
    ) {
        let physical = Arc::new(PhysicalProvider {
            calls: AtomicUsize::new(0),
            error,
        });
        let client = P2PClient::new(
            P2PClientConfig {
                enabled: state.is_some(),
                port: 12345,
                probe_timeout: Duration::from_millis(100),
                range_timeout: Duration::from_millis(100),
            },
            SnapdMetrics::new("physical-fallback-test").unwrap(),
        )
        .unwrap();
        let runtime = RuntimeIdentity::new("physical-fallback");
        let peer = P2PSource {
            snapshot_id: "snapshot".into(),
            spec_checksum: "checksum".into(),
            worker_ip: "127.0.0.1".parse().unwrap(),
        };
        if let Some(state) = state {
            client.peers.lock().await.insert(
                runtime.clone(),
                Arc::new(Mutex::new(RuntimePeer {
                    source: peer.clone(),
                    state,
                    stream: None,
                })),
            );
        }
        let reader = RangeIO::new(physical.clone(), Some(client));
        (reader, physical, runtime, peer)
    }

    #[tokio::test]
    async fn consumed_peer_hints_allow_raw_authoritative_physical_fallback() {
        for state in [
            None,
            Some(ProbeState::ProbeMiss),
            Some(ProbeState::IntegrityDisabled),
        ] {
            let (reader, physical, runtime, peer) = fixture(state, None).await;
            let response = reader.read_range(request(&runtime, &peer)).await.unwrap();
            assert_eq!(response.transport, RangeReadTransport::Provider);
            assert_eq!(response.payload, [1, 2, 3, 4]);
            assert_eq!(physical.calls.load(Ordering::Relaxed), 1);
        }
    }

    #[tokio::test]
    async fn physical_fallback_preserves_peer_reason_and_typed_authoritative_error() {
        let authoritative = RangeReadError::failure(RangeReadFailureClass::Authorization, "denied");
        let (reader, physical, runtime, peer) =
            fixture(Some(ProbeState::ProbeMiss), Some(authoritative.clone())).await;
        let error = reader
            .read_range(request(&runtime, &peer))
            .await
            .unwrap_err();
        assert!(matches!(
            &error,
            RangeReadError::Fallback { preferred_source, .. }
                if preferred_source.class == RangeReadFallbackClass::Unavailable
        ));
        assert_eq!(error.into_final_error(), authoritative);
        assert_eq!(physical.calls.load(Ordering::Relaxed), 1);
    }
}
