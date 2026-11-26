//! Outbound CLH runtime-control payloads and domain validation.
//!
//! This covers launch, generation cut, promotion, controlled adoption,
//! and shutdown. Shared socket contracts live in `crate::vm::protocol`.

use std::collections::HashSet;

use serde::{Deserialize, Serialize};

use crate::vm::protocol::{
    validate_all_fds_referenced, validate_collection, validate_deadline, validate_fd_reference,
    validate_fd_roles, validate_hex_sha256, validate_protocol_error, validate_size_alignment,
    validate_text, validate_unique_texts, Channel, FdAccess, FdExpectation, FdRole,
    FdRoleDescriptor, ProtocolError, SocketFdRole, SocketFdRoleDescriptor, ValidateVmmMessage,
    MAX_IDENTITY_LENGTH, MAX_OBJECT_KEY_LENGTH,
};

const MAX_RESIDENT_COVERAGE_DATA_RANGES: u32 = 1_048_576;
const MAX_TOKEN_LENGTH: usize = 4096;
const MAX_REASON_LENGTH: usize = 4096;
const MAX_CAPABILITIES: usize = 64;

mod base64_payload {
    use base64::Engine as _;
    use serde::{Deserialize, Deserializer, Serializer};

    pub fn serialize<S>(bytes: &[u8], serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        serializer.serialize_str(&base64::engine::general_purpose::STANDARD.encode(bytes))
    }

    pub fn deserialize<'de, D>(deserializer: D) -> Result<Vec<u8>, D::Error>
    where
        D: Deserializer<'de>,
    {
        let encoded = String::deserialize(deserializer)?;
        base64::engine::general_purpose::STANDARD
            .decode(encoded)
            .map_err(serde::de::Error::custom)
    }
}

#[derive(Clone, Debug, Deserialize, Eq, Hash, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Capability {
    pub name: String,
    pub version: u16,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, Hash, PartialEq, Serialize)]
#[serde(deny_unknown_fields, rename_all = "snake_case")]
pub enum LayerKind {
    Memory,
    Block,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields, rename_all = "snake_case")]
