//! Per-VM RemoteLoad wire adapter with bounded ingress and in-flight work.

use std::io;
use std::net::Shutdown;
use std::os::unix::net::UnixStream;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use thiserror::Error;
use tokio::sync::{mpsc, Semaphore};
use tokio::task::{JoinHandle, JoinSet};

use crate::endpoints::lazy_load::protocol::{
    RemoteLoad, RemoteLoadOrigin, RemoteLoadReady, RemoteLoadReply, RemoteLoadRequest,
};
use crate::endpoints::lazy_load::uds::RemoteLoadTransport;
use crate::observability::metrics::{DataKind, SnapdMetrics};
use crate::snapshot::io::layer_resources::LayerResources;
use crate::snapshot::services::ondemand_loader::{
    RangeLoadExecutor, RemoteLoadError, RemoteLoadRequest as ServiceRequest,
};
use crate::snapshot::transport::{RangeReadError, RangeReadFailureClass};
use crate::vm::protocol::ReceivedVmmMessage;
use crate::vm::protocol::{
    ErrorCode, MessageHeader, ProtocolError, WireEnvelope, VMM_PROTOCOL_VERSION,
};
use crate::vm::runtime::dependencies::RuntimeDependencies;
use crate::RuntimeIdentity;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RemoteFaultLimits {
    pub max_inflight_per_vm: usize,
    pub max_pending_per_vm: usize,
    pub remote_load_timeout: Duration,
}

impl Default for RemoteFaultLimits {
    fn default() -> Self {
        Self {
            max_inflight_per_vm: 8,
            max_pending_per_vm: 256,
            remote_load_timeout: Duration::from_secs(30),
        }
    }
}

impl RemoteFaultLimits {
    pub fn validate(self) -> Result<Self, RemoteFaultError> {
        if self.max_inflight_per_vm == 0
            || self.max_pending_per_vm == 0
            || self.max_pending_per_vm < self.max_inflight_per_vm
            || self.remote_load_timeout.is_zero()
        {
            return Err(RemoteFaultError::InvalidLimits);
        }
        Ok(self)
    }
}

#[derive(Debug, Error)]
pub enum RemoteFaultError {
    #[error("remote-fault connection limits are invalid")]
    InvalidLimits,
    #[error("remote-fault socket I/O failed: {0}")]
    Io(#[from] io::Error),
    #[error("remote-fault worker failed: {0}")]
    Worker(String),
}

#[async_trait]
pub trait RemoteFaultApi: Send + Sync + 'static {
    async fn begin_runtime(&self, runtime: &RuntimeIdentity) -> Result<(), ProtocolError>;

    async fn clone_layer_target(
        &self,
        source_target_id: &str,
        runtime: &RuntimeIdentity,
        target_id: &str,
    ) -> Result<Arc<LayerResources>, ProtocolError>;

    async fn remote_load(
        &self,
        runtime: &RuntimeIdentity,
        request: RemoteLoad,
    ) -> Result<RemoteLoadReady, ProtocolError>;

    async fn close_runtime(&self, runtime: &RuntimeIdentity);

    fn metrics(&self) -> Arc<SnapdMetrics>;
}

/// A thin ownership adapter keeps the wire layer independent from the range
/// service while preserving the service's Arc-based cancellation semantics.
pub struct RemoteFaultService {
    service: Arc<RangeLoadExecutor>,
}

impl RemoteFaultService {
    pub fn new(service: Arc<RangeLoadExecutor>) -> Arc<Self> {
        Arc::new(Self { service })
    }
}

#[async_trait]
impl RemoteFaultApi for RemoteFaultService {
    async fn begin_runtime(&self, runtime: &RuntimeIdentity) -> Result<(), ProtocolError> {
        self.service
            .begin_runtime(runtime)
            .await
            .map_err(map_remote_error)
    }

    async fn clone_layer_target(
        &self,
        source_target_id: &str,
        runtime: &RuntimeIdentity,
        target_id: &str,
    ) -> Result<Arc<LayerResources>, ProtocolError> {
        self.service
            .clone_layer_target(source_target_id, runtime, target_id)
            .await
            .map(|(resources, _)| resources)
            .map_err(map_remote_error)
    }

