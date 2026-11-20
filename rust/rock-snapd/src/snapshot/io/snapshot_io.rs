//! Snapshot-level metadata assembly, layer DATA dispatch and ordered publication.
//!
//! Startup reads manifests, chain indexes, layer indexes and VM state, but
//! never fetches or probes a DATA pack. DATA objects remain non-resident until
//! a VMM RemoteLoad request. Recovery/audit callers may select the strict mode,
//! which additionally checks every DATA object with `HEAD`.

#[cfg(test)]
use crate::snapshot::metadata::RangeMapEncoding;
use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::mem::size_of;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

use std::sync::{Arc, Mutex as StdMutex, Weak};
use std::time::{Duration, Instant};

use async_trait::async_trait;

#[cfg(test)]
use serde_json::json;
use serde_json::Value;
use thiserror::Error;
use tokio::sync::{watch, Mutex, Notify, OnceCell, OwnedSemaphorePermit, Semaphore};
use tokio::task::JoinSet;

#[cfg(test)]
use crate::snapshot::format::overlaybd::MAX_BLOB_BYTES;
use crate::snapshot::metadata::{
    canonical_json_bytes, parse_canonical_model, sha256_identity, snapshot_spec_checksum,
    LayerRangeMap, SnapshotSpec, StorageDescriptor, MAX_RANGE_MAP_BYTES,
    MAX_SNAPSHOT_METADATA_BYTES,
};
use crate::snapshot::transport::{
    BoundedGetError, ImmutableObjectStore, ObjectStoreError, RemoteObjectMetadata,
};
use crate::snapshot::{SourceError, SourceResolver};

use crate::observability::metrics::{DataUploadOutcome, SnapdMetrics};
use crate::snapshot::format::overlaybd_s3::data::{DataPackIdentity, DataPackUploadRequest};
use crate::snapshot::format::overlaybd_s3::publication::OverlaybdS3Publication;
use crate::snapshot::format::{
    ImmutableObject, LayerUploadPlan, SnapshotPublicationContext, SnapshotUploadError,
    SnapshotUploadInterface, SnapshotUploadPlan, SnapshotUploadSource, UploadedSnapshot,
};
use crate::snapshot::io::range_io::{RangeIO, RangeReader, RangeWriter};
use crate::snapshot::transport::{
    DataPackSource, DataPackUploadReceipt, DataPackWriteTarget, ImmutableObjectIdentity,
    IoCancellation, PublishFailureKind, ReplayableByteSource, SnapshotDataUploadError,
    SnapshotMetadataWriteTarget, UploadTransactionLease,
};
#[cfg(test)]
use crate::snapshot::{LazyLoadContext, LazyLoadError, LazyLoadInterface, SnapshotReadRange};

#[cfg(test)]
use crate::snapshot::format::overlaybd_s3::data::{DataPackObjectKey, DataPackSliceIdentity};
#[cfg(test)]
use crate::snapshot::format::overlaybd_s3::scan::{
    scan_encoded_source, scan_source, ZFILE_SCAN_RAW_BATCH_BYTES,
};
#[cfg(test)]
use crate::snapshot::transport::DataPackSourceError;
#[cfg(test)]
use crate::snapshot::transport::ImmutableStreamWriter;
#[cfg(test)]
use sha2::{Digest, Sha256};

const DEFAULT_START_CACHE_ENTRIES: usize = 64;
const DEFAULT_START_CACHE_BYTES: usize = 512 * 1024 * 1024;
const DEFAULT_START_LOAD_CONCURRENCY: usize = 8;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SnapshotLoadLimits {
    pub max_spec_bytes: usize,
    pub max_manifest_bytes: usize,
    pub max_chain_index_bytes: usize,
    pub max_range_map_bytes: usize,
    pub max_vm_state_bytes: usize,
    pub max_total_metadata_bytes: usize,
}

impl Default for SnapshotLoadLimits {
    fn default() -> Self {
        Self {
            max_spec_bytes: 1024 * 1024,
            max_manifest_bytes: 1024 * 1024,
            max_chain_index_bytes: 8 * 1024 * 1024,
            max_range_map_bytes: MAX_RANGE_MAP_BYTES,
            max_vm_state_bytes: 64 * 1024 * 1024,
            max_total_metadata_bytes: MAX_SNAPSHOT_METADATA_BYTES,
        }
    }
}

impl SnapshotLoadLimits {
    fn validate(self) -> Result<Self, SnapshotLoadError> {
        if self.max_spec_bytes == 0
            || self.max_manifest_bytes == 0
            || self.max_chain_index_bytes == 0
            || self.max_range_map_bytes == 0
            || self.max_vm_state_bytes == 0
            || self.max_total_metadata_bytes == 0
        {
            return Err(SnapshotLoadError::InvalidSnapshotMetadata(
                "snapshot load limits must be positive".into(),
            ));
        }
        Ok(self)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SnapshotStartCacheLimits {
    pub max_entries: usize,
    pub max_bytes: usize,
    /// Count-bound for distinct metadata graphs being fetched or parsed.
    /// Each producer is also constrained by `SnapshotLoadLimits`; this is not
    /// a weighted-byte admission controller.
    pub max_inflight: usize,
    /// Service-owned deadline, independent of any first caller's HTTP timeout.
    pub producer_timeout: Duration,
}

impl Default for SnapshotStartCacheLimits {
    fn default() -> Self {
        Self {
            max_entries: DEFAULT_START_CACHE_ENTRIES,
            max_bytes: DEFAULT_START_CACHE_BYTES,
            max_inflight: DEFAULT_START_LOAD_CONCURRENCY,
            producer_timeout: Duration::from_secs(60),
        }
    }
}

impl SnapshotStartCacheLimits {
    fn validate(self) -> Result<Self, SnapshotLoadError> {
        if self.max_entries == 0
            || self.max_bytes == 0
            || self.max_inflight == 0
            || self.producer_timeout.is_zero()
        {
            return Err(SnapshotLoadError::InvalidSnapshotMetadata(
                "snapshot start cache limits must be positive".into(),
            ));
        }
        Ok(self)
    }
}

#[derive(Debug, Error, Clone)]
pub enum SnapshotLoadError {
    #[error("Invalid remote snapshot bootstrap: {0}")]
    InvalidSnapshotMetadata(String),
    #[error("Remote snapshot identity conflict: {0}")]
    IdentityConflict(String),
    #[error("Required remote snapshot object is missing: {0}")]
    MissingObject(String),
    #[error("Remote snapshot metadata load was invalidated: {0}")]
    Invalidated(String),
    #[error("Remote snapshot metadata exceeds a configured bound: {0}")]
    BoundExceeded(String),
    #[error(transparent)]
    Store(#[from] ObjectStoreError),
}

impl SnapshotLoadError {
    pub fn retryable(&self) -> bool {
        matches!(self, Self::Invalidated(_))
            || matches!(self, Self::Store(error) if error.retryable())
    }
}

#[cfg(test)]
use crate::snapshot::metadata::{ChainIndexRef, LayerChainIndex, LayerRef, RangeKind};
use crate::snapshot::{LayerKind, SnapshotInstance};

#[derive(Debug, Clone)]
pub(crate) struct VerifiedMetadata {
    payload: Vec<u8>,
    sha256: String,
    version: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct StartCacheKey {
    snapshot_id: String,
    spec_checksum: String,
}

#[derive(Debug)]
struct StartCacheEntry {
    metadata: Arc<dyn SnapshotInstance>,
    retained_bytes: usize,
    last_used: u64,
}

#[derive(Debug, Clone)]
struct SharedStartLoad {
    metadata: Arc<dyn SnapshotInstance>,
    retained_bytes: usize,
}

#[derive(Debug)]
struct InFlightStartLoad {
    invalidated: AtomicBool,
    result: OnceCell<Result<SharedStartLoad, SnapshotLoadError>>,
    completion: Notify,
}

#[derive(Debug)]
struct SnapshotLoadEpoch {
    invalidated: AtomicBool,
    invalidated_state: watch::Sender<bool>,
}

impl SnapshotLoadEpoch {
    fn new() -> Arc<Self> {
        let (invalidated_state, _) = watch::channel(false);
        Arc::new(Self {
            invalidated: AtomicBool::new(false),
            invalidated_state,
        })
    }

    fn invalidate(&self) {
        self.invalidated.store(true, Ordering::Release);
        self.invalidated_state.send_replace(true);
    }

    fn is_invalidated(&self) -> bool {
        self.invalidated.load(Ordering::Acquire)
    }

    async fn acquire_permit(
        &self,
        permits: Arc<Semaphore>,
        snapshot_id: &str,
    ) -> Result<OwnedSemaphorePermit, SnapshotLoadError> {
        let mut invalidated = self.invalidated_state.subscribe();
        if self.is_invalidated() {
            return Err(SnapshotLoadError::Invalidated(snapshot_id.to_owned()));
        }
        tokio::select! {
            biased;
            _ = invalidated.changed() => {
                Err(SnapshotLoadError::Invalidated(snapshot_id.to_owned()))
            }
            permit = permits.acquire_owned() => {
                let permit = permit.expect("snapshot start load semaphore is never closed");
                if self.is_invalidated() {
                    Err(SnapshotLoadError::Invalidated(snapshot_id.to_owned()))
                } else {
                    Ok(permit)
                }
            }
        }
    }
}

impl InFlightStartLoad {
    fn finish(&self, result: Result<SharedStartLoad, SnapshotLoadError>) {
        let _ = self.result.set(result);
        self.completion.notify_waiters();
    }

    async fn wait(&self) -> Result<SharedStartLoad, SnapshotLoadError> {
        loop {
            let notified = self.completion.notified();
            if let Some(result) = self.result.get() {
                return result.clone();
            }
            notified.await;
        }
    }
}

struct StartProducerCompletionGuard {
    attempt: Arc<InFlightStartLoad>,
    armed: bool,
}

impl StartProducerCompletionGuard {
    fn new(attempt: Arc<InFlightStartLoad>) -> Self {
        Self {
            attempt,
            armed: true,
        }
    }

    fn disarm(&mut self) {
        self.armed = false;
    }
}

impl Drop for StartProducerCompletionGuard {
    fn drop(&mut self) {
        if self.armed {
            self.attempt
                .finish(Err(SnapshotLoadError::Store(ObjectStoreError::new(
                    crate::snapshot::transport::PublishFailureKind::Retryable,
                    "snapshot metadata loader task was cancelled",
                ))));
        }
    }
}

#[derive(Debug, Default)]
struct StartCacheState {
    entries: HashMap<StartCacheKey, StartCacheEntry>,
    inflight: HashMap<StartCacheKey, Arc<InFlightStartLoad>>,
    retained_bytes: usize,
    clock: u64,
    load_epochs: HashMap<String, Weak<SnapshotLoadEpoch>>,
}

#[derive(Debug)]
struct ValidatedSnapshotSpec {
    spec: SnapshotSpec,
    canonical_bytes: usize,
}

#[derive(Debug)]
struct LoadedSnapshotWithSize {
    metadata: Arc<dyn SnapshotInstance>,
    retained_bytes: usize,
}

/// Per-open byte I/O capabilities, independent of snapshot metadata profiles.
/// Formats own graph interpretation; bounded reads, SHA/version verification and
/// transport selection stay here and in RangeIO.
pub(crate) struct SnapshotMetadataReadContext<'a> {
    store: Option<&'a dyn ImmutableObjectStore>,
    range_reader: &'a dyn RangeReader,
    pub(crate) limits: SnapshotLoadLimits,
    pub(crate) source_resolver: &'a dyn SourceResolver,
}

impl SnapshotMetadataReadContext<'_> {
    pub(crate) fn with_store_and_reader<'a>(
        &'a self,
        store: &'a dyn ImmutableObjectStore,
        range_reader: &'a dyn RangeReader,
        source_resolver: &'a dyn SourceResolver,
    ) -> SnapshotMetadataReadContext<'a> {
        SnapshotMetadataReadContext {
            store: Some(store),
            range_reader,
            limits: self.limits,
            source_resolver,
        }
    }
    fn metadata_store(&self) -> Result<&dyn ImmutableObjectStore, SnapshotLoadError> {
        self.store.ok_or_else(|| {
            SnapshotLoadError::MissingObject(
                "snapshot metadata transport is not configured for this DATA-only reader".into(),
            )
        })
    }
    #[allow(clippy::too_many_arguments)]
    pub(crate) async fn read_verified(
        &self,
        object_key: &str,
        expected_sha256: &str,
        expected_version: Option<&str>,
        max_bytes: usize,
        total_bytes: &mut usize,
        cache: &mut HashMap<String, VerifiedMetadata>,
    ) -> Result<Vec<u8>, SnapshotLoadError> {
        if let Some(verified) = cache.get(object_key) {
            if verified.sha256 != expected_sha256
                || expected_version
                    .is_some_and(|version| verified.version.as_deref() != Some(version))
            {
                return Err(SnapshotLoadError::IdentityConflict(format!(
                    "object {object_key:?} has conflicting references"
                )));
            }
            return Ok(verified.payload.clone());
        }
        let object = self
            .range_reader
            .read_metadata(
                self.metadata_store()?,
                object_key,
                expected_version,
                max_bytes as u64,
            )
            .await
            .map_err(|error| match error {
                BoundedGetError::BoundExceeded { .. } => {
                    SnapshotLoadError::BoundExceeded(format!("object {object_key:?}"))
                }
                BoundedGetError::Unsupported { .. } => {
                    SnapshotLoadError::Store(ObjectStoreError::new(
                        crate::snapshot::transport::PublishFailureKind::Fatal,
                        format!(
                            "object store does not support bounded metadata GET for {object_key:?}"
                        ),
                    ))
                }
                BoundedGetError::Store(error) => SnapshotLoadError::Store(error),
            })?
            .ok_or_else(|| SnapshotLoadError::MissingObject(object_key.to_owned()))?;
        self.verify_metadata_identity(
            object_key,
            &object.metadata,
            expected_sha256,
            expected_version,
        )?;
        if object.metadata.size > max_bytes as u64 {
            return Err(SnapshotLoadError::BoundExceeded(format!(
                "object {object_key:?}"
            )));
        }
        let payload = object.payload;
        if payload.len() as u64 != object.metadata.size
            || sha256_identity(&payload) != expected_sha256
        {
            return Err(SnapshotLoadError::IdentityConflict(format!(
                "object {object_key:?} payload conflicts with metadata"
            )));
        }
        *total_bytes = total_bytes
            .checked_add(payload.len())
            .ok_or_else(|| SnapshotLoadError::BoundExceeded("total metadata byte count".into()))?;
        if *total_bytes > self.limits.max_total_metadata_bytes {
            return Err(SnapshotLoadError::BoundExceeded(
                "total metadata byte count".into(),
            ));
        }
        cache.insert(
            object_key.to_owned(),
            VerifiedMetadata {
                payload: payload.clone(),
                sha256: expected_sha256.to_owned(),
                version: object.metadata.version,
            },
        );
        Ok(payload)
    }

