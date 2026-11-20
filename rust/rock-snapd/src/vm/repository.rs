//! Node-local VM instances, lifecycle operations and start ownership.
//!
//! VmRepository owns the instance state machine. VmService runs VM/Snapshot
//! workflows over that repository, including its concrete VMM execution capabilities.

use crate::snapshot::SnapshotInstance;

use std::collections::{BTreeSet, HashMap, HashSet};
use std::fs::File;
use std::future::Future;
use std::net::IpAddr;
use std::os::fd::OwnedFd;
use std::os::unix::fs::FileExt;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex as StdMutex, OnceLock, Weak};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use thiserror::Error;
use tokio::io::AsyncWriteExt;
use tokio::sync::{watch, Mutex, Notify, OwnedMutexGuard, OwnedSemaphorePermit, Semaphore};
use uuid::Uuid;

use crate::observability::metrics::{DataKind, LeaseKind, SnapdMetrics};
use crate::observability::structured_log::{
    StructuredComponent, StructuredEvent, StructuredEventName, StructuredLogLevel,
    StructuredOutcome,
};
use crate::snapshot::io::layer_resources::LayerResources;
use crate::snapshot::io::snapshot_io::SnapshotReader;
use crate::vm::error::{
    snapshot_load_error, snapshot_restore_error, OperationError as SnapshotControlError,
};
use crate::vm::runtime::client::{
    launch_client, validate_launch_rocklet, RuntimeLaunchFactory, VMMClientInterface,
    VmmControllerError,
};
use crate::vm::runtime::dependencies::{DependencyLayer, RuntimeDependencies};
use crate::vm::runtime::prepare::SnapshotRestorePreparer;

use crate::snapshot::image::model::{BuildImage, ImageBuildAccepted};
use crate::snapshot::image::service::{BaseImageIngestionError, BaseImageIngestionService};
use crate::snapshot::metadata::{
    canonical_json_bytes, sha256_identity, LayerChainIndex, LayerKind as SnapshotLayerKind,
    RockletArtifactCompatibility, SnapshotProvenance,
};
use crate::snapshot::repository::frozen_layer::{
    CapturedSnapshotPin, FrozenLayerError, FrozenLayerMetadata, FrozenLayerPin, PackedOwnershipMap,
    VmStateMetadata, VmStatePin,
};
use crate::snapshot::repository::lineage::SnapshotLineage;
use crate::snapshot::repository::{
    LocalSnapshotError, PublicationState, Snapshot, SnapshotRepository,
};
use crate::snapshot::services::background_uploader::{
    LayerUploadState, LayerUploadStatus, PublicationFinalized, ResolvedSnapshotUploadTarget,
    SnapshotPublication, SnapshotStatus, SnapshotUploadService, SnapshotUploadServiceError,
};
use crate::snapshot::services::p2p::{
    P2PAvailabilityResult, P2PReadRangeRequest, P2PServeError, P2PSnapshotServeRegistry,
};
use crate::snapshot::transport::p2p::native::{
    NativeCoverageRequest, NativeCoverageResult, NativeReadRangeRequest, NativeSnapshotMetadata,
};
use crate::vm::protocol::{
    Channel, ErrorCode, MessageHeader, ReceivedVmmMessage, ValidateVmmMessage, WireEnvelope,
    VMM_PROTOCOL_VERSION,
};
use crate::vm::runtime::protocol::{
    CaptureGeneration, CapturedGenerationAccepted, ControlReply, ControlRequest,
    GenerationCaptured, LayerGeometry, LayerKind as ProtocolLayerKind,
};
use crate::RuntimeIdentity;

use crate::endpoints::lazy_load::handler::{
    RemoteFaultApi, RemoteFaultConnection, RemoteFaultLimits, RemoteFaultService,
};
use crate::snapshot::io::backing::{DataRangeKey, DataRangeResidencyRegistry};
use crate::snapshot::metadata::{LayerRangeMap, RangeKind};
use crate::snapshot::repository::lineage::{
    RemoteLayerSource, SnapshotLineageEntry, SourceResidency, SourceResidencyHandle,
};
use crate::snapshot::repository::FrozenLocalLayer;
use crate::snapshot::services::ondemand_loader::RangeLoadExecutor;
use crate::snapshot::ValidateCanonicalModel;
use crate::vm::runtime::client::{VmControlApi, VmmClientError};
use crate::vm::runtime::launcher::{GuestEndpointInfo, VmmLaunchError, VmmLaunchSpec};
use crate::vm::runtime::prepare::PreparedFinalization;
use crate::vm::runtime::prepare::{
    current_header, duplicate_fd, io_alignment, protocol_kind, sealed_payload_readonly_fd, sha_hex,
    validate_readonly_backing, validate_shared_mapping_backing, validate_vmm_range_index_size,
    vmm_range_index, CoverageAttachment, LayerAttachment, RuntimeBackingPayload, VmStateAttachment,
};
use crate::vm::runtime::protocol::{
    Capability, DataResidency, FinalizePublishedGeneration, OwnershipMapDescriptor,
    ResidentCoverageDescriptor,
};

use crate::snapshot::services::p2p::ResidentRangeSource;

type BoxFuture<T> = Pin<Box<dyn Future<Output = T> + Send + 'static>>;

#[derive(Clone, Debug, Error, Eq, PartialEq)]
pub enum VmRepositoryError {
    #[error("Invalid VMM registry input: {0}")]
    Invalid(String),
    #[error("VMM identity conflict: {0}")]
    IdentityConflict(String),
    #[error("VMM already exists: {0}")]
    AlreadyExists(String),
    #[error("VMM was not found: {0}")]
    NotFound(String),
    #[error("Invalid VMM lifecycle transition: {0}")]
    InvalidState(String),
    #[error("VMM command belongs to a stale daemon epoch")]
    StaleDaemonEpoch,
    #[error("VMM runtime operation failed: {0}")]
    Runtime(String),
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum VmmLifecycleState {
    Creating,
    Running,
    Stopping,
    Failed,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum VmmOperation {
    Start,
    Stop,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum VmmOperationFlag {
    SnapshotInFlight,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OperationCode {
    Running,
    CreateFailed,
    ProcessExited,
    Stopped,
    CleanupFailed,
}

impl OperationCode {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Running => "RUNNING",
            Self::CreateFailed => "CREATE_FAILED",
            Self::ProcessExited => "PROCESS_EXITED",
            Self::Stopped => "STOPPED",
            Self::CleanupFailed => "CLEANUP_FAILED",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OperationResult {
    pub vm_id: String,
    pub operation: VmmOperation,
    pub success: bool,
    pub code: OperationCode,
    pub message: Option<String>,
}

#[derive(Debug)]
pub struct StartedRuntime<H> {
    pub pid: u32,
    pub handles: H,
}

impl<H> StartedRuntime<H> {
    pub fn new(pid: u32, handles: H) -> Result<Self, VmRepositoryError> {
        if pid == 0 {
            return invalid("runtime PID must be positive");
        }
        Ok(Self { pid, handles })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct VmmInstanceSnapshot {
    pub vm_id: String,
    pub lifecycle_state: VmmLifecycleState,
    pub pid: Option<u32>,
    pub operation_flags: HashSet<VmmOperationFlag>,
    pub last_error: Option<String>,
    pub result_code: Option<String>,
    pub exit_reason: Option<String>,
}

#[derive(Debug)]
struct InstanceState<H> {
    exit_watched: bool,
    lifecycle_state: VmmLifecycleState,
    pid: Option<u32>,
    handles: Option<Arc<H>>,
    operation_flags: HashSet<VmmOperationFlag>,
    last_error: Option<String>,
    result_code: Option<String>,
    exit_reason: Option<String>,
}

pub struct VmmInstance<H> {
    start_operation: Option<Arc<StartOperation>>,
    recovered_start: bool,
    vm_id: String,
    state: Mutex<InstanceState<H>>,
    /// The instance is the runtime's resource owner. Global indexes only borrow it.
    binding: Mutex<Option<Arc<RuntimeBinding>>>,
    cleanup_complete: Mutex<bool>,
    journal_io: Mutex<()>,
}

impl<H> VmmInstance<H> {
    fn new(vm_id: String) -> Self {
        Self {
            start_operation: None,
            recovered_start: false,
            vm_id,
            state: Mutex::new(InstanceState {
                exit_watched: false,
                lifecycle_state: VmmLifecycleState::Creating,
                pid: None,
                handles: None,
                operation_flags: HashSet::new(),
                last_error: None,
                result_code: None,
                exit_reason: None,
            }),
            binding: Mutex::new(None),
            cleanup_complete: Mutex::new(false),
            journal_io: Mutex::new(()),
        }
    }

    fn cleaned_start_failure(&self, current: &VmmInstanceSnapshot) -> bool {
        current.lifecycle_state == VmmLifecycleState::Failed
            && current.pid.is_none()
            && self.start_operation.as_ref().is_some_and(|operation| *operation.done.borrow())
            // Never await this mutex while holding registry state: cleanup
            // takes these locks in the opposite order. A running cleanup is
            // not a replaceable failure and must be retried later.
            && self.cleanup_complete.try_lock().is_ok_and(|complete| *complete)
    }

    pub async fn start_exit_watch(&self) -> bool {
        let mut state = self.state.lock().await;
        if state.exit_watched {
            return false;
        }
        state.exit_watched = true;
        true
    }

    pub fn vm_id(&self) -> &str {
        &self.vm_id
    }

    pub async fn snapshot(&self) -> VmmInstanceSnapshot {
        let state = self.state.lock().await;
        VmmInstanceSnapshot {
            vm_id: self.vm_id.clone(),
            lifecycle_state: state.lifecycle_state,
            pid: state.pid,
            operation_flags: state.operation_flags.clone(),
            last_error: state.last_error.clone(),
            result_code: state.result_code.clone(),
            exit_reason: state.exit_reason.clone(),
        }
    }

    pub async fn runtime_handles(&self) -> Option<Arc<H>> {
        self.state.lock().await.handles.clone()
    }

    async fn runtime_binding(&self) -> Option<Arc<RuntimeBinding>> {
        self.binding.lock().await.clone()
    }
}

impl<H> std::fmt::Debug for VmmInstance<H> {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("VmmInstance")
            .field("vm_id", &self.vm_id)
            .finish_non_exhaustive()
    }
}

pub type CleanupCallback<H> = Arc<
    dyn Fn(Arc<VmmInstance<H>>) -> BoxFuture<Result<(), VmRepositoryError>> + Send + Sync + 'static,
>;

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
struct OperationKey {
    vm_id: String,
    operation: VmmOperation,
}

struct SharedResult {
    result: watch::Sender<Option<OperationResult>>,
}

impl SharedResult {
    fn new() -> Self {
        let (result, _) = watch::channel(None);
        Self { result }
    }

    async fn complete(&self, result: OperationResult) {
        self.result.send_replace(Some(result));
    }

    async fn wait(&self) -> OperationResult {
        let mut result = self.result.subscribe();
        loop {
            if let Some(result) = result.borrow_and_update().clone() {
                return result;
            }
            result
                .changed()
                .await
                .expect("registry retains the operation result sender");
        }
    }
}

struct RegistryState<H> {
    instances: HashMap<String, Arc<VmmInstance<H>>>,
    inflight: HashMap<OperationKey, Arc<SharedResult>>,
    start_by_vm: HashMap<String, Arc<SharedResult>>,
}

struct VmRepositoryInner<H> {
    daemon_epoch: String,
    state: Arc<Mutex<RegistryState<H>>>,
    cleanup: CleanupCallback<H>,
    start_slots: Arc<Semaphore>,
    start_journal: Arc<StdMutex<Option<StartJournal>>>,
    start_timeout: Arc<StdMutex<Duration>>,

    runtime_index: Arc<RuntimeIndex>,
    launcher: LaunchCallback,
    semaphore: Arc<Semaphore>,
    required_capabilities: Vec<Capability>,
    request_timeout: Duration,
    runtime_start_timeout: Duration,
    daemon_identity: String,
    remote_fault_api: Option<Arc<dyn RemoteFaultApi>>,
    remote_fault_limits: RemoteFaultLimits,
    release_hooks: Arc<StdMutex<Vec<RuntimeReleaseHook>>>,
    one_shot_submissions: Mutex<HashSet<RuntimeSubmission>>,
    source_locks: Mutex<HashMap<String, Arc<Mutex<()>>>>,
    /// CLH attachment metadata retained by the local snapshot lease. Snapshot
    /// owns the native sources; this cache never owns a snapshot or a VM.
    local_snapshot_backings: LocalSnapshotBackingLeases,
}

pub struct VmRepository<H = Box<dyn VMMClientInterface>> {
    inner: Arc<VmRepositoryInner<H>>,
}

impl<H> Clone for VmRepository<H> {
    fn clone(&self) -> Self {
        Self {
            inner: Arc::clone(&self.inner),
        }
    }
}

impl<H: Send + Sync + 'static> VmRepository<H> {
    #[cfg(test)]
    fn new_for_lifecycle_test(cleanup: Option<CleanupCallback<H>>) -> Self {
        let daemon_epoch = Uuid::new_v4().simple().to_string();
        let cleanup = cleanup.unwrap_or_else(|| Arc::new(|_| Box::pin(async { Ok(()) })));
        Self {
            inner: Arc::new(VmRepositoryInner {
                daemon_epoch,
                state: Arc::new(Mutex::new(RegistryState {
                    instances: HashMap::new(),
                    inflight: HashMap::new(),
                    start_by_vm: HashMap::new(),
                })),
                cleanup,
                start_slots: Arc::new(Semaphore::new(64)),
                start_journal: Arc::new(StdMutex::new(None)),
                start_timeout: Arc::new(StdMutex::new(Duration::from_secs(300))),
                // The generic fixtures exercise the same instance state
                // machine. CLH execution is available only for client handles.
                runtime_index: Arc::new(RuntimeIndex::new(None)),
                launcher: Arc::new(|spec| {
                    Box::pin(async move { launch_client(spec).map_err(Into::into) })
                }),
                semaphore: Arc::new(Semaphore::new(MAX_CONCURRENT_STARTS)),
                required_capabilities: Vec::new(),
                request_timeout: Duration::from_secs(30),
                runtime_start_timeout: Duration::from_secs(300),
                daemon_identity: Uuid::new_v4().simple().to_string(),
                remote_fault_api: None,
                remote_fault_limits: RemoteFaultLimits::default(),
                release_hooks: Arc::new(StdMutex::new(Vec::new())),
                one_shot_submissions: Mutex::new(HashSet::new()),
                source_locks: Mutex::new(HashMap::new()),
                local_snapshot_backings: LocalSnapshotBackingLeases::default(),
            }),
        }
    }

    pub fn daemon_epoch(&self) -> &str {
        &self.inner.daemon_epoch
    }

    pub fn require_daemon_epoch(&self, daemon_epoch: &str) -> Result<(), VmRepositoryError> {
        if daemon_epoch != self.inner.daemon_epoch {
            return Err(VmRepositoryError::StaleDaemonEpoch);
        }
        Ok(())
    }

    pub(crate) async fn source_lock(&self, source_id: &str) -> Arc<Mutex<()>> {
        self.inner
            .source_locks
            .lock()
            .await
            .entry(source_id.into())
            .or_insert_with(|| Arc::new(Mutex::new(())))
            .clone()
    }

    fn journal(&self) -> Option<StartJournal> {
        self.inner
            .start_journal
            .lock()
            .expect("start journal configuration poisoned")
            .clone()
    }

    pub(crate) fn configure_start_journal(
        &self,
        directory: impl AsRef<Path>,
        timeout: Duration,
    ) -> Result<(), VmRepositoryError> {
        if timeout.is_zero() {
            return invalid("asynchronous start timeout must be positive");
        }
        let (journal, records) = StartJournal::open(directory.as_ref())
            .map_err(|error| VmRepositoryError::Runtime(format!("async start journal: {error}")))?;
        let mut state = self.inner.state.try_lock().map_err(|_| {
            VmRepositoryError::InvalidState(
                "cannot configure start journal while registry operations are running".into(),
            )
        })?;
        for record in records {
            // RUNNING must be established by controlled adoption, never by a
            // durable status file without a corresponding process/handle.
            if matches!(record.state, JournalState::Running | JournalState::Stopped)
                || state.instances.contains_key(&record.sandbox_id)
            {
                continue;
            }
            let mut instance = VmmInstance::new(record.sandbox_id.clone());
            instance.start_operation = Some(StartOperation::new(record.snapshot_id, true, None));
            instance.recovered_start = true;
            let instance_state = instance.state.get_mut();
            instance_state.lifecycle_state = VmmLifecycleState::Failed;
            instance_state.last_error = if matches!(record.state, JournalState::Starting) {
                Some("snapd restarted before asynchronous start completed".into())
            } else {
                record.last_error
            };
            instance_state.result_code = if matches!(record.state, JournalState::Starting) {
                Some("INTERNAL".into())
            } else {
                record.result_code
            };
            *instance.cleanup_complete.get_mut() = true;
            state
                .instances
                .insert(record.sandbox_id, Arc::new(instance));
        }
        *self
            .inner
            .start_journal
            .lock()
            .expect("start journal configuration poisoned") = Some(journal);
        *self
            .inner
            .start_timeout
            .lock()
            .expect("start timeout configuration poisoned") = timeout;
        Ok(())
    }

    pub(crate) fn start_timeout(&self) -> Duration {
        *self
            .inner
            .start_timeout
            .lock()
            .expect("start timeout configuration poisoned")
    }

    pub(crate) async fn begin_async_start(
        &self,
        vm_id: &str,
        snapshot_id: &str,
    ) -> Result<Arc<VmmInstance<H>>, VmRepositoryError> {
        validate_identity(vm_id)?;
        if snapshot_id.is_empty() {
            return invalid("snapshot identity must not be empty");
        }
        let mut state = self.inner.state.lock().await;
        if state.inflight.contains_key(&OperationKey {
            vm_id: vm_id.into(),
            operation: VmmOperation::Stop,
        }) {
            return Err(VmRepositoryError::InvalidState(
                "VMM stop is in progress".into(),
            ));
        }
        if let Some(instance) = state.instances.get(vm_id) {
            let current = instance.snapshot().await;
            let finished_failure = current.lifecycle_state == VmmLifecycleState::Failed
                && instance
                    .start_operation
                    .as_ref()
                    .is_some_and(|operation| *operation.done.borrow());
            if !finished_failure {
                return Err(VmRepositoryError::AlreadyExists(vm_id.into()));
            }
            if !instance.cleaned_start_failure(&current) {
                return Err(VmRepositoryError::InvalidState(
                    "previous VMM cleanup must complete before another start".into(),
                ));
            }
        }
        let permit = Arc::clone(&self.inner.start_slots)
            .try_acquire_owned()
            .map_err(|_| {
                VmRepositoryError::InvalidState("asynchronous start queue is full".into())
            })?;
        let operation = StartOperation::new(snapshot_id.into(), false, Some(permit));
        let mut instance = VmmInstance::new(vm_id.into());
        instance.start_operation = Some(Arc::clone(&operation));
        let instance = Arc::new(instance);
        state.instances.insert(vm_id.into(), Arc::clone(&instance));
        drop(state);
        let journal_result = {
            let _io = instance.journal_io.lock().await;
            if let Some(journal) = self.journal() {
                journal.save(&instance.snapshot().await, snapshot_id).await
            } else {
                Ok(())
            }
        };
        if let Err(error) = journal_result {
            let message = format!("async start journal: {error}");
            self.complete_async_start(&instance, Some(("INTERNAL".into(), message.clone())))
                .await;
            return Err(VmRepositoryError::Runtime(message));
        }
        Ok(instance)
    }

    pub(crate) async fn discard_completed_failure(
        &self,
        vm_id: &str,
    ) -> Result<(), VmRepositoryError> {
        let instance = self.inner.state.lock().await.instances.get(vm_id).cloned();
        if let Some(instance) = instance {
            if instance.cleaned_start_failure(&instance.snapshot().await) {
                self.cleanup_once(instance, false)
                    .await
                    .map_err(VmRepositoryError::Runtime)?;
            }
        }
        Ok(())
    }

    #[cfg(test)]
    async fn is_current_instance(&self, starting_instance: &Arc<VmmInstance<H>>) -> bool {
        self.inner
            .state
            .lock()
            .await
            .instances
            .get(starting_instance.vm_id())
            .is_some_and(|instance| Arc::ptr_eq(instance, starting_instance))
    }

    pub(crate) async fn complete_async_start(
        &self,
        starting_instance: &Arc<VmmInstance<H>>,
        failure: Option<(String, String)>,
    ) {
        let was_accepted = starting_instance.start_was_accepted();
        if failure.is_none() {
            starting_instance.accept_start();
        }
        let instance = self
            .inner
            .state
            .lock()
            .await
            .instances
            .get(starting_instance.vm_id())
            .filter(|instance| Arc::ptr_eq(instance, starting_instance))
            .cloned();
        let Some(instance) = instance else {
            starting_instance.start_operation().done.send_replace(true);
            starting_instance
                .start_operation()
                .permit
                .lock()
                .expect("start permit poisoned")
                .take();
            return;
        };
        let cleanup_error = if failure.is_some() {
            self.cleanup_once(Arc::clone(&instance), was_accepted)
                .await
                .err()
        } else {
            None
        };
        let _io = instance.journal_io.lock().await;
        let owns_instance = self
            .inner
            .state
            .lock()
            .await
            .instances
            .get(starting_instance.vm_id())
            .is_some_and(|current| Arc::ptr_eq(current, &instance));
        if owns_instance {
            if let Some((code, message)) = failure {
                let mut current = instance.state.lock().await;
                current.lifecycle_state = VmmLifecycleState::Failed;
                current.result_code = Some(code);
                current.last_error = Some(match cleanup_error {
                    Some(error) => format!("{message}; cleanup failed: {error}"),
                    None => message,
                });
            }
            if let Some(journal) = self.journal() {
                if let Err(error) = journal
                    .save(
                        &instance.snapshot().await,
                        &starting_instance.start_operation().snapshot_id,
                    )
                    .await
                {
                    eprintln!(
                        "cannot persist async start completion sandbox_id={}: {error}",
                        starting_instance.vm_id
                    );
                }
            }
        }
        starting_instance.start_operation().done.send_replace(true);
        starting_instance
            .start_operation()
            .permit
            .lock()
            .expect("start permit poisoned")
            .take();
    }

    pub(crate) async fn close_async_starts(&self) -> Result<(), VmRepositoryError> {
        self.inner.start_slots.close();
        let operations = self
            .inner
            .state
            .lock()
            .await
            .instances
            .values()
            .filter_map(|instance| instance.start_operation.clone())
            .collect::<Vec<_>>();
        for operation in &operations {
            operation.request_cancel();
        }
        for operation in operations {
            operation
                .done
                .subscribe()
                .wait_for(|done| *done)
                .await
                .map_err(|error| VmRepositoryError::Runtime(error.to_string()))?;
        }
        Ok(())
    }

    /// Install a runtime only after an external controlled-adoption
    /// transaction has rebuilt all local owners and committed at the VMM.
    ///
    /// Unlike `start`, this never runs user work while holding registry state.
    /// The caller retains responsibility for rolling back `started` if this
    /// exact atomic installation fails.
    pub async fn install_adopted(
        &self,
        vm_id: &str,
        started: StartedRuntime<H>,
    ) -> Result<Arc<VmmInstance<H>>, (VmRepositoryError, StartedRuntime<H>)> {
        if let Err(error) = validate_identity(vm_id) {
            return Err((error, started));
        }
        if started.pid == 0 {
            return Err((
                VmRepositoryError::Invalid("adopted runtime PID must be positive".into()),
                started,
            ));
        }
        let mut state = self.inner.state.lock().await;
        let replaceable_recovered = if let Some(instance) = state.instances.get(vm_id) {
            instance.recovered_start
                && instance.cleaned_start_failure(&instance.snapshot().await)
                && instance.journal_io.try_lock().is_ok()
        } else {
            true
        };
        if !replaceable_recovered
            || state.start_by_vm.contains_key(vm_id)
            || state.inflight.keys().any(|key| key.vm_id == vm_id)
        {
            return Err((VmRepositoryError::AlreadyExists(vm_id.into()), started));
        }
        let instance = Arc::new(VmmInstance::new(vm_id.into()));
        {
            let mut instance_state = instance.state.lock().await;
            instance_state.lifecycle_state = VmmLifecycleState::Running;
            instance_state.pid = Some(started.pid);
            instance_state.handles = Some(Arc::new(started.handles));
        }
        state.instances.insert(vm_id.into(), instance.clone());
        Ok(instance)
    }

    pub async fn start<F, Fut>(
        &self,
        vm_id: &str,
        start: F,
    ) -> Result<OperationResult, VmRepositoryError>
    where
        F: FnOnce(Arc<VmmInstance<H>>) -> Fut + Send + 'static,
        Fut: Future<Output = Result<StartedRuntime<H>, VmRepositoryError>> + Send + 'static,
    {
        self.start_inner(vm_id, None, start).await
    }

    async fn start_inner<F, Fut>(
        &self,
        vm_id: &str,
        starting_instance: Option<&Arc<VmmInstance<H>>>,
        start: F,
    ) -> Result<OperationResult, VmRepositoryError>
    where
        F: FnOnce(Arc<VmmInstance<H>>) -> Fut + Send + 'static,
        Fut: Future<Output = Result<StartedRuntime<H>, VmRepositoryError>> + Send + 'static,
    {
        validate_identity(vm_id)?;
        if starting_instance.is_none() {
            self.discard_completed_failure(vm_id).await?;
        }
        let key = OperationKey {
            vm_id: vm_id.into(),
            operation: VmmOperation::Start,
        };
        let (instance, shared) = {
            let mut state = self.inner.state.lock().await;
            let instance = Self::startable_instance(&state, vm_id, starting_instance)
                .await?
                .unwrap_or_else(|| Arc::new(VmmInstance::new(vm_id.into())));
            let shared = Arc::new(SharedResult::new());
            state.instances.insert(vm_id.into(), instance.clone());
            state.inflight.insert(key.clone(), shared.clone());
            state.start_by_vm.insert(vm_id.into(), shared.clone());
            (instance, shared)
        };
        let registry = self.clone();
        let result = shared.clone();
        tokio::spawn(async move {
            registry.run_start(key, instance, start, result).await;
        });
        Ok(shared.wait().await)
    }

    async fn startable_instance(
        state: &RegistryState<H>,
        vm_id: &str,
        expected: Option<&Arc<VmmInstance<H>>>,
    ) -> Result<Option<Arc<VmmInstance<H>>>, VmRepositoryError> {
        if state.inflight.contains_key(&OperationKey {
            vm_id: vm_id.into(),
            operation: VmmOperation::Stop,
        }) {
            return Err(VmRepositoryError::InvalidState(
                "VMM stop is in progress".into(),
            ));
        }
        if state.inflight.contains_key(&OperationKey {
            vm_id: vm_id.into(),
            operation: VmmOperation::Start,
        }) {
            return Err(VmRepositoryError::AlreadyExists(vm_id.into()));
        }
        match (state.instances.get(vm_id), expected) {
            (None, None) => Ok(None),
            (Some(current), Some(expected)) if Arc::ptr_eq(current, expected) => {
                let instance_state = current.state.lock().await;
                if instance_state.lifecycle_state != VmmLifecycleState::Creating
                    || current
                        .start_operation
                        .as_ref()
                        .is_none_or(|operation| operation.cancel.load(Ordering::Acquire))
                {
                    return Err(VmRepositoryError::InvalidState(
                        "VMM is not available for startup".into(),
                    ));
                }
                Ok(Some(Arc::clone(current)))
            }
            (_, Some(_)) => Err(VmRepositoryError::InvalidState(
                "start no longer owns the current instance".into(),
            )),
            (Some(_), None) => Err(VmRepositoryError::AlreadyExists(vm_id.into())),
        }
    }

    pub(crate) async fn ensure_startable(
        &self,
        vm_id: &str,
        expected: Option<&Arc<VmmInstance<H>>>,
    ) -> Result<(), VmRepositoryError> {
        if expected.is_none() {
            self.discard_completed_failure(vm_id).await?;
        }
        let state = self.inner.state.lock().await;
        Self::startable_instance(&state, vm_id, expected)
            .await
            .map(|_| ())
    }

    pub(crate) async fn ensure_running(&self, vm_id: &str) -> Result<(), VmRepositoryError> {
        let state = self.inner.state.lock().await;
        let instance = state
            .instances
            .get(vm_id)
            .ok_or_else(|| VmRepositoryError::NotFound(vm_id.into()))?;
        if state.inflight.contains_key(&OperationKey {
            vm_id: vm_id.into(),
            operation: VmmOperation::Stop,
        }) || instance.state.lock().await.lifecycle_state != VmmLifecycleState::Running
        {
            return Err(VmRepositoryError::InvalidState(
                "operation requires a RUNNING VMM with no stop in progress".into(),
            ));
        }
        Ok(())
    }

    pub async fn stop(
        &self,
        vm_id: &str,
        reason: &str,
    ) -> Result<OperationResult, VmRepositoryError> {
        validate_identity(vm_id)?;
        if reason.is_empty() {
            return invalid("stop reason must not be empty");
        }
        let key = OperationKey {
            vm_id: vm_id.into(),
            operation: VmmOperation::Stop,
        };
        let (instance, pending_start, shared) = {
            let mut state = self.inner.state.lock().await;
            if state.inflight.contains_key(&key) {
                return Err(VmRepositoryError::InvalidState(
                    "VMM stop is in progress".into(),
                ));
            }
            let instance = state
                .instances
                .get(vm_id)
                .cloned()
                .ok_or_else(|| VmRepositoryError::NotFound(vm_id.into()))?;
            let pending_start = state.start_by_vm.get(vm_id).cloned();
            let instance_state = instance.state.lock().await;
            if instance_state.lifecycle_state == VmmLifecycleState::Creating
                && instance.start_operation.is_none()
                && pending_start.is_none()
            {
                return Err(VmRepositoryError::InvalidState(
                    "CREATING instance has no start operation".into(),
                ));
            }
            drop(instance_state);
            let shared = Arc::new(SharedResult::new());
            state.inflight.insert(key.clone(), shared.clone());
            (instance, pending_start, shared)
        };
        let registry = self.clone();
        let reason = reason.to_owned();
        let result = shared.clone();
        tokio::spawn(async move {
            registry
                .run_stop(key, instance, pending_start, reason, result)
                .await;
        });
        Ok(shared.wait().await)
    }

    pub async fn runtime_exited(&self, vm_id: &str, exit_reason: &str) -> bool {
        let instance = {
            let state = self.inner.state.lock().await;
            state.instances.get(vm_id).cloned()
        };
        let Some(instance) = instance else {
            return false;
        };
        self.runtime_exited_instance(instance, exit_reason).await
    }

    pub async fn runtime_exited_instance(
        &self,
        instance: Arc<VmmInstance<H>>,
        exit_reason: &str,
    ) -> bool {
        if !self
            .inner
            .state
            .lock()
            .await
            .instances
            .get(instance.vm_id())
            .is_some_and(|current| Arc::ptr_eq(current, &instance))
        {
            return false;
        }
        let during_create = {
            let mut state = instance.state.lock().await;
            state.exit_reason = Some(exit_reason.into());
            match state.lifecycle_state {
                VmmLifecycleState::Creating => true,
                VmmLifecycleState::Running => {
                    state.lifecycle_state = VmmLifecycleState::Stopping;
                    false
                }
                VmmLifecycleState::Stopping | VmmLifecycleState::Failed => false,
            }
        };
        if !during_create {
            let preserve_start_failure = instance
                .start_operation
                .as_ref()
                .is_some_and(|operation| !*operation.done.borrow());
            if let Err(error) = self
                .cleanup_once(instance.clone(), preserve_start_failure)
                .await
            {
                instance.state.lock().await.last_error = Some(error);
            }
        }
        true
    }

    pub async fn try_begin_operation(
        &self,
        vm_id: &str,
        flag: VmmOperationFlag,
    ) -> Result<bool, VmRepositoryError> {
        let instance = self
            .instance(vm_id)
            .await
            .ok_or_else(|| VmRepositoryError::NotFound(vm_id.into()))?;
        let mut state = instance.state.lock().await;
        if state.lifecycle_state != VmmLifecycleState::Running {
            return Err(VmRepositoryError::InvalidState(
                "runtime operation flags require a RUNNING VMM".into(),
            ));
        }
        Ok(state.operation_flags.insert(flag))
    }

    pub async fn finish_operation(&self, vm_id: &str, flag: VmmOperationFlag) -> bool {
        let Some(instance) = self.instance(vm_id).await else {
            return false;
        };
        let removed = instance.state.lock().await.operation_flags.remove(&flag);
        removed
    }

    pub async fn get(&self, vm_id: &str) -> Option<VmmInstanceSnapshot> {
        let instance = self.instance(vm_id).await?;
        Some(instance.snapshot().await)
    }

    pub async fn vm_ids(&self) -> HashSet<String> {
        self.inner
            .state
            .lock()
            .await
            .instances
            .keys()
            .cloned()
            .collect()
    }

    pub async fn instance(&self, vm_id: &str) -> Option<Arc<VmmInstance<H>>> {
        self.inner.state.lock().await.instances.get(vm_id).cloned()
    }

    async fn run_start<F, Fut>(
        &self,
        key: OperationKey,
        instance: Arc<VmmInstance<H>>,
        start: F,
        shared: Arc<SharedResult>,
    ) where
        F: FnOnce(Arc<VmmInstance<H>>) -> Fut + Send + 'static,
        Fut: Future<Output = Result<StartedRuntime<H>, VmRepositoryError>> + Send + 'static,
    {
        let result = match start(instance.clone()).await {
            Ok(runtime) => {
                let present = {
                    let state = self.inner.state.lock().await;
                    state
                        .instances
                        .get(instance.vm_id())
                        .is_some_and(|current| Arc::ptr_eq(current, &instance))
                };
                if !present {
                    operation_result(
                        instance.vm_id(),
                        VmmOperation::Start,
                        false,
                        OperationCode::CreateFailed,
                        Some("VMM was removed while its start operation was running".into()),
                    )
                } else {
                    let exited = {
                        let mut state = instance.state.lock().await;
                        state.pid = Some(runtime.pid);
                        state.handles = Some(Arc::new(runtime.handles));
                        let exited = state.exit_reason.is_some();
                        state.lifecycle_state = if exited {
                            VmmLifecycleState::Stopping
                        } else {
                            VmmLifecycleState::Running
                        };
                        exited
                    };
                    if exited {
                        let cleanup_error = self.cleanup_once(instance.clone(), true).await.err();
                        let state = instance.state.lock().await;
                        let mut message = state
                            .exit_reason
                            .clone()
                            .unwrap_or_else(|| "VMM exited while it was being created".into());
                        if let Some(error) = &cleanup_error {
                            message = format!("{message}; cleanup failed: {error}");
                        }
                        operation_result(
                            instance.vm_id(),
                            VmmOperation::Start,
                            false,
                            if cleanup_error.is_some() {
                                OperationCode::CleanupFailed
                            } else {
                                OperationCode::ProcessExited
                            },
                            Some(message),
                        )
                    } else {
                        operation_result(
                            instance.vm_id(),
                            VmmOperation::Start,
                            true,
                            OperationCode::Running,
                            None,
                        )
                    }
                }
            }
            Err(error) => {
                let message = error.to_string();
                instance.state.lock().await.last_error = Some(message.clone());
                let cleanup_error = self.cleanup_once(instance.clone(), true).await.err();
                let message = cleanup_error
                    .as_ref()
                    .map(|cleanup| format!("{message}; cleanup failed: {cleanup}"))
                    .unwrap_or(message);
                operation_result(
                    instance.vm_id(),
                    VmmOperation::Start,
                    false,
                    if cleanup_error.is_some() {
                        OperationCode::CleanupFailed
                    } else {
                        OperationCode::CreateFailed
                    },
                    Some(message),
                )
            }
        };
        self.finish_result(key, result, shared).await;
    }

    async fn run_stop(
        &self,
        key: OperationKey,
        instance: Arc<VmmInstance<H>>,
        pending_start: Option<Arc<SharedResult>>,
        reason: String,
        shared: Arc<SharedResult>,
    ) {
        let cleanup_error = async {
            // The stop owns this exact generation before cancellation can
            // remove it. Other stop/start requests cannot join or replace it.
            if let Some(operation) = &instance.start_operation {
                operation.request_cancel();
                operation
                    .done
                    .subscribe()
                    .wait_for(|done| *done)
                    .await
                    .map_err(|error| error.to_string())?;
            }
            if let Some(pending_start) = pending_start {
                pending_start.wait().await;
            }
            // A capture owns the source through local capture/registration.
            // Cancel preparation before taking this lock: its worker may be
            // waiting for the same lock and must remain able to exit.
            let source_lock = self.source_lock(instance.vm_id()).await;
            let _source_guard = source_lock.lock().await;
            {
                let mut state = instance.state.lock().await;
                if state.lifecycle_state == VmmLifecycleState::Running {
                    state.lifecycle_state = VmmLifecycleState::Stopping;
                }
                if state.exit_reason.is_none() {
                    state.exit_reason = Some(reason);
                }
            }
            self.cleanup_once(instance.clone(), false).await
        }
        .await
        .err();
        let result = match cleanup_error {
            None => operation_result(
                instance.vm_id(),
                VmmOperation::Stop,
                true,
                OperationCode::Stopped,
                None,
            ),
            Some(error) => operation_result(
                instance.vm_id(),
                VmmOperation::Stop,
                false,
                OperationCode::CleanupFailed,
                Some(error),
            ),
        };
        self.finish_result(key, result, shared).await;
    }

    async fn cleanup_once(
        &self,
        instance: Arc<VmmInstance<H>>,
        preserve_start_failure: bool,
    ) -> Result<(), String> {
        let mut complete = instance.cleanup_complete.lock().await;
        if !*complete {
            if let Err(error) = (self.inner.cleanup)(instance.clone()).await {
                let message = error.to_string();
                instance.state.lock().await.last_error = Some(message.clone());
                return Err(message);
            }
            let mut instance_state = instance.state.lock().await;
            instance_state.operation_flags.clear();
            instance_state.handles = None;
            instance_state.pid = None;
            *complete = true;
        }
        {
            let state = self.inner.state.lock().await;
            if !state
                .instances
                .get(instance.vm_id())
                .is_some_and(|current| Arc::ptr_eq(current, &instance))
            {
                return Ok(());
            }
            instance.state.lock().await.lifecycle_state =
                if preserve_start_failure && instance.start_operation.is_some() {
                    VmmLifecycleState::Failed
                } else {
                    VmmLifecycleState::Stopping
                };
        }
        if preserve_start_failure && instance.start_operation.is_some() {
            return Ok(());
        }
        let _io = instance.journal_io.lock().await;
        // A replaced/adopted old generation must never remove the current
        // generation's durable file.
        if !self
            .inner
            .state
            .lock()
            .await
            .instances
            .get(instance.vm_id())
            .is_some_and(|current| Arc::ptr_eq(current, &instance))
        {
            return Ok(());
        }
        if let Some(journal) = self.journal() {
            if let Err(error) = journal.remove(instance.vm_id()).await {
                let message = error.to_string();
                instance.state.lock().await.last_error = Some(message.clone());
                return Err(message);
            }
        }
        let mut state = self.inner.state.lock().await;
        if state
            .instances
            .get(instance.vm_id())
            .is_some_and(|current| Arc::ptr_eq(current, &instance))
        {
            state.instances.remove(instance.vm_id());
        }
        Ok(())
    }

    async fn finish_result(
        &self,
        key: OperationKey,
        result: OperationResult,
        shared: Arc<SharedResult>,
    ) {
        {
            let mut state = self.inner.state.lock().await;
            if state
                .inflight
                .get(&key)
                .is_some_and(|pending| Arc::ptr_eq(pending, &shared))
            {
                state.inflight.remove(&key);
            }
            if key.operation == VmmOperation::Start
                && state
                    .start_by_vm
                    .get(&key.vm_id)
                    .is_some_and(|pending| Arc::ptr_eq(pending, &shared))
            {
                state.start_by_vm.remove(&key.vm_id);
            }
        }
        shared.complete(result).await;
    }
}

fn validate_identity(vm_id: &str) -> Result<(), VmRepositoryError> {
    if vm_id.is_empty() {
        return invalid("VM identity must not be empty");
    }
    Ok(())
}

fn operation_result(
    vm_id: &str,
    operation: VmmOperation,
    success: bool,
    code: OperationCode,
    message: Option<String>,
) -> OperationResult {
    OperationResult {
        vm_id: vm_id.into(),
        operation,
        success,
        code,
        message,
    }
}

fn invalid<T>(message: &str) -> Result<T, VmRepositoryError> {
    Err(VmRepositoryError::Invalid(message.into()))
}

#[cfg(test)]
mod start_lifecycle_tests {
    use super::*;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicUsize, Ordering};

    struct TestRoot(PathBuf);

    impl TestRoot {
        fn new() -> Self {
            Self(std::env::temp_dir().join(format!("rock-vm-start-registry-{}", Uuid::new_v4())))
        }
    }

    impl Drop for TestRoot {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    async fn starting_instance(
        registry: &VmRepository<usize>,
        id: &str,
    ) -> Arc<VmmInstance<usize>> {
        let starting_instance = registry.begin_async_start(id, "snapshot").await.unwrap();
        starting_instance.accept_start();
        starting_instance
    }

    async fn launch(registry: &VmRepository<usize>, starting_instance: &Arc<VmmInstance<usize>>) {
        let result = registry
            .start_inner(
                starting_instance.vm_id(),
                Some(starting_instance),
                |_| async { StartedRuntime::new(123, 7) },
            )
            .await
            .unwrap();
        assert!(result.success);
    }

    #[tokio::test]
    async fn preparation_is_one_instance_and_completed_failure_can_retry() {
        let registry = VmRepository::<usize>::new_for_lifecycle_test(None);
        let first = starting_instance(&registry, "vm").await;
        let state = registry.get("vm").await.unwrap();
        assert_eq!(state.lifecycle_state, VmmLifecycleState::Creating);
        assert!(state.pid.is_none());
        assert!(matches!(
            registry.begin_async_start("vm", "snapshot").await,
            Err(VmRepositoryError::AlreadyExists(_))
        ));
        registry
            .complete_async_start(
                &first,
                Some(("INVALID_ARGUMENT".into(), "bad snapshot".into())),
            )
            .await;
        let state = registry.get("vm").await.unwrap();
        assert_eq!(state.lifecycle_state, VmmLifecycleState::Failed);
        assert_eq!(state.result_code.as_deref(), Some("INVALID_ARGUMENT"));
        assert_eq!(state.last_error.as_deref(), Some("bad snapshot"));
        let next = starting_instance(&registry, "vm").await;
        assert!(!registry.is_current_instance(&first).await);
        registry
            .complete_async_start(&first, Some(("INTERNAL".into(), "old completion".into())))
            .await;
        assert!(registry.is_current_instance(&next).await);
        assert!(!Arc::ptr_eq(&first, &next));
        launch(&registry, &next).await;
        registry.complete_async_start(&next, None).await;
        assert_eq!(
            registry.get("vm").await.unwrap().lifecycle_state,
            VmmLifecycleState::Running
        );
    }

    #[tokio::test]
    async fn only_current_instance_can_turn_preparation_into_launch() {
        let registry = VmRepository::<usize>::new_for_lifecycle_test(None);
        let first = starting_instance(&registry, "vm").await;
        let bare = registry
            .start("vm", |_| async { StartedRuntime::new(1, 1) })
            .await;
        assert!(matches!(bare, Err(VmRepositoryError::AlreadyExists(_))));
        registry
            .complete_async_start(&first, Some(("INTERNAL".into(), "first failed".into())))
            .await;
        let next = starting_instance(&registry, "vm").await;
        assert!(!Arc::ptr_eq(&first, &next));
        assert_eq!(
            registry.get("vm").await.unwrap().lifecycle_state,
            VmmLifecycleState::Creating
        );
        assert!(matches!(
            registry.ensure_startable("vm", Some(&first)).await,
            Err(VmRepositoryError::InvalidState(_))
        ));
        registry.ensure_startable("vm", Some(&next)).await.unwrap();
        let stale = registry
            .start_inner("vm", Some(&first), |_| async {
                panic!("a stale instance must not launch a runtime")
            })
            .await;
        assert!(matches!(stale, Err(VmRepositoryError::InvalidState(_))));
        assert!(registry.is_current_instance(&next).await);
        assert_eq!(
            registry.get("vm").await.unwrap().lifecycle_state,
            VmmLifecycleState::Creating
        );
        launch(&registry, &next).await;
        registry.complete_async_start(&next, None).await;
        registry
            .start("ordinary", |_| async { StartedRuntime::new(77, 8) })
            .await
            .unwrap();
        let running = registry.instance("ordinary").await.unwrap();
        assert!(running.start_operation.is_none());
        assert!(matches!(
            registry.ensure_startable("ordinary", Some(&running)).await,
            Err(VmRepositoryError::InvalidState(_))
        ));
        assert!(matches!(
            registry
                .start_inner("ordinary", Some(&running), |_| async {
                    panic!("a running instance cannot become a prepared start")
                })
                .await,
            Err(VmRepositoryError::InvalidState(_))
        ));
    }

    #[tokio::test]
    async fn stop_cancels_late_preparation_waiting_for_a_business_lock() {
        let registry = VmRepository::<usize>::new_for_lifecycle_test(None);
        let business_lock = Arc::new(Mutex::new(()));
        // Model stop already holding the orchestration source lock when a
        // late start is admitted and its worker queues for that same lock.
        let held = business_lock.lock().await;
        let starting_instance = starting_instance(&registry, "vm").await;
        let worker_registry = registry.clone();
        let worker_lock = Arc::clone(&business_lock);
        let worker = tokio::spawn(async move {
            tokio::select! {
                _guard = worker_lock.lock() => panic!("stop still owns the business lock"),
                _ = starting_instance.start_cancelled() => {},
            }
            worker_registry
                .complete_async_start(
                    &starting_instance,
                    Some(("INVALID_STATE".into(), "cancelled".into())),
                )
                .await;
        });
        let stopped =
            tokio::time::timeout(Duration::from_secs(2), registry.stop("vm", "test stop"))
                .await
                .unwrap()
                .unwrap();
        assert!(stopped.success);
        assert!(registry.get("vm").await.is_none());
        drop(held);
        worker.await.unwrap();
        tokio::time::timeout(Duration::from_secs(2), registry.close_async_starts())
            .await
            .unwrap()
            .unwrap();
    }

    #[tokio::test]
    async fn cleaned_failed_instance_is_removed_by_stop_without_retaining_a_tombstone() {
        let root = TestRoot::new();
        let registry = VmRepository::<usize>::new_for_lifecycle_test(None);
        registry
            .configure_start_journal(&root.0, Duration::from_secs(30))
            .unwrap();
        let starting_instance = starting_instance(&registry, "vm").await;
        registry
            .complete_async_start(
                &starting_instance,
                Some(("INTERNAL".into(), "prepare failed".into())),
            )
            .await;
        assert_eq!(std::fs::read_dir(&root.0).unwrap().count(), 1);
        let stopped = registry.stop("vm", "delete failed sandbox").await.unwrap();
        assert!(stopped.success);
        assert!(registry.get("vm").await.is_none());
        assert_eq!(std::fs::read_dir(&root.0).unwrap().count(), 0);
    }

    #[tokio::test]
    async fn cleanup_failure_retains_runtime_until_a_successful_stop_retry() {
        let calls = Arc::new(AtomicUsize::new(0));
        let cleanup_calls = Arc::clone(&calls);
        let registry = VmRepository::<usize>::new_for_lifecycle_test(Some(Arc::new(move |_| {
            let calls = Arc::clone(&cleanup_calls);
            Box::pin(async move {
                if calls.fetch_add(1, Ordering::AcqRel) == 0 {
                    Err(VmRepositoryError::Runtime(
                        "injected cleanup failure".into(),
                    ))
                } else {
                    Ok(())
                }
            })
        })));
        let starting_instance = starting_instance(&registry, "vm").await;
        launch(&registry, &starting_instance).await;
        registry
            .complete_async_start(&starting_instance, None)
            .await;
        let first = registry.stop("vm", "test stop").await.unwrap();
        assert!(!first.success);
        let state = registry.get("vm").await.unwrap();
        assert_eq!(state.pid, Some(123));
        assert!(registry
            .instance("vm")
            .await
            .unwrap()
            .runtime_handles()
            .await
            .is_some());
        assert!(matches!(
            registry.begin_async_start("vm", "snapshot").await,
            Err(_)
        ));
        let second = registry.stop("vm", "test stop").await.unwrap();
        assert!(second.success);
        assert!(registry.get("vm").await.is_none());
        assert_eq!(calls.load(Ordering::Acquire), 2);
    }

    #[tokio::test]
    async fn exit_before_whole_start_completion_still_reports_failed() {
        let registry = VmRepository::<usize>::new_for_lifecycle_test(None);
        let starting_instance = starting_instance(&registry, "vm").await;
        launch(&registry, &starting_instance).await;
        assert!(registry.runtime_exited("vm", "early process exit").await);
        registry
            .complete_async_start(
                &starting_instance,
                Some(("INTERNAL".into(), "guest readiness lost".into())),
            )
            .await;
        let state = registry.get("vm").await.unwrap();
        assert_eq!(state.lifecycle_state, VmmLifecycleState::Failed);
        assert!(state.pid.is_none());
        assert_eq!(state.last_error.as_deref(), Some("guest readiness lost"));
        assert!(registry.stop("vm", "cleanup").await.unwrap().success);
        assert!(registry.get("vm").await.is_none());
    }

    #[tokio::test]
    async fn synchronous_start_replaces_a_cleaned_failure_with_new_runtime() {
        let registry = VmRepository::<usize>::new_for_lifecycle_test(None);
        let starting_instance = starting_instance(&registry, "vm").await;
        registry
            .complete_async_start(
                &starting_instance,
                Some(("INTERNAL".into(), "failed".into())),
            )
            .await;
        let result = registry
            .start("vm", |_| async { StartedRuntime::new(44, 8) })
            .await
            .unwrap();
        assert!(result.success);
        let state = registry.get("vm").await.unwrap();
        assert_eq!(state.pid, Some(44));
        assert!(!registry.is_current_instance(&starting_instance).await);
        registry
            .complete_async_start(
                &starting_instance,
                Some(("INTERNAL".into(), "stale failed".into())),
            )
            .await;
        assert_eq!(
            registry.get("vm").await.unwrap().lifecycle_state,
            VmmLifecycleState::Running
        );
    }

    #[tokio::test]
    async fn duplicate_start_is_rejected_while_starting_and_running() {
        let registry = VmRepository::<usize>::new_for_lifecycle_test(None);
        let (entered_tx, entered_rx) = tokio::sync::oneshot::channel();
        let (release_tx, release_rx) = tokio::sync::oneshot::channel();
        let owner_registry = registry.clone();
        let owner = tokio::spawn(async move {
            owner_registry
                .start("vm", |_| async {
                    entered_tx.send(()).unwrap();
                    release_rx.await.unwrap();
                    StartedRuntime::new(123, 7)
                })
                .await
        });
        entered_rx.await.unwrap();
        assert!(matches!(
            registry
                .start("vm", |_| async {
                    panic!("duplicate start must not launch a runtime")
                })
                .await,
            Err(VmRepositoryError::AlreadyExists(_))
        ));
        release_tx.send(()).unwrap();
        assert!(owner.await.unwrap().unwrap().success);
        assert!(matches!(
            registry
                .start("vm", |_| async {
                    panic!("running runtime must not be returned as a new start")
                })
                .await,
            Err(VmRepositoryError::AlreadyExists(_))
        ));
    }

    #[tokio::test]
    async fn duplicate_stop_is_rejected_and_stopped_runtime_is_not_found() {
        let entered = Arc::new(Notify::new());
        let release = Arc::new(Notify::new());
        let cleanup_entered = Arc::clone(&entered);
        let cleanup_release = Arc::clone(&release);
        let registry = VmRepository::<usize>::new_for_lifecycle_test(Some(Arc::new(move |_| {
            let entered = Arc::clone(&cleanup_entered);
            let release = Arc::clone(&cleanup_release);
            Box::pin(async move {
                entered.notify_one();
                release.notified().await;
                Ok(())
            })
        })));
        assert!(matches!(
            registry.stop("missing", "test stop").await,
            Err(VmRepositoryError::NotFound(_))
        ));
        registry
            .start("vm", |_| async { StartedRuntime::new(123, 7) })
            .await
            .unwrap();
        let owner_registry = registry.clone();
        let owner = tokio::spawn(async move { owner_registry.stop("vm", "test stop").await });
        entered.notified().await;
        assert!(matches!(
            registry.stop("vm", "test stop").await,
            Err(VmRepositoryError::InvalidState(_))
        ));
        release.notify_one();
        assert!(owner.await.unwrap().unwrap().success);
        assert!(matches!(
            registry.stop("vm", "test stop").await,
            Err(VmRepositoryError::NotFound(_))
        ));
    }

    #[tokio::test]
    async fn legacy_journal_recovery_does_not_invent_running_processes_or_block_adoption() {
        let root = TestRoot::new();
        std::fs::create_dir_all(&root.0).unwrap();
        for (id, state) in [
            ("pending", "STARTING"),
            ("failed", "FAILED"),
            ("running", "RUNNING"),
            ("stopped", "STOPPED"),
        ] {
            std::fs::write(
                root.0.join(format!("{id}.json")),
                serde_json::to_vec(&serde_json::json!({
                    "sandbox_id":id, "snapshot_id":"snapshot", "request_hash":"hash", "state":state,
                    "last_error":"old error", "result_code":"INTERNAL",
                }))
                .unwrap(),
            )
            .unwrap();
        }
        let registry = VmRepository::<usize>::new_for_lifecycle_test(None);
        registry
            .configure_start_journal(&root.0, Duration::from_secs(30))
            .unwrap();
        assert!(registry.get("running").await.is_none());
        assert!(registry.get("stopped").await.is_none());
        assert_eq!(
            registry.get("pending").await.unwrap().lifecycle_state,
            VmmLifecycleState::Failed
        );
        assert_eq!(
            registry.get("failed").await.unwrap().last_error.as_deref(),
            Some("old error")
        );
        registry
            .install_adopted("pending", StartedRuntime::new(77, 4).unwrap())
            .await
            .unwrap();
        let recovered = registry.get("pending").await.unwrap();
        assert_eq!(recovered.lifecycle_state, VmmLifecycleState::Running);
        assert_eq!(recovered.pid, Some(77));
    }

    #[tokio::test]
    async fn journal_without_request_metadata_preserves_failed_start_state() {
        let root = TestRoot::new();
        let registry = VmRepository::<usize>::new_for_lifecycle_test(None);
        registry
            .configure_start_journal(&root.0, Duration::from_secs(30))
            .unwrap();
        let starting_instance = starting_instance(&registry, "vm").await;
        registry
            .complete_async_start(
                &starting_instance,
                Some(("INTERNAL".into(), "prepare failed".into())),
            )
            .await;
        let journal_path = std::fs::read_dir(&root.0)
            .unwrap()
            .next()
            .unwrap()
            .unwrap()
            .path();
        let record: Value = serde_json::from_slice(&std::fs::read(journal_path).unwrap()).unwrap();
        assert_eq!(record["snapshot_id"], "snapshot");
        assert!(record.get("request_id").is_none());
        assert!(record.get("request_hash").is_none());
        drop(registry);

        let recovered = VmRepository::<usize>::new_for_lifecycle_test(None);
        recovered
            .configure_start_journal(&root.0, Duration::from_secs(30))
            .unwrap();
        let instance = recovered.get("vm").await.unwrap();
        assert_eq!(instance.lifecycle_state, VmmLifecycleState::Failed);
        assert_eq!(instance.last_error.as_deref(), Some("prepare failed"));
        assert_eq!(instance.result_code.as_deref(), Some("INTERNAL"));
    }

    #[tokio::test]
    async fn start_shutdown_cancels_every_pending_instance() {
        let registry = VmRepository::<usize>::new_for_lifecycle_test(None);
        let mut workers = Vec::new();
        for id in ["one", "two"] {
            let starting_instance = starting_instance(&registry, id).await;
            let worker_registry = registry.clone();
            workers.push(tokio::spawn(async move {
                starting_instance.start_cancelled().await;
                worker_registry
                    .complete_async_start(
                        &starting_instance,
                        Some(("INVALID_STATE".into(), "cancelled".into())),
                    )
                    .await;
            }));
        }
        tokio::time::timeout(Duration::from_secs(2), registry.close_async_starts())
            .await
            .unwrap()
            .unwrap();
        for worker in workers {
            worker.await.unwrap();
        }
        assert!(registry
            .begin_async_start("three", "snapshot")
            .await
            .is_err());
    }
    #[tokio::test]
    async fn duplicate_start_is_rejected_and_unaccepted_failure_rolls_back() {
        let root = TestRoot::new();
        let registry = VmRepository::<usize>::new_for_lifecycle_test(None);
        registry
            .configure_start_journal(&root.0, Duration::from_secs(30))
            .unwrap();
        let first = registry.begin_async_start("vm", "snapshot").await.unwrap();
        assert!(matches!(
            registry.begin_async_start("vm", "snapshot").await,
            Err(VmRepositoryError::AlreadyExists(_))
        ));
        assert!(!first.start_was_accepted());
        registry
            .complete_async_start(
                &first,
                Some((
                    "IDENTITY_CONFLICT".into(),
                    "snapshot has an active delete fence".into(),
                )),
            )
            .await;
        assert!(!first.start_was_accepted());
        assert!(
            registry.get("vm").await.is_none(),
            "rejected starting_instance is not an accepted Failed sandbox"
        );
        assert_eq!(std::fs::read_dir(&root.0).unwrap().count(), 0);
    }

    #[tokio::test]
    async fn blocked_instance_journal_does_not_block_other_vm_operations() {
        let root = TestRoot::new();
        let registry = VmRepository::<usize>::new_for_lifecycle_test(None);
        registry
            .configure_start_journal(&root.0, Duration::from_secs(30))
            .unwrap();
        let starting_instance = starting_instance(&registry, "slow").await;
        let instance = registry.instance("slow").await.unwrap();
        let held_io = instance.journal_io.lock().await;
        let worker_registry = registry.clone();
        let completion = tokio::spawn(async move {
            worker_registry
                .complete_async_start(
                    &starting_instance,
                    Some(("INTERNAL".into(), "failed".into())),
                )
                .await;
        });
        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                if registry.get("slow").await.unwrap().lifecycle_state == VmmLifecycleState::Failed
                {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        let unrelated = tokio::time::timeout(
            Duration::from_secs(2),
            registry.start("other", |_| async { StartedRuntime::new(55, 1) }),
        )
        .await
        .unwrap()
        .unwrap();
        assert!(unrelated.success);
        assert!(!completion.is_finished());
        drop(held_io);
        completion.await.unwrap();
    }
    #[tokio::test]
    async fn start_precheck_is_revalidated_when_stop_cancels_preparation() {
        let registry = VmRepository::<usize>::new_for_lifecycle_test(None);
        let capacity = registry.inner.start_slots.available_permits();
        let preparing = starting_instance(&registry, "vm").await;
        registry
            .ensure_startable("vm", Some(&preparing))
            .await
            .unwrap();
        let stop_registry = registry.clone();
        let stopping =
            tokio::spawn(
                async move { stop_registry.stop("vm", "stop after start precheck").await },
            );
        // Observe stop's cancellation, rather than guessing when its task ran.
        tokio::time::timeout(Duration::from_secs(2), preparing.start_cancelled())
            .await
            .unwrap();
        assert!(!stopping.is_finished());
        let calls = Arc::new(AtomicUsize::new(0));
        let launched = Arc::clone(&calls);
        let result = registry
            .start_inner("vm", Some(&preparing), move |_| async move {
                launched.fetch_add(1, Ordering::Relaxed);
                StartedRuntime::new(123, 7)
            })
            .await;
        let error = result.unwrap_err();
        assert!(matches!(&error, VmRepositoryError::InvalidState(_)));
        let control_error: SnapshotControlError = RuntimeStartError::from(error).into();
        assert!(matches!(
            &control_error,
            SnapshotControlError::InvalidState(_)
        ));
        assert_eq!(control_error.code(), "INVALID_STATE");
        assert_eq!(calls.load(Ordering::Relaxed), 0);
        registry
            .complete_async_start(
                &preparing,
                Some(("INVALID_STATE".into(), "startup cancelled".into())),
            )
            .await;
        assert!(
            tokio::time::timeout(Duration::from_secs(2), stopping)
                .await
                .unwrap()
                .unwrap()
                .unwrap()
                .success
        );
        assert!(registry.get("vm").await.is_none());
        assert_eq!(registry.inner.start_slots.available_permits(), capacity);
    }
}

pub(in crate::vm) struct StartOperation {
    pub snapshot_id: String,
    pub cancel: Arc<AtomicBool>,
    pub done: watch::Sender<bool>,
    pub cancelled: Notify,
    accepted: AtomicBool,
    pub permit: StdMutex<Option<OwnedSemaphorePermit>>,
}

impl StartOperation {
    pub fn request_cancel(&self) {
        self.cancel.store(true, Ordering::Release);
        self.cancelled.notify_waiters();
    }

    pub fn new(snapshot_id: String, done: bool, permit: Option<OwnedSemaphorePermit>) -> Arc<Self> {
        Arc::new(Self {
            snapshot_id,
            cancel: Arc::new(AtomicBool::new(false)),
            done: watch::channel(done).0,
            cancelled: Notify::new(),
            accepted: AtomicBool::new(done),
            permit: StdMutex::new(permit),
        })
    }
}

impl<H> VmmInstance<H> {
    fn start_operation(&self) -> &StartOperation {
        self.start_operation
            .as_deref()
            .expect("asynchronous instance owns its start operation")
    }

    pub(crate) fn start_was_accepted(&self) -> bool {
        self.start_operation().accepted.load(Ordering::Acquire)
    }

    pub(crate) fn accept_start(&self) {
        self.start_operation()
            .accepted
            .store(true, Ordering::Release);
    }

    pub(crate) async fn start_cancelled(&self) {
        loop {
            let notified = self.start_operation().cancelled.notified();
            if self.start_operation().cancel.load(Ordering::Acquire) {
                return;
            }
            notified.await;
        }
    }

    pub(crate) fn start_cancellation(&self) -> Arc<AtomicBool> {
        Arc::clone(&self.start_operation().cancel)
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub(in crate::vm) enum JournalState {
    Starting,
    Running,
    Stopped,
    Failed,
}

// Persist lifecycle state only; runtime handles remain process-local. Serde
// ignores request metadata left in older journal records.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub(in crate::vm) struct StartRecord {
    pub sandbox_id: String,
    pub snapshot_id: String,
    pub state: JournalState,
    pub last_error: Option<String>,
    pub result_code: Option<String>,
}

#[derive(Clone)]
pub(in crate::vm) struct StartJournal(PathBuf);

impl StartJournal {
    pub fn open(directory: &Path) -> std::io::Result<(Self, Vec<StartRecord>)> {
        std::fs::create_dir_all(directory)?;
        let mut records = Vec::new();
        for entry in std::fs::read_dir(directory)? {
            let path = entry?.path();
            if path.extension().and_then(|value| value.to_str()) == Some("json") {
                records.push(serde_json::from_slice(&std::fs::read(path)?)?);
            }
        }
        Ok((Self(directory.to_owned()), records))
    }

    pub async fn save(
        &self,
        instance: &VmmInstanceSnapshot,
        snapshot_id: &str,
    ) -> std::io::Result<()> {
        let record = StartRecord {
            sandbox_id: instance.vm_id.clone(),
            snapshot_id: snapshot_id.into(),
            state: match instance.lifecycle_state {
                VmmLifecycleState::Creating => JournalState::Starting,
                VmmLifecycleState::Running => JournalState::Running,
                VmmLifecycleState::Stopping => JournalState::Stopped,
                VmmLifecycleState::Failed => JournalState::Failed,
            },
            last_error: instance.last_error.clone(),
            result_code: instance.result_code.clone(),
        };
        let name = format!("{:x}", Sha256::digest(instance.vm_id.as_bytes()));
        let destination = self.0.join(format!("{name}.json"));
        let temporary = self.0.join(format!("{name}.tmp"));
        let mut file = tokio::fs::File::create(&temporary).await?;
        file.write_all(&serde_json::to_vec(&record)?).await?;
        file.sync_all().await?;
        drop(file);
        tokio::fs::rename(temporary, destination).await?;
        tokio::fs::File::open(&self.0).await?.sync_all().await
    }

    pub async fn remove(&self, vm_id: &str) -> std::io::Result<()> {
        let name = format!("{:x}", Sha256::digest(vm_id.as_bytes()));
        match tokio::fs::remove_file(self.0.join(format!("{name}.json"))).await {
            Ok(()) => tokio::fs::File::open(&self.0).await?.sync_all().await,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct StartFromSnapshot {
    pub sandbox_id: String,
    pub snapshot_spec: Value,
    pub spec_checksum: String,
    pub resource_spec: Value,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub p2p_source_worker_ip: Option<IpAddr>,
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub local_snapshot: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StopSandbox {
    pub reason: String,
    pub deadline_unix_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SandboxStarted {
    pub identity: String,
    pub state: String,
    pub guest_endpoint: GuestEndpoint,
    pub vmm_pid: u32,
    pub rocklet_artifact: RockletArtifact,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SandboxStatus {
    pub sandbox_id: String,
    pub state: SandboxState,
    pub last_error: Option<String>,
    pub result_code: Option<String>,
    pub guest_endpoint: Option<GuestEndpoint>,
    pub vmm_pid: Option<u32>,
    pub rocklet_artifact: Option<RockletArtifact>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SandboxState {
    Starting,
    Running,
    Stopped,
    Failed,
}

impl StartFromSnapshot {
    pub fn p2p_source(
        &self,
        snapshot_id: &str,
    ) -> Option<crate::snapshot::io::range_io::P2PSource> {
        self.p2p_source_worker_ip
            .map(|worker_ip| crate::snapshot::io::range_io::P2PSource {
                snapshot_id: snapshot_id.to_owned(),
                spec_checksum: self.spec_checksum.clone(),
                worker_ip,
            })
    }

    pub fn validate(&self) -> Result<(), String> {
        require_nonempty("sandbox_id", &self.sandbox_id, 4096)?;
        require_object("snapshot_spec", &self.snapshot_spec)?;
        if self.local_snapshot
            && (self
                .snapshot_spec
                .as_object()
                .is_some_and(|spec| !spec.is_empty())
                || self.p2p_source_worker_ip.is_some())
        {
            return Err("local snapshot start must not supply a remote spec or P2P source".into());
        }
        validate_prefixed_sha256(&self.spec_checksum)?;
        require_object("resource_spec", &self.resource_spec)
    }
}

impl StopSandbox {
    pub fn validate(&self) -> Result<(), String> {
        require_nonempty("reason", &self.reason, 4096)
    }
}

fn remaining_deadline(deadline_unix_ms: u64) -> Result<Duration, SnapshotControlError> {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis();
    let deadline = u128::from(deadline_unix_ms);
    if deadline <= now {
        return Err(SnapshotControlError::Deadline(
            "request deadline expired".into(),
        ));
    }
    Ok(Duration::from_millis(
        u64::try_from(deadline - now).unwrap_or(u64::MAX).max(1),
    ))
}

pub(crate) fn validate_source(source_id: &str) -> Result<(), SnapshotControlError> {
    if source_id.is_empty() || source_id.len() > 4096 || source_id.contains('/') {
        Err(SnapshotControlError::Invalid(
            "sandbox identity is invalid".into(),
        ))
    } else {
        Ok(())
    }
}

pub struct VmService {
    catalog: Arc<SnapshotRepository>,
    repository: VmRepository,
    capture: Arc<SnapshotCaptureService>,
    runtime_launch_factory: RuntimeLaunchFactory,
    upload: Option<Arc<SnapshotUploadService>>,
    base_images: Option<Arc<BaseImageIngestionService>>,
    remote_loader: Option<Arc<dyn SnapshotReader>>,
    remote_preparer: Option<Arc<SnapshotRestorePreparer>>,
    metrics: Option<Arc<SnapdMetrics>>,
    p2p_serve: Arc<P2PSnapshotServeRegistry>,
    state: Arc<Mutex<SnapshotRuntimeState>>,
    snapshot_locks: Mutex<HashMap<String, Arc<Mutex<()>>>>,
}

impl VmService {
    /// Resolve a FULL OCI index through the existing Registry reader and keep
    /// its authenticated graph in the node catalog. Publication identities are
    /// retained; importing a snapshot never aliases it or builds a disk image.
    pub async fn import_oci_snapshot(
        &self,
        oci_ref: &str,
        deadline_unix_ms: u64,
    ) -> Result<SnapshotStatus, SnapshotControlError> {
        let loader = self.remote_loader.as_ref().ok_or_else(|| {
            SnapshotControlError::InvalidState("remote snapshot loading is not configured".into())
        })?;
        let snapshot = tokio::time::timeout(
            remaining_deadline(deadline_unix_ms)?,
            Arc::clone(loader).import_oci_snapshot(oci_ref),
        )
        .await
        .map_err(|_| SnapshotControlError::Deadline("OCI snapshot import timed out".into()))?
        .map_err(snapshot_load_error)?;
        if snapshot.snapshot_kind() != crate::snapshot::SnapshotKind::Full
            || snapshot.spec().is_none_or(|spec| {
                spec.registry_snapshot.is_none() || spec.memory.is_none() || spec.vm_state.is_none()
            })
        {
            return Err(SnapshotControlError::Invalid(
                "OCI resume requires a FULL memory and VMM-state snapshot".into(),
            ));
        }
        let snapshot_id = snapshot.snapshot_id().to_owned();
        validate_source(&snapshot_id)?;
        let lock = self.snapshot_lock(&snapshot_id).await;
        let _guard = lock.lock().await;
        {
            let mut state = self.state.lock().await;
            ensure_snapshot_mutation_not_fenced_locked(&mut state, &snapshot_id, Instant::now())?;
            self.register_opened(snapshot)?;
        }
        self.snapshot_status(&snapshot_id).await
    }

    pub async fn start_sandbox_from_snapshot(
        &self,
        snapshot_id: &str,
        request_id: &str,
        request: StartFromSnapshot,
        deadline_unix_ms: u64,
    ) -> Result<SandboxStarted, SnapshotControlError> {
        self.start_sandbox_from_snapshot_inner(
            snapshot_id,
            request_id,
            request,
            deadline_unix_ms,
            None,
        )
        .await
    }

    async fn start_sandbox_from_snapshot_inner(
        &self,
        snapshot_id: &str,
        request_id: &str,
        request: StartFromSnapshot,
        deadline_unix_ms: u64,
        starting_instance: Option<Arc<VmmInstance<Box<dyn VMMClientInterface>>>>,
    ) -> Result<SandboxStarted, SnapshotControlError> {
        let cancel_requested = starting_instance
            .as_ref()
            .map(|instance| instance.start_cancellation());
        let total_started = Instant::now();
        request.validate().map_err(SnapshotControlError::Invalid)?;
        validate_source(snapshot_id)?;
        validate_source(&request.sandbox_id)?;
        if request.local_snapshot {
            return self
                .start_local_checkpoint(snapshot_id, request, deadline_unix_ms, starting_instance)
                .await;
        }
        let loader = self.remote_loader.as_ref().ok_or_else(|| {
            SnapshotControlError::InvalidState("remote snapshot loading is not configured".into())
        })?;
        let preparer = self.remote_preparer.as_ref().ok_or_else(|| {
            SnapshotControlError::InvalidState(
                "remote runtime preparation is not configured".into(),
            )
        })?;
        let sandbox_lock = self.repository.source_lock(&request.sandbox_id).await;
        let _sandbox_guard = match &starting_instance {
            Some(starting_instance) => tokio::select! {
                guard = sandbox_lock.lock() => guard,
                _ = starting_instance.start_cancelled() => return Err(SnapshotControlError::InvalidState("asynchronous start cancelled".into())),
            },
            None => sandbox_lock.lock().await,
        };
        if cancel_requested
            .as_ref()
            .is_some_and(|flag| flag.load(std::sync::atomic::Ordering::Acquire))
        {
            return Err(SnapshotControlError::InvalidState(
                "asynchronous start cancelled".into(),
            ));
        }
        self.repository
            .ensure_startable(&request.sandbox_id, starting_instance.as_ref())
            .await
            .map_err(repository_error)?;
        let plan = RuntimeLaunchPlan {
            sandbox_id: request.sandbox_id.clone(),
            resource_spec: request.resource_spec.clone(),
        };
        let launch_spec = (self.runtime_launch_factory)(&plan)?;
        let runtime = launch_spec.runtime.clone();
        let remaining = remaining_deadline(deadline_unix_ms)?;
        self.reserve_runtime_reference(snapshot_id, &runtime)
            .await?;
        preparer
            .service()
            .begin_snapshot_hotset(snapshot_id, &request.spec_checksum, &runtime);
        let prepared = tokio::time::timeout(remaining, async {
            let metadata_started = Instant::now();
            let parsed: Arc<dyn crate::snapshot::SnapshotInstance> = Arc::clone(loader)
                .load_for_start_shared(&request.snapshot_spec, &request.spec_checksum, snapshot_id)
                .await
                .map_err(snapshot_load_error)?;
            self.register_opened(Arc::clone(&parsed))?;
            self.emit_runtime_stage(
                "start_sandbox",
                snapshot_id,
                &request.sandbox_id,
                request_id,
                "remote_metadata_load",
                metadata_started,
                total_started,
            );
            validate_launch_rocklet(
                &launch_spec,
                parsed
                    .spec()
                    .ok_or_else(|| {
                        SnapshotControlError::InvalidState(
                            "remote snapshot has no persistent spec".into(),
                        )
                    })?
                    .compatibility
                    .rocklet_artifact
                    .as_ref(),
            )?;
            let backing_started = Instant::now();
            let p2p_source = request.p2p_source(snapshot_id);
            let prepared = preparer
                .prepare(
                    parsed.as_ref(),
                    &runtime,
                    &self.repository,
                    p2p_source.as_ref(),
                )
                .await
                .map_err(snapshot_restore_error)?;
            self.emit_runtime_stage(
                "start_sandbox",
                snapshot_id,
                &request.sandbox_id,
                request_id,
                "remote_layer_prepare",
                backing_started,
                total_started,
            );
            let p2p_started = Instant::now();
            self.register_remote_runtime(
                snapshot_id,
                &request.spec_checksum,
                &runtime,
                preparer,
                parsed.as_ref(),
            )
            .await?;
            self.emit_runtime_stage(
                "start_sandbox",
                snapshot_id,
                &request.sandbox_id,
                request_id,
                "p2p_runtime_register",
                p2p_started,
                total_started,
            );
            self.emit_runtime_stage(
                "start_sandbox",
                snapshot_id,
                &request.sandbox_id,
                request_id,
                "remote_backing_prepare",
                backing_started,
                total_started,
            );
            preparer.launch_snapshot_hotset_prefetch(&runtime).await;
            Ok::<_, SnapshotControlError>(prepared)
        })
        .await;
        let prepared = match prepared {
            Ok(Ok(prepared)) => prepared,
            Ok(Err(error)) => {
                self.cleanup_remote_start(preparer, &runtime, snapshot_id)
                    .await;
                return Err(error);
            }
            Err(_) => {
                self.cleanup_remote_start(preparer, &runtime, snapshot_id)
                    .await;
                return Err(SnapshotControlError::Deadline(
                    "remote snapshot metadata load timed out".into(),
                ));
            }
        };
        if let Err(error) = remaining_deadline(deadline_unix_ms).and_then(|_| {
            if cancel_requested
                .as_ref()
                .is_some_and(|flag| flag.load(std::sync::atomic::Ordering::Acquire))
            {
                Err(SnapshotControlError::InvalidState(
                    "asynchronous start cancelled".into(),
                ))
            } else {
                Ok(Duration::ZERO)
            }
        }) {
            self.cleanup_remote_start(preparer, &runtime, snapshot_id)
                .await;
            return Err(error);
        }
        // Once start_runtime is called the registry owns a durable background
        // operation. Do not cancel that future at the HTTP deadline and then
        // revoke its data grants while the VMM may still become RUNNING.
        let manager_started = Instant::now();
        let result = self
            .repository
            .start_runtime(
                RuntimeStartRequest {
                    vm_id: request.sandbox_id.clone(),
                    launch_spec,
                    runtime_source: SnapshotRuntimeSource::Prepared(prepared),
                    cancel_requested,
                },
                starting_instance.as_ref(),
            )
            .await;
        self.emit_runtime_stage(
            "start_sandbox",
            snapshot_id,
            &request.sandbox_id,
            request_id,
            "runtime_manager_call",
            manager_started,
            total_started,
        );
        match result {
            Ok(result) if result.success => {
                let observation_started = Instant::now();
                let observation = self
                    .repository
                    .guest_runtime_observation(&request.sandbox_id)
                    .await
                    .ok_or_else(|| {
                        SnapshotControlError::InvalidState(
                            "started sandbox has no guest Rocklet endpoint".into(),
                        )
                    })?;
                self.emit_runtime_stage(
                    "start_sandbox",
                    snapshot_id,
                    &request.sandbox_id,
                    request_id,
                    "runtime_observation",
                    observation_started,
                    total_started,
                );
                preparer.service().finish_snapshot_hotset(&runtime, true);
                preparer
                    .service()
                    .launch_background_prefetch(&runtime)
                    .await;
                Ok(sandbox_started_result(
                    request.sandbox_id.clone(),
                    observation,
                ))
            }
            Ok(result) => {
                self.cleanup_remote_start(preparer, &runtime, snapshot_id)
                    .await;
                Err(SnapshotControlError::Runtime(
                    result.message.unwrap_or(result.code),
                ))
            }
            Err(error) => {
                self.cleanup_remote_start(preparer, &runtime, snapshot_id)
                    .await;
                Err(error.into())
            }
        }
    }

    async fn start_local_checkpoint(
        &self,
        snapshot_id: &str,
        request: StartFromSnapshot,
        deadline_unix_ms: u64,
        starting_instance: Option<Arc<VmmInstance<Box<dyn VMMClientInterface>>>>,
    ) -> Result<SandboxStarted, SnapshotControlError> {
        let cancel_requested = starting_instance
            .as_ref()
            .map(|instance| instance.start_cancellation());
        let snapshot_lock = self.snapshot_lock(snapshot_id).await;
        let _snapshot_guard = match &starting_instance {
            Some(starting_instance) => tokio::select! {
                guard = snapshot_lock.lock() => guard,
                _ = starting_instance.start_cancelled() => return Err(SnapshotControlError::InvalidState("asynchronous start cancelled".into())),
            },
            None => snapshot_lock.lock().await,
        };
        let source_lock = self.repository.source_lock(&request.sandbox_id).await;
        let _source_guard = match &starting_instance {
            Some(starting_instance) => tokio::select! {
                guard = source_lock.lock() => guard,
                _ = starting_instance.start_cancelled() => return Err(SnapshotControlError::InvalidState("asynchronous start cancelled".into())),
            },
            None => source_lock.lock().await,
        };
        remaining_deadline(deadline_unix_ms)?;
        if cancel_requested
            .as_ref()
            .is_some_and(|flag| flag.load(std::sync::atomic::Ordering::Acquire))
        {
            return Err(SnapshotControlError::InvalidState(
                "asynchronous start cancelled".into(),
            ));
        }
        self.repository
            .ensure_startable(&request.sandbox_id, starting_instance.as_ref())
            .await
            .map_err(repository_error)?;
        let local = self.local_snapshot(snapshot_id).await?;
        let plan = RuntimeLaunchPlan {
            sandbox_id: request.sandbox_id.clone(),
            resource_spec: request.resource_spec.clone(),
        };
        let launch_spec = (self.runtime_launch_factory)(&plan)?;
        validate_launch_rocklet(&launch_spec, local.rocklet_artifact.as_ref())?;
        let runtime = launch_spec.runtime.clone();
        let spec_sha256 = self.local_start_spec_checksum(&local).await?;
        self.reserve_runtime_reference(snapshot_id, &runtime)
            .await?;
        let result =
            self.repository
                .start_runtime(
                    RuntimeStartRequest {
                        vm_id: request.sandbox_id.clone(),
                        launch_spec,
                        runtime_source: SnapshotRuntimeSource::Local(
                            LocalSnapshotRuntimeSource::new(local, spec_sha256),
                        ),
                        cancel_requested,
                    },
                    starting_instance.as_ref(),
                )
                .await;
        match result {
            Ok(result) if result.success => {
                let observation = self
                    .repository
                    .guest_runtime_observation(&request.sandbox_id)
                    .await
                    .ok_or_else(|| {
                        SnapshotControlError::InvalidState(
                            "local start has no guest endpoint".into(),
                        )
                    })?;
                Ok(sandbox_started_result(request.sandbox_id, observation))
            }
            other => {
                self.remove_runtime_reference(snapshot_id, &runtime).await;
                match other {
                    Err(error) => Err(error.into()),
                    Ok(result) => Err(SnapshotControlError::Runtime(
                        result.message.unwrap_or(result.code),
                    )),
                }
            }
        }
    }

    pub async fn background_prefetch_status(
        &self,
        sandbox_id: &str,
    ) -> Result<serde_json::Value, SnapshotControlError> {
        self.sandbox_status(sandbox_id).await?;
        let runtime = crate::RuntimeIdentity::new(sandbox_id);
        let progress = self
            .remote_preparer
            .as_ref()
            .and_then(|p| p.service().background_prefetch_status(&runtime));
        serde_json::to_value(progress)
            .map_err(|error| SnapshotControlError::InvalidState(error.to_string()))
    }

    pub async fn sandbox_status(
        &self,
        sandbox_id: &str,
    ) -> Result<SandboxStatus, SnapshotControlError> {
        validate_source(sandbox_id)?;
        let instance = self.repository.get(sandbox_id).await;
        let state = match instance.as_ref().map(|value| value.lifecycle_state) {
            Some(VmmLifecycleState::Creating) => SandboxState::Starting,
            Some(VmmLifecycleState::Running) => SandboxState::Running,
            Some(VmmLifecycleState::Failed) => SandboxState::Failed,
            Some(VmmLifecycleState::Stopping) | None => SandboxState::Stopped,
        };
        let observation = if state == SandboxState::Running {
            Some(
                self.repository
                    .guest_runtime_observation(sandbox_id)
                    .await
                    .ok_or_else(|| {
                        SnapshotControlError::InvalidState(
                            "running sandbox has no guest Rocklet endpoint".into(),
                        )
                    })?,
            )
        } else {
            None
        };
        let (guest_endpoint, vmm_pid, rocklet_artifact) = observation
            .map(runtime_result_parts)
            .map_or((None, None, None), |(endpoint, pid, artifact)| {
                (Some(endpoint), Some(pid), Some(artifact))
            });
        Ok(SandboxStatus {
            sandbox_id: sandbox_id.into(),
            state,
            last_error: instance.as_ref().and_then(|value| value.last_error.clone()),
            result_code: instance.and_then(|value| value.result_code),
            guest_endpoint,
            vmm_pid,
            rocklet_artifact,
        })
    }

    pub async fn plugger_control(
        &self,
        sandbox_id: &str,
        action: &str,
        payload: &serde_json::Value,
        deadline_unix_ms: u64,
    ) -> Result<crate::vm::runtime::plugger::PluggerReply, SnapshotControlError> {
        validate_source(sandbox_id)?;
        let timeout = remaining_deadline(deadline_unix_ms)?;
        let instance = self
            .repository
            .instance(sandbox_id)
            .await
            .ok_or_else(|| SnapshotControlError::Invalid("sandbox runtime is absent".into()))?;
        let handles = instance.runtime_handles().await.ok_or_else(|| {
            SnapshotControlError::Invalid("sandbox runtime handles are absent".into())
        })?;
        handles
            .plugger_control(action, payload, timeout)
            .await
            .map_err(|error| RuntimeStartError::from(error).into())
    }

    pub async fn stop_sandbox(
        &self,
        sandbox_id: &str,
        request: &StopSandbox,
    ) -> Result<OperationAccepted, SnapshotControlError> {
        request.validate().map_err(SnapshotControlError::Invalid)?;
        self.stop_sandbox_operation(sandbox_id, &request.reason)
            .await
    }

    pub async fn delete_sandbox(
        &self,
        sandbox_id: &str,
    ) -> Result<OperationAccepted, SnapshotControlError> {
        self.stop_sandbox_operation(sandbox_id, "sandbox deletion requested")
            .await
    }

    async fn stop_sandbox_operation(
        &self,
        sandbox_id: &str,
        reason: &str,
    ) -> Result<OperationAccepted, SnapshotControlError> {
        validate_source(sandbox_id)?;
        let result = self
            .repository
            .stop(sandbox_id, reason)
            .await
            .map_err(repository_error)?;
        if !result.success {
            return Err(SnapshotControlError::Runtime(
                result
                    .message
                    .unwrap_or_else(|| result.code.as_str().into()),
            ));
        }
        // A new generation may be started after stop completes. Its worker
        // uses this lock before reserving references, so a late cleanup only
        // scans references while no replacement runtime owns this identity.
        let source_lock = self.repository.source_lock(sandbox_id).await;
        let _source_guard = source_lock.lock().await;
        if self.repository.get(sandbox_id).await.is_none() {
            self.release_runtime_references(&crate::RuntimeIdentity::new(sandbox_id))
                .await;
        }
        Ok(OperationAccepted {
            identity: sandbox_id.into(),
            state: "STOPPED".into(),
        })
    }

    async fn cleanup_remote_start(
        &self,
        preparer: &SnapshotRestorePreparer,
        runtime: &crate::RuntimeIdentity,
        snapshot_id: &str,
    ) {
        preparer.service().finish_snapshot_hotset(runtime, false);
        self.unregister_runtime(runtime).await;
        preparer.service().revoke_runtime(runtime).await;
        self.remove_runtime_reference(snapshot_id, runtime).await;
    }
}

fn sandbox_started_result(
    sandbox_id: String,
    observation: GuestRuntimeObservation,
) -> SandboxStarted {
    let (guest_endpoint, vmm_pid, rocklet_artifact) = runtime_result_parts(observation);
    SandboxStarted {
        identity: sandbox_id,
        state: "RUNNING".into(),
        guest_endpoint,
        vmm_pid,
        rocklet_artifact,
    }
}

fn repository_error(error: VmRepositoryError) -> SnapshotControlError {
    match error {
        VmRepositoryError::Invalid(message) => SnapshotControlError::Invalid(message),
        VmRepositoryError::IdentityConflict(message)
        | VmRepositoryError::AlreadyExists(message) => {
            SnapshotControlError::IdentityConflict(message)
        }
        VmRepositoryError::NotFound(message) => SnapshotControlError::NotFound(message),
        VmRepositoryError::InvalidState(message)
            if message == "asynchronous start queue is full" =>
        {
            SnapshotControlError::RetryableInvalidState(message)
        }
        VmRepositoryError::InvalidState(message) => SnapshotControlError::InvalidState(message),
        error => SnapshotControlError::Runtime(error.to_string()),
    }
}

impl VmService {
    pub(crate) async fn close_async_starts(&self) -> Result<(), SnapshotControlError> {
        self.repository
            .close_async_starts()
            .await
            .map_err(repository_error)
    }

    pub fn with_async_start_journal(
        self,
        directory: impl AsRef<Path>,
        timeout: Duration,
    ) -> Result<Self, SnapshotControlError> {
        self.repository
            .configure_start_journal(directory, timeout)
            .map_err(repository_error)?;
        Ok(self)
    }

    pub async fn start_sandbox_from_snapshot_async(
        self: Arc<Self>,
        snapshot_id: &str,
        request_id: &str,
        request: StartFromSnapshot,
        deadline_unix_ms: u64,
    ) -> Result<OperationAccepted, SnapshotControlError> {
        request.validate().map_err(SnapshotControlError::Invalid)?;
        validate_source(snapshot_id)?;
        validate_source(&request.sandbox_id)?;
        remaining_deadline(deadline_unix_ms)?;
        if self.remote_loader.is_none() || self.remote_preparer.is_none() {
            return Err(SnapshotControlError::InvalidState(
                "remote snapshot loading is not configured".into(),
            ));
        }
        let snapshot_id = snapshot_id.to_owned();
        let request_id = request_id.to_owned();
        // The starting instance must always have an owner. Disconnecting the
        // HTTP future cannot abandon it between journal/pin and worker spawn.
        self.spawn_snapshot_start(snapshot_id, request_id, request, deadline_unix_ms)
            .await
            .map_err(|error| {
                SnapshotControlError::Runtime(format!("asynchronous start setup failed: {error}"))
            })?
    }

    fn spawn_snapshot_start(
        self: Arc<Self>,
        snapshot_id: String,
        request_id: String,
        request: StartFromSnapshot,
        deadline_unix_ms: u64,
    ) -> tokio::task::JoinHandle<Result<OperationAccepted, SnapshotControlError>> {
        tokio::spawn(async move {
            self.begin_snapshot_start(snapshot_id, request_id, request, deadline_unix_ms)
                .await
        })
    }

    async fn begin_snapshot_start(
        self: Arc<Self>,
        snapshot_id: String,
        request_id: String,
        request: StartFromSnapshot,
        deadline_unix_ms: u64,
    ) -> Result<OperationAccepted, SnapshotControlError> {
        remaining_deadline(deadline_unix_ms)?;
        let launch = (self.runtime_launch_factory)(&RuntimeLaunchPlan {
            sandbox_id: request.sandbox_id.clone(),
            resource_spec: request.resource_spec.clone(),
        })?;
        let starting_instance = self
            .repository
            .begin_async_start(&request.sandbox_id, &snapshot_id)
            .await
            .map_err(repository_error)?;
        let source_lock = self.repository.source_lock(&request.sandbox_id).await;
        let source_guard = async {
            let remaining = remaining_deadline(deadline_unix_ms)?;
            tokio::time::timeout(remaining, async {
                tokio::select! {
                    guard = source_lock.lock() => Ok(guard),
                    _ = starting_instance.start_cancelled() => Err(SnapshotControlError::InvalidState("asynchronous start cancelled".into())),
                }
            }).await.map_err(|_| SnapshotControlError::Deadline("asynchronous start setup deadline expired".into()))?
        }.await;
        let _source_guard = match source_guard {
            Ok(guard) => guard,
            Err(error) => {
                self.repository
                    .complete_async_start(
                        &starting_instance,
                        Some((error.code().into(), error.to_string())),
                    )
                    .await;
                return Err(error);
            }
        };
        // Protect the snapshot before acknowledging, including before the
        // background worker acquires the source's operation lock.
        let reserved = async {
            tokio::time::timeout(
                remaining_deadline(deadline_unix_ms)?,
                self.reserve_runtime_reference(&snapshot_id, &launch.runtime),
            )
            .await
            .map_err(|_| {
                SnapshotControlError::Deadline("asynchronous start setup deadline expired".into())
            })?
        }
        .await;
        if let Err(error) = reserved {
            self.repository
                .complete_async_start(
                    &starting_instance,
                    Some((error.code().into(), error.to_string())),
                )
                .await;
            return Err(error);
        }
        let identity = request.sandbox_id.clone();
        starting_instance.accept_start();
        tokio::spawn(async move {
            let deadline = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_millis() as u64
                + self.repository.start_timeout().as_millis() as u64;
            let worker = self.clone();
            let worker_snapshot = snapshot_id.clone();
            let worker_instance = starting_instance.clone();
            let result = tokio::spawn(async move {
                worker
                    .start_sandbox_from_snapshot_inner(
                        &worker_snapshot,
                        &request_id,
                        request,
                        deadline,
                        Some(worker_instance),
                    )
                    .await
            })
            .await
            .unwrap_or_else(|error| {
                Err(SnapshotControlError::Runtime(format!(
                    "asynchronous start task failed: {error}"
                )))
            });
            if result.is_err() {
                let runtime = crate::RuntimeIdentity::new(starting_instance.vm_id());
                self.remove_runtime_reference(&snapshot_id, &runtime).await;
            }
            // Only release starting_instance ownership after every old-generation
            // business reference/cleanup has completed.
            self.repository
                .complete_async_start(
                    &starting_instance,
                    result
                        .err()
                        .map(|error| (error.code().into(), error.to_string())),
                )
                .await;
        });
        Ok(OperationAccepted {
            identity,
            state: "STARTING".into(),
        })
    }
}

#[cfg(test)]
mod async_start_tests {
    use super::*;
    use crate::vm::runtime::launcher::VmmLaunchSpec;
    use std::collections::BTreeMap;
    use std::path::PathBuf;

    fn service() -> Arc<VmService> {
        let runtime = VmRepository::new(
            None,
            Vec::new(),
            Duration::from_secs(30),
            Duration::from_secs(1),
            1,
            Duration::from_secs(1),
        )
        .unwrap();
        let capture = Arc::new(SnapshotCaptureService::new(Duration::from_secs(1)).unwrap());
        let factory: RuntimeLaunchFactory = Arc::new(|plan| {
            Ok(VmmLaunchSpec {
                command: vec!["unreachable-test-vmm".into()],
                runtime: crate::RuntimeIdentity::new(&plan.sandbox_id),
                launch_identity: "test-launch".into(),
                fault_around_pages: 1,
                cache_root: PathBuf::from("/tmp/test-cache"),
                private_root: PathBuf::from("/tmp/test-private"),
                environment: BTreeMap::new(),
                cwd: None,
                guest_network: false,
                guest_readiness: crate::vm::runtime::plugger::GuestReadinessMode::Rocklet,
                guest_rocklet: None,
            })
        });
        Arc::new(VmService::new(runtime, capture, factory))
    }

    fn deadline_after(duration: Duration) -> u64 {
        (SystemTime::now().duration_since(UNIX_EPOCH).unwrap() + duration).as_millis() as u64
    }

    fn request(id: &str) -> StartFromSnapshot {
        StartFromSnapshot {
            sandbox_id: id.into(),
            snapshot_spec: serde_json::json!({}),
            spec_checksum: format!("sha256:{}", "0".repeat(64)),
            resource_spec: serde_json::json!({}),
            p2p_source_worker_ip: None,
            local_snapshot: true,
        }
    }

    #[tokio::test]
    async fn dropped_start_waiter_does_not_strand_preparation_behind_stop_lock() {
        let service = service();
        let source_lock = service.repository.source_lock("vm").await;
        let held = source_lock.lock().await;
        let request = StartFromSnapshot {
            sandbox_id: "vm".into(),
            snapshot_spec: serde_json::json!({}),
            spec_checksum: format!("sha256:{}", "0".repeat(64)),
            resource_spec: serde_json::json!({}),
            p2p_source_worker_ip: None,
            local_snapshot: true,
        };
        // Exercise the same owned starting_instance boundary used by the public HTTP
        // operation; its waiter may disappear while starting_instance is awaiting a
        // source lock, before any worker or snapshot reference exists.
        let owner = service.clone().spawn_snapshot_start(
            "snapshot".into(),
            "request".into(),
            request,
            deadline_after(Duration::from_secs(30)),
        );
        tokio::time::timeout(Duration::from_secs(2), async {
            while service.repository.get("vm").await.is_none() {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        let waiter = tokio::spawn(async move { owner.await });
        waiter.abort();
        let _ = waiter.await;
        let stop_registry = service.repository.clone();
        let stopping =
            tokio::spawn(async move { stop_registry.stop("vm", "cancel pending start").await });
        tokio::time::timeout(Duration::from_secs(2), async {
            while service.repository.get("vm").await.is_some() {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        assert!(!stopping.is_finished());
        assert!(matches!(
            service.repository.stop("vm", "cancel pending start").await,
            Err(VmRepositoryError::InvalidState(_))
        ));
        assert!(matches!(
            service.repository.begin_async_start("vm", "snapshot").await,
            Err(VmRepositoryError::InvalidState(_))
        ));
        drop(held);
        let stopped = tokio::time::timeout(Duration::from_secs(2), stopping)
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        assert!(stopped.success);
        assert!(service.repository.get("vm").await.is_none());
        assert!(service
            .active_runtimes("snapshot")
            .await
            .unwrap()
            .is_empty());
        tokio::time::timeout(Duration::from_secs(2), service.close_async_starts())
            .await
            .unwrap()
            .unwrap();
    }
    #[tokio::test]
    async fn acknowledged_fast_start_failure_remains_queryable() {
        let service = service();
        let accepted = service
            .clone()
            .spawn_snapshot_start(
                "missing-snapshot".into(),
                "request".into(),
                request("vm"),
                deadline_after(Duration::from_secs(30)),
            )
            .await
            .unwrap()
            .unwrap();
        assert_eq!(accepted.state, "STARTING");
        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                if service
                    .repository
                    .get("vm")
                    .await
                    .is_some_and(|instance| instance.lifecycle_state == VmmLifecycleState::Failed)
                {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        let status = service.sandbox_status("vm").await.unwrap();
        assert_eq!(status.state, SandboxState::Failed);
        assert_eq!(status.result_code.as_deref(), Some("NOT_FOUND"));
        assert!(service
            .active_runtimes("missing-snapshot")
            .await
            .unwrap()
            .is_empty());
    }

    #[tokio::test]
    async fn start_setup_deadline_while_waiting_for_source_lock_rolls_back() {
        let service = service();
        let source_lock = service.repository.source_lock("vm").await;
        let held = source_lock.lock().await;
        let owner = service.clone().spawn_snapshot_start(
            "snapshot".into(),
            "request".into(),
            request("vm"),
            deadline_after(Duration::from_millis(50)),
        );
        let result = tokio::time::timeout(Duration::from_secs(2), owner)
            .await
            .unwrap()
            .unwrap();
        assert!(matches!(result, Err(SnapshotControlError::Deadline(_))));
        assert!(service.repository.get("vm").await.is_none());
        assert!(service
            .active_runtimes("snapshot")
            .await
            .unwrap()
            .is_empty());
        drop(held);
    }

    #[tokio::test]
    async fn duplicate_start_setup_is_rejected_without_cancelling_the_owner() {
        let service = service();
        let source_lock = service.repository.source_lock("vm").await;
        let held = source_lock.lock().await;
        let owner = service.clone().spawn_snapshot_start(
            "snapshot".into(),
            "owner".into(),
            request("vm"),
            deadline_after(Duration::from_secs(30)),
        );
        tokio::time::timeout(Duration::from_secs(2), async {
            while service.repository.get("vm").await.is_none() {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        let duplicate = service.clone().spawn_snapshot_start(
            "snapshot".into(),
            "duplicate".into(),
            request("vm"),
            deadline_after(Duration::from_millis(50)),
        );
        assert!(matches!(
            tokio::time::timeout(Duration::from_secs(2), duplicate)
                .await
                .unwrap()
                .unwrap(),
            Err(SnapshotControlError::IdentityConflict(_))
        ));
        assert_eq!(
            service.repository.get("vm").await.unwrap().lifecycle_state,
            VmmLifecycleState::Creating
        );
        assert!(!owner.is_finished());
        let stop_service = service.clone();
        let stopping = tokio::spawn(async move { stop_service.delete_sandbox("vm").await });
        assert!(tokio::time::timeout(Duration::from_secs(2), owner)
            .await
            .unwrap()
            .unwrap()
            .is_err());
        assert!(service.repository.get("vm").await.is_none());
        assert!(!stopping.is_finished());
        assert!(matches!(
            service.delete_sandbox("vm").await,
            Err(SnapshotControlError::InvalidState(_))
        ));
        drop(held);
        let stopped = tokio::time::timeout(Duration::from_secs(2), stopping)
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        assert_eq!(stopped.state, "STOPPED");
        assert!(matches!(
            service.delete_sandbox("vm").await,
            Err(SnapshotControlError::NotFound(_))
        ));
    }
    #[tokio::test]
    async fn start_setup_deadline_while_waiting_for_snapshot_reference_lock_rolls_back() {
        let service = service();
        let reference_owner = Arc::clone(&service);
        let (entered_tx, entered_rx) = tokio::sync::oneshot::channel();
        let (release_tx, release_rx) = tokio::sync::oneshot::channel();
        let holder = tokio::spawn(async move {
            reference_owner
                .hold_reference_updates_for_test(entered_tx, release_rx)
                .await;
        });
        entered_rx.await.unwrap();
        let owner = service.clone().spawn_snapshot_start(
            "snapshot".into(),
            "request".into(),
            request("vm"),
            deadline_after(Duration::from_millis(250)),
        );
        tokio::time::timeout(Duration::from_secs(2), async {
            while service.repository.get("vm").await.is_none() {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        let result = tokio::time::timeout(Duration::from_secs(2), owner)
            .await
            .unwrap()
            .unwrap();
        assert!(matches!(result, Err(SnapshotControlError::Deadline(_))));
        assert!(service.repository.get("vm").await.is_none());
        // Rejection must finish its owner without requiring the still-held
        // snapshot mutex. Otherwise shutdown would hang on a lost starting_instance.
        tokio::time::timeout(Duration::from_secs(2), service.close_async_starts())
            .await
            .unwrap()
            .unwrap();
        release_tx.send(()).unwrap();
        holder.await.unwrap();
        assert!(service
            .active_runtimes("snapshot")
            .await
            .unwrap()
            .is_empty());
    }
    #[tokio::test]
    async fn synchronous_start_rejects_preparing_instance_before_snapshot_lookup() {
        let service = service();
        let preparing = service
            .repository
            .begin_async_start("vm", "original-snapshot")
            .await
            .unwrap();
        preparing.accept_start();
        // The requested local snapshot is absent. Instance occupancy must win
        // before snapshot lookup or any attempt to launch another runtime.
        let result = service
            .start_sandbox_from_snapshot(
                "missing-snapshot",
                "sync-request",
                request("vm"),
                deadline_after(Duration::from_secs(30)),
            )
            .await;
        assert!(matches!(
            result,
            Err(SnapshotControlError::IdentityConflict(_))
        ));
        assert!(service.repository.is_current_instance(&preparing).await);
        assert_eq!(
            service.repository.get("vm").await.unwrap().lifecycle_state,
            VmmLifecycleState::Creating
        );
        assert!(service.capture.status("missing-snapshot").await.is_none());
        service
            .repository
            .complete_async_start(
                &preparing,
                Some(("INTERNAL".into(), "test preparation finished".into())),
            )
            .await;
        assert!(
            service
                .repository
                .stop("vm", "test cleanup")
                .await
                .unwrap()
                .success
        );
    }

    #[tokio::test]
    async fn snapshot_and_fork_reject_non_running_instances_before_capture() {
        for failed in [false, true] {
            let service = service();
            let preparing = service
                .repository
                .begin_async_start("source", "original-snapshot")
                .await
                .unwrap();
            preparing.accept_start();
            if failed {
                service
                    .repository
                    .complete_async_start(
                        &preparing,
                        Some(("INTERNAL".into(), "preparation failed".into())),
                    )
                    .await;
            }
            let snapshot = service
                .snapshot_sandbox(
                    "source",
                    CaptureSnapshot {
                        snapshot_id: "capture-not-running".into(),
                    },
                    deadline_after(Duration::from_secs(30)),
                )
                .await;
            assert!(matches!(
                snapshot,
                Err(SnapshotControlError::InvalidState(_))
            ));
            let fork = service
                .fork_sandbox(
                    "source",
                    ForkSnapshot {
                        snapshot: CaptureSnapshot {
                            snapshot_id: "fork-not-running".into(),
                        },
                        children: vec![RuntimeLaunchPlan {
                            sandbox_id: "fork-target".into(),
                            resource_spec: serde_json::json!({}),
                        }],
                    },
                    deadline_after(Duration::from_secs(30)),
                )
                .await;
            assert!(matches!(fork, Err(SnapshotControlError::InvalidState(_))));
            assert!(service
                .capture
                .status("capture-not-running")
                .await
                .is_none());
            assert!(service.capture.status("fork-not-running").await.is_none());
            assert!(service.repository.get("fork-target").await.is_none());
            assert_eq!(
                service
                    .repository
                    .get("source")
                    .await
                    .unwrap()
                    .lifecycle_state,
                if failed {
                    VmmLifecycleState::Failed
                } else {
                    VmmLifecycleState::Creating
                }
            );
            if !failed {
                service
                    .repository
                    .complete_async_start(
                        &preparing,
                        Some(("INTERNAL".into(), "test preparation finished".into())),
                    )
                    .await;
            }
            assert!(
                service
                    .repository
                    .stop("source", "test cleanup")
                    .await
                    .unwrap()
                    .success
            );
        }
    }
}

const MAX_FORK_CHILDREN: usize = 32;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CaptureSnapshot {
    pub snapshot_id: String,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ForkSnapshot {
    #[serde(flatten)]
    pub snapshot: CaptureSnapshot,
    pub children: Vec<RuntimeLaunchPlan>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FinalizeSnapshot {
    pub spec_checksum: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SnapshotFence {
    pub fence_nonce: String,
    pub ttl_ms: u64,
    pub expected_runtimes: Vec<RuntimeIdentity>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AbortDeleteFence {
    pub fence_nonce: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InvalidateSnapshot {
    pub fence_nonce: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SnapshotCaptured {
    pub snapshot_id: String,
    pub state: CaptureState,
    pub layer_identities: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ForkChild {
    pub sandbox_id: String,
    pub state: ForkChildState,
    pub error: Option<String>,
    pub guest_endpoint: Option<GuestEndpoint>,
    pub vmm_pid: Option<u32>,
    pub rocklet_artifact: Option<RockletArtifact>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ForkCaptured {
    pub snapshot: SnapshotCaptured,
    pub children: Vec<ForkChild>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SnapshotUsage {
    pub snapshot_id: String,
    pub state: SnapshotUsageState,
    pub active_runtimes: Vec<RuntimeIdentity>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SnapshotInvalidated {
    pub identity: String,
    pub state: String,
    pub invalidated_aliases: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CaptureState {
    LocalReady,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ForkChildState {
    Running,
    Pending,
    Failed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SnapshotUsageState {
    ActiveRuntimes,
    FencedNoRuntime,
}

impl CaptureSnapshot {
    pub fn validate(&self) -> Result<(), String> {
        require_nonempty("snapshot_id", &self.snapshot_id, 4096)?;
        Ok(())
    }
}

impl ForkSnapshot {
    pub fn validate(&self) -> Result<(), String> {
        self.snapshot.validate()?;
        if self.children.is_empty() || self.children.len() > MAX_FORK_CHILDREN {
            return Err(format!(
                "children must contain 1..={MAX_FORK_CHILDREN} entries"
            ));
        }
        let mut identities = HashSet::with_capacity(self.children.len());
        for child in &self.children {
            child.validate()?;
            if !identities.insert(&child.sandbox_id) {
                return Err("fork child sandbox IDs must be unique".into());
            }
        }
        Ok(())
    }
}

impl FinalizeSnapshot {
    pub fn validate(&self) -> Result<(), String> {
        validate_prefixed_sha256(&self.spec_checksum)
    }
}

impl SnapshotFence {
    pub fn validate(&self) -> Result<(), String> {
        require_nonempty("fence_nonce", &self.fence_nonce, 4096)?;
        if self.ttl_ms == 0 || self.ttl_ms > 300_000 {
            return Err("ttl_ms must be within 1..=300000".into());
        }
        let expected = self.expected_runtimes.iter().collect::<HashSet<_>>();
        if expected.len() != self.expected_runtimes.len()
            || self
                .expected_runtimes
                .iter()
                .any(|runtime| runtime.sandbox_id.is_empty())
        {
            return Err("expected_runtimes must be unique valid runtime identities".into());
        }
        Ok(())
    }
}

impl AbortDeleteFence {
    pub fn validate(&self) -> Result<(), String> {
        require_nonempty("fence_nonce", &self.fence_nonce, 4096)
    }
}

impl InvalidateSnapshot {
    pub fn validate(&self) -> Result<(), String> {
        require_nonempty("fence_nonce", &self.fence_nonce, 4096)
    }
}

pub use crate::vm::runtime::client::VmControlApi as CaptureControlClient;

#[derive(Clone)]
pub struct RuntimeCaptureContext {
    pub runtime: RuntimeIdentity,
    pub control_client: Arc<dyn CaptureControlClient>,
    pub geometry: Vec<LayerGeometry>,
    pub(crate) lineage: SnapshotLineage,
    pub rocklet_artifact: RockletArtifactCompatibility,
}

impl RuntimeCaptureContext {
    /// Builds a capture context for a directly launched runtime whose ancestry
    /// consists only of already-published remote chains.
    pub fn from_remote_chains(
        source_snapshot_id: String,
        runtime: RuntimeIdentity,
        control_client: Arc<dyn CaptureControlClient>,
        geometry: Vec<LayerGeometry>,
        parent_chains: HashMap<SnapshotLayerKind, LayerChainIndex>,
        rocklet_artifact: RockletArtifactCompatibility,
    ) -> Self {
        Self {
            runtime,
            control_client,
            geometry,
            lineage: SnapshotLineage::from_remote(&source_snapshot_id, &parent_chains, &[]),
            rocklet_artifact,
        }
    }
}

struct SnapshotJob {
    source_sandbox_id: String,
    source_stopped: bool,
    snapshot: std::sync::Weak<dyn crate::snapshot::SnapshotInstance>,
    last_error: Option<SnapshotControlError>,
}

struct DeleteFence {
    nonce: String,
    expires_at: Instant,
}

#[derive(Default)]
struct SnapshotRuntimeState {
    jobs: HashMap<String, SnapshotJob>,
    runtime_references: HashMap<String, HashSet<crate::RuntimeIdentity>>,
    delete_fences: HashMap<String, DeleteFence>,
    invalidating_snapshots: HashSet<String>,
    invalidated_snapshots: BTreeSet<String>,
}

const MAX_INVALIDATED_SNAPSHOTS: usize = 4096;

#[derive(Clone)]
pub(crate) struct SnapshotRuntimeRecoveryHandle {
    state: Arc<Mutex<SnapshotRuntimeState>>,
    p2p_serve: Arc<P2PSnapshotServeRegistry>,
}

impl SnapshotRuntimeRecoveryHandle {
    pub async fn release_runtime(&self, runtime: &crate::RuntimeIdentity) -> usize {
        self.p2p_serve.unregister_runtime(runtime).await;
        let mut state = self.state.lock().await;
        let mut released = 0;
        for job in state.jobs.values_mut() {
            if job.source_sandbox_id == runtime.sandbox_id {
                job.source_stopped = true;
            }
        }
        state.runtime_references.retain(|_, runtimes| {
            if runtimes.remove(runtime) {
                released += 1;
            }
            !runtimes.is_empty()
        });
        released
    }
}

impl VmService {
    fn emit_runtime_stage(
        &self,
        operation: &'static str,
        snapshot_id: &str,
        sandbox_id: &str,
        request_id: &str,
        phase: &'static str,
        started: Instant,
        total_started: Instant,
    ) {
        if let Some(metrics) = &self.metrics {
            metrics.emit_structured(
                StructuredEvent::new(
                    StructuredLogLevel::Info,
                    StructuredEventName::RuntimeStartStageCompleted,
                    StructuredComponent::Runtime,
                    StructuredOutcome::Success,
                )
                .operation(operation)
                .snapshot_id(snapshot_id)
                .sandbox_id(sandbox_id)
                .request_id(request_id)
                .phase(phase)
                .duration(started.elapsed())
                .total_duration(total_started.elapsed()),
            );
        }
    }

    pub fn new(
        repository: VmRepository,
        capture: Arc<SnapshotCaptureService>,
        runtime_launch_factory: RuntimeLaunchFactory,
    ) -> Self {
        Self {
            catalog: SnapshotRepository::new(),
            repository,
            capture,
            runtime_launch_factory,
            upload: None,
            base_images: None,
            remote_loader: None,
            remote_preparer: None,
            metrics: None,
            p2p_serve: P2PSnapshotServeRegistry::new(),
            state: Arc::new(Mutex::new(SnapshotRuntimeState::default())),
            snapshot_locks: Mutex::new(HashMap::new()),
        }
    }
    pub(crate) fn recovery_handle(&self) -> SnapshotRuntimeRecoveryHandle {
        SnapshotRuntimeRecoveryHandle {
            state: self.state.clone(),
            p2p_serve: Arc::clone(&self.p2p_serve),
        }
    }

    pub fn with_catalog(mut self, catalog: Arc<SnapshotRepository>) -> Self {
        self.catalog = catalog;
        self
    }

    pub fn catalog(&self) -> Arc<SnapshotRepository> {
        Arc::clone(&self.catalog)
    }

    pub fn with_upload(mut self, upload: Arc<SnapshotUploadService>) -> Self {
        self.catalog = upload.catalog();
        self.upload = Some(upload);
        self
    }

    pub fn with_base_image_ingestion(
        mut self,
        base_images: Arc<BaseImageIngestionService>,
    ) -> Self {
        self.base_images = Some(base_images);
        self
    }

    pub fn with_remote_start(
        mut self,
        loader: Arc<dyn SnapshotReader>,
        preparer: Arc<SnapshotRestorePreparer>,
    ) -> Self {
        self.remote_loader = Some(loader);
        self.remote_preparer = Some(preparer);
        self
    }

    pub fn with_metrics(mut self, metrics: Arc<SnapdMetrics>) -> Self {
        self.metrics = Some(metrics);
        self
    }

    pub fn repository(&self) -> &VmRepository {
        &self.repository
    }

    /// Source discovery for native layers never enters the uploader, canonical
    /// format builder, remote loader, or evictable physical byte cache.
    pub async fn p2p_native_metadata(
        &self,
        snapshot_id: &str,
        generation_id: Option<&str>,
    ) -> Result<NativeSnapshotMetadata, SnapshotControlError> {
        validate_source(snapshot_id)?;
        // Unknown peers must not allocate a persistent snapshot-lock entry.
        if !self
            .catalog
            .get(snapshot_id)
            .and_then(|source| source.downcast::<Snapshot>())
            .is_some_and(|source| source.native_is_locally_ready())
        {
            return Err(SnapshotControlError::NotFound(
                "native snapshot is not locally ready".into(),
            ));
        }
        let lock = self.snapshot_lock(snapshot_id).await;
        let _guard = lock.lock().await;
        {
            let mut state = self.state.lock().await;
            ensure_snapshot_mutation_not_fenced_locked(&mut state, snapshot_id, Instant::now())?;
        }
        let local = self
            .catalog
            .get(snapshot_id)
            .and_then(|source| source.downcast::<Snapshot>())
            .filter(|source| source.native_is_locally_ready())
            .ok_or_else(|| {
                SnapshotControlError::NotFound("native snapshot is not locally ready".into())
            })?;
        let metadata = self
            .p2p_serve
            .register_native(&local)
            .await
            .map_err(p2p_serve_error)?;
        if generation_id.is_some_and(|expected| expected != metadata.generation_id) {
            return Err(SnapshotControlError::IdentityConflict(
                "native snapshot generation differs".into(),
            ));
        }
        Ok(metadata)
    }

    pub async fn p2p_native_coverage(
        &self,
        snapshot_id: &str,
        request: &NativeCoverageRequest,
    ) -> Result<NativeCoverageResult, SnapshotControlError> {
        validate_source(snapshot_id)?;
        {
            let mut state = self.state.lock().await;
            ensure_snapshot_mutation_not_fenced_locked(&mut state, snapshot_id, Instant::now())?;
        }
        self.p2p_serve
            .native_coverage(snapshot_id, request)
            .await
            .map_err(p2p_serve_error)
    }

    pub async fn p2p_native_read_range(
        &self,
        snapshot_id: &str,
        request: &NativeReadRangeRequest,
    ) -> Result<Vec<u8>, SnapshotControlError> {
        validate_source(snapshot_id)?;
        {
            let mut state = self.state.lock().await;
            ensure_snapshot_mutation_not_fenced_locked(&mut state, snapshot_id, Instant::now())?;
        }
        let bytes = self
            .p2p_serve
            .read_native_range(snapshot_id, request)
            .await
            .map_err(p2p_serve_error)?;
        if let Some(metrics) = &self.metrics {
            metrics.add_p2p_source_range();
        }
        Ok(bytes)
    }

    async fn ensure_published_p2p_source(
        &self,
        snapshot_id: &str,
        spec_checksum: &str,
    ) -> Result<bool, SnapshotControlError> {
        let started = Instant::now();
        let Some(upload) = &self.upload else {
            return Ok(false);
        };
        let Some(source) = upload
            .published_p2p_source(snapshot_id, spec_checksum)
            .await
            .map_err(upload_error)?
        else {
            return Ok(false);
        };
        let Some(loader) = &self.remote_loader else {
            return Ok(false);
        };
        let Some(source_sandbox_id) = self
            .state
            .lock()
            .await
            .jobs
            .get(snapshot_id)
            .map(|job| job.source_sandbox_id.clone())
        else {
            return Ok(false);
        };
        let binding = match self.repository.capture_binding(&source_sandbox_id).await {
            Ok(binding) => binding,
            Err(_) => return Ok(false),
        };
        let spec_value = serde_json::to_value(&source.published.spec).map_err(|error| {
            SnapshotControlError::InvalidState(format!(
                "published P2P source spec cannot be serialized: {error}"
            ))
        })?;
        let metadata: Arc<dyn crate::snapshot::SnapshotInstance> = Arc::clone(loader)
            .load_for_start_shared(&spec_value, spec_checksum, snapshot_id)
            .await
            .map_err(snapshot_load_error)?;
        self.catalog
            .register_opened(Arc::clone(&metadata))
            .map_err(SnapshotControlError::IdentityConflict)?;
        self.p2p_serve
            .register_local_graph(
                &source.local,
                &source.published,
                spec_checksum,
                Some(Arc::new(binding.p2p_source)),
                metadata.as_ref(),
            )
            .await
            .map_err(p2p_serve_error)?;
        if let Some(preparer) = &self.remote_preparer {
            self.p2p_serve
                .register_remote_runtime(
                    snapshot_id,
                    spec_checksum,
                    &binding.context.runtime,
                    preparer.service(),
                    metadata.as_ref(),
                )
                .await
                .map_err(p2p_serve_error)?;
        }
        if let Some(metrics) = &self.metrics {
            metrics.emit_structured(
                StructuredEvent::new(
                    StructuredLogLevel::Info,
                    StructuredEventName::P2PProbeCompleted,
                    StructuredComponent::P2P,
                    StructuredOutcome::Success,
                )
                .operation("p2p_source_prepare")
                .snapshot_id(snapshot_id)
                .duration(started.elapsed()),
            );
        }
        Ok(true)
    }

    pub async fn p2p_availability(
        &self,
        snapshot_id: &str,
        spec_checksum: &str,
    ) -> Result<P2PAvailabilityResult, SnapshotControlError> {
        self.p2p_availability_inner(snapshot_id, spec_checksum, false)
            .await
    }

    pub async fn p2p_selection_availability(
        &self,
        snapshot_id: &str,
        spec_checksum: &str,
    ) -> Result<P2PAvailabilityResult, SnapshotControlError> {
        self.p2p_availability_inner(snapshot_id, spec_checksum, true)
            .await
    }

    async fn p2p_availability_inner(
        &self,
        snapshot_id: &str,
        spec_checksum: &str,
        selection_only: bool,
    ) -> Result<P2PAvailabilityResult, SnapshotControlError> {
        validate_source(snapshot_id)?;
        validate_p2p_checksum(spec_checksum)?;
        // Reject arbitrary, unknown identities before allocating a persistent
        // per-snapshot lock. A snapshot is discoverable when control state
        // still owns its job/runtime, or when a complete graph is already
        // registered. Fences and invalidation remain fail-closed.
        let known_in_state = {
            let mut state = self.state.lock().await;
            ensure_snapshot_mutation_not_fenced_locked(&mut state, snapshot_id, Instant::now())?;
            snapshot_is_known_for_p2p_locked(&state, snapshot_id)
        };
        let registered = self
            .p2p_serve
            .availability_for_probe(snapshot_id, spec_checksum, selection_only)
            .await;
        if !known_in_state && !registered.complete {
            return Ok(registered);
        }
        // Serialize discovery -> graph load -> registration with snapshot
        // invalidation. Otherwise a discovery that began before deletion could
        // repopulate the P2P registry after invalidation cleared it.
        let snapshot_lock = self.snapshot_lock(snapshot_id).await;
        // The restore probe has a 300 ms budget. Never queue behind source
        // preparation or deletion: a not-yet-ready peer safely falls back to
        // object storage. Full diagnostic discovery still owns this lock.
        let _snapshot_guard = if selection_only {
            match snapshot_lock.try_lock() {
                Ok(guard) => guard,
                Err(_) => {
                    return Ok(P2PAvailabilityResult {
                        snapshot_id: snapshot_id.into(),
                        spec_checksum: spec_checksum.into(),
                        available: false,
                        complete: false,
                        servable_objects: 0,
                    })
                }
            }
        } else {
            snapshot_lock.lock().await
        };
        let known_in_state = {
            let mut state = self.state.lock().await;
            ensure_snapshot_mutation_not_fenced_locked(&mut state, snapshot_id, Instant::now())?;
            snapshot_is_known_for_p2p_locked(&state, snapshot_id)
        };
        let registered = self
            .p2p_serve
            .availability_for_probe(snapshot_id, spec_checksum, selection_only)
            .await;
        // Selection is read-only. Loading metadata and building the source
        // graph belongs to finalize, not the first target's timed probe.
        if selection_only || registered.complete {
            return Ok(registered);
        }
        if !known_in_state {
            return Ok(registered);
        }

        match self
            .ensure_published_p2p_source(snapshot_id, spec_checksum)
            .await
        {
            Ok(_) | Err(SnapshotControlError::NotFound(_)) => {}
            Err(error) => return Err(error),
        }
        Ok(self
            .p2p_serve
            .availability_for_probe(snapshot_id, spec_checksum, selection_only)
            .await)
    }

    pub async fn p2p_read_range(
        &self,
        snapshot_id: &str,
        request: &P2PReadRangeRequest,
    ) -> Result<Vec<u8>, SnapshotControlError> {
        let started = Instant::now();
        validate_source(snapshot_id)?;
        request.validate().map_err(p2p_serve_error)?;
        // Availability is the only operation allowed to discover and index a
        // local source. A direct-TCP session must probe first; range reads use
        // that already-validated registry entry and never rebuild the complete
        // Snapshot graph for every 4 KiB guest fault.
        let result = self
            .p2p_serve
            .read_range(snapshot_id, request)
            .await
            .map_err(p2p_serve_error);
        if let Some(metrics) = &self.metrics {
            let (level, outcome, error_code) = match &result {
                Ok(_) => {
                    metrics.add_p2p_source_range();
                    return result;
                }
                // A partial peer's residency miss is normal data-plane traffic.
                Err(SnapshotControlError::NotFound(_)) => return result,
                Err(SnapshotControlError::IdentityConflict(_)) => (
                    StructuredLogLevel::Error,
                    StructuredOutcome::Error,
                    Some("identity_conflict"),
                ),
                Err(_) => (
                    StructuredLogLevel::Warning,
                    StructuredOutcome::Error,
                    Some("read_error"),
                ),
            };
            let mut event = StructuredEvent::new(
                level,
                StructuredEventName::P2PRangeCompleted,
                StructuredComponent::P2P,
                outcome,
            )
            .operation("p2p_source_read")
            .snapshot_id(snapshot_id)
            .object_sha256(&request.object_sha256)
            .range(request.offset, request.length)
            .duration(started.elapsed());
            if let Some(error_code) = error_code {
                event = event.error_code(error_code);
            }
            metrics.emit_structured(event);
        }
        result
    }

    pub async fn create_base_image(
        &self,
        request: BuildImage,
    ) -> Result<ImageBuildAccepted, SnapshotControlError> {
        request.validate().map_err(SnapshotControlError::Invalid)?;
        let snapshot_lock = self.snapshot_lock(&request.snapshot_id).await;
        let _snapshot_guard = snapshot_lock.lock().await;
        {
            let mut state = self.state.lock().await;
            ensure_snapshot_mutation_not_fenced_locked(
                &mut state,
                &request.snapshot_id,
                Instant::now(),
            )?;
            if state.jobs.contains_key(&request.snapshot_id)
                || self.catalog.get(&request.snapshot_id).is_some()
            {
                return Err(SnapshotControlError::IdentityConflict(format!(
                    "snapshot ID {:?} is already occupied",
                    request.snapshot_id
                )));
            }
        }
        let service = self.base_images.as_ref().ok_or_else(|| {
            SnapshotControlError::InvalidState("BASE_IMAGE ingestion is not configured".into())
        })?;
        let result = service
            .create(request.clone())
            .await
            .map_err(base_image_error)?;
        Ok(result)
    }

    pub async fn snapshot_sandbox(
        &self,
        source_sandbox_id: &str,
        request: CaptureSnapshot,
        deadline_unix_ms: u64,
    ) -> Result<SnapshotCaptured, SnapshotControlError> {
        request.validate().map_err(SnapshotControlError::Invalid)?;
        validate_source(source_sandbox_id)?;
        let snapshot_lock = self.snapshot_lock(&request.snapshot_id).await;
        let _snapshot_guard = snapshot_lock.lock().await;
        {
            let mut state = self.state.lock().await;
            ensure_snapshot_mutation_not_fenced_locked(
                &mut state,
                &request.snapshot_id,
                Instant::now(),
            )?;
        }
        self.begin_job(&request.snapshot_id, source_sandbox_id)
            .await?;
        let result = async {
            let _source_guard = self
                .try_source_cut(source_sandbox_id, &request.snapshot_id)
                .await?;
            let upload_target = self.upload_preflight_for_source(source_sandbox_id).await?;
            let local = self
                .capture_local(source_sandbox_id, &request, deadline_unix_ms)
                .await?;
            self.catalog
                .register_raw(Arc::clone(&local))
                .map_err(SnapshotControlError::IdentityConflict)?;
            self.register_upload(local.clone(), request.clone(), upload_target)
                .await?;
            Ok::<_, SnapshotControlError>(local)
        }
        .await;
        match result {
            Ok(local) => Ok(snapshot_result(&local)),
            Err(error) => {
                self.fail_job(&request.snapshot_id, error.clone()).await;
                Err(error)
            }
        }
    }

    pub async fn fork_sandbox(
        &self,
        source_sandbox_id: &str,
        request: ForkSnapshot,
        deadline_unix_ms: u64,
    ) -> Result<ForkCaptured, SnapshotControlError> {
        let total_started = Instant::now();
        request.validate().map_err(SnapshotControlError::Invalid)?;
        if let Some(metrics) = &self.metrics {
            metrics.set_fork_children(request.children.len() as u64);
        }
        validate_source(source_sandbox_id)?;
        let snapshot_id = request.snapshot.snapshot_id.clone();
        let snapshot_lock = self.snapshot_lock(&snapshot_id).await;
        let _snapshot_guard = snapshot_lock.lock().await;
        {
            let mut state = self.state.lock().await;
            ensure_snapshot_mutation_not_fenced_locked(&mut state, &snapshot_id, Instant::now())?;
        }
        self.begin_job(&snapshot_id, source_sandbox_id).await?;
        self.emit_runtime_stage(
            "fork_sandbox",
            &snapshot_id,
            source_sandbox_id,
            &snapshot_id,
            "fork_control_preflight",
            total_started,
            total_started,
        );
        let result: Result<ForkCaptured, SnapshotControlError> = async {
            let _source_guard = self.try_source_cut(source_sandbox_id, &snapshot_id).await?;
            let upload_target = self.upload_preflight_for_source(source_sandbox_id).await?;
            let capture_started = Instant::now();
            let local = self
                .capture_local(source_sandbox_id, &request.snapshot, deadline_unix_ms)
                .await?;
            self.catalog
                .register_raw(Arc::clone(&local))
                .map_err(SnapshotControlError::IdentityConflict)?;
            self.emit_runtime_stage(
                "fork_sandbox",
                &snapshot_id,
                source_sandbox_id,
                &snapshot_id,
                "fork_capture_local",
                capture_started,
                total_started,
            );
            let spec_sha256 = local_runtime_spec_sha256(&local, upload_target.as_ref())?;
            let children_started = Instant::now();
            let children = self
                .launch_fork_children(&request, local.clone(), spec_sha256)
                .await?;
            self.emit_runtime_stage(
                "fork_sandbox",
                &snapshot_id,
                source_sandbox_id,
                &snapshot_id,
                "fork_runtime_children",
                children_started,
                total_started,
            );
            let upload_started = Instant::now();
            self.register_upload(local.clone(), request.snapshot.clone(), upload_target)
                .await?;
            self.emit_runtime_stage(
                "fork_sandbox",
                &snapshot_id,
                source_sandbox_id,
                &snapshot_id,
                "fork_upload_registration",
                upload_started,
                total_started,
            );
            Ok(ForkCaptured {
                snapshot: snapshot_result(&local),
                children,
            })
        }
        .await;
        if let Err(error) = &result {
            self.fail_job(&snapshot_id, error.clone()).await;
        }
        result
    }

    pub async fn snapshot_status(
        &self,
        snapshot_id: &str,
    ) -> Result<SnapshotStatus, SnapshotControlError> {
        if let Some(base_images) = &self.base_images {
            match base_images.status(snapshot_id).await {
                Ok(status) => return Ok(status),
                Err(BaseImageIngestionError::NotFound(_)) => {}
                Err(error) => return Err(base_image_error(error)),
            }
        }
        if let Some(upload) = &self.upload {
            match upload.status(snapshot_id).await {
                Ok(status) => return Ok(status),
                Err(SnapshotUploadServiceError::NotFound(_)) => {}
                Err(error) => return Err(upload_error(error)),
            }
        }
        let state = self.state.lock().await;
        let job = state.jobs.get(snapshot_id);
        let snapshot = self
            .catalog
            .get(snapshot_id)
            .ok_or_else(|| SnapshotControlError::NotFound(snapshot_id.into()))?;
        if job.is_none() && snapshot.state() != PublicationState::RemoteReady {
            return Err(SnapshotControlError::NotFound(snapshot_id.into()));
        }
        let persistent_spec = snapshot
            .spec()
            .map(serde_json::to_value)
            .transpose()
            .map_err(|error| {
                SnapshotControlError::InvalidState(format!(
                    "snapshot spec cannot be serialized: {error}"
                ))
            })?;
        let persistent_checksum = persistent_spec
            .as_ref()
            .map(|value| {
                crate::snapshot::metadata::snapshot_spec_checksum(value)
                    .map_err(|error| SnapshotControlError::InvalidState(error.to_string()))
            })
            .transpose()?;
        let chains = snapshot.chains();
        let memory_bytes = chains
            .iter()
            .find(|chain| chain.kind == SnapshotLayerKind::Memory)
            .map(|chain| chain.logical_bytes);
        let rootfs_bytes = chains
            .iter()
            .find(|chain| chain.kind == SnapshotLayerKind::Block)
            .map(|chain| chain.logical_bytes);
        let layers = snapshot
            .chains()
            .into_iter()
            .map(|chain| {
                (
                    chain.kind,
                    LayerUploadStatus {
                        upload_state: if snapshot.state() == PublicationState::RemoteReady {
                            LayerUploadState::RemoteReady
                        } else {
                            LayerUploadState::LocalReady
                        },
                        remote_publishing: false,
                        attempt_count: 0,
                        last_error: None,
                        retry_at_unix_ms: None,
                    },
                )
            })
            .collect();
        let catalog_pin_held = Arc::clone(&snapshot)
            .downcast::<Snapshot>()
            .filter(|snapshot| snapshot.has_native())
            .as_ref()
            .map(|local| !local.is_closed().unwrap_or(false))
            .unwrap_or(false);
        Ok(SnapshotStatus {
            snapshot_id: snapshot_id.into(),
            state: snapshot.state(),
            remote_publishing: false,
            progress: if snapshot.state() == PublicationState::Creating {
                0.0
            } else {
                1.0
            },
            attempt_count: 0,
            last_error: job
                .and_then(|job| job.last_error.as_ref())
                .map(ToString::to_string),
            failure_code: job
                .and_then(|job| job.last_error.as_ref())
                .map(|error| error.code().into()),
            retryable: job
                .and_then(|job| job.last_error.as_ref())
                .is_some_and(SnapshotControlError::retryable),
            retry_at_unix_ms: None,
            snapshot_spec: persistent_spec,
            memory_bytes,
            rootfs_bytes,
            spec_checksum: persistent_checksum,
            admin_finalized: false,
            catalog_pin_held,
            layers,
        })
    }

    pub async fn finalize_published_snapshot(
        &self,
        snapshot_id: &str,
        request: &FinalizeSnapshot,
    ) -> Result<PublicationFinalized, SnapshotControlError> {
        request.validate().map_err(SnapshotControlError::Invalid)?;
        let is_base_image = match &self.base_images {
            Some(base_images) => base_images
                .contains(snapshot_id)
                .await
                .map_err(base_image_error)?,
            None => false,
        };
        let upload = self.upload.as_ref().ok_or_else(|| {
            SnapshotControlError::InvalidState("remote upload is not configured".into())
        })?;
        if is_base_image {
            return upload
                .finalize_published(snapshot_id, &request.spec_checksum)
                .await
                .map_err(upload_error);
        }
        let snapshot_lock = self.snapshot_lock(snapshot_id).await;
        let _snapshot_guard = snapshot_lock.lock().await;
        let source_sandbox_id = {
            let mut state = self.state.lock().await;
            ensure_snapshot_mutation_not_fenced_locked(&mut state, snapshot_id, Instant::now())?;
            state
                .jobs
                .get(snapshot_id)
                .map(|job| job.source_sandbox_id.clone())
        };
        let Some(source_sandbox_id) = source_sandbox_id else {
            return upload
                .finalize_published(snapshot_id, &request.spec_checksum)
                .await
                .map_err(upload_error);
        };
        let Some(source) = upload
            .finalization_source(snapshot_id, &request.spec_checksum)
            .await
            .map_err(upload_error)?
        else {
            // Source-free publications still need the preparation barrier.
            self.ensure_published_p2p_source(snapshot_id, &request.spec_checksum)
                .await?;
            return upload
                .finalize_published(snapshot_id, &request.spec_checksum)
                .await
                .map_err(upload_error);
        };
        let source_stopped = self
            .state
            .lock()
            .await
            .jobs
            .get(snapshot_id)
            .is_some_and(|job| job.source_stopped);
        if source_stopped {
            self.capture
                .mark_stopped_source_published(snapshot_id)
                .await;
            self.ensure_published_p2p_source(snapshot_id, &request.spec_checksum)
                .await?;
            let result = upload
                .finalize_published(snapshot_id, &request.spec_checksum)
                .await
                .map_err(upload_error)?;
            // Retain ancestry grants for an already-admitted local restart. The
            // snapshot's invalidation releases them with its other local aliases.
            return Ok(result);
        }
        let status = self.capture.status(snapshot_id).await.ok_or_else(|| {
            SnapshotControlError::InvalidState(
                "capture identity is unavailable for finalization".into(),
            )
        })?;
        let capture_correlation_id = status.capture_correlation_id.ok_or_else(|| {
            SnapshotControlError::InvalidState("capture correlation identity is unavailable".into())
        })?;
        let capture_request_sha256 = status.capture_request_sha256.ok_or_else(|| {
            SnapshotControlError::InvalidState(
                "capture canonical request identity is unavailable".into(),
            )
        })?;
        let binding = self.repository.capture_binding(&source_sandbox_id).await?;
        let _runtime_guard = self.capture.lock_runtime(&binding.context.runtime).await;
        if !self.capture.can_finalize(snapshot_id).await {
            return Err(SnapshotControlError::InvalidState(
                "Earlier captured snapshots must be promoted first; retry finalization later"
                    .into(),
            ));
        }
        let preparer = self.remote_preparer.as_ref().ok_or_else(|| {
            SnapshotControlError::InvalidState(
                "remote runtime preparation is not configured".into(),
            )
        })?;
        let prepared = preparer
            .prepare_finalization(
                &source.local,
                &source.published.spec,
                &binding.context.runtime,
                &self.repository,
            )
            .await
            .map_err(snapshot_restore_error)?;
        let bare_spec_sha256 = request
            .spec_checksum
            .strip_prefix("sha256:")
            .ok_or_else(|| SnapshotControlError::Invalid("spec checksum is not SHA-256".into()))?;
        if let Err(error) = self
            .repository
            .promote_published_snapshot(
                &source_sandbox_id,
                &capture_correlation_id,
                &capture_request_sha256,
                snapshot_id,
                bare_spec_sha256,
                &prepared,
            )
            .await
        {
            if !matches!(error, RuntimeStartError::FinalizationUnknown(_)) {
                prepared.rollback(preparer.service()).await;
            }
            return Err(error.into());
        }
        self.capture
            .mark_finalized(&binding.context.runtime, snapshot_id)
            .await;
        // Build the complete serving index while the upload pin and the
        // per-snapshot lock are held. A successful finalize therefore leaves
        // the first restore probe with only an identity/residency lookup.
        self.ensure_published_p2p_source(snapshot_id, &request.spec_checksum)
            .await?;
        let result = upload
            .finalize_published(snapshot_id, &request.spec_checksum)
            .await
            .map_err(upload_error)?;
        self.repository.release_local_snapshot(snapshot_id).await;
        Ok(result)
    }

    pub async fn local_snapshot(
        &self,
        snapshot_id: &str,
    ) -> Result<Arc<Snapshot>, SnapshotControlError> {
        let snapshot = self
            .catalog
            .get(snapshot_id)
            .and_then(|snapshot| snapshot.downcast::<Snapshot>())
            .filter(|snapshot| snapshot.has_native())
            .ok_or_else(|| SnapshotControlError::NotFound(snapshot_id.into()))?;
        snapshot
            .ensure_source_integrity()
            .map_err(|error| SnapshotControlError::InvalidState(error.to_string()))?;
        Ok(snapshot)
    }

    pub async fn active_runtimes(
        &self,
        snapshot_id: &str,
    ) -> Result<Vec<crate::RuntimeIdentity>, SnapshotControlError> {
        let state = self.state.lock().await;
        let mut runtimes = state
            .runtime_references
            .get(snapshot_id)
            .cloned()
            .unwrap_or_default()
            .into_iter()
            .collect::<Vec<_>>();
        sort_runtimes(&mut runtimes);
        Ok(runtimes)
    }

    pub async fn check_snapshot_usage(
        &self,
        snapshot_id: &str,
        request: &SnapshotFence,
    ) -> Result<SnapshotUsage, SnapshotControlError> {
        request.validate().map_err(SnapshotControlError::Invalid)?;
        validate_source(snapshot_id)?;
        let mut state = self.state.lock().await;
        check_snapshot_usage_locked(&mut state, snapshot_id, request, Instant::now())
    }

    pub async fn abort_delete_fence(
        &self,
        snapshot_id: &str,
        request: &AbortDeleteFence,
    ) -> Result<OperationAccepted, SnapshotControlError> {
        request.validate().map_err(SnapshotControlError::Invalid)?;
        validate_source(snapshot_id)?;
        let key = snapshot_id.to_owned();
        let mut state = self.state.lock().await;
        if state.invalidating_snapshots.contains(&key) {
            return Err(SnapshotControlError::RetryableInvalidState(
                "snapshot invalidation is in progress".into(),
            ));
        }
        let fence = state.delete_fences.get(&key).ok_or_else(|| {
            SnapshotControlError::InvalidState("snapshot has no delete fence".into())
        })?;
        if fence.nonce != request.fence_nonce {
            return Err(SnapshotControlError::IdentityConflict(
                "delete fence nonce differs".into(),
            ));
        }
        state.delete_fences.remove(&key);
        Ok(OperationAccepted {
            identity: snapshot_id.into(),
            state: "FENCE_RELEASED".into(),
        })
    }

    pub async fn invalidate_snapshot(
        &self,
        snapshot_id: &str,
        request: &InvalidateSnapshot,
    ) -> Result<SnapshotInvalidated, SnapshotControlError> {
        request.validate().map_err(SnapshotControlError::Invalid)?;
        validate_source(snapshot_id)?;
        let snapshot_lock = self.snapshot_lock(snapshot_id).await;
        let _snapshot_guard = snapshot_lock.lock().await;
        let key = snapshot_id.to_owned();
        {
            let mut state = self.state.lock().await;
            if state.invalidated_snapshots.contains(&key) {
                return Err(SnapshotControlError::NotFound(snapshot_id.into()));
            }
            let fence = state.delete_fences.get(&key).ok_or_else(|| {
                SnapshotControlError::InvalidState("valid delete fence is required".into())
            })?;
            if fence.nonce != request.fence_nonce || fence.expires_at <= Instant::now() {
                return Err(SnapshotControlError::InvalidState(
                    "valid delete fence is required".into(),
                ));
            }
            if state
                .runtime_references
                .get(&key)
                .is_some_and(|runtimes| !runtimes.is_empty())
            {
                return Err(SnapshotControlError::InvalidState(
                    "snapshot acquired a runtime reference after fencing".into(),
                ));
            }
            if !state.invalidating_snapshots.insert(key.clone()) {
                return Err(SnapshotControlError::RetryableInvalidState(
                    "snapshot invalidation is already in progress".into(),
                ));
            }
        }
        let invalidate_aliases = async {
            if let Some(base_images) = &self.base_images {
                base_images
                    .ensure_invalidation_ready(snapshot_id)
                    .await
                    .map_err(base_image_error)?;
            }
            if let Some(upload) = &self.upload {
                upload
                    .ensure_invalidation_ready(snapshot_id)
                    .await
                    .map_err(upload_error)?;
            }
            if let Some(upload) = &self.upload {
                upload.invalidate(snapshot_id).await.map_err(upload_error)?;
            }
            if let Some(base_images) = &self.base_images {
                base_images
                    .invalidate(snapshot_id)
                    .await
                    .map_err(base_image_error)?;
            }
            self.capture.invalidate_snapshot(snapshot_id).await;
            self.repository.release_local_snapshot(snapshot_id).await;
            self.p2p_serve.invalidate(snapshot_id).await;
            Ok::<_, SnapshotControlError>(())
        }
        .await;
        if let Err(error) = invalidate_aliases {
            self.state.lock().await.invalidating_snapshots.remove(&key);
            return Err(error);
        }
        let mut state = self.state.lock().await;
        if state
            .delete_fences
            .get(&key)
            .is_none_or(|fence| fence.nonce != request.fence_nonce)
        {
            state.invalidating_snapshots.remove(&key);
            return Err(SnapshotControlError::InvalidState(
                "delete fence changed during invalidation".into(),
            ));
        }
        let invalidated_aliases = state
            .runtime_references
            .remove(&key)
            .map_or(0, |runtimes| runtimes.len() as u64);
        state.jobs.remove(snapshot_id);
        self.catalog.invalidate(snapshot_id);
        state.delete_fences.remove(&key);
        state.invalidating_snapshots.remove(&key);
        let result = SnapshotInvalidated {
            identity: snapshot_id.into(),
            state: "INVALIDATED".into(),
            invalidated_aliases,
        };
        state.invalidated_snapshots.insert(key);
        if state.invalidated_snapshots.len() > MAX_INVALIDATED_SNAPSHOTS {
            state.invalidated_snapshots.pop_first();
        }
        drop(state);
        if let Some(loader) = &self.remote_loader {
            loader.invalidate_start_cache(snapshot_id).await;
        }
        Ok(result)
    }

    pub(crate) async fn remove_runtime_reference(
        &self,
        snapshot_id: &str,
        runtime: &crate::RuntimeIdentity,
    ) {
        let mut state = self.state.lock().await;
        let key = snapshot_id.to_owned();
        if let Some(runtimes) = state.runtime_references.get_mut(&key) {
            runtimes.remove(runtime);
            if runtimes.is_empty() {
                state.runtime_references.remove(&key);
            }
        }
    }

    async fn upload_preflight_for(
        &self,
        provenance: Option<&SnapshotProvenance>,
    ) -> Result<Option<ResolvedSnapshotUploadTarget>, SnapshotControlError> {
        match &self.upload {
            Some(upload) if upload.enabled() => upload
                .preflight_for(provenance)
                .await
                .map(Some)
                .map_err(upload_error),
            _ => Ok(None),
        }
    }

    /// The caller holds the source cut lock, so the image identity used for
    /// target selection belongs to the exact runtime about to be captured.
    async fn upload_preflight_for_source(
        &self,
        source_sandbox_id: &str,
    ) -> Result<Option<ResolvedSnapshotUploadTarget>, SnapshotControlError> {
        if self.upload.as_ref().is_none_or(|upload| !upload.enabled()) {
            return Ok(None);
        }
        let binding = self
            .repository
            .capture_binding(source_sandbox_id)
            .await
            .map_err(|error| SnapshotControlError::NotFound(error.to_string()))?;
        self.upload_preflight_for(binding.context.lineage.provenance())
            .await
    }

    async fn register_upload(
        &self,
        local: Arc<Snapshot>,
        request: CaptureSnapshot,
        target: Option<ResolvedSnapshotUploadTarget>,
    ) -> Result<(), SnapshotControlError> {
        match (&self.upload, target) {
            (Some(upload), Some(target)) => upload
                .register(
                    local,
                    SnapshotPublication {
                        snapshot_id: request.snapshot_id,
                    },
                    target,
                )
                .await
                .map(|_| ())
                .map_err(upload_error),
            (None, None) => Ok(()),
            (Some(upload), None) if !upload.enabled() => Ok(()),
            _ => Err(SnapshotControlError::InvalidState(
                "upload preflight state is inconsistent".into(),
            )),
        }
    }

    async fn capture_local(
        &self,
        source_sandbox_id: &str,
        request: &CaptureSnapshot,
        deadline_unix_ms: u64,
    ) -> Result<Arc<Snapshot>, SnapshotControlError> {
        let binding = self
            .repository
            .capture_binding(source_sandbox_id)
            .await
            .map_err(|error| SnapshotControlError::NotFound(error.to_string()))?;
        let key = request.snapshot_id.clone();
        self.state
            .lock()
            .await
            .runtime_references
            .entry(key.clone())
            .or_default()
            .insert(binding.context.runtime.clone());
        let instance = self
            .catalog
            .get(&request.snapshot_id)
            .and_then(|instance| instance.downcast::<Snapshot>())
            .ok_or_else(|| {
                SnapshotControlError::InvalidState(
                    "capture has no reserved snapshot instance".into(),
                )
            })?;
        let dependencies = self
            .repository
            .runtime_binding(source_sandbox_id)
            .await
            .ok_or_else(|| {
                SnapshotControlError::NotFound("source runtime binding is absent".into())
            })?
            .dependencies
            .clone();
        let _capture_guard = dependencies
            .begin_capture()
            .map_err(SnapshotControlError::InvalidState)?;
        let captured = self
            .capture
            .capture(
                &binding.context,
                CaptureRequest {
                    snapshot_id: request.snapshot_id.clone(),
                    deadline_unix_ms,
                },
                instance,
            )
            .await;
        match captured {
            Ok(local) => {
                self.repository
                    .pin_local_snapshot(source_sandbox_id, &local)
                    .await?;
                self.p2p_serve
                    .register_native(&local)
                    .await
                    .map_err(p2p_serve_error)?;
                Ok(local)
            }
            Err(error) => {
                let committed_or_unknown = self
                    .capture
                    .requires_source_reference(&request.snapshot_id)
                    .await;
                if !committed_or_unknown {
                    if let Some(runtimes) = self.state.lock().await.runtime_references.get_mut(&key)
                    {
                        runtimes.remove(&binding.context.runtime);
                    }
                }
                Err(error.into())
            }
        }
    }

    async fn launch_fork_children(
        &self,
        request: &ForkSnapshot,
        local: Arc<Snapshot>,
        spec_sha256: String,
    ) -> Result<Vec<ForkChild>, SnapshotControlError> {
        let rocklet_artifact = local.rocklet_artifact.clone().ok_or_else(|| {
            SnapshotControlError::Invalid(
                "FULL fork snapshot is missing its captured Rocklet artifact pin".into(),
            )
        })?;
        let runtime_source =
            SnapshotRuntimeSource::Local(LocalSnapshotRuntimeSource::new(local, spec_sha256));
        let mut launch_requests = Vec::with_capacity(request.children.len());
        let mut preflight_errors = HashMap::new();
        for plan in &request.children {
            match (self.runtime_launch_factory)(plan) {
                Ok(launch_spec) => {
                    if let Err(error) =
                        validate_launch_rocklet(&launch_spec, Some(&rocklet_artifact))
                    {
                        preflight_errors.insert(plan.sandbox_id.clone(), error.to_string());
                        continue;
                    }
                    launch_requests.push(RuntimeStartRequest {
                        vm_id: plan.sandbox_id.clone(),
                        launch_spec,
                        runtime_source: runtime_source.clone(),
                        cancel_requested: None,
                    });
                }
                Err(error) => {
                    preflight_errors.insert(plan.sandbox_id.clone(), error.to_string());
                }
            }
        }
        let launched = if launch_requests.is_empty() {
            Vec::new()
        } else {
            self.repository
                .start_runtimes(launch_requests)
                .await?
                .runtimes
        };
        let launched = launched
            .into_iter()
            .map(|child| (child.vm_id.clone(), child))
            .collect::<HashMap<_, _>>();
        let mut observations = HashMap::new();
        for child in launched.values().filter(|child| child.success) {
            if let Some(observation) = self
                .repository
                .guest_runtime_observation(&child.vm_id)
                .await
            {
                observations.insert(child.vm_id.clone(), observation);
            }
        }
        let mut results = Vec::with_capacity(request.children.len());
        let mut state = self.state.lock().await;
        let reference_key = request.snapshot.snapshot_id.clone();
        for plan in &request.children {
            if let Some(error) = preflight_errors.get(&plan.sandbox_id) {
                results.push(ForkChild {
                    sandbox_id: plan.sandbox_id.clone(),
                    state: ForkChildState::Failed,
                    error: Some(error.clone()),
                    guest_endpoint: None,
                    vmm_pid: None,
                    rocklet_artifact: None,
                });
                continue;
            }
            let child = launched.get(&plan.sandbox_id).ok_or_else(|| {
                SnapshotControlError::Runtime(
                    "child runtime returned an incomplete result set".into(),
                )
            })?;
            let result = fork_child_result(child, observations.remove(&plan.sandbox_id));
            if result.state != ForkChildState::Failed {
                state
                    .runtime_references
                    .entry(reference_key.clone())
                    .or_default()
                    .insert(crate::RuntimeIdentity::new(plan.sandbox_id.clone()));
            }
            results.push(result);
        }
        drop(state);
        if let Some(preparer) = &self.remote_preparer {
            for plan in &request.children {
                if launched
                    .get(&plan.sandbox_id)
                    .is_some_and(|child| child.success)
                {
                    preparer
                        .service()
                        .launch_background_prefetch(&crate::RuntimeIdentity::new(
                            plan.sandbox_id.clone(),
                        ))
                        .await;
                }
            }
        }
        Ok(results)
    }

    async fn begin_job(
        &self,
        snapshot_id: &str,
        source_sandbox_id: &str,
    ) -> Result<(), SnapshotControlError> {
        let mut state = self.state.lock().await;
        if state.jobs.contains_key(snapshot_id) || self.catalog.get(snapshot_id).is_some() {
            return Err(SnapshotControlError::IdentityConflict(format!(
                "snapshot ID {snapshot_id:?} is already occupied"
            )));
        }
        let snapshot: Arc<dyn SnapshotInstance> = self
            .catalog
            .reserve_raw(snapshot_id, crate::snapshot::SnapshotKind::Full)
            .map_err(SnapshotControlError::IdentityConflict)?;
        state.jobs.insert(
            snapshot_id.into(),
            SnapshotJob {
                source_sandbox_id: source_sandbox_id.into(),
                source_stopped: false,
                snapshot: Arc::downgrade(&snapshot),
                last_error: None,
            },
        );
        Ok(())
    }

    async fn fail_job(&self, snapshot_id: &str, error: SnapshotControlError) {
        let mut state = self.state.lock().await;
        if let Some(job) = state.jobs.get_mut(snapshot_id) {
            if let Some(snapshot) = job.snapshot.upgrade() {
                self.catalog
                    .update_instance(&snapshot, |state| state.fail("capture", error.to_string()));
            }
            job.last_error = Some(error);
        }
    }

    pub(crate) async fn snapshot_lock(&self, snapshot_id: &str) -> Arc<Mutex<()>> {
        self.snapshot_locks
            .lock()
            .await
            .entry(snapshot_id.into())
            .or_insert_with(|| Arc::new(Mutex::new(())))
            .clone()
    }

    async fn try_source_cut(
        &self,
        source_id: &str,
        snapshot_id: &str,
    ) -> Result<OwnedMutexGuard<()>, SnapshotControlError> {
        let source_lock = self.repository.source_lock(source_id).await;
        match source_lock.try_lock_owned() {
            Ok(guard) => {
                self.repository
                    .ensure_running(source_id)
                    .await
                    .map_err(repository_error)?;
                Ok(guard)
            }
            Err(_) => {
                let message = format!(
                    "source sandbox {source_id:?} is busy; snapshot {snapshot_id:?} was not submitted"
                );
                Err(SnapshotControlError::IdentityConflict(message))
            }
        }
    }
    #[cfg(test)]
    pub(crate) async fn hold_reference_updates_for_test(
        &self,
        entered: tokio::sync::oneshot::Sender<()>,
        release: tokio::sync::oneshot::Receiver<()>,
    ) {
        let _guard = self.state.lock().await;
        let _ = entered.send(());
        let _ = release.await;
    }

    pub(crate) async fn reserve_runtime_reference(
        &self,
        snapshot_id: &str,
        runtime: &crate::RuntimeIdentity,
    ) -> Result<(), SnapshotControlError> {
        reserve_snapshot_runtime_locked(
            &mut *self.state.lock().await,
            snapshot_id,
            runtime,
            Instant::now(),
        )
    }

    pub(crate) async fn release_runtime_references(&self, runtime: &crate::RuntimeIdentity) {
        self.state
            .lock()
            .await
            .runtime_references
            .retain(|_, runtimes| {
                runtimes.remove(runtime);
                !runtimes.is_empty()
            });
    }

    pub(crate) fn register_opened(
        &self,
        snapshot: Arc<dyn crate::snapshot::SnapshotInstance>,
    ) -> Result<(), SnapshotControlError> {
        self.catalog
            .register_opened(snapshot)
            .map_err(SnapshotControlError::IdentityConflict)?;
        Ok(())
    }

    pub(crate) async fn register_remote_runtime(
        &self,
        snapshot_id: &str,
        spec_checksum: &str,
        runtime: &crate::RuntimeIdentity,
        preparer: &SnapshotRestorePreparer,
        snapshot: &dyn crate::snapshot::SnapshotInstance,
    ) -> Result<(), SnapshotControlError> {
        self.p2p_serve
            .register_remote_runtime(
                snapshot_id,
                spec_checksum,
                runtime,
                preparer.service(),
                snapshot,
            )
            .await
            .map_err(p2p_serve_error)
    }

    pub(crate) async fn unregister_runtime(&self, runtime: &crate::RuntimeIdentity) {
        self.p2p_serve.unregister_runtime(runtime).await;
    }

    pub(crate) async fn local_start_spec_checksum(
        &self,
        local: &Snapshot,
    ) -> Result<String, SnapshotControlError> {
        // Reading an existing local generation must not require enough image
        // provenance to select a destination for a future upload. Capture and
        // fork still perform that source-aware preflight before cutting DATA.
        if local
            .provenance()
            .and_then(|provenance| provenance.oci_ref.as_deref())
            .is_none()
        {
            return local_runtime_spec_sha256(local, None);
        }
        let target = self.upload_preflight_for(local.provenance()).await?;
        local_runtime_spec_sha256(local, target.as_ref())
    }
}

fn fork_child_result(
    child: &RuntimeStartResult,
    observation: Option<GuestRuntimeObservation>,
) -> ForkChild {
    let mut result = ForkChild {
        sandbox_id: child.vm_id.clone(),
        state: ForkChildState::Failed,
        error: child.message.clone(),
        guest_endpoint: None,
        vmm_pid: None,
        rocklet_artifact: None,
    };
    if child.success {
        if let Some(observation) = observation {
            let (endpoint, pid, artifact) = runtime_result_parts(observation);
            result.state = ForkChildState::Running;
            result.guest_endpoint = Some(endpoint);
            result.vmm_pid = Some(pid);
            result.rocklet_artifact = Some(artifact);
        } else {
            result.state = ForkChildState::Pending;
            result.error = Some(format!(
                "fork child {} has no guest Rocklet endpoint",
                child.vm_id
            ));
        }
    } else if matches!(child.code.as_str(), "RESULT_UNKNOWN" | "CLEANUP_FAILED") {
        result.state = ForkChildState::Pending;
    }
    result
}

fn snapshot_result(local: &Snapshot) -> SnapshotCaptured {
    let mut layers = local
        .frozen_layers
        .values()
        .map(|layer| layer.layer_id.clone())
        .collect::<Vec<_>>();
    layers.sort();
    SnapshotCaptured {
        snapshot_id: local.snapshot_id.clone(),
        state: CaptureState::LocalReady,
        layer_identities: layers,
    }
}

fn local_runtime_spec_sha256(
    local: &Snapshot,
    target: Option<&ResolvedSnapshotUploadTarget>,
) -> Result<String, SnapshotControlError> {
    local
        .ensure_source_integrity()
        .map_err(|error| SnapshotControlError::InvalidState(error.to_string()))?;
    let parents = local
        .lineage()
        .frozen_snapshots()
        .map(|parent| &parent.snapshot_id)
        .collect::<Vec<_>>();
    let mut identity = serde_json::json!({
        "base_parent_chains": local.remote_parent_chains(),
        "compatibility": target.map(|target| &target.compatibility),
        "format": "frozen-local-runtime-v1",
        "layers": local.frozen_layers,
        "parents": parents,
        "snapshot_id": local.snapshot_id,
        "storage": target.map(|target| &target.storage),
    });
    if target.is_none() {
        // A local runtime binds the frozen source and captured guest artifact;
        // it does not need to invent a persistence provider or credentials.
        identity["rocklet_artifact"] = serde_json::to_value(&local.rocklet_artifact)
            .map_err(|error| SnapshotControlError::Invalid(error.to_string()))?;
    }
    let bytes = canonical_json_bytes(&identity)
        .map_err(|error| SnapshotControlError::Invalid(error.to_string()))?;
    let identity = sha256_identity(&bytes);
    identity
        .strip_prefix("sha256:")
        .map(ToOwned::to_owned)
        .ok_or_else(|| SnapshotControlError::Invalid("spec checksum is not SHA-256".into()))
}

fn validate_p2p_checksum(value: &str) -> Result<(), SnapshotControlError> {
    if value.strip_prefix("sha256:").is_some_and(|hex| {
        hex.len() == 64
            && hex
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    }) {
        Ok(())
    } else {
        Err(SnapshotControlError::Invalid(
            "P2P checksum must be sha256:<64 lowercase hex>".into(),
        ))
    }
}

fn check_snapshot_usage_locked(
    state: &mut SnapshotRuntimeState,
    snapshot_id: &str,
    request: &SnapshotFence,
    now: Instant,
) -> Result<SnapshotUsage, SnapshotControlError> {
    let key = snapshot_id.to_owned();
    if state
        .delete_fences
        .get(&key)
        .is_some_and(|fence| fence.expires_at > now && fence.nonce != request.fence_nonce)
    {
        return Err(SnapshotControlError::RetryableIdentityConflict(
            "another delete fence is active".into(),
        ));
    }
    let mut active_runtimes = state
        .runtime_references
        .get(&key)
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .collect::<Vec<_>>();
    sort_runtimes(&mut active_runtimes);
    let expected = request
        .expected_runtimes
        .iter()
        .cloned()
        .collect::<HashSet<_>>();
    let actual = active_runtimes.iter().cloned().collect::<HashSet<_>>();
    if !expected.is_subset(&actual) {
        return Err(SnapshotControlError::RetryableInvalidState(
            "durable runtime expectation is missing from node-local ownership".into(),
        ));
    }
    if !active_runtimes.is_empty() {
        return Ok(SnapshotUsage {
            snapshot_id: snapshot_id.into(),
            state: SnapshotUsageState::ActiveRuntimes,
            active_runtimes,
        });
    }
    state.delete_fences.insert(
        key,
        DeleteFence {
            nonce: request.fence_nonce.clone(),
            expires_at: now + Duration::from_millis(request.ttl_ms),
        },
    );
    Ok(SnapshotUsage {
        snapshot_id: snapshot_id.into(),
        state: SnapshotUsageState::FencedNoRuntime,
        active_runtimes,
    })
}

fn reserve_snapshot_runtime_locked(
    state: &mut SnapshotRuntimeState,
    snapshot_id: &str,
    runtime: &crate::RuntimeIdentity,
    now: Instant,
) -> Result<(), SnapshotControlError> {
    ensure_snapshot_mutation_not_fenced_locked(state, snapshot_id, now)?;
    state
        .runtime_references
        .entry(snapshot_id.to_owned())
        .or_default()
        .insert(runtime.clone());
    Ok(())
}

fn sort_runtimes(runtimes: &mut [crate::RuntimeIdentity]) {
    runtimes.sort_by(|left, right| left.sandbox_id.cmp(&right.sandbox_id));
}

fn ensure_snapshot_mutation_not_fenced_locked(
    state: &mut SnapshotRuntimeState,
    snapshot_id: &str,
    now: Instant,
) -> Result<(), SnapshotControlError> {
    let key = snapshot_id.to_owned();
    if state.invalidated_snapshots.contains(&key) {
        return Err(SnapshotControlError::IdentityConflict(
            "invalidated snapshot identity cannot be reused".into(),
        ));
    }
    if state.invalidating_snapshots.contains(&key) {
        return Err(SnapshotControlError::RetryableInvalidState(
            "snapshot invalidation is in progress".into(),
        ));
    }
    if state
        .delete_fences
        .get(&key)
        .is_some_and(|fence| fence.expires_at <= now)
    {
        state.delete_fences.remove(&key);
    }
    if state.delete_fences.contains_key(&key) {
        return Err(SnapshotControlError::RetryableIdentityConflict(
            "snapshot has an active delete fence".into(),
        ));
    }
    Ok(())
}

fn snapshot_is_known_for_p2p_locked(state: &SnapshotRuntimeState, snapshot_id: &str) -> bool {
    state.jobs.contains_key(snapshot_id)
        || state
            .runtime_references
            .get(snapshot_id)
            .is_some_and(|runtimes| !runtimes.is_empty())
}

fn upload_error(error: SnapshotUploadServiceError) -> SnapshotControlError {
    error.into()
}

fn p2p_serve_error(error: P2PServeError) -> SnapshotControlError {
    match error {
        P2PServeError::Invalid(message) => SnapshotControlError::Invalid(message),
        P2PServeError::NotFound => {
            SnapshotControlError::NotFound("P2P snapshot DATA is not locally resident".into())
        }
        P2PServeError::IdentityConflict => {
            SnapshotControlError::IdentityConflict("P2P snapshot DATA identity conflicts".into())
        }
        P2PServeError::Io(error) => SnapshotControlError::Runtime(error.to_string()),
    }
}

fn base_image_error(error: BaseImageIngestionError) -> SnapshotControlError {
    error.into()
}

#[derive(Debug, Error)]
pub enum SnapshotCaptureError {
    #[error("Invalid capture request: {0}")]
    Invalid(String),
    #[error("Snapshot capture identity conflict: {0}")]
    IdentityConflict(String),
    #[error("Snapshot capture deadline exceeded: {0}")]
    Deadline(String),
    #[error("VMM capture request failed: {0}")]
    Vmm(String),
    #[error("Snapshot capture result is unknown: {0}")]
    ResultUnknown(String),
    #[error("VMM capture protocol failed: {0}")]
    Protocol(String),
    #[error("Snapshot conversion failed: {0}")]
    Conversion(String),
    #[error("Snapshot capture is terminal: {0}")]
    Terminal(String),
}

impl From<FrozenLayerError> for SnapshotCaptureError {
    fn from(error: FrozenLayerError) -> Self {
        Self::Conversion(error.to_string())
    }
}

impl From<LocalSnapshotError> for SnapshotCaptureError {
    fn from(error: LocalSnapshotError) -> Self {
        Self::Conversion(error.to_string())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CaptureRequest {
    pub snapshot_id: String,
    pub deadline_unix_ms: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CaptureStatus {
    pub has_frozen_pin: bool,
    pub local_ready: bool,
    pub capture_result_unknown: bool,
    pub terminal_error: Option<String>,
    pub pause_duration_ns: Option<u64>,
    pub capture_correlation_id: Option<String>,
    pub capture_request_sha256: Option<String>,
}

struct CaptureRecord {
    instance: Arc<Snapshot>,
    finalized: bool,
    runtime: RuntimeIdentity,
    lineage: SnapshotLineage,
    pin: Option<CapturedSnapshotPin>,
    local: Option<Arc<Snapshot>>,
    terminal_error: Option<String>,
    capture_result_unknown: bool,
    pause_duration_ns: Option<u64>,
    capture_correlation_id: Option<String>,
    capture_request_sha256: Option<String>,
}

#[derive(Clone)]
struct RuntimeHead {
    snapshot_id: String,
    lineage: SnapshotLineage,
    accepted: bool,
}

#[derive(Default)]
struct CaptureTransactionState {
    records: HashMap<String, CaptureRecord>,
    runtime_heads: HashMap<RuntimeIdentity, RuntimeHead>,
    active_runtime_capture: HashMap<RuntimeIdentity, String>,
}

/// Daemon-lifetime owner for stable online snapshot capture.
pub struct SnapshotCaptureService {
    state: Mutex<CaptureTransactionState>,
    runtime_locks: Mutex<HashMap<RuntimeIdentity, Arc<Mutex<()>>>>,
    request_timeout: Duration,
    metrics: Option<Arc<SnapdMetrics>>,
}

impl SnapshotCaptureService {
    pub fn new(request_timeout: Duration) -> Result<Self, SnapshotCaptureError> {
        Self::new_with_metrics(request_timeout, None)
    }

    pub fn new_with_metrics(
        request_timeout: Duration,
        metrics: Option<Arc<SnapdMetrics>>,
    ) -> Result<Self, SnapshotCaptureError> {
        if request_timeout.is_zero() {
            return Err(SnapshotCaptureError::Invalid(
                "capture request timeout must be positive".into(),
            ));
        }
        Ok(Self {
            state: Mutex::new(CaptureTransactionState::default()),
            runtime_locks: Mutex::new(HashMap::new()),
            request_timeout,
            metrics,
        })
    }

    pub async fn capture(
        &self,
        context: &RuntimeCaptureContext,
        request: CaptureRequest,
        instance: Arc<Snapshot>,
    ) -> Result<Arc<Snapshot>, SnapshotCaptureError> {
        validate_context(context)?;
        validate_request(&request)?;
        let key = context.runtime.clone();
        let runtime_lock = {
            let mut locks = self.runtime_locks.lock().await;
            Arc::clone(locks.entry(key).or_insert_with(|| Arc::new(Mutex::new(()))))
        };
        let _runtime_guard = runtime_lock.lock().await;
        self.accept_previous_capture(context, &request).await?;
        self.capture_runtime_locked(context, request, instance)
            .await
    }

    /// Transfer the previous cut before admitting a new one. The immutable
    /// local snapshot owns every backing FD; remote publication is unrelated.
    async fn accept_previous_capture(
        &self,
        context: &RuntimeCaptureContext,
        request: &CaptureRequest,
    ) -> Result<(), SnapshotCaptureError> {
        let accepted = {
            let state = self.state.lock().await;
            if state.records.contains_key(&request.snapshot_id) {
                return Err(SnapshotCaptureError::IdentityConflict(format!(
                    "snapshot ID {:?} is already occupied",
                    request.snapshot_id
                )));
            }
            let Some(head) = state
                .runtime_heads
                .get(&context.runtime)
                .filter(|head| !head.accepted)
            else {
                return Ok(());
            };
            let record = &state.records[&head.snapshot_id];
            CapturedGenerationAccepted {
                snapshot_id: head.snapshot_id.clone(),
                capture_correlation_id: record.capture_correlation_id.clone().ok_or_else(|| {
                    SnapshotCaptureError::Protocol(
                        "Local capture has no correlation identity".into(),
                    )
                })?,
                capture_request_sha256: record.capture_request_sha256.clone().ok_or_else(|| {
                    SnapshotCaptureError::Protocol("Local capture has no request identity".into())
                })?,
            }
        };
        let reply = self
            .request(
                context,
                WireEnvelope {
                    header: header(context, "accept", request),
                    message: ControlRequest::AcceptCapturedGeneration(accepted.clone()),
                },
                request.deadline_unix_ms,
            )
            .await?;
        if !reply.fds.is_empty()
            || reply.envelope.message != ControlReply::CapturedGenerationAccepted(accepted)
        {
            return Err(SnapshotCaptureError::Protocol(
                "Capture ownership handoff was not acknowledged".into(),
            ));
        }
        if let Some(head) = self
            .state
            .lock()
            .await
            .runtime_heads
            .get_mut(&context.runtime)
        {
            head.accepted = true;
        }
        Ok(())
    }

    pub async fn lock_runtime(
        &self,
        runtime: &RuntimeIdentity,
    ) -> tokio::sync::OwnedMutexGuard<()> {
        let lock = self
            .runtime_locks
            .lock()
            .await
            .entry(runtime.clone())
            .or_insert_with(|| Arc::new(Mutex::new(())))
            .clone();
        lock.lock_owned().await
    }

    pub async fn can_finalize(&self, snapshot_id: &str) -> bool {
        let state = self.state.lock().await;
        let Some(record) = state.records.get(snapshot_id) else {
            return false;
        };
        let pending_parent = record.lineage.frozen_snapshots().any(|parent| {
            state.records.get(&parent.snapshot_id).is_some_and(|older| {
                older.runtime == record.runtime && older.local.is_some() && !older.finalized
            })
        });
        !pending_parent
    }

    /// Called only after CLH acknowledges the exact published snapshot.
    /// Does not acquire the capture lock: the next capture may be waiting on us.
    pub async fn mark_finalized(&self, runtime: &RuntimeIdentity, snapshot_id: &str) {
        let mut state = self.state.lock().await;
        if let Some(record) = state.records.get_mut(snapshot_id) {
            if record.runtime == *runtime {
                record.finalized = true;
            }
        }
        if let Some(head) = state.runtime_heads.get_mut(runtime) {
            if head.snapshot_id == snapshot_id {
                head.accepted = true;
            }
        }
    }

    /// Publication after source exit completes lineage bookkeeping without
    /// promoting or accepting a head belonging to a later same-ID runtime.
    pub async fn mark_stopped_source_published(&self, snapshot_id: &str) {
        if let Some(record) = self.state.lock().await.records.get_mut(snapshot_id) {
            record.finalized = true;
        }
    }

    pub async fn requires_source_reference(&self, snapshot_id: &str) -> bool {
        self.state
            .lock()
            .await
            .records
            .get(snapshot_id)
            .is_some_and(|record| {
                record.local.is_some() || record.pin.is_some() || record.capture_result_unknown
            })
    }

    pub async fn status(&self, snapshot_id: &str) -> Option<CaptureStatus> {
        self.state
            .lock()
            .await
            .records
            .get(snapshot_id)
            .map(|record| CaptureStatus {
                has_frozen_pin: record.pin.is_some(),
                local_ready: record.local.is_some(),
                capture_result_unknown: record.capture_result_unknown,
                terminal_error: record.terminal_error.clone(),
                pause_duration_ns: record.pause_duration_ns,
                capture_correlation_id: record.capture_correlation_id.clone(),
                capture_request_sha256: record.capture_request_sha256.clone(),
            })
    }

    pub async fn release_runtime(
        &self,
        runtime: &RuntimeIdentity,
    ) -> Result<usize, SnapshotCaptureError> {
        let key = runtime.clone();
        let runtime_lock = {
            let mut locks = self.runtime_locks.lock().await;
            Arc::clone(
                locks
                    .entry(key.clone())
                    .or_insert_with(|| Arc::new(Mutex::new(()))),
            )
        };
        let _runtime_guard = runtime_lock.lock().await;
        let (locals, pin) = {
            let mut state = self.state.lock().await;
            let head = state.runtime_heads.remove(&key);
            let pin = state
                .active_runtime_capture
                .remove(&key)
                .and_then(|snapshot_id| state.records.get_mut(&snapshot_id))
                .and_then(|record| {
                    record
                        .terminal_error
                        .get_or_insert_with(|| "runtime stopped before capture completed".into());
                    record.capture_result_unknown = false;
                    record.pin.take()
                });
            self.refresh_capture_metrics(&state);
            (
                head.map_or_else(Vec::new, |head| {
                    head.lineage.frozen_snapshots().cloned().collect()
                }),
                pin,
            )
        };
        drop(pin);
        let mut released = 0;
        let mut seen = std::collections::HashSet::new();
        for local in locals {
            let identity = Arc::as_ptr(&local) as usize;
            if seen.insert(identity) {
                released += 1;
            }
        }
        Ok(released)
    }

    /// Drop the capture-service alias after the durable snapshot has been
    /// invalidated.  Upload and control services own separate aliases; leaving
    /// this record behind keeps the frozen memory/block descriptors alive even
    /// after every runtime has stopped and the snapshot catalog row is deleted.
    pub async fn invalidate_snapshot(&self, snapshot_id: &str) -> bool {
        let removed = {
            let mut state = self.state.lock().await;
            let removed = state.records.remove(snapshot_id);
            if let Some(record) = removed.as_ref() {
                if state
                    .active_runtime_capture
                    .get(&record.runtime)
                    .is_some_and(|active| active == snapshot_id)
                {
                    state.active_runtime_capture.remove(&record.runtime);
                }
            }
            self.refresh_capture_metrics(&state);
            removed
        };
        removed.is_some()
    }

    async fn capture_runtime_locked(
        &self,
        context: &RuntimeCaptureContext,
        request: CaptureRequest,
        instance: Arc<Snapshot>,
    ) -> Result<Arc<Snapshot>, SnapshotCaptureError> {
        let key = context.runtime.clone();
        {
            let mut state = self.state.lock().await;
            if state.records.contains_key(&request.snapshot_id) {
                return Err(SnapshotCaptureError::IdentityConflict(format!(
                    "snapshot ID {:?} is already occupied",
                    request.snapshot_id
                )));
            }
            if request.deadline_unix_ms <= now_unix_ms() {
                return Err(SnapshotCaptureError::Deadline(
                    "deadline expired before CaptureGeneration".into(),
                ));
            }
            if let Some(active) = state.active_runtime_capture.get(&key) {
                return Err(SnapshotCaptureError::IdentityConflict(format!(
                    "live runtime has unresolved snapshot capture {active:?}"
                )));
            }
            let lineage = match state.runtime_heads.get(&key) {
                Some(head) => head.lineage.clone(),
                None => context.lineage.clone(),
            };
            state.records.insert(
                request.snapshot_id.clone(),
                CaptureRecord {
                    instance,
                    finalized: false,
                    runtime: context.runtime.clone(),
                    lineage,
                    pin: None,
                    local: None,
                    terminal_error: None,
                    capture_result_unknown: false,
                    pause_duration_ns: None,
                    capture_correlation_id: None,
                    capture_request_sha256: None,
                },
            );
            state
                .active_runtime_capture
                .insert(key, request.snapshot_id.clone());
        }
        self.capture_and_adopt(context, &request).await
    }

    async fn capture_and_adopt(
        &self,
        context: &RuntimeCaptureContext,
        request: &CaptureRequest,
    ) -> Result<Arc<Snapshot>, SnapshotCaptureError> {
        let cut_request_received_mono_ns = host_monotonic_ns();
        let capture_header = header(context, "capture", request);
        let capture_message = ControlRequest::CaptureGeneration(CaptureGeneration {
            snapshot_id: request.snapshot_id.clone(),
            geometry: context.geometry.clone(),
            deadline_unix_ms: request.deadline_unix_ms,
        });
        let capture_request_sha256 = format!(
            "{:x}",
            Sha256::digest(
                serde_json::to_vec(&capture_message)
                    .map_err(|error| SnapshotCaptureError::Protocol(error.to_string()))?
            )
        );
        let capture = WireEnvelope {
            header: capture_header.clone(),
            message: capture_message,
        };
        self.set_capture_unknown(&request.snapshot_id, true).await?;
        let captured_reply = self
            .request(context, capture, request.deadline_unix_ms)
            .await?;
        let capture_reply_received_mono_ns = host_monotonic_ns();
        match &captured_reply.envelope.message {
            ControlReply::Error(error) => {
                let message = format!("CaptureGeneration failed: {}", error.message);
                self.mark_terminal(request, message.clone(), true).await;
                return if error.code == ErrorCode::DeadlineExceeded {
                    Err(SnapshotCaptureError::Deadline(message))
                } else {
                    Err(SnapshotCaptureError::Terminal(message))
                };
            }
            ControlReply::GenerationCaptured(captured)
                if captured.snapshot_id == request.snapshot_id => {}
            ControlReply::GenerationCaptured(_) => {
                return Err(SnapshotCaptureError::Protocol(
                    "VMM captured a different snapshot identity".into(),
                ));
            }
            _ => {
                return Err(SnapshotCaptureError::Protocol(
                    "CaptureGeneration received an unexpected reply".into(),
                ));
            }
        }

        let pause_duration_ns = match &captured_reply.envelope.message {
            ControlReply::GenerationCaptured(captured) => captured.pause_duration_ns,
            _ => unreachable!(),
        };
        let pin = acquire_captured_snapshot(captured_reply)?;
        let captured_snapshot_pinned_mono_ns = host_monotonic_ns();
        let (lineage, observe_pause) = {
            let mut state = self.state.lock().await;
            let record = state.records.get_mut(&request.snapshot_id).ok_or_else(|| {
                SnapshotCaptureError::IdentityConflict(
                    "capture record disappeared after CaptureGeneration".into(),
                )
            })?;
            record.capture_result_unknown = false;
            let observe_pause = record.pause_duration_ns.is_none();
            record.pause_duration_ns.get_or_insert(pause_duration_ns);
            record.capture_correlation_id = Some(capture_header.correlation_id);
            record.capture_request_sha256 = Some(capture_request_sha256);
            (record.lineage.clone(), observe_pause)
        };
        if observe_pause {
            if let Some(metrics) = &self.metrics {
                metrics.observe_pause_ns(pause_duration_ns);
            }
        }
        let adopted = self
            .adopt_pin(
                &request.snapshot_id,
                pin,
                lineage,
                context.rocklet_artifact.clone(),
            )
            .await;
        let local_ready_mono_ns = host_monotonic_ns();
        if std::env::var_os("ROCK_PERF_TRACE_STAGES").as_deref() == Some(std::ffi::OsStr::new("1"))
        {
            let (memory_data_units, memory_data_bytes, block_data_units, block_data_bytes) =
                adopted.as_ref().map_or((0, 0, 0, 0), |local| {
                    let memory = local.frozen_layers.get(&SnapshotLayerKind::Memory);
                    let block = local.frozen_layers.get(&SnapshotLayerKind::Block);
                    let memory_units = memory.map_or(0, |layer| layer.data_units());
                    let block_units = block.map_or(0, |layer| layer.data_units());
                    (
                        memory_units,
                        memory.map_or(0, |layer| {
                            memory_units.saturating_mul(layer.unit_size_bytes)
                        }),
                        block_units,
                        block.map_or(0, |layer| block_units.saturating_mul(layer.unit_size_bytes)),
                    )
                });
            eprintln!(
                "ROCK_SNAPSHOT_CUT_TIMING snapshot_id={} cut_request_received_mono_ns={} capture_reply_received_mono_ns={} immutable_generation_pinned_mono_ns={} local_ready_mono_ns={} memory_data_units={} memory_data_bytes={} block_data_units={} block_data_bytes={} success={}",
                request.snapshot_id,
                cut_request_received_mono_ns,
                capture_reply_received_mono_ns,
                captured_snapshot_pinned_mono_ns,
                local_ready_mono_ns,
                memory_data_units,
                memory_data_bytes,
                block_data_units,
                block_data_bytes,
                u8::from(adopted.is_ok())
            );
        }
        adopted
    }

    async fn adopt_pin(
        &self,
        snapshot_id: &str,
        pin: CapturedSnapshotPin,
        lineage: SnapshotLineage,
        rocklet_artifact: RockletArtifactCompatibility,
    ) -> Result<Arc<Snapshot>, SnapshotCaptureError> {
        let instance = self
            .state
            .lock()
            .await
            .records
            .get(snapshot_id)
            .map(|record| Arc::clone(&record.instance))
            .ok_or_else(|| {
                SnapshotCaptureError::IdentityConflict(
                    "capture instance disappeared before adoption".into(),
                )
            })?;
        match instance.adopt_capture(pin, lineage, rocklet_artifact) {
            Ok(local) => {
                let mut state = self.state.lock().await;
                let record = state.records.get_mut(snapshot_id).ok_or_else(|| {
                    SnapshotCaptureError::IdentityConflict(
                        "capture record disappeared during snapshot adoption".into(),
                    )
                })?;
                record.local = Some(Arc::clone(&local));
                self.advance_runtime_head(&mut state, snapshot_id)?;
                Ok(local)
            }
            Err(failure) => {
                let message = failure.error.to_string();
                let pin = failure.into_pin();
                if let Some(record) = self.state.lock().await.records.get_mut(snapshot_id) {
                    record.pin = Some(pin);
                }
                Err(SnapshotCaptureError::Conversion(message))
            }
        }
    }

    fn advance_runtime_head(
        &self,
        state: &mut CaptureTransactionState,
        snapshot_id: &str,
    ) -> Result<(), SnapshotCaptureError> {
        let record = state.records.get(snapshot_id).ok_or_else(|| {
            SnapshotCaptureError::IdentityConflict("capture record is missing".into())
        })?;
        let local = record.local.as_ref().ok_or_else(|| {
            SnapshotCaptureError::Protocol("cannot advance without LOCAL_READY owner".into())
        })?;
        let key = record.runtime.clone();
        if let Some(head) = state.runtime_heads.get(&key) {
            if head.snapshot_id == snapshot_id {
                return Ok(());
            }
            if record
                .lineage
                .frozen_snapshots()
                .last()
                .map(|parent| parent.snapshot_id.as_str())
                != Some(head.snapshot_id.as_str())
            {
                return Err(SnapshotCaptureError::IdentityConflict(
                    "snapshot capture no longer extends the live COW ancestry".into(),
                ));
            }
        }
        state.runtime_heads.insert(
            key.clone(),
            RuntimeHead {
                snapshot_id: snapshot_id.into(),
                lineage: record.lineage.with_frozen(Arc::clone(local)),
                accepted: false,
            },
        );
        state.active_runtime_capture.remove(&key);
        self.refresh_capture_metrics(state);
        Ok(())
    }

    fn refresh_capture_metrics(&self, state: &CaptureTransactionState) {
        let Some(metrics) = &self.metrics else {
            return;
        };
        let mut seen = std::collections::HashSet::new();
        let mut dirty = [0_u64; 2];
        let mut depth = [0_u64; 2];
        for head in state.runtime_heads.values() {
            for kind in [SnapshotLayerKind::Memory, SnapshotLayerKind::Block] {
                let index = match kind {
                    SnapshotLayerKind::Memory => 0,
                    SnapshotLayerKind::Block => 1,
                };
                depth[index] = depth[index].max(head.lineage.depth(kind));
            }
            for local in head.lineage.frozen_snapshots() {
                if !seen.insert(Arc::as_ptr(local) as usize) {
                    continue;
                }
                for (kind, layer) in &local.frozen_layers {
                    let index = match kind {
                        SnapshotLayerKind::Memory => 0,
                        SnapshotLayerKind::Block => 1,
                    };
                    dirty[index] = dirty[index].saturating_add(layer.data_units());
                }
            }
        }
        metrics.set_dirty_pages(DataKind::Memory, dirty[0]);
        metrics.set_dirty_pages(DataKind::Block, dirty[1]);
        metrics.set_chain_depth(DataKind::Memory, depth[0]);
        metrics.set_chain_depth(DataKind::Block, depth[1]);
    }

    async fn request(
        &self,
        context: &RuntimeCaptureContext,
        envelope: WireEnvelope<ControlRequest>,
        deadline_unix_ms: u64,
    ) -> Result<ReceivedVmmMessage<ControlReply>, SnapshotCaptureError> {
        let remaining = deadline_unix_ms.saturating_sub(now_unix_ms());
        if remaining == 0 {
            return Err(SnapshotCaptureError::Deadline(
                "capture control deadline expired before submission".into(),
            ));
        }
        let timeout = self.request_timeout.min(Duration::from_millis(remaining));
        match tokio::time::timeout(
            timeout,
            context.control_client.request(envelope.clone(), Vec::new()),
        )
        .await
        {
            Ok(Ok(reply)) => Ok(reply),
            Ok(Err(error)) => Err(SnapshotCaptureError::ResultUnknown(format!(
                "CaptureGeneration {:?} transport failed; do not resubmit: {error}",
                envelope.header.correlation_id
            ))),
            Err(_) => Err(SnapshotCaptureError::ResultUnknown(format!(
                "CaptureGeneration {:?} timed out; do not resubmit",
                envelope.header.correlation_id
            ))),
        }
    }

    async fn set_capture_unknown(
        &self,
        snapshot_id: &str,
        value: bool,
    ) -> Result<(), SnapshotCaptureError> {
        let mut state = self.state.lock().await;
        let record = state.records.get_mut(snapshot_id).ok_or_else(|| {
            SnapshotCaptureError::IdentityConflict("capture record is missing".into())
        })?;
        record.capture_result_unknown = value;
        Ok(())
    }

    async fn mark_terminal(&self, request: &CaptureRequest, message: String, release: bool) {
        let mut state = self.state.lock().await;
        let key = state
            .records
            .get(&request.snapshot_id)
            .map(|record| record.runtime.clone());
        if let Some(record) = state.records.get_mut(&request.snapshot_id) {
            record.terminal_error = Some(message);
            record.capture_result_unknown = false;
        }
        if release {
            if let Some(key) = key {
                state.active_runtime_capture.remove(&key);
            }
        }
    }
}

fn validate_context(context: &RuntimeCaptureContext) -> Result<(), SnapshotCaptureError> {
    if context.geometry.len() != 2
        || !context
            .geometry
            .iter()
            .any(|geometry| geometry.kind == crate::vm::runtime::protocol::LayerKind::Memory)
        || !context
            .geometry
            .iter()
            .any(|geometry| geometry.kind == crate::vm::runtime::protocol::LayerKind::Block)
        || context
            .geometry
            .iter()
            .any(|geometry| geometry.logical_size == 0 || geometry.alignment == 0)
    {
        return Err(SnapshotCaptureError::Invalid(
            "capture requires one valid memory and one valid block geometry".into(),
        ));
    }
    Ok(())
}

fn validate_request(request: &CaptureRequest) -> Result<(), SnapshotCaptureError> {
    if request.snapshot_id.is_empty() || request.snapshot_id.len() > 256 {
        return Err(SnapshotCaptureError::Invalid(
            "snapshot identity is invalid".into(),
        ));
    }
    Ok(())
}

fn header(
    context: &RuntimeCaptureContext,
    operation: &str,
    request: &CaptureRequest,
) -> MessageHeader {
    let identity = format!(
        "{operation}\0{}\0{}",
        context.runtime.sandbox_id, request.snapshot_id
    );
    MessageHeader {
        protocol_version: VMM_PROTOCOL_VERSION,
        channel: Channel::RuntimeControl,
        correlation_id: format!("{operation}-{:x}", Sha256::digest(identity.as_bytes())),
    }
}

fn now_unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .try_into()
        .unwrap_or(u64::MAX)
}

fn host_monotonic_ns() -> u64 {
    let mut value = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // SAFETY: `value` is a valid writable timespec. ROCK capture only runs on
    // Linux, where CLOCK_MONOTONIC is the same host-wide clock used by CLH.
    if unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut value) } != 0 {
        return 0;
    }
    u64::try_from(value.tv_sec)
        .ok()
        .and_then(|seconds| seconds.checked_mul(1_000_000_000))
        .and_then(|seconds| {
            u64::try_from(value.tv_nsec)
                .ok()
                .and_then(|nanos| seconds.checked_add(nanos))
        })
        .unwrap_or(0)
}

fn acquire_captured_snapshot(
    received: ReceivedVmmMessage<ControlReply>,
) -> Result<CapturedSnapshotPin, FrozenLayerError> {
    let (envelope, fds) = received.into_parts();
    if !matches!(&envelope.message, ControlReply::GenerationCaptured(_)) {
        return Err(FrozenLayerError::Invalid(
            "Only GenerationCaptured can create a frozen pin".into(),
        ));
    }
    envelope
        .message
        .validate()
        .map_err(FrozenLayerError::Invalid)?;
    let ControlReply::GenerationCaptured(captured) = envelope.message else {
        unreachable!("reply kind was checked before validating it")
    };
    acquire_captured_snapshot_parts(captured, fds)
}

fn acquire_captured_snapshot_parts(
    mut captured: GenerationCaptured,
    fds: Vec<OwnedFd>,
) -> Result<CapturedSnapshotPin, FrozenLayerError> {
    let mut slots = fds.into_iter().map(Some).collect::<Vec<_>>();
    let mut layers = Vec::with_capacity(captured.frozen_layers.len());
    for descriptor in captured.frozen_layers {
        let ownership = captured
            .ownership_maps
            .iter_mut()
            .find(|ownership| ownership.kind == descriptor.kind)
            .ok_or_else(|| {
                FrozenLayerError::Invalid(
                    "Frozen layer is missing its packed ownership payload".into(),
                )
            })?;
        let backing = claim_fd(&mut slots, descriptor.fd_index)?;
        layers.push(FrozenLayerPin::new(
            FrozenLayerMetadata {
                kind: snapshot_kind(descriptor.kind),
                layer_id: descriptor.layer_id,
                provisional_content_id: descriptor.provisional_content_id,
                logical_size: descriptor.logical_size,
                alignment: descriptor.alignment,
            },
            PackedOwnershipMap {
                kind: snapshot_kind(ownership.kind),
                snapshot_id: std::mem::take(&mut ownership.snapshot_id),
                format_version: ownership.format_version,
                logical_size: ownership.logical_size,
                granularity: ownership.granularity,
                packed_states: std::mem::take(&mut ownership.packed_states),
            },
            backing,
            false,
        ));
    }
    let vm_fd = claim_fd(&mut slots, captured.vm_state.fd_index)?;
    if slots.iter().any(Option::is_some) {
        return Err(FrozenLayerError::Invalid(
            "GenerationCaptured contains an unclaimed FD".into(),
        ));
    }
    CapturedSnapshotPin::new(
        captured.snapshot_id,
        layers,
        VmStatePin::new(
            VmStateMetadata {
                format_version: captured.vm_state.format_version,
                content_id: captured.vm_state.content_id,
                size: captured.vm_state.size,
                alignment: captured.vm_state.alignment,
            },
            vm_fd,
        ),
    )
}

fn claim_fd(slots: &mut [Option<OwnedFd>], index: u32) -> Result<OwnedFd, FrozenLayerError> {
    slots
        .get_mut(index as usize)
        .and_then(Option::take)
        .ok_or_else(|| FrozenLayerError::Invalid("FD index is absent or already claimed".into()))
}

fn snapshot_kind(kind: ProtocolLayerKind) -> SnapshotLayerKind {
    match kind {
        ProtocolLayerKind::Memory => SnapshotLayerKind::Memory,
        ProtocolLayerKind::Block => SnapshotLayerKind::Block,
    }
}

#[cfg(test)]
mod snapshot_creation_tests {
    use super::*;

    fn service() -> VmService {
        let repository = VmRepository::new(
            None,
            Vec::new(),
            Duration::from_secs(30),
            Duration::from_secs(1),
            1,
            Duration::from_secs(1),
        )
        .unwrap();
        VmService::new(
            repository,
            Arc::new(SnapshotCaptureService::new(Duration::from_secs(1)).unwrap()),
            Arc::new(|_| panic!("duplicate snapshot must not launch children")),
        )
    }

    #[tokio::test]
    async fn occupied_snapshot_is_rejected_before_source_lookup() {
        let service = service();
        service.begin_job("occupied", "source").await.unwrap();
        for source in ["source", "another-source"] {
            let result = service
                .snapshot_sandbox(
                    source,
                    CaptureSnapshot {
                        snapshot_id: "occupied".into(),
                    },
                    u64::MAX,
                )
                .await;
            assert!(matches!(
                result,
                Err(SnapshotControlError::IdentityConflict(_))
            ));
        }
        assert_eq!(service.state.lock().await.jobs.len(), 1);
        assert!(service.capture.status("occupied").await.is_none());
    }

    #[tokio::test]
    async fn fork_cannot_reuse_an_occupied_snapshot_id() {
        let service = service();
        service.begin_job("occupied", "source").await.unwrap();
        let result = service
            .fork_sandbox(
                "source",
                ForkSnapshot {
                    snapshot: CaptureSnapshot {
                        snapshot_id: "occupied".into(),
                    },
                    children: vec![RuntimeLaunchPlan {
                        sandbox_id: "child".into(),
                        resource_spec: serde_json::json!({}),
                    }],
                },
                u64::MAX,
            )
            .await;
        assert!(matches!(
            result,
            Err(SnapshotControlError::IdentityConflict(_))
        ));
        assert!(service.capture.status("occupied").await.is_none());
    }

    #[tokio::test]
    async fn catalog_occupancy_prevents_a_new_capture_job() {
        let service = service();
        let occupied: Arc<dyn SnapshotInstance> = service
            .catalog
            .reserve_raw("occupied", crate::snapshot::SnapshotKind::Full)
            .unwrap();
        let result = service.begin_job("occupied", "source").await;
        assert!(matches!(
            result,
            Err(SnapshotControlError::IdentityConflict(_))
        ));
        assert!(service.state.lock().await.jobs.is_empty());
        assert!(Arc::ptr_eq(
            &service.catalog.get("occupied").unwrap(),
            &occupied
        ));
    }

    #[tokio::test]
    async fn base_image_cannot_mutate_an_existing_snapshot_identity() {
        for has_job in [false, true] {
            let service = service();
            if has_job {
                service
                    .begin_job("occupied", "original-source")
                    .await
                    .unwrap();
            }
            let original = service.catalog.get("occupied").unwrap_or_else(|| {
                service
                    .catalog
                    .reserve_raw("occupied", crate::snapshot::SnapshotKind::Full)
                    .unwrap()
            });
            let original_state = original.state();
            let request = BuildImage {
                snapshot_id: "occupied".into(),
                oci_ref: "registry.example/rootfs:tag".into(),
                oci_digest: format!("sha256:{}", "a".repeat(64)),
                platform: "linux/amd64".into(),
                base_image_builder_version: "builder-v1".into(),
                base_image_build_key: format!("sha256:{}", "b".repeat(64)),
            };
            request.validate().unwrap();
            // No builder is configured: reaching builder resolution would
            // return InvalidState instead of this identity conflict.
            let result = service.create_base_image(request).await;
            assert!(matches!(
                result,
                Err(SnapshotControlError::IdentityConflict(_))
            ));
            let retained = service.catalog.get("occupied").unwrap();
            assert!(Arc::ptr_eq(&original, &retained));
            assert_eq!(retained.state(), original_state);
            let state = service.state.lock().await;
            if has_job {
                assert_eq!(state.jobs["occupied"].source_sandbox_id, "original-source");
                assert!(state.jobs["occupied"].last_error.is_none());
            } else {
                assert!(state.jobs.is_empty());
            }
        }
    }

    #[tokio::test]
    async fn aborting_a_delete_fence_requires_an_existing_fence() {
        let service = service();
        service.state.lock().await.delete_fences.insert(
            "snapshot".into(),
            DeleteFence {
                nonce: "fence".into(),
                expires_at: Instant::now() + Duration::from_secs(60),
            },
        );
        let request = AbortDeleteFence {
            fence_nonce: "fence".into(),
        };
        let first = service
            .abort_delete_fence("snapshot", &request)
            .await
            .unwrap();
        assert_eq!(first.state, "FENCE_RELEASED");
        assert!(matches!(
            service.abort_delete_fence("snapshot", &request).await,
            Err(SnapshotControlError::InvalidState(message))
                if message == "snapshot has no delete fence"
        ));
    }

    #[tokio::test]
    async fn invalidation_of_an_already_deleted_snapshot_does_not_replay_success() {
        let service = service();
        service
            .state
            .lock()
            .await
            .invalidated_snapshots
            .insert("deleted".into());
        for nonce in ["original-nonce", "another-nonce"] {
            let result = service
                .invalidate_snapshot(
                    "deleted",
                    &InvalidateSnapshot {
                        fence_nonce: nonce.into(),
                    },
                )
                .await;
            assert!(matches!(result, Err(SnapshotControlError::NotFound(_))));
        }
    }

    struct NoCaptureCalls;

    #[async_trait::async_trait]
    impl CaptureControlClient for NoCaptureCalls {
        async fn request(
            &self,
            _request: WireEnvelope<ControlRequest>,
            _fds: Vec<OwnedFd>,
        ) -> Result<ReceivedVmmMessage<ControlReply>, VmmClientError> {
            panic!("an occupied snapshot must not submit another CLH request");
        }
    }

    #[tokio::test]
    async fn capture_records_reject_duplicates_instead_of_replaying_errors() {
        let service = SnapshotCaptureService::new(Duration::from_secs(1)).unwrap();
        let context = RuntimeCaptureContext::from_remote_chains(
            "base".into(),
            RuntimeIdentity::new("source"),
            Arc::new(NoCaptureCalls),
            [ProtocolLayerKind::Memory, ProtocolLayerKind::Block]
                .into_iter()
                .map(|kind| LayerGeometry {
                    kind,
                    logical_size: 4096,
                    alignment: 4096,
                })
                .collect(),
            HashMap::new(),
            RockletArtifactCompatibility {
                identity: "rocklet".into(),
                version: "test".into(),
                raw_sha256: format!("sha256:{}", "0".repeat(64)),
                logical_bytes: 1,
                guest_port: 1234,
            },
        );
        for (terminal_error, capture_result_unknown) in [
            (None, false),
            (Some("stored failure".into()), false),
            (None, true),
        ] {
            service.state.lock().await.records.insert(
                "occupied".into(),
                CaptureRecord {
                    instance: Snapshot::creating("occupied", crate::snapshot::SnapshotKind::Full),
                    finalized: false,
                    runtime: context.runtime.clone(),
                    lineage: context.lineage.clone(),
                    pin: None,
                    local: None,
                    terminal_error,
                    capture_result_unknown,
                    pause_duration_ns: None,
                    capture_correlation_id: None,
                    capture_request_sha256: None,
                },
            );
            let result = service
                .capture(
                    &context,
                    CaptureRequest {
                        snapshot_id: "occupied".into(),
                        deadline_unix_ms: u64::MAX,
                    },
                    Snapshot::creating("occupied", crate::snapshot::SnapshotKind::Full),
                )
                .await;
            assert!(matches!(
                result,
                Err(SnapshotCaptureError::IdentityConflict(_))
            ));
        }
    }
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    use std::fs::{File, OpenOptions};
    use std::os::fd::AsRawFd;
    use std::os::unix::fs::{FileExt, MetadataExt};

    use crate::snapshot::metadata::sha256_identity;
    use crate::snapshot::repository::frozen_layer::BLOCK_OWNERSHIP_UNIT;
    use crate::vm::protocol::{FdAccess, FdRole, FdRoleDescriptor, WireEnvelope};
    use crate::vm::runtime::prepare::current_header;
    use crate::vm::runtime::protocol::{
        FrozenLayerDescriptor, OwnershipMapDescriptor, VmStateDescriptor,
    };

    fn temporary_file() -> File {
        let path = std::env::temp_dir().join(format!("capture-adapter-{}", uuid::Uuid::new_v4()));
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
            .unwrap();
        std::fs::remove_file(path).unwrap();
        file
    }

    fn reply() -> (ReceivedVmmMessage<ControlReply>, (u64, u64)) {
        let block = temporary_file();
        block.set_len(2 * BLOCK_OWNERSHIP_UNIT).unwrap();
        block.write_all_at(&[0x4d; 32], 0).unwrap();
        let metadata = block.metadata().unwrap();
        let identity = (metadata.dev(), metadata.ino());
        let state = temporary_file();
        let state_bytes = vec![0x62; 4096];
        state.write_all_at(&state_bytes, 0).unwrap();
        let state_readonly = File::open(format!("/proc/self/fd/{}", state.as_raw_fd())).unwrap();
        let block_content_id = sha256_identity(b"native-captured-block");
        let state_content_id = sha256_identity(&state_bytes);
        let captured = GenerationCaptured {
            snapshot_id: "adapter-captured".into(),
            pause_duration_ns: 1234,
            frozen_layers: vec![FrozenLayerDescriptor {
                kind: ProtocolLayerKind::Block,
                layer_id: "native-block".into(),
                provisional_content_id: block_content_id.clone(),
                logical_size: 2 * BLOCK_OWNERSHIP_UNIT,
                alignment: BLOCK_OWNERSHIP_UNIT,
                fd_index: 0,
            }],
            vm_state: VmStateDescriptor {
                format_version: 1,
                content_id: state_content_id.clone(),
                size: 4096,
                alignment: 4096,
                fd_index: 1,
            },
            ownership_maps: vec![OwnershipMapDescriptor {
                kind: ProtocolLayerKind::Block,
                snapshot_id: "adapter-captured".into(),
                format_version: 1,
                logical_size: 2 * BLOCK_OWNERSHIP_UNIT,
                granularity: BLOCK_OWNERSHIP_UNIT,
                packed_states: vec![0b1001],
            }],
            fd_roles: vec![
                FdRoleDescriptor {
                    role: FdRole::BlockRoLayer,
                    access: FdAccess::ReadWrite,
                    content_id: block_content_id,
                    size: 2 * BLOCK_OWNERSHIP_UNIT,
                    alignment: BLOCK_OWNERSHIP_UNIT,
                },
                FdRoleDescriptor {
                    role: FdRole::VmState,
                    access: FdAccess::ReadOnly,
                    content_id: state_content_id,
                    size: 4096,
                    alignment: 4096,
                },
            ],
        };
        (
            ReceivedVmmMessage {
                envelope: WireEnvelope {
                    header: current_header("capture-adapter-test".into()),
                    message: ControlReply::GenerationCaptured(captured),
                },
                fds: vec![block.into(), state_readonly.into()],
            },
            identity,
        )
    }

    #[test]
    fn capture_adapter_preserves_native_inode_and_packed_coverage() {
        let (reply, identity) = reply();
        let captured_snapshot = acquire_captured_snapshot(reply).unwrap();
        assert_eq!(captured_snapshot.metadata.snapshot_id, "adapter-captured");
        let layer = captured_snapshot.layer(SnapshotLayerKind::Block).unwrap();
        assert_eq!(layer.ownership.packed_states, [0b1001]);
        assert_eq!(layer.read_data(0, 32).unwrap(), [0x4d; 32]);
        let metadata = File::from(layer.open_readonly().unwrap())
            .metadata()
            .unwrap();
        assert_eq!((metadata.dev(), metadata.ino()), identity);
        assert_eq!(
            captured_snapshot.vm_state.read_verified().unwrap(),
            vec![0x62; 4096]
        );
    }

    #[test]
    fn capture_adapter_rejects_aliasing_fd_roles_before_snapshot_adoption() {
        let (mut reply, _) = reply();
        let ControlReply::GenerationCaptured(captured) = &mut reply.envelope.message else {
            unreachable!()
        };
        captured.vm_state.fd_index = 0;
        assert!(acquire_captured_snapshot(reply).is_err());
    }

    #[test]
    fn capture_adapter_keeps_native_coverage_validation() {
        let (mut reply, _) = reply();
        let ControlReply::GenerationCaptured(captured) = &mut reply.envelope.message else {
            unreachable!()
        };
        // State 3 is invalid even when geometry and wire descriptors match.
        captured.ownership_maps[0].packed_states[0] = 0b1011;
        assert!(acquire_captured_snapshot(reply).is_err());
    }

    struct RejectRecapture;

    #[async_trait::async_trait]
    impl CaptureControlClient for RejectRecapture {
        async fn request(
            &self,
            _request: WireEnvelope<ControlRequest>,
            _fds: Vec<OwnedFd>,
        ) -> Result<ReceivedVmmMessage<ControlReply>, VmmClientError> {
            panic!("an unresolved captured snapshot must not be submitted again")
        }
    }

    #[tokio::test]
    async fn failed_snapshot_adoption_retains_pin_and_prevents_another_capture() {
        let (reply, identity) = reply();
        // This valid capture contains only block DATA. A FULL Snapshot
        // cannot adopt it, but the capture has already happened and owns FDs.
        let pin = acquire_captured_snapshot(reply).unwrap();
        let service = SnapshotCaptureService::new(Duration::from_secs(1)).unwrap();
        let runtime = RuntimeIdentity::new("adoption-source");
        let snapshot_id = "adapter-captured";
        let artifact = RockletArtifactCompatibility {
            identity: "rocklet".into(),
            version: "test".into(),
            raw_sha256: format!("sha256:{}", "0".repeat(64)),
            logical_bytes: 1,
            guest_port: 1234,
        };
        {
            let mut state = service.state.lock().await;
            state.records.insert(
                snapshot_id.into(),
                CaptureRecord {
                    instance: Snapshot::creating(snapshot_id, crate::snapshot::SnapshotKind::Full),
                    finalized: false,
                    runtime: runtime.clone(),
                    lineage: SnapshotLineage::default(),
                    pin: None,
                    local: None,
                    terminal_error: None,
                    capture_result_unknown: false,
                    pause_duration_ns: Some(1234),
                    capture_correlation_id: Some("capture-original".into()),
                    capture_request_sha256: Some(sha256_identity(b"capture-original")),
                },
            );
            state
                .active_runtime_capture
                .insert(runtime.clone(), snapshot_id.into());
        }
        let result = service
            .adopt_pin(
                snapshot_id,
                pin,
                SnapshotLineage::default(),
                artifact.clone(),
            )
            .await;
        assert!(matches!(result, Err(SnapshotCaptureError::Conversion(_))));
        {
            let state = service.state.lock().await;
            let record = &state.records[snapshot_id];
            assert!(record.local.is_none());
            let retained = record
                .pin
                .as_ref()
                .expect("failed adoption must return the original pin");
            let file = File::from(
                retained
                    .layer(SnapshotLayerKind::Block)
                    .unwrap()
                    .open_readonly()
                    .unwrap(),
            );
            let metadata = file.metadata().unwrap();
            assert_eq!((metadata.dev(), metadata.ino()), identity);
            assert_eq!(retained.vm_state.read_verified().unwrap(), vec![0x62; 4096]);
            assert_eq!(
                record.capture_correlation_id.as_deref(),
                Some("capture-original")
            );
            assert_eq!(
                state
                    .active_runtime_capture
                    .get(&runtime)
                    .map(String::as_str),
                Some(snapshot_id)
            );
            assert!(!state.runtime_heads.contains_key(&runtime));
        }
        assert!(service.status(snapshot_id).await.unwrap().has_frozen_pin);
        let context = RuntimeCaptureContext::from_remote_chains(
            "base".into(),
            runtime,
            Arc::new(RejectRecapture),
            vec![
                LayerGeometry {
                    kind: ProtocolLayerKind::Memory,
                    logical_size: 4096,
                    alignment: 4096,
                },
                LayerGeometry {
                    kind: ProtocolLayerKind::Block,
                    logical_size: 2 * BLOCK_OWNERSHIP_UNIT,
                    alignment: BLOCK_OWNERSHIP_UNIT,
                },
            ],
            HashMap::new(),
            artifact,
        );
        let repeated = service
            .capture(
                &context,
                CaptureRequest {
                    snapshot_id: "another-capture".into(),
                    deadline_unix_ms: u64::MAX,
                },
                Snapshot::creating("another-capture", crate::snapshot::SnapshotKind::Full),
            )
            .await;
        assert!(
            matches!(&repeated, Err(SnapshotCaptureError::IdentityConflict(_))),
            "unexpected recapture result: {:?}",
            repeated.as_ref().err()
        );
        assert_eq!(service.state.lock().await.records.len(), 1);
    }
}

pub const MAX_CHILDREN: usize = 32;
pub const MAX_CONCURRENT_STARTS: usize = 8;

pub(crate) type RuntimeReleaseHook =
    Arc<dyn Fn(RuntimeIdentity) -> BoxFuture<Result<(), String>> + Send + Sync + 'static>;

#[derive(Clone, Debug, Error)]
pub enum RuntimeStartError {
    #[error("Invalid runtime start request: {0}")]
    Invalid(String),
    #[error("Runtime identity conflict: {0}")]
    IdentityConflict(String),
    #[error("VMM startup protocol failed: {0}")]
    Protocol(String),
    #[error("VMM startup operation failed: {0}")]
    Vmm(String),
    #[error("VMM finalization result is unknown; do not resubmit: {0}")]
    FinalizationUnknown(String),
    #[error("Runtime cleanup failed: {0}")]
    CleanupFailed(String),
    #[error("Runtime I/O failed: {0}")]
    Io(String),
    #[error("Runtime was not found: {0}")]
    NotFound(String),
    #[error("Invalid runtime state: {0}")]
    InvalidState(String),
}

impl From<VmmClientError> for RuntimeStartError {
    fn from(error: VmmClientError) -> Self {
        Self::Vmm(error.to_string())
    }
}

impl From<VmmLaunchError> for RuntimeStartError {
    fn from(error: VmmLaunchError) -> Self {
        Self::Vmm(error.to_string())
    }
}

impl From<VmRepositoryError> for RuntimeStartError {
    fn from(error: VmRepositoryError) -> Self {
        match error {
            VmRepositoryError::Invalid(message) => Self::Invalid(message),
            VmRepositoryError::NotFound(message) => Self::NotFound(message),
            VmRepositoryError::InvalidState(message) => Self::InvalidState(message),
            VmRepositoryError::IdentityConflict(message)
            | VmRepositoryError::AlreadyExists(message) => Self::IdentityConflict(message),
            error => Self::Vmm(error.to_string()),
        }
    }
}

impl From<VmmControllerError> for RuntimeStartError {
    fn from(error: VmmControllerError) -> Self {
        match error {
            VmmControllerError::Invalid(message) => Self::Invalid(message),
            VmmControllerError::Protocol(message) => Self::Protocol(message),
            VmmControllerError::Vmm(message) => Self::Vmm(message),
            VmmControllerError::Io(message) => Self::Io(message),
        }
    }
}

impl From<LocalSnapshotError> for RuntimeStartError {
    fn from(error: LocalSnapshotError) -> Self {
        Self::Invalid(error.to_string())
    }
}

// Immutable snapshot bindings owned by a running VMM session.
#[derive(Debug)]
pub(crate) struct LayerBacking {
    metadata: LayerBackingMetadata,
    coverage: LayerCoveragePayload,
    backing: Arc<OwnedFd>,
    shared_mapping: Option<Arc<OwnedFd>>,
    data_residency: DataResidency,
    dynamic_residency: Option<DynamicLayerResidency>,
}

#[derive(Debug)]
enum LayerCoveragePayload {
    Canonical {
        payload: Arc<RangeIndexPayload>,
        range_map: Arc<LayerRangeMap>,
    },
    Frozen(OwnershipMapDescriptor),
}

#[derive(Debug)]
struct DynamicLayerResidency {
    backing_id: String,
    registry: Arc<DataRangeResidencyRegistry>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct LayerBackingMetadata {
    format_version: u64,
    kind: SnapshotLayerKind,
    content_sha256: String,
    logical_bytes: u64,
    unit_size_bytes: u64,
}

impl From<&LayerRangeMap> for LayerBackingMetadata {
    fn from(index: &LayerRangeMap) -> Self {
        Self {
            format_version: index.format_version,
            kind: index.kind,
            content_sha256: index.content_sha256.clone(),
            logical_bytes: index.logical_bytes,
            unit_size_bytes: index.unit_size_bytes,
        }
    }
}

impl LayerBackingMetadata {
    fn from_frozen(layer: &FrozenLocalLayer) -> Self {
        Self {
            format_version: layer.ownership().format_version,
            kind: layer.kind,
            content_sha256: layer.provisional_content_id.clone(),
            logical_bytes: layer.logical_bytes,
            unit_size_bytes: layer.unit_size_bytes,
        }
    }
}

#[derive(Debug)]
struct RangeIndexPayload {
    size: u64,
    content_id: String,
    fd: OwnedFd,
}

/// Opaque, immutable canonical coverage compiled once for one verified remote
/// range-map. Runtime-specific capabilities (backing descriptors, grants,
/// authorization and residency) intentionally do not live here.
#[derive(Debug)]
pub(crate) struct CompiledCanonicalLayerCoverage {
    metadata: LayerBackingMetadata,
    payload: Arc<RangeIndexPayload>,
    range_map: Arc<LayerRangeMap>,
    cache_weight_bytes: usize,
}

impl CompiledCanonicalLayerCoverage {
    pub(crate) fn compile(index: LayerRangeMap) -> Result<Arc<Self>, RuntimeStartError> {
        index.validate().map_err(RuntimeStartError::Invalid)?;
        let metadata = LayerBackingMetadata::from(&index);
        let payload = RangeIndexPayload::compile(&index)?;
        let cache_weight_bytes = estimated_compiled_coverage_bytes(&index, payload.size);
        Ok(Arc::new(Self {
            metadata,
            payload,
            range_map: Arc::new(index),
            cache_weight_bytes,
        }))
    }

    pub(crate) fn cache_weight_bytes(&self) -> usize {
        self.cache_weight_bytes
    }

    pub(crate) const fn cache_fd_count(&self) -> usize {
        1
    }

    /// Return the verified immutable mapping shared by every runtime that
    /// attaches this compiled layer.  Runtime authorization is deliberately
    /// kept outside this table; callers may only use it behind a target-level
    /// capability owned by the current runtime.
    pub(crate) fn range_map(&self) -> Arc<LayerRangeMap> {
        Arc::clone(&self.range_map)
    }
}

fn estimated_compiled_coverage_bytes(index: &LayerRangeMap, payload_size: u64) -> usize {
    let payload_size = usize::try_from(payload_size).unwrap_or(usize::MAX);
    let mut heap_bytes = std::mem::size_of::<LayerRangeMap>()
        .saturating_add(index.content_sha256.len())
        .saturating_add(
            index
                .ranges
                .len()
                .saturating_mul(std::mem::size_of::<crate::snapshot::metadata::LayerRange>()),
        );
    for source in index
        .ranges
        .iter()
        .filter_map(|range| range.source.as_ref())
    {
        heap_bytes = heap_bytes
            .saturating_add(source.object_key.len())
            .saturating_add(source.object_sha256.len())
            .saturating_add(source.object_version.as_ref().map_or(0, String::len))
            .saturating_add(source.checksum_sha256.len());
    }
    heap_bytes
        .saturating_add(
            index
                .zfile
                .as_ref()
                .map_or(0, |index| index.retained_bytes()),
        )
        .saturating_add(
            index
                .registry
                .as_ref()
                .map_or(0, |rootfs| rootfs.retained_bytes()),
        )
        .saturating_add(payload_size)
}

impl RangeIndexPayload {
    fn compile(index: &LayerRangeMap) -> Result<Arc<Self>, RuntimeStartError> {
        let bytes = vmm_range_index(index)?;
        let content_id = sha_hex(&crate::snapshot::metadata::sha256_identity(&bytes));
        Self::new(&bytes, content_id)
    }

    fn new(bytes: &[u8], content_id: String) -> Result<Arc<Self>, RuntimeStartError> {
        validate_vmm_range_index_size(bytes.len())?;
        let size = u64::try_from(bytes.len())
            .map_err(|_| RuntimeStartError::Invalid("range-index size overflows u64".into()))?;
        let fd = sealed_payload_readonly_fd(bytes)?;
        Ok(Arc::new(Self {
            size,
            content_id,
            fd,
        }))
    }
}

#[derive(Debug)]
pub struct RuntimeLayerSeed {
    pub ordinal: u32,
    pub layer_id: String,
    target_id: Option<String>,
    backing: Arc<LayerBacking>,
    resources: Option<Arc<crate::snapshot::io::layer_resources::LayerResources>>,
}

impl RuntimeLayerSeed {
    pub fn new(
        ordinal: u32,
        layer_id: String,
        index: LayerRangeMap,
        backing: OwnedFd,
    ) -> Result<Self, RuntimeStartError> {
        Self::new_with_residency(
            ordinal,
            layer_id,
            index,
            backing,
            None,
            DataResidency::LocalComplete,
            None,
        )
    }

    pub fn new_remote_lazy(
        ordinal: u32,
        layer_id: String,
        index: LayerRangeMap,
        backing: OwnedFd,
        shared_mapping: Option<OwnedFd>,
        backing_id: String,
        residency_registry: Arc<DataRangeResidencyRegistry>,
    ) -> Result<Self, RuntimeStartError> {
        Self::new_with_residency(
            ordinal,
            layer_id,
            index,
            backing,
            shared_mapping,
            DataResidency::RemoteLazy,
            Some(DynamicLayerResidency {
                backing_id,
                registry: residency_registry,
            }),
        )
    }

    pub(crate) fn new_remote_lazy_precompiled(
        ordinal: u32,
        layer_id: String,
        coverage: Arc<CompiledCanonicalLayerCoverage>,
        resources: Arc<crate::snapshot::io::layer_resources::LayerResources>,
    ) -> Result<Self, RuntimeStartError> {
        let backing = resources.backing().ok_or_else(|| {
            RuntimeStartError::Invalid("remote layer has no managed backing lease".into())
        })?;
        if resources.reader().is_none() {
            return invalid_runtime("remote layer has no installed DATA reader");
        }
        let readonly = resources
            .readonly_file()
            .map_err(|error| RuntimeStartError::Invalid(error.to_string()))?;
        let shared_mapping = resources
            .memory_mapping_file()
            .map_err(|error| RuntimeStartError::Invalid(error.to_string()))?;
        let mut seed = Self::new_with_compiled_residency(
            ordinal,
            layer_id,
            coverage,
            readonly.into(),
            shared_mapping.map(Into::into),
            DataResidency::RemoteLazy,
            Some(DynamicLayerResidency {
                backing_id: backing.backing_id.clone(),
                registry: Arc::clone(&backing.downloads),
            }),
        )?;
        seed.resources = Some(resources);
        Ok(seed)
    }

    fn new_with_residency(
        ordinal: u32,
        layer_id: String,
        index: LayerRangeMap,
        backing: OwnedFd,
        shared_mapping: Option<OwnedFd>,
        data_residency: DataResidency,
        dynamic_residency: Option<DynamicLayerResidency>,
    ) -> Result<Self, RuntimeStartError> {
        let coverage = CompiledCanonicalLayerCoverage::compile(index)?;
        Self::new_with_compiled_residency(
            ordinal,
            layer_id,
            coverage,
            backing,
            shared_mapping,
            data_residency,
            dynamic_residency,
        )
    }

    fn new_with_compiled_residency(
        ordinal: u32,
        layer_id: String,
        coverage: Arc<CompiledCanonicalLayerCoverage>,
        backing: OwnedFd,
        shared_mapping: Option<OwnedFd>,
        data_residency: DataResidency,
        dynamic_residency: Option<DynamicLayerResidency>,
    ) -> Result<Self, RuntimeStartError> {
        if layer_id.is_empty() || layer_id.len() > 256 {
            return invalid_runtime("layer identity length must be 1..=256");
        }
        validate_readonly_backing(&backing, coverage.metadata.logical_bytes)?;
        validate_shared_mapping_backing(
            &backing,
            shared_mapping.as_ref(),
            coverage.metadata.kind,
            data_residency,
            coverage.metadata.logical_bytes,
        )?;
        if (data_residency == DataResidency::RemoteLazy) != dynamic_residency.is_some() {
            return invalid_runtime("remote-lazy backing requires a dynamic residency registry");
        }
        Ok(Self {
            ordinal,
            layer_id,
            target_id: None,
            resources: None,
            backing: Arc::new(LayerBacking {
                metadata: coverage.metadata.clone(),
                coverage: LayerCoveragePayload::Canonical {
                    payload: Arc::clone(&coverage.payload),
                    range_map: Arc::clone(&coverage.range_map),
                },
                backing: Arc::new(backing),
                shared_mapping: shared_mapping.map(Arc::new),
                data_residency,
                dynamic_residency,
            }),
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PreparedVmState {
    pub format_version: u64,
    pub payload: Vec<u8>,
}

/// Immutable anchor that lets a restored remote snapshot participate in the
/// local frozen-parent lineage used for subsequent captures and forks.
#[derive(Clone, Debug)]
pub struct PreparedSnapshotLineage {
    pub snapshot_id: String,
    pub spec_sha256: String,
    pub provenance: Option<SnapshotProvenance>,
    pub parent_chains: HashMap<SnapshotLayerKind, LayerChainIndex>,
    /// Object key and SHA-256 for each chain index object referenced by the
    /// published snapshot spec (memory, block).
    pub chain_identities: HashMap<SnapshotLayerKind, (String, String)>,
}

#[derive(Debug)]
pub struct PreparedSnapshotRuntime {
    snapshot_id: String,
    spec_sha256: String,
    layers: Vec<RuntimeLayerSeed>,
    parent_chains: HashMap<SnapshotLayerKind, LayerChainIndex>,
    vm_state: Option<PreparedVmState>,
    lineage: PreparedSnapshotLineage,
}

impl PreparedSnapshotRuntime {
    pub fn new(
        snapshot_id: String,
        spec_sha256: String,
        layers: Vec<RuntimeLayerSeed>,
        parent_chains: HashMap<SnapshotLayerKind, LayerChainIndex>,
        vm_state: Option<PreparedVmState>,
        lineage: PreparedSnapshotLineage,
    ) -> Result<Arc<Self>, RuntimeStartError> {
        if snapshot_id.is_empty() || layers.is_empty() {
            return invalid_runtime("prepared snapshot and layers must be non-empty");
        }
        if spec_sha256.len() != 64 || !spec_sha256.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return invalid_runtime("prepared snapshot spec checksum must be sha256 hex");
        }
        validate_seed_chains(&layers, &parent_chains)?;
        let has_memory = layers
            .iter()
            .any(|layer| layer.backing.metadata.kind == SnapshotLayerKind::Memory);
        let has_block = layers
            .iter()
            .any(|layer| layer.backing.metadata.kind == SnapshotLayerKind::Block);
        if !has_block || vm_state.is_some() != has_memory {
            return invalid_runtime(
                "prepared runtime must be BASE block-only or FULL memory+block+state",
            );
        }
        if vm_state
            .as_ref()
            .is_some_and(|state| state.format_version == 0 || state.payload.is_empty())
        {
            return invalid_runtime("prepared VM state format and payload must be non-empty");
        }
        if lineage.snapshot_id != snapshot_id || lineage.spec_sha256 != spec_sha256 {
            return invalid_runtime("prepared lineage identity must match the prepared runtime");
        }
        if !lineage
            .chain_identities
            .keys()
            .all(|kind| lineage.parent_chains.contains_key(kind))
        {
            return invalid_runtime("prepared lineage chain identities must match parent chains");
        }
        Ok(Arc::new(Self {
            snapshot_id,
            spec_sha256,
            layers,
            parent_chains,
            vm_state,
            lineage,
        }))
    }

    pub fn snapshot_id(&self) -> &str {
        &self.snapshot_id
    }

    pub fn spec_sha256(&self) -> &str {
        &self.spec_sha256
    }

    pub fn layer_count(&self) -> usize {
        self.layers.len()
    }

    pub fn lineage(&self) -> &PreparedSnapshotLineage {
        &self.lineage
    }
}

/// Weak, read-only view of a live runtime binding used by the versioned
/// P2P source. It cannot extend a VMM/backing lifetime and only exposes layers
/// whose complete immutable bytes are already local.
#[derive(Clone)]
pub(crate) struct WeakRuntimeBindingHandle(Weak<RuntimeBinding>);

impl std::fmt::Debug for WeakRuntimeBindingHandle {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("WeakRuntimeBindingHandle")
            .field("live", &self.0.strong_count().ne(&0))
            .finish()
    }
}

impl WeakRuntimeBindingHandle {
    pub(crate) fn has_local_complete_layer(
        &self,
        kind: SnapshotLayerKind,
        ordinal: u32,
        layer_id: &str,
        content_sha256: &str,
    ) -> bool {
        self.0.upgrade().is_some_and(|binding| {
            binding.layers.iter().any(|layer| {
                layer.backing.data_residency == DataResidency::LocalComplete
                    && layer.backing.metadata.kind == kind
                    && layer.ordinal == ordinal
                    && layer.layer_id == layer_id
                    && layer.backing.metadata.content_sha256 == content_sha256
                    && layer
                        .resources
                        .get()
                        .and_then(|resources| resources.backing())
                        .is_none_or(|backing| !backing.has_reclaimed_data())
            })
        })
    }

    pub(crate) fn read_local_complete_range(
        &self,
        kind: SnapshotLayerKind,
        ordinal: u32,
        layer_id: &str,
        content_sha256: &str,
        offset: u64,
        length: u64,
    ) -> Result<Option<Vec<u8>>, RuntimeStartError> {
        let Some(binding) = self.0.upgrade() else {
            return Ok(None);
        };
        let Some(layer) = binding.layers.iter().find(|layer| {
            layer.backing.data_residency == DataResidency::LocalComplete
                && layer.backing.metadata.kind == kind
                && layer.ordinal == ordinal
                && layer.layer_id == layer_id
                && layer.backing.metadata.content_sha256 == content_sha256
        }) else {
            return Ok(None);
        };
        let end = offset
            .checked_add(length)
            .ok_or_else(|| RuntimeStartError::Invalid("P2P backing range overflows".into()))?;
        if length == 0 || end > layer.backing.metadata.logical_bytes {
            return Err(RuntimeStartError::Invalid(
                "P2P backing range exceeds the immutable layer".into(),
            ));
        }
        let payload_len = usize::try_from(length).map_err(|_| {
            RuntimeStartError::Invalid("P2P backing range exceeds address space".into())
        })?;
        let mut payload = vec![0_u8; payload_len];
        // A VM depends only on its winning ranges. P2P may ask for a shadowed
        // range in the same inode, so it needs an independent read lease.
        let _pin = match layer
            .resources
            .get()
            .and_then(|resources| resources.backing())
        {
            Some(backing) => match backing.pin_native_reader() {
                Ok(pin) => Some(pin),
                Err(_) => return Ok(None),
            },
            None => None,
        };
        let file = File::from(duplicate_fd(&layer.backing.backing)?);
        file.read_exact_at(&mut payload, offset)
            .map_err(|error| RuntimeStartError::Io(error.to_string()))?;
        Ok(Some(payload))
    }
}

#[derive(Clone)]
pub struct LocalSnapshotRuntimeSource {
    pub snapshot: Arc<Snapshot>,
    pub spec_sha256: String,
    layer_backings: Arc<LocalLayerBackingCache>,
}

impl LocalSnapshotRuntimeSource {
    pub fn new(snapshot: Arc<Snapshot>, spec_sha256: String) -> Self {
        Self {
            snapshot,
            spec_sha256,
            layer_backings: Arc::new(LocalLayerBackingCache::default()),
        }
    }

    fn layer_backing(
        &self,
        snapshot: &Snapshot,
        kind: SnapshotLayerKind,
        layer: &FrozenLocalLayer,
    ) -> Result<Arc<LayerBacking>, RuntimeStartError> {
        self.layer_backings.get_or_create(
            LocalLayerBackingKey {
                snapshot_id: snapshot.snapshot_id.clone(),
                kind,
                layer_id: layer.layer_id.clone(),
                provisional_content_id: layer.provisional_content_id.clone(),
            },
            || {
                // These descriptors stay internal until dependency preparation
                // restores winning holes; exporting them earlier is forbidden.
                let handle = snapshot.backing_handle(kind)?;
                let backing = Arc::new(OwnedFd::from(
                    handle
                        .backing
                        .try_clone_readonly_file()
                        .map_err(|error| RuntimeStartError::Invalid(error.to_string()))?,
                ));
                validate_readonly_backing(&backing, layer.logical_bytes)?;
                Ok(LayerBacking {
                    metadata: LayerBackingMetadata::from_frozen(layer),
                    coverage: LayerCoveragePayload::Frozen(OwnershipMapDescriptor {
                        kind: protocol_kind(layer.ownership().kind),
                        snapshot_id: layer.ownership().snapshot_id.clone(),
                        format_version: layer.ownership().format_version,
                        logical_size: layer.ownership().logical_size,
                        granularity: layer.ownership().granularity,
                        packed_states: layer.ownership().packed_states.clone(),
                    }),
                    backing,
                    shared_mapping: match kind {
                        SnapshotLayerKind::Memory => Some(Arc::new(OwnedFd::from(
                            handle
                                .backing
                                .try_clone_memory_mapping_file()
                                .map_err(|error| RuntimeStartError::Invalid(error.to_string()))?,
                        ))),
                        SnapshotLayerKind::Block => None,
                    },
                    data_residency: DataResidency::LocalComplete,
                    dynamic_residency: None,
                })
            },
        )
    }

    fn remote_layer_backing(
        &self,
        source: &RemoteLayerSource,
    ) -> Result<Arc<LayerBacking>, RuntimeStartError> {
        self.layer_backings.get_or_create(
            LocalLayerBackingKey::remote(&self.snapshot.snapshot_id, source),
            || {
                // Rebuild only the CLH attachment metadata when the original
                // attachment lease is absent. DATA stays in the same inode.
                Ok(LayerBacking {
                    metadata: LayerBackingMetadata::from(source.range_map.as_ref()),
                    coverage: LayerCoveragePayload::Canonical {
                        payload: RangeIndexPayload::compile(&source.range_map)?,
                        range_map: source.range_map.clone(),
                    },
                    backing: source.backing.clone(),
                    shared_mapping: source.shared_mapping.clone(),
                    data_residency: match source.residency {
                        SourceResidency::Complete => DataResidency::LocalComplete,
                        SourceResidency::OnDemand => DataResidency::RemoteLazy,
                    },
                    dynamic_residency: source.dynamic_residency.as_ref().map(|handle| {
                        DynamicLayerResidency {
                            backing_id: handle.backing_id.clone(),
                            registry: handle.registry.clone(),
                        }
                    }),
                })
            },
        )
    }
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
struct LocalLayerBackingKey {
    snapshot_id: String,
    kind: SnapshotLayerKind,
    layer_id: String,
    provisional_content_id: String,
}

impl LocalLayerBackingKey {
    fn remote(snapshot_id: &str, source: &RemoteLayerSource) -> Self {
        Self {
            snapshot_id: snapshot_id.to_owned(),
            kind: source.range_map.kind,
            layer_id: source.layer_id.clone(),
            provisional_content_id: source.range_map.content_sha256.clone(),
        }
    }
}

#[derive(Debug, Default)]
struct LocalLayerBackingCache {
    backings: StdMutex<HashMap<LocalLayerBackingKey, Arc<LayerBacking>>>,
}

impl LocalLayerBackingCache {
    fn retain_remote(
        &self,
        snapshot_id: &str,
        source: &RemoteLayerSource,
        backing: Arc<LayerBacking>,
    ) -> Result<(), RuntimeStartError> {
        self.backings
            .lock()
            .map_err(|_| RuntimeStartError::Vmm("local layer-backing cache is poisoned".into()))?
            .insert(LocalLayerBackingKey::remote(snapshot_id, source), backing);
        Ok(())
    }

    fn get_or_create<F>(
        &self,
        key: LocalLayerBackingKey,
        create: F,
    ) -> Result<Arc<LayerBacking>, RuntimeStartError>
    where
        F: FnOnce() -> Result<LayerBacking, RuntimeStartError>,
    {
        let mut backings = self
            .backings
            .lock()
            .map_err(|_| RuntimeStartError::Vmm("local layer-backing cache is poisoned".into()))?;
        if let Some(backing) = backings.get(&key) {
            return Ok(backing.clone());
        }
        let backing = Arc::new(create()?);
        backings.insert(key, backing.clone());
        Ok(backing)
    }
}

/// An explicit local-snapshot lease owns one attachment cache, including all
/// frozen ancestors used by that snapshot. Runtime attachment never inserts a
/// lease here, so a released ancestor cannot be resurrected by a later fork.
#[derive(Debug, Default)]
struct LocalSnapshotBackingLeases {
    snapshots: StdMutex<HashMap<String, Arc<LocalLayerBackingCache>>>,
}

impl LocalSnapshotBackingLeases {
    fn retain_snapshot(
        &self,
        snapshot_id: &str,
        cache: Arc<LocalLayerBackingCache>,
    ) -> Result<(), RuntimeStartError> {
        self.snapshots
            .lock()
            .map_err(|_| RuntimeStartError::Vmm("snapshot attachment leases are poisoned".into()))?
            .insert(snapshot_id.to_owned(), cache);
        Ok(())
    }

    fn get_snapshot(
        &self,
        snapshot_id: &str,
    ) -> Result<Option<Arc<LocalLayerBackingCache>>, RuntimeStartError> {
        Ok(self
            .snapshots
            .lock()
            .map_err(|_| RuntimeStartError::Vmm("snapshot attachment leases are poisoned".into()))?
            .get(snapshot_id)
            .cloned())
    }

    fn release_snapshot(&self, snapshot_id: &str) {
        // In-flight users keep their independent Arc. Nothing can recreate the
        // removed lease except an explicit successful snapshot pin.
        let mut snapshots = self
            .snapshots
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        snapshots.remove(snapshot_id);
    }
}

#[derive(Clone)]
pub enum SnapshotRuntimeSource {
    Prepared(Arc<PreparedSnapshotRuntime>),
    Local(LocalSnapshotRuntimeSource),
}

#[derive(Clone, Debug)]
pub(crate) struct BindingLayer {
    pub(in crate::vm) target_id: String,
    /// Existing remote target whose immutable capability must be cloned when
    /// this layer is rebound into a local fork child.
    capability_source_target_id: Option<String>,
    pub(crate) ordinal: u32,
    pub(crate) layer_id: String,
    pub(crate) backing: Arc<LayerBacking>,
    /// One runtime's reader and backing lease; never stored in the shared FD cache.
    resources: OnceLock<Arc<LayerResources>>,
}

impl BindingLayer {
    fn snapshot_source(&self) -> Result<RemoteLayerSource, RuntimeStartError> {
        let LayerCoveragePayload::Canonical { range_map, .. } = &self.backing.coverage else {
            return invalid_runtime("remote ancestry must have canonical coverage");
        };
        Ok(RemoteLayerSource {
            ordinal: self.ordinal,
            layer_id: self.layer_id.clone(),
            range_map: range_map.clone(),
            backing: self.backing.backing.clone(),
            shared_mapping: self.backing.shared_mapping.clone(),
            residency: match self.backing.data_residency {
                DataResidency::LocalComplete => SourceResidency::Complete,
                DataResidency::RemoteLazy => SourceResidency::OnDemand,
            },
            dynamic_residency: self.backing.dynamic_residency.as_ref().map(|residency| {
                SourceResidencyHandle {
                    backing_id: residency.backing_id.clone(),
                    registry: residency.registry.clone(),
                }
            }),
        })
    }

    pub(crate) fn matches_content_identity(&self, identity: &str) -> bool {
        self.backing
            .metadata
            .content_sha256
            .trim_start_matches("sha256:")
            == identity.trim_start_matches("sha256:")
    }
}

pub(in crate::vm) struct RuntimeBinding {
    pub(in crate::vm) snapshot_id: String,
    pub(in crate::vm) spec_sha256: String,
    pub(in crate::vm) runtime: RuntimeIdentity,
    pub(in crate::vm) layers: Vec<BindingLayer>,
    pub(in crate::vm) vm_state: Option<PreparedVmState>,
    pub(in crate::vm) lineage: SnapshotLineage,
    pub(in crate::vm) expected_geometry: HashMap<ProtocolLayerKind, (u64, u64)>,
    dependencies: Arc<RuntimeDependencies>,
    state: Mutex<RuntimeBindingState>,
}

#[derive(Default)]
struct RuntimeBindingState {
    geometry: Option<Vec<LayerGeometry>>,
    data_plane: Option<Arc<RemoteFaultConnection>>,
    /// Includes submitted promotions with an uncertain result: CLH may use their FDs.
    promoted_layers: HashMap<String, Vec<Arc<LayerResources>>>,
}

impl RuntimeBinding {
    fn dependency_layers(&self) -> Result<Vec<DependencyLayer>, RuntimeStartError> {
        let mut layers = Vec::new();
        let mut ordered = self.layers.iter().collect::<Vec<_>>();
        ordered.sort_by_key(|layer| layer.ordinal);
        for layer in ordered {
            let Some(backing) = layer
                .resources
                .get()
                .and_then(|resources| resources.backing())
            else {
                continue;
            };
            let mut data = Vec::new();
            let mut zero = Vec::new();
            match &layer.backing.coverage {
                LayerCoveragePayload::Canonical { range_map, .. } => {
                    for range in &range_map.ranges {
                        let interval = (range.logical_offset, range.logical_offset + range.length);
                        match range.kind {
                            RangeKind::Data => data.push(interval),
                            RangeKind::Zero => zero.push(interval),
                            RangeKind::Inherit => {}
                        }
                    }
                }
                LayerCoveragePayload::Frozen(ownership) => {
                    let count = ownership.logical_size.div_ceil(ownership.granularity);
                    for unit in 0..count {
                        let state =
                            (ownership.packed_states[unit as usize / 4] >> ((unit % 4) * 2)) & 3;
                        let start = unit * ownership.granularity;
                        let end = (start + ownership.granularity).min(ownership.logical_size);
                        let output = match state {
                            0 => continue,
                            1 => &mut data,
                            2 => &mut zero,
                            _ => return invalid_runtime("invalid frozen ownership state"),
                        };
                        if let Some(last) = output
                            .last_mut()
                            .filter(|last: &&mut (u64, u64)| last.1 == start)
                        {
                            last.1 = end;
                        } else {
                            output.push((start, end));
                        }
                    }
                }
            }
            layers.push(DependencyLayer {
                backing,
                data,
                zero,
            });
        }
        Ok(layers)
    }

    pub(in crate::vm) fn requires_remote_data(&self) -> bool {
        self.layers
            .iter()
            .any(|layer| layer.backing.data_residency == DataResidency::RemoteLazy)
    }
}

/// Lookup and metrics only; each instance owns its binding.
#[derive(Default)]
struct RuntimeIndexState {
    bindings: HashMap<String, Weak<RuntimeBinding>>,
}

struct RuntimeIndex {
    state: Mutex<RuntimeIndexState>,
    metrics: Option<Arc<SnapdMetrics>>,
}

impl RuntimeIndex {
    fn new(metrics: Option<Arc<SnapdMetrics>>) -> Self {
        Self {
            state: Mutex::new(RuntimeIndexState::default()),
            metrics,
        }
    }

    fn emit_structured(&self, event: StructuredEvent) {
        if let Some(metrics) = &self.metrics {
            metrics.emit_structured(event);
        }
    }

    async fn insert<H>(
        &self,
        instance: &VmmInstance<H>,
        binding: Arc<RuntimeBinding>,
    ) -> Result<(), RuntimeStartError> {
        let mut state = self.state.lock().await;
        let before = state.bindings.len();
        state
            .bindings
            .retain(|_, binding| binding.strong_count() > 0);
        if let Some(metrics) = &self.metrics {
            metrics.add_lease(
                LeaseKind::Runtime,
                -((before - state.bindings.len()) as i64),
            );
        }
        if state.bindings.contains_key(instance.vm_id()) {
            return conflict("VM already has an active runtime binding");
        }
        let mut owner = instance.binding.lock().await;
        if owner.is_some() {
            return conflict("instance already owns a runtime binding");
        }
        let target_ids: HashSet<_> = state
            .bindings
            .values()
            .filter_map(Weak::upgrade)
            .flat_map(|binding| {
                binding
                    .layers
                    .iter()
                    .map(|layer| layer.target_id.clone())
                    .collect::<Vec<_>>()
            })
            .collect();
        if binding
            .layers
            .iter()
            .any(|layer| target_ids.contains(&layer.target_id))
        {
            return conflict("runtime target identity is already active");
        }
        *owner = Some(binding.clone());
        state
            .bindings
            .insert(instance.vm_id().into(), Arc::downgrade(&binding));
        if let Some(metrics) = &self.metrics {
            metrics.add_lease(LeaseKind::Runtime, 1);
            Self::update_depths(&state, metrics);
        }
        Ok(())
    }

    async fn release<H>(
        &self,
        instance: &VmmInstance<H>,
        runtime: &RuntimeIdentity,
    ) -> Option<Arc<RemoteFaultConnection>> {
        let mut state = self.state.lock().await;
        let mut owner = instance.binding.lock().await;
        let binding = owner.as_ref()?;
        if binding.runtime != *runtime {
            return None;
        }
        // A late cleanup for an old instance must not remove a replacement's index.
        if state
            .bindings
            .get(instance.vm_id())
            .is_some_and(|indexed| indexed.ptr_eq(&Arc::downgrade(binding)))
        {
            state.bindings.remove(instance.vm_id());
            if let Some(metrics) = &self.metrics {
                metrics.add_lease(LeaseKind::Runtime, -1);
                Self::update_depths(&state, metrics);
            }
        }
        let binding = owner.take().expect("binding checked above");
        let plane = binding.state.lock().await.data_plane.take();
        plane
    }

    fn update_depths(state: &RuntimeIndexState, metrics: &SnapdMetrics) {
        for (kind, data_kind) in [
            (SnapshotLayerKind::Memory, DataKind::Memory),
            (SnapshotLayerKind::Block, DataKind::Block),
        ] {
            let depth = state
                .bindings
                .values()
                .filter_map(Weak::upgrade)
                .map(|binding| binding.lineage.depth(kind))
                .max()
                .unwrap_or(0);
            metrics.set_runtime_chain_depth(data_kind, depth);
        }
    }
}

pub(in crate::vm) fn bind_prepared(
    prepared: &PreparedSnapshotRuntime,
    runtime: &RuntimeIdentity,
    daemon_identity: &str,
) -> Result<Arc<RuntimeBinding>, RuntimeStartError> {
    let layers = prepared
        .layers
        .iter()
        .map(|layer| BindingLayer {
            target_id: layer.target_id.clone().unwrap_or_else(|| {
                target_id(
                    daemon_identity,
                    runtime,
                    protocol_kind(layer.backing.metadata.kind),
                    layer.ordinal,
                )
            }),
            capability_source_target_id: None,
            ordinal: layer.ordinal,
            layer_id: layer.layer_id.clone(),
            backing: layer.backing.clone(),
            resources: layer
                .resources
                .clone()
                .map(OnceLock::from)
                .unwrap_or_default(),
        })
        .collect::<Vec<_>>();
    let expected_geometry = expected_geometry_from_layers(&layers);
    let sources = layers
        .iter()
        .map(BindingLayer::snapshot_source)
        .collect::<Result<Vec<_>, _>>()?;
    let lineage = SnapshotLineage::from_remote(
        &prepared.lineage.snapshot_id,
        &prepared.parent_chains,
        &sources,
    )
    .with_provenance(prepared.lineage.provenance.clone());
    Ok(Arc::new(RuntimeBinding {
        snapshot_id: prepared.snapshot_id.clone(),
        spec_sha256: prepared.spec_sha256.clone(),
        runtime: runtime.clone(),
        layers,
        vm_state: prepared.vm_state.clone(),
        lineage,
        expected_geometry,
        dependencies: RuntimeDependencies::new(),
        state: Mutex::new(RuntimeBindingState::default()),
    }))
}

fn local_snapshot_owner(snapshot_id: &str) -> RuntimeIdentity {
    RuntimeIdentity::new(format!("snapshot-pin:{snapshot_id}"))
}

fn local_snapshot_target(snapshot_id: &str, kind: SnapshotLayerKind, ordinal: u32) -> String {
    target_id(
        "checkpoint",
        &local_snapshot_owner(snapshot_id),
        protocol_kind(kind),
        ordinal,
    )
}

pub(in crate::vm) fn bind_local(
    runtime_source: &LocalSnapshotRuntimeSource,
    runtime: &RuntimeIdentity,
    daemon_identity: &str,
) -> Result<Arc<RuntimeBinding>, RuntimeStartError> {
    runtime_source
        .snapshot
        .ensure_source_integrity()
        .map_err(|error| RuntimeStartError::Invalid(error.to_string()))?;
    let metadata: &dyn crate::snapshot::SnapshotInstance = runtime_source.snapshot.as_ref();
    // The snapshot owns its complete immutable ancestry. A fork is therefore
    // independent of the source VM binding and remains launchable after the
    // source exits.
    let mut layers = Vec::new();
    for kind in [SnapshotLayerKind::Memory, SnapshotLayerKind::Block] {
        let declared = metadata
            .layer_chain(kind)
            .map_err(RuntimeStartError::Invalid)?;
        let sources = local_layer_sources(&runtime_source.snapshot, kind);
        let Some(chain) = declared else {
            if sources.is_empty() {
                continue;
            }
            return invalid_runtime("local snapshot metadata omits an attachable layer chain");
        };
        if chain.layers.len() != sources.len() {
            return invalid_runtime("local snapshot metadata differs from its backing chain");
        }
        // Snapshot metadata establishes cardinality, ordering and geometry.
        // Runtime sources only resolve the immutable backing/FD for each entry.
        for (expected_ordinal, (description, source)) in
            chain.layers.iter().zip(sources).enumerate()
        {
            if description.ordinal != expected_ordinal as u64
                || description.logical_bytes != chain.logical_bytes
                || match kind {
                    SnapshotLayerKind::Memory => {
                        description.unit_size_bytes != chain.unit_size_bytes
                    }
                    SnapshotLayerKind::Block => {
                        description.unit_size_bytes == 0
                            || description.unit_size_bytes % 512 != 0
                            || chain.logical_bytes % description.unit_size_bytes != 0
                    }
                }
            {
                return invalid_runtime(
                    "local snapshot metadata has invalid layer geometry or order",
                );
            }
            let ordinal = u32::try_from(description.ordinal).map_err(|_| {
                RuntimeStartError::Invalid("local snapshot layer ordinal exceeds u32".into())
            })?;
            let binding = match source {
                LocalLayerSource::Remote(source) => {
                    if source.ordinal != ordinal {
                        return invalid_runtime(
                            "remote ancestry layer ordinals are not contiguous",
                        );
                    }
                    BindingLayer {
                        target_id: target_id(
                            daemon_identity,
                            runtime,
                            protocol_kind(kind),
                            ordinal,
                        ),
                        capability_source_target_id: Some(local_snapshot_target(
                            metadata.snapshot_id(),
                            kind,
                            source.ordinal,
                        )),
                        ordinal,
                        layer_id: source.layer_id.clone(),
                        backing: runtime_source.remote_layer_backing(source)?,
                        resources: OnceLock::new(),
                    }
                }
                LocalLayerSource::Frozen { snapshot, layer } => local_binding_layer(
                    runtime_source,
                    snapshot,
                    kind,
                    layer,
                    ordinal,
                    runtime,
                    daemon_identity,
                )?,
            };
            if binding.layer_id != description.layer_id
                || !binding.matches_content_identity(&description.content_identity)
                || binding.backing.metadata.kind != kind
                || binding.backing.metadata.logical_bytes != description.logical_bytes
                || binding.backing.metadata.unit_size_bytes != description.unit_size_bytes
            {
                return invalid_runtime(
                    "local snapshot metadata differs from its immutable backing",
                );
            }
            layers.push(binding);
        }
    }
    if layers.is_empty() {
        return invalid_runtime("snapshot has no attachable memory or block layer");
    }
    let vm_state = runtime_source
        .snapshot
        .vm_state
        .as_ref()
        .map(|state| PreparedVmState {
            format_version: state.format_version,
            payload: state.payload.clone(),
        });
    let expected_geometry = expected_geometry_from_layers(&layers);
    let lineage = runtime_source
        .snapshot
        .lineage()
        .with_frozen(Arc::clone(&runtime_source.snapshot));
    runtime_source
        .snapshot
        .ensure_source_integrity()
        .map_err(|error| RuntimeStartError::Invalid(error.to_string()))?;
    Ok(Arc::new(RuntimeBinding {
        snapshot_id: metadata.snapshot_id().to_owned(),
        spec_sha256: runtime_source.spec_sha256.clone(),
        runtime: runtime.clone(),
        layers,
        vm_state,
        lineage,
        expected_geometry,
        dependencies: RuntimeDependencies::new(),
        state: Mutex::new(RuntimeBindingState::default()),
    }))
}

enum LocalLayerSource<'a> {
    Remote(&'a RemoteLayerSource),
    Frozen {
        snapshot: &'a Snapshot,
        layer: &'a FrozenLocalLayer,
    },
}

fn local_layer_sources(snapshot: &Snapshot, kind: SnapshotLayerKind) -> Vec<LocalLayerSource<'_>> {
    let mut sources = Vec::new();
    for entry in snapshot.lineage().entries() {
        match entry {
            SnapshotLineageEntry::Remote {
                kind: entry_kind,
                layers,
                ..
            } if *entry_kind == kind => {
                sources.extend(layers.iter().map(LocalLayerSource::Remote));
            }
            SnapshotLineageEntry::FrozenLocal(ancestor) => {
                if let Some(layer) = ancestor.frozen_layers.get(&kind) {
                    sources.push(LocalLayerSource::Frozen {
                        snapshot: ancestor,
                        layer,
                    });
                }
            }
            _ => {}
        }
    }
    if let Some(layer) = snapshot.frozen_layers.get(&kind) {
        sources.push(LocalLayerSource::Frozen { snapshot, layer });
    }
    sources
}

fn local_binding_layer(
    runtime_source: &LocalSnapshotRuntimeSource,
    snapshot: &Snapshot,
    kind: SnapshotLayerKind,
    local: &FrozenLocalLayer,
    ordinal: u32,
    runtime: &RuntimeIdentity,
    daemon_identity: &str,
) -> Result<BindingLayer, RuntimeStartError> {
    let backing = runtime_source.layer_backing(snapshot, kind, local)?;
    let reader = snapshot
        .layer_reader(kind, &local.provisional_content_id)
        .map_err(|error| RuntimeStartError::Invalid(error.to_string()))?;
    let resources = LayerResources::new_native(snapshot.backing_handle(kind)?, Some(reader));
    Ok(BindingLayer {
        target_id: target_id(daemon_identity, runtime, protocol_kind(kind), ordinal),
        capability_source_target_id: None,
        ordinal,
        layer_id: local.layer_id.clone(),
        backing,
        resources: OnceLock::from(resources),
    })
}

async fn build_runtime_backing(
    binding: &RuntimeBinding,
) -> Result<RuntimeBackingPayload, RuntimeStartError> {
    let mut attachments = Vec::with_capacity(binding.layers.len());
    for layer in &binding.layers {
        let coverage = match &layer.backing.coverage {
            LayerCoveragePayload::Canonical { payload, .. } => CoverageAttachment::Canonical {
                fd: &payload.fd,
                size: payload.size,
                content_id: &payload.content_id,
            },
            LayerCoveragePayload::Frozen(ownership) => CoverageAttachment::Frozen(ownership),
        };
        attachments.push(LayerAttachment {
            kind: protocol_kind(layer.backing.metadata.kind),
            data_residency: layer.backing.data_residency,
            ordinal: layer.ordinal,
            layer_id: &layer.layer_id,
            target_id: &layer.target_id,
            content_id: &layer.backing.metadata.content_sha256,
            format_version: layer.backing.metadata.format_version,
            logical_bytes: layer.backing.metadata.logical_bytes,
            unit_size_bytes: layer.backing.metadata.unit_size_bytes,
            backing: &layer.backing.backing,
            shared_mapping: layer.backing.shared_mapping.as_deref(),
            coverage,
            initial_residency: build_initial_residency(&layer.backing).await?,
        });
    }
    crate::vm::runtime::prepare::build_runtime_backing(
        &attachments,
        binding.vm_state.as_ref().map(|state| VmStateAttachment {
            format_version: state.format_version,
            payload: &state.payload,
        }),
    )
    .map_err(Into::into)
}

async fn build_initial_residency(
    backing: &LayerBacking,
) -> Result<Option<ResidentCoverageDescriptor>, RuntimeStartError> {
    if backing.data_residency != DataResidency::RemoteLazy {
        if backing.dynamic_residency.is_some() {
            return invalid_runtime("local-complete backing must not have dynamic residency");
        }
        return Ok(None);
    }
    let dynamic = backing.dynamic_residency.as_ref().ok_or_else(|| {
        RuntimeStartError::Invalid("remote-lazy backing has no residency registry".into())
    })?;
    let LayerCoveragePayload::Canonical { range_map, .. } = &backing.coverage else {
        return invalid_runtime("remote-lazy backing requires a canonical range map");
    };
    let data_range_count = range_map
        .ranges
        .iter()
        .filter(|range| range.kind == RangeKind::Data)
        .count();
    let data_range_count_u32 = u32::try_from(data_range_count)
        .map_err(|_| RuntimeStartError::Invalid("DATA range count exceeds u32".into()))?;
    if data_range_count == 0 {
        return Ok(None);
    }
    let resident = dynamic.registry.resident_ranges().await;
    let mut resident_bitmap = vec![0_u8; data_range_count.div_ceil(8)];
    for (data_index, range) in range_map
        .ranges
        .iter()
        .filter(|range| range.kind == RangeKind::Data)
        .enumerate()
    {
        let key = DataRangeKey::new(
            dynamic.backing_id.clone(),
            range.logical_offset,
            range.length,
        )
        .map_err(|error| RuntimeStartError::Invalid(error.to_string()))?;
        if resident.contains(&key) {
            resident_bitmap[data_index / 8] |= 1_u8 << (data_index % 8);
        }
    }
    if resident_bitmap.iter().all(|byte| *byte == 0) {
        return Ok(None);
    }
    Ok(Some(ResidentCoverageDescriptor {
        data_range_count: data_range_count_u32,
        resident_bitmap,
    }))
}

pub(in crate::vm) async fn build_start_runtime(
    binding: &RuntimeBinding,
    launch: &VmmLaunchSpec,
    correlation_id: &str,
    deadline_unix_ms: u64,
    required_capabilities: Vec<Capability>,
) -> Result<(WireEnvelope<ControlRequest>, Vec<OwnedFd>), RuntimeStartError> {
    let backing = build_runtime_backing(binding).await?;
    Ok(crate::vm::runtime::prepare::build_start_request(
        &binding.runtime.sandbox_id,
        &binding.snapshot_id,
        &binding.spec_sha256,
        launch,
        correlation_id,
        deadline_unix_ms,
        required_capabilities,
        backing,
    ))
}

pub(in crate::vm) fn validate_ready_geometry(
    geometry: &[LayerGeometry],
    binding: &RuntimeBinding,
) -> Result<(), RuntimeStartError> {
    let expected = &binding.expected_geometry;
    let actual = geometry
        .iter()
        .map(|item| (item.kind, (item.logical_size, item.alignment)))
        .collect::<HashMap<_, _>>();
    let matches = if expected.contains_key(&ProtocolLayerKind::Memory) {
        &actual == expected
    } else {
        actual.get(&ProtocolLayerKind::Block) == expected.get(&ProtocolLayerKind::Block)
    };
    if !matches {
        return protocol("RuntimeStarted geometry differs from attached geometry");
    }
    Ok(())
}

fn expected_geometry_from_layers(
    layers: &[BindingLayer],
) -> HashMap<ProtocolLayerKind, (u64, u64)> {
    layers
        .iter()
        .map(|layer| {
            let kind = protocol_kind(layer.backing.metadata.kind);
            (
                kind,
                (layer.backing.metadata.logical_bytes, io_alignment(kind)),
            )
        })
        .collect()
}

fn validate_seed_chains(
    layers: &[RuntimeLayerSeed],
    chains: &HashMap<SnapshotLayerKind, LayerChainIndex>,
) -> Result<(), RuntimeStartError> {
    let mut seen = HashSet::new();
    let mut seen_targets = HashSet::new();
    for kind in [SnapshotLayerKind::Memory, SnapshotLayerKind::Block] {
        let mut selected = layers
            .iter()
            .filter(|layer| layer.backing.metadata.kind == kind)
            .collect::<Vec<_>>();
        selected.sort_by_key(|layer| layer.ordinal);
        if selected.is_empty() {
            if chains.contains_key(&kind) {
                return invalid_runtime("prepared chain has no backing layers");
            }
            continue;
        }
        let chain = chains.get(&kind).ok_or_else(|| {
            RuntimeStartError::Invalid("prepared layers have no chain index".into())
        })?;
        chain.validate().map_err(RuntimeStartError::Invalid)?;
        if chain.layers.len() != selected.len() {
            return invalid_runtime("prepared chain length differs from backing layers");
        }
        for (position, (layer, reference)) in selected.iter().zip(&chain.layers).enumerate() {
            if layer.ordinal as usize != position
                || reference.ordinal as usize != position
                || layer.layer_id != reference.layer_id
                || layer.backing.metadata.content_sha256 != reference.content_sha256
                || layer.backing.metadata.logical_bytes != chain.logical_bytes
                || layer.backing.metadata.unit_size_bytes != chain.unit_size_bytes
            {
                return invalid_runtime(
                    "prepared layer identity or geometry differs from chain index",
                );
            }
            if !seen.insert((kind, layer.ordinal)) {
                return invalid_runtime("prepared runtime contains a duplicate layer ordinal");
            }
            if layer
                .target_id
                .as_ref()
                .is_some_and(|target| !seen_targets.insert(target.clone()))
            {
                return invalid_runtime("prepared runtime contains a duplicate adopted target");
            }
        }
    }
    Ok(())
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RuntimeStartResult {
    pub vm_id: String,
    pub success: bool,
    pub code: String,
    pub message: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RuntimeBatchStartResult {
    pub runtimes: Vec<RuntimeStartResult>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GuestRuntimeObservation {
    pub vmm_pid: u32,
    pub endpoint: GuestEndpointInfo,
}

pub(crate) fn runtime_result_parts(
    observation: GuestRuntimeObservation,
) -> (GuestEndpoint, u32, RockletArtifact) {
    let endpoint = observation.endpoint;
    (
        GuestEndpoint {
            transport: "vsock-tcp-forward".into(),
            host_port: endpoint.host_port,
            guest_port: endpoint.guest_port,
            vsock_cid: endpoint.vsock_cid,
        },
        observation.vmm_pid,
        RockletArtifact {
            identity: endpoint.artifact.identity,
            version: endpoint.artifact.version,
            sha256: format!("sha256:{}", endpoint.artifact.sha256),
            logical_bytes: endpoint.artifact.logical_bytes,
        },
    )
}

impl RuntimeBatchStartResult {
    pub fn successful_vm_ids(&self) -> Vec<String> {
        self.runtimes
            .iter()
            .filter(|child| child.success)
            .map(|child| child.vm_id.clone())
            .collect()
    }
}

pub type LaunchCallback = Arc<
    dyn Fn(VmmLaunchSpec) -> BoxFuture<Result<Box<dyn VMMClientInterface>, RuntimeStartError>>
        + Send
        + Sync,
>;

#[derive(Clone)]
pub struct RuntimeStartRequest {
    pub vm_id: String,
    pub launch_spec: VmmLaunchSpec,
    pub runtime_source: SnapshotRuntimeSource,
    pub cancel_requested: Option<Arc<AtomicBool>>,
}

impl RuntimeStartRequest {
    fn validate(&self) -> Result<(), RuntimeStartError> {
        if self.vm_id.is_empty() {
            return invalid_runtime("VM identity must not be empty");
        }
        if self.launch_spec.runtime.sandbox_id != self.vm_id {
            return invalid_runtime("launch runtime sandbox must match vm_id");
        }
        self.launch_spec.validate()?;
        Ok(())
    }

    fn cancelled(&self) -> bool {
        self.cancel_requested
            .as_ref()
            .is_some_and(|cancelled| cancelled.load(Ordering::Acquire))
    }
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
enum RuntimeSubmission {
    Finalize {
        runtime: RuntimeIdentity,
        capture_correlation_id: String,
    },
}

#[derive(Clone)]
pub struct ActiveCaptureBinding {
    pub context: RuntimeCaptureContext,
    pub(crate) p2p_source: WeakRuntimeBindingHandle,
}

impl VmRepository<Box<dyn VMMClientInterface>> {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        launcher: Option<LaunchCallback>,
        required_capabilities: Vec<Capability>,
        runtime_start_timeout: Duration,
        request_timeout: Duration,
        max_concurrency: usize,
        shutdown_timeout: Duration,
    ) -> Result<Self, RuntimeStartError> {
        Self::new_inner(
            launcher,
            required_capabilities,
            runtime_start_timeout,
            request_timeout,
            max_concurrency,
            shutdown_timeout,
            None,
            RemoteFaultLimits::default(),
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub fn new_with_remote_load(
        launcher: Option<LaunchCallback>,
        required_capabilities: Vec<Capability>,
        runtime_start_timeout: Duration,
        request_timeout: Duration,
        max_concurrency: usize,
        shutdown_timeout: Duration,
        remote_load: Arc<RangeLoadExecutor>,
        remote_fault_limits: RemoteFaultLimits,
    ) -> Result<Self, RuntimeStartError> {
        Self::new_inner(
            launcher,
            required_capabilities,
            runtime_start_timeout,
            request_timeout,
            max_concurrency,
            shutdown_timeout,
            Some(RemoteFaultService::new(remote_load)),
            remote_fault_limits,
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn new_inner(
        launcher: Option<LaunchCallback>,
        required_capabilities: Vec<Capability>,
        runtime_start_timeout: Duration,
        request_timeout: Duration,
        max_concurrency: usize,
        shutdown_timeout: Duration,
        remote_fault_api: Option<Arc<dyn RemoteFaultApi>>,
        remote_fault_limits: RemoteFaultLimits,
    ) -> Result<Self, RuntimeStartError> {
        if runtime_start_timeout.is_zero()
            || request_timeout.is_zero()
            || shutdown_timeout.is_zero()
            || !(1..=MAX_CONCURRENT_STARTS).contains(&max_concurrency)
        {
            return invalid_runtime("runtime timeouts and bounded concurrency are invalid");
        }
        remote_fault_limits
            .validate()
            .map_err(|error| RuntimeStartError::Invalid(error.to_string()))?;
        let metrics = remote_fault_api.as_ref().map(|api| api.metrics());
        let bindings = Arc::new(RuntimeIndex::new(metrics));
        let cleanup_bindings = bindings.clone();
        let release_hooks = Arc::new(StdMutex::new(Vec::<RuntimeReleaseHook>::new()));
        let cleanup_release_hooks = release_hooks.clone();
        let cleanup_remote_api = remote_fault_api.clone();
        let cleanup: CleanupCallback<Box<dyn VMMClientInterface>> = Arc::new(move |instance| {
            let bindings = cleanup_bindings.clone();
            let release_hooks = cleanup_release_hooks.clone();
            let remote_api = cleanup_remote_api.clone();
            Box::pin(async move {
                let mut first_error = None;
                // Hold the binding through revoke/drain, so weak lookup still finds
                // every layer while closing the data plane and provider session.
                let binding = instance.runtime_binding().await;
                let runtime_identity = binding.as_ref().map(|binding| binding.runtime.clone());
                if let Some(handles) = instance.runtime_handles().await {
                    if let Err(error) = handles
                        .cleanup("runtime registry cleanup", shutdown_timeout)
                        .await
                    {
                        match handles.has_exited() {
                            Ok(true) => {
                                // The graceful protocol failed but the
                                // bounded fallback already proved the exact
                                // process dead. Local owners may now be
                                // released. The VMM-side failure remains in
                                // the process log for structured gate evidence.
                            }
                            Ok(false) => return Err(VmRepositoryError::Runtime(error.to_string())),
                            Err(identity_error) => {
                                return Err(VmRepositoryError::Runtime(format!(
                                    "{error}; process exit verification failed: {identity_error}"
                                )));
                            }
                        }
                    }
                }
                let data_plane = match &binding {
                    Some(binding) => binding.state.lock().await.data_plane.clone(),
                    None => None,
                };
                let had_data_plane = data_plane.is_some();
                if let Some(data_plane) = data_plane {
                    if let Err(error) = data_plane.close().await {
                        first_error.get_or_insert_with(|| error.to_string());
                    }
                }
                if let Some(binding) = &binding {
                    binding.dependencies.close().await;
                }
                if let Some(runtime) = &runtime_identity {
                    // Also covers launch failures before a data plane was installed.
                    if !had_data_plane {
                        if let Some(api) = &remote_api {
                            api.close_runtime(runtime).await;
                        }
                    }
                    let hooks = match release_hooks.lock() {
                        Ok(hooks) => hooks.clone(),
                        Err(_) => {
                            first_error.get_or_insert_with(|| {
                                "runtime release-hook registry is poisoned".into()
                            });
                            Vec::new()
                        }
                    };
                    for hook in hooks {
                        if let Err(error) = hook(runtime.clone()).await {
                            first_error.get_or_insert(error);
                        }
                    }
                }
                match first_error {
                    Some(error) => Err(VmRepositoryError::Runtime(error)),
                    None => {
                        if let Some(runtime) = &runtime_identity {
                            bindings.release(instance.as_ref(), runtime).await;
                        }
                        Ok(())
                    }
                }
            })
        });
        let launcher = launcher.unwrap_or_else(|| {
            Arc::new(|spec| Box::pin(async move { launch_client(spec).map_err(Into::into) }))
        });
        Ok(Self {
            inner: Arc::new(VmRepositoryInner {
                daemon_epoch: Uuid::new_v4().simple().to_string(),
                state: Arc::new(Mutex::new(RegistryState {
                    instances: HashMap::new(),
                    inflight: HashMap::new(),
                    start_by_vm: HashMap::new(),
                })),
                cleanup,
                start_slots: Arc::new(Semaphore::new(64)),
                start_journal: Arc::new(StdMutex::new(None)),
                start_timeout: Arc::new(StdMutex::new(Duration::from_secs(300))),
                runtime_index: bindings,
                launcher,
                semaphore: Arc::new(Semaphore::new(max_concurrency)),
                required_capabilities,
                request_timeout,
                runtime_start_timeout,
                daemon_identity: Uuid::new_v4().simple().to_string(),
                remote_fault_api,
                remote_fault_limits,
                release_hooks,
                one_shot_submissions: Mutex::new(HashSet::new()),
                source_locks: Mutex::new(HashMap::new()),
                local_snapshot_backings: LocalSnapshotBackingLeases::default(),
            }),
        })
    }

    pub(crate) fn register_release_hook(
        &self,
        hook: RuntimeReleaseHook,
    ) -> Result<(), RuntimeStartError> {
        let mut hooks = self.inner.release_hooks.lock().map_err(|_| {
            RuntimeStartError::Vmm("runtime release-hook registry is poisoned".into())
        })?;
        hooks.push(hook);
        Ok(())
    }

    pub fn remote_target_id(
        &self,
        runtime: &RuntimeIdentity,
        kind: SnapshotLayerKind,
        ordinal: u32,
    ) -> String {
        target_id(
            &self.inner.daemon_identity,
            runtime,
            protocol_kind(kind),
            ordinal,
        )
    }

    pub async fn start_runtime(
        &self,
        request: RuntimeStartRequest,
        starting_instance: Option<&Arc<VmmInstance<Box<dyn VMMClientInterface>>>>,
    ) -> Result<RuntimeStartResult, RuntimeStartError> {
        request.validate()?;
        self.ensure_startable(&request.vm_id, starting_instance)
            .await?;
        if request.cancelled() {
            return Ok(cancelled_result(&request));
        }
        let permit = self.inner.semaphore.clone().acquire_owned();
        let _permit = match starting_instance {
            Some(starting_instance) => tokio::select! {
                permit = permit => permit,
                _ = starting_instance.start_cancelled() => return Ok(cancelled_result(&request)),
            },
            None => permit.await,
        }
        .map_err(|_| RuntimeStartError::Vmm("runtime launch semaphore is closed".into()))?;
        if request.cancelled() {
            return Ok(cancelled_result(&request));
        }
        let manager = self.clone();
        let callback_request = request.clone();
        let start = move |instance| async move {
            manager
                .start_instance(callback_request, instance)
                .await
                .map_err(|error| VmRepositoryError::Runtime(error.to_string()))
        };
        let result = self
            .start_inner(&request.vm_id, starting_instance, start)
            .await?;
        if result.success {
            self.ensure_exit_watcher(&request.vm_id).await;
        }
        Ok(runtime_start_result(&request.launch_spec.runtime, result))
    }

    pub async fn start_runtimes(
        &self,
        requests: Vec<RuntimeStartRequest>,
    ) -> Result<RuntimeBatchStartResult, RuntimeStartError> {
        validate_batch(&requests)?;
        let mut tasks = Vec::with_capacity(requests.len());
        for request in requests {
            let manager = self.clone();
            let vm_id = request.vm_id.clone();
            let task_vm_id = vm_id.clone();
            let task = tokio::spawn(async move {
                match manager.start_runtime(request, None).await {
                    Ok(result) => result,
                    Err(error) => RuntimeStartResult {
                        vm_id: task_vm_id,
                        success: false,
                        code: "REQUEST_REJECTED".into(),
                        message: Some(error.to_string()),
                    },
                }
            });
            tasks.push((vm_id, task));
        }
        let mut runtimes = Vec::with_capacity(tasks.len());
        for (vm_id, task) in tasks {
            runtimes.push(joined_runtime_result(vm_id, task.await));
        }
        Ok(RuntimeBatchStartResult { runtimes })
    }

    #[allow(clippy::too_many_arguments)]
    pub async fn promote_published_snapshot(
        &self,
        vm_id: &str,
        capture_correlation_id: &str,
        capture_request_sha256: &str,
        snapshot_id: &str,
        spec_sha256: &str,
        prepared: &PreparedFinalization,
    ) -> Result<(), RuntimeStartError> {
        let instance = self
            .instance(vm_id)
            .await
            .ok_or_else(|| RuntimeStartError::Invalid("runtime instance was not found".into()))?;
        let binding = instance
            .runtime_binding()
            .await
            .ok_or_else(|| RuntimeStartError::Invalid("runtime binding was not found".into()))?;
        if prepared.runtime != binding.runtime {
            return conflict("prepared promotion belongs to a different runtime");
        }
        let runtime = instance.runtime_handles().await.ok_or_else(|| {
            RuntimeStartError::Invalid("runtime handles are not installed".into())
        })?;
        let request = FinalizePublishedGeneration {
            capture_correlation_id: capture_correlation_id.into(),
            capture_request_sha256: capture_request_sha256.into(),
            snapshot_id: snapshot_id.into(),
            spec_sha256: spec_sha256.into(),
            layers: prepared.layers.clone(),
            fd_roles: prepared.fd_roles.clone(),
        };
        let correlation = stable_correlation(
            "finalize-published-generation",
            &binding.runtime,
            capture_correlation_id,
        );
        let fds = prepared
            .fds
            .iter()
            .map(duplicate_fd)
            .collect::<Result<Vec<_>, _>>()?;
        self.reserve_submission(RuntimeSubmission::Finalize {
            runtime: binding.runtime.clone(),
            capture_correlation_id: capture_correlation_id.into(),
        })
        .await?;
        let envelope = WireEnvelope {
            header: current_header(correlation.clone()),
            message: ControlRequest::FinalizePublishedGeneration(request),
        };
        // Transfer ownership before the first send/await. If this future is cancelled,
        // CLH may already have installed the FDs; the instance must retain their leases.
        binding
            .state
            .lock()
            .await
            .promoted_layers
            .insert(correlation.clone(), prepared.resources.clone());
        let reply = tokio::time::timeout(
            self.inner.request_timeout,
            runtime.control().request(envelope, fds),
        )
        .await;
        match reply {
            Ok(Ok(received)) => {
                if !received.fds.is_empty() {
                    return Err(RuntimeStartError::FinalizationUnknown(
                        "PublishedGenerationFinalized unexpectedly carried descriptors".into(),
                    ));
                }
                match received.envelope.message {
                    ControlReply::PublishedGenerationFinalized(reply)
                        if reply.capture_correlation_id == capture_correlation_id
                            && reply.capture_request_sha256 == capture_request_sha256
                            && reply.snapshot_id == snapshot_id
                            && reply.spec_sha256 == spec_sha256 =>
                    {
                        Ok(())
                    }
                    ControlReply::Error(error) => {
                        // Explicit rejection proves the replacement was not committed.
                        binding
                            .state
                            .lock()
                            .await
                            .promoted_layers
                            .remove(&correlation);
                        protocol(&format!(
                            "FinalizePublishedGeneration failed: {}",
                            error.message
                        ))
                    }
                    _ => Err(RuntimeStartError::FinalizationUnknown(
                        "FinalizePublishedGeneration received a mismatched or unexpected reply"
                            .into(),
                    )),
                }
            }
            Ok(Err(error)) => Err(RuntimeStartError::FinalizationUnknown(error.to_string())),
            Err(_) => Err(RuntimeStartError::FinalizationUnknown(
                "FinalizePublishedGeneration timed out".into(),
            )),
        }
    }

    /// Keep remote ancestry readable after the captured VM has released its grants.
    pub async fn pin_local_snapshot(
        &self,
        vm_id: &str,
        local: &Snapshot,
    ) -> Result<(), RuntimeStartError> {
        let binding = self.runtime_binding(vm_id).await.ok_or_else(|| {
            RuntimeStartError::Invalid("snapshot source binding is absent".into())
        })?;
        if self.inner.remote_fault_api.is_none() && binding.requires_remote_data() {
            return invalid_runtime("local checkpoint requires remote ancestry grants");
        }
        // The old writable generation is now frozen, while CLH has already
        // resumed with a new active generation. Register its DATA before the
        // uploader can install a recoverable source and trigger reclamation.
        for (kind, layer) in &local.frozen_layers {
            let handle = local.backing_handle(*kind)?;
            let ownership = layer.ownership();
            let mut ranges = Vec::new();
            for unit in 0..ownership.logical_size.div_ceil(ownership.granularity) {
                if (ownership.packed_states[unit as usize / 4] >> ((unit % 4) * 2)) & 3 == 1 {
                    let offset = unit * ownership.granularity;
                    let length = ownership.granularity.min(ownership.logical_size - offset);
                    if let Some(last) = ranges
                        .last_mut()
                        .filter(|last: &&mut (u64, u64)| last.0 + last.1 == offset)
                    {
                        last.1 += length;
                    } else {
                        ranges.push((offset, length));
                    }
                }
            }
            binding
                .dependencies
                .register(handle.backing.clone(), &ranges)
                .map_err(RuntimeStartError::Invalid)?;
        }
        let mut ancestry = Vec::new();
        for entry in local.lineage().entries() {
            if let SnapshotLineageEntry::Remote { kind, layers, .. } = entry {
                for source in layers {
                    let layer = binding
                        .layers
                        .iter()
                        .find(|layer| {
                            layer.ordinal == source.ordinal
                                && layer.backing.metadata.kind == *kind
                                && layer.layer_id == source.layer_id
                                && layer.matches_content_identity(&source.range_map.content_sha256)
                        })
                        .ok_or_else(|| {
                            RuntimeStartError::Invalid("snapshot ancestry binding is absent".into())
                        })?;
                    ancestry.push((*kind, source, layer));
                }
            }
        }
        let attachment_cache = Arc::new(LocalLayerBackingCache::default());
        for (_, source, layer) in &ancestry {
            attachment_cache.retain_remote(&local.snapshot_id, source, layer.backing.clone())?;
        }
        self.inner
            .local_snapshot_backings
            .retain_snapshot(&local.snapshot_id, attachment_cache)?;
        let Some(api) = &self.inner.remote_fault_api else {
            return Ok(());
        };
        let owner = local_snapshot_owner(&local.snapshot_id);
        if let Err(error) = api.begin_runtime(&owner).await {
            self.inner
                .local_snapshot_backings
                .release_snapshot(&local.snapshot_id);
            return Err(RuntimeStartError::Protocol(format!("{error:?}")));
        }
        let mut resources = Vec::with_capacity(ancestry.len());
        for (kind, source, layer) in ancestry {
            match api
                .clone_layer_target(
                    &layer.target_id,
                    &owner,
                    &local_snapshot_target(&local.snapshot_id, kind, source.ordinal),
                )
                .await
            {
                Ok(resource) => resources.push(resource),
                Err(error) => {
                    api.close_runtime(&owner).await;
                    self.inner
                        .local_snapshot_backings
                        .release_snapshot(&local.snapshot_id);
                    return Err(RuntimeStartError::Protocol(format!(
                        "checkpoint ancestry pin failed: {error:?}"
                    )));
                }
            }
        }
        if let Err(error) = local.retain_layer_resources(resources) {
            api.close_runtime(&owner).await;
            self.inner
                .local_snapshot_backings
                .release_snapshot(&local.snapshot_id);
            return Err(error.into());
        }
        Ok(())
    }

    pub async fn release_local_snapshot(&self, snapshot_id: &str) {
        if let Some(api) = &self.inner.remote_fault_api {
            api.close_runtime(&local_snapshot_owner(snapshot_id)).await;
        }
        self.inner
            .local_snapshot_backings
            .release_snapshot(snapshot_id);
    }

    pub async fn capture_binding(
        &self,
        vm_id: &str,
    ) -> Result<ActiveCaptureBinding, RuntimeStartError> {
        let binding = self
            .runtime_binding(vm_id)
            .await
            .ok_or_else(|| RuntimeStartError::Invalid("runtime binding is absent".into()))?;
        let geometry = binding
            .state
            .lock()
            .await
            .geometry
            .clone()
            .ok_or_else(|| RuntimeStartError::Invalid("runtime geometry is absent".into()))?;
        let instance = self
            .instance(vm_id)
            .await
            .ok_or_else(|| RuntimeStartError::Invalid("runtime instance is absent".into()))?;
        let handles = instance
            .runtime_handles()
            .await
            .ok_or_else(|| RuntimeStartError::Invalid("runtime handles are absent".into()))?;
        let endpoint = handles.guest_endpoint().ok_or_else(|| {
            RuntimeStartError::Invalid("runtime has no verified guest Rocklet endpoint".into())
        })?;
        let control = handles.control();
        Ok(ActiveCaptureBinding {
            context: RuntimeCaptureContext {
                runtime: binding.runtime.clone(),
                control_client: control,
                geometry,
                lineage: binding.lineage.clone(),
                rocklet_artifact: crate::snapshot::metadata::RockletArtifactCompatibility {
                    identity: endpoint.artifact.identity,
                    version: endpoint.artifact.version,
                    raw_sha256: format!("sha256:{}", endpoint.artifact.sha256),
                    logical_bytes: endpoint.artifact.logical_bytes,
                    guest_port: endpoint.guest_port,
                },
            },
            p2p_source: WeakRuntimeBindingHandle(Arc::downgrade(&binding)),
        })
    }

    async fn runtime_binding(&self, vm_id: &str) -> Option<Arc<RuntimeBinding>> {
        self.instance(vm_id).await?.runtime_binding().await
    }

    pub async fn runtime_identity(&self, vm_id: &str) -> Option<RuntimeIdentity> {
        self.runtime_binding(vm_id)
            .await
            .map(|binding| binding.runtime.clone())
    }

    pub async fn guest_runtime_observation(&self, vm_id: &str) -> Option<GuestRuntimeObservation> {
        let instance = self.instance(vm_id).await?;
        let snapshot = instance.snapshot().await;
        let handles = instance.runtime_handles().await?;
        let endpoint = handles.guest_endpoint()?;
        Some(GuestRuntimeObservation {
            vmm_pid: snapshot.pid?,
            endpoint,
        })
    }

    async fn start_instance(
        &self,
        request: RuntimeStartRequest,
        instance: Arc<VmmInstance<Box<dyn VMMClientInterface>>>,
    ) -> Result<StartedRuntime<Box<dyn VMMClientInterface>>, RuntimeStartError> {
        let total_started = Instant::now();
        if let Some(api) = &self.inner.remote_fault_api {
            api.begin_runtime(&request.launch_spec.runtime)
                .await
                .map_err(|error| {
                    RuntimeStartError::Protocol(format!(
                        "previous runtime cleanup failed: {error:?}"
                    ))
                })?;
        }
        let binding_started = Instant::now();
        let binding =
            self.bind_runtime_source(&request.runtime_source, &request.launch_spec.runtime)?;
        if binding.requires_remote_data() && self.inner.remote_fault_api.is_none() {
            return invalid_runtime(
                "REMOTE_LAZY runtime requires a configured remote range loader",
            );
        }
        if let Some(api) = &self.inner.remote_fault_api {
            for layer in &binding.layers {
                let Some(source_target_id) = &layer.capability_source_target_id else {
                    continue;
                };
                match api
                    .clone_layer_target(source_target_id, &binding.runtime, &layer.target_id)
                    .await
                {
                    Ok(resources) => {
                        layer.resources.set(resources).map_err(|_| {
                            RuntimeStartError::Invalid(
                                "runtime layer resources already installed".into(),
                            )
                        })?;
                    }
                    Err(error) => {
                        api.close_runtime(&binding.runtime).await;
                        return Err(RuntimeStartError::Protocol(format!(
                            "failed to clone snapshot remote-layer capability: {error:?}"
                        )));
                    }
                }
            }
        }
        if let Err(error) = binding
            .dependencies
            .prepare(binding.dependency_layers()?)
            .await
        {
            binding.dependencies.close().await;
            if let Some(api) = &self.inner.remote_fault_api {
                api.close_runtime(&binding.runtime).await;
            }
            return Err(RuntimeStartError::Invalid(error));
        }
        if let Err(error) = self
            .inner
            .runtime_index
            .insert(instance.as_ref(), binding.clone())
            .await
        {
            if let Some(api) = &self.inner.remote_fault_api {
                api.close_runtime(&binding.runtime).await;
            }
            return Err(error);
        }
        self.emit_runtime_start_stage(
            &binding,
            "runtime_binding",
            binding_started.elapsed(),
            total_started.elapsed(),
            None,
            None,
        );
        let launch_started = Instant::now();
        let runtime = match tokio::time::timeout(
            self.inner.request_timeout,
            (self.inner.launcher)(request.launch_spec.clone()),
        )
        .await
        {
            Ok(Ok(runtime)) => runtime,
            Ok(Err(error)) => return Err(error),
            Err(_) => {
                return Err(RuntimeStartError::Vmm(
                    "VMM launch exceeded the configured request timeout".into(),
                ));
            }
        };
        self.emit_runtime_start_stage(
            &binding,
            "vmm_launch",
            launch_started.elapsed(),
            total_started.elapsed(),
            Some(runtime.as_ref()),
            None,
        );
        let control = runtime.control();
        let started = async {
            if self.inner.remote_fault_api.is_some() {
                let data_plane_started = Instant::now();
                let stream = runtime.remote_load_stream()?.ok_or_else(|| {
                    RuntimeStartError::Vmm("VMM did not expose its remote-load channel".into())
                })?;
                let remote_api: Arc<dyn RemoteFaultApi> = self
                    .inner
                    .remote_fault_api
                    .as_ref()
                    .expect("remote fault API checked above")
                    .clone();
                let data_plane = RemoteFaultConnection::start_with_dependencies(
                    stream,
                    binding.runtime.clone(),
                    remote_api,
                    self.inner.remote_fault_limits,
                    Some(binding.dependencies.clone()),
                )
                .map_err(|error| RuntimeStartError::Vmm(error.to_string()))?;
                {
                    let mut state = binding.state.lock().await;
                    if state.data_plane.is_some() {
                        drop(state);
                        let _ = data_plane.close().await;
                        return conflict("runtime already has a remote data plane");
                    }
                    state.data_plane = Some(data_plane);
                }
                self.emit_runtime_start_stage(
                    &binding,
                    "remote_data_plane",
                    data_plane_started.elapsed(),
                    total_started.elapsed(),
                    Some(runtime.as_ref()),
                    None,
                );
            }
            let runtime_control_started = Instant::now();
            let geometry = self
                .submit_runtime_start(control, &binding, &request.launch_spec, total_started)
                .await?;
            self.emit_runtime_start_stage(
                &binding,
                "runtime_control",
                runtime_control_started.elapsed(),
                total_started.elapsed(),
                Some(runtime.as_ref()),
                None,
            );
            if request.launch_spec.guest_rocklet.is_some()
                || request.launch_spec.guest_readiness
                    == crate::vm::runtime::plugger::GuestReadinessMode::PluggerOnly
            {
                let readiness = crate::vm::runtime::client::selected_guest_readiness(
                    runtime.as_ref(),
                    request.launch_spec.guest_readiness,
                    self.inner.runtime_start_timeout,
                )
                .await?;
                if request.launch_spec.guest_readiness
                    == crate::vm::runtime::plugger::GuestReadinessMode::Rocklet
                    && runtime.guest_endpoint().is_none()
                {
                    return invalid_runtime(
                        "guest Rocklet readiness succeeded without an endpoint",
                    );
                }
                self.emit_runtime_start_stage(
                    &binding,
                    if readiness.transport_event_observed {
                        "vsock_transport_ready"
                    } else {
                        "vsock_transport_fallback"
                    },
                    readiness.transport_event_wait,
                    total_started.elapsed(),
                    Some(runtime.as_ref()),
                    None,
                );
                for (index, attempt) in readiness.probe_attempts.iter().enumerate() {
                    let attempt_number = index as u64 + 1;
                    if let Some(duration) = attempt.tcp_connect {
                        self.emit_runtime_readiness_segment(
                            &binding,
                            "readiness_tcp_connect",
                            duration,
                            attempt_number,
                            attempt.error_code,
                            runtime.as_ref(),
                        );
                    }
                    if let Some(duration) = attempt.http_first_byte {
                        self.emit_runtime_readiness_segment(
                            &binding,
                            "readiness_http_first_byte",
                            duration,
                            attempt_number,
                            attempt.error_code,
                            runtime.as_ref(),
                        );
                    }
                    if let Some(duration) = attempt.http_full_response {
                        self.emit_runtime_readiness_segment(
                            &binding,
                            "readiness_http_full_response",
                            duration,
                            attempt_number,
                            attempt.error_code,
                            runtime.as_ref(),
                        );
                    }
                }
                for (index, attempt) in readiness.proxy_attempts.iter().enumerate() {
                    let attempt_number = index as u64 + 1;
                    if let Some(duration) = attempt.vsock_connect {
                        self.emit_runtime_readiness_segment(
                            &binding,
                            "readiness_vsock_connect",
                            duration,
                            attempt_number,
                            attempt.error_code,
                            runtime.as_ref(),
                        );
                    }
                    if let Some(duration) = attempt.connect_handshake {
                        self.emit_runtime_readiness_segment(
                            &binding,
                            "readiness_connect_handshake",
                            duration,
                            attempt_number,
                            attempt.error_code,
                            runtime.as_ref(),
                        );
                    }
                }
                self.emit_runtime_start_stage(
                    &binding,
                    "guest_readiness",
                    readiness.elapsed,
                    total_started.elapsed(),
                    Some(runtime.as_ref()),
                    Some(readiness.attempts),
                );
            }
            binding.state.lock().await.geometry = Some(geometry);
            Ok::<(), RuntimeStartError>(())
        }
        .await;
        if let Err(error) = started {
            let cleanup_error = runtime
                .cleanup(
                    "runtime failed before RuntimeStarted",
                    self.inner.request_timeout,
                )
                .await
                .err();
            if cleanup_error.is_some() && !matches!(runtime.has_exited(), Ok(true)) {
                // An uncertain/alive VMM may still use its FDs. Hand it back to the
                // instance so ordinary stop/cleanup can retry without losing leases.
                let mut state = instance.state.lock().await;
                state.pid = Some(runtime.pid());
                state.handles = Some(Arc::new(runtime));
                return Err(RuntimeStartError::CleanupFailed(format!(
                    "{error}; failed-start process exit is unconfirmed: {}",
                    cleanup_error.expect("checked above")
                )));
            }
            // run_start invokes the one cleanup owner, including release hooks.
            // Leave the binding installed until that entire cleanup succeeds.
            return Err(match cleanup_error {
                Some(cleanup) => RuntimeStartError::Vmm(format!(
                    "{error}; failed-start cleanup failed: {cleanup}"
                )),
                None => error,
            });
        }
        StartedRuntime::new(runtime.pid(), runtime).map_err(Into::into)
    }

    fn emit_runtime_start_stage(
        &self,
        binding: &RuntimeBinding,
        phase: &'static str,
        duration: Duration,
        total_duration: Duration,
        runtime: Option<&dyn VMMClientInterface>,
        attempts: Option<u64>,
    ) {
        let mut event = StructuredEvent::new(
            StructuredLogLevel::Info,
            StructuredEventName::RuntimeStartStageCompleted,
            StructuredComponent::Runtime,
            StructuredOutcome::Success,
        )
        .operation("start_runtime")
        .phase(phase)
        .snapshot_id(&binding.snapshot_id)
        .sandbox_id(&binding.runtime.sandbox_id)
        .duration(duration)
        .total_duration(total_duration);
        if let Some(runtime) = runtime {
            event = event.vmm_pid(runtime.pid());
            if let Some(endpoint) = runtime.guest_endpoint() {
                event = event
                    .host_port(endpoint.host_port)
                    .vsock_cid(endpoint.vsock_cid);
            }
        }
        if let Some(attempts) = attempts {
            event = event.attempts(attempts);
        }
        self.inner.runtime_index.emit_structured(event);
    }

    fn emit_runtime_readiness_segment(
        &self,
        binding: &RuntimeBinding,
        phase: &'static str,
        duration: Duration,
        attempt: u64,
        error_code: &'static str,
        runtime: &dyn VMMClientInterface,
    ) {
        let outcome = if error_code == "ok" {
            StructuredOutcome::Success
        } else {
            StructuredOutcome::Error
        };
        let mut event = StructuredEvent::new(
            StructuredLogLevel::Info,
            StructuredEventName::RuntimeStartStageCompleted,
            StructuredComponent::Runtime,
            outcome,
        )
        .operation("probe_guest_readiness")
        .phase(phase)
        .snapshot_id(&binding.snapshot_id)
        .sandbox_id(&binding.runtime.sandbox_id)
        .duration(duration)
        .duration_micros(duration)
        .attempts(attempt)
        .error_code(error_code)
        .vmm_pid(runtime.pid());
        if let Some(endpoint) = runtime.guest_endpoint() {
            event = event
                .host_port(endpoint.host_port)
                .vsock_cid(endpoint.vsock_cid);
        }
        self.inner.runtime_index.emit_structured(event);
    }

    async fn ensure_exit_watcher(&self, vm_id: &str) {
        let Some(instance) = self.instance(vm_id).await else {
            return;
        };
        if !instance.start_exit_watch().await {
            return;
        }
        let registry = self.clone();
        tokio::spawn(async move {
            loop {
                let Some(runtime) = instance.runtime_handles().await else {
                    break;
                };
                match runtime.has_exited() {
                    Ok(false) => tokio::time::sleep(Duration::from_millis(25)).await,
                    Ok(true) => {
                        registry
                            .runtime_exited_instance(instance, "VMM process exited")
                            .await;
                        break;
                    }
                    Err(error) => {
                        registry
                            .runtime_exited_instance(
                                instance,
                                &format!("VMM process monitoring failed: {error}"),
                            )
                            .await;
                        break;
                    }
                }
            }
        });
    }

    fn bind_runtime_source(
        &self,
        runtime_source: &SnapshotRuntimeSource,
        runtime: &RuntimeIdentity,
    ) -> Result<Arc<RuntimeBinding>, RuntimeStartError> {
        match runtime_source {
            SnapshotRuntimeSource::Prepared(prepared) => {
                bind_prepared(prepared, runtime, &self.inner.daemon_identity)
            }
            SnapshotRuntimeSource::Local(local) => {
                let mut local = local.clone();
                if let Some(cache) = self
                    .inner
                    .local_snapshot_backings
                    .get_snapshot(&local.snapshot.snapshot_id)?
                {
                    local.layer_backings = cache;
                }
                bind_local(&local, runtime, &self.inner.daemon_identity)
            }
        }
    }

    async fn submit_runtime_start(
        &self,
        control: Arc<dyn VmControlApi>,
        binding: &RuntimeBinding,
        launch: &VmmLaunchSpec,
        total_started: Instant,
    ) -> Result<Vec<LayerGeometry>, RuntimeStartError> {
        let correlation =
            stable_correlation("start-runtime", &binding.runtime, &binding.spec_sha256);
        let deadline_unix_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|error| RuntimeStartError::Vmm(error.to_string()))?
            .as_millis()
            .checked_add(self.inner.runtime_start_timeout.as_millis())
            .and_then(|value| u64::try_from(value).ok())
            .ok_or_else(|| RuntimeStartError::Invalid("runtime start deadline overflows".into()))?;
        let payload_started = Instant::now();
        let (request, fds) = build_start_runtime(
            binding,
            launch,
            &correlation,
            deadline_unix_ms,
            self.inner.required_capabilities.clone(),
        )
        .await?;
        // Preparation may refault backing bytes asynchronously. Check source
        // health again immediately before exporting the resulting descriptors.
        for snapshot in binding.lineage.frozen_snapshots() {
            snapshot
                .ensure_source_integrity()
                .map_err(|error| RuntimeStartError::Invalid(error.to_string()))?;
        }
        self.emit_runtime_start_stage(
            binding,
            "runtime_payload_prepare",
            payload_started.elapsed(),
            total_started.elapsed(),
            None,
            None,
        );
        let roundtrip_started = Instant::now();
        let received = match tokio::time::timeout(
            self.inner.runtime_start_timeout,
            control.request(request, fds),
        )
        .await
        {
            Ok(Ok(received)) => received,
            Ok(Err(error)) => {
                return Err(RuntimeStartError::Vmm(format!(
                    "StartRuntime result is unknown; terminate this VMM and do not resubmit: {error}"
                )))
            }
            Err(_) => {
                return Err(RuntimeStartError::Vmm(
                    "StartRuntime timed out with an unknown result; terminate this VMM and do not resubmit"
                        .into(),
                ))
            }
        };
        self.emit_runtime_start_stage(
            binding,
            "runtime_control_roundtrip",
            roundtrip_started.elapsed(),
            total_started.elapsed(),
            None,
            None,
        );
        if !received.fds.is_empty() {
            return protocol("RuntimeStarted unexpectedly carried descriptors");
        }
        match received.envelope.message {
            ControlReply::RuntimeStarted(started) => {
                let expected_targets = binding
                    .layers
                    .iter()
                    .map(|layer| layer.target_id.clone())
                    .collect::<Vec<_>>();
                let offered = started.capabilities.iter().cloned().collect::<HashSet<_>>();
                if started.sandbox_id != binding.runtime.sandbox_id
                    || started.snapshot_id != binding.snapshot_id
                    || started.attached_target_ids != expected_targets
                {
                    return protocol("VMM started a different sandbox, snapshot, or target set");
                }
                if self
                    .inner
                    .required_capabilities
                    .iter()
                    .any(|capability| !offered.contains(capability))
                {
                    return protocol("VMM does not provide all required runtime capabilities");
                }
                validate_ready_geometry(&started.geometry, binding)?;
                Ok(started.geometry)
            }
            ControlReply::Error(error) => {
                protocol(&format!("StartRuntime failed: {}", error.message))
            }
            _ => protocol("StartRuntime received an unexpected reply"),
        }
    }

    async fn reserve_submission(
        &self,
        submission: RuntimeSubmission,
    ) -> Result<(), RuntimeStartError> {
        if self
            .inner
            .one_shot_submissions
            .lock()
            .await
            .insert(submission)
        {
            Ok(())
        } else {
            Err(RuntimeStartError::IdentityConflict(
                "RuntimeControl operation was already submitted; its result must not be inferred or resubmitted"
                    .into(),
            ))
        }
    }
}

fn validate_batch(requests: &[RuntimeStartRequest]) -> Result<(), RuntimeStartError> {
    if requests.is_empty() || requests.len() > MAX_CHILDREN {
        return invalid_runtime("one launch batch must contain 1..=32 runtimes");
    }
    let mut vm_ids = HashSet::new();
    for request in requests {
        if !vm_ids.insert(&request.vm_id) {
            return invalid_runtime("runtime VM identities must be unique within one batch");
        }
    }
    Ok(())
}

fn runtime_start_result(
    _runtime: &RuntimeIdentity,
    result: crate::vm::repository::OperationResult,
) -> RuntimeStartResult {
    RuntimeStartResult {
        vm_id: result.vm_id,
        success: result.success,
        code: result.code.as_str().into(),
        message: result.message,
    }
}

fn joined_runtime_result(
    vm_id: String,
    result: Result<RuntimeStartResult, tokio::task::JoinError>,
) -> RuntimeStartResult {
    result.unwrap_or_else(|error| RuntimeStartResult {
        vm_id,
        success: false,
        code: "RESULT_UNKNOWN".into(),
        message: Some(format!("runtime launch task failed: {error}")),
    })
}

fn cancelled_result(request: &RuntimeStartRequest) -> RuntimeStartResult {
    RuntimeStartResult {
        vm_id: request.vm_id.clone(),
        success: false,
        code: "STOP_REQUESTED".into(),
        message: Some("runtime was stopped before VMM creation".into()),
    }
}

fn stable_correlation(operation: &str, runtime: &RuntimeIdentity, identity: &str) -> String {
    let value = format!("{operation}\0{}\0{identity}", runtime.sandbox_id);
    format!("{operation}-{:x}", Sha256::digest(value.as_bytes()))
}

pub(in crate::vm) fn target_id(
    daemon_identity: &str,
    runtime: &RuntimeIdentity,
    kind: ProtocolLayerKind,
    ordinal: u32,
) -> String {
    let kind = match kind {
        ProtocolLayerKind::Memory => "memory",
        ProtocolLayerKind::Block => "block",
    };
    let identity = format!(
        "{daemon_identity}\0{}\0{kind}\0{ordinal}",
        runtime.sandbox_id
    );
    format!("target-{:x}", Sha256::digest(identity.as_bytes()))
}

pub(in crate::vm) fn invalid_runtime<T>(message: &str) -> Result<T, RuntimeStartError> {
    Err(RuntimeStartError::Invalid(message.into()))
}

pub(in crate::vm) fn conflict<T>(message: &str) -> Result<T, RuntimeStartError> {
    Err(RuntimeStartError::IdentityConflict(message.into()))
}

pub(in crate::vm) fn protocol<T>(message: &str) -> Result<T, RuntimeStartError> {
    Err(RuntimeStartError::Protocol(message.into()))
}

#[cfg(all(test, target_os = "linux"))]
mod snapshot_boundary_tests {
    use super::*;
    use std::fs::OpenOptions;
    use std::os::fd::AsRawFd;
    use std::os::unix::fs::MetadataExt;

    use crate::snapshot::metadata::{
        sha256_identity, DataCodec, DataSource, LayerRange, SnapshotProvenance,
    };
    use crate::snapshot::repository::frozen_layer::{
        FrozenLayerMetadata, FrozenLayerPin, PackedOwnershipMap, BLOCK_OWNERSHIP_UNIT,
    };

    fn sparse_file() -> File {
        let path = std::env::temp_dir().join(format!("snapshot-adapter-{}", uuid::Uuid::new_v4()));
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
            .unwrap();
        std::fs::remove_file(path).unwrap();
        file.set_len(BLOCK_OWNERSHIP_UNIT).unwrap();
        file
    }

    fn source_fixture() -> (LocalSnapshotRuntimeSource, RemoteLayerSource) {
        source_fixture_named("local-lease")
    }

    fn source_fixture_named(snapshot_id: &str) -> (LocalSnapshotRuntimeSource, RemoteLayerSource) {
        let local = Snapshot::from_base_layer(
            snapshot_id.into(),
            FrozenLayerPin::new(
                FrozenLayerMetadata {
                    kind: SnapshotLayerKind::Block,
                    layer_id: format!("{snapshot_id}-block"),
                    provisional_content_id: sha256_identity(b"local-layer"),
                    logical_size: BLOCK_OWNERSHIP_UNIT,
                    alignment: BLOCK_OWNERSHIP_UNIT,
                },
                PackedOwnershipMap {
                    kind: SnapshotLayerKind::Block,
                    snapshot_id: snapshot_id.into(),
                    format_version: 1,
                    logical_size: BLOCK_OWNERSHIP_UNIT,
                    granularity: BLOCK_OWNERSHIP_UNIT,
                    packed_states: vec![2],
                },
                sparse_file().into(),
                false,
            ),
            SnapshotProvenance {
                oci_ref: None,
                oci_digest: None,
            },
        )
        .unwrap();
        let sparse = sparse_file();
        let readonly = File::open(format!("/proc/self/fd/{}", sparse.as_raw_fd())).unwrap();
        let source = RemoteLayerSource {
            ordinal: 0,
            layer_id: "remote-base".into(),
            range_map: Arc::new(LayerRangeMap {
                registry_objects: None,
                format_version: 1,
                kind: SnapshotLayerKind::Block,
                content_sha256: sha256_identity(b"remote-layer"),
                logical_bytes: BLOCK_OWNERSHIP_UNIT,
                unit_size_bytes: BLOCK_OWNERSHIP_UNIT,
                ranges: vec![LayerRange {
                    kind: RangeKind::Data,
                    logical_offset: 0,
                    length: BLOCK_OWNERSHIP_UNIT,
                    source: Some(DataSource {
                        object_key: "remote-data.pack".into(),
                        object_sha256: sha256_identity(b"remote-object"),
                        object_version: None,
                        pack_offset: 0,
                        encoded_length: BLOCK_OWNERSHIP_UNIT,
                        decoded_length: BLOCK_OWNERSHIP_UNIT,
                        codec: DataCodec::Raw,
                        checksum_sha256: sha256_identity(&vec![
                            0x5a;
                            BLOCK_OWNERSHIP_UNIT as usize
                        ]),
                    }),
                }],
                zfile: None,
                registry: None,
            }),
            backing: Arc::new(readonly.into()),
            shared_mapping: None,
            residency: SourceResidency::OnDemand,
            dynamic_residency: None,
        };
        (
            LocalSnapshotRuntimeSource::new(local, sha256_identity(b"local-spec")),
            source,
        )
    }

    #[test]
    fn missing_vm_attachment_lease_rebuilds_metadata_without_materializing_data() {
        let (runtime, source) = source_fixture();
        // DATA is deliberately absent. Its nonzero checksum cannot be verified
        // by reading this sparse file before a demand load.
        let before = File::from(source.backing.as_ref().try_clone().unwrap())
            .metadata()
            .unwrap();
        assert_eq!(before.blocks(), 0);
        let backing = runtime.remote_layer_backing(&source).unwrap();
        assert!(Arc::ptr_eq(&backing.backing, &source.backing));
        assert_eq!(backing.data_residency, DataResidency::RemoteLazy);
        let LayerCoveragePayload::Canonical { payload, range_map } = &backing.coverage else {
            panic!("canonical attachment")
        };
        assert!(payload.size > 0);
        assert!(Arc::ptr_eq(range_map, &source.range_map));
        let after = File::from(backing.backing.as_ref().try_clone().unwrap())
            .metadata()
            .unwrap();
        assert_eq!(
            (after.dev(), after.ino(), after.blocks()),
            (before.dev(), before.ino(), 0)
        );
    }

    #[test]
    fn local_snapshot_lease_reuses_original_attachment_until_explicit_release() {
        let (runtime, source) = source_fixture();
        let original = runtime.remote_layer_backing(&source).unwrap();
        let leases = LocalSnapshotBackingLeases::default();
        let cache = Arc::new(LocalLayerBackingCache::default());
        cache
            .retain_remote(&runtime.snapshot.snapshot_id, &source, original.clone())
            .unwrap();
        leases
            .retain_snapshot(&runtime.snapshot.snapshot_id, cache)
            .unwrap();
        let mut after_source_exit =
            LocalSnapshotRuntimeSource::new(runtime.snapshot.clone(), runtime.spec_sha256.clone());
        after_source_exit.layer_backings = leases
            .get_snapshot(&runtime.snapshot.snapshot_id)
            .unwrap()
            .unwrap();
        assert!(Arc::ptr_eq(
            &after_source_exit.remote_layer_backing(&source).unwrap(),
            &original
        ));
        leases.release_snapshot(&runtime.snapshot.snapshot_id);
        assert!(leases
            .get_snapshot(&runtime.snapshot.snapshot_id)
            .unwrap()
            .is_none());
        // An already-bound request remains usable; a later unleased request
        // uses its own cache instead of recreating a manager-owned lease.
        assert!(Arc::ptr_eq(
            &after_source_exit.remote_layer_backing(&source).unwrap(),
            &original
        ));
        let after_release =
            LocalSnapshotRuntimeSource::new(runtime.snapshot.clone(), runtime.spec_sha256.clone());
        let rebuilt = after_release.remote_layer_backing(&source).unwrap();
        assert!(!Arc::ptr_eq(&rebuilt, &original));
        assert!(Arc::ptr_eq(&rebuilt.backing, &original.backing));
    }

    #[test]
    fn descendant_rebinding_released_ancestor_does_not_resurrect_manager_fd_leases() {
        let leases = LocalSnapshotBackingLeases::default();
        let (ancestor, _) = source_fixture_named("ancestor-A");
        leases
            .retain_snapshot("ancestor-A", Arc::new(LocalLayerBackingCache::default()))
            .unwrap();
        leases.release_snapshot("ancestor-A");

        let (mut descendant, mut remote) = source_fixture_named("descendant-B");
        leases
            .retain_snapshot("descendant-B", Arc::new(LocalLayerBackingCache::default()))
            .unwrap();
        descendant.layer_backings = leases.get_snapshot("descendant-B").unwrap().unwrap();
        let active_frozen = descendant
            .layer_backing(
                &ancestor.snapshot,
                SnapshotLayerKind::Block,
                &ancestor.snapshot.frozen_layers[&SnapshotLayerKind::Block],
            )
            .unwrap();
        // The descendant's attachment group also owns a native memory mapping.
        // Its lifetime follows the same group, never the remote ancestor key.
        Arc::make_mut(&mut remote.range_map).kind = SnapshotLayerKind::Memory;
        Arc::make_mut(&mut remote.range_map).unit_size_bytes = 4096;
        remote.shared_mapping = Some(Arc::new(sparse_file().into()));
        let active_remote = descendant.remote_layer_backing(&remote).unwrap();
        let raw_fd = Arc::downgrade(&active_frozen.backing);
        let shared_fd = Arc::downgrade(active_remote.shared_mapping.as_ref().unwrap());
        assert!(leases.get_snapshot("ancestor-A").unwrap().is_none());

        leases.release_snapshot("descendant-B");
        assert!(leases.get_snapshot("descendant-B").unwrap().is_none());
        drop(descendant);
        drop(ancestor);
        drop(remote);
        // Releasing catalog ownership must not invalidate an active VM's FD.
        assert!(raw_fd.upgrade().is_some());
        assert!(shared_fd.upgrade().is_some());
        drop(active_frozen);
        drop(active_remote);
        assert!(
            raw_fd.upgrade().is_none(),
            "manager retained a released ancestor raw FD"
        );
        assert!(
            shared_fd.upgrade().is_none(),
            "manager retained a released shared mapping FD"
        );
    }
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct RuntimeLaunchPlan {
    pub sandbox_id: String,
    pub resource_spec: Value,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GuestEndpoint {
    pub transport: String,
    pub host_port: u16,
    pub guest_port: u32,
    pub vsock_cid: u32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RockletArtifact {
    pub identity: String,
    pub version: String,
    pub sha256: String,
    pub logical_bytes: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OperationAccepted {
    pub identity: String,
    pub state: String,
}

impl RuntimeLaunchPlan {
    pub fn validate(&self) -> Result<(), String> {
        require_nonempty("sandbox_id", &self.sandbox_id, 4096)?;
        require_object("resource_spec", &self.resource_spec)
    }
}

pub(in crate::vm) fn require_object(label: &str, value: &Value) -> Result<(), String> {
    if value.is_object() {
        Ok(())
    } else {
        Err(format!("{label} must be a JSON object"))
    }
}

pub(in crate::vm) fn require_nonempty(label: &str, value: &str, max: usize) -> Result<(), String> {
    if value.is_empty() || value.len() > max {
        Err(format!("{label} length must be 1..={max}"))
    } else {
        Ok(())
    }
}

pub(in crate::vm) fn validate_prefixed_sha256(value: &str) -> Result<(), String> {
    let Some(hex) = value.strip_prefix("sha256:") else {
        return Err("checksum must use sha256:<64 lowercase hex>".into());
    };
    validate_bare_sha256(hex)
}

fn validate_bare_sha256(value: &str) -> Result<(), String> {
    if value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        Ok(())
    } else {
        Err("checksum must contain 64 lowercase hex characters".into())
    }
}

impl ResidentRangeSource for WeakRuntimeBindingHandle {
    fn has_local_complete_layer(
        &self,
        kind: SnapshotLayerKind,
        ordinal: u32,
        layer_id: &str,
        content_sha256: &str,
    ) -> bool {
        WeakRuntimeBindingHandle::has_local_complete_layer(
            self,
            kind,
            ordinal,
            layer_id,
            content_sha256,
        )
    }

    fn read_local_complete_range(
        &self,
        kind: SnapshotLayerKind,
        ordinal: u32,
        layer_id: &str,
        content_sha256: &str,
        offset: u64,
        length: u64,
    ) -> std::io::Result<Option<Vec<u8>>> {
        WeakRuntimeBindingHandle::read_local_complete_range(
            self,
            kind,
            ordinal,
            layer_id,
            content_sha256,
            offset,
            length,
        )
        .map_err(|error| std::io::Error::other(error.to_string()))
    }
}

#[cfg(test)]
mod runtime_binding_ownership_tests {
    use super::*;

    fn empty_binding(runtime_id: &str) -> Arc<RuntimeBinding> {
        Arc::new(RuntimeBinding {
            snapshot_id: "ownership-fixture".into(),
            spec_sha256: "a".repeat(64),
            runtime: RuntimeIdentity::new(runtime_id),
            layers: Vec::new(),
            vm_state: None,
            lineage: SnapshotLineage::default(),
            expected_geometry: HashMap::new(),
            dependencies: RuntimeDependencies::new(),
            state: Mutex::new(RuntimeBindingState::default()),
        })
    }

    #[tokio::test]
    async fn runtime_index_does_not_extend_the_owning_instance_lifetime() {
        let index = RuntimeIndex::new(None);
        let instance = Arc::new(VmmInstance::<()>::new("same-vm".into()));
        let binding = empty_binding("runtime-epoch-1");
        let weak = Arc::downgrade(&binding);
        index.insert(&instance, binding).await.unwrap();
        assert!(instance.runtime_binding().await.is_some());
        assert!(weak.upgrade().is_some());

        drop(instance);
        assert!(
            weak.upgrade().is_none(),
            "only the instance may own the binding"
        );
        assert!(index
            .state
            .lock()
            .await
            .bindings
            .get("same-vm")
            .unwrap()
            .upgrade()
            .is_none());

        // A dead weak entry is harmless and must not reserve the sandbox ID.
        let replacement = VmmInstance::<()>::new("same-vm".into());
        let replacement_binding = empty_binding("runtime-epoch-2");
        let replacement_weak = Arc::downgrade(&replacement_binding);
        index
            .insert(&replacement, replacement_binding)
            .await
            .unwrap();
        assert!(replacement.runtime_binding().await.is_some());
        drop(replacement);
        assert!(replacement_weak.upgrade().is_none());
    }

    #[tokio::test]
    async fn runtime_release_detaches_instance_without_invalidating_existing_borrowers() {
        let index = RuntimeIndex::new(None);
        let instance = VmmInstance::<()>::new("release-vm".into());
        let binding = empty_binding("release-runtime");
        let runtime = binding.runtime.clone();
        let weak = Arc::downgrade(&binding);
        index.insert(&instance, Arc::clone(&binding)).await.unwrap();

        assert!(index.release(&instance, &runtime).await.is_none());
        assert!(instance.runtime_binding().await.is_none());
        assert!(!index.state.lock().await.bindings.contains_key("release-vm"));
        assert!(
            weak.upgrade().is_some(),
            "the caller's in-flight borrow is still valid"
        );
        drop(binding);
        assert!(weak.upgrade().is_none());
        assert!(index.release(&instance, &runtime).await.is_none());
    }

    #[tokio::test]
    async fn late_old_instance_release_preserves_same_id_replacement_index() {
        let index = RuntimeIndex::new(None);
        let old_instance = VmmInstance::<()>::new("reused-vm".into());
        let old_binding = empty_binding("old-runtime");
        let old_runtime = old_binding.runtime.clone();
        let old_weak = Arc::downgrade(&old_binding);
        index.insert(&old_instance, old_binding).await.unwrap();

        // Model the repository publishing a replacement while a detached old
        // instance's asynchronous resource cleanup has not finished yet.
        index.state.lock().await.bindings.remove("reused-vm");
        let new_instance = VmmInstance::<()>::new("reused-vm".into());
        let new_binding = empty_binding("new-runtime");
        let new_runtime = new_binding.runtime.clone();
        let new_weak = Arc::downgrade(&new_binding);
        index
            .insert(&new_instance, Arc::clone(&new_binding))
            .await
            .unwrap();

        assert!(index.release(&old_instance, &old_runtime).await.is_none());
        assert!(old_instance.runtime_binding().await.is_none());
        assert!(old_weak.upgrade().is_none());
        let current = index
            .state
            .lock()
            .await
            .bindings
            .get("reused-vm")
            .and_then(Weak::upgrade)
            .expect("old cleanup must not remove the replacement");
        assert!(Arc::ptr_eq(&current, &new_binding));
        drop(current);
        let owner = new_instance.runtime_binding().await.unwrap();
        assert!(Arc::ptr_eq(&owner, &new_binding));
        drop(owner);

        index.release(&new_instance, &new_runtime).await;
        drop(new_binding);
        assert!(new_weak.upgrade().is_none());
        assert!(new_instance.runtime_binding().await.is_none());
    }

    #[tokio::test]
    async fn wrong_runtime_identity_cannot_release_current_instance_binding() {
        let index = RuntimeIndex::new(None);
        let instance = VmmInstance::<()>::new("epoch-vm".into());
        let binding = empty_binding("current-runtime-epoch");
        let current_runtime = binding.runtime.clone();
        index.insert(&instance, Arc::clone(&binding)).await.unwrap();

        assert!(index
            .release(&instance, &RuntimeIdentity::new("previous-runtime-epoch"))
            .await
            .is_none());
        let owned = instance.runtime_binding().await.unwrap();
        assert!(Arc::ptr_eq(&owned, &binding));
        drop(owned);
        let indexed = index
            .state
            .lock()
            .await
            .bindings
            .get("epoch-vm")
            .and_then(Weak::upgrade)
            .unwrap();
        assert!(Arc::ptr_eq(&indexed, &binding));
        drop(indexed);

        index.release(&instance, &current_runtime).await;
        assert!(instance.runtime_binding().await.is_none());
        assert!(!index.state.lock().await.bindings.contains_key("epoch-vm"));
    }

    #[tokio::test]
    async fn rejected_duplicate_binding_does_not_replace_owner_or_retain_candidate() {
        let index = RuntimeIndex::new(None);
        let current = VmmInstance::<()>::new("occupied-vm".into());
        let original = empty_binding("accepted-runtime");
        let accepted_runtime = original.runtime.clone();
        let original_weak = Arc::downgrade(&original);
        index.insert(&current, original).await.unwrap();

        let candidate_instance = VmmInstance::<()>::new("occupied-vm".into());
        let candidate = empty_binding("rejected-runtime");
        let candidate_weak = Arc::downgrade(&candidate);
        assert!(matches!(
            index.insert(&candidate_instance, candidate).await,
            Err(RuntimeStartError::IdentityConflict(_))
        ));
        assert!(candidate_instance.runtime_binding().await.is_none());
        assert!(candidate_weak.upgrade().is_none());
        assert!(current.runtime_binding().await.is_some());
        assert!(index
            .state
            .lock()
            .await
            .bindings
            .get("occupied-vm")
            .unwrap()
            .ptr_eq(&original_weak));

        index.release(&current, &accepted_runtime).await;
        assert!(original_weak.upgrade().is_none());
    }
}
#[cfg(test)]
mod promotion_resource_ownership_tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;

    use crate::vm::protocol::ProtocolError;
    use crate::vm::runtime::protocol::PublishedGenerationFinalized;

    #[derive(Clone, Copy)]
    enum PromotionReply {
        Ack,
        Reject,
        Pending,
    }

    struct PromotionControl {
        reply: PromotionReply,
        calls: AtomicUsize,
        entered: Notify,
    }

    #[async_trait::async_trait]
    impl VmControlApi for PromotionControl {
        async fn request(
            &self,
            request: WireEnvelope<ControlRequest>,
            fds: Vec<OwnedFd>,
        ) -> Result<ReceivedVmmMessage<ControlReply>, VmmClientError> {
            assert!(
                fds.is_empty(),
                "this fixture does not send actual descriptors"
            );
            let ControlRequest::FinalizePublishedGeneration(finalize) = request.message else {
                panic!("the fixture only accepts finalization requests")
            };
            self.calls.fetch_add(1, Ordering::SeqCst);
            self.entered.notify_one();
            let message = match self.reply {
                PromotionReply::Ack => {
                    ControlReply::PublishedGenerationFinalized(PublishedGenerationFinalized {
                        capture_correlation_id: finalize.capture_correlation_id,
                        capture_request_sha256: finalize.capture_request_sha256,
                        snapshot_id: finalize.snapshot_id,
                        spec_sha256: finalize.spec_sha256,
                    })
                }
                PromotionReply::Reject => ControlReply::Error(ProtocolError {
                    code: ErrorCode::InvalidArgument,
                    message: "fixture explicitly rejected before commit".into(),
                    retryable: false,
                }),
                PromotionReply::Pending => std::future::pending().await,
            };
            Ok(ReceivedVmmMessage {
                envelope: WireEnvelope {
                    header: request.header,
                    message,
                },
                fds: Vec::new(),
            })
        }
    }

    struct PromotionRuntime {
        control: Arc<PromotionControl>,
    }

    #[async_trait::async_trait]
    impl VMMClientInterface for PromotionRuntime {
        fn pid(&self) -> u32 {
            123
        }
        fn control(&self) -> Arc<dyn VmControlApi> {
            self.control.clone()
        }
        fn has_exited(&self) -> Result<bool, VmmControllerError> {
            Ok(false)
        }
        async fn cleanup(
            &self,
            _reason: &str,
            _timeout: Duration,
        ) -> Result<(), VmmControllerError> {
            Ok(())
        }
    }

    struct PromotionFixture {
        repository: VmRepository<Box<dyn VMMClientInterface>>,
        instance: Arc<VmmInstance<Box<dyn VMMClientInterface>>>,
        runtime: RuntimeIdentity,
        control: Arc<PromotionControl>,
    }

    impl PromotionFixture {
        async fn new(reply: PromotionReply, timeout: Duration) -> Self {
            let repository = VmRepository::new(
                None,
                Vec::new(),
                Duration::from_secs(1),
                timeout,
                1,
                Duration::from_secs(1),
            )
            .unwrap();
            let control = Arc::new(PromotionControl {
                reply,
                calls: AtomicUsize::new(0),
                entered: Notify::new(),
            });
            let instance = Arc::new(VmmInstance::new("promotion-vm".into()));
            {
                let mut state = instance.state.lock().await;
                state.lifecycle_state = VmmLifecycleState::Running;
                state.pid = Some(123);
                state.handles = Some(Arc::new(Box::new(PromotionRuntime {
                    control: Arc::clone(&control),
                }) as Box<dyn VMMClientInterface>));
            }
            repository
                .inner
                .state
                .lock()
                .await
                .instances
                .insert(instance.vm_id().into(), Arc::clone(&instance));
            let runtime = RuntimeIdentity::new("promotion-runtime");
            let binding = Arc::new(RuntimeBinding {
                snapshot_id: "base".into(),
                spec_sha256: "a".repeat(64),
                runtime: runtime.clone(),
                layers: Vec::new(),
                vm_state: None,
                lineage: SnapshotLineage::default(),
                expected_geometry: HashMap::new(),
                dependencies: RuntimeDependencies::new(),
                state: Mutex::new(RuntimeBindingState::default()),
            });
            repository
                .inner
                .runtime_index
                .insert(instance.as_ref(), binding)
                .await
                .unwrap();
            Self {
                repository,
                instance,
                runtime,
                control,
            }
        }

        fn prepared(&self) -> (PreparedFinalization, Weak<LayerResources>) {
            // The resource has a real owned FD, but no loader registration or
            // wire payload is needed to exercise the actual control method.
            let file = tempfile::tempfile().unwrap();
            file.set_len(65536).unwrap();
            let handle =
                crate::snapshot::io::backing::LocalBackingHandle::from_native(FrozenLayerPin::new(
                    FrozenLayerMetadata {
                        kind: SnapshotLayerKind::Block,
                        layer_id: "fixture".into(),
                        provisional_content_id: "native-fixture".into(),
                        logical_size: 65536,
                        alignment: 65536,
                    },
                    PackedOwnershipMap {
                        kind: SnapshotLayerKind::Block,
                        snapshot_id: "fixture".into(),
                        format_version: 1,
                        logical_size: 65536,
                        granularity: 65536,
                        packed_states: vec![1],
                    },
                    file.into(),
                    false,
                ))
                .unwrap();
            let resource = LayerResources::new_native(handle, None);
            let weak = Arc::downgrade(&resource);
            (
                PreparedFinalization {
                    layers: Vec::new(),
                    fd_roles: Vec::new(),
                    fds: Vec::new(),
                    created_resources: Vec::new(),
                    resources: vec![resource],
                    runtime: self.runtime.clone(),
                    _native_readers: Vec::new(),
                },
                weak,
            )
        }

        async fn promote(&self, prepared: &PreparedFinalization) -> Result<(), RuntimeStartError> {
            self.repository
                .promote_published_snapshot(
                    self.instance.vm_id(),
                    "one-capture-operation",
                    &"b".repeat(64),
                    "published-snapshot",
                    &"c".repeat(64),
                    prepared,
                )
                .await
        }

        async fn release(&self) {
            self.repository
                .inner
                .runtime_index
                .release(self.instance.as_ref(), &self.runtime)
                .await;
        }
    }

    #[tokio::test]
    async fn acknowledged_promotion_keeps_resources_after_prepared_drops_until_runtime_release() {
        let fixture = PromotionFixture::new(PromotionReply::Ack, Duration::from_secs(1)).await;
        let (prepared, weak) = fixture.prepared();
        fixture.promote(&prepared).await.unwrap();
        drop(prepared);
        assert!(weak.upgrade().unwrap().readonly_file().is_ok());
        assert_eq!(fixture.control.calls.load(Ordering::SeqCst), 1);

        fixture.release().await;
        assert!(fixture.instance.runtime_binding().await.is_none());
        assert!(weak.upgrade().is_none());
    }

    #[tokio::test]
    async fn explicit_promotion_rejection_releases_staged_resources_without_releasing_runtime() {
        let fixture = PromotionFixture::new(PromotionReply::Reject, Duration::from_secs(1)).await;
        let (prepared, weak) = fixture.prepared();
        assert!(matches!(
            fixture.promote(&prepared).await,
            Err(RuntimeStartError::Protocol(_))
        ));
        drop(prepared);
        assert!(weak.upgrade().is_none());
        assert!(fixture.instance.runtime_binding().await.is_some());
        assert_eq!(fixture.control.calls.load(Ordering::SeqCst), 1);
        fixture.release().await;
    }

    #[tokio::test]
    async fn promotion_timeout_keeps_unknown_resources_and_does_not_allow_resubmission() {
        let fixture =
            PromotionFixture::new(PromotionReply::Pending, Duration::from_millis(30)).await;
        let (prepared, weak) = fixture.prepared();
        assert!(matches!(
            fixture.promote(&prepared).await,
            Err(RuntimeStartError::FinalizationUnknown(_))
        ));
        drop(prepared);
        assert!(weak.upgrade().unwrap().readonly_file().is_ok());

        let (retry, retry_weak) = fixture.prepared();
        assert!(matches!(
            fixture.promote(&retry).await,
            Err(RuntimeStartError::IdentityConflict(_))
        ));
        drop(retry);
        assert!(retry_weak.upgrade().is_none());
        assert_eq!(fixture.control.calls.load(Ordering::SeqCst), 1);
        fixture.release().await;
        assert!(weak.upgrade().is_none());
    }

    #[tokio::test]
    async fn cancelling_submitted_promotion_preserves_resources_until_runtime_release() {
        let fixture = PromotionFixture::new(PromotionReply::Pending, Duration::from_secs(30)).await;
        let (prepared, weak) = fixture.prepared();
        let repository = fixture.repository.clone();
        let vm_id = fixture.instance.vm_id().to_owned();
        let task = tokio::spawn(async move {
            repository
                .promote_published_snapshot(
                    &vm_id,
                    "one-capture-operation",
                    &"b".repeat(64),
                    "published-snapshot",
                    &"c".repeat(64),
                    &prepared,
                )
                .await
        });
        tokio::time::timeout(Duration::from_secs(2), fixture.control.entered.notified())
            .await
            .expect("the mock control must observe submission before cancellation");
        task.abort();
        assert!(task.await.unwrap_err().is_cancelled());
        assert!(weak.upgrade().unwrap().readonly_file().is_ok());
        assert_eq!(fixture.control.calls.load(Ordering::SeqCst), 1);

        let (retry, retry_weak) = fixture.prepared();
        assert!(matches!(
            fixture.promote(&retry).await,
            Err(RuntimeStartError::IdentityConflict(_))
        ));
        drop(retry);
        assert!(retry_weak.upgrade().is_none());
        assert_eq!(fixture.control.calls.load(Ordering::SeqCst), 1);
        fixture.release().await;
        assert!(weak.upgrade().is_none());
    }

    #[tokio::test]
    async fn duplicate_promotion_reservation_does_not_attach_additional_resources() {
        let fixture = PromotionFixture::new(PromotionReply::Ack, Duration::from_secs(1)).await;
        let (original, original_weak) = fixture.prepared();
        fixture.promote(&original).await.unwrap();
        drop(original);

        let (duplicate, duplicate_weak) = fixture.prepared();
        assert!(matches!(
            fixture.promote(&duplicate).await,
            Err(RuntimeStartError::IdentityConflict(_))
        ));
        drop(duplicate);
        assert!(duplicate_weak.upgrade().is_none());
        assert!(original_weak.upgrade().is_some());
        assert_eq!(fixture.control.calls.load(Ordering::SeqCst), 1);

        fixture.release().await;
        assert!(original_weak.upgrade().is_none());
    }

    // Insert inside vm/repository.rs::promotion_resource_ownership_tests.
    // Uses the existing PromotionFixture/Runtime; cleanup() returns Ok without
    // spawning a VMM, while the real repository cleanup callback and index run.
    #[tokio::test]
    async fn failed_release_hook_keeps_binding_and_resources_for_successful_cleanup_retry() {
        let fixture = PromotionFixture::new(PromotionReply::Ack, Duration::from_secs(1)).await;
        let (prepared, resource) = fixture.prepared();
        fixture.promote(&prepared).await.unwrap();
        drop(prepared);
        let attempts = Arc::new(AtomicUsize::new(0));
        let expected_runtime = fixture.runtime.clone();
        fixture
            .repository
            .register_release_hook(Arc::new({
                let attempts = Arc::clone(&attempts);
                move |runtime| {
                    let attempts = Arc::clone(&attempts);
                    let expected_runtime = expected_runtime.clone();
                    Box::pin(async move {
                        assert_eq!(runtime, expected_runtime);
                        if attempts.fetch_add(1, Ordering::SeqCst) == 0 {
                            Err("release-hook cleanup is temporarily unavailable".into())
                        } else {
                            Ok(())
                        }
                    })
                }
            }))
            .unwrap();

        let first = fixture
            .repository
            .cleanup_once(Arc::clone(&fixture.instance), false)
            .await;
        assert!(first.unwrap_err().contains("temporarily unavailable"));
        assert_eq!(attempts.load(Ordering::SeqCst), 1);
        assert!(!*fixture.instance.cleanup_complete.lock().await);
        assert!(fixture.instance.runtime_binding().await.is_some());
        assert!(fixture.instance.runtime_handles().await.is_some());
        assert!(resource.upgrade().unwrap().readonly_file().is_ok());
        assert!(
            fixture
                .repository
                .instance(fixture.instance.vm_id())
                .await
                .is_some(),
            "failed cleanup must leave the exact instance available to stop retry"
        );
        assert!(
            fixture
                .repository
                .inner
                .runtime_index
                .state
                .lock()
                .await
                .bindings
                .get(fixture.instance.vm_id())
                .and_then(Weak::upgrade)
                .is_some(),
            "failed release hook must not detach the binding's weak lookup entry"
        );

        fixture
            .repository
            .cleanup_once(Arc::clone(&fixture.instance), false)
            .await
            .unwrap();
        assert_eq!(
            attempts.load(Ordering::SeqCst),
            2,
            "retry must execute the failed release hook, not skip it after losing runtime identity"
        );
        assert!(*fixture.instance.cleanup_complete.lock().await);
        assert!(fixture.instance.runtime_binding().await.is_none());
        assert!(fixture.instance.runtime_handles().await.is_none());
        assert!(fixture
            .repository
            .instance(fixture.instance.vm_id())
            .await
            .is_none());
        assert!(resource.upgrade().is_none());
        assert!(!fixture
            .repository
            .inner
            .runtime_index
            .state
            .lock()
            .await
            .bindings
            .contains_key(fixture.instance.vm_id()));
    }

    #[tokio::test]
    async fn rejected_promotion_keeps_a_reused_resource_owned_by_an_earlier_commit() {
        let fixture = PromotionFixture::new(PromotionReply::Ack, Duration::from_secs(1)).await;
        let (original, weak) = fixture.prepared();
        fixture.promote(&original).await.unwrap();
        let (mut rejected, _) = fixture.prepared();
        rejected.resources = original.resources.clone();
        drop(original);
        let rejection = Arc::new(PromotionControl {
            reply: PromotionReply::Reject,
            calls: AtomicUsize::new(0),
            entered: Notify::new(),
        });
        fixture.instance.state.lock().await.handles = Some(Arc::new(Box::new(PromotionRuntime {
            control: rejection.clone(),
        })
            as Box<dyn VMMClientInterface>));
        assert!(matches!(
            fixture
                .repository
                .promote_published_snapshot(
                    fixture.instance.vm_id(),
                    "different-capture",
                    &"b".repeat(64),
                    "published-snapshot",
                    &"c".repeat(64),
                    &rejected,
                )
                .await,
            Err(RuntimeStartError::Protocol(_))
        ));
        drop(rejected);
        assert!(
            weak.upgrade().unwrap().readonly_file().is_ok(),
            "rejecting one submission must preserve an earlier committed lease"
        );
        assert_eq!(rejection.calls.load(Ordering::SeqCst), 1);
        fixture.release().await;
        assert!(weak.upgrade().is_none());
    }
}

#[cfg(test)]
mod native_p2p_vm_acceptance_tests {
    use super::*;

    use std::os::unix::fs::FileExt;

    use crate::endpoints::p2p::handler::P2pService;
    use crate::snapshot::metadata::{validate_sha256, LayerKind, SnapshotProvenance};
    use crate::snapshot::repository::frozen_layer::{
        FrozenLayerMetadata, FrozenLayerPin, PackedOwnershipMap,
    };
    use crate::snapshot::transport::p2p::native::{
        NativeCoverageRequest, NativeLayerIdentityKind, NativeReadRangeRequest,
    };
    use crate::snapshot::transport::p2p::{NativeP2PClient, NativeP2PError};
    use crate::snapshot::transport::posix_file;

    const UNIT: u64 = 65536;

    fn source_snapshot(id: &str, byte: u8) -> Arc<Snapshot> {
        let file = posix_file::memory_file(c"vm-native-peer", 2 * UNIT).unwrap();
        file.write_all_at(&vec![byte; UNIT as usize], 0).unwrap();
        // ZERO coverage must synthesize zeroes without exposing stale bytes in
        // the backing or consulting another layer/provider.
        file.write_all_at(&vec![0xee; UNIT as usize], UNIT).unwrap();
        Snapshot::from_base_layer(
            id.into(),
            FrozenLayerPin::new(
                FrozenLayerMetadata {
                    kind: LayerKind::Block,
                    layer_id: "native-block".into(),
                    provisional_content_id: "capture:native-block".into(),
                    logical_size: 2 * UNIT,
                    alignment: UNIT,
                },
                PackedOwnershipMap {
                    kind: LayerKind::Block,
                    snapshot_id: id.into(),
                    format_version: 1,
                    logical_size: 2 * UNIT,
                    granularity: UNIT,
                    packed_states: vec![0b1001],
                },
                file.into(),
                false,
            ),
            SnapshotProvenance {
                oci_ref: None,
                oci_digest: None,
            },
        )
        .unwrap()
    }

    fn service() -> Arc<VmService> {
        let repository = VmRepository::new(
            None,
            Vec::new(),
            Duration::from_secs(30),
            Duration::from_secs(1),
            1,
            Duration::from_secs(1),
        )
        .unwrap();
        Arc::new(VmService::new(
            repository,
            Arc::new(SnapshotCaptureService::new(Duration::from_secs(1)).unwrap()),
            Arc::new(|_| panic!("native peer serving must not launch a VM or origin loader")),
        ))
    }

    #[tokio::test]
    async fn enabled_automatic_upload_does_not_block_legacy_local_start_identity() {
        use crate::snapshot::io::snapshot_io::{SnapshotDataUploadConfig, SnapshotPublisher};
        use crate::snapshot::services::background_uploader::SnapshotUploadTargetResolver;
        use std::sync::atomic::{AtomicUsize, Ordering};

        struct RequireSource(AtomicUsize);
        #[async_trait::async_trait]
        impl SnapshotUploadTargetResolver for RequireSource {
            async fn resolve(
                &self,
            ) -> Result<ResolvedSnapshotUploadTarget, SnapshotUploadServiceError> {
                panic!("automatic repository selection must inspect the image source");
            }

            async fn resolve_for(
                &self,
                provenance: Option<&SnapshotProvenance>,
            ) -> Result<ResolvedSnapshotUploadTarget, SnapshotUploadServiceError> {
                self.0.fetch_add(1, Ordering::SeqCst);
                assert!(provenance
                    .and_then(|source| source.oci_ref.as_ref())
                    .is_none());
                Err(SnapshotUploadServiceError::Invalid(
                    "automatic OCI upload requires the original source image reference".into(),
                ))
            }
        }

        let base = Arc::try_unwrap(service()).ok().unwrap();
        let resolver = Arc::new(RequireSource(AtomicUsize::new(0)));
        let upload = SnapshotUploadService::new(
            SnapshotPublisher::new(SnapshotDataUploadConfig::default(), None),
            resolver.clone(),
            Duration::from_millis(1),
            Duration::from_millis(2),
        )
        .unwrap()
        .with_catalog(Arc::clone(&base.catalog));
        assert!(upload.enabled());
        let service = Arc::new(base.with_upload(Arc::clone(&upload)));
        let legacy = source_snapshot("legacy-without-image-source", 0x6a);
        service.catalog.register_raw(Arc::clone(&legacy)).unwrap();
        let expected = local_runtime_spec_sha256(&legacy, None).unwrap();
        assert_eq!(
            service.local_start_spec_checksum(&legacy).await.unwrap(),
            expected
        );
        assert_eq!(resolver.0.load(Ordering::SeqCst), 0);
        assert!(matches!(
            service.upload_preflight_for(legacy.provenance()).await,
            Err(SnapshotControlError::Invalid(message))
                if message.contains("original source image reference")
        ));
        assert_eq!(resolver.0.load(Ordering::SeqCst), 1);
        assert_eq!(legacy.state(), PublicationState::LocalReady);
        assert_eq!(
            service.local_start_spec_checksum(&legacy).await.unwrap(),
            expected
        );
        assert_eq!(resolver.0.load(Ordering::SeqCst), 1);
        upload.close().await.unwrap();
    }

    #[tokio::test]
    async fn disabled_upload_keeps_local_runtime_identity_status_and_native_peer_reads_usable() {
        use crate::snapshot::io::snapshot_io::{SnapshotDataUploadConfig, SnapshotPublisher};
        use crate::snapshot::services::background_uploader::SnapshotUploadTargetResolver;

        struct NoWriteTarget;
        #[async_trait::async_trait]
        impl SnapshotUploadTargetResolver for NoWriteTarget {
            async fn resolve(
                &self,
            ) -> Result<ResolvedSnapshotUploadTarget, SnapshotUploadServiceError> {
                panic!("disabled local capture must not resolve an upload target");
            }
        }
        let base = Arc::try_unwrap(service()).ok().unwrap();
        let upload = SnapshotUploadService::new(
            SnapshotPublisher::new(SnapshotDataUploadConfig::default(), None),
            Arc::new(NoWriteTarget),
            Duration::from_millis(1),
            Duration::from_millis(2),
        )
        .unwrap()
        .with_enabled(false)
        .with_catalog(Arc::clone(&base.catalog));
        let service = Arc::new(base.with_upload(Arc::clone(&upload)));
        let source = source_snapshot("disabled-local-cut", 0x6a);
        let record: Arc<dyn SnapshotInstance> =
            service.catalog.register_raw(Arc::clone(&source)).unwrap();
        service.state.lock().await.jobs.insert(
            source.snapshot_id.clone(),
            SnapshotJob {
                source_sandbox_id: "source".into(),
                source_stopped: false,
                snapshot: Arc::downgrade(&record),
                last_error: None,
            },
        );
        let target = service.upload_preflight_for(None).await.unwrap();
        assert!(target.is_none());
        service
            .register_upload(
                Arc::clone(&source),
                CaptureSnapshot {
                    snapshot_id: source.snapshot_id.clone(),
                },
                target,
            )
            .await
            .unwrap();
        let checksum = service.local_start_spec_checksum(&source).await.unwrap();
        assert_eq!(checksum.len(), 64);
        assert_eq!(
            checksum,
            service.local_start_spec_checksum(&source).await.unwrap()
        );
        let status = service.snapshot_status(source.snapshot_id()).await.unwrap();
        assert_eq!(status.state, PublicationState::LocalReady);
        assert!(!status.remote_publishing);
        assert_eq!(status.attempt_count, 0);
        assert!(status.last_error.is_none());
        assert!(status.retry_at_unix_ms.is_none());
        assert!(status.catalog_pin_held);
        let metadata = service
            .p2p_native_metadata(source.snapshot_id(), None)
            .await
            .unwrap();
        let layer = &metadata.layers[0];
        assert_eq!(
            service
                .p2p_native_read_range(
                    source.snapshot_id(),
                    &NativeReadRangeRequest {
                        generation_id: metadata.generation_id.clone(),
                        kind: layer.kind,
                        ordinal: layer.ordinal,
                        layer_id: layer.layer_id.clone(),
                        layer_generation_id: layer.layer_generation_id.clone(),
                        offset: 3,
                        length: 11,
                    }
                )
                .await
                .unwrap(),
            [0x6a; 11]
        );
        upload.close().await.unwrap();
        assert!(source.native_is_locally_ready());
    }

    #[tokio::test]
    async fn inconsistent_native_cut_rejects_new_clone_and_existing_peer_tokens_but_retains_raw() {
        use crate::snapshot::metadata::{StorageDescriptor, StorageProvider, VmmCompatibility};
        use crate::snapshot::repository::LocalSnapshotError;

        let service = service();
        let source = source_snapshot("inconsistent-native-cut", 0x4d);
        service.catalog.register_raw(Arc::clone(&source)).unwrap();
        let backing = source.backing_handle(LayerKind::Block).unwrap();
        let alias = backing.backing.try_clone_file().unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let endpoint = P2pService::new(Arc::clone(&service), None);
        let server = tokio::spawn(async move {
            loop {
                let (stream, _) = listener.accept().await.unwrap();
                let endpoint = Arc::clone(&endpoint);
                tokio::spawn(async move {
                    endpoint.serve_connection(stream).await.unwrap();
                });
            }
        });
        let timeout = Duration::from_secs(2);
        let (mut old_reader, metadata) =
            NativeP2PClient::connect(address, timeout, source.snapshot_id(), None)
                .await
                .unwrap();
        let (mut old_coverage, _) = NativeP2PClient::connect(
            address,
            timeout,
            source.snapshot_id(),
            Some(&metadata.generation_id),
        )
        .await
        .unwrap();
        let layer = &metadata.layers[0];
        let request = NativeReadRangeRequest {
            generation_id: metadata.generation_id.clone(),
            kind: layer.kind,
            ordinal: layer.ordinal,
            layer_id: layer.layer_id.clone(),
            layer_generation_id: layer.layer_generation_id.clone(),
            offset: 7,
            length: 11,
        };
        assert_eq!(old_reader.read_range(&request).await.unwrap(), [0x4d; 11]);
        // Cache a local binding before quarantine as well: reuse must perform
        // a new admission check, rather than trusting retained raw descriptors.
        let cached = LocalSnapshotRuntimeSource::new(Arc::clone(&source), sha256_identity(b"spec"));
        assert!(bind_local(
            &cached,
            &RuntimeIdentity::new("before-quarantine"),
            "fixture"
        )
        .is_ok());
        let directory = tempfile::tempdir().unwrap();
        let cache =
            posix_file::LocalFileCache::new(directory.path().to_owned(), "quarantine").unwrap();
        let error = source
            .upload_plan_with_scratch(
                StorageDescriptor {
                    provider: StorageProvider::S3,
                    endpoint_profile: "fixture".into(),
                    credential_ref: "fixture:v1".into(),
                },
                VmmCompatibility {
                    architecture: "x86_64".into(),
                    vmm_type: "any".into(),
                    vmm_state_format_version: None,
                    rocklet_artifact: None,
                },
                &|capacity| {
                    // Deterministically change source DATA between the encoder's
                    // checksum pass and its payload pass, without any live VM.
                    alias.write_all_at(&[0x6b], 17).unwrap();
                    cache.create_staged_object(capacity).map_err(|error| {
                        crate::snapshot::repository::frozen_layer::FrozenLayerError::Io(
                            error.to_string(),
                        )
                    })
                },
            )
            .unwrap_err();
        assert!(matches!(error, LocalSnapshotError::SourceIntegrity(_)));
        assert_eq!(source.state(), PublicationState::Failed);
        assert!(source.is_closed().unwrap());
        assert!(!source.native_is_locally_ready());
        assert!(service.local_snapshot(source.snapshot_id()).await.is_err());
        assert!(bind_local(
            &cached,
            &RuntimeIdentity::new("after-quarantine"),
            "fixture"
        )
        .is_err());
        assert!(matches!(
            service
                .start_local_checkpoint(
                    source.snapshot_id(),
                    StartFromSnapshot {
                        sandbox_id: "rejected-clone".into(),
                        snapshot_spec: serde_json::json!({}),
                        spec_checksum: sha256_identity(b"spec"),
                        resource_spec: serde_json::json!({}),
                        p2p_source_worker_ip: None,
                        local_snapshot: true,
                    },
                    now_unix_ms() + 2000,
                    None,
                )
                .await,
            Err(SnapshotControlError::InvalidState(_))
        )); // The fixture's launch factory panics if admission reaches it.
        assert!(old_reader.read_range(&request).await.is_err());
        assert!(old_coverage
            .coverage(&NativeCoverageRequest {
                generation_id: request.generation_id.clone(),
                kind: request.kind,
                ordinal: request.ordinal,
                layer_id: request.layer_id.clone(),
                layer_generation_id: request.layer_generation_id.clone(),
                offset: 0,
                length: 2 * UNIT,
            })
            .await
            .is_err());
        assert!(NativeP2PClient::connect(
            address,
            timeout,
            source.snapshot_id(),
            Some(&metadata.generation_id),
        )
        .await
        .is_err());
        assert!(service
            .p2p_serve
            .native_metadata(source.snapshot_id(), Some(&metadata.generation_id))
            .await
            .is_err());
        assert!(
            !service
                .p2p_serve
                .native_availability(source.snapshot_id(), &metadata.generation_id, false)
                .await
                .available
        );
        let retained = service
            .catalog
            .get(source.snapshot_id())
            .unwrap()
            .downcast::<Snapshot>()
            .unwrap();
        assert!(Arc::ptr_eq(&retained, &source));
        assert!(backing.backing.reclaim_unused().await.unwrap().is_empty());
        assert!(!backing.backing.has_reclaimed_data());
        let mut bytes = [0; 32];
        alias.read_exact_at(&mut bytes, 0).unwrap();
        let mut expected = [0x4d; 32];
        expected[17] = 0x6b;
        assert_eq!(bytes, expected);
        assert_eq!(alias.metadata().unwrap().len(), 2 * UNIT);
        assert!(service.state.lock().await.jobs.is_empty());
        server.abort();
        assert!(server.await.unwrap_err().is_cancelled());
    }

    #[tokio::test]
    async fn local_ready_native_peer_serves_real_tcp_without_upload_or_remote_services() {
        let service = service();
        assert!(service.upload.is_none());
        assert!(service.remote_loader.is_none());
        assert!(service.remote_preparer.is_none());
        let source = source_snapshot("catalog-only-native", 0x4d);
        service.catalog.register_raw(Arc::clone(&source)).unwrap();
        assert_eq!(source.state(), PublicationState::LocalReady);
        assert!(source.spec().is_none());

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let endpoint = P2pService::new(Arc::clone(&service), None);
        let server = tokio::spawn(async move {
            loop {
                let (stream, _) = listener.accept().await.unwrap();
                let endpoint = Arc::clone(&endpoint);
                tokio::spawn(async move {
                    endpoint.serve_connection(stream).await.unwrap();
                });
            }
        });
        let timeout = Duration::from_secs(2);
        let (mut client, metadata) =
            NativeP2PClient::connect(address, timeout, source.snapshot_id(), None)
                .await
                .unwrap();
        metadata.validate().unwrap();
        assert_eq!(metadata.layers.len(), 1);
        let layer = &metadata.layers[0];
        assert_eq!(
            layer.identity_kind,
            NativeLayerIdentityKind::NativeGeneration
        );
        assert!(layer.content_identity.starts_with("native-generation:"));
        assert!(validate_sha256(&layer.content_identity).is_err());
        let coverage = client
            .coverage(&NativeCoverageRequest {
                generation_id: metadata.generation_id.clone(),
                kind: layer.kind,
                ordinal: layer.ordinal,
                layer_id: layer.layer_id.clone(),
                layer_generation_id: layer.layer_generation_id.clone(),
                offset: 0,
                length: 2 * UNIT,
            })
            .await
            .unwrap();
        assert_eq!(coverage.packed_states, [0b1001]);
        assert_eq!(coverage.unit_size, UNIT);
        let request = NativeReadRangeRequest {
            generation_id: metadata.generation_id.clone(),
            kind: layer.kind,
            ordinal: layer.ordinal,
            layer_id: layer.layer_id.clone(),
            layer_generation_id: layer.layer_generation_id.clone(),
            offset: UNIT - 13,
            length: 32,
        };
        let mut expected = vec![0x4d; 13];
        expected.extend_from_slice(&[0; 19]);
        assert_eq!(client.read_range(&request).await.unwrap(), expected);
        assert_eq!(
            client
                .read_range(&NativeReadRangeRequest {
                    offset: 7,
                    length: 11,
                    ..request.clone()
                })
                .await
                .unwrap(),
            [0x4d; 11]
        );
        assert_eq!(
            client
                .read_range(&NativeReadRangeRequest {
                    offset: UNIT + 7,
                    length: 11,
                    ..request.clone()
                })
                .await
                .unwrap(),
            [0; 11]
        );

        let forged_generation = uuid::Uuid::new_v4().to_string();
        assert!(matches!(
            NativeP2PClient::connect(
                address,
                timeout,
                source.snapshot_id(),
                Some(&forged_generation),
            )
            .await,
            Err(NativeP2PError::Identity(_))
        ));
        let forged = NativeReadRangeRequest {
            generation_id: forged_generation,
            ..request.clone()
        };
        assert!(matches!(
            client.read_range(&forged).await,
            Err(NativeP2PError::Identity(_))
        ));
        assert!(matches!(
            service
                .p2p_native_read_range(source.snapshot_id(), &forged)
                .await,
            Err(SnapshotControlError::IdentityConflict(_))
        ));
        assert_eq!(source.state(), PublicationState::LocalReady);
        assert!(source.spec().is_none());
        assert!(service.state.lock().await.jobs.is_empty());
        let backing = source.backing_handle(LayerKind::Block).unwrap();
        assert_eq!(backing.backing.dependency_counts(0).unwrap().remaining(), 0);
        assert!(
            backing.backing.reclaim_unused().await.unwrap().is_empty(),
            "native peer discovery/reads must not create a sealed encoding/recovery source"
        );

        source
            .lifecycle()
            .lock()
            .unwrap()
            .fail("upload", "injected terminal upload failure");
        assert_eq!(source.state(), PublicationState::Failed);
        let (mut failed_upload_client, failed_upload_metadata) = NativeP2PClient::connect(
            address,
            timeout,
            source.snapshot_id(),
            Some(&metadata.generation_id),
        )
        .await
        .unwrap();
        assert_eq!(failed_upload_metadata.generation_id, metadata.generation_id);
        assert_eq!(
            failed_upload_client.read_range(&request).await.unwrap(),
            expected
        );
        assert_eq!(source.state(), PublicationState::Failed);
        drop(failed_upload_client);

        // A catalog snapshot has no source VM or upload service to keep alive.
        // Invalidating its peer registry fences even a still-live backing Arc.
        service.p2p_serve.invalidate(source.snapshot_id()).await;
        service.catalog.invalidate(source.snapshot_id());
        assert!(matches!(
            client.read_range(&request).await,
            Err(NativeP2PError::Miss(_))
        ));
        let locks = service.snapshot_locks.lock().await.len();
        for number in 0..4 {
            assert!(matches!(
                NativeP2PClient::connect(
                    address,
                    timeout,
                    format!("unknown-native-{number}"),
                    None,
                )
                .await,
                Err(NativeP2PError::Miss(_))
            ));
        }
        assert_eq!(service.snapshot_locks.lock().await.len(), locks);

        let replacement = source_snapshot(source.snapshot_id(), 0x7a);
        service
            .catalog
            .register_raw(Arc::clone(&replacement))
            .unwrap();
        let (mut next, next_metadata) =
            NativeP2PClient::connect(address, timeout, replacement.snapshot_id(), None)
                .await
                .unwrap();
        assert_ne!(metadata.generation_id, next_metadata.generation_id);
        assert_ne!(
            layer.layer_generation_id,
            next_metadata.layers[0].layer_generation_id
        );
        assert!(matches!(
            client.read_range(&request).await,
            Err(NativeP2PError::Identity(_)) | Err(NativeP2PError::Miss(_))
        ));
        let next_layer = &next_metadata.layers[0];
        assert_eq!(
            next.read_range(&NativeReadRangeRequest {
                generation_id: next_metadata.generation_id.clone(),
                kind: next_layer.kind,
                ordinal: next_layer.ordinal,
                layer_id: next_layer.layer_id.clone(),
                layer_generation_id: next_layer.layer_generation_id.clone(),
                offset: 3,
                length: 17,
            })
            .await
            .unwrap(),
            [0x7a; 17]
        );
        assert!(service.state.lock().await.jobs.is_empty());
        drop(next);
        drop(client);
        server.abort();
        assert!(server.await.unwrap_err().is_cancelled());
    }
}
