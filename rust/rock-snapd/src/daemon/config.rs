//! Strict write-domain configuration plus image-owned node defaults.

use std::io;
use std::path::PathBuf;

use serde::Deserialize;
use thiserror::Error;

use crate::daemon::bootstrap::DaemonCompositionError;
pub use crate::snapshot::io::oci_publication::RegistrySnapshotTagMode;
use crate::snapshot::ValidateCanonicalModel;

pub(super) const SNAPD_PORT: u16 = 22_556;
pub(super) const STORAGE_ENDPOINT_PROFILE: &str = "default";
pub(super) const STORAGE_CLIENT_IDENTITY: &str = "rock-snapd-worker";
pub(super) const BACKING_ROOT: &str = "/r/backings";
pub(super) const MAX_INFLIGHT_PER_VM: usize = 8;
pub(super) const MAX_PENDING_PER_VM: usize = 256;
pub(super) const REMOTE_LOAD_TIMEOUT_SECONDS: u64 = 30;
pub(super) const RUNTIME_START_TIMEOUT_SECONDS: u64 = 300;
pub(super) const MAX_CONCURRENT_DATA_UPLOADS: usize = 4;
pub(super) const MAX_QUEUED_DATA_UPLOADS: usize = 64;
pub(super) const MAX_DATA_UPLOAD_BYTES_IN_FLIGHT: u64 = 256 * 1024 * 1024;
pub(super) const DATA_UPLOAD_CHUNK_BYTES: usize = 8 * 1024 * 1024;
pub(super) const DATA_UPLOAD_ATTEMPT_TIMEOUT_SECONDS: u64 = 300;
pub(super) const P2P_PROBE_TIMEOUT_MS: u64 = 300;
pub(super) const P2P_RANGE_TIMEOUT_MS: u64 = 1_500;

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RustSnapdConfig {
    #[serde(default)]
    pub storage: Option<S3NodeConfig>,
    #[serde(default)]
    pub upload_format: SnapshotUploadFormat,
    #[serde(default)]
    pub registry: Option<RegistryNodeConfig>,
    #[serde(default)]
    pub registry_upload: Option<RegistryNodeConfig>,
    #[serde(default)]
    pub guest_readiness: crate::vm::runtime::plugger::GuestReadinessMode,
    #[serde(default)]
    pub local_cache: LocalCacheNodeConfig,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SnapshotUploadFormat {
    #[default]
    Disabled,
    OverlaybdS3,
    OverlaybdOci,
}

impl SnapshotUploadFormat {
    pub(super) fn enabled(self) -> bool {
        self != Self::Disabled
    }
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RegistryNodeConfig {
    pub registry: String,
    /// Only this exact HTTPS token endpoint may receive Registry credentials
    /// when a Bearer challenge points outside the Registry origin.
    #[serde(default)]
    pub auth_realm: Option<String>,
    /// An explicit fixed destination remains available for existing deployments.
    /// Otherwise each snapshot keeps its source image's complete repository path.
    #[serde(default)]
    pub repository: Option<String>,
    /// Optionally place source repositories underneath one target namespace.
    #[serde(default)]
    pub namespace: Option<String>,
    pub credential_ref: String,
    #[serde(default)]
    pub allow_http: bool,
    #[serde(default)]
    pub username: Option<String>,
    #[serde(default)]
    pub password: Option<String>,
    #[serde(default = "default_registry_tag_prefix")]
    pub tag_prefix: String,
    /// Source-derived tags are the compatible default. Snapshot IDs can instead
    /// be published unchanged as Registry tags; tag_prefix then has no effect.
    #[serde(default)]
    pub tag_mode: RegistrySnapshotTagMode,
}

impl std::fmt::Debug for RegistryNodeConfig {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("RegistryNodeConfig")
            .field("registry", &self.registry)
            .field("auth_realm", &self.auth_realm)
            .field("repository", &self.repository)
            .field("namespace", &self.namespace)
            .field("credential_ref", &self.credential_ref)
            .field("allow_http", &self.allow_http)
            .field("authenticated", &self.username.is_some())
            .field("tag_prefix", &self.tag_prefix)
            .field("tag_mode", &self.tag_mode)
            .finish()
    }
}

fn default_registry_tag_prefix() -> String {
    "snapshot-".into()
}

/// Physical cache objects and bounded generated-object staging. Unpublished
/// sources are pinned; live native backings remain outside this disk budget.
#[derive(Clone, Debug, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct LocalCacheNodeConfig {
    pub max_bytes: u64,
}