    pub(crate) async fn head_required(
        &self,
        object_key: &str,
    ) -> Result<RemoteObjectMetadata, SnapshotLoadError> {
        self.range_reader
            .head_metadata(self.metadata_store()?, object_key)
            .await?
            .ok_or_else(|| SnapshotLoadError::MissingObject(object_key.to_owned()))
    }

    pub(crate) fn verify_metadata_identity(
        &self,
        object_key: &str,
        metadata: &RemoteObjectMetadata,
        expected_sha256: &str,
        expected_version: Option<&str>,
    ) -> Result<(), SnapshotLoadError> {
        if metadata.sha256 != expected_sha256
            || expected_version.is_some_and(|version| metadata.version.as_deref() != Some(version))
        {
            return Err(SnapshotLoadError::IdentityConflict(format!(
                "remote object identity conflicts for {object_key:?}"
            )));
        }
        Ok(())
    }
}

pub struct SnapshotIo {
    store: Option<Arc<dyn ImmutableObjectStore>>,
    range_reader: Arc<dyn RangeReader>,
    limits: SnapshotLoadLimits,
    expected_storage: Option<StorageDescriptor>,
    start_cache_limits: SnapshotStartCacheLimits,
    start_cache: Mutex<StartCacheState>,
    start_load_permits: Arc<Semaphore>,
    source_resolver: Arc<dyn SourceResolver>,
}

impl std::fmt::Debug for SnapshotIo {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("SnapshotIo")
            .field("metadata_configured", &self.store.is_some())
            .field("limits", &self.limits)
            .field("start_cache_limits", &self.start_cache_limits)
            .finish_non_exhaustive()
    }
}

impl SnapshotIo {
    /// Configure physical I/O policy before sharing the snapshot assembler.
    pub fn with_range_reader(mut self: Arc<Self>, reader: Arc<dyn RangeReader>) -> Arc<Self> {
        Arc::get_mut(&mut self)
            .expect("range I/O is configured before sharing the snapshot reader")
            .range_reader = reader;
        self
    }

    pub(crate) fn with_source_resolver(
        mut self: Arc<Self>,
        source_resolver: Arc<dyn SourceResolver>,
    ) -> Arc<Self> {
        Arc::get_mut(&mut self)
            .expect("source resolver is configured before sharing the loader")
            .source_resolver = source_resolver;
        self
    }

    pub fn new(store: Arc<dyn ImmutableObjectStore>) -> Arc<Self> {
        Self::new_with_limits(store, SnapshotLoadLimits::default())
            .expect("default snapshot load limits are valid")
    }

    pub fn new_with_limits(
        store: Arc<dyn ImmutableObjectStore>,
        limits: SnapshotLoadLimits,
    ) -> Result<Arc<Self>, SnapshotLoadError> {
        Self::new_with_limits_and_cache(store, limits, SnapshotStartCacheLimits::default())
    }

    pub fn new_with_limits_and_cache(
        store: Arc<dyn ImmutableObjectStore>,
        limits: SnapshotLoadLimits,
        start_cache_limits: SnapshotStartCacheLimits,
    ) -> Result<Arc<Self>, SnapshotLoadError> {
        Self::build(store, limits, None, start_cache_limits)
    }

    pub fn new_with_storage_identity(
        store: Arc<dyn ImmutableObjectStore>,
        expected_storage: StorageDescriptor,
        limits: SnapshotLoadLimits,
    ) -> Result<Arc<Self>, SnapshotLoadError> {
        Self::new_with_storage_identity_and_cache(
            store,
            expected_storage,
            limits,
            SnapshotStartCacheLimits::default(),
        )
    }

    pub(crate) fn new_with_optional_storage_identity(
        store: Option<Arc<dyn ImmutableObjectStore>>,
        expected_storage: Option<StorageDescriptor>,
        limits: SnapshotLoadLimits,
    ) -> Result<Arc<Self>, SnapshotLoadError> {
        if store.is_some() != expected_storage.is_some() {
            return Err(SnapshotLoadError::IdentityConflict(
                "Metadata transport and storage identity must be configured together".into(),
            ));
        }
        match (store, expected_storage) {
            (Some(store), Some(storage)) => Self::new_with_storage_identity(store, storage, limits),
            (None, None) => {
                Self::build_source(None, limits, None, SnapshotStartCacheLimits::default())
            }
            _ => unreachable!("validated optional storage pair"),
        }
    }

    pub fn new_with_storage_identity_and_cache(
        store: Arc<dyn ImmutableObjectStore>,
        expected_storage: StorageDescriptor,
        limits: SnapshotLoadLimits,
        start_cache_limits: SnapshotStartCacheLimits,
    ) -> Result<Arc<Self>, SnapshotLoadError> {
        Self::build_with_storage_identity(store, expected_storage, limits, start_cache_limits)
    }

    fn build_with_storage_identity(
        store: Arc<dyn ImmutableObjectStore>,
        expected_storage: StorageDescriptor,
        limits: SnapshotLoadLimits,
        start_cache_limits: SnapshotStartCacheLimits,
    ) -> Result<Arc<Self>, SnapshotLoadError> {
        if expected_storage.endpoint_profile.is_empty()
            || expected_storage.credential_ref.is_empty()
        {
            return Err(SnapshotLoadError::IdentityConflict(
                "configured storage identity conflicts with object store".into(),
            ));
        }
        Self::build(store, limits, Some(expected_storage), start_cache_limits)
    }

    fn build(
        store: Arc<dyn ImmutableObjectStore>,
        limits: SnapshotLoadLimits,
        expected_storage: Option<StorageDescriptor>,
        start_cache_limits: SnapshotStartCacheLimits,
    ) -> Result<Arc<Self>, SnapshotLoadError> {
        Self::build_source(Some(store), limits, expected_storage, start_cache_limits)
    }

    /// Dispatch an opened layer without a node object store. Registry formats
    /// may still open their own digest-pinned metadata transport; object-store
    /// snapshot metadata remains unavailable in this mode.
    pub fn data_only() -> Arc<Self> {
        Self::build_source(
            None,
            SnapshotLoadLimits::default(),
            None,
            SnapshotStartCacheLimits::default(),
        )
        .expect("default snapshot DATA reader limits are valid")
    }

    fn metadata_store(&self) -> Result<&dyn ImmutableObjectStore, SnapshotLoadError> {
        self.store.as_deref().ok_or_else(|| {
            SnapshotLoadError::MissingObject(
                "snapshot metadata transport is not configured for this DATA-only reader".into(),
            )
        })
    }

    fn build_source(
        store: Option<Arc<dyn ImmutableObjectStore>>,
        limits: SnapshotLoadLimits,
        expected_storage: Option<StorageDescriptor>,
        start_cache_limits: SnapshotStartCacheLimits,
    ) -> Result<Arc<Self>, SnapshotLoadError> {
        let start_cache_limits = start_cache_limits.validate()?;
        Ok(Arc::new(Self {
            store,
            range_reader: RangeIO::for_fixed_targets(),
            limits: limits.validate()?,
            expected_storage,
            start_cache_limits,
            start_cache: Mutex::new(StartCacheState::default()),
            start_load_permits: Arc::new(Semaphore::new(start_cache_limits.max_inflight)),
            source_resolver: Arc::new(
                crate::snapshot::format::overlaybd_oci::OverlaybdOciSourceResolver::default(),
            ),
        }))
    }

    /// Strictly load and verify a remote snapshot graph, including eager HEAD
    /// validation of every referenced DATA pack. Recovery and audit paths use
    /// this mode when they must prove that the complete graph is present.
    pub async fn load(
        &self,
        spec_value: &Value,
        spec_checksum: &str,
        snapshot_id: &str,
    ) -> Result<Arc<dyn SnapshotInstance>, SnapshotLoadError> {
        self.load_inner(spec_value, spec_checksum, snapshot_id, true)
            .await
    }

    /// Load the metadata required to start a sandbox without eagerly touching
    /// DATA packs. Pack bytes and provider identity are verified lazily by
    /// RemoteLoad when the VMM first accesses the corresponding range.
    pub async fn load_for_start(
        &self,
        spec_value: &Value,
        spec_checksum: &str,
        snapshot_id: &str,
    ) -> Result<Arc<dyn SnapshotInstance>, SnapshotLoadError> {
        self.load_inner(spec_value, spec_checksum, snapshot_id, false)
            .await
    }

    /// Load start metadata through the node-local bounded cache. Every call
    /// still validates the caller-provided canonical spec and storage/layout
    /// binding before a cached graph can be returned.
    pub async fn load_for_start_shared(
        self: &Arc<Self>,
        spec_value: &Value,
        spec_checksum: &str,
        snapshot_id: &str,
    ) -> Result<Arc<dyn SnapshotInstance>, SnapshotLoadError> {
        let load_epoch = self.start_load_epoch(snapshot_id).await;
        let validated = self.validate_requested_spec(spec_value, spec_checksum, snapshot_id)?;
        if load_epoch.is_invalidated() {
            return Err(SnapshotLoadError::Invalidated(snapshot_id.to_owned()));
        }
        let expected_spec = validated.spec.clone();
        let key = StartCacheKey {
            snapshot_id: snapshot_id.to_owned(),
            spec_checksum: spec_checksum.to_owned(),
        };
        let (attempt, producer_permit) = {
            let mut reserved_permit: Option<OwnedSemaphorePermit> = None;
            loop {
                let mut state = self.start_cache.lock().await;
                if load_epoch.is_invalidated() {
                    return Err(SnapshotLoadError::Invalidated(snapshot_id.to_owned()));
                }
                state.clock = state.clock.saturating_add(1);
                let now = state.clock;
                if let Some(entry) = state.entries.get_mut(&key) {
                    if entry.metadata.spec() != Some(&expected_spec) {
                        return Err(SnapshotLoadError::IdentityConflict(
                            "cached snapshot spec conflicts with validated request".into(),
                        ));
                    }
                    entry.last_used = now;
                    return Ok(Arc::clone(&entry.metadata));
                }
                if let Some(inflight) = state.inflight.get(&key) {
                    break (Arc::clone(inflight), None);
                }
                let permit = match reserved_permit.take() {
                    Some(permit) => permit,
                    None => match Arc::clone(&self.start_load_permits).try_acquire_owned() {
                        Ok(permit) => permit,
                        Err(_) => {
                            drop(state);
                            reserved_permit = Some(
                                load_epoch
                                    .acquire_permit(
                                        Arc::clone(&self.start_load_permits),
                                        snapshot_id,
                                    )
                                    .await?,
                            );
                            continue;
                        }
                    },
                };
                let inflight = Arc::new(InFlightStartLoad {
                    invalidated: AtomicBool::new(false),
                    result: OnceCell::new(),
                    completion: Notify::new(),
                });
                state.inflight.insert(key.clone(), Arc::clone(&inflight));
                break (inflight, Some(permit));
            }
        };

        if let Some(producer_permit) = producer_permit {
            let loader = Arc::clone(self);
            let attempt = Arc::clone(&attempt);
            let key = key.clone();
            let snapshot_id = snapshot_id.to_owned();
            tokio::spawn(async move {
                let _producer_permit = producer_permit;
                let mut completion_guard = StartProducerCompletionGuard::new(Arc::clone(&attempt));
                let worker_loader = Arc::clone(&loader);
                let worker_snapshot_id = snapshot_id.clone();
                let producer = tokio::spawn(async move {
                    tokio::time::timeout(
                        worker_loader.start_cache_limits.producer_timeout,
                        worker_loader.load_validated(validated, &worker_snapshot_id, false),
                    )
                    .await
                    .map_err(|_| {
                        SnapshotLoadError::Store(ObjectStoreError::new(
                            crate::snapshot::transport::PublishFailureKind::Retryable,
                            "snapshot metadata loader timed out",
                        ))
                    })?
                    .map(|loaded| SharedStartLoad {
                        metadata: loaded.metadata,
                        retained_bytes: loaded.retained_bytes,
                    })
                });
                let result = producer.await.unwrap_or_else(|_| {
                    Err(SnapshotLoadError::Store(ObjectStoreError::new(
                        crate::snapshot::transport::PublishFailureKind::Fatal,
                        "snapshot metadata loader task failed",
                    )))
                });
                loader.complete_start_load(&key, &attempt, result).await;
                completion_guard.disarm();
            });
        }
        let result = attempt.wait().await;
        if result.is_err() {
            self.remove_failed_start_load(&key, &attempt).await;
        }
        let loaded = result?;
        if loaded.metadata.spec() != Some(&expected_spec) {
            return Err(SnapshotLoadError::IdentityConflict(
                "singleflight snapshot spec conflicts with validated request".into(),
            ));
        }
        Ok(loaded.metadata)
    }

