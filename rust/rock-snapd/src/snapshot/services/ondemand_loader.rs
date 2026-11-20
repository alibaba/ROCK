//! Authorized snapshot DATA loading for CLH demand requests and prefetch.
//!
//! Runtime grants, backing materialization and task lifetimes belong here.
//! Snapshot/format readers use shared RangeIO for physical byte delivery.

use crate::snapshot::io::range_io::{
    P2PSource, RangeReadError, RangeReadFailureClass, RangeReader,
};
#[cfg(test)]
use crate::snapshot::io::range_io::{RangeDataKind, RangeReadRequest};
use crate::RuntimeIdentity;
use serde::{Deserialize, Serialize};

// Shared authorized loading for demand and prefetch policies.
use std::collections::{HashMap, HashSet};
use std::fs::File;
#[cfg(test)]
use std::fs::{self, OpenOptions};
use std::io;
use std::os::fd::{AsFd, AsRawFd};
use std::os::unix::fs::FileExt;
use std::os::unix::fs::MetadataExt;
#[cfg(test)]
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex as StdMutex, Weak};
use std::time::{Duration, Instant};

use sha2::{Digest, Sha256};
use thiserror::Error;
use tokio::sync::{Mutex, Notify, OwnedSemaphorePermit, Semaphore};

use crate::observability::metrics::{DataKind, DownloadOutcomeMetric, SnapdMetrics};
use crate::observability::read_path::{ReadPathStage, ReadPathTimer};
use crate::observability::structured_log::{
    StructuredComponent, StructuredEvent, StructuredEventName, StructuredLogLevel,
    StructuredOutcome,
};
use crate::snapshot::io::backing::{
    BackingError, DataRangeKey, DataRangeResidencyRegistry, DataRangeStatus, DownloadState,
    LayerBackingIdentity, LayerKind, LocalBackingRegistry, RangeError, RangeLoadOutcome,
};
pub use crate::snapshot::io::layer_resources::CredentialIdentity;
use crate::snapshot::io::layer_resources::{LayerResources, RemoteLayerIdentity};
use crate::snapshot::metadata::{
    DataCodec, LayerRange, LayerRangeMap, RangeKind, SnapshotHotRange,
};
use crate::snapshot::services::background_prefetcher::SnapshotHotsetService;

#[cfg(test)]
use crate::snapshot::io::snapshot_io::SnapshotIo;
use crate::snapshot::io::snapshot_io::SnapshotReader;
use crate::snapshot::metadata::{LazyLoadContext, LazyLoadError, SnapshotReadRange};
use crate::snapshot::LazyLoadInterface;

use crate::snapshot::services::background_prefetcher as background;

pub const DEFAULT_MAX_RANGE_BYTES: u64 = 16 * 1024 * 1024;
pub const MAX_REMOTE_LOAD_GRANTS_PER_BATCH: usize = 8 * 1024;
const MEMORY_COALESCED_READ_BYTES: u64 = 256 * 1024;
const DEFAULT_PROVIDER_LOAD_TIMEOUT: Duration = Duration::from_secs(30);
const CLEANUP_TIMEOUT_SLACK: Duration = Duration::from_secs(1);

#[derive(Debug)]
pub(crate) struct RemoteLoadAdmissionController {
    semaphore: Arc<Semaphore>,
    active: AtomicUsize,
    max_active: AtomicUsize,
}

impl RemoteLoadAdmissionController {
    pub(crate) fn new(limit: usize) -> Arc<Self> {
        Arc::new(Self {
            semaphore: Arc::new(Semaphore::new(limit)),
            active: AtomicUsize::new(0),
            max_active: AtomicUsize::new(0),
        })
    }

    pub(crate) fn begin_run(
        self: &Arc<Self>,
        metrics: Arc<SnapdMetrics>,
        snapshot_id: &str,
        layer_id: &str,
        runtime: &RuntimeIdentity,
    ) -> (Arc<RemoteLoadAdmissionRun>, RemoteLoadCallerGuard) {
        let run = Arc::new(RemoteLoadAdmissionRun {
            metrics,
            snapshot_id: snapshot_id.to_owned(),
            layer_id: layer_id.to_owned(),
            runtime: runtime.clone(),
            started: Instant::now(),
            state: StdMutex::new(RemoteLoadAdmissionRunState::default()),
        });
        (
            Arc::clone(&run),
            RemoteLoadCallerGuard {
                run,
                returned: false,
            },
        )
    }

    pub(crate) async fn acquire(
        self: &Arc<Self>,
        run: Arc<RemoteLoadAdmissionRun>,
    ) -> Result<RemoteLoadAdmission, RemoteLoadError> {
        let permit = Arc::clone(&self.semaphore)
            .acquire_owned()
            .await
            .map_err(|_| RemoteLoadError::Closed)?;
        let active = self.active.fetch_add(1, Ordering::AcqRel) + 1;
        self.max_active.fetch_max(active, Ordering::AcqRel);
        if !run.accept(active) {
            let previous = self.active.fetch_sub(1, Ordering::AcqRel);
            debug_assert!(previous > 0);
            drop(permit);
            return Err(RemoteLoadError::Closed);
        }
        Ok(RemoteLoadAdmission {
            permit: Some(permit),
            controller: Arc::clone(self),
            run,
            released: false,
            finished: false,
        })
    }
}

#[derive(Debug)]
pub(crate) struct RemoteLoadAdmission {
    permit: Option<OwnedSemaphorePermit>,
    controller: Arc<RemoteLoadAdmissionController>,
    run: Arc<RemoteLoadAdmissionRun>,
    released: bool,
    finished: bool,
}

#[derive(Debug)]
pub(crate) struct RemoteLoadCallerGuard {
    run: Arc<RemoteLoadAdmissionRun>,
    returned: bool,
}

impl RemoteLoadCallerGuard {
    pub(crate) fn returned(&mut self) {
        self.returned = true;
    }
}

impl Drop for RemoteLoadCallerGuard {
    fn drop(&mut self) {
        self.run.finish_caller(!self.returned);
    }
}

#[derive(Debug)]
struct RemoteLoadAdmissionRunState {
    accepted: usize,
    drained: usize,
    node_active_at_start: Option<usize>,
    node_max_active: usize,
    caller_finished: bool,
    caller_cancelled: bool,
    all_success: bool,
    first_error_code: Option<&'static str>,
    emitted: bool,
}

impl Default for RemoteLoadAdmissionRunState {
    fn default() -> Self {
        Self {
            accepted: 0,
            drained: 0,
            node_active_at_start: None,
            node_max_active: 0,
            caller_finished: false,
            caller_cancelled: false,
            all_success: true,
            first_error_code: None,
            emitted: false,
        }
    }
}

#[derive(Debug)]
pub(crate) struct RemoteLoadAdmissionRun {
    metrics: Arc<SnapdMetrics>,
    snapshot_id: String,
    layer_id: String,
    runtime: RuntimeIdentity,
    started: Instant,
    state: StdMutex<RemoteLoadAdmissionRunState>,
}

#[derive(Clone, Copy, Debug)]
pub(crate) struct RemoteLoadAdmissionObservation {
    pub(crate) node_active_at_start: usize,
    pub(crate) node_max_active: usize,
    pub(crate) caller_cancelled: bool,
    pub(crate) accepted_drained: bool,
}

impl Default for RemoteLoadAdmissionObservation {
    fn default() -> Self {
        Self {
            node_active_at_start: 0,
            node_max_active: 0,
            caller_cancelled: false,
            // A run that accepted no work is vacuously drained.
            accepted_drained: true,
        }
    }
}

impl RemoteLoadAdmissionRun {
    fn accept(&self, node_active: usize) -> bool {
        let mut state = self.state.lock().expect("prefetch run lock poisoned");
        if state.caller_finished {
            return false;
        }
        state.accepted += 1;
        state.node_active_at_start.get_or_insert(node_active);
        state.node_max_active = state.node_max_active.max(node_active);
        true
    }

    fn finish_admitted(&self, success: bool, error_code: Option<&'static str>) {
        {
            let mut state = self.state.lock().expect("prefetch run lock poisoned");
            state.drained += 1;
            state.all_success &= success;
            if state.first_error_code.is_none() {
                state.first_error_code = error_code;
            }
        }
        self.emit_if_drained();
    }

    fn finish_caller(&self, cancelled: bool) {
        {
            let mut state = self.state.lock().expect("prefetch run lock poisoned");
            if state.caller_finished {
                return;
            }
            state.caller_finished = true;
            state.caller_cancelled = cancelled;
        }
        self.emit_if_drained();
    }

    fn emit_if_drained(&self) {
        let (accepted, active_at_start, max_active, caller_cancelled, success, error_code) = {
            let mut state = self.state.lock().expect("prefetch run lock poisoned");
            if state.emitted || !state.caller_finished || state.drained != state.accepted {
                return;
            }
            state.emitted = true;
            (
                state.accepted,
                state.node_active_at_start.unwrap_or(0),
                state.node_max_active,
                state.caller_cancelled,
                state.all_success && !state.caller_cancelled,
                state.first_error_code,
            )
        };
        // The normal completion path folds these fields into the single
        // latest_memory_prefetch_completed summary.  A cancelled caller can
        // no longer emit that summary, so retain one asynchronous terminal
        // event only for the cancellation case.
        if !caller_cancelled {
            return;
        }
        let mut event = StructuredEvent::new(
            if success {
                StructuredLogLevel::Info
            } else {
                StructuredLogLevel::Warning
            },
            StructuredEventName::LatestMemoryPrefetchAdmissionCompleted,
            StructuredComponent::RemoteLoad,
            if success {
                StructuredOutcome::Success
            } else {
                StructuredOutcome::Error
            },
        )
        .operation("latest_memory_prefetch_admission")
        .phase("drained")
        .snapshot_id(&self.snapshot_id)
        .layer_id(&self.layer_id)
        .sandbox_id(&self.runtime.sandbox_id)
        .duration_micros(self.started.elapsed())
        .attempts(accepted.try_into().unwrap_or(u64::MAX))
        .prefetch_admission(
            active_at_start.try_into().unwrap_or(u64::MAX),
            max_active.try_into().unwrap_or(u64::MAX),
            caller_cancelled,
            true,
        );
        if let Some(error_code) = error_code {
            event = event.error_code(error_code);
        }
        self.metrics.emit_structured(event);
    }

    pub(crate) fn observation(&self) -> RemoteLoadAdmissionObservation {
        let state = self.state.lock().expect("prefetch run lock poisoned");
        RemoteLoadAdmissionObservation {
            node_active_at_start: state.node_active_at_start.unwrap_or(0),
            node_max_active: state.node_max_active,
            caller_cancelled: state.caller_cancelled,
            accepted_drained: state.caller_finished && state.drained == state.accepted,
        }
    }
}

impl RemoteLoadAdmission {
    fn finish(mut self, success: bool, error_code: Option<&'static str>) {
        self.finished = true;
        self.run.finish_admitted(success, error_code);
        self.release();
    }

    fn release(&mut self) {
        if self.released {
            return;
        }
        self.released = true;
        let previous = self.controller.active.fetch_sub(1, Ordering::AcqRel);
        debug_assert!(previous > 0);
        self.permit.take();
    }
}

