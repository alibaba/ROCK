//! Format-owned frozen-layer encoding and immutable snapshot metadata.
//! The core supplies a validated, retained read capability; no VM lifecycle or
//! background work is owned here, and memfds are encoded without a raw dump.
use crate::snapshot::format::overlaybd::{
    encode_descriptor, encode_zfile_descriptor, OverlaybdBlobPlan,
};
use crate::snapshot::format::{encode_layer, LayerEncodeError};
use crate::snapshot::io::backing::{BackingError, BackingRecovery};
use crate::snapshot::metadata::{
    canonical_json_bytes, compute_layer_content_sha256, sha256_identity, ChainIndexRef, DataCodec,
    DataSource, LayerChainIndex, LayerKind, LayerRange, LayerRangeMap, LayerRangeMapRef, LayerRef,
    ManifestChainRef, ObjectRef, RangeKind, RangeMapEncoding, RockletArtifactCompatibility,
    SnapshotKind, SnapshotProvenance, FORMAT_VERSION_V1, MAX_CHAIN_DEPTH, MAX_DATA_RANGE_BYTES,
    MAX_RANGES_PER_LAYER,
};
use crate::snapshot::transport::{DataIntegrityFailure, SealedObject, StagedObjectWriter};
use crate::snapshot::ValidateCanonicalModel;
use std::collections::HashMap;
use std::fs::File;
use std::os::unix::fs::FileExt;
use std::sync::{Arc, Mutex};
use thiserror::Error;
const MAX_CHAIN_INDEX_BYTES: usize = 8 * 1024 * 1024;
#[derive(Debug, Error)]
pub enum FormatCaptureError {
    #[error(transparent)]
    SourceIntegrity(DataIntegrityFailure),
    #[error("Invalid frozen format source: {0}")]
    Invalid(String),
    #[error("Frozen format source I/O failed: {0}")]
    Io(String),
    #[error("Canonical snapshot metadata failed: {0}")]
    CanonicalModel(String),
}
impl From<LayerEncodeError> for FormatCaptureError {
    fn from(error: LayerEncodeError) -> Self {
        match error {
            LayerEncodeError::SourceIntegrity(failure) => Self::SourceIntegrity(failure),
            LayerEncodeError::Invalid(m) => Self::Invalid(m),
            LayerEncodeError::Io(m) => Self::Io(m),
            LayerEncodeError::CanonicalModel(m) => Self::CanonicalModel(m),
        }
    }
}
fn invalid<T>(message: &str) -> Result<T, FormatCaptureError> {
    Err(FormatCaptureError::Invalid(message.into()))
}
fn artifact_error(error: crate::snapshot::metadata::CanonicalModelError) -> FormatCaptureError {
    FormatCaptureError::CanonicalModel(error.to_string())
}

#[derive(Clone, Debug)]
pub struct LocalOverlaybdBlob {
    pub object_key: String,
    pub sha256: String,
    pub length: u64,
    pub(crate) plan: Arc<OverlaybdBlobPlan>,
    object: Arc<Mutex<Option<Arc<dyn SealedObject>>>>,
}

impl PartialEq for LocalOverlaybdBlob {
    fn eq(&self, other: &Self) -> bool {
        self.object_key == other.object_key
            && self.sha256 == other.sha256
            && self.length == other.length
            && self.plan == other.plan
    }
}

impl Eq for LocalOverlaybdBlob {}

impl LocalOverlaybdBlob {
    pub(crate) fn sealed_object(
        &self,
    ) -> Result<Option<Arc<dyn SealedObject>>, FormatCaptureError> {
        self.object
            .lock()
            .map(|file| file.clone())
            .map_err(|_| FormatCaptureError::Io("Sealed object lock is poisoned".into()))
    }

    #[cfg(test)]
    pub(crate) fn encoded_file(&self) -> Result<Option<Arc<File>>, FormatCaptureError> {
        self.sealed_object()?
            .map(|object| {
                object
                    .file()
                    .try_clone()
                    .map(Arc::new)
                    .map_err(|error| FormatCaptureError::Io(error.to_string()))
            })
            .transpose()
    }