pub enum DataResidency {
    LocalComplete,
    RemoteLazy,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct BlockPathDescriptor {
    pub path: String,
    pub expected_inode: u64,
    pub expected_size: u64,
    pub content_id: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RangeIndexDescriptor {
    pub format_version: u64,
    pub content_id: String,
    pub size: u64,
    pub alignment: u64,
    pub fd_index: u32,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct VmStateDescriptor {
    pub format_version: u64,
    pub content_id: String,
    pub size: u64,
    pub alignment: u64,
    pub fd_index: u32,
}

/// Snapshot of exact DATA ranges already present in a node-local remote-lazy
/// backing. Bit `i` (LSB first) describes DATA range `i` in canonical order.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ResidentCoverageDescriptor {
    pub data_range_count: u32,
    #[serde(with = "base64_payload")]
    pub resident_bitmap: Vec<u8>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct BackingDescriptor {
    pub kind: LayerKind,
    pub data_residency: DataResidency,
    pub ordinal: u32,
    pub layer_id: String,
    pub target_id: String,
    pub content_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub range_index: Option<RangeIndexDescriptor>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub local_ownership: Option<OwnershipMapDescriptor>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub initial_residency: Option<ResidentCoverageDescriptor>,
    pub logical_size: u64,
    pub alignment: u64,
    pub io_alignment: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fd_index: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub shared_mapping_fd_index: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub block_path: Option<BlockPathDescriptor>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct StartRuntime {
    pub sandbox_id: String,
    pub snapshot_id: String,
    pub spec_sha256: String,
    pub launch_identity: String,
    pub config_sha256: String,
    #[serde(default)]
    pub required_capabilities: Vec<Capability>,
    pub deadline_unix_ms: u64,
    pub layers: Vec<BackingDescriptor>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub vm_state: Option<VmStateDescriptor>,
    #[serde(default)]
    pub fd_roles: Vec<FdRoleDescriptor>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeStarted {
    pub sandbox_id: String,
    #[serde(default)]
    pub capabilities: Vec<Capability>,
    pub snapshot_id: String,
    pub attached_target_ids: Vec<String>,
    pub geometry: Vec<LayerGeometry>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct LayerGeometry {
    pub kind: LayerKind,
    pub logical_size: u64,
    pub alignment: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct FrozenLayerDescriptor {
    pub kind: LayerKind,
    pub layer_id: String,
    pub provisional_content_id: String,
    pub logical_size: u64,
    pub alignment: u64,
    pub fd_index: u32,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct OwnershipMapDescriptor {
    pub kind: LayerKind,
    pub snapshot_id: String,
    pub format_version: u64,
    pub logical_size: u64,
    pub granularity: u64,
    #[serde(with = "base64_payload")]
    pub packed_states: Vec<u8>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CaptureGeneration {
    pub snapshot_id: String,
    pub geometry: Vec<LayerGeometry>,
    pub deadline_unix_ms: u64,
}

/// Local ownership handoff only: never promotes or releases a frozen layer.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CapturedGenerationAccepted {
    pub snapshot_id: String,
    pub capture_correlation_id: String,
    pub capture_request_sha256: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct GenerationCaptured {
    pub snapshot_id: String,
    pub pause_duration_ns: u64,
    pub frozen_layers: Vec<FrozenLayerDescriptor>,
    pub vm_state: VmStateDescriptor,
    pub ownership_maps: Vec<OwnershipMapDescriptor>,
    pub fd_roles: Vec<FdRoleDescriptor>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ShutdownRuntimeSession {
    pub reason: String,
    pub deadline_unix_ms: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeSessionStopped {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub exit_reason: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct EnableAdoptionSurvival {
    pub launch_identity: String,
    pub daemon_epoch: String,
    pub config_sha256: String,
    pub adoption_proof_sha256: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AdoptionSurvivalEnabled {
    pub launch_identity: String,
    pub daemon_epoch: String,
    pub config_sha256: String,
    pub adoption_reply_proof_sha256: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PrepareReattachRuntime {
    pub launch_identity: String,
    pub config_sha256: String,
    pub previous_daemon_epoch: String,
    pub new_daemon_epoch: String,
    pub adoption_proof_sha256: String,
    pub challenge: String,
    #[serde(default)]
    pub required_capabilities: Vec<Capability>,
    pub remote_load_fd_index: u32,
    pub socket_fd_roles: Vec<SocketFdRoleDescriptor>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ReattachedFrozenGeneration {
    pub capture_correlation_id: String,
    pub capture_request_sha256: String,
    pub snapshot_id: String,
    pub pause_duration_ns: u64,
    pub frozen_layers: Vec<FrozenLayerDescriptor>,
    pub vm_state: VmStateDescriptor,
    pub ownership_maps: Vec<OwnershipMapDescriptor>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PromotedLayerIdentity {
    pub kind: LayerKind,
    pub ordinal: u32,
    pub layer_id: String,
    pub target_id: String,
    pub content_id: String,
    pub range_index_content_id: String,
    pub logical_size: u64,
    pub alignment: u64,
    pub io_alignment: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ReattachedPromotedGeneration {
    pub capture_correlation_id: String,
    pub capture_request_sha256: String,
    pub snapshot_id: String,
    pub spec_sha256: String,
    pub layers: Vec<PromotedLayerIdentity>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeReattachPrepared {
    pub challenge: String,
    pub snapshot_id: String,
    pub adoption_reply_proof_sha256: String,
    #[serde(default)]
    pub capabilities: Vec<Capability>,
    pub geometry: Vec<LayerGeometry>,
    pub attached_layers: Vec<BackingDescriptor>,
    #[serde(default)]
    pub frozen_generations: Vec<ReattachedFrozenGeneration>,
    #[serde(default)]
    pub promoted_generations: Vec<ReattachedPromotedGeneration>,
    pub fd_roles: Vec<FdRoleDescriptor>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CommitReattach {
    pub challenge: String,
    pub previous_daemon_epoch: String,
    pub new_daemon_epoch: String,
    pub request_proof_sha256: String,
    pub prepared_reply_proof_sha256: String,
    pub commit_proof_sha256: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AbortReattach {
    pub challenge: String,
    pub previous_daemon_epoch: String,
    pub new_daemon_epoch: String,
    pub request_proof_sha256: String,
    pub prepared_reply_proof_sha256: String,
    pub reason: String,
    pub abort_proof_sha256: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct FinalizePublishedGeneration {
    pub capture_correlation_id: String,
    pub capture_request_sha256: String,
    pub snapshot_id: String,
    pub spec_sha256: String,
    pub layers: Vec<BackingDescriptor>,
    pub fd_roles: Vec<FdRoleDescriptor>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PublishedGenerationFinalized {
    pub capture_correlation_id: String,
    pub capture_request_sha256: String,
    pub snapshot_id: String,
    pub spec_sha256: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeReattachCommitted {
    pub challenge: String,
    pub new_daemon_epoch: String,
    pub request_proof_sha256: String,
    pub prepared_reply_proof_sha256: String,
    pub commit_reply_proof_sha256: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeReattachAborted {
    pub challenge: String,
    pub new_daemon_epoch: String,
    pub request_proof_sha256: String,
    pub prepared_reply_proof_sha256: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(
    deny_unknown_fields,
    tag = "type",
    content = "body",
    rename_all = "snake_case"
)]
pub enum ControlRequest {
    EnableAdoptionSurvival(EnableAdoptionSurvival),
    ShutdownRuntimeSession(ShutdownRuntimeSession),
    PrepareReattachRuntime(PrepareReattachRuntime),
    CommitReattach(CommitReattach),
    AbortReattach(AbortReattach),
    StartRuntime(StartRuntime),
    CaptureGeneration(CaptureGeneration),
    AcceptCapturedGeneration(CapturedGenerationAccepted),
    FinalizePublishedGeneration(FinalizePublishedGeneration),
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(
    deny_unknown_fields,
    tag = "type",
    content = "body",
    rename_all = "snake_case"
)]
pub enum ControlReply {
    AdoptionSurvivalEnabled(AdoptionSurvivalEnabled),
    RuntimeSessionStopped(RuntimeSessionStopped),
    RuntimeReattachPrepared(RuntimeReattachPrepared),
    RuntimeReattachCommitted(RuntimeReattachCommitted),
    RuntimeReattachAborted(RuntimeReattachAborted),
    RuntimeStarted(RuntimeStarted),
    GenerationCaptured(GenerationCaptured),
    CapturedGenerationAccepted(CapturedGenerationAccepted),
    PublishedGenerationFinalized(PublishedGenerationFinalized),
    Error(ProtocolError),
}

impl ValidateVmmMessage for ControlRequest {
    const CHANNEL: Channel = Channel::RuntimeControl;

    fn validate(&self) -> Result<(), String> {
        match self {
            Self::EnableAdoptionSurvival(value) => validate_enable_adoption(value),
            Self::ShutdownRuntimeSession(value) => {
                validate_text("shutdown reason", &value.reason, MAX_REASON_LENGTH)?;
                validate_deadline(value.deadline_unix_ms)
            }
            Self::PrepareReattachRuntime(value) => validate_prepare_reattach(value),
            Self::CommitReattach(value) => validate_commit_reattach(value),
            Self::AbortReattach(value) => validate_abort_reattach(value),
            Self::StartRuntime(value) => validate_start_runtime(value),
            Self::CaptureGeneration(value) => validate_capture_generation(value),
            Self::AcceptCapturedGeneration(value) => validate_captured_accepted(value),
            Self::FinalizePublishedGeneration(value) => validate_finalization(value),
        }
    }

    fn fd_roles(&self) -> &[FdRoleDescriptor] {
        match self {
            Self::StartRuntime(value) => &value.fd_roles,
            Self::FinalizePublishedGeneration(value) => &value.fd_roles,
            _ => &[],
        }
    }

    fn socket_fd_roles(&self) -> &[SocketFdRoleDescriptor] {
        match self {
            Self::PrepareReattachRuntime(value) => &value.socket_fd_roles,
            _ => &[],
        }
    }
}

impl ValidateVmmMessage for ControlReply {
    const CHANNEL: Channel = Channel::RuntimeControl;

    fn validate(&self) -> Result<(), String> {
        match self {
            Self::AdoptionSurvivalEnabled(value) => validate_adoption_enabled(value),
            Self::RuntimeSessionStopped(value) => match &value.exit_reason {
                Some(reason) => validate_text("exit reason", reason, MAX_REASON_LENGTH),
                None => Ok(()),
            },
            Self::RuntimeReattachPrepared(value) => validate_reattach_prepared(value),
            Self::RuntimeReattachCommitted(value) => validate_reattach_committed(value),
            Self::RuntimeReattachAborted(value) => validate_reattach_aborted(value),
            Self::RuntimeStarted(value) => validate_runtime_started(value),
            Self::GenerationCaptured(value) => validate_generation_captured(value),
            Self::PublishedGenerationFinalized(value) => validate_finalized_identity(value),
            Self::CapturedGenerationAccepted(value) => validate_captured_accepted(value),
            Self::Error(value) => validate_protocol_error(value),
        }
    }

    fn fd_roles(&self) -> &[FdRoleDescriptor] {
        match self {
            Self::GenerationCaptured(value) => &value.fd_roles,
            Self::RuntimeReattachPrepared(value) => &value.fd_roles,
            _ => &[],
        }
    }
}

fn validate_start_runtime(value: &StartRuntime) -> Result<(), String> {
    validate_text("sandbox ID", &value.sandbox_id, MAX_IDENTITY_LENGTH)?;
    validate_text("snapshot ID", &value.snapshot_id, MAX_IDENTITY_LENGTH)?;
    validate_hex_sha256(&value.spec_sha256, true)?;
    validate_text(
        "launch identity",
        &value.launch_identity,
        MAX_IDENTITY_LENGTH,
    )?;
    validate_hex_sha256(&value.config_sha256, true)?;
    validate_capabilities(&value.required_capabilities)?;
    validate_deadline(value.deadline_unix_ms)?;
    validate_runtime_backing(
        &value.snapshot_id,
        &value.layers,
        value.vm_state.as_ref(),
        &value.fd_roles,
    )
}

fn validate_runtime_started(value: &RuntimeStarted) -> Result<(), String> {
    validate_text("sandbox ID", &value.sandbox_id, MAX_IDENTITY_LENGTH)?;
    validate_capabilities(&value.capabilities)?;
    validate_text("snapshot ID", &value.snapshot_id, MAX_IDENTITY_LENGTH)?;
    validate_unique_texts("attached target IDs", &value.attached_target_ids, true)?;
    validate_geometry(&value.geometry, true)
}

fn validate_capture_generation(value: &CaptureGeneration) -> Result<(), String> {
    validate_text("snapshot ID", &value.snapshot_id, MAX_IDENTITY_LENGTH)?;
    validate_geometry(&value.geometry, true)?;
    validate_deadline(value.deadline_unix_ms)
}

fn validate_generation_captured(value: &GenerationCaptured) -> Result<(), String> {
    validate_captured_generation(value)
}

fn validate_captured_accepted(value: &CapturedGenerationAccepted) -> Result<(), String> {
    validate_text(
        "capture correlation ID",
        &value.capture_correlation_id,
        MAX_IDENTITY_LENGTH,
    )?;
    validate_text("snapshot ID", &value.snapshot_id, MAX_IDENTITY_LENGTH)?;
    validate_hex_sha256(&value.capture_request_sha256, true)
}

fn validate_finalization(value: &FinalizePublishedGeneration) -> Result<(), String> {
    validate_text(
        "capture correlation ID",
        &value.capture_correlation_id,
        MAX_IDENTITY_LENGTH,
    )?;
    validate_hex_sha256(&value.capture_request_sha256, true)?;
    validate_text("snapshot ID", &value.snapshot_id, MAX_IDENTITY_LENGTH)?;
    validate_hex_sha256(&value.spec_sha256, true)?;
    validate_published_layers(&value.layers, &value.fd_roles)
}

fn validate_finalized_identity(value: &PublishedGenerationFinalized) -> Result<(), String> {
    validate_text(
        "capture correlation ID",
        &value.capture_correlation_id,
        MAX_IDENTITY_LENGTH,
    )?;
    validate_hex_sha256(&value.capture_request_sha256, true)?;
    validate_text("snapshot ID", &value.snapshot_id, MAX_IDENTITY_LENGTH)?;
    validate_hex_sha256(&value.spec_sha256, true)
}

fn validate_runtime_backing(
    snapshot_id: &str,
    layers: &[BackingDescriptor],
    vm_state: Option<&VmStateDescriptor>,
    fd_roles: &[FdRoleDescriptor],
) -> Result<(), String> {
    validate_text("snapshot ID", snapshot_id, MAX_IDENTITY_LENGTH)?;
    validate_collection("backing layers", layers.len(), true)?;
    validate_fd_roles(fd_roles)?;
    let mut layer_ids = HashSet::new();
    let mut target_ids = HashSet::new();
    let mut referenced = HashSet::new();
    let mut ordinals = [0_u32, 0_u32];
    let mut resident_range_counts = [0_u32, 0_u32];
    for layer in layers {
        validate_backing(layer)?;
        if !layer_ids.insert((layer.kind, layer.layer_id.as_str())) {
            return Err("duplicate backing layer".into());
        }
        if !target_ids.insert(layer.target_id.as_str()) {
            return Err("duplicate load target".into());
        }
        let position = kind_index(layer.kind);
        if let Some(coverage) = &layer.initial_residency {
            resident_range_counts[position] = resident_range_counts[position]
                .checked_add(coverage.data_range_count)
                .ok_or_else(|| "initial residency DATA range count overflows".to_string())?;
            if resident_range_counts[position] > MAX_RESIDENT_COVERAGE_DATA_RANGES {
                return Err("initial residency exceeds the per-kind DATA range bound".into());
            }
        }
        if layer.ordinal != ordinals[position] {
            return Err("backing layer ordinals must be contiguous root-to-leaf".into());
        }
        ordinals[position] = ordinals[position]
            .checked_add(1)
            .ok_or_else(|| "backing layer ordinal overflows".to_string())?;
        match layer.kind {
            LayerKind::Memory => {
                if layer.block_path.is_some() {
                    return Err("memory backing must not contain a block path".into());
                }
                validate_fd_reference(
                    layer
                        .fd_index
                        .ok_or_else(|| "memory backing must reference an FD".to_string())?,
                    fd_roles,
                    FdExpectation::new(
                        FdRole::MemoryRoLayer,
                        FdAccess::ReadOnly,
                        &layer.content_id,
                        layer.logical_size,
                        layer.alignment,
                    ),
                    &mut referenced,
                )?;
                match layer.shared_mapping_fd_index {
                    Some(mapping_fd_index) => {
                        validate_fd_reference(
                            mapping_fd_index,
                            fd_roles,
                            FdExpectation::new(
                                FdRole::MemorySharedMapping,
                                FdAccess::ReadWrite,
                                &layer.content_id,
                                layer.logical_size,
                                layer.alignment,
                            ),
                            &mut referenced,
                        )?;
                    }
                    None => {
                        return Err("memory backing requires a shared mapping FD".into());
                    }
                }
            }
            LayerKind::Block => match (layer.fd_index, &layer.block_path) {
                (Some(index), None) => validate_fd_reference(
                    index,
                    fd_roles,
                    FdExpectation::new(
                        FdRole::BlockRoLayer,
                        FdAccess::ReadOnly,
                        &layer.content_id,
                        layer.logical_size,
                        layer.alignment,
                    ),
                    &mut referenced,
                )?,
                (None, Some(path)) => validate_block_path(path, layer)?,
                _ => return Err("block backing must contain exactly one FD or path".into()),
            },
        }
        if layer.kind == LayerKind::Block && layer.shared_mapping_fd_index.is_some() {
            return Err("block backing must not contain a shared mapping FD".into());
        }
        match (&layer.range_index, &layer.local_ownership) {
            (Some(index), None) => validate_fd_reference(
                index.fd_index,
                fd_roles,
                FdExpectation::new(
                    range_index_role(layer.kind),
                    FdAccess::ReadOnly,
                    &index.content_id,
                    index.size,
                    index.alignment,
                ),
                &mut referenced,
            )?,
            (None, Some(ownership)) => {
                validate_ownership_map(ownership)?;
                if layer.data_residency != DataResidency::LocalComplete
                    || ownership.kind != layer.kind
                    || ownership.logical_size != layer.logical_size
                    || ownership.granularity != layer.alignment
                {
                    return Err("local frozen ownership does not match its backing".into());
                }
            }
            _ => {
                return Err(
                    "backing must contain exactly one canonical range index or local ownership payload"
                        .into(),
                );
            }
        }
    }
    if let Some(vm_state) = vm_state {
        validate_vm_state(vm_state)?;
        validate_fd_reference(
            vm_state.fd_index,
            fd_roles,
            FdExpectation::new(
                FdRole::VmState,
                FdAccess::ReadOnly,
                &vm_state.content_id,
                vm_state.size,
                vm_state.alignment,
            ),
            &mut referenced,
        )?;
    }
    validate_all_fds_referenced(fd_roles, &referenced)
}

fn validate_captured_generation(value: &GenerationCaptured) -> Result<(), String> {
    validate_text("snapshot ID", &value.snapshot_id, MAX_IDENTITY_LENGTH)?;
    validate_collection("frozen layers", value.frozen_layers.len(), true)?;
    validate_collection("ownership maps", value.ownership_maps.len(), true)?;
    validate_fd_roles(&value.fd_roles)?;
    let mut referenced = HashSet::new();
    let mut layer_kinds = HashSet::new();
    for layer in &value.frozen_layers {
        validate_text("layer ID", &layer.layer_id, MAX_IDENTITY_LENGTH)?;
        validate_text(
            "provisional content ID",
            &layer.provisional_content_id,
            MAX_IDENTITY_LENGTH,
        )?;
        validate_size_alignment(layer.logical_size, layer.alignment, "frozen layer")?;
        if !layer_kinds.insert(layer.kind) {
            return Err("duplicate frozen layer kind".into());
        }
        validate_fd_reference(
            layer.fd_index,
            &value.fd_roles,
            FdExpectation::new(
                layer_role(layer.kind),
                FdAccess::ReadWrite,
                &layer.provisional_content_id,
                layer.logical_size,
                layer.alignment,
            ),
            &mut referenced,
        )?;
    }
    validate_vm_state(&value.vm_state)?;
    validate_fd_reference(
        value.vm_state.fd_index,
        &value.fd_roles,
        FdExpectation::new(
            FdRole::VmState,
            FdAccess::ReadOnly,
            &value.vm_state.content_id,
            value.vm_state.size,
            value.vm_state.alignment,
        ),
        &mut referenced,
    )?;
    let mut ownership_kinds = HashSet::new();
    for ownership in &value.ownership_maps {
        validate_ownership_map(ownership)?;
        if ownership.snapshot_id != value.snapshot_id {
            return Err("ownership map snapshot ID does not match capture".into());
        }
        if !ownership_kinds.insert(ownership.kind) {
            return Err("duplicate ownership map kind".into());
        }
        let layer = value
            .frozen_layers
            .iter()
            .find(|layer| layer.kind == ownership.kind)
            .ok_or_else(|| "ownership map has no frozen layer".to_string())?;
        if ownership.logical_size != layer.logical_size {
            return Err("ownership map geometry does not match frozen layer".into());
        }
    }
    if ownership_kinds != layer_kinds {
        return Err("every frozen layer must have one ownership map".into());
    }
    validate_all_fds_referenced(&value.fd_roles, &referenced)
}

fn validate_enable_adoption(value: &EnableAdoptionSurvival) -> Result<(), String> {
    validate_text(
        "launch identity",
        &value.launch_identity,
        MAX_IDENTITY_LENGTH,
    )?;
    validate_text("daemon epoch", &value.daemon_epoch, MAX_IDENTITY_LENGTH)?;
    validate_hex_sha256(&value.config_sha256, true)?;
    validate_hex_sha256(&value.adoption_proof_sha256, true)
}

fn validate_adoption_enabled(value: &AdoptionSurvivalEnabled) -> Result<(), String> {
    validate_text(
        "launch identity",
        &value.launch_identity,
        MAX_IDENTITY_LENGTH,
    )?;
    validate_text("daemon epoch", &value.daemon_epoch, MAX_IDENTITY_LENGTH)?;
    validate_hex_sha256(&value.config_sha256, true)?;
    validate_hex_sha256(&value.adoption_reply_proof_sha256, true)
}

fn validate_prepare_reattach(value: &PrepareReattachRuntime) -> Result<(), String> {
    validate_text(
        "launch identity",
        &value.launch_identity,
        MAX_IDENTITY_LENGTH,
    )?;
    validate_hex_sha256(&value.config_sha256, true)?;
    validate_text(
        "previous daemon epoch",
        &value.previous_daemon_epoch,
        MAX_IDENTITY_LENGTH,
    )?;
    validate_text(
        "new daemon epoch",
        &value.new_daemon_epoch,
        MAX_IDENTITY_LENGTH,
    )?;
    if value.previous_daemon_epoch == value.new_daemon_epoch {
        return Err("reattach must advance to a different daemon epoch".into());
    }
    validate_hex_sha256(&value.adoption_proof_sha256, true)?;
    validate_text("challenge", &value.challenge, MAX_TOKEN_LENGTH)?;
    validate_capabilities(&value.required_capabilities)?;
    if value.remote_load_fd_index != 0 || value.socket_fd_roles.len() != 1 {
        return Err("remote-load socket must be the first and only socket FD".into());
    }
    let descriptor = &value.socket_fd_roles[0];
    if descriptor.role != SocketFdRole::RemoteLoadChannel
        || descriptor.content_id != value.new_daemon_epoch
    {
        return Err("remote-load socket identity must match the new daemon epoch".into());
    }
    Ok(())
}

fn validate_reattach_terminal_identity(
    challenge: &str,
    previous_daemon_epoch: &str,
    new_daemon_epoch: &str,
    request_proof: &str,
    prepared_proof: &str,
) -> Result<(), String> {
    validate_text("challenge", challenge, MAX_TOKEN_LENGTH)?;
    validate_text(
        "previous daemon epoch",
        previous_daemon_epoch,
        MAX_IDENTITY_LENGTH,
    )?;
    validate_text("new daemon epoch", new_daemon_epoch, MAX_IDENTITY_LENGTH)?;
    if previous_daemon_epoch == new_daemon_epoch {
        return Err("reattach terminal operation must advance daemon epoch".into());
    }
    validate_hex_sha256(request_proof, true)?;
    validate_hex_sha256(prepared_proof, true)
}

fn validate_commit_reattach(value: &CommitReattach) -> Result<(), String> {
    validate_reattach_terminal_identity(
        &value.challenge,
        &value.previous_daemon_epoch,
        &value.new_daemon_epoch,
        &value.request_proof_sha256,
        &value.prepared_reply_proof_sha256,
    )?;
    validate_hex_sha256(&value.commit_proof_sha256, true)
}

fn validate_abort_reattach(value: &AbortReattach) -> Result<(), String> {
    validate_reattach_terminal_identity(
        &value.challenge,
        &value.previous_daemon_epoch,
        &value.new_daemon_epoch,
        &value.request_proof_sha256,
        &value.prepared_reply_proof_sha256,
    )?;
    validate_text("abort reason", &value.reason, MAX_REASON_LENGTH)?;
    validate_hex_sha256(&value.abort_proof_sha256, true)
}

fn validate_reattach_committed(value: &RuntimeReattachCommitted) -> Result<(), String> {
    validate_text("challenge", &value.challenge, MAX_TOKEN_LENGTH)?;
    validate_text(
        "new daemon epoch",
        &value.new_daemon_epoch,
        MAX_IDENTITY_LENGTH,
    )?;
    validate_hex_sha256(&value.request_proof_sha256, true)?;
    validate_hex_sha256(&value.prepared_reply_proof_sha256, true)?;
    validate_hex_sha256(&value.commit_reply_proof_sha256, true)
}

fn validate_reattach_aborted(value: &RuntimeReattachAborted) -> Result<(), String> {
    validate_text("challenge", &value.challenge, MAX_TOKEN_LENGTH)?;
    validate_text(
        "new daemon epoch",
        &value.new_daemon_epoch,
        MAX_IDENTITY_LENGTH,
    )?;
    validate_hex_sha256(&value.request_proof_sha256, true)?;
    validate_hex_sha256(&value.prepared_reply_proof_sha256, true)
}

fn validate_reattach_prepared(value: &RuntimeReattachPrepared) -> Result<(), String> {
    validate_text("challenge", &value.challenge, MAX_TOKEN_LENGTH)?;
    validate_text("snapshot ID", &value.snapshot_id, MAX_IDENTITY_LENGTH)?;
    validate_hex_sha256(&value.adoption_reply_proof_sha256, true)?;
    validate_capabilities(&value.capabilities)?;
    validate_geometry(&value.geometry, true)?;
    validate_collection("attached layers", value.attached_layers.len(), true)?;
    validate_collection(
        "frozen recovery generations",
        value.frozen_generations.len(),
        false,
    )?;
    validate_collection(
        "promoted recovery generations",
        value.promoted_generations.len(),
        false,
    )?;
    validate_fd_roles(&value.fd_roles)?;

    let mut referenced = HashSet::new();
    let mut expected_index = 0_u32;
    let mut previous_kind = None;
    let mut next_ordinal = [0_u32, 0_u32];
    for layer in &value.attached_layers {
        if previous_kind == Some(LayerKind::Block) && layer.kind == LayerKind::Memory {
            return Err("reattached layers must order memory before block".into());
        }
        previous_kind = Some(layer.kind);
        let ordinal = &mut next_ordinal[kind_index(layer.kind)];
        if layer.ordinal != *ordinal {
            return Err("reattached layer ordinals must be contiguous root-to-leaf".into());
        }
        *ordinal = ordinal
            .checked_add(1)
            .ok_or_else(|| "reattached layer ordinal overflows".to_string())?;
        if layer.fd_index != Some(expected_index) || layer.block_path.is_some() {
            return Err("reattached backing must use canonical SCM_RIGHTS FD order".into());
        }
        validate_backing(layer)?;
        validate_fd_reference(
            expected_index,
            &value.fd_roles,
            FdExpectation::new(
                layer_role(layer.kind),
                FdAccess::ReadOnly,
                &layer.content_id,
                layer.logical_size,
                layer.alignment,
            ),
            &mut referenced,
        )?;
        expected_index += 1;
        match layer.kind {
            LayerKind::Memory => {
                if layer.shared_mapping_fd_index != Some(expected_index) {
                    return Err("reattached memory mapping must use the next canonical FD".into());
                }
                validate_fd_reference(
                    expected_index,
                    &value.fd_roles,
                    FdExpectation::new(
                        FdRole::MemorySharedMapping,
                        FdAccess::ReadWrite,
                        &layer.content_id,
                        layer.logical_size,
                        layer.alignment,
                    ),
                    &mut referenced,
                )?;
                expected_index += 1;
            }
            LayerKind::Block if layer.shared_mapping_fd_index.is_none() => {}
            LayerKind::Block => {
                return Err("reattached block must not contain a shared mapping FD".into());
            }
        }
        match (&layer.range_index, &layer.local_ownership) {
            (Some(range_index), None) => {
                if range_index.fd_index != expected_index {
                    return Err("reattached range index must use the next canonical FD".into());
                }
                validate_fd_reference(
                    expected_index,
                    &value.fd_roles,
                    FdExpectation::new(
                        range_index_role(layer.kind),
                        FdAccess::ReadOnly,
                        &range_index.content_id,
                        range_index.size,
                        range_index.alignment,
                    ),
                    &mut referenced,
                )?;
                expected_index += 1;
            }
            (None, Some(ownership)) => {
                validate_ownership_map(ownership)?;
                if layer.data_residency != DataResidency::LocalComplete
                    || ownership.kind != layer.kind
                    || ownership.logical_size != layer.logical_size
                    || ownership.granularity != layer.alignment
                {
                    return Err("reattached local ownership does not match backing".into());
                }
            }
            _ => return Err("reattached layer coverage is ambiguous".into()),
        }
    }

    let mut frozen_snapshots = HashSet::new();
    for frozen in &value.frozen_generations {
        validate_reattached_frozen(
            frozen,
            &value.fd_roles,
            &mut referenced,
            &mut expected_index,
        )?;
        if !frozen_snapshots.insert(&frozen.snapshot_id) {
            return Err("frozen recovery snapshot identities must be unique".into());
        }
    }

    let mut promoted_snapshots = HashSet::new();
    for promoted in &value.promoted_generations {
        validate_reattached_promoted(promoted, &value.attached_layers)?;
        if !promoted_snapshots.insert(&promoted.snapshot_id) {
            return Err("promoted recovery snapshot identities must be unique".into());
        }
    }
    validate_all_fds_referenced(&value.fd_roles, &referenced)
}

fn validate_reattached_frozen(
    value: &ReattachedFrozenGeneration,
    fd_roles: &[FdRoleDescriptor],
    referenced: &mut HashSet<u32>,
    expected_index: &mut u32,
) -> Result<(), String> {
    validate_text(
        "capture correlation ID",
        &value.capture_correlation_id,
        MAX_IDENTITY_LENGTH,
    )?;
    validate_hex_sha256(&value.capture_request_sha256, true)?;
    validate_text("snapshot ID", &value.snapshot_id, MAX_IDENTITY_LENGTH)?;
    if value.frozen_layers.len() != 2
        || value.frozen_layers[0].kind != LayerKind::Memory
        || value.frozen_layers[1].kind != LayerKind::Block
    {
        return Err("frozen recovery must contain memory then block exactly once".into());
    }
    for layer in &value.frozen_layers {
        validate_text("layer ID", &layer.layer_id, MAX_IDENTITY_LENGTH)?;
        validate_text(
            "provisional content ID",
            &layer.provisional_content_id,
            MAX_IDENTITY_LENGTH,
        )?;
        validate_size_alignment(layer.logical_size, layer.alignment, "frozen layer")?;
        if layer.fd_index != *expected_index {
            return Err("frozen layer must use the next canonical FD".into());
        }
        validate_fd_reference(
            *expected_index,
            fd_roles,
            FdExpectation::new(
                layer_role(layer.kind),
                FdAccess::ReadOnly,
                &layer.provisional_content_id,
                layer.logical_size,
                layer.alignment,
            ),
            referenced,
        )?;
        *expected_index += 1;
    }
    if value.vm_state.fd_index != *expected_index {
        return Err("frozen VM state must use the next canonical FD".into());
    }
    validate_vm_state(&value.vm_state)?;
    validate_fd_reference(
        *expected_index,
        fd_roles,
        FdExpectation::new(
            FdRole::VmState,
            FdAccess::ReadOnly,
            &value.vm_state.content_id,
            value.vm_state.size,
            value.vm_state.alignment,
        ),
        referenced,
    )?;
    *expected_index += 1;
    if value.ownership_maps.len() != 2
        || value.ownership_maps[0].kind != LayerKind::Memory
        || value.ownership_maps[1].kind != LayerKind::Block
    {
        return Err("ownership maps must match canonical frozen layer order".into());
    }
    for ownership in &value.ownership_maps {
        validate_ownership_map(ownership)?;
        if ownership.snapshot_id != value.snapshot_id {
            return Err("ownership map snapshot ID does not match frozen recovery".into());
        }
        let layer = value
            .frozen_layers
            .iter()
            .find(|layer| layer.kind == ownership.kind)
            .ok_or_else(|| "ownership map has no frozen layer".to_string())?;
        if ownership.logical_size != layer.logical_size {
            return Err("ownership map geometry does not match frozen layer".into());
        }
    }
    Ok(())
}

fn validate_reattached_promoted(
    value: &ReattachedPromotedGeneration,
    attached_layers: &[BackingDescriptor],
) -> Result<(), String> {
    validate_text(
        "capture correlation ID",
        &value.capture_correlation_id,
        MAX_IDENTITY_LENGTH,
    )?;
    validate_hex_sha256(&value.capture_request_sha256, true)?;
    validate_text("snapshot ID", &value.snapshot_id, MAX_IDENTITY_LENGTH)?;
    validate_hex_sha256(&value.spec_sha256, true)?;
    if value.layers.len() != 2
        || value.layers[0].kind != LayerKind::Memory
        || value.layers[1].kind != LayerKind::Block
    {
        return Err("promoted recovery must identify memory then block exactly once".into());
    }
    for identity in &value.layers {
        validate_promoted_layer_identity(identity)?;
        if attached_layers
            .iter()
            .filter(|layer| promoted_identity_matches(identity, layer))
            .count()
            != 1
        {
            return Err("promoted recovery layer must match one attached layer exactly".into());
        }
    }
    Ok(())
}

fn validate_promoted_layer_identity(value: &PromotedLayerIdentity) -> Result<(), String> {
    validate_text("layer ID", &value.layer_id, MAX_IDENTITY_LENGTH)?;
    validate_text("target ID", &value.target_id, MAX_IDENTITY_LENGTH)?;
    validate_text("content ID", &value.content_id, MAX_IDENTITY_LENGTH)?;
    validate_text(
        "range index content ID",
        &value.range_index_content_id,
        MAX_IDENTITY_LENGTH,
    )?;
    validate_size_alignment(value.logical_size, value.alignment, "promoted layer")?;
    if value.io_alignment == 0
        || !value.io_alignment.is_power_of_two()
        || !value.logical_size.is_multiple_of(value.io_alignment)
    {
        return Err("promoted layer io_alignment is incompatible with logical size".into());
    }
    Ok(())
}

fn promoted_identity_matches(identity: &PromotedLayerIdentity, layer: &BackingDescriptor) -> bool {
    identity.kind == layer.kind
        && identity.ordinal == layer.ordinal
        && identity.layer_id == layer.layer_id
        && identity.target_id == layer.target_id
        && identity.content_id == layer.content_id
        && layer
            .range_index
            .as_ref()
            .is_some_and(|index| identity.range_index_content_id == index.content_id)
        && identity.logical_size == layer.logical_size
        && identity.alignment == layer.alignment
        && identity.io_alignment == layer.io_alignment
}

fn validate_published_layers(
    layers: &[BackingDescriptor],
    fd_roles: &[FdRoleDescriptor],
) -> Result<(), String> {
    if layers.len() != 2
        || layers[0].kind != LayerKind::Memory
        || layers[1].kind != LayerKind::Block
        || fd_roles.len() != 5
    {
        return Err("promotion requires canonical memory and block FD pairs".into());
    }
    validate_fd_roles(fd_roles)?;
    let mut referenced = HashSet::new();
    let mut expected_index = 0_u32;
    for layer in layers {
        validate_backing(layer)?;
        if layer.data_residency != DataResidency::LocalComplete || layer.block_path.is_some() {
            return Err("promoted layers must be local-complete immutable backings".into());
        }
        if layer.fd_index != Some(expected_index) {
            return Err("promoted backing has noncanonical FD order".into());
        }
        validate_fd_reference(
            expected_index,
            fd_roles,
            FdExpectation::new(
                layer_role(layer.kind),
                FdAccess::ReadOnly,
                &layer.content_id,
                layer.logical_size,
                layer.alignment,
            ),
            &mut referenced,
        )?;
        expected_index += 1;
        match layer.kind {
            LayerKind::Memory => {
                if layer.shared_mapping_fd_index != Some(expected_index) {
                    return Err("promoted memory mapping has noncanonical FD order".into());
                }
                validate_fd_reference(
                    expected_index,
                    fd_roles,
                    FdExpectation::new(
                        FdRole::MemorySharedMapping,
                        FdAccess::ReadWrite,
                        &layer.content_id,
                        layer.logical_size,
                        layer.alignment,
                    ),
                    &mut referenced,
                )?;
                expected_index += 1;
            }
            LayerKind::Block if layer.shared_mapping_fd_index.is_none() => {}
            LayerKind::Block => {
                return Err("promoted block must not contain a shared mapping FD".into());
            }
        }
        let range_index = layer
            .range_index
            .as_ref()
            .ok_or_else(|| "promoted layer must contain a canonical range index".to_string())?;
        if layer.local_ownership.is_some() || range_index.fd_index != expected_index {
            return Err("promoted range index has noncanonical FD order".into());
        }
        validate_fd_reference(
            expected_index,
            fd_roles,
            FdExpectation::new(
                range_index_role(layer.kind),
                FdAccess::ReadOnly,
                &range_index.content_id,
                range_index.size,
                range_index.alignment,
            ),
            &mut referenced,
        )?;
        expected_index += 1;
    }
    validate_all_fds_referenced(fd_roles, &referenced)
}

fn validate_backing(value: &BackingDescriptor) -> Result<(), String> {
    validate_text("layer ID", &value.layer_id, MAX_IDENTITY_LENGTH)?;
    validate_text("target ID", &value.target_id, MAX_IDENTITY_LENGTH)?;
    validate_text("content ID", &value.content_id, MAX_IDENTITY_LENGTH)?;
    validate_size_alignment(value.logical_size, value.alignment, "backing")?;
    validate_size_alignment(value.logical_size, value.io_alignment, "backing I/O")?;
    match (&value.range_index, &value.local_ownership) {
        (Some(index), None) => {
            validate_text(
                "range index content ID",
                &index.content_id,
                MAX_IDENTITY_LENGTH,
            )?;
            validate_size_alignment(index.size, index.alignment, "range index")?;
        }
        (None, Some(ownership)) => validate_ownership_map(ownership)?,
        _ => return Err("backing coverage is missing or ambiguous".into()),
    }
    if let Some(coverage) = &value.initial_residency {
        if value.data_residency != DataResidency::RemoteLazy
            || value.range_index.is_none()
            || value.local_ownership.is_some()
        {
            return Err("initial residency requires a remote-lazy canonical range index".into());
        }
        validate_resident_coverage(coverage)?;
    }
    if !value.alignment.is_multiple_of(value.io_alignment) {
        return Err("backing range alignment must be a multiple of I/O alignment".into());
    }
    Ok(())
}

fn validate_resident_coverage(value: &ResidentCoverageDescriptor) -> Result<(), String> {
    if value.data_range_count == 0 || value.data_range_count > MAX_RESIDENT_COVERAGE_DATA_RANGES {
        return Err("initial residency DATA range count is invalid".into());
    }
    let expected_len = usize::try_from(value.data_range_count.div_ceil(8))
        .map_err(|_| "initial residency bitmap size overflows".to_string())?;
    if value.resident_bitmap.len() != expected_len {
        return Err("initial residency bitmap length does not match DATA range count".into());
    }
    if !value.resident_bitmap.iter().any(|byte| *byte != 0) {
        return Err("empty initial residency must be omitted".into());
    }
    let remainder = value.data_range_count % 8;
    if remainder != 0 {
        let unused_mask = !((1_u8 << remainder) - 1);
        if value
            .resident_bitmap
            .last()
            .is_some_and(|byte| byte & unused_mask != 0)
        {
            return Err("initial residency bitmap has nonzero padding bits".into());
        }
    }
    Ok(())
}

fn validate_block_path(
    path: &BlockPathDescriptor,
    layer: &BackingDescriptor,
) -> Result<(), String> {
    validate_text("block path", &path.path, MAX_OBJECT_KEY_LENGTH)?;
    if !path.path.starts_with('/') || path.expected_inode == 0 {
        return Err("block path must be absolute and have a nonzero inode".into());
    }
    if path.expected_size != layer.logical_size || path.content_id != layer.content_id {
        return Err("block path identity or geometry conflicts with layer".into());
    }
    Ok(())
}

fn validate_vm_state(value: &VmStateDescriptor) -> Result<(), String> {
    validate_text(
        "VM state content ID",
        &value.content_id,
        MAX_IDENTITY_LENGTH,
    )?;
    validate_size_alignment(value.size, value.alignment, "VM state")
}

fn validate_geometry(values: &[LayerGeometry], canonical: bool) -> Result<(), String> {
    validate_collection("geometry", values.len(), true)?;
    let mut kinds = HashSet::new();
    for value in values {
        if !kinds.insert(value.kind) {
            return Err("duplicate generation geometry kind".into());
        }
        validate_size_alignment(value.logical_size, value.alignment, "generation geometry")?;
    }
    if canonical
        && (values.len() != 2
            || values[0].kind != LayerKind::Memory
            || values[1].kind != LayerKind::Block)
    {
        return Err("runtime geometry must be ordered memory then block".into());
    }
    Ok(())
}

fn validate_capabilities(values: &[Capability]) -> Result<(), String> {
    if values.len() > MAX_CAPABILITIES {
        return Err("too many capabilities".into());
    }
    let mut identities = HashSet::new();
    for value in values {
        validate_text("capability name", &value.name, MAX_IDENTITY_LENGTH)?;
        if value.version == 0 || !identities.insert((value.name.as_str(), value.version)) {
            return Err("invalid or duplicate capability".into());
        }
    }
    Ok(())
}

fn validate_ownership_map(value: &OwnershipMapDescriptor) -> Result<(), String> {
    validate_text(
        "ownership snapshot ID",
        &value.snapshot_id,
        MAX_IDENTITY_LENGTH,
    )?;
    const PACKED_2BIT_FORMAT_VERSION: u64 = 1;
    if value.format_version != PACKED_2BIT_FORMAT_VERSION {
        return Err("ownership map encoding version is unsupported".into());
    }
    if value.logical_size == 0
        || value.granularity == 0
        || !value.granularity.is_power_of_two()
        || !value.logical_size.is_multiple_of(value.granularity)
    {
        return Err("ownership map geometry is invalid".into());
    }
    let units = value.logical_size / value.granularity;
    let expected_bytes = units
        .checked_add(3)
        .and_then(|units| units.checked_div(4))
        .and_then(|bytes| usize::try_from(bytes).ok())
        .ok_or_else(|| "ownership map length overflows".to_string())?;
    if value.packed_states.len() != expected_bytes {
        return Err("packed ownership payload length does not match geometry".into());
    }
    for unit in 0..units {
        let byte = value.packed_states[usize::try_from(unit / 4)
            .map_err(|_| "ownership unit index overflows".to_string())?];
        if (byte >> ((unit % 4) * 2)) & 0b11 == 0b11 {
            return Err("packed ownership payload contains an invalid state".into());
        }
    }
    let used_bits = (units % 4) * 2;
    if used_bits != 0
        && value
            .packed_states
            .last()
            .is_some_and(|byte| *byte >> used_bits != 0)
    {
        return Err("packed ownership payload has non-zero trailing bits".into());
    }
    Ok(())
}

fn layer_role(kind: LayerKind) -> FdRole {
    match kind {
        LayerKind::Memory => FdRole::MemoryRoLayer,
        LayerKind::Block => FdRole::BlockRoLayer,
    }
}

fn range_index_role(kind: LayerKind) -> FdRole {
    match kind {
        LayerKind::Memory => FdRole::MemoryRangeIndex,
        LayerKind::Block => FdRole::BlockRangeIndex,
    }
}

fn kind_index(kind: LayerKind) -> usize {
    match kind {
        LayerKind::Memory => 0,
        LayerKind::Block => 1,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vm::protocol::{
        decode_message, encode_message, MessageHeader, WireEnvelope, VMM_PROTOCOL_VERSION,
    };

    fn shutdown() -> WireEnvelope<ControlRequest> {
        WireEnvelope {
            header: MessageHeader {
                protocol_version: VMM_PROTOCOL_VERSION,
                channel: Channel::RuntimeControl,
                correlation_id: "golden".into(),
            },
            message: ControlRequest::ShutdownRuntimeSession(ShutdownRuntimeSession {
                reason: "test".into(),
                deadline_unix_ms: 7,
            }),
        }
    }

    #[test]
    fn outbound_control_json_golden_bytes_survive_protocol_ownership_move() {
        let message = shutdown();
        let expected = br#"{"header":{"protocol_version":10,"channel":"runtime_control","correlation_id":"golden"},"message":{"type":"shutdown_runtime_session","body":{"reason":"test","deadline_unix_ms":7}}}"#;
        assert_eq!(encode_message(&message, "golden").unwrap(), expected);
        assert_eq!(
            decode_message::<ControlRequest>(expected, "golden").unwrap(),
            message
        );
    }

    #[test]
    fn shared_codec_rejects_wrong_channel_version_and_unknown_fields() {
        let mut message = shutdown();
        message.header.channel = Channel::RemoteLoad;
        assert!(encode_message(&message, "wrong channel")
            .unwrap_err()
            .to_string()
            .contains("wrong channel"));
        message.header.channel = Channel::RuntimeControl;
        message.header.protocol_version = VMM_PROTOCOL_VERSION - 1;
        assert!(encode_message(&message, "wrong version")
            .unwrap_err()
            .to_string()
            .contains("unsupported protocol version"));
        let payload = encode_message(&shutdown(), "golden").unwrap();
        let mut value: serde_json::Value = serde_json::from_slice(&payload).unwrap();
        value["header"]["unexpected"] = true.into();
        assert!(decode_message::<ControlRequest>(
            &serde_json::to_vec(&value).unwrap(),
            "unknown field"
        )
        .is_err());
    }
}
