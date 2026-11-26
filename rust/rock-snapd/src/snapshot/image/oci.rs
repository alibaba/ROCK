//! OCI layout validation and deterministic ext4 BASE_IMAGE construction.

use std::collections::HashMap;
use std::ffi::CString;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::os::fd::OwnedFd;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Component, Path, PathBuf};
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use flate2::read::GzDecoder;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use tar::{Archive, EntryType};
use tokio::io::AsyncReadExt;
use tokio::process::Command;
use uuid::Uuid;

use crate::snapshot::format::overlaybd_oci::OverlaybdOciSourceResolver;
use crate::snapshot::image::model::{BuildImage, ResolveImage, ResolvedImage};
use crate::snapshot::image::service::{
    BaseImageBuildError, BaseImageBuildResult, BaseImageBuilder, BaseImageSource,
};
use crate::snapshot::metadata::{
    canonical_json_bytes, sha256_identity, SnapshotProvenance, VmmCompatibility,
};
use crate::snapshot::repository::frozen_layer::{
    FrozenLayerMetadata, FrozenLayerPin, PackedOwnershipMap, BLOCK_OWNERSHIP_UNIT,
};
use crate::snapshot::repository::Snapshot;
use crate::snapshot::transport::registry::{
    layer_ref, RegistryClient, RegistryError, RegistryLayerRef, RegistryManifest, RegistryReference,
};
use crate::snapshot::{SourceError, SourceResolver};

const MAX_JSON_BYTES: u64 = 16 * 1024 * 1024;
const MAX_LAYERS: usize = 128;
const MAX_LAYER_BLOB_BYTES: u64 = 4 * 1024 * 1024 * 1024;
const MAX_TOTAL_BLOB_BYTES: u64 = 16 * 1024 * 1024 * 1024;
const MAX_UNPACKED_BYTES: u64 = 64 * 1024 * 1024 * 1024;
const MAX_MEMBERS: u64 = 1_000_000;
const MAX_PATH_BYTES: usize = 4096;
const MAX_PROCESS_OUTPUT: usize = 64 * 1024;
const MAX_WORKER_ERROR_MESSAGE_BYTES: usize = 32 * 1024;
const IMAGE_ALIGNMENT: u64 = 2 * 1024 * 1024;
pub const BASE_IMAGE_BUILDER_VERSION: &str = "oci-ext4-overlaybd-zfile-lz4-v1-skopeo-e2fsprogs";
pub const REGISTRY_BASE_IMAGE_BUILDER_VERSION: &str = "oci-registry-source-v2-tls-zfile-crc";
const SNAPSHOT_FORMAT_VERSION: u64 = 1;
const ROOTFS_WORKER_ERROR_PREFIX: &str = "ROCK_SNAPD_BASE_IMAGE_WORKER_ERROR ";

const MANIFEST_MEDIA_TYPES: &[&str] = &[
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
];
const INDEX_MEDIA_TYPES: &[&str] = &[
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
];
const LAYER_MEDIA_TYPES: &[&str] = &[
    "application/vnd.oci.image.layer.v1.tar",
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.docker.image.rootfs.diff.tar",
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
];

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OciPlatform {
    pub os: String,
    pub architecture: String,
    pub variant: Option<String>,
}

impl OciPlatform {
    pub fn parse(value: &str) -> Result<Self, BaseImageBuildError> {
        if value.is_empty() || value.len() > 192 {
            return build_error(
                "BASE_IMAGE_PLATFORM_INVALID",
                "OCI platform is empty or too long",
            );
        }
        let parts = value.split('/').collect::<Vec<_>>();
        if !(2..=3).contains(&parts.len())
            || parts.iter().any(|part| !valid_platform_part(part))
            || parts[0] != "linux"
        {
            return build_error(
                "BASE_IMAGE_PLATFORM_INVALID",
                "OCI platform must be linux/arch[/variant]",
            );
        }
        Ok(Self {
            os: parts[0].into(),
            architecture: parts[1].into(),
            variant: parts.get(2).map(|value| (*value).into()),
        })
    }

    pub fn vmm_architecture(&self) -> String {
        match self.architecture.as_str() {
            "amd64" => "x86_64".into(),
            "arm64" => "aarch64".into(),
            value => value.into(),
        }
    }

