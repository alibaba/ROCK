//! Bounded BASE_IMAGE build and upload orchestration.

use std::collections::BTreeMap;
use std::sync::{Arc, Weak};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use async_trait::async_trait;
use thiserror::Error;
use tokio::sync::{watch, Mutex, Notify, Semaphore};

use crate::snapshot::format::overlaybd::composite::RegistryRootfs;
use crate::snapshot::image::model::{BuildImage, ImageBuildAccepted, ResolveImage, ResolvedImage};
use crate::snapshot::metadata::{SnapshotKind, SnapshotProvenance, VmmCompatibility};
use crate::snapshot::repository::PublicationState;
use crate::snapshot::services::background_uploader::{
    LayerUploadState, LayerUploadStatus, ResolvedSnapshotUploadTarget, SnapshotPublication,
    SnapshotStatus, SnapshotUploadService, SnapshotUploadServiceError,
};
use crate::snapshot::Snapshot;
use crate::snapshot::SnapshotInstance;

const MAX_CONCURRENT_BUILDS: usize = 16;
const MAX_BUILD_ATTEMPTS: u64 = 10;

#[derive(Clone, Debug, Error, Eq, PartialEq)]
#[error("{code}: {message}")]
pub struct BaseImageBuildError {
    pub code: String,
    pub message: String,
    pub retryable: bool,
    pub oci_digest: Option<String>,
}

impl BaseImageBuildError {
    pub fn new(
        code: impl Into<String>,
        message: impl Into<String>,
        retryable: bool,
        oci_digest: Option<String>,
    ) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            retryable,
            oci_digest,
        }
    }
}

#[derive(Debug)]
pub enum BaseImageSource {
    Local(Arc<Snapshot>),
    Registry(Arc<RegistryRootfs>),
}

impl BaseImageSource {
    fn close_upload_pin(&self) {
        if let Self::Local(local) = self {
            let _ = local.close_upload_pin();
        }
    }
}

#[derive(Debug)]
pub struct BaseImageBuildResult {
    pub source: BaseImageSource,
    pub compatibility: VmmCompatibility,
    pub oci_digest: String,
}

#[async_trait]
pub trait BaseImageBuilder: Send + Sync + 'static {
    async fn resolve(&self, request: &ResolveImage) -> Result<ResolvedImage, BaseImageBuildError>;

    async fn build(
        &self,
        request: &BuildImage,
    ) -> Result<BaseImageBuildResult, BaseImageBuildError>;
}

