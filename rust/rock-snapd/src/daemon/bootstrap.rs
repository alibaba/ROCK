//! Process bootstrap, dependency assembly and shared TCP connection ownership.

use std::collections::{BTreeMap, HashMap};
use std::convert::Infallible;
use std::ffi::CString;
use std::ffi::OsString;
use std::fs::{File, OpenOptions};
use std::future::Future;
use std::io::{self, Read};
use std::net::{Ipv4Addr, SocketAddr};
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;

use async_trait::async_trait;
use aws_sdk_s3::config::{BehaviorVersion, Credentials, Region};
use aws_smithy_runtime::client::http::hyper_014::HyperClientBuilder;
use hyper::server::conn::Http;
use hyper::service::service_fn;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use thiserror::Error;
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::watch;
use tokio::task::JoinSet;

use crate::endpoints::control::http::{
    SnapdControlApi, SnapdControlHttpService, SnapdControlServices,
};
use crate::endpoints::lazy_load::handler::RemoteFaultLimits;
use crate::endpoints::p2p::handler::{P2pApi, P2pService};
use crate::endpoints::p2p::protocol::P2P_TCP_MAGIC;
use crate::observability::metrics::SnapdMetrics;
use crate::observability::structured_log::{
    StructuredComponent, StructuredEvent, StructuredEventName, StructuredLogLevel,
    StructuredLogger, StructuredOutcome,
};
use crate::snapshot::image::metadata::ObjectStoreImageMetadata;
use crate::snapshot::image::oci::{
    OciExt4BaseImageBuilder, OciExt4BuilderConfig, SkopeoOciLayoutPuller,
};
use crate::snapshot::image::service::{BaseImageBuilder, BaseImageIngestionService};
use crate::snapshot::io::backing::LocalBackingRegistry;
use crate::snapshot::io::range_io::RangeIO;
use crate::snapshot::io::range_io::RangeReader;
use crate::snapshot::io::snapshot_io::{
    SnapshotDataUploadConfig, SnapshotIo, SnapshotPublisher, SnapshotReader,
};
use crate::snapshot::metadata::{
    SnapshotProvenance, StorageDescriptor, StorageProvider, VmmCompatibility,
};
use crate::snapshot::repository::SnapshotRepository;
use crate::snapshot::services::background_prefetcher::{
    LatestMemoryPrefetchConfig, LATEST_MEMORY_PREFETCH_MAX_BYTES,
};
use crate::snapshot::services::background_uploader::{
    ResolvedSnapshotUploadTarget, SnapshotUploadService, SnapshotUploadServiceError,
    SnapshotUploadTargetResolver,
};
use crate::snapshot::services::ondemand_loader::{
    CredentialIdentity, RangeLoadExecutor, DEFAULT_MAX_RANGE_BYTES,
};
use crate::snapshot::transport::p2p::{P2PClient, P2PClientConfig};
use crate::snapshot::transport::posix_file::LocalFileCache;
use crate::snapshot::transport::s3::{ConditionalWriteDialect, S3ImmutableObjectStore};
use crate::snapshot::transport::DataPackWriteTarget;
use crate::snapshot::transport::MetadataObjectStore;
use crate::snapshot::transport::{
    ImmutableObjectStore, ImmutableStreamWriter, NamespaceMutationFence,
    SnapshotMetadataWriteTarget,
};
use crate::snapshot::{SnapshotImageMetadataReader, SnapshotImageMetadataUploader};
use crate::vm::error::OperationError as SnapshotControlError;
use crate::vm::repository::RuntimeLaunchPlan;
use crate::vm::repository::{
    SnapshotCaptureService, VmRepository, VmRepositoryError, VmService, MAX_CONCURRENT_STARTS,
};
use crate::vm::runtime::client::RuntimeLaunchFactory;
use crate::vm::runtime::launcher::{
    GuestRockletLaunch, RockletArtifactIdentity, VmmLaunchSpec, ROCKLET_ARTIFACT_FD,
    ROCK_UFFD_LEGACY_ENV,
};
use crate::vm::runtime::prepare::SnapshotRestorePreparer;
use crate::vm::runtime::protocol::Capability;
use crate::RuntimeIdentity;

pub use super::config::{
    BaseImageNodeConfig, DaemonError, RockletArtifactConfig, RustSnapdConfig, S3NodeConfig,
    VmmNodeConfig,
};
use super::config::{
    BACKING_ROOT, DATA_UPLOAD_ATTEMPT_TIMEOUT_SECONDS, DATA_UPLOAD_CHUNK_BYTES,
    MAX_CONCURRENT_DATA_UPLOADS, MAX_DATA_UPLOAD_BYTES_IN_FLIGHT, MAX_INFLIGHT_PER_VM,
    MAX_PENDING_PER_VM, MAX_QUEUED_DATA_UPLOADS, P2P_PROBE_TIMEOUT_MS, P2P_RANGE_TIMEOUT_MS,
    REMOTE_LOAD_TIMEOUT_SECONDS, RUNTIME_START_TIMEOUT_SECONDS, SNAPD_PORT,
    STORAGE_CLIENT_IDENTITY, STORAGE_ENDPOINT_PROFILE,
};

const MAX_CONFIG_BYTES: u64 = 1024 * 1024;
const LOCAL_OBJECT_CACHE_ROOT: &str = "/r/overlaybd";
const FAULT_AROUND_PAGES_ENV: &str = "ROCK_FAULT_AROUND_PAGES";
const SNAPSHOT_PREFETCH_CONCURRENCY_ENV: &str = "ROCK_SNAPSHOT_PREFETCH_CONCURRENCY";
const SNAPSHOT_PREFETCH_MAX_BYTES_ENV: &str = "ROCK_SNAPSHOT_PREFETCH_MAX_BYTES";
#[derive(Clone, Debug, Hash, Eq, PartialEq)]
pub struct SnapshotWriteDomainIdentity {
    pub provider: StorageProvider,
    pub endpoint_profile: String,
    pub namespace: String,
}

pub struct SnapshotWriteDomainRegistry {
    domains: StdMutex<HashMap<SnapshotWriteDomainIdentity, Arc<SnapshotWriteDomain>>>,
}

impl SnapshotWriteDomainRegistry {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            domains: StdMutex::new(HashMap::new()),
        })
    }

    pub fn register(
        &self,
        identity: SnapshotWriteDomainIdentity,
        metadata: Arc<dyn ImmutableObjectStore>,
        data: Arc<dyn ImmutableStreamWriter>,
        fence: Arc<NamespaceMutationFence>,
    ) -> Result<Arc<SnapshotWriteDomain>, SnapshotUploadServiceError> {
        if identity.endpoint_profile.is_empty()
            || identity.namespace.is_empty()
            || metadata.namespace() != identity.namespace
            || data.namespace() != identity.namespace
            || data.write_domain_id().is_empty()
            || !Arc::ptr_eq(&metadata.fence(), &fence)
            || !Arc::ptr_eq(&data.fence(), &fence)
        {
            return Err(SnapshotUploadServiceError::IdentityConflict(
                "Snapshot write-domain capabilities do not share one canonical fence".into(),
            ));
        }
        let mut domains = self
            .domains
            .lock()
            .map_err(|_| SnapshotUploadServiceError::Publish {
                message: "Snapshot write-domain registry lock is poisoned".into(),
                retryable: false,
            })?;
        if domains.contains_key(&identity) {
            return Err(SnapshotUploadServiceError::IdentityConflict(
                "Snapshot write domain was registered more than once".into(),
            ));
        }
        let write_target_identity = data.write_domain_id().to_owned();
        let metadata_target = SnapshotMetadataWriteTarget::new(
            metadata,
            write_target_identity.clone(),
            Arc::clone(&fence),
        )
        .map_err(|error| SnapshotUploadServiceError::IdentityConflict(error.to_string()))?;
        let data_target = DataPackWriteTarget::new(data, write_target_identity, Arc::clone(&fence))
            .map_err(|error| SnapshotUploadServiceError::IdentityConflict(error.to_string()))?;
        let domain = Arc::new(SnapshotWriteDomain {
            identity: identity.clone(),
            metadata_target,
            data_target,
            fence,
        });
        domains.insert(identity, Arc::clone(&domain));
        Ok(domain)
    }
}

pub struct SnapshotWriteDomain {
    identity: SnapshotWriteDomainIdentity,
    metadata_target: SnapshotMetadataWriteTarget,
    data_target: DataPackWriteTarget,
    fence: Arc<NamespaceMutationFence>,
}

impl SnapshotWriteDomain {
    pub fn resolved_target(
        self: &Arc<Self>,
        storage: StorageDescriptor,
        compatibility: VmmCompatibility,
    ) -> Result<ResolvedSnapshotUploadTarget, SnapshotUploadServiceError> {
        if storage.provider != self.identity.provider
            || storage.endpoint_profile != self.identity.endpoint_profile
            || !Arc::ptr_eq(&self.metadata_target.fence(), &self.fence)
            || !Arc::ptr_eq(&self.data_target.fence(), &self.fence)
        {
            return Err(SnapshotUploadServiceError::IdentityConflict(
                "resolved Snapshot target differs from its canonical write domain".into(),
            ));
        }
        Ok(ResolvedSnapshotUploadTarget {
            metadata_target: self.metadata_target.clone(),
            data_target: self.data_target.clone(),
            storage,
            compatibility,
        })
    }
}