    pub fn canonical(&self) -> String {
        match &self.variant {
            Some(variant) => format!("{}/{}/{}", self.os, self.architecture, variant),
            None => format!("{}/{}", self.os, self.architecture),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OciLayer {
    pub path: PathBuf,
    pub media_type: String,
    pub diff_id: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OciLayoutSelection {
    pub manifest_digest: String,
    pub layers: Vec<OciLayer>,
}

#[async_trait]
pub trait OciLayoutPuller: Send + Sync + 'static {
    async fn resolve(
        &self,
        oci_ref: &str,
        platform: &OciPlatform,
        timeout: Duration,
        home: &Path,
    ) -> Result<String, BaseImageBuildError>;

    async fn pull(
        &self,
        oci_ref: &str,
        destination: &Path,
        platform: &OciPlatform,
        timeout: Duration,
        home: &Path,
    ) -> Result<(), BaseImageBuildError>;
}

#[derive(Debug)]
pub struct SkopeoOciLayoutPuller {
    executable: PathBuf,
}

impl SkopeoOciLayoutPuller {
    pub fn new(executable: impl Into<PathBuf>) -> Result<Arc<Self>, BaseImageBuildError> {
        let executable = executable.into();
        require_executable(&executable, "skopeo")?;
        Ok(Arc::new(Self { executable }))
    }
}

#[async_trait]
impl OciLayoutPuller for SkopeoOciLayoutPuller {
    async fn resolve(
        &self,
        oci_ref: &str,
        platform: &OciPlatform,
        timeout: Duration,
        home: &Path,
    ) -> Result<String, BaseImageBuildError> {
        validate_oci_ref(oci_ref)?;
        let mut arguments = vec![
            "inspect".to_owned(),
            "--raw".to_owned(),
            "--override-os".to_owned(),
            platform.os.clone(),
            "--override-arch".to_owned(),
            platform.architecture.clone(),
        ];
        if let Some(variant) = &platform.variant {
            arguments.push("--override-variant".into());
            arguments.push(variant.clone());
        }
        arguments.push(format!("docker://{oci_ref}"));
        let output = run_bounded_process(
            &self.executable,
            &arguments,
            timeout,
            home,
            "BASE_IMAGE_RESOLVE_FAILED",
        )
        .await?;
        resolve_raw_manifest_digest(&output, platform)
    }

    async fn pull(
        &self,
        oci_ref: &str,
        destination: &Path,
        platform: &OciPlatform,
        timeout: Duration,
        home: &Path,
    ) -> Result<(), BaseImageBuildError> {
        validate_oci_ref(oci_ref)?;
        let mut arguments = vec![
            "copy".to_owned(),
            "--override-os".to_owned(),
            platform.os.clone(),
            "--override-arch".to_owned(),
            platform.architecture.clone(),
        ];
        if let Some(variant) = &platform.variant {
            arguments.push("--override-variant".into());
            arguments.push(variant.clone());
        }
        arguments.push(format!("docker://{oci_ref}"));
        arguments.push(format!("oci:{}:rock", destination.display()));
        run_bounded_process(
            &self.executable,
            &arguments,
            timeout,
            home,
            "BASE_IMAGE_PULL_FAILED",
        )
        .await
        .map(|_| ())
    }
}

pub struct OciExt4BaseImageBuilder {
    cache_root: PathBuf,
    worker_binary: PathBuf,
    fakeroot_binary: Option<PathBuf>,
    mke2fs_binary: PathBuf,
    e2fsck_binary: PathBuf,
    debugfs_binary: PathBuf,
    minimum_image_bytes: u64,
    headroom_bytes: u64,
    timeout: Duration,
    puller: Arc<dyn OciLayoutPuller>,
    source_resolver: Arc<dyn SourceResolver>,
    registry_clients: HashMap<String, Arc<RegistryClient>>,
}

pub struct OciExt4BuilderConfig {
    pub cache_root: PathBuf,
    pub worker_binary: PathBuf,
    pub fakeroot_binary: Option<PathBuf>,
    pub mke2fs_binary: PathBuf,
    pub e2fsck_binary: PathBuf,
    pub debugfs_binary: PathBuf,
    pub minimum_image_bytes: u64,
    pub headroom_bytes: u64,
    pub timeout: Duration,
}

impl OciExt4BaseImageBuilder {
    pub fn new(
        config: OciExt4BuilderConfig,
        puller: Arc<dyn OciLayoutPuller>,
    ) -> Result<Arc<Self>, BaseImageBuildError> {
        Self::new_with_source_resolver(
            config,
            puller,
            Arc::new(OverlaybdOciSourceResolver::default()),
            Vec::new(),
        )
    }

    pub(crate) fn new_with_source_resolver(
        config: OciExt4BuilderConfig,
        puller: Arc<dyn OciLayoutPuller>,
        source_resolver: Arc<dyn SourceResolver>,
        registry_clients: Vec<Arc<RegistryClient>>,
    ) -> Result<Arc<Self>, BaseImageBuildError> {
        if config.minimum_image_bytes == 0
            || !config.minimum_image_bytes.is_multiple_of(IMAGE_ALIGNMENT)
            || config.timeout.is_zero()
            || config.timeout > Duration::from_secs(3600)
        {
            return build_error(
                "BASE_IMAGE_CONFIG_INVALID",
                "OCI ext4 builder size or timeout is outside its bound",
            );
        }
        require_executable(&config.worker_binary, "BASE_IMAGE worker")?;
        require_executable(&config.mke2fs_binary, "mke2fs")?;
        require_executable(&config.e2fsck_binary, "e2fsck")?;
        require_executable(&config.debugfs_binary, "debugfs")?;
        if unsafe { libc::geteuid() } != 0 {
            let fakeroot = config.fakeroot_binary.as_ref().ok_or_else(|| {
                BaseImageBuildError::new(
                    "BASE_IMAGE_TOOL_MISSING",
                    "fakeroot is required when snapd is non-root",
                    false,
                    None,
                )
            })?;
            require_executable(fakeroot, "fakeroot")?;
        }
        prepare_cache_root(&config.cache_root)?;
        Ok(Arc::new(Self {
            cache_root: config.cache_root,
            worker_binary: config.worker_binary,
            fakeroot_binary: config.fakeroot_binary,
            mke2fs_binary: config.mke2fs_binary,
            e2fsck_binary: config.e2fsck_binary,
            debugfs_binary: config.debugfs_binary,
            minimum_image_bytes: config.minimum_image_bytes,
            headroom_bytes: config.headroom_bytes,
            timeout: config.timeout,
            puller,
            source_resolver,
            registry_clients: registry_clients
                .into_iter()
                .map(|client| (client.registry().to_owned(), client))
                .collect(),
        }))
    }

    fn registry_client(&self, reference: &str) -> Result<Arc<RegistryClient>, BaseImageBuildError> {
        let reference = RegistryReference::parse(reference).map_err(registry_build_error)?;
        self.registry_clients
            .get(&reference.registry)
            .cloned()
            .map(Ok)
            .unwrap_or_else(|| RegistryClient::new(&reference.registry))
            .map_err(registry_build_error)
    }

    async fn build_inner(
        &self,
        request: &BuildImage,
    ) -> Result<BaseImageBuildResult, BaseImageBuildError> {
        request.validate().map_err(|message| {
            BaseImageBuildError::new("INVALID_ARGUMENT", message, false, None)
        })?;
        if request.base_image_builder_version == REGISTRY_BASE_IMAGE_BUILDER_VERSION {
            return build_registry_rootfs(request, self.source_resolver.as_ref()).await;
        }
        if accelerated_reference(&request.oci_ref) {
            return build_error(
                "BASE_IMAGE_ACCELERATED_INVALID",
                "accelerated references require the registry-rootfs builder identity",
            );
        }
        validate_oci_ref(&request.oci_ref)?;
        let platform = OciPlatform::parse(&request.platform)?;
        let canonical_platform = platform.canonical();
        let expected_build_key = base_image_build_key(
            &request.oci_digest,
            &canonical_platform,
            &request.base_image_builder_version,
        )?;
        if request.platform != canonical_platform
            || request.base_image_builder_version != BASE_IMAGE_BUILDER_VERSION
            || request.base_image_build_key != expected_build_key
        {
            return build_error(
                "IDENTITY_CONFLICT",
                "BASE_IMAGE request differs from the canonical builder identity",
            );
        }
        prepare_cache_root(&self.cache_root)?;
        let job_root = create_job_root(&self.cache_root)?;
        let layout = job_root.join("oci-layout");
        let raw_image = job_root.join("rootfs.raw");
        let result = async {
            let pinned_ref = pin_oci_ref(&request.oci_ref, &request.oci_digest)?;
            self.puller
                .pull(&pinned_ref, &layout, &platform, self.timeout, &job_root)
                .await?;
            let observed_digest = self
                .puller
                .resolve(&request.oci_ref, &platform, self.timeout, &job_root)
                .await?;
            if observed_digest != request.oci_digest {
                return Err(BaseImageBuildError::new(
                    "IDENTITY_CONFLICT",
                    "OCI tag changed after digest resolution",
                    false,
                    Some(observed_digest),
                ));
            }
            let selection = inspect_oci_layout(&layout, &platform)?;
            let image_size =
                estimate_image_size(&selection, self.minimum_image_bytes, self.headroom_bytes)?;
            self.run_worker(
                &layout,
                &raw_image,
                &selection.manifest_digest,
                image_size,
                &request.platform,
                &job_root,
            )
            .await?;
            local_snapshot_from_raw(request, &selection.manifest_digest, &platform, &raw_image)
        }
        .await;
        if let Err(error) = remove_job_root(&job_root, &self.cache_root) {
            if result.is_ok() {
                return Err(error);
            }
        }
        result
    }

    #[allow(clippy::too_many_arguments)]
    async fn run_worker(
        &self,
        layout: &Path,
        output: &Path,
        digest: &str,
        image_size: u64,
        platform: &str,
        home: &Path,
    ) -> Result<(), BaseImageBuildError> {
        let mut arguments = Vec::new();
        let executable = if unsafe { libc::geteuid() } == 0 {
            self.worker_binary.clone()
        } else {
            arguments.push(self.worker_binary.to_string_lossy().into_owned());
            self.fakeroot_binary
                .clone()
                .expect("non-root configuration requires fakeroot")
        };
        arguments.extend([
            "_rootfs_worker".into(),
            layout.to_string_lossy().into_owned(),
            output.to_string_lossy().into_owned(),
            digest.into(),
            image_size.to_string(),
            platform.into(),
            self.mke2fs_binary.to_string_lossy().into_owned(),
            self.e2fsck_binary.to_string_lossy().into_owned(),
            self.debugfs_binary.to_string_lossy().into_owned(),
        ]);
        run_bounded_process(
            &executable,
            &arguments,
            self.timeout,
            home,
            "BASE_IMAGE_ROOTFS_FAILED",
        )
        .await
        .map(|_| ())
    }

    async fn resolve_inner(
        &self,
        request: &ResolveImage,
    ) -> Result<ResolvedImage, BaseImageBuildError> {
        request.validate().map_err(|message| {
            BaseImageBuildError::new("INVALID_ARGUMENT", message, false, None)
        })?;
        if accelerated_reference(&request.oci_ref) {
            return resolve_registry_rootfs(request, &self.registry_client(&request.oci_ref)?)
                .await;
        }
        validate_oci_ref(&request.oci_ref)?;
        let platform = OciPlatform::parse(&request.platform)?;
        let canonical_platform = platform.canonical();
        prepare_cache_root(&self.cache_root)?;
        let job_root = create_job_root(&self.cache_root)?;
        let result = async {
            let oci_digest = self
                .puller
                .resolve(&request.oci_ref, &platform, self.timeout, &job_root)
                .await?;
            if !accelerated_candidates(&request.oci_ref).is_empty() {
                let client = self.registry_client(&request.oci_ref)?;
                let accelerated = tokio::time::timeout(
                    self.timeout.min(Duration::from_secs(30)),
                    discover_registry_rootfs(request, &platform, &oci_digest, &client),
                )
                .await
                .map_err(|_| {
                    BaseImageBuildError::new(
                        "BASE_IMAGE_REGISTRY_FAILED",
                        "accelerated image discovery timed out",
                        true,
                        None,
                    )
                })??;
                if let Some(accelerated) = accelerated {
                    return Ok(accelerated);
                }
            }
            let base_image_build_key =
                base_image_build_key(&oci_digest, &canonical_platform, BASE_IMAGE_BUILDER_VERSION)?;
            Ok(ResolvedImage {
                oci_ref: request.oci_ref.clone(),
                oci_digest,
                oci_platform: canonical_platform,
                base_image_builder_version: BASE_IMAGE_BUILDER_VERSION.into(),
                base_image_build_key,
            })
        }
        .await;
        if let Err(error) = remove_job_root(&job_root, &self.cache_root) {
            if result.is_ok() {
                return Err(error);
            }
        }
        result
    }
}

#[async_trait]
impl BaseImageBuilder for OciExt4BaseImageBuilder {
    async fn resolve(&self, request: &ResolveImage) -> Result<ResolvedImage, BaseImageBuildError> {
        self.resolve_inner(request).await
    }

    async fn build(
        &self,
        request: &BuildImage,
    ) -> Result<BaseImageBuildResult, BaseImageBuildError> {
        self.build_inner(request).await
    }
}

fn accelerated_reference(reference: &str) -> bool {
    reference
        .split('@')
        .next()
        .and_then(|value| value.rsplit('/').next())
        .and_then(|value| value.rsplit_once(':'))
        .is_some_and(|(_, tag)| tag.ends_with("_accelerated"))
}

/// Only mutable, explicitly qualified tags participate in discovery. A digest
/// is an exact content request and must never be replaced with a sibling tag.
fn accelerated_candidates(reference: &str) -> Vec<String> {
    if reference.contains('@') || accelerated_reference(reference) {
        return Vec::new();
    }
    let authority = reference
        .strip_prefix("docker://")
        .unwrap_or(reference)
        .split('/')
        .next()
        .unwrap_or_default();
    if !authority.contains('.') && !authority.contains(':') && authority != "localhost" {
        return Vec::new();
    }
    let Ok(reference) = RegistryReference::parse(reference) else {
        return Vec::new();
    };
    ["_containerd_accelerated", "_accelerated"]
        .into_iter()
        .filter_map(|suffix| {
            let tag = format!("{}{suffix}", reference.reference);
            (tag.len() <= 128)
                .then(|| format!("{}/{}:{tag}", reference.registry, reference.repository))
        })
        .collect()
}

async fn discover_registry_rootfs(
    request: &ResolveImage,
    platform: &OciPlatform,
    source_digest: &str,
    client: &Arc<RegistryClient>,
) -> Result<Option<ResolvedImage>, BaseImageBuildError> {
    let reference = RegistryReference::parse(&request.oci_ref).map_err(registry_build_error)?;
    let mut source = None;
    for candidate in accelerated_candidates(&request.oci_ref) {
        let candidate_ref = RegistryReference::parse(&candidate).map_err(registry_build_error)?;
        let root = match client
            .manifest(&reference.repository, &candidate_ref.reference)
            .await
        {
            Ok(manifest) => manifest,
            Err(error) if error.is_not_found() => continue,
            Err(error)
                if error.kind
                    == crate::snapshot::transport::registry::RegistryErrorKind::Invalid =>
            {
                continue
            }
            Err(error) => return Err(registry_build_error(error)),
        };
        let root_subject = root.value.get("subject").cloned();
        let is_index = root.value.get("manifests").is_some();
        let selected = match client
            .select_manifest(&reference.repository, root, &platform.canonical())
            .await
        {
            Ok(manifest) => manifest,
            Err(error)
                if error.kind
                    == crate::snapshot::transport::registry::RegistryErrorKind::Invalid =>
            {
                continue
            }
            Err(error) => return Err(registry_build_error(error)),
        };
        let config =
            match validate_accelerated_manifest(client, &reference, &selected, platform).await {
                Ok(config) => config,
                Err(error) if error.code == "BASE_IMAGE_ACCELERATED_INVALID" => continue,
                Err(error) => return Err(error),
            };
        if source.is_none() {
            let root = client
                .manifest(&reference.repository, &reference.reference)
                .await
                .map_err(registry_build_error)?;
            let root_identity = (root.digest.clone(), root.payload.len() as u64);
            let selected = client
                .select_manifest(&reference.repository, root, &platform.canonical())
                .await
                .map_err(registry_build_error)?;
            if selected.digest != source_digest {
                return build_error(
                    "IDENTITY_CONFLICT",
                    "source tag changed during accelerated image discovery",
                );
            }
            source = Some((root_identity, selected));
        }
        let (root_identity, original) = source.as_ref().unwrap();
        let matches = if is_index && root_subject.is_some() {
            subject_matches(
                root_subject.as_ref().unwrap(),
                &root_identity.0,
                root_identity.1,
            ) && selected.value.get("subject").is_none_or(|subject| {
                subject_matches(subject, &original.digest, original.payload.len() as u64)
            })
        } else if let Some(subject) = selected.value.get("subject") {
            subject_matches(subject, &original.digest, original.payload.len() as u64)
        } else {
            // Some legacy converters preserve the complete source config,
            // including rootfs diff_ids. A suffix alone is not a version binding.
            config["rootfs"]["type"] == "layers"
                && config["rootfs"]["diff_ids"].as_array().is_some_and(|ids| {
                    !ids.is_empty()
                        && ids.iter().all(|id| {
                            id.as_str()
                                .is_some_and(|id| validate_sha256_digest(id).is_ok())
                        })
                })
                && layer_ref(&original.value["config"]).is_ok_and(|original_config| {
                    layer_ref(&selected.value["config"])
                        .is_ok_and(|config| config == original_config)
                })
        };
        if matches {
            return resolved_registry_rootfs(candidate, selected, platform).map(Some);
        }
    }
    Ok(None)
}

fn subject_matches(subject: &Value, digest: &str, size: u64) -> bool {
    layer_ref(subject).is_ok_and(|subject| subject.digest == digest && subject.size == size)
}

async fn registry_manifest(
    reference: &str,
    selector: Option<&str>,
    platform: &OciPlatform,
    client: &Arc<RegistryClient>,
) -> Result<(Arc<RegistryClient>, RegistryReference, RegistryManifest), BaseImageBuildError> {
    if platform.architecture == "snapd-memory-amd64" {
        return build_error(
            "BASE_IMAGE_PLATFORM_INVALID",
            "The private snapd memory platform cannot be used as a cold-boot disk image",
        );
    }
    let reference = RegistryReference::parse(reference).map_err(registry_build_error)?;
    let manifest = client
        .resolve_manifest(
            &reference.repository,
            selector.unwrap_or(&reference.reference),
            &platform.canonical(),
        )
        .await
        .map_err(registry_build_error)?;
    validate_accelerated_manifest(client, &reference, &manifest, platform).await?;
    Ok((Arc::clone(client), reference, manifest))
}

async fn validate_accelerated_manifest(
    client: &Arc<RegistryClient>,
    reference: &RegistryReference,
    manifest: &RegistryManifest,
    platform: &OciPlatform,
) -> Result<Value, BaseImageBuildError> {
    accelerated_layers(&manifest.value)?;
    let config = layer_ref(manifest.value.get("config").ok_or_else(|| {
        BaseImageBuildError::new(
            "BASE_IMAGE_ACCELERATED_INVALID",
            "accelerated manifest has no config",
            false,
            None,
        )
    })?)
    .map_err(registry_build_error)?;
    let payload = client
        .read_blob_metadata(&reference.repository, &config)
        .await
        .map_err(registry_build_error)?;
    let config: Value = serde_json::from_slice(&payload).map_err(|_| {
        BaseImageBuildError::new(
            "BASE_IMAGE_ACCELERATED_INVALID",
            "accelerated config is not JSON",
            false,
            None,
        )
    })?;
    if config.get("os").and_then(Value::as_str) != Some(platform.os.as_str())
        || config.get("architecture").and_then(Value::as_str)
            != Some(platform.architecture.as_str())
        || platform.variant.as_ref().is_some_and(|variant| {
            config.get("variant").and_then(Value::as_str) != Some(variant.as_str())
        })
    {
        return build_error(
            "BASE_IMAGE_ACCELERATED_INVALID",
            "accelerated image config differs from requested platform",
        );
    }
    Ok(config)
}

/// `_accelerated` is a DADI locator manifest, not a tar image to unpack. Its
/// SHA-bound annotations name native blobs in the same repository. Never use
/// the embedded VPC URL as a source identity. Sibling-tag discovery separately
/// checks the native format, platform and binding to the current source image.
fn accelerated_layers(manifest: &Value) -> Result<Vec<RegistryLayerRef>, BaseImageBuildError> {
    crate::snapshot::format::overlaybd_oci::accelerated_layers(manifest).map_err(|error| {
        BaseImageBuildError::new(
            "BASE_IMAGE_ACCELERATED_INVALID",
            error.to_string(),
            error.retryable(),
            None,
        )
    })
}

async fn resolve_registry_rootfs(
    request: &ResolveImage,
    client: &Arc<RegistryClient>,
) -> Result<ResolvedImage, BaseImageBuildError> {
    let platform = OciPlatform::parse(&request.platform)?;
    let (_, _, manifest) = registry_manifest(&request.oci_ref, None, &platform, client).await?;
    resolved_registry_rootfs(request.oci_ref.clone(), manifest, &platform)
}

fn resolved_registry_rootfs(
    oci_ref: String,
    manifest: RegistryManifest,
    platform: &OciPlatform,
) -> Result<ResolvedImage, BaseImageBuildError> {
    Ok(ResolvedImage {
        oci_ref,
        oci_digest: manifest.digest.clone(),
        oci_platform: platform.canonical(),
        base_image_builder_version: REGISTRY_BASE_IMAGE_BUILDER_VERSION.into(),
        base_image_build_key: base_image_build_key(
            &manifest.digest,
            &platform.canonical(),
            REGISTRY_BASE_IMAGE_BUILDER_VERSION,
        )?,
    })
}

async fn build_registry_rootfs(
    request: &BuildImage,
    resolver: &dyn SourceResolver,
) -> Result<BaseImageBuildResult, BaseImageBuildError> {
    let platform = OciPlatform::parse(&request.platform)?;
    if request.platform != platform.canonical()
        || request.base_image_build_key
            != base_image_build_key(
                &request.oci_digest,
                &request.platform,
                REGISTRY_BASE_IMAGE_BUILDER_VERSION,
            )?
    {
        return build_error(
            "IDENTITY_CONFLICT",
            "accelerated request differs from the canonical builder identity",
        );
    }
    // The request pins a selected manifest; the source resolver owns metadata
    // caching and reconstruction and never substitutes a later mutable tag.
    let reference = RegistryReference::parse(&request.oci_ref).map_err(registry_build_error)?;
    let resolved = resolver
        .import_registry(
            &reference.registry,
            &reference.repository,
            &request.oci_digest,
            &request.platform,
        )
        .await
        .map_err(|error| source_build_error(error, &request.oci_digest))?;
    resolved
        .validate()
        .map_err(|error| source_build_error(error, &request.oci_digest))?;
    let crate::snapshot::metadata::SourceRef::Registry {
        registry,
        repository,
        manifest_digest,
        platform: source_platform,
        ..
    } = &resolved.source_ref;
    if registry != &reference.registry
        || repository != &reference.repository
        || manifest_digest != &request.oci_digest
        || source_platform != &request.platform
    {
        return build_error(
            "IDENTITY_CONFLICT",
            "resolved Registry source differs from the pinned request",
        );
    }
    let rootfs = resolved.range_map.registry.as_ref().ok_or_else(|| {
        BaseImageBuildError::new(
            "BASE_IMAGE_ACCELERATED_INVALID",
            "resolved source has no native format capability",
            false,
            None,
        )
    })?;
    Ok(BaseImageBuildResult {
        source: BaseImageSource::Registry(Arc::clone(rootfs)),
        compatibility: VmmCompatibility {
            architecture: platform.vmm_architecture(),
            vmm_type: "any".into(),
            vmm_state_format_version: None,
            rocklet_artifact: None,
        },
        oci_digest: request.oci_digest.clone(),
    })
}

fn source_build_error(error: SourceError, digest: &str) -> BaseImageBuildError {
    BaseImageBuildError::new(
        if error.is_integrity() {
            "IDENTITY_CONFLICT"
        } else {
            "BASE_IMAGE_REGISTRY_FAILED"
        },
        error.to_string(),
        error.retryable(),
        Some(digest.into()),
    )
}

fn registry_build_error(error: RegistryError) -> BaseImageBuildError {
    BaseImageBuildError::new(
        "BASE_IMAGE_REGISTRY_FAILED",
        error.to_string(),
        error.retryable(),
        None,
    )
}

pub fn rootfs_worker(arguments: &[String]) -> Result<(), BaseImageBuildError> {
    if arguments.len() != 8 {
        return build_error(
            "BASE_IMAGE_WORKER_INVALID",
            "rootfs worker requires exactly eight arguments",
        );
    }
    let layout = PathBuf::from(&arguments[0]);
    let output = PathBuf::from(&arguments[1]);
    let digest = &arguments[2];
    let image_size = arguments[3].parse::<u64>().map_err(|_| {
        BaseImageBuildError::new(
            "BASE_IMAGE_GEOMETRY_INVALID",
            "raw image size is not u64",
            false,
            None,
        )
    })?;
    let platform = OciPlatform::parse(&arguments[4])?;
    let mke2fs = PathBuf::from(&arguments[5]);
    let e2fsck = PathBuf::from(&arguments[6]);
    let debugfs = PathBuf::from(&arguments[7]);
    require_executable(&mke2fs, "mke2fs")?;
    require_executable(&e2fsck, "e2fsck")?;
    require_executable(&debugfs, "debugfs")?;
    if image_size == 0 || !image_size.is_multiple_of(IMAGE_ALIGNMENT) {
        return build_error(
            "BASE_IMAGE_GEOMETRY_INVALID",
            "raw image size is not 2 MiB aligned",
        );
    }
    let layout = layout.canonicalize().map_err(io_build_error)?;
    let output_parent = output
        .parent()
        .ok_or_else(|| {
            BaseImageBuildError::new(
                "BASE_IMAGE_PATH_UNSAFE",
                "rootfs output has no parent",
                false,
                None,
            )
        })?
        .canonicalize()
        .map_err(io_build_error)?;
    if output.exists() || layout.parent() != Some(output_parent.as_path()) {
        return build_error(
            "BASE_IMAGE_PATH_UNSAFE",
            "rootfs worker output escaped build root",
        );
    }
    let selection = inspect_oci_layout(&layout, &platform)?;
    if selection.manifest_digest != *digest {
        return build_error(
            "OCI_DIGEST_INVALID",
            "selected OCI digest changed before conversion",
        );
    }
    let rootfs = output_parent.join("rootfs");
    fs::create_dir(&rootfs).map_err(io_build_error)?;
    for layer in &selection.layers {
        apply_layer(layer, &rootfs)?;
    }
    canonicalize_tree_times(&rootfs)?;
    build_ext4(
        &rootfs, &output, image_size, digest, &mke2fs, &e2fsck, &debugfs,
    )
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RootfsWorkerErrorRecord {
    code: String,
    message: String,
    retryable: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    oci_digest: Option<String>,
}

/// Encode a bounded, machine-readable failure for the parent snapd process.
///
/// The fixed prefix lets the parent distinguish this private worker protocol
/// from structured diagnostic lines emitted by the worker or its tools.
pub fn rootfs_worker_error_line(error: &BaseImageBuildError) -> String {
    let message = truncate_utf8(&error.message, MAX_WORKER_ERROR_MESSAGE_BYTES);
    let record = RootfsWorkerErrorRecord {
        code: error.code.clone(),
        message: message.to_owned(),
        retryable: error.retryable,
        oci_digest: error.oci_digest.clone(),
    };
    let payload = serde_json::to_string(&record)
        .expect("rootfs worker failure record contains only serializable strings");
    format!("{ROOTFS_WORKER_ERROR_PREFIX}{payload}")
}

fn decode_rootfs_worker_error(stderr: &[u8]) -> Option<BaseImageBuildError> {
    let stderr = std::str::from_utf8(stderr).ok()?;
    let payload = stderr
        .lines()
        .rev()
        .find_map(|line| line.strip_prefix(ROOTFS_WORKER_ERROR_PREFIX))?;
    let record = serde_json::from_str::<RootfsWorkerErrorRecord>(payload).ok()?;
    Some(BaseImageBuildError::new(
        record.code,
        record.message,
        record.retryable,
        record.oci_digest,
    ))
}

fn truncate_utf8(value: &str, max_bytes: usize) -> &str {
    if value.len() <= max_bytes {
        return value;
    }
    let mut end = max_bytes;
    while !value.is_char_boundary(end) {
        end -= 1;
    }
    &value[..end]
}

fn resolve_raw_manifest_digest(
    payload: &[u8],
    platform: &OciPlatform,
) -> Result<String, BaseImageBuildError> {
    let document: Value = serde_json::from_slice(payload).map_err(|error| {
        BaseImageBuildError::new(
            "OCI_LAYOUT_INVALID",
            format!("OCI registry manifest JSON is invalid: {error}"),
            false,
            None,
        )
    })?;
    let object = document.as_object().ok_or_else(|| {
        BaseImageBuildError::new(
            "OCI_LAYOUT_INVALID",
            "OCI registry manifest root is not an object",
            false,
            None,
        )
    })?;
    if object.get("schemaVersion").and_then(Value::as_u64) != Some(2) {
        return build_error(
            "OCI_LAYOUT_INVALID",
            "OCI registry manifest schemaVersion must be 2",
        );
    }
    if let Some(manifests) = object.get("manifests") {
        if object
            .get("mediaType")
            .and_then(Value::as_str)
            .is_some_and(|media_type| !INDEX_MEDIA_TYPES.contains(&media_type))
        {
            return build_error(
                "OCI_MEDIA_TYPE_UNSUPPORTED",
                "OCI registry index media type is unsupported",
            );
        }
        let manifests = manifests.as_array().ok_or_else(|| {
            BaseImageBuildError::new(
                "OCI_LAYOUT_INVALID",
                "OCI registry index manifests is not an array",
                false,
                None,
            )
        })?;
        return Ok(descriptor_digest(select_platform_manifest(manifests, platform)?)?.into());
    }
    if object.get("config").is_none() || object.get("layers").is_none() {
        return build_error(
            "OCI_LAYOUT_INVALID",
            "OCI registry response is neither an image manifest nor an image index",
        );
    }
    if object
        .get("mediaType")
        .and_then(Value::as_str)
        .is_some_and(|media_type| !MANIFEST_MEDIA_TYPES.contains(&media_type))
    {
        return build_error(
            "OCI_MEDIA_TYPE_UNSUPPORTED",
            "OCI registry manifest media type is unsupported",
        );
    }
    Ok(sha256_identity(payload))
}

fn select_platform_manifest<'a>(
    manifests: &'a [Value],
    platform: &OciPlatform,
) -> Result<&'a Value, BaseImageBuildError> {
    if manifests.is_empty() || manifests.len() > 256 {
        return build_error(
            "OCI_LAYOUT_INVALID",
            "OCI index manifest count is outside 1..=256",
        );
    }
    let mut qualified = Vec::new();
    let mut unqualified = Vec::new();
    for descriptor in manifests {
        validate_descriptor(descriptor, Some(MANIFEST_MEDIA_TYPES))?;
        match descriptor.get("platform") {
            None => unqualified.push(descriptor),
            Some(Value::Object(value))
                if value.get("os").and_then(Value::as_str) == Some(platform.os.as_str())
                    && value.get("architecture").and_then(Value::as_str)
                        == Some(platform.architecture.as_str())
                    && platform.variant.as_deref().is_none_or(|variant| {
                        value.get("variant").and_then(Value::as_str) == Some(variant)
                    }) =>
            {
                qualified.push(descriptor)
            }
            Some(Value::Object(_)) => {}
            Some(_) => {
                return build_error(
                    "OCI_LAYOUT_INVALID",
                    "OCI descriptor platform is not an object",
                )
            }
        }
    }
    if qualified.len() == 1 {
        Ok(qualified[0])
    } else if qualified.is_empty() && manifests.len() == 1 && unqualified.len() == 1 {
        Ok(unqualified[0])
    } else {
        build_error(
            "OCI_PLATFORM_NOT_FOUND",
            "OCI index does not select exactly one requested platform",
        )
    }
}

pub fn inspect_oci_layout(
    layout: &Path,
    platform: &OciPlatform,
) -> Result<OciLayoutSelection, BaseImageBuildError> {
    let layout = layout.canonicalize().map_err(io_build_error)?;
    if !layout.is_dir()
        || fs::symlink_metadata(&layout)
            .map_err(io_build_error)?
            .file_type()
            .is_symlink()
    {
        return build_error("OCI_LAYOUT_INVALID", "OCI layout is not a real directory");
    }
    let index = read_json_object(&layout.join("index.json"))?;
    let manifests = index
        .get("manifests")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            BaseImageBuildError::new(
                "OCI_LAYOUT_INVALID",
                "OCI index manifests is not an array",
                false,
                None,
            )
        })?;
    let selected = select_platform_manifest(manifests, platform)?;
    let manifest_digest = descriptor_digest(selected)?.to_owned();
    let manifest = read_json_object(&verified_blob(&layout, selected)?)?;
    if manifest.get("schemaVersion").and_then(Value::as_u64) != Some(2) {
        return build_error("OCI_LAYOUT_INVALID", "OCI manifest schemaVersion must be 2");
    }
    let config = manifest.get("config").ok_or_else(|| {
        BaseImageBuildError::new(
            "OCI_LAYOUT_INVALID",
            "OCI manifest has no config descriptor",
            false,
            None,
        )
    })?;
    validate_descriptor(config, None)?;
    let config = read_json_object(&verified_blob(&layout, config)?)?;
    if config
        .get("os")
        .and_then(Value::as_str)
        .is_some_and(|value| value != platform.os)
        || config
            .get("architecture")
            .and_then(Value::as_str)
            .is_some_and(|value| value != platform.architecture)
    {
        return build_error(
            "OCI_PLATFORM_NOT_FOUND",
            "OCI config platform differs from selected descriptor",
        );
    }
    let layers = manifest
        .get("layers")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            BaseImageBuildError::new(
                "OCI_LAYOUT_INVALID",
                "OCI manifest layers is not an array",
                false,
                None,
            )
        })?;
    if layers.is_empty() || layers.len() > MAX_LAYERS {
        return build_error("OCI_LAYOUT_INVALID", "OCI layer count is outside 1..=128");
    }
    let diff_ids = config
        .get("rootfs")
        .and_then(Value::as_object)
        .and_then(|value| value.get("diff_ids"))
        .and_then(Value::as_array)
        .ok_or_else(|| {
            BaseImageBuildError::new(
                "OCI_LAYOUT_INVALID",
                "OCI config rootfs.diff_ids is not an array",
                false,
                None,
            )
        })?;
    if diff_ids.len() != layers.len() {
        return build_error(
            "OCI_LAYOUT_INVALID",
            "OCI config diff_ids does not match manifest layers",
        );
    }
    let mut total = 0_u64;
    let mut selected_layers = Vec::with_capacity(layers.len());
    for (descriptor, diff_id) in layers.iter().zip(diff_ids) {
        validate_descriptor(descriptor, Some(LAYER_MEDIA_TYPES))?;
        let size = descriptor_size(descriptor)?;
        total = total.checked_add(size).ok_or_else(|| {
            BaseImageBuildError::new(
                "OCI_LAYOUT_TOO_LARGE",
                "OCI layer size sum overflows",
                false,
                None,
            )
        })?;
        if total > MAX_TOTAL_BLOB_BYTES {
            return build_error(
                "OCI_LAYOUT_TOO_LARGE",
                "OCI layer blobs exceed total size bound",
            );
        }
        let diff_id = diff_id.as_str().ok_or_else(|| {
            BaseImageBuildError::new(
                "OCI_DIGEST_INVALID",
                "OCI diff_id is not a string",
                false,
                None,
            )
        })?;
        validate_sha256_digest(diff_id)?;
        let path = verified_blob(&layout, descriptor)?;
        let media_type = descriptor
            .get("mediaType")
            .and_then(Value::as_str)
            .expect("validated media type")
            .to_owned();
        verify_uncompressed_layer(&path, &media_type, diff_id)?;
        selected_layers.push(OciLayer {
            path,
            media_type,
            diff_id: diff_id.into(),
        });
    }
    Ok(OciLayoutSelection {
        manifest_digest,
        layers: selected_layers,
    })
}