    async fn remote_load(
        &self,
        runtime: &RuntimeIdentity,
        request: RemoteLoad,
    ) -> Result<RemoteLoadReady, ProtocolError> {
        let loaded = self
            .service
            .remote_load_with_coverage(runtime, service_request(&request))
            .await
            .map_err(map_remote_error)?;
        Ok(RemoteLoadReady {
            target_id: request.target_id,
            target_offset: request.target_offset,
            length: request.length,
            resident_offset: loaded.resident_offset,
            resident_length: loaded.resident_length,
        })
    }

    async fn close_runtime(&self, runtime: &RuntimeIdentity) {
        self.service.revoke_runtime(runtime).await;
    }

    fn metrics(&self) -> Arc<SnapdMetrics> {
        self.service.metrics()
    }
}

struct QueuedRequest {
    received: ReceivedVmmMessage<RemoteLoadRequest>,
    queued_at: Instant,
}

pub struct RemoteFaultConnection {
    runtime: RuntimeIdentity,
    api: Arc<dyn RemoteFaultApi>,
    shutdown: StdMutex<Option<UnixStream>>,
    done: StdMutex<Option<JoinHandle<Result<(), RemoteFaultError>>>>,
    closed: Arc<AtomicBool>,
}

impl std::fmt::Debug for RemoteFaultConnection {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("RemoteFaultConnection")
            .field("runtime", &self.runtime)
            .field("closed", &self.closed.load(Ordering::Acquire))
            .finish_non_exhaustive()
    }
}

impl RemoteFaultConnection {
    pub fn start(
        stream: UnixStream,
        runtime: RuntimeIdentity,
        api: Arc<dyn RemoteFaultApi>,
        limits: RemoteFaultLimits,
    ) -> Result<Arc<Self>, RemoteFaultError> {
        Self::start_with_dependencies(stream, runtime, api, limits, None)
    }