#[derive(Clone)]
pub struct DaemonCompositionConfig {
    pub backing_root: PathBuf,
    pub upload_enabled: bool,
    pub(crate) registry_clients: Vec<Arc<crate::snapshot::transport::registry::RegistryClient>>,
    pub(crate) registry_publication:
        Option<Arc<crate::snapshot::io::oci_publication::RegistrySnapshotPublication>>,
    pub(crate) local_cache: Option<Arc<LocalFileCache>>,
    pub(crate) image_metadata_store: Option<Arc<dyn MetadataObjectStore>>,
    pub daemon_epoch: String,
    pub max_range_bytes: u64,
    pub max_inflight_per_vm: usize,
    pub max_pending_per_vm: usize,
    pub remote_load_timeout: Duration,
    pub latest_memory_prefetch: LatestMemoryPrefetchConfig,
    pub required_capabilities: Vec<Capability>,
    pub runtime_start_timeout: Duration,
    pub control_timeout: Duration,
    pub max_concurrent_starts: usize,
    pub shutdown_timeout: Duration,
    pub upload_retry_base: Duration,
    pub upload_retry_max: Duration,
    pub data_upload: SnapshotDataUploadConfig,
    pub base_image_concurrency: usize,
    pub base_image_attempts: u64,
    pub base_image_retry_base: Duration,
    pub base_image_retry_max: Duration,
    pub metrics: Option<Arc<SnapdMetrics>>,
}

impl DaemonCompositionConfig {
    pub fn bounded(backing_root: PathBuf, daemon_epoch: String) -> Self {
        Self {
            backing_root,
            upload_enabled: false,
            registry_clients: Vec::new(),
            registry_publication: None,
            local_cache: None,
            image_metadata_store: None,
            daemon_epoch,
            max_range_bytes: DEFAULT_MAX_RANGE_BYTES,
            max_inflight_per_vm: 8,
            max_pending_per_vm: 256,
            remote_load_timeout: Duration::from_secs(30),
            latest_memory_prefetch: LatestMemoryPrefetchConfig::default(),
            required_capabilities: Vec::new(),
            runtime_start_timeout: Duration::from_secs(30),
            control_timeout: Duration::from_secs(10),
            max_concurrent_starts: MAX_CONCURRENT_STARTS,
            shutdown_timeout: Duration::from_secs(10),
            upload_retry_base: Duration::from_millis(100),
            upload_retry_max: Duration::from_secs(30),
            data_upload: SnapshotDataUploadConfig::default(),
            base_image_concurrency: 2,
            base_image_attempts: 5,
            base_image_retry_base: Duration::from_secs(1),
            base_image_retry_max: Duration::from_secs(30),
            metrics: None,
        }
    }

    fn validate(&self) -> Result<(), DaemonCompositionError> {
        if !self.backing_root.is_absolute()
            || self.daemon_epoch.is_empty()
            || self.max_range_bytes == 0
            || self.max_inflight_per_vm == 0
            || self.max_pending_per_vm < self.max_inflight_per_vm
            || self.remote_load_timeout.is_zero()
            || self.latest_memory_prefetch.validate().is_err()
            || self.data_upload.validate().is_err()
        {
            return Err(DaemonCompositionError::Invalid(
                "runtime roots, epoch or data-plane limits are invalid".into(),
            ));
        }
        Ok(())
    }
}

// Node services share this ownership graph so backing, upload, ingestion,
// remote loading and VMM cleanup have one bounded lifetime.
pub struct DaemonCompositionInputs {
    pub store: Option<Arc<dyn ImmutableObjectStore>>,
    pub expected_storage: Option<StorageDescriptor>,
    pub range_provider_name: String,
    pub range_provider: Arc<dyn RangeReader>,
    pub range_authorization: Option<CredentialIdentity>,
    pub upload_resolver: Arc<dyn SnapshotUploadTargetResolver>,
    pub base_image_builder: Arc<dyn BaseImageBuilder>,
    pub runtime_launch_factory: RuntimeLaunchFactory,
}

#[derive(Debug, Error)]
pub enum DaemonCompositionError {
    #[error("invalid Rust snapd composition: {0}")]
    Invalid(String),
    #[error("Rust snapd composition failed: {0}")]
    Component(String),
    #[error("Rust snapd shutdown failed: {0}")]
    Shutdown(String),
}

pub struct DaemonComposition {
    pub catalog: Arc<SnapshotRepository>,
    pub vm: Arc<VmService>,
    pub repository: VmRepository,
    pub remote_load: Arc<RangeLoadExecutor>,
    pub upload: Arc<SnapshotUploadService>,
    pub base_images: Arc<BaseImageIngestionService>,
    pub metrics: Arc<SnapdMetrics>,
}

impl DaemonComposition {
    pub fn new(
        config: DaemonCompositionConfig,
        inputs: DaemonCompositionInputs,
    ) -> Result<Arc<Self>, DaemonCompositionError> {
        config.validate()?;
        if inputs.range_provider_name.is_empty() {
            return Err(DaemonCompositionError::Invalid(
                "range provider identity is empty".into(),
            ));
        }
        let metrics = match config.metrics.clone() {
            Some(metrics) => metrics,
            None => SnapdMetrics::new(&inputs.range_provider_name).map_err(composition_error)?,
        };
        let catalog = SnapshotRepository::new();
        let backings = LocalBackingRegistry::new_with_metrics(
            &config.backing_root,
            &config.daemon_epoch,
            Some(Arc::clone(&metrics)),
        )
        .map_err(composition_error)?;
        metrics.install_disk_sources(
            config.local_cache.as_ref().map(|cache| cache.byte_cache()),
            &backings,
        );
        let source_resolver: Arc<dyn crate::snapshot::SourceResolver> = Arc::new(
            crate::snapshot::format::overlaybd_oci::OverlaybdOciSourceResolver::new_with_registry_clients(
                config.local_cache.clone(),
                config.registry_clients.clone(),
            ),
        );
        let loader = SnapshotIo::new_with_optional_storage_identity(
            inputs.store,
            inputs.expected_storage,
            crate::snapshot::io::snapshot_io::SnapshotLoadLimits::default(),
        )
        .map_err(composition_error)?
        .with_source_resolver(Arc::clone(&source_resolver))
        .with_range_reader(Arc::clone(&inputs.range_provider));
        let snapshot_reader: Arc<dyn SnapshotReader> = loader;
        let remote_load = RangeLoadExecutor::new_with_metrics_and_timeout(
            backings,
            Arc::clone(&snapshot_reader),
            HashMap::from([(inputs.range_provider_name.clone(), inputs.range_provider)]),
            config.max_range_bytes,
            config.remote_load_timeout,
            Arc::clone(&metrics),
        )
        .map_err(composition_error)?;
        if let Some(store) = config
            .image_metadata_store
            .as_ref()
            .filter(|_| config.upload_enabled)
        {
            // The daemon selects the metadata write domain. Registry-backed
            // snapshots keep their DATA in Registry and publish only hints here.
            let metadata = ObjectStoreImageMetadata::new(Arc::clone(store));
            let reader: Arc<dyn SnapshotImageMetadataReader> = metadata.clone();
            let uploader: Arc<dyn SnapshotImageMetadataUploader> = metadata;
            remote_load
                .configure_snapshot_image_metadata(reader, uploader)
                .map_err(composition_error)?;
        }
        let repository = VmRepository::new_with_remote_load(
            None,
            config.required_capabilities,
            config.runtime_start_timeout,
            config.control_timeout,
            config.max_concurrent_starts,
            config.shutdown_timeout,
            remote_load.clone(),
            RemoteFaultLimits {
                max_inflight_per_vm: config.max_inflight_per_vm,
                max_pending_per_vm: config.max_pending_per_vm,
                remote_load_timeout: config.remote_load_timeout,
            },
        )
        .map_err(composition_error)?;
        let capture = Arc::new(
            SnapshotCaptureService::new_with_metrics(
                config.control_timeout,
                Some(Arc::clone(&metrics)),
            )
            .map_err(composition_error)?,
        );
        let mut publisher = SnapshotPublisher::new_with_config(
            config.data_upload.clone(),
            Some(Arc::clone(&metrics)),
        )
        .map_err(composition_error)?
        .with_source_resolver(Arc::clone(&source_resolver));
        if let Some(registry) = &config.registry_publication {
            publisher = publisher.with_registry_publication(Arc::clone(registry));
        }
        let upload = SnapshotUploadService::new_with_metrics_and_local_cache(
            publisher,
            inputs.upload_resolver,
            config.upload_retry_base,
            config.upload_retry_max,
            Some(Arc::clone(&metrics)),
            config.local_cache.clone(),
        )
        .map_err(composition_error)?
        .with_enabled(config.upload_enabled)
        .with_catalog(Arc::clone(&catalog));
        let base_images = BaseImageIngestionService::new(
            inputs.base_image_builder,
            upload.clone(),
            config.base_image_concurrency,
            config.base_image_attempts,
            config.base_image_retry_base,
            config.base_image_retry_max,
        )
        .map_err(composition_error)?;
        let preparer = SnapshotRestorePreparer::new_with_metrics_and_prefetch(
            remote_load.clone(),
            inputs.range_provider_name,
            inputs.range_authorization,
            Some(Arc::clone(&metrics)),
            config.latest_memory_prefetch,
        )
        .map_err(composition_error)?;
        let vm = Arc::new(
            VmService::new(
                repository.clone(),
                capture.clone(),
                inputs.runtime_launch_factory,
            )
            .with_catalog(Arc::clone(&catalog))
            .with_upload(upload.clone())
            .with_base_image_ingestion(base_images.clone())
            .with_remote_start(snapshot_reader, preparer)
            .with_metrics(Arc::clone(&metrics))
            .with_async_start_journal(
                config.backing_root.join("async-starts"),
                config.runtime_start_timeout,
            )
            .map_err(composition_error)?,
        );
        let recovery_handle = vm.recovery_handle();
        let release_capture = capture.clone();
        let release_snapshots = recovery_handle.clone();
        let release_remote_load = remote_load.clone();
        repository
            .register_release_hook(Arc::new(move |runtime| {
                let capture = release_capture.clone();
                let snapshot_runtime = release_snapshots.clone();
                let remote_load = release_remote_load.clone();
                Box::pin(async move {
                    let capture_result = capture
                        .release_runtime(&runtime)
                        .await
                        .map_err(|error| error.to_string());
                    snapshot_runtime.release_runtime(&runtime).await;
                    remote_load.revoke_runtime(&runtime).await;
                    capture_result.map(|_| ())
                })
            }))
            .map_err(composition_error)?;
        Ok(Arc::new(Self {
            catalog,
            vm,
            repository,
            remote_load,
            upload,
            base_images,
            metrics,
        }))
    }