pub fn estimate_image_size(
    selection: &OciLayoutSelection,
    minimum_image_bytes: u64,
    headroom_bytes: u64,
) -> Result<u64, BaseImageBuildError> {
    let mut members = 0_u64;
    let mut unpacked = 0_u64;
    for layer in &selection.layers {
        let mut archive = layer_archive(layer)?;
        for entry in archive.entries().map_err(io_build_error)? {
            let entry = entry.map_err(io_build_error)?;
            members = members.saturating_add(1);
            unpacked = unpacked.checked_add(entry.size()).ok_or_else(|| {
                BaseImageBuildError::new(
                    "OCI_LAYOUT_TOO_LARGE",
                    "OCI unpacked byte count overflows",
                    false,
                    None,
                )
            })?;
            if members > MAX_MEMBERS || unpacked > MAX_UNPACKED_BYTES {
                return build_error(
                    "OCI_LAYOUT_TOO_LARGE",
                    "OCI unpacked members or bytes exceed bound",
                );
            }
            let path = entry.path().map_err(io_build_error)?;
            validate_relative_path(&path, true)?;
            validate_entry_type(entry.header().entry_type())?;
        }
    }
    let requested = unpacked
        .checked_add((unpacked / 3).max(headroom_bytes))
        .ok_or_else(|| {
            BaseImageBuildError::new(
                "OCI_LAYOUT_TOO_LARGE",
                "ext4 image estimate overflows",
                false,
                None,
            )
        })?
        .max(minimum_image_bytes);
    align_up(requested, IMAGE_ALIGNMENT)
}