    async fn remove_failed_start_load(
        &self,
        key: &StartCacheKey,
        attempt: &Arc<InFlightStartLoad>,
    ) {
        let mut state = self.start_cache.lock().await;
        if state
            .inflight
            .get(key)
            .is_some_and(|current| Arc::ptr_eq(current, attempt))
        {
            state.inflight.remove(key);
        }
    }

    async fn start_load_epoch(&self, snapshot_id: &str) -> Arc<SnapshotLoadEpoch> {
        let mut state = self.start_cache.lock().await;
        if let Some(epoch) = state.load_epochs.get(snapshot_id).and_then(Weak::upgrade) {
            return epoch;
        }
        if state.load_epochs.len() >= self.start_cache_limits.max_entries.saturating_mul(4) {
            state
                .load_epochs
                .retain(|_, epoch| epoch.strong_count() > 0);
        }
        let epoch = SnapshotLoadEpoch::new();
        state
            .load_epochs
            .insert(snapshot_id.to_owned(), Arc::downgrade(&epoch));
        epoch
    }

    /// Drop cached metadata for an invalidated snapshot and detach/poison its
    /// in-flight loads so new callers cannot join an obsolete graph.
    pub async fn invalidate_start_cache(&self, snapshot_id: &str) {
        let mut state = self.start_cache.lock().await;
        if let Some(epoch) = state
            .load_epochs
            .remove(snapshot_id)
            .and_then(|epoch| epoch.upgrade())
        {
            epoch.invalidate();
        }
        let keys = state
            .entries
            .keys()
            .filter(|key| key.snapshot_id == snapshot_id)
            .cloned()
            .collect::<Vec<_>>();
        for key in keys {
            if let Some(entry) = state.entries.remove(&key) {
                state.retained_bytes = state.retained_bytes.saturating_sub(entry.retained_bytes);
            }
        }
        let inflight_keys = state
            .inflight
            .keys()
            .filter(|key| key.snapshot_id == snapshot_id)
            .cloned()
            .collect::<Vec<_>>();
        for key in inflight_keys {
            if let Some(attempt) = state.inflight.remove(&key) {
                attempt.invalidated.store(true, Ordering::Release);
                attempt.finish(Err(SnapshotLoadError::Invalidated(snapshot_id.to_owned())));
            }
        }
    }

    async fn complete_start_load(
        &self,
        key: &StartCacheKey,
        attempt: &Arc<InFlightStartLoad>,
        result: Result<SharedStartLoad, SnapshotLoadError>,
    ) {
        let mut state = self.start_cache.lock().await;
        if state
            .inflight
            .get(key)
            .is_some_and(|current| Arc::ptr_eq(current, attempt))
        {
            state.inflight.remove(key);
        }
        if attempt.invalidated.load(Ordering::Acquire) {
            attempt.finish(Err(SnapshotLoadError::Invalidated(key.snapshot_id.clone())));
            return;
        }
        if let Ok(loaded) = &result {
            if loaded.retained_bytes <= self.start_cache_limits.max_bytes
                && !state.entries.contains_key(key)
            {
                while !state.entries.is_empty()
                    && (state.entries.len() >= self.start_cache_limits.max_entries
                        || state.retained_bytes.saturating_add(loaded.retained_bytes)
                            > self.start_cache_limits.max_bytes)
                {
                    let evict = state
                        .entries
                        .iter()
                        .min_by_key(|(_, entry)| entry.last_used)
                        .map(|(key, _)| key.clone())
                        .expect("non-empty cache has an LRU entry");
                    if let Some(entry) = state.entries.remove(&evict) {
                        state.retained_bytes =
                            state.retained_bytes.saturating_sub(entry.retained_bytes);
                    }
                }
                state.clock = state.clock.saturating_add(1);
                let last_used = state.clock;
                state.retained_bytes = state.retained_bytes.saturating_add(loaded.retained_bytes);
                state.entries.insert(
                    key.clone(),
                    StartCacheEntry {
                        metadata: Arc::clone(&loaded.metadata),
                        retained_bytes: loaded.retained_bytes,
                        last_used,
                    },
                );
            }
        }

        // Publication and cache mutation share the same critical section as
        // invalidation. An invalidation can therefore linearize either before
        // this result (and poison it) or after it (and evict it), never in the
        // middle after the cache commit but before waking the waiter.

        attempt.finish(result);
    }

    async fn load_inner(
        &self,
        spec_value: &Value,
        spec_checksum: &str,
        snapshot_id: &str,
        verify_data_objects: bool,
    ) -> Result<Arc<dyn SnapshotInstance>, SnapshotLoadError> {
        let validated = self.validate_requested_spec(spec_value, spec_checksum, snapshot_id)?;
        Ok(self
            .load_validated(validated, snapshot_id, verify_data_objects)
            .await?
            .metadata)
    }

    fn validate_requested_spec(
        &self,
        spec_value: &Value,
        spec_checksum: &str,
        snapshot_id: &str,
    ) -> Result<ValidatedSnapshotSpec, SnapshotLoadError> {
        // Registry metadata supplies a source-scoped store after its complete
        // spec, checksum and immutable root have been validated below. Preserve
        // the early unconfigured-store error for ordinary object-store inputs.
        if spec_value
            .get("storage")
            .and_then(|storage| storage.get("provider"))
            .and_then(Value::as_str)
            != Some("Registry")
        {
            self.metadata_store()?;
        }
        let spec_payload = canonical_json_bytes(spec_value)
            .map_err(|error| SnapshotLoadError::InvalidSnapshotMetadata(error.to_string()))?;
        if spec_payload.len() > self.limits.max_spec_bytes {
            return Err(SnapshotLoadError::BoundExceeded("snapshot spec".into()));
        }
        if snapshot_spec_checksum(spec_value)
            .map_err(|error| SnapshotLoadError::InvalidSnapshotMetadata(error.to_string()))?
            != spec_checksum
        {
            return Err(SnapshotLoadError::IdentityConflict(
                "snapshot spec checksum mismatch".into(),
            ));
        }
        let spec: SnapshotSpec = parse_canonical_model(&spec_payload, "snapshot spec")
            .map_err(|error| SnapshotLoadError::InvalidSnapshotMetadata(error.to_string()))?;
        crate::snapshot::format::snapshot_metadata_format(&spec).validate_spec_layout(
            &spec,
            snapshot_id,
            self.expected_storage.as_ref(),
        )?;

        Ok(ValidatedSnapshotSpec {
            spec,
            canonical_bytes: spec_payload.len(),
        })
    }

    fn metadata_context(&self) -> SnapshotMetadataReadContext<'_> {
        SnapshotMetadataReadContext {
            store: self.store.as_deref(),
            range_reader: self.range_reader.as_ref(),
            limits: self.limits,
            source_resolver: self.source_resolver.as_ref(),
        }
    }

    async fn load_validated(
        &self,
        validated: ValidatedSnapshotSpec,
        snapshot_id: &str,
        verify_data_objects: bool,
    ) -> Result<LoadedSnapshotWithSize, SnapshotLoadError> {
        let decoded = crate::snapshot::format::snapshot_metadata_format(&validated.spec)
            .load(
                &self.metadata_context(),
                validated.spec,
                snapshot_id,
                verify_data_objects,
            )
            .await?;
        Ok(Self::with_retained_size(
            decoded.metadata,
            validated.canonical_bytes,
            decoded.metadata_bytes,
        ))
    }

    fn with_retained_size(
        metadata: Arc<dyn SnapshotInstance>,
        spec_bytes: usize,
        metadata_bytes: usize,
    ) -> LoadedSnapshotWithSize {
        // Count native registry and decoded ZFile indexes explicitly: their
        // retained metadata belongs to the active graph, separately
        // from the authenticated wire metadata used during loading.
        let native_bytes = [LayerKind::Block, LayerKind::Memory]
            .into_iter()
            .filter_map(|kind| metadata.chain_view(kind))
            .flat_map(|chain| &chain.layers)
            .fold(0_usize, |bytes, layer| {
                let map = &layer.range_map;
                let mut retained = map.zfile.as_ref().map_or(0, |index| index.retained_bytes());
                if let Some(native) = &map.registry {
                    // This IR is derived from compact native extents, so its
                    // allocation cannot be bounded by the wire-size multiplier.
                    retained = retained
                        .saturating_add(native.retained_bytes())
                        .saturating_add(size_of::<LayerRangeMap>())
                        .saturating_add(map.content_sha256.capacity())
                        .saturating_add(
                            map.ranges
                                .capacity()
                                .saturating_mul(size_of::<crate::snapshot::metadata::LayerRange>()),
                        );
                    for source in map.ranges.iter().filter_map(|range| range.source.as_ref()) {
                        retained = retained
                            .saturating_add(source.object_key.capacity())
                            .saturating_add(source.object_sha256.capacity())
                            .saturating_add(
                                source.object_version.as_ref().map_or(0, String::capacity),
                            )
                            .saturating_add(source.checksum_sha256.capacity());
                    }
                }
                bytes.saturating_add(retained)
            });
        // Other parsed strings/vectors retain more heap than their canonical
        // wire representation. Keep the existing conservative wire allowance
        // in addition to the exact new index allocation above.
        let retained_bytes = std::mem::size_of_val(metadata.as_ref())
            .saturating_add(spec_bytes.saturating_add(metadata_bytes).saturating_mul(4))
            .saturating_add(native_bytes);
        LoadedSnapshotWithSize {
            metadata,
            retained_bytes,
        }
    }
}

pub(crate) fn source_load_error(error: SourceError) -> SnapshotLoadError {
    if error.is_integrity() {
        SnapshotLoadError::IdentityConflict(error.to_string())
    } else if error.retryable() {
        SnapshotLoadError::Store(ObjectStoreError::new(
            crate::snapshot::transport::PublishFailureKind::Retryable,
            error.to_string(),
        ))
    } else {
        SnapshotLoadError::InvalidSnapshotMetadata(error.to_string())
    }
}

/// Snapshot capabilities used by restoration, demand loading and prefetch.
/// One opened snapshot may aggregate layers with different format codecs.
#[async_trait]
pub trait SnapshotReader: std::fmt::Debug + Send + Sync {
    /// Resolve and verify an OCI FULL snapshot without cold-boot conversion.
    /// Implementations without Registry support reject this capability.
    async fn import_oci_snapshot(
        self: Arc<Self>,
        _oci_ref: &str,
    ) -> Result<Arc<dyn SnapshotInstance>, SnapshotLoadError> {
        Err(SnapshotLoadError::InvalidSnapshotMetadata(
            "OCI snapshot import is not configured".into(),
        ))
    }

    fn open_layer(
        &self,
        map: Arc<LayerRangeMap>,
    ) -> Arc<dyn crate::snapshot::format::LayerViewInterface>;
    async fn load(
        &self,
        spec: &Value,
        checksum: &str,
        snapshot_id: &str,
    ) -> Result<Arc<dyn SnapshotInstance>, SnapshotLoadError>;
    async fn load_for_start(
        &self,
        spec: &Value,
        checksum: &str,
        snapshot_id: &str,
    ) -> Result<Arc<dyn SnapshotInstance>, SnapshotLoadError>;
    async fn load_for_start_shared(
        self: Arc<Self>,
        spec: &Value,
        checksum: &str,
        snapshot_id: &str,
    ) -> Result<Arc<dyn SnapshotInstance>, SnapshotLoadError>;
    async fn invalidate_start_cache(&self, snapshot_id: &str);
}

#[async_trait]
impl SnapshotReader for SnapshotIo {
    async fn import_oci_snapshot(
        self: Arc<Self>,
        oci_ref: &str,
    ) -> Result<Arc<dyn SnapshotInstance>, SnapshotLoadError> {
        // Bootstrap configs are bounded and use the same admission budget as
        // metadata loads. Release before the shared graph loader admits its own
        // producer, so concurrent imports cannot exhaust the pool recursively.
        let bootstrap = self.start_load_permits.acquire().await.map_err(|_| {
            SnapshotLoadError::InvalidSnapshotMetadata("snapshot metadata admission closed".into())
        })?;
        let resolved = crate::snapshot::format::overlaybd_oci::resolve_full_snapshot_metadata(
            self.source_resolver.as_ref(),
            oci_ref,
            self.limits.max_total_metadata_bytes,
        )
        .await
        .map_err(source_load_error);
        drop(bootstrap);
        let (snapshot_id, spec) = resolved?;
        let value = serde_json::to_value(&spec)
            .map_err(|error| SnapshotLoadError::InvalidSnapshotMetadata(error.to_string()))?;
        let checksum = snapshot_spec_checksum(&value)
            .map_err(|error| SnapshotLoadError::InvalidSnapshotMetadata(error.to_string()))?;
        let snapshot = self
            .load_for_start_shared(&value, &checksum, &snapshot_id)
            .await?;
        if snapshot.snapshot_kind() != crate::snapshot::SnapshotKind::Full
            || snapshot.vm_state().is_none()
        {
            return Err(SnapshotLoadError::InvalidSnapshotMetadata(
                "OCI resume requires a FULL memory and VMM-state snapshot".into(),
            ));
        }
        Ok(snapshot)
    }