    pub(crate) fn start_with_dependencies(
        stream: UnixStream,
        runtime: RuntimeIdentity,
        api: Arc<dyn RemoteFaultApi>,
        limits: RemoteFaultLimits,
        dependencies: Option<Arc<RuntimeDependencies>>,
    ) -> Result<Arc<Self>, RemoteFaultError> {
        let limits = limits.validate()?;
        let reader_stream = stream.try_clone()?;
        let shutdown = stream.try_clone()?;
        let (requests_tx, mut requests_rx) = mpsc::channel(limits.max_pending_per_vm);
        let (replies_tx, mut replies_rx) = mpsc::channel(limits.max_pending_per_vm);
        let closed = Arc::new(AtomicBool::new(false));
        let overload_replies = replies_tx.clone();
        let reader = tokio::task::spawn_blocking(move || -> io::Result<()> {
            let mut transport = RemoteLoadTransport::new(reader_stream)?;
            loop {
                let received = transport.receive_request()?;
                match requests_tx.try_send(QueuedRequest {
                    received,
                    queued_at: Instant::now(),
                }) {
                    Ok(()) => {}
                    Err(mpsc::error::TrySendError::Full(queued)) => {
                        let reply = error_reply(
                            &queued.received,
                            protocol_error(
                                ErrorCode::ResourceExhausted,
                                "per-VM RemoteLoad ingress is full",
                                true,
                            ),
                        );
                        if overload_replies.blocking_send(reply).is_err() {
                            return Ok(());
                        }
                    }
                    Err(mpsc::error::TrySendError::Closed(_)) => return Ok(()),
                }
            }
        });
        let writer_closed = Arc::clone(&closed);
        let writer = tokio::task::spawn_blocking(move || -> io::Result<()> {
            let mut transport = RemoteLoadTransport::new(stream)?;
            while let Some(reply) = replies_rx.blocking_recv() {
                let reply =
                    suppress_success_after_revoke(reply, writer_closed.load(Ordering::Acquire));
                transport.send_reply(&reply)?;
            }
            Ok(())
        });
        let coordinator_runtime = runtime.clone();
        let coordinator_api = Arc::clone(&api);
        let per_vm = Arc::new(Semaphore::new(limits.max_inflight_per_vm));
        let coordinator_closed = Arc::clone(&closed);
        let done = tokio::spawn(async move {
            let mut workers = JoinSet::new();
            while let Some(queued) = requests_rx.recv().await {
                if !queued.received.fds.is_empty() {
                    break;
                }
                let deadline =
                    tokio::time::Instant::from_std(queued.queued_at) + limits.remote_load_timeout;
                let permit =
                    match tokio::time::timeout_at(deadline, Arc::clone(&per_vm).acquire_owned())
                        .await
                    {
                        Ok(Ok(permit)) => permit,
                        Ok(Err(_)) => {
                            let _ = replies_tx
                                .send(error_reply(
                                    &queued.received,
                                    protocol_error(
                                        ErrorCode::Internal,
                                        "per-VM RemoteLoad limiter is closed",
                                        false,
                                    ),
                                ))
                                .await;
                            continue;
                        }
                        Err(_) => {
                            let _ = replies_tx
                            .send(error_reply(
                                &queued.received,
                                protocol_error(
                                    ErrorCode::DeadlineExceeded,
                                    "RemoteLoad deadline expired while waiting for per-VM capacity",
                                    true,
                                ),
                            ))
                            .await;
                            continue;
                        }
                    };
                let api = Arc::clone(&coordinator_api);
                let replies = replies_tx.clone();
                let reply_runtime = coordinator_runtime.clone();
                let worker_closed = Arc::clone(&coordinator_closed);
                let dependencies = dependencies.clone();
                workers.spawn(async move {
                    if let RemoteLoadRequest::ReleaseDependencies(release) =
                        queued.received.envelope.message.clone()
                    {
                        let result = tokio::time::timeout_at(deadline, async {
                            if worker_closed.load(Ordering::Acquire) {
                                return Err("runtime dependency capability was revoked".into());
                            }
                            match dependencies {
                                Some(dependencies) => dependencies.release(&release).await,
                                None => Err("runtime has no dependency capability".into()),
                            }
                        })
                        .await;
                        drop(permit);
                        let message = match result {
                            Ok(Ok(())) => RemoteLoadReply::DependenciesReleased(release),
                            Ok(Err(error)) => RemoteLoadReply::Error(protocol_error(
                                ErrorCode::InvalidArgument,
                                &error,
                                false,
                            )),
                            Err(_) => RemoteLoadReply::Error(protocol_error(
                                ErrorCode::DeadlineExceeded,
                                "dependency release deadline expired",
                                true,
                            )),
                        };
                        let _ = replies
                            .send(WireEnvelope {
                                header: queued.received.envelope.header.clone(),
                                message,
                            })
                            .await;
                        return;
                    }
                    let RemoteLoadRequest::RemoteLoad(request) =
                        queued.received.envelope.message.clone()
                    else {
                        unreachable!()
                    };
                    let kind = data_kind(request.origin);
                    let metrics = api.metrics();
                    metrics.observe_remote_missing_lag(kind, queued.queued_at.elapsed());
                    metrics.add_inflight(kind, 1);
                    let result = if worker_closed.load(Ordering::Acquire) {
                        Ok(Err(protocol_error(
                            ErrorCode::StaleRuntimeEpoch,
                            "RemoteLoad runtime was revoked",
                            false,
                        )))
                    } else {
                        tokio::time::timeout_at(deadline, api.remote_load(&reply_runtime, request))
                            .await
                    };
                    metrics.add_inflight(kind, -1);
                    drop(permit);
                    let message = match result {
                        Ok(Ok(_)) if worker_closed.load(Ordering::Acquire) => {
                            RemoteLoadReply::Error(protocol_error(
                                ErrorCode::StaleRuntimeEpoch,
                                "RemoteLoad runtime was revoked",
                                false,
                            ))
                        }
                        Ok(Ok(ready)) => RemoteLoadReply::RemoteLoadReady(ready),
                        Ok(Err(error)) => RemoteLoadReply::Error(error),
                        Err(_) => RemoteLoadReply::Error(protocol_error(
                            ErrorCode::DeadlineExceeded,
                            "RemoteLoad deadline expired",
                            true,
                        )),
                    };
                    let _ = replies
                        .send(WireEnvelope {
                            header: MessageHeader {
                                protocol_version: VMM_PROTOCOL_VERSION,
                                channel: crate::vm::protocol::Channel::RemoteLoad,
                                correlation_id: queued.received.envelope.header.correlation_id,
                            },
                            message,
                        })
                        .await;
                });
            }
            coordinator_closed.store(true, Ordering::Release);
            coordinator_api.close_runtime(&coordinator_runtime).await;
            while workers.join_next().await.is_some() {}
            drop(replies_tx);
            let reader_result = reader
                .await
                .map_err(|error| RemoteFaultError::Worker(error.to_string()))?;
            let writer_result = writer
                .await
                .map_err(|error| RemoteFaultError::Worker(error.to_string()))?;
            match (reader_result, writer_result) {
                (Err(error), _) if !is_channel_disconnect(&error) => Err(error.into()),
                (_, Err(error)) if !is_channel_disconnect(&error) => Err(error.into()),
                _ => Ok(()),
            }
        });
        Ok(Arc::new(Self {
            runtime,
            api,
            shutdown: StdMutex::new(Some(shutdown)),
            done: StdMutex::new(Some(done)),
            closed,
        }))
    }