fn apply_layer(layer: &OciLayer, rootfs: &Path) -> Result<(), BaseImageBuildError> {
    let mut members = 0_u64;
    let mut unpacked = 0_u64;
    let mut archive = layer_archive(layer)?;
    archive.set_preserve_permissions(true);
    archive.set_preserve_ownerships(true);
    archive.set_unpack_xattrs(true);
    archive.set_overwrite(true);
    for entry in archive.entries().map_err(io_build_error)? {
        let mut entry = entry.map_err(io_build_error)?;
        members = members.saturating_add(1);
        unpacked = unpacked.checked_add(entry.size()).ok_or_else(|| {
            BaseImageBuildError::new(
                "OCI_LAYOUT_TOO_LARGE",
                "OCI layer unpacked bytes overflow",
                false,
                None,
            )
        })?;
        if members > MAX_MEMBERS || unpacked > MAX_UNPACKED_BYTES {
            return build_error("OCI_LAYOUT_TOO_LARGE", "OCI layer unpack bounds exceeded");
        }
        let path = entry.path().map_err(io_build_error)?.into_owned();
        validate_relative_path(&path, true)?;
        let entry_type = entry.header().entry_type();
        validate_entry_type(entry_type)?;
        let name = match path.file_name() {
            Some(value) => Some(value.to_str().ok_or_else(|| {
                BaseImageBuildError::new(
                    "OCI_PATH_UNSAFE",
                    "OCI member filename is not UTF-8",
                    false,
                    None,
                )
            })?),
            None if entry_type.is_dir() => None,
            None => return build_error("OCI_PATH_UNSAFE", "OCI root member is not a directory"),
        };
        if name == Some(".wh..wh..opq") {
            let parent = path.parent().unwrap_or_else(|| Path::new(""));
            let directory = resolve_destination_beneath(rootfs, parent)?;
            if directory.is_dir() {
                for child in fs::read_dir(&directory).map_err(io_build_error)? {
                    remove_entry(&child.map_err(io_build_error)?.path())?;
                }
            }
            continue;
        }
        if let Some(target_name) = name.and_then(|value| value.strip_prefix(".wh.")) {
            if target_name.is_empty() {
                return build_error("OCI_PATH_UNSAFE", "OCI whiteout target is empty");
            }
            let target = path
                .parent()
                .unwrap_or_else(|| Path::new(""))
                .join(target_name);
            let destination = resolve_destination_beneath(rootfs, &target)?;
            if fs::symlink_metadata(&destination).is_ok() {
                remove_entry(&destination)?;
            }
            continue;
        }
        if entry_type.is_symlink() || entry_type.is_hard_link() {
            let link = entry.link_name().map_err(io_build_error)?.ok_or_else(|| {
                BaseImageBuildError::new("OCI_PATH_UNSAFE", "OCI link has no target", false, None)
            })?;
            validate_link_target(&path, &link, entry_type.is_hard_link())?;
        }
        let destination = resolve_destination_beneath(rootfs, &path)?;
        if fs::symlink_metadata(&destination).is_ok() && !entry_type.is_dir() {
            remove_entry(&destination)?;
        }
        let unpacked = entry.unpack_in(rootfs).map_err(io_build_error)?;
        if !unpacked {
            return build_error(
                "OCI_PATH_UNSAFE",
                "tar entry refused to unpack beneath rootfs",
            );
        }
    }
    Ok(())
}