    fn open_layer(
        &self,
        map: Arc<LayerRangeMap>,
    ) -> Arc<dyn crate::snapshot::format::LayerViewInterface> {
        crate::snapshot::format::reader::LayerReadCapability::new(map)
    }
    async fn load(
        &self,
        spec: &Value,
        checksum: &str,
        snapshot_id: &str,
    ) -> Result<Arc<dyn SnapshotInstance>, SnapshotLoadError> {
        SnapshotIo::load(self, spec, checksum, snapshot_id).await
    }
    async fn load_for_start(
        &self,
        spec: &Value,
        checksum: &str,
        snapshot_id: &str,
    ) -> Result<Arc<dyn SnapshotInstance>, SnapshotLoadError> {
        SnapshotIo::load_for_start(self, spec, checksum, snapshot_id).await
    }
    async fn load_for_start_shared(
        self: Arc<Self>,
        spec: &Value,
        checksum: &str,
        snapshot_id: &str,
    ) -> Result<Arc<dyn SnapshotInstance>, SnapshotLoadError> {
        SnapshotIo::load_for_start_shared(&self, spec, checksum, snapshot_id).await
    }
    async fn invalidate_start_cache(&self, snapshot_id: &str) {
        SnapshotIo::invalidate_start_cache(self, snapshot_id).await;
    }
}

/// Publication keeps the selected write domain, immutable identities and
/// manifest-last commit ordering. A reader need not implement this capability.
#[async_trait]
pub trait SnapshotWriter: std::fmt::Debug + Send + Sync {
    fn prepare_upload(
        &self,
        source: &dyn SnapshotUploadSource,
        storage: StorageDescriptor,
        compatibility: crate::snapshot::metadata::VmmCompatibility,
        scratch: &dyn Fn(
            u64,
        ) -> Result<
            Arc<dyn crate::snapshot::transport::StagedObjectWriter>,
            SnapshotUploadError,
        >,
    ) -> Result<SnapshotUploadPlan, SnapshotUploadError>;
    async fn publish(
        self: Arc<Self>,
        plan: SnapshotUploadPlan,
        metadata_target: SnapshotMetadataWriteTarget,
        data_target: DataPackWriteTarget,
    ) -> Result<UploadedSnapshot, SnapshotUploadError>;
    async fn close(&self);
}

#[async_trait]
impl SnapshotWriter for SnapshotPublisher {
    fn prepare_upload(
        &self,
        source: &dyn SnapshotUploadSource,
        storage: StorageDescriptor,
        compatibility: crate::snapshot::metadata::VmmCompatibility,
        scratch: &dyn Fn(
            u64,
        ) -> Result<
            Arc<dyn crate::snapshot::transport::StagedObjectWriter>,
            SnapshotUploadError,
        >,
    ) -> Result<SnapshotUploadPlan, SnapshotUploadError> {
        self.format
            .prepare_upload(source, storage, compatibility, scratch)
    }
    async fn publish(
        self: Arc<Self>,
        plan: SnapshotUploadPlan,
        metadata_target: SnapshotMetadataWriteTarget,
        data_target: DataPackWriteTarget,
    ) -> Result<UploadedSnapshot, SnapshotUploadError> {
        SnapshotPublisher::publish(&self, plan, metadata_target, data_target).await
    }
    async fn close(&self) {
        SnapshotPublisher::close(self).await;
    }
}

// One snapshot publication and manifest-last commit.

#[derive(Debug)]
struct PublicationAttempt<T: Clone> {
    result: StdMutex<Option<Result<T, SnapshotUploadError>>>,
    notify: Notify,
}

impl<T: Clone> PublicationAttempt<T> {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            result: StdMutex::new(None),
            notify: Notify::new(),
        })
    }

    fn result(&self) -> Option<Result<T, SnapshotUploadError>> {
        self.result.lock().expect("attempt lock poisoned").clone()
    }

    fn finish(&self, result: Result<T, SnapshotUploadError>) {
        *self.result.lock().expect("attempt lock poisoned") = Some(result);
        self.notify.notify_waiters();
    }

    async fn wait(&self) -> Result<T, SnapshotUploadError> {
        loop {
            let notified = self.notify.notified();
            if let Some(result) = self.result() {
                return result;
            }
            notified.await;
        }
    }
}

#[derive(Debug, Default)]
struct PublisherState {
    snapshot_identities: HashMap<(String, String), String>,
    snapshots: HashMap<(String, String), Arc<PublicationAttempt<UploadedSnapshot>>>,
    layers: HashMap<String, Arc<PublicationAttempt<Vec<DataPackUploadReceipt>>>>,
    closed: bool,
}

#[derive(Debug)]
pub struct SnapshotPublisher {
    pub(super) data_service: Arc<SnapshotDataUploadService>,
    pub(super) range_writer: Arc<dyn RangeWriter>,
    state: Mutex<PublisherState>,
    pub(super) source_resolver: Arc<dyn SourceResolver>,
    pub(super) format: Arc<dyn SnapshotUploadInterface>,
    registry_publication: Option<Arc<super::oci_publication::RegistrySnapshotPublication>>,
}

impl SnapshotPublisher {
    pub(crate) fn with_registry_publication(
        mut self: Arc<Self>,
        publication: Arc<super::oci_publication::RegistrySnapshotPublication>,
    ) -> Arc<Self> {
        let publisher = Arc::get_mut(&mut self)
            .expect("Registry publication is configured before sharing the uploader");
        publisher.format =
            Arc::new(crate::snapshot::format::overlaybd_oci::publication::OverlaybdOciPublication);
        publisher.registry_publication = Some(publication);
        self
    }

    pub fn with_format(mut self: Arc<Self>, format: Arc<dyn SnapshotUploadInterface>) -> Arc<Self> {
        Arc::get_mut(&mut self)
            .expect("snapshot write format is configured before sharing the uploader")
            .format = format;
        self
    }

    pub(crate) fn with_source_resolver(
        mut self: Arc<Self>,
        source_resolver: Arc<dyn SourceResolver>,
    ) -> Arc<Self> {
        Arc::get_mut(&mut self)
            .expect("source resolver is configured before sharing the uploader")
            .source_resolver = source_resolver;
        self
    }

    pub fn new(
        config: SnapshotDataUploadConfig,
        metrics: Option<Arc<crate::observability::metrics::SnapdMetrics>>,
    ) -> Arc<Self> {
        Self::new_with_config(config, metrics).expect("default DATA upload configuration is valid")
    }

    pub fn new_with_config(
        config: SnapshotDataUploadConfig,
        metrics: Option<Arc<crate::observability::metrics::SnapdMetrics>>,
    ) -> Result<Arc<Self>, SnapshotUploadError> {
        Self::new_with_config_and_range_writer(config, metrics, RangeIO::for_fixed_targets())
    }

    pub fn new_with_config_and_range_writer(
        config: SnapshotDataUploadConfig,
        metrics: Option<Arc<crate::observability::metrics::SnapdMetrics>>,
        range_writer: Arc<dyn RangeWriter>,
    ) -> Result<Arc<Self>, SnapshotUploadError> {
        let data_service = SnapshotDataUploadService::new_with_range_writer(
            config,
            metrics,
            Arc::clone(&range_writer),
        )
        .map_err(data_error)?;
        Ok(Arc::new(Self {
            data_service,
            range_writer,
            state: Mutex::new(PublisherState::default()),
            source_resolver: Arc::new(
                crate::snapshot::format::overlaybd_oci::OverlaybdOciSourceResolver::default(),
            ),
            format: Arc::new(OverlaybdS3Publication),
            registry_publication: None,
        }))
    }

    pub async fn publish(
        self: &Arc<Self>,
        plan: SnapshotUploadPlan,
        metadata_target: SnapshotMetadataWriteTarget,
        data_target: DataPackWriteTarget,
    ) -> Result<UploadedSnapshot, SnapshotUploadError> {
        if metadata_target.namespace() != data_target.namespace()
            || metadata_target.write_target_identity() != data_target.write_target_identity()
            || !Arc::ptr_eq(&metadata_target.fence(), &data_target.fence())
        {
            return Err(SnapshotUploadError::IdentityConflict(
                "metadata and DATA targets do not share one canonical write domain".into(),
            ));
        }
        self.format.validate_prepared(&plan)?;
        let identity = plan.identity()?;
        let target_identity = metadata_target.write_target_identity().to_owned();
        let snapshot_key = (target_identity, plan.snapshot_id.clone());
        let attempt = {
            let mut state = self.state.lock().await;
            if state.closed {
                return Err(SnapshotUploadError::Closed);
            }
            if let Some(existing) = state.snapshot_identities.get(&snapshot_key) {
                if existing != &identity {
                    return Err(SnapshotUploadError::IdentityConflict(
                        "snapshot ID was reused with different artifacts".into(),
                    ));
                }
            } else {
                state
                    .snapshot_identities
                    .insert(snapshot_key.clone(), identity);
            }
            match state.snapshots.get(&snapshot_key) {
                Some(existing) if !matches!(existing.result(), Some(Err(_))) => {
                    Arc::clone(existing)
                }
                _ => {
                    let attempt = PublicationAttempt::new();
                    state.snapshots.insert(snapshot_key, Arc::clone(&attempt));
                    let publisher = Arc::clone(self);
                    let worker = tokio::spawn(async move {
                        publisher
                            .run_publish(plan, metadata_target, data_target)
                            .await
                    });
                    let worker_attempt = Arc::clone(&attempt);
                    tokio::spawn(async move {
                        let result = worker.await.unwrap_or_else(|error| {
                            Err(SnapshotUploadError::Worker(error.to_string()))
                        });
                        worker_attempt.finish(result);
                    });
                    attempt
                }
            }
        };
        attempt.wait().await
    }

    pub async fn close(&self) {
        let attempts = {
            let mut state = self.state.lock().await;
            if state.closed {
                return;
            }
            state.closed = true;
            state.snapshots.values().cloned().collect::<Vec<_>>()
        };
        // DATA attempts can be blocked in a provider or multipart request.  Close the
        // data plane first so its cancellation token wakes those owners; waiting for
        // the enclosing snapshot attempts before doing this would invert shutdown and
        // make daemon termination depend on the normal remote I/O timeout.
        self.data_service.close().await;
        for attempt in attempts {
            let _ = attempt.wait().await;
        }
    }

    async fn run_publish(
        self: &Arc<Self>,
        plan: SnapshotUploadPlan,
        metadata_target: SnapshotMetadataWriteTarget,
        data_target: DataPackWriteTarget,
    ) -> Result<UploadedSnapshot, SnapshotUploadError> {
        if let Some(registry) = &self.registry_publication {
            return registry
                .publish(self, plan, metadata_target, data_target)
                .await;
        }
        let lease = UploadTransactionLease::acquire(metadata_target.fence()).await;
        let all_data_requests = plan
            .layers
            .iter()
            .flat_map(|layer| layer.data_requests().iter().cloned())
            .collect::<Vec<_>>();
        self.data_service
            .scan_packs(&all_data_requests)
            .await
            .map_err(data_error)?;
        let mut data_receipts = Vec::with_capacity(all_data_requests.len());
        for layer in &plan.layers {
            data_receipts.extend(
                self.publish_layer(
                    layer.clone(),
                    metadata_target.clone(),
                    data_target.clone(),
                    lease.clone(),
                )
                .await?,
            );
        }
        for object in &plan.chain_indexes {
            self.ensure(&metadata_target, object, &lease).await?;
        }
        if let Some(vm_state) = &plan.vm_state {
            self.ensure(&metadata_target, vm_state, &lease).await?;
        }
        let manifest = self
            .format
            .validate_uploaded_artifacts(
                &plan,
                &data_receipts,
                &SnapshotPublicationContext::new(
                    &metadata_target,
                    &lease,
                    self.source_resolver.as_ref(),
                    self.range_writer.as_ref(),
                ),
            )
            .await?;
        self.ensure(&metadata_target, self.format.root_metadata(&plan)?, &lease)
            .await?;
        let published = UploadedSnapshot {
            registry_objects: None,
            spec: plan.spec,
            manifest,
        };
        self.format.validate_published(&published)?;
        // Only the committed remote root makes locally sealed DATA disposable.
        // Failed attempts keep their original sealed sources pinned for replay.
        for request in &all_data_requests {
            request.source.mark_remote_committed();
        }
        Ok(published)
    }

