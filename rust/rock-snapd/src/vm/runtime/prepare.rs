//! Verified restore preparation, CLH attachments and FD contract encoding.

use std::collections::{HashMap, HashSet, VecDeque};
use std::ffi::CString;
use std::fs::File;
use std::io::Write;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::sync::Arc;
use std::time::{Duration, Instant};

use sha2::{Digest, Sha256};
use thiserror::Error;

use crate::observability::metrics::SnapdMetrics;
use crate::observability::structured_log::{
    StructuredComponent, StructuredEvent, StructuredEventName, StructuredLogLevel,
    StructuredOutcome,
};
use crate::snapshot::io::backing::BackingError;
use crate::snapshot::io::backing::{LayerBackingIdentity, LayerKind};
use crate::snapshot::io::layer_resources::LayerResources;
use crate::snapshot::io::range_io::P2PSource;
use crate::snapshot::metadata::{
    canonical_json_bytes, sha256_identity, LayerKind as SnapshotLayerKind, LayerRangeMap,
    RangeKind, SnapshotChainView, SnapshotKind, SnapshotLayerView, SnapshotSpec,
    MAX_RANGES_PER_LAYER,
};
use crate::snapshot::services::ondemand_loader::{
    CredentialIdentity, RangeLoadExecutor, RemoteLayerTargetGrant, RemoteLoadError,
};
use crate::snapshot::Snapshot;
use crate::snapshot::{SnapshotInstance, ValidateCanonicalModel};
use crate::vm::protocol::{
    Channel, FdAccess, FdRole, FdRoleDescriptor, MessageHeader, WireEnvelope, VMM_PROTOCOL_VERSION,
};
use crate::vm::repository::{
    CompiledCanonicalLayerCoverage, PreparedSnapshotLineage, PreparedSnapshotRuntime,
    PreparedVmState, RuntimeLayerSeed, RuntimeStartError, VmRepository,
};
use crate::vm::runtime::client::VmmControllerError;
use crate::vm::runtime::launcher::VmmLaunchSpec;
use crate::vm::runtime::protocol::{
    BackingDescriptor, Capability, ControlRequest, DataResidency, LayerKind as ProtocolLayerKind,
    OwnershipMapDescriptor, RangeIndexDescriptor, ResidentCoverageDescriptor, StartRuntime,
    VmStateDescriptor,
};
use crate::RuntimeIdentity;

/// A fully staged replacement for one committed private generation.
///
/// Every descriptor and owned FD is ready before this value can cross the VMM
/// commit point. Dropping it closes the FDs; `rollback` additionally revokes
/// the temporary remote-load targets.
#[derive(Debug)]
pub struct PreparedFinalization {
    pub layers: Vec<BackingDescriptor>,
    pub fd_roles: Vec<FdRoleDescriptor>,
    pub fds: Vec<OwnedFd>,
    pub(in crate::vm) created_resources: Vec<Arc<LayerResources>>,
    // These are the same reader/backing leases indexed weakly by the loader.
    // VM finalization retains them across success and unknown outcomes.
    pub(in crate::vm) resources: Vec<Arc<LayerResources>>,
    pub(in crate::vm) runtime: RuntimeIdentity,
    pub(in crate::vm) _native_readers: Vec<crate::snapshot::io::backing::NativeReadLease>,
}

impl PreparedFinalization {
    pub async fn rollback(self, service: &RangeLoadExecutor) {
        for resource in &self.created_resources {
            service.revoke_layer_resources(resource).await;
        }
    }
}

// The cache retains only immutable range-map metadata and one sealed
// range-index memfd per entry. It never pins a potentially GiB-sized layer
// backing. Oversized entries are single-flighted for concurrent callers but
// are not retained after that build completes.
const COMPILED_LAYER_CACHE_MAX_ENTRIES: usize = 64;
const COMPILED_LAYER_CACHE_MAX_BYTES: usize = 512 * 1024 * 1024;
const COMPILED_LAYER_CACHE_MAX_FDS: usize = 64;
const COMPILED_LAYER_CACHE_MAX_INFLIGHT_COMPILES: usize = 8;

use crate::snapshot::services::background_prefetcher::{
    AttachPrefetchError, LatestMemoryPrefetchConfig, LatestMemoryPrefetcher,
};

#[derive(Clone, Copy, Debug)]
struct CompiledLayerCacheLimits {
    max_entries: usize,
    max_bytes: usize,
    max_fds: usize,
    max_inflight_compiles: usize,
}

impl Default for CompiledLayerCacheLimits {
    fn default() -> Self {
        Self {
            max_entries: COMPILED_LAYER_CACHE_MAX_ENTRIES,
            max_bytes: COMPILED_LAYER_CACHE_MAX_BYTES,
            max_fds: COMPILED_LAYER_CACHE_MAX_FDS,
            max_inflight_compiles: COMPILED_LAYER_CACHE_MAX_INFLIGHT_COMPILES,
        }
    }
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
struct CompiledLayerCacheKey {
    range_map_object_sha256: String,
    range_index_encoding_version: u16,
    vmm_protocol_version: u16,
    kind: SnapshotLayerKind,
    format_version: u64,
    logical_bytes: u64,
    unit_size_bytes: u64,
    content_sha256: String,
}

impl CompiledLayerCacheKey {
    fn from_verified_layer(layer: &SnapshotLayerView) -> Self {
        Self {
            range_map_object_sha256: layer.layer_ref.range_map.sha256.clone(),
            range_index_encoding_version: VMM_RANGE_INDEX_ENCODING_VERSION,
            vmm_protocol_version: VMM_PROTOCOL_VERSION,
            kind: layer.range_map.kind,
            format_version: layer.range_map.format_version,
            logical_bytes: layer.range_map.logical_bytes,
            unit_size_bytes: layer.range_map.unit_size_bytes,
            content_sha256: layer.range_map.content_sha256.clone(),
        }
    }
}

#[derive(Debug)]
struct CompiledLayerCacheEntry {
    coverage: Arc<CompiledCanonicalLayerCoverage>,
    bytes: usize,
    fds: usize,
}

#[derive(Debug)]
struct CompiledLayerAttempt {
    result: tokio::sync::watch::Sender<
        Option<Result<Arc<CompiledCanonicalLayerCoverage>, RuntimeStartError>>,
    >,
}

impl CompiledLayerAttempt {
    fn new() -> Arc<Self> {
        let (result, _) = tokio::sync::watch::channel(None);
        Arc::new(Self { result })
    }

    fn finish(&self, result: Result<Arc<CompiledCanonicalLayerCoverage>, RuntimeStartError>) {
        self.result.send_replace(Some(result));
    }

    async fn wait(&self) -> Result<Arc<CompiledCanonicalLayerCoverage>, RuntimeStartError> {
        let mut result = self.result.subscribe();
        loop {
            if let Some(result) = result.borrow_and_update().clone() {
                return result;
            }
            result.changed().await.map_err(|_| {
                RuntimeStartError::Vmm(
                    "compiled range-index single-flight ended without a result".into(),
                )
            })?;
        }
    }
}

#[derive(Debug, Default)]
struct CompiledLayerCacheState {
    completed: HashMap<CompiledLayerCacheKey, CompiledLayerCacheEntry>,
    lru: VecDeque<CompiledLayerCacheKey>,
    inflight: HashMap<CompiledLayerCacheKey, Arc<CompiledLayerAttempt>>,
    bytes: usize,
    fds: usize,
}

#[derive(Debug)]
struct CompiledLayerCache {
    limits: CompiledLayerCacheLimits,
    state: tokio::sync::Mutex<CompiledLayerCacheState>,
    compile_slots: Arc<tokio::sync::Semaphore>,
}

impl CompiledLayerCache {
    fn new(limits: CompiledLayerCacheLimits) -> Arc<Self> {
        assert!(
            limits.max_entries > 0
                && limits.max_bytes > 0
                && limits.max_fds > 0
                && limits.max_inflight_compiles > 0,
            "compiled layer cache limits must be positive"
        );
        let max_inflight_compiles = limits.max_inflight_compiles;
        Arc::new(Self {
            limits,
            state: tokio::sync::Mutex::new(CompiledLayerCacheState::default()),
            compile_slots: Arc::new(tokio::sync::Semaphore::new(max_inflight_compiles)),
        })
    }

