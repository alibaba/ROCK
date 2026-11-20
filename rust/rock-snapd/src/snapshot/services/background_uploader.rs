//! Snapshot upload lifecycle, ordered publication and bounded object transfer.
//!
//! SnapshotRepository owns snapshot and publication state. SnapshotUploadService
//! executes uploads, retry timers and shutdown without a second snapshot table.
//! SnapshotWriter prepares and publishes immutable artifacts; range I/O owns
//! physical source selection and fixed-target transport delivery.

use std::collections::BTreeMap;
#[cfg(test)]
use std::fs::File;
#[cfg(test)]
use std::future::Future;
#[cfg(test)]
use std::os::unix::fs::FileExt;
#[cfg(test)]
use std::pin::Pin;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use async_trait::async_trait;
use serde_json::{json, Value};
use thiserror::Error;
use tokio::sync::{watch, Mutex, Notify};

use crate::observability::metrics::{LeaseKind, SnapdMetrics};
use crate::observability::structured_log::{
    StructuredComponent, StructuredEvent, StructuredEventName, StructuredLogLevel,
    StructuredOutcome,
};
#[cfg(test)]
use crate::snapshot::format::overlaybd::cache::OverlaybdObjectValidator;
#[cfg(test)]
use crate::snapshot::format::LayerUploadPlan;
use crate::snapshot::format::SnapshotUploadPlan;
use crate::snapshot::format::{SnapshotUploadError, UploadedSnapshot};
#[cfg(test)]
use crate::snapshot::io::range_io::RangeIO;
use crate::snapshot::io::snapshot_io::SnapshotWriter;
#[cfg(test)]
use crate::snapshot::io::snapshot_io::{SnapshotDataUploadConfig, SnapshotPublisher};
#[cfg(test)]
use crate::snapshot::metadata::StorageProvider;
use crate::snapshot::metadata::{
    canonical_json_bytes, sha256_identity, snapshot_spec_checksum, LayerKind,
    SnapshotChainMetadata, SnapshotProvenance, StorageDescriptor, VmmCompatibility,
};
#[cfg(test)]
use crate::snapshot::repository::frozen_layer::FrozenLayerError;
use crate::snapshot::repository::LocalSnapshotError;
use crate::snapshot::repository::{
    PublicationState, SnapshotRepository, SnapshotState, SnapshotUploadFailure,
    SnapshotUploadTargetIdentity,
};
use crate::snapshot::transport::posix_file::{CacheError, LocalFileCache};
use crate::snapshot::transport::DataPackWriteTarget;
#[cfg(test)]
use crate::snapshot::transport::ImmutableObjectIdentity;
#[cfg(test)]
use crate::snapshot::transport::{DataPackSource, DataPackSourceError};
use crate::snapshot::transport::{PublishFailureKind, SnapshotMetadataWriteTarget};
use crate::snapshot::Snapshot;
use crate::snapshot::SnapshotInstance;

/// Publish one already-frozen snapshot. Serialization is used only by stable
/// request-identity calculations, independently of the HTTP request model.
#[derive(Clone, Debug, Eq, PartialEq, serde::Serialize)]
pub struct SnapshotPublication {
    pub snapshot_id: String,
}