    pub async fn close(&self) -> Result<(), DaemonCompositionError> {
        let mut failures = Vec::new();
        if let Err(error) = self.vm.close_async_starts().await {
            failures.push(error.to_string());
        }
        let mut vm_ids = self
            .repository
            .vm_ids()
            .await
            .into_iter()
            .collect::<Vec<_>>();
        vm_ids.sort();
        for vm_id in vm_ids {
            match self
                .repository
                .stop(&vm_id, "Rust snapd daemon shutdown")
                .await
            {
                // An exit watcher may remove a listed VM before this stop.
                Err(VmRepositoryError::NotFound(_)) | Ok(_) => {}
                Err(error) => failures.push(error.to_string()),
            }
        }
        if let Err(error) = self.base_images.close().await {
            failures.push(error.to_string());
        }
        if let Err(error) = self.upload.close().await {
            failures.push(error.to_string());
        }
        self.remote_load.close().await;
        if failures.is_empty() {
            Ok(())
        } else {
            Err(DaemonCompositionError::Shutdown(failures.join("; ")))
        }
    }
}

fn composition_error(error: impl std::fmt::Display) -> DaemonCompositionError {
    DaemonCompositionError::Component(error.to_string())
}

#[derive(Clone)]
struct LoadedRockletArtifact {
    file: Arc<File>,
    identity: RockletArtifactIdentity,
    guest_port: u32,
    proxy_port_start: u16,
    proxy_port_end: u16,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RockletArtifactManifest {
    schema_version: u32,
    identity: String,
    version: String,
    sha256: String,
    logical_bytes: u64,
    guest_entrypoint: String,
    guest_port: u32,
}

#[derive(Clone)]
struct FixedUploadResolver {
    target: ResolvedSnapshotUploadTarget,
}

#[async_trait]
impl SnapshotUploadTargetResolver for FixedUploadResolver {
    async fn resolve(&self) -> Result<ResolvedSnapshotUploadTarget, SnapshotUploadServiceError> {
        Ok(self.target.clone())
    }
}

struct DisabledUploadResolver;

#[async_trait]
impl SnapshotUploadTargetResolver for DisabledUploadResolver {
    async fn resolve(&self) -> Result<ResolvedSnapshotUploadTarget, SnapshotUploadServiceError> {
        Err(SnapshotUploadServiceError::UploadDisabled)
    }
}

/// Each destination repository owns one mutation fence for the lifetime of this
/// daemon. Captures and their retries resolve to the same scoped capabilities.
struct RegistryUploadResolver {
    client: Arc<crate::snapshot::transport::registry::RegistryClient>,
    publication: Arc<crate::snapshot::io::oci_publication::RegistrySnapshotPublication>,
    domains: Arc<SnapshotWriteDomainRegistry>,
    repository: Option<String>,
    namespace: Option<String>,
    credential_ref: String,
    compatibility: VmmCompatibility,
    targets: StdMutex<HashMap<String, ResolvedSnapshotUploadTarget>>,
}

impl RegistryUploadResolver {
    fn repository_for(
        &self,
        provenance: Option<&SnapshotProvenance>,
    ) -> Result<String, SnapshotUploadServiceError> {
        if let Some(repository) = &self.repository {
            return Ok(repository.clone());
        }
        let reference = provenance
            .and_then(|source| source.oci_ref.as_deref())
            .ok_or_else(|| SnapshotUploadServiceError::Invalid(
                "automatic OCI upload requires the original source image reference; configure a fixed repository for snapshots without provenance".into(),
            ))?;
        let source = crate::snapshot::transport::registry::RegistryReference::parse(reference)
            .map_err(|error| SnapshotUploadServiceError::Invalid(error.to_string()))?;
        Ok(match &self.namespace {
            Some(namespace) => format!("{namespace}/{}", source.repository),
            None => source.repository,
        })
    }

    fn resolve_target(
        &self,
        provenance: Option<&SnapshotProvenance>,
    ) -> Result<ResolvedSnapshotUploadTarget, SnapshotUploadServiceError> {
        let repository = self.repository_for(provenance)?;
        let mut targets = self.targets.lock().map_err(|_| {
            SnapshotUploadServiceError::Invalid("Registry target lock is poisoned".into())
        })?;
        if let Some(target) = targets.get(&repository) {
            return Ok(target.clone());
        }
        let namespace = format!("{}/{}", self.client.registry(), repository);
        let fence = NamespaceMutationFence::new(&namespace)
            .map_err(|error| SnapshotUploadServiceError::Invalid(error.to_string()))?;
        let writer = crate::snapshot::transport::registry::RegistryWriteTarget::new(
            Arc::clone(&self.client),
            repository.clone(),
            format!("registry:{namespace}"),
            fence,
        )
        .map_err(|error| SnapshotUploadServiceError::Invalid(error.to_string()))?;
        let domain = self.domains.register(
            SnapshotWriteDomainIdentity {
                provider: StorageProvider::Registry,
                endpoint_profile: "registry".into(),
                namespace,
            },
            self.publication.metadata_store_for(Arc::clone(&writer))?,
            writer.clone(),
            writer.fence(),
        )?;
        let target = domain.resolved_target(
            StorageDescriptor {
                provider: StorageProvider::Registry,
                endpoint_profile: "registry".into(),
                credential_ref: self.credential_ref.clone(),
            },
            self.compatibility.clone(),
        )?;
        targets.insert(repository, target.clone());
        Ok(target)
    }
}

#[async_trait]
impl SnapshotUploadTargetResolver for RegistryUploadResolver {
    async fn resolve(&self) -> Result<ResolvedSnapshotUploadTarget, SnapshotUploadServiceError> {
        self.resolve_target(None)
    }

