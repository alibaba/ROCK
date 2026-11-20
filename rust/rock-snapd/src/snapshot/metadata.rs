//! Shared snapshot, chain and layer metadata and their logical read contracts.
//!
//! Formats normalize their authenticated descriptors into this common model.
//! Snapshot aggregation and ancestry rules live here; storage discovery,
//! transport clients, upload scheduling and VM lifecycle stay in their owners.

use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;

use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use thiserror::Error;
pub(crate) mod kinds;
pub use kinds::{LayerKind, SnapshotKind};

use crate::snapshot::format::reader::LayerReadCapability;
use crate::snapshot::io::range_io::{P2PSource, RangeReader};
use crate::snapshot::transport::RangeReadError;
use crate::RuntimeIdentity;

// Compatibility exports; each trait is defined once in its owning module.
pub use crate::snapshot::format::{LayerMetadataInterface, LayerViewInterface};
pub use crate::snapshot::{LazyLoadInterface, SnapshotInstance, ValidateCanonicalModel};

// Snapshot publication models.

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StorageDescriptor {
    pub provider: StorageProvider,
    pub endpoint_profile: String,
    pub credential_ref: String,
}

#[derive(Debug, Clone, Copy, Hash, PartialEq, Eq, Serialize, Deserialize)]
pub enum StorageProvider {
    S3,
    OSS,
    NFS,
    Registry,
}

/// Immutable remote source location. It contains no access token or transient
/// URL; format selection remains in the authenticated layer encoding.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase", deny_unknown_fields)]
pub enum SourceRef {
    Registry {
        registry: String,
        repository: String,
        manifest_digest: String,
        platform: String,
        descriptor_sha256: String,
    },
}