    pub(super) async fn publish_layer(
        self: &Arc<Self>,
        layer: LayerUploadPlan,
        metadata_target: SnapshotMetadataWriteTarget,
        data_target: DataPackWriteTarget,
        lease: UploadTransactionLease,
    ) -> Result<Vec<DataPackUploadReceipt>, SnapshotUploadError> {
        let identity = format!(
            "{}:{}",
            metadata_target.write_target_identity(),
            layer.identity(metadata_target.namespace())
        );
        let attempt = {
            let mut state = self.state.lock().await;
            match state.layers.get(&identity) {
                Some(existing) if !matches!(existing.result(), Some(Err(_))) => {
                    Arc::clone(existing)
                }
                _ => {
                    let attempt = PublicationAttempt::new();
                    state.layers.insert(identity, Arc::clone(&attempt));
                    let publisher = Arc::clone(self);
                    let worker = tokio::spawn(async move {
                        publisher
                            .run_layer(layer, metadata_target, data_target, lease)
                            .await
                    });
                    let worker_attempt = Arc::clone(&attempt);
                    tokio::spawn(async move {
                        let result = worker.await.unwrap_or_else(|error| {
                            Err(SnapshotUploadError::Worker(error.to_string()))
                        });
                        worker_attempt.finish(result);
                    });
                    attempt
                }
            }
        };
        attempt.wait().await
    }

    async fn run_layer(
        &self,
        layer: LayerUploadPlan,
        metadata_target: SnapshotMetadataWriteTarget,
        data_target: DataPackWriteTarget,
        lease: UploadTransactionLease,
    ) -> Result<Vec<DataPackUploadReceipt>, SnapshotUploadError> {
        layer.validate()?;
        let receipts = self
            .data_service
            .ensure_prevalidated_packs(data_target, layer.data_requests().to_vec(), lease.clone())
            .await
            .map_err(data_error)?;
        self.ensure(&metadata_target, &layer.range_map, &lease)
            .await?;
        self.ensure(&metadata_target, &layer.committed, &lease)
            .await?;
        Ok(receipts)
    }

    pub(super) async fn ensure(
        &self,
        target: &SnapshotMetadataWriteTarget,
        object: &ImmutableObject,
        lease: &UploadTransactionLease,
    ) -> Result<(), SnapshotUploadError> {
        self.range_writer
            .ensure_metadata_verified(
                target,
                &object.object_key,
                &object.payload,
                &object.sha256,
                lease,
            )
            .await
            .map_err(Into::into)
    }
}

pub(super) fn data_error(error: SnapshotDataUploadError) -> SnapshotUploadError {
    match error {
        SnapshotDataUploadError::IdentityConflict(message) => {
            SnapshotUploadError::IdentityConflict(message)
        }
        SnapshotDataUploadError::Invalid(message) => SnapshotUploadError::InvalidPlan(message),
        error => SnapshotUploadError::Data {
            retryable: error.retryable(),
            message: error.to_string(),
        },
    }
}

// Per-object validation and bounded streaming transfer.

const MAX_DATA_PACK_BYTES: u64 = 16 * 1024 * 1024;

#[derive(Clone, Debug)]
pub struct SnapshotDataUploadConfig {
    pub max_concurrent_uploads: usize,
    pub max_queued_uploads: usize,
    pub max_bytes_in_flight: u64,
    pub chunk_bytes: usize,
    pub attempt_timeout: Duration,
}

impl Default for SnapshotDataUploadConfig {
    fn default() -> Self {
        Self {
            max_concurrent_uploads: 4,
            max_queued_uploads: 64,
            max_bytes_in_flight: 256 * 1024 * 1024,
            chunk_bytes: 8 * 1024 * 1024,
            attempt_timeout: Duration::from_secs(300),
        }
    }
}

impl SnapshotDataUploadConfig {
    pub fn validate(&self) -> Result<(), SnapshotDataUploadError> {
        if self.max_concurrent_uploads == 0
            || self.max_queued_uploads < self.max_concurrent_uploads
            || self.max_bytes_in_flight < MAX_DATA_PACK_BYTES
            || !(5 * 1024 * 1024..=64 * 1024 * 1024).contains(&self.chunk_bytes)
            || self.max_bytes_in_flight < 2 * self.stream_chunk_bytes() as u64
            || usize::try_from(self.max_bytes_in_flight)
                .ok()
                .is_none_or(|bytes| bytes > Semaphore::MAX_PERMITS)
            || self.attempt_timeout.is_zero()
            || self
                .max_concurrent_uploads
                .checked_mul(self.chunk_bytes)
                .is_none()
        {
            return Err(SnapshotDataUploadError::Invalid(
                "DATA upload resource bounds are invalid".into(),
            ));
        }
        Ok(())
    }

    // Source windows are independent of S3 multipart part sizes. Larger
    // configured parts are streamed as several bounded source windows.
    pub(super) fn stream_chunk_bytes(&self) -> usize {
        self.chunk_bytes.min(MAX_DATA_PACK_BYTES as usize)
    }

    fn reservation_bytes(&self, length: u64) -> u64 {
        // The HTTP body may hand the preceding Bytes to the network
        // while preparing the next source window.
        2 * self.scan_reservation_bytes(length)
    }

    fn scan_reservation_bytes(&self, length: u64) -> u64 {
        length.min(self.stream_chunk_bytes() as u64)
    }
}

#[derive(Debug)]
struct Attempt {
    identity: DataPackIdentity,
    result: StdMutex<Option<Result<DataPackUploadReceipt, SnapshotDataUploadError>>>,
    notify: Notify,
    cancellation: watch::Sender<bool>,
    waiters: AtomicUsize,
}

impl Attempt {
    fn new(identity: DataPackIdentity) -> Arc<Self> {
        let (cancellation, _) = watch::channel(false);
        Arc::new(Self {
            identity,
            result: StdMutex::new(None),
            notify: Notify::new(),
            cancellation,
            waiters: AtomicUsize::new(0),
        })
    }

    fn finish(&self, result: Result<DataPackUploadReceipt, SnapshotDataUploadError>) {
        *self
            .result
            .lock()
            .expect("DATA upload attempt lock poisoned") = Some(result);
        self.notify.notify_waiters();
    }

    fn cancel(&self) {
        let _ = self.cancellation.send(true);
    }

    fn cancellation(&self) -> watch::Receiver<bool> {
        self.cancellation.subscribe()
    }

    async fn wait(self: &Arc<Self>) -> Result<DataPackUploadReceipt, SnapshotDataUploadError> {
        let _waiter = AttemptWaiter::new(Arc::clone(self));
        loop {
            let notified = self.notify.notified();
            if let Some(result) = self
                .result
                .lock()
                .expect("DATA upload attempt lock poisoned")
                .clone()
            {
                return result;
            }
            notified.await;
        }
    }
}

struct AttemptWaiter {
    attempt: Arc<Attempt>,
}

impl AttemptWaiter {
    fn new(attempt: Arc<Attempt>) -> Self {
        attempt.waiters.fetch_add(1, Ordering::AcqRel);
        Self { attempt }
    }
}

impl Drop for AttemptWaiter {
    fn drop(&mut self) {
        if self.attempt.waiters.fetch_sub(1, Ordering::AcqRel) == 1
            && self
                .attempt
                .result
                .lock()
                .expect("DATA upload attempt lock poisoned")
                .is_none()
        {
            self.attempt.cancel();
        }
    }
}

#[derive(Debug, Default)]
struct DataTransferState {
    active: HashMap<(String, String), Arc<Attempt>>,
    bytes_in_flight: u64,
    closed: bool,
}

#[derive(Debug)]
pub(super) struct SnapshotDataUploadService {
    pub(super) config: SnapshotDataUploadConfig,
    range_writer: Arc<dyn RangeWriter>,
    state: Mutex<DataTransferState>,
    pub(super) transfer_permits: Arc<Semaphore>,
    scan_permits: Arc<Semaphore>,
    pub(super) buffer_permits: Arc<Semaphore>,
    pub(super) shutdown: watch::Sender<bool>,
    metrics: Option<Arc<SnapdMetrics>>,
}

impl SnapshotDataUploadService {
    #[cfg(test)]
    fn new(
        config: SnapshotDataUploadConfig,
        metrics: Option<Arc<SnapdMetrics>>,
    ) -> Result<Arc<Self>, SnapshotDataUploadError> {
        Self::new_with_range_writer(config, metrics, RangeIO::for_fixed_targets())
    }

    fn new_with_range_writer(
        config: SnapshotDataUploadConfig,
        metrics: Option<Arc<SnapdMetrics>>,
        range_writer: Arc<dyn RangeWriter>,
    ) -> Result<Arc<Self>, SnapshotDataUploadError> {
        config.validate()?;
        let (shutdown, _) = watch::channel(false);
        Ok(Arc::new(Self {
            transfer_permits: Arc::new(Semaphore::new(config.max_concurrent_uploads)),
            scan_permits: Arc::new(Semaphore::new(config.max_concurrent_uploads)),
            buffer_permits: Arc::new(Semaphore::new(config.max_bytes_in_flight as usize)),
            state: Mutex::new(DataTransferState::default()),
            config,
            range_writer,
            shutdown,
            metrics,
        }))
    }

    pub(super) async fn scan_packs(
        self: &Arc<Self>,
        requests: &[DataPackUploadRequest],
    ) -> Result<(), SnapshotDataUploadError> {
        validate_requests(requests)?;
        // Phase A is a full-batch barrier.  No remote writer is invoked until
        // every local source has been independently scanned and verified.
        for window in requests.chunks(self.config.max_concurrent_uploads) {
            let mut scans = JoinSet::new();
            for request in window.iter().cloned() {
                let service = Arc::clone(self);
                scans.spawn(async move { service.scan(request).await });
            }
            while let Some(result) = scans.join_next().await {
                result.map_err(|error| SnapshotDataUploadError::Worker(error.to_string()))??;
            }
        }
        Ok(())
    }

    async fn ensure_prevalidated_packs(
        self: &Arc<Self>,
        target: DataPackWriteTarget,
        requests: Vec<DataPackUploadRequest>,
        lease: UploadTransactionLease,
    ) -> Result<Vec<DataPackUploadReceipt>, SnapshotDataUploadError> {
        validate_requests(&requests)?;
        let mut receipts = Vec::with_capacity(requests.len());
        let mut window_start = 0_usize;
        while window_start < requests.len() {
            let mut window_end = window_start;
            let mut window_bytes = 0_u64;
            while window_end < requests.len()
                && window_end - window_start < self.config.max_concurrent_uploads
                && window_end - window_start < self.config.max_queued_uploads
            {
                let next = self
                    .config
                    .reservation_bytes(requests[window_end].identity.length);
                if window_end > window_start
                    && window_bytes
                        .checked_add(next)
                        .is_none_or(|total| total > self.config.max_bytes_in_flight)
                {
                    break;
                }
                window_bytes = window_bytes
                    .checked_add(next)
                    .ok_or(SnapshotDataUploadError::Busy)?;
                window_end += 1;
            }
            debug_assert!(window_end > window_start);
            let window = &requests[window_start..window_end];
            let mut uploads = JoinSet::new();
            for request in window.iter().cloned() {
                let service = Arc::clone(self);
                let target = target.clone();
                let lease = lease.clone();
                uploads.spawn(async move { service.ensure_one(target, request, lease).await });
            }
            while let Some(result) = uploads.join_next().await {
                receipts.push(
                    result
                        .map_err(|error| SnapshotDataUploadError::Worker(error.to_string()))??,
                );
            }
            window_start = window_end;
        }
        receipts.sort_by(|left, right| left.object_key.cmp(&right.object_key));
        Ok(receipts)
    }

    async fn scan(&self, request: DataPackUploadRequest) -> Result<(), SnapshotDataUploadError> {
        let _permit = self
            .scan_permits
            .clone()
            .acquire_owned()
            .await
            .map_err(|_| SnapshotDataUploadError::Closed)?;
        let deadline = Instant::now()
            .checked_add(self.config.attempt_timeout)
            .ok_or_else(|| {
                SnapshotDataUploadError::Invalid("DATA scan deadline overflow".into())
            })?;
        let reservation = self
            .config
            .scan_reservation_bytes(request.identity.length)
            .max(request.minimum_scan_buffer_bytes());
        if reservation > self.config.max_bytes_in_flight {
            return Err(SnapshotDataUploadError::Busy);
        }
        let _buffer = self
            .buffer_permits
            .clone()
            .acquire_many_owned(reservation as u32)
            .await
            .map_err(|_| SnapshotDataUploadError::Closed)?;
        request
            .verify_source(
                self.config.stream_chunk_bytes(),
                deadline,
                &self.shutdown.subscribe(),
            )
            .await
    }