#[derive(Clone, Debug, Error, Eq, PartialEq)]
pub enum BaseImageIngestionError {
    #[error("invalid BASE_IMAGE request: {0}")]
    Invalid(String),
    #[error("BASE_IMAGE identity conflict: {0}")]
    IdentityConflict(String),
    #[error("BASE_IMAGE state transition is invalid: {0}")]
    InvalidState(String),
    #[error("BASE_IMAGE was not found: {0}")]
    NotFound(String),
    #[error("BASE_IMAGE ingestion service is closed")]
    Closed,
    #[error(transparent)]
    Build(#[from] BaseImageBuildError),
    #[error(transparent)]
    Upload(#[from] SnapshotUploadServiceError),
}

#[derive(Clone, Debug)]
struct BuildFailure {
    code: String,
    message: String,
    retryable: bool,
}

#[derive(Debug)]
struct IngestionRecord {
    request: BuildImage,
    snapshot: Weak<dyn SnapshotInstance>,
    active: bool,
    registered: bool,
    attempt_count: u64,
    retry_at_unix_ms: Option<u64>,
    resolved_oci_digest: Option<String>,
    failure: Option<BuildFailure>,
}

#[derive(Default)]
struct IngestionState {
    records: BTreeMap<String, IngestionRecord>,
    closed: bool,
}

pub struct BaseImageIngestionService {
    builder: Arc<dyn BaseImageBuilder>,
    uploads: Arc<SnapshotUploadService>,
    slots: Arc<Semaphore>,
    max_attempts: u64,
    retry_base: Duration,
    retry_max: Duration,
    state: Mutex<IngestionState>,
    changed: Notify,
    shutdown: watch::Sender<bool>,
}

impl BaseImageIngestionService {
    pub fn new(
        builder: Arc<dyn BaseImageBuilder>,
        uploads: Arc<SnapshotUploadService>,
        max_concurrent_builds: usize,
        max_attempts: u64,
        retry_base: Duration,
        retry_max: Duration,
    ) -> Result<Arc<Self>, BaseImageIngestionError> {
        if !(1..=MAX_CONCURRENT_BUILDS).contains(&max_concurrent_builds)
            || !(1..=MAX_BUILD_ATTEMPTS).contains(&max_attempts)
            || retry_max.is_zero()
            || retry_base > retry_max
        {
            return Err(BaseImageIngestionError::Invalid(
                "BASE_IMAGE concurrency or retry policy is outside its bound".into(),
            ));
        }
        let (shutdown, _) = watch::channel(false);
        Ok(Arc::new(Self {
            builder,
            uploads,
            slots: Arc::new(Semaphore::new(max_concurrent_builds)),
            max_attempts,
            retry_base,
            retry_max,
            state: Mutex::new(IngestionState::default()),
            changed: Notify::new(),
            shutdown,
        }))
    }

    pub async fn create(
        self: &Arc<Self>,
        request: BuildImage,
    ) -> Result<ImageBuildAccepted, BaseImageIngestionError> {
        request
            .validate()
            .map_err(BaseImageIngestionError::Invalid)?;
        // Registry templates persist their immutable source reference in the
        // catalog spec. Preparing one must not resolve a writable object store.
        let target = if !self.uploads.enabled()
            || request.base_image_builder_version == super::oci::REGISTRY_BASE_IMAGE_BUILDER_VERSION
        {
            None
        } else {
            Some(
                self.uploads
                    .preflight_for(Some(&image_provenance(&request)))
                    .await?,
            )
        };
        let snapshot_id = request.snapshot_id.clone();
        let mut state = self.state.lock().await;
        if state.closed {
            return Err(BaseImageIngestionError::Closed);
        }
        if state.records.contains_key(&snapshot_id) {
            return Err(BaseImageIngestionError::IdentityConflict(format!(
                "snapshot ID {snapshot_id:?} already owns a BASE_IMAGE build"
            )));
        }
        let snapshot: Arc<dyn SnapshotInstance> = if request.base_image_builder_version
            == super::oci::REGISTRY_BASE_IMAGE_BUILDER_VERSION
        {
            self.uploads
                .catalog()
                .reserve_oci(&snapshot_id)
                .map_err(BaseImageIngestionError::IdentityConflict)?
        } else {
            self.uploads
                .catalog()
                .reserve_raw(&snapshot_id, SnapshotKind::BaseImage)
                .map_err(BaseImageIngestionError::IdentityConflict)?
        };
        state.records.insert(
            snapshot_id.clone(),
            IngestionRecord {
                request: request.clone(),
                snapshot: Arc::downgrade(&snapshot),
                active: true,
                registered: false,
                attempt_count: 0,
                retry_at_unix_ms: None,
                resolved_oci_digest: Some(request.oci_digest.clone()),
                failure: None,
            },
        );
        drop(state);
        self.spawn_build(snapshot, target);
        Ok(ImageBuildAccepted {
            identity: snapshot_id,
            state: PublicationState::Creating,
        })
    }

    pub async fn resolve(
        &self,
        request: ResolveImage,
    ) -> Result<ResolvedImage, BaseImageIngestionError> {
        request
            .validate()
            .map_err(BaseImageIngestionError::Invalid)?;
        self.builder.resolve(&request).await.map_err(Into::into)
    }

    pub async fn status(
        &self,
        snapshot_id: &str,
    ) -> Result<SnapshotStatus, BaseImageIngestionError> {
        let own_status = {
            let state = self.state.lock().await;
            let record = require_record(&state, snapshot_id)?;
            if record.registered {
                None
            } else {
                let snapshot = record
                    .snapshot
                    .upgrade()
                    .ok_or_else(|| BaseImageIngestionError::NotFound(snapshot_id.into()))?;
                if self.current_record(&state, &snapshot).is_none() {
                    return Err(BaseImageIngestionError::NotFound(snapshot_id.into()));
                }
                Some(ingestion_status(record, &snapshot))
            }
        };
        match own_status {
            Some(status) => Ok(status),
            None => self.uploads.status(snapshot_id).await.map_err(Into::into),
        }
    }

    pub async fn contains(&self, snapshot_id: &str) -> Result<bool, BaseImageIngestionError> {
        let state = self.state.lock().await;
        Ok(state.records.contains_key(snapshot_id))
    }

    pub async fn wait(&self, snapshot_id: &str) -> Result<SnapshotStatus, BaseImageIngestionError> {
        loop {
            let notified = self.changed.notified();
            let status = self.status(snapshot_id).await?;
            if status.state != PublicationState::Creating {
                return Ok(status);
            }
            notified.await;
        }
    }

    pub async fn ensure_invalidation_ready(
        &self,
        snapshot_id: &str,
    ) -> Result<bool, BaseImageIngestionError> {
        let state = self.state.lock().await;
        let Some(record) = state.records.get(snapshot_id) else {
            return Ok(false);
        };
        if record.active {
            return Err(BaseImageIngestionError::InvalidState(
                "BASE_IMAGE build is still mutating".into(),
            ));
        }
        Ok(true)
    }

    pub async fn invalidate(&self, snapshot_id: &str) -> Result<bool, BaseImageIngestionError> {
        let mut state = self.state.lock().await;
        let Some(record) = state.records.get(snapshot_id) else {
            return Ok(false);
        };
        if record.active {
            return Err(BaseImageIngestionError::InvalidState(
                "BASE_IMAGE build is still mutating".into(),
            ));
        }
        state.records.remove(snapshot_id);
        self.changed.notify_waiters();
        Ok(true)
    }

    pub async fn close(&self) -> Result<(), BaseImageIngestionError> {
        {
            let mut state = self.state.lock().await;
            if state.closed {
                return Ok(());
            }
            state.closed = true;
            for record in state.records.values_mut() {
                record.retry_at_unix_ms = None;
            }
        }
        // Preserve shutdown even when a newly spawned build has not subscribed
        // yet. `send` would discard the value when there are no receivers.
        self.shutdown.send_replace(true);
        loop {
            let notified = self.changed.notified();
            if !self
                .state
                .lock()
                .await
                .records
                .values()
                .any(|record| record.active)
            {
                return Ok(());
            }
            notified.await;
        }
    }

    fn current_record<'a>(
        &self,
        state: &'a IngestionState,
        snapshot: &Arc<dyn SnapshotInstance>,
    ) -> Option<&'a IngestionRecord> {
        let record = state.records.get(snapshot.snapshot_id())?;
        if !record
            .snapshot
            .upgrade()
            .is_some_and(|owner| Arc::ptr_eq(&owner, snapshot))
            || self
                .uploads
                .catalog()
                .update_instance(snapshot, |_| ())
                .is_none()
        {
            return None;
        }
        Some(record)
    }