impl ValidateCanonicalModel for SourceRef {
    fn validate(&self) -> Result<(), String> {
        let Self::Registry {
            registry,
            repository,
            manifest_digest,
            platform,
            descriptor_sha256,
        } = self;
        validate_sha256(manifest_digest)?;
        validate_sha256(descriptor_sha256)?;
        if registry.is_empty()
            || registry.len() > 255
            || !registry.bytes().all(|byte| {
                byte.is_ascii_lowercase() || byte.is_ascii_digit() || b".-:[]".contains(&byte)
            })
        {
            return Err("invalid Registry source authority".into());
        }
        if repository.is_empty()
            || repository.len() > 1024
            || !repository.split('/').all(|part| {
                !part.is_empty()
                    && part != "."
                    && part != ".."
                    && part.bytes().all(|byte| {
                        byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"._-".contains(&byte)
                    })
            })
        {
            return Err("invalid Registry source repository".into());
        }
        let parts = platform.split('/').collect::<Vec<_>>();
        if !(2..=3).contains(&parts.len())
            || platform.len() > 128
            || !parts.iter().all(|part| {
                !part.is_empty()
                    && part.bytes().all(|byte| {
                        byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"._-".contains(&byte)
                    })
            })
        {
            return Err("invalid Registry source platform".into());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VmmCompatibility {
    pub architecture: String,
    pub vmm_type: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub vmm_state_format_version: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rocklet_artifact: Option<RockletArtifactCompatibility>,
}

/// Immutable guest control-plane artifact required to resume a captured VMM.
///
/// The Rocklet disk is deliberately not part of the mutable block generation,
/// but a FULL snapshot's memory and device state were created while this exact
/// read-only artifact was attached.  Persisting the dependency in the
/// canonical snapshot spec prevents a later node from combining old VMM state
/// with a different Rocklet userspace.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RockletArtifactCompatibility {
    pub identity: String,
    pub version: String,
    pub raw_sha256: String,
    pub logical_bytes: u64,
    pub guest_port: u32,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SnapshotProvenance {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub oci_ref: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub oci_digest: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ObjectRef {
    pub object_key: String,
    pub sha256: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub object_version: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VmStateRef {
    pub object_key: String,
    pub sha256: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub object_version: Option<String>,
    pub stored_bytes: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChainIndexRef {
    pub logical_bytes: u64,
    pub index_object_key: String,
    pub index_sha256: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub object_version: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_ref: Option<SourceRef>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SnapshotSpec {
    pub format_version: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub registry_snapshot: Option<RegistrySnapshotRef>,
    pub compatibility: VmmCompatibility,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub provenance: Option<SnapshotProvenance>,
    pub storage: StorageDescriptor,
    pub manifest: ObjectRef,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub vm_state: Option<VmStateRef>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub memory: Option<ChainIndexRef>,
    pub rootfs: ChainIndexRef,
}

/// Immutable OCI root for a complete snapshot. The private memory platform is
/// a dispatch marker; real CPU and VMM compatibility remain in the bound spec.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RegistrySnapshotRef {
    pub registry: String,
    pub repository: String,
    pub index_digest: String,
}

impl ValidateCanonicalModel for RegistrySnapshotRef {
    fn validate(&self) -> Result<(), String> {
        SourceRef::Registry {
            registry: self.registry.clone(),
            repository: self.repository.clone(),
            manifest_digest: self.index_digest.clone(),
            platform: "linux/amd64".into(),
            descriptor_sha256: self.index_digest.clone(),
        }
        .validate()
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ManifestChainRef {
    pub object_key: String,
    pub sha256: String,
    pub logical_bytes: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub object_version: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SnapshotManifest {
    pub format_version: u64,
    pub snapshot_id: String,
    pub snapshot_kind: SnapshotKind,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub vm_state: Option<VmStateRef>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub memory: Option<ManifestChainRef>,
    pub rootfs: ManifestChainRef,
}

impl ValidateCanonicalModel for SnapshotSpec {
    fn validate(&self) -> Result<(), String> {
        self.validate_publication_binding(true)
    }
}

impl SnapshotSpec {
    /// An upload plan describes its graph before OCI configs and the final
    /// index exist. Published/catalog specs must use strict validation instead.
    pub(crate) fn validate_for_preparation(&self) -> Result<(), String> {
        self.validate_publication_binding(false)
    }

    fn validate_publication_binding(&self, require_registry_root: bool) -> Result<(), String> {
        require_v1(self.format_version)?;
        require_nonempty(
            "compatibility.architecture",
            &self.compatibility.architecture,
            64,
        )?;
        require_nonempty("compatibility.vmm_type", &self.compatibility.vmm_type, 64)?;
        validate_storage(&self.storage)?;
        validate_object_ref(&self.manifest)?;
        validate_chain_ref(&self.rootfs)?;
        if self.storage.provider == StorageProvider::Registry {
            if let Some(reference) = &self.registry_snapshot {
                reference.validate()?;
                if self.rootfs.source_ref.is_some() {
                    return Err("OCI snapshot root cannot also pin a disk-only source".into());
                }
            } else if self.rootfs.source_ref.is_some() {
                if self.memory.is_some() || self.vm_state.is_some() {
                    return Err("Registry image sources must be disk-only bases".into());
                }
            } else if require_registry_root {
                return Err("Registry snapshot requires an immutable OCI index root".into());
            }
        } else if self.rootfs.source_ref.is_some() || self.registry_snapshot.is_some() {
            return Err("Registry source bindings require Registry storage".into());
        }
        if let Some(memory) = &self.memory {
            validate_chain_ref(memory)?;
            if memory.source_ref.is_some() {
                return Err("memory indexes cannot use external image sources".into());
            }
        }
        if let Some(state) = &self.vm_state {
            validate_vm_state_ref(state)?;
        }
        if self.vm_state.is_some() != self.compatibility.vmm_state_format_version.is_some() {
            return Err("vm_state and vmm_state_format_version must be present together".into());
        }
        if self.vm_state.is_some() != self.compatibility.rocklet_artifact.is_some() {
            return Err("FULL snapshots must pin exactly one Rocklet artifact".into());
        }
        if self.vm_state.is_some() && self.compatibility.vmm_type == "any" {
            return Err("vm_state requires a concrete vmm_type".into());
        }
        if let Some(rocklet) = &self.compatibility.rocklet_artifact {
            validate_sha256(&rocklet.identity)?;
            require_nonempty(
                "compatibility.rocklet_artifact.version",
                &rocklet.version,
                128,
            )?;
            if !rocklet.version.bytes().all(|byte| {
                byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-' | b'+')
            }) {
                return Err(
                    "compatibility.rocklet_artifact.version is not a canonical token".into(),
                );
            }
            validate_sha256(&rocklet.raw_sha256)?;
            if rocklet.logical_bytes == 0
                || !rocklet.logical_bytes.is_multiple_of(512)
                || rocklet.guest_port == 0
            {
                return Err(
                    "compatibility.rocklet_artifact geometry or guest port is invalid".into(),
                );
            }
        }
        if let Some(provenance) = &self.provenance {
            if let Some(reference) = &provenance.oci_ref {
                require_nonempty("provenance.oci_ref", reference, 1024)?;
            }
            if let Some(digest) = &provenance.oci_digest {
                validate_sha256(digest)?;
            }
        }
        Ok(())
    }
}

impl ValidateCanonicalModel for SnapshotManifest {
    fn validate(&self) -> Result<(), String> {
        require_v1(self.format_version)?;
        validate_identity("snapshot_id", &self.snapshot_id, 128)?;
        validate_manifest_chain(&self.rootfs)?;
        if let Some(memory) = &self.memory {
            validate_manifest_chain(memory)?;
        }
        if let Some(state) = &self.vm_state {
            validate_vm_state_ref(state)?;
        }
        match self.snapshot_kind {
            SnapshotKind::BaseImage if self.memory.is_some() || self.vm_state.is_some() => {
                Err("BASE_IMAGE manifest cannot contain memory or VM state".into())
            }
            SnapshotKind::Full if self.memory.is_none() || self.vm_state.is_none() => {
                Err("FULL manifest requires memory and VM state".into())
            }
            _ => Ok(()),
        }
    }
}

// Optional image metadata is a rebuildable performance hint. It is kept out
// of SnapshotSpec and SnapshotManifest so learning does not mutate core identity.

pub const MAX_SNAPSHOT_HOTSET_RANGES: usize = 8 * 1024;
pub const MAX_SNAPSHOT_HOTSET_DECODED_BYTES: u64 = 32 * 1024 * 1024;
pub const MAX_SNAPSHOT_IMAGE_METADATA_BYTES: u64 = 2 * 1024 * 1024;

#[derive(Clone, Debug, Eq, PartialEq, Hash)]
pub struct SnapshotMetadataSubject {
    pub snapshot_id: String,
    pub spec_checksum: String,
}

impl SnapshotMetadataSubject {
    pub fn new(
        snapshot_id: impl Into<String>,
        spec_checksum: impl Into<String>,
    ) -> Result<Self, String> {
        let subject = Self {
            snapshot_id: snapshot_id.into(),
            spec_checksum: spec_checksum.into(),
        };
        subject.validate()?;
        Ok(subject)
    }

    pub fn validate(&self) -> Result<(), String> {
        validate_identity("snapshot metadata snapshot_id", &self.snapshot_id, 128)?;
        validate_sha256(&self.spec_checksum)
    }

    pub fn local_identity(&self) -> String {
        format!("{}\n{}", self.snapshot_id, self.spec_checksum)
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Hash, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SnapshotHotRange {
    pub layer_content_sha256: String,
    pub target_offset: u64,
    pub length: u64,
}

impl SnapshotHotRange {
    pub fn validate(&self) -> Result<(), String> {
        // Preserve identities accepted by the existing RCKHOT02 local hints.
        // Actual reads still match the current authenticated layer capability.
        let digest = self
            .layer_content_sha256
            .strip_prefix("sha256:")
            .unwrap_or(&self.layer_content_sha256);
        if digest.len() != 64 || !digest.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err("invalid hotset layer content identity".into());
        }
        if self.length == 0 || self.target_offset.checked_add(self.length).is_none() {
            return Err("invalid hotset range geometry".into());
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SnapshotHotset {
    pub ranges: Vec<SnapshotHotRange>,
}

impl SnapshotHotset {
    pub fn validate(&self) -> Result<(), String> {
        if self.ranges.len() > MAX_SNAPSHOT_HOTSET_RANGES {
            return Err("hotset range count exceeds limit".into());
        }
        let mut bytes = 0_u64;
        for range in &self.ranges {
            range.validate()?;
            bytes = bytes
                .checked_add(range.length)
                .ok_or_else(|| "hotset byte count overflows".to_owned())?;
            if bytes > MAX_SNAPSHOT_HOTSET_DECODED_BYTES {
                return Err("hotset decoded byte count exceeds limit".into());
            }
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SnapshotImageMetadata {
    pub format_version: u64,
    pub snapshot_id: String,
    pub spec_checksum: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub hotset: Option<SnapshotHotset>,
}

impl SnapshotImageMetadata {
    pub fn subject(&self) -> Result<SnapshotMetadataSubject, String> {
        SnapshotMetadataSubject::new(&self.snapshot_id, &self.spec_checksum)
    }

    pub fn validate(&self) -> Result<(), String> {
        require_v1(self.format_version)?;
        self.subject()?;
        if let Some(hotset) = &self.hotset {
            hotset.validate()?;
        }
        Ok(())
    }

    pub fn validate_for(&self, subject: &SnapshotMetadataSubject) -> Result<(), String> {
        subject.validate()?;
        self.validate()?;
        if self.snapshot_id != subject.snapshot_id || self.spec_checksum != subject.spec_checksum {
            return Err("image metadata subject differs from snapshot identity".into());
        }
        Ok(())
    }
}

impl ValidateCanonicalModel for SnapshotImageMetadata {
    fn validate(&self) -> Result<(), String> {
        SnapshotImageMetadata::validate(self)
    }
}

/// Merge hints using the existing old-first policy and exact range identity.
/// Invalid optional hints are ignored; the returned plan is always bounded.
pub fn merge_snapshot_hot_ranges(
    old: &[SnapshotHotRange],
    new: &[SnapshotHotRange],
) -> Vec<SnapshotHotRange> {
    let mut merged = Vec::new();
    let mut seen = HashSet::new();
    let mut bytes = 0_u64;
    for range in old.iter().chain(new.iter()) {
        if range.validate().is_err()
            || seen.contains(range)
            || merged.len() >= MAX_SNAPSHOT_HOTSET_RANGES
        {
            continue;
        }
        let Some(next) = bytes.checked_add(range.length) else {
            break;
        };
        if next > MAX_SNAPSHOT_HOTSET_DECODED_BYTES {
            break;
        }
        bytes = next;
        seen.insert(range.clone());
        merged.push(range.clone());
    }
    merged
}

// Logical layer coverage and chain invariants.

pub const MAX_CHAIN_DEPTH: usize = 64;
pub const MAX_RANGES_PER_LAYER: usize = 1024 * 1024;
pub const MEMORY_LOAD_UNIT_BYTES: u64 = 4096;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum RangeMapEncoding {
    /// Standard sealed LSMT blob plus authenticated metadata and DATA hashes.
    OverlaybdLsmtV1,
    /// Standard ZFile-LZ4 container around a sealed LSMT byte stream.
    OverlaybdZfileLz4V1,
    /// External native rootfs: pinned Registry source with ZFile block CRC.
    RegistryOverlaybdV1,
}

impl RangeMapEncoding {
    pub const fn descriptor_name(self) -> &'static str {
        match self {
            Self::OverlaybdLsmtV1 => "overlaybd.json",
            Self::OverlaybdZfileLz4V1 => "overlaybd-zfile-lz4.json",
            Self::RegistryOverlaybdV1 => "registry-overlaybd.json",
        }
    }

    pub const fn committed_name(self) -> &'static str {
        match self {
            Self::OverlaybdLsmtV1 => "COMMITTED.overlaybd",
            Self::OverlaybdZfileLz4V1 => "COMMITTED.overlaybd-zfile-lz4",
            Self::RegistryOverlaybdV1 => "COMMITTED.registry-overlaybd",
        }
    }
}

impl LayerKind {
    pub const fn object_key_component(self) -> &'static str {
        match self {
            Self::Memory => "memory",
            Self::Block => "block",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LayerRangeMapRef {
    pub object_key: String,
    pub sha256: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub object_version: Option<String>,
    /// Persistent layers require an explicit supported OverlayBD encoding.
    /// An absent discriminator is rejected, never inferred from the payload.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub encoding: Option<RangeMapEncoding>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_ref: Option<SourceRef>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LayerRef {
    pub ordinal: u64,
    pub layer_id: String,
    pub format_version: u64,
    pub content_sha256: String,
    pub range_map: LayerRangeMapRef,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LayerChainIndex {
    pub format_version: u64,
    pub kind: LayerKind,
    pub logical_bytes: u64,
    pub unit_size_bytes: u64,
    pub layers: Vec<LayerRef>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DataSource {
    pub object_key: String,
    pub object_sha256: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub object_version: Option<String>,
    pub pack_offset: u64,
    pub encoded_length: u64,
    pub decoded_length: u64,
    pub codec: DataCodec,
    pub checksum_sha256: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum DataCodec {
    Raw,
    /// Logical rootfs bytes assembled from trusted Registry ZFile-CRC layers.
    #[serde(rename = "registry-rootfs-v1")]
    RegistryRootfs,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum RangeKind {
    Data,
    Zero,
    Inherit,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LayerRange {
    pub kind: RangeKind,
    pub logical_offset: u64,
    pub length: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source: Option<DataSource>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LayerRangeMap {
    pub format_version: u64,
    pub kind: LayerKind,
    pub content_sha256: String,
    pub logical_bytes: u64,
    pub unit_size_bytes: u64,
    pub ranges: Vec<LayerRange>,
    /// Authenticated storage container metadata, pinned with the decoded map.
    /// DATA offsets and codecs address the inner LSMT stream independently of
    /// the compressed object's physical identity and storage length.
    #[serde(skip)]
    pub zfile: Option<Arc<crate::snapshot::format::overlaybd::zfile::ZfileIndex>>,
    /// Capability restored only from an authenticated external-rootfs descriptor.
    #[serde(skip)]
    pub registry: Option<Arc<crate::snapshot::format::overlaybd::composite::RegistryRootfs>>,
    /// Source-bound physical routing restored from the authenticated OCI root.
    /// Retaining it in the map also preserves authority across local forks.
    #[serde(skip)]
    pub registry_objects: Option<Arc<crate::snapshot::format::overlaybd_oci::RegistryObjectMap>>,
}

impl ValidateCanonicalModel for LayerChainIndex {
    fn validate(&self) -> Result<(), String> {
        require_v1(self.format_version)?;
        require_geometry(self.logical_bytes, self.unit_size_bytes)?;
        if self.kind == LayerKind::Memory && self.unit_size_bytes != MEMORY_LOAD_UNIT_BYTES {
            return Err("memory chain unit size must be 4096 bytes".into());
        }
        if self.layers.is_empty() || self.layers.len() > MAX_CHAIN_DEPTH {
            return Err(format!("chain depth must be 1..={MAX_CHAIN_DEPTH}"));
        }
        let mut ids = HashSet::with_capacity(self.layers.len());
        for (ordinal, layer) in self.layers.iter().enumerate() {
            if let Some(source) = &layer.range_map.source_ref {
                source.validate()?;
                let SourceRef::Registry {
                    descriptor_sha256, ..
                } = source;
                if self.kind != LayerKind::Block
                    || ordinal != 0
                    || layer.range_map.encoding != Some(RangeMapEncoding::RegistryOverlaybdV1)
                    || layer.range_map.object_version.is_some()
                    || descriptor_sha256 != &layer.range_map.sha256
                    || descriptor_sha256 != &layer.content_sha256
                {
                    return Err(
                        "external layer source conflicts with its root descriptor identity".into(),
                    );
                }
            }
            if layer.ordinal != ordinal as u64 {
                return Err("chain layer ordinals must be contiguous root-to-leaf order".into());
            }
            require_nonempty("layer_id", &layer.layer_id, 256)?;
            require_v1(layer.format_version)?;
            validate_sha256(&layer.content_sha256)?;
            validate_object_ref(&ObjectRef {
                object_key: layer.range_map.object_key.clone(),
                sha256: layer.range_map.sha256.clone(),
                object_version: layer.range_map.object_version.clone(),
            })?;
            if !ids.insert(&layer.layer_id) {
                return Err("chain layer IDs must be unique".into());
            }
            let content = layer.content_sha256.trim_start_matches("sha256:");
            let suffix = required_layer_encoding(layer.range_map.encoding)?.descriptor_name();
            let expected = format!(
                "layers/{}/{}/{content}/{suffix}",
                self.kind.object_key_component(),
                layer.format_version
            );
            if layer.range_map.object_key != expected {
                return Err(format!("layer range-map object key must be {expected:?}"));
            }
        }
        Ok(())
    }
}

impl ValidateCanonicalModel for LayerRangeMap {
    fn validate(&self) -> Result<(), String> {
        if let Some(registry) = &self.registry {
            return registry.validate_map(self);
        }
        if self.ranges.iter().any(|range| {
            range
                .source
                .as_ref()
                .is_some_and(|source| source.codec == DataCodec::RegistryRootfs)
        }) {
            return Err("Registry rootfs DATA requires its authenticated source capability".into());
        }
        require_v1(self.format_version)?;
        validate_sha256(&self.content_sha256)?;
        require_geometry(self.logical_bytes, self.unit_size_bytes)?;
        if self.kind == LayerKind::Memory && self.unit_size_bytes != MEMORY_LOAD_UNIT_BYTES {
            return Err("memory range-map unit size must be 4096 bytes".into());
        }
        if self.ranges.is_empty() || self.ranges.len() > MAX_RANGES_PER_LAYER {
            return Err(format!("range count must be 1..={MAX_RANGES_PER_LAYER}"));
        }
        let mut cursor = 0_u64;
        let mut previous_non_data = None;
        for range in &self.ranges {
            if range.logical_offset != cursor {
                return Err(
                    "layer ranges must completely and contiguously cover logical space".into(),
                );
            }
            if range.length == 0
                || range.logical_offset % self.unit_size_bytes != 0
                || range.length % self.unit_size_bytes != 0
            {
                return Err("layer range must be non-empty and aligned to unit size".into());
            }
            cursor = cursor
                .checked_add(range.length)
                .ok_or_else(|| "layer range overflows u64".to_string())?;
            if cursor > self.logical_bytes {
                return Err("layer range exceeds logical size".into());
            }
            match (range.kind, &range.source) {
                (RangeKind::Data, Some(source)) => {
                    if self.kind == LayerKind::Memory && range.length != MEMORY_LOAD_UNIT_BYTES {
                        return Err(
                            "memory DATA ranges must be independently verifiable 4096-byte units"
                                .into(),
                        );
                    }
                    validate_source(source, range.length)?;
                    previous_non_data = None;
                }
                (RangeKind::Data, None) => return Err("DATA range requires a source".into()),
                (kind, None) => {
                    if previous_non_data == Some(kind) {
                        return Err("adjacent equivalent ranges must be coalesced".into());
                    }
                    previous_non_data = Some(kind);
                }
                (_, Some(_)) => return Err("ZERO/INHERIT range must not contain a source".into()),
            }
        }
        if cursor != self.logical_bytes {
            return Err("layer ranges must completely cover logical space".into());
        }
        let actual = compute_layer_content_sha256(self).map_err(|error| error.to_string())?;
        if actual != self.content_sha256
            && legacy_layer_content_sha256(self).map_err(|error| error.to_string())?
                != self.content_sha256
        {
            return Err(format!(
                "layer content identity mismatch: expected {}, got {actual}",
                self.content_sha256
            ));
        }
        Ok(())
    }
}

pub fn compute_layer_content_sha256(layer: &LayerRangeMap) -> Result<String, CanonicalModelError> {
    if let Some(registry) = &layer.registry {
        return registry
            .content_sha256()
            .map_err(CanonicalModelError::Encode);
    }
    // Domain-separated binary logical identity v2. Physical pack placement,
    // object keys and versions intentionally do not participate. Stream into
    // SHA256: no per-range JSON objects, strings, or whole-layer byte buffer.
    let mut hash = Sha256::new();
    hash.update(b"ROCK-LAYER-CONTENT\0\x02");
    hash.update(layer.format_version.to_le_bytes());
    hash.update([match layer.kind {
        LayerKind::Memory => 0,
        LayerKind::Block => 1,
    }]);
    hash.update(layer.logical_bytes.to_le_bytes());
    hash.update(layer.unit_size_bytes.to_le_bytes());
    hash.update((layer.ranges.len() as u64).to_le_bytes());
    for range in &layer.ranges {
        hash.update(range.logical_offset.to_le_bytes());
        hash.update(range.length.to_le_bytes());
        hash.update([match range.kind {
            RangeKind::Data => 0,
            RangeKind::Zero => 1,
            RangeKind::Inherit => 2,
        }]);
        hash.update([u8::from(range.source.is_some())]);
        if let Some(source) = &range.source {
            validate_sha256(&source.checksum_sha256).map_err(CanonicalModelError::Encode)?;
            let hex = &source.checksum_sha256[7..];
            let mut checksum = [0_u8; 32];
            for (index, byte) in checksum.iter_mut().enumerate() {
                *byte = u8::from_str_radix(&hex[index * 2..index * 2 + 2], 16)
                    .map_err(|error| CanonicalModelError::Encode(error.to_string()))?;
            }
            hash.update(checksum);
            hash.update([match source.codec {
                DataCodec::Raw => 0,
                DataCodec::RegistryRootfs => {
                    return Err(CanonicalModelError::Encode(
                        "Registry codec requires a source capability".into(),
                    ));
                }
            }]);
            hash.update(source.decoded_length.to_le_bytes());
            hash.update(source.encoded_length.to_le_bytes());
        }
    }
    Ok(format!("sha256:{:x}", hash.finalize()))
}

/// Read compatibility only for pre-binary-identity artifacts. This streams
/// the historical canonical spelling into SHA256 without a JSON DOM/buffer.
/// New capture and successful v2 validation never call this function.
fn legacy_layer_content_sha256(layer: &LayerRangeMap) -> Result<String, CanonicalModelError> {
    use std::io::Write;
    struct HashWriter(Sha256);
    impl Write for HashWriter {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            self.0.update(bytes);
            Ok(bytes.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    let mut writer = HashWriter(Sha256::new());
    let kind = match layer.kind {
        LayerKind::Memory => "MEMORY",
        LayerKind::Block => "BLOCK",
    };
    write!(writer, "{{\"format_version\":{},\"identity_schema\":\"range-map-v1\",\"kind\":\"{kind}\",\"logical_bytes\":{},\"ranges\":[", layer.format_version, layer.logical_bytes).unwrap();
    for (index, range) in layer.ranges.iter().enumerate() {
        if index != 0 {
            writer.write_all(b",").unwrap();
        }
        let kind = match range.kind {
            RangeKind::Data => "DATA",
            RangeKind::Zero => "ZERO",
            RangeKind::Inherit => "INHERIT",
        };
        write!(
            writer,
            "{{\"kind\":\"{kind}\",\"length\":{},\"logical_offset\":{}",
            range.length, range.logical_offset
        )
        .unwrap();
        if let Some(source) = &range.source {
            // Only a validated ASCII digest is interpolated into the legacy spelling.
            validate_sha256(&source.checksum_sha256).map_err(CanonicalModelError::Encode)?;
            let codec = match source.codec {
                DataCodec::Raw => "raw",
                DataCodec::RegistryRootfs => {
                    return Err(CanonicalModelError::Encode(
                        "Registry codec has no legacy encoding".into(),
                    ));
                }
            };
            write!(writer, ",\"source\":{{\"checksum_sha256\":\"{}\",\"codec\":\"{codec}\",\"decoded_length\":{},\"encoded_length\":{}}}", source.checksum_sha256, source.decoded_length, source.encoded_length).unwrap();
        }
        writer.write_all(b"}").unwrap();
    }
    writeln!(writer, "],\"unit_size_bytes\":{}}}", layer.unit_size_bytes).unwrap();
    Ok(format!("sha256:{:x}", writer.0.finalize()))
}

// Canonical metadata and identity validation.

pub const FORMAT_VERSION_V1: u64 = 1;
pub const MAX_DATA_RANGE_BYTES: u64 = 16 * 1024 * 1024;
// A canonical-v1 MEMORY DATA entry is independently verifiable at 4 KiB
// granularity and is roughly 0.5 KiB once its pack identity and checksum are
// encoded. Keep this bound large enough for MAX_RANGES_PER_LAYER entries while
// retaining a finite parser/allocation ceiling.
pub const MAX_RANGE_MAP_BYTES: usize = 512 * 1024 * 1024;
pub const MAX_SNAPSHOT_METADATA_BYTES: usize = 1024 * 1024 * 1024;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum CanonicalModelError {
    #[error("invalid {label} JSON: {message}")]
    InvalidJson { label: String, message: String },
    #[error("invalid {label}: {message}")]
    InvalidModel { label: String, message: String },
    #[error("{0} is not canonical JSON")]
    NonCanonical(String),
    #[error("cannot encode canonical JSON: {0}")]
    Encode(String),
}

/// Admin/snapd request identity intentionally excludes the trailing newline
/// used by immutable snapshot objects. This mirrors Python's sorted compact
/// JSON checksum contract exactly.
pub fn snapshot_spec_checksum(value: &Value) -> Result<String, CanonicalModelError> {
    let mut payload = canonical_json_bytes(value)?;
    if payload.pop() != Some(b'\n') {
        return Err(CanonicalModelError::Encode(
            "canonical JSON encoder omitted its trailing newline".into(),
        ));
    }
    Ok(format!("sha256:{:x}", Sha256::digest(payload)))
}

pub fn parse_canonical_model<T>(payload: &[u8], label: &str) -> Result<T, CanonicalModelError>
where
    T: DeserializeOwned + Serialize + ValidateCanonicalModel,
{
    let parsed: T =
        serde_json::from_slice(payload).map_err(|error| CanonicalModelError::InvalidJson {
            label: label.to_owned(),
            message: error.to_string(),
        })?;
    parsed
        .validate()
        .map_err(|message| CanonicalModelError::InvalidModel {
            label: label.to_owned(),
            message,
        })?;
    let canonical = canonical_json_bytes(&parsed)?;
    if payload != canonical {
        return Err(CanonicalModelError::NonCanonical(label.to_owned()));
    }
    Ok(parsed)
}

pub fn canonical_json_bytes<T: Serialize>(value: &T) -> Result<Vec<u8>, CanonicalModelError> {
    let value = serde_json::to_value(value)
        .map_err(|error| CanonicalModelError::Encode(error.to_string()))?;
    let mut payload = serde_json::to_vec(&value)
        .map_err(|error| CanonicalModelError::Encode(error.to_string()))?;
    payload.push(b'\n');
    Ok(payload)
}

pub fn sha256_identity(payload: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(payload))
}

pub(in crate::snapshot) fn require_v1(version: u64) -> Result<(), String> {
    if version == FORMAT_VERSION_V1 {
        Ok(())
    } else {
        Err(format!("unsupported format_version {version}"))
    }
}

pub(in crate::snapshot) fn require_nonempty(
    label: &str,
    value: &str,
    maximum: usize,
) -> Result<(), String> {
    if value.is_empty() || value.len() > maximum {
        Err(format!("{label} length must be 1..={maximum}"))
    } else {
        Ok(())
    }
}

pub(in crate::snapshot) fn validate_identity(
    label: &str,
    value: &str,
    maximum: usize,
) -> Result<(), String> {
    require_nonempty(label, value, maximum)?;
    let mut chars = value.chars();
    let valid_first = chars
        .next()
        .is_some_and(|value| value.is_ascii_alphanumeric());
    let valid_rest =
        chars.all(|value| value.is_ascii_alphanumeric() || matches!(value, '.' | '_' | '-'));
    if valid_first && valid_rest {
        Ok(())
    } else {
        Err(format!("{label} contains invalid characters"))
    }
}

pub(in crate::snapshot) fn validate_storage(storage: &StorageDescriptor) -> Result<(), String> {
    require_nonempty("storage.endpoint_profile", &storage.endpoint_profile, 128)?;
    require_nonempty("storage.credential_ref", &storage.credential_ref, 256)
}

pub(in crate::snapshot) fn validate_object_ref(reference: &ObjectRef) -> Result<(), String> {
    require_nonempty("object_key", &reference.object_key, 4096)?;
    validate_sha256(&reference.sha256)?;
    if let Some(version) = &reference.object_version {
        require_nonempty("object_version", version, 1024)?;
    }
    Ok(())
}

pub(in crate::snapshot) fn validate_vm_state_ref(reference: &VmStateRef) -> Result<(), String> {
    validate_object_ref(&ObjectRef {
        object_key: reference.object_key.clone(),
        sha256: reference.sha256.clone(),
        object_version: reference.object_version.clone(),
    })?;
    if reference.stored_bytes == 0 {
        return Err("VM state stored_bytes must be positive".into());
    }
    Ok(())
}

pub(in crate::snapshot) fn validate_chain_ref(reference: &ChainIndexRef) -> Result<(), String> {
    if let Some(source) = &reference.source_ref {
        source.validate()?;
        if reference.object_version.is_some() {
            return Err("external source index cannot claim an object store version".into());
        }
    }
    if reference.logical_bytes == 0 {
        return Err("chain logical_bytes must be positive".into());
    }
    require_nonempty("index_object_key", &reference.index_object_key, 4096)?;
    validate_sha256(&reference.index_sha256)?;
    if let Some(version) = &reference.object_version {
        require_nonempty("object_version", version, 1024)?;
    }
    Ok(())
}

pub(in crate::snapshot) fn validate_manifest_chain(
    reference: &ManifestChainRef,
) -> Result<(), String> {
    if reference.logical_bytes == 0 {
        return Err("manifest chain logical_bytes must be positive".into());
    }
    require_nonempty("manifest chain object_key", &reference.object_key, 4096)?;
    validate_sha256(&reference.sha256)
}

pub(crate) fn validate_sha256(value: &str) -> Result<(), String> {
    let hex = value.strip_prefix("sha256:").unwrap_or_default();
    if hex.len() == 64
        && hex
            .bytes()
            .all(|value| value.is_ascii_digit() || (b'a'..=b'f').contains(&value))
    {
        Ok(())
    } else {
        Err(format!("invalid sha256 identity {value:?}"))
    }
}

pub(in crate::snapshot) fn require_geometry(
    logical_bytes: u64,
    unit_size_bytes: u64,
) -> Result<(), String> {
    if logical_bytes == 0 || unit_size_bytes == 0 || !logical_bytes.is_multiple_of(unit_size_bytes)
    {
        Err("logical size must be positive and aligned to unit size".into())
    } else {
        Ok(())
    }
}

pub(in crate::snapshot) fn validate_source(
    source: &DataSource,
    range_length: u64,
) -> Result<(), String> {
    require_nonempty("DATA object_key", &source.object_key, 4096)?;
    validate_sha256(&source.object_sha256)?;
    validate_sha256(&source.checksum_sha256)?;
    if source.encoded_length == 0 || source.encoded_length > MAX_DATA_RANGE_BYTES {
        return Err(format!(
            "encoded DATA length must be 1..={MAX_DATA_RANGE_BYTES}"
        ));
    }
    if source.decoded_length != range_length || source.encoded_length != range_length {
        return Err("raw DATA encoded/decoded length must equal logical range length".into());
    }
    source
        .pack_offset
        .checked_add(source.encoded_length)
        .ok_or_else(|| "DATA pack range overflows u64".to_string())?;
    Ok(())
}

// Opened snapshot aggregation and common lazy-read capabilities.

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SnapshotLayerMetadata {
    pub ordinal: u64,
    pub layer_id: String,
    /// An opaque identity: local frozen generations may use provisional IDs.
    pub content_identity: String,
    pub format: String,
    pub logical_bytes: u64,
    pub unit_size_bytes: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SnapshotChainMetadata {
    pub kind: LayerKind,
    pub logical_bytes: u64,
    pub unit_size_bytes: u64,
    /// Logical layers in root-to-leaf order, not physical blob count.
    pub layers: Vec<SnapshotLayerMetadata>,
}

/// Codec-independent layer view. Persistent formats normalize into this IR;
/// VM managers consume coverage without parsing an on-disk descriptor.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SnapshotLayerView {
    pub layer_ref: LayerRef,
    pub range_map: Arc<LayerRangeMap>,
}

impl SnapshotLayerView {
    pub fn reader(&self) -> Result<Arc<dyn LayerViewInterface>, LazyLoadError> {
        let encoding = self
            .layer_ref
            .range_map
            .encoding
            .ok_or(LazyLoadError::UnsupportedCodec)?;
        Ok(LayerReadCapability::for_encoding(
            Arc::clone(&self.range_map),
            encoding,
        ))
    }

    pub fn allows_p2p(&self) -> bool {
        self.range_map.registry.is_none()
            && self.range_map.registry_objects.is_none()
            && self.layer_ref.range_map.encoding.is_some_and(|encoding| {
                crate::snapshot::format::format_for_encoding(encoding).allows_p2p()
            })
    }

    pub fn allows_background_prefetch(&self) -> bool {
        self.range_map.registry.is_none()
            && self.layer_ref.range_map.encoding.is_some_and(|encoding| {
                crate::snapshot::format::format_for_encoding(encoding).allows_background_prefetch()
            })
    }
}

/// Ordered common coverage, independent of its source descriptor codec.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SnapshotChainView {
    pub index: LayerChainIndex,
    pub layers: Vec<SnapshotLayerView>,
}

impl SnapshotChainView {
    pub fn metadata(&self) -> Result<SnapshotChainMetadata, String> {
        Ok(SnapshotChainMetadata {
            kind: self.index.kind,
            logical_bytes: self.index.logical_bytes,
            unit_size_bytes: self.index.unit_size_bytes,
            layers: self
                .index
                .layers
                .iter()
                .map(|layer| {
                    Ok(SnapshotLayerMetadata {
                        ordinal: layer.ordinal,
                        layer_id: layer.layer_id.clone(),
                        content_identity: layer.content_sha256.clone(),
                        format: encoding_name(layer.range_map.encoding)?.into(),
                        logical_bytes: self.index.logical_bytes,
                        unit_size_bytes: self.index.unit_size_bytes,
                    })
                })
                .collect::<Result<Vec<_>, String>>()?,
        })
    }

    pub fn layer_reader(
        &self,
        identity: &str,
    ) -> Result<Arc<dyn LazyLoadInterface>, LazyLoadError> {
        let layer = self
            .layers
            .iter()
            .find(|layer| {
                layer.range_map.content_sha256.trim_start_matches("sha256:")
                    == identity.trim_start_matches("sha256:")
            })
            .ok_or(LazyLoadError::IdentityConflict)?;
        layer
            .reader()
            .map(|reader| reader as Arc<dyn LazyLoadInterface>)
    }
}

pub(crate) fn required_layer_encoding(
    encoding: Option<RangeMapEncoding>,
) -> Result<RangeMapEncoding, String> {
    encoding.ok_or_else(|| "persistent layer encoding is required".into())
}

pub fn encoding_name(encoding: Option<RangeMapEncoding>) -> Result<&'static str, String> {
    Ok(crate::snapshot::format::format_for_encoding(required_layer_encoding(encoding)?).name())
}

/// A read addresses one logical layer and its logical bytes. Object keys,
/// physical offsets and format-specific index types are deliberately absent.
#[derive(Clone, Copy, Debug)]
pub struct SnapshotReadRange<'a> {
    pub kind: LayerKind,
    pub layer_content_sha256: &'a str,
    pub logical_offset: u64,
    pub length: u64,
}

/// The runtime/P2P binding is opaque to format callers. A format never writes
/// a backing or manipulates VM state; this context only authorizes byte reads.
pub struct LazyLoadContext {
    pub(in crate::snapshot) provider: Arc<dyn RangeReader>,
    pub(in crate::snapshot) runtime: RuntimeIdentity,
    pub(in crate::snapshot) p2p_source: Option<P2PSource>,
    pub(in crate::snapshot) provider_timeout: Duration,
}

impl LazyLoadContext {
    pub(crate) fn new(
        provider: Arc<dyn RangeReader>,
        runtime: RuntimeIdentity,
        p2p_source: Option<P2PSource>,
    ) -> Self {
        Self {
            provider,
            runtime,
            p2p_source,
            provider_timeout: Duration::from_secs(30),
        }
    }

    pub(crate) fn with_provider_timeout(mut self, timeout: Duration) -> Self {
        self.provider_timeout = timeout;
        self
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct LazyReadAccounting {
    pub cache_bytes: u64,
    pub provider_bytes: u64,
    pub p2p_bytes: u64,
    pub registry_logical_bytes: u64,
}

#[derive(Debug)]
pub struct LazyReadResult {
    pub payload: Vec<u8>,
    pub accounting: LazyReadAccounting,
}

#[derive(Debug, Error)]
pub enum LazyLoadError {
    #[error("logical DATA range is invalid")]
    InvalidRange,
    #[error("logical range contains ZERO or INHERIT, not DATA")]
    NonDataRange,
    #[error("logical layer identity conflicts with the read")]
    IdentityConflict,
    #[error("unsupported DATA codec")]
    UnsupportedCodec,
    #[error("immutable DATA read returned too few bytes")]
    ShortRead,
    #[error("payload checksum mismatch")]
    ChecksumMismatch,
    #[error("local raw DATA read failed: {0}")]
    LocalIo(String),
    #[error(transparent)]
    Provider(#[from] RangeReadError),
}
