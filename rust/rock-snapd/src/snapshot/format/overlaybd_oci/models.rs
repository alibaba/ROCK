//! Immutable OCI envelope metadata. The root index identity is attached only
//! after encoding, so no config contains the digest of its own parent index.

use std::collections::{HashMap, HashSet};

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::snapshot::metadata::{
    canonical_json_bytes, sha256_identity, RegistrySnapshotRef, SnapshotSpec, SourceRef,
    StorageProvider,
};
use crate::snapshot::transport::BlobRef;
use crate::snapshot::ValidateCanonicalModel;

pub(crate) const OCI_INDEX: &str = "application/vnd.oci.image.index.v1+json";
pub(crate) const OCI_MANIFEST: &str = "application/vnd.oci.image.manifest.v1+json";
pub(crate) const OCI_CONFIG: &str = "application/vnd.oci.image.config.v1+json";
pub(crate) const NATIVE_ARTIFACT: &str = "application/vnd.containerd.overlaybd.native.v1+json";
pub(crate) const NATIVE_LAYER: &str = "application/vnd.oci.image.layer.v1.tar";
pub(crate) const AUXILIARY_LAYER: &str = "application/vnd.rock.snapd.metadata.v1";
pub(crate) const VM_STATE_LAYER: &str = "application/vnd.rock.snapd.vm-state.v1";
pub(crate) const MEMORY_ARCHITECTURE: &str = "snapd-memory-amd64";
pub(crate) const SNAPSHOT_FORMAT: &str = "org.rock.snapd.snapshot.format";
pub(crate) const SNAPSHOT_ID: &str = "org.rock.snapd.snapshot.id";
pub(crate) const METADATA_DIGEST: &str = "org.rock.snapd.snapshot.metadata.digest";
pub(crate) const LAYER_ROLE: &str = "org.rock.snapd.layer.role";
pub(crate) const MAX_SNAPSHOT_OBJECTS: usize = 1024;
pub(crate) const MAX_OCI_METADATA_BYTES: usize = 16 * 1024 * 1024;

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct RegistryObjectBinding {
    pub object_key: String,
    pub blob: BlobRef,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub object_version: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct RegistryEmbeddedSource {
    pub source_ref: SourceRef,
    pub descriptor_base64: String,
}

impl RegistryEmbeddedSource {
    pub(crate) fn new(source_ref: SourceRef, descriptor: Vec<u8>) -> Result<Self, String> {
        let source = Self {
            source_ref,
            descriptor_base64: STANDARD.encode(descriptor),
        };
        source.decode()?;
        Ok(source)
    }

    pub(crate) fn decode(&self) -> Result<Vec<u8>, String> {
        self.source_ref.validate()?;
        if self.descriptor_base64.len() > MAX_OCI_METADATA_BYTES.div_ceil(3) * 4 {
            return Err("Embedded Registry descriptor exceeds the metadata bound".into());
        }
        let bytes = STANDARD
            .decode(&self.descriptor_base64)
            .map_err(|_| "Embedded Registry descriptor is not base64".to_owned())?;
        let SourceRef::Registry {
            descriptor_sha256, ..
        } = &self.source_ref;
        if bytes.len() > MAX_OCI_METADATA_BYTES
            || STANDARD.encode(&bytes) != self.descriptor_base64
            || sha256_identity(&bytes) != *descriptor_sha256
        {
            return Err("Embedded Registry descriptor conflicts with its source".into());
        }
        Ok(bytes)
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct RegistryMemoryLayout {
    pub logical_bytes: u64,
    pub unit_size_bytes: u64,
    pub regions: Vec<RegistryMemoryRegion>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct RegistryMemoryRegion {
    pub guest_phys_addr: u64,
    pub logical_offset: u64,
    pub length: u64,
}

pub(crate) fn memory_layout_from_state(
    spec: &SnapshotSpec,
    payload: &[u8],
) -> Result<RegistryMemoryLayout, String> {
    let memory = spec
        .memory
        .as_ref()
        .ok_or("Memory layout requires a memory chain")?;
    let state_ref = spec
        .vm_state
        .as_ref()
        .ok_or("Memory layout requires a VMM state reference")?;
    if state_ref.sha256 != sha256_identity(payload)
        || state_ref.stored_bytes != payload.len() as u64
    {
        return Err("VMM state bytes differ from the bound snapshot state".into());
    }
    let state: Value = serde_json::from_slice(payload).map_err(|_| "VMM state pack is not JSON")?;
    if state["magic"] != "ROCK_VM_STATE"
        || state["format_version"].as_u64() != spec.compatibility.vmm_state_format_version
        || state["format_version"] != 1
    {
        return Err("VMM state pack format differs from snapshot compatibility".into());
    }
    let manager = state["vm_state"]["snapshots"]["memory-manager"]["snapshot_data"]["state"]
        .as_str()
        .ok_or("VMM state has no memory-manager state")?;
    let manager: Value =
        serde_json::from_str(manager).map_err(|_| "Memory-manager state is not JSON")?;
    let mappings = manager["guest_ram_mappings"]
        .as_array()
        .filter(|mappings| !mappings.is_empty() && mappings.len() <= 256)
        .ok_or("VMM memory region count exceeds its bound or is empty")?;
    let mut regions = Vec::with_capacity(mappings.len());
    for mapping in mappings {
        let guest_phys_addr = mapping["gpa"]
            .as_u64()
            .ok_or("Memory region has no guest address")?;
        let length = mapping["size"]
            .as_u64()
            .ok_or("Memory region has no size")?;
        regions.push(RegistryMemoryRegion {
            guest_phys_addr,
            logical_offset: guest_phys_addr,
            length,
        });
    }
    regions.sort_by_key(|region| region.guest_phys_addr);
    let layout = RegistryMemoryLayout {
        logical_bytes: memory.logical_bytes,
        unit_size_bytes: 4096,
        regions,
    };
    layout.validate()?;
    Ok(layout)
}

impl RegistryMemoryLayout {
    fn validate(&self) -> Result<(), String> {
        if self.unit_size_bytes != 4096
            || self.logical_bytes == 0
            || !self.logical_bytes.is_multiple_of(4096)
            || self.regions.is_empty()
            || self.regions.len() > 256
        {
            return Err("Registry memory geometry or region count is invalid".into());
        }
        let mut end = 0;
        for region in &self.regions {
            if region.guest_phys_addr != region.logical_offset
                || region.guest_phys_addr < end
                || !region.guest_phys_addr.is_multiple_of(4096)
                || region.length == 0
                || !region.length.is_multiple_of(4096)
                || region
                    .guest_phys_addr
                    .checked_add(region.length)
                    .is_none_or(|end| end > self.logical_bytes)
            {
                return Err("Registry memory regions overlap or exceed the native layout".into());
            }
            end = region.guest_phys_addr + region.length;
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct RegistrySnapshotMetadata {
    pub format_version: u64,
    pub snapshot_id: String,
    /// CPU, VMM state version, Rocklet and all graph hashes are bound here.
    /// registry_snapshot is absent until the final index digest is known.
    pub spec: SnapshotSpec,
    pub memory_layout: Option<RegistryMemoryLayout>,
    pub objects: Vec<RegistryObjectBinding>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub external_sources: Vec<RegistryEmbeddedSource>,
}

impl RegistrySnapshotMetadata {
    pub(crate) fn new(
        mut spec: SnapshotSpec,
        mut objects: Vec<RegistryObjectBinding>,
        external_sources: Vec<RegistryEmbeddedSource>,
        vm_state: Option<&[u8]>,
    ) -> Result<Self, String> {
        spec.registry_snapshot = None;
        let snapshot_id = spec
            .manifest
            .object_key
            .strip_prefix("snapshots/")
            .and_then(|key| key.strip_suffix("/manifest.json"))
            .ok_or_else(|| "Registry snapshot manifest path is invalid".to_owned())?
            .to_owned();
        objects.sort_by(|left, right| left.object_key.cmp(&right.object_key));
        let metadata = Self {
            format_version: 1,
            snapshot_id,
            memory_layout: match (spec.memory.as_ref(), vm_state) {
                (Some(_), Some(payload)) => Some(memory_layout_from_state(&spec, payload)?),
                (None, None) => None,
                _ => {
                    return Err(
                        "Registry snapshot memory and VMM state bytes must be paired".into(),
                    )
                }
            },
            spec,
            objects,
            external_sources,
        };
        metadata.validate()?;
        Ok(metadata)
    }

    pub(crate) fn bound_spec(
        &self,
        reference: &RegistrySnapshotRef,
    ) -> Result<SnapshotSpec, String> {
        self.validate()?;
        let mut spec = self.spec.clone();
        spec.registry_snapshot = Some(reference.clone());
        spec.validate()?;
        Ok(spec)
    }

    pub(crate) fn digest(&self) -> Result<String, String> {
        Ok(sha256_identity(
            &canonical_json_bytes(self).map_err(|error| error.to_string())?,
        ))
    }

    pub(crate) fn validate(&self) -> Result<(), String> {
        if self.format_version != 1
            || self.spec.registry_snapshot.is_some()
            || self.spec.storage.provider != StorageProvider::Registry
            || self.spec.rootfs.source_ref.is_some()
            || self.spec.compatibility.architecture != "x86_64"
            || self.objects.is_empty()
            || self.objects.len() > MAX_SNAPSHOT_OBJECTS
            || self.external_sources.len() > 64
        {
            return Err("Registry snapshot envelope or architecture is invalid".into());
        }
        self.spec.validate_for_preparation()?;
        super::validate_snapshot_paths(&self.spec, &self.snapshot_id)
            .map_err(|error| error.to_string())?;
        if self.spec.memory.is_some() != self.spec.vm_state.is_some()
            || self.spec.memory.is_some() != self.memory_layout.is_some()
        {
            return Err("Registry memory layout or VMM state binding differs".into());
        }
        if let Some(layout) = &self.memory_layout {
            layout.validate()?;
            if self
                .spec
                .memory
                .as_ref()
                .is_none_or(|memory| memory.logical_bytes != layout.logical_bytes)
            {
                return Err("Registry memory layout differs from its chain geometry".into());
            }
        }
        let mut previous = None;
        let mut objects = HashMap::new();
        let mut physical = HashMap::new();
        for object in &self.objects {
            validate_blob(&object.blob)?;
            if object.object_key.is_empty()
                || object.object_key.len() > 4096
                || previous.is_some_and(|value: &str| value >= object.object_key.as_str())
                || objects
                    .insert(object.object_key.as_str(), &object.blob)
                    .is_some()
            {
                return Err("Registry object map is not canonical or has conflicting keys".into());
            }
            if object
                .object_version
                .as_ref()
                .is_some_and(|version| version.is_empty() || version.len() > 1024)
                || physical
                    .insert(&object.blob.digest, object.blob.size)
                    .is_some_and(|size| size != object.blob.size)
            {
                return Err("Registry object map has conflicting versions or sizes".into());
            }
            previous = Some(object.object_key.as_str());
        }
        let check = |key: &str, digest: &str, size: Option<u64>| {
            let blob = objects
                .get(key)
                .ok_or_else(|| format!("Registry object map has no entry for {key:?}"))?;
            if blob.digest != digest || size.is_some_and(|size| size != blob.size) {
                return Err(format!("Registry object map conflicts for {key:?}"));
            }
            Ok(())
        };
        check(
            &self.spec.manifest.object_key,
            &self.spec.manifest.sha256,
            None,
        )?;
        check(
            &self.spec.rootfs.index_object_key,
            &self.spec.rootfs.index_sha256,
            None,
        )?;
        if let Some(memory) = &self.spec.memory {
            check(&memory.index_object_key, &memory.index_sha256, None)?;
        }
        if let Some(state) = &self.spec.vm_state {
            check(&state.object_key, &state.sha256, Some(state.stored_bytes))?;
        }
        let mut external = HashSet::new();
        for source in &self.external_sources {
            source.decode()?;
            if !external.insert(&source.source_ref) {
                return Err("Registry snapshot repeats an embedded source".into());
            }
        }
        if canonical_json_bytes(self)
            .map_err(|error| error.to_string())?
            .len()
            > MAX_OCI_METADATA_BYTES
        {
            return Err("Registry snapshot metadata exceeds its bound".into());
        }
        Ok(())
    }
}

pub(crate) fn validate_blob(blob: &BlobRef) -> Result<(), String> {
    crate::snapshot::metadata::validate_sha256(&blob.digest)?;
    if blob.size == 0 {
        return Err("Registry object has an empty physical blob".into());
    }
    Ok(())
}

#[derive(Clone, Debug)]
pub(crate) struct RegistryDocument {
    pub media_type: &'static str,
    pub digest: String,
    pub payload: Vec<u8>,
}

impl RegistryDocument {
    pub(crate) fn new(media_type: &'static str, value: Value) -> Result<Self, String> {
        let payload = canonical_json_bytes(&value).map_err(|error| error.to_string())?;
        if payload.len() > MAX_OCI_METADATA_BYTES {
            return Err("Registry document exceeds its metadata bound".into());
        }
        Ok(Self {
            media_type,
            digest: sha256_identity(&payload),
            payload,
        })
    }

    pub(crate) fn descriptor(&self) -> Value {
        json!({"mediaType": self.media_type, "digest": self.digest, "size": self.payload.len()})
    }
}

#[derive(Clone, Debug)]
pub(crate) struct RegistrySnapshotDocuments {
    pub disk_config: RegistryDocument,
    pub disk_manifest: RegistryDocument,
    pub memory_config: Option<RegistryDocument>,
    pub memory_manifest: Option<RegistryDocument>,
    pub index: RegistryDocument,
}

pub(crate) fn build_snapshot_documents(
    metadata: &RegistrySnapshotMetadata,
    disk_layers: &[BlobRef],
    memory_layers: &[BlobRef],
) -> Result<RegistrySnapshotDocuments, String> {
    metadata.validate()?;
    if disk_layers.is_empty()
        || disk_layers.len() > 64
        || memory_layers.len() > 64
        || (metadata.spec.memory.is_some() != !memory_layers.is_empty())
    {
        return Err("Registry snapshot DATA layer count or memory role differs".into());
    }
    let all: HashSet<_> = metadata
        .objects
        .iter()
        .map(|object| (object.blob.digest.as_str(), object.blob.size))
        .collect();
    let mut native = HashSet::new();
    for blob in disk_layers.iter().chain(memory_layers) {
        validate_blob(blob)?;
        if !all.contains(&(blob.digest.as_str(), blob.size)) {
            return Err("Registry DATA manifest references an unbound blob".into());
        }
        native.insert(blob.digest.as_str());
    }
    let digest = metadata.digest()?;
    let annotations = json!({
        (SNAPSHOT_FORMAT): "1", (SNAPSHOT_ID): metadata.snapshot_id,
        (METADATA_DIGEST): digest,
    });
    let layer = |blob: &BlobRef, role: &str, media_type: &str| {
        json!({
            "mediaType": media_type, "digest": blob.digest, "size": blob.size,
            "annotations": {
                (LAYER_ROLE): role,
                "containerd.io/snapshot/overlaybd/blob-digest": blob.digest,
                "containerd.io/snapshot/overlaybd/blob-size": blob.size.to_string(),
                "containerd.io/snapshot/overlaybd/version": "0.1.0",
            }
        })
    };
    let disk_descriptors = disk_layers
        .iter()
        .map(|blob| layer(blob, "disk", NATIVE_LAYER))
        .collect::<Vec<_>>();
    let mut private_descriptors = memory_layers
        .iter()
        .map(|blob| layer(blob, "memory", NATIVE_LAYER))
        .collect::<Vec<_>>();
    let mut auxiliary = HashSet::new();
    for object in &metadata.objects {
        if !native.contains(object.blob.digest.as_str())
            && auxiliary.insert(object.blob.digest.as_str())
        {
            let state = metadata
                .spec
                .vm_state
                .as_ref()
                .is_some_and(|state| state.object_key == object.object_key);
            private_descriptors.push(layer(
                &object.blob,
                if state { "vm-state" } else { "metadata" },
                if state {
                    VM_STATE_LAYER
                } else {
                    AUXILIARY_LAYER
                },
            ));
        }
    }
    let config = |architecture: &str, role: &str, layers: &[Value]| {
        RegistryDocument::new(
            OCI_CONFIG,
            json!({
                "architecture": architecture, "os": "linux",
                "rootfs": {"type": "layers", "diff_ids": layers.iter().map(|layer| {
                    layer["digest"].clone()
                }).collect::<Vec<_>>()},
                "config": {}, "snapd_role": role, "snapd_metadata_digest": digest,
                "snapd": metadata,
            }),
        )
    };
    let disk_config = config("amd64", "disk", &disk_descriptors)?;
    let memory_config = config(MEMORY_ARCHITECTURE, "memory", &private_descriptors)?;
    let manifest = |config: &RegistryDocument, layers: &[Value]| {
        RegistryDocument::new(
            OCI_MANIFEST,
            json!({
                "schemaVersion": 2, "mediaType": OCI_MANIFEST,
                "artifactType": NATIVE_ARTIFACT,
                "config": config.descriptor(), "layers": layers,
                "annotations": annotations,
            }),
        )
    };
    let disk_manifest = manifest(&disk_config, &disk_descriptors)?;
    let memory_manifest = manifest(&memory_config, &private_descriptors)?;
    let mut disk = disk_manifest.descriptor();
    disk["platform"] = json!({"os": "linux", "architecture": "amd64"});
    let mut memory = memory_manifest.descriptor();
    memory["platform"] = json!({"os": "linux", "architecture": MEMORY_ARCHITECTURE});
    let index = RegistryDocument::new(
        OCI_INDEX,
        json!({
            "schemaVersion": 2, "mediaType": OCI_INDEX,
            "manifests": [disk, memory], "annotations": annotations,
        }),
    )?;
    Ok(RegistrySnapshotDocuments {
        disk_config,
        disk_manifest,
        memory_config: Some(memory_config),
        memory_manifest: Some(memory_manifest),
        index,
    })
}