    async fn resolve_for(
        &self,
        provenance: Option<&SnapshotProvenance>,
    ) -> Result<ResolvedSnapshotUploadTarget, SnapshotUploadServiceError> {
        self.resolve_target(provenance)
    }
}

struct ConfiguredRegistryClients {
    upload: Option<Arc<crate::snapshot::transport::registry::RegistryClient>>,
    readers: Vec<Arc<crate::snapshot::transport::registry::RegistryClient>>,
}

fn configured_registry_clients(
    config: &RustSnapdConfig,
) -> Result<ConfiguredRegistryClients, DaemonError> {
    let upload = config
        .registry_upload_config()
        .map(|registry| {
            crate::snapshot::transport::registry::RegistryClient::new_configured_with_auth_realm(
                &registry.registry,
                registry.allow_http,
                registry.username.clone(),
                registry.password.clone(),
                registry.auth_realm.as_deref(),
            )
        })
        .transpose()
        .map_err(component)?;
    // Preserve the existing configured-authority read behavior. Other read
    // authorities continue to come from image/snapshot references.
    let readers = upload.iter().cloned().collect();
    Ok(ConfiguredRegistryClients { upload, readers })
}

fn node_cache_identity(
    storage: Option<(&S3NodeConfig, &str)>,
    registry: Option<(&super::config::RegistryNodeConfig, &str)>,
) -> (String, String) {
    if let Some((storage, source)) = storage {
        (
            format!(
                "s3\0{}\0{}\0{}",
                storage.endpoint_url, storage.region, storage.bucket
            ),
            source.to_owned(),
        )
    } else if let Some((registry, source)) = registry {
        (
            match &registry.repository {
                Some(repository) => format!("registry\0{}\0{}", registry.registry, repository),
                None => format!(
                    "registry\0{}\0source\0{}",
                    registry.registry,
                    registry.namespace.as_deref().unwrap_or("")
                ),
            },
            source.to_owned(),
        )
    } else {
        (
            "local\0snapshot-cache".into(),
            "snapd-local-cache:v1".into(),
        )
    }
}

pub fn load_config(path: &Path) -> Result<RustSnapdConfig, DaemonError> {
    let payload = read_private_file(path, MAX_CONFIG_BYTES, false)?;
    let config: RustSnapdConfig = serde_json::from_slice(&payload).map_err(|error| {
        DaemonError::Invalid(format!("configuration is not strict JSON: {error}"))
    })?;
    config.validate()?;
    Ok(config)
}

fn fault_around_pages(value: Option<&str>, default: u32) -> Result<u32, DaemonError> {
    match value {
        None => Ok(default),
        Some(value) => value
            .parse::<u32>()
            .ok()
            .filter(|pages| (1..=4096).contains(pages))
            .ok_or_else(|| {
                DaemonError::Invalid(format!(
                    "{FAULT_AROUND_PAGES_ENV} must be an integer in 1..=4096"
                ))
            }),
    }
}

fn latest_memory_prefetch_config(
    concurrency: Option<&str>,
    max_bytes: Option<&str>,
) -> Result<LatestMemoryPrefetchConfig, DaemonError> {
    let defaults = LatestMemoryPrefetchConfig::default();
    let concurrency = match concurrency {
        Some(value) => value.parse::<usize>().map_err(|_| {
            DaemonError::Invalid(format!(
                "{SNAPSHOT_PREFETCH_CONCURRENCY_ENV} must be one of 0, 1, 4 or 8"
            ))
        })?,
        None => defaults.concurrency,
    };
    let max_encoded_bytes = match max_bytes {
        Some(value) => value.parse::<u64>().map_err(|_| {
            DaemonError::Invalid(format!(
                "{SNAPSHOT_PREFETCH_MAX_BYTES_ENV} must be an integer in 0..={LATEST_MEMORY_PREFETCH_MAX_BYTES}"
            ))
        })?,
        None => defaults.max_encoded_bytes,
    };
    LatestMemoryPrefetchConfig {
        max_encoded_bytes,
        concurrency,
    }
    .validate()
    .map_err(|error| DaemonError::Invalid(error.to_string()))
}

fn optional_utf8_env(name: &str) -> Result<Option<String>, DaemonError> {
    match std::env::var(name) {
        Ok(value) => Ok(Some(value)),
        Err(std::env::VarError::NotPresent) => Ok(None),
        Err(std::env::VarError::NotUnicode(_)) => Err(DaemonError::Invalid(format!(
            "{name} must contain valid UTF-8"
        ))),
    }
}

fn s3_config_builder(
    endpoint_url: &str,
    region: &str,
    credentials: Credentials,
) -> aws_sdk_s3::config::Builder {
    aws_sdk_s3::Config::builder()
        .behavior_version(BehaviorVersion::latest())
        .endpoint_url(endpoint_url)
        .region(Region::new(region.to_owned()))
        .credentials_provider(credentials)
        .force_path_style(false)
}

pub async fn serve(config: RustSnapdConfig) -> Result<(), DaemonError> {
    config.validate()?;
    let uffd_legacy = std::env::var(ROCK_UFFD_LEGACY_ENV).unwrap_or_else(|_| "off".into());
    if !matches!(uffd_legacy.as_str(), "off" | "on") {
        return Err(DaemonError::Invalid(
            "ROCK_UFFD_LEGACY must be off or on".into(),
        ));
    }
    let mut vmm = VmmNodeConfig::image_defaults(uffd_legacy);
    vmm.fault_around_pages = fault_around_pages(
        optional_utf8_env(FAULT_AROUND_PAGES_ENV)?.as_deref(),
        vmm.fault_around_pages,
    )?;
    vmm.guest_readiness = config.guest_readiness;
    vmm.guest_network =
        match optional_utf8_env(crate::vm::runtime::network::NETWORK_ENV)?.as_deref() {
            None | Some("off") => false,
            Some("netns") => true,
            _ => {
                return Err(DaemonError::Invalid(
                    "ROCK_GUEST_NETWORK must be off or netns".into(),
                ))
            }
        };
    let base_image = BaseImageNodeConfig::image_defaults();
    let prefetch_concurrency = optional_utf8_env(SNAPSHOT_PREFETCH_CONCURRENCY_ENV)?;
    let prefetch_max_bytes = optional_utf8_env(SNAPSHOT_PREFETCH_MAX_BYTES_ENV)?;
    let latest_memory_prefetch = latest_memory_prefetch_config(
        prefetch_concurrency.as_deref(),
        prefetch_max_bytes.as_deref(),
    )?;
    let address = SocketAddr::from((Ipv4Addr::UNSPECIFIED, SNAPD_PORT));
    let data_upload_config = SnapshotDataUploadConfig {
        max_concurrent_uploads: MAX_CONCURRENT_DATA_UPLOADS,
        max_queued_uploads: MAX_QUEUED_DATA_UPLOADS,
        max_bytes_in_flight: MAX_DATA_UPLOAD_BYTES_IN_FLIGHT,
        chunk_bytes: DATA_UPLOAD_CHUNK_BYTES,
        attempt_timeout: Duration::from_secs(DATA_UPLOAD_ATTEMPT_TIMEOUT_SECONDS),
    };
    let s3 = if let Some(storage) = &config.storage {
        let credentials = load_s3_credentials(storage)?;
        let metadata_config =
            s3_config_builder(&storage.endpoint_url, &storage.region, credentials.clone())
                .http_client(HyperClientBuilder::new().build_https())
                .build();
        let upload_config = s3_config_builder(&storage.endpoint_url, &storage.region, credentials)
            .http_client(HyperClientBuilder::new().build_https())
            .build();
        let fence = NamespaceMutationFence::new(storage.bucket.clone()).map_err(component)?;
        Some(
            S3ImmutableObjectStore::new_with_upload_client(
                storage.bucket.clone(),
                aws_sdk_s3::Client::from_conf(metadata_config),
                aws_sdk_s3::Client::from_conf(upload_config),
                5 * 1024 * 1024,
                DATA_UPLOAD_CHUNK_BYTES,
                fence,
                ConditionalWriteDialect::for_endpoint(&storage.endpoint_url),
            )
            .map_err(component)?
            .with_physical_source(&storage.endpoint_url, &storage.region)
            .map_err(component)?,
        )
    } else {
        None
    };
    let metadata_store = s3
        .as_ref()
        .map(|store| store.clone() as Arc<dyn ImmutableObjectStore>);
    let storage = config.storage.as_ref().map(|storage| StorageDescriptor {
        provider: StorageProvider::S3,
        endpoint_profile: STORAGE_ENDPOINT_PROFILE.into(),
        credential_ref: storage.credential_ref.clone(),
    });
    let compatibility = VmmCompatibility {
        architecture: std::env::consts::ARCH.into(),
        vmm_type: "cloud-hypervisor".into(),
        vmm_state_format_version: Some(1),
        rocklet_artifact: None,
    };
    let registry = SnapshotWriteDomainRegistry::new();
    let mut target = None;
    if config.upload_format == super::config::SnapshotUploadFormat::OverlaybdS3 {
        let s3 = s3.as_ref().ok_or_else(|| {
            DaemonError::Invalid("S3 upload requires storage configuration".into())
        })?;
        let domain = registry
            .register(
                SnapshotWriteDomainIdentity {
                    provider: StorageProvider::S3,
                    endpoint_profile: STORAGE_ENDPOINT_PROFILE.into(),
                    namespace: ImmutableObjectStore::namespace(s3.as_ref()).to_owned(),
                },
                s3.clone(),
                s3.clone(),
                ImmutableObjectStore::fence(s3.as_ref()),
            )
            .map_err(component)?;
        target = Some(
            domain
                .resolved_target(
                    storage.clone().expect("S3 storage identity configured"),
                    compatibility.clone(),
                )
                .map_err(component)?,
        );
    }
    let ConfiguredRegistryClients {
        upload: configured_registry_client,
        readers: registry_clients,
    } = configured_registry_clients(&config)?;
    let (cache_domain, cache_source) = node_cache_identity(
        config
            .storage
            .as_ref()
            .zip(s3.as_ref())
            .map(|(storage, s3)| {
                (
                    storage,
                    crate::snapshot::transport::PhysicalRangeReader::source_id(s3.as_ref()),
                )
            }),
        config
            .registry_upload_config()
            .zip(configured_registry_client.as_ref())
            .map(|(registry, client)| {
                (
                    registry,
                    crate::snapshot::transport::PhysicalRangeReader::source_id(client.as_ref()),
                )
            }),
    );
    let puller = SkopeoOciLayoutPuller::new(&base_image.skopeo_binary).map_err(component)?;
    let local_cache = LocalFileCache::new_with_source_and_limit(
        LOCAL_OBJECT_CACHE_ROOT.into(),
        &cache_domain,
        &cache_source,
        config.local_cache.max_bytes,
    )
    .map_err(component)?;
    let mut registry_publication = None;
    let mut registry_resolver: Option<Arc<dyn SnapshotUploadTargetResolver>> = None;
    if config.upload_format == super::config::SnapshotUploadFormat::OverlaybdOci {
        let registry_config = config.registry_upload_config().ok_or_else(|| {
            DaemonError::Invalid("OCI upload requires Registry configuration".into())
        })?;
        let client = configured_registry_client
            .ok_or_else(|| DaemonError::Invalid("Registry client is not configured".into()))?;
        let publication =
            crate::snapshot::io::oci_publication::RegistrySnapshotPublication::new_configured(
                Arc::clone(&client),
                metadata_store.clone(),
                s3.as_ref().map(|s3| {
                    s3.clone() as Arc<dyn crate::snapshot::transport::PhysicalRangeReader>
                }),
                Some(local_cache.clone() as Arc<dyn crate::snapshot::transport::PhysicalRangeCache>),
                registry_config.tag_prefix.clone(),
                data_upload_config.clone(),
            )
            .with_tag_mode(registry_config.tag_mode);
        registry_resolver = Some(Arc::new(RegistryUploadResolver {
            client,
            publication: Arc::clone(&publication),
            domains: Arc::clone(&registry),
            repository: registry_config.repository.clone(),
            namespace: registry_config.namespace.clone(),
            credential_ref: registry_config.credential_ref.clone(),
            compatibility,
            targets: StdMutex::new(HashMap::new()),
        }));
        registry_publication = Some(publication);
    }
    let resolver: Arc<dyn SnapshotUploadTargetResolver> = match (registry_resolver, target) {
        (Some(resolver), _) => resolver,
        (_, Some(target)) => Arc::new(FixedUploadResolver { target }),
        _ if !config.upload_format.enabled() => Arc::new(DisabledUploadResolver),
        _ => {
            return Err(DaemonError::Invalid(
                "No upload target is configured".into(),
            ))
        }
    };
    let source_resolver = Arc::new(
        crate::snapshot::format::overlaybd_oci::OverlaybdOciSourceResolver::new_with_registry_clients(
            Some(Arc::clone(&local_cache)), registry_clients.clone(),
        ),
    );
    let base_builder: Arc<dyn BaseImageBuilder> =
        OciExt4BaseImageBuilder::new_with_source_resolver(
            OciExt4BuilderConfig {
                cache_root: base_image.cache_root.clone(),
                worker_binary: base_image.worker_binary.clone(),
                fakeroot_binary: base_image.fakeroot_binary.clone(),
                mke2fs_binary: base_image.mke2fs_binary.clone(),
                e2fsck_binary: base_image.e2fsck_binary.clone(),
                debugfs_binary: base_image.debugfs_binary.clone(),
                minimum_image_bytes: base_image.minimum_image_bytes,
                headroom_bytes: base_image.headroom_bytes,
                timeout: Duration::from_secs(base_image.timeout_seconds),
            },
            puller,
            source_resolver,
            registry_clients.clone(),
        )
        .map_err(component)?;
    let rocklet_artifact = prepare_runtime_roots(&vmm, &base_image)?;
    let daemon_epoch = uuid::Uuid::new_v4().simple().to_string();
    let mut composition_config =
        DaemonCompositionConfig::bounded(BACKING_ROOT.into(), daemon_epoch.clone());
    composition_config.upload_enabled = config.upload_format.enabled();
    composition_config.local_cache = Some(local_cache.clone());
    composition_config.registry_clients = registry_clients.clone();
    composition_config.registry_publication = registry_publication;
    composition_config.image_metadata_store = s3
        .as_ref()
        .map(|s3| s3.clone() as Arc<dyn MetadataObjectStore>);
    composition_config.max_inflight_per_vm = MAX_INFLIGHT_PER_VM;
    composition_config.max_pending_per_vm = MAX_PENDING_PER_VM;
    composition_config.remote_load_timeout = Duration::from_secs(REMOTE_LOAD_TIMEOUT_SECONDS);
    composition_config.runtime_start_timeout = Duration::from_secs(RUNTIME_START_TIMEOUT_SECONDS);
    composition_config.latest_memory_prefetch = latest_memory_prefetch;
    composition_config.data_upload = data_upload_config;
    let provider_name = if s3.is_some() { "s3" } else { "registry" };
    let metrics = SnapdMetrics::new(provider_name).map_err(component)?;
    composition_config.metrics = Some(Arc::clone(&metrics));
    let launch_factory = launch_factory(vmm, daemon_epoch, rocklet_artifact);
    let immutable_store = metadata_store;
    let peer = P2PClient::new(
        P2PClientConfig {
            enabled: true,
            port: SNAPD_PORT,
            probe_timeout: Duration::from_millis(P2P_PROBE_TIMEOUT_MS),
            range_timeout: Duration::from_millis(P2P_RANGE_TIMEOUT_MS),
        },
        metrics,
    )
    .map_err(component)?;
    let physical_cache: Arc<dyn crate::snapshot::transport::PhysicalRangeCache> = local_cache;
    let range_provider: Arc<dyn RangeReader> = match s3 {
        Some(s3) => RangeIO::new_with_cache(s3, Some(peer), Some(physical_cache))
            .with_registry_clients(registry_clients),
        None => RangeIO::for_fixed_targets().with_registry_clients(registry_clients),
    };
    let composition = DaemonComposition::new(
        composition_config,
        DaemonCompositionInputs {
            store: immutable_store,
            expected_storage: storage,
            range_provider_name: provider_name.into(),
            range_provider,
            range_authorization: config.storage.as_ref().map(|storage| CredentialIdentity {
                endpoint_profile: STORAGE_ENDPOINT_PROFILE.into(),
                credential_ref: storage.credential_ref.clone(),
                client_identity: STORAGE_CLIENT_IDENTITY.into(),
            }),
            upload_resolver: resolver,
            base_image_builder: base_builder,
            runtime_launch_factory: launch_factory,
        },
    )?;
    composition
        .metrics
        .install_structured_logger(StructuredLogger::stderr())
        .map_err(component)?;
    let listener = TcpListener::bind(address).await.map_err(DaemonError::Io)?;
    composition.metrics.emit_structured(StructuredEvent::new(
        StructuredLogLevel::Info,
        StructuredEventName::DaemonLifecycle,
        StructuredComponent::Daemon,
        StructuredOutcome::Started,
    ));
    let service = SnapdControlHttpService::new_with_metrics(
        SnapdControlServices::new(composition.vm.clone(), composition.base_images.clone()),
        Some(Arc::clone(&composition.metrics)),
    );
    let peer_service = P2pService::new(
        composition.vm.clone(),
        Some(Arc::clone(&composition.metrics)),
    );
    let (shutdown, shutdown_rx) = watch::channel(false);
    let mut server_task = tokio::spawn(serve_connections_until(
        listener,
        service,
        peer_service,
        wait_for_shutdown(shutdown_rx.clone()),
    ));
    enum FirstExit {
        Signal,
        Server(io::Result<()>),
    }
    let first = tokio::select! {
        _ = shutdown_signal() => FirstExit::Signal,
        result = &mut server_task => FirstExit::Server(join_server("http", result)),
    };
    let _ = shutdown.send(true);
    let server_result = match first {
        FirstExit::Signal => join_server("http", server_task.await),
        FirstExit::Server(result) => result,
    };
    let close = composition.close().await;
    if let Err(error) = server_result {
        composition.metrics.emit_structured(
            StructuredEvent::new(
                StructuredLogLevel::Error,
                StructuredEventName::DaemonLifecycle,
                StructuredComponent::Daemon,
                StructuredOutcome::Error,
            )
            .error_code("listener_failed"),
        );
        return Err(DaemonError::Io(error));
    }
    match close {
        Ok(()) => {
            composition.metrics.emit_structured(StructuredEvent::new(
                StructuredLogLevel::Info,
                StructuredEventName::DaemonLifecycle,
                StructuredComponent::Daemon,
                StructuredOutcome::Stopped,
            ));
            Ok(())
        }
        Err(error) => {
            composition.metrics.emit_structured(
                StructuredEvent::new(
                    StructuredLogLevel::Error,
                    StructuredEventName::DaemonLifecycle,
                    StructuredComponent::Daemon,
                    StructuredOutcome::Error,
                )
                .error_code("shutdown_failed"),
            );
            Err(error.into())
        }
    }
}

// HTTP and peer TCP share one port. Endpoint handlers retain request semantics;
// bootstrap owns accepted connection tasks and shutdown.
async fn serve_connections_until<A, P, F>(
    listener: TcpListener,
    http: Arc<SnapdControlHttpService<A>>,
    p2p: Arc<P2pService<P>>,
    shutdown: F,
) -> io::Result<()>
where
    A: SnapdControlApi,
    P: P2pApi,
    F: Future<Output = ()> + Send,
{
    tokio::pin!(shutdown);
    let mut connections = JoinSet::new();
    loop {
        tokio::select! {
            biased;
            _ = &mut shutdown => break,
            accepted = listener.accept() => {
                let (stream, _) = accepted?;
                stream.set_nodelay(true)?;
                let http = Arc::clone(&http);
                let p2p = Arc::clone(&p2p);
                connections.spawn(async move {
                    if is_p2p_tcp_connection(&stream).await? {
                        p2p.serve_connection(stream).await
                    } else {
                        Http::new()
                            .http1_only(true)
                            .http1_keep_alive(false)
                            .serve_connection(stream, service_fn(move |request| {
                                let http = Arc::clone(&http);
                                async move { Ok::<_, Infallible>(http.handle(request).await) }
                            }))
                            .await
                            .map_err(io::Error::other)
                    }
                });
            }
            Some(_) = connections.join_next(), if !connections.is_empty() => {}
        }
    }
    connections.shutdown().await;
    Ok(())
}

async fn is_p2p_tcp_connection(stream: &TcpStream) -> io::Result<bool> {
    let mut first = [0_u8; 1];
    match stream.peek(&mut first).await? {
        0 => Err(io::Error::new(
            io::ErrorKind::UnexpectedEof,
            "connection closed before protocol preface",
        )),
        _ => Ok(first[0] == P2P_TCP_MAGIC[0]),
    }
}

async fn wait_for_shutdown(mut shutdown: watch::Receiver<bool>) {
    if *shutdown.borrow() {
        return;
    }
    let _ = shutdown.changed().await;
}

fn join_server(
    label: &str,
    result: Result<io::Result<()>, tokio::task::JoinError>,
) -> io::Result<()> {
    result.map_err(|error| io::Error::other(format!("{label} server task failed: {error}")))?
}

fn load_rocklet_artifact(
    config: &RockletArtifactConfig,
) -> Result<LoadedRockletArtifact, DaemonError> {
    let artifact = open_immutable_regular(&config.path, None)?;
    let artifact_bytes = artifact.metadata().map_err(DaemonError::Io)?.len();
    let mut manifest_path = config.path.as_os_str().to_os_string();
    manifest_path.push(".manifest.json");
    let mut manifest_file = open_immutable_regular(Path::new(&manifest_path), Some(64 * 1024))?;
    let mut manifest_bytes = Vec::new();
    manifest_file
        .read_to_end(&mut manifest_bytes)
        .map_err(DaemonError::Io)?;
    let manifest: RockletArtifactManifest =
        serde_json::from_slice(&manifest_bytes).map_err(|error| {
            DaemonError::Invalid(format!(
                "Rocklet artifact manifest is not strict JSON: {error}"
            ))
        })?;
    if manifest.schema_version != 1
        || !valid_prefixed_sha256(&manifest.identity)
        || !valid_lower_sha256(&manifest.sha256)
        || !valid_version(&manifest.version)
        || manifest.logical_bytes == 0
        || !manifest.logical_bytes.is_multiple_of(512)
        || manifest.logical_bytes != artifact_bytes
        || manifest.guest_entrypoint != "/opt/rocklet/bin/rocklet"
        || manifest.guest_port != config.guest_port
    {
        return Err(DaemonError::Invalid(
            "Rocklet artifact manifest or immutable file metadata is invalid".into(),
        ));
    }
    Ok(LoadedRockletArtifact {
        file: Arc::new(artifact),
        identity: RockletArtifactIdentity {
            identity: manifest.identity,
            version: manifest.version,
            sha256: manifest.sha256,
            logical_bytes: manifest.logical_bytes,
        },
        guest_port: config.guest_port,
        proxy_port_start: config.proxy_port_start,
        proxy_port_end: config.proxy_port_end,
    })
}

fn valid_prefixed_sha256(value: &str) -> bool {
    value
        .strip_prefix("sha256:")
        .is_some_and(valid_lower_sha256)
}

fn valid_lower_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn valid_version(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-' | b'+'))
}

fn open_immutable_regular(path: &Path, max_bytes: Option<u64>) -> Result<File, DaemonError> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW)
        .open(path)
        .map_err(DaemonError::Io)?;
    let metadata = file.metadata().map_err(DaemonError::Io)?;
    if !metadata.is_file()
        || metadata.permissions().mode() & 0o222 != 0
        || max_bytes.is_some_and(|limit| metadata.len() > limit)
    {
        return Err(DaemonError::Invalid(format!(
            "trusted Rocklet asset {} must be immutable, regular, and bounded",
            path.display()
        )));
    }
    Ok(file)
}