/// Publication progress projected from the repository's snapshot record.
#[derive(Clone, Debug, PartialEq)]
pub struct SnapshotStatus {
    pub snapshot_id: String,
    pub state: PublicationState,
    pub remote_publishing: bool,
    pub progress: f64,
    pub attempt_count: u64,
    pub last_error: Option<String>,
    pub failure_code: Option<String>,
    pub retryable: bool,
    pub retry_at_unix_ms: Option<u64>,
    pub snapshot_spec: Option<Value>,
    pub memory_bytes: Option<u64>,
    pub rootfs_bytes: Option<u64>,
    pub spec_checksum: Option<String>,
    pub admin_finalized: bool,
    pub catalog_pin_held: bool,
    pub layers: BTreeMap<LayerKind, LayerUploadStatus>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LayerUploadStatus {
    pub upload_state: LayerUploadState,
    pub remote_publishing: bool,
    pub attempt_count: u64,
    pub last_error: Option<String>,
    pub retry_at_unix_ms: Option<u64>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LayerUploadState {
    LocalReady,
    RemoteReady,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PublicationFinalized {
    pub identity: String,
    pub state: PublicationState,
    pub released_pins: u64,
}

// Upload task lifecycle and source ownership.

#[derive(Clone, Debug, Error, Eq, PartialEq)]
pub enum SnapshotUploadServiceError {
    #[error("snapshot upload is disabled; select an upload_format to enable publication")]
    UploadDisabled,
    #[error(transparent)]
    SourceIntegrity(crate::snapshot::transport::DataIntegrityFailure),
    #[error("invalid upload request: {0}")]
    Invalid(String),
    #[error("upload identity conflict: {0}")]
    IdentityConflict(String),
    #[error("upload state transition is invalid: {0}")]
    InvalidState(String),
    #[error("upload record was not found: {0}")]
    NotFound(String),
    #[error("upload service is closed")]
    Closed,
    #[error("upload failed: {message}")]
    Publish { message: String, retryable: bool },
}

impl SnapshotUploadServiceError {
    pub const fn retryable(&self) -> bool {
        matches!(
            self,
            Self::Publish {
                retryable: true,
                ..
            }
        )
    }
}

impl From<LocalSnapshotError> for SnapshotUploadServiceError {
    fn from(error: LocalSnapshotError) -> Self {
        match error {
            LocalSnapshotError::SourceIntegrity(failure) => Self::SourceIntegrity(failure),
            LocalSnapshotError::Io(message) => Self::Publish {
                message,
                retryable: true,
            },
            error => Self::Invalid(error.to_string()),
        }
    }
}

#[cfg(test)]
fn scratch_file_error(error: CacheError) -> FrozenLayerError {
    match error {
        CacheError::Io(error) => FrozenLayerError::Io(error.to_string()),
        error => FrozenLayerError::CanonicalModel(error.to_string()),
    }
}

#[derive(Clone)]
pub struct ResolvedSnapshotUploadTarget {
    pub metadata_target: SnapshotMetadataWriteTarget,
    pub data_target: DataPackWriteTarget,
    pub storage: StorageDescriptor,
    pub compatibility: VmmCompatibility,
}

#[async_trait]
pub trait SnapshotUploadTargetResolver: Send + Sync + 'static {
    async fn resolve(&self) -> Result<ResolvedSnapshotUploadTarget, SnapshotUploadServiceError>;

    async fn resolve_for(
        &self,
        _provenance: Option<&SnapshotProvenance>,
    ) -> Result<ResolvedSnapshotUploadTarget, SnapshotUploadServiceError> {
        self.resolve().await
    }
}

#[derive(Clone, Debug)]
pub struct UploadedSnapshotFinalizationSource {
    pub local: Arc<Snapshot>,
    pub published: UploadedSnapshot,
}

/// A response projection copied under the record lock. Local source inspection
/// happens after releasing repository locks, because encoding may hold its lock.
struct UploadStatusSnapshot {
    snapshot_id: String,
    state: PublicationState,
    chains: Vec<SnapshotChainMetadata>,
    local: Option<Arc<Snapshot>>,
    remote_publishing: bool,
    attempt_count: u64,
    retry_at_unix_ms: Option<u64>,
    failure: Option<SnapshotUploadFailure>,
    snapshot_spec: Option<Value>,
    spec_checksum: Option<String>,
    admin_finalized: bool,
}

impl UploadStatusSnapshot {
    fn from_instance(record: &Arc<dyn SnapshotInstance>, data: &SnapshotState) -> Self {
        Self {
            snapshot_id: record.snapshot_id().to_owned(),
            state: data.state,
            chains: data.chains.clone(),
            local: raw_ready(record, data),
            remote_publishing: data.remote_publishing,
            attempt_count: data.attempt_count,
            retry_at_unix_ms: data.retry_at_unix_ms,
            failure: data.upload_failure.clone(),
            snapshot_spec: data.snapshot_spec.clone(),
            spec_checksum: data.spec_checksum.clone(),
            admin_finalized: data.admin_finalized,
        }
    }
}

pub struct SnapshotUploadService {
    enabled: bool,
    catalog: Arc<SnapshotRepository>,
    publisher: Arc<dyn SnapshotWriter>,
    resolver: Arc<dyn SnapshotUploadTargetResolver>,
    retry_base: Duration,
    retry_max: Duration,
    closed: Mutex<bool>,
    changed: Notify,
    shutdown: watch::Sender<bool>,
    metrics: Option<Arc<SnapdMetrics>>,
    local_cache: Option<Arc<LocalFileCache>>,
}

impl SnapshotUploadService {
    pub fn new(
        publisher: Arc<dyn SnapshotWriter>,
        resolver: Arc<dyn SnapshotUploadTargetResolver>,
        retry_base: Duration,
        retry_max: Duration,
    ) -> Result<Arc<Self>, SnapshotUploadServiceError> {
        Self::new_with_metrics(publisher, resolver, retry_base, retry_max, None)
    }

    pub fn new_with_metrics(
        publisher: Arc<dyn SnapshotWriter>,
        resolver: Arc<dyn SnapshotUploadTargetResolver>,
        retry_base: Duration,
        retry_max: Duration,
        metrics: Option<Arc<SnapdMetrics>>,
    ) -> Result<Arc<Self>, SnapshotUploadServiceError> {
        Self::new_with_metrics_and_local_cache(
            publisher, resolver, retry_base, retry_max, metrics, None,
        )
    }

    pub(crate) fn new_with_metrics_and_local_cache(
        publisher: Arc<dyn SnapshotWriter>,
        resolver: Arc<dyn SnapshotUploadTargetResolver>,
        retry_base: Duration,
        retry_max: Duration,
        metrics: Option<Arc<SnapdMetrics>>,
        local_cache: Option<Arc<LocalFileCache>>,
    ) -> Result<Arc<Self>, SnapshotUploadServiceError> {
        if retry_max.is_zero() || retry_base > retry_max {
            return Err(SnapshotUploadServiceError::Invalid(
                "upload retry backoff is invalid".into(),
            ));
        }
        let (shutdown, _) = watch::channel(false);
        Ok(Arc::new(Self {
            enabled: true,
            catalog: SnapshotRepository::new(),
            publisher,
            resolver,
            retry_base,
            retry_max,
            closed: Mutex::new(false),
            changed: Notify::new(),
            shutdown,
            metrics,
            local_cache,
        }))
    }

    /// Disabled publication keeps local snapshot ownership and read-only OCI
    /// template registration available without resolving any write target.
    pub(crate) fn with_enabled(mut self: Arc<Self>, enabled: bool) -> Arc<Self> {
        Arc::get_mut(&mut self)
            .expect("upload policy is configured before sharing uploads")
            .enabled = enabled;
        self
    }

    pub fn enabled(&self) -> bool {
        self.enabled
    }

    fn ensure_enabled(&self) -> Result<(), SnapshotUploadServiceError> {
        if self.enabled {
            Ok(())
        } else {
            Err(SnapshotUploadServiceError::UploadDisabled)
        }
    }

    /// Inject the one node authority before this service is shared or started.
    pub fn with_catalog(mut self: Arc<Self>, catalog: Arc<SnapshotRepository>) -> Arc<Self> {
        let service =
            Arc::get_mut(&mut self).expect("catalog is configured before sharing uploads");
        service.catalog = catalog;
        self
    }

    pub fn catalog(&self) -> Arc<SnapshotRepository> {
        Arc::clone(&self.catalog)
    }

    pub async fn preflight(
        &self,
    ) -> Result<ResolvedSnapshotUploadTarget, SnapshotUploadServiceError> {
        self.preflight_for(None).await
    }

    pub async fn preflight_for(
        &self,
        provenance: Option<&SnapshotProvenance>,
    ) -> Result<ResolvedSnapshotUploadTarget, SnapshotUploadServiceError> {
        self.ensure_enabled()?;
        if *self.closed.lock().await {
            return Err(SnapshotUploadServiceError::Closed);
        }
        let target = self.resolver.resolve_for(provenance).await?;
        validate_target(&target)?;
        Ok(target)
    }

    pub async fn register(
        self: &Arc<Self>,
        local: Arc<Snapshot>,
        request: SnapshotPublication,
        target: ResolvedSnapshotUploadTarget,
    ) -> Result<SnapshotStatus, SnapshotUploadServiceError> {
        self.ensure_enabled()?;
        validate_target(&target)?;
        if !local.has_native() {
            return Err(SnapshotUploadServiceError::InvalidState(
                "raw snapshot resources are still being captured".into(),
            ));
        }
        local.ensure_source_integrity()?;
        if local.snapshot_id != request.snapshot_id {
            return Err(SnapshotUploadServiceError::IdentityConflict(
                "LOCAL_READY snapshot does not match the upload request".into(),
            ));
        }
        if local.is_closed()? {
            return Err(SnapshotUploadServiceError::Invalid(
                "LOCAL_READY upload pin is closed".into(),
            ));
        }
        let target_identity = immutable_target_identity(&target);
        let identity = local_upload_identity(&local, &target_identity)?;
        let closed = self.closed.lock().await;
        if *closed {
            return Err(SnapshotUploadServiceError::Closed);
        }
        self.ensure_unregistered(&request.snapshot_id)?;
        let snapshot: Arc<dyn SnapshotInstance> = self
            .catalog
            .register_raw(Arc::clone(&local))
            .map_err(SnapshotUploadServiceError::IdentityConflict)?;
        let current = self
            .catalog
            .update_instance(&snapshot, |data| {
                if data.upload_identity.is_some() {
                    return Err(SnapshotUploadServiceError::IdentityConflict(
                        "snapshot ID already owns an upload".into(),
                    ));
                }
                data.upload_identity = Some(identity);
                data.upload_target = Some(target_identity);
                self.start_attempt_locked(&snapshot, data, Some(target))?;
                Ok(UploadStatusSnapshot::from_instance(&snapshot, data))
            })
            .ok_or_else(|| SnapshotUploadServiceError::NotFound(request.snapshot_id.clone()))??;
        if let Some(metrics) = &self.metrics {
            metrics.add_lease(LeaseKind::Upload, 1);
            metrics.set_upload_backlog(self.local_ready_count());
        }
        drop(closed);
        status(current)
    }

    pub async fn status(
        &self,
        snapshot_id: &str,
    ) -> Result<SnapshotStatus, SnapshotUploadServiceError> {
        let current = self.update_upload(snapshot_id, |snapshot, data| {
            Ok(UploadStatusSnapshot::from_instance(snapshot, data))
        })?;
        status(current)
    }

    /// Register a source-backed template without publishing metadata or DATA
    /// to a writable store. The catalog's checksummed spec is its durable root.
    pub async fn register_registry(
        self: &Arc<Self>,
        snapshot: &Arc<dyn SnapshotInstance>,
        rootfs: Arc<crate::snapshot::format::overlaybd::composite::RegistryRootfs>,
        request: SnapshotPublication,
        compatibility: VmmCompatibility,
        provenance: Option<SnapshotProvenance>,
    ) -> Result<SnapshotStatus, SnapshotUploadServiceError> {
        let plan = crate::snapshot::format::overlaybd_oci::registry_base_plan_with_provenance(
            &request.snapshot_id,
            &rootfs,
            compatibility,
            provenance,
        )?;
        let target_identity = SnapshotUploadTargetIdentity {
            provider: plan.spec.storage.provider,
            endpoint_profile: plan.spec.storage.endpoint_profile.clone(),
            backend_namespace: format!("{}/{}", rootfs.registry(), rootfs.repository()),
            compatibility: plan.spec.compatibility.clone(),
        };
        let identity = plan.identity()?;
        {
            let closed = self.closed.lock().await;
            if *closed {
                return Err(SnapshotUploadServiceError::Closed);
            }
            self.ensure_unregistered(&request.snapshot_id)?;
        }
        let chains = plan.metadata_chains()?;
        let manifest = serde_json::from_slice(&plan.manifest.payload)
            .map_err(|error| SnapshotUploadServiceError::Invalid(error.to_string()))?;
        let published = UploadedSnapshot {
            registry_objects: None,
            spec: plan.spec,
            manifest,
        };
        let snapshot_spec = serde_json::to_value(&published.spec)
            .map_err(|error| SnapshotUploadServiceError::Invalid(error.to_string()))?;
        let spec_checksum = snapshot_spec_checksum(&snapshot_spec)
            .map_err(|error| SnapshotUploadServiceError::Invalid(error.to_string()))?;
        let closed = self.closed.lock().await;
        if *closed {
            return Err(SnapshotUploadServiceError::Closed);
        }
        self.ensure_unregistered(&request.snapshot_id)?;
        if snapshot.snapshot_id() != request.snapshot_id {
            return Err(SnapshotUploadServiceError::IdentityConflict(
                "Registry snapshot does not match the reserved identity".into(),
            ));
        }
        let ready = crate::snapshot::format::overlaybd_oci::OverlaybdOciSnapshot::from_registry(
            rootfs, &published,
        )
        .map_err(|error| SnapshotUploadServiceError::Invalid(error.to_string()))?;
        self.catalog
            .initialize_oci(snapshot, ready)
            .map_err(SnapshotUploadServiceError::IdentityConflict)?;
        let current = self
            .catalog
            .update_instance(snapshot, |data| {
                if data.upload_identity.is_some() {
                    return Err(SnapshotUploadServiceError::IdentityConflict(
                        "snapshot ID already owns a base or upload".into(),
                    ));
                }
                self.catalog
                    .commit_remote_data(snapshot.snapshot_id(), data, chains, &spec_checksum)
                    .map_err(SnapshotUploadServiceError::IdentityConflict)?;
                data.upload_identity = Some(identity);
                data.upload_target = Some(target_identity);
                data.attempt_count = 1;
                data.published = Some(published);
                data.snapshot_spec = Some(snapshot_spec);
                data.spec_checksum = Some(spec_checksum);
                Ok(UploadStatusSnapshot::from_instance(snapshot, data))
            })
            .ok_or_else(|| SnapshotUploadServiceError::NotFound(request.snapshot_id.clone()))??;
        self.changed.notify_waiters();
        drop(closed);
        status(current)
    }

    pub async fn retry(
        self: &Arc<Self>,
        snapshot_id: &str,
    ) -> Result<SnapshotStatus, SnapshotUploadServiceError> {
        self.ensure_enabled()?;
        // Frozen ancestry is checked before entering the record callback,
        // which already holds this snapshot's lifecycle mutex.
        if let Some(local) = self
            .catalog
            .get(snapshot_id)
            .and_then(|snapshot| snapshot.downcast::<Snapshot>())
        {
            local.ensure_source_integrity()?;
        }
        let closed = self.closed.lock().await;
        let current = self.update_upload(snapshot_id, |snapshot, data| {
            if let Some(failure) = &data.source_integrity_failure {
                return Err(SnapshotUploadServiceError::SourceIntegrity(failure.clone()));
            }
            if !data.remote_root_committed
                && raw_ready(snapshot, data).is_some()
                && !data.remote_publishing
                && !data
                    .upload_failure
                    .as_ref()
                    .is_some_and(|failure| !failure.retryable)
            {
                if *closed {
                    return Err(SnapshotUploadServiceError::Closed);
                }
                self.start_attempt_locked(snapshot, data, None)?;
            }
            Ok(UploadStatusSnapshot::from_instance(snapshot, data))
        })?;
        drop(closed);
        status(current)
    }

    pub async fn wait_current_attempt(
        &self,
        snapshot_id: &str,
    ) -> Result<SnapshotStatus, SnapshotUploadServiceError> {
        let snapshot = self
            .catalog
            .get(snapshot_id)
            .ok_or_else(|| SnapshotUploadServiceError::NotFound(snapshot_id.into()))?;
        loop {
            let notified = self.changed.notified();
            let current = self
                .catalog
                .update_instance(&snapshot, |data| {
                    if data.upload_identity.is_none() {
                        return Err(SnapshotUploadServiceError::NotFound(snapshot_id.into()));
                    }
                    Ok((
                        data.remote_publishing,
                        UploadStatusSnapshot::from_instance(&snapshot, data),
                    ))
                })
                .ok_or_else(|| {
                    SnapshotUploadServiceError::IdentityConflict(
                        "upload identity changed while waiting".into(),
                    )
                })??;
            if !current.0 {
                return status(current.1);
            }
            notified.await;
        }
    }

    pub async fn finalize_published(
        &self,
        snapshot_id: &str,
        spec_checksum: &str,
    ) -> Result<PublicationFinalized, SnapshotUploadServiceError> {
        let _closed = self.closed.lock().await;
        let (snapshot, local) = self.update_upload(snapshot_id, |snapshot, data| {
            validate_finalization(data, spec_checksum, self.metrics.as_deref())?;
            Ok((Arc::clone(snapshot), raw_ready(snapshot, data)))
        })?;
        // Local source locks may be held by encoding. Never retain repository
        // locks while inspecting or releasing the upload pin.
        let released_pins = if let Some(local) = local {
            let held = u64::from(!local.is_closed()?);
            local.close_upload_pin()?;
            held
        } else {
            0
        };
        if released_pins != 0 {
            if let Some(metrics) = &self.metrics {
                metrics.add_lease(LeaseKind::Upload, -(released_pins as i64));
            }
        }
        self.catalog
            .release_local_source(&snapshot)
            .map_err(SnapshotUploadServiceError::IdentityConflict)?;
        Ok(PublicationFinalized {
            identity: snapshot_id.into(),
            state: PublicationState::RemoteReady,
            released_pins,
        })
    }

    pub async fn finalization_source(
        &self,
        snapshot_id: &str,
        spec_checksum: &str,
    ) -> Result<Option<UploadedSnapshotFinalizationSource>, SnapshotUploadServiceError> {
        self.update_upload(snapshot_id, |snapshot, data| {
            if data.state != PublicationState::RemoteReady {
                return Err(SnapshotUploadServiceError::Invalid(
                    "snapshot immutable upload is not REMOTE_READY".into(),
                ));
            }
            if data.spec_checksum.as_deref() != Some(spec_checksum) {
                return Err(SnapshotUploadServiceError::IdentityConflict(
                    "published snapshot spec checksum differs from Admin confirmation".into(),
                ));
            }
            if data.admin_finalized {
                return Err(SnapshotUploadServiceError::Invalid(
                    "snapshot publication is already finalized".into(),
                ));
            }
            finalization_source(snapshot, data)
        })
    }

    /// Published canonical-object aliases only. Native LOCAL_READY peer reads
    /// are served from Snapshot/LocalBacking without entering this service.
    pub async fn published_p2p_source(
        &self,
        snapshot_id: &str,
        spec_checksum: &str,
    ) -> Result<Option<UploadedSnapshotFinalizationSource>, SnapshotUploadServiceError> {
        let result = self.update_upload(snapshot_id, |snapshot, data| {
            if data.state != PublicationState::RemoteReady {
                return Ok(None);
            }
            if data.spec_checksum.as_deref() != Some(spec_checksum) {
                return Err(SnapshotUploadServiceError::IdentityConflict(
                    "P2P source spec checksum differs from the published snapshot".into(),
                ));
            }
            finalization_source(snapshot, data)
        });
        match result {
            Err(SnapshotUploadServiceError::NotFound(_)) => Ok(None),
            result => result,
        }
    }

    /// Verify the Admin delete fence can invalidate this process-local alias.
    pub async fn ensure_invalidation_ready(
        &self,
        snapshot_id: &str,
    ) -> Result<bool, SnapshotUploadServiceError> {
        match self.update_upload(snapshot_id, |_, data| {
            if data.remote_publishing {
                return Err(SnapshotUploadServiceError::InvalidState(
                    "snapshot upload is still mutating".into(),
                ));
            }
            Ok(true)
        }) {
            Err(SnapshotUploadServiceError::NotFound(_)) => Ok(false),
            result => result,
        }
    }

    /// Removing the node-local alias does not garbage-collect immutable objects.
    /// Timers retain the original record Arc and cannot affect a replacement.
    pub async fn invalidate(&self, snapshot_id: &str) -> Result<bool, SnapshotUploadServiceError> {
        let _closed = self.closed.lock().await;
        let result = self.update_upload(snapshot_id, |snapshot, data| {
            if data.remote_publishing {
                return Err(SnapshotUploadServiceError::InvalidState(
                    "snapshot upload is still mutating".into(),
                ));
            }
            let was_local_ready = data.state == PublicationState::LocalReady;
            data.retry_scheduled = false;
            Ok((
                Arc::clone(snapshot),
                raw_ready(snapshot, data),
                was_local_ready,
            ))
        });
        let (snapshot, local, was_local_ready) = match result {
            Err(SnapshotUploadServiceError::NotFound(_)) => return Ok(false),
            result => result?,
        };
        let pin_held = match local {
            Some(local) => {
                let held = !local.is_closed()?;
                local.close_upload_pin()?;
                held
            }
            None => false,
        };
        if !self.catalog.invalidate_instance(&snapshot) {
            return Err(SnapshotUploadServiceError::IdentityConflict(
                "snapshot instance changed during invalidation".into(),
            ));
        }
        if let Some(metrics) = &self.metrics {
            if pin_held {
                metrics.add_lease(LeaseKind::Upload, -1);
            }
            if was_local_ready {
                metrics.set_upload_backlog(self.local_ready_count());
            }
        }
        self.changed.notify_waiters();
        Ok(true)
    }

    pub async fn close(&self) -> Result<(), SnapshotUploadServiceError> {
        {
            let mut closed = self.closed.lock().await;
            if *closed {
                return Ok(());
            }
            *closed = true;
            for snapshot in self.catalog.instances() {
                self.catalog.update_instance(&snapshot, |data| {
                    if data.upload_identity.is_some() {
                        data.retry_scheduled = false;
                        data.retry_at_unix_ms = None;
                    }
                });
            }
        }
        let _ = self.shutdown.send(true);
        self.publisher.close().await;
        loop {
            let notified = self.changed.notified();
            let active = self.catalog.instances().iter().any(|snapshot| {
                self.catalog
                    .update_instance(snapshot, |data| {
                        data.upload_identity.is_some() && data.remote_publishing
                    })
                    .unwrap_or(false)
            });
            if !active {
                break;
            }
            notified.await;
        }
        let mut held_pins = 0_i64;
        for snapshot in self.catalog.instances() {
            let local = self
                .catalog
                .update_instance(&snapshot, |data| {
                    data.upload_identity
                        .as_ref()
                        .and(raw_ready(&snapshot, data))
                })
                .flatten();
            if let Some(local) = local {
                held_pins += i64::from(!local.is_closed()?);
                local.close_upload_pin()?;
            }
        }
        if let Some(metrics) = &self.metrics {
            metrics.add_lease(LeaseKind::Upload, -held_pins);
            metrics.set_upload_backlog(0);
        }
        Ok(())
    }

    // Callers hold the service lifecycle gate and the current repository record.
    fn start_attempt_locked(
        self: &Arc<Self>,
        snapshot: &Arc<dyn SnapshotInstance>,
        data: &mut SnapshotState,
        initial_target: Option<ResolvedSnapshotUploadTarget>,
    ) -> Result<(), SnapshotUploadServiceError> {
        self.ensure_enabled()?;
        if let Some(failure) = &data.source_integrity_failure {
            return Err(SnapshotUploadServiceError::SourceIntegrity(failure.clone()));
        }
        if data.remote_publishing {
            return Ok(());
        }
        data.resume();
        data.remote_publishing = true;
        data.retry_scheduled = false;
        data.retry_at_unix_ms = None;
        data.upload_failure = None;
        data.attempt_count = data.attempt_count.saturating_add(1);
        let service = Arc::clone(self);
        let snapshot = Arc::clone(snapshot);
        tokio::spawn(async move {
            service.run_attempt(snapshot, initial_target).await;
        });
        Ok(())
    }

    async fn run_attempt(
        self: Arc<Self>,
        snapshot: Arc<dyn SnapshotInstance>,
        initial_target: Option<ResolvedSnapshotUploadTarget>,
    ) {
        let snapshot_id = snapshot.snapshot_id().to_owned();
        let inputs = self
            .catalog
            .update_instance(&snapshot, |data| {
                if !data.remote_publishing {
                    return None;
                }
                Some((
                    raw_ready(&snapshot, data).expect("only local uploads run DATA attempts"),
                    data.upload_target
                        .clone()
                        .expect("registered upload has a target identity"),
                    data.attempt_count,
                ))
            })
            .flatten();
        let Some((local, target_identity, attempt_count)) = inputs else {
            return;
        };
        let result = async {
            local.ensure_source_integrity()?;
            // LOCAL_READY descendants may be cut before their ancestors reach
            // storage. Keep capture asynchronous, but never publish a manifest
            // whose inherited layer objects are still owned only locally.
            self.wait_for_local_ancestors(&local).await?;
            let target = match initial_target {
                Some(target) => target,
                None => {
                    let mut target = self.resolver.resolve_for(local.provenance.as_ref()).await?;
                    // Re-resolving may rebuild the write clients, but it must
                    // not replace the compatibility identity captured when this
                    // immutable upload was registered. Base-image ingestion uses a
                    // deliberately different compatibility from runtime snapshots.
                    target.compatibility = target_identity.compatibility.clone();
                    target
                }
            };
            validate_retry_target(&target_identity, &target)?;
            let storage = target.storage.clone();
            let compatibility = target.compatibility.clone();
            let staging_cache = self.local_cache.clone();
            let writer = Arc::clone(&self.publisher);
            let mut plan = tokio::task::spawn_blocking(move || {
                let scratch = |max_bytes| match &staging_cache {
                    Some(cache) => {
                        cache
                            .create_staged_object(max_bytes)
                            .map_err(|error| match error {
                                CacheError::Io(error) => SnapshotUploadError::Data {
                                    retryable: true,
                                    message: error.to_string(),
                                },
                                error if error.is_capacity() => SnapshotUploadError::Data {
                                    retryable: true,
                                    message: error.to_string(),
                                },
                                error => SnapshotUploadError::InvalidPlan(error.to_string()),
                            })
                    }
                    None => crate::snapshot::transport::temporary_staged_object(max_bytes).map_err(
                        |error| SnapshotUploadError::Data {
                            retryable: error.retryable(),
                            message: error.to_string(),
                        },
                    ),
                };
                writer.prepare_upload(local.as_ref(), storage, compatibility, &scratch)
            })
            .await
            .map_err(|error| SnapshotUploadServiceError::Publish {
                message: format!("upload plan worker failed: {error}"),
                retryable: true,
            })??;
            if let Some(cache) = &self.local_cache {
                let mut shutdown = self.shutdown.subscribe();
                if *shutdown.borrow() {
                    return Err(SnapshotUploadServiceError::Closed);
                }
                plan = tokio::select! {
                    biased;
                    _ = shutdown.changed() => return Err(SnapshotUploadServiceError::Closed),
                    persisted = persist_upload_plan(plan, Arc::clone(cache)) => persisted?,
                };
            }
            let chains = plan.metadata_chains()?;
            self.catalog
                .update_instance(&snapshot, |data| {
                    self.catalog
                        .stage_upload_data(&snapshot_id, data, chains.clone())
                })
                .ok_or_else(|| SnapshotUploadServiceError::NotFound(snapshot_id.clone()))?
                .map_err(SnapshotUploadServiceError::IdentityConflict)?;
            let published = self
                .publisher
                .clone()
                .publish(plan, target.metadata_target, target.data_target)
                .await?;
            let snapshot_spec = serde_json::to_value(&published.spec).map_err(|error| {
                SnapshotUploadServiceError::Invalid(format!(
                    "published snapshot spec is not JSON: {error}"
                ))
            })?;
            let spec_checksum = snapshot_spec_checksum(&snapshot_spec)
                .map_err(|error| SnapshotUploadServiceError::Invalid(error.to_string()))?;
            Ok::<_, SnapshotUploadServiceError>((published, snapshot_spec, spec_checksum, chains))
        }
        .await;

        let closed = self.closed.lock().await;
        let completion = self
            .catalog
            .update_instance(&snapshot, |data| {
                if !data.remote_publishing || data.attempt_count != attempt_count {
                    return None;
                }
                let result =
                    result.and_then(|(published, snapshot_spec, spec_checksum, chains)| {
                        self.catalog
                            .commit_remote_data(&snapshot_id, data, chains, &spec_checksum)
                            .map_err(SnapshotUploadServiceError::IdentityConflict)?;
                        Ok((published, snapshot_spec, spec_checksum))
                    });
                let succeeded = result.is_ok();
                let error_code = result.as_ref().err().map(upload_error_code);
                let mut retry = None;
                let mut integrity_violation = false;
                data.remote_publishing = false;
                match result {
                    Ok((published, snapshot_spec, spec_checksum)) => {
                        data.published = Some(published);
                        data.snapshot_spec = Some(snapshot_spec);
                        data.spec_checksum = Some(spec_checksum);
                        data.upload_failure = None;
                        data.retry_at_unix_ms = None;
                    }
                    Err(error) => {
                        integrity_violation = matches!(
                            error,
                            SnapshotUploadServiceError::IdentityConflict(_)
                                | SnapshotUploadServiceError::SourceIntegrity(_)
                        );
                        let failure = failure_from_error(&error);
                        if !failure.retryable {
                            data.fail("upload", failure.message.clone());
                        }
                        if failure.retryable && !*closed {
                            let delay = retry_delay(self.retry_base, self.retry_max, attempt_count);
                            data.retry_at_unix_ms =
                                Some(now_unix_ms().saturating_add(
                                    delay.as_millis().try_into().unwrap_or(u64::MAX),
                                ));
                            data.retry_scheduled = true;
                            retry = Some(delay);
                        }
                        data.upload_failure = Some(failure);
                    }
                }
                Some((succeeded, error_code, integrity_violation, retry))
            })
            .flatten();
        drop(closed);
        let Some((completed_successfully, structured_error, integrity_violation, retry)) =
            completion
        else {
            return;
        };
        if let Some(metrics) = &self.metrics {
            metrics.set_upload_backlog(self.local_ready_count());
            if integrity_violation {
                metrics.add_integrity_violation();
            }
            let mut event = StructuredEvent::new(
                if completed_successfully {
                    StructuredLogLevel::Info
                } else {
                    StructuredLogLevel::Error
                },
                StructuredEventName::UploadCompleted,
                StructuredComponent::Upload,
                if completed_successfully {
                    StructuredOutcome::Success
                } else {
                    StructuredOutcome::Error
                },
            )
            .operation("publish_snapshot")
            .snapshot_id(&snapshot_id);
            if let Some(error_code) = structured_error {
                event = event.error_code(error_code);
            }
            metrics.emit_structured(event);
        }
        self.changed.notify_waiters();
        if let Some(delay) = retry {
            self.spawn_retry(snapshot, attempt_count, delay);
        }
    }

    async fn wait_for_local_ancestors(
        &self,
        local: &Snapshot,
    ) -> Result<(), SnapshotUploadServiceError> {
        let mut shutdown = self.shutdown.subscribe();
        loop {
            let changed = self.changed.notified();
            if *self.closed.lock().await {
                return Err(SnapshotUploadServiceError::Closed);
            }
            let mut ready = true;
            for ancestor in local.lineage().frozen_snapshots() {
                let ancestor_ready = self
                    .update_upload(&ancestor.snapshot_id, |snapshot, data| {
                        if let Some(failure) = &data.source_integrity_failure {
                            return Err(SnapshotUploadServiceError::SourceIntegrity(
                                failure.clone(),
                            ));
                        }
                        if data.state == PublicationState::RemoteReady {
                            return Ok(true);
                        }
                        if !raw_ready(snapshot, data)
                            .is_some_and(|local| Arc::ptr_eq(&local, ancestor))
                        {
                            return Err(SnapshotUploadServiceError::IdentityConflict(
                                "local ancestor upload owner differs from captured lineage".into(),
                            ));
                        }
                        if let Some(failure) = &data.upload_failure {
                            if !failure.retryable {
                                return Err(SnapshotUploadServiceError::Publish {
                                    message: format!(
                                        "ancestor {} failed: {}",
                                        ancestor.snapshot_id, failure.message
                                    ),
                                    retryable: false,
                                });
                            }
                        }
                        Ok(false)
                    })
                    .map_err(|error| match error {
                        SnapshotUploadServiceError::NotFound(_) => {
                            SnapshotUploadServiceError::InvalidState(format!(
                                "local ancestor {} has no upload owner",
                                ancestor.snapshot_id
                            ))
                        }
                        error => error,
                    })?;
                ready &= ancestor_ready;
            }
            if ready {
                return Ok(());
            }
            tokio::select! {
                _ = changed => {},
                _ = shutdown.changed() => return Err(SnapshotUploadServiceError::Closed),
            }
        }
    }

    fn spawn_retry(
        self: &Arc<Self>,
        snapshot: Arc<dyn SnapshotInstance>,
        expected_attempt: u64,
        delay: Duration,
    ) {
        if !self.enabled {
            return;
        }
        let service = Arc::clone(self);
        let mut shutdown = self.shutdown.subscribe();
        tokio::spawn(async move {
            tokio::select! {
                _ = tokio::time::sleep(delay) => {}
                _ = shutdown.changed() => return,
            }
            let closed = service.closed.lock().await;
            if *closed {
                return;
            }
            service.catalog.update_instance(&snapshot, |data| {
                if data.attempt_count == expected_attempt
                    && data.retry_scheduled
                    && !data.remote_publishing
                    && !data.remote_root_committed
                    && data.source_integrity_failure.is_none()
                    && raw_ready(&snapshot, data).is_some()
                {
                    let _ = service.start_attempt_locked(&snapshot, data, None);
                }
            });
        });
    }

    /// Operate on the repository's registered upload without another ID table.
    fn update_upload<T>(
        &self,
        snapshot_id: &str,
        update: impl FnOnce(
            &Arc<dyn SnapshotInstance>,
            &mut SnapshotState,
        ) -> Result<T, SnapshotUploadServiceError>,
    ) -> Result<T, SnapshotUploadServiceError> {
        let snapshot = self
            .catalog
            .get(snapshot_id)
            .ok_or_else(|| SnapshotUploadServiceError::NotFound(snapshot_id.into()))?;
        self.catalog
            .update_instance(&snapshot, |data| {
                if data.upload_identity.is_none() {
                    return Err(SnapshotUploadServiceError::NotFound(snapshot_id.into()));
                }
                update(&snapshot, data)
            })
            .ok_or_else(|| SnapshotUploadServiceError::NotFound(snapshot_id.into()))?
    }

    fn ensure_unregistered(&self, snapshot_id: &str) -> Result<(), SnapshotUploadServiceError> {
        if let Some(snapshot) = self.catalog.get(snapshot_id) {
            if self
                .catalog
                .update_instance(&snapshot, |data| data.upload_identity.is_some())
                .unwrap_or(false)
            {
                return Err(SnapshotUploadServiceError::IdentityConflict(
                    "snapshot ID already owns an upload".into(),
                ));
            }
        }
        Ok(())
    }

    fn local_ready_count(&self) -> u64 {
        self.catalog
            .instances()
            .iter()
            .filter(|snapshot| {
                self.catalog
                    .update_instance(snapshot, |data| {
                        data.upload_identity.is_some() && data.state == PublicationState::LocalReady
                    })
                    .unwrap_or(false)
            })
            .count() as u64
    }
}

// The concrete-type query is intentionally lock-free; Creating instances are
// valid repository reservations but cannot yet supply upload or runtime bytes.
fn raw_ready(snapshot: &Arc<dyn SnapshotInstance>, data: &SnapshotState) -> Option<Arc<Snapshot>> {
    Arc::clone(snapshot)
        .downcast::<Snapshot>()
        .filter(|snapshot| snapshot.has_native() && !data.local_released)
}

fn finalization_source(
    snapshot: &Arc<dyn SnapshotInstance>,
    data: &SnapshotState,
) -> Result<Option<UploadedSnapshotFinalizationSource>, SnapshotUploadServiceError> {
    let Some(local) = raw_ready(snapshot, data) else {
        return Ok(None);
    };
    Ok(Some(UploadedSnapshotFinalizationSource {
        local,
        published: data.published.clone().ok_or_else(|| {
            SnapshotUploadServiceError::Invalid(
                "REMOTE_READY upload has no immutable result".into(),
            )
        })?,
    }))
}

fn upload_error_code(error: &SnapshotUploadServiceError) -> &'static str {
    match error {
        SnapshotUploadServiceError::UploadDisabled => "upload_disabled",
        SnapshotUploadServiceError::SourceIntegrity(_) => "source_integrity",
        SnapshotUploadServiceError::Invalid(_) => "invalid_argument",
        SnapshotUploadServiceError::IdentityConflict(_) => "identity_conflict",
        SnapshotUploadServiceError::InvalidState(_) => "invalid_state",
        SnapshotUploadServiceError::NotFound(_) => "not_found",
        SnapshotUploadServiceError::Closed => "closed",
        SnapshotUploadServiceError::Publish {
            retryable: true, ..
        } => "publish_retryable",
        SnapshotUploadServiceError::Publish {
            retryable: false, ..
        } => "publish_terminal",
    }
}

impl From<SnapshotUploadError> for SnapshotUploadServiceError {
    fn from(error: SnapshotUploadError) -> Self {
        match error {
            SnapshotUploadError::SourceIntegrity(failure) => Self::SourceIntegrity(failure),
            SnapshotUploadError::IdentityConflict(message) => Self::IdentityConflict(message),
            SnapshotUploadError::InvalidPlan(message) => Self::Invalid(message),
            SnapshotUploadError::Store(store) if store.data_integrity.is_some() => {
                Self::SourceIntegrity(store.data_integrity.expect("checked typed cause"))
            }
            SnapshotUploadError::Store(store) => match store.kind {
                PublishFailureKind::IdentityConflict => Self::IdentityConflict(store.message),
                PublishFailureKind::Retryable => Self::Publish {
                    message: store.message,
                    retryable: true,
                },
                PublishFailureKind::Fatal => Self::Publish {
                    message: store.message,
                    retryable: false,
                },
            },
            SnapshotUploadError::Closed => Self::Closed,
            SnapshotUploadError::Worker(message) => Self::Publish {
                message,
                retryable: true,
            },
            SnapshotUploadError::Data { message, retryable } => {
                Self::Publish { message, retryable }
            }
        }
    }
}

fn validate_retry_target(
    expected: &SnapshotUploadTargetIdentity,
    target: &ResolvedSnapshotUploadTarget,
) -> Result<(), SnapshotUploadServiceError> {
    validate_target(target)?;
    if &immutable_target_identity(target) != expected {
        return Err(SnapshotUploadServiceError::IdentityConflict(
            "credential refresh changed immutable storage or VMM compatibility identity".into(),
        ));
    }
    Ok(())
}

fn validate_target(
    target: &ResolvedSnapshotUploadTarget,
) -> Result<(), SnapshotUploadServiceError> {
    if target.storage.endpoint_profile.is_empty()
        || target.storage.credential_ref.is_empty()
        || target.compatibility.architecture.is_empty()
        || target.compatibility.vmm_type.is_empty()
    {
        return Err(SnapshotUploadServiceError::Invalid(
            "resolved upload target is incomplete".into(),
        ));
    }
    if target.metadata_target.namespace() != target.data_target.namespace()
        || target.metadata_target.write_target_identity()
            != target.data_target.write_target_identity()
        || !Arc::ptr_eq(&target.metadata_target.fence(), &target.data_target.fence())
    {
        return Err(SnapshotUploadServiceError::IdentityConflict(
            "resolved upload targets do not share one canonical storage domain".into(),
        ));
    }
    Ok(())
}

fn immutable_target_identity(
    target: &ResolvedSnapshotUploadTarget,
) -> SnapshotUploadTargetIdentity {
    SnapshotUploadTargetIdentity {
        provider: target.storage.provider,
        endpoint_profile: target.storage.endpoint_profile.clone(),
        backend_namespace: target.metadata_target.namespace().to_owned(),
        compatibility: target.compatibility.clone(),
    }
}

fn local_upload_identity(
    local: &Snapshot,
    target: &SnapshotUploadTargetIdentity,
) -> Result<String, SnapshotUploadServiceError> {
    let mut layers = BTreeMap::new();
    for kind in [LayerKind::Memory, LayerKind::Block] {
        let Some(layer) = local.frozen_layers.get(&kind) else {
            continue;
        };
        layers.insert(
            kind.object_key_component(),
            json!({
                "layer_id": layer.layer_id,
                "logical_bytes": layer.logical_bytes,
                "ownership_sha256": sha256_identity(&layer.ownership().packed_states),
                "provisional_content_id": layer.provisional_content_id,
                "unit_size_bytes": layer.unit_size_bytes,
            }),
        );
    }
    let parents = local
        .lineage()
        .frozen_snapshots()
        .map(|parent| &parent.snapshot_id)
        .collect::<Vec<_>>();
    let value = json!({
        "base_parent_chains": local.remote_parent_chains(),
        "compatibility": target.compatibility,
        "layers": layers,
        "parents": parents,
        "provenance": local.provenance,
        "snapshot_id": local.snapshot_id,
        "snapshot_kind": local.snapshot_kind,
        "storage": {
        "namespace": target.backend_namespace,
            "endpoint_profile": target.endpoint_profile,
            "provider": target.provider,
        },
        "vm_state_sha256": local.vm_state.as_ref().map(|state| &state.sha256),
    });
    canonical_json_bytes(&value)
        .map(|payload| sha256_identity(&payload))
        .map_err(|error| SnapshotUploadServiceError::Invalid(error.to_string()))
}

fn validate_finalization(
    record: &SnapshotState,
    spec_checksum: &str,
    metrics: Option<&SnapdMetrics>,
) -> Result<(), SnapshotUploadServiceError> {
    if record.state != PublicationState::RemoteReady {
        return Err(SnapshotUploadServiceError::Invalid(
            "snapshot immutable upload is not REMOTE_READY".into(),
        ));
    }
    if record.spec_checksum.as_deref() != Some(spec_checksum) {
        if let Some(metrics) = metrics {
            metrics.add_integrity_violation();
        }
        return Err(SnapshotUploadServiceError::IdentityConflict(
            "published snapshot spec checksum differs from Admin confirmation".into(),
        ));
    }
    if record.admin_finalized {
        return Err(SnapshotUploadServiceError::Invalid(
            "snapshot publication is already finalized".into(),
        ));
    }
    Ok(())
}

fn status(record: UploadStatusSnapshot) -> Result<SnapshotStatus, SnapshotUploadServiceError> {
    let failure = record.failure.as_ref();
    let layers = record
        .chains
        .iter()
        .map(|chain| {
            let upload_state = if record.state == PublicationState::RemoteReady {
                LayerUploadState::RemoteReady
            } else {
                LayerUploadState::LocalReady
            };
            (
                chain.kind,
                LayerUploadStatus {
                    upload_state,
                    remote_publishing: record.remote_publishing,
                    attempt_count: record.attempt_count,
                    last_error: failure.map(|value| value.message.clone()),
                    retry_at_unix_ms: record.retry_at_unix_ms,
                },
            )
        })
        .collect();
    Ok(SnapshotStatus {
        snapshot_id: record.snapshot_id.clone(),
        state: record.state,
        remote_publishing: record.remote_publishing,
        progress: f64::from(record.state == PublicationState::RemoteReady),
        attempt_count: record.attempt_count,
        last_error: failure.map(|value| value.message.clone()),
        failure_code: failure.map(|value| value.code.clone()),
        retryable: failure.is_some_and(|value| value.retryable),
        retry_at_unix_ms: record.retry_at_unix_ms,
        snapshot_spec: record.snapshot_spec.clone(),
        memory_bytes: record
            .chains
            .iter()
            .find(|chain| chain.kind == LayerKind::Memory)
            .map(|chain| chain.logical_bytes),
        rootfs_bytes: record
            .chains
            .iter()
            .find(|chain| chain.kind == LayerKind::Block)
            .map(|chain| chain.logical_bytes),
        spec_checksum: record.spec_checksum.clone(),
        admin_finalized: record.admin_finalized,
        catalog_pin_held: match &record.local {
            Some(local) => !local.is_closed()?,
            None => false,
        },
        layers,
    })
}

fn failure_from_error(error: &SnapshotUploadServiceError) -> SnapshotUploadFailure {
    let (code, retryable) = match error {
        SnapshotUploadServiceError::UploadDisabled => ("UPLOAD_DISABLED", false),
        SnapshotUploadServiceError::SourceIntegrity(_) => ("SOURCE_INTEGRITY", false),
        SnapshotUploadServiceError::IdentityConflict(_) => ("CONTENT_IDENTITY_CONFLICT", false),
        SnapshotUploadServiceError::InvalidState(_) => ("INVALID_STATE", false),
        SnapshotUploadServiceError::Invalid(_) => ("INVALID_ARGUMENT", false),
        SnapshotUploadServiceError::NotFound(_) => ("NOT_FOUND", false),
        SnapshotUploadServiceError::Closed => ("INVALID_STATE", false),
        SnapshotUploadServiceError::Publish { retryable, .. } => ("INTERNAL", *retryable),
    };
    SnapshotUploadFailure {
        code: code.into(),
        message: error.to_string(),
        retryable,
    }
}

fn retry_delay(base: Duration, maximum: Duration, attempt_count: u64) -> Duration {
    let shift = attempt_count.saturating_sub(1).min(30) as u32;
    base.saturating_mul(1_u32 << shift).min(maximum)
}

fn now_unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .try_into()
        .unwrap_or(u64::MAX)
}