fn local_snapshot_from_raw(
    request: &BuildImage,
    layout_manifest_digest: &str,
    platform: &OciPlatform,
    raw_image: &Path,
) -> Result<BaseImageBuildResult, BaseImageBuildError> {
    let metadata = fs::metadata(raw_image).map_err(io_build_error)?;
    let logical_size = metadata.len();
    if logical_size == 0 || !logical_size.is_multiple_of(BLOCK_OWNERSHIP_UNIT) {
        return build_error(
            "BASE_IMAGE_GEOMETRY_INVALID",
            "raw ext4 size is not block-unit aligned",
        );
    }
    let raw_sha256 = sha256_file(raw_image)?;
    let ownership_payload = crate::snapshot::repository::frozen_layer::packed_data_ownership(
        logical_size,
        BLOCK_OWNERSHIP_UNIT,
    )
    .map_err(|error| {
        BaseImageBuildError::new(
            "BASE_IMAGE_GEOMETRY_INVALID",
            error.to_string(),
            false,
            None,
        )
    })?;
    let backing: OwnedFd = OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(libc::O_CLOEXEC)
        .open(raw_image)
        .map_err(io_build_error)?
        .into();
    let digest = layout_manifest_digest
        .strip_prefix("sha256:")
        .expect("validated manifest digest");
    let layer = FrozenLayerPin::new(
        FrozenLayerMetadata {
            kind: crate::snapshot::metadata::LayerKind::Block,
            layer_id: format!("base-{}", &digest[..24]),
            provisional_content_id: format!("sha256:{raw_sha256}"),
            logical_size,
            alignment: BLOCK_OWNERSHIP_UNIT,
        },
        PackedOwnershipMap {
            kind: crate::snapshot::metadata::LayerKind::Block,
            snapshot_id: request.snapshot_id.clone(),
            format_version: 1,
            logical_size,
            granularity: BLOCK_OWNERSHIP_UNIT,
            packed_states: ownership_payload,
        },
        backing,
        false,
    );
    let local = Snapshot::from_base_layer(
        request.snapshot_id.clone(),
        layer,
        SnapshotProvenance {
            oci_ref: Some(request.oci_ref.clone()),
            oci_digest: Some(request.oci_digest.clone()),
        },
    )
    .map_err(|error| {
        BaseImageBuildError::new(
            "BASE_IMAGE_CANONICALIZATION_FAILED",
            error.to_string(),
            false,
            Some(request.oci_digest.clone()),
        )
    })?;
    Ok(BaseImageBuildResult {
        source: BaseImageSource::Local(local),
        compatibility: VmmCompatibility {
            architecture: platform.vmm_architecture(),
            vmm_type: "any".into(),
            vmm_state_format_version: None,
            rocklet_artifact: None,
        },
        oci_digest: request.oci_digest.clone(),
    })
}

fn build_ext4(
    rootfs: &Path,
    output: &Path,
    image_size: u64,
    digest: &str,
    mke2fs: &Path,
    e2fsck: &Path,
    debugfs: &Path,
) -> Result<(), BaseImageBuildError> {
    let raw = OpenOptions::new()
        .create_new(true)
        .read(true)
        .write(true)
        .mode(0o600)
        .open(output)
        .map_err(io_build_error)?;
    raw.set_len(image_size).map_err(io_build_error)?;
    raw.sync_all().map_err(io_build_error)?;
    drop(raw);
    let uuid = digest_uuid(digest)?;
    let mut mke2fs_command = std::process::Command::new(mke2fs);
    configure_e2fs_environment(&mut mke2fs_command);
    let mke2fs_status = mke2fs_command
        .args([
            "-q",
            "-t",
            "ext4",
            "-F",
            "-b",
            "4096",
            "-L",
            "ROCK_ROOTFS",
            "-U",
            &uuid,
            "-O",
            "^has_journal",
            "-E",
            &format!("hash_seed={uuid},lazy_itable_init=0,lazy_journal_init=0"),
            "-d",
            path_as_str(rootfs)?,
            path_as_str(output)?,
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .output()
        .map_err(io_build_error)?;
    if !mke2fs_status.status.success() {
        return process_failure("BASE_IMAGE_EXT4_FAILED", "mke2fs", &mke2fs_status.stderr);
    }
    canonicalize_ext4_ctimes(rootfs, output, debugfs)?;
    let mut e2fsck_command = std::process::Command::new(e2fsck);
    configure_e2fs_environment(&mut e2fsck_command);
    let checked = e2fsck_command
        .args(["-f", "-n", path_as_str(output)?])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .output()
        .map_err(io_build_error)?;
    // e2fsck returns 0 for clean and 1 when it corrected nothing in read-only
    // mode but observed a benign state. Any higher bit is a hard failure.
    if !matches!(checked.status.code(), Some(0 | 1)) {
        return process_failure("BASE_IMAGE_EXT4_FAILED", "e2fsck", &checked.stderr);
    }
    Ok(())
}

fn configure_e2fs_environment(command: &mut std::process::Command) {
    command
        .env_clear()
        .env("E2FSPROGS_FAKE_TIME", "1")
        .env("HOME", "/nonexistent")
        .env("LANG", "C.UTF-8")
        .env("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
        .env("TZ", "UTC");
}

fn canonicalize_ext4_ctimes(
    rootfs: &Path,
    output: &Path,
    debugfs: &Path,
) -> Result<(), BaseImageBuildError> {
    let command_path = output.with_extension("debugfs");
    let mut paths = vec![PathBuf::from("/"), PathBuf::from("/lost+found")];
    collect_rootfs_paths(rootfs, rootfs, &mut paths)?;
    paths.sort();
    let result = (|| {
        let mut commands = OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o600)
            .open(&command_path)
            .map_err(io_build_error)?;
        for path in paths {
            let path = path_as_str(&path)?;
            if path
                .bytes()
                .any(|byte| byte.is_ascii_control() || matches!(byte, b'"' | b'\\'))
            {
                return build_error(
                    "OCI_PATH_UNSAFE",
                    "OCI path cannot be represented safely for ext4 canonicalization",
                );
            }
            writeln!(commands, "set_inode_field \"{path}\" ctime 1").map_err(io_build_error)?;
            writeln!(commands, "set_inode_field \"{path}\" ctime_extra 0")
                .map_err(io_build_error)?;
        }
        commands.sync_all().map_err(io_build_error)?;
        let mut debugfs_command = std::process::Command::new(debugfs);
        configure_e2fs_environment(&mut debugfs_command);
        let completed = debugfs_command
            .args([
                "-w",
                "-f",
                path_as_str(&command_path)?,
                path_as_str(output)?,
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .output()
            .map_err(io_build_error)?;
        let diagnostic = String::from_utf8_lossy(
            &completed.stderr[..completed.stderr.len().min(MAX_PROCESS_OUTPUT)],
        );
        let unexpected = diagnostic.lines().any(|line| !line.starts_with("debugfs "));
        if !completed.status.success() || unexpected {
            return Err(BaseImageBuildError::new(
                "BASE_IMAGE_CANONICALIZATION_FAILED",
                format!("debugfs failed: {diagnostic}"),
                false,
                None,
            ));
        }
        Ok(())
    })();
    let _ = fs::remove_file(&command_path);
    result
}

fn collect_rootfs_paths(
    rootfs: &Path,
    directory: &Path,
    paths: &mut Vec<PathBuf>,
) -> Result<(), BaseImageBuildError> {
    let mut entries = fs::read_dir(directory)
        .map_err(io_build_error)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(io_build_error)?;
    entries.sort_by_key(std::fs::DirEntry::file_name);
    for entry in entries {
        let path = entry.path();
        let relative = path.strip_prefix(rootfs).map_err(|_| {
            BaseImageBuildError::new(
                "BASE_IMAGE_PATH_UNSAFE",
                "rootfs entry escaped canonicalization root",
                false,
                None,
            )
        })?;
        paths.push(Path::new("/").join(relative));
        let metadata = fs::symlink_metadata(&path).map_err(io_build_error)?;
        if metadata.is_dir() && !metadata.file_type().is_symlink() {
            collect_rootfs_paths(rootfs, &path, paths)?;
        }
    }
    Ok(())
}

fn path_as_str(path: &Path) -> Result<&str, BaseImageBuildError> {
    path.to_str().ok_or_else(|| {
        BaseImageBuildError::new(
            "BASE_IMAGE_PATH_UNSAFE",
            "BASE_IMAGE path is not UTF-8",
            false,
            None,
        )
    })
}

async fn run_bounded_process(
    executable: &Path,
    arguments: &[String],
    timeout: Duration,
    home: &Path,
    failure_code: &str,
) -> Result<Vec<u8>, BaseImageBuildError> {
    let mut command = Command::new(executable);
    command
        .args(arguments)
        .env_clear()
        .env("HOME", home)
        .env("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
        .env("LANG", "C.UTF-8")
        .env("LC_ALL", "C.UTF-8")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    let mut child = command.spawn().map_err(|error| {
        BaseImageBuildError::new(
            "BASE_IMAGE_TOOL_MISSING",
            format!("could not start {}: {error}", executable.display()),
            false,
            None,
        )
    })?;
    let stdout = child.stdout.take().expect("piped stdout");
    let stderr = child.stderr.take().expect("piped stderr");
    let stdout_task = tokio::spawn(async move {
        let mut value = Vec::new();
        stdout
            .take((MAX_PROCESS_OUTPUT + 1) as u64)
            .read_to_end(&mut value)
            .await
            .map(|_| value)
    });
    let stderr_task = tokio::spawn(async move {
        let mut value = Vec::new();
        stderr
            .take((MAX_PROCESS_OUTPUT + 1) as u64)
            .read_to_end(&mut value)
            .await
            .map(|_| value)
    });
    let status = match tokio::time::timeout(timeout, child.wait()).await {
        Ok(result) => result.map_err(io_build_error)?,
        Err(_) => {
            let _ = child.kill().await;
            let _ = child.wait().await;
            return Err(BaseImageBuildError::new(
                failure_code,
                format!("{} timed out", executable.display()),
                true,
                None,
            ));
        }
    };
    let stdout = stdout_task
        .await
        .map_err(|error| io_build_error(io::Error::other(error.to_string())))?
        .map_err(io_build_error)?;
    let stderr = stderr_task
        .await
        .map_err(|error| io_build_error(io::Error::other(error.to_string())))?
        .map_err(io_build_error)?;
    if stdout.len() > MAX_PROCESS_OUTPUT || stderr.len() > MAX_PROCESS_OUTPUT {
        return build_error(
            "BASE_IMAGE_PROCESS_OUTPUT_EXCEEDED",
            "base image tool output exceeds 64 KiB",
        );
    }
    if !status.success() {
        return process_failure(failure_code, &executable.display().to_string(), &stderr);
    }
    Ok(stdout)
}

fn read_json_object(path: &Path) -> Result<serde_json::Map<String, Value>, BaseImageBuildError> {
    let metadata = fs::symlink_metadata(path).map_err(io_build_error)?;
    if !metadata.is_file() || metadata.file_type().is_symlink() || metadata.len() > MAX_JSON_BYTES {
        return build_error(
            "OCI_LAYOUT_INVALID",
            "OCI JSON is not a bounded regular file",
        );
    }
    let payload = fs::read(path).map_err(io_build_error)?;
    let value: Value = serde_json::from_slice(&payload).map_err(|error| {
        BaseImageBuildError::new(
            "OCI_LAYOUT_INVALID",
            format!("OCI JSON is invalid: {error}"),
            false,
            None,
        )
    })?;
    value.as_object().cloned().ok_or_else(|| {
        BaseImageBuildError::new(
            "OCI_LAYOUT_INVALID",
            "OCI JSON root is not an object",
            false,
            None,
        )
    })
}

fn validate_descriptor(
    descriptor: &Value,
    media_types: Option<&[&str]>,
) -> Result<(), BaseImageBuildError> {
    let object = descriptor.as_object().ok_or_else(|| {
        BaseImageBuildError::new(
            "OCI_LAYOUT_INVALID",
            "OCI descriptor is not an object",
            false,
            None,
        )
    })?;
    let digest = object
        .get("digest")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            BaseImageBuildError::new(
                "OCI_DIGEST_INVALID",
                "OCI descriptor digest is absent",
                false,
                None,
            )
        })?;
    validate_sha256_digest(digest)?;
    let size = descriptor_size(descriptor)?;
    if size == 0 || size > MAX_LAYER_BLOB_BYTES {
        return build_error(
            "OCI_LAYOUT_INVALID",
            "OCI descriptor size is outside its bound",
        );
    }
    if let Some(allowed) = media_types {
        let media_type = object
            .get("mediaType")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                BaseImageBuildError::new(
                    "OCI_MEDIA_TYPE_UNSUPPORTED",
                    "OCI descriptor media type is absent",
                    false,
                    None,
                )
            })?;
        if !allowed.contains(&media_type) {
            return build_error(
                "OCI_MEDIA_TYPE_UNSUPPORTED",
                &format!("unsupported OCI media type {media_type:?}"),
            );
        }
    }
    Ok(())
}

fn descriptor_digest(descriptor: &Value) -> Result<&str, BaseImageBuildError> {
    descriptor
        .get("digest")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            BaseImageBuildError::new(
                "OCI_DIGEST_INVALID",
                "OCI descriptor digest is absent",
                false,
                None,
            )
        })
}

