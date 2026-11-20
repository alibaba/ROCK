//! Strict version-one Admin-to-snapd HTTP payloads.
//!
//! These models intentionally mirror `rock.admin.snapd.protocol`.  HTTP
//! Protocol-version, request-identity and deadline headers are transport
//! concerns; this module owns the JSON body and reply contract, validation,
//! and conversions between HTTP payloads and domain-owned operations.

use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashSet};
use std::net::IpAddr;
use thiserror::Error;

use crate::snapshot::image::model as image;
use crate::snapshot::metadata::{LayerKind, SnapshotKind};
use crate::snapshot::repository::PublicationState;
use crate::snapshot::services::background_uploader;
use crate::vm::repository as vm;
use crate::RuntimeIdentity;

pub const SNAPD_PROTOCOL_VERSION: u64 = 1;
pub const MAX_FORK_CHILDREN: usize = 32;
pub const MAX_ADOPTION_RECORDS: usize = 10_000;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum ProtocolError {
    #[error("invalid {label} JSON: {message}")]
    InvalidJson { label: String, message: String },
    #[error("invalid {label}: {message}")]
    InvalidModel { label: String, message: String },
    #[error("cannot encode canonical JSON: {0}")]
    Encode(String),
}

pub trait ValidateProtocol {
    fn validate(&self) -> Result<(), String>;
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SnapdErrorCode {
    Ok,
    UploadDisabled,
    InvalidArgument,
    NotFound,
    IdentityConflict,
    ContentIdentityConflict,
    InvalidState,
    SnapshotInUse,
    ChecksumMismatch,
    StaleRuntimeEpoch,
    CapabilityUnsupported,
    ResourceExhausted,
    DeadlineExceeded,
    Internal,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SnapdReply {
    pub request_id: String,
    pub code: SnapdErrorCode,
    #[serde(default)]
    pub retryable: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error_message: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
}

impl ValidateProtocol for SnapdReply {
    fn validate(&self) -> Result<(), String> {
        require_nonempty("request_id", &self.request_id, 4096)?;
        if self.code == SnapdErrorCode::Ok && self.error_message.is_some() {
            return Err("successful reply must not include error_message".into());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InitializeRuntimeRequest {}

impl ValidateProtocol for InitializeRuntimeRequest {
    fn validate(&self) -> Result<(), String> {
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeAdoptionStatusResult {
    pub daemon_epoch: String,
    pub pending: bool,
    pub pending_fingerprint: String,
    #[serde(default)]
    pub records: Vec<Value>,
}

impl ValidateProtocol for RuntimeAdoptionStatusResult {
    fn validate(&self) -> Result<(), String> {
        require_nonempty("daemon_epoch", &self.daemon_epoch, 256)?;
        validate_bare_sha256(&self.pending_fingerprint)?;
        if self.records.len() > MAX_ADOPTION_RECORDS {
            return Err(format!("records exceeds {MAX_ADOPTION_RECORDS}"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PromotedLineageSpec {
    pub snapshot_id: String,
    pub spec_sha256: String,
    pub capture_correlation_id: String,
    pub capture_request_sha256: String,
    pub spec_value: Value,
}

impl ValidateProtocol for PromotedLineageSpec {
    fn validate(&self) -> Result<(), String> {
        require_nonempty("promoted snapshot ID", &self.snapshot_id, 256)?;
        validate_bare_sha256(&self.spec_sha256)?;
        require_nonempty(
            "promoted capture correlation ID",
            &self.capture_correlation_id,
            256,
        )?;
        validate_bare_sha256(&self.capture_request_sha256)?;
        require_object("promoted snapshot spec", &self.spec_value)
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AdoptionDesiredRuntime {
    pub vm_id: String,
    pub spec_value: Value,
    pub spec_checksum: String,
    pub resource_spec: Value,
    #[serde(default)]
    pub promoted_lineage_specs: Vec<PromotedLineageSpec>,
}

impl ValidateProtocol for AdoptionDesiredRuntime {
    fn validate(&self) -> Result<(), String> {
        validate_identity("adoption VM ID", &self.vm_id, 256)?;
        require_object("snapshot spec", &self.spec_value)?;
        validate_prefixed_sha256(&self.spec_checksum)?;
        if crate::snapshot::metadata::snapshot_spec_checksum(&self.spec_value)
            .map_err(|error| error.to_string())?
            != self.spec_checksum
        {
            return Err("snapshot spec checksum differs from its canonical value".into());
        }
        require_object("resource spec", &self.resource_spec)?;
        if self.promoted_lineage_specs.len() > 64 {
            return Err("promoted lineage exceeds 64 generations".into());
        }
        let mut snapshots = HashSet::new();
        for promoted in &self.promoted_lineage_specs {
            promoted.validate()?;
            if crate::snapshot::metadata::snapshot_spec_checksum(&promoted.spec_value)
                .map_err(|error| error.to_string())?
                .trim_start_matches("sha256:")
                != promoted.spec_sha256
            {
                return Err(
                    "promoted snapshot spec checksum differs from its canonical value".into(),
                );
            }
            if !snapshots.insert(&promoted.snapshot_id) {
                return Err("promoted lineage contains duplicate snapshot IDs".into());
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReconcileRuntimeAdoptionsRequest {
    pub reconciliation_id: String,
    pub complete: bool,
    #[serde(default)]
    pub desired: Vec<AdoptionDesiredRuntime>,
}

impl ReconcileRuntimeAdoptionsRequest {
    pub fn fingerprint(&self) -> Result<String, String> {
        let value = serde_json::to_value(&self.desired).map_err(|error| error.to_string())?;
        let payload = serde_json::to_vec(&value).map_err(|error| error.to_string())?;
        Ok(format!("{:x}", Sha256::digest(payload)))
    }
}

impl ValidateProtocol for ReconcileRuntimeAdoptionsRequest {
    fn validate(&self) -> Result<(), String> {
        if !self.complete {
            return Err("controlled-adoption reconciliation must be a complete batch".into());
        }
        if self.desired.len() > MAX_ADOPTION_RECORDS {
            return Err(format!("desired exceeds {MAX_ADOPTION_RECORDS}"));
        }
        let mut vm_ids = HashSet::new();
        for desired in &self.desired {
            desired.validate()?;
            if !vm_ids.insert(&desired.vm_id) {
                return Err("desired adoption batch contains duplicate VM IDs".into());
            }
        }
        let expected = format!("adoption-reconcile-{}", self.fingerprint()?);
        if self.reconciliation_id != expected {
            return Err("adoption reconciliation ID differs from its canonical batch".into());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CreateBaseImageRequest {
    pub snapshot_id: String,
    pub oci_ref: String,
    pub oci_digest: String,
    #[serde(default = "default_platform")]
    pub platform: String,
    pub base_image_builder_version: String,
    pub base_image_build_key: String,
}

impl ValidateProtocol for CreateBaseImageRequest {
    fn validate(&self) -> Result<(), String> {
        require_nonempty("snapshot_id", &self.snapshot_id, 4096)?;
        require_nonempty("oci_ref", &self.oci_ref, 4096)?;
        validate_prefixed_sha256(&self.oci_digest)?;
        require_nonempty("platform", &self.platform, 256)?;
        require_nonempty(
            "base_image_builder_version",
            &self.base_image_builder_version,
            256,
        )?;
        validate_prefixed_sha256(&self.base_image_build_key)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResolveBaseImageRequest {
    pub oci_ref: String,
    #[serde(default = "default_platform")]
    pub platform: String,
}

/// A complete OCI snapshot reference, never a disk/base-image conversion input.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ImportOciSnapshotRequest {
    pub oci_ref: String,
}

impl ValidateProtocol for ImportOciSnapshotRequest {
    fn validate(&self) -> Result<(), String> {
        require_nonempty("oci_ref", &self.oci_ref, 4096)?;
        crate::snapshot::transport::registry::RegistryReference::parse(&self.oci_ref)
            .map_err(|error| error.to_string())?;
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ImportOciSnapshotResult {
    pub snapshot_id: String,
    pub state: SnapshotState,
    pub snapshot_kind: SnapshotKind,
    pub oci_ref: String,
    pub snapshot_spec: Value,
    pub spec_checksum: String,
    pub memory_bytes: u64,
    pub rootfs_bytes: u64,
}

impl TryFrom<background_uploader::SnapshotStatus> for ImportOciSnapshotResult {
    type Error = String;

    fn try_from(value: background_uploader::SnapshotStatus) -> Result<Self, Self::Error> {
        if value.state != PublicationState::RemoteReady {
            return Err("imported OCI snapshot is not REMOTE_READY".into());
        }
        let snapshot_spec = value
            .snapshot_spec
            .ok_or("imported OCI snapshot has no spec")?;
        let spec: crate::snapshot::metadata::SnapshotSpec =
            serde_json::from_value(snapshot_spec.clone()).map_err(|error| error.to_string())?;
        crate::snapshot::ValidateCanonicalModel::validate(&spec)?;
        let reference = spec
            .registry_snapshot
            .as_ref()
            .ok_or("OCI snapshot has no immutable index")?;
        let memory = spec
            .memory
            .as_ref()
            .ok_or("OCI resume requires FULL memory")?;
        if spec.vm_state.is_none() {
            return Err("OCI resume requires FULL VMM state".into());
        }
        let spec_checksum = crate::snapshot::metadata::snapshot_spec_checksum(&snapshot_spec)
            .map_err(|error| error.to_string())?;
        if value.spec_checksum.as_deref() != Some(spec_checksum.as_str()) {
            return Err("imported OCI snapshot checksum differs".into());
        }
        Ok(Self {
            snapshot_id: value.snapshot_id,
            state: SnapshotState::RemoteReady,
            snapshot_kind: SnapshotKind::Full,
            oci_ref: format!(
                "{}/{}@{}",
                reference.registry, reference.repository, reference.index_digest
            ),
            memory_bytes: memory.logical_bytes,
            rootfs_bytes: spec.rootfs.logical_bytes,
            snapshot_spec,
            spec_checksum,
        })
    }
}

impl ValidateProtocol for ResolveBaseImageRequest {
    fn validate(&self) -> Result<(), String> {
        require_nonempty("oci_ref", &self.oci_ref, 4096)?;
        require_nonempty("platform", &self.platform, 256)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResolvedBaseImageResult {
    pub oci_ref: String,
    pub oci_digest: String,
    pub oci_platform: String,
    pub base_image_builder_version: String,
    pub base_image_build_key: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SnapshotSandboxRequest {
    pub snapshot_id: String,
}

impl ValidateProtocol for SnapshotSandboxRequest {
    fn validate(&self) -> Result<(), String> {
        require_nonempty("snapshot_id", &self.snapshot_id, 4096)?;
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeLaunchPlan {
    pub sandbox_id: String,

    #[serde(default = "empty_object")]
    pub resource_spec: Value,
}

impl ValidateProtocol for RuntimeLaunchPlan {
    fn validate(&self) -> Result<(), String> {
        require_nonempty("sandbox_id", &self.sandbox_id, 4096)?;
        require_object("resource_spec", &self.resource_spec)
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ForkSandboxRequest {
    #[serde(flatten)]
    pub snapshot: SnapshotSandboxRequest,
    pub children: Vec<RuntimeLaunchPlan>,
}

impl ValidateProtocol for ForkSandboxRequest {
    fn validate(&self) -> Result<(), String> {
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

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StartSandboxFromSnapshotRequest {
    pub sandbox_id: String,
    pub snapshot_spec: Value,
    pub spec_checksum: String,
    #[serde(default = "empty_object")]
    pub resource_spec: Value,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub p2p_source_worker_ip: Option<IpAddr>,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub local_snapshot: bool,
}

impl StartSandboxFromSnapshotRequest {
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
}

impl ValidateProtocol for StartSandboxFromSnapshotRequest {
    fn validate(&self) -> Result<(), String> {
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

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LayerUploadStatus {
    pub upload_state: UploadState,
    #[serde(default)]
    pub remote_publishing: bool,
    #[serde(default)]
    pub attempt_count: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_error: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub retry_at_unix_ms: Option<u64>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum UploadState {
    LocalReady,
    RemoteReady,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum UploadLayerKind {
    Memory,
    Block,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SnapshotState {
    Creating,
    LocalReady,
    RemoteReady,
    Failed,
}

impl From<crate::snapshot::repository::PublicationState> for SnapshotState {
    fn from(state: crate::snapshot::repository::PublicationState) -> Self {
        match state {
            crate::snapshot::repository::PublicationState::Creating => Self::Creating,
            crate::snapshot::repository::PublicationState::LocalReady => Self::LocalReady,
            crate::snapshot::repository::PublicationState::RemoteReady => Self::RemoteReady,
            crate::snapshot::repository::PublicationState::Failed => Self::Failed,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SnapshotStatusResult {
    pub snapshot_id: String,
    pub state: SnapshotState,
    #[serde(default)]
    pub remote_publishing: bool,
    pub progress: f64,
    #[serde(default)]
    pub attempt_count: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_error: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub failure_code: Option<String>,
    #[serde(default)]
    pub retryable: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub retry_at_unix_ms: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub snapshot_spec: Option<Value>,
    /// Resource summaries for Admin; the snapshot descriptor remains opaque there.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub memory_bytes: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rootfs_bytes: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub spec_checksum: Option<String>,
    #[serde(default)]
    pub admin_finalized: bool,
    #[serde(default)]
    pub catalog_pin_held: bool,
    #[serde(default)]
    pub layers: BTreeMap<UploadLayerKind, LayerUploadStatus>,
}

impl ValidateProtocol for SnapshotStatusResult {
    fn validate(&self) -> Result<(), String> {
        require_nonempty("snapshot_id", &self.snapshot_id, 4096)?;
        if !(0.0..=1.0).contains(&self.progress) || !self.progress.is_finite() {
            return Err("progress must be finite and within 0..=1".into());
        }
        if let Some(checksum) = &self.spec_checksum {
            validate_prefixed_sha256(checksum)?;
        }
        if let Some(spec) = &self.snapshot_spec {
            require_object("snapshot_spec", spec)?;
        }
        if self.memory_bytes == Some(0) || self.rootfs_bytes == Some(0) {
            return Err("snapshot sizes must be positive".into());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SandboxState {
    Starting,
    Running,
    Stopped,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SandboxStatusResult {
    pub sandbox_id: String,
    pub state: SandboxState,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_error: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub result_code: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub guest_endpoint: Option<GuestEndpointResult>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub vmm_pid: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rocklet_artifact: Option<RockletArtifactResult>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StopSandboxRequest {
    pub reason: String,
    pub deadline_unix_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PluggerControlRequest {
    pub action: String,
    pub payload: Value,
}

impl ValidateProtocol for PluggerControlRequest {
    fn validate(&self) -> Result<(), String> {
        if !matches!(self.action.as_str(), "configure" | "state" | "health") {
            return Err("unknown snapd-plugger action".into());
        }
        require_object("plugger payload", &self.payload)?;
        if serde_json::to_vec(&self.payload)
            .map_err(|error| error.to_string())?
            .len()
            > 64 * 1024
        {
            return Err("plugger payload exceeds 64 KiB".into());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PluggerControlResult {
    pub status: u16,
    pub body: Value,
}

impl From<crate::vm::runtime::plugger::PluggerReply> for PluggerControlResult {
    fn from(reply: crate::vm::runtime::plugger::PluggerReply) -> Self {
        Self {
            status: reply.status,
            body: reply.body,
        }
    }
}

impl ValidateProtocol for StopSandboxRequest {
    fn validate(&self) -> Result<(), String> {
        require_nonempty("reason", &self.reason, 4096)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DeleteSandboxRequest {}

impl ValidateProtocol for DeleteSandboxRequest {
    fn validate(&self) -> Result<(), String> {
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FinalizePublishedSnapshotRequest {
    pub spec_checksum: String,
}

impl ValidateProtocol for FinalizePublishedSnapshotRequest {
    fn validate(&self) -> Result<(), String> {
        validate_prefixed_sha256(&self.spec_checksum)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SnapshotFenceRequest {
    pub fence_nonce: String,
    pub ttl_ms: u64,
    pub expected_runtimes: Vec<RuntimeIdentity>,
}

impl ValidateProtocol for SnapshotFenceRequest {
    fn validate(&self) -> Result<(), String> {
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

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AbortDeleteFenceRequest {
    pub fence_nonce: String,
}

impl ValidateProtocol for AbortDeleteFenceRequest {
    fn validate(&self) -> Result<(), String> {
        require_nonempty("fence_nonce", &self.fence_nonce, 4096)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InvalidateSnapshotRequest {
    pub fence_nonce: String,
}

impl ValidateProtocol for InvalidateSnapshotRequest {
    fn validate(&self) -> Result<(), String> {
        require_nonempty("fence_nonce", &self.fence_nonce, 4096)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MutationAcceptedResult {
    pub identity: String,
    pub state: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GuestEndpointResult {
    pub transport: String,
    pub host_port: u16,
    pub guest_port: u32,
    pub vsock_cid: u32,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RockletArtifactResult {
    pub identity: String,
    pub version: String,
    pub sha256: String,
    pub logical_bytes: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SandboxStartedResult {
    pub identity: String,
    pub state: String,
    pub guest_endpoint: GuestEndpointResult,
    pub vmm_pid: u32,
    pub rocklet_artifact: RockletArtifactResult,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FinalizePublishedSnapshotResult {
    pub identity: String,
    pub state: SnapshotState,
    pub released_pins: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InvalidateSnapshotResult {
    pub identity: String,
    pub state: String,
    pub invalidated_aliases: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SnapshotLocalReadyResult {
    pub snapshot_id: String,
    pub state: LocalReadyState,
    #[serde(default)]
    pub layer_identities: Vec<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum LocalReadyState {
    LocalReady,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ForkChildResult {
    pub sandbox_id: String,
    pub state: ForkChildState,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub guest_endpoint: Option<GuestEndpointResult>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub vmm_pid: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rocklet_artifact: Option<RockletArtifactResult>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ForkChildState {
    Running,
    Pending,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ForkLocalReadyResult {
    #[serde(flatten)]
    pub snapshot: SnapshotLocalReadyResult,
    pub children: Vec<ForkChildResult>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SnapshotUsageCheckResult {
    pub snapshot_id: String,
    pub state: SnapshotUsageState,
    #[serde(default)]
    pub active_runtimes: Vec<RuntimeIdentity>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SnapshotUsageState {
    ActiveRuntimes,
    FencedNoRuntime,
}

pub fn parse_strict<T>(label: &str, bytes: &[u8]) -> Result<T, ProtocolError>
where
    T: DeserializeOwned + ValidateProtocol,
{
    let parsed: T = serde_json::from_slice(bytes).map_err(|error| ProtocolError::InvalidJson {
        label: label.into(),
        message: error.to_string(),
    })?;
    parsed
        .validate()
        .map_err(|message| ProtocolError::InvalidModel {
            label: label.into(),
            message,
        })?;
    Ok(parsed)
}

pub fn canonical_json<T: Serialize>(value: &T) -> Result<Vec<u8>, ProtocolError> {
    let value =
        serde_json::to_value(value).map_err(|error| ProtocolError::Encode(error.to_string()))?;
    let mut encoded =
        serde_json::to_vec(&value).map_err(|error| ProtocolError::Encode(error.to_string()))?;
    encoded.push(b'\n');
    Ok(encoded)
}

fn default_platform() -> String {
    "linux/amd64".into()
}

fn empty_object() -> Value {
    Value::Object(Default::default())
}

fn require_object(label: &str, value: &Value) -> Result<(), String> {
    if value.is_object() {
        Ok(())
    } else {
        Err(format!("{label} must be a JSON object"))
    }
}

fn require_nonempty(label: &str, value: &str, max: usize) -> Result<(), String> {
    if value.is_empty() || value.len() > max {
        Err(format!("{label} length must be 1..={max}"))
    } else {
        Ok(())
    }
}

fn validate_identity(label: &str, value: &str, max: usize) -> Result<(), String> {
    require_nonempty(label, value, max)?;
    if value.bytes().enumerate().all(|(index, byte)| {
        byte.is_ascii_alphanumeric() || (index > 0 && matches!(byte, b'.' | b'_' | b'-'))
    }) {
        Ok(())
    } else {
        Err(format!("{label} contains invalid characters"))
    }
}

fn validate_prefixed_sha256(value: &str) -> Result<(), String> {
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

// Convert HTTP payloads to domain values and domain results to HTTP replies.
impl From<CreateBaseImageRequest> for image::BuildImage {
    fn from(value: CreateBaseImageRequest) -> Self {
        Self {
            snapshot_id: value.snapshot_id,
            oci_ref: value.oci_ref,
            oci_digest: value.oci_digest,
            platform: value.platform,
            base_image_builder_version: value.base_image_builder_version,
            base_image_build_key: value.base_image_build_key,
        }
    }
}

impl From<ResolveBaseImageRequest> for image::ResolveImage {
    fn from(value: ResolveBaseImageRequest) -> Self {
        Self {
            oci_ref: value.oci_ref,
            platform: value.platform,
        }
    }
}

impl From<SnapshotSandboxRequest> for vm::CaptureSnapshot {
    fn from(value: SnapshotSandboxRequest) -> Self {
        Self {
            snapshot_id: value.snapshot_id,
        }
    }
}

impl From<SnapshotSandboxRequest> for background_uploader::SnapshotPublication {
    fn from(value: SnapshotSandboxRequest) -> Self {
        Self {
            snapshot_id: value.snapshot_id,
        }
    }
}

impl From<RuntimeLaunchPlan> for vm::RuntimeLaunchPlan {
    fn from(value: RuntimeLaunchPlan) -> Self {
        Self {
            sandbox_id: value.sandbox_id,
            resource_spec: value.resource_spec,
        }
    }
}

impl From<ForkSandboxRequest> for vm::ForkSnapshot {
    fn from(value: ForkSandboxRequest) -> Self {
        Self {
            snapshot: value.snapshot.into(),
            children: value.children.into_iter().map(Into::into).collect(),
        }
    }
}

impl From<StartSandboxFromSnapshotRequest> for vm::StartFromSnapshot {
    fn from(value: StartSandboxFromSnapshotRequest) -> Self {
        Self {
            sandbox_id: value.sandbox_id,
            snapshot_spec: value.snapshot_spec,
            spec_checksum: value.spec_checksum,
            resource_spec: value.resource_spec,
            p2p_source_worker_ip: value.p2p_source_worker_ip,
            local_snapshot: value.local_snapshot,
        }
    }
}

impl From<&StopSandboxRequest> for vm::StopSandbox {
    fn from(value: &StopSandboxRequest) -> Self {
        Self {
            reason: value.reason.clone(),
            deadline_unix_ms: value.deadline_unix_ms,
        }
    }
}

impl From<&FinalizePublishedSnapshotRequest> for vm::FinalizeSnapshot {
    fn from(value: &FinalizePublishedSnapshotRequest) -> Self {
        Self {
            spec_checksum: value.spec_checksum.clone(),
        }
    }
}

impl From<&SnapshotFenceRequest> for vm::SnapshotFence {
    fn from(value: &SnapshotFenceRequest) -> Self {
        Self {
            fence_nonce: value.fence_nonce.clone(),
            ttl_ms: value.ttl_ms,
            expected_runtimes: value.expected_runtimes.clone(),
        }
    }
}

impl From<&AbortDeleteFenceRequest> for vm::AbortDeleteFence {
    fn from(value: &AbortDeleteFenceRequest) -> Self {
        Self {
            fence_nonce: value.fence_nonce.clone(),
        }
    }
}

impl From<&InvalidateSnapshotRequest> for vm::InvalidateSnapshot {
    fn from(value: &InvalidateSnapshotRequest) -> Self {
        Self {
            fence_nonce: value.fence_nonce.clone(),
        }
    }
}

impl From<image::ResolvedImage> for ResolvedBaseImageResult {
    fn from(value: image::ResolvedImage) -> Self {
        Self {
            oci_ref: value.oci_ref,
            oci_digest: value.oci_digest,
            oci_platform: value.oci_platform,
            base_image_builder_version: value.base_image_builder_version,
            base_image_build_key: value.base_image_build_key,
        }
    }
}

impl From<image::ImageBuildAccepted> for MutationAcceptedResult {
    fn from(value: image::ImageBuildAccepted) -> Self {
        Self {
            identity: value.identity,
            state: match value.state {
                PublicationState::Creating => "CREATING",
                PublicationState::LocalReady => "LOCAL_READY",
                PublicationState::RemoteReady => "REMOTE_READY",
                PublicationState::Failed => "FAILED",
            }
            .into(),
        }
    }
}

impl From<vm::OperationAccepted> for MutationAcceptedResult {
    fn from(value: vm::OperationAccepted) -> Self {
        Self {
            identity: value.identity,
            state: value.state,
        }
    }
}

impl From<vm::GuestEndpoint> for GuestEndpointResult {
    fn from(value: vm::GuestEndpoint) -> Self {
        Self {
            transport: value.transport,
            host_port: value.host_port,
            guest_port: value.guest_port,
            vsock_cid: value.vsock_cid,
        }
    }
}

impl From<vm::RockletArtifact> for RockletArtifactResult {
    fn from(value: vm::RockletArtifact) -> Self {
        Self {
            identity: value.identity,
            version: value.version,
            sha256: value.sha256,
            logical_bytes: value.logical_bytes,
        }
    }
}

impl From<vm::SandboxStarted> for SandboxStartedResult {
    fn from(value: vm::SandboxStarted) -> Self {
        Self {
            identity: value.identity,
            state: value.state,
            guest_endpoint: value.guest_endpoint.into(),
            vmm_pid: value.vmm_pid,
            rocklet_artifact: value.rocklet_artifact.into(),
        }
    }
}

impl From<vm::SandboxStatus> for SandboxStatusResult {
    fn from(value: vm::SandboxStatus) -> Self {
        Self {
            sandbox_id: value.sandbox_id,
            state: value.state.into(),
            last_error: value.last_error,
            result_code: value.result_code,
            guest_endpoint: value.guest_endpoint.map(Into::into),
            vmm_pid: value.vmm_pid,
            rocklet_artifact: value.rocklet_artifact.map(Into::into),
        }
    }
}

impl From<vm::SnapshotCaptured> for SnapshotLocalReadyResult {
    fn from(value: vm::SnapshotCaptured) -> Self {
        Self {
            snapshot_id: value.snapshot_id,
            state: match value.state {
                vm::CaptureState::LocalReady => LocalReadyState::LocalReady,
            },
            layer_identities: value.layer_identities,
        }
    }
}

impl From<vm::ForkChild> for ForkChildResult {
    fn from(value: vm::ForkChild) -> Self {
        Self {
            sandbox_id: value.sandbox_id,
            state: value.state.into(),
            error: value.error,
            guest_endpoint: value.guest_endpoint.map(Into::into),
            vmm_pid: value.vmm_pid,
            rocklet_artifact: value.rocklet_artifact.map(Into::into),
        }
    }
}

impl From<vm::ForkCaptured> for ForkLocalReadyResult {
    fn from(value: vm::ForkCaptured) -> Self {
        Self {
            snapshot: value.snapshot.into(),
            children: value.children.into_iter().map(Into::into).collect(),
        }
    }
}

impl From<vm::SnapshotUsage> for SnapshotUsageCheckResult {
    fn from(value: vm::SnapshotUsage) -> Self {
        Self {
            snapshot_id: value.snapshot_id,
            state: value.state.into(),
            active_runtimes: value.active_runtimes,
        }
    }
}

impl From<vm::SnapshotInvalidated> for InvalidateSnapshotResult {
    fn from(value: vm::SnapshotInvalidated) -> Self {
        Self {
            identity: value.identity,
            state: value.state,
            invalidated_aliases: value.invalidated_aliases,
        }
    }
}

impl From<background_uploader::PublicationFinalized> for FinalizePublishedSnapshotResult {
    fn from(value: background_uploader::PublicationFinalized) -> Self {
        Self {
            identity: value.identity,
            state: value.state.into(),
            released_pins: value.released_pins,
        }
    }
}

impl From<background_uploader::LayerUploadStatus> for LayerUploadStatus {
    fn from(value: background_uploader::LayerUploadStatus) -> Self {
        Self {
            upload_state: value.upload_state.into(),
            remote_publishing: value.remote_publishing,
            attempt_count: value.attempt_count,
            last_error: value.last_error,
            retry_at_unix_ms: value.retry_at_unix_ms,
        }
    }
}

impl From<background_uploader::SnapshotStatus> for SnapshotStatusResult {
    fn from(value: background_uploader::SnapshotStatus) -> Self {
        Self {
            snapshot_id: value.snapshot_id,
            state: value.state.into(),
            remote_publishing: value.remote_publishing,
            progress: value.progress,
            attempt_count: value.attempt_count,
            last_error: value.last_error,
            failure_code: value.failure_code,
            retryable: value.retryable,
            retry_at_unix_ms: value.retry_at_unix_ms,
            snapshot_spec: value.snapshot_spec,
            memory_bytes: value.memory_bytes,
            rootfs_bytes: value.rootfs_bytes,
            spec_checksum: value.spec_checksum,
            admin_finalized: value.admin_finalized,
            catalog_pin_held: value.catalog_pin_held,
            layers: value
                .layers
                .into_iter()
                .map(|(kind, status)| (kind.into(), status.into()))
                .collect(),
        }
    }
}

impl From<vm::SandboxState> for SandboxState {
    fn from(value: vm::SandboxState) -> Self {
        match value {
            vm::SandboxState::Starting => Self::Starting,
            vm::SandboxState::Running => Self::Running,
            vm::SandboxState::Stopped => Self::Stopped,
            vm::SandboxState::Failed => Self::Failed,
        }
    }
}

impl From<vm::ForkChildState> for ForkChildState {
    fn from(value: vm::ForkChildState) -> Self {
        match value {
            vm::ForkChildState::Running => Self::Running,
            vm::ForkChildState::Pending => Self::Pending,
            vm::ForkChildState::Failed => Self::Failed,
        }
    }
}

impl From<vm::SnapshotUsageState> for SnapshotUsageState {
    fn from(value: vm::SnapshotUsageState) -> Self {
        match value {
            vm::SnapshotUsageState::ActiveRuntimes => Self::ActiveRuntimes,
            vm::SnapshotUsageState::FencedNoRuntime => Self::FencedNoRuntime,
        }
    }
}

impl From<background_uploader::LayerUploadState> for UploadState {
    fn from(value: background_uploader::LayerUploadState) -> Self {
        match value {
            background_uploader::LayerUploadState::LocalReady => Self::LocalReady,
            background_uploader::LayerUploadState::RemoteReady => Self::RemoteReady,
        }
    }
}

impl From<LayerKind> for UploadLayerKind {
    fn from(value: LayerKind) -> Self {
        match value {
            LayerKind::Memory => Self::Memory,
            LayerKind::Block => Self::Block,
        }
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use serde::de::DeserializeOwned;
    use serde_json::{json, Value};

    use super::ValidateProtocol;
    use crate::endpoints::control::protocol as wire;
    use crate::snapshot::image::model as image;
    use crate::snapshot::metadata::{canonical_json_bytes, sha256_identity, LayerKind};
    use crate::snapshot::repository::PublicationState;
    use crate::snapshot::services::background_uploader;
    use crate::vm::repository as vm;
    use crate::RuntimeIdentity;

    #[test]
    fn oci_snapshot_import_requires_a_reference_and_rejects_inline_credentials() {
        for reference in [
            "registry.example/team/image:checkpoint".to_owned(),
            format!("registry.example/team/image@{}", digest('a')),
        ] {
            let request: wire::ImportOciSnapshotRequest = decode(json!({"oci_ref":reference}));
            request.validate().unwrap();
        }
        for value in [
            json!({"oci_ref":"image"}),
            json!({"oci_ref":"https://user:password@registry.example/team/image:tag"}),
            json!({"oci_ref":"registry.example/team/image:tag", "password":"do-not-accept"}),
        ] {
            assert!(wire::parse_strict::<wire::ImportOciSnapshotRequest>(
                "import",
                &serde_json::to_vec(&value).unwrap()
            )
            .is_err());
        }
    }

    #[test]
    fn plugger_control_rejects_unknown_or_unbounded_requests() {
        for action in ["configure", "state", "health"] {
            let request: wire::PluggerControlRequest =
                decode(json!({"action": action, "payload": {}}));
            assert_eq!(request.action, action);
        }
        for value in [
            json!({"action": "exec", "payload": {}}),
            json!({"action": "plug", "payload": {}}),
            json!({"action": "unplug", "payload": {}}),
            json!({"action": "configure", "payload": []}),
            json!({"action": "configure", "payload": {"owner": "x".repeat(64 * 1024)}}),
            json!({"action": "configure", "payload": {}, "unknown": true}),
        ] {
            assert!(wire::parse_strict::<wire::PluggerControlRequest>(
                "plugger fixture",
                &serde_json::to_vec(&value).unwrap(),
            )
            .is_err());
        }
    }

    fn digest(character: char) -> String {
        format!("sha256:{}", character.to_string().repeat(64))
    }

    fn decode<T: DeserializeOwned + ValidateProtocol>(value: Value) -> T {
        wire::parse_strict("boundary fixture", &serde_json::to_vec(&value).unwrap()).unwrap()
    }

    fn image_request_json() -> Value {
        json!({
            "snapshot_id": "base-01",
            "oci_ref": "registry.example/repo:tag",
            "oci_digest": digest('a'),
            "base_image_builder_version": "builder-v1",
            "base_image_build_key": digest('b')
        })
    }

    fn endpoint() -> vm::GuestEndpoint {
        vm::GuestEndpoint {
            transport: "vsock-tcp-forward".into(),
            host_port: 40300,
            guest_port: 4400,
            vsock_cid: 71,
        }
    }

    fn artifact() -> vm::RockletArtifact {
        vm::RockletArtifact {
            identity: "rocklet-fixture".into(),
            version: "v1".into(),
            sha256: digest('a'),
            logical_bytes: 67108864,
        }
    }

    fn endpoint_json() -> Value {
        json!({
            "transport": "vsock-tcp-forward",
            "host_port": 40300,
            "guest_port": 4400,
            "vsock_cid": 71
        })
    }

    fn artifact_json() -> Value {
        json!({
            "identity": "rocklet-fixture",
            "version": "v1",
            "sha256": digest('a'),
            "logical_bytes": 67108864
        })
    }

    #[test]
    fn http_defaults_and_strict_decoding_survive_business_conversion() {
        let request: wire::CreateBaseImageRequest = decode(image_request_json());
        let command: image::BuildImage = request.into();
        command.validate().unwrap();
        assert_eq!(command.platform, "linux/amd64");

        let request: wire::ResolveBaseImageRequest = decode(json!({"oci_ref": "repo:tag"}));
        let query: image::ResolveImage = request.into();
        assert_eq!(query.oci_ref, "repo:tag");
        assert_eq!(query.platform, "linux/amd64");

        let request: wire::ForkSandboxRequest = decode(json!({
            "snapshot_id": "snap-01", "children": [{"sandbox_id": "child-a"}]
        }));
        let command: vm::ForkSnapshot = request.into();
        command.validate().unwrap();
        assert_eq!(command.children[0].resource_spec, json!({}));

        let mut unknown = image_request_json();
        unknown["unexpected"] = true.into();
        assert!(wire::parse_strict::<wire::CreateBaseImageRequest>(
            "unknown field",
            &serde_json::to_vec(&unknown).unwrap()
        )
        .is_err());
        assert!(wire::parse_strict::<wire::StartSandboxFromSnapshotRequest>(
            "invalid resources",
            &serde_json::to_vec(&json!({
                "sandbox_id": "child-a", "snapshot_spec": {},
                "spec_checksum": digest('c'), "resource_spec": []
            }))
            .unwrap()
        )
        .is_err());
    }

    #[test]
    fn image_build_identity_and_snapshot_commands_preserve_inputs() {
        let request: wire::CreateBaseImageRequest = decode(image_request_json());
        let command: image::BuildImage = request.clone().into();
        let bytes = canonical_json_bytes(&command).unwrap();
        assert_eq!(bytes, wire::canonical_json(&request).unwrap());
        assert_eq!(
            sha256_identity(&bytes),
            "sha256:b4dc4e3721bc0cb82d54b419ba6b59073d931652a5a6707f7d397d11f653f874"
        );

        let request: wire::SnapshotSandboxRequest = decode(json!({"snapshot_id": "snap-01"}));
        let capture: vm::CaptureSnapshot = request.clone().into();
        let publication: background_uploader::SnapshotPublication = request.clone().into();
        let expected = b"{\"snapshot_id\":\"snap-01\"}\n";
        assert_eq!(canonical_json_bytes(&capture).unwrap(), expected);
        assert_eq!(canonical_json_bytes(&publication).unwrap(), expected);

        let request: wire::ForkSandboxRequest = decode(json!({
            "snapshot_id": "snap-01",
            "children": [
                {"sandbox_id": "child-a", "resource_spec": {"cpu": 1, "memory": 1024}},
                {"sandbox_id": "child-b"}
            ]
        }));
        let command: vm::ForkSnapshot = request.clone().into();
        assert_eq!(
            serde_json::to_value(&command).unwrap(),
            json!({
                "snapshot_id": "snap-01",
                "children": [
                    {"sandbox_id": "child-a", "resource_spec": {"cpu": 1, "memory": 1024}},
                    {"sandbox_id": "child-b", "resource_spec": {}}
                ]
            })
        );
    }

    #[test]
    fn start_commands_preserve_optional_source_and_local_fields() {
        for (local, peer) in [(false, None), (false, Some("192.0.2.44")), (true, None)] {
            let mut value = json!({
                "sandbox_id": "child-start",
                "snapshot_spec": if local { json!({}) } else { json!({"snapshot_id": "base-01"}) },
                "spec_checksum": digest('c')
            });
            if local {
                value["local_snapshot"] = true.into();
            }
            if let Some(peer) = peer {
                value["p2p_source_worker_ip"] = peer.into();
            }
            let request: wire::StartSandboxFromSnapshotRequest = decode(value);
            let command: vm::StartFromSnapshot = request.clone().into();
            command.validate().unwrap();
            assert_eq!(command.resource_spec, json!({}));
            assert_eq!(
                serde_json::to_value(&command).unwrap(),
                serde_json::to_value(&request).unwrap()
            );
            assert_eq!(command.p2p_source("base-01").is_some(), peer.is_some());

            let encoded = serde_json::to_value(command).unwrap();
            assert_eq!(encoded.get("local_snapshot").is_some(), local);
            assert_eq!(
                encoded.get("p2p_source_worker_ip").is_some(),
                peer.is_some()
            );
        }
    }

    #[test]
    fn snapshot_status_projection_preserves_all_states_and_publication_fields() {
        for (state, label) in [
            (PublicationState::Creating, "CREATING"),
            (PublicationState::LocalReady, "LOCAL_READY"),
            (PublicationState::RemoteReady, "REMOTE_READY"),
            (PublicationState::Failed, "FAILED"),
        ] {
            let status = background_uploader::SnapshotStatus {
                snapshot_id: "snap-01".into(),
                state,
                remote_publishing: true,
                progress: 0.375,
                attempt_count: 7,
                last_error: Some("transient provider failure".into()),
                failure_code: Some("INTERNAL".into()),
                retryable: true,
                retry_at_unix_ms: Some(987654321),
                snapshot_spec: Some(json!({"fixture": "committed-root"})),
                memory_bytes: Some(1048576),
                rootfs_bytes: Some(65536),
                spec_checksum: Some(digest('d')),
                admin_finalized: true,
                catalog_pin_held: false,
                layers: BTreeMap::from([
                    (
                        LayerKind::Memory,
                        background_uploader::LayerUploadStatus {
                            upload_state: background_uploader::LayerUploadState::LocalReady,
                            remote_publishing: true,
                            attempt_count: 7,
                            last_error: Some("retrying DATA".into()),
                            retry_at_unix_ms: Some(987654321),
                        },
                    ),
                    (
                        LayerKind::Block,
                        background_uploader::LayerUploadStatus {
                            upload_state: background_uploader::LayerUploadState::RemoteReady,
                            remote_publishing: false,
                            attempt_count: 2,
                            last_error: None,
                            retry_at_unix_ms: None,
                        },
                    ),
                ]),
            };
            let response: wire::SnapshotStatusResult = status.into();
            response.validate().unwrap();
            assert_eq!(
                serde_json::to_value(response).unwrap(),
                json!({
                    "snapshot_id": "snap-01", "state": label, "remote_publishing": true,
                    "progress": 0.375, "attempt_count": 7,
                    "last_error": "transient provider failure", "failure_code": "INTERNAL",
                    "retryable": true, "retry_at_unix_ms": 987654321,
                    "snapshot_spec": {"fixture": "committed-root"},
                    "memory_bytes": 1048576, "rootfs_bytes": 65536, "spec_checksum": digest('d'),
                    "admin_finalized": true, "catalog_pin_held": false,
                    "layers": {
                        "MEMORY": {"upload_state": "LOCAL_READY", "remote_publishing": true,
                            "attempt_count": 7, "last_error": "retrying DATA", "retry_at_unix_ms": 987654321},
                        "BLOCK": {"upload_state": "REMOTE_READY", "remote_publishing": false, "attempt_count": 2}
                    }
                })
            );
        }
    }

    #[test]
    fn fork_and_runtime_projection_preserve_nested_outcomes_and_omissions() {
        let result = vm::ForkCaptured {
            snapshot: vm::SnapshotCaptured {
                snapshot_id: "snap-01".into(),
                state: vm::CaptureState::LocalReady,
                layer_identities: vec![digest('a'), digest('b')],
            },
            children: vec![
                vm::ForkChild {
                    sandbox_id: "running-child".into(),
                    state: vm::ForkChildState::Running,
                    error: None,
                    guest_endpoint: Some(endpoint()),
                    vmm_pid: Some(1234),
                    rocklet_artifact: Some(artifact()),
                },
                vm::ForkChild {
                    sandbox_id: "pending-child".into(),
                    state: vm::ForkChildState::Pending,
                    error: None,
                    guest_endpoint: None,
                    vmm_pid: None,
                    rocklet_artifact: None,
                },
                vm::ForkChild {
                    sandbox_id: "failed-child".into(),
                    state: vm::ForkChildState::Failed,
                    error: Some("launch refused".into()),
                    guest_endpoint: None,
                    vmm_pid: None,
                    rocklet_artifact: None,
                },
            ],
        };
        let response: wire::ForkLocalReadyResult = result.into();
        assert_eq!(
            serde_json::to_value(response).unwrap(),
            json!({
                "snapshot_id": "snap-01", "state": "LOCAL_READY",
                "layer_identities": [digest('a'), digest('b')],
                "children": [
                    {"sandbox_id": "running-child", "state": "RUNNING", "guest_endpoint": endpoint_json(),
                        "vmm_pid": 1234, "rocklet_artifact": artifact_json()},
                    {"sandbox_id": "pending-child", "state": "PENDING"},
                    {"sandbox_id": "failed-child", "state": "FAILED", "error": "launch refused"}
                ]
            })
        );

        let started: wire::SandboxStartedResult = vm::SandboxStarted {
            identity: "running-child".into(),
            state: "RUNNING".into(),
            guest_endpoint: endpoint(),
            vmm_pid: 1234,
            rocklet_artifact: artifact(),
        }
        .into();
        assert_eq!(
            serde_json::to_value(started).unwrap(),
            json!({
                "identity": "running-child", "state": "RUNNING", "guest_endpoint": endpoint_json(),
                "vmm_pid": 1234, "rocklet_artifact": artifact_json()
            })
        );
        for (state, label) in [
            (vm::SandboxState::Starting, "STARTING"),
            (vm::SandboxState::Running, "RUNNING"),
            (vm::SandboxState::Stopped, "STOPPED"),
            (vm::SandboxState::Failed, "FAILED"),
        ] {
            let response: wire::SandboxStatusResult = vm::SandboxStatus {
                sandbox_id: "observed-child".into(),
                state,
                last_error: None,
                result_code: None,
                guest_endpoint: None,
                vmm_pid: None,
                rocklet_artifact: None,
            }
            .into();
            assert_eq!(
                serde_json::to_value(response).unwrap(),
                json!({"sandbox_id": "observed-child", "state": label})
            );
        }
    }

    #[test]
    fn mutation_finalization_and_usage_projection_keep_external_contract() {
        for (state, label) in [
            (PublicationState::Creating, "CREATING"),
            (PublicationState::LocalReady, "LOCAL_READY"),
            (PublicationState::RemoteReady, "REMOTE_READY"),
            (PublicationState::Failed, "FAILED"),
        ] {
            let response: wire::MutationAcceptedResult = image::ImageBuildAccepted {
                identity: "base-01".into(),
                state,
            }
            .into();
            assert_eq!(
                serde_json::to_value(response).unwrap(),
                json!({"identity": "base-01", "state": label})
            );
        }
        let resolved: wire::ResolvedBaseImageResult = image::ResolvedImage {
            oci_ref: "registry.example/repo:tag".into(),
            oci_digest: digest('a'),
            oci_platform: "linux/amd64".into(),
            base_image_builder_version: "builder-v1".into(),
            base_image_build_key: digest('b'),
        }
        .into();
        assert_eq!(
            serde_json::to_value(resolved).unwrap(),
            json!({
                "oci_ref": "registry.example/repo:tag", "oci_digest": digest('a'),
                "oci_platform": "linux/amd64", "base_image_builder_version": "builder-v1",
                "base_image_build_key": digest('b')
            })
        );
        let stopped: wire::MutationAcceptedResult = vm::OperationAccepted {
            identity: "child-a".into(),
            state: "STOPPED".into(),
        }
        .into();
        assert_eq!(
            serde_json::to_value(stopped).unwrap(),
            json!({"identity": "child-a", "state": "STOPPED"})
        );
        let finalized: wire::FinalizePublishedSnapshotResult =
            background_uploader::PublicationFinalized {
                identity: "snap-01".into(),
                state: PublicationState::RemoteReady,
                released_pins: 2,
            }
            .into();
        assert_eq!(
            serde_json::to_value(finalized).unwrap(),
            json!({"identity": "snap-01", "state": "REMOTE_READY", "released_pins": 2})
        );
        let invalidated: wire::InvalidateSnapshotResult = vm::SnapshotInvalidated {
            identity: "snap-01".into(),
            state: "INVALIDATED".into(),
            invalidated_aliases: 12,
        }
        .into();
        assert_eq!(
            serde_json::to_value(invalidated).unwrap(),
            json!({
                "identity": "snap-01", "state": "INVALIDATED", "invalidated_aliases": 12
            })
        );
        for (state, label, runtimes) in [
            (
                vm::SnapshotUsageState::ActiveRuntimes,
                "ACTIVE_RUNTIMES",
                vec![RuntimeIdentity::new("child-a")],
            ),
            (
                vm::SnapshotUsageState::FencedNoRuntime,
                "FENCED_NO_RUNTIME",
                vec![],
            ),
        ] {
            let response: wire::SnapshotUsageCheckResult = vm::SnapshotUsage {
                snapshot_id: "snap-01".into(),
                state,
                active_runtimes: runtimes.clone(),
            }
            .into();
            assert_eq!(
                serde_json::to_value(response).unwrap(),
                json!({
                    "snapshot_id": "snap-01", "state": label, "active_runtimes": runtimes
                })
            );
        }
    }

    #[test]
    fn borrowed_commands_retain_fence_identity_and_deadline() {
        let stop: wire::StopSandboxRequest = decode(json!({
            "reason": "boundary stop", "deadline_unix_ms": 987654321
        }));
        let command: vm::StopSandbox = (&stop).into();
        command.validate().unwrap();
        assert_eq!(command.reason, "boundary stop");
        assert_eq!(command.deadline_unix_ms, 987654321);

        let finalize: wire::FinalizePublishedSnapshotRequest = decode(json!({
            "spec_checksum": digest('a')
        }));
        let command: vm::FinalizeSnapshot = (&finalize).into();
        command.validate().unwrap();
        assert_eq!(command.spec_checksum, digest('a'));

        let fence: wire::SnapshotFenceRequest = decode(json!({
            "fence_nonce": "nonce-01", "ttl_ms": 300000,
            "expected_runtimes": [{"sandbox_id": "child-a"}, {"sandbox_id": "child-b"}]
        }));
        let command: vm::SnapshotFence = (&fence).into();
        command.validate().unwrap();
        assert_eq!(command.fence_nonce, "nonce-01");
        assert_eq!(command.ttl_ms, 300000);
        assert_eq!(
            command.expected_runtimes,
            [
                RuntimeIdentity::new("child-a"),
                RuntimeIdentity::new("child-b")
            ]
        );
        for ttl in [0, 300001] {
            let mut invalid_wire = fence.clone();
            invalid_wire.ttl_ms = ttl;
            let invalid_domain: vm::SnapshotFence = (&invalid_wire).into();
            assert_eq!(
                invalid_domain.validate().unwrap_err(),
                invalid_wire.validate().unwrap_err()
            );
        }
        let abort = wire::AbortDeleteFenceRequest {
            fence_nonce: "nonce-01".into(),
        };
        let command: vm::AbortDeleteFence = (&abort).into();
        command.validate().unwrap();
        assert_eq!(command.fence_nonce, "nonce-01");
        let invalidate = wire::InvalidateSnapshotRequest {
            fence_nonce: "nonce-02".into(),
        };
        let command: vm::InvalidateSnapshot = (&invalidate).into();
        command.validate().unwrap();
        assert_eq!(command.fence_nonce, "nonce-02");
    }

    #[test]
    fn wire_and_domain_validation_keep_the_same_business_bounds() {
        let valid_image: wire::CreateBaseImageRequest = decode(image_request_json());
        for checksum in [digest('a'), "a".repeat(64), digest('A'), "sha256:ab".into()] {
            let mut request = valid_image.clone();
            request.oci_digest = checksum;
            let command: image::BuildImage = request.clone().into();
            assert_eq!(request.validate(), command.validate());
        }

        let valid_start: wire::StartSandboxFromSnapshotRequest = decode(json!({
            "sandbox_id": "child-a", "snapshot_spec": {}, "spec_checksum": digest('c')
        }));
        for (local, spec, peer) in [
            (false, json!({"fixture": "remote"}), None),
            (true, json!({}), None),
            (true, json!({"fixture": "remote"}), None),
            (true, json!({}), Some("192.0.2.44".parse().unwrap())),
        ] {
            let mut request = valid_start.clone();
            request.local_snapshot = local;
            request.snapshot_spec = spec;
            request.p2p_source_worker_ip = peer;
            let command: vm::StartFromSnapshot = request.clone().into();
            assert_eq!(request.validate(), command.validate());
        }

        let child = wire::RuntimeLaunchPlan {
            sandbox_id: "child-a".into(),
            resource_spec: json!({}),
        };
        for children in [vec![child.clone()], vec![child.clone(); 2], vec![child; 33]] {
            let request = wire::ForkSandboxRequest {
                snapshot: wire::SnapshotSandboxRequest {
                    snapshot_id: "snap-01".into(),
                },
                children,
            };
            let command: vm::ForkSnapshot = request.clone().into();
            assert_eq!(request.validate(), command.validate());
        }
        for (ttl_ms, runtimes) in [
            (1, vec![RuntimeIdentity::new("child-a")]),
            (300001, vec![]),
            (
                1,
                vec![
                    RuntimeIdentity::new("child-a"),
                    RuntimeIdentity::new("child-a"),
                ],
            ),
        ] {
            let request = wire::SnapshotFenceRequest {
                fence_nonce: "nonce-01".into(),
                ttl_ms,
                expected_runtimes: runtimes,
            };
            let command: vm::SnapshotFence = (&request).into();
            assert_eq!(request.validate(), command.validate());
        }
    }
}