    async fn get_or_compile(
        self: &Arc<Self>,
        key: CompiledLayerCacheKey,
        index: &LayerRangeMap,
    ) -> Result<Arc<CompiledCanonicalLayerCoverage>, RuntimeStartError> {
        enum Decision {
            Hit(Arc<CompiledCanonicalLayerCoverage>),
            Wait(Arc<CompiledLayerAttempt>),
            NeedSlot,
            Start(Arc<CompiledLayerAttempt>, tokio::sync::OwnedSemaphorePermit),
        }

        let initial = {
            let mut state = self.state.lock().await;
            if let Some(coverage) = state
                .completed
                .get(&key)
                .map(|entry| Arc::clone(&entry.coverage))
            {
                touch_lru(&mut state.lru, &key);
                Decision::Hit(coverage)
            } else if let Some(attempt) = state.inflight.get(&key) {
                Decision::Wait(Arc::clone(attempt))
            } else {
                Decision::NeedSlot
            }
        };

        let decision = match initial {
            Decision::NeedSlot => {
                // Wait for global compile capacity before cloning the large
                // range-map. Another caller may populate this key meanwhile,
                // so the cache must be checked again after acquiring a slot.
                let permit = Arc::clone(&self.compile_slots)
                    .acquire_owned()
                    .await
                    .map_err(|_| {
                        RuntimeStartError::Vmm("compiled range-index cache is closed".into())
                    })?;
                let mut state = self.state.lock().await;
                if let Some(coverage) = state
                    .completed
                    .get(&key)
                    .map(|entry| Arc::clone(&entry.coverage))
                {
                    touch_lru(&mut state.lru, &key);
                    drop(permit);
                    Decision::Hit(coverage)
                } else if let Some(attempt) = state.inflight.get(&key) {
                    let attempt = Arc::clone(attempt);
                    drop(permit);
                    Decision::Wait(attempt)
                } else {
                    let attempt = CompiledLayerAttempt::new();
                    state.inflight.insert(key.clone(), Arc::clone(&attempt));
                    Decision::Start(attempt, permit)
                }
            }
            ready => ready,
        };

        match decision {
            Decision::Hit(coverage) => Ok(coverage),
            Decision::Wait(attempt) => attempt.wait().await,
            Decision::Start(attempt, permit) => {
                // Only the elected producer clones the large range-map. The
                // global cache lock has already been released, and the CPU
                // heavy canonical JSON/index build runs on the blocking pool.
                let owned_index = index.clone();
                let cache = Arc::clone(self);
                let key_for_task = key;
                let attempt_for_task = Arc::clone(&attempt);
                tokio::spawn(async move {
                    let result = tokio::task::spawn_blocking(move || {
                        CompiledCanonicalLayerCoverage::compile(owned_index)
                    })
                    .await
                    .unwrap_or_else(|error| {
                        Err(RuntimeStartError::Vmm(format!(
                            "compiled range-index worker failed: {error}"
                        )))
                    });
                    cache.complete(key_for_task, attempt_for_task, result).await;

                    drop(permit);
                });
                attempt.wait().await
            }
            Decision::NeedSlot => unreachable!("compile slot decision must be resolved"),
        }
    }