fn prepare_runtime_roots(
    vmm: &VmmNodeConfig,
    base_image: &BaseImageNodeConfig,
) -> Result<LoadedRockletArtifact, DaemonError> {
    for path in [
        Path::new(BACKING_ROOT),
        &vmm.cache_root,
        &vmm.private_root,
        &base_image.cache_root,
    ] {
        std::fs::create_dir_all(path).map_err(DaemonError::Io)?;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))
            .map_err(DaemonError::Io)?;
    }
    for path in [&vmm.binary, &vmm.kernel] {
        if !path.is_file() {
            return Err(DaemonError::Invalid(format!(
                "trusted runtime asset {} is missing",
                path.display()
            )));
        }
    }
    if !vmm.initramfs.is_file() {
        return Err(DaemonError::Invalid(format!(
            "trusted initramfs {} is missing",
            vmm.initramfs.display()
        )));
    }
    load_rocklet_artifact(&vmm.rocklet)
}

fn launch_factory(
    config: VmmNodeConfig,
    daemon_epoch: String,
    rocklet: LoadedRockletArtifact,
) -> RuntimeLaunchFactory {
    Arc::new(move |plan| build_launch_spec(&config, &daemon_epoch, &rocklet, plan))
}

fn build_launch_spec(
    config: &VmmNodeConfig,
    _daemon_epoch: &str,
    rocklet: &LoadedRockletArtifact,
    plan: &RuntimeLaunchPlan,
) -> Result<VmmLaunchSpec, SnapshotControlError> {
    if plan.sandbox_id.is_empty()
        || plan.sandbox_id.len() > 128
        || !plan
            .sandbox_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
    {
        return Err(SnapshotControlError::Invalid(
            "guest sandbox identity is not a canonical kernel-command-line token".into(),
        ));
    }
    let cpu_count = bounded_resource_u64(
        &plan.resource_spec,
        "cpu",
        u64::from(config.default_cpu_count),
        1,
        256,
    )?;
    let memory_mib = bounded_resource_u64(
        &plan.resource_spec,
        "memory_mib",
        config.default_memory_mib,
        64,
        1024 * 1024,
    )?;
    let vm_hash = format!("{:x}", Sha256::digest(plan.sandbox_id.as_bytes()));
    let private = config.private_root.join(vm_hash);
    std::fs::create_dir_all(&private).map_err(|error| {
        SnapshotControlError::Runtime(format!("private VMM root failed: {error}"))
    })?;
    std::fs::set_permissions(&private, std::fs::Permissions::from_mode(0o700)).map_err(
        |error| SnapshotControlError::Runtime(format!("private VMM root mode failed: {error}")),
    )?;
    let vsock_cid = derive_vsock_cid(&plan.sandbox_id);
    let rocklet_vsock = private.join("rocklet.vsock");
    let mut kernel_cmdline = format!(
        "{} rocklet_identity={} rocklet_version={} rocklet_guest_port={} rock_sandbox_id={}",
        config.kernel_cmdline,
        rocklet.identity.identity,
        rocklet.identity.version,
        rocklet.guest_port,
        plan.sandbox_id,
    );
    if config.guest_network {
        kernel_cmdline.push_str(" rock_guest_network=netns-v1");
    }
    if config.guest_readiness == crate::vm::runtime::plugger::GuestReadinessMode::PluggerOnly {
        kernel_cmdline.push_str(" snapd_plugger_only=1");
    }
    let mut command = vec![
        config.binary.as_os_str().to_owned(),
        OsString::from("--cpus"),
        OsString::from(format!("boot={cpu_count},core_scheduling=off")),
        OsString::from("--memory"),
        OsString::from(format!("size={memory_mib}M,shared=on")),
        OsString::from("--kernel"),
        config.kernel.as_os_str().to_owned(),
        OsString::from("--cmdline"),
        OsString::from(kernel_cmdline),
        OsString::from("--disk"),
        OsString::from("id=rootfs,path=/dev/null"),
        OsString::from("--disk"),
        OsString::from(format!(
            "id=rocklet,path=/proc/self/fd/{},image_type=raw,readonly=on",
            ROCKLET_ARTIFACT_FD
        )),
        OsString::from("--vsock"),
        OsString::from(format!(
            "id=rocklet-vsock,cid={vsock_cid},socket={}",
            rocklet_vsock.display()
        )),
        OsString::from("--serial"),
        OsString::from(format!("file={}", private.join("serial.log").display())),
        OsString::from("--console"),
        OsString::from("off"),
    ];
    if config.guest_network {
        command.push(OsString::from("--net"));
        command.push(OsString::from(crate::vm::runtime::network::NET_ARGUMENT));
    }
    command.push(OsString::from("--initramfs"));
    command.push(config.initramfs.as_os_str().to_owned());
    let environment = BTreeMap::from([(ROCK_UFFD_LEGACY_ENV.into(), config.uffd_legacy.clone())]);
    Ok(VmmLaunchSpec {
        command,
        runtime: RuntimeIdentity::new(&plan.sandbox_id),
        launch_identity: format!("launch-{}", uuid::Uuid::new_v4().simple()),
        fault_around_pages: config.fault_around_pages,
        cache_root: config.cache_root.clone(),
        private_root: private,
        environment,
        cwd: None,
        guest_network: config.guest_network,
        guest_readiness: config.guest_readiness,
        guest_rocklet: Some(GuestRockletLaunch {
            artifact: Arc::clone(&rocklet.file),
            identity: rocklet.identity.clone(),
            guest_port: rocklet.guest_port,
            vsock_cid,
            proxy_port_start: rocklet.proxy_port_start,
            proxy_port_end: rocklet.proxy_port_end,
        }),
    })
}