impl Drop for RemoteLoadAdmission {
    fn drop(&mut self) {
        if !self.finished {
            self.run.finish_admitted(false, Some("worker_cancelled"));
            self.finished = true;
        }
        self.release();
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RemoteRangeSource {
    pub content_id: String,
    pub object_key: String,
    pub object_offset: u64,
    pub encoded_length: u64,
    pub checksum_sha256: String,
    pub codec: String,
}

impl RemoteRangeSource {
    pub fn validate(&self) -> Result<(), RemoteLoadError> {
        if self.content_id.is_empty() || self.object_key.is_empty() || self.codec.is_empty() {
            return Err(RemoteLoadError::InvalidArgument(
                "remote source identity must not be empty".into(),
            ));
        }
        if self.encoded_length == 0 {
            return Err(RemoteLoadError::InvalidArgument(
                "encoded_length must be positive".into(),
            ));
        }
        if self.codec == "registry-rootfs-v1" {
            if !self.checksum_sha256.is_empty() {
                return Err(RemoteLoadError::InvalidArgument(
                    "registry-rootfs-v1 must not claim a DATA SHA checksum".into(),
                ));
            }
        } else if self.checksum_sha256.len() != 64
            || !self
                .checksum_sha256
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit())
        {
            return Err(RemoteLoadError::InvalidArgument(
                "checksum_sha256 must contain 64 hexadecimal characters".into(),
            ));
        }
        Ok(())
    }
}

impl CredentialIdentity {
    fn validate(&self) -> Result<(), RemoteLoadError> {
        if [
            &self.endpoint_profile,
            &self.credential_ref,
            &self.client_identity,
        ]
        .iter()
        .any(|value| value.is_empty())
        {
            return Err(RemoteLoadError::InvalidArgument(
                "credential authorization identity is incomplete".into(),
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RemoteLoadGrant {
    pub runtime: RuntimeIdentity,
    pub target_id: String,
    pub backing_key: LayerBackingIdentity,
    pub target_offset: u64,
    pub length: u64,
    pub source: RemoteRangeSource,
    pub provider: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub object_version: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub authorization: Option<CredentialIdentity>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub p2p_source: Option<P2PSource>,
}

/// Target-level capability.  It intentionally exists independently from a
/// DATA range so an all-ZERO layer still owns exactly one sparse backing handle.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RemoteLayerTargetGrant {
    pub runtime: RuntimeIdentity,
    pub target_id: String,
    pub backing_key: LayerBackingIdentity,
    pub provider: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub authorization: Option<CredentialIdentity>,
}

impl RemoteLayerTargetGrant {
    fn validate(&self) -> Result<(), RemoteLoadError> {
        self.backing_key.validate()?;
        if self.runtime.sandbox_id.is_empty()
            || self.target_id.is_empty()
            || self.provider.is_empty()
        {
            return Err(RemoteLoadError::InvalidArgument(
                "target grant identity must not be empty".into(),
            ));
        }
        if let Some(identity) = &self.authorization {
            identity.validate()?;
        }
        Ok(())
    }
}

impl RemoteLoadGrant {
    pub fn validate(&self, max_range_bytes: u64) -> Result<(), RemoteLoadError> {
        self.backing_key.validate()?;
        self.source.validate()?;
        if self.runtime.sandbox_id.is_empty()
            || self.target_id.is_empty()
            || self.provider.is_empty()
        {
            return Err(RemoteLoadError::InvalidArgument(
                "grant identity must not be empty".into(),
            ));
        }
        if self.length == 0
            || self.length > max_range_bytes
            || self.source.encoded_length > max_range_bytes
        {
            return Err(RemoteLoadError::InvalidArgument(
                "RemoteLoad grant exceeds configured range limit".into(),
            ));
        }
        let end = self
            .target_offset
            .checked_add(self.length)
            .ok_or_else(|| RemoteLoadError::InvalidArgument("target range overflows".into()))?;
        if end > self.backing_key.logical_size
            || !self
                .target_offset
                .is_multiple_of(self.backing_key.unit_size)
            || !self.length.is_multiple_of(self.backing_key.unit_size)
        {
            return Err(RemoteLoadError::InvalidArgument(
                "target range is out of bounds or unaligned".into(),
            ));
        }
        if matches!(self.source.codec.as_str(), "raw" | "registry-rootfs-v1")
            && self.source.encoded_length != self.length
        {
            return Err(RemoteLoadError::InvalidArgument(
                "raw encoded length must equal target length".into(),
            ));
        }
        if let Some(identity) = &self.authorization {
            identity.validate()?;
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RemoteLoadRequest {
    pub target_id: String,
    pub target_offset: u64,
    pub length: u64,
    pub origin: String,
}

impl RemoteLoadRequest {
    fn validate(&self) -> Result<(), RemoteLoadError> {
        if self.target_id.is_empty() || self.origin.is_empty() || self.length == 0 {
            return Err(RemoteLoadError::InvalidArgument(
                "request identity and range must not be empty".into(),
            ));
        }
        self.target_offset
            .checked_add(self.length)
            .ok_or_else(|| RemoteLoadError::InvalidArgument("request range overflows".into()))?;
        Ok(())
    }
}

#[derive(Debug, Error)]
pub enum RemoteLoadError {
    #[error("invalid argument: {0}")]
    InvalidArgument(String),
    #[error("target was not granted")]
    TargetNotFound,
    #[error("remote-load identity conflicts with an existing capability")]
    IdentityConflict,
    #[error("runtime identity was revoked")]
    RuntimeRevoked,
    #[error("remote-load service is closed")]
    Closed,
    #[error("provider is not configured")]
    ProviderNotConfigured,
    #[error(transparent)]
    Provider(#[from] RangeReadError),
    #[error("provider returned a short object range")]
    ShortRead,
    #[error("payload checksum mismatch")]
    ChecksumMismatch,
    #[error("unsupported codec")]
    UnsupportedCodec,
    #[error("backing write made no progress")]
    WriteNoProgress,
    #[error("backing write failed: {0}")]
    Write(#[from] io::Error),
    #[error(transparent)]
    Backing(#[from] BackingError),
    #[error(transparent)]
    Layer(#[from] crate::snapshot::io::backing::LayerBackingIdentityError),
    #[error(transparent)]
    Range(#[from] RangeError),
    #[error("remote-load worker terminated: {0}")]
    Worker(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ResidentRangeSummary {
    pub servable_objects: usize,
    pub complete: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RemoteLoadResult {
    pub outcome: RangeLoadOutcome,
    pub resident_offset: u64,
    pub resident_length: u64,
}

/// Exact immutable object range whose target bytes are already resident.
///
/// This is a read-only residency snapshot for P2P availability. Producing it
/// never invokes a provider and does not pin the runtime or its backing.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub(crate) struct ResidentObjectRange {
    pub object_key: String,
    pub object_sha256: String,
    pub object_version: Option<String>,
    pub offset: u64,
    pub length: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub(in crate::snapshot) struct TargetRangeKey {
    target_id: String,
    offset: u64,
    length: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct BackingRangeKey {
    backing: LayerBackingIdentity,
    offset: u64,
    length: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct MemoryWindowKey {
    runtime: RuntimeIdentity,
    target_id: String,
    provider: String,
    object_version: Option<String>,
    content_id: String,
    object_key: String,
    window_start: u64,
    target_source_delta: i128,
}

/// Cancellation-safe pin for an operation that has already incremented a
/// target's active-load count.  Dropping an async future releases the pin just
/// like ordinary completion, so revoke cannot wait forever on an aborted
/// resident read or service monitor.
struct ActiveLoadGuard {
    service: Arc<RangeLoadExecutor>,
    target: Arc<LayerResources>,
}

impl ActiveLoadGuard {
    fn from_incremented(service: Arc<RangeLoadExecutor>, target: Arc<LayerResources>) -> Self {
        Self { service, target }
    }
}

impl Drop for ActiveLoadGuard {
    fn drop(&mut self) {
        self.service.finish_active_load(&self.target);
    }
}

#[derive(Debug, Clone)]
pub(in crate::snapshot) struct GrantedRange {
    pub(in crate::snapshot) grant: RemoteLoadGrant,
    pub(in crate::snapshot) layer_target: Arc<LayerResources>,
}

/// An index entry grants access but never keeps an attachment alive.
#[derive(Debug, Clone)]
pub(in crate::snapshot) struct IndexedRangeGrant {
    pub(in crate::snapshot) grant: RemoteLoadGrant,
    layer_target: Weak<LayerResources>,
}

impl IndexedRangeGrant {
    pub(in crate::snapshot) fn upgrade(&self) -> Option<GrantedRange> {
        Some(GrantedRange {
            grant: self.grant.clone(),
            layer_target: self.layer_target.upgrade()?,
        })
    }
}

#[derive(Debug)]
struct PendingGrant {
    grant: RemoteLoadGrant,
    target_key: TargetRangeKey,
    range_key: BackingRangeKey,
    memory_window: Option<MemoryWindowKey>,
}

#[derive(Debug, Clone)]
pub(in crate::snapshot) struct CoalescedRange {
    grants: Vec<RemoteLoadGrant>,
}

impl CoalescedRange {
    pub(in crate::snapshot) fn first(&self) -> &RemoteLoadGrant {
        self.grants
            .first()
            .expect("coalesced range must contain the requested grant")
    }

    pub(in crate::snapshot) fn length(&self) -> u64 {
        self.grants.iter().map(|grant| grant.length).sum()
    }
}

#[derive(Default)]
pub(in crate::snapshot) struct ServiceState {
    pub(in crate::snapshot) targets: HashMap<TargetRangeKey, IndexedRangeGrant>,
    memory_windows: HashMap<MemoryWindowKey, Vec<TargetRangeKey>>,
    pub(in crate::snapshot) layer_targets: HashMap<String, Weak<LayerResources>>,
    range_identities: HashMap<BackingRangeKey, RemoteRangeSource>,
    pub(in crate::snapshot) revoked_runtimes: HashSet<RuntimeIdentity>,
    runtime_cleanups: HashSet<RuntimeIdentity>,
    pub(in crate::snapshot) closed: bool,
}

impl ServiceState {
    pub(in crate::snapshot) fn live_layers(
        &self,
    ) -> impl Iterator<Item = (&String, Arc<LayerResources>)> {
        self.layer_targets
            .iter()
            .filter_map(|(id, layer)| Some((id, layer.upgrade()?)))
    }

    fn remove_expired_target(&mut self, target_id: &str) {
        if self
            .layer_targets
            .get(target_id)
            .and_then(Weak::upgrade)
            .is_some()
        {
            return;
        }
        self.layer_targets.remove(target_id);
        self.targets.retain(|key, _| key.target_id != target_id);
        self.memory_windows
            .retain(|key, _| key.target_id != target_id);
    }
}

pub struct RangeLoadExecutor {
    pub(in crate::snapshot) snapshot_reader: Arc<dyn SnapshotReader>,
    pub(in crate::snapshot) backing_registry: Arc<LocalBackingRegistry>,
    providers: HashMap<String, Arc<dyn RangeReader>>,
    max_range_bytes: u64,
    provider_load_timeout: Duration,
    pub(in crate::snapshot) metrics: Arc<SnapdMetrics>,
    pub(in crate::snapshot) hotsets: SnapshotHotsetService,
    pub(in crate::snapshot) background: background::BackgroundPrefetch,
    pub(in crate::snapshot) demand_loads: AtomicUsize,
    pub(in crate::snapshot) state: Mutex<ServiceState>,
    activity_changed: Notify,
}

impl std::fmt::Debug for RangeLoadExecutor {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("RangeLoadExecutor")
            .field("provider_count", &self.providers.len())
            .field("max_range_bytes", &self.max_range_bytes)
            .field("provider_load_timeout", &self.provider_load_timeout)
            .finish_non_exhaustive()
    }
}

impl RangeLoadExecutor {
    pub fn metrics(&self) -> Arc<SnapdMetrics> {
        Arc::clone(&self.metrics)
    }

    pub fn new(
        backing_registry: Arc<LocalBackingRegistry>,
        snapshot_reader: Arc<dyn SnapshotReader>,
        providers: HashMap<String, Arc<dyn RangeReader>>,
        max_range_bytes: u64,
    ) -> Result<Arc<Self>, RemoteLoadError> {
        let metrics = SnapdMetrics::new("test")
            .map_err(|error| RemoteLoadError::InvalidArgument(error.to_string()))?;
        Self::new_with_metrics(
            backing_registry,
            snapshot_reader,
            providers,
            max_range_bytes,
            metrics,
        )
    }

    pub fn new_with_metrics(
        backing_registry: Arc<LocalBackingRegistry>,
        snapshot_reader: Arc<dyn SnapshotReader>,
        providers: HashMap<String, Arc<dyn RangeReader>>,
        max_range_bytes: u64,
        metrics: Arc<SnapdMetrics>,
    ) -> Result<Arc<Self>, RemoteLoadError> {
        Self::new_with_metrics_and_timeout(
            backing_registry,
            snapshot_reader,
            providers,
            max_range_bytes,
            DEFAULT_PROVIDER_LOAD_TIMEOUT,
            metrics,
        )
    }

    pub fn new_with_metrics_and_timeout(
        backing_registry: Arc<LocalBackingRegistry>,
        snapshot_reader: Arc<dyn SnapshotReader>,
        providers: HashMap<String, Arc<dyn RangeReader>>,
        max_range_bytes: u64,
        provider_load_timeout: Duration,
        metrics: Arc<SnapdMetrics>,
    ) -> Result<Arc<Self>, RemoteLoadError> {
        if max_range_bytes == 0 || provider_load_timeout.is_zero() {
            return Err(RemoteLoadError::InvalidArgument(
                "range limit and provider timeout must be positive".into(),
            ));
        }
        let hotset_root = backing_registry
            .cache_root()
            .with_file_name("snapshot-hotsets");
        let hotsets = SnapshotHotsetService::new(hotset_root).with_metrics(Arc::clone(&metrics));
        Ok(Arc::new(Self {
            backing_registry,
            snapshot_reader,
            providers,
            max_range_bytes,
            provider_load_timeout,
            metrics,
            hotsets,
            background: background::BackgroundPrefetch::from_env()?,
            demand_loads: AtomicUsize::new(0),
            state: Mutex::new(ServiceState::default()),
            activity_changed: Notify::new(),
        }))
    }

    pub async fn grant_target(
        self: &Arc<Self>,
        grant: RemoteLoadGrant,
    ) -> Result<(Arc<LayerResources>, bool), RemoteLoadError> {
        grant.validate(self.max_range_bytes)?;
        if grant.source.codec == "registry-rootfs-v1" {
            return Err(RemoteLoadError::UnsupportedCodec);
        }
        let mut state = self.state.lock().await;
        if state.closed {
            return Err(RemoteLoadError::Closed);
        }
        if state.revoked_runtimes.contains(&grant.runtime) {
            return Err(RemoteLoadError::RuntimeRevoked);
        }
        let target_grant = RemoteLayerTargetGrant {
            runtime: grant.runtime.clone(),
            target_id: grant.target_id.clone(),
            backing_key: grant.backing_key.clone(),
            provider: grant.provider.clone(),
            authorization: grant.authorization.clone(),
        };
        let existing_layer = self
            .prepare_target_locked(&mut state, &target_grant, None)
            .await?;
        let target_key = TargetRangeKey {
            target_id: grant.target_id.clone(),
            offset: grant.target_offset,
            length: grant.length,
        };
        if let Some(existing) = state.targets.get(&target_key) {
            return if existing.grant == grant {
                Ok((existing_layer, false))
            } else {
                Err(RemoteLoadError::IdentityConflict)
            };
        }
        let range_key = BackingRangeKey {
            backing: grant.backing_key.clone(),
            offset: grant.target_offset,
            length: grant.length,
        };
        if state
            .range_identities
            .get(&range_key)
            .is_some_and(|source| source != &grant.source)
        {
            return Err(RemoteLoadError::IdentityConflict);
        }
        let layer = existing_layer;
        state
            .range_identities
            .insert(range_key, grant.source.clone());
        let memory_window = memory_window_key(&grant, self.max_range_bytes);
        state.targets.insert(
            target_key.clone(),
            IndexedRangeGrant {
                grant,
                layer_target: Arc::downgrade(&layer),
            },
        );
        if let Some(window_key) = memory_window {
            state
                .memory_windows
                .entry(window_key)
                .or_default()
                .push(target_key);
        }
        Ok((layer, true))
    }

    /// Atomically install a bounded batch of DATA grants for one already
    /// prepared layer target.
    ///
    /// All validation and batch-local conflict detection happens before the
    /// service-state lock is taken. Once locked, every existing capability and
    /// memory-window entry is checked before any map is changed, so an error
    /// never commits a prefix of the batch. Identical grants are idempotent.
    /// Target creation remains a separate operation because acquiring a
    /// backing is asynchronous and must not be moved into this lock-only path.
    pub async fn grant_target_batch(
        self: &Arc<Self>,
        grants: Vec<RemoteLoadGrant>,
    ) -> Result<usize, RemoteLoadError> {
        if grants.is_empty() {
            return Ok(0);
        }
        if grants.len() > MAX_REMOTE_LOAD_GRANTS_PER_BATCH {
            return Err(RemoteLoadError::InvalidArgument(format!(
                "RemoteLoad grant batch exceeds {MAX_REMOTE_LOAD_GRANTS_PER_BATCH} items"
            )));
        }

        let mut target_identity = None::<RemoteLayerTargetGrant>;
        let mut target_keys = HashMap::<TargetRangeKey, usize>::with_capacity(grants.len());
        let mut range_keys = HashMap::<BackingRangeKey, usize>::with_capacity(grants.len());
        let mut pending = Vec::<PendingGrant>::with_capacity(grants.len());
        for grant in grants {
            grant.validate(self.max_range_bytes)?;
            if grant.source.codec == "registry-rootfs-v1" {
                return Err(RemoteLoadError::UnsupportedCodec);
            }
            if let Some(expected) = &target_identity {
                if grant.runtime != expected.runtime
                    || grant.target_id != expected.target_id
                    || grant.backing_key != expected.backing_key
                    || grant.provider != expected.provider
                    || grant.authorization != expected.authorization
                {
                    return Err(RemoteLoadError::IdentityConflict);
                }
            } else {
                target_identity = Some(RemoteLayerTargetGrant {
                    runtime: grant.runtime.clone(),
                    target_id: grant.target_id.clone(),
                    backing_key: grant.backing_key.clone(),
                    provider: grant.provider.clone(),
                    authorization: grant.authorization.clone(),
                });
            }

            let target_key = TargetRangeKey {
                target_id: grant.target_id.clone(),
                offset: grant.target_offset,
                length: grant.length,
            };
            if let Some(existing_index) = target_keys.get(&target_key) {
                if pending[*existing_index].grant != grant {
                    return Err(RemoteLoadError::IdentityConflict);
                }
                continue;
            }
            let range_key = BackingRangeKey {
                backing: grant.backing_key.clone(),
                offset: grant.target_offset,
                length: grant.length,
            };
            if let Some(existing_index) = range_keys.get(&range_key) {
                if pending[*existing_index].grant.source != grant.source {
                    return Err(RemoteLoadError::IdentityConflict);
                }
            }
            range_keys.insert(range_key.clone(), pending.len());
            let memory_window = memory_window_key(&grant, self.max_range_bytes);
            target_keys.insert(target_key.clone(), pending.len());
            pending.push(PendingGrant {
                grant,
                target_key,
                range_key,
                memory_window,
            });
        }

        let target_identity = target_identity.expect("non-empty batch has one target identity");
        let mut state = self.state.lock().await;
        if state.closed {
            return Err(RemoteLoadError::Closed);
        }
        if state.revoked_runtimes.contains(&target_identity.runtime) {
            return Err(RemoteLoadError::RuntimeRevoked);
        }
        let layer = state
            .layer_targets
            .get(&target_identity.target_id)
            .and_then(Weak::upgrade)
            .ok_or(RemoteLoadError::TargetNotFound)?;
        if layer.remote_identity().runtime != target_identity.runtime
            || layer.remote_identity().backing_key != target_identity.backing_key
            || layer.remote_identity().provider != target_identity.provider
            || layer.remote_identity().authorization != target_identity.authorization
            || layer.revoked.load(Ordering::Acquire)
            || layer.released.load(Ordering::Acquire)
        {
            return Err(RemoteLoadError::IdentityConflict);
        }
        let layer = Arc::clone(&layer);

        // Validate the complete transaction against existing service state.
        // In particular, an idempotent target must already have the exact
        // memory-window membership originally installed with its grant.
        for candidate in &pending {
            if state
                .range_identities
                .get(&candidate.range_key)
                .is_some_and(|source| source != &candidate.grant.source)
            {
                return Err(RemoteLoadError::IdentityConflict);
            }
            if let Some(existing) = state.targets.get(&candidate.target_key) {
                if existing.grant != candidate.grant {
                    return Err(RemoteLoadError::IdentityConflict);
                }
                if let Some(window) = &candidate.memory_window {
                    if !state
                        .memory_windows
                        .get(window)
                        .is_some_and(|keys| keys.contains(&candidate.target_key))
                    {
                        return Err(RemoteLoadError::IdentityConflict);
                    }
                }
                continue;
            }
            if candidate.memory_window.as_ref().is_some_and(|window| {
                state
                    .memory_windows
                    .get(window)
                    .is_some_and(|keys| keys.contains(&candidate.target_key))
            }) {
                return Err(RemoteLoadError::IdentityConflict);
            }
        }

        let mut inserted = 0;
        for candidate in pending {
            if state.targets.contains_key(&candidate.target_key) {
                continue;
            }
            state
                .range_identities
                .insert(candidate.range_key, candidate.grant.source.clone());
            state.targets.insert(
                candidate.target_key.clone(),
                IndexedRangeGrant {
                    grant: candidate.grant,
                    layer_target: Arc::downgrade(&layer),
                },
            );
            if let Some(window) = candidate.memory_window {
                state
                    .memory_windows
                    .entry(window)
                    .or_default()
                    .push(candidate.target_key);
            }
            inserted += 1;
        }
        Ok(inserted)
    }

    pub async fn prepare_target(
        self: &Arc<Self>,
        grant: RemoteLayerTargetGrant,
    ) -> Result<(Arc<LayerResources>, bool), RemoteLoadError> {
        grant.validate()?;
        let mut state = self.state.lock().await;
        if state.closed {
            return Err(RemoteLoadError::Closed);
        }
        if state.revoked_runtimes.contains(&grant.runtime) {
            return Err(RemoteLoadError::RuntimeRevoked);
        }
        let existed = state
            .layer_targets
            .get(&grant.target_id)
            .and_then(Weak::upgrade)
            .is_some();
        let layer = self.prepare_target_locked(&mut state, &grant, None).await?;
        Ok((layer, !existed))
    }

    /// Clone an already verified immutable layer capability into a new
    /// runtime-scoped target. Local fork children use distinct target IDs, but
    /// their remote ancestry has exactly the same backing and range map.
    pub async fn clone_layer_target(
        self: &Arc<Self>,
        source_target_id: &str,
        runtime: &RuntimeIdentity,
        target_id: &str,
    ) -> Result<(Arc<LayerResources>, bool), RemoteLoadError> {
        if source_target_id.is_empty() || target_id.is_empty() || runtime.sandbox_id.is_empty() {
            return Err(RemoteLoadError::InvalidArgument(
                "cloned target identity must not be empty".into(),
            ));
        }
        let (grant, range_map, p2p_source, reader, source) = {
            let state = self.state.lock().await;
            if state.closed {
                return Err(RemoteLoadError::Closed);
            }
            if state.revoked_runtimes.contains(runtime) {
                return Err(RemoteLoadError::RuntimeRevoked);
            }
            let source = state
                .layer_targets
                .get(source_target_id)
                .and_then(Weak::upgrade)
                .ok_or(RemoteLoadError::TargetNotFound)?;
            if source.revoked.load(Ordering::Acquire) || source.released.load(Ordering::Acquire) {
                return Err(RemoteLoadError::RuntimeRevoked);
            }
            let range_map = source
                .range_map
                .lock()
                .expect("layer capability lock poisoned")
                .clone()
                .ok_or(RemoteLoadError::TargetNotFound)?;
            let p2p_source = source
                .p2p_source
                .lock()
                .expect("layer P2P capability lock poisoned")
                .clone();
            let reader = source.reader().ok_or(RemoteLoadError::TargetNotFound)?;
            source.active_loads.fetch_add(1, Ordering::AcqRel);
            (
                RemoteLayerTargetGrant {
                    runtime: runtime.clone(),
                    target_id: target_id.to_owned(),
                    backing_key: source.remote_identity().backing_key.clone(),
                    provider: source.remote_identity().provider.clone(),
                    authorization: source.remote_identity().authorization.clone(),
                },
                range_map,
                p2p_source,
                reader,
                source,
            )
        };
        let _source_pin = ActiveLoadGuard::from_incremented(Arc::clone(self), source);
        let (layer, created) = self.prepare_target(grant).await?;
        if let Err(error) = self
            .install_layer_capability_with_reader(target_id, runtime, range_map, p2p_source, reader)
            .await
        {
            if created {
                self.revoke_layer_resources(&layer).await;
            }
            return Err(error);
        }
        Ok((layer, created))
    }

    /// Prepare a promotion target from a frozen source while permitting
    /// content-addressed reuse of an existing shared backing.  This must not
    /// be used for runtime reattachment, which requires exact inode identity.
    pub async fn prepare_promotion_target<F: AsRawFd + ?Sized>(
        self: &Arc<Self>,
        grant: RemoteLayerTargetGrant,
        readonly: &F,
    ) -> Result<(Arc<LayerResources>, bool), RemoteLoadError> {
        grant.validate()?;
        let mut state = self.state.lock().await;
        if state.closed {
            return Err(RemoteLoadError::Closed);
        }
        if state.revoked_runtimes.contains(&grant.runtime) {
            return Err(RemoteLoadError::RuntimeRevoked);
        }
        if let Some(layer) = state
            .layer_targets
            .get(&grant.target_id)
            .and_then(Weak::upgrade)
        {
            if layer.remote_identity().runtime != grant.runtime
                || layer.remote_identity().backing_key != grant.backing_key
                || layer.remote_identity().provider != grant.provider
                || layer.remote_identity().authorization != grant.authorization
            {
                return Err(RemoteLoadError::IdentityConflict);
            }
            return Ok((layer, false));
        }
        state.remove_expired_target(&grant.target_id);
        let backing_handle = self
            .backing_registry
            .seed_readonly_or_acquire_existing(grant.backing_key.clone(), readonly)
            .await?;
        let layer = LayerResources::new_remote(
            RemoteLayerIdentity {
                runtime: grant.runtime.clone(),
                target_id: grant.target_id.clone(),
                backing_key: grant.backing_key.clone(),
                provider: grant.provider.clone(),
                authorization: grant.authorization.clone(),
            },
            backing_handle,
        );
        state
            .layer_targets
            .insert(grant.target_id, Arc::downgrade(&layer));
        Ok((layer, true))
    }

    async fn prepare_target_locked(
        &self,
        state: &mut ServiceState,
        grant: &RemoteLayerTargetGrant,
        adopted: Option<&File>,
    ) -> Result<Arc<LayerResources>, RemoteLoadError> {
        if let Some(layer) = state
            .layer_targets
            .get(&grant.target_id)
            .and_then(Weak::upgrade)
        {
            if layer.remote_identity().runtime != grant.runtime
                || layer.remote_identity().backing_key != grant.backing_key
                || layer.remote_identity().provider != grant.provider
                || layer.remote_identity().authorization != grant.authorization
            {
                return Err(RemoteLoadError::IdentityConflict);
            }
            return Ok(Arc::clone(&layer));
        }
        state.remove_expired_target(&grant.target_id);
        let backing_handle = match adopted {
            Some(readonly) => {
                self.backing_registry
                    .adopt_readonly(grant.backing_key.clone(), readonly)
                    .await?
            }
            None => {
                self.backing_registry
                    .acquire(grant.backing_key.clone())
                    .await?
            }
        };
        let layer = LayerResources::new_remote(
            RemoteLayerIdentity {
                runtime: grant.runtime.clone(),
                target_id: grant.target_id.clone(),
                backing_key: grant.backing_key.clone(),
                provider: grant.provider.clone(),
                authorization: grant.authorization.clone(),
            },
            backing_handle,
        );
        state
            .layer_targets
            .insert(grant.target_id.clone(), Arc::downgrade(&layer));
        Ok(layer)
    }

    /// Install one runtime-scoped capability over a verified immutable layer.
    ///
    /// The caller is the snapshot loader/compiled-cache boundary: the table
    /// has already passed canonical validation and its content identity is
    /// bound into `backing_key`.  Keeping this Arc on the layer target avoids
    /// materializing tens of thousands of runtime-specific range grants while
    /// still requiring every RemoteLoad request to match one exact DATA entry.
    pub async fn install_layer_capability(
        &self,
        target_id: &str,
        runtime: &RuntimeIdentity,
        range_map: Arc<LayerRangeMap>,
        p2p_source: Option<P2PSource>,
    ) -> Result<bool, RemoteLoadError> {
        let reader: Arc<dyn LazyLoadInterface> =
            self.snapshot_reader.open_layer(Arc::clone(&range_map));
        self.install_layer_capability_with_reader(target_id, runtime, range_map, p2p_source, reader)
            .await
    }

    /// Bind the reader supplied by the opened snapshot to this runtime target.
    /// Residency/authorization stay here; logical offsets and decoding belong
    /// to the format capability. The default entry above supports legacy grants.
    pub async fn install_layer_capability_with_reader(
        &self,
        target_id: &str,
        runtime: &RuntimeIdentity,
        range_map: Arc<LayerRangeMap>,
        p2p_source: Option<P2PSource>,
        reader: Arc<dyn LazyLoadInterface>,
    ) -> Result<bool, RemoteLoadError> {
        let started = Instant::now();
        if range_map.registry.is_some() && p2p_source.is_some() {
            return Err(RemoteLoadError::IdentityConflict);
        }
        let (data_ranges, objects) = layer_capability_counts(&range_map);
        let state = self.state.lock().await;
        if state.closed {
            return Err(RemoteLoadError::Closed);
        }
        if state.revoked_runtimes.contains(runtime) {
            return Err(RemoteLoadError::RuntimeRevoked);
        }
        let target = state
            .layer_targets
            .get(target_id)
            .and_then(Weak::upgrade)
            .ok_or(RemoteLoadError::TargetNotFound)?;
        if &target.remote_identity().runtime != runtime || target.revoked.load(Ordering::Acquire) {
            return Err(RemoteLoadError::IdentityConflict);
        }
        validate_layer_capability_identity(&target.remote_identity().backing_key, &range_map)?;
        let mut installed = target
            .range_map
            .lock()
            .expect("layer capability lock poisoned");
        let mut installed_p2p = target
            .p2p_source
            .lock()
            .expect("layer P2P capability lock poisoned");
        if let Some(existing) = installed.as_ref() {
            if !Arc::ptr_eq(existing, &range_map)
                && (existing.content_sha256 != range_map.content_sha256
                    || existing.kind != range_map.kind
                    || existing.logical_bytes != range_map.logical_bytes
                    || existing.unit_size_bytes != range_map.unit_size_bytes)
            {
                return Err(RemoteLoadError::IdentityConflict);
            }
            if *installed_p2p != p2p_source {
                return Err(RemoteLoadError::IdentityConflict);
            }
            return Ok(false);
        }
        *target
            .lazy_reader
            .lock()
            .expect("layer reader lock poisoned") = Some(reader);
        *installed = Some(range_map);
        *installed_p2p = p2p_source;
        self.metrics.emit_structured(
            StructuredEvent::new(
                StructuredLogLevel::Info,
                StructuredEventName::RuntimeStartStageCompleted,
                StructuredComponent::Runtime,
                StructuredOutcome::Success,
            )
            .operation("layer_capability_install")
            .phase("installed")
            .layer_id(target_id)
            .sandbox_id(&runtime.sandbox_id)
            .duration_micros(started.elapsed())
            .graph_counts(
                1,
                data_ranges.try_into().unwrap_or(u64::MAX),
                data_ranges.try_into().unwrap_or(u64::MAX),
                objects.try_into().unwrap_or(u64::MAX),
            ),
        );
        Ok(true)
    }

    pub async fn backing_for_target(
        &self,
        target_id: &str,
        runtime: &RuntimeIdentity,
    ) -> Result<Arc<crate::snapshot::io::backing::LocalBacking>, RemoteLoadError> {
        let state = self.state.lock().await;
        let target = state
            .layer_targets
            .get(target_id)
            .and_then(Weak::upgrade)
            .ok_or(RemoteLoadError::TargetNotFound)?;
        if &target.remote_identity().runtime != runtime || target.revoked.load(Ordering::Acquire) {
            return Err(RemoteLoadError::IdentityConflict);
        }
        target.backing().ok_or(RemoteLoadError::TargetNotFound)
    }

    /// A content-key match may reuse a partially downloaded backing. Fill its
    /// missing DATA in place before advertising it to the VMM as LocalComplete.
    pub async fn complete_promotion_target<F: AsFd + ?Sized>(
        self: &Arc<Self>,
        target_id: &str,
        runtime: &RuntimeIdentity,
        source: &F,
    ) -> Result<(), RemoteLoadError> {
        let (target, handle, ranges) = {
            let state = self.state.lock().await;
            let target = state
                .layer_targets
                .get(target_id)
                .and_then(Weak::upgrade)
                .ok_or(RemoteLoadError::TargetNotFound)?;
            if &target.remote_identity().runtime != runtime
                || target.revoked.load(Ordering::Acquire)
            {
                return Err(RemoteLoadError::IdentityConflict);
            }
            let handle = target
                .backing_handle()
                .ok_or(RemoteLoadError::TargetNotFound)?;
            let ranges = target
                .range_map
                .lock()
                .expect("layer capability lock poisoned")
                .clone()
                .ok_or(RemoteLoadError::TargetNotFound)?;
            (Arc::clone(&target), handle, ranges)
        };
        if ranges.registry.is_some() {
            return Err(RemoteLoadError::UnsupportedCodec);
        }
        let source = Arc::new(File::from(source.as_fd().try_clone_to_owned()?));
        let destination = Arc::new(handle.backing.try_clone_file()?);
        let source_identity = source.metadata()?;
        let destination_identity = destination.metadata()?;
        if source_identity.dev() == destination_identity.dev()
            && source_identity.ino() == destination_identity.ino()
        {
            return Ok(());
        }
        for range in ranges
            .ranges
            .iter()
            .filter(|range| range.kind == RangeKind::Data)
        {
            let grant = grant_from_layer_range(target_id, &target, range)?;
            let key = DataRangeKey::new(
                &handle.backing.backing_id,
                grant.target_offset,
                grant.length,
            )?;
            let source = Arc::clone(&source);
            let destination = Arc::clone(&destination);
            // The backing-owned registry also serializes against remote loads
            // from other runtimes and publishes residency only after the write.
            handle
                .backing
                .downloads
                .ensure_loaded(key, move || async move {
                    tokio::task::spawn_blocking(move || {
                        let mut payload = vec![0; grant.length as usize];
                        source.read_exact_at(&mut payload, grant.target_offset)?;
                        if format!("{:x}", Sha256::digest(&payload)) != grant.source.checksum_sha256
                        {
                            return Err(RemoteLoadError::ChecksumMismatch);
                        }
                        pwrite_all(&destination, &payload, grant.target_offset)
                    })
                    .await
                    .map_err(|error| RemoteLoadError::Worker(error.to_string()))?
                })
                .await
                .map_err(|error| map_load_failure(error.to_string()))?;
        }
        if target.revoked.load(Ordering::Acquire) {
            return Err(RemoteLoadError::RuntimeRevoked);
        }
        Ok(())
    }

    /// Report DATA that this runtime can serve without causing a provider read.
    /// A range is counted only after the backing-owned residency registry has
    /// reached its terminal Resident state.
    pub async fn resident_range_summary(&self, runtime: &RuntimeIdentity) -> ResidentRangeSummary {
        let ranges = {
            let state = self.state.lock().await;
            let mut ranges = state
                .targets
                .values()
                .filter_map(IndexedRangeGrant::upgrade)
                .filter(|range| {
                    &range.grant.runtime == runtime
                        && !range.layer_target.revoked.load(Ordering::Acquire)
                })
                .collect::<Vec<_>>();
            ranges.extend(compiled_grants_for_runtime(&state, runtime));
            ranges.retain(|range| range.grant.source.codec == "raw");
            ranges
        };
        if ranges.is_empty() {
            return ResidentRangeSummary {
                servable_objects: 0,
                complete: false,
            };
        }
        let mut objects = HashSet::new();
        let mut complete = true;
        for range in ranges {
            let Some(backing) = target_backing(&range.layer_target) else {
                complete = false;
                continue;
            };
            let Ok(key) = DataRangeKey::new(
                backing.backing_id.clone(),
                range.grant.target_offset,
                range.grant.length,
            ) else {
                complete = false;
                continue;
            };
            if backing.downloads.status(&key).await.state == DownloadState::Resident {
                objects.insert(range.grant.source.object_key.clone());
            } else {
                complete = false;
            }
        }
        ResidentRangeSummary {
            servable_objects: objects.len(),
            complete,
        }
    }

    /// Take one O(ranges) snapshot of the exact resident object ranges for a
    /// runtime. Residency is monotonic for the backing lifetime, so callers
    /// can safely use this to answer an availability probe without issuing an
    /// O(ranges²) sequence of target-map scans.
    pub(crate) async fn resident_object_ranges(
        &self,
        runtime: &RuntimeIdentity,
    ) -> HashSet<ResidentObjectRange> {
        let ranges = {
            let state = self.state.lock().await;
            let mut ranges = state
                .targets
                .values()
                .filter_map(IndexedRangeGrant::upgrade)
                .filter(|range| {
                    &range.grant.runtime == runtime
                        && !range.layer_target.revoked.load(Ordering::Acquire)
                        && range.grant.source.codec == "raw"
                })
                .collect::<Vec<_>>();
            ranges.extend(
                compiled_grants_for_runtime(&state, runtime)
                    .into_iter()
                    .filter(|range| range.grant.source.codec == "raw"),
            );
            ranges
        };
        let mut by_backing = HashMap::<
            String,
            (
                Arc<crate::snapshot::io::backing::LocalBacking>,
                Vec<GrantedRange>,
            ),
        >::new();
        for range in ranges {
            let Some(backing) = target_backing(&range.layer_target) else {
                continue;
            };
            by_backing
                .entry(backing.backing_id.clone())
                .or_insert_with(|| (Arc::clone(&backing), Vec::new()))
                .1
                .push(range);
        }
        let mut result = HashSet::new();
        for (_, (backing, ranges)) in by_backing {
            let resident = backing.downloads.resident_ranges().await;
            for range in ranges {
                let Ok(key) = DataRangeKey::new(
                    backing.backing_id.clone(),
                    range.grant.target_offset,
                    range.grant.length,
                ) else {
                    continue;
                };
                if !resident.contains(&key) {
                    continue;
                }
                let source = &range.grant.source;
                result.insert(ResidentObjectRange {
                    object_key: source.object_key.clone(),
                    object_sha256: prefixed_content_id(&source.content_id),
                    object_version: range.grant.object_version.clone(),
                    offset: source.object_offset,
                    length: source.encoded_length,
                });
            }
        }
        result
    }

    /// Read a range only when it is covered by one exact authorized DATA
    /// mapping and that whole mapping is already resident. This path never
    /// invokes a RangeReader, so a source snapd cannot proxy S3 or another peer.
    pub async fn read_resident_range(
        self: &Arc<Self>,
        runtime: &RuntimeIdentity,
        object_key: &str,
        object_sha256: &str,
        object_version: Option<&str>,
        offset: u64,
        length: u64,
    ) -> Result<Option<Vec<u8>>, RemoteLoadError> {
        if object_key.is_empty()
            || length == 0
            || length > self.max_range_bytes
            || offset.checked_add(length).is_none()
        {
            return Err(RemoteLoadError::InvalidArgument(
                "resident range identity or geometry is invalid".into(),
            ));
        }
        let object_sha256 = object_sha256
            .strip_prefix("sha256:")
            .unwrap_or(object_sha256);
        let candidate = {
            let state = self.state.lock().await;
            let requested_end = offset
                .checked_add(length)
                .expect("validated resident range cannot overflow");
            let legacy = state
                .targets
                .values()
                .filter_map(IndexedRangeGrant::upgrade)
                .find(|range| {
                    let source = &range.grant.source;
                    let source_sha256 = source
                        .content_id
                        .strip_prefix("sha256:")
                        .unwrap_or(&source.content_id);
                    let source_end = source.object_offset.checked_add(source.encoded_length);
                    &range.grant.runtime == runtime
                        && !range.layer_target.revoked.load(Ordering::Acquire)
                        && source.codec == "raw"
                        && source.object_key == object_key
                        && source_sha256.eq_ignore_ascii_case(object_sha256)
                        && range.grant.object_version.as_deref() == object_version
                        && offset >= source.object_offset
                        && source_end.is_some_and(|end| requested_end <= end)
                });
            let compiled = state
                .live_layers()
                .filter(|(_, target)| {
                    &target.remote_identity().runtime == runtime
                        && !target.revoked.load(Ordering::Acquire)
                })
                .find_map(|(target_id, target)| {
                    let range_map = target
                        .range_map
                        .lock()
                        .expect("layer capability lock poisoned")
                        .clone()?;
                    range_map.ranges.iter().find_map(|range| {
                        // Inspect borrowed metadata first. Cloning a complete
                        // grant for every nonmatching page made partial-peer
                        // reads proportional to graph bytes, not just ranges.
                        let source = range.source.as_ref()?;
                        let source_sha256 = source
                            .object_sha256
                            .strip_prefix("sha256:")
                            .unwrap_or(&source.object_sha256);
                        let source_end = source.pack_offset.checked_add(source.encoded_length)?;
                        if range.kind != RangeKind::Data
                            || source.codec != DataCodec::Raw
                            || source.object_key != object_key
                            || !source_sha256.eq_ignore_ascii_case(object_sha256)
                            || source.object_version.as_deref() != object_version
                            || offset < source.pack_offset
                            || requested_end > source_end
                        {
                            return None;
                        }
                        let grant = grant_from_layer_range(target_id, &target, range).ok()?;
                        Some(GrantedRange {
                            grant,
                            layer_target: Arc::clone(&target),
                        })
                    })
                });
            legacy.or(compiled).map(|range| {
                range
                    .layer_target
                    .active_loads
                    .fetch_add(1, Ordering::AcqRel);
                range
            })
        };
        let Some(candidate) = candidate else {
            return Ok(None);
        };
        let _active = ActiveLoadGuard::from_incremented(
            Arc::clone(self),
            Arc::clone(&candidate.layer_target),
        );
        self.read_resident_candidate(&candidate, offset, length)
            .await
    }

    async fn read_resident_candidate(
        &self,
        candidate: &GrantedRange,
        object_offset: u64,
        length: u64,
    ) -> Result<Option<Vec<u8>>, RemoteLoadError> {
        let Some(backing) = target_backing(&candidate.layer_target) else {
            return Ok(None);
        };
        let _native_pin = backing.pin_native_io()?;
        let key = DataRangeKey::new(
            backing.backing_id.clone(),
            candidate.grant.target_offset,
            candidate.grant.length,
        )?;
        if backing.downloads.status(&key).await.state != DownloadState::Resident {
            return Ok(None);
        }
        let delta = object_offset
            .checked_sub(candidate.grant.source.object_offset)
            .ok_or_else(|| RemoteLoadError::InvalidArgument("resident range underflows".into()))?;
        let backing_offset = candidate
            .grant
            .target_offset
            .checked_add(delta)
            .ok_or_else(|| RemoteLoadError::InvalidArgument("resident range overflows".into()))?;
        let mut payload = vec![
            0_u8;
            usize::try_from(length).map_err(|_| {
                RemoteLoadError::InvalidArgument("resident range exceeds address space".into())
            })?
        ];
        let file = backing.try_clone_readonly_file()?;
        file.read_exact_at(&mut payload, backing_offset)?;
        Ok(Some(payload))
    }

    /// Run one speculative load under a caller-owned node admission permit.
    /// The permit is transferred to the service completion monitor before the
    /// caller reaches another cancellation point, so cancelling the attach
    /// future cannot make an in-flight provider operation invisible to the
    /// global concurrency bound.
    pub(crate) async fn load_range_with_admission(
        self: &Arc<Self>,
        runtime: &RuntimeIdentity,
        request: RemoteLoadRequest,
        admission: RemoteLoadAdmission,
    ) -> Result<RangeLoadOutcome, RemoteLoadError> {
        self.load_range(runtime, request, Some(admission))
            .await
            .map(|result| result.outcome)
    }

    pub(in crate::snapshot) async fn load_range(
        self: &Arc<Self>,
        runtime: &RuntimeIdentity,
        request: RemoteLoadRequest,
        admission: Option<RemoteLoadAdmission>,
    ) -> Result<RemoteLoadResult, RemoteLoadError> {
        request.validate()?;
        let started = Instant::now();
        let origin = request.origin.clone();
        let runtime = runtime.clone();
        let target = self.authorize(&runtime, &request).await?;
        let layer_id = target.grant.source.content_id.clone();
        let kind = match target.grant.backing_key.kind {
            LayerKind::Memory => DataKind::Memory,
            LayerKind::Block => DataKind::Block,
        };
        let background = origin == "background_prefetch";
        let demand =
            (!background && origin != "attach_prefetch" && origin != "snapshot_hotset_prefetch")
                .then(|| background::DemandGuard::new(Arc::clone(self)));
        let active =
            ActiveLoadGuard::from_incremented(Arc::clone(self), Arc::clone(&target.layer_target));
        let execution_service = Arc::clone(self);
        let execution_target = target.clone();
        let execution = tokio::spawn(async move {
            execution_service
                .execute_remote_load(&execution_target, kind, background)
                .await
        });
        let worker = tokio::spawn(async move {
            // Keep speculative node admission until the service-owned
            // execution really terminates, not merely until its caller waits
            // or is cancelled.
            let result = match execution.await {
                Ok(result) => result,
                Err(error) => Err(RemoteLoadError::Worker(error.to_string())),
            };
            drop(active);
            drop(demand);
            if let Some(admission) = admission {
                admission.finish(
                    result.is_ok(),
                    result.as_ref().err().map(remote_load_error_code),
                );
            }
            result
        });
        let result = worker
            .await
            .map_err(|error| RemoteLoadError::Worker(error.to_string()))?;
        self.metrics.observe_remote_load(kind, started.elapsed());
        let outcome = match &result {
            Ok(RemoteLoadResult {
                outcome: RangeLoadOutcome::Loaded,
                ..
            }) => DownloadOutcomeMetric::Loaded,
            Ok(RemoteLoadResult {
                outcome: RangeLoadOutcome::CacheHit,
                ..
            }) => DownloadOutcomeMetric::CacheHit,
            Ok(RemoteLoadResult {
                outcome: RangeLoadOutcome::Joined,
                ..
            }) => DownloadOutcomeMetric::Joined,
            Err(_) => DownloadOutcomeMetric::Failed,
        };
        let outcome_phase = match outcome {
            DownloadOutcomeMetric::Loaded => "loaded",
            DownloadOutcomeMetric::CacheHit => "cache_hit",
            DownloadOutcomeMetric::Joined => "joined",
            DownloadOutcomeMetric::Failed => "failed",
        };
        self.metrics.observe_download(kind, &origin, outcome);
        if result.is_ok()
            && !background
            && origin != "attach_prefetch"
            && origin != "snapshot_hotset_prefetch"
        {
            self.hotsets.record(
                &runtime,
                SnapshotHotRange {
                    layer_content_sha256: target.grant.backing_key.content_sha256.clone(),
                    target_offset: request.target_offset,
                    length: request.length,
                },
            );
        }
        // Successful range loads are already counted/histogrammed above.
        // Do not construct/serialize a JSON event for every guest page fault.
        // Keep failure diagnostics, and aggregate background/stage events.
        if result.is_ok()
            || background
            || origin == "attach_prefetch"
            || origin == "snapshot_hotset_prefetch"
        {
            return result;
        }
        let mut event = StructuredEvent::new(
            if result.is_ok() {
                StructuredLogLevel::Info
            } else {
                StructuredLogLevel::Error
            },
            StructuredEventName::RemoteLoadCompleted,
            StructuredComponent::RemoteLoad,
            if result.is_ok() {
                StructuredOutcome::Success
            } else {
                StructuredOutcome::Error
            },
        )
        .operation("remote_load")
        .phase(outcome_phase)
        .layer_id(&layer_id)
        .sandbox_id(&runtime.sandbox_id)
        .range(request.target_offset, request.length)
        .duration_micros(started.elapsed());
        if let Err(error) = &result {
            event = event.error_code(remote_load_error_code(error));
        }
        self.metrics.emit_structured(event);
        result
    }

    async fn authorize(
        &self,
        runtime: &RuntimeIdentity,
        request: &RemoteLoadRequest,
    ) -> Result<GrantedRange, RemoteLoadError> {
        let state = self.state.lock().await;
        if state.closed {
            return Err(RemoteLoadError::Closed);
        }
        if let Some(layer) = state
            .layer_targets
            .get(&request.target_id)
            .and_then(Weak::upgrade)
        {
            if &layer.remote_identity().runtime != runtime || layer.revoked.load(Ordering::Acquire)
            {
                return Err(RemoteLoadError::IdentityConflict);
            }
            if let Some(grant) = exact_layer_grant(
                &request.target_id,
                &layer,
                request.target_offset,
                request.length,
            )? {
                layer.active_loads.fetch_add(1, Ordering::AcqRel);
                return Ok(GrantedRange {
                    grant,
                    layer_target: Arc::clone(&layer),
                });
            }
        }
        let target = state
            .targets
            .get(&TargetRangeKey {
                target_id: request.target_id.clone(),
                offset: request.target_offset,
                length: request.length,
            })
            .and_then(IndexedRangeGrant::upgrade)
            .ok_or(RemoteLoadError::TargetNotFound)?;
        if &target.grant.runtime != runtime || target.layer_target.revoked.load(Ordering::Acquire) {
            return Err(RemoteLoadError::IdentityConflict);
        }
        target
            .layer_target
            .active_loads
            .fetch_add(1, Ordering::AcqRel);
        Ok(target.clone())
    }

    async fn execute_remote_load(
        &self,
        target: &GrantedRange,
        kind: DataKind,
        provider_only: bool,
    ) -> Result<RemoteLoadResult, RemoteLoadError> {
        let grant = target.grant.clone();
        let provider = self
            .providers
            .get(&grant.provider)
            .cloned()
            .ok_or(RemoteLoadError::ProviderNotConfigured)?;
        let reader = target
            .layer_target
            .lazy_reader
            .lock()
            .expect("layer reader lock poisoned")
            .clone();
        let source = RangeDownloadSource {
            snapshot_reader: Arc::clone(&self.snapshot_reader),
            provider,
            reader,
        };
        let backing = target
            .layer_target
            .backing()
            .ok_or(RemoteLoadError::TargetNotFound)?;
        let backing_for_load = Arc::clone(&backing);
        let downloads = Arc::clone(&backing.downloads);
        let metrics = Arc::clone(&self.metrics);
        let exact_key = DataRangeKey::new(
            backing.backing_id.clone(),
            grant.target_offset,
            grant.length,
        )?;
        let mut coalesced = self
            .coalesced_range_with_policy(target, provider_only)
            .await;
        if provider_only {
            for member in &mut coalesced.grants {
                member.p2p_source = None;
            }
        }
        let member_keys = coalesced
            .grants
            .iter()
            .map(|member| {
                DataRangeKey::new(
                    backing.backing_id.clone(),
                    member.target_offset,
                    member.length,
                )
            })
            .collect::<Result<Vec<_>, _>>()?;
        let batch_key = DataRangeKey::new(
            backing.backing_id.clone(),
            coalesced.first().target_offset,
            coalesced.length(),
        )?;
        let retry_key = if coalesced.grants.len() == 1 {
            &exact_key
        } else {
            &batch_key
        };
        let retrying_failed_provider_load =
            downloads.status(retry_key).await.state == DownloadState::LoadFailed;
        let provider_load_timeout = self.provider_load_timeout;
        let batch_downloads = Arc::clone(&downloads);
        let coalesced_for_load = coalesced.clone();
        let outcome = downloads
            .ensure_loaded(exact_key, move || async move {
                if coalesced_for_load.grants.len() == 1 {
                    return download_coalesced_range(
                        source,
                        backing_for_load,
                        metrics,
                        kind,
                        coalesced_for_load,
                        retrying_failed_provider_load,
                        provider_load_timeout,
                    )
                    .await;
                }
                batch_downloads
                    .ensure_group_loaded(batch_key, member_keys, move || {
                        download_coalesced_range(
                            source,
                            backing_for_load,
                            metrics,
                            kind,
                            coalesced_for_load,
                            retrying_failed_provider_load,
                            provider_load_timeout,
                        )
                    })
                    .await?;
                Ok::<(), RemoteLoadError>(())
            })
            .await
            .map_err(|error| match error {
                RangeError::LoadFailed(message) => map_load_failure(message),
                other => RemoteLoadError::Range(other),
            })?;
        let (resident_offset, resident_length) = contiguous_resident_coverage(
            &downloads,
            &backing.backing_id,
            &coalesced,
            grant.target_offset,
            grant.length,
        )
        .await?;
        Ok(RemoteLoadResult {
            outcome,
            resident_offset,
            resident_length,
        })
    }

    pub(in crate::snapshot) async fn coalesced_range(
        &self,
        requested: &GrantedRange,
    ) -> CoalescedRange {
        self.coalesced_range_with_policy(requested, false).await
    }

    pub(in crate::snapshot) async fn coalesced_range_with_policy(
        &self,
        requested: &GrantedRange,
        provider_only: bool,
    ) -> CoalescedRange {
        let grant = &requested.grant;
        if !supports_coalescing(grant)
            || grant.length != grant.backing_key.unit_size
            || grant.source.encoded_length != grant.length
        {
            return CoalescedRange {
                grants: vec![grant.clone()],
            };
        }
        if let Some(source) = grant.p2p_source.as_ref().filter(|_| !provider_only) {
            let Some(provider) = self.providers.get(&grant.provider) else {
                return CoalescedRange {
                    grants: vec![grant.clone()],
                };
            };
            // Keep exact, checksummed ranges for a usable peer. Once the peer
            // is disabled, use the existing provider-only wide-read path.
            if !provider
                .allows_provider_coalescing(&grant.runtime, source)
                .await
            {
                return CoalescedRange {
                    grants: vec![grant.clone()],
                };
            }
        }
        let Some(window_key) = memory_window_key(grant, self.max_range_bytes) else {
            return CoalescedRange {
                grants: vec![grant.clone()],
            };
        };
        let window_bytes = MEMORY_COALESCED_READ_BYTES
            .min(self.max_range_bytes)
            .max(grant.backing_key.unit_size);
        let window_start = window_key.window_start;
        let Some(window_end) = window_start.checked_add(window_bytes) else {
            return CoalescedRange {
                grants: vec![grant.clone()],
            };
        };
        let compiled_map = requested
            .layer_target
            .range_map
            .lock()
            .expect("layer capability lock poisoned")
            .clone();
        if let Some(range_map) = compiled_map {
            if let Ok(requested_index) = range_map
                .ranges
                .binary_search_by_key(&grant.target_offset, |range| range.logical_offset)
            {
                let mut first = requested_index;
                while first > 0 {
                    let Ok(left) = grant_from_layer_range(
                        &grant.target_id,
                        &requested.layer_target,
                        &range_map.ranges[first - 1],
                    ) else {
                        break;
                    };
                    let Ok(right) = grant_from_layer_range(
                        &grant.target_id,
                        &requested.layer_target,
                        &range_map.ranges[first],
                    ) else {
                        break;
                    };
                    if !coalesced_grant_matches(grant, &left, window_start, window_end)
                        || !grants_are_contiguous(&left, &right)
                    {
                        break;
                    }
                    first -= 1;
                }
                let mut last = requested_index + 1;
                while last < range_map.ranges.len() {
                    let Ok(left) = grant_from_layer_range(
                        &grant.target_id,
                        &requested.layer_target,
                        &range_map.ranges[last - 1],
                    ) else {
                        break;
                    };
                    let Ok(right) = grant_from_layer_range(
                        &grant.target_id,
                        &requested.layer_target,
                        &range_map.ranges[last],
                    ) else {
                        break;
                    };
                    if !coalesced_grant_matches(grant, &right, window_start, window_end)
                        || !grants_are_contiguous(&left, &right)
                    {
                        break;
                    }
                    last += 1;
                }
                let candidates = range_map.ranges[first..last]
                    .iter()
                    .map(|range| {
                        grant_from_layer_range(&grant.target_id, &requested.layer_target, range)
                    })
                    .collect::<Result<Vec<_>, _>>();
                if let Ok(candidates) = candidates {
                    if candidates.iter().any(|candidate| {
                        candidate.target_offset == grant.target_offset
                            && candidate.length == grant.length
                            && candidate.source == grant.source
                    }) {
                        return CoalescedRange { grants: candidates };
                    }
                }
            }
            return CoalescedRange {
                grants: vec![grant.clone()],
            };
        }
        let state = self.state.lock().await;
        let mut candidates = state
            .memory_windows
            .get(&window_key)
            .into_iter()
            .flatten()
            .filter_map(|key| state.targets.get(key))
            .filter(|candidate| {
                Weak::ptr_eq(
                    &candidate.layer_target,
                    &Arc::downgrade(&requested.layer_target),
                )
            })
            .map(|candidate| &candidate.grant)
            .filter(|candidate| {
                candidate.backing_key.kind == LayerKind::Memory
                    && candidate.source.codec == "raw"
                    && candidate.length == grant.backing_key.unit_size
                    && candidate.source.encoded_length == candidate.length
                    && candidate.provider == grant.provider
                    && candidate.object_version == grant.object_version
                    && candidate.source.content_id == grant.source.content_id
                    && candidate.source.object_key == grant.source.object_key
                    && candidate.p2p_source == grant.p2p_source
                    && candidate.source.object_offset >= window_start
                    && candidate
                        .source
                        .object_offset
                        .checked_add(candidate.source.encoded_length)
                        .is_some_and(|end| end <= window_end)
            })
            .cloned()
            .collect::<Vec<_>>();
        candidates.sort_by_key(|candidate| candidate.source.object_offset);
        let Some(requested_index) = candidates.iter().position(|candidate| {
            candidate.target_offset == grant.target_offset
                && candidate.length == grant.length
                && candidate.source == grant.source
        }) else {
            return CoalescedRange {
                grants: vec![grant.clone()],
            };
        };
        let mut first = requested_index;
        while first > 0 && grants_are_contiguous(&candidates[first - 1], &candidates[first]) {
            first -= 1;
        }
        let mut last = requested_index + 1;
        while last < candidates.len()
            && grants_are_contiguous(&candidates[last - 1], &candidates[last])
        {
            last += 1;
        }
        CoalescedRange {
            grants: candidates[first..last].to_vec(),
        }
    }

    pub async fn status(
        &self,
        target_id: &str,
        runtime: &RuntimeIdentity,
        range: Option<(u64, u64)>,
    ) -> Result<DataRangeStatus, RemoteLoadError> {
        let target = {
            let state = self.state.lock().await;
            let compiled =
                if let Some(layer) = state.layer_targets.get(target_id).and_then(Weak::upgrade) {
                    if &layer.remote_identity().runtime != runtime
                        || layer.revoked.load(Ordering::Acquire)
                    {
                        return Err(RemoteLoadError::IdentityConflict);
                    }
                    if let Some((offset, length)) = range {
                        exact_layer_grant(target_id, &layer, offset, length)?.map(|grant| {
                            GrantedRange {
                                grant,
                                layer_target: Arc::clone(&layer),
                            }
                        })
                    } else if layer
                        .range_map
                        .lock()
                        .expect("layer capability lock poisoned")
                        .is_some()
                    {
                        return Err(RemoteLoadError::InvalidArgument(
                            "multi-range target status requires an exact range".into(),
                        ));
                    } else {
                        None
                    }
                } else {
                    None
                };
            if let Some(target) = compiled {
                target
            } else {
                let matches = state
                    .targets
                    .iter()
                    .filter(|(key, _)| key.target_id == target_id)
                    .filter(|(key, _)| range.is_none_or(|value| (key.offset, key.length) == value))
                    .filter_map(|(_, target)| target.upgrade())
                    .collect::<Vec<_>>();
                if range.is_none() && matches.len() > 1 {
                    return Err(RemoteLoadError::InvalidArgument(
                        "multi-range target status requires an exact range".into(),
                    ));
                }
                let target = matches.first().ok_or(RemoteLoadError::TargetNotFound)?;
                if &target.grant.runtime != runtime {
                    return Err(RemoteLoadError::IdentityConflict);
                }
                (*target).clone()
            }
        };
        self.status_for_grant(target).await
    }

    async fn status_for_grant(
        &self,
        target: GrantedRange,
    ) -> Result<DataRangeStatus, RemoteLoadError> {
        let backing = target
            .layer_target
            .backing()
            .ok_or(RemoteLoadError::TargetNotFound)?;
        let key = DataRangeKey::new(
            backing.backing_id.clone(),
            target.grant.target_offset,
            target.grant.length,
        )?;
        Ok(backing.downloads.status(&key).await)
    }

    pub async fn revoke_target(&self, target_id: &str, runtime: &RuntimeIdentity) -> bool {
        let target = {
            let mut state = self.state.lock().await;
            let Some(target) = state.layer_targets.get(target_id).and_then(Weak::upgrade) else {
                state.remove_expired_target(target_id);
                return false;
            };
            if &target.remote_identity().runtime != runtime {
                return false;
            }
            state.layer_targets.remove(target_id);
            state.targets.retain(|key, _| key.target_id != target_id);
            state
                .memory_windows
                .retain(|key, _| key.target_id != target_id);
            target.revoked.store(true, Ordering::Release);
            target
        };
        self.release_if_idle(&target);
        true
    }

    /// Rollback/release a specific registration without revoking a replacement
    /// that happens to reuse its textual target identity.
    pub(crate) async fn revoke_layer_resources(&self, expected: &Arc<LayerResources>) -> bool {
        let target_id = &expected.remote_identity().target_id;
        let mut state = self.state.lock().await;
        if !state
            .layer_targets
            .get(target_id)
            .is_some_and(|current| Weak::ptr_eq(current, &Arc::downgrade(expected)))
        {
            return false;
        }
        state.layer_targets.remove(target_id);
        state.targets.retain(|key, _| key.target_id != *target_id);
        state
            .memory_windows
            .retain(|key, _| key.target_id != *target_id);
        expected.revoked.store(true, Ordering::Release);
        drop(state);
        self.release_if_idle(expected);
        true
    }

    /// Reuse a sandbox ID only after its previous loads and provider cleanup drain.
    pub async fn begin_runtime(&self, runtime: &RuntimeIdentity) -> Result<(), RemoteLoadError> {
        loop {
            let changed = self.activity_changed.notified();
            let mut state = self.state.lock().await;
            if state.closed {
                return Err(RemoteLoadError::Closed);
            }
            if !state.runtime_cleanups.contains(runtime) {
                state.revoked_runtimes.remove(runtime);
                return Ok(());
            }
            drop(state);
            changed.await;
        }
    }

    pub async fn revoke_runtime(self: &Arc<Self>, runtime: &RuntimeIdentity) -> usize {
        self.background.cancel(runtime);
        let (targets, start_cleanup) = {
            let mut state = self.state.lock().await;
            state.revoked_runtimes.insert(runtime.clone());
            let start_cleanup = state.runtime_cleanups.insert(runtime.clone());
            let ids = state
                .live_layers()
                .filter(|(_, target)| &target.remote_identity().runtime == runtime)
                .map(|(id, _)| id.clone())
                .collect::<Vec<_>>();
            let targets = ids
                .iter()
                .filter_map(|id| {
                    state
                        .layer_targets
                        .remove(id)
                        .and_then(|target| target.upgrade())
                })
                .collect::<Vec<_>>();
            let id_set = ids.iter().collect::<HashSet<_>>();
            state
                .targets
                .retain(|key, _| !id_set.contains(&key.target_id));
            state
                .memory_windows
                .retain(|key, _| &key.runtime != runtime);
            for target in &targets {
                target.revoked.store(true, Ordering::Release);
            }
            (targets, start_cleanup)
        };
        for target in &targets {
            self.release_if_idle(target);
        }
        // Revoke is an API-path operation: remove capabilities and release any
        // idle backing synchronously, then let a bounded service-owned cleanup
        // wait for already-authorized loads.  Forgetting a P2P runtime before
        // those loads drain could let a late read recreate peer state.
        if start_cleanup {
            let cleanup = Arc::clone(self);
            let cleanup_targets = targets.clone();
            let cleanup_runtime = runtime.clone();
            tokio::spawn(async move {
                cleanup
                    .forget_runtime_after_loads(cleanup_runtime, cleanup_targets)
                    .await;
            });
        }
        targets.len()
    }

    pub async fn close(&self) {
        self.hotsets.close();
        self.background.cancel_all();
        let targets = {
            let mut state = self.state.lock().await;
            if state.closed {
                return;
            }
            state.closed = true;
            state.targets.clear();
            let targets = state
                .layer_targets
                .drain()
                .filter_map(|(_, target)| target.upgrade())
                .collect::<Vec<_>>();
            for target in &targets {
                target.revoked.store(true, Ordering::Release);
            }
            targets
        };
        for target in &targets {
            self.release_if_idle(target);
        }
        let _ = tokio::time::timeout(
            self.cleanup_wait_timeout(),
            self.wait_until_released(&targets),
        )
        .await;
        self.backing_registry.close().await;
    }

    fn cleanup_wait_timeout(&self) -> Duration {
        self.provider_load_timeout
            .saturating_add(CLEANUP_TIMEOUT_SLACK)
    }

    async fn forget_runtime_after_loads(
        self: Arc<Self>,
        runtime: RuntimeIdentity,
        targets: Vec<Arc<LayerResources>>,
    ) {
        // A new instance may reuse this sandbox ID after this task completes.
        self.wait_until_released(&targets).await;
        for provider in self.providers.values() {
            // Provider cleanup is advisory state reclamation and must not hold
            // the service or API lifecycle hostage indefinitely.
            let _ = tokio::time::timeout(
                self.provider_load_timeout,
                provider.forget_runtime(&runtime),
            )
            .await;
        }
        self.state.lock().await.runtime_cleanups.remove(&runtime);
        self.activity_changed.notify_waiters();
    }

    async fn wait_until_released(&self, targets: &[Arc<LayerResources>]) {
        while targets
            .iter()
            .any(|target| !target.released.load(Ordering::Acquire))
        {
            let changed = self.activity_changed.notified();
            if targets
                .iter()
                .all(|target| target.released.load(Ordering::Acquire))
            {
                break;
            }
            changed.await;
        }
    }

    fn finish_active_load(&self, target: &Arc<LayerResources>) {
        let previous = target.active_loads.fetch_sub(1, Ordering::AcqRel);
        debug_assert!(previous > 0);
        if previous == 1 {
            self.release_if_idle(target);
            self.activity_changed.notify_waiters();
        }
    }

    fn release_if_idle(&self, target: &Arc<LayerResources>) {
        if !target.revoked.load(Ordering::Acquire)
            || target.active_loads.load(Ordering::Acquire) != 0
            || target.released.swap(true, Ordering::AcqRel)
        {
            return;
        }
        target.release_lease();
        self.activity_changed.notify_waiters();
    }
}

fn coalesced_grant_matches(
    requested: &RemoteLoadGrant,
    candidate: &RemoteLoadGrant,
    window_start: u64,
    window_end: u64,
) -> bool {
    candidate.backing_key.kind == requested.backing_key.kind
        && candidate.source.codec == requested.source.codec
        && candidate.length == requested.backing_key.unit_size
        && candidate.source.encoded_length == candidate.length
        && candidate.provider == requested.provider
        && candidate.object_version == requested.object_version
        && candidate.source.content_id == requested.source.content_id
        && candidate.source.object_key == requested.source.object_key
        && candidate.p2p_source == requested.p2p_source
        && candidate.source.object_offset >= window_start
        && candidate
            .source
            .object_offset
            .checked_add(candidate.source.encoded_length)
            .is_some_and(|end| end <= window_end)
}

pub(in crate::snapshot) fn target_backing(
    target: &LayerResources,
) -> Option<Arc<crate::snapshot::io::backing::LocalBacking>> {
    target.backing()
}

fn prefixed_content_id(content_id: &str) -> String {
    if content_id.starts_with("sha256:") {
        content_id.to_owned()
    } else {
        format!("sha256:{content_id}")
    }
}

pub(in crate::snapshot) fn remote_load_error_code(error: &RemoteLoadError) -> &'static str {
    match error {
        RemoteLoadError::InvalidArgument(_) => "invalid_argument",
        RemoteLoadError::TargetNotFound => "target_not_found",
        RemoteLoadError::IdentityConflict => "identity_conflict",
        RemoteLoadError::RuntimeRevoked => "runtime_revoked",
        RemoteLoadError::Closed => "closed",
        RemoteLoadError::ProviderNotConfigured => "provider_not_configured",
        RemoteLoadError::Provider(error) => range_read_error_code(error),
        RemoteLoadError::ShortRead => "short_read",
        RemoteLoadError::ChecksumMismatch => "checksum_mismatch",
        RemoteLoadError::UnsupportedCodec => "unsupported_codec",
        RemoteLoadError::WriteNoProgress => "write_no_progress",
        RemoteLoadError::Write(_) => "write_error",
        RemoteLoadError::Backing(_) => "backing_error",
        RemoteLoadError::Layer(_) => "layer_error",
        RemoteLoadError::Range(_) => "range_error",
        RemoteLoadError::Worker(_) => "worker_error",
    }
}

fn range_read_error_code(error: &RangeReadError) -> &'static str {
    match error {
        RangeReadError::NotFound => "provider_not_found",
        RangeReadError::InvalidRange => "provider_invalid_range",
        RangeReadError::Failure { class, .. } => match class {
            RangeReadFailureClass::Transient => "provider_transient",
            RangeReadFailureClass::Authorization => "provider_authorization",
            RangeReadFailureClass::Integrity => "provider_integrity",
            RangeReadFailureClass::Permanent => "provider_permanent",
        },
        RangeReadError::Fallback { final_error, .. } => range_read_error_code(final_error),
    }
}

fn validate_layer_capability_identity(
    backing: &LayerBackingIdentity,
    range_map: &LayerRangeMap,
) -> Result<(), RemoteLoadError> {
    if let Some(native) = &range_map.registry {
        native
            .validate_map(range_map)
            .map_err(|_| RemoteLoadError::IdentityConflict)?;
        if range_map.zfile.is_some() {
            return Err(RemoteLoadError::IdentityConflict);
        }
    } else if range_map
        .ranges
        .iter()
        .filter_map(|range| range.source.as_ref())
        .any(|source| source.codec == DataCodec::RegistryRootfs)
    {
        return Err(RemoteLoadError::IdentityConflict);
    }
    let kind = match range_map.kind {
        crate::snapshot::metadata::LayerKind::Memory => LayerKind::Memory,
        crate::snapshot::metadata::LayerKind::Block => LayerKind::Block,
    };
    if backing.kind != kind
        || backing.format_version != range_map.format_version
        || backing.logical_size != range_map.logical_bytes
        || backing.unit_size != range_map.unit_size_bytes
        || backing.content_sha256.trim_start_matches("sha256:")
            != range_map.content_sha256.trim_start_matches("sha256:")
    {
        return Err(RemoteLoadError::IdentityConflict);
    }
    Ok(())
}

fn layer_capability_counts(range_map: &LayerRangeMap) -> (usize, usize) {
    let mut data_ranges = 0;
    let mut objects = HashSet::new();
    for range in &range_map.ranges {
        if let Some(source) = range.source.as_ref() {
            data_ranges += 1;
            objects.insert((
                source.object_key.as_str(),
                source.object_sha256.as_str(),
                source.object_version.as_deref(),
            ));
        }
    }
    (data_ranges, objects.len())
}

pub(in crate::snapshot) fn grant_from_layer_range(
    target_id: &str,
    target: &Arc<LayerResources>,
    range: &LayerRange,
) -> Result<RemoteLoadGrant, RemoteLoadError> {
    if range.kind != RangeKind::Data {
        return Err(RemoteLoadError::TargetNotFound);
    }
    let source = range.source.as_ref().ok_or_else(|| {
        RemoteLoadError::InvalidArgument("verified DATA range has no source".into())
    })?;
    let codec = match source.codec {
        DataCodec::Raw => "raw",
        DataCodec::RegistryRootfs => "registry-rootfs-v1",
    };
    Ok(RemoteLoadGrant {
        runtime: target.remote_identity().runtime.clone(),
        target_id: target_id.to_owned(),
        backing_key: target.remote_identity().backing_key.clone(),
        target_offset: range.logical_offset,
        length: range.length,
        source: RemoteRangeSource {
            content_id: source
                .object_sha256
                .trim_start_matches("sha256:")
                .to_owned(),
            object_key: source.object_key.clone(),
            object_offset: source.pack_offset,
            encoded_length: source.encoded_length,
            checksum_sha256: source
                .checksum_sha256
                .trim_start_matches("sha256:")
                .to_owned(),
            codec: codec.into(),
        },
        provider: target.remote_identity().provider.clone(),
        object_version: source.object_version.clone(),
        authorization: target.remote_identity().authorization.clone(),
        p2p_source: if source.codec == DataCodec::RegistryRootfs {
            None
        } else {
            target
                .p2p_source
                .lock()
                .expect("layer P2P capability lock poisoned")
                .clone()
        },
    })
}

pub(in crate::snapshot) fn exact_layer_grant(
    target_id: &str,
    target: &Arc<LayerResources>,
    offset: u64,
    length: u64,
) -> Result<Option<RemoteLoadGrant>, RemoteLoadError> {
    let range_map = target
        .range_map
        .lock()
        .expect("layer capability lock poisoned")
        .clone();
    let Some(range_map) = range_map else {
        return Ok(None);
    };
    let Ok(index) = range_map
        .ranges
        .binary_search_by_key(&offset, |range| range.logical_offset)
    else {
        return Ok(None);
    };
    let range = &range_map.ranges[index];
    if range.length != length || range.kind != RangeKind::Data {
        return Ok(None);
    }
    grant_from_layer_range(target_id, &target, range).map(Some)
}

fn compiled_grants_for_runtime(
    state: &ServiceState,
    runtime: &RuntimeIdentity,
) -> Vec<GrantedRange> {
    let mut grants = Vec::new();
    for (target_id, target) in state.live_layers() {
        if &target.remote_identity().runtime != runtime || target.revoked.load(Ordering::Acquire) {
            continue;
        }
        let range_map = target
            .range_map
            .lock()
            .expect("layer capability lock poisoned")
            .clone();
        let Some(range_map) = range_map else {
            continue;
        };
        grants.extend(range_map.ranges.iter().filter_map(|range| {
            grant_from_layer_range(target_id, &target, range)
                .ok()
                .map(|grant| GrantedRange {
                    grant,
                    layer_target: Arc::clone(&target),
                })
        }));
    }
    grants
}

fn supports_coalescing(grant: &RemoteLoadGrant) -> bool {
    matches!(
        (grant.backing_key.kind, grant.source.codec.as_str()),
        (LayerKind::Memory, "raw") | (LayerKind::Block, "registry-rootfs-v1")
    )
}

fn memory_window_key(grant: &RemoteLoadGrant, max_range_bytes: u64) -> Option<MemoryWindowKey> {
    if !supports_coalescing(grant)
        || grant.length != grant.backing_key.unit_size
        || grant.source.encoded_length != grant.length
    {
        return None;
    }
    let window_bytes = MEMORY_COALESCED_READ_BYTES
        .min(max_range_bytes)
        .max(grant.backing_key.unit_size);
    Some(MemoryWindowKey {
        runtime: grant.runtime.clone(),
        target_id: grant.target_id.clone(),
        provider: grant.provider.clone(),
        object_version: grant.object_version.clone(),
        content_id: grant.source.content_id.clone(),
        object_key: grant.source.object_key.clone(),
        window_start: grant.source.object_offset / window_bytes * window_bytes,
        target_source_delta: i128::from(grant.target_offset)
            - i128::from(grant.source.object_offset),
    })
}

fn grants_are_contiguous(left: &RemoteLoadGrant, right: &RemoteLoadGrant) -> bool {
    left.target_offset.checked_add(left.length) == Some(right.target_offset)
        && left
            .source
            .object_offset
            .checked_add(left.source.encoded_length)
            == Some(right.source.object_offset)
}

async fn contiguous_resident_coverage(
    downloads: &DataRangeResidencyRegistry,
    backing_id: &str,
    candidates: &CoalescedRange,
    requested_offset: u64,
    requested_length: u64,
) -> Result<(u64, u64), RemoteLoadError> {
    let keys = candidates
        .grants
        .iter()
        .map(|grant| DataRangeKey::new(backing_id, grant.target_offset, grant.length))
        .collect::<Result<Vec<_>, _>>()?;
    let resident = downloads.resident_members(&keys).await;
    let requested_index = candidates
        .grants
        .iter()
        .position(|grant| {
            grant.target_offset == requested_offset && grant.length == requested_length
        })
        .ok_or_else(|| {
            RemoteLoadError::Worker("coalesced range omitted the requested DATA grant".into())
        })?;
    if !resident[requested_index] {
        return Err(RemoteLoadError::Worker(
            "successful load did not publish requested DATA residency".into(),
        ));
    }
    let mut first = requested_index;
    while first > 0 && resident[first - 1] {
        first -= 1;
    }
    let mut last = requested_index + 1;
    while last < candidates.grants.len() && resident[last] {
        last += 1;
    }
    let offset = candidates.grants[first].target_offset;
    let end = candidates.grants[last - 1]
        .target_offset
        .checked_add(candidates.grants[last - 1].length)
        .ok_or_else(|| RemoteLoadError::Worker("resident coverage range overflows".into()))?;
    Ok((offset, end - offset))
}

struct RangeDownloadSource {
    snapshot_reader: Arc<dyn SnapshotReader>,
    provider: Arc<dyn RangeReader>,
    reader: Option<Arc<dyn LazyLoadInterface>>,
}

async fn download_coalesced_range(
    source: RangeDownloadSource,
    backing: Arc<crate::snapshot::io::backing::LocalBacking>,
    metrics: Arc<SnapdMetrics>,
    kind: DataKind,
    range: CoalescedRange,
    retrying_failed_provider_load: bool,
    provider_load_timeout: Duration,
) -> Result<(), RemoteLoadError> {
    let _native_pin = backing.pin_native_io()?;
    let first = range.first();
    // Explicit legacy grants remain supported. Their authorization boundary
    // supplies the same common logical map as an opened persistent snapshot;
    // source offsets and decoding below this point belong to the format layer.
    let reader: Arc<dyn LazyLoadInterface> = if let Some(reader) = source.reader {
        reader
    } else {
        let ranges = range
            .grants
            .iter()
            .map(|grant| {
                if grant.source.codec != "raw" {
                    return Err(RemoteLoadError::UnsupportedCodec);
                }
                Ok(crate::snapshot::metadata::LayerRange {
                    kind: RangeKind::Data,
                    logical_offset: grant.target_offset,
                    length: grant.length,
                    source: Some(crate::snapshot::metadata::DataSource {
                        object_key: grant.source.object_key.clone(),
                        object_sha256: prefixed_content_id(&grant.source.content_id),
                        object_version: grant.object_version.clone(),
                        pack_offset: grant.source.object_offset,
                        encoded_length: grant.source.encoded_length,
                        decoded_length: grant.length,
                        codec: DataCodec::Raw,
                        checksum_sha256: grant.source.checksum_sha256.clone(),
                    }),
                })
            })
            .collect::<Result<Vec<_>, RemoteLoadError>>()?;
        let map = Arc::new(LayerRangeMap {
            registry_objects: None,
            format_version: first.backing_key.format_version,
            kind: match first.backing_key.kind {
                LayerKind::Memory => crate::snapshot::metadata::LayerKind::Memory,
                LayerKind::Block => crate::snapshot::metadata::LayerKind::Block,
            },
            content_sha256: prefixed_content_id(&first.backing_key.content_sha256),
            logical_bytes: first.backing_key.logical_size,
            unit_size_bytes: first.backing_key.unit_size,
            ranges,
            zfile: None,
            registry: None,
        });
        source.snapshot_reader.open_layer(map)
    };
    let context = LazyLoadContext::new(
        source.provider,
        first.runtime.clone(),
        first.p2p_source.clone(),
    )
    .with_provider_timeout(provider_load_timeout);
    let request = SnapshotReadRange {
        kind: match first.backing_key.kind {
            LayerKind::Memory => crate::snapshot::metadata::LayerKind::Memory,
            LayerKind::Block => crate::snapshot::metadata::LayerKind::Block,
        },
        layer_content_sha256: &first.backing_key.content_sha256,
        logical_offset: first.target_offset,
        length: range.length(),
    };
    let result = reader.read_data_range(&context, request).await;
    let result = match result {
        Ok(result) => result,
        Err(error) => {
            metrics.add_provider_error(kind);
            return Err(match error {
                LazyLoadError::InvalidRange | LazyLoadError::NonDataRange => {
                    RemoteLoadError::InvalidArgument(error.to_string())
                }
                LazyLoadError::IdentityConflict => {
                    metrics.add_integrity_violation();
                    RemoteLoadError::IdentityConflict
                }
                LazyLoadError::UnsupportedCodec => RemoteLoadError::UnsupportedCodec,
                LazyLoadError::ShortRead => RemoteLoadError::ShortRead,
                LazyLoadError::ChecksumMismatch => {
                    metrics.add_integrity_violation();
                    RemoteLoadError::ChecksumMismatch
                }
                LazyLoadError::LocalIo(message) => {
                    RemoteLoadError::Backing(BackingError::Io(io::Error::other(message)))
                }
                LazyLoadError::Provider(error) => {
                    if matches!(
                        &error,
                        RangeReadError::Failure {
                            class: RangeReadFailureClass::Integrity,
                            ..
                        }
                    ) {
                        metrics.add_integrity_violation();
                    }
                    RemoteLoadError::Provider(error.into_final_error())
                }
            });
        }
    };
    if retrying_failed_provider_load
        && (result.accounting.provider_bytes > 0 || result.accounting.registry_logical_bytes > 0)
    {
        metrics.add_provider_retry(kind);
    }
    metrics.add_local_cache_bytes(kind, result.accounting.cache_bytes);
    metrics.add_provider_bytes(kind, result.accounting.provider_bytes);
    metrics.add_p2p_bytes(kind, result.accounting.p2p_bytes);
    metrics.add_registry_logical_bytes(kind, result.accounting.registry_logical_bytes);
    if result.payload.len() as u64 != range.length() {
        return Err(RemoteLoadError::ShortRead);
    }
    let file = backing.try_clone_file()?;
    pwrite_all(&file, &result.payload, first.target_offset)
}

fn pwrite_all(file: &std::fs::File, payload: &[u8], offset: u64) -> Result<(), RemoteLoadError> {
    // This span is the native backing write itself; fetching and decoding the
    // payload, cloning the backing FD and residency publication are outside it.
    let timer = ReadPathTimer::start(ReadPathStage::BackingWrite);
    let result = pwrite_all_with(payload, offset, |remaining, current| {
        file.write_at(remaining, current)
    });
    timer.finish(
        if result.is_ok() {
            payload.len() as u64
        } else {
            0
        },
        result.is_ok(),
    );
    result
}

fn pwrite_all_with<F>(payload: &[u8], offset: u64, mut write: F) -> Result<(), RemoteLoadError>
where
    F: FnMut(&[u8], u64) -> io::Result<usize>,
{
    let mut written = 0_usize;
    while written < payload.len() {
        let current = offset
            .checked_add(written as u64)
            .ok_or_else(|| RemoteLoadError::InvalidArgument("write offset overflows".into()))?;
        let count = write(&payload[written..], current)?;
        if count == 0 {
            return Err(RemoteLoadError::WriteNoProgress);
        }
        if count > payload.len() - written {
            return Err(RemoteLoadError::Write(io::Error::new(
                io::ErrorKind::InvalidData,
                "pwrite returned an impossible byte count",
            )));
        }
        written += count;
    }
    Ok(())
}

fn map_load_failure(message: String) -> RemoteLoadError {
    // A wide memory read is represented by an outer exact residency attempt
    // wrapping an inner group attempt. Preserve the inner typed failure when
    // the registry has serialized it through one or more RangeError displays.
    let mut detail = message.as_str();
    while let Some(inner) = detail.strip_prefix("range load failed: ") {
        detail = inner;
    }
    if detail == "payload checksum mismatch" {
        RemoteLoadError::ChecksumMismatch
    } else if detail == "unsupported codec" {
        RemoteLoadError::UnsupportedCodec
    } else if detail == "provider returned a short object range" {
        RemoteLoadError::ShortRead
    } else if detail == "backing write made no progress" {
        RemoteLoadError::WriteNoProgress
    } else if let Some(error) = RangeReadError::from_stable_display(detail) {
        // The residency registry stores one bounded display string for joined
        // callers. Recover only our fixed typed envelope; never inspect the
        // diagnostic message or object key to make retry decisions.
        RemoteLoadError::Provider(error)
    } else {
        RemoteLoadError::Worker(message)
    }
}

impl RangeLoadExecutor {
    pub async fn remote_load(
        self: &Arc<Self>,
        runtime: &RuntimeIdentity,
        request: RemoteLoadRequest,
    ) -> Result<RangeLoadOutcome, RemoteLoadError> {
        self.load_range(runtime, request, None)
            .await
            .map(|result| result.outcome)
    }

    pub async fn remote_load_with_coverage(
        self: &Arc<Self>,
        runtime: &RuntimeIdentity,
        request: RemoteLoadRequest,
    ) -> Result<RemoteLoadResult, RemoteLoadError> {
        self.load_range(runtime, request, None).await
    }
}

#[cfg(test)]
mod load_tests {
    //! Private remote-load unit tests.

    mod ownership {
        use super::super::*;
        use crate::snapshot::io::range_io::RangeReadResult;
        use crate::snapshot::metadata::{
            compute_layer_content_sha256, sha256_identity, DataSource, LayerKind as FormatLayerKind,
        };
        use std::sync::atomic::AtomicBool;

        struct TestRoot(PathBuf);

        impl TestRoot {
            fn new() -> Self {
                let path = std::env::temp_dir()
                    .join(format!("snapd-layer-ownership-{}", uuid::Uuid::new_v4()));
                fs::create_dir_all(&path).unwrap();
                Self(path)
            }
        }

        impl Drop for TestRoot {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.0);
            }
        }

        struct Provider {
            calls: AtomicUsize,
            block: bool,
            entered: Notify,
            resume: Notify,
            forgotten: AtomicBool,
        }

        #[async_trait::async_trait]
        impl RangeReader for Provider {
            async fn read_range(
                &self,
                request: RangeReadRequest<'_>,
            ) -> Result<RangeReadResult, RangeReadError> {
                self.calls.fetch_add(1, Ordering::SeqCst);
                self.entered.notify_one();
                if self.block {
                    self.resume.notified().await;
                }
                Ok(RangeReadResult::provider(vec![
                    0xab;
                    request.length as usize
                ]))
            }

            async fn forget_runtime(&self, _runtime: &RuntimeIdentity) {
                self.forgotten.store(true, Ordering::SeqCst);
            }
        }

        fn fixture(
            root: &TestRoot,
            block: bool,
        ) -> (Arc<RangeLoadExecutor>, Arc<Provider>, Arc<LayerRangeMap>) {
            let provider = Arc::new(Provider {
                calls: AtomicUsize::new(0),
                block,
                entered: Notify::new(),
                resume: Notify::new(),
                forgotten: AtomicBool::new(false),
            });
            let metrics = SnapdMetrics::new("layer-ownership").unwrap();
            let backing = LocalBackingRegistry::new_with_metrics(
                root.0.join("backings"),
                "ownership",
                Some(Arc::clone(&metrics)),
            )
            .unwrap();
            let service = RangeLoadExecutor::new_with_metrics(
                backing,
                SnapshotIo::data_only(),
                HashMap::from([("fixture".into(), provider.clone() as Arc<dyn RangeReader>)]),
                DEFAULT_MAX_RANGE_BYTES,
                metrics,
            )
            .unwrap();
            let payload = vec![0xab; 4096];
            let mut map = LayerRangeMap {
                registry_objects: None,
                format_version: 1,
                kind: FormatLayerKind::Memory,
                content_sha256: String::new(),
                logical_bytes: 8192,
                unit_size_bytes: 4096,
                registry: None,
                zfile: None,
                ranges: (0..2)
                    .map(|unit| LayerRange {
                        kind: RangeKind::Data,
                        logical_offset: unit * 4096,
                        length: 4096,
                        source: Some(DataSource {
                            object_key: "ownership/payload.pack".into(),
                            object_sha256: sha256_identity(&vec![0xab; 8192]),
                            object_version: None,
                            pack_offset: unit * 4096,
                            encoded_length: 4096,
                            decoded_length: 4096,
                            codec: DataCodec::Raw,
                            checksum_sha256: sha256_identity(&payload),
                        }),
                    })
                    .collect(),
            };
            map.content_sha256 = compute_layer_content_sha256(&map).unwrap();
            (service, provider, Arc::new(map))
        }

        fn target(
            runtime: &RuntimeIdentity,
            id: &str,
            map: &LayerRangeMap,
        ) -> RemoteLayerTargetGrant {
            RemoteLayerTargetGrant {
                runtime: runtime.clone(),
                target_id: id.into(),
                backing_key: LayerBackingIdentity {
                    kind: LayerKind::Memory,
                    format_version: map.format_version,
                    logical_size: map.logical_bytes,
                    unit_size: map.unit_size_bytes,
                    content_sha256: map.content_sha256.trim_start_matches("sha256:").into(),
                },
                provider: "fixture".into(),
                authorization: None,
            }
        }

        fn request(id: &str) -> RemoteLoadRequest {
            RemoteLoadRequest {
                target_id: id.into(),
                target_offset: 0,
                length: 4096,
                origin: "uffd_missing".into(),
            }
        }

        #[tokio::test]
        async fn weak_indexes_do_not_own_legacy_grants_or_revoke_replacements() {
            let root = TestRoot::new();
            let (service, _, map) = fixture(&root, false);
            let runtime = RuntimeIdentity::new("weak-owner");
            let source = &map.ranges[0].source.as_ref().unwrap();
            let grant = RemoteLoadGrant {
                runtime: runtime.clone(),
                target_id: "same-target".into(),
                backing_key: target(&runtime, "same-target", &map).backing_key,
                target_offset: 0,
                length: 4096,
                source: RemoteRangeSource {
                    content_id: source.object_sha256.trim_start_matches("sha256:").into(),
                    object_key: source.object_key.clone(),
                    object_offset: 0,
                    encoded_length: 4096,
                    checksum_sha256: source.checksum_sha256.trim_start_matches("sha256:").into(),
                    codec: "raw".into(),
                },
                provider: "fixture".into(),
                object_version: None,
                authorization: None,
                p2p_source: None,
            };
            let (first, created) = service.grant_target(grant.clone()).await.unwrap();
            assert!(created);
            let weak = Arc::downgrade(&first);
            let backing = first.backing().unwrap();
            drop(first);
            assert!(
                weak.upgrade().is_none(),
                "indexes must not retain their layer owner"
            );
            assert!(
                backing.is_closed(),
                "the last resource owner releases its lease"
            );
            assert!(matches!(
                service.remote_load(&runtime, request("same-target")).await,
                Err(RemoteLoadError::TargetNotFound)
            ));
            let (old, created) = service.grant_target(grant.clone()).await.unwrap();
            assert!(created);
            assert!(service.revoke_layer_resources(&old).await);
            let (replacement, created) = service.grant_target(grant).await.unwrap();
            assert!(created);
            assert!(
                !service.revoke_layer_resources(&old).await,
                "late rollback must not revoke a replacement with the same target ID"
            );
            assert!(replacement.backing().is_some());
            assert!(Arc::ptr_eq(
                &service.state.lock().await.layer_targets["same-target"]
                    .upgrade()
                    .unwrap(),
                &replacement,
            ));
        }

        #[tokio::test]
        async fn snapshot_pin_and_child_keep_the_same_reader_and_backing_after_source_stop() {
            let root = TestRoot::new();
            let (service, provider, map) = fixture(&root, false);
            let source_runtime = RuntimeIdentity::new("source-vm");
            let (source, _) = service
                .prepare_target(target(&source_runtime, "source", &map))
                .await
                .unwrap();
            service
                .install_layer_capability("source", &source_runtime, Arc::clone(&map), None)
                .await
                .unwrap();
            let pin_runtime = RuntimeIdentity::new("snapshot-pin:capture");
            let (pin, _) = service
                .clone_layer_target("source", &pin_runtime, "pin")
                .await
                .unwrap();
            let reader = pin.reader().unwrap();
            assert!(Arc::ptr_eq(&reader, &source.reader().unwrap()));
            let backing = pin.backing().unwrap();
            let inode = backing
                .try_clone_readonly_file()
                .unwrap()
                .metadata()
                .unwrap()
                .ino();
            assert_eq!(service.revoke_runtime(&source_runtime).await, 1);
            assert!(source.backing().is_none());
            assert_eq!(provider.calls.load(Ordering::SeqCst), 0);
            service
                .remote_load(&pin_runtime, request("pin"))
                .await
                .unwrap();
            assert_eq!(
                provider.calls.load(Ordering::SeqCst),
                1,
                "snapshot ancestry must still load from the provider after source stop"
            );
            let mut data = vec![0; 8192];
            pin.readonly_file()
                .unwrap()
                .read_exact_at(&mut data, 0)
                .unwrap();
            assert_eq!(data, vec![0xab; 8192]);

            let child_runtime = RuntimeIdentity::new("fork-child");
            let (child, _) = service
                .clone_layer_target("pin", &child_runtime, "child")
                .await
                .unwrap();
            assert!(Arc::ptr_eq(&reader, &child.reader().unwrap()));
            assert!(Arc::ptr_eq(&backing, &child.backing().unwrap()));
            assert_eq!(
                child.readonly_file().unwrap().metadata().unwrap().ino(),
                inode
            );
            assert!(service.revoke_layer_resources(&child).await);
            service
                .remote_load(&pin_runtime, request("pin"))
                .await
                .unwrap();
            assert_eq!(provider.calls.load(Ordering::SeqCst), 1);
            let weak_pin = Arc::downgrade(&pin);
            drop((pin, child));
            assert!(weak_pin.upgrade().is_none());
            assert!(
                backing.is_closed(),
                "source/child revocation plus final pin drop releases backing"
            );
            assert!(service
                .metrics
                .render(false)
                .lines()
                .any(|line| line == "rock_snapd_leases{kind=\"backing\"} 0"));
        }

        #[tokio::test]
        async fn cancelled_caller_keeps_provider_load_pinned_until_runtime_cleanup_drains() {
            let root = TestRoot::new();
            let (service, provider, map) = fixture(&root, true);
            let runtime = RuntimeIdentity::new("cancelled-load");
            let (resources, _) = service
                .prepare_target(target(&runtime, "target", &map))
                .await
                .unwrap();
            service
                .install_layer_capability("target", &runtime, map, None)
                .await
                .unwrap();
            let backing = resources.backing().unwrap();
            let task_service = Arc::clone(&service);
            let task_runtime = runtime.clone();
            let entered = provider.entered.notified();
            let caller = tokio::spawn(async move {
                task_service
                    .remote_load(&task_runtime, request("target"))
                    .await
            });
            tokio::time::timeout(Duration::from_secs(1), entered)
                .await
                .unwrap();
            caller.abort();
            assert_eq!(service.revoke_runtime(&runtime).await, 1);
            drop(resources);
            assert!(!backing.is_closed());
            assert!(!provider.forgotten.load(Ordering::SeqCst));
            let begin_service = Arc::clone(&service);
            let begin_runtime = runtime.clone();
            let mut begin =
                tokio::spawn(async move { begin_service.begin_runtime(&begin_runtime).await });
            assert!(tokio::time::timeout(Duration::from_millis(20), &mut begin)
                .await
                .is_err());
            provider.resume.notify_one();
            tokio::time::timeout(Duration::from_secs(1), begin)
                .await
                .unwrap()
                .unwrap()
                .unwrap();
            assert!(provider.forgotten.load(Ordering::SeqCst));
            assert!(backing.is_closed());
        }
    }

    mod format_reader {
        //! Proves that a snapshot-supplied reader reaches the real demand-load write
        //! path rather than being replaced by a default reader built from the map.

        use super::super::*;

        use crate::snapshot::io::range_io::RangeReadResult;
        use crate::snapshot::metadata::{
            compute_layer_content_sha256, sha256_identity, DataSource,
            LayerKind as FormatLayerKind, LazyReadAccounting, LazyReadResult,
        };

        struct TestRoot(PathBuf);

        impl Drop for TestRoot {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.0);
            }
        }

        #[derive(Default)]
        struct UnusedProvider {
            calls: AtomicUsize,
        }

        #[async_trait::async_trait]
        impl RangeReader for UnusedProvider {
            async fn read_range(
                &self,
                _request: RangeReadRequest<'_>,
            ) -> Result<RangeReadResult, RangeReadError> {
                self.calls.fetch_add(1, Ordering::SeqCst);
                Err(RangeReadError::NotFound)
            }
        }

        #[derive(Debug)]
        struct SnapshotSuppliedReader {
            calls: AtomicUsize,
            layer_identity: String,
            payload: Vec<u8>,
        }

        #[async_trait::async_trait]
        impl LazyLoadInterface for SnapshotSuppliedReader {
            async fn read_data_range(
                &self,
                _context: &LazyLoadContext,
                request: SnapshotReadRange<'_>,
            ) -> Result<LazyReadResult, LazyLoadError> {
                self.calls.fetch_add(1, Ordering::SeqCst);
                assert_eq!(request.kind, FormatLayerKind::Memory);
                assert_eq!(
                    request.layer_content_sha256.trim_start_matches("sha256:"),
                    self.layer_identity.trim_start_matches("sha256:")
                );
                // Logical offset deliberately differs from the map's physical offset.
                assert_eq!((request.logical_offset, request.length), (4096, 4096));
                Ok(LazyReadResult {
                    payload: self.payload.clone(),
                    accounting: LazyReadAccounting {
                        cache_bytes: self.payload.len() as u64,
                        ..Default::default()
                    },
                })
            }
        }

        #[tokio::test]
        async fn supplied_format_reader_is_used_by_demand_load_and_materializes_logical_backing() {
            let root = TestRoot(
                std::env::temp_dir().join(format!("rock-format-reader-{}", uuid::Uuid::new_v4())),
            );
            let payload = vec![0xa7; 4096];
            let mut map = LayerRangeMap {
                registry_objects: None,
                format_version: 1,
                kind: FormatLayerKind::Memory,
                content_sha256: String::new(),
                logical_bytes: 8192,
                unit_size_bytes: 4096,
                ranges: vec![
                    LayerRange {
                        kind: RangeKind::Zero,
                        logical_offset: 0,
                        length: 4096,
                        source: None,
                    },
                    LayerRange {
                        kind: RangeKind::Data,
                        logical_offset: 4096,
                        length: 4096,
                        source: Some(DataSource {
                            object_key: "unavailable-provider.pack".into(),
                            object_sha256: sha256_identity(b"immutable source identity"),
                            object_version: None,
                            pack_offset: 12288,
                            encoded_length: 4096,
                            decoded_length: 4096,
                            codec: DataCodec::Raw,
                            checksum_sha256: sha256_identity(&payload),
                        }),
                    },
                ],
                zfile: None,
                registry: None,
            };
            map.content_sha256 = compute_layer_content_sha256(&map).unwrap();
            let reader = Arc::new(SnapshotSuppliedReader {
                calls: AtomicUsize::new(0),
                layer_identity: map.content_sha256.clone(),
                payload: payload.clone(),
            });
            let provider = Arc::new(UnusedProvider::default());
            let runtime = RuntimeIdentity::new("interface-reader-runtime");
            let target_id = "interface-reader-target";
            let registry =
                LocalBackingRegistry::new(root.0.join("work"), "interface-reader-epoch").unwrap();
            let service = RangeLoadExecutor::new(
                registry,
                SnapshotIo::data_only(),
                HashMap::from([("fixture".into(), provider.clone() as Arc<dyn RangeReader>)]),
                DEFAULT_MAX_RANGE_BYTES,
            )
            .unwrap();
            let (_resources, _) = service
                .prepare_target(RemoteLayerTargetGrant {
                    runtime: runtime.clone(),
                    target_id: target_id.into(),
                    backing_key: LayerBackingIdentity {
                        kind: LayerKind::Memory,
                        format_version: 1,
                        logical_size: map.logical_bytes,
                        unit_size: map.unit_size_bytes,
                        content_sha256: map.content_sha256.trim_start_matches("sha256:").into(),
                    },
                    provider: "fixture".into(),
                    authorization: None,
                })
                .await
                .unwrap();
            service
                .install_layer_capability_with_reader(
                    target_id,
                    &runtime,
                    Arc::new(map),
                    None,
                    reader.clone(),
                )
                .await
                .unwrap();

            for _ in 0..2 {
                service
                    .remote_load(
                        &runtime,
                        RemoteLoadRequest {
                            target_id: target_id.into(),
                            target_offset: 4096,
                            length: 4096,
                            origin: "uffd_missing".into(),
                        },
                    )
                    .await
                    .unwrap();
            }
            assert_eq!(reader.calls.load(Ordering::SeqCst), 1);
            assert_eq!(provider.calls.load(Ordering::SeqCst), 0);

            let backing = service
                .backing_for_target(target_id, &runtime)
                .await
                .unwrap();
            let status = backing
                .downloads
                .status(&DataRangeKey::new(&backing.backing_id, 4096, 4096).unwrap())
                .await;
            assert_eq!(status.state, DownloadState::Resident);
            let mut bytes = vec![0; 8192];
            backing
                .try_clone_file()
                .unwrap()
                .read_exact_at(&mut bytes, 0)
                .unwrap();
            assert!(bytes[..4096].iter().all(|byte| *byte == 0));
            assert_eq!(&bytes[4096..], payload.as_slice());
            service.close().await;
        }
    }

    mod local_cache {
        use super::super::*;
        use crate::snapshot::format::overlaybd::{
            decode_descriptor, encode_descriptor, encode_zfile_descriptor, plan_layer,
        };
        use crate::snapshot::io::range_io::RangeReadResult;
        use crate::snapshot::metadata::{
            compute_layer_content_sha256, sha256_identity, DataSource, ObjectRef,
        };
        use crate::snapshot::transport::posix_file::LocalFileCache;
        use std::os::unix::fs::PermissionsExt;

        struct TestDirectory(PathBuf);

        impl TestDirectory {
            fn new() -> Self {
                let path = std::env::temp_dir().join(format!(
                    "snapd-local-restore-{}",
                    uuid::Uuid::new_v4().simple()
                ));
                fs::create_dir_all(&path).unwrap();
                Self(path)
            }
        }

        impl Drop for TestDirectory {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.0);
            }
        }

        struct Fixture {
            descriptor: Vec<u8>,
            blob: Vec<u8>,
            raw: Vec<u8>,
            layer: LayerRangeMap,
        }

        fn fixture(root: &std::path::Path) -> Fixture {
            let mut raw = vec![3_u8; 8192];
            raw[4096..].fill(9);
            let mut layer = LayerRangeMap {
                registry_objects: None,
                format_version: 1,
                kind: crate::snapshot::metadata::LayerKind::Memory,
                content_sha256: String::new(),
                logical_bytes: raw.len() as u64,
                unit_size_bytes: 4096,
                registry: None,
                zfile: None,
                ranges: raw
                    .chunks_exact(4096)
                    .enumerate()
                    .map(|(page, payload)| LayerRange {
                        kind: RangeKind::Data,
                        logical_offset: page as u64 * 4096,
                        length: 4096,
                        source: Some(DataSource {
                            object_key: "local/provisional.pack".into(),
                            object_sha256: sha256_identity(b"provisional"),
                            object_version: None,
                            pack_offset: 0,
                            encoded_length: 4096,
                            decoded_length: 4096,
                            codec: DataCodec::Raw,
                            checksum_sha256: sha256_identity(payload),
                        }),
                    })
                    .collect(),
            };
            layer.content_sha256 = compute_layer_content_sha256(&layer).unwrap();
            let plan = plan_layer(&layer).unwrap();
            let raw_path = root.join("source.raw");
            fs::write(&raw_path, &raw).unwrap();
            let blob = plan
                .read_at(
                    &File::open(raw_path).unwrap(),
                    0,
                    plan.stored_bytes as usize,
                )
                .unwrap();
            let sha256 = sha256_identity(&blob);
            let object = ObjectRef {
                object_key: format!(
                    "layers/memory/1/{}/data/{}.commit",
                    &layer.content_sha256[7..],
                    &sha256[7..]
                ),
                sha256,
                object_version: Some("fixture-version".into()),
            };
            let descriptor = encode_descriptor(&layer, &plan, &object).unwrap();
            Fixture {
                layer: decode_descriptor(&descriptor).unwrap(),
                descriptor,
                blob,
                raw,
            }
        }

        fn compressed_fixture(root: &std::path::Path) -> Fixture {
            let mut fixture = fixture(root);
            let plan = plan_layer(&fixture.layer).unwrap();
            let file = File::options()
                .read(true)
                .write(true)
                .create_new(true)
                .open(root.join("fixture.zfile"))
                .unwrap();
            let encoded = crate::snapshot::format::overlaybd::zfile::encode_with_reader(
                fixture.blob.len() as u64,
                &file,
                |offset, count| Ok(fixture.blob[offset as usize..offset as usize + count].to_vec()),
            )
            .unwrap();
            let reference = ObjectRef {
                object_key: format!(
                    "layers/memory/1/{}/data/{}.commit",
                    &fixture.layer.content_sha256[7..],
                    &encoded.sha256[7..]
                ),
                sha256: encoded.sha256,
                object_version: Some("fixture-zfile-version".into()),
            };
            fixture.descriptor =
                encode_zfile_descriptor(&fixture.layer, &plan, &reference, &encoded.index).unwrap();
            fixture.layer = decode_descriptor(&fixture.descriptor).unwrap();
            fixture.blob = vec![0; encoded.index.encoded_bytes as usize];
            file.read_exact_at(&mut fixture.blob, 0).unwrap();
            fixture
        }

        struct FixtureProvider {
            blob: Vec<u8>,
            calls: AtomicUsize,
            fail: bool,
            corrupt: bool,
        }

        impl FixtureProvider {
            fn new(fixture: &Fixture, fail: bool, corrupt: bool) -> Arc<Self> {
                Arc::new(Self {
                    blob: fixture.blob.clone(),
                    calls: AtomicUsize::new(0),
                    fail,
                    corrupt,
                })
            }
        }

        trait FixtureSource: Send + Sync {
            fn with_cache(self: Arc<Self>, cache: Arc<LocalFileCache>) -> Arc<dyn RangeReader>;
        }

        impl FixtureSource for FixtureProvider {
            fn with_cache(self: Arc<Self>, cache: Arc<LocalFileCache>) -> Arc<dyn RangeReader> {
                crate::snapshot::io::range_io::RangeIO::new_with_cache(self, None, Some(cache))
            }
        }

        #[async_trait::async_trait]
        impl crate::snapshot::transport::PhysicalRangeReader for FixtureProvider {
            fn source_id(&self) -> &str {
                "physical-fixture"
            }
            async fn read_physical_range(
                &self,
                request: crate::snapshot::transport::PhysicalRangeRequest<'_>,
            ) -> Result<Vec<u8>, RangeReadError> {
                self.calls.fetch_add(1, Ordering::Relaxed);
                if self.fail {
                    return Err(RangeReadError::NotFound);
                }
                let mut payload = self.blob
                    [request.offset as usize..(request.offset + request.length) as usize]
                    .to_vec();
                if self.corrupt {
                    let corrupt_at = payload.len() / 2;
                    payload[corrupt_at] ^= 0xff;
                }
                Ok(payload)
            }
        }

        async fn load_fixture(
            root: &std::path::Path,
            cache: Arc<LocalFileCache>,
            fixture: &Fixture,
            provider: Arc<dyn FixtureSource>,
        ) -> (Result<(), RemoteLoadError>, Vec<u8>, Arc<SnapdMetrics>) {
            load_fixture_with_timeout(root, cache, fixture, provider, Duration::from_secs(1)).await
        }

        async fn load_fixture_with_timeout(
            root: &std::path::Path,
            cache: Arc<LocalFileCache>,
            fixture: &Fixture,
            provider: Arc<dyn FixtureSource>,
            provider_timeout: Duration,
        ) -> (Result<(), RemoteLoadError>, Vec<u8>, Arc<SnapdMetrics>) {
            let key = LayerBackingIdentity {
                kind: LayerKind::Memory,
                format_version: 1,
                logical_size: fixture.layer.logical_bytes,
                unit_size: fixture.layer.unit_size_bytes,
                content_sha256: fixture.layer.content_sha256[7..].into(),
            };
            let registry = LocalBackingRegistry::new(root.join("work"), "test-epoch").unwrap();
            let runtime = RuntimeIdentity::new("local-cache-test");
            let metrics = SnapdMetrics::new("local-cache-test").unwrap();
            let provider = provider.with_cache(cache);
            let service = RangeLoadExecutor::new_with_metrics_and_timeout(
                registry,
                SnapshotIo::data_only(),
                HashMap::from([("fixture".into(), provider)]),
                DEFAULT_MAX_RANGE_BYTES,
                provider_timeout,
                Arc::clone(&metrics),
            )
            .unwrap();
            let target_id = "local-cache-target";
            let (_resources, _) = service
                .prepare_target(RemoteLayerTargetGrant {
                    runtime: runtime.clone(),
                    target_id: target_id.into(),
                    backing_key: key,
                    provider: "fixture".into(),
                    authorization: None,
                })
                .await
                .unwrap();
            service
                .install_layer_capability(
                    target_id,
                    &runtime,
                    Arc::new(fixture.layer.clone()),
                    None,
                )
                .await
                .unwrap();
            let result = service
                .remote_load(
                    &runtime,
                    RemoteLoadRequest {
                        target_id: target_id.into(),
                        target_offset: 0,
                        length: 4096,
                        origin: "uffd_missing".into(),
                    },
                )
                .await
                .map(|_| ());
            let backing = service
                .backing_for_target(target_id, &runtime)
                .await
                .unwrap();
            for offset in [0, 4096] {
                let status = backing
                    .downloads
                    .status(&DataRangeKey::new(&backing.backing_id, offset, 4096).unwrap())
                    .await;
                assert_eq!(status.state == DownloadState::Resident, result.is_ok());
            }
            let mut native = vec![0; fixture.raw.len()];
            backing
                .try_clone_file()
                .unwrap()
                .read_exact_at(&mut native, 0)
                .unwrap();
            (result, native, metrics)
        }

        fn partial_file(root: &std::path::Path) -> PathBuf {
            for entry in fs::read_dir(root).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    if let Some(found) = find_partial(&path) {
                        return found;
                    }
                }
            }
            panic!("write-through did not create a physical range cache entry")
        }

        fn find_partial(root: &std::path::Path) -> Option<PathBuf> {
            for entry in fs::read_dir(root).ok()? {
                let path = entry.ok()?.path();
                if path
                    .extension()
                    .is_some_and(|extension| extension == "partial")
                {
                    return Some(path);
                }
                if path.is_dir() {
                    if let Some(found) = find_partial(&path) {
                        return Some(found);
                    }
                }
            }
            None
        }

        #[tokio::test]
        async fn coalesced_memory_uses_verified_local_bytes_after_cache_restart() {
            let root = TestDirectory::new();
            let fixture = fixture(&root.0);
            let cache_root = root.0.join("cache");
            let cache = LocalFileCache::new(cache_root.clone(), "fixture-domain").unwrap();
            let provider = FixtureProvider::new(&fixture, false, false);
            let (result, native, _) =
                load_fixture(&root.0, cache, &fixture, provider.clone()).await;
            result.unwrap();
            assert_eq!(native, fixture.raw);
            assert_eq!(provider.calls.load(Ordering::Relaxed), 1);

            // A fresh cache and native memfd cannot inherit process-local residency.
            let cache = LocalFileCache::new(cache_root, "fixture-domain").unwrap();
            let offline_provider = FixtureProvider::new(&fixture, true, false);
            let (result, native, metrics) =
                load_fixture(&root.0, cache, &fixture, offline_provider.clone()).await;
            result.unwrap();
            assert_eq!(native, fixture.raw);
            assert_eq!(offline_provider.calls.load(Ordering::Relaxed), 0);
            let rendered = metrics.render(false);
            assert!(rendered.lines().any(|line| {
                line.starts_with("rock_snapd_local_cache_bytes_total{")
                    && line.contains("kind=\"memory\"")
                    && line.ends_with(" 8192")
            }));
            assert!(rendered.lines().any(|line| {
                line.starts_with("rock_snapd_provider_bytes_total{")
                    && line.contains("kind=\"memory\"")
                    && line.ends_with(" 0")
            }));
        }

        #[tokio::test]
        async fn cached_data_does_not_consume_the_provider_io_deadline() {
            let root = TestDirectory::new();
            let fixture = fixture(&root.0);
            let cache_root = root.0.join("cache");
            let cache = LocalFileCache::new(cache_root.clone(), "fixture-domain").unwrap();
            let online = FixtureProvider::new(&fixture, false, false);
            load_fixture(&root.0, cache, &fixture, online)
                .await
                .0
                .unwrap();

            let cache = LocalFileCache::new(cache_root, "fixture-domain").unwrap();
            let offline = FixtureProvider::new(&fixture, true, false);
            // The cache lookup uses spawn_blocking and takes longer than this
            // budget. Only actual provider I/O may time out; a cache hit succeeds.
            let (result, native, _) = load_fixture_with_timeout(
                &root.0,
                cache,
                &fixture,
                offline.clone(),
                Duration::from_nanos(1),
            )
            .await;
            result.unwrap();
            assert_eq!(native, fixture.raw);
            assert_eq!(offline.calls.load(Ordering::Relaxed), 0);
        }

        #[tokio::test]
        async fn corrupt_local_unit_falls_back_and_repairs_without_aggregate_checksum() {
            let root = TestDirectory::new();
            let fixture = fixture(&root.0);
            let cache_root = root.0.join("cache");
            let cache = LocalFileCache::new(cache_root.clone(), "fixture-domain").unwrap();
            let provider = FixtureProvider::new(&fixture, false, false);
            load_fixture(&root.0, cache, &fixture, provider)
                .await
                .0
                .unwrap();
            let cache_entry = partial_file(&cache_root);
            fs::set_permissions(&cache_entry, fs::Permissions::from_mode(0o600)).unwrap();
            let corrupted = OpenOptions::new()
                .read(true)
                .write(true)
                .open(cache_entry)
                .unwrap();
            let last = corrupted.metadata().unwrap().len() - 1;
            let mut byte = [0];
            corrupted.read_exact_at(&mut byte, last).unwrap();
            byte[0] ^= 0x80;
            corrupted.write_all_at(&byte, last).unwrap();

            let cache = LocalFileCache::new(cache_root.clone(), "fixture-domain").unwrap();
            let provider = FixtureProvider::new(&fixture, false, false);
            let (result, native, _) =
                load_fixture(&root.0, cache, &fixture, provider.clone()).await;
            result.unwrap();
            assert_eq!(native, fixture.raw);
            assert_eq!(provider.calls.load(Ordering::Relaxed), 1);

            let cache = LocalFileCache::new(cache_root, "fixture-domain").unwrap();
            let offline_provider = FixtureProvider::new(&fixture, true, false);
            let (result, native, _) =
                load_fixture(&root.0, cache, &fixture, offline_provider.clone()).await;
            result.unwrap();
            assert_eq!(native, fixture.raw);
            assert_eq!(offline_provider.calls.load(Ordering::Relaxed), 0);
        }

        #[tokio::test]
        async fn corrupt_coalesced_provider_unit_never_populates_native_or_local_cache() {
            let root = TestDirectory::new();
            let fixture = fixture(&root.0);
            let cache_root = root.0.join("cache");
            let cache = LocalFileCache::new(cache_root.clone(), "fixture-domain").unwrap();
            let provider = FixtureProvider::new(&fixture, false, true);
            let (result, native, _) =
                load_fixture(&root.0, cache, &fixture, provider.clone()).await;
            assert!(matches!(result, Err(RemoteLoadError::ChecksumMismatch)));
            assert_eq!(native, vec![0; fixture.raw.len()]);
            assert_eq!(provider.calls.load(Ordering::Relaxed), 2);

            let cache = LocalFileCache::new(cache_root, "fixture-domain").unwrap();
            let provider = FixtureProvider::new(&fixture, false, false);
            let (result, native, _) =
                load_fixture(&root.0, cache, &fixture, provider.clone()).await;
            result.unwrap();
            assert_eq!(native, fixture.raw);
            assert_eq!(provider.calls.load(Ordering::Relaxed), 1);
        }
        #[tokio::test]
        async fn compressed_coalesced_memory_survives_restart_and_repairs_corrupt_encoded_cache() {
            let root = TestDirectory::new();
            let fixture = compressed_fixture(&root.0);
            let cache_root = root.0.join("cache");
            let cache = LocalFileCache::new(cache_root.clone(), "compressed-domain").unwrap();
            let provider = FixtureProvider::new(&fixture, false, false);
            let wrapped = provider.clone();
            let (result, native, metrics) = load_fixture(&root.0, cache, &fixture, wrapped).await;
            result.unwrap();
            assert_eq!(native, fixture.raw);
            let index = fixture.layer.zfile.as_ref().unwrap();
            let offset = fixture.layer.ranges[0].source.as_ref().unwrap().pack_offset;
            let span = index.compressed_span(offset, 8192).unwrap();
            assert!(span.length < 8192);
            assert!(metrics
                .render(false)
                .lines()
                .any(|line| line.starts_with("rock_snapd_provider_bytes_total{")
                    && line.contains("kind=\"memory\"")
                    && line.ends_with(&format!(" {}", span.length))));
            let cache = LocalFileCache::new(cache_root.clone(), "compressed-domain").unwrap();
            let offline = FixtureProvider::new(&fixture, true, false);
            let wrapped = offline.clone();
            let (result, native, _) = load_fixture(&root.0, cache, &fixture, wrapped).await;
            result.unwrap();
            assert_eq!(native, fixture.raw);
            assert_eq!(offline.calls.load(Ordering::Relaxed), 0);
            let cache_entry = partial_file(&cache_root);
            fs::set_permissions(&cache_entry, fs::Permissions::from_mode(0o600)).unwrap();
            let corrupted = OpenOptions::new()
                .read(true)
                .write(true)
                .open(cache_entry)
                .unwrap();
            let last = corrupted.metadata().unwrap().len() - 1;
            let mut byte = [0];
            corrupted.read_exact_at(&mut byte, last).unwrap();
            byte[0] ^= 0x80;
            corrupted.write_all_at(&byte, last).unwrap();
            let cache = LocalFileCache::new(cache_root, "compressed-domain").unwrap();
            let provider = FixtureProvider::new(&fixture, false, false);
            let wrapped = provider.clone();
            let (result, native, _) = load_fixture(&root.0, cache, &fixture, wrapped).await;
            result.unwrap();
            assert_eq!(native, fixture.raw);
            assert_eq!(provider.calls.load(Ordering::Relaxed), 1);
        }

        #[tokio::test]
        async fn corrupt_compressed_provider_never_populates_native_or_partial() {
            let root = TestDirectory::new();
            let fixture = compressed_fixture(&root.0);
            let cache_root = root.0.join("cache");
            let cache = LocalFileCache::new(cache_root.clone(), "compressed-domain").unwrap();
            let provider = FixtureProvider::new(&fixture, false, true);
            let wrapped = provider;
            let (result, native, _) = load_fixture(&root.0, cache, &fixture, wrapped).await;
            assert!(matches!(
                result,
                Err(RemoteLoadError::Provider(RangeReadError::Failure {
                    class: RangeReadFailureClass::Integrity,
                    ..
                }))
            ));
            assert_eq!(native, vec![0; fixture.raw.len()]);
            assert!(find_partial(&cache_root).is_none());
        }

        #[tokio::test]
        async fn compressed_provider_uses_active_index_when_cache_descriptor_is_absent() {
            let root = TestDirectory::new();
            let fixture = compressed_fixture(&root.0);
            // Equivalent to an LRU eviction: the cache has no capability, but the
            // authenticated active map must retain independent decoding authority.
            let cache = LocalFileCache::new(root.0.join("cache"), "compressed-domain").unwrap();
            let provider = FixtureProvider::new(&fixture, false, false);
            let wrapped = provider.clone();
            let (result, native, _) = load_fixture(&root.0, cache, &fixture, wrapped).await;
            result.unwrap();
            assert_eq!(native, fixture.raw);
            assert_eq!(provider.calls.load(Ordering::Relaxed), 1);
        }

        struct DecodedPeer(Vec<u8>);
        impl FixtureSource for DecodedPeer {
            fn with_cache(self: Arc<Self>, _cache: Arc<LocalFileCache>) -> Arc<dyn RangeReader> {
                self
            }
        }
        #[async_trait::async_trait]
        impl RangeReader for DecodedPeer {
            async fn read_range(
                &self,
                request: RangeReadRequest<'_>,
            ) -> Result<RangeReadResult, RangeReadError> {
                assert!(request.physical.is_some());
                assert_eq!(request.length as usize, self.0.len());
                Ok(RangeReadResult::p2p(self.0.clone()))
            }
        }

        #[tokio::test]
        async fn decoded_peer_data_never_forges_compressed_partial_identity() {
            let root = TestDirectory::new();
            let fixture = compressed_fixture(&root.0);
            let cache_root = root.0.join("cache");
            let cache = LocalFileCache::new(cache_root.clone(), "compressed-domain").unwrap();
            let (result, native, metrics) = load_fixture(
                &root.0,
                cache,
                &fixture,
                Arc::new(DecodedPeer(fixture.raw.clone())),
            )
            .await;
            result.unwrap();
            assert_eq!(native, fixture.raw);
            assert!(metrics
                .render(false)
                .lines()
                .any(|line| line.starts_with("rock_snapd_p2p_bytes_total{")
                    && line.contains("kind=\"memory\"")
                    && line.ends_with(" 8192")));
            assert!(find_partial(&cache_root).is_none());
            let cache = LocalFileCache::new(cache_root, "compressed-domain").unwrap();
            let offline = FixtureProvider::new(&fixture, true, false);
            let wrapped = offline.clone();
            let (result, _, _) = load_fixture(&root.0, cache, &fixture, wrapped).await;
            assert!(result.is_err());
            assert_eq!(offline.calls.load(Ordering::Relaxed), 1);
        }
        #[derive(Clone, Copy)]
        enum RegistryReply {
            Verified,
            Unverified,
            Peer,
            Encoded,
            Short,
        }

        struct RegistryProvider {
            blob: Vec<u8>,
            reply: RegistryReply,
            calls: AtomicUsize,
            requests: StdMutex<Vec<(u64, u64)>>,
        }

        #[async_trait::async_trait]
        impl RangeReader for RegistryProvider {
            async fn read_range(
                &self,
                request: RangeReadRequest<'_>,
            ) -> Result<RangeReadResult, RangeReadError> {
                self.calls.fetch_add(1, Ordering::Relaxed);
                self.requests
                    .lock()
                    .unwrap()
                    .push((request.offset, request.length));
                assert_eq!(request.kind, RangeDataKind::Block);
                assert!(request.preferred_p2p_source.is_none());
                assert!(request.expected_range_sha256.is_empty());
                assert_eq!(request.authority, Some("example.com"));
                let physical = request
                    .physical
                    .expect("format must issue an encoded physical plan");
                assert_eq!(physical.namespace, Some("images/rootfs"));
                assert_eq!(physical.object_size, Some(self.blob.len() as u64));
                let mut payload = self.blob
                    [physical.offset as usize..(physical.offset + physical.length) as usize]
                    .to_vec();
                match self.reply {
                    RegistryReply::Verified => Ok(RangeReadResult::encoded(payload)),
                    RegistryReply::Unverified => Ok(RangeReadResult::provider(vec![
                        0x5a;
                        request.length
                            as usize
                    ])),
                    RegistryReply::Peer => {
                        Ok(RangeReadResult::p2p(vec![0x5a; request.length as usize]))
                    }
                    RegistryReply::Encoded => {
                        payload[0] ^= 0xff;
                        Ok(RangeReadResult::encoded(payload))
                    }
                    RegistryReply::Short => {
                        payload.pop();
                        Ok(RangeReadResult::encoded(payload))
                    }
                }
            }
        }

        async fn registry_service(
            root: &std::path::Path,
            reply: RegistryReply,
        ) -> (
            Arc<RangeLoadExecutor>,
            RuntimeIdentity,
            Arc<LayerRangeMap>,
            Arc<RegistryProvider>,
            Arc<SnapdMetrics>,
            Arc<LayerResources>,
        ) {
            let (native, blob) =
                crate::snapshot::format::overlaybd::composite::test_registry_rootfs_fixture(4);
            registry_service_with_native(root, reply, native, blob).await
        }

        async fn registry_service_with_native(
            root: &std::path::Path,
            reply: RegistryReply,
            native: Arc<crate::snapshot::format::overlaybd::composite::RegistryRootfs>,
            blob: Vec<u8>,
        ) -> (
            Arc<RangeLoadExecutor>,
            RuntimeIdentity,
            Arc<LayerRangeMap>,
            Arc<RegistryProvider>,
            Arc<SnapdMetrics>,
            Arc<LayerResources>,
        ) {
            let map = Arc::new(native.build_map().unwrap());
            let backing = LocalBackingRegistry::new(root.join("work"), "registry-test").unwrap();
            let metrics = SnapdMetrics::new("registry-test").unwrap();
            let provider = Arc::new(RegistryProvider {
                blob,
                reply,
                calls: AtomicUsize::new(0),
                requests: StdMutex::new(Vec::new()),
            });
            let service = RangeLoadExecutor::new_with_metrics_and_timeout(
                backing,
                SnapshotIo::data_only(),
                HashMap::from([("test".into(), provider.clone() as Arc<dyn RangeReader>)]),
                DEFAULT_MAX_RANGE_BYTES,
                Duration::from_secs(1),
                Arc::clone(&metrics),
            )
            .unwrap();
            let runtime = RuntimeIdentity::new("registry-test");
            let (resources, _) = service
                .prepare_target(RemoteLayerTargetGrant {
                    runtime: runtime.clone(),
                    target_id: "registry-target".into(),
                    provider: "test".into(),
                    authorization: None,
                    backing_key: LayerBackingIdentity {
                        kind: LayerKind::Block,
                        format_version: map.format_version,
                        logical_size: map.logical_bytes,
                        unit_size: map.unit_size_bytes,
                        content_sha256: map.content_sha256.trim_start_matches("sha256:").into(),
                    },
                })
                .await
                .unwrap();
            (service, runtime, map, provider, metrics, resources)
        }

        /// Record the shared executor's logical request while delegating the
        /// actual read to the real snapshot-selected format capability.
        #[derive(Debug)]
        struct RecordingRegistryLayer {
            reader: Arc<dyn LazyLoadInterface>,
            requests: Arc<StdMutex<Vec<(u64, u64)>>>,
        }

        #[async_trait::async_trait]
        impl LazyLoadInterface for RecordingRegistryLayer {
            async fn read_data_range(
                &self,
                context: &LazyLoadContext,
                request: SnapshotReadRange<'_>,
            ) -> Result<crate::snapshot::LazyReadResult, LazyLoadError> {
                self.requests
                    .lock()
                    .unwrap()
                    .push((request.logical_offset, request.length));
                self.reader.read_data_range(context, request).await
            }
        }

        async fn install_recording_registry_layer(
            service: &Arc<RangeLoadExecutor>,
            runtime: &RuntimeIdentity,
            map: &Arc<LayerRangeMap>,
        ) -> Arc<StdMutex<Vec<(u64, u64)>>> {
            let requests = Arc::new(StdMutex::new(Vec::new()));
            let reader = service.snapshot_reader.open_layer(Arc::clone(map));
            service
                .install_layer_capability_with_reader(
                    "registry-target",
                    runtime,
                    Arc::clone(map),
                    None,
                    Arc::new(RecordingRegistryLayer {
                        reader,
                        requests: Arc::clone(&requests),
                    }),
                )
                .await
                .unwrap();
            requests
        }

        fn registry_fixture_physical_spans(
            map: &LayerRangeMap,
            logical: &[(u64, u64)],
        ) -> Vec<(u64, u64)> {
            let native = map.registry.as_ref().unwrap();
            assert_eq!(native.layers().len(), 1);
            // This dense single-layer fixture stores DATA immediately after
            // the standard 4096-byte LSMT header. Its authenticated ZFile index
            // maps each expected logical window to the encoded blob span.
            logical
                .iter()
                .map(|&(offset, length)| {
                    let span = native.layers()[0]
                        .zfile
                        .compressed_span(4096 + offset, length)
                        .unwrap();
                    (span.offset, span.length)
                })
                .collect()
        }

        async fn read_registry_target(
            service: &Arc<RangeLoadExecutor>,
            runtime: &RuntimeIdentity,
            map: &LayerRangeMap,
        ) -> Result<RangeLoadOutcome, RemoteLoadError> {
            service
                .remote_load(
                    runtime,
                    RemoteLoadRequest {
                        target_id: "registry-target".into(),
                        target_offset: map.ranges[0].logical_offset,
                        length: map.unit_size_bytes,
                        origin: "virtio_blk_read_miss".into(),
                    },
                )
                .await
        }

        #[tokio::test]
        async fn verified_registry_data_materializes_native_without_fabricated_range_sha() {
            let root = TestDirectory::new();
            let (service, runtime, map, provider, metrics, _resources) =
                registry_service(&root.0, RegistryReply::Verified).await;
            let logical_requests = install_recording_registry_layer(&service, &runtime, &map).await;
            assert_eq!(map.ranges.len(), 4);
            assert!(map.ranges.iter().all(|range| range.length == 65536));
            read_registry_target(&service, &runtime, &map)
                .await
                .unwrap();
            let backing = service
                .backing_for_target("registry-target", &runtime)
                .await
                .unwrap();
            let mut bytes = vec![0; map.logical_bytes as usize];
            backing
                .try_clone_file()
                .unwrap()
                .read_exact_at(&mut bytes, 0)
                .unwrap();
            assert_eq!(bytes, vec![0x5a; map.logical_bytes as usize]);
            assert_eq!(provider.calls.load(Ordering::Relaxed), 1);
            let expected_logical = vec![(0, 4 * 65536)];
            assert_eq!(*logical_requests.lock().unwrap(), expected_logical);
            assert_eq!(
                *provider.requests.lock().unwrap(),
                registry_fixture_physical_spans(&map, &expected_logical),
            );
            assert_eq!(
                service
                    .remote_load(
                        &runtime,
                        RemoteLoadRequest {
                            target_id: "registry-target".into(),
                            target_offset: 65536,
                            length: 65536,
                            origin: "virtio_blk_read_miss".into(),
                        },
                    )
                    .await
                    .unwrap(),
                RangeLoadOutcome::CacheHit,
                "a neighbouring unit must reuse the materialized coalesced window"
            );
            assert_eq!(provider.calls.load(Ordering::Relaxed), 1);
            assert_eq!(
                *logical_requests.lock().unwrap(),
                expected_logical,
                "resident neighbour must not re-enter the format reader"
            );
            assert!(map
                .ranges
                .iter()
                .filter_map(|range| range.source.as_ref())
                .all(|source| source.codec == DataCodec::RegistryRootfs
                    && source.checksum_sha256.is_empty()));
            assert!(service.resident_object_ranges(&runtime).await.is_empty());
            assert_eq!(
                service
                    .resident_range_summary(&runtime)
                    .await
                    .servable_objects,
                0
            );
            let native = map.registry.as_ref().unwrap();
            assert!(service
                .read_resident_range(
                    &runtime,
                    &native.logical_object_key().unwrap(),
                    &native.content_sha256().unwrap(),
                    None,
                    0,
                    map.unit_size_bytes
                )
                .await
                .unwrap()
                .is_none());
            let rendered = metrics.render(false);
            assert!(rendered.lines().any(|line| line
                .starts_with("rock_snapd_registry_logical_bytes_total{")
                && line.contains("kind=\"block\"")
                && line.ends_with(&format!(" {}", map.logical_bytes))));
            assert!(rendered
                .lines()
                .any(|line| line.starts_with("rock_snapd_provider_bytes_total{")
                    && line.contains("kind=\"block\"")
                    && line.ends_with(" 0")));
        }

        #[tokio::test]
        async fn registry_demand_units_do_not_read_across_the_256k_window() {
            let root = TestDirectory::new();
            let (native, blob) =
                crate::snapshot::format::overlaybd::composite::test_registry_rootfs_fixture(10);
            let (service, runtime, map, provider, _, _resources) =
                registry_service_with_native(&root.0, RegistryReply::Verified, native, blob).await;
            assert_eq!(map.ranges.len(), 10);
            assert!(map.ranges.iter().all(|range| range.length == 65536));
            let logical_requests = install_recording_registry_layer(&service, &runtime, &map).await;
            // One dense external extent spans three coalescing windows. These
            // requests model the VMM's exact 64KiB DATA coverage records.
            for unit in [3, 4, 9] {
                service
                    .remote_load(
                        &runtime,
                        RemoteLoadRequest {
                            target_id: "registry-target".into(),
                            target_offset: unit * 65536,
                            length: 65536,
                            origin: "virtio_blk_read_miss".into(),
                        },
                    )
                    .await
                    .unwrap();
            }
            assert_eq!(provider.calls.load(Ordering::Relaxed), 3);
            let expected_logical = vec![
                (0, 4 * 65536),
                (4 * 65536, 4 * 65536),
                (8 * 65536, 2 * 65536),
            ];
            assert_eq!(*logical_requests.lock().unwrap(), expected_logical);
            assert_eq!(
                *provider.requests.lock().unwrap(),
                registry_fixture_physical_spans(&map, &expected_logical),
            );
            let backing = service
                .backing_for_target("registry-target", &runtime)
                .await
                .unwrap();
            let mut bytes = vec![0; map.logical_bytes as usize];
            backing
                .try_clone_file()
                .unwrap()
                .read_exact_at(&mut bytes, 0)
                .unwrap();
            assert!(bytes.iter().all(|byte| *byte == 0x5a));
        }

        #[tokio::test]
        async fn registry_provider_requires_crc_provenance_and_exact_decoded_length_before_native_write(
        ) {
            for reply in [
                RegistryReply::Unverified,
                RegistryReply::Peer,
                RegistryReply::Encoded,
                RegistryReply::Short,
            ] {
                let root = TestDirectory::new();
                let (service, runtime, map, provider, _, _resources) =
                    registry_service(&root.0, reply).await;
                service
                    .install_layer_capability("registry-target", &runtime, Arc::clone(&map), None)
                    .await
                    .unwrap();
                assert!(read_registry_target(&service, &runtime, &map)
                    .await
                    .is_err());
                let backing = service
                    .backing_for_target("registry-target", &runtime)
                    .await
                    .unwrap();
                let mut bytes = vec![0; map.logical_bytes as usize];
                backing
                    .try_clone_file()
                    .unwrap()
                    .read_exact_at(&mut bytes, 0)
                    .unwrap();
                assert!(bytes.iter().all(|byte| *byte == 0));
                assert_eq!(provider.calls.load(Ordering::Relaxed), 1);
                assert!(find_partial(&root.0.join("cache")).is_none());
            }
        }

        #[tokio::test]
        async fn registry_requires_exact_pinned_map_and_rejects_p2p_or_unbound_legacy_grants() {
            let root = TestDirectory::new();
            let (service, runtime, map, provider, _, _resources) =
                registry_service(&root.0, RegistryReply::Verified).await;
            let mut missing = (*map).clone();
            missing.registry = None;
            assert!(matches!(
                service
                    .install_layer_capability("registry-target", &runtime, Arc::new(missing), None)
                    .await,
                Err(RemoteLoadError::IdentityConflict)
            ));
            let mut mixed = (*map).clone();
            mixed.ranges[0].source.as_mut().unwrap().codec = DataCodec::Raw;
            assert!(matches!(
                service
                    .install_layer_capability("registry-target", &runtime, Arc::new(mixed), None)
                    .await,
                Err(RemoteLoadError::IdentityConflict)
            ));
            let peer = P2PSource {
                snapshot_id: "test".into(),
                spec_checksum: "test".into(),
                worker_ip: "127.0.0.1".parse().unwrap(),
            };
            assert!(matches!(
                service
                    .install_layer_capability(
                        "registry-target",
                        &runtime,
                        Arc::clone(&map),
                        Some(peer)
                    )
                    .await,
                Err(RemoteLoadError::IdentityConflict)
            ));
            let source = map.ranges[0].source.as_ref().unwrap();
            let remote_source = RemoteRangeSource {
                content_id: source.object_sha256.trim_start_matches("sha256:").into(),
                object_key: source.object_key.clone(),
                object_offset: source.pack_offset,
                encoded_length: source.encoded_length,
                checksum_sha256: String::new(),
                codec: "registry-rootfs-v1".into(),
            };
            remote_source.validate().unwrap();
            let mut fake_sha = remote_source.clone();
            fake_sha.checksum_sha256 = "0".repeat(64);
            assert!(fake_sha.validate().is_err());
            let mut missing_raw_sha = remote_source.clone();
            missing_raw_sha.codec = "raw".into();
            assert!(missing_raw_sha.validate().is_err());
            let grant = RemoteLoadGrant {
                runtime: runtime.clone(),
                target_id: "registry-target".into(),
                backing_key: LayerBackingIdentity {
                    kind: LayerKind::Block,
                    format_version: map.format_version,
                    logical_size: map.logical_bytes,
                    unit_size: map.unit_size_bytes,
                    content_sha256: map.content_sha256.trim_start_matches("sha256:").into(),
                },
                target_offset: 0,
                length: source.encoded_length,
                source: remote_source,
                provider: "test".into(),
                object_version: None,
                authorization: None,
                p2p_source: None,
            };
            assert!(matches!(
                service.grant_target(grant.clone()).await,
                Err(RemoteLoadError::UnsupportedCodec)
            ));
            assert!(matches!(
                service.grant_target_batch(vec![grant]).await,
                Err(RemoteLoadError::UnsupportedCodec)
            ));
            assert_eq!(provider.calls.load(Ordering::Relaxed), 0);
        }
        #[tokio::test]
        async fn default_background_prefetch_excludes_registry_but_demand_still_reads() {
            let root = TestDirectory::new();
            let (service, runtime, map, provider, _, _resources) =
                registry_service(&root.0, RegistryReply::Verified).await;
            service
                .install_layer_capability("registry-target", &runtime, Arc::clone(&map), None)
                .await
                .unwrap();
            assert!(
                service.launch_background_prefetch(&runtime).await,
                "test must exercise the default enabled background lane"
            );
            let mut completed = false;
            for _ in 0..100 {
                if let Some(status) = service.background_prefetch_status(&runtime) {
                    if status.state == "complete" {
                        assert_eq!(status.total_ranges, 0);
                        assert_eq!(status.total_bytes, 0);
                        completed = true;
                        break;
                    }
                }
                tokio::task::yield_now().await;
            }
            assert!(completed);
            assert_eq!(provider.calls.load(Ordering::Relaxed), 0);
            read_registry_target(&service, &runtime, &map)
                .await
                .unwrap();
            assert_eq!(provider.calls.load(Ordering::Relaxed), 1);
        }
    }
}