// Encoders already returned pinned, sealed physical objects. The uploader only
// publishes optional graph metadata here; it never copies or re-encodes DATA.
#[cfg(test)]
const SEAL_CHUNK_BYTES: usize = 8 * 1024 * 1024;
#[cfg(test)]
const MAX_SOURCE_READ_BYTES: usize = 16 * 1024 * 1024;

pub(crate) async fn persist_upload_plan(
    plan: SnapshotUploadPlan,
    cache: Arc<LocalFileCache>,
) -> Result<SnapshotUploadPlan, SnapshotUploadServiceError> {
    tokio::task::spawn_blocking(move || {
        plan.validate_shallow()?;
        match persist_plan_metadata(&plan, &cache) {
            Ok(()) => Ok(plan),
            // Graph metadata is re-downloadable after remote publication. A
            // metadata cache miss must not discard pinned DATA upload sources.
            Err(error) if error.is_capacity() => Ok(plan),
            Err(error) => Err(cache_error(error)),
        }
    })
    .await
    .map_err(worker_error)?
}

fn persist_plan_metadata(
    plan: &SnapshotUploadPlan,
    cache: &LocalFileCache,
) -> Result<(), CacheError> {
    let objects = plan
        .layers
        .iter()
        .flat_map(|layer| [&layer.range_map, &layer.committed])
        .chain(plan.chain_indexes.iter())
        .chain(plan.vm_state.iter())
        .chain([&plan.manifest])
        .collect::<Vec<_>>();
    let metadata = objects
        .iter()
        .map(|object| (object.object_key.as_str(), object.sha256.as_str()))
        .collect::<Vec<_>>();
    // Protect both existing and not-yet-written metadata until the local
    // manifest is committed. LRU cannot evict an earlier graph member to
    // make room for a later member and publish an incomplete local graph.
    let _metadata_pin = cache.pin_metadata(&metadata)?;
    for object in objects {
        cache.persist_metadata(&object.object_key, &object.payload, &object.sha256)?;
    }
    Ok(())
}