impl Default for LocalCacheNodeConfig {
    fn default() -> Self {
        Self {
            max_bytes: crate::snapshot::transport::posix_file::DEFAULT_MAX_BYTES,
        }
    }
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct S3NodeConfig {
    pub endpoint_url: String,
    pub region: String,
    pub bucket: String,
    pub credential_ref: String,
    pub access_key_id: String,
    pub access_key_secret: String,
    #[serde(default)]
    pub session_token: Option<String>,
}

#[derive(Clone, Debug)]
pub struct VmmNodeConfig {
    pub binary: PathBuf,
    pub kernel: PathBuf,
    pub initramfs: PathBuf,
    pub cache_root: PathBuf,
    pub private_root: PathBuf,
    pub kernel_cmdline: String,
    pub default_cpu_count: u16,
    pub default_memory_mib: u64,
    pub fault_around_pages: u32,
    pub uffd_legacy: String,
    pub guest_network: bool,
    pub guest_readiness: crate::vm::runtime::plugger::GuestReadinessMode,
    pub rocklet: RockletArtifactConfig,
}

#[derive(Clone, Debug)]
pub struct RockletArtifactConfig {
    pub path: PathBuf,
    pub guest_port: u32,
    pub proxy_port_start: u16,
    pub proxy_port_end: u16,
}

#[derive(Clone, Debug)]
pub struct BaseImageNodeConfig {
    pub cache_root: PathBuf,
    pub worker_binary: PathBuf,
    pub skopeo_binary: PathBuf,
    pub fakeroot_binary: Option<PathBuf>,
    pub mke2fs_binary: PathBuf,
    pub e2fsck_binary: PathBuf,
    pub debugfs_binary: PathBuf,
    pub minimum_image_bytes: u64,
    pub headroom_bytes: u64,
    pub timeout_seconds: u64,
}

impl VmmNodeConfig {
    pub(super) fn image_defaults(uffd_legacy: String) -> Self {
        Self {
            binary: "/opt/rock-vmm/cloud-hypervisor".into(),
            kernel: "/opt/rock-vmm/vmlinux".into(),
            initramfs: "/opt/rock-vmm/rocklet-initramfs.cpio.gz".into(),
            cache_root: "/r/cache".into(),
            private_root: "/r/vmm".into(),
            kernel_cmdline: default_kernel_cmdline(),
            default_cpu_count: 1,
            default_memory_mib: 128,
            fault_around_pages: 4,
            uffd_legacy,
            guest_network: false,
            guest_readiness: crate::vm::runtime::plugger::GuestReadinessMode::Rocklet,
            rocklet: RockletArtifactConfig {
                path: "/opt/rocklet/rocklet.raw".into(),
                guest_port: 22_555,
                proxy_port_start: 31_000,
                proxy_port_end: 31_063,
            },
        }
    }
}

impl BaseImageNodeConfig {
    pub(super) fn image_defaults() -> Self {
        Self {
            cache_root: "/r/base".into(),
            worker_binary: "/usr/local/bin/rock-snapd-base-image-worker".into(),
            skopeo_binary: "/usr/bin/skopeo".into(),
            fakeroot_binary: Some("/usr/bin/fakeroot".into()),
            mke2fs_binary: "/usr/sbin/mke2fs".into(),
            e2fsck_binary: "/usr/sbin/e2fsck".into(),
            debugfs_binary: "/usr/sbin/debugfs".into(),
            minimum_image_bytes: 64 * 1024 * 1024,
            headroom_bytes: 64 * 1024 * 1024,
            timeout_seconds: 900,
        }
    }
}

#[derive(Debug, Error)]
pub enum DaemonError {
    #[error("invalid Rust snapd daemon configuration: {0}")]
    Invalid(String),
    #[error("Rust snapd daemon I/O failed")]
    Io(#[source] io::Error),
    #[error(transparent)]
    Composition(#[from] DaemonCompositionError),
    #[error("Rust snapd daemon component failed: {0}")]
    Component(String),
}

impl RustSnapdConfig {
    pub(super) fn registry_upload_config(&self) -> Option<&RegistryNodeConfig> {
        self.registry_upload.as_ref().or(self.registry.as_ref())
    }

    pub(super) fn validate(&self) -> Result<(), DaemonError> {
        if let Some(storage) = &self.storage {
            if storage.endpoint_url.is_empty()
                || storage.region.is_empty()
                || storage.bucket.is_empty()
                || invalid_secret(&storage.access_key_id)
                || invalid_secret(&storage.access_key_secret)
                || storage.session_token.as_deref().is_some_and(invalid_secret)
            {
                return Err(DaemonError::Invalid(
                    "storage endpoint, namespace or inline credential is invalid".into(),
                ));
            }
            parse_credential_ref(&storage.credential_ref)?;
        } else if self.upload_format == SnapshotUploadFormat::OverlaybdS3 {
            return Err(DaemonError::Invalid(
                "overlaybd_s3 upload requires S3 storage configuration".into(),
            ));
        }
        if self.registry.is_some() && self.registry_upload.is_some() {
            return Err(DaemonError::Invalid(
                "legacy registry cannot be combined with registry_upload".into(),
            ));
        }
        if self.upload_format == SnapshotUploadFormat::OverlaybdOci
            && self.registry_upload_config().is_none()
        {
            return Err(DaemonError::Invalid(
                "overlaybd_oci upload requires registry_upload (or legacy registry) configuration"
                    .into(),
            ));
        }
        if let Some(registry) = self.registry_upload_config() {
            parse_credential_ref(&registry.credential_ref)?;
            validate_registry_credentials(&registry.username, &registry.password)?;
            if let Some(realm) = &registry.auth_realm {
                crate::snapshot::transport::registry::parse_auth_realm(realm)
                    .map_err(|error| DaemonError::Invalid(error.to_string()))?;
            }
            if registry.tag_prefix.len() > 63
                || registry
                    .tag_prefix
                    .as_bytes()
                    .first()
                    .is_some_and(|byte| !byte.is_ascii_alphanumeric() && *byte != b'_')
                || !registry
                    .tag_prefix
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
            {
                return Err(DaemonError::Invalid(
                    "Registry credentials or tag prefix are invalid".into(),
                ));
            }
            if registry.repository.is_some() && registry.namespace.is_some() {
                return Err(DaemonError::Invalid(
                    "Registry repository override and namespace cannot be combined".into(),
                ));
            }
            if registry
                .namespace
                .as_ref()
                .is_some_and(|namespace| namespace.is_empty() || namespace.contains('/'))
            {
                return Err(DaemonError::Invalid(
                    "Registry namespace must be one nonempty path component".into(),
                ));
            }
            crate::snapshot::metadata::RegistrySnapshotRef {
                registry: registry.registry.clone(),
                // Validate the authority and any configured path without
                // manufacturing a writable destination for automatic routing.
                repository: registry.repository.clone().unwrap_or_else(|| {
                    registry
                        .namespace
                        .clone()
                        .unwrap_or_else(|| "source".into())
                }),
                index_digest: format!("sha256:{}", "0".repeat(64)),
            }
            .validate()
            .map_err(DaemonError::Invalid)?;
        }
        Ok(())
    }
}

fn validate_registry_credentials(
    username: &Option<String>,
    password: &Option<String>,
) -> Result<(), DaemonError> {
    if username.is_some() != password.is_some()
        || username.as_ref().is_some_and(|username| {
            username.is_empty() || username.len() > 4096 || username.contains(':')
        })
        || password
            .as_ref()
            .is_some_and(|password| password.is_empty() || password.len() > 64 * 1024)
    {
        return Err(DaemonError::Invalid(
            "Registry credentials require a bounded username/password pair".into(),
        ));
    }
    Ok(())
}

fn invalid_secret(value: &str) -> bool {
    value.is_empty() || value.chars().any(char::is_whitespace)
}

pub(super) fn parse_credential_ref(value: &str) -> Result<(), DaemonError> {
    let (name, version) = value
        .split_once(':')
        .ok_or_else(|| DaemonError::Invalid("credential_ref must be name:version".into()))?;
    if name.is_empty()
        || version.is_empty()
        || value.matches(':').count() != 1
        || !name
            .bytes()
            .chain(version.bytes())
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
    {
        return Err(DaemonError::Invalid(
            "credential_ref is not canonical name:version".into(),
        ));
    }
    Ok(())
}

pub(super) fn default_kernel_cmdline() -> String {
    "console=ttyS0 reboot=k panic=-1".into()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn storage_only_configs_keep_uploads_disabled_with_100_gib_cache_default() {
        let mut value = json!({"storage": {
            "endpoint_url": "http://minio:9000", "region": "test", "bucket": "snapshots",
            "credential_ref": "test:v1", "access_key_id": "test", "access_key_secret": "test"
        }});
        let config: RustSnapdConfig = serde_json::from_value(value.clone()).unwrap();
        config.validate().unwrap();
        assert_eq!(config.local_cache.max_bytes, 107_374_182_400);
        assert_eq!(config.upload_format, SnapshotUploadFormat::Disabled);
        assert!(config.registry.is_none());
        assert!(config.registry_upload.is_none());
        value["local_cache"] = json!({"max_bytes": 65_536});
        let config: RustSnapdConfig = serde_json::from_value(value.clone()).unwrap();
        assert_eq!(config.local_cache.max_bytes, 65_536);
        value["local_cache"] = json!({"max_bytes": 0});
        let config: RustSnapdConfig = serde_json::from_value(value.clone()).unwrap();
        assert_eq!(config.local_cache.max_bytes, 0);
        value["local_cache"] = json!({"max_byte": 1});
        assert!(serde_json::from_value::<RustSnapdConfig>(value).is_err());
    }

    #[test]
    fn oci_publication_requires_a_valid_registry_and_keeps_s3_configuration() {
        let mut value = json!({"storage": {
            "endpoint_url": "http://minio:9000", "region": "test", "bucket": "snapshots",
            "credential_ref": "test:v1", "access_key_id": "test", "access_key_secret": "test"
        }, "upload_format": "overlaybd_oci"});
        assert!(serde_json::from_value::<RustSnapdConfig>(value.clone())
            .unwrap()
            .validate()
            .is_err());
        value["registry"] = json!({
            "registry": "registry.test:5000", "repository": "snapshots/full",
            "allow_http": true, "credential_ref": "registry:v1",
            "username": "uploader", "password": "private-password"
        });
        let config = serde_json::from_value::<RustSnapdConfig>(value.clone()).unwrap();
        config.validate().unwrap();
        assert_eq!(config.upload_format, SnapshotUploadFormat::OverlaybdOci);
        assert_eq!(config.storage.as_ref().unwrap().bucket, "snapshots");
        assert_eq!(config.registry.as_ref().unwrap().tag_prefix, "snapshot-");
        assert!(!format!("{:?}", config.registry).contains("private-password"));
        for invalid in ["../snapshots", "snapshots//full", "SnapShots"] {
            value["registry"]["repository"] = json!(invalid);
            assert!(serde_json::from_value::<RustSnapdConfig>(value.clone())
                .unwrap()
                .validate()
                .is_err());
        }
        value["registry"]["repository"] = json!("snapshots");
        value["registry"]["password"] = serde_json::Value::Null;
        assert!(serde_json::from_value::<RustSnapdConfig>(value.clone())
            .unwrap()
            .validate()
            .is_err());
        value["upload_format"] = json!("guessed_format");
        assert!(serde_json::from_value::<RustSnapdConfig>(value).is_err());
    }

    #[test]
    fn registry_only_configuration_needs_no_s3_credentials_or_endpoint() {
        let mut value = json!({
            "upload_format": "overlaybd_oci",
            "registry": {"registry": "registry.test:5000", "repository": "snapshots",
                "allow_http": true, "credential_ref": "registry:v1"}
        });
        let config = serde_json::from_value::<RustSnapdConfig>(value.clone()).unwrap();
        config.validate().unwrap();
        assert!(config.storage.is_none());
        for prefix in [".", "-tag", "bad/prefix"] {
            value["registry"]["tag_prefix"] = json!(prefix);
            assert!(serde_json::from_value::<RustSnapdConfig>(value.clone())
                .unwrap()
                .validate()
                .is_err());
        }
        value["registry"]["tag_prefix"] = json!("a".repeat(64));
        assert!(serde_json::from_value::<RustSnapdConfig>(value.clone())
            .unwrap()
            .validate()
            .is_err());
        value["registry"]["tag_prefix"] = json!("snapshot-");
        value["upload_format"] = json!("overlaybd_s3");
        assert!(serde_json::from_value::<RustSnapdConfig>(value)
            .unwrap()
            .validate()
            .is_err());
    }

    fn explicit_registry_upload() -> serde_json::Value {
        json!({
            "registry": "snapshots.test:5000", "repository": "snapshots/full",
            "credential_ref": "push:v1", "allow_http": true,
            "username": "writer", "password": "write-password", "tag_prefix": "capture-"
        })
    }

    #[test]
    fn explicit_registry_upload_preserves_the_legacy_configuration_contract() {
        for field in ["registry_upload", "registry"] {
            let mut value = json!({"upload_format": "overlaybd_oci"});
            value[field] = explicit_registry_upload();
            let config: RustSnapdConfig = serde_json::from_value(value).unwrap();
            config.validate().unwrap();
            assert!(config.storage.is_none());
            let upload = config.registry_upload_config().unwrap();
            assert_eq!(upload.registry, "snapshots.test:5000");
            assert_eq!(upload.repository.as_deref(), Some("snapshots/full"));
            assert_eq!(upload.credential_ref, "push:v1");
            assert_eq!(upload.username.as_deref(), Some("writer"));
            assert_eq!(upload.password.as_deref(), Some("write-password"));
            assert_eq!(upload.tag_prefix, "capture-");
            assert_eq!(upload.tag_mode, RegistrySnapshotTagMode::SourceSnapshot);
            assert!(upload.allow_http);
            assert!(!format!("{config:?}").contains("write-password"));
        }
    }

    #[test]
    fn registry_upload_tag_modes_are_explicit_and_default_to_legacy_source_tags() {
        for field in ["registry_upload", "registry"] {
            let mut value = json!({"upload_format": "overlaybd_oci"});
            value[field] = explicit_registry_upload();
            for (mode, expected) in [
                ("source_snapshot", RegistrySnapshotTagMode::SourceSnapshot),
                ("snapshot_id", RegistrySnapshotTagMode::SnapshotId),
            ] {
                value[field]["tag_mode"] = json!(mode);
                let config: RustSnapdConfig = serde_json::from_value(value.clone()).unwrap();
                config.validate().unwrap();
                assert_eq!(config.registry_upload_config().unwrap().tag_mode, expected);
                assert_eq!(
                    config.registry_upload_config().unwrap().tag_prefix,
                    "capture-"
                );
            }
            for invalid in [
                json!("snapshot"),
                json!("SNAPSHOT_ID"),
                json!(null),
                json!(1),
            ] {
                value[field]["tag_mode"] = invalid;
                assert!(serde_json::from_value::<RustSnapdConfig>(value.clone()).is_err());
            }
        }
    }

    #[test]
    fn explicit_registry_upload_rejects_legacy_alias_conflicts() {
        let value = json!({
            "upload_format": "overlaybd_oci",
            "registry_upload": explicit_registry_upload(),
            "registry": explicit_registry_upload()
        });
        assert!(serde_json::from_value::<RustSnapdConfig>(value)
            .unwrap()
            .validate()
            .is_err());
    }

    #[test]
    fn registry_upload_can_inherit_source_repositories_under_one_namespace() {
        let mut upload = explicit_registry_upload();
        upload.as_object_mut().unwrap().remove("repository");
        let mut value = json!({"upload_format": "overlaybd_oci", "registry_upload": upload});
        let config: RustSnapdConfig = serde_json::from_value(value.clone()).unwrap();
        config.validate().unwrap();
        assert!(config
            .registry_upload_config()
            .unwrap()
            .repository
            .is_none());
        value["registry_upload"]["namespace"] = json!("rock-archive");
        let config: RustSnapdConfig = serde_json::from_value(value.clone()).unwrap();
        config.validate().unwrap();
        assert_eq!(
            config
                .registry_upload_config()
                .unwrap()
                .namespace
                .as_deref(),
            Some("rock-archive")
        );
        for invalid in ["", "../archive", "archive/nested", "Archive"] {
            value["registry_upload"]["namespace"] = json!(invalid);
            assert!(serde_json::from_value::<RustSnapdConfig>(value.clone())
                .unwrap()
                .validate()
                .is_err());
        }
        value["registry_upload"]["namespace"] = json!("rock-archive");
        value["registry_upload"]["repository"] = json!("fixed/snapshots");
        assert!(serde_json::from_value::<RustSnapdConfig>(value)
            .unwrap()
            .validate()
            .is_err());
    }

    #[test]
    fn registry_credentials_require_an_explicit_https_auth_realm_for_cross_origin_tokens() {
        let mut value = json!({"upload_format": "overlaybd_oci", "registry_upload": explicit_registry_upload()});
        let config: RustSnapdConfig = serde_json::from_value(value.clone()).unwrap();
        config.validate().unwrap();
        assert!(config
            .registry_upload_config()
            .unwrap()
            .auth_realm
            .is_none());
        value["registry_upload"]["auth_realm"] =
            json!("https://dockerauth.cn-shanghai.aliyuncs.com/auth");
        let config: RustSnapdConfig = serde_json::from_value(value.clone()).unwrap();
        config.validate().unwrap();
        assert_eq!(
            config
                .registry_upload_config()
                .unwrap()
                .auth_realm
                .as_deref(),
            Some("https://dockerauth.cn-shanghai.aliyuncs.com/auth")
        );
        for invalid in [
            "http://auth.test/token",
            "https://writer:password@auth.test/token",
            "https://auth.test/token?scope=anything",
            "https://auth.test/token#fragment",
            "auth.test/token",
            "",
        ] {
            value["registry_upload"]["auth_realm"] = json!(invalid);
            assert!(serde_json::from_value::<RustSnapdConfig>(value.clone())
                .unwrap()
                .validate()
                .is_err());
        }
    }

    #[test]
    fn registry_configuration_does_not_enable_upload_without_an_explicit_format() {
        for (field, invalid) in [
            ("registry", json!("https://snapshots.test")),
            ("repository", json!("../snapshots")),
            ("credential_ref", json!("unversioned")),
            ("username", json!("invalid:name")),
            ("password", json!(null)),
            ("tag_prefix", json!("bad/prefix")),
        ] {
            let mut upload = explicit_registry_upload();
            upload[field] = invalid;
            let value = json!({"upload_format": "overlaybd_oci", "registry_upload": upload});
            assert!(
                serde_json::from_value::<RustSnapdConfig>(value)
                    .unwrap()
                    .validate()
                    .is_err(),
                "accepted invalid upload field {field}"
            );
        }
        let mut value = json!({"registry_upload": explicit_registry_upload()});
        let config: RustSnapdConfig = serde_json::from_value(value.clone()).unwrap();
        assert_eq!(config.upload_format, SnapshotUploadFormat::Disabled);
        config.validate().unwrap();
        value["storage"] = json!({
            "endpoint_url": "http://minio:9000", "region": "test", "bucket": "snapshots",
            "credential_ref": "s3:v1", "access_key_id": "test", "access_key_secret": "secret"
        });
        let config: RustSnapdConfig = serde_json::from_value(value).unwrap();
        config.validate().unwrap();
        assert_eq!(config.upload_format, SnapshotUploadFormat::Disabled);
    }

    #[test]
    fn empty_or_explicitly_disabled_configuration_needs_no_remote_storage() {
        for value in [json!({}), json!({"upload_format": "disabled"})] {
            let config: RustSnapdConfig = serde_json::from_value(value).unwrap();
            config.validate().unwrap();
            assert_eq!(config.upload_format, SnapshotUploadFormat::Disabled);
            assert!(!config.upload_format.enabled());
            assert!(config.storage.is_none());
            assert!(config.registry_upload_config().is_none());
        }
        for format in ["overlaybd_s3", "overlaybd_oci"] {
            let config: RustSnapdConfig =
                serde_json::from_value(json!({"upload_format": format})).unwrap();
            assert!(config.validate().is_err());
            assert!(config.upload_format.enabled());
        }
        let config: RustSnapdConfig = serde_json::from_value(json!({
            "upload_format": "overlaybd_s3",
            "storage": {
                "endpoint_url": "http://minio:9000", "region": "test", "bucket": "snapshots",
                "credential_ref": "s3:v1", "access_key_id": "test", "access_key_secret": "secret"
            }
        }))
        .unwrap();
        config.validate().unwrap();
        assert_eq!(config.upload_format, SnapshotUploadFormat::OverlaybdS3);
    }
}