    pub async fn close(&self) -> Result<(), RemoteFaultError> {
        let stream = self
            .shutdown
            .lock()
            .expect("remote shutdown lock poisoned")
            .take();
        let first_close = stream.is_some();
        if let Some(stream) = stream {
            // Socket shutdown is the revoke linearization point. A writer that
            // already passed its atomic check either sends before this point or
            // fails the send; it cannot publish a success after shutdown.
            let _ = stream.shutdown(Shutdown::Both);
            self.closed.store(true, Ordering::Release);
        }
        if first_close {
            self.api.close_runtime(&self.runtime).await;
        }
        let done = self
            .done
            .lock()
            .expect("remote runtime join lock poisoned")
            .take();
        if let Some(done) = done {
            done.await
                .map_err(|error| RemoteFaultError::Worker(error.to_string()))??;
        }
        Ok(())
    }
}

fn suppress_success_after_revoke(
    reply: WireEnvelope<RemoteLoadReply>,
    revoked: bool,
) -> WireEnvelope<RemoteLoadReply> {
    if !revoked || matches!(reply.message, RemoteLoadReply::Error(_)) {
        return reply;
    }
    WireEnvelope {
        header: reply.header,
        message: RemoteLoadReply::Error(protocol_error(
            ErrorCode::StaleRuntimeEpoch,
            "RemoteLoad runtime was revoked",
            false,
        )),
    }
}

fn error_reply(
    received: &ReceivedVmmMessage<RemoteLoadRequest>,
    error: ProtocolError,
) -> WireEnvelope<RemoteLoadReply> {
    WireEnvelope {
        header: MessageHeader {
            protocol_version: VMM_PROTOCOL_VERSION,
            channel: crate::vm::protocol::Channel::RemoteLoad,
            correlation_id: received.envelope.header.correlation_id.clone(),
        },
        message: RemoteLoadReply::Error(error),
    }
}

fn is_channel_disconnect(error: &io::Error) -> bool {
    matches!(
        error.kind(),
        io::ErrorKind::UnexpectedEof
            | io::ErrorKind::BrokenPipe
            | io::ErrorKind::ConnectionReset
            | io::ErrorKind::ConnectionAborted
            | io::ErrorKind::NotConnected
            | io::ErrorKind::InvalidInput
    )
}

fn service_request(request: &RemoteLoad) -> ServiceRequest {
    ServiceRequest {
        target_id: request.target_id.clone(),
        target_offset: request.target_offset,
        length: request.length,
        origin: match request.origin {
            RemoteLoadOrigin::UffdMissing => "uffd_missing",
            RemoteLoadOrigin::VirtioBlkReadMiss => "virtio_blk_read_miss",
            RemoteLoadOrigin::DebugOther => "debug_other",
        }
        .into(),
    }
}

fn data_kind(origin: RemoteLoadOrigin) -> DataKind {
    match origin {
        RemoteLoadOrigin::VirtioBlkReadMiss => DataKind::Block,
        RemoteLoadOrigin::UffdMissing | RemoteLoadOrigin::DebugOther => DataKind::Memory,
    }
}