fn cache_error(error: CacheError) -> SnapshotUploadServiceError {
    if error.is_identity() {
        SnapshotUploadServiceError::IdentityConflict(error.to_string())
    } else {
        SnapshotUploadServiceError::Publish {
            retryable: error.retryable(),
            message: format!("local immutable-object persistence failed: {error}"),
        }
    }
}

fn worker_error(error: tokio::task::JoinError) -> SnapshotUploadServiceError {
    SnapshotUploadServiceError::Publish {
        message: format!("local OverlayBD persistence worker failed: {error}"),
        retryable: true,
    }
}

#[cfg(test)]
mod service_tests {
    use super::*;
    use crate::snapshot::format::overlaybd::zfile::ZfileError;
    use crate::snapshot::format::LayerEncodeError;

    fn encoding_error(error: ZfileError) -> FrozenLayerError {
        FrozenLayerError::from(LayerEncodeError::from(error))
    }

    #[test]
    fn staging_enospc_is_retryable_but_zfile_format_errors_are_fatal() {
        let cases = [
            (
                encoding_error(ZfileError::Io(std::io::Error::from_raw_os_error(28))),
                true,
            ),
            (
                scratch_file_error(CacheError::Io(std::io::Error::from_raw_os_error(28))),
                true,
            ),
            (
                encoding_error(ZfileError::Invalid("invalid header".into())),
                false,
            ),
            (
                encoding_error(ZfileError::Bound("oversized table".into())),
                false,
            ),
            (
                encoding_error(ZfileError::Source("short raw input".into())),
                false,
            ),
        ];
        for (error, retryable) in cases {
            let error = SnapshotUploadServiceError::from(LocalSnapshotError::from(error));
            assert_eq!(error.retryable(), retryable, "{error}");
            if retryable {
                assert!(matches!(error, SnapshotUploadServiceError::Publish { .. }));
            } else {
                assert!(matches!(error, SnapshotUploadServiceError::Invalid(_)));
            }
        }
    }