    async fn ensure_one(
        self: &Arc<Self>,
        target: DataPackWriteTarget,
        request: DataPackUploadRequest,
        lease: UploadTransactionLease,
    ) -> Result<DataPackUploadReceipt, SnapshotDataUploadError> {
        let key = (
            target.write_target_identity().to_owned(),
            request.identity.object_key.as_str().to_owned(),
        );
        let (attempt, owner) = {
            let mut state = self.state.lock().await;
            if state.closed {
                return Err(SnapshotDataUploadError::Closed);
            }
            if let Some(attempt) = state.active.get(&key) {
                if attempt.identity != request.identity {
                    if let Some(metrics) = &self.metrics {
                        metrics.record_data_upload_rejected(DataUploadOutcome::IdentityConflict);
                    }
                    return Err(SnapshotDataUploadError::IdentityConflict(
                        "in-flight DATA key is bound to another checksum or length".into(),
                    ));
                }
                (Arc::clone(attempt), false)
            } else {
                let next_bytes = state
                    .bytes_in_flight
                    .checked_add(self.config.reservation_bytes(request.identity.length))
                    .ok_or(SnapshotDataUploadError::Busy)?;
                if state.active.len() >= self.config.max_queued_uploads
                    || next_bytes > self.config.max_bytes_in_flight
                {
                    if let Some(metrics) = &self.metrics {
                        metrics.record_data_upload_rejected(DataUploadOutcome::Busy);
                    }
                    return Err(SnapshotDataUploadError::Busy);
                }
                let attempt = Attempt::new(request.identity.clone());
                state.bytes_in_flight = next_bytes;
                state.active.insert(key.clone(), Arc::clone(&attempt));
                (attempt, true)
            }
        };
        if owner {
            let admitted_at = Instant::now();
            let started = Arc::new(AtomicBool::new(false));
            if let Some(metrics) = &self.metrics {
                metrics.data_upload_admitted(request.identity.length);
            }
            let service = Arc::clone(self);
            let owner_attempt = Arc::clone(&attempt);
            let owner_started = Arc::clone(&started);
            let cancellation = owner_attempt.cancellation();
            tokio::spawn(async move {
                let result = service
                    .transfer(
                        target,
                        request,
                        lease,
                        Arc::clone(&owner_started),
                        cancellation,
                    )
                    .await;
                if let Some(metrics) = &service.metrics {
                    metrics.data_upload_finished(
                        owner_attempt.identity.length,
                        owner_started.load(Ordering::Acquire),
                        data_upload_outcome(&result),
                        admitted_at.elapsed(),
                    );
                }
                owner_attempt.finish(result);
                let mut state = service.state.lock().await;
                if state
                    .active
                    .get(&key)
                    .is_some_and(|current| Arc::ptr_eq(current, &owner_attempt))
                {
                    state.active.remove(&key);
                    state.bytes_in_flight = state.bytes_in_flight.saturating_sub(
                        service
                            .config
                            .reservation_bytes(owner_attempt.identity.length),
                    );
                }
            });
        }
        attempt.wait().await
    }

    async fn transfer(
        &self,
        target: DataPackWriteTarget,
        request: DataPackUploadRequest,
        lease: UploadTransactionLease,
        started: Arc<AtomicBool>,
        mut cancellation: watch::Receiver<bool>,
    ) -> Result<DataPackUploadReceipt, SnapshotDataUploadError> {
        if *cancellation.borrow() {
            return Err(SnapshotDataUploadError::Cancelled);
        }
        let _permit = tokio::select! {
            permit = self.transfer_permits.clone().acquire_owned() => {
                permit.map_err(|_| SnapshotDataUploadError::Closed)?
            }
            changed = cancellation.changed() => {
                if changed.is_err() || *cancellation.borrow() {
                    return Err(SnapshotDataUploadError::Cancelled);
                }
                return Err(SnapshotDataUploadError::Worker(
                    "DATA upload cancellation channel changed without cancellation".into(),
                ));
            }
        };
        let _buffer = tokio::select! {
            permit = self.buffer_permits.clone().acquire_many_owned(
                self.config.reservation_bytes(request.identity.length) as u32,
            ) => permit.map_err(|_| SnapshotDataUploadError::Closed)?,
            _ = cancellation.changed() => return Err(SnapshotDataUploadError::Cancelled),
        };
        started.store(true, Ordering::Release);
        if let Some(metrics) = &self.metrics {
            metrics.data_upload_started();
        }
        let deadline = Instant::now()
            .checked_add(self.config.attempt_timeout)
            .ok_or_else(|| {
                SnapshotDataUploadError::Invalid("DATA transfer deadline overflow".into())
            })?;
        let identity = ImmutableObjectIdentity {
            object_key: request.identity.object_key.as_str().to_owned(),
            sha256: request.identity.expected_sha256.clone(),
            length: request.identity.length,
        };
        let source: Arc<dyn ReplayableByteSource> = Arc::new(StorageSource(request.source));
        let receipt = self
            .range_writer
            .ensure_stream_verified(
                &target,
                &identity,
                source,
                lease,
                deadline,
                IoCancellation::new(cancellation),
                self.config.stream_chunk_bytes(),
            )
            .await
            .map_err(map_store_error)?;
        if receipt.object_key != identity.object_key
            || receipt.sha256 != identity.sha256
            || receipt.length != identity.length
        {
            return Err(SnapshotDataUploadError::IdentityConflict(
                "DATA writer returned a receipt for another immutable identity".into(),
            ));
        }
        Ok(DataPackUploadReceipt {
            object_key: receipt.object_key,
            sha256: receipt.sha256,
            length: receipt.length,
            object_version: receipt.object_version,
        })
    }

    async fn close(&self) {
        let attempts = {
            let mut state = self.state.lock().await;
            state.closed = true;
            let _ = self.shutdown.send(true);
            state.active.values().cloned().collect::<Vec<_>>()
        };
        self.transfer_permits.close();
        self.scan_permits.close();
        self.buffer_permits.close();
        for attempt in &attempts {
            attempt.cancel();
        }
        for attempt in attempts {
            let _ = attempt.wait().await;
        }
    }
}

fn validate_requests(requests: &[DataPackUploadRequest]) -> Result<(), SnapshotDataUploadError> {
    let mut unique = HashSet::new();
    for request in requests {
        request.identity.validate()?;
        if request.source.length() != request.identity.length
            || !unique.insert(request.identity.object_key.clone())
        {
            return Err(SnapshotDataUploadError::Invalid(
                "DATA upload requests are duplicate or differ from their source".into(),
            ));
        }
    }
    Ok(())
}

fn data_upload_outcome(
    result: &Result<DataPackUploadReceipt, SnapshotDataUploadError>,
) -> DataUploadOutcome {
    match result {
        Ok(_) => DataUploadOutcome::Success,
        Err(SnapshotDataUploadError::Busy) => DataUploadOutcome::Busy,
        Err(SnapshotDataUploadError::Cancelled | SnapshotDataUploadError::Closed) => {
            DataUploadOutcome::Cancelled
        }
        Err(SnapshotDataUploadError::Timeout) => DataUploadOutcome::Timeout,
        Err(SnapshotDataUploadError::IdentityConflict(_)) => DataUploadOutcome::IdentityConflict,
        Err(SnapshotDataUploadError::Invalid(_)) => DataUploadOutcome::IntegrityError,
        Err(SnapshotDataUploadError::Backend {
            abort_cleanup_unconfirmed: true,
            ..
        }) => DataUploadOutcome::AbortCleanupUnconfirmed,
        Err(SnapshotDataUploadError::Backend { .. } | SnapshotDataUploadError::Worker(_)) => {
            DataUploadOutcome::RetryableError
        }
    }
}

struct StorageSource(Arc<dyn DataPackSource>);

impl ReplayableByteSource for StorageSource {
    fn length(&self) -> u64 {
        self.0.length()
    }

    fn read_at<'a>(
        &'a self,
        offset: u64,
        max_bytes: usize,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<u8>, ObjectStoreError>> + Send + 'a>> {
        Box::pin(async move {
            self.0.read_at(offset, max_bytes).await.map_err(|error| {
                ObjectStoreError::new(PublishFailureKind::Fatal, error.to_string())
            })
        })
    }
}

fn map_store_error(error: ObjectStoreError) -> SnapshotDataUploadError {
    if error.kind == PublishFailureKind::IdentityConflict {
        SnapshotDataUploadError::IdentityConflict(error.message)
    } else {
        SnapshotDataUploadError::Backend {
            retryable: error.retryable(),
            message: error.message,
            abort_cleanup_unconfirmed: error.abort_cleanup_unconfirmed,
        }
    }
}

#[cfg(test)]
mod overlaybd_tests {
    use super::*;
    use std::fs::File;
    use std::sync::atomic::{AtomicU64, AtomicUsize};

    use crate::snapshot::format::overlaybd::{encode_descriptor, plan_layer};
    use crate::snapshot::metadata::{
        compute_layer_content_sha256, LayerRange, LayerRangeMapRef, ObjectRef,
    };
    use crate::snapshot::transport::{NamespaceMutationFence, RemoteObject};
    use crate::snapshot::ValidateCanonicalModel;

    #[derive(Debug)]
    struct MetadataOnlyStore {
        fence: Arc<NamespaceMutationFence>,
        metadata: HashMap<String, Vec<u8>>,
        blob: ObjectRef,
        blob_size: AtomicU64,
        blob_heads: AtomicUsize,
    }

    impl MetadataOnlyStore {
        fn metadata(&self, key: &str, payload: &[u8]) -> RemoteObjectMetadata {
            let _ = key;
            RemoteObjectMetadata {
                size: payload.len() as u64,
                sha256: sha256_identity(payload),
                version: None,
                last_modified_unix: None,
                etag: None,
            }
        }
    }

    #[async_trait::async_trait]
    impl ImmutableObjectStore for MetadataOnlyStore {
        fn namespace(&self) -> &str {
            self.fence.namespace()
        }

        fn fence(&self) -> Arc<NamespaceMutationFence> {
            Arc::clone(&self.fence)
        }

        async fn head(&self, key: &str) -> Result<Option<RemoteObjectMetadata>, ObjectStoreError> {
            if key == self.blob.object_key {
                self.blob_heads.fetch_add(1, Ordering::Relaxed);
                return Ok(Some(RemoteObjectMetadata {
                    size: self.blob_size.load(Ordering::Relaxed),
                    sha256: self.blob.sha256.clone(),
                    version: None,
                    last_modified_unix: None,
                    etag: None,
                }));
            }
            Ok(self
                .metadata
                .get(key)
                .map(|payload| self.metadata(key, payload)))
        }

        async fn put_if_absent(
            &self,
            _key: &str,
            _payload: &[u8],
            _sha256: &str,
        ) -> Result<bool, ObjectStoreError> {
            panic!("metadata loading must not write objects")
        }

        async fn get(&self, key: &str) -> Result<Vec<u8>, ObjectStoreError> {
            assert_ne!(
                key, self.blob.object_key,
                "metadata load must not GET the blob"
            );
            Ok(self.metadata[key].clone())
        }

        async fn get_with_metadata(
            &self,
            key: &str,
            _version: Option<&str>,
            max_bytes: u64,
        ) -> Result<Option<RemoteObject>, BoundedGetError> {
            assert_ne!(
                key, self.blob.object_key,
                "metadata load must not GET the blob"
            );
            let Some(payload) = self.metadata.get(key) else {
                return Ok(None);
            };
            if payload.len() as u64 > max_bytes {
                return Err(BoundedGetError::BoundExceeded {
                    object_key: key.to_owned(),
                    actual_bytes: payload.len() as u64,
                    max_bytes,
                });
            }
            Ok(Some(RemoteObject {
                payload: payload.clone(),
                metadata: self.metadata(key, payload),
            }))
        }
    }

    fn zero_layer_fixture() -> (Arc<MetadataOnlyStore>, ChainIndexRef, u64) {
        zero_layer_fixture_with_codec(false)
    }

    fn zero_layer_fixture_with_codec(
        compressed: bool,
    ) -> (Arc<MetadataOnlyStore>, ChainIndexRef, u64) {
        let mut layer = LayerRangeMap {
            registry_objects: None,
            format_version: 1,
            kind: LayerKind::Block,
            content_sha256: format!("sha256:{}", "0".repeat(64)),
            logical_bytes: 65536,
            unit_size_bytes: 65536,
            registry: None,
            zfile: None,
            ranges: vec![LayerRange {
                kind: RangeKind::Zero,
                logical_offset: 0,
                length: 65536,
                source: None,
            }],
        };
        layer.content_sha256 = compute_layer_content_sha256(&layer).unwrap();
        let plan = plan_layer(&layer).unwrap();
        let payload = plan
            .read_at(&File::open("/dev/null").unwrap(), 0, 1024 * 1024)
            .unwrap();
        assert_eq!(payload.len() as u64, plan.stored_bytes);
        let zfile = if compressed {
            let path =
                std::env::temp_dir().join(format!("snapd-loader-zfile-{}", uuid::Uuid::new_v4()));
            let output = File::options()
                .read(true)
                .write(true)
                .create_new(true)
                .open(&path)
                .unwrap();
            let encoded = crate::snapshot::format::overlaybd::zfile::encode_with_reader(
                payload.len() as u64,
                &output,
                |offset, count| Ok(payload[offset as usize..offset as usize + count].to_vec()),
            )
            .unwrap();
            std::fs::remove_file(path).unwrap();
            Some(encoded)
        } else {
            None
        };
        let size = zfile
            .as_ref()
            .map_or(plan.stored_bytes, |encoded| encoded.index.encoded_bytes);
        let hash = zfile.as_ref().map_or_else(
            || sha256_identity(&payload),
            |encoded| encoded.sha256.clone(),
        );
        let encoding = if compressed {
            RangeMapEncoding::OverlaybdZfileLz4V1
        } else {
            RangeMapEncoding::OverlaybdLsmtV1
        };
        let prefix = format!("layers/block/1/{}/", &layer.content_sha256[7..]);
        let blob = ObjectRef {
            object_key: format!("{prefix}data/{}.commit", &hash[7..]),
            sha256: hash,
            object_version: None,
        };
        let descriptor = if let Some(encoded) = zfile {
            crate::snapshot::format::overlaybd::encode_zfile_descriptor(
                &layer,
                &plan,
                &blob,
                &encoded.index,
            )
            .unwrap()
        } else {
            encode_descriptor(&layer, &plan, &blob).unwrap()
        };
        let layer_ref = LayerRef {
            ordinal: 0,
            layer_id: "zero-layer".into(),
            format_version: 1,
            content_sha256: layer.content_sha256.clone(),
            range_map: LayerRangeMapRef {
                object_key: format!("{prefix}{}", encoding.descriptor_name()),
                sha256: sha256_identity(&descriptor),
                source_ref: None,
                object_version: None,
                encoding: Some(encoding),
            },
        };
        let committed = canonical_json_bytes(&json!({
            "content_sha256": layer.content_sha256,
            "format_version": 1,
            "range_map_sha256": layer_ref.range_map.sha256,
            "kind": layer.kind,
        }))
        .unwrap();
        let chain = LayerChainIndex {
            format_version: 1,
            kind: LayerKind::Block,
            logical_bytes: 65536,
            unit_size_bytes: 65536,
            layers: vec![layer_ref.clone()],
        };
        let chain_payload = canonical_json_bytes(&chain).unwrap();
        let reference = ChainIndexRef {
            logical_bytes: 65536,
            index_object_key: "snapshots/fixture/rootfs.index".into(),
            index_sha256: sha256_identity(&chain_payload),
            source_ref: None,
            object_version: None,
        };
        let store = Arc::new(MetadataOnlyStore {
            fence: NamespaceMutationFence::new("fixture").unwrap(),
            metadata: HashMap::from([
                (reference.index_object_key.clone(), chain_payload),
                (layer_ref.range_map.object_key, descriptor),
                (format!("{prefix}{}", encoding.committed_name()), committed),
            ]),
            blob,
            blob_size: AtomicU64::new(size),
            blob_heads: AtomicUsize::new(0),
        });
        (store, reference, size)
    }