    fn owned_record_mut<'a>(
        &self,
        state: &'a mut IngestionState,
        snapshot: &Arc<dyn SnapshotInstance>,
    ) -> Option<&'a mut IngestionRecord> {
        let record = state.records.get_mut(snapshot.snapshot_id())?;
        record
            .snapshot
            .upgrade()
            .is_some_and(|owner| Arc::ptr_eq(&owner, snapshot))
            .then_some(record)
    }

    fn current_record_mut<'a>(
        &self,
        state: &'a mut IngestionState,
        snapshot: &Arc<dyn SnapshotInstance>,
    ) -> Option<&'a mut IngestionRecord> {
        let record = self.owned_record_mut(state, snapshot)?;
        if self
            .uploads
            .catalog()
            .update_instance(snapshot, |_| ())
            .is_none()
        {
            // Only this obsolete job is being retired. A replacement ingestion
            // record has a different owner and cannot reach this branch.
            record.active = false;
            record.retry_at_unix_ms = None;
            self.changed.notify_waiters();
            return None;
        }
        Some(record)
    }

    fn spawn_build(
        self: &Arc<Self>,
        snapshot: Arc<dyn SnapshotInstance>,
        target: Option<ResolvedSnapshotUploadTarget>,
    ) {
        let service = Arc::clone(self);
        tokio::spawn(async move {
            service.run_build(snapshot, target).await;
        });
    }

    async fn run_build(
        self: Arc<Self>,
        snapshot: Arc<dyn SnapshotInstance>,
        target: Option<ResolvedSnapshotUploadTarget>,
    ) {
        let request = {
            let mut state = self.state.lock().await;
            let Some(record) = self.current_record_mut(&mut state, &snapshot) else {
                return;
            };
            if !record.active {
                return;
            }
            record.request.clone()
        };
        let mut built = None;
        let mut terminal_failure = None;
        for relative_attempt in 0..self.max_attempts {
            let attempt = {
                let mut state = self.state.lock().await;
                let Some(record) = self.current_record_mut(&mut state, &snapshot) else {
                    return;
                };
                record.attempt_count = record.attempt_count.saturating_add(1);
                record.retry_at_unix_ms = None;
                record.attempt_count
            };
            let mut shutdown = self.shutdown.subscribe();
            if *shutdown.borrow() {
                self.finish_cancelled(&snapshot).await;
                return;
            }
            let result = tokio::select! {
                biased;
                _ = shutdown.changed() => return self.finish_cancelled(&snapshot).await,
                result = async {
                    let _permit = self.slots.clone().acquire_owned().await.map_err(|_| {
                        BaseImageBuildError::new(
                            "BASE_IMAGE_BUILD_CLOSED",
                            "BASE_IMAGE build semaphore is closed",
                            false,
                            None,
                        )
                    })?;
                    self.builder.build(&request).await
                } => result,
            };
            match result {
                Ok(result) => {
                    built = Some(result);
                    break;
                }
                Err(error) => {
                    if let Some(digest) = &error.oci_digest {
                        let mut state = self.state.lock().await;
                        let Some(record) = self.current_record_mut(&mut state, &snapshot) else {
                            return;
                        };
                        if record
                            .resolved_oci_digest
                            .as_ref()
                            .is_some_and(|existing| existing != digest)
                        {
                            terminal_failure = Some(BuildFailure {
                                code: "IDENTITY_CONFLICT".into(),
                                message: "OCI tag resolved to another immutable digest".into(),
                                retryable: false,
                            });
                            break;
                        }
                        record.resolved_oci_digest = Some(digest.clone());
                    }
                    if !error.retryable || relative_attempt + 1 == self.max_attempts {
                        terminal_failure = Some(BuildFailure {
                            code: error.code,
                            message: error.message,
                            retryable: error.retryable,
                        });
                        break;
                    }
                    let delay = retry_delay(self.retry_base, self.retry_max, attempt);
                    {
                        let mut state = self.state.lock().await;
                        let Some(record) = self.current_record_mut(&mut state, &snapshot) else {
                            return;
                        };
                        record.failure = Some(BuildFailure {
                            code: error.code,
                            message: error.message,
                            retryable: true,
                        });
                        record.retry_at_unix_ms = Some(
                            now_unix_ms()
                                .saturating_add(delay.as_millis().try_into().unwrap_or(u64::MAX)),
                        );
                    }
                    self.changed.notify_waiters();
                    let mut shutdown = self.shutdown.subscribe();
                    if *shutdown.borrow() {
                        self.finish_cancelled(&snapshot).await;
                        return;
                    }
                    tokio::select! {
                        biased;
                        _ = shutdown.changed() => return self.finish_cancelled(&snapshot).await,
                        _ = tokio::time::sleep(delay) => {}
                    }
                }
            }
        }
        let Some(result) = built else {
            self.finish_failed(&snapshot, terminal_failure).await;
            return;
        };
        {
            let mut state = self.state.lock().await;
            let Some(record) = self.current_record_mut(&mut state, &snapshot) else {
                result.source.close_upload_pin();
                return;
            };
            if record
                .resolved_oci_digest
                .as_ref()
                .is_some_and(|digest| digest != &result.oci_digest)
            {
                drop(state);
                result.source.close_upload_pin();
                self.finish_failed(
                    &snapshot,
                    Some(BuildFailure {
                        code: "IDENTITY_CONFLICT".into(),
                        message: "OCI tag changed during BASE_IMAGE build".into(),
                        retryable: false,
                    }),
                )
                .await;
                return;
            }
            record.resolved_oci_digest = Some(result.oci_digest.clone());
        }
        let mut shutdown = self.shutdown.subscribe();
        let closing = *shutdown.borrow();
        if closing {
            result.source.close_upload_pin();
            self.finish_cancelled(&snapshot).await;
            return;
        }
        let registration = tokio::select! {
            biased;
            _ = shutdown.changed() => {
                result.source.close_upload_pin();
                self.finish_cancelled(&snapshot).await;
                return;
            }
            registered = async {
                match &result.source {
                    BaseImageSource::Local(local) => {
                        let local = self.uploads.catalog()
                            .initialize_raw(&snapshot, Arc::clone(local))
                            .map_err(SnapshotUploadServiceError::IdentityConflict)?;
                        if !self.uploads.enabled() {
                            // Resource initialization preserves the reserved
                            // Creating lifecycle. Commit its local graph without
                            // creating an upload identity, attempt or timer.
                            self.uploads.catalog()
                                .register_raw(local)
                                .map_err(SnapshotUploadServiceError::IdentityConflict)?;
                            return Ok(false);
                        }
                        let mut target = match target {
                            Some(target) => target,
                            None => self.uploads.preflight_for(local.provenance()).await?,
                        };
                        target.compatibility = result.compatibility;
                        self.uploads
                            .register(local, upload_request(&request), target)
                            .await
                            .map(|_| true)
                    },
                    BaseImageSource::Registry(rootfs) => self.uploads
                        .register_registry(&snapshot, Arc::clone(rootfs), upload_request(&request), result.compatibility, Some(image_provenance(&request)))
                        .await
                        .map(|_| true),
                }
            } => registered,
        };
        match registration {
            Ok(registered) => {
                let mut state = self.state.lock().await;
                // Registration already committed this source. Finalization may
                // now rehome it as S3; finishing our job must not retain raw or
                // require the repository to keep the old concrete instance.
                if let Some(record) = self.owned_record_mut(&mut state, &snapshot) {
                    record.active = false;
                    record.registered = registered;
                    record.failure = None;
                    record.retry_at_unix_ms = None;
                }
            }
            Err(error) => {
                // The catalog retains the last usable frozen source on failure;
                // only explicit finalization/invalidation may release it.
                self.finish_failed(
                    &snapshot,
                    Some(BuildFailure {
                        code: "BASE_IMAGE_UPLOAD_FAILED".into(),
                        message: error.to_string(),
                        retryable: error.retryable(),
                    }),
                )
                .await;
                return;
            }
        }
        self.changed.notify_waiters();
    }

    async fn finish_failed(
        &self,
        snapshot: &Arc<dyn SnapshotInstance>,
        failure: Option<BuildFailure>,
    ) {
        let mut state = self.state.lock().await;
        if let Some(record) = self.current_record_mut(&mut state, snapshot) {
            snapshot.fail(
                "base_image_build",
                failure
                    .as_ref()
                    .map(|failure| failure.message.clone())
                    .unwrap_or_else(|| "BASE_IMAGE build ended without a result".into()),
            );
            record.active = false;
            record.retry_at_unix_ms = None;
            record.failure = failure.or_else(|| {
                Some(BuildFailure {
                    code: "BASE_IMAGE_BUILD_FAILED".into(),
                    message: "BASE_IMAGE build ended without a result".into(),
                    retryable: false,
                })
            });
        }
        drop(state);
        self.changed.notify_waiters();
    }

    async fn finish_cancelled(&self, snapshot: &Arc<dyn SnapshotInstance>) {
        let mut state = self.state.lock().await;
        if let Some(record) = self.current_record_mut(&mut state, snapshot) {
            record.active = false;
            record.retry_at_unix_ms = None;
            snapshot.fail(
                "base_image_build",
                "BASE_IMAGE build was cancelled during service shutdown".into(),
            );
            record.failure = Some(BuildFailure {
                code: "BASE_IMAGE_BUILD_CANCELLED".into(),
                message: "BASE_IMAGE build was cancelled during service shutdown".into(),
                retryable: true,
            });
        }
        drop(state);
        self.changed.notify_waiters();
    }
}