    struct PreflightProbe;

    #[async_trait]
    impl SnapshotUploadTargetResolver for PreflightProbe {
        async fn resolve(
            &self,
        ) -> Result<ResolvedSnapshotUploadTarget, SnapshotUploadServiceError> {
            Err(SnapshotUploadServiceError::Invalid(
                "preflight reached resolver".into(),
            ))
        }
    }

    fn encoding_fixture() -> Arc<Snapshot> {
        use crate::snapshot::metadata::SnapshotProvenance;
        use crate::snapshot::repository::frozen_layer::{
            create_temporary_zfile, FrozenLayerMetadata, FrozenLayerPin, PackedOwnershipMap,
        };
        use std::os::unix::fs::FileExt;

        let backing = create_temporary_zfile().unwrap();
        backing.set_len(65536).unwrap();
        backing.write_all_at(&vec![0x5a; 65536], 0).unwrap();
        Snapshot::from_base_layer(
            "status-encoding-fixture".into(),
            FrozenLayerPin::new(
                FrozenLayerMetadata {
                    kind: LayerKind::Block,
                    layer_id: "status-encoding-layer".into(),
                    provisional_content_id: sha256_identity(b"status-encoding-layer"),
                    logical_size: 65536,
                    alignment: 65536,
                },
                PackedOwnershipMap {
                    kind: LayerKind::Block,
                    snapshot_id: "status-encoding-fixture".into(),
                    format_version: 1,
                    logical_size: 65536,
                    granularity: 65536,
                    packed_states: vec![1],
                },
                backing.into(),
                false,
            ),
            SnapshotProvenance {
                oci_ref: Some("fixture:status-encoding".into()),
                oci_digest: Some(sha256_identity(b"status-encoding-fixture")),
            },
        )
        .unwrap()
    }

    fn status_fixture_record(
        catalog: &SnapshotRepository,
        local: &Arc<Snapshot>,
    ) -> Arc<dyn SnapshotInstance> {
        let record: Arc<dyn SnapshotInstance> = catalog.register_raw(Arc::clone(local)).unwrap();
        catalog
            .update_instance(&record, |data| {
                data.upload_identity = Some("status-encoding-identity".into());
                data.upload_target = Some(SnapshotUploadTargetIdentity {
                    provider: StorageProvider::S3,
                    endpoint_profile: "fixture".into(),
                    backend_namespace: "fixture".into(),
                    compatibility: VmmCompatibility {
                        architecture: "x86_64".into(),
                        vmm_type: "any".into(),
                        vmm_state_format_version: None,
                        rocklet_artifact: None,
                    },
                });
            })
            .unwrap();
        record
    }

    fn commit_fixture(
        service: &SnapshotUploadService,
        record: &Arc<dyn SnapshotInstance>,
        local: &Snapshot,
    ) -> String {
        let plan = local
            .upload_plan(
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
            )
            .unwrap();
        let chains = plan.metadata_chains().unwrap();
        let published = UploadedSnapshot {
            registry_objects: None,
            spec: plan.spec,
            manifest: serde_json::from_slice(&plan.manifest.payload).unwrap(),
        };
        let snapshot_spec = serde_json::to_value(&published.spec).unwrap();
        let checksum = snapshot_spec_checksum(&snapshot_spec).unwrap();
        // Model a completed immutable write using the real encoded plan and
        // canonical source, rather than an invented checksum or empty spec.
        service
            .catalog
            .update_instance(record, |data| {
                service
                    .catalog
                    .commit_remote_data(local.snapshot_id(), data, chains, &checksum)
                    .unwrap();
                data.published = Some(published);
                data.snapshot_spec = Some(snapshot_spec);
                data.spec_checksum = Some(checksum.clone());
            })
            .unwrap();
        checksum
    }

    fn fixture_service() -> Arc<SnapshotUploadService> {
        SnapshotUploadService::new(
            SnapshotPublisher::new(SnapshotDataUploadConfig::default(), None),
            Arc::new(PreflightProbe),
            Duration::from_millis(1),
            Duration::from_millis(2),
        )
        .unwrap()
    }

    #[tokio::test]
    async fn source_aware_preflight_passes_original_provenance_to_the_resolver() {
        struct SourceProbe;
        #[async_trait]
        impl SnapshotUploadTargetResolver for SourceProbe {
            async fn resolve(
                &self,
            ) -> Result<ResolvedSnapshotUploadTarget, SnapshotUploadServiceError> {
                panic!("source-aware preflight must use resolve_for")
            }

            async fn resolve_for(
                &self,
                provenance: Option<&SnapshotProvenance>,
            ) -> Result<ResolvedSnapshotUploadTarget, SnapshotUploadServiceError> {
                Err(SnapshotUploadServiceError::Invalid(
                    provenance
                        .and_then(|source| source.oci_ref.clone())
                        .unwrap_or_else(|| "missing source".into()),
                ))
            }
        }
        let service = SnapshotUploadService::new(
            SnapshotPublisher::new(SnapshotDataUploadConfig::default(), None),
            Arc::new(SourceProbe),
            Duration::from_millis(1),
            Duration::from_millis(2),
        )
        .unwrap();
        let source = SnapshotProvenance {
            oci_ref: Some("source.test/team/app:v1".into()),
            oci_digest: Some(sha256_identity(b"source1")),
        };
        assert!(matches!(
            service.preflight_for(Some(&source)).await,
            Err(SnapshotUploadServiceError::Invalid(message)) if message == "source.test/team/app:v1"
        ));
        assert!(matches!(
            service.preflight().await,
            Err(SnapshotUploadServiceError::Invalid(message)) if message == "missing source"
        ));
        assert!(matches!(
            fixture_service().preflight_for(Some(&source)).await,
            Err(SnapshotUploadServiceError::Invalid(message)) if message == "preflight reached resolver"
        ));
    }

    #[tokio::test]
    async fn disabled_upload_requests_leave_native_snapshot_ready_without_resolving_or_retrying() {
        use crate::snapshot::transport::registry::{RegistryClient, RegistryWriteTarget};
        use crate::snapshot::transport::NamespaceMutationFence;
        use std::sync::atomic::{AtomicUsize, Ordering};

        struct ResolverProbe(Arc<AtomicUsize>);
        #[async_trait]
        impl SnapshotUploadTargetResolver for ResolverProbe {
            async fn resolve(
                &self,
            ) -> Result<ResolvedSnapshotUploadTarget, SnapshotUploadServiceError> {
                self.0.fetch_add(1, Ordering::Relaxed);
                Err(SnapshotUploadServiceError::Invalid(
                    "disabled resolver was called".into(),
                ))
            }
        }
        let calls = Arc::new(AtomicUsize::new(0));
        let service = SnapshotUploadService::new(
            SnapshotPublisher::new(SnapshotDataUploadConfig::default(), None),
            Arc::new(ResolverProbe(Arc::clone(&calls))),
            Duration::from_millis(1),
            Duration::from_millis(2),
        )
        .unwrap()
        .with_enabled(false);
        let local = encoding_fixture();
        let record: Arc<dyn SnapshotInstance> =
            service.catalog.register_raw(Arc::clone(&local)).unwrap();
        let fence = NamespaceMutationFence::new("registry.invalid/snapshots").unwrap();
        let writer = RegistryWriteTarget::new(
            RegistryClient::new("registry.invalid").unwrap(),
            "snapshots".into(),
            "disabled-test".into(),
            Arc::clone(&fence),
        )
        .unwrap();
        let publication = crate::snapshot::io::oci_publication::RegistrySnapshotPublication::new(
            Arc::clone(&writer),
            None,
            None,
            None,
            "snapshot-".into(),
            SnapshotDataUploadConfig::default(),
        );
        let target = ResolvedSnapshotUploadTarget {
            metadata_target: SnapshotMetadataWriteTarget::new(
                publication.metadata_store().unwrap(),
                "disabled-test".into(),
                Arc::clone(&fence),
            )
            .unwrap(),
            data_target: DataPackWriteTarget::new(writer, "disabled-test".into(), fence).unwrap(),
            storage: StorageDescriptor {
                provider: StorageProvider::Registry,
                endpoint_profile: "registry".into(),
                credential_ref: "test:v1".into(),
            },
            compatibility: VmmCompatibility {
                architecture: "x86_64".into(),
                vmm_type: "cloud-hypervisor".into(),
                vmm_state_format_version: Some(1),
                rocklet_artifact: None,
            },
        };
        assert!(matches!(
            service.preflight().await,
            Err(SnapshotUploadServiceError::UploadDisabled)
        ));
        assert!(matches!(
            service
                .register(
                    Arc::clone(&local),
                    SnapshotPublication {
                        snapshot_id: local.snapshot_id.clone()
                    },
                    target
                )
                .await,
            Err(SnapshotUploadServiceError::UploadDisabled)
        ));
        assert!(matches!(
            service.retry(local.snapshot_id()).await,
            Err(SnapshotUploadServiceError::UploadDisabled)
        ));
        service
            .catalog
            .update_instance(&record, |data| {
                assert!(matches!(
                    service.start_attempt_locked(&record, data, None),
                    Err(SnapshotUploadServiceError::UploadDisabled)
                ));
                assert_eq!(data.state, PublicationState::LocalReady);
                assert!(data.upload_identity.is_none());
                assert!(data.upload_failure.is_none());
                assert!(!data.remote_publishing);
                assert!(!data.retry_scheduled);
                assert!(data.retry_at_unix_ms.is_none());
                assert_eq!(data.attempt_count, 0);
            })
            .unwrap();
        service.spawn_retry(Arc::clone(&record), 0, Duration::ZERO);
        assert_eq!(calls.load(Ordering::Relaxed), 0);
        assert!(local.native_is_locally_ready());
        assert!(!local.is_closed().unwrap());
        let error = SnapshotUploadServiceError::UploadDisabled;
        assert!(!error.retryable());
        assert_eq!(failure_from_error(&error).code, "UPLOAD_DISABLED");
        service.close().await.unwrap();
        assert!(!local.is_closed().unwrap());
    }