    pub(crate) fn release_sealed_object(&self) -> Result<(), FormatCaptureError> {
        self.object
            .lock()
            .map_err(|_| FormatCaptureError::Io("Sealed object lock is poisoned".into()))?
            .take();
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CanonicalLocalLayer {
    pub layer_id: String,
    pub range_map: Arc<LayerRangeMap>,
    pub range_map_bytes: Vec<u8>,
    pub range_map_sha256: String,
    pub blob: LocalOverlaybdBlob,
}

/// The format retains only the authenticated descriptor and sealed physical
/// object. It never retains the native backing whose pages it can reconstruct.
#[derive(Debug)]
struct SealedLayerRecovery {
    map: Arc<LayerRangeMap>,
    object: Arc<dyn SealedObject>,
}

pub(crate) fn backing_recovery(
    layer: &CanonicalLocalLayer,
) -> Result<Arc<dyn BackingRecovery>, FormatCaptureError> {
    let object = layer.blob.sealed_object()?.ok_or_else(|| {
        FormatCaptureError::Invalid("sealed recovery source is unavailable".into())
    })?;
    Ok(Arc::new(SealedLayerRecovery {
        map: Arc::clone(&layer.range_map),
        object,
    }))
}

#[async_trait::async_trait]
impl BackingRecovery for SealedLayerRecovery {
    async fn read_data(&self, offset: u64, length: u64) -> Result<Vec<u8>, BackingError> {
        let end = offset
            .checked_add(length)
            .filter(|end| {
                length != 0 && length <= MAX_DATA_RANGE_BYTES && *end <= self.map.logical_bytes
            })
            .ok_or(BackingError::InvalidNativeRange)?;
        let invalid = |message: String| {
            BackingError::Io(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                message,
            ))
        };
        let mut result = Vec::with_capacity(length as usize);
        let mut cursor = offset;
        let first = self
            .map
            .ranges
            .partition_point(|range| range.logical_offset + range.length <= offset);
        for range in &self.map.ranges[first..] {
            if cursor == end {
                break;
            }
            if range.kind != RangeKind::Data || range.logical_offset > cursor {
                return Err(BackingError::InvalidNativeRange);
            }
            let source = range
                .source
                .as_ref()
                .ok_or(BackingError::InvalidNativeRange)?;
            // Authenticate the entire format DATA range before slicing out a
            // native fixed-size unit. Block ranges can cover multiple units.
            let decoded = if let Some(index) = &self.map.zfile {
                let span = index
                    .compressed_span(source.pack_offset, range.length)
                    .map_err(|error| invalid(error.to_string()))?;
                let bytes = self
                    .object
                    .read_at(span.offset, span.length as usize)
                    .await
                    .map_err(|error| invalid(error.to_string()))?;
                span.decode(&bytes)
                    .map_err(|error| invalid(error.to_string()))?
            } else {
                self.object
                    .read_at(source.pack_offset, range.length as usize)
                    .await
                    .map_err(|error| invalid(error.to_string()))?
            };
            if decoded.len() as u64 != range.length
                || sha256_identity(&decoded).trim_start_matches("sha256:")
                    != source.checksum_sha256.trim_start_matches("sha256:")
            {
                return Err(invalid("sealed recovery DATA checksum mismatch".into()));
            }
            let stop = end.min(range.logical_offset + range.length);
            result.extend_from_slice(
                &decoded[(cursor - range.logical_offset) as usize
                    ..(stop - range.logical_offset) as usize],
            );
            cursor = stop;
        }
        if cursor != end {
            return Err(BackingError::InvalidNativeRange);
        }
        Ok(result)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LocalVmState {
    pub object_key: String,
    pub sha256: String,
    pub format_version: u64,
    pub payload: Vec<u8>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LocalChainIndex {
    pub object_key: String,
    pub sha256: String,
    pub payload: Vec<u8>,
    pub index: LayerChainIndex,
}

#[derive(Debug)]
pub(crate) struct CanonicalLocalSnapshot {
    pub(crate) layers: HashMap<LayerKind, CanonicalLocalLayer>,
    pub(crate) chain_indexes: HashMap<LayerKind, LocalChainIndex>,
}

/// A retained source for one captured snapshot. File capabilities may name a
/// memfd or an immutable disk file; neither requires a full raw dump first.
pub struct PreparedSnapshotSource {
    pub snapshot_id: String,
    pub snapshot_kind: SnapshotKind,
    pub(crate) canonical: Arc<CanonicalLocalSnapshot>,
    pub(crate) vm_state: Option<LocalVmState>,
    pub(crate) rocklet_artifact: Option<RockletArtifactCompatibility>,
    pub(crate) provenance: Option<SnapshotProvenance>,
}

pub(crate) struct FrozenLayerInput<'a> {
    pub kind: LayerKind,
    pub layer_id: &'a str,
    pub logical_bytes: u64,
    pub unit_size_bytes: u64,
    pub packed_states: &'a [u8],
    pub file: &'a File,
}
impl FrozenLayerInput<'_> {
    fn validate(&self) -> Result<(), FormatCaptureError> {
        if self.layer_id.is_empty()
            || self.logical_bytes == 0
            || self.unit_size_bytes == 0
            || !self.logical_bytes.is_multiple_of(self.unit_size_bytes)
            || self.logical_bytes / self.unit_size_bytes > 16 * 1024 * 1024
            || self.packed_states.len() as u64
                != (self.logical_bytes / self.unit_size_bytes).div_ceil(4)
            || self
                .file
                .metadata()
                .map_err(|e| FormatCaptureError::Io(e.to_string()))?
                .len()
                != self.logical_bytes
        {
            return invalid("Frozen source identity, geometry or coverage is invalid");
        }
        Ok(())
    }
    fn ownership_state(&self, unit: u64) -> Result<u8, FormatCaptureError> {
        let byte = self.packed_states.get((unit / 4) as usize).ok_or_else(|| {
            FormatCaptureError::Invalid("Ownership unit is outside source coverage".into())
        })?;
        match (byte >> ((unit % 4) * 2)) & 3 {
            state @ 0..=2 => Ok(state),
            _ => invalid("Ownership state is invalid"),
        }
    }
    fn read_data(&self, offset: u64, length: u64) -> Result<Vec<u8>, FormatCaptureError> {
        let length = usize::try_from(length)
            .map_err(|_| FormatCaptureError::Invalid("DATA length exceeds usize".into()))?;
        let mut payload = vec![0; length];
        self.file
            .read_exact_at(&mut payload, offset)
            .map_err(|e| FormatCaptureError::Io(e.to_string()))?;
        Ok(payload)
    }
}
pub(crate) fn encode_frozen_layer(
    input: FrozenLayerInput<'_>,
    has_parent_layer: bool,
    canonical_unit_size: Option<u64>,
    create_scratch: &dyn Fn(u64) -> Result<Arc<dyn StagedObjectWriter>, FormatCaptureError>,
) -> Result<CanonicalLocalLayer, FormatCaptureError> {
    input.validate()?;
    let kind = input.kind;
    let unit = input.unit_size_bytes;
    let canonical_unit = canonical_unit_size.unwrap_or(unit);
    if canonical_unit == 0
        || !unit.is_multiple_of(canonical_unit)
        || (kind == LayerKind::Memory && canonical_unit != unit)
    {
        return invalid("Canonical load unit is incompatible with frozen ownership");
    }
    let mut ranges = Vec::<LayerRange>::new();
    let unit_count = usize::try_from(input.logical_bytes / unit)
        .map_err(|_| FormatCaptureError::Invalid("Ownership unit count exceeds usize".into()))?;
    let mut cursor = 0_usize;
    while cursor < unit_count {
        let state = input.ownership_state(cursor as u64)?;
        let mut end = cursor + 1;
        while end < unit_count && input.ownership_state(end as u64)? == state {
            end += 1;
        }
        let logical_offset = (cursor as u64)
            .checked_mul(unit)
            .ok_or_else(|| FormatCaptureError::Invalid("Logical offset overflows u64".into()))?;
        let length = ((end - cursor) as u64)
            .checked_mul(unit)
            .ok_or_else(|| FormatCaptureError::Invalid("Logical length overflows u64".into()))?;
        if state == 1 {
            let load_unit = if kind == LayerKind::Memory {
                unit
            } else {
                MAX_DATA_RANGE_BYTES
            };
            let data_units = usize::try_from(length.div_ceil(load_unit))
                .map_err(|_| FormatCaptureError::Invalid("DATA unit count exceeds usize".into()))?;
            if ranges.len().saturating_add(data_units) > MAX_RANGES_PER_LAYER {
                return invalid("Canonical layer range count exceeds the format bound");
            }
            // Memory remains independently authenticated per 4 KiB page.
            // Block uses bounded contiguous ranges to keep large rootfs indexes
            // small. LSMT segment boundaries do not determine load granularity.
            let mut data_offset = logical_offset;
            let mut remaining = length;
            while remaining != 0 {
                let chunk_length = remaining.min(MAX_DATA_RANGE_BYTES);
                let payload = input.read_data(data_offset, chunk_length)?;
                for (unit_index, payload) in payload.chunks(load_unit as usize).enumerate() {
                    let load_length = payload.len() as u64;
                    let checksum = sha256_identity(payload);
                    ranges.push(LayerRange {
                        kind: RangeKind::Data,
                        logical_offset: data_offset + unit_index as u64 * load_unit,
                        length: load_length,
                        source: Some(DataSource {
                            // Only checksums and geometry participate in the
                            // logical identity. Final blob locations are decoded
                            // from the authenticated standard LSMT descriptor.
                            object_key: "pending".into(),
                            object_sha256: checksum.clone(),
                            object_version: None,
                            pack_offset: 0,
                            encoded_length: load_length,
                            decoded_length: load_length,
                            codec: DataCodec::Raw,
                            checksum_sha256: checksum,
                        }),
                    });
                }
                data_offset += chunk_length;
                remaining -= chunk_length;
            }
        } else {
            let range_kind = if state == 2 || (state == 0 && !has_parent_layer) {
                RangeKind::Zero
            } else {
                RangeKind::Inherit
            };
            if let Some(previous) = ranges.last_mut().filter(|range| range.kind == range_kind) {
                previous.length = previous.length.checked_add(length).ok_or_else(|| {
                    FormatCaptureError::Invalid("Non-DATA range length overflows u64".into())
                })?;
            } else {
                ranges.push(LayerRange {
                    kind: range_kind,
                    logical_offset,
                    length,
                    source: None,
                });
            }
        }
        cursor = end;
    }
    if ranges.len() > MAX_RANGES_PER_LAYER {
        return invalid("Canonical layer range count exceeds the format bound");
    }

    let mut logical_layer = LayerRangeMap {
        registry_objects: None,
        format_version: FORMAT_VERSION_V1,
        kind,
        content_sha256: format!("sha256:{}", "0".repeat(64)),
        logical_bytes: input.logical_bytes,
        // Frozen block ownership remains 64 KiB. A 512-byte external parent
        // fixes the canonical chain geometry, without splitting native COW
        // ownership or changing already aligned DATA/INHERIT/ZERO extents.
        unit_size_bytes: canonical_unit,
        ranges,
        registry: None,
        zfile: None,
    };
    logical_layer.content_sha256 =
        compute_layer_content_sha256(&logical_layer).map_err(artifact_error)?;
    logical_layer
        .validate()
        .map_err(FormatCaptureError::Invalid)?;
    let encoded = encode_layer(logical_layer, input.file, create_scratch)?;
    let blob = LocalOverlaybdBlob {
        object_key: encoded.blob.object_key,
        sha256: encoded.blob.sha256,
        length: encoded.blob_length,
        plan: encoded.plan,
        object: Arc::new(Mutex::new(Some(encoded.sealed_object))),
    };
    Ok(CanonicalLocalLayer {
        layer_id: input.layer_id.to_owned(),
        range_map: Arc::new(encoded.range_map),
        range_map_bytes: encoded.descriptor,
        range_map_sha256: encoded.descriptor_sha256,
        blob,
    })
}

pub(crate) fn build_chain_index(
    snapshot_id: &str,
    kind: LayerKind,
    layer: &CanonicalLocalLayer,
    parent: Option<&LayerChainIndex>,
) -> Result<LocalChainIndex, FormatCaptureError> {
    let mut layers = match parent {
        Some(parent) => {
            parent.validate().map_err(FormatCaptureError::Invalid)?;
            if parent.kind != kind
                || parent.logical_bytes != layer.range_map.logical_bytes
                || parent.unit_size_bytes != layer.range_map.unit_size_bytes
                || parent.format_version != FORMAT_VERSION_V1
            {
                return Err(FormatCaptureError::Invalid(
                    "Parent chain geometry or kind does not match captured layer".into(),
                ));
            }
            if parent
                .layers
                .iter()
                .any(|item| item.layer_id == layer.layer_id)
            {
                return Err(FormatCaptureError::Invalid(
                    "Captured layer ID already exists in parent chain".into(),
                ));
            }
            if parent.layers.len() >= MAX_CHAIN_DEPTH {
                return Err(FormatCaptureError::Invalid(
                    "Parent chain is already at the format depth bound".into(),
                ));
            }
            parent.layers.clone()
        }
        None => Vec::new(),
    };
    let ordinal = u64::try_from(layers.len())
        .map_err(|_| FormatCaptureError::Invalid("Chain ordinal exceeds u64".into()))?;
    layers.push(LayerRef {
        ordinal,
        layer_id: layer.layer_id.clone(),
        format_version: layer.range_map.format_version,
        content_sha256: layer.range_map.content_sha256.clone(),
        range_map: LayerRangeMapRef {
            object_key: range_map_object_key(layer),
            sha256: layer.range_map_sha256.clone(),
            source_ref: None,
            object_version: None,
            encoding: Some(layer_encoding(layer)),
        },
    });
    let index = LayerChainIndex {
        format_version: FORMAT_VERSION_V1,
        kind,
        logical_bytes: layer.range_map.logical_bytes,
        unit_size_bytes: layer.range_map.unit_size_bytes,
        layers,
    };
    index.validate().map_err(FormatCaptureError::Invalid)?;
    let payload = canonical_json_bytes(&index)
        .map_err(|error| FormatCaptureError::Invalid(error.to_string()))?;
    if payload.len() > MAX_CHAIN_INDEX_BYTES {
        return Err(FormatCaptureError::Invalid(
            "Canonical chain index exceeds the format byte bound".into(),
        ));
    }
    Ok(LocalChainIndex {
        object_key: chain_object_key(snapshot_id, kind),
        sha256: sha256_identity(&payload),
        payload,
        index,
    })
}

pub(crate) fn validate_canonical_parts(
    snapshot_id: &str,
    layers: &HashMap<LayerKind, CanonicalLocalLayer>,
    chain_indexes: &HashMap<LayerKind, LocalChainIndex>,
    vm_state: Option<&LocalVmState>,
    snapshot_kind: SnapshotKind,
) -> Result<(), FormatCaptureError> {
    if layers.is_empty()
        || layers.len() != chain_indexes.len()
        || layers.keys().any(|kind| !chain_indexes.contains_key(kind))
    {
        return Err(FormatCaptureError::Invalid(
            "Canonical local layers and chains must match and be non-empty".into(),
        ));
    }
    match snapshot_kind {
        SnapshotKind::BaseImage
            if layers.len() != 1
                || !layers.contains_key(&LayerKind::Block)
                || vm_state.is_some() =>
        {
            return Err(FormatCaptureError::Invalid(
                "BASE_IMAGE must contain cold-boot rootfs only".into(),
            ));
        }
        SnapshotKind::Full
            if !layers.contains_key(&LayerKind::Memory)
                || !layers.contains_key(&LayerKind::Block)
                || vm_state.is_none() =>
        {
            return Err(FormatCaptureError::Invalid(
                "FULL snapshot requires memory, rootfs and VM state".into(),
            ));
        }
        _ => {}
    }
    for (kind, layer) in layers {
        validate_layer_chain(
            snapshot_id,
            *kind,
            layer,
            chain_indexes.get(kind).ok_or_else(|| {
                FormatCaptureError::Invalid("Canonical local chain is missing".into())
            })?,
        )?;
    }
    Ok(())
}

pub(crate) fn validate_layer_chain(
    snapshot_id: &str,
    kind: LayerKind,
    layer: &CanonicalLocalLayer,
    chain: &LocalChainIndex,
) -> Result<(), FormatCaptureError> {
    layer
        .range_map
        .validate()
        .map_err(FormatCaptureError::Invalid)?;
    let blob_ref = ObjectRef {
        object_key: layer.blob.object_key.clone(),
        sha256: layer.blob.sha256.clone(),
        object_version: None,
    };
    let canonical_range_map = match &layer.range_map.zfile {
        Some(index) => {
            encode_zfile_descriptor(&layer.range_map, &layer.blob.plan, &blob_ref, index)
        }
        None => encode_descriptor(&layer.range_map, &layer.blob.plan, &blob_ref),
    }
    .map_err(FormatCaptureError::Invalid)?;
    chain
        .index
        .validate()
        .map_err(FormatCaptureError::Invalid)?;
    let canonical = canonical_json_bytes(&chain.index)
        .map_err(|error| FormatCaptureError::Invalid(error.to_string()))?;
    let head = chain.index.layers.last().ok_or_else(|| {
        FormatCaptureError::Invalid("Local snapshot chain cannot be empty".into())
    })?;
    if layer.range_map.kind != kind
        || layer.range_map_bytes != canonical_range_map
        || layer.range_map_sha256 != sha256_identity(&canonical_range_map)
        || chain.index.kind != kind
        || chain.index.logical_bytes != layer.range_map.logical_bytes
        || chain.index.unit_size_bytes != layer.range_map.unit_size_bytes
        || chain.object_key != chain_object_key(snapshot_id, kind)
        || chain.payload != canonical
        || chain.sha256 != sha256_identity(&canonical)
        || head.layer_id != layer.layer_id
        || head.format_version != layer.range_map.format_version
        || head.content_sha256 != layer.range_map.content_sha256
        || head.range_map.object_key != range_map_object_key(layer)
        || head.range_map.sha256 != layer.range_map_sha256
        || head.range_map.object_version.is_some()
        || head.range_map.encoding != Some(layer_encoding(layer))
    {
        return Err(FormatCaptureError::Invalid(
            "Local snapshot head does not match its captured canonical layer".into(),
        ));
    }
    Ok(())
}

pub(crate) fn range_map_object_key(layer: &CanonicalLocalLayer) -> String {
    let content = layer
        .range_map
        .content_sha256
        .strip_prefix("sha256:")
        .unwrap_or(&layer.range_map.content_sha256);
    format!(
        "layers/{}/{}/{content}/{}",
        layer.range_map.kind.object_key_component(),
        layer.range_map.format_version,
        layer_encoding(layer).descriptor_name()
    )
}

pub(crate) fn layer_encoding(layer: &CanonicalLocalLayer) -> RangeMapEncoding {
    if layer.range_map.zfile.is_some() {
        RangeMapEncoding::OverlaybdZfileLz4V1
    } else {
        RangeMapEncoding::OverlaybdLsmtV1
    }
}

pub(crate) fn chain_object_key(snapshot_id: &str, kind: LayerKind) -> String {
    format!(
        "snapshots/{snapshot_id}/{}",
        if kind == LayerKind::Memory {
            "memory.index"
        } else {
            "rootfs.index"
        }
    )
}

pub(crate) fn manifest_chain(chain: &LocalChainIndex) -> ManifestChainRef {
    ManifestChainRef {
        object_key: chain.object_key.clone(),
        sha256: chain.sha256.clone(),
        logical_bytes: chain.index.logical_bytes,
        object_version: None,
    }
}

pub(crate) fn chain_ref(chain: &LocalChainIndex) -> ChainIndexRef {
    ChainIndexRef {
        logical_bytes: chain.index.logical_bytes,
        index_object_key: chain.object_key.clone(),
        index_sha256: chain.sha256.clone(),
        source_ref: None,
        object_version: None,
    }
}
