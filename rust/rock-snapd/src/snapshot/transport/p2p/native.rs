//! Native logical-layer peer identities. These are separate from published
//! object SHA identities and carry no storage codec or provider coordinates.

use serde::{Deserialize, Serialize};

use super::kind_models::{LayerKind, SnapshotKind};
use sha2::{Digest, Sha256};

pub const NATIVE_P2P_METADATA_VERSION: u64 = 1;
pub const MAX_NATIVE_COVERAGE_UNITS: u64 = 4096;
pub const MAX_NATIVE_CHAIN_DEPTH: usize = 64;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NativeLayerIdentityKind {
    NativeGeneration,
    CanonicalContent,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NativeLayerMetadata {
    pub kind: LayerKind,
    pub ordinal: u32,
    pub layer_id: String,
    pub layer_generation_id: String,
    pub identity_kind: NativeLayerIdentityKind,
    /// Native generations use an explicit `native-generation:` identity;
    /// only CanonicalContent values assert an authenticated content SHA.
    pub content_identity: String,
    pub logical_bytes: u64,
    pub coverage_unit_size: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NativeSnapshotMetadata {
    pub metadata_version: u64,
    pub snapshot_id: String,
    pub snapshot_kind: SnapshotKind,
    pub generation_id: String,
    /// Checksum of immutable descriptors, never a checksum of layer DATA.
    pub metadata_sha256: String,
    pub layers: Vec<NativeLayerMetadata>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NativeCoverageRequest {
    pub generation_id: String,
    pub kind: LayerKind,
    pub ordinal: u32,
    pub layer_id: String,
    pub layer_generation_id: String,
    pub offset: u64,
    pub length: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NativeCoverageResult {
    pub offset: u64,
    pub length: u64,
    pub unit_size: u64,
    /// Two bits per unit: 0 INHERIT, 1 DATA, 2 ZERO. Trailing bits are zero.
    pub packed_states: Vec<u8>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NativeReadRangeRequest {
    pub generation_id: String,
    pub kind: LayerKind,
    pub ordinal: u32,
    pub layer_id: String,
    pub layer_generation_id: String,
    pub offset: u64,
    pub length: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NativeAvailabilityResult {
    pub snapshot_id: String,
    pub generation_id: String,
    pub available: bool,
    pub complete: bool,
    pub servable_layers: usize,
}

impl NativeSnapshotMetadata {
    pub fn descriptor_checksum(&self) -> Result<String, String> {
        let bytes = serde_json::to_vec(&(
            "snapd-native-descriptors-v1",
            self.metadata_version,
            &self.snapshot_id,
            self.snapshot_kind,
            &self.generation_id,
            &self.layers,
        ))
        .map_err(|error| error.to_string())?;
        Ok(format!("sha256:{:x}", Sha256::digest(&bytes)))
    }

    pub fn validate(&self) -> Result<(), String> {
        if self.metadata_version != NATIVE_P2P_METADATA_VERSION
            || self.snapshot_id.is_empty()
            || self.snapshot_id.len() > 4096
            || uuid::Uuid::parse_str(&self.generation_id).is_err()
            || self.layers.is_empty()
            || self.layers.len() > 2 * MAX_NATIVE_CHAIN_DEPTH
            || self.descriptor_checksum()? != self.metadata_sha256
        {
            return Err("native descriptor identity or checksum is invalid".into());
        }
        let mut ordinals = std::collections::HashMap::new();
        for layer in &self.layers {
            let ordinal = ordinals.entry(layer.kind).or_insert(0_u32);
            if layer.ordinal != *ordinal
                || layer.ordinal as usize >= MAX_NATIVE_CHAIN_DEPTH
                || layer.layer_id.is_empty()
                || layer.layer_id.len() > 4096
                || layer.logical_bytes == 0
                || layer.coverage_unit_size == 0
                || !layer.logical_bytes.is_multiple_of(layer.coverage_unit_size)
                || !match (layer.kind, layer.identity_kind) {
                    (LayerKind::Memory, _) => layer.coverage_unit_size == 4096,
                    (LayerKind::Block, NativeLayerIdentityKind::NativeGeneration) => {
                        layer.coverage_unit_size == 65536
                    }
                    (LayerKind::Block, NativeLayerIdentityKind::CanonicalContent) => {
                        matches!(layer.coverage_unit_size, 512 | 65536)
                    }
                }
            {
                return Err("native layer descriptor geometry is invalid".into());
            }
            *ordinal = ordinal
                .checked_add(1)
                .ok_or("native layer ordinal overflows")?;
            match layer.identity_kind {
                NativeLayerIdentityKind::NativeGeneration => {
                    if uuid::Uuid::parse_str(&layer.layer_generation_id).is_err()
                        || layer.content_identity
                            != format!("native-generation:{}", layer.layer_generation_id)
                    {
                        return Err("native generation must not assert a content SHA".into());
                    }
                }
                NativeLayerIdentityKind::CanonicalContent => {
                    let hex = layer
                        .content_identity
                        .strip_prefix("sha256:")
                        .unwrap_or_default();
                    if hex.len() != 64
                        || !hex
                            .bytes()
                            .all(|value| value.is_ascii_digit() || (b'a'..=b'f').contains(&value))
                    {
                        return Err("canonical layer content identity is not a SHA-256".into());
                    }
                    if layer.layer_generation_id != layer.content_identity {
                        return Err(
                            "canonical layer generation differs from its content identity".into(),
                        );
                    }
                }
            }
        }
        Ok(())
    }
}