    /// Waiting for the captured Arc owner to drop proves the timer has exited;
    /// unlike a fixed sleep this also waits for a mistakenly launched worker.
    async fn wait_for_record_owners(
        record: &std::sync::Weak<dyn SnapshotInstance>,
        expected: usize,
    ) {
        tokio::time::timeout(Duration::from_secs(2), async {
            while record.strong_count() != expected {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("retry timer or its worker retained the snapshot record");
    }

    #[tokio::test]
    async fn finalization_rejects_a_second_request_after_releasing_the_upload_pin() {
        let local = encoding_fixture();
        let service = fixture_service();
        let record = status_fixture_record(&service.catalog, &local);
        let checksum = commit_fixture(&service, &record, &local);

        let first = service
            .finalize_published(&local.snapshot_id, &checksum)
            .await
            .unwrap();
        assert_eq!(first.released_pins, 1);
        assert!(local.is_closed().unwrap());
        assert!(
            service
                .status(&local.snapshot_id)
                .await
                .unwrap()
                .admin_finalized
        );
        assert!(record.lifecycle().lock().unwrap().local_released);
        assert!(matches!(
            service.finalize_published(&local.snapshot_id, &checksum).await,
            Err(SnapshotUploadServiceError::Invalid(message))
                if message == "snapshot publication is already finalized"
        ));
        service.close().await.unwrap();
    }

    #[tokio::test]
    async fn finalization_preserves_snapshot_identity_readers_and_timer_fence() {
        let local = encoding_fixture();
        let service = fixture_service();
        let record = status_fixture_record(&service.catalog, &local);
        let checksum = commit_fixture(&service, &record, &local);
        let raw_weak = Arc::downgrade(&local);
        let identity = local.frozen_layers[&LayerKind::Block]
            .provisional_content_id
            .clone();
        let reader = local.layer_reader(LayerKind::Block, &identity).unwrap();
        let backing = local.backing_handle(LayerKind::Block).unwrap();
        let native_pin = backing
            .backing
            .restore_and_pin_native_reader()
            .await
            .unwrap();
        let direct_reader = native_pin.file().try_clone().unwrap();
        service
            .finalize_published(local.snapshot_id(), &checksum)
            .await
            .unwrap();
        assert!(local.native_is_locally_ready());
        let peers = crate::snapshot::services::p2p::P2PSnapshotServeRegistry::new();
        let metadata = peers.register_native(&local).await.unwrap();
        let layer = &metadata.layers[0];
        assert_eq!(
            peers
                .read_native_range(
                    local.snapshot_id(),
                    &crate::snapshot::transport::p2p::native::NativeReadRangeRequest {
                        generation_id: metadata.generation_id.clone(),
                        kind: layer.kind,
                        ordinal: layer.ordinal,
                        layer_id: layer.layer_id.clone(),
                        layer_generation_id: layer.layer_generation_id.clone(),
                        offset: 3,
                        length: 11,
                    },
                )
                .await
                .unwrap(),
            [0x5a; 11]
        );
        let current = service.catalog.get(local.snapshot_id()).unwrap();
        assert!(Arc::ptr_eq(&current, &record));
        assert!(Arc::ptr_eq(
            &Arc::clone(&current).downcast::<Snapshot>().unwrap(),
            &local
        ));
        assert!(current.spec().is_some());
        let before = service.status(local.snapshot_id()).await.unwrap();

        let weak = Arc::downgrade(&record);
        let owners = weak.strong_count();
        service.spawn_retry(Arc::clone(&record), 1, Duration::from_millis(1));
        wait_for_record_owners(&weak, owners).await;
        assert_eq!(service.status(local.snapshot_id()).await.unwrap(), before);

        let snapshot_id = local.snapshot_id().to_owned();
        drop(record);
        drop(local);
        assert!(
            raw_weak.upgrade().is_some(),
            "repository must retain the same unified snapshot identity"
        );
        // Direct descriptors remain safe for the explicit reader lease's
        // lifetime. Closing the upload capability cannot reclaim this inode.
        assert!(backing.backing.reclaim_unused().await.unwrap().is_empty());
        let mut direct_payload = vec![0; 65536];
        direct_reader.read_exact_at(&mut direct_payload, 0).unwrap();
        assert_eq!(direct_payload, vec![0x5a; 65536]);
        drop(direct_reader);
        drop(native_pin);
        backing.backing.reclaim_unused().await.unwrap();
        assert!(backing.backing.has_reclaimed_data());
        let context = crate::snapshot::metadata::LazyLoadContext::new(
            crate::snapshot::io::range_io::RangeIO::for_fixed_targets(),
            crate::RuntimeIdentity::new("finalized-reader"),
            None,
        );
        let payload = reader
            .read_data_range(
                &context,
                crate::snapshot::metadata::SnapshotReadRange {
                    kind: LayerKind::Block,
                    layer_content_sha256: &identity,
                    logical_offset: 0,
                    length: 65536,
                },
            )
            .await
            .unwrap()
            .payload;
        assert_eq!(payload, vec![0x5a; 65536]);
        assert!(service.status(&snapshot_id).await.unwrap().admin_finalized);
        service.close().await.unwrap();
    }

    #[tokio::test]
    async fn source_integrity_quarantine_rejects_upload_retry_without_releasing_raw() {
        let local = encoding_fixture();
        let service = fixture_service();
        let record = status_fixture_record(&service.catalog, &local);
        let directory = tempfile::tempdir().unwrap();
        let cache = LocalFileCache::new(directory.path().to_owned(), "source-quarantine").unwrap();
        let backing = local.backing_handle(LayerKind::Block).unwrap();
        let alias = backing.backing.try_clone_file().unwrap();
        let error = local
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
                    alias.write_all_at(&[0x6b], 17).unwrap();
                    cache
                        .create_staged_object(capacity)
                        .map_err(scratch_file_error)
                },
            )
            .unwrap_err();
        assert!(matches!(error, LocalSnapshotError::SourceIntegrity(_)));
        let error = SnapshotUploadServiceError::from(error);
        assert!(!error.retryable());
        assert_eq!(failure_from_error(&error).code, "SOURCE_INTEGRITY");
        let before = service.status(local.snapshot_id()).await.unwrap();
        assert_eq!(before.state, PublicationState::Failed);
        assert!(!before.catalog_pin_held);
        assert!(matches!(
            service.retry(local.snapshot_id()).await,
            Err(SnapshotUploadServiceError::SourceIntegrity(_))
        ));
        // A pending retry and a direct resume cannot clear sticky source health.
        record.lifecycle().lock().unwrap().resume();
        let weak = Arc::downgrade(&record);
        let owners = weak.strong_count();
        service.spawn_retry(Arc::clone(&record), 0, Duration::from_millis(1));
        wait_for_record_owners(&weak, owners).await;
        assert_eq!(service.status(local.snapshot_id()).await.unwrap(), before);
        assert!(service.catalog.register_raw(Arc::clone(&local)).is_err());
        assert_eq!(service.status(local.snapshot_id()).await.unwrap(), before);
        assert!(Arc::ptr_eq(
            &service.catalog.get(local.snapshot_id()).unwrap(),
            &record
        ));
        assert!(backing.backing.reclaim_unused().await.unwrap().is_empty());
        let mut byte = [0];
        alias.read_exact_at(&mut byte, 17).unwrap();
        assert_eq!(byte, [0x6b]);
        assert_eq!(alias.metadata().unwrap().len(), 65536);
        assert!(Snapshot::creating(local.snapshot_id(), local.snapshot_kind)
            .initialize_from(&local)
            .is_err());
        service.close().await.unwrap();
    }

    #[tokio::test]
    async fn failed_rehome_keeps_finalization_retryable_after_releasing_its_pin() {
        let local = encoding_fixture();
        let metrics = SnapdMetrics::new("s3").unwrap();
        let service = SnapshotUploadService::new_with_metrics(
            SnapshotPublisher::new(SnapshotDataUploadConfig::default(), None),
            Arc::new(PreflightProbe),
            Duration::from_millis(1),
            Duration::from_millis(2),
            Some(Arc::clone(&metrics)),
        )
        .unwrap();
        let record = status_fixture_record(&service.catalog, &local);
        metrics.add_lease(LeaseKind::Upload, 1);
        let checksum = commit_fixture(&service, &record, &local);
        let published = service
            .catalog
            .update_instance(&record, |data| {
                let published = data.published.clone().unwrap();
                data.published.as_mut().unwrap().manifest.snapshot_id =
                    "conflicting-publication".into();
                published
            })
            .unwrap();
        assert!(matches!(
            service
                .finalize_published(local.snapshot_id(), &checksum)
                .await,
            Err(SnapshotUploadServiceError::IdentityConflict(_))
        ));
        assert!(local.is_closed().unwrap());
        assert!(metrics
            .render(false)
            .contains("rock_snapd_leases{kind=\"upload\"} 0"));
        service
            .catalog
            .update_instance(&record, |data| {
                assert!(!data.admin_finalized);
                assert!(!data.local_released);
                data.published = Some(published);
            })
            .unwrap();
        let retry = service
            .finalize_published(local.snapshot_id(), &checksum)
            .await
            .unwrap();
        assert_eq!(retry.released_pins, 0);
        assert!(
            service
                .status(local.snapshot_id())
                .await
                .unwrap()
                .admin_finalized
        );
        assert!(metrics
            .render(false)
            .contains("rock_snapd_leases{kind=\"upload\"} 0"));
        service.close().await.unwrap();
    }

    #[tokio::test]
    async fn finalization_does_not_report_success_for_an_absent_upload() {
        let service = SnapshotUploadService::new(
            SnapshotPublisher::new(SnapshotDataUploadConfig::default(), None),
            Arc::new(PreflightProbe),
            Duration::from_millis(1),
            Duration::from_millis(2),
        )
        .unwrap();
        assert!(matches!(
            service.finalize_published("absent", &sha256_identity(b"absent")).await,
            Err(SnapshotUploadServiceError::NotFound(identity)) if identity == "absent"
        ));
    }

    #[test]
    fn status_projection_keeps_publication_and_spec_from_one_service_snapshot() {
        let local = encoding_fixture();
        let catalog = SnapshotRepository::new();
        let record = status_fixture_record(&catalog, &local);
        let before_publication = catalog
            .update_instance(&record, |data| {
                UploadStatusSnapshot::from_instance(&record, data)
            })
            .unwrap();

        // Model a background publication commit after the record lock was
        // released but before the response inspects its local pin.
        let checksum = sha256_identity(b"status-publication-root");
        let after_publication = catalog
            .update_instance(&record, |data| {
                let chains = data.chains.clone();
                catalog
                    .commit_remote_data(&local.snapshot_id, data, chains, &checksum)
                    .unwrap();
                data.snapshot_spec = Some(json!({"fixture": "committed"}));
                data.spec_checksum = Some(checksum.clone());
                UploadStatusSnapshot::from_instance(&record, data)
            })
            .unwrap();

        let before = status(before_publication).unwrap();
        assert_eq!(before.state, PublicationState::LocalReady);
        assert!(before.snapshot_spec.is_none());
        assert!(before.spec_checksum.is_none());
        assert_eq!(before.progress, 0.0);
        assert!(before
            .layers
            .values()
            .all(|layer| layer.upload_state == LayerUploadState::LocalReady));

        let after = status(after_publication).unwrap();
        assert_eq!(after.state, PublicationState::RemoteReady);
        assert_eq!(after.snapshot_spec, Some(json!({"fixture": "committed"})));
        assert_eq!(after.spec_checksum.as_deref(), Some(checksum.as_str()));
        assert_eq!(after.progress, 1.0);
        assert!(after
            .layers
            .values()
            .all(|layer| layer.upload_state == LayerUploadState::RemoteReady));
    }

    /// The encoder is deliberately held at scratch creation, before it can
    /// finish its expensive work. A status request on that snapshot must not
    /// retain the service-wide lock needed by the next capture's preflight.
    #[tokio::test(flavor = "current_thread")]
    async fn status_during_encoding_does_not_block_new_capture_preflight() {
        use crate::snapshot::io::snapshot_io::SnapshotDataUploadConfig;
        use crate::snapshot::repository::frozen_layer::create_temporary_staged_object;
        use std::sync::atomic::{AtomicBool, Ordering};
        use std::sync::{mpsc, Barrier};
        use std::time::Instant;

        let local = encoding_fixture();
        let publisher = SnapshotPublisher::new(SnapshotDataUploadConfig::default(), None);
        let service = SnapshotUploadService::new(
            publisher,
            Arc::new(PreflightProbe),
            Duration::from_millis(1),
            Duration::from_millis(2),
        )
        .unwrap();
        let compatibility = VmmCompatibility {
            architecture: "x86_64".into(),
            vmm_type: "any".into(),
            vmm_state_format_version: None,
            rocklet_artifact: None,
        };
        status_fixture_record(service.catalog.as_ref(), &local);

        let release_encoder = Arc::new(Barrier::new(2));
        let release_in_encoder = Arc::clone(&release_encoder);
        let (entered_tx, entered_rx) = mpsc::channel();
        let local_in_encoder = Arc::clone(&local);
        let encoder = std::thread::spawn(move || {
            let first = AtomicBool::new(true);
            local_in_encoder.upload_plan_with_scratch(
                StorageDescriptor {
                    provider: StorageProvider::S3,
                    endpoint_profile: "fixture".into(),
                    credential_ref: "fixture:v1".into(),
                },
                compatibility,
                &|max_bytes| {
                    if first.swap(false, Ordering::Relaxed) {
                        entered_tx.send(()).unwrap();
                        release_in_encoder.wait();
                    }
                    create_temporary_staged_object(max_bytes)
                },
            )
        });
        entered_rx.recv_timeout(Duration::from_secs(5)).unwrap();

        let owners_before_status = Arc::strong_count(&local);
        let status_service = Arc::clone(&service);
        let snapshot_id = local.snapshot_id.clone();
        let (status_started_tx, status_started_rx) = mpsc::channel();
        let (status_done_tx, status_done_rx) = mpsc::channel();
        let status_thread = std::thread::spawn(move || {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .unwrap();
            status_started_tx.send(()).unwrap();
            let result = runtime.block_on(status_service.status(&snapshot_id));
            status_done_tx.send(()).unwrap();
            result
        });
        status_started_rx
            .recv_timeout(Duration::from_secs(5))
            .unwrap();
        let observed_until = Instant::now() + Duration::from_secs(1);
        let mut observed_status = false;
        while Instant::now() < observed_until {
            if status_done_rx.try_recv().is_ok()
                || Arc::strong_count(&local) > owners_before_status
                || service.closed.try_lock().is_err()
            {
                observed_status = true;
                break;
            }
            tokio::task::yield_now().await;
            std::thread::yield_now();
        }
        let preflight = tokio::time::timeout(Duration::from_millis(200), service.preflight()).await;
        // Always release/join before assertions, including on the regressing
        // implementation, so an expected timeout cannot strand a worker.
        release_encoder.wait();
        encoder.join().unwrap().unwrap();
        let status = status_thread.join().unwrap().unwrap();
        service.close().await.unwrap();

        assert!(
            observed_status,
            "status worker did not enter its projection"
        );
        assert_eq!(status.state, PublicationState::LocalReady);
        assert!(
            matches!(preflight, Ok(Err(SnapshotUploadServiceError::Invalid(message))) if message == "preflight reached resolver"),
            "another snapshot's preflight was blocked by status/encoding"
        );
    }

    #[tokio::test]
    async fn capture_only_record_is_not_an_upload_and_keeps_its_pin_on_close() {
        let service = fixture_service();
        let local = encoding_fixture();
        let record = service.catalog.register_raw(Arc::clone(&local)).unwrap();
        let checksum = sha256_identity(b"capture-only-root");

        assert!(matches!(
            service.status(&local.snapshot_id).await,
            Err(SnapshotUploadServiceError::NotFound(identity)) if identity == local.snapshot_id
        ));
        assert!(matches!(
            service.finalize_published(&local.snapshot_id, &checksum).await,
            Err(SnapshotUploadServiceError::NotFound(identity)) if identity == local.snapshot_id
        ));
        assert_eq!(service.local_ready_count(), 0);

        service.close().await.unwrap();

        assert!(!local.is_closed().unwrap());
        assert!(Arc::ptr_eq(&record, &local));
        assert_eq!(record.state(), PublicationState::LocalReady);
        assert!(matches!(
            service.status(&local.snapshot_id).await,
            Err(SnapshotUploadServiceError::NotFound(identity)) if identity == local.snapshot_id
        ));
    }

    #[tokio::test]
    async fn retry_timer_cannot_mutate_a_replacement_with_the_same_id_and_identity() {
        let service = fixture_service();
        let old_local = encoding_fixture();
        let old = status_fixture_record(&service.catalog, &old_local);
        service
            .catalog
            .update_instance(&old, |data| {
                data.attempt_count = 1;
                data.retry_scheduled = true;
            })
            .unwrap();

        // Both the old timer and any new attempt need this lifecycle gate.
        // Replace the record before either can inspect the current repository.
        let gate = service.closed.lock().await;
        let old_weak = Arc::downgrade(&old);
        service.spawn_retry(old, 1, Duration::from_millis(1));
        service.catalog.invalidate(&old_local.snapshot_id);
        let new_local = encoding_fixture();
        let replacement = status_fixture_record(&service.catalog, &new_local);
        service
            .catalog
            .update_instance(&replacement, |data| {
                data.attempt_count = 1;
                data.retry_scheduled = true;
            })
            .unwrap();
        drop(gate);

        // The raw fixture and instance share one allocation; release the
        // fixture owner so reaching zero still proves the old task drained.
        drop(old_local);
        wait_for_record_owners(&old_weak, 0).await;
        service
            .catalog
            .update_instance(&replacement, |data| {
                assert_eq!(
                    data.upload_identity.as_deref(),
                    Some("status-encoding-identity")
                );
                assert_eq!(data.attempt_count, 1);
                assert!(data.retry_scheduled);
                assert!(!data.remote_publishing);
                assert!(data.upload_failure.is_none());
                assert_eq!(data.state, PublicationState::LocalReady);
            })
            .unwrap();

        // A timer for the actual owner must run. The resolver deliberately
        // produces a permanent error so this positive control cannot recurse.
        let weak = Arc::downgrade(&replacement);
        let owners = weak.strong_count();
        service.spawn_retry(Arc::clone(&replacement), 1, Duration::from_millis(1));
        wait_for_record_owners(&weak, owners).await;
        let current = service.status(&new_local.snapshot_id).await.unwrap();
        assert_eq!(current.attempt_count, 2);
        assert_eq!(current.state, PublicationState::Failed);
        assert!(!current.remote_publishing);
        assert!(!current.retryable);
        assert!(current
            .last_error
            .unwrap()
            .contains("preflight reached resolver"));
        service.close().await.unwrap();
    }

    #[tokio::test]
    async fn retry_timer_from_an_older_attempt_cannot_claim_the_current_retry() {
        let service = fixture_service();
        let local = encoding_fixture();
        let record = status_fixture_record(&service.catalog, &local);
        service
            .catalog
            .update_instance(&record, |data| {
                data.attempt_count = 1;
                data.retry_scheduled = true;
            })
            .unwrap();

        let gate = service.closed.lock().await;
        let weak = Arc::downgrade(&record);
        let owners = weak.strong_count();
        service.spawn_retry(Arc::clone(&record), 1, Duration::from_millis(1));
        // Model an explicit retry and its subsequent retryable failure while
        // the first attempt's timer is still waiting for the lifecycle gate.
        service
            .catalog
            .update_instance(&record, |data| {
                data.attempt_count = 2;
                data.retry_scheduled = true;
                data.retry_at_unix_ms = Some(now_unix_ms().saturating_add(60_000));
                data.upload_failure = Some(SnapshotUploadFailure {
                    code: "INTERNAL".into(),
                    message: "second attempt failed temporarily".into(),
                    retryable: true,
                });
            })
            .unwrap();
        drop(gate);

        wait_for_record_owners(&weak, owners).await;
        service
            .catalog
            .update_instance(&record, |data| {
                assert_eq!(data.attempt_count, 2);
                assert!(data.retry_scheduled);
                assert!(!data.remote_publishing);
                assert_eq!(
                    data.upload_failure.as_ref().unwrap().message,
                    "second attempt failed temporarily",
                );
            })
            .unwrap();

        service.spawn_retry(Arc::clone(&record), 2, Duration::from_millis(1));
        wait_for_record_owners(&weak, owners).await;
        let current = service.status(&local.snapshot_id).await.unwrap();
        assert_eq!(current.attempt_count, 3);
        assert_eq!(current.state, PublicationState::Failed);
        assert!(!current.remote_publishing);
        assert!(!current.retryable);
        assert!(current
            .last_error
            .unwrap()
            .contains("preflight reached resolver"));
        service.close().await.unwrap();
    }
}

#[cfg(test)]
mod local_persistence_tests {
    use super::*;
    use std::path::PathBuf;