fn descriptor_size(descriptor: &Value) -> Result<u64, BaseImageBuildError> {
    descriptor
        .get("size")
        .and_then(Value::as_u64)
        .ok_or_else(|| {
            BaseImageBuildError::new(
                "OCI_LAYOUT_INVALID",
                "OCI descriptor size is invalid",
                false,
                None,
            )
        })
}

fn verified_blob(layout: &Path, descriptor: &Value) -> Result<PathBuf, BaseImageBuildError> {
    let digest = descriptor_digest(descriptor)?;
    let digest_hex = digest
        .strip_prefix("sha256:")
        .expect("validated SHA-256 digest");
    let root = layout
        .join("blobs/sha256")
        .canonicalize()
        .map_err(io_build_error)?;
    let candidate = root.join(digest_hex);
    let metadata = fs::symlink_metadata(&candidate).map_err(io_build_error)?;
    if !metadata.is_file()
        || metadata.file_type().is_symlink()
        || metadata.len() != descriptor_size(descriptor)?
    {
        return build_error(
            "OCI_DIGEST_INVALID",
            "OCI blob geometry differs from its descriptor",
        );
    }
    let resolved = candidate.canonicalize().map_err(io_build_error)?;
    if resolved.parent() != Some(root.as_path())
        || format!("sha256:{}", sha256_file(&resolved)?) != digest
    {
        return build_error(
            "OCI_DIGEST_INVALID",
            "OCI blob escaped its root or failed digest verification",
        );
    }
    Ok(resolved)
}

fn verify_uncompressed_layer(
    path: &Path,
    media_type: &str,
    expected_diff_id: &str,
) -> Result<(), BaseImageBuildError> {
    let mut reader = layer_reader(path, media_type)?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0_u8; 1024 * 1024];
    let mut total = 0_u64;
    loop {
        let read = reader.read(&mut buffer).map_err(io_build_error)?;
        if read == 0 {
            break;
        }
        total = total.saturating_add(read as u64);
        if total > MAX_UNPACKED_BYTES {
            return build_error(
                "OCI_LAYOUT_TOO_LARGE",
                "OCI uncompressed layer exceeds bound",
            );
        }
        hasher.update(&buffer[..read]);
    }
    let actual = format!("sha256:{:x}", hasher.finalize());
    if actual != expected_diff_id {
        return build_error(
            "OCI_DIGEST_INVALID",
            "OCI uncompressed layer differs from config diff_id",
        );
    }
    Ok(())
}

fn layer_archive(layer: &OciLayer) -> Result<Archive<Box<dyn Read>>, BaseImageBuildError> {
    Ok(Archive::new(layer_reader(&layer.path, &layer.media_type)?))
}

fn layer_reader(path: &Path, media_type: &str) -> Result<Box<dyn Read>, BaseImageBuildError> {
    let file = File::open(path).map_err(io_build_error)?;
    if media_type.ends_with("+gzip") || media_type.ends_with(".gzip") {
        Ok(Box::new(GzDecoder::new(file)))
    } else {
        Ok(Box::new(file))
    }
}

fn validate_entry_type(entry_type: EntryType) -> Result<(), BaseImageBuildError> {
    if entry_type.is_file()
        || entry_type.is_dir()
        || entry_type.is_symlink()
        || entry_type.is_hard_link()
        || entry_type.is_pax_global_extensions()
        || entry_type.is_pax_local_extensions()
        || entry_type.is_gnu_longname()
        || entry_type.is_gnu_longlink()
    {
        Ok(())
    } else {
        build_error(
            "OCI_SPECIAL_FILE_UNSUPPORTED",
            "OCI devices, FIFOs and unknown tar entries are forbidden",
        )
    }
}

fn validate_relative_path(path: &Path, allow_root: bool) -> Result<(), BaseImageBuildError> {
    if path.as_os_str().as_encoded_bytes().len() > MAX_PATH_BYTES {
        return build_error("OCI_PATH_UNSAFE", "OCI path exceeds 4096 bytes");
    }
    let mut count = 0_usize;
    for component in path.components() {
        match component {
            Component::Normal(value) if !value.is_empty() => count += 1,
            Component::CurDir => {}
            Component::ParentDir | Component::RootDir | Component::Prefix(_) => {
                return build_error(
                    "OCI_PATH_UNSAFE",
                    "OCI path is absolute or contains a parent traversal",
                )
            }
            Component::Normal(_) => unreachable!(),
        }
    }
    if count == 0 && !allow_root {
        return build_error("OCI_PATH_UNSAFE", "OCI path resolves to root");
    }
    Ok(())
}

fn validate_link_target(
    entry_path: &Path,
    link: &Path,
    hardlink: bool,
) -> Result<(), BaseImageBuildError> {
    if link.as_os_str().is_empty() || link.as_os_str().as_encoded_bytes().len() > MAX_PATH_BYTES {
        return build_error("OCI_PATH_UNSAFE", "OCI link target is empty or too long");
    }
    if link.is_absolute() {
        if hardlink {
            return build_error("OCI_PATH_UNSAFE", "OCI hard link target is absolute");
        }
        return Ok(());
    }
    let base = if hardlink {
        Path::new("")
    } else {
        entry_path.parent().unwrap_or_else(|| Path::new(""))
    };
    let mut depth = 0_i64;
    for component in base.components().chain(link.components()) {
        match component {
            Component::Normal(_) => depth += 1,
            Component::CurDir => {}
            Component::ParentDir => depth -= 1,
            Component::RootDir | Component::Prefix(_) => depth = -1,
        }
        if depth < 0 {
            return build_error("OCI_PATH_UNSAFE", "OCI link target escapes rootfs");
        }
    }
    Ok(())
}

fn resolve_destination_beneath(
    root: &Path,
    relative: &Path,
) -> Result<PathBuf, BaseImageBuildError> {
    validate_relative_path(relative, true)?;
    let mut current = root.to_path_buf();
    let mut components = relative.components().peekable();
    while let Some(component) = components.next() {
        let Component::Normal(value) = component else {
            if matches!(component, Component::CurDir) {
                continue;
            }
            return build_error("OCI_PATH_UNSAFE", "OCI destination path is unsafe");
        };
        current.push(value);
        if components.peek().is_some() {
            match fs::symlink_metadata(&current) {
                Ok(metadata) if metadata.file_type().is_symlink() => {
                    return build_error("OCI_PATH_UNSAFE", "OCI destination has a symlink ancestor")
                }
                Ok(metadata) if !metadata.is_dir() => {
                    return build_error(
                        "OCI_PATH_UNSAFE",
                        "OCI destination ancestor is not a directory",
                    )
                }
                Ok(_) => {}
                Err(error) if error.kind() == io::ErrorKind::NotFound => {
                    fs::create_dir(&current).map_err(io_build_error)?;
                    fs::set_permissions(&current, fs::Permissions::from_mode(0o755))
                        .map_err(io_build_error)?;
                }
                Err(error) => return Err(io_build_error(error)),
            }
        }
    }
    if !current.starts_with(root) {
        return build_error("OCI_PATH_UNSAFE", "OCI destination escaped rootfs");
    }
    Ok(current)
}

fn remove_entry(path: &Path) -> Result<(), BaseImageBuildError> {
    let metadata = fs::symlink_metadata(path).map_err(io_build_error)?;
    if metadata.is_dir() && !metadata.file_type().is_symlink() {
        fs::remove_dir_all(path).map_err(io_build_error)
    } else {
        fs::remove_file(path).map_err(io_build_error)
    }
}

fn canonicalize_tree_times(root: &Path) -> Result<(), BaseImageBuildError> {
    let mut stack = vec![root.to_path_buf()];
    while let Some(path) = stack.pop() {
        let metadata = fs::symlink_metadata(&path).map_err(io_build_error)?;
        if metadata.is_dir() && !metadata.file_type().is_symlink() {
            for entry in fs::read_dir(&path).map_err(io_build_error)? {
                stack.push(entry.map_err(io_build_error)?.path());
            }
        }
        set_epoch(&path, metadata.file_type().is_symlink())?;
    }
    Ok(())
}

fn set_epoch(path: &Path, nofollow: bool) -> Result<(), BaseImageBuildError> {
    let value = CString::new(path.as_os_str().as_encoded_bytes()).map_err(|_| {
        BaseImageBuildError::new("OCI_PATH_UNSAFE", "rootfs path contains NUL", false, None)
    })?;
    let times = [
        libc::timespec {
            tv_sec: 1,
            tv_nsec: 0,
        },
        libc::timespec {
            tv_sec: 1,
            tv_nsec: 0,
        },
    ];
    let flags = if nofollow {
        libc::AT_SYMLINK_NOFOLLOW
    } else {
        0
    };
    // SAFETY: value and times are valid for the duration of this syscall.
    if unsafe { libc::utimensat(libc::AT_FDCWD, value.as_ptr(), times.as_ptr(), flags) } != 0 {
        return Err(io_build_error(io::Error::last_os_error()));
    }
    Ok(())
}

fn create_job_root(cache_root: &Path) -> Result<PathBuf, BaseImageBuildError> {
    for _ in 0..16 {
        let path = cache_root.join(format!("base-image-{}", Uuid::new_v4().simple()));
        match fs::create_dir(&path) {
            Ok(()) => {
                fs::set_permissions(&path, fs::Permissions::from_mode(0o700))
                    .map_err(io_build_error)?;
                return Ok(path);
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(io_build_error(error)),
        }
    }
    build_error(
        "BASE_IMAGE_PATH_UNSAFE",
        "could not allocate a unique build directory",
    )
}

fn prepare_cache_root(path: &Path) -> Result<(), BaseImageBuildError> {
    if !path.is_absolute() {
        return build_error(
            "BASE_IMAGE_PATH_UNSAFE",
            "BASE_IMAGE cache root must be absolute",
        );
    }
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => {}
        Ok(_) => {
            return build_error(
                "BASE_IMAGE_PATH_UNSAFE",
                "BASE_IMAGE cache root is not a real directory",
            )
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            fs::create_dir_all(path).map_err(io_build_error)?;
        }
        Err(error) => return Err(io_build_error(error)),
    }
    fs::set_permissions(path, fs::Permissions::from_mode(0o700)).map_err(io_build_error)
}