    #[tokio::test]
    async fn zero_data_overlaybd_start_is_metadata_only_but_audit_checks_exact_blob_size() {
        let (store, reference, expected_size) = zero_layer_fixture();
        let cache_root = std::env::temp_dir().join(format!(
            "snapd-metadata-only-cache-{}",
            uuid::Uuid::new_v4().simple()
        ));
        let _local_cache = crate::snapshot::transport::posix_file::LocalFileCache::new(
            cache_root.clone(),
            "metadata-only-fixture",
        )
        .unwrap();
        let loader = SnapshotIo::build(
            store.clone(),
            SnapshotLoadLimits::default(),
            None,
            SnapshotStartCacheLimits::default(),
        )
        .unwrap();
        let mut objects = HashMap::new();
        let loaded = crate::snapshot::format::overlaybd_s3::OverlaybdS3Metadata
            .load_chain(
                &loader.metadata_context(),
                &reference,
                LayerKind::Block,
                &mut 0,
                &mut HashMap::new(),
                &mut objects,
            )
            .await
            .unwrap();
        assert_eq!(loaded.layers[0].range_map.ranges[0].kind, RangeKind::Zero);
        assert_eq!(store.blob_heads.load(Ordering::Relaxed), 0);
        assert_eq!(
            objects.len(),
            1,
            "zero-DATA standard blob must remain in the audit graph"
        );
        assert_eq!(
            objects[&store.blob.object_key].exact_size,
            Some(expected_size)
        );
        crate::snapshot::format::overlaybd_s3::OverlaybdS3Metadata
            .verify_data_objects(&loader.metadata_context(), &objects)
            .await
            .unwrap();
        assert_eq!(store.blob_heads.load(Ordering::Relaxed), 1);
        store
            .blob_size
            .store(expected_size + 512, Ordering::Relaxed);
        assert!(matches!(
            crate::snapshot::format::overlaybd_s3::OverlaybdS3Metadata
                .verify_data_objects(&loader.metadata_context(), &objects)
                .await,
            Err(SnapshotLoadError::IdentityConflict(_))
        ));
        let _ = std::fs::remove_dir_all(cache_root);
    }

    #[tokio::test]
    async fn compressed_zero_data_start_pins_index_without_data_reads_and_audits_physical_size() {
        let (store, reference, size) = zero_layer_fixture_with_codec(true);
        let loader =
            SnapshotIo::new_with_limits(store.clone(), SnapshotLoadLimits::default()).unwrap();
        let mut objects = HashMap::new();
        let loaded = crate::snapshot::format::overlaybd_s3::OverlaybdS3Metadata
            .load_chain(
                &loader.metadata_context(),
                &reference,
                LayerKind::Block,
                &mut 0,
                &mut HashMap::new(),
                &mut objects,
            )
            .await
            .unwrap();
        let index = loaded.layers[0].range_map.zfile.as_ref().unwrap();
        assert_eq!(index.encoded_bytes, size);
        assert!(index.raw_bytes > size);
        assert_eq!(store.blob_heads.load(Ordering::Relaxed), 0);
        assert_eq!(objects[&store.blob.object_key].exact_size, Some(size));
        crate::snapshot::format::overlaybd_s3::OverlaybdS3Metadata
            .verify_data_objects(&loader.metadata_context(), &objects)
            .await
            .unwrap();
        assert_eq!(store.blob_heads.load(Ordering::Relaxed), 1);
    }

    #[tokio::test]
    async fn lsmt_and_zfile_markers_coexist_for_the_same_logical_layer() {
        // Retain the historical regression name while covering the two current
        // standard encodings rather than a removed JSON persistence format.
        let (mut store, raw_reference, _) = zero_layer_fixture();
        let (compressed_store, compressed_reference, _) = zero_layer_fixture_with_codec(true);
        let mut mixed: LayerChainIndex = parse_canonical_model(
            &store.metadata[&raw_reference.index_object_key],
            "raw fixture chain",
        )
        .unwrap();
        let compressed: LayerChainIndex = parse_canonical_model(
            &compressed_store.metadata[&compressed_reference.index_object_key],
            "compressed fixture chain",
        )
        .unwrap();
        let mut compressed_layer = compressed.layers[0].clone();
        compressed_layer.ordinal = 1;
        compressed_layer.layer_id = "zero-layer-compressed".into();
        mixed.layers.push(compressed_layer);
        mixed.validate().unwrap();
        assert_eq!(
            mixed.layers[0].content_sha256,
            mixed.layers[1].content_sha256
        );
        assert_ne!(
            mixed.layers[0].range_map.sha256,
            mixed.layers[1].range_map.sha256
        );
        let prefix = format!("layers/block/1/{}/", &mixed.layers[0].content_sha256[7..]);
        let mut conflicting = mixed.clone();
        conflicting.layers[1].range_map.sha256 = mixed.layers[0].range_map.sha256.clone();
        let mixed_payload = canonical_json_bytes(&mixed).unwrap();
        let conflicting_payload = canonical_json_bytes(&conflicting).unwrap();
        let mixed_reference = ChainIndexRef {
            logical_bytes: mixed.logical_bytes,
            index_object_key: "snapshots/fixture-mixed/rootfs.index".into(),
            index_sha256: sha256_identity(&mixed_payload),
            source_ref: None,
            object_version: None,
        };
        let conflicting_reference = ChainIndexRef {
            logical_bytes: mixed.logical_bytes,
            index_object_key: "snapshots/fixture-conflicting/rootfs.index".into(),
            index_sha256: sha256_identity(&conflicting_payload),
            source_ref: None,
            object_version: None,
        };
        let objects = &mut Arc::get_mut(&mut store).unwrap().metadata;
        for (key, payload) in &compressed_store.metadata {
            if key != &compressed_reference.index_object_key {
                objects.insert(key.clone(), payload.clone());
            }
        }
        objects.insert(mixed_reference.index_object_key.clone(), mixed_payload);
        objects.insert(
            conflicting_reference.index_object_key.clone(),
            conflicting_payload,
        );
        assert_ne!(
            objects[&format!(
                "{prefix}{}",
                RangeMapEncoding::OverlaybdLsmtV1.committed_name()
            )],
            objects[&format!(
                "{prefix}{}",
                RangeMapEncoding::OverlaybdZfileLz4V1.committed_name()
            )]
        );
        let loader = SnapshotIo::new(store.clone());
        let mut bytes = 0;
        let mut cache = HashMap::new();
        let mut data_objects = HashMap::new();
        let loaded = crate::snapshot::format::overlaybd_s3::OverlaybdS3Metadata
            .load_chain(
                &loader.metadata_context(),
                &mixed_reference,
                LayerKind::Block,
                &mut bytes,
                &mut cache,
                &mut data_objects,
            )
            .await
            .unwrap();
        assert_eq!(loaded.layers.len(), 2);
        assert!(loaded.layers[0].range_map.zfile.is_none());
        assert!(loaded.layers[1].range_map.zfile.is_some());
        assert_eq!(
            loaded.layers[0].range_map.ranges,
            loaded.layers[1].range_map.ranges
        );
        assert_eq!(
            data_objects.len(),
            2,
            "both standard blobs remain in the audit graph"
        );
        let error = crate::snapshot::format::overlaybd_s3::OverlaybdS3Metadata
            .load_chain(
                &loader.metadata_context(),
                &conflicting_reference,
                LayerKind::Block,
                &mut bytes,
                &mut cache,
                &mut data_objects,
            )
            .await
            .unwrap_err();
        assert!(
            matches!(error, SnapshotLoadError::IdentityConflict(message) if message.contains("conflicting references"))
        );
        assert_eq!(store.blob_heads.load(Ordering::Relaxed), 0);
    }
    #[tokio::test]
    async fn registry_descriptor_start_is_metadata_only_and_not_a_snapd_data_object() {
        let native = crate::snapshot::format::overlaybd::composite::test_registry_rootfs();
        let descriptor = native.encode_descriptor().unwrap();
        let content = native.content_sha256().unwrap();
        let prefix = format!("layers/block/1/{}/", &content[7..]);
        let layer_ref = LayerRef {
            ordinal: 0,
            layer_id: "external-base".into(),
            format_version: 1,
            content_sha256: content.clone(),
            range_map: LayerRangeMapRef {
                object_key: format!("{prefix}registry-overlaybd.json"),
                sha256: sha256_identity(&descriptor),
                source_ref: None,
                object_version: None,
                encoding: Some(RangeMapEncoding::RegistryOverlaybdV1),
            },
        };
        let chain = LayerChainIndex {
            format_version: 1,
            kind: LayerKind::Block,
            logical_bytes: native.logical_bytes(),
            unit_size_bytes: 65536,
            layers: vec![layer_ref.clone()],
        };
        let payload = canonical_json_bytes(&chain).unwrap();
        let reference = ChainIndexRef {
            logical_bytes: native.logical_bytes(),
            index_object_key: "snapshots/external/rootfs.index".into(),
            index_sha256: sha256_identity(&payload),
            source_ref: None,
            object_version: None,
        };
        let marker = canonical_json_bytes(&json!({"content_sha256":content,"format_version":1,
            "range_map_sha256":layer_ref.range_map.sha256,"kind":LayerKind::Block}))
        .unwrap();
        let store = Arc::new(MetadataOnlyStore {
            fence: NamespaceMutationFence::new("registry-test").unwrap(),
            metadata: HashMap::from([
                (reference.index_object_key.clone(), payload),
                (layer_ref.range_map.object_key.clone(), descriptor.clone()),
                (format!("{prefix}COMMITTED.registry-overlaybd"), marker),
            ]),
            blob: ObjectRef {
                object_key: native.logical_object_key().unwrap(),
                sha256: native.content_sha256().unwrap(),
                object_version: None,
            },
            blob_size: AtomicU64::new(0),
            blob_heads: AtomicUsize::new(0),
        });
        let root =
            std::env::temp_dir().join(format!("snapd-registry-start-{}", uuid::Uuid::new_v4()));
        let _cache = crate::snapshot::transport::posix_file::LocalFileCache::new(
            root.clone(),
            "registry-start",
        )
        .unwrap();
        let loader = SnapshotIo::build(
            store.clone(),
            SnapshotLoadLimits::default(),
            None,
            SnapshotStartCacheLimits::default(),
        )
        .unwrap();
        let mut data = HashMap::new();
        let loaded = crate::snapshot::format::overlaybd_s3::OverlaybdS3Metadata
            .load_chain(
                &loader.metadata_context(),
                &reference,
                LayerKind::Block,
                &mut 0,
                &mut HashMap::new(),
                &mut data,
            )
            .await
            .unwrap();
        assert!(loaded.layers[0].range_map.registry.is_some());
        assert!(data.is_empty());
        crate::snapshot::format::overlaybd_s3::OverlaybdS3Metadata
            .verify_data_objects(&loader.metadata_context(), &data)
            .await
            .unwrap();
        assert_eq!(store.blob_heads.load(Ordering::Relaxed), 0);
        let _ = std::fs::remove_dir_all(root);
    }
}

#[cfg(test)]
mod transfer_tests {
    use super::*;
    use crate::snapshot::transport::{NamespaceMutationFence, VerifiedWriteReceipt};
    use tokio::sync::Barrier;

    struct VirtualSource {
        length: u64,
        max_read: AtomicUsize,
    }

    impl DataPackSource for VirtualSource {
        fn length(&self) -> u64 {
            self.length
        }