    use serde_json::json;

    use crate::snapshot::format::overlaybd::{encode_descriptor, plan_layer};
    use crate::snapshot::format::overlaybd_s3::data::{
        DataPackIdentity, DataPackObjectKey, DataPackSliceIdentity, DataPackUploadRequest,
    };
    use crate::snapshot::format::ImmutableObject;
    use crate::snapshot::metadata::{
        canonical_json_bytes, compute_layer_content_sha256, sha256_identity, ChainIndexRef,
        DataCodec, DataSource, LayerKind, LayerRange, LayerRangeMap, ManifestChainRef, ObjectRef,
        RangeKind, RockletArtifactCompatibility, SnapshotKind, SnapshotManifest, SnapshotSpec,
        StorageDescriptor, StorageProvider, VmStateRef, VmmCompatibility,
    };
    use crate::snapshot::transport::SealedObject;

    struct TestRoot(PathBuf);

    impl TestRoot {
        fn new() -> Self {
            Self(std::env::temp_dir().join(format!(
                "rock-local-persistence-{}",
                uuid::Uuid::new_v4().simple()
            )))
        }

        fn cache(&self) -> Arc<LocalFileCache> {
            LocalFileCache::new(self.0.clone(), "fixture-domain").unwrap()
        }
    }

    impl Drop for TestRoot {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    struct FixtureSealedSource {
        object: Arc<dyn SealedObject>,
    }

    impl DataPackSource for FixtureSealedSource {
        fn length(&self) -> u64 {
            self.object.length()
        }

        fn mark_remote_committed(&self) {
            self.object.mark_remote_committed();
        }

        fn read_at<'a>(
            &'a self,
            offset: u64,
            max_bytes: usize,
        ) -> Pin<Box<dyn Future<Output = Result<Vec<u8>, DataPackSourceError>> + Send + 'a>>
        {
            Box::pin(async move {
                if offset > self.length() || max_bytes > MAX_SOURCE_READ_BYTES {
                    return Err(DataPackSourceError::InvalidRange);
                }
                self.object
                    .read_at(offset, max_bytes)
                    .await
                    .map_err(|error| DataPackSourceError::Io(error.to_string()))
            })
        }
    }

    fn layer(cache: &Arc<LocalFileCache>, kind: LayerKind) -> LayerUploadPlan {
        layer_with_data(cache, kind, false).unwrap()
    }

    fn layer_with_data(
        cache: &Arc<LocalFileCache>,
        kind: LayerKind,
        data: bool,
    ) -> Result<LayerUploadPlan, SnapshotUploadServiceError> {
        let unit = match kind {
            LayerKind::Memory => 4096,
            LayerKind::Block => 65536,
        };
        let raw = vec![0x5a; unit as usize];
        let mut layer = LayerRangeMap {
            registry_objects: None,
            registry: None,

            zfile: None,
            format_version: 1,
            kind,
            content_sha256: format!("sha256:{}", "0".repeat(64)),
            logical_bytes: unit,
            unit_size_bytes: unit,
            ranges: vec![LayerRange {
                kind: if data {
                    RangeKind::Data
                } else {
                    RangeKind::Zero
                },
                logical_offset: 0,
                length: unit,
                source: data.then(|| DataSource {
                    object_key: "fixture-native-source".into(),
                    object_sha256: format!("sha256:{}", "0".repeat(64)),
                    object_version: None,
                    pack_offset: 0,
                    encoded_length: unit,
                    decoded_length: unit,
                    codec: DataCodec::Raw,
                    checksum_sha256: sha256_identity(&raw),
                }),
            }],
        };
        layer.content_sha256 = compute_layer_content_sha256(&layer).unwrap();
        let plan = plan_layer(&layer).unwrap();
        let source = if data {
            let path = std::env::temp_dir()
                .join(format!("rock-persistence-data-{}", uuid::Uuid::new_v4()));
            let file = File::options()
                .read(true)
                .write(true)
                .create_new(true)
                .open(&path)
                .unwrap();
            std::fs::remove_file(path).unwrap();
            file.write_all_at(&raw, 0).unwrap();
            file
        } else {
            File::open("/dev/null").unwrap()
        };
        let payload = plan.read_at(&source, 0, SEAL_CHUNK_BYTES).unwrap();
        let hash = sha256_identity(&payload);
        let prefix = format!(
            "layers/{}/1/{}/",
            kind.object_key_component(),
            &layer.content_sha256[7..]
        );
        let blob = ObjectRef {
            object_key: format!("{prefix}data/{}.commit", &hash[7..]),
            sha256: hash,
            object_version: None,
        };
        let descriptor = ImmutableObject::new(
            format!("{prefix}overlaybd.json"),
            encode_descriptor(&layer, &plan, &blob).unwrap(),
        )
        .unwrap();
        let marker = ImmutableObject::new(
            format!("{prefix}COMMITTED.overlaybd"),
            canonical_json_bytes(&json!({
                "content_sha256":layer.content_sha256,
                "format_version":1,
                "range_map_sha256":descriptor.sha256,
                "kind":kind,
            }))
            .unwrap(),
        )
        .unwrap();
        // The fixture follows the encoder contract: seal physical bytes into
        // the transport before constructing an upload plan. The native input
        // FD is closed here, so all later reads must use the sealed handle.
        let physical_identity = ImmutableObjectIdentity {
            object_key: blob.object_key.clone(),
            sha256: blob.sha256.clone(),
            length: payload.len() as u64,
        };
        let validator =
            OverlaybdObjectValidator::from_descriptor(&descriptor.payload).map_err(|error| {
                SnapshotUploadServiceError::Publish {
                    retryable: error.retryable(),
                    message: error.to_string(),
                }
            })?;
        let staged = cache
            .create_staged_object(physical_identity.length)
            .map_err(cache_error)?;
        staged
            .write_all_at(&payload, 0)
            .map_err(|error| cache_error(CacheError::Io(error)))?;
        let object = staged
            .seal(&physical_identity, validator.as_ref())
            .map_err(|error| SnapshotUploadServiceError::Publish {
                retryable: error.retryable(),
                message: error.to_string(),
            })?;
        drop(source);
        let request = DataPackUploadRequest {
            zfile: None,
            identity: DataPackIdentity {
                object_key: DataPackObjectKey::new(blob.object_key).unwrap(),
                expected_sha256: blob.sha256,
                length: payload.len() as u64,
            },
            source: Arc::new(FixtureSealedSource { object }),
            slices: [
                (0, plan.header.as_slice()),
                (plan.index_offset, plan.index.as_slice()),
                (
                    plan.stored_bytes - plan.trailer.len() as u64,
                    plan.trailer.as_slice(),
                ),
            ]
            .into_iter()
            .chain(data.then(|| (plan.data[0].blob_offset, raw.as_slice())))
            .filter(|(_, payload)| !payload.is_empty())
            .map(|(offset, payload)| DataPackSliceIdentity {
                offset,
                length: payload.len() as u64,
                expected_sha256: sha256_identity(payload),
            })
            .collect(),
        };
        Ok(LayerUploadPlan::from_requests(
            kind,
            1,
            layer.content_sha256,
            vec![request],
            descriptor,
            marker,
        )
        .unwrap())
    }