fn remove_job_root(path: &Path, cache_root: &Path) -> Result<(), BaseImageBuildError> {
    if path.parent() != Some(cache_root) || !path.starts_with(cache_root) {
        return build_error(
            "BASE_IMAGE_PATH_UNSAFE",
            "refused to remove a path outside BASE_IMAGE cache root",
        );
    }
    match fs::remove_dir_all(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(io_build_error(error)),
    }
}

fn sha256_file(path: &Path) -> Result<String, BaseImageBuildError> {
    let mut file = File::open(path).map_err(io_build_error)?;
    file.seek(SeekFrom::Start(0)).map_err(io_build_error)?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0_u8; 1024 * 1024];
    loop {
        let read = file.read(&mut buffer).map_err(io_build_error)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

fn validate_sha256_digest(value: &str) -> Result<(), BaseImageBuildError> {
    let Some(hex) = value.strip_prefix("sha256:") else {
        return build_error("OCI_DIGEST_INVALID", "OCI digest is not SHA-256");
    };
    if hex.len() != 64
        || !hex
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return build_error(
            "OCI_DIGEST_INVALID",
            "OCI SHA-256 digest is not canonical lowercase hex",
        );
    }
    Ok(())
}

pub fn base_image_build_key(
    oci_digest: &str,
    oci_platform: &str,
    builder_version: &str,
) -> Result<String, BaseImageBuildError> {
    validate_sha256_digest(oci_digest)?;
    let platform = OciPlatform::parse(oci_platform)?;
    if platform.canonical() != oci_platform
        || builder_version.is_empty()
        || builder_version.len() > 256
    {
        return build_error(
            "BASE_IMAGE_BUILD_KEY_INVALID",
            "BASE_IMAGE build identity is not canonical",
        );
    }
    let identity = serde_json::json!({
        "base_image_builder_version": builder_version,
        "oci_digest": oci_digest,
        "oci_platform": oci_platform,
        "snapshot_format_version": SNAPSHOT_FORMAT_VERSION,
    });
    let payload = canonical_json_bytes(&identity).map_err(|error| {
        BaseImageBuildError::new(
            "BASE_IMAGE_BUILD_KEY_INVALID",
            error.to_string(),
            false,
            Some(oci_digest.into()),
        )
    })?;
    Ok(sha256_identity(&payload))
}

fn pin_oci_ref(oci_ref: &str, digest: &str) -> Result<String, BaseImageBuildError> {
    validate_oci_ref(oci_ref)?;
    validate_sha256_digest(digest)?;
    let repository = oci_ref.split_once('@').map_or(oci_ref, |(value, _)| value);
    let slash = repository.rfind('/');
    let tag = repository.rfind(':');
    let repository = if tag.is_some_and(|position| slash.is_none_or(|slash| position > slash)) {
        &repository[..tag.expect("tag position exists")]
    } else {
        repository
    };
    if repository.is_empty() {
        return build_error(
            "BASE_IMAGE_OCI_REF_INVALID",
            "OCI reference has no repository",
        );
    }
    Ok(format!("{repository}@{digest}"))
}

fn validate_oci_ref(value: &str) -> Result<(), BaseImageBuildError> {
    if value.is_empty()
        || value.len() > 1024
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._/:@+-".contains(&byte))
        || !value.as_bytes()[0].is_ascii_alphanumeric()
    {
        return build_error("BASE_IMAGE_OCI_REF_INVALID", "OCI reference is invalid");
    }
    Ok(())
}

fn valid_platform_part(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value.as_bytes()[0].is_ascii_lowercase()
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"._-".contains(&byte)
        })
}

fn digest_uuid(digest: &str) -> Result<String, BaseImageBuildError> {
    validate_sha256_digest(digest)?;
    let hex = &digest[7..39];
    Ok(format!(
        "{}-{}-{}-{}-{}",
        &hex[..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..32]
    ))
}

fn align_up(value: u64, alignment: u64) -> Result<u64, BaseImageBuildError> {
    value
        .checked_add(alignment - 1)
        .map(|value| value / alignment * alignment)
        .ok_or_else(|| {
            BaseImageBuildError::new(
                "BASE_IMAGE_GEOMETRY_INVALID",
                "image size alignment overflows",
                false,
                None,
            )
        })
}

fn require_executable(path: &Path, name: &str) -> Result<(), BaseImageBuildError> {
    let metadata = fs::metadata(path).map_err(|_| {
        BaseImageBuildError::new(
            "BASE_IMAGE_TOOL_MISSING",
            format!("{name} is missing"),
            false,
            None,
        )
    })?;
    if !metadata.is_file() || metadata.permissions().mode() & 0o111 == 0 {
        return build_error(
            "BASE_IMAGE_TOOL_MISSING",
            &format!("{name} is not executable"),
        );
    }
    Ok(())
}

fn process_failure<T>(
    code: &str,
    operation: &str,
    stderr: &[u8],
) -> Result<T, BaseImageBuildError> {
    if code == "BASE_IMAGE_ROOTFS_FAILED" {
        if let Some(error) = decode_rootfs_worker_error(stderr) {
            return Err(error);
        }
    }
    let stderr = String::from_utf8_lossy(&stderr[..stderr.len().min(MAX_PROCESS_OUTPUT)]);
    Err(BaseImageBuildError::new(
        code,
        format!("{operation} failed: {stderr}"),
        true,
        None,
    ))
}

fn io_build_error(error: io::Error) -> BaseImageBuildError {
    BaseImageBuildError::new("BASE_IMAGE_IO_FAILED", error.to_string(), false, None)
}

fn build_error<T>(code: &str, message: &str) -> Result<T, BaseImageBuildError> {
    Err(BaseImageBuildError::new(code, message, false, None))
}

#[cfg(test)]
mod accelerated_import_tests {
    use super::*;
    use hyper::service::{make_service_fn, service_fn};
    use hyper::{Body, Request, Response, StatusCode};
    use serde_json::json;
    use std::collections::HashMap;
    use std::convert::Infallible;
    use std::sync::Mutex;

    struct RegistryFixture {
        client: Arc<RegistryClient>,
        source: RegistryManifest,
        objects: Arc<Mutex<HashMap<String, Vec<u8>>>>,
        paths: Arc<Mutex<Vec<String>>>,
        server: tokio::task::JoinHandle<()>,
    }

    impl Drop for RegistryFixture {
        fn drop(&mut self) {
            self.server.abort();
        }
    }

    impl RegistryFixture {
        fn set_manifest(&self, reference: &str, value: Value) -> RegistryManifest {
            let manifest = manifest(value);
            let mut objects = self.objects.lock().unwrap();
            objects.insert(format!("manifests/{reference}"), manifest.payload.clone());
            objects.insert(
                format!("manifests/{}", manifest.digest),
                manifest.payload.clone(),
            );
            manifest
        }
    }

    fn manifest(value: Value) -> RegistryManifest {
        let payload = serde_json::to_vec(&value).unwrap();
        RegistryManifest {
            digest: sha256_identity(&payload),
            payload,
            value,
        }
    }

    fn registry_fixture(
        candidates: &[(&str, Option<&str>, &str)],
        failure: Option<StatusCode>,
    ) -> RegistryFixture {
        let config = serde_json::to_vec(&json!({
            "os":"linux", "architecture":"amd64",
            "rootfs":{"type":"layers", "diff_ids":[format!("sha256:{}", "1".repeat(64))]}
        }))
        .unwrap();
        let config_digest = sha256_identity(&config);
        let source = manifest(json!({
            "schemaVersion":2, "mediaType":MANIFEST_MEDIA_TYPES[0],
            "config":{"digest":config_digest,"size":config.len()},
            "layers":[{"digest":format!("sha256:{}", "2".repeat(64)),"size":1024}]
        }));
        let mut objects = HashMap::new();
        objects.insert("manifests/v1".to_owned(), source.payload.clone());
        objects.insert(
            format!("manifests/{}", source.digest),
            source.payload.clone(),
        );
        objects.insert(format!("blobs/{config_digest}"), config);
        for (tag, subject, architecture) in candidates {
            let mut value = source.value.clone();
            if tag.ends_with("_containerd_accelerated") {
                value["artifactType"] =
                    json!("application/vnd.containerd.overlaybd.native.v1+json");
            } else {
                value["layers"][0]["annotations"] = json!({
                    "dadi.image.layer.v1.digest":format!("sha256:{}", "3".repeat(64)),
                    "dadi.image.layer.v1.size":"2048"
                });
            }
            if let Some(subject) = subject {
                value["subject"] = json!({
                    "digest":if *subject == "source" {source.digest.clone()} else {subject.to_string()},
                    "size":source.payload.len(), "mediaType":MANIFEST_MEDIA_TYPES[0]
                });
            }
            if *architecture != "amd64" {
                let config =
                    serde_json::to_vec(&json!({"os":"linux","architecture":architecture})).unwrap();
                let digest = sha256_identity(&config);
                value["config"] = json!({"digest":digest,"size":config.len()});
                objects.insert(format!("blobs/{digest}"), config);
            }
            let candidate = manifest(value);
            objects.insert(format!("manifests/{tag}"), candidate.payload.clone());
            objects.insert(format!("manifests/{}", candidate.digest), candidate.payload);
        }
        let socket = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        socket.set_nonblocking(true).unwrap();
        let client =
            RegistryClient::test_http_client(&format!("http://{}", socket.local_addr().unwrap()));
        let paths = Arc::new(Mutex::new(Vec::new()));
        let observed = paths.clone();
        let objects = Arc::new(Mutex::new(objects));
        let served_objects = objects.clone();
        let service = make_service_fn(move |_| {
            let objects = served_objects.clone();
            let paths = observed.clone();
            async move {
                Ok::<_, Infallible>(service_fn(move |request: Request<Body>| {
                    let path = request.uri().path().to_owned();
                    paths.lock().unwrap().push(path.clone());
                    let key = path.strip_prefix("/v2/team/test/").unwrap();
                    let response = if let Some(bytes) = objects.lock().unwrap().get(key) {
                        if key.starts_with("blobs/") {
                            assert_eq!(
                                request.headers()["range"],
                                format!("bytes=0-{}", bytes.len() - 1)
                            );
                            Response::builder()
                                .status(206)
                                .header(
                                    "content-range",
                                    format!("bytes 0-{}/{}", bytes.len() - 1, bytes.len()),
                                )
                                .body(Body::from(bytes.clone()))
                                .unwrap()
                        } else {
                            Response::new(Body::from(bytes.clone()))
                        }
                    } else {
                        Response::builder()
                            .status(failure.unwrap_or(StatusCode::NOT_FOUND))
                            .body(Body::empty())
                            .unwrap()
                    };
                    async move { Ok::<_, Infallible>(response) }
                }))
            }
        });
        let server = tokio::spawn(async move {
            hyper::Server::from_tcp(socket)
                .unwrap()
                .serve(service)
                .await
                .unwrap();
        });
        RegistryFixture {
            client,
            source,
            objects,
            paths,
            server,
        }
    }

    async fn discover(
        fixture: &RegistryFixture,
    ) -> Result<Option<ResolvedImage>, BaseImageBuildError> {
        discover_registry_rootfs(
            &ResolveImage {
                oci_ref: format!("{}/team/test:v1", fixture.client.registry()),
                platform: "linux/amd64".into(),
            },
            &OciPlatform::parse("linux/amd64").unwrap(),
            &fixture.source.digest,
            &fixture.client,
        )
        .await
    }