fn derive_vsock_cid(sandbox_id: &str) -> u32 {
    let digest = Sha256::digest(sandbox_id.as_bytes());
    let mut cid = u32::from_le_bytes(digest[..4].try_into().expect("sha256 prefix"));
    if [u32::MAX, 0, 1, 2].contains(&cid) {
        cid = cid.wrapping_add(3);
    }
    cid
}

fn bounded_resource_u64(
    value: &serde_json::Value,
    field: &str,
    default: u64,
    minimum: u64,
    maximum: u64,
) -> Result<u64, SnapshotControlError> {
    let selected = value.get(field).map_or(Ok(default), |item| {
        item.as_u64().ok_or_else(|| {
            SnapshotControlError::Invalid(format!("resource_spec.{field} is not u64"))
        })
    })?;
    if !(minimum..=maximum).contains(&selected) {
        return Err(SnapshotControlError::Invalid(format!(
            "resource_spec.{field} is outside {minimum}..={maximum}"
        )));
    }
    Ok(selected)
}

fn load_s3_credentials(config: &S3NodeConfig) -> Result<Credentials, DaemonError> {
    Ok(Credentials::new(
        config.access_key_id.clone(),
        config.access_key_secret.clone(),
        config.session_token.clone(),
        None,
        "rock-snapd-inline-config",
    ))
}