    async fn complete(
        &self,
        key: CompiledLayerCacheKey,
        attempt: Arc<CompiledLayerAttempt>,
        result: Result<Arc<CompiledCanonicalLayerCoverage>, RuntimeStartError>,
    ) {
        let mut state = self.state.lock().await;
        if !state
            .inflight
            .get(&key)
            .is_some_and(|active| Arc::ptr_eq(active, &attempt))
        {
            drop(state);
            attempt.finish(result);
            return;
        }
        state.inflight.remove(&key);
        if let Ok(coverage) = &result {
            let bytes = coverage.cache_weight_bytes();
            let fds = coverage.cache_fd_count();
            if bytes <= self.limits.max_bytes && fds <= self.limits.max_fds {
                while state.completed.len() >= self.limits.max_entries
                    || state.bytes.saturating_add(bytes) > self.limits.max_bytes
                    || state.fds.saturating_add(fds) > self.limits.max_fds
                {
                    let Some(oldest) = state.lru.pop_front() else {
                        break;
                    };
                    if let Some(evicted) = state.completed.remove(&oldest) {
                        state.bytes = state.bytes.saturating_sub(evicted.bytes);
                        state.fds = state.fds.saturating_sub(evicted.fds);
                    }
                }
                state.bytes = state.bytes.saturating_add(bytes);
                state.fds = state.fds.saturating_add(fds);
                state.completed.insert(
                    key.clone(),
                    CompiledLayerCacheEntry {
                        coverage: Arc::clone(coverage),
                        bytes,
                        fds,
                    },
                );
                touch_lru(&mut state.lru, &key);
            }
        }
        drop(state);
        attempt.finish(result);
    }
}

fn touch_lru(lru: &mut VecDeque<CompiledLayerCacheKey>, key: &CompiledLayerCacheKey) {
    lru.retain(|candidate| candidate != key);
    lru.push_back(key.clone());
}

#[derive(Debug, Error)]
pub enum SnapshotRestoreError {
    #[error("Invalid remote snapshot runtime: {0}")]
    Invalid(String),
    #[error(transparent)]
    RemoteLoad(#[from] RemoteLoadError),
    #[error(transparent)]
    Runtime(#[from] RuntimeStartError),
    #[error(transparent)]
    Backing(#[from] BackingError),
    #[error("Runtime descriptor I/O failed: {0}")]
    Io(#[from] std::io::Error),
}

impl From<AttachPrefetchError> for SnapshotRestoreError {
    fn from(error: AttachPrefetchError) -> Self {
        match error {
            AttachPrefetchError::Invalid(message) => Self::Invalid(message),
            AttachPrefetchError::RemoteLoad(error) => Self::RemoteLoad(error),
        }
    }
}

impl From<VmmControllerError> for SnapshotRestoreError {
    fn from(error: VmmControllerError) -> Self {
        Self::Runtime(RuntimeStartError::from(error))
    }
}

#[derive(Debug)]
pub struct SnapshotRestorePreparer {
    service: Arc<RangeLoadExecutor>,
    provider: String,
    authorization: Option<CredentialIdentity>,
    metrics: Option<Arc<SnapdMetrics>>,
    compiled_layers: Arc<CompiledLayerCache>,
    latest_memory_prefetcher: LatestMemoryPrefetcher,
}

impl SnapshotRestorePreparer {
    pub async fn launch_snapshot_hotset_prefetch(&self, runtime: &RuntimeIdentity) -> (usize, u64) {
        self.latest_memory_prefetcher
            .launch_snapshot_hotset_prefetch(runtime)
            .await
    }
    pub fn new(
        service: Arc<RangeLoadExecutor>,
        provider: impl Into<String>,
        authorization: Option<CredentialIdentity>,
    ) -> Result<Arc<Self>, SnapshotRestoreError> {
        Self::new_with_metrics(service, provider, authorization, None)
    }

    pub fn new_with_metrics(
        service: Arc<RangeLoadExecutor>,
        provider: impl Into<String>,
        authorization: Option<CredentialIdentity>,
        metrics: Option<Arc<SnapdMetrics>>,
    ) -> Result<Arc<Self>, SnapshotRestoreError> {
        Self::new_with_metrics_and_cache_limits(
            service,
            provider,
            authorization,
            metrics,
            CompiledLayerCacheLimits::default(),
        )
    }

    pub fn new_with_metrics_and_prefetch(
        service: Arc<RangeLoadExecutor>,
        provider: impl Into<String>,
        authorization: Option<CredentialIdentity>,
        metrics: Option<Arc<SnapdMetrics>>,
        latest_memory_prefetch: LatestMemoryPrefetchConfig,
    ) -> Result<Arc<Self>, SnapshotRestoreError> {
        Self::new_with_metrics_cache_and_prefetch(
            service,
            provider,
            authorization,
            metrics,
            CompiledLayerCacheLimits::default(),
            latest_memory_prefetch,
        )
    }

    fn new_with_metrics_and_cache_limits(
        service: Arc<RangeLoadExecutor>,
        provider: impl Into<String>,
        authorization: Option<CredentialIdentity>,
        metrics: Option<Arc<SnapdMetrics>>,
        cache_limits: CompiledLayerCacheLimits,
    ) -> Result<Arc<Self>, SnapshotRestoreError> {
        Self::new_with_metrics_cache_and_prefetch(
            service,
            provider,
            authorization,
            metrics,
            cache_limits,
            LatestMemoryPrefetchConfig::default(),
        )
    }

    fn new_with_metrics_cache_and_prefetch(
        service: Arc<RangeLoadExecutor>,
        provider: impl Into<String>,
        authorization: Option<CredentialIdentity>,
        metrics: Option<Arc<SnapdMetrics>>,
        cache_limits: CompiledLayerCacheLimits,
        latest_memory_prefetch: LatestMemoryPrefetchConfig,
    ) -> Result<Arc<Self>, SnapshotRestoreError> {
        let provider = provider.into();
        if provider.is_empty() {
            return Err(SnapshotRestoreError::Invalid(
                "range provider identity is required".into(),
            ));
        }
        let latest_memory_prefetcher = LatestMemoryPrefetcher::new(
            Arc::clone(&service),
            metrics.clone(),
            latest_memory_prefetch,
        )?;
        Ok(Arc::new(Self {
            service,
            provider,
            authorization,
            metrics,
            compiled_layers: CompiledLayerCache::new(cache_limits),
            latest_memory_prefetcher,
        }))
    }

    pub fn service(&self) -> &Arc<RangeLoadExecutor> {
        &self.service
    }

    /// Stage the shared immutable MEMORY+BLOCK descendants used to replace a
    /// VMM-owned private frozen generation. Every target and FD is ready before
    /// the caller sends the no-partial-commit finalization request to the VMM.
    pub async fn prepare_finalization(
        &self,
        local: &Snapshot,
        published: &SnapshotSpec,
        runtime: &RuntimeIdentity,
        repository: &VmRepository,
    ) -> Result<PreparedFinalization, SnapshotRestoreError> {
        let canonical = validate_finalization_source(local, published)?;
        let mut layers = Vec::with_capacity(2);
        let mut fd_roles = Vec::with_capacity(4);
        let mut fds = Vec::with_capacity(4);
        let mut created_resources = Vec::with_capacity(2);
        let mut resources = Vec::with_capacity(2);
        let mut native_readers = Vec::with_capacity(2);
        let result = async {
            for kind in [SnapshotLayerKind::Memory, SnapshotLayerKind::Block] {
                let local_layer = canonical.layers.get(&kind).ok_or_else(|| {
                    SnapshotRestoreError::Invalid("FULL promotion is missing a layer".into())
                })?;
                let chain = canonical.chain_indexes.get(&kind).ok_or_else(|| {
                    SnapshotRestoreError::Invalid("promotion chain is missing".into())
                })?;
                let head = chain.index.layers.last().ok_or_else(|| {
                    SnapshotRestoreError::Invalid("promotion chain is empty".into())
                })?;
                let ordinal = u32::try_from(head.ordinal).map_err(|_| {
                    SnapshotRestoreError::Invalid("promotion layer ordinal exceeds u32".into())
                })?;
                let target_id = repository.remote_target_id(runtime, kind, ordinal);
                let layer_kind = match kind {
                    SnapshotLayerKind::Memory => LayerKind::Memory,
                    SnapshotLayerKind::Block => LayerKind::Block,
                };
                let key = LayerBackingIdentity {
                    kind: layer_kind,
                    format_version: local_layer.range_map.format_version,
                    logical_size: local_layer.range_map.logical_bytes,
                    unit_size: local_layer.range_map.unit_size_bytes,
                    content_sha256: checked_sha_hex(&local_layer.range_map.content_sha256)?.into(),
                };
                key.validate()
                    .map_err(|error| SnapshotRestoreError::Invalid(error.to_string()))?;
                let native = local.backing_handle(kind).map_err(|error| {
                    SnapshotRestoreError::Invalid(format!(
                        "promotion source backing is unavailable: {error}"
                    ))
                })?;
                let pin = native
                    .backing
                    .restore_and_pin_native_reader()
                    .await
                    .map_err(|error| SnapshotRestoreError::Invalid(error.to_string()))?;
                let source = pin.file();
                let (resource, created) = self
                    .service
                    .prepare_promotion_target(
                        RemoteLayerTargetGrant {
                            runtime: runtime.clone(),
                            target_id: target_id.clone(),
                            backing_key: key.clone(),
                            provider: self.provider.clone(),
                            authorization: self.authorization.clone(),
                        },
                        source,
                    )
                    .await?;
                if created {
                    created_resources.push(Arc::clone(&resource));
                }
                resources.push(Arc::clone(&resource));
                self.service
                    .install_layer_capability(
                        &target_id,
                        runtime,
                        Arc::clone(&local_layer.range_map),
                        None,
                    )
                    .await?;
                self.service
                    .complete_promotion_target(&target_id, runtime, source)
                    .await?;
                native_readers.push(pin);
                let protocol_kind = match kind {
                    SnapshotLayerKind::Memory => ProtocolLayerKind::Memory,
                    SnapshotLayerKind::Block => ProtocolLayerKind::Block,
                };
                let content_id = checked_sha_hex(&local_layer.range_map.content_sha256)?.to_owned();
                let backing = resource.backing().ok_or_else(|| {
                    SnapshotRestoreError::Invalid(
                        "promotion target has no managed backing lease".into(),
                    )
                })?;
                let backing_fd: OwnedFd = backing.try_clone_readonly_file()?.into();
                let shared_mapping_fd = match protocol_kind {
                    ProtocolLayerKind::Memory => {
                        Some(OwnedFd::from(backing.try_clone_memory_mapping_file()?))
                    }
                    ProtocolLayerKind::Block => None,
                };
                let range_index = vmm_range_index(&local_layer.range_map)?;
                let range_identity = sha256_identity(&range_index);
                let range_content = checked_sha_hex(&range_identity)?;
                let range_fd = payload_readonly_fd(&range_index)?;
                let backing_index = u32::try_from(fds.len()).map_err(|_| {
                    SnapshotRestoreError::Invalid("promotion FD index overflows".into())
                })?;
                fds.push(backing_fd);
                fd_roles.push(FdRoleDescriptor {
                    role: match protocol_kind {
                        ProtocolLayerKind::Memory => FdRole::MemoryRoLayer,
                        ProtocolLayerKind::Block => FdRole::BlockRoLayer,
                    },
                    access: FdAccess::ReadOnly,
                    content_id: content_id.clone(),
                    size: local_layer.range_map.logical_bytes,
                    alignment: local_layer.range_map.unit_size_bytes,
                });
                let shared_mapping_fd_index = if let Some(shared_mapping_fd) = shared_mapping_fd {
                    let index = u32::try_from(fds.len()).map_err(|_| {
                        SnapshotRestoreError::Invalid("promotion FD index overflows".into())
                    })?;
                    fds.push(shared_mapping_fd);
                    fd_roles.push(FdRoleDescriptor {
                        role: FdRole::MemorySharedMapping,
                        access: FdAccess::ReadWrite,
                        content_id: content_id.clone(),
                        size: local_layer.range_map.logical_bytes,
                        alignment: local_layer.range_map.unit_size_bytes,
                    });
                    Some(index)
                } else {
                    None
                };
                let range_index_fd = u32::try_from(fds.len()).map_err(|_| {
                    SnapshotRestoreError::Invalid("promotion FD index overflows".into())
                })?;
                let io_alignment = match protocol_kind {
                    ProtocolLayerKind::Memory => 4096,
                    ProtocolLayerKind::Block => 512,
                };
                layers.push(BackingDescriptor {
                    kind: protocol_kind,
                    data_residency: DataResidency::LocalComplete,
                    ordinal,
                    layer_id: head.layer_id.clone(),
                    target_id,
                    content_id: content_id.clone(),
                    range_index: Some(RangeIndexDescriptor {
                        format_version: local_layer.range_map.format_version,
                        content_id: range_content.to_owned(),
                        size: range_index.len() as u64,
                        alignment: 1,
                        fd_index: range_index_fd,
                    }),
                    local_ownership: None,
                    initial_residency: None,
                    logical_size: local_layer.range_map.logical_bytes,
                    alignment: local_layer.range_map.unit_size_bytes,
                    io_alignment,
                    fd_index: Some(backing_index),
                    shared_mapping_fd_index,
                    block_path: None,
                });
                fd_roles.push(FdRoleDescriptor {
                    role: match protocol_kind {
                        ProtocolLayerKind::Memory => FdRole::MemoryRangeIndex,
                        ProtocolLayerKind::Block => FdRole::BlockRangeIndex,
                    },
                    access: FdAccess::ReadOnly,
                    content_id: range_content.to_owned(),
                    size: range_index.len() as u64,
                    alignment: 1,
                });
                fds.push(range_fd);
            }
            Ok::<(), SnapshotRestoreError>(())
        }
        .await;
        if let Err(error) = result {
            for resource in &created_resources {
                self.service.revoke_layer_resources(resource).await;
            }
            return Err(error);
        }
        Ok(PreparedFinalization {
            layers,
            fd_roles,
            fds,
            created_resources,
            resources,
            runtime: runtime.clone(),
            _native_readers: native_readers,
        })
    }

    /// Prepare all targets before VMM launch.  Any failure revokes the whole
    /// not-yet-running runtime so no partial capability or backing owner leaks.
    pub async fn prepare(
        &self,
        snapshot: &dyn SnapshotInstance,
        runtime: &RuntimeIdentity,
        repository: &VmRepository,
        p2p_source: Option<&P2PSource>,
    ) -> Result<Arc<PreparedSnapshotRuntime>, SnapshotRestoreError> {
        self.service.begin_runtime(runtime).await?;
        let result = self
            .prepare_inner(snapshot, runtime, repository, p2p_source)
            .await;
        if result.is_err() {
            self.service.revoke_runtime(runtime).await;
        }
        result
    }

    async fn prepare_inner(
        &self,
        snapshot: &dyn SnapshotInstance,
        runtime: &RuntimeIdentity,
        repository: &VmRepository,
        p2p_source: Option<&P2PSource>,
    ) -> Result<Arc<PreparedSnapshotRuntime>, SnapshotRestoreError> {
        let total_started = Instant::now();
        let memory = snapshot.chain_view(SnapshotLayerKind::Memory);
        let rootfs = snapshot
            .chain_view(SnapshotLayerKind::Block)
            .ok_or_else(|| SnapshotRestoreError::Invalid("snapshot has no rootfs chain".into()))?;
        let spec = snapshot.spec().ok_or_else(|| {
            SnapshotRestoreError::Invalid("remote snapshot has no persistent spec".into())
        })?;
        // The format contract, not a codec-specific descriptor, establishes the
        // chain cardinality/order and geometry handed to the unchanged VMM API.
        for (kind, opened) in [
            (SnapshotLayerKind::Memory, memory),
            (SnapshotLayerKind::Block, Some(rootfs)),
        ] {
            let described = snapshot
                .layer_chain(kind)
                .map_err(SnapshotRestoreError::Invalid)?;
            match (described, opened) {
                (None, None) => {}
                (Some(description), Some(chain))
                    if description.logical_bytes == chain.index.logical_bytes
                        && description.unit_size_bytes == chain.index.unit_size_bytes
                        && description.layers.len() == chain.layers.len()
                        && description
                            .layers
                            .iter()
                            .zip(&chain.layers)
                            .all(|(meta, layer)| {
                                meta.ordinal == layer.layer_ref.ordinal
                                    && meta.layer_id == layer.layer_ref.layer_id
                                    && meta.content_identity == layer.range_map.content_sha256
                            }) => {}
                _ => {
                    return Err(SnapshotRestoreError::Invalid(
                        "snapshot metadata contract disagrees with opened layer chain".into(),
                    ));
                }
            }
        }
        let mut seeds = Vec::new();
        let mut chains = HashMap::new();
        if let Some(memory) = memory {
            let started = Instant::now();
            self.prepare_chain(
                snapshot, runtime, repository, memory, &mut seeds, p2p_source,
            )
            .await?;
            self.emit_prepare_stage(
                snapshot.snapshot_id(),
                runtime,
                "memory_chain",
                started.elapsed(),
                total_started.elapsed(),
                None,
                graph_counts(memory),
            );
            chains.insert(SnapshotLayerKind::Memory, memory.index.clone());
        }
        let started = Instant::now();
        self.prepare_chain(
            snapshot, runtime, repository, rootfs, &mut seeds, p2p_source,
        )
        .await?;
        self.emit_prepare_stage(
            snapshot.snapshot_id(),
            runtime,
            "block_chain",
            started.elapsed(),
            total_started.elapsed(),
            None,
            graph_counts(rootfs),
        );
        chains.insert(SnapshotLayerKind::Block, rootfs.index.clone());

        let started = Instant::now();
        let vm_state = match (snapshot.vm_state(), &spec.vm_state) {
            (Some(payload), Some(_)) => Some(PreparedVmState {
                format_version: spec.compatibility.vmm_state_format_version.ok_or_else(|| {
                    SnapshotRestoreError::Invalid(
                        "FULL snapshot has no VMM state format version".into(),
                    )
                })?,
                payload: payload.to_vec(),
            }),
            (None, None) => None,
            _ => {
                return Err(SnapshotRestoreError::Invalid(
                    "VM state metadata and payload disagree".into(),
                ));
            }
        };
        self.emit_prepare_stage(
            snapshot.snapshot_id(),
            runtime,
            "vm_state_seed",
            started.elapsed(),
            total_started.elapsed(),
            None,
            GraphCounts::default(),
        );
        let started = Instant::now();
        let mut chain_identities = HashMap::new();
        chain_identities.insert(
            SnapshotLayerKind::Block,
            (
                spec.rootfs.index_object_key.clone(),
                spec.rootfs.index_sha256.clone(),
            ),
        );
        if let Some(memory) = &spec.memory {
            chain_identities.insert(
                SnapshotLayerKind::Memory,
                (memory.index_object_key.clone(), memory.index_sha256.clone()),
            );
        }
        let spec_sha256 = checked_sha_hex(&sha256_identity(
            &canonical_json_bytes(spec)
                .map_err(|error| SnapshotRestoreError::Invalid(error.to_string()))?,
        ))?
        .to_owned();
        self.emit_prepare_stage(
            snapshot.snapshot_id(),
            runtime,
            "lineage_hash",
            started.elapsed(),
            total_started.elapsed(),
            None,
            GraphCounts::default(),
        );
        let started = Instant::now();
        let prepared = PreparedSnapshotRuntime::new(
            snapshot.snapshot_id().to_owned(),
            spec_sha256.clone(),
            seeds,
            chains.clone(),
            vm_state,
            PreparedSnapshotLineage {
                snapshot_id: snapshot.snapshot_id().to_owned(),
                spec_sha256,
                provenance: spec.provenance.clone(),
                parent_chains: chains,
                chain_identities,
            },
        )
        .map_err(SnapshotRestoreError::Runtime)?;
        self.emit_prepare_stage(
            snapshot.snapshot_id(),
            runtime,
            "prepared_runtime_build",
            started.elapsed(),
            total_started.elapsed(),
            None,
            GraphCounts::default(),
        );
        let counts = snapshot_graph_counts(snapshot);
        self.emit_prepare_stage(
            snapshot.snapshot_id(),
            runtime,
            "remote_preparer_total",
            total_started.elapsed(),
            total_started.elapsed(),
            None,
            counts,
        );
        Ok(prepared)
    }

    async fn prepare_chain(
        &self,
        snapshot: &dyn SnapshotInstance,
        runtime: &RuntimeIdentity,
        repository: &VmRepository,
        chain: &SnapshotChainView,
        seeds: &mut Vec<RuntimeLayerSeed>,
        p2p_source: Option<&P2PSource>,
    ) -> Result<(), SnapshotRestoreError> {
        let snapshot_id = snapshot.snapshot_id();
        for (layer_index, layer) in chain.layers.iter().enumerate() {
            let layer_started = Instant::now();
            let ordinal = u32::try_from(layer.layer_ref.ordinal)
                .map_err(|_| SnapshotRestoreError::Invalid("layer ordinal exceeds u32".into()))?;
            let target_id = repository.remote_target_id(runtime, layer.range_map.kind, ordinal);
            let backing_key = layer_key(&layer.range_map)?;
            let target = RemoteLayerTargetGrant {
                runtime: runtime.clone(),
                target_id: target_id.clone(),
                backing_key: backing_key.clone(),
                provider: self.provider.clone(),
                authorization: self.authorization.clone(),
            };
            let started = Instant::now();
            let (resources, _) = self.service.prepare_target(target).await?;
            self.emit_prepare_stage(
                snapshot_id,
                runtime,
                phase_for(layer.range_map.kind, "prepare_target"),
                started.elapsed(),
                layer_started.elapsed(),
                Some(&layer.layer_ref.layer_id),
                graph_counts_for_layer(layer),
            );
            let started = Instant::now();
            let coverage = self
                .compiled_layers
                .get_or_compile(
                    CompiledLayerCacheKey::from_verified_layer(layer),
                    &layer.range_map,
                )
                .await?;
            self.emit_prepare_stage(
                snapshot_id,
                runtime,
                phase_for(layer.range_map.kind, "seed_build"),
                started.elapsed(),
                layer_started.elapsed(),
                Some(&layer.layer_ref.layer_id),
                graph_counts_for_layer(layer),
            );
            let started = Instant::now();
            self.service
                .install_layer_capability_with_reader(
                    &target_id,
                    runtime,
                    coverage.range_map(),
                    // External registry DATA has CRC identity, without a
                    // snapd per-range SHA. It is never a P2P capability.
                    if !layer.allows_p2p() {
                        None
                    } else {
                        p2p_source.cloned()
                    },
                    snapshot
                        .layer_reader(layer.range_map.kind, &layer.range_map.content_sha256)
                        .map_err(|error| SnapshotRestoreError::Invalid(error.to_string()))?,
                )
                .await?;
            self.emit_prepare_stage(
                snapshot_id,
                runtime,
                phase_for(layer.range_map.kind, "grant_ranges"),
                started.elapsed(),
                layer_started.elapsed(),
                Some(&layer.layer_ref.layer_id),
                graph_counts_for_layer(layer),
            );
            let started = Instant::now();
            let backing = resources.backing().ok_or_else(|| {
                SnapshotRestoreError::Invalid("remote target has no managed backing lease".into())
            })?;
            self.emit_prepare_stage(
                snapshot_id,
                runtime,
                phase_for(layer.range_map.kind, "backing_acquire"),
                started.elapsed(),
                layer_started.elapsed(),
                Some(&layer.layer_ref.layer_id),
                graph_counts_for_layer(layer),
            );
            if layer.range_map.kind == SnapshotLayerKind::Memory
                && layer_index + 1 == chain.layers.len()
            {
                self.latest_memory_prefetcher
                    .prefetch_latest_memory_layer(
                        snapshot_id,
                        runtime,
                        &target_id,
                        layer,
                        p2p_source.is_some(),
                        &backing,
                    )
                    .await?;
            }
            seeds.push(RuntimeLayerSeed::new_remote_lazy_precompiled(
                ordinal,
                layer.layer_ref.layer_id.clone(),
                coverage,
                resources,
            )?);
        }
        Ok(())
    }

    fn emit_prepare_stage(
        &self,
        snapshot_id: &str,
        runtime: &RuntimeIdentity,
        phase: &'static str,
        duration: Duration,
        total: Duration,
        layer_id: Option<&str>,
        counts: GraphCounts,
    ) {
        let Some(metrics) = &self.metrics else {
            return;
        };
        let mut event = StructuredEvent::new(
            StructuredLogLevel::Info,
            StructuredEventName::RuntimeStartStageCompleted,
            StructuredComponent::Runtime,
            StructuredOutcome::Success,
        )
        .operation("prepare_remote_runtime")
        .phase(phase)
        .snapshot_id(snapshot_id)
        .sandbox_id(&runtime.sandbox_id)
        .duration_micros(duration)
        .total_duration(total)
        .graph_counts(
            counts.layers,
            counts.ranges,
            counts.data_ranges,
            counts.unique_objects,
        );
        if let Some(layer_id) = layer_id {
            event = event.layer_id(layer_id);
        }
        metrics.emit_structured(event);
    }
}

#[derive(Clone, Copy, Debug, Default)]
struct GraphCounts {
    layers: u64,
    ranges: u64,
    data_ranges: u64,
    unique_objects: u64,
}

fn graph_counts(chain: &SnapshotChainView) -> GraphCounts {
    let mut counts = GraphCounts::default();
    let mut objects = HashSet::new();
    counts.layers = chain.layers.len().try_into().unwrap_or(u64::MAX);
    for layer in &chain.layers {
        counts.ranges = counts
            .ranges
            .saturating_add(layer.range_map.ranges.len().try_into().unwrap_or(u64::MAX));
        for range in &layer.range_map.ranges {
            if let Some(source) = &range.source {
                counts.data_ranges = counts.data_ranges.saturating_add(1);
                objects.insert((source.object_key.as_str(), source.object_sha256.as_str()));
            }
        }
    }
    counts.unique_objects = objects.len().try_into().unwrap_or(u64::MAX);
    counts
}

fn graph_counts_for_layer(layer: &SnapshotLayerView) -> GraphCounts {
    let mut objects = HashSet::new();
    let data_ranges = layer
        .range_map
        .ranges
        .iter()
        .filter(|range| {
            if let Some(source) = &range.source {
                objects.insert((source.object_key.as_str(), source.object_sha256.as_str()));
                true
            } else {
                false
            }
        })
        .count();
    GraphCounts {
        layers: 1,
        ranges: layer.range_map.ranges.len().try_into().unwrap_or(u64::MAX),
        data_ranges: data_ranges.try_into().unwrap_or(u64::MAX),
        unique_objects: objects.len().try_into().unwrap_or(u64::MAX),
    }
}

fn snapshot_graph_counts(snapshot: &dyn SnapshotInstance) -> GraphCounts {
    let mut total = GraphCounts::default();
    for kind in [SnapshotLayerKind::Block, SnapshotLayerKind::Memory] {
        if let Some(chain) = snapshot.chain_view(kind) {
            let counts = graph_counts(chain);
            total.layers = total.layers.saturating_add(counts.layers);
            total.ranges = total.ranges.saturating_add(counts.ranges);
            total.data_ranges = total.data_ranges.saturating_add(counts.data_ranges);
            // Memory and block objects have distinct canonical identities.
            total.unique_objects = total.unique_objects.saturating_add(counts.unique_objects);
        }
    }
    total
}

fn phase_for(kind: SnapshotLayerKind, operation: &'static str) -> &'static str {
    match (kind, operation) {
        (SnapshotLayerKind::Memory, "prepare_target") => "memory_prepare_target",
        (SnapshotLayerKind::Memory, "grant_ranges") => "memory_grant_ranges",
        (SnapshotLayerKind::Memory, "backing_acquire") => "memory_backing_acquire",
        (SnapshotLayerKind::Memory, "seed_build") => "memory_seed_build",
        (SnapshotLayerKind::Block, "prepare_target") => "block_prepare_target",
        (SnapshotLayerKind::Block, "grant_ranges") => "block_grant_ranges",
        (SnapshotLayerKind::Block, "backing_acquire") => "block_backing_acquire",
        (SnapshotLayerKind::Block, "seed_build") => "block_seed_build",
        _ => "unknown_prepare_stage",
    }
}

pub(crate) fn layer_key(
    layer: &LayerRangeMap,
) -> Result<LayerBackingIdentity, SnapshotRestoreError> {
    let kind = match layer.kind {
        SnapshotLayerKind::Memory => LayerKind::Memory,
        SnapshotLayerKind::Block => LayerKind::Block,
    };
    let key = LayerBackingIdentity {
        kind,
        format_version: layer.format_version,
        logical_size: layer.logical_bytes,
        unit_size: layer.unit_size_bytes,
        content_sha256: checked_sha_hex(&layer.content_sha256)?.to_owned(),
    };
    key.validate()
        .map_err(|error| SnapshotRestoreError::Invalid(error.to_string()))?;
    Ok(key)
}

fn validate_finalization_source(
    local: &Snapshot,
    published: &SnapshotSpec,
) -> Result<
    Arc<crate::snapshot::format::overlaybd_s3::capture::CanonicalLocalSnapshot>,
    SnapshotRestoreError,
> {
    published
        .validate()
        .map_err(SnapshotRestoreError::Invalid)?;
    if local.snapshot_kind != SnapshotKind::Full
        || local.vm_state.is_none()
        || published.memory.is_none()
        || published.vm_state.is_none()
    {
        return Err(SnapshotRestoreError::Invalid(
            "only a complete FULL snapshot can promote a running generation".into(),
        ));
    }
    let canonical = local.canonical_snapshot().map_err(|error| {
        SnapshotRestoreError::Invalid(format!(
            "promotion canonicalization is unavailable: {error}"
        ))
    })?;
    for kind in [SnapshotLayerKind::Memory, SnapshotLayerKind::Block] {
        canonical.layers.get(&kind).ok_or_else(|| {
            SnapshotRestoreError::Invalid("promotion local layer is missing".into())
        })?;
        let chain = canonical.chain_indexes.get(&kind).ok_or_else(|| {
            SnapshotRestoreError::Invalid("promotion local chain is missing".into())
        })?;
        let head = chain.index.layers.last().ok_or_else(|| {
            SnapshotRestoreError::Invalid("promotion local head is missing".into())
        })?;
        let published_chain = match kind {
            SnapshotLayerKind::Memory => published
                .memory
                .as_ref()
                .expect("FULL source was checked above"),
            SnapshotLayerKind::Block => &published.rootfs,
        };
        if published_chain.index_sha256 != chain.sha256
            || published_chain.logical_bytes != chain.index.logical_bytes
            || chain.index.layers.last() != Some(head)
        {
            return Err(SnapshotRestoreError::Invalid(
                "published chain differs from the local frozen generation".into(),
            ));
        }
    }
    Ok(canonical)
}

fn checked_sha_hex(identity: &str) -> Result<&str, SnapshotRestoreError> {
    identity.strip_prefix("sha256:").ok_or_else(|| {
        SnapshotRestoreError::Invalid(format!("invalid SHA-256 identity {identity:?}"))
    })
}

const MEMORY_IO_ALIGNMENT: u64 = 4096;
const BLOCK_IO_ALIGNMENT: u64 = 512;
const INITIAL_RESIDENCY_CAPABILITY: &str = "rock.remote-load.initial-residency";

/// Revision of CLH's logical coverage attachment, independent of storage codecs.
pub(crate) const VMM_RANGE_INDEX_ENCODING_VERSION: u16 = 3;
const MAX_VMM_RANGE_INDEX_BYTES: usize = 96 + MAX_RANGES_PER_LAYER * 24 * 2;

/// Compile only logical coverage for the VMM. Pack identity, offsets, codecs and
/// per-range checksums remain in snapd's verified storage index.
pub(crate) fn vmm_range_index(index: &LayerRangeMap) -> Result<Vec<u8>, VmmControllerError> {
    const HEADER_SIZE: usize = 96;
    const RECORD_SIZE: usize = 24;
    let range_count = u32::try_from(index.ranges.len())
        .map_err(|_| VmmControllerError::Invalid("VMM range count overflows u32".into()))?;
    let mut mapping_runs: Vec<(u64, u64, RangeKind)> = Vec::new();
    let mut data_count = 0_u32;
    for range in &index.ranges {
        if range.kind == RangeKind::Data {
            data_count += 1;
        }
        if let Some((_, length, kind)) = mapping_runs.last_mut() {
            if *kind == range.kind {
                *length = length.checked_add(range.length).ok_or_else(|| {
                    VmmControllerError::Invalid("VMM mapping run length overflows".into())
                })?;
                continue;
            }
        }
        mapping_runs.push((range.logical_offset, range.length, range.kind));
    }
    let mapping_count = u32::try_from(mapping_runs.len())
        .map_err(|_| VmmControllerError::Invalid("VMM mapping run count overflows u32".into()))?;
    let total_size = index
        .ranges
        .len()
        .checked_add(mapping_runs.len())
        .and_then(|count| count.checked_mul(RECORD_SIZE))
        .and_then(|size| size.checked_add(HEADER_SIZE))
        .ok_or_else(|| VmmControllerError::Invalid("VMM coverage size overflows".into()))?;
    validate_vmm_range_index_size(total_size)?;
    let mut bytes = Vec::with_capacity(total_size);
    bytes.extend_from_slice(b"ROCKCOV1");
    bytes.extend_from_slice(&1_u16.to_le_bytes());
    bytes.extend_from_slice(&(HEADER_SIZE as u16).to_le_bytes());
    bytes.push(match index.kind {
        SnapshotLayerKind::Memory => 0,
        SnapshotLayerKind::Block => 1,
    });
    bytes.push(0);
    bytes.extend_from_slice(&(RECORD_SIZE as u16).to_le_bytes());
    bytes.extend_from_slice(&index.logical_bytes.to_le_bytes());
    bytes.extend_from_slice(&index.unit_size_bytes.to_le_bytes());
    bytes.extend_from_slice(&range_count.to_le_bytes());
    bytes.extend_from_slice(&data_count.to_le_bytes());
    bytes.extend_from_slice(&parse_coverage_sha256(&index.content_sha256)?);
    bytes.extend_from_slice(&mapping_count.to_le_bytes());
    bytes.resize(HEADER_SIZE, 0);
    for (offset, length, kind) in index
        .ranges
        .iter()
        .map(|range| (range.logical_offset, range.length, range.kind))
        .chain(mapping_runs)
    {
        bytes.extend_from_slice(&offset.to_le_bytes());
        bytes.extend_from_slice(&length.to_le_bytes());
        bytes.push(match kind {
            RangeKind::Data => 0,
            RangeKind::Zero => 1,
            RangeKind::Inherit => 2,
        });
        bytes.extend_from_slice(&[0_u8; 7]);
    }
    debug_assert_eq!(bytes.len(), total_size);
    Ok(bytes)
}

fn parse_coverage_sha256(value: &str) -> Result<[u8; 32], VmmControllerError> {
    let hex = value.strip_prefix("sha256:").unwrap_or(value);
    if hex.len() != 64 || !hex.is_ascii() {
        return invalid("layer content SHA-256 is not canonical");
    }
    let mut digest = [0_u8; 32];
    for (position, output) in digest.iter_mut().enumerate() {
        let start = position * 2;
        *output = u8::from_str_radix(&hex[start..start + 2], 16).map_err(|_| {
            VmmControllerError::Invalid("layer content SHA-256 is not hexadecimal".into())
        })?;
    }
    Ok(digest)
}

pub(crate) fn validate_vmm_range_index_size(size: usize) -> Result<(), VmmControllerError> {
    if size == 0 || size > MAX_VMM_RANGE_INDEX_BYTES {
        return invalid("VMM range index exceeds the protocol byte bound");
    }
    Ok(())
}

pub(crate) fn validate_readonly_backing(
    fd: &OwnedFd,
    expected_size: u64,
) -> Result<(), VmmControllerError> {
    // SAFETY: fd is live and borrowed for fcntl/fstat only.
    let flags = unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_GETFL) };
    if flags < 0 || flags & libc::O_ACCMODE != libc::O_RDONLY {
        return invalid("runtime backing must be opened read-only");
    }
    // SAFETY: zeroed is a valid initial stat buffer and fd is live.
    let mut stat: libc::stat = unsafe { std::mem::zeroed() };
    if unsafe { libc::fstat(fd.as_raw_fd(), &mut stat) } != 0
        || stat.st_size < 0
        || stat.st_size as u64 != expected_size
    {
        return invalid("runtime backing size differs from immutable geometry");
    }
    Ok(())
}

pub(crate) fn validate_shared_mapping_backing(
    readonly: &OwnedFd,
    mapping: Option<&OwnedFd>,
    kind: SnapshotLayerKind,
    data_residency: DataResidency,
    expected_size: u64,
) -> Result<(), VmmControllerError> {
    let required = kind == SnapshotLayerKind::Memory;
    if !required {
        if mapping.is_some() {
            return invalid("shared mapping capability is only valid for memory");
        }
        return Ok(());
    }
    let mapping = mapping.ok_or_else(|| {
        VmmControllerError::Invalid(format!(
            "{data_residency:?} memory has no shared mapping capability"
        ))
    })?;
    // SAFETY: both descriptors are live and borrowed for fcntl/fstat only.
    let flags = unsafe { libc::fcntl(mapping.as_raw_fd(), libc::F_GETFL) };
    if flags < 0 || flags & libc::O_ACCMODE != libc::O_RDWR {
        return invalid("shared mapping capability must be opened read-write");
    }
    // SAFETY: zeroed values are valid stat buffers and both descriptors live.
    let mut readonly_stat: libc::stat = unsafe { std::mem::zeroed() };
    let mut mapping_stat: libc::stat = unsafe { std::mem::zeroed() };
    if unsafe { libc::fstat(readonly.as_raw_fd(), &mut readonly_stat) } != 0
        || unsafe { libc::fstat(mapping.as_raw_fd(), &mut mapping_stat) } != 0
        || readonly_stat.st_dev != mapping_stat.st_dev
        || readonly_stat.st_ino != mapping_stat.st_ino
        || mapping_stat.st_size < 0
        || mapping_stat.st_size as u64 != expected_size
    {
        return invalid("shared mapping capability does not identify the immutable backing");
    }
    Ok(())
}

pub(crate) fn duplicate_fd(fd: &OwnedFd) -> Result<OwnedFd, VmmControllerError> {
    // SAFETY: fcntl returns a fresh descriptor for the live source FD.
    let duplicate = unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 0) };
    if duplicate < 0 {
        return Err(VmmControllerError::Io(
            std::io::Error::last_os_error().to_string(),
        ));
    }
    // SAFETY: fcntl returned this descriptor exactly once.
    Ok(unsafe { OwnedFd::from_raw_fd(duplicate) })
}

pub(crate) fn payload_readonly_fd(payload: &[u8]) -> Result<OwnedFd, VmmControllerError> {
    create_payload_readonly_fd(payload, false)
}

pub(crate) fn sealed_payload_readonly_fd(payload: &[u8]) -> Result<OwnedFd, VmmControllerError> {
    create_payload_readonly_fd(payload, true)
}

fn create_payload_readonly_fd(payload: &[u8], seal: bool) -> Result<OwnedFd, VmmControllerError> {
    let name = CString::new("rock-snapd-runtime").expect("static memfd name contains no NUL");
    let flags = libc::MFD_CLOEXEC | if seal { libc::MFD_ALLOW_SEALING } else { 0 };
    // SAFETY: name is NUL-terminated and flags are valid for memfd_create.
    let raw = unsafe { libc::memfd_create(name.as_ptr(), flags) };
    if raw < 0 {
        return Err(VmmControllerError::Io(
            std::io::Error::last_os_error().to_string(),
        ));
    }
    // SAFETY: memfd_create returned a fresh descriptor.
    let mut writable = unsafe { File::from_raw_fd(raw) };
    writable
        .write_all(payload)
        .and_then(|_| writable.sync_all())
        .map_err(|error| VmmControllerError::Io(error.to_string()))?;
    if seal {
        let seals =
            libc::F_SEAL_WRITE | libc::F_SEAL_GROW | libc::F_SEAL_SHRINK | libc::F_SEAL_SEAL;
        // SAFETY: writable owns a live memfd created with MFD_ALLOW_SEALING.
        if unsafe { libc::fcntl(writable.as_raw_fd(), libc::F_ADD_SEALS, seals) } != 0 {
            return Err(VmmControllerError::Io(
                std::io::Error::last_os_error().to_string(),
            ));
        }
    }
    let readonly = File::open(format!("/proc/self/fd/{}", writable.as_raw_fd()))
        .map_err(|error| VmmControllerError::Io(error.to_string()))?;
    Ok(readonly.into())
}

fn invalid<T>(message: &str) -> Result<T, VmmControllerError> {
    Err(VmmControllerError::Invalid(message.into()))
}

/// The runtime has already selected immutable layers and residency. These
/// borrowed inputs let the controller export protocol descriptors without
/// owning a VM record or deciding cache/layer policy.
pub(crate) enum CoverageAttachment<'a> {
    Canonical {
        fd: &'a OwnedFd,
        size: u64,
        content_id: &'a str,
    },
    Frozen(&'a OwnershipMapDescriptor),
}

pub(crate) struct LayerAttachment<'a> {
    pub kind: ProtocolLayerKind,
    pub data_residency: DataResidency,
    pub ordinal: u32,
    pub layer_id: &'a str,
    pub target_id: &'a str,
    pub content_id: &'a str,
    pub format_version: u64,
    pub logical_bytes: u64,
    pub unit_size_bytes: u64,
    pub backing: &'a OwnedFd,
    pub shared_mapping: Option<&'a OwnedFd>,
    pub coverage: CoverageAttachment<'a>,
    pub initial_residency: Option<ResidentCoverageDescriptor>,
}

pub(crate) struct VmStateAttachment<'a> {
    pub format_version: u64,
    pub payload: &'a [u8],
}