    #[test]
    fn discovery_suffixes_preserve_repository_and_never_rewrite_digest_inputs() {
        assert_eq!(
            accelerated_candidates("registry.example:5000/team/test:v1"),
            vec![
                "registry.example:5000/team/test:v1_containerd_accelerated",
                "registry.example:5000/team/test:v1_accelerated",
            ]
        );
        assert_eq!(
            accelerated_candidates("registry.example/team/test")[0],
            "registry.example/team/test:latest_containerd_accelerated"
        );
        for reference in [
            format!("registry.example/team/test@sha256:{}", "a".repeat(64)),
            format!("registry.example/team/test:v1@sha256:{}", "a".repeat(64)),
            "registry.example/team/test:v1_accelerated".into(),
            "registry.example/team/test:v1_containerd_accelerated".into(),
            "ubuntu:22.04".into(),
            "library/ubuntu:22.04".into(),
            "team/app:v1".into(),
            format!("registry.example/team/test:{}", "a".repeat(128)),
        ] {
            assert!(accelerated_candidates(&reference).is_empty(), "{reference}");
        }
    }

    #[tokio::test]
    async fn discovery_prefers_containerd_and_pins_the_native_manifest() {
        let fixture = registry_fixture(
            &[
                ("v1_containerd_accelerated", Some("source"), "amd64"),
                ("v1_accelerated", Some("source"), "amd64"),
            ],
            None,
        );
        let resolved = discover(&fixture).await.unwrap().unwrap();
        assert!(resolved.oci_ref.ends_with(":v1_containerd_accelerated"));
        assert_ne!(resolved.oci_digest, fixture.source.digest);
        assert_eq!(
            resolved.base_image_builder_version,
            REGISTRY_BASE_IMAGE_BUILDER_VERSION
        );
        assert_eq!(
            resolved.base_image_build_key,
            base_image_build_key(
                &resolved.oci_digest,
                "linux/amd64",
                REGISTRY_BASE_IMAGE_BUILDER_VERSION
            )
            .unwrap()
        );
        let paths = fixture.paths.lock().unwrap();
        assert!(!paths.iter().any(|path| path.ends_with("/v1_accelerated")));
        // Discovery downloads manifests/config only, never layer DATA.
        assert!(!paths.iter().any(|path| path.contains(&"2".repeat(64))));
    }

    #[tokio::test]
    async fn discovery_tries_legacy_suffix_and_falls_back_when_tags_are_missing() {
        let fixture = registry_fixture(&[("v1_accelerated", None, "amd64")], None);
        assert!(discover(&fixture)
            .await
            .unwrap()
            .unwrap()
            .oci_ref
            .ends_with(":v1_accelerated"));
        let fixture = registry_fixture(&[], None);
        assert!(discover(&fixture).await.unwrap().is_none());
        assert_eq!(fixture.paths.lock().unwrap().len(), 2);
    }

    #[tokio::test]
    async fn discovery_skips_stale_or_wrong_platform_candidates() {
        let wrong = format!("sha256:{}", "f".repeat(64));
        for candidate in [
            ("v1_containerd_accelerated", Some(wrong.as_str()), "amd64"),
            ("v1_containerd_accelerated", Some("source"), "arm64"),
        ] {
            let fixture = registry_fixture(
                &[candidate, ("v1_accelerated", Some("source"), "amd64")],
                None,
            );
            assert!(discover(&fixture)
                .await
                .unwrap()
                .unwrap()
                .oci_ref
                .ends_with(":v1_accelerated"));
        }
        let fixture = registry_fixture(&[("v1_accelerated", None, "arm64")], None);
        assert!(discover(&fixture).await.unwrap().is_none());
    }

    #[tokio::test]
    async fn discovery_reports_auth_and_transient_failures_instead_of_treating_them_as_absent() {
        for (status, retryable) in [
            (StatusCode::FORBIDDEN, false),
            (StatusCode::SERVICE_UNAVAILABLE, true),
        ] {
            let fixture = registry_fixture(&[], Some(status));
            let error = discover(&fixture).await.unwrap_err();
            assert_eq!(error.code, "BASE_IMAGE_REGISTRY_FAILED");
            assert_eq!(error.retryable, retryable);
        }
    }

    #[tokio::test]
    async fn discovery_rejects_unbound_config_and_detects_source_tag_changes() {
        let fixture = registry_fixture(&[("v1_accelerated", None, "amd64")], None);
        let mut original = fixture.source.value.clone();
        original["config"]["digest"] = json!(format!("sha256:{}", "e".repeat(64)));
        let changed = fixture.set_manifest("v1", original);
        let request = ResolveImage {
            oci_ref: format!("{}/team/test:v1", fixture.client.registry()),
            platform: "linux/amd64".into(),
        };
        assert!(discover_registry_rootfs(
            &request,
            &OciPlatform::parse("linux/amd64").unwrap(),
            &changed.digest,
            &fixture.client
        )
        .await
        .unwrap()
        .is_none());
        assert_eq!(
            discover(&fixture).await.unwrap_err().code,
            "IDENTITY_CONFLICT"
        );
    }

    #[tokio::test]
    async fn discovery_skips_sibling_tags_that_are_ordinary_tar_images() {
        let fixture = registry_fixture(&[], None);
        fixture.set_manifest("v1_accelerated", fixture.source.value.clone());
        assert!(discover(&fixture).await.unwrap().is_none());
        assert!(!fixture
            .paths
            .lock()
            .unwrap()
            .iter()
            .any(|path| path.contains("/blobs/")));
    }

    #[tokio::test]
    async fn discovery_accepts_platform_indexes_bound_to_the_source_index() {
        let fixture = registry_fixture(&[("v1_containerd_accelerated", None, "amd64")], None);
        let source_index = fixture.set_manifest(
            "v1",
            json!({"schemaVersion":2,"manifests":[{
                "digest":fixture.source.digest,"size":fixture.source.payload.len(),
                "platform":{"os":"linux","architecture":"amd64"}
            }]}),
        );
        let candidate =
            fixture.objects.lock().unwrap()["manifests/v1_containerd_accelerated"].clone();
        let candidate_digest = sha256_identity(&candidate);
        fixture.set_manifest(
            "v1_containerd_accelerated",
            json!({
                "schemaVersion":2,
                "subject":{"digest":source_index.digest,"size":source_index.payload.len()},
                "manifests":[{"digest":candidate_digest,"size":candidate.len(),
                    "platform":{"os":"linux","architecture":"amd64"}}]
            }),
        );
        assert_eq!(
            discover(&fixture).await.unwrap().unwrap().oci_digest,
            candidate_digest
        );
    }

    struct MetadataOnlyPuller(String);

    #[async_trait]
    impl OciLayoutPuller for MetadataOnlyPuller {
        async fn resolve(
            &self,
            _: &str,
            _: &OciPlatform,
            _: Duration,
            _: &Path,
        ) -> Result<String, BaseImageBuildError> {
            Ok(self.0.clone())
        }
        async fn pull(
            &self,
            _: &str,
            _: &Path,
            _: &OciPlatform,
            _: Duration,
            _: &Path,
        ) -> Result<(), BaseImageBuildError> {
            panic!("resolve must never pull full image layers");
        }
    }

    #[tokio::test]
    async fn builder_resolve_selects_native_or_retains_the_ordinary_builder() {
        for native in [true, false] {
            let fixture = registry_fixture(
                if native {
                    &[("v1_containerd_accelerated", Some("source"), "amd64")]
                } else {
                    &[]
                },
                None,
            );
            let root = tempfile::tempdir().unwrap();
            let executable = PathBuf::from("/bin/true");
            let builder = OciExt4BaseImageBuilder::new_with_source_resolver(
                OciExt4BuilderConfig {
                    cache_root: root.path().join("builds"),
                    worker_binary: executable.clone(),
                    fakeroot_binary: Some(executable.clone()),
                    mke2fs_binary: executable.clone(),
                    e2fsck_binary: executable.clone(),
                    debugfs_binary: executable,
                    minimum_image_bytes: IMAGE_ALIGNMENT,
                    headroom_bytes: 0,
                    timeout: Duration::from_secs(5),
                },
                Arc::new(MetadataOnlyPuller(fixture.source.digest.clone())),
                Arc::new(OverlaybdOciSourceResolver::default()),
                vec![fixture.client.clone()],
            )
            .unwrap();
            let request = ResolveImage {
                oci_ref: format!("{}/team/test:v1", fixture.client.registry()),
                platform: "linux/amd64".into(),
            };
            let resolved = builder.resolve(&request).await.unwrap();
            if native {
                assert_eq!(
                    resolved.base_image_builder_version,
                    REGISTRY_BASE_IMAGE_BUILDER_VERSION
                );
                // The resolved accelerated locator can also be passed explicitly.
                assert_eq!(
                    builder
                        .resolve(&ResolveImage {
                            oci_ref: resolved.oci_ref.clone(),
                            platform: request.platform
                        })
                        .await
                        .unwrap(),
                    resolved
                );
            } else {
                assert_eq!(
                    resolved.base_image_builder_version,
                    BASE_IMAGE_BUILDER_VERSION
                );
                assert_eq!(resolved.oci_digest, fixture.source.digest);
                assert_eq!(resolved.oci_ref, request.oci_ref);
            }
            assert_eq!(
                std::fs::read_dir(root.path().join("builds"))
                    .unwrap()
                    .count(),
                0
            );
        }
    }

    #[tokio::test]
    async fn private_memory_platform_parses_but_cannot_be_a_cold_boot_disk() {
        let platform = OciPlatform::parse("linux/snapd-memory-amd64").unwrap();
        assert_eq!(platform.os, "linux");
        assert_eq!(platform.architecture, "snapd-memory-amd64");
        assert_eq!(platform.variant, None);
        assert!(registry_manifest(
            "registry.invalid/team/test:latest",
            None,
            &platform,
            &RegistryClient::new("registry.invalid").unwrap()
        )
        .await
        .is_err());
    }

    #[test]
    fn dadi_annotations_select_native_blobs_without_rewriting_requested_tag() {
        let native_digest = format!("sha256:{}", "a".repeat(64));
        let wrapper_digest = format!("sha256:{}", "b".repeat(64));
        let manifest = json!({"schemaVersion":2,"layers":[{
            "digest":wrapper_digest,"size":7168,"mediaType":"application/vnd.docker.image.rootfs.diff.tar",
            "annotations":{"dadi.image.layer.v1.digest":native_digest,"dadi.image.layer.v1.size":"123456"}
        }]});
        assert_eq!(
            accelerated_layers(&manifest).unwrap(),
            vec![RegistryLayerRef {
                digest: native_digest,
                size: 123456
            }]
        );
        for reference in [
            "registry.example/team/test:v_accelerated",
            "registry.example/team/test:v_containerd_accelerated",
            "registry.example/team/test:v_accelerated@sha256:abc",
        ] {
            assert!(accelerated_reference(reference));
        }
        assert!(!accelerated_reference(
            "registry.example/team/test:ordinary"
        ));
    }

    #[test]
    fn ordinary_or_incomplete_locator_manifests_fail_instead_of_falling_back() {
        let digest = format!("sha256:{}", "c".repeat(64));
        let ordinary = json!({"schemaVersion":2,"layers":[{"digest":digest,"size":123}]});
        assert_eq!(
            accelerated_layers(&ordinary).unwrap_err().code,
            "BASE_IMAGE_ACCELERATED_INVALID"
        );
        let mut missing_size = ordinary.clone();
        missing_size["layers"][0]["annotations"] = json!({"dadi.image.layer.v1.digest":digest});
        assert!(accelerated_layers(&missing_size).is_err());
        let mut native = ordinary;
        native["artifactType"] = json!("application/vnd.containerd.overlaybd.native.v1+json");
        assert_eq!(accelerated_layers(&native).unwrap()[0].digest, digest);
        native["layers"][0]["annotations"] =
            json!({"containerd.io/snapshot/overlaybd/blob-size":"124"});
        assert!(accelerated_layers(&native).is_err());
    }
}