fn map_remote_error(error: RemoteLoadError) -> ProtocolError {
    match error {
        RemoteLoadError::InvalidArgument(message) => {
            protocol_error(ErrorCode::InvalidArgument, &message, false)
        }
        RemoteLoadError::TargetNotFound => protocol_error(
            ErrorCode::InvalidArgument,
            "RemoteLoad target is unknown",
            false,
        ),
        RemoteLoadError::IdentityConflict => protocol_error(
            ErrorCode::TargetContentConflict,
            "RemoteLoad identity conflicts with its grant",
            false,
        ),
        RemoteLoadError::RuntimeRevoked => protocol_error(
            ErrorCode::StaleRuntimeEpoch,
            "RemoteLoad runtime was revoked",
            false,
        ),
        RemoteLoadError::ProviderNotConfigured | RemoteLoadError::UnsupportedCodec => {
            protocol_error(
                ErrorCode::CapabilityUnsupported,
                "RemoteLoad provider or codec is unsupported",
                false,
            )
        }
        RemoteLoadError::Provider(RangeReadError::NotFound)
        | RemoteLoadError::Provider(RangeReadError::InvalidRange)
        | RemoteLoadError::ShortRead
        | RemoteLoadError::ChecksumMismatch => protocol_error(
            ErrorCode::TargetContentConflict,
            "RemoteLoad immutable source failed verification",
            false,
        ),
        RemoteLoadError::Provider(RangeReadError::Fallback { final_error, .. }) => {
            map_remote_error(RemoteLoadError::Provider(final_error.into_final_error()))
        }
        RemoteLoadError::Provider(RangeReadError::Failure { class, .. }) => match class {
            RangeReadFailureClass::Transient => protocol_error(
                ErrorCode::Internal,
                "RemoteLoad provider is temporarily unavailable",
                true,
            ),
            RangeReadFailureClass::Integrity => protocol_error(
                ErrorCode::TargetContentConflict,
                "RemoteLoad immutable source failed verification",
                false,
            ),
            RangeReadFailureClass::Authorization | RangeReadFailureClass::Permanent => {
                protocol_error(
                    ErrorCode::Internal,
                    "RemoteLoad provider rejected the immutable read",
                    false,
                )
            }
        },
        RemoteLoadError::Closed => {
            protocol_error(ErrorCode::Internal, "RemoteLoad service is closed", false)
        }
        _ => protocol_error(
            ErrorCode::Internal,
            "RemoteLoad internal worker failed",
            true,
        ),
    }
}

fn protocol_error(code: ErrorCode, message: &str, retryable: bool) -> ProtocolError {
    ProtocolError {
        code,
        message: message.into(),
        retryable,
    }
}

#[cfg(test)]
mod tests {
    use std::io::Write;
    use std::os::unix::fs::FileExt;

    use super::*;
    use crate::endpoints::lazy_load::protocol::DependencyRelease;
    use crate::snapshot::io::backing::LocalBackingHandle;
    use crate::snapshot::metadata::LayerKind as SnapshotKind;
    use crate::snapshot::repository::frozen_layer::{
        FrozenLayerMetadata, FrozenLayerPin, PackedOwnershipMap,
    };
    use crate::snapshot::transport::posix_file;
    use crate::vm::runtime::dependencies::DependencyLayer;
    use crate::vm::runtime::protocol::LayerKind;

    struct DependencyOnlyApi;

    #[async_trait]
    impl RemoteFaultApi for DependencyOnlyApi {
        async fn begin_runtime(&self, _: &RuntimeIdentity) -> Result<(), ProtocolError> {
            unreachable!("dependency notification must not begin a runtime")
        }

        async fn clone_layer_target(
            &self,
            _: &str,
            _: &RuntimeIdentity,
            _: &str,
        ) -> Result<Arc<LayerResources>, ProtocolError> {
            unreachable!("dependency notification must not clone a target")
        }

        async fn remote_load(
            &self,
            _: &RuntimeIdentity,
            _: RemoteLoad,
        ) -> Result<RemoteLoadReady, ProtocolError> {
            unreachable!("dependency notification must not perform a remote load")
        }

        async fn close_runtime(&self, _: &RuntimeIdentity) {}