pub(crate) struct RuntimeBackingPayload {
    pub layers: Vec<BackingDescriptor>,
    pub vm_state: Option<VmStateDescriptor>,
    pub fd_roles: Vec<FdRoleDescriptor>,
    pub fds: Vec<OwnedFd>,
}

pub(crate) fn build_runtime_backing(
    attachments: &[LayerAttachment<'_>],
    state: Option<VmStateAttachment<'_>>,
) -> Result<RuntimeBackingPayload, VmmControllerError> {
    let mut fds = Vec::new();
    let mut roles = Vec::new();
    let mut layers = Vec::new();
    for layer in attachments {
        let kind = layer.kind;
        let backing = duplicate_fd(layer.backing)?;
        let backing_index = u32::try_from(fds.len())
            .map_err(|_| VmmControllerError::Invalid("too many attach descriptors".into()))?;
        fds.push(backing);
        roles.push(FdRoleDescriptor {
            role: layer_role(kind),
            access: FdAccess::ReadOnly,
            content_id: sha_hex(layer.content_id),
            size: layer.logical_bytes,
            alignment: layer.unit_size_bytes,
        });
        let shared_mapping_fd_index = if let Some(mapping) = layer.shared_mapping {
            let mapping_index = u32::try_from(fds.len())
                .map_err(|_| VmmControllerError::Invalid("too many attach descriptors".into()))?;
            fds.push(duplicate_fd(mapping)?);
            roles.push(FdRoleDescriptor {
                role: FdRole::MemorySharedMapping,
                access: FdAccess::ReadWrite,
                content_id: sha_hex(layer.content_id),
                size: layer.logical_bytes,
                alignment: layer.unit_size_bytes,
            });
            Some(mapping_index)
        } else {
            None
        };
        let (range_index, local_ownership) = match &layer.coverage {
            CoverageAttachment::Canonical {
                fd,
                size,
                content_id,
            } => {
                let index_fd = u32::try_from(fds.len()).map_err(|_| {
                    VmmControllerError::Invalid("too many attach descriptors".into())
                })?;
                fds.push(duplicate_fd(fd)?);
                roles.push(FdRoleDescriptor {
                    role: range_role(kind),
                    access: FdAccess::ReadOnly,
                    content_id: (*content_id).to_owned(),
                    size: *size,
                    alignment: 1,
                });
                (
                    Some(RangeIndexDescriptor {
                        format_version: layer.format_version,
                        content_id: (*content_id).to_owned(),
                        size: *size,
                        alignment: 1,
                        fd_index: index_fd,
                    }),
                    None,
                )
            }
            CoverageAttachment::Frozen(ownership) => {
                if layer.data_residency != DataResidency::LocalComplete {
                    return invalid("frozen-local coverage requires local-complete residency");
                }
                if kind == ProtocolLayerKind::Memory && shared_mapping_fd_index.is_none() {
                    return invalid("frozen-local memory requires a shared mapping capability");
                }
                (None, Some((**ownership).clone()))
            }
        };
        layers.push(BackingDescriptor {
            kind,
            data_residency: layer.data_residency,
            ordinal: layer.ordinal,
            layer_id: layer.layer_id.to_owned(),
            target_id: layer.target_id.to_owned(),
            content_id: sha_hex(layer.content_id),
            range_index,
            local_ownership,
            initial_residency: layer.initial_residency.clone(),
            logical_size: layer.logical_bytes,
            alignment: layer.unit_size_bytes,
            io_alignment: io_alignment(kind),
            fd_index: Some(backing_index),
            shared_mapping_fd_index,
            block_path: None,
        });
    }
    let vm_state = if let Some(state) = state {
        let content_id = format!("{:x}", Sha256::digest(state.payload));
        let fd = payload_readonly_fd(state.payload)?;
        let fd_index = u32::try_from(fds.len())
            .map_err(|_| VmmControllerError::Invalid("too many attach descriptors".into()))?;
        fds.push(fd);
        roles.push(FdRoleDescriptor {
            role: FdRole::VmState,
            access: FdAccess::ReadOnly,
            content_id: content_id.clone(),
            size: state.payload.len() as u64,
            alignment: 1,
        });
        Some(VmStateDescriptor {
            format_version: state.format_version,
            content_id,
            size: state.payload.len() as u64,
            alignment: 1,
            fd_index,
        })
    } else {
        None
    };
    Ok(RuntimeBackingPayload {
        layers,
        vm_state,
        fd_roles: roles,
        fds,
    })
}

pub(crate) fn build_start_request(
    sandbox_id: &str,
    snapshot_id: &str,
    spec_sha256: &str,
    launch: &VmmLaunchSpec,
    correlation_id: &str,
    deadline_unix_ms: u64,
    mut required_capabilities: Vec<Capability>,
    backing: RuntimeBackingPayload,
) -> (WireEnvelope<ControlRequest>, Vec<OwnedFd>) {
    if backing
        .layers
        .iter()
        .any(|layer| layer.initial_residency.is_some())
        && !required_capabilities
            .iter()
            .any(|capability| capability.name == INITIAL_RESIDENCY_CAPABILITY)
    {
        required_capabilities.push(Capability {
            name: INITIAL_RESIDENCY_CAPABILITY.into(),
            version: 1,
        });
    }
    (
        WireEnvelope {
            header: current_header(correlation_id.into()),
            message: ControlRequest::StartRuntime(StartRuntime {
                sandbox_id: sandbox_id.to_owned(),
                snapshot_id: snapshot_id.to_owned(),
                spec_sha256: spec_sha256.to_owned(),
                launch_identity: launch.launch_identity.clone(),
                config_sha256: launch.config_sha256(),
                required_capabilities,
                deadline_unix_ms,
                layers: backing.layers,
                vm_state: backing.vm_state,
                fd_roles: backing.fd_roles,
            }),
        },
        backing.fds,
    )
}

pub(crate) fn current_header(correlation_id: String) -> MessageHeader {
    MessageHeader {
        protocol_version: VMM_PROTOCOL_VERSION,
        channel: Channel::RuntimeControl,
        correlation_id,
    }
}

pub(crate) fn protocol_kind(kind: SnapshotLayerKind) -> ProtocolLayerKind {
    match kind {
        SnapshotLayerKind::Memory => ProtocolLayerKind::Memory,
        SnapshotLayerKind::Block => ProtocolLayerKind::Block,
    }
}

pub(crate) fn io_alignment(kind: ProtocolLayerKind) -> u64 {
    match kind {
        ProtocolLayerKind::Memory => MEMORY_IO_ALIGNMENT,
        ProtocolLayerKind::Block => BLOCK_IO_ALIGNMENT,
    }
}

pub(crate) fn layer_role(kind: ProtocolLayerKind) -> FdRole {
    match kind {
        ProtocolLayerKind::Memory => FdRole::MemoryRoLayer,
        ProtocolLayerKind::Block => FdRole::BlockRoLayer,
    }
}

pub(crate) fn range_role(kind: ProtocolLayerKind) -> FdRole {
    match kind {
        ProtocolLayerKind::Memory => FdRole::MemoryRangeIndex,
        ProtocolLayerKind::Block => FdRole::BlockRangeIndex,
    }
}

pub(crate) fn sha_hex(identity: &str) -> String {
    identity
        .strip_prefix("sha256:")
        .unwrap_or(identity)
        .to_lowercase()
}

#[cfg(test)]
mod resource_ownership_tests {
    use super::*;
    use crate::snapshot::format::UploadedSnapshot;
    use crate::snapshot::io::backing::LocalBackingRegistry;
    use crate::snapshot::io::range_io::{RangeReadRequest, RangeReadResult, RangeReader};
    use crate::snapshot::io::snapshot_io::SnapshotIo;
    use crate::snapshot::metadata::{
        RockletArtifactCompatibility, StorageDescriptor, StorageProvider, VmmCompatibility,
    };
    use crate::snapshot::repository::frozen_layer::{
        CapturedSnapshotPin, FrozenLayerMetadata, FrozenLayerPin, PackedOwnershipMap,
        VmStateMetadata, VmStatePin,
    };
    use crate::snapshot::repository::lineage::SnapshotLineage;
    use crate::snapshot::transport::{posix_file, RangeReadError};
    use std::os::unix::fs::FileExt;
    use std::path::{Path, PathBuf};

    struct TestRoot(PathBuf);
    impl TestRoot {
        fn new() -> Self {
            Self(std::env::temp_dir().join(format!("rock-prepared-owner-{}", uuid::Uuid::new_v4())))
        }
    }
    impl Drop for TestRoot {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[derive(Debug)]
    struct NoProviderReads;
    #[async_trait::async_trait]
    impl RangeReader for NoProviderReads {
        async fn read_range(
            &self,
            _request: RangeReadRequest<'_>,
        ) -> Result<RangeReadResult, RangeReadError> {
            panic!("staging a zero-only base must not read remote DATA");
        }
    }

    fn service(root: &Path) -> Arc<RangeLoadExecutor> {
        RangeLoadExecutor::new(
            LocalBackingRegistry::new(root.join("backings"), "prepared-owner").unwrap(),
            SnapshotIo::data_only(),
            HashMap::from([(
                "fixture".into(),
                Arc::new(NoProviderReads) as Arc<dyn RangeReader>,
            )]),
            16 * 1024 * 1024,
        )
        .unwrap()
    }

    fn repository() -> VmRepository {
        VmRepository::new(
            None,
            Vec::new(),
            Duration::from_secs(1),
            Duration::from_secs(1),
            1,
            Duration::from_secs(1),
        )
        .unwrap()
    }

    fn raw_snapshot(id: &str, full: bool) -> Arc<Snapshot> {
        raw_snapshot_with_lineage(id, full, SnapshotLineage::default())
    }

    fn raw_snapshot_with_lineage(id: &str, full: bool, lineage: SnapshotLineage) -> Arc<Snapshot> {
        let mut pins = Vec::new();
        for (kind, unit) in [
            (SnapshotLayerKind::Memory, 4096),
            (SnapshotLayerKind::Block, 65536),
        ] {
            if !full && kind == SnapshotLayerKind::Memory {
                continue;
            }
            let file =
                posix_file::memory_file(&CString::new(format!("{id}-{kind:?}")).unwrap(), unit)
                    .unwrap();
            pins.push(FrozenLayerPin::new(
                FrozenLayerMetadata {
                    kind,
                    layer_id: format!("{id}-{kind:?}"),
                    provisional_content_id: sha256_identity(format!("{id}-{kind:?}").as_bytes()),
                    logical_size: unit,
                    alignment: unit,
                },
                PackedOwnershipMap {
                    kind,
                    snapshot_id: id.into(),
                    format_version: 1,
                    logical_size: unit,
                    granularity: unit,
                    packed_states: vec![2],
                },
                file.into(),
                false,
            ));
        }
        if !full {
            return Snapshot::from_base_layer(
                id.into(),
                pins.pop().unwrap(),
                crate::snapshot::metadata::SnapshotProvenance {
                    oci_ref: None,
                    oci_digest: None,
                },
            )
            .unwrap();
        }
        let payload = b"prepared VM state";
        let writer = posix_file::memory_file(
            &CString::new("prepared-vm-state").unwrap(),
            payload.len() as u64,
        )
        .unwrap();
        writer.write_all_at(payload, 0).unwrap();
        let pin = CapturedSnapshotPin::new(
            id.into(),
            pins,
            VmStatePin::new(
                VmStateMetadata {
                    format_version: 1,
                    content_id: sha256_identity(payload),
                    size: payload.len() as u64,
                    alignment: 1,
                },
                posix_file::reopen_readonly(writer.as_raw_fd())
                    .unwrap()
                    .into(),
            ),
        )
        .unwrap();
        Snapshot::from_capture(
            pin,
            lineage,
            RockletArtifactCompatibility {
                identity: sha256_identity(b"prepared-rocklet"),
                version: "v1".into(),
                raw_sha256: sha256_identity(b"prepared-rocklet"),
                logical_bytes: 65536,
                guest_port: 4400,
            },
        )
        .unwrap()
    }

    #[tokio::test]
    async fn remote_restore_and_later_captures_retain_original_image_provenance() {
        use crate::snapshot::metadata::SnapshotProvenance;

        for (case, provenance) in [
            (
                "tag",
                Some(SnapshotProvenance {
                    oci_ref: Some("source.example/team/application:v1".into()),
                    oci_digest: Some(sha256_identity(b"original resolved image")),
                }),
            ),
            (
                "digest",
                Some(SnapshotProvenance {
                    oci_ref: Some(format!(
                        "source.example/team/application@{}",
                        sha256_identity(b"original resolved image")
                    )),
                    oci_digest: Some(sha256_identity(b"original resolved image")),
                }),
            ),
            ("legacy", None),
        ] {
            let root = TestRoot::new();
            let service = service(&root.0);
            let preparer = SnapshotRestorePreparer::new(service, "fixture", None).unwrap();
            let repository = repository();
            let original = raw_snapshot_with_lineage(
                &format!("origin-{case}"),
                true,
                SnapshotLineage::default().with_provenance(provenance.clone()),
            );
            let imported = original
                .published_snapshot(&publication(&original))
                .unwrap();
            let runtime = RuntimeIdentity::new(format!("restored-{case}"));
            let prepared = preparer
                .prepare(imported.as_ref(), &runtime, &repository, None)
                .await
                .unwrap();
            assert_eq!(prepared.lineage().provenance, provenance);
            let binding =
                crate::vm::repository::bind_prepared(&prepared, &runtime, "fixture").unwrap();
            assert_eq!(binding.lineage.provenance(), provenance.as_ref());
            let captured = raw_snapshot_with_lineage(
                &format!("captured-{case}"),
                true,
                binding.lineage.clone(),
            );
            assert_eq!(captured.provenance(), provenance.as_ref());
            let child = raw_snapshot_with_lineage(
                &format!("child-{case}"),
                true,
                captured.lineage().with_frozen(Arc::clone(&captured)),
            );
            assert_eq!(child.provenance(), provenance.as_ref());
            assert_eq!(publication(&child).spec.provenance, provenance);
        }
    }

    fn publication(raw: &Snapshot) -> UploadedSnapshot {
        let full = raw.snapshot_kind() == SnapshotKind::Full;
        let plan = raw
            .upload_plan(
                StorageDescriptor {
                    provider: StorageProvider::S3,
                    endpoint_profile: "fixture".into(),
                    credential_ref: "fixture:v1".into(),
                },
                VmmCompatibility {
                    architecture: "x86_64".into(),
                    vmm_type: if full { "cloud-hypervisor" } else { "any" }.into(),
                    vmm_state_format_version: full.then_some(1),
                    rocklet_artifact: None,
                },
            )
            .unwrap();
        UploadedSnapshot {
            registry_objects: None,
            spec: plan.spec,
            manifest: serde_json::from_slice(&plan.manifest.payload).unwrap(),
        }
    }

    fn grant(
        snapshot: &dyn SnapshotInstance,
        runtime: &RuntimeIdentity,
        repository: &VmRepository,
        kind: SnapshotLayerKind,
    ) -> RemoteLayerTargetGrant {
        let layer = &snapshot.chain_view(kind).unwrap().layers[0];
        RemoteLayerTargetGrant {
            runtime: runtime.clone(),
            target_id: repository.remote_target_id(runtime, kind, 0),
            backing_key: layer_key(&layer.range_map).unwrap(),
            provider: "fixture".into(),
            authorization: None,
        }
    }

    #[tokio::test]
    async fn prepared_runtime_owns_same_layer_lease_before_launch_and_releases_on_drop() {
        let root = TestRoot::new();
        let service = service(&root.0);
        let preparer = SnapshotRestorePreparer::new(service.clone(), "fixture", None).unwrap();
        let repository = repository();
        let raw = raw_snapshot("prepared-owner", false);
        let snapshot = raw.published_snapshot(&publication(&raw)).unwrap();
        let runtime = RuntimeIdentity::new("prepared-owner-runtime");
        let target = grant(
            snapshot.as_ref(),
            &runtime,
            &repository,
            SnapshotLayerKind::Block,
        );
        let prepared = preparer
            .prepare(snapshot.as_ref(), &runtime, &repository, None)
            .await
            .unwrap();
        let (resources, created) = service.prepare_target(target.clone()).await.unwrap();
        assert!(
            !created,
            "preparation must retain the exact registered layer resource"
        );
        assert!(resources.reader().is_some());
        let retained = Arc::downgrade(&resources);
        drop(resources);
        assert!(
            retained.upgrade().is_some(),
            "prepared object must own resources before VMM launch"
        );
        drop(prepared);
        assert!(
            retained.upgrade().is_none(),
            "loader indexes must not own abandoned prepared resources"
        );
        assert!(service
            .backing_for_target(&target.target_id, &runtime)
            .await
            .is_err());
    }

    #[tokio::test]
    async fn cancelling_preparation_while_compiling_releases_pending_target_lease() {
        let root = TestRoot::new();
        let service = service(&root.0);
        let preparer = SnapshotRestorePreparer::new(service.clone(), "fixture", None).unwrap();
        let repository = repository();
        let raw = raw_snapshot("cancel-prepared", false);
        let snapshot = raw.published_snapshot(&publication(&raw)).unwrap();
        let runtime = RuntimeIdentity::new("cancel-prepared-runtime");
        let target = grant(
            snapshot.as_ref(),
            &runtime,
            &repository,
            SnapshotLayerKind::Block,
        );
        let slots = Arc::clone(&preparer.compiled_layers.compile_slots)
            .acquire_many_owned(COMPILED_LAYER_CACHE_MAX_INFLIGHT_COMPILES as u32)
            .await
            .unwrap();
        let prepare_task = tokio::spawn({
            let preparer = Arc::clone(&preparer);
            let runtime = runtime.clone();
            async move {
                preparer
                    .prepare(snapshot.as_ref(), &runtime, &repository, None)
                    .await
            }
        });
        tokio::time::timeout(Duration::from_secs(2), async {
            while service
                .backing_for_target(&target.target_id, &runtime)
                .await
                .is_err()
            {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("target registration must precede blocked compilation");
        let (resource, created) = service.prepare_target(target.clone()).await.unwrap();
        assert!(!created);
        let retained = Arc::downgrade(&resource);
        drop(resource);
        assert!(
            retained.upgrade().is_some(),
            "prepare future must hold the uninstalled resource"
        );
        prepare_task.abort();
        assert!(prepare_task.await.unwrap_err().is_cancelled());
        assert!(
            retained.upgrade().is_none(),
            "cancelled preparation leaked its pending layer lease"
        );
        drop(slots);
        assert!(service
            .backing_for_target(&target.target_id, &runtime)
            .await
            .is_err());
    }

    #[tokio::test]
    async fn finalization_rollback_only_revokes_targets_created_by_that_preparation() {
        let root = TestRoot::new();
        let service = service(&root.0);
        let preparer = SnapshotRestorePreparer::new(service.clone(), "fixture", None).unwrap();
        let repository = repository();
        let raw = raw_snapshot("prepared-promotion", true);
        let published = publication(&raw);
        let snapshot = raw.published_snapshot(&published).unwrap();
        let runtime = RuntimeIdentity::new("prepared-promotion-runtime");
        let memory = grant(
            snapshot.as_ref(),
            &runtime,
            &repository,
            SnapshotLayerKind::Memory,
        );
        let source = raw.pin_native_layer(SnapshotLayerKind::Memory).unwrap();
        let (existing, created) = service
            .prepare_promotion_target(memory.clone(), source.file())
            .await
            .unwrap();
        assert!(created);
        let prepared = preparer
            .prepare_finalization(raw.as_ref(), &published.spec, &runtime, &repository)
            .await
            .unwrap();
        assert_eq!(prepared.resources.len(), 2);
        assert!(Arc::ptr_eq(&prepared.resources[0], &existing));
        assert_eq!(prepared.created_resources.len(), 1);
        assert!(!prepared
            .created_resources
            .iter()
            .any(|resource| Arc::ptr_eq(resource, &existing)));
        let block = grant(
            snapshot.as_ref(),
            &runtime,
            &repository,
            SnapshotLayerKind::Block,
        );
        let newly_created = Arc::downgrade(&prepared.resources[1]);
        prepared.rollback(&service).await;
        assert!(service
            .backing_for_target(&memory.target_id, &runtime)
            .await
            .is_ok());
        assert!(newly_created.upgrade().is_none());
        assert!(service
            .backing_for_target(&block.target_id, &runtime)
            .await
            .is_err());
    }
}