fn read_private_file(
    path: &Path,
    maximum: u64,
    reject_empty: bool,
) -> Result<Vec<u8>, DaemonError> {
    if !path.is_absolute() {
        return Err(DaemonError::Invalid(
            "private file path is not absolute".into(),
        ));
    }
    let mut file = open_private_file(path).map_err(DaemonError::Io)?;
    let metadata = file.metadata().map_err(DaemonError::Io)?;
    let mode = metadata.permissions().mode() & 0o777;
    // SAFETY: geteuid has no preconditions.
    let euid = unsafe { libc::geteuid() };
    // SAFETY: getegid has no preconditions.
    let egid = unsafe { libc::getegid() };
    if !metadata.is_file()
        || !private_file_permissions_valid(metadata.uid(), metadata.gid(), mode, euid, egid)
        || metadata.len() > maximum
        || (reject_empty && metadata.len() == 0)
    {
        return Err(DaemonError::Invalid(
            "private file must be regular owner 0400 or root-owned runtime-group 0440 within its size bound".into(),
        ));
    }
    let mut payload = Vec::with_capacity(metadata.len() as usize);
    file.read_to_end(&mut payload).map_err(DaemonError::Io)?;
    Ok(payload)
}

fn open_private_file(path: &Path) -> io::Result<File> {
    const RESOLVE_NO_XDEV: u64 = 0x01;
    const RESOLVE_NO_MAGICLINKS: u64 = 0x02;
    const RESOLVE_BENEATH: u64 = 0x08;

    #[repr(C)]
    struct OpenHow {
        flags: u64,
        mode: u64,
        resolve: u64,
    }

    let parent = path
        .parent()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "private path has no parent"))?;
    let name = path.file_name().ok_or_else(|| {
        io::Error::new(io::ErrorKind::InvalidInput, "private path has no file name")
    })?;
    let name = CString::new(name.as_bytes()).map_err(|_| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "private file name contains NUL",
        )
    })?;
    let directory = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_CLOEXEC | libc::O_DIRECTORY | libc::O_NOFOLLOW)
        .open(parent)?;
    let metadata = directory.metadata()?;
    let mode = metadata.permissions().mode() & 0o777;
    // A projected ConfigMap/Secret is a root-owned, read-only bind mount whose
    // atomic writer uses relative symlinks below this directory.  Kubelet may
    // leave write bits on the host-side projection (notably when fsGroup is
    // active), so trust those bits only when this process' mount namespace
    // proves the bind itself is read-only.  On an ordinary writable mount the
    // runtime user must never be able to replace the links or their targets.
    // SAFETY: geteuid has no preconditions.
    let euid = unsafe { libc::geteuid() };
    let owner = metadata.uid();
    let writable_by_runtime = mode & 0o022 != 0 && !fd_is_on_read_only_mount(&directory)?;
    if !metadata.is_dir() || (owner != 0 && owner != euid) || writable_by_runtime {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "private file parent is not a trusted directory",
        ));
    }
    let how = OpenHow {
        flags: (libc::O_RDONLY | libc::O_CLOEXEC) as u64,
        mode: 0,
        resolve: RESOLVE_NO_XDEV | RESOLVE_NO_MAGICLINKS | RESOLVE_BENEATH,
    };
    // SAFETY: `directory` and `name` remain live for the syscall, `how` has
    // the kernel open_how layout, and a successful return is a new owned fd.
    let fd = unsafe {
        libc::syscall(
            libc::SYS_openat2,
            directory.as_raw_fd(),
            name.as_ptr(),
            &how,
            std::mem::size_of::<OpenHow>(),
        )
    } as libc::c_int;
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: the successful openat2 result is uniquely owned here.
    Ok(unsafe { File::from_raw_fd(fd) })
}

fn fd_is_on_read_only_mount(file: &File) -> io::Result<bool> {
    let mut filesystem = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    // SAFETY: `filesystem` points to writable storage for one statvfs value
    // and `file` owns a valid descriptor for the duration of the call.
    if unsafe { libc::fstatvfs(file.as_raw_fd(), filesystem.as_mut_ptr()) } != 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: fstatvfs returned success and initialized the entire value.
    let filesystem = unsafe { filesystem.assume_init() };
    Ok(filesystem.f_flag & libc::ST_RDONLY as libc::c_ulong != 0)
}

const fn private_file_permissions_valid(
    owner: libc::uid_t,
    group: libc::gid_t,
    mode: u32,
    effective_user: libc::uid_t,
    effective_group: libc::gid_t,
) -> bool {
    (owner == effective_user && mode == 0o400)
        || (owner == 0 && group == effective_group && mode == 0o440)
}

async fn shutdown_signal() {
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        .expect("SIGTERM handler installation failed");
    tokio::select! {
        _ = tokio::signal::ctrl_c() => {}
        _ = terminate.recv() => {}
    }
}