fn upload_request(request: &BuildImage) -> SnapshotPublication {
    SnapshotPublication {
        snapshot_id: request.snapshot_id.clone(),
    }
}

fn image_provenance(request: &BuildImage) -> SnapshotProvenance {
    SnapshotProvenance {
        oci_ref: Some(request.oci_ref.clone()),
        oci_digest: Some(request.oci_digest.clone()),
    }
}

fn require_record<'a>(
    state: &'a IngestionState,
    snapshot_id: &str,
) -> Result<&'a IngestionRecord, BaseImageIngestionError> {
    state
        .records
        .get(snapshot_id)
        .ok_or_else(|| BaseImageIngestionError::NotFound(snapshot_id.into()))
}

fn ingestion_status(
    record: &IngestionRecord,
    snapshot: &Arc<dyn SnapshotInstance>,
) -> SnapshotStatus {
    let state = snapshot.state();
    let chains = snapshot.chains();
    let local = Arc::clone(snapshot).downcast::<Snapshot>();
    SnapshotStatus {
        snapshot_id: record.request.snapshot_id.clone(),
        state,
        remote_publishing: false,
        progress: f64::from(matches!(
            state,
            PublicationState::LocalReady | PublicationState::RemoteReady
        )),
        attempt_count: record.attempt_count,
        last_error: record
            .failure
            .as_ref()
            .map(|failure| failure.message.clone()),
        failure_code: record.failure.as_ref().map(|failure| failure.code.clone()),
        retryable: record
            .failure
            .as_ref()
            .is_some_and(|failure| failure.retryable),
        retry_at_unix_ms: record.retry_at_unix_ms,
        snapshot_spec: None,
        memory_bytes: chains
            .iter()
            .find(|chain| chain.kind == crate::snapshot::metadata::LayerKind::Memory)
            .map(|chain| chain.logical_bytes),
        rootfs_bytes: chains
            .iter()
            .find(|chain| chain.kind == crate::snapshot::metadata::LayerKind::Block)
            .map(|chain| chain.logical_bytes),
        spec_checksum: None,
        admin_finalized: false,
        catalog_pin_held: local
            .as_ref()
            .is_some_and(|local| local.has_native() && !local.is_closed().unwrap_or(true)),
        layers: chains
            .into_iter()
            .map(|chain| {
                (
                    chain.kind,
                    LayerUploadStatus {
                        upload_state: if state == PublicationState::RemoteReady {
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
            .collect(),
    }
}

#[cfg(test)]
mod registry_template_tests {
    use super::*;
    use crate::snapshot::io::snapshot_io::{SnapshotDataUploadConfig, SnapshotPublisher};
    use std::sync::atomic::{AtomicUsize, Ordering};

    use crate::snapshot::image::oci::REGISTRY_BASE_IMAGE_BUILDER_VERSION;
    use crate::snapshot::services::background_uploader::SnapshotUploadTargetResolver;

    struct UnavailableWriteStore(AtomicUsize);

    #[async_trait]
    impl SnapshotUploadTargetResolver for UnavailableWriteStore {
        async fn resolve(
            &self,
        ) -> Result<ResolvedSnapshotUploadTarget, SnapshotUploadServiceError> {
            self.0.fetch_add(1, Ordering::Relaxed);
            Err(SnapshotUploadServiceError::Invalid(
                "write store unavailable".into(),
            ))
        }
    }

    struct NativeBuilder;

    #[async_trait]
    impl BaseImageBuilder for NativeBuilder {
        async fn resolve(
            &self,
            _request: &ResolveImage,
        ) -> Result<ResolvedImage, BaseImageBuildError> {
            unreachable!("the request already pins the manifest digest")
        }

        async fn build(
            &self,
            request: &BuildImage,
        ) -> Result<BaseImageBuildResult, BaseImageBuildError> {
            Ok(BaseImageBuildResult {
                source: BaseImageSource::Registry(
                    crate::snapshot::format::overlaybd::composite::test_registry_rootfs(),
                ),
                compatibility: VmmCompatibility {
                    architecture: "x86_64".into(),
                    vmm_type: "any".into(),
                    vmm_state_format_version: None,
                    rocklet_artifact: None,
                },
                oci_digest: request.oci_digest.clone(),
            })
        }
    }

    struct LocalBuilder;

    #[async_trait]
    impl BaseImageBuilder for LocalBuilder {
        async fn resolve(
            &self,
            _request: &ResolveImage,
        ) -> Result<ResolvedImage, BaseImageBuildError> {
            unreachable!("the local fixture already pins its source")
        }

        async fn build(
            &self,
            request: &BuildImage,
        ) -> Result<BaseImageBuildResult, BaseImageBuildError> {
            use crate::snapshot::metadata::{sha256_identity, LayerKind, SnapshotProvenance};
            use crate::snapshot::repository::frozen_layer::{
                create_temporary_zfile, FrozenLayerMetadata, FrozenLayerPin, PackedOwnershipMap,
            };
            use std::os::unix::fs::FileExt;

            let backing = create_temporary_zfile().unwrap();
            backing.set_len(65536).unwrap();
            backing.write_all_at(&vec![0x5a; 65536], 0).unwrap();
            let local = Snapshot::from_base_layer(
                request.snapshot_id.clone(),
                FrozenLayerPin::new(
                    FrozenLayerMetadata {
                        kind: LayerKind::Block,
                        layer_id: "disabled-local-base".into(),
                        provisional_content_id: sha256_identity(b"disabled-local-base"),
                        logical_size: 65536,
                        alignment: 65536,
                    },
                    PackedOwnershipMap {
                        kind: LayerKind::Block,
                        snapshot_id: request.snapshot_id.clone(),
                        format_version: 1,
                        logical_size: 65536,
                        granularity: 65536,
                        packed_states: vec![1],
                    },
                    backing.into(),
                    false,
                ),
                SnapshotProvenance {
                    oci_ref: Some(request.oci_ref.clone()),
                    oci_digest: Some(request.oci_digest.clone()),
                },
            )
            .unwrap();
            // Match the real process builder: native resources exist, while
            // the ingestion service still owns the Creating -> LocalReady
            // catalog transition. A pre-ready fixture would hide that step.
            assert!(local.has_native());
            {
                let state = local.lifecycle().lock().unwrap();
                assert_eq!(state.state, PublicationState::Creating);
                assert!(state.chains.is_empty());
                assert!(state.last_usable.is_none());
                assert!(state.upload_identity.is_none());
            }
            Ok(BaseImageBuildResult {
                source: BaseImageSource::Local(local),
                compatibility: VmmCompatibility {
                    architecture: "x86_64".into(),
                    vmm_type: "any".into(),
                    vmm_state_format_version: None,
                    rocklet_artifact: None,
                },
                oci_digest: request.oci_digest.clone(),
            })
        }
    }

    struct PendingBuilder(Arc<Notify>, bool);

    #[async_trait]
    impl BaseImageBuilder for PendingBuilder {
        async fn resolve(
            &self,
            _request: &ResolveImage,
        ) -> Result<ResolvedImage, BaseImageBuildError> {
            unreachable!()
        }

        async fn build(
            &self,
            _request: &BuildImage,
        ) -> Result<BaseImageBuildResult, BaseImageBuildError> {
            self.0.notify_one();
            if self.1 {
                return Err(BaseImageBuildError::new(
                    "retry",
                    "transient fixture",
                    true,
                    None,
                ));
            }
            std::future::pending().await
        }
    }

    fn native_request() -> BuildImage {
        BuildImage {
            snapshot_id: "base-native-without-s3".into(),
            oci_ref: "example.com/images/rootfs:v_accelerated".into(),
            oci_digest: crate::snapshot::format::overlaybd::composite::test_registry_rootfs()
                .manifest_digest()
                .to_owned(),
            platform: "linux/amd64".into(),
            base_image_builder_version: REGISTRY_BASE_IMAGE_BUILDER_VERSION.into(),
            base_image_build_key: format!("sha256:{}", "b".repeat(64)),
        }
    }

    #[tokio::test]
    async fn disabled_upload_keeps_local_base_image_ready_and_native_peer_readable() {
        use crate::snapshot::metadata::LayerKind;

        let write_store = Arc::new(UnavailableWriteStore(AtomicUsize::new(0)));
        let uploads = SnapshotUploadService::new(
            SnapshotPublisher::new(SnapshotDataUploadConfig::default(), None),
            write_store.clone(),
            Duration::from_millis(1),
            Duration::from_millis(2),
        )
        .unwrap()
        .with_enabled(false);
        let service = BaseImageIngestionService::new(
            Arc::new(LocalBuilder),
            uploads.clone(),
            1,
            1,
            Duration::from_millis(1),
            Duration::from_millis(2),
        )
        .unwrap();
        let mut request = native_request();
        request.base_image_builder_version = super::super::oci::BASE_IMAGE_BUILDER_VERSION.into();
        service.create(request.clone()).await.unwrap();
        let status =
            tokio::time::timeout(Duration::from_secs(2), service.wait(&request.snapshot_id))
                .await
                .unwrap()
                .unwrap();
        assert_eq!(status.state, PublicationState::LocalReady);
        assert_eq!(status.progress, 1.0);
        assert_eq!(status.rootfs_bytes, Some(65536));
        assert!(status.catalog_pin_held);
        assert!(!status.remote_publishing);
        assert!(status.snapshot_spec.is_none());
        assert!(status.last_error.is_none());
        assert!(status.retry_at_unix_ms.is_none());
        assert_eq!(
            status.layers[&LayerKind::Block].upload_state,
            LayerUploadState::LocalReady
        );
        assert_eq!(status.layers[&LayerKind::Block].attempt_count, 0);
        assert_eq!(write_store.0.load(Ordering::Relaxed), 0);
        let snapshot = uploads.catalog().get(&request.snapshot_id).unwrap();
        {
            let state = snapshot.lifecycle().lock().unwrap();
            assert_eq!(state.state, PublicationState::LocalReady);
            assert_eq!(state.last_usable, Some(PublicationState::LocalReady));
            assert_eq!(state.chains.len(), 1);
            assert_eq!(state.chains[0].layers.len(), 1);
            assert!(state.upload_identity.is_none());
            assert_eq!(state.attempt_count, 0);
            assert!(!state.retry_scheduled);
            assert!(!state.remote_publishing);
        }
        for chain in snapshot.chains() {
            for layer in chain.layers {
                let identity = crate::snapshot::repository::LayerIdentity {
                    kind: chain.kind,
                    content_identity: layer.content_identity,
                    format: layer.format,
                    logical_bytes: layer.logical_bytes,
                    unit_size_bytes: layer.unit_size_bytes,
                };
                let indexed = uploads.catalog().layer(&identity).unwrap();
                assert_eq!(indexed.state, PublicationState::LocalReady);
                assert_eq!(indexed.last_usable, Some(PublicationState::LocalReady));
                assert!(indexed.local_snapshot_ids.contains(&request.snapshot_id));
                assert!(indexed.referenced_by.contains(&request.snapshot_id));
                assert!(indexed.prepared_by.is_empty());
            }
        }
        let local = snapshot.downcast::<Snapshot>().unwrap();
        assert!(local.native_is_locally_ready());
        let peers = crate::snapshot::services::p2p::P2PSnapshotServeRegistry::new();
        let metadata = peers.register_native(&local).await.unwrap();
        let layer = &metadata.layers[0];
        assert_eq!(
            peers
                .read_native_range(
                    &request.snapshot_id,
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
        assert_eq!(service.status(&request.snapshot_id).await.unwrap(), status);
        service.close().await.unwrap();
        uploads.close().await.unwrap();
    }

    #[tokio::test]
    async fn disabled_upload_preserves_native_registry_template_import() {
        let write_store = Arc::new(UnavailableWriteStore(AtomicUsize::new(0)));
        let uploads = SnapshotUploadService::new(
            SnapshotPublisher::new(SnapshotDataUploadConfig::default(), None),
            write_store.clone(),
            Duration::from_millis(1),
            Duration::from_millis(2),
        )
        .unwrap()
        .with_enabled(false);
        let service = BaseImageIngestionService::new(
            Arc::new(NativeBuilder),
            uploads.clone(),
            1,
            1,
            Duration::from_millis(1),
            Duration::from_millis(2),
        )
        .unwrap();
        let request = native_request();
        service.create(request.clone()).await.unwrap();
        let status =
            tokio::time::timeout(Duration::from_secs(2), service.wait(&request.snapshot_id))
                .await
                .unwrap()
                .unwrap();
        assert_eq!(status.state, PublicationState::RemoteReady);
        assert_eq!(
            status.snapshot_spec.as_ref().unwrap()["storage"]["provider"],
            "Registry"
        );
        assert_eq!(write_store.0.load(Ordering::Relaxed), 0);
        uploads
            .finalize_published(&request.snapshot_id, status.spec_checksum.as_ref().unwrap())
            .await
            .unwrap();
        service.close().await.unwrap();
        uploads.close().await.unwrap();
    }

    #[tokio::test]
    async fn registry_template_reaches_ready_without_a_writable_store() {
        let write_store = Arc::new(UnavailableWriteStore(AtomicUsize::new(0)));
        let publisher =
            SnapshotPublisher::new_with_config(SnapshotDataUploadConfig::default(), None).unwrap();
        let uploads = SnapshotUploadService::new(
            publisher,
            write_store.clone(),
            Duration::from_millis(1),
            Duration::from_millis(2),
        )
        .unwrap();
        let service = BaseImageIngestionService::new(
            Arc::new(NativeBuilder),
            uploads.clone(),
            1,
            1,
            Duration::from_millis(1),
            Duration::from_millis(2),
        )
        .unwrap();
        let request = native_request();
        service.create(request.clone()).await.unwrap();
        let status =
            tokio::time::timeout(Duration::from_secs(2), service.wait(&request.snapshot_id))
                .await
                .unwrap()
                .unwrap();
        assert_eq!(status.state, PublicationState::RemoteReady);
        assert_eq!(
            status.snapshot_spec.as_ref().unwrap()["storage"]["provider"],
            "Registry"
        );
        assert!(status.snapshot_spec.as_ref().unwrap()["rootfs"]["source_ref"].is_object());
        assert_eq!(write_store.0.load(Ordering::Relaxed), 0);
        let checksum = status.spec_checksum.as_ref().unwrap();
        let finalized = uploads
            .finalize_published(&request.snapshot_id, checksum)
            .await
            .unwrap();
        assert_eq!(finalized.released_pins, 0);
        assert!(
            service
                .status(&request.snapshot_id)
                .await
                .unwrap()
                .admin_finalized
        );
        assert!(matches!(
            service.create(request.clone()).await,
            Err(BaseImageIngestionError::IdentityConflict(_))
        ));
        let mut conflict = request.clone();
        conflict.oci_digest = format!("sha256:{}", "c".repeat(64));
        assert!(matches!(
            service.create(conflict).await,
            Err(BaseImageIngestionError::IdentityConflict(_))
        ));
        assert_eq!(write_store.0.load(Ordering::Relaxed), 0);
        service.close().await.unwrap();
        uploads.close().await.unwrap();
    }

    #[tokio::test]
    async fn duplicate_build_requests_do_not_join_or_restart_existing_work() {
        for fails in [false, true] {
            let entered = Arc::new(Notify::new());
            let uploads = SnapshotUploadService::new(
                SnapshotPublisher::new(SnapshotDataUploadConfig::default(), None),
                Arc::new(UnavailableWriteStore(AtomicUsize::new(0))),
                Duration::from_millis(1),
                Duration::from_millis(2),
            )
            .unwrap();
            let service = BaseImageIngestionService::new(
                Arc::new(PendingBuilder(Arc::clone(&entered), fails)),
                uploads.clone(),
                1,
                1,
                Duration::from_millis(1),
                Duration::from_millis(2),
            )
            .unwrap();
            let request = native_request();
            service.create(request.clone()).await.unwrap();
            tokio::time::timeout(Duration::from_secs(1), entered.notified())
                .await
                .unwrap();
            if fails {
                let status = tokio::time::timeout(
                    Duration::from_secs(1),
                    service.wait(&request.snapshot_id),
                )
                .await
                .unwrap()
                .unwrap();
                assert_eq!(status.state, PublicationState::Failed);
                assert!(status.retryable);
            }
            assert!(matches!(
                service.create(request.clone()).await,
                Err(BaseImageIngestionError::IdentityConflict(_))
            ));
            let status = service.status(&request.snapshot_id).await.unwrap();
            assert_eq!(status.attempt_count, 1);
            assert_eq!(
                status.state,
                if fails {
                    PublicationState::Failed
                } else {
                    PublicationState::Creating
                }
            );
            if fails {
                assert_eq!(status.failure_code.as_deref(), Some("retry"));
            }
            service.close().await.unwrap();
            uploads.close().await.unwrap();
        }
    }

    struct DelayedRegistryBuilder {
        entered: Arc<Notify>,
        release: Arc<Notify>,
        fails: bool,
    }

    #[async_trait]
    impl BaseImageBuilder for DelayedRegistryBuilder {
        async fn resolve(
            &self,
            _request: &ResolveImage,
        ) -> Result<ResolvedImage, BaseImageBuildError> {
            unreachable!("the fixture already pins its manifest")
        }

        async fn build(
            &self,
            request: &BuildImage,
        ) -> Result<BaseImageBuildResult, BaseImageBuildError> {
            self.entered.notify_one();
            self.release.notified().await;
            if self.fails {
                Err(BaseImageBuildError::new(
                    "late_failure",
                    "obsolete import",
                    false,
                    None,
                ))
            } else {
                NativeBuilder.build(request).await
            }
        }
    }

    #[tokio::test]
    async fn late_registry_build_completion_cannot_mutate_a_same_id_replacement() {
        for fails in [false, true] {
            let entered = Arc::new(Notify::new());
            let release = Arc::new(Notify::new());
            let uploads = SnapshotUploadService::new(
                SnapshotPublisher::new(SnapshotDataUploadConfig::default(), None),
                Arc::new(UnavailableWriteStore(AtomicUsize::new(0))),
                Duration::from_millis(1),
                Duration::from_millis(2),
            )
            .unwrap();
            let service = BaseImageIngestionService::new(
                Arc::new(DelayedRegistryBuilder {
                    entered: Arc::clone(&entered),
                    release: Arc::clone(&release),
                    fails,
                }),
                Arc::clone(&uploads),
                1,
                1,
                Duration::from_millis(1),
                Duration::from_millis(2),
            )
            .unwrap();
            let request = native_request();
            service.create(request.clone()).await.unwrap();
            tokio::time::timeout(Duration::from_secs(1), entered.notified())
                .await
                .unwrap();
            let old = uploads.catalog().get(&request.snapshot_id).unwrap();
            let old_weak = Arc::downgrade(&old);
            // Normal invalidation rejects active builds. Deliberately replace
            // the authority here to verify the independent object-identity fence.
            uploads.catalog().invalidate(&request.snapshot_id);
            let replacement: Arc<dyn SnapshotInstance> =
                uploads.catalog().reserve_oci(&request.snapshot_id).unwrap();
            {
                let mut state = service.state.lock().await;
                state.records.insert(
                    request.snapshot_id.clone(),
                    IngestionRecord {
                        request: request.clone(),
                        snapshot: Arc::downgrade(&replacement),
                        active: false,
                        registered: false,
                        attempt_count: 7,
                        retry_at_unix_ms: None,
                        resolved_oci_digest: Some(request.oci_digest.clone()),
                        failure: None,
                    },
                );
            }
            release.notify_one();
            tokio::time::timeout(Duration::from_secs(1), async {
                while old_weak.strong_count() != 1 {
                    tokio::task::yield_now().await;
                }
            })
            .await
            .expect("obsolete build retained its original instance");
            let status = service.status(&request.snapshot_id).await.unwrap();
            assert_eq!(status.state, PublicationState::Creating);
            assert_eq!(status.attempt_count, 7);
            assert!(status.last_error.is_none());
            assert!(replacement.failure().is_none());
            assert!(Arc::ptr_eq(
                &uploads.catalog().get(&request.snapshot_id).unwrap(),
                &replacement
            ));

            // Registration and both terminal callbacks must retain that same
            // fence even when called after the obsolete worker has drained.
            let ready = NativeBuilder.build(&request).await.unwrap();
            let BaseImageSource::Registry(rootfs) = ready.source else {
                unreachable!()
            };
            assert!(matches!(
                uploads
                    .register_registry(
                        &old,
                        rootfs,
                        upload_request(&request),
                        ready.compatibility,
                        None
                    )
                    .await,
                Err(SnapshotUploadServiceError::IdentityConflict(_))
            ));
            service
                .finish_failed(
                    &old,
                    Some(BuildFailure {
                        code: "late_failure".into(),
                        message: "obsolete import".into(),
                        retryable: false,
                    }),
                )
                .await;
            service.finish_cancelled(&old).await;
            let status = service.status(&request.snapshot_id).await.unwrap();
            assert_eq!(status.state, PublicationState::Creating);
            assert_eq!(status.attempt_count, 7);
            assert!(status.last_error.is_none());
            service.close().await.unwrap();
            uploads.close().await.unwrap();
        }
    }

    #[tokio::test]
    async fn close_cancels_registry_import_before_or_after_build_admission() {
        for (wait_for_build, retry) in [(true, false), (false, false), (true, true)] {
            let entered = Arc::new(Notify::new());
            let uploads = SnapshotUploadService::new(
                SnapshotPublisher::new(SnapshotDataUploadConfig::default(), None),
                Arc::new(UnavailableWriteStore(AtomicUsize::new(0))),
                Duration::from_millis(1),
                Duration::from_millis(2),
            )
            .unwrap();
            let service = BaseImageIngestionService::new(
                Arc::new(PendingBuilder(Arc::clone(&entered), retry)),
                uploads.clone(),
                1,
                2,
                Duration::from_secs(30),
                Duration::from_secs(30),
            )
            .unwrap();
            let request = native_request();
            service.create(request.clone()).await.unwrap();
            if wait_for_build {
                tokio::time::timeout(Duration::from_secs(1), entered.notified())
                    .await
                    .unwrap();
            }
            tokio::time::timeout(Duration::from_secs(1), service.close())
                .await
                .unwrap()
                .unwrap();
            let status = service.status(&request.snapshot_id).await.unwrap();
            assert_eq!(status.state, PublicationState::Failed);
            assert_eq!(
                status.failure_code.as_deref(),
                Some("BASE_IMAGE_BUILD_CANCELLED")
            );
            uploads.close().await.unwrap();
        }
    }
}

fn retry_delay(base: Duration, maximum: Duration, attempt: u64) -> Duration {
    let shift = u32::try_from(attempt.saturating_sub(1).min(31)).unwrap_or(31);
    base.checked_mul(1_u32 << shift)
        .unwrap_or(maximum)
        .min(maximum)
}

fn now_unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .try_into()
        .unwrap_or(u64::MAX)
}