    fn fixture(cache: &Arc<LocalFileCache>, kind: SnapshotKind) -> SnapshotUploadPlan {
        fixture_with_data(cache, kind, false)
    }

    fn fixture_with_data(
        cache: &Arc<LocalFileCache>,
        kind: SnapshotKind,
        block_data: bool,
    ) -> SnapshotUploadPlan {
        let snapshot_id = "local-persistence-fixture".to_owned();
        let mut layers = Vec::new();
        if kind == SnapshotKind::Full {
            let layer = layer(cache, LayerKind::Memory);
            layers.push(layer);
        }
        let layer = layer_with_data(cache, LayerKind::Block, block_data).unwrap();
        layers.push(layer);
        let chain_indexes = layers
            .iter()
            .map(|layer| {
                let logical_bytes = match layer.kind {
                    LayerKind::Memory => 4096,
                    LayerKind::Block => 65536,
                };
                let name = match layer.kind {
                    LayerKind::Memory => "memory",
                    LayerKind::Block => "rootfs",
                };
                ImmutableObject::new(
                    format!("snapshots/{snapshot_id}/{name}.index"),
                    canonical_json_bytes(&json!({
                        "format_version":1,
                        "kind":layer.kind,
                        "logical_bytes":logical_bytes,
                        "unit_size_bytes":logical_bytes,
                        "layers":[{
                            "ordinal":0,"layer_id":name,"format_version":1,
                            "content_sha256":layer.content_sha256,
                            "range_map":{
                                "object_key":layer.range_map.object_key,
                                "sha256":layer.range_map.sha256,
                                "encoding":"overlaybd-lsmt-v1",
                            },
                        }],
                    }))
                    .unwrap(),
                )
                .unwrap()
            })
            .collect::<Vec<_>>();
        let chain_ref = |object: &ImmutableObject, logical_bytes| ChainIndexRef {
            source_ref: None,
            logical_bytes,
            index_object_key: object.object_key.clone(),
            index_sha256: object.sha256.clone(),
            object_version: None,
        };
        let rootfs = chain_ref(chain_indexes.last().unwrap(), 65536);
        let memory = (kind == SnapshotKind::Full).then(|| chain_ref(&chain_indexes[0], 4096));
        let vm_state = (kind == SnapshotKind::Full).then(|| {
            ImmutableObject::new(
                format!("snapshots/{snapshot_id}/vm-state.pack"),
                b"opaque-vmm-state".to_vec(),
            )
            .unwrap()
        });
        let vm_ref = vm_state.as_ref().map(|object| VmStateRef {
            object_key: object.object_key.clone(),
            sha256: object.sha256.clone(),
            object_version: None,
            stored_bytes: object.payload.len() as u64,
        });
        let manifest_ref = |chain: &ChainIndexRef| ManifestChainRef {
            object_key: chain.index_object_key.clone(),
            sha256: chain.index_sha256.clone(),
            logical_bytes: chain.logical_bytes,
            object_version: None,
        };
        let manifest = ImmutableObject::new(
            format!("snapshots/{snapshot_id}/manifest.json"),
            canonical_json_bytes(&SnapshotManifest {
                format_version: 1,
                snapshot_id: snapshot_id.clone(),
                snapshot_kind: kind,
                vm_state: vm_ref.clone(),
                memory: memory.as_ref().map(manifest_ref),
                rootfs: manifest_ref(&rootfs),
            })
            .unwrap(),
        )
        .unwrap();
        let spec = SnapshotSpec {
            registry_snapshot: None,
            format_version: 1,
            compatibility: VmmCompatibility {
                architecture: "x86_64".into(),
                vmm_type: "cloud-hypervisor".into(),
                vmm_state_format_version: vm_ref.as_ref().map(|_| 1),
                rocklet_artifact: vm_ref.as_ref().map(|_| RockletArtifactCompatibility {
                    identity: sha256_identity(b"rocklet"),
                    version: "fixture-v1".into(),
                    raw_sha256: sha256_identity(b"rocklet"),
                    logical_bytes: 512,
                    guest_port: 22555,
                }),
            },
            provenance: None,
            storage: StorageDescriptor {
                provider: StorageProvider::S3,
                endpoint_profile: "fixture".into(),
                credential_ref: "fixture:v1".into(),
            },
            manifest: ObjectRef {
                object_key: manifest.object_key.clone(),
                sha256: manifest.sha256.clone(),
                object_version: None,
            },
            vm_state: vm_ref,
            memory,
            rootfs,
        };
        let plan = SnapshotUploadPlan {
            registry_sources: Vec::new(),
            snapshot_id,
            snapshot_kind: kind,
            spec,
            layers,
            chain_indexes,
            vm_state,
            manifest,
        };
        plan.validate_shallow().unwrap();
        plan
    }

    #[tokio::test]
    async fn full_and_base_image_persist_metadata_without_replacing_sealed_sources() {
        for kind in [SnapshotKind::Full, SnapshotKind::BaseImage] {
            let root = TestRoot::new();
            let cache = root.cache();
            let plan = fixture(&cache, kind);
            let identity = plan.identity().unwrap();
            let sources = plan
                .layers
                .iter()
                .map(|layer| Arc::clone(&layer.data_requests()[0].source))
                .collect::<Vec<_>>();
            let payload_bytes = cache.stats().unwrap().used_bytes;
            let persisted = persist_upload_plan(plan, Arc::clone(&cache)).await.unwrap();
            assert_eq!(persisted.identity().unwrap(), identity);
            assert!(cache.stats().unwrap().used_bytes >= payload_bytes);
            for (layer, source) in persisted.layers.iter().zip(&sources) {
                for object in [&layer.range_map, &layer.committed] {
                    assert_eq!(
                        cache
                            .read_metadata(&object.object_key, &object.sha256)
                            .unwrap(),
                        Some(object.payload.clone())
                    );
                }
                let request = &layer.data_requests()[0];
                assert!(Arc::ptr_eq(&request.source, source));
                let bytes = request.source.read_at(0, SEAL_CHUNK_BYTES).await.unwrap();
                assert_eq!(sha256_identity(&bytes), request.identity.expected_sha256);
                assert_eq!(bytes.len() as u64, request.identity.length);
                // The same encoded object feeds upload and subsequent physical
                // reads; neither a new encoding nor a second DATA copy is needed.
                struct OfflineOrigin;
                #[async_trait::async_trait]
                impl crate::snapshot::transport::PhysicalRangeReader for OfflineOrigin {
                    fn source_id(&self) -> &str {
                        "fixture-domain"
                    }
                    async fn read_physical_range(
                        &self,
                        _: crate::snapshot::transport::PhysicalRangeRequest<'_>,
                    ) -> Result<Vec<u8>, crate::snapshot::transport::RangeReadError>
                    {
                        panic!("sealed upload object must satisfy physical cache read")
                    }
                }
                let io =
                    RangeIO::new_with_cache(Arc::new(OfflineOrigin), None, Some(cache.clone()));
                let cached = crate::snapshot::io::range_io::RangeReader::read_object_range(
                    io.as_ref(),
                    "fixture-domain",
                    crate::snapshot::transport::PhysicalRangeRequest {
                        namespace: None,
                        object_key: request.identity.object_key.as_str(),
                        object_sha256: &request.identity.expected_sha256,
                        object_version: None,
                        object_size: Some(request.identity.length),
                        offset: 32,
                        length: 64,
                    },
                )
                .await
                .unwrap();
                assert_eq!(cached, bytes[32..96]);
                assert!(request
                    .source
                    .read_at(request.identity.length, 1)
                    .await
                    .unwrap()
                    .is_empty());
                assert!(request.source.read_at(0, 0).await.unwrap().is_empty());
                assert!(request
                    .source
                    .read_at(request.identity.length + 1, 1)
                    .await
                    .is_err());
                assert!(request
                    .source
                    .read_at(0, MAX_SOURCE_READ_BYTES + 1)
                    .await
                    .is_err());
            }
            for object in persisted
                .chain_indexes
                .iter()
                .chain(persisted.vm_state.iter())
                .chain([&persisted.manifest])
            {
                assert_eq!(
                    cache
                        .read_metadata(&object.object_key, &object.sha256)
                        .unwrap(),
                    Some(object.payload.clone())
                );
            }
        }
    }

    #[tokio::test]
    async fn generated_object_capacity_failure_keeps_earlier_sealed_source_readable() {
        let root = TestRoot::new();
        let cache =
            LocalFileCache::new_with_limit(root.0.clone(), "fixture-domain", 65536).unwrap();
        let first = layer(&cache, LayerKind::Memory);
        let error = layer_with_data(&cache, LayerKind::Block, true).unwrap_err();
        assert!(error.retryable());
        let request = &first.data_requests()[0];
        let payload = request.source.read_at(0, SEAL_CHUNK_BYTES).await.unwrap();
        assert_eq!(sha256_identity(&payload), request.identity.expected_sha256);
        assert!(cache.stats().unwrap().used_bytes <= 65536);
    }

    #[tokio::test]
    async fn sealed_upload_source_survives_quota_pressure_and_repeated_retry_reads() {
        let root = TestRoot::new();
        let cache =
            LocalFileCache::new_with_limit(root.0.clone(), "fixture-domain", 128 * 1024).unwrap();
        let plan = fixture_with_data(&cache, SnapshotKind::BaseImage, true);
        let sealed = persist_upload_plan(plan, Arc::clone(&cache)).await.unwrap();
        let before = cache.stats().unwrap();
        assert!(before.pinned_bytes > 0);
        // A new unpublished object must reserve space instead of bypassing
        // cache admission and becoming another unbounded source allocation.
        assert!(cache.create_staged_object(128 * 1024).is_err());
        let request = &sealed.layers[0].data_requests()[0];
        // The fixture's native FD is already closed. A retry still consumes
        // the exact same physical bytes solely through the sealed handle.
        for _attempt in 0..3 {
            let prefix = request.source.read_at(0, 4096).await.unwrap();
            let rest = request
                .source
                .read_at(prefix.len() as u64, SEAL_CHUNK_BYTES)
                .await
                .unwrap();
            let payload = [prefix, rest].concat();
            assert_eq!(sha256_identity(&payload), request.identity.expected_sha256);
        }
        assert!(cache.stats().unwrap().pinned_bytes >= before.pinned_bytes);
    }

    #[tokio::test]
    async fn metadata_capacity_failure_does_not_publish_manifest_or_drop_sealed_sources() {
        let root = TestRoot::new();
        let cache =
            LocalFileCache::new_with_limit(root.0.clone(), "fixture-domain", 65536).unwrap();
        let plan = fixture(&cache, SnapshotKind::BaseImage);
        // An admitted concurrent encoder consumes the remaining budget.
        // Optional graph persistence must not expose a manifest or discard
        // this plan's already sealed and pinned upload source.
        let remaining = 65536 - cache.stats().unwrap().used_bytes;
        let _other_encoder = cache.create_staged_object(remaining).unwrap();
        let manifest = plan.manifest.clone();
        let persisted = persist_upload_plan(plan, Arc::clone(&cache)).await.unwrap();
        assert_eq!(
            cache
                .read_metadata(&manifest.object_key, &manifest.sha256)
                .unwrap(),
            None
        );
        let request = &persisted.layers[0].data_requests()[0];
        let payload = request.source.read_at(0, SEAL_CHUNK_BYTES).await.unwrap();
        assert_eq!(sha256_identity(&payload), request.identity.expected_sha256);
    }

    #[tokio::test]
    async fn chain_metadata_failure_does_not_publish_manifest() {
        let root = TestRoot::new();
        let cache = root.cache();
        let mut plan = fixture(&cache, SnapshotKind::BaseImage);
        plan.chain_indexes[0].object_key = "../invalid-chain".into();
        plan.spec.rootfs.index_object_key = "../invalid-chain".into();
        let mut manifest: SnapshotManifest =
            serde_json::from_slice(&plan.manifest.payload).unwrap();
        manifest.rootfs.object_key = "../invalid-chain".into();
        plan.manifest.payload = canonical_json_bytes(&manifest).unwrap();
        plan.manifest.sha256 = sha256_identity(&plan.manifest.payload);
        plan.spec.manifest.sha256.clone_from(&plan.manifest.sha256);
        plan.validate_shallow().unwrap();
        let manifest = plan.manifest.clone();
        let error = persist_upload_plan(plan, Arc::clone(&cache))
            .await
            .unwrap_err();
        assert!(!error.retryable());
        assert_eq!(
            cache
                .read_metadata(&manifest.object_key, &manifest.sha256)
                .unwrap(),
            None
        );
    }
}