fn component(error: impl std::fmt::Display) -> DaemonError {
    DaemonError::Component(error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    #[test]
    fn fault_around_pages_preserves_default_and_accepts_bounded_overrides() {
        let default = VmmNodeConfig::image_defaults("off".into()).fault_around_pages;
        assert_eq!(fault_around_pages(None, default).unwrap(), 4);
        for pages in [1, 64, 4096] {
            assert_eq!(
                fault_around_pages(Some(&pages.to_string()), default).unwrap(),
                pages
            );
        }
    }

    #[test]
    fn fault_around_pages_rejects_invalid_or_out_of_range_overrides() {
        for value in ["0", "4097", "4294967296", "-1", "1.5", "many", ""] {
            let error = fault_around_pages(Some(value), 4).unwrap_err();
            assert!(matches!(error, DaemonError::Invalid(_)));
            assert!(error.to_string().contains(FAULT_AROUND_PAGES_ENV));
        }
    }

    fn source_repository_resolver(
        repository: Option<&str>,
        namespace: Option<&str>,
    ) -> RegistryUploadResolver {
        let client = crate::snapshot::transport::registry::RegistryClient::new_configured(
            "target.test:5000",
            true,
            None,
            None,
        )
        .unwrap();
        let publication =
            crate::snapshot::io::oci_publication::RegistrySnapshotPublication::new_configured(
                Arc::clone(&client),
                None,
                None,
                None,
                "snapshot-".into(),
                SnapshotDataUploadConfig::default(),
            );
        RegistryUploadResolver {
            client,
            publication,
            domains: SnapshotWriteDomainRegistry::new(),
            repository: repository.map(str::to_owned),
            namespace: namespace.map(str::to_owned),
            credential_ref: "push:v1".into(),
            compatibility: VmmCompatibility {
                architecture: "x86_64".into(),
                vmm_type: "cloud-hypervisor".into(),
                vmm_state_format_version: Some(1),
                rocklet_artifact: None,
            },
            targets: StdMutex::new(HashMap::new()),
        }
    }

    #[tokio::test]
    async fn source_repository_routing_preserves_paths_and_reuses_scoped_fences() {
        let resolver = source_repository_resolver(None, None);
        let source = |reference: &str| SnapshotProvenance {
            oci_ref: Some(reference.into()),
            oci_digest: Some(format!("sha256:{}", "a".repeat(64))),
        };
        let first = resolver
            .resolve_for(Some(&source("origin.test/team1/app:v1")))
            .await
            .unwrap();
        let second = resolver
            .resolve_for(Some(&source("origin.test/team2/app:v1")))
            .await
            .unwrap();
        let retry = resolver
            .resolve_for(Some(&source("origin.test/team1/app:v2")))
            .await
            .unwrap();
        assert_eq!(
            first.metadata_target.namespace(),
            "target.test:5000/team1/app"
        );
        assert_eq!(
            second.metadata_target.namespace(),
            "target.test:5000/team2/app"
        );
        assert!(Arc::ptr_eq(
            &first.metadata_target.fence(),
            &first.data_target.fence()
        ));
        assert!(Arc::ptr_eq(
            &first.metadata_target.fence(),
            &retry.metadata_target.fence()
        ));
        assert!(!Arc::ptr_eq(
            &first.metadata_target.fence(),
            &second.metadata_target.fence()
        ));
        assert_eq!(resolver.targets.lock().unwrap().len(), 2);
        assert!(resolver.resolve().await.is_err());

        let namespaced = source_repository_resolver(None, Some("snapshots"));
        let target = namespaced
            .resolve_for(Some(&source(&format!(
                "docker://origin.test/team1/app@sha256:{}",
                "b".repeat(64)
            ))))
            .await
            .unwrap();
        assert_eq!(
            target.metadata_target.namespace(),
            "target.test:5000/snapshots/team1/app"
        );
        assert!(namespaced
            .resolve_for(Some(&source("invalid")))
            .await
            .is_err());

        let legacy = source_repository_resolver(Some("fixed/snapshots"), None);
        assert_eq!(
            legacy.resolve().await.unwrap().metadata_target.namespace(),
            "target.test:5000/fixed/snapshots"
        );
    }

    #[test]
    fn registry_upload_and_legacy_alias_keep_the_existing_shared_read_client() {
        for field in ["registry_upload", "registry"] {
            let mut value = serde_json::json!({"upload_format": "overlaybd_oci"});
            value[field] = serde_json::json!({
                "registry": "snapshots.test:5000", "repository": "snapshots/full",
                "credential_ref": "push:v1", "allow_http": true,
                "username": "writer", "password": "write-password"
            });
            let config: RustSnapdConfig = serde_json::from_value(value).unwrap();
            config.validate().unwrap();
            let clients = configured_registry_clients(&config).unwrap();
            let upload = clients.upload.unwrap();
            assert_eq!(upload.registry(), "snapshots.test:5000");
            assert_eq!(clients.readers.len(), 1);
            assert!(Arc::ptr_eq(&upload, &clients.readers[0]));
        }
    }

    #[test]
    fn registry_and_storage_configuration_do_not_implicitly_enable_upload() {
        let config: RustSnapdConfig = serde_json::from_value(serde_json::json!({
            "storage": {
                "endpoint_url": "http://minio:9000", "region": "test", "bucket": "snapshots",
                "credential_ref": "s3:v1", "access_key_id": "test", "access_key_secret": "secret"
            },
            "registry_upload": {
                "registry": "snapshots.test", "repository": "snapshots",
                "credential_ref": "push:v1", "username": "writer", "password": "write-secret"
            }
        }))
        .unwrap();
        config.validate().unwrap();
        assert_eq!(
            config.upload_format,
            super::super::config::SnapshotUploadFormat::Disabled
        );
        let clients = configured_registry_clients(&config).unwrap();
        assert!(Arc::ptr_eq(
            clients.upload.as_ref().unwrap(),
            &clients.readers[0]
        ));
    }

    #[test]
    fn storage_only_configuration_keeps_no_configured_registry_clients() {
        let config: RustSnapdConfig = serde_json::from_value(serde_json::json!({
            "storage": {
                "endpoint_url": "http://minio:9000", "region": "test", "bucket": "snapshots",
                "credential_ref": "s3:v1", "access_key_id": "test", "access_key_secret": "secret"
            }
        }))
        .unwrap();
        config.validate().unwrap();
        let clients = configured_registry_clients(&config).unwrap();
        assert!(clients.upload.is_none());
        assert!(clients.readers.is_empty());
    }

    #[tokio::test]
    async fn local_only_configuration_has_a_cache_identity_without_an_upload_target() {
        let config: RustSnapdConfig = serde_json::from_value(serde_json::json!({})).unwrap();
        config.validate().unwrap();
        let clients = configured_registry_clients(&config).unwrap();
        assert!(clients.upload.is_none());
        assert!(clients.readers.is_empty());
        let directory = tempfile::tempdir().unwrap();
        let (domain, source) = node_cache_identity(None, None);
        let cache = LocalFileCache::new_with_source_and_limit(
            directory.path().to_owned(),
            &domain,
            &source,
            1024 * 1024,
        )
        .unwrap();
        assert_eq!(cache.source_id(), source);
        assert!(
            !DaemonCompositionConfig::bounded(directory.path().to_owned(), "local-epoch".into())
                .upload_enabled
        );
        assert!(matches!(
            DisabledUploadResolver.resolve().await,
            Err(SnapshotUploadServiceError::UploadDisabled)
        ));
    }

    #[test]
    fn plugger_readiness_is_opt_in_in_daemon_configuration() {
        let mut config = serde_json::json!({"storage": {
            "endpoint_url":"https://storage.invalid",
            "region":"test-region",
            "bucket":"test-bucket",
            "credential_ref":"snapd:v1",
            "access_key_id":"test-key",
            "access_key_secret":"test-secret"
        }});
        let default: RustSnapdConfig = serde_json::from_value(config.clone()).unwrap();
        assert_eq!(
            default.guest_readiness,
            crate::vm::runtime::plugger::GuestReadinessMode::Rocklet
        );
        config["guest_readiness"] = serde_json::json!("plugger_only");
        let plugger: RustSnapdConfig = serde_json::from_value(config.clone()).unwrap();
        assert_eq!(
            plugger.guest_readiness,
            crate::vm::runtime::plugger::GuestReadinessMode::PluggerOnly
        );
        config["guest_readiness"] = serde_json::json!("fallback");
        assert!(serde_json::from_value::<RustSnapdConfig>(config).is_err());
    }

    #[test]
    fn plugger_only_launch_carries_explicit_guest_boot_mode() {
        let directory = tempfile::tempdir().unwrap();
        let mut config = VmmNodeConfig::image_defaults("off".into());
        config.private_root = directory.path().join("private");
        let artifact = LoadedRockletArtifact {
            file: Arc::new(tempfile::tempfile().unwrap()),
            identity: RockletArtifactIdentity {
                identity: format!("sha256:{}", "a".repeat(64)),
                version: "test-v1".into(),
                sha256: "b".repeat(64),
                logical_bytes: 4096,
            },
            guest_port: 22_555,
            proxy_port_start: 31_000,
            proxy_port_end: 31_063,
        };
        let plan = RuntimeLaunchPlan {
            sandbox_id: "plugger-launch-test".into(),
            resource_spec: serde_json::json!({}),
        };
        let default = build_launch_spec(&config, "test-epoch", &artifact, &plan).unwrap();
        assert_eq!(
            default.guest_readiness,
            crate::vm::runtime::plugger::GuestReadinessMode::Rocklet
        );
        assert!(!default
            .command
            .iter()
            .any(|argument| argument.to_string_lossy().contains("snapd_plugger_only=")));
        config.guest_readiness = crate::vm::runtime::plugger::GuestReadinessMode::PluggerOnly;
        let plugger = build_launch_spec(&config, "test-epoch", &artifact, &plan).unwrap();
        assert_eq!(plugger.guest_readiness, config.guest_readiness);
        assert!(plugger
            .command
            .iter()
            .any(|argument| argument.to_string_lossy().contains("snapd_plugger_only=1")));
    }

    #[tokio::test]
    async fn protocol_selection_does_not_consume_the_endpoint_preface() {
        for preface in [P2P_TCP_MAGIC.as_slice(), b"GET /healthz HTTP/1.1\r\n"] {
            let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let mut client = TcpStream::connect(listener.local_addr().unwrap())
                .await
                .unwrap();
            let (mut server, _) = listener.accept().await.unwrap();
            client.write_all(preface).await.unwrap();
            assert_eq!(
                is_p2p_tcp_connection(&server).await.unwrap(),
                preface == P2P_TCP_MAGIC
            );
            let mut actual = vec![0; preface.len()];
            server.read_exact(&mut actual).await.unwrap();
            assert_eq!(actual, preface);
        }
    }

    #[tokio::test]
    async fn protocol_selection_rejects_a_connection_closed_before_its_preface() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let client = TcpStream::connect(listener.local_addr().unwrap())
            .await
            .unwrap();
        let (server, _) = listener.accept().await.unwrap();
        drop(client);
        assert_eq!(
            is_p2p_tcp_connection(&server).await.unwrap_err().kind(),
            io::ErrorKind::UnexpectedEof
        );
    }
}