        fn read_at<'a>(
            &'a self,
            offset: u64,
            max_bytes: usize,
        ) -> Pin<Box<dyn Future<Output = Result<Vec<u8>, DataPackSourceError>> + Send + 'a>>
        {
            Box::pin(async move {
                if offset > self.length || max_bytes == 0 {
                    return Err(DataPackSourceError::InvalidRange);
                }
                self.max_read.fetch_max(max_bytes, Ordering::Relaxed);
                Ok(vec![
                    0x5a;
                    (self.length - offset).min(max_bytes as u64) as usize
                ])
            })
        }
    }

    #[derive(Debug)]
    struct AdmissionProbe {
        fence: Arc<NamespaceMutationFence>,
        entered: Arc<Barrier>,
        release: Arc<Barrier>,
    }

    #[async_trait::async_trait]
    impl ImmutableStreamWriter for AdmissionProbe {
        fn namespace(&self) -> &str {
            self.fence.namespace()
        }

        fn fence(&self) -> Arc<NamespaceMutationFence> {
            Arc::clone(&self.fence)
        }

        fn write_domain_id(&self) -> &str {
            "stream-probe"
        }

        async fn ensure_stream_verified(
            &self,
            identity: &ImmutableObjectIdentity,
            source: Arc<dyn ReplayableByteSource>,
            _lease: UploadTransactionLease,
            _deadline: Instant,
            _cancellation: IoCancellation,
            chunk_bytes: usize,
        ) -> Result<VerifiedWriteReceipt, ObjectStoreError> {
            assert_eq!(identity.length, 1024 * 1024 * 1024);
            assert_eq!(source.length(), identity.length);
            assert_eq!(chunk_bytes, 8 * 1024 * 1024);
            // Observe the stream without materializing or uploading the
            // GiB-long virtual object. The writer stops before any PUT.
            assert_eq!(source.read_at(0, 32).await?.len(), 32);
            self.entered.wait().await;
            self.release.wait().await;
            Err(ObjectStoreError::new(
                PublishFailureKind::Fatal,
                "admission probe: no remote upload",
            ))
        }
    }

    #[tokio::test]
    async fn gib_virtual_blobs_reserve_chunks_instead_of_whole_objects() {
        let config = SnapshotDataUploadConfig::default();
        let service = SnapshotDataUploadService::new(config.clone(), None).unwrap();
        let fence = NamespaceMutationFence::new("probe").unwrap();
        let entered = Arc::new(Barrier::new(5));
        let release = Arc::new(Barrier::new(5));
        let writer = Arc::new(AdmissionProbe {
            fence: Arc::clone(&fence),
            entered: Arc::clone(&entered),
            release: Arc::clone(&release),
        });
        let target =
            DataPackWriteTarget::new(writer, "stream-probe".into(), Arc::clone(&fence)).unwrap();
        let lease = UploadTransactionLease::acquire(fence).await;
        let mut uploads = JoinSet::new();
        let mut sources = Vec::new();
        for digit in ['1', '2', '3', '4'] {
            let hash = digit.to_string().repeat(64);
            let source = Arc::new(VirtualSource {
                length: 1024 * 1024 * 1024,
                max_read: AtomicUsize::new(0),
            });
            let request = DataPackUploadRequest {
                zfile: None,
                identity: DataPackIdentity {
                    object_key: DataPackObjectKey::new(format!(
                        "layers/block/1/{}/data/{hash}.commit",
                        "a".repeat(64),
                    ))
                    .unwrap(),
                    expected_sha256: format!("sha256:{hash}"),
                    length: source.length,
                },
                source: source.clone(),
                slices: Vec::new(),
            };
            validate_requests(std::slice::from_ref(&request)).unwrap();
            sources.push(source);
            let service = Arc::clone(&service);
            let target = target.clone();
            let lease = lease.clone();
            uploads.spawn(async move { service.ensure_one(target, request, lease).await });
        }
        tokio::time::timeout(Duration::from_secs(5), entered.wait())
            .await
            .unwrap();
        assert_eq!(
            service.state.lock().await.bytes_in_flight,
            4 * 2 * 8 * 1024 * 1024
        );
        assert_eq!(
            service.buffer_permits.available_permits(),
            config.max_bytes_in_flight as usize - 4 * 2 * config.chunk_bytes
        );
        assert!(sources
            .iter()
            .all(|source| source.max_read.load(Ordering::Relaxed) == 32));
        release.wait().await;
        while let Some(result) = uploads.join_next().await {
            assert!(matches!(
                result.unwrap(),
                Err(SnapshotDataUploadError::Backend { .. })
            ));
        }
        // A waiter can observe the finished result just before the
        // service-owned worker retires its single-flight reservation.
        tokio::time::timeout(Duration::from_secs(5), async {
            loop {
                if service.state.lock().await.bytes_in_flight == 0 {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        assert_eq!(service.state.lock().await.bytes_in_flight, 0);
        assert_eq!(
            service.buffer_permits.available_permits(),
            config.max_bytes_in_flight as usize
        );
    }

    #[tokio::test]
    async fn scan_checks_full_blob_and_unit_slices_with_metadata_gaps() {
        let source = VirtualSource {
            length: 40,
            max_read: AtomicUsize::new(0),
        };
        let digest = |length: usize| format!("sha256:{:x}", Sha256::digest(vec![0x5a; length]));
        let hash = digest(40);
        let mut identity = DataPackIdentity {
            object_key: DataPackObjectKey::new(format!(
                "layers/memory/1/{}/data/{}.commit",
                "a".repeat(64),
                &hash[7..],
            ))
            .unwrap(),
            expected_sha256: hash,
            length: 40,
        };
        let slices = vec![
            DataPackSliceIdentity {
                offset: 5,
                length: 7,
                expected_sha256: digest(7),
            },
            DataPackSliceIdentity {
                offset: 20,
                length: 4,
                expected_sha256: digest(4),
            },
        ];
        let (_, cancellation) = watch::channel(false);
        scan_source(
            &source,
            &identity,
            &slices,
            6,
            Instant::now() + Duration::from_secs(5),
            &cancellation,
        )
        .await
        .unwrap();
        let mut metadata_slices = slices.clone();
        metadata_slices.push(DataPackSliceIdentity {
            offset: 0,
            length: 5,
            expected_sha256: digest(4),
        });
        // Even with the correct full-object identity and DATA hashes, a
        // blob whose metadata differs from the mirrored standard plan is
        // rejected by its independent metadata-slice checksum.
        assert!(matches!(
            scan_source(
                &source,
                &identity,
                &metadata_slices,
                6,
                Instant::now() + Duration::from_secs(5),
                &cancellation,
            )
            .await,
            Err(SnapshotDataUploadError::Invalid(_))
        ));
        // Unit checksums still match, but the full SHA covers the
        // header/index/trailer gaps too and must not be bypassed.
        identity.expected_sha256 = digest(39);
        assert!(matches!(
            scan_source(
                &source,
                &identity,
                &slices,
                6,
                Instant::now() + Duration::from_secs(5),
                &cancellation
            )
            .await,
            Err(SnapshotDataUploadError::Invalid(_))
        ));
    }

    #[tokio::test]
    async fn zfile_scan_authenticates_actual_container_and_decoded_slices() {
        use std::os::unix::fs::FileExt;

        struct BytesSource(Vec<u8>);
        impl DataPackSource for BytesSource {
            fn length(&self) -> u64 {
                self.0.len() as u64
            }
            fn read_at<'a>(
                &'a self,
                offset: u64,
                count: usize,
            ) -> Pin<Box<dyn Future<Output = Result<Vec<u8>, DataPackSourceError>> + Send + 'a>>
            {
                Box::pin(async move {
                    if offset > self.0.len() as u64 || count > 16 * 1024 * 1024 {
                        return Err(DataPackSourceError::InvalidRange);
                    }
                    let end = (offset as usize).saturating_add(count).min(self.0.len());
                    Ok(self.0[offset as usize..end].to_vec())
                })
            }
        }

        let raw = vec![0x5a; 3 * ZFILE_SCAN_RAW_BATCH_BYTES + 4096];
        let slices = raw
            .chunks(4096)
            .enumerate()
            .map(|(unit, bytes)| DataPackSliceIdentity {
                offset: (unit * 4096) as u64,
                length: bytes.len() as u64,
                expected_sha256: format!("sha256:{:x}", Sha256::digest(bytes)),
            })
            .collect::<Vec<_>>();
        for scenario in 0..5 {
            let mut actual_raw = raw.clone();
            if scenario == 1 {
                actual_raw[20] ^= 1;
            } // inner metadata differs
            if scenario == 2 {
                actual_raw[4096 + 10] ^= 1;
            } // DATA differs
            let file = crate::snapshot::repository::frozen_layer::create_temporary_zfile().unwrap();
            let encoded = crate::snapshot::format::overlaybd::zfile::encode_with_reader(
                actual_raw.len() as u64,
                &file,
                |offset, count| Ok(actual_raw[offset as usize..offset as usize + count].to_vec()),
            )
            .unwrap();
            let mut payload = vec![0; encoded.index.encoded_bytes as usize];
            file.read_exact_at(&mut payload, 0).unwrap();
            if scenario == 3 {
                payload[0] ^= 1;
            } // actual wrapper differs from mirrors
            if scenario == 4 {
                payload[512 + 7] ^= 1;
            } // actual compressed block/CRC differs
            let hash = format!("sha256:{:x}", Sha256::digest(&payload));
            let identity = DataPackIdentity {
                object_key: DataPackObjectKey::new(format!(
                    "layers/memory/1/{}/data/{}.commit",
                    "a".repeat(64),
                    &hash[7..]
                ))
                .unwrap(),
                expected_sha256: hash,
                length: payload.len() as u64,
            };
            let source = BytesSource(payload);
            let (_, cancellation) = watch::channel(false);
            let result = scan_encoded_source(
                &source,
                &identity,
                &slices,
                Some(&encoded.index),
                4096,
                Instant::now() + Duration::from_secs(10),
                &cancellation,
            )
            .await;
            assert_eq!(
                result.is_ok(),
                scenario == 0,
                "scenario {scenario}: {result:?}"
            );
        }
    }

    #[test]
    fn transfer_pack_and_overlaybd_blob_limits_are_separate() {
        let hash = "a".repeat(64);
        for (suffix, length, valid) in [
            ("pack", MAX_DATA_PACK_BYTES, true),
            ("pack", MAX_DATA_PACK_BYTES + 1, false),
            ("commit", 1024 * 1024 * 1024, true),
            ("commit", MAX_BLOB_BYTES + 1, false),
        ] {
            let identity = DataPackIdentity {
                object_key: DataPackObjectKey::new(format!(
                    "layers/block/1/{hash}/data/{hash}.{suffix}"
                ))
                .unwrap(),
                expected_sha256: format!("sha256:{hash}"),
                length,
            };
            assert_eq!(identity.validate().is_ok(), valid);
        }
    }
}

#[cfg(test)]
mod snapshot_reader_tests {
    use super::*;
    use crate::snapshot::format::raw::RawLayerReader;
    use crate::snapshot::metadata::SnapshotLayerMetadata;
    use crate::RuntimeIdentity;
    use std::os::unix::fs::FileExt;

    #[tokio::test]
    async fn data_only_snapshot_reader_keeps_raw_fd_reads_and_rejects_metadata_open() {
        let reader: Arc<dyn SnapshotReader> = SnapshotIo::data_only();
        assert!(matches!(
            reader.load(&json!({}), "unconfigured", "raw-only").await,
            Err(SnapshotLoadError::MissingObject(_))
        ));
        let file = crate::snapshot::repository::frozen_layer::create_temporary_zfile().unwrap();
        file.set_len(8192).unwrap();
        file.write_all_at(&vec![0x7b; 4096], 4096).unwrap();
        let content_identity = sha256_identity(b"snapshot-raw-fd");
        let raw = RawLayerReader::new(
            SnapshotLayerMetadata {
                ordinal: 0,
                layer_id: "raw-fd".into(),
                content_identity: content_identity.clone(),
                format: "raw".into(),
                logical_bytes: 8192,
                unit_size_bytes: 4096,
            },
            LayerKind::Memory,
            vec![(4096, 4096)],
            crate::snapshot::io::backing::LocalBackingHandle::from_native(
                crate::snapshot::repository::frozen_layer::FrozenLayerPin::new(
                    crate::snapshot::repository::frozen_layer::FrozenLayerMetadata {
                        kind: LayerKind::Memory,
                        layer_id: "raw-fd".into(),
                        provisional_content_id: content_identity.clone(),
                        logical_size: 8192,
                        alignment: 4096,
                    },
                    crate::snapshot::repository::frozen_layer::PackedOwnershipMap {
                        kind: LayerKind::Memory,
                        snapshot_id: "raw-only".into(),
                        format_version: 1,
                        logical_size: 8192,
                        granularity: 4096,
                        packed_states: vec![4],
                    },
                    file.into(),
                    false,
                ),
            )
            .unwrap(),
        )
        .unwrap();
        // No DATA transport is configured: this operation must dispatch to
        // RawLayerReader's retained FD, while the sparse hole stays non-DATA.
        let context = LazyLoadContext::new(
            RangeIO::for_fixed_targets(),
            RuntimeIdentity::new("raw-fd-runtime"),
            None,
        );
        let request = SnapshotReadRange {
            kind: LayerKind::Memory,
            layer_content_sha256: &content_identity,
            logical_offset: 4096,
            length: 4096,
        };
        let result = raw.read_data_range(&context, request).await.unwrap();
        assert_eq!(result.payload, vec![0x7b; 4096]);
        assert_eq!(result.accounting.cache_bytes, 4096);
        assert_eq!(result.accounting.provider_bytes, 0);
        assert!(matches!(
            raw.read_data_range(
                &context,
                SnapshotReadRange {
                    logical_offset: 0,
                    ..request
                }
            )
            .await,
            Err(LazyLoadError::NonDataRange)
        ));
    }
}