        fn metrics(&self) -> Arc<SnapdMetrics> {
            unreachable!("dependency notification must not enter the remote-load metrics path")
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn binary_cow_release_is_authorized_idempotent_and_scoped_to_runtime() {
        let file = posix_file::memory_file(c"dependency-wire-test", 4096).unwrap();
        file.write_all_at(&vec![0x5a; 4096], 0).unwrap();
        let native = LocalBackingHandle::from_native(FrozenLayerPin::new(
            FrozenLayerMetadata {
                kind: SnapshotKind::Memory,
                layer_id: "native-wire-source".into(),
                provisional_content_id: "native-wire-capture".into(),
                logical_size: 4096,
                alignment: 4096,
            },
            PackedOwnershipMap {
                kind: SnapshotKind::Memory,
                snapshot_id: "native-wire-capture".into(),
                format_version: 1,
                logical_size: 4096,
                granularity: 4096,
                // One DATA unit; all unused trailing bits remain zero.
                packed_states: vec![1],
            },
            file.into(),
            false,
        ))
        .unwrap();
        let dependencies = RuntimeDependencies::new();
        let other_runtime = RuntimeDependencies::new();
        for runtime in [&dependencies, &other_runtime] {
            runtime
                .prepare(vec![DependencyLayer {
                    backing: native.backing.clone(),
                    data: vec![(0, 4096)],
                    zero: vec![],
                }])
                .await
                .unwrap();
        }
        assert_eq!(native.backing.dependency_counts(0).unwrap().remaining(), 2);
        let (device, inode) = native.backing.physical_identity().unwrap();
        let release = DependencyRelease {
            kind: LayerKind::Memory,
            device,
            inode,
            offset: 0,
            length: 4096,
        };
        let (mut peer, server) = UnixStream::pair().unwrap();
        peer.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        peer.set_write_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        let connection = RemoteFaultConnection::start_with_dependencies(
            server,
            RuntimeIdentity::new("wire-source-runtime"),
            Arc::new(DependencyOnlyApi),
            RemoteFaultLimits {
                max_inflight_per_vm: 1,
                max_pending_per_vm: 2,
                remote_load_timeout: Duration::from_secs(1),
            },
            Some(dependencies.clone()),
        )
        .unwrap();
        tokio::task::spawn_blocking(move || {
            let mut replies = RemoteLoadTransport::new(peer.try_clone().unwrap()).unwrap();
            let mut forged = release.clone();
            forged.inode += 1;
            for (index, message) in [release.clone(), release.clone(), forged]
                .into_iter()
                .enumerate()
            {
                let correlation = format!("cow-{index}");
                // Construct the CLH positional frame independently of snapd's
                // encoder: tag 3, memory kind 0, then four little-endian u64s.
                let mut payload = b"RCKLOAD2".to_vec();
                payload.extend_from_slice(&VMM_PROTOCOL_VERSION.to_le_bytes());
                payload.extend_from_slice(&(correlation.len() as u32).to_le_bytes());
                payload.extend_from_slice(correlation.as_bytes());
                payload.extend_from_slice(&[3, 0]);
                for value in [
                    message.device,
                    message.inode,
                    message.offset,
                    message.length,
                ] {
                    payload.extend_from_slice(&value.to_le_bytes());
                }
                peer.write_all(&(payload.len() as u32).to_le_bytes())
                    .unwrap();
                peer.write_all(&payload).unwrap();
                let reply = replies.receive_reply().unwrap().envelope;
                assert_eq!(reply.header.correlation_id, correlation);
                if index < 2 {
                    assert_eq!(
                        reply.message,
                        RemoteLoadReply::DependenciesReleased(release.clone())
                    );
                } else {
                    let RemoteLoadReply::Error(error) = reply.message else {
                        panic!("an unregistered inode must not be acknowledged");
                    };
                    assert_eq!(error.code, ErrorCode::InvalidArgument);
                }
            }
        })
        .await
        .unwrap();
        // The duplicate notification and forged inode cannot consume the
        // second runtime's dependency on the same physical memfd.
        assert_eq!(native.backing.dependency_counts(0).unwrap().remaining(), 1);
        tokio::time::timeout(Duration::from_secs(2), connection.close())
            .await
            .unwrap()
            .unwrap();
        dependencies.close().await;
        assert_eq!(native.backing.dependency_counts(0).unwrap().remaining(), 1);
        other_runtime.close().await;
        assert_eq!(native.backing.dependency_counts(0).unwrap().remaining(), 0);
    }
}
