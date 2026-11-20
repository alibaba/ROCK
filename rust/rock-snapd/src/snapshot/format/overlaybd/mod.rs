//! Standard uncompressed sealed OverlayBD LSMT layers and lazy integrity metadata.
//!
//! Wire layout follows containerd/overlaybd@1fe0ea1b87989a616f21105a13455b79c8512ffb,
//! src/overlaybd/lsmt/{file.cpp,index.h}. This deliberately implements the ordinary
//! sealed append-layer subset with an optional standard ZFile-LZ4 container.
//! Warp files and a block-device runtime are not part of this adapter.
//! The descriptor authenticates the standard metadata and per-unit DATA digests;
//! logical placement is represented only by the standard LSMT index.

pub(crate) mod cache;
pub mod codec;
pub mod composite;
mod lsmt;
pub mod reader;
pub(crate) mod writer;
pub mod zfile;

use std::fs::File;
use std::os::unix::fs::FileExt;
use std::sync::Arc;

use crate::snapshot::format::overlaybd::zfile::ZfileIndex;

use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde::{Deserialize, Serialize};

use crate::snapshot::metadata::{
    canonical_json_bytes, compute_layer_content_sha256, validate_object_ref, validate_sha256,
    DataCodec, DataSource, LayerKind, LayerRange, LayerRangeMap, ObjectRef, RangeKind,
    MAX_DATA_RANGE_BYTES, MAX_RANGES_PER_LAYER, MEMORY_LOAD_UNIT_BYTES,
};
use crate::snapshot::ValidateCanonicalModel;

pub(crate) const MAX_BLOB_BYTES: u64 = 2 * 1024 * 1024 * 1024 * 1024;
const BLOCK_UNIT_BYTES: u64 = 64 * 1024;
// Transport chunks may exceed independently verified DATA units/ranges.
const MAX_READ_BYTES: u64 = 64 * 1024 * 1024;
const STRUCT_SIZE: u32 = 390;
const MAX_SEGMENT_SECTORS: u64 = (1 << 14) - 1;
const MAX_INDEX_RECORDS: usize = 1_000_000;
const RECORD_BYTES: usize = 16;
const RECORDS_PER_BLOCK: usize = 4096 / RECORD_BYTES;
const MAX_DESCRIPTOR_BYTES: usize = 128 * 1024 * 1024;
#[cfg(test)]
use self::lsmt::u64_at;
use self::lsmt::{INVALID_OFFSET, MAGIC as MAGIC0, MAGIC_UUID as MAGIC1, SECTOR, SPACE};

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct OverlaybdDataExtent {
    pub logical_offset: u64,
    pub blob_offset: u64,
    pub length: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct OverlaybdBlobPlan {
    pub header: Vec<u8>,
    pub index: Vec<u8>,
    pub trailer: Vec<u8>,
    pub data: Vec<OverlaybdDataExtent>,
    pub stored_bytes: u64,
    pub index_offset: u64,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Descriptor {
    format_version: u64,
    kind: LayerKind,
    content_sha256: String,
    logical_bytes: u64,
    unit_size_bytes: u64,
    blob: ObjectRef,
    stored_bytes: u64,
    header_base64: String,
    index_base64: String,
    trailer_base64: String,
    data_checksums_base64: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    zfile: Option<ZfileDescriptor>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ZfileDescriptor {
    encoded_bytes: u64,
    header_base64: String,
    jump_table_base64: String,
    trailer_base64: String,
}

fn require(condition: bool, message: &str) -> Result<(), String> {
    if condition {
        Ok(())
    } else {
        Err(message.to_owned())
    }
}

fn add(a: u64, b: u64) -> Result<u64, String> {
    a.checked_add(b)
        .ok_or_else(|| "OverlayBD offset overflows u64".to_owned())
}

fn put_u64(bytes: &mut [u8], offset: usize, value: u64) {
    bytes[offset..offset + 8].copy_from_slice(&value.to_le_bytes());
}

fn digest_bytes(identity: &str) -> Result<[u8; 32], String> {
    validate_sha256(identity)?;
    let mut digest = [0; 32];
    for (n, byte) in digest.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&identity[7 + n * 2..9 + n * 2], 16)
            .map_err(|error| error.to_string())?;
    }
    Ok(digest)
}

fn digest_identity(bytes: &[u8]) -> String {
    let mut value = String::with_capacity(71);
    value.push_str("sha256:");
    const HEX: &[u8; 16] = b"0123456789abcdef";
    for byte in bytes {
        value.push(HEX[(byte >> 4) as usize] as char);
        value.push(HEX[(byte & 15) as usize] as char);
    }
    value
}

fn content_uuid(identity: &str) -> Result<[u8; 37], String> {
    let digest = digest_bytes(identity)?;
    let mut bytes = [0; 16];
    bytes.copy_from_slice(&digest[..16]);
    // UUIDv8 permits an application-defined deterministic SHA256-derived payload.
    bytes[6] = (bytes[6] & 15) | 0x80;
    bytes[8] = (bytes[8] & 63) | 0x80;
    let text = uuid::Uuid::from_bytes(bytes).hyphenated().to_string();
    let mut field = [0; 37];
    field[..36].copy_from_slice(text.as_bytes());
    Ok(field)
}

fn standard_metadata(
    layer: &LayerRangeMap,
    is_header: bool,
    index_offset: u64,
    index_records: u64,
) -> Result<Vec<u8>, String> {
    let mut block = vec![0; SPACE as usize];
    block[..8].copy_from_slice(MAGIC0);
    block[8..24].copy_from_slice(&MAGIC1);
    block[24..28].copy_from_slice(&STRUCT_SIZE.to_le_bytes());
    block[28..32].copy_from_slice(&(if is_header { 7_u32 } else { 6 }).to_le_bytes());
    put_u64(&mut block, 32, index_offset);
    put_u64(&mut block, 40, index_records);
    put_u64(&mut block, 48, layer.logical_bytes);
    block[56..93].copy_from_slice(&content_uuid(&layer.content_sha256)?);
    // LayerRangeMap has no parent identity. The chain manifest is authoritative;
    // nil is a standard UUID and does not manufacture a false parent relation.
    block[93..129].copy_from_slice(b"00000000-0000-0000-0000-000000000000");
    block[132] = 1;
    block[133] = 1;
    Ok(block)
}

fn validate_geometry(kind: LayerKind, logical_bytes: u64, unit: u64) -> Result<(), String> {
    let supported = match kind {
        LayerKind::Memory => unit == MEMORY_LOAD_UNIT_BYTES,
        // A native captured generation owns 64 KiB COW units. Its published
        // descriptor can retain a parent chain's 512-byte sector geometry.
        LayerKind::Block => unit == BLOCK_UNIT_BYTES || unit == SECTOR,
    };
    require(supported, "OverlayBD layer has unsupported unit size")?;
    require(
        logical_bytes > 0 && logical_bytes <= MAX_BLOB_BYTES && logical_bytes.is_multiple_of(unit),
        "OverlayBD logical size is zero, too large, or misaligned",
    )
}

fn append_mapping(
    index: &mut Vec<u8>,
    logical_offset: u64,
    length: u64,
    blob_offset: u64,
    zero: bool,
) -> Result<(), String> {
    require(
        index.len() / RECORD_BYTES < MAX_INDEX_RECORDS,
        "OverlayBD index exceeds upstream mapping limit",
    )?;
    let logical_sector = logical_offset / SECTOR;
    let sectors = length / SECTOR;
    let physical_sector = blob_offset / SECTOR;
    require(
        logical_sector < INVALID_OFFSET
            && sectors > 0
            && sectors <= MAX_SEGMENT_SECTORS
            && physical_sector < (1 << 55),
        "OverlayBD mapping cannot be represented",
    )?;
    index.extend_from_slice(&(logical_sector | (sectors << 50)).to_le_bytes());
    index.extend_from_slice(&(physical_sector | (u64::from(zero) << 55)).to_le_bytes());
    Ok(())
}

/// Plan an ordinary standard sealed LSMT layer over a logical raw backing file.
/// Index records coalesce DATA independently of checksum boundaries: memory keeps
/// 4 KiB digests and block runs keep at most 16 MiB digests on 64 KiB ownership.
pub(crate) fn plan_layer(layer: &LayerRangeMap) -> Result<OverlaybdBlobPlan, String> {
    require(
        layer.registry.is_none(),
        "external Registry bases cannot be repacked as local DATA",
    )?;
    layer.validate()?;
    validate_geometry(layer.kind, layer.logical_bytes, layer.unit_size_bytes)?;
    require(
        compute_layer_content_sha256(layer).map_err(|error| error.to_string())?
            == layer.content_sha256,
        "OverlayBD requires the current logical content identity",
    )?;
    let unit = layer.unit_size_bytes;
    let max_segment_bytes = (MAX_SEGMENT_SECTORS * SECTOR / unit) * unit;
    let mut data: Vec<OverlaybdDataExtent> = Vec::new();
    let mut blob_cursor = SPACE;
    let mut index = Vec::new();
    let mut run: Option<(RangeKind, u64, u64, u64)> = None;
    let flush =
        |run: &mut Option<(RangeKind, u64, u64, u64)>, index: &mut Vec<u8>| -> Result<(), String> {
            if let Some((kind, logical, length, physical)) = run.take() {
                let mut done = 0;
                while done < length {
                    let step = (length - done).min(max_segment_bytes);
                    append_mapping(
                        index,
                        add(logical, done)?,
                        step,
                        if kind == RangeKind::Zero {
                            physical
                        } else {
                            add(physical, done)?
                        },
                        kind == RangeKind::Zero,
                    )?;
                    done = add(done, step)?;
                }
            }
            Ok(())
        };
    let checksum_chunk = match layer.kind {
        LayerKind::Memory => unit,
        LayerKind::Block => MAX_DATA_RANGE_BYTES,
    };
    let mut previous_data_length = None;
    for range in &layer.ranges {
        if range.kind == RangeKind::Data {
            require(
                range.length <= checksum_chunk
                    && previous_data_length.is_none_or(|length| length == checksum_chunk),
                "OverlayBD DATA ranges must use canonical checksum chunks per logical run",
            )?;
            previous_data_length = Some(range.length);
            let next = OverlaybdDataExtent {
                logical_offset: range.logical_offset,
                blob_offset: blob_cursor,
                length: range.length,
            };
            if let Some(previous) = data.last_mut().filter(|previous| {
                previous.logical_offset.checked_add(previous.length) == Some(next.logical_offset)
                    && previous.blob_offset.checked_add(previous.length) == Some(next.blob_offset)
            }) {
                previous.length = add(previous.length, next.length)?;
            } else {
                data.push(next);
            }
        }
        if range.kind != RangeKind::Data {
            previous_data_length = None;
        }
        if range.kind == RangeKind::Inherit {
            flush(&mut run, &mut index)?;
        } else if let Some((kind, logical, length, physical)) =
            run.as_mut().filter(|(kind, logical, length, _)| {
                *kind == range.kind && logical.checked_add(*length) == Some(range.logical_offset)
            })
        {
            let _ = (kind, logical, physical);
            *length = add(*length, range.length)?;
        } else {
            flush(&mut run, &mut index)?;
            run = Some((range.kind, range.logical_offset, range.length, blob_cursor));
        }
        if range.kind == RangeKind::Data {
            blob_cursor = add(blob_cursor, range.length)?;
        }
    }
    flush(&mut run, &mut index)?;
    let records = index.len() / RECORD_BYTES;
    let padded_records = records.div_ceil(RECORDS_PER_BLOCK) * RECORDS_PER_BLOCK;
    require(
        padded_records <= MAX_INDEX_RECORDS,
        "OverlayBD padded index exceeds upstream mapping limit",
    )?;
    for _ in records..padded_records {
        index.extend_from_slice(&INVALID_OFFSET.to_le_bytes());
        index.extend_from_slice(&0_u64.to_le_bytes());
    }
    let stored_bytes = add(add(blob_cursor, index.len() as u64)?, SPACE)?;
    require(
        stored_bytes <= MAX_BLOB_BYTES,
        "OverlayBD blob exceeds maximum size",
    )?;
    Ok(OverlaybdBlobPlan {
        header: standard_metadata(layer, true, 0, 0)?,
        index,
        trailer: standard_metadata(layer, false, blob_cursor, padded_records as u64)?,
        data,
        stored_bytes,
        index_offset: blob_cursor,
    })
}

impl OverlaybdBlobPlan {
    /// Read a bounded slice of header + selected raw DATA + index + trailer.
    /// The backing file is addressed by logical offsets, not legacy pack offsets.
    pub(crate) fn read_at(
        &self,
        file: &File,
        offset: u64,
        max_bytes: usize,
    ) -> Result<Vec<u8>, String> {
        require(
            max_bytes as u64 <= MAX_READ_BYTES,
            "OverlayBD virtual read exceeds maximum chunk size",
        )?;
        require(
            offset <= self.stored_bytes,
            "OverlayBD read starts past EOF",
        )?;
        require(
            self.header.len() as u64 == SPACE
                && self.trailer.len() as u64 == SPACE
                && add(add(self.index_offset, self.index.len() as u64)?, SPACE)?
                    == self.stored_bytes,
            "invalid OverlayBD virtual stream geometry",
        )?;
        let count = (self.stored_bytes - offset).min(max_bytes as u64) as usize;
        let mut output = vec![0; count];
        let mut done = 0;
        let trailer_offset = self.stored_bytes - SPACE;
        while done < count {
            let cursor = add(offset, done as u64)?;
            let remaining = count - done;
            if cursor < SPACE {
                let take = remaining.min((SPACE - cursor) as usize);
                output[done..done + take]
                    .copy_from_slice(&self.header[cursor as usize..cursor as usize + take]);
                done += take;
            } else if cursor < self.index_offset {
                let at = self.data.partition_point(|extent| {
                    extent.blob_offset.saturating_add(extent.length) <= cursor
                });
                let extent = self
                    .data
                    .get(at)
                    .filter(|extent| extent.blob_offset <= cursor)
                    .ok_or_else(|| "gap in OverlayBD virtual DATA stream".to_owned())?;
                let within = cursor - extent.blob_offset;
                let available = extent
                    .length
                    .checked_sub(within)
                    .ok_or_else(|| "invalid OverlayBD DATA extent".to_owned())?;
                let take = (remaining as u64)
                    .min(available)
                    .min(self.index_offset - cursor) as usize;
                require(take > 0, "empty OverlayBD DATA extent")?;
                file.read_exact_at(
                    &mut output[done..done + take],
                    add(extent.logical_offset, within)?,
                )
                .map_err(|error| format!("read OverlayBD raw backing: {error}"))?;
                done += take;
            } else if cursor < trailer_offset {
                let within = (cursor - self.index_offset) as usize;
                let take = remaining.min((trailer_offset - cursor) as usize);
                output[done..done + take].copy_from_slice(&self.index[within..within + take]);
                done += take;
            } else {
                let within = (cursor - trailer_offset) as usize;
                let take = remaining.min((self.stored_bytes - cursor) as usize);
                output[done..done + take].copy_from_slice(&self.trailer[within..within + take]);
                done += take;
            }
        }
        Ok(output)
    }
}

fn validate_blob(layer: &LayerRangeMap, blob: &ObjectRef) -> Result<(), String> {
    validate_object_ref(blob)?;
    let content = &layer.content_sha256[7..];
    let digest = &blob.sha256[7..];
    let expected = format!(
        "layers/{}/1/{content}/data/{digest}.commit",
        layer.kind.object_key_component()
    );
    require(
        blob.object_key == expected,
        "OverlayBD blob key does not match kind/content/object identity",
    )
}

pub(crate) fn encode_descriptor(
    layer: &LayerRangeMap,
    plan: &OverlaybdBlobPlan,
    blob: &ObjectRef,
) -> Result<Vec<u8>, String> {
    encode_storage_descriptor(layer, plan, blob, None)
}

/// Budget the standard mirrored metadata before allocating a jump table or
/// compressing a large layer. Oversized layers retain the raw sealed profile.
pub(crate) fn fits_zfile_descriptor(layer: &LayerRangeMap, plan: &OverlaybdBlobPlan) -> bool {
    let table = plan.stored_bytes.div_ceil(4096).saturating_mul(4);
    let data_checks = layer
        .ranges
        .iter()
        .filter(|range| range.source.is_some())
        .count() as u64
        * 32;
    let mirrored = table
        .saturating_add(plan.index.len() as u64)
        .saturating_add(data_checks)
        .saturating_add(4096 * 2 + 512 * 2);
    table <= crate::snapshot::format::overlaybd::zfile::MAX_JUMP_TABLE_BYTES as u64
        && mirrored
            .div_ceil(3)
            .saturating_mul(4)
            .saturating_add(16 * 1024)
            <= MAX_DESCRIPTOR_BYTES as u64
}

pub(crate) fn encode_zfile_descriptor(
    layer: &LayerRangeMap,
    plan: &OverlaybdBlobPlan,
    blob: &ObjectRef,
    zfile: &ZfileIndex,
) -> Result<Vec<u8>, String> {
    require(
        zfile.raw_bytes == plan.stored_bytes,
        "ZFile inner length conflicts with LSMT",
    )?;
    require(
        zfile.encoded_bytes <= MAX_BLOB_BYTES,
        "ZFile physical object exceeds maximum size",
    )?;
    encode_storage_descriptor(layer, plan, blob, Some(zfile))
}

fn encode_storage_descriptor(
    layer: &LayerRangeMap,
    plan: &OverlaybdBlobPlan,
    blob: &ObjectRef,
    zfile: Option<&ZfileIndex>,
) -> Result<Vec<u8>, String> {
    require(
        &plan_layer(layer)? == plan,
        "OverlayBD plan does not match logical layer",
    )?;
    validate_blob(layer, blob)?;
    let mut checksums = Vec::new();
    for range in &layer.ranges {
        if let Some(source) = &range.source {
            checksums.extend_from_slice(&digest_bytes(&source.checksum_sha256)?);
        }
    }
    let descriptor = Descriptor {
        format_version: 1,
        kind: layer.kind,
        content_sha256: layer.content_sha256.clone(),
        logical_bytes: layer.logical_bytes,
        unit_size_bytes: layer.unit_size_bytes,
        blob: blob.clone(),
        stored_bytes: plan.stored_bytes,
        header_base64: STANDARD.encode(&plan.header),
        index_base64: STANDARD.encode(&plan.index),
        trailer_base64: STANDARD.encode(&plan.trailer),
        data_checksums_base64: STANDARD.encode(checksums),
        zfile: zfile.map(|index| ZfileDescriptor {
            encoded_bytes: index.encoded_bytes,
            header_base64: STANDARD.encode(&index.header),
            jump_table_base64: STANDARD.encode(&index.jump_table),
            trailer_base64: STANDARD.encode(&index.trailer),
        }),
    };
    let payload = canonical_json_bytes(&descriptor).map_err(|error| error.to_string())?;
    require(
        payload.len() <= MAX_DESCRIPTOR_BYTES,
        "OverlayBD integrity descriptor exceeds size limit",
    )?;
    Ok(payload)
}

fn decode_base64(value: &str, maximum: usize) -> Result<Vec<u8>, String> {
    let max_encoded = maximum
        .checked_add(2)
        .and_then(|n| n.checked_div(3))
        .and_then(|n| n.checked_mul(4))
        .ok_or_else(|| "OverlayBD base64 bound overflow".to_owned())?;
    require(
        value.len() <= max_encoded,
        "OverlayBD base64 field is too large",
    )?;
    let bytes = STANDARD.decode(value).map_err(|error| error.to_string())?;
    require(
        bytes.len() <= maximum && STANDARD.encode(&bytes) == value,
        "OverlayBD base64 field is not canonical or exceeds bounds",
    )?;
    Ok(bytes)
}

fn push_non_data(
    ranges: &mut Vec<LayerRange>,
    kind: RangeKind,
    offset: u64,
    length: u64,
) -> Result<(), String> {
    if length == 0 {
        return Ok(());
    }
    if let Some(previous) = ranges.last_mut().filter(|range| range.kind == kind) {
        require(
            add(previous.logical_offset, previous.length)? == offset,
            "non-contiguous OverlayBD gap",
        )?;
        previous.length = add(previous.length, length)?;
    } else {
        require(
            ranges.len() < MAX_RANGES_PER_LAYER,
            "OverlayBD reconstructed range count exceeds limit",
        )?;
        ranges.push(LayerRange {
            kind,
            logical_offset: offset,
            length,
            source: None,
        });
    }
    Ok(())
}

#[derive(Clone, Copy)]
struct DataRun {
    logical: u64,
    physical: u64,
    length: u64,
}

fn flush_data_run(
    layer: &mut LayerRangeMap,
    blob: &ObjectRef,
    run: &mut Option<DataRun>,
    checksums: &[u8],
    checksum_cursor: &mut usize,
) -> Result<(), String> {
    let Some(run) = run.take() else {
        return Ok(());
    };
    let chunk_bytes = match layer.kind {
        LayerKind::Memory => layer.unit_size_bytes,
        LayerKind::Block => MAX_DATA_RANGE_BYTES,
    };
    let mut done = 0;
    while done < run.length {
        require(
            layer.ranges.len() < MAX_RANGES_PER_LAYER,
            "OverlayBD reconstructed range count exceeds limit",
        )?;
        let length = (run.length - done).min(chunk_bytes);
        let checksum_end = checksum_cursor
            .checked_add(32)
            .ok_or_else(|| "OverlayBD checksum cursor overflows".to_owned())?;
        let checksum = checksums
            .get(*checksum_cursor..checksum_end)
            .ok_or_else(|| "OverlayBD DATA checksum count is too small".to_owned())?;
        layer.ranges.push(LayerRange {
            kind: RangeKind::Data,
            logical_offset: add(run.logical, done)?,
            length,
            source: Some(DataSource {
                object_key: blob.object_key.clone(),
                object_sha256: blob.sha256.clone(),
                object_version: blob.object_version.clone(),
                pack_offset: add(run.physical, done)?,
                encoded_length: length,
                decoded_length: length,
                codec: DataCodec::Raw,
                checksum_sha256: digest_identity(checksum),
            }),
        });
        *checksum_cursor = checksum_end;
        done = add(done, length)?;
    }
    Ok(())
}

fn parse_descriptor(payload: &[u8]) -> Result<(LayerRangeMap, ObjectRef, u64), String> {
    require(
        payload.len() <= MAX_DESCRIPTOR_BYTES,
        "OverlayBD integrity descriptor exceeds size limit",
    )?;
    let descriptor: Descriptor =
        serde_json::from_slice(payload).map_err(|error| error.to_string())?;
    require(
        descriptor.format_version == 1,
        "unsupported OverlayBD descriptor version",
    )?;
    require(
        canonical_json_bytes(&descriptor).map_err(|error| error.to_string())? == payload,
        "OverlayBD descriptor is not canonical JSON",
    )?;
    validate_sha256(&descriptor.content_sha256)?;
    validate_geometry(
        descriptor.kind,
        descriptor.logical_bytes,
        descriptor.unit_size_bytes,
    )?;
    let header = decode_base64(&descriptor.header_base64, SPACE as usize)?;
    let trailer = decode_base64(&descriptor.trailer_base64, SPACE as usize)?;
    require(
        header.len() as u64 == SPACE && trailer.len() as u64 == SPACE,
        "OverlayBD header/trailer area must contain 4096 bytes",
    )?;
    let index = decode_base64(&descriptor.index_base64, MAX_INDEX_RECORDS * RECORD_BYTES)?;
    let checksums = decode_base64(
        &descriptor.data_checksums_base64,
        MAX_RANGES_PER_LAYER
            .checked_mul(32)
            .ok_or_else(|| "OverlayBD checksum limit overflow".to_owned())?,
    )?;
    require(
        checksums.len().is_multiple_of(32),
        "truncated OverlayBD DATA checksum",
    )?;
    lsmt::metadata(&header, true)?;
    let metadata = lsmt::metadata(&trailer, false)?;
    let index_offset = metadata.index_offset;
    let record_count = metadata.index_records;
    require(
        index.len().is_multiple_of(RECORD_BYTES)
            && record_count == (index.len() / RECORD_BYTES) as u64
            && index_offset >= SPACE
            && index_offset.is_multiple_of(SECTOR)
            && add(add(index_offset, index.len() as u64)?, SPACE)? == descriptor.stored_bytes
            && descriptor.stored_bytes <= MAX_BLOB_BYTES,
        "invalid OverlayBD index/blob geometry",
    )?;
    let mut layer = LayerRangeMap {
        registry_objects: None,
        registry: None,

        zfile: None,
        format_version: 1,
        kind: descriptor.kind,
        content_sha256: descriptor.content_sha256.clone(),
        logical_bytes: descriptor.logical_bytes,
        unit_size_bytes: descriptor.unit_size_bytes,
        ranges: Vec::new(),
    };
    validate_blob(&layer, &descriptor.blob)?;
    let unit = layer.unit_size_bytes;
    let mut logical_cursor = 0;
    let mut checksum_cursor = 0_usize;
    let mut padding = false;
    let mut data_run: Option<DataRun> = None;
    for record in index.chunks_exact(RECORD_BYTES) {
        let (logical, length, physical, zero, origin) = match lsmt::mapping(record)? {
            lsmt::Mapping::Padding { low, high } => {
                require(
                    low == INVALID_OFFSET && high == 0,
                    "invalid OverlayBD padding record",
                )?;
                padding = true;
                continue;
            }
            lsmt::Mapping::Extent {
                logical,
                length,
                physical,
                zero,
                origin,
            } => (logical, length, physical, zero, origin),
        };
        require(!padding, "OverlayBD valid mapping follows padding")?;
        let end = add(logical, length)?;
        require(
            origin == 0
                && length > 0
                && length.is_multiple_of(unit)
                && logical.is_multiple_of(unit)
                && logical >= logical_cursor
                && end <= layer.logical_bytes
                && physical >= SPACE
                && if zero {
                    physical <= index_offset
                } else {
                    add(physical, length)? <= index_offset
                },
            "invalid OverlayBD mapping alignment/order/physical bounds",
        )?;
        if logical > logical_cursor || zero {
            flush_data_run(
                &mut layer,
                &descriptor.blob,
                &mut data_run,
                &checksums,
                &mut checksum_cursor,
            )?;
        }
        push_non_data(
            &mut layer.ranges,
            RangeKind::Inherit,
            logical_cursor,
            logical - logical_cursor,
        )?;
        if zero {
            push_non_data(&mut layer.ranges, RangeKind::Zero, logical, length)?;
        } else if let Some(previous) = data_run.as_mut().filter(|previous| {
            previous.logical.checked_add(previous.length) == Some(logical)
                && previous.physical.checked_add(previous.length) == Some(physical)
        }) {
            previous.length = add(previous.length, length)?;
        } else {
            flush_data_run(
                &mut layer,
                &descriptor.blob,
                &mut data_run,
                &checksums,
                &mut checksum_cursor,
            )?;
            data_run = Some(DataRun {
                logical,
                physical,
                length,
            });
        }
        logical_cursor = end;
    }
    flush_data_run(
        &mut layer,
        &descriptor.blob,
        &mut data_run,
        &checksums,
        &mut checksum_cursor,
    )?;
    push_non_data(
        &mut layer.ranges,
        RangeKind::Inherit,
        logical_cursor,
        layer.logical_bytes - logical_cursor,
    )?;
    require(
        checksum_cursor == checksums.len(),
        "OverlayBD DATA checksum count is too large",
    )?;
    // Replanning validates the complete canonical IR and enforces exact standard
    // bytes (magic, UUID, flags, version, metadata/padding, packed data offsets,
    // coalescing and segment boundaries), not merely a similar private format.
    let canonical = plan_layer(&layer)?;
    require(
        canonical.header == header
            && canonical.index == index
            && canonical.trailer == trailer
            && canonical.stored_bytes == descriptor.stored_bytes,
        "OverlayBD mirrored metadata is not the canonical standard layer plan",
    )?;
    let physical_bytes = if let Some(wrapper) = descriptor.zfile {
        let index = ZfileIndex::from_owned_mirrors(
            decode_base64(&wrapper.header_base64, 512)?,
            decode_base64(&wrapper.trailer_base64, 512)?,
            decode_base64(
                &wrapper.jump_table_base64,
                crate::snapshot::format::overlaybd::zfile::MAX_JUMP_TABLE_BYTES,
            )?,
        )
        .map_err(|error| error.to_string())?;
        require(
            index.raw_bytes == descriptor.stored_bytes
                && index.encoded_bytes == wrapper.encoded_bytes
                && index.encoded_bytes <= MAX_BLOB_BYTES,
            "ZFile wrapper geometry conflicts with authenticated LSMT",
        )?;
        let encoded_bytes = index.encoded_bytes;
        layer.zfile = Some(Arc::new(index));
        encoded_bytes
    } else {
        descriptor.stored_bytes
    };
    Ok((layer, descriptor.blob, physical_bytes))
}

/// Decode once when the caller also needs strict object identity/size auditing.
pub(crate) fn decode_descriptor_with_blob(
    payload: &[u8],
) -> Result<(LayerRangeMap, ObjectRef, u64), String> {
    parse_descriptor(payload)
}

pub(crate) fn decode_descriptor(payload: &[u8]) -> Result<LayerRangeMap, String> {
    parse_descriptor(payload).map(|(layer, _, _)| layer)
}

#[cfg(test)]
pub(crate) fn descriptor_blob(payload: &[u8]) -> Result<(ObjectRef, u64), String> {
    parse_descriptor(payload).map(|(_, blob, stored_bytes)| (blob, stored_bytes))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::snapshot::metadata::sha256_identity;
    use std::io::Write;
    use std::path::PathBuf;

    struct Backing {
        file: File,
        path: PathBuf,
    }

    impl Drop for Backing {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.path);
        }
    }

    fn backing(bytes: &[u8]) -> Backing {
        let path = std::env::temp_dir().join(format!("rock-overlaybd-{}", uuid::Uuid::new_v4()));
        let mut file = File::options()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
            .unwrap();
        file.write_all(bytes).unwrap();
        Backing { file, path }
    }

    fn source(bytes: &[u8]) -> DataSource {
        DataSource {
            object_key: "local/provisional.pack".into(),
            object_sha256: sha256_identity(b"provisional"),
            object_version: None,
            pack_offset: 0,
            encoded_length: bytes.len() as u64,
            decoded_length: bytes.len() as u64,
            codec: DataCodec::Raw,
            checksum_sha256: sha256_identity(bytes),
        }
    }

    fn identify(mut layer: LayerRangeMap) -> LayerRangeMap {
        layer.content_sha256 = compute_layer_content_sha256(&layer).unwrap();
        layer.validate().unwrap();
        layer
    }

    fn mixed_layer() -> (LayerRangeMap, Vec<u8>) {
        let unit = MEMORY_LOAD_UNIT_BYTES;
        let mut raw = vec![0; (unit * 5) as usize];
        raw[..unit as usize].fill(b'A');
        raw[unit as usize..(unit * 2) as usize].fill(b'B');
        raw[(unit * 3) as usize..(unit * 4) as usize].fill(b'X');
        raw[(unit * 4) as usize..].fill(b'C');
        let mut ranges = Vec::new();
        for (page, kind) in [
            RangeKind::Data,
            RangeKind::Data,
            RangeKind::Zero,
            RangeKind::Inherit,
            RangeKind::Data,
        ]
        .into_iter()
        .enumerate()
        {
            let offset = page as u64 * unit;
            ranges.push(LayerRange {
                kind,
                logical_offset: offset,
                length: unit,
                source: (kind == RangeKind::Data)
                    .then(|| source(&raw[offset as usize..(offset + unit) as usize])),
            });
        }
        (
            identify(LayerRangeMap {
                registry_objects: None,
                registry: None,

                zfile: None,
                format_version: 1,
                kind: LayerKind::Memory,
                content_sha256: String::new(),
                logical_bytes: raw.len() as u64,
                unit_size_bytes: unit,
                ranges,
            }),
            raw,
        )
    }

    fn blob(layer: &LayerRangeMap, bytes: &[u8]) -> ObjectRef {
        let sha256 = sha256_identity(bytes);
        ObjectRef {
            object_key: format!(
                "layers/{}/1/{}/data/{}.commit",
                layer.kind.object_key_component(),
                &layer.content_sha256[7..],
                &sha256[7..]
            ),
            sha256,
            object_version: Some("immutable-version".into()),
        }
    }

    fn descriptor_fixture() -> (LayerRangeMap, OverlaybdBlobPlan, Vec<u8>) {
        let (layer, raw) = mixed_layer();
        let backing = backing(&raw);
        let plan = plan_layer(&layer).unwrap();
        let stream = plan
            .read_at(&backing.file, 0, plan.stored_bytes as usize)
            .unwrap();
        let reference = blob(&layer, &stream);
        let descriptor = encode_descriptor(&layer, &plan, &reference).unwrap();
        (layer, plan, descriptor)
    }

    fn mutate_descriptor(payload: &[u8], change: impl FnOnce(&mut Descriptor)) -> Vec<u8> {
        let mut descriptor: Descriptor = serde_json::from_slice(payload).unwrap();
        change(&mut descriptor);
        canonical_json_bytes(&descriptor).unwrap()
    }

    #[test]
    fn standard_layout_and_integrity_descriptor_roundtrip() {
        let (layer, plan, payload) = descriptor_fixture();
        assert_eq!(&plan.header[..8], MAGIC0);
        assert_eq!(&plan.header[8..24], &MAGIC1);
        assert_eq!(&plan.header[24..28], &390_u32.to_le_bytes());
        assert_eq!(&plan.header[28..32], &7_u32.to_le_bytes());
        assert_eq!(&plan.trailer[28..32], &6_u32.to_le_bytes());
        assert_eq!(plan.data.len(), 2);
        assert_eq!(plan.data[0].length, 8192);
        assert_eq!(plan.index_offset, 4096 + 3 * 4096);
        assert_eq!(plan.index.len(), 4096);
        assert_eq!(u64_at(&plan.trailer, 40).unwrap(), 256);
        let decoded = decode_descriptor(&payload).unwrap();
        assert_eq!(decoded.content_sha256, layer.content_sha256);
        assert_eq!(decoded.ranges[2].kind, RangeKind::Zero);
        assert_eq!(decoded.ranges[3].kind, RangeKind::Inherit);
        assert_eq!(decoded.ranges[0].source.as_ref().unwrap().pack_offset, 4096);
        assert_eq!(decoded.ranges[1].source.as_ref().unwrap().pack_offset, 8192);
        assert_eq!(
            decoded.ranges[4].source.as_ref().unwrap().pack_offset,
            12288
        );
        let (reference, stored_bytes) = descriptor_blob(&payload).unwrap();
        assert_eq!(stored_bytes, plan.stored_bytes);
        assert_eq!(
            encode_descriptor(&decoded, &plan, &reference).unwrap(),
            payload
        );
    }

    #[test]
    fn virtual_stream_slices_cross_all_boundaries_without_raw_holes() {
        let (layer, raw) = mixed_layer();
        let backing = backing(&raw);
        let plan = plan_layer(&layer).unwrap();
        let mut expected = plan.header.clone();
        expected.extend_from_slice(&raw[..8192]);
        expected.extend_from_slice(&raw[16384..]);
        expected.extend_from_slice(&plan.index);
        expected.extend_from_slice(&plan.trailer);
        assert_eq!(expected.len() as u64, plan.stored_bytes);
        for chunk in [1, 31, 511, 4095, 4096, 8193] {
            let mut output = Vec::new();
            while output.len() < expected.len() {
                output.extend(
                    plan.read_at(&backing.file, output.len() as u64, chunk)
                        .unwrap(),
                );
            }
            assert_eq!(output, expected);
        }
        assert!(plan
            .read_at(&backing.file, plan.stored_bytes, 1)
            .unwrap()
            .is_empty());
        assert!(plan
            .read_at(&backing.file, plan.stored_bytes + 1, 1)
            .is_err());
        assert!(plan
            .read_at(&backing.file, 0, MAX_READ_BYTES as usize + 1)
            .is_err());
        let short = self::backing(&[]);
        assert!(plan.read_at(&short.file, SPACE, 1).is_err());
        if let Ok(directory) = std::env::var("ROCK_OVERLAYBD_FIXTURE_DIR") {
            std::fs::create_dir_all(&directory).unwrap();
            std::fs::write(format!("{directory}/snapd-standard.lsmt"), expected).unwrap();
            let mut restored = raw;
            restored[12288..16384].fill(0);
            std::fs::write(format!("{directory}/snapd-standard.raw"), restored).unwrap();
        }
    }

    #[test]
    fn zero_and_inherit_layers_always_have_a_standard_blob() {
        for kind in [RangeKind::Zero, RangeKind::Inherit] {
            let layer = identify(LayerRangeMap {
                registry_objects: None,
                registry: None,

                zfile: None,
                format_version: 1,
                kind: LayerKind::Memory,
                content_sha256: String::new(),
                logical_bytes: 4096,
                unit_size_bytes: 4096,
                ranges: vec![LayerRange {
                    kind,
                    logical_offset: 0,
                    length: 4096,
                    source: None,
                }],
            });
            let plan = plan_layer(&layer).unwrap();
            assert!(plan.data.is_empty());
            assert_eq!(plan.index_offset, SPACE);
            assert_eq!(
                plan.stored_bytes,
                if kind == RangeKind::Zero { 12288 } else { 8192 }
            );
            let backing = backing(&[]);
            let bytes = plan.read_at(&backing.file, 0, 16384).unwrap();
            let payload = encode_descriptor(&layer, &plan, &blob(&layer, &bytes)).unwrap();
            assert_eq!(decode_descriptor(&payload).unwrap(), layer);
            assert_eq!(descriptor_blob(&payload).unwrap().1, plan.stored_bytes);
            if let Ok(directory) = std::env::var("ROCK_OVERLAYBD_FIXTURE_DIR") {
                std::fs::create_dir_all(&directory).unwrap();
                let name = if kind == RangeKind::Zero {
                    "zero"
                } else {
                    "inherit"
                };
                std::fs::write(format!("{directory}/{name}-standard.lsmt"), bytes).unwrap();
            }
        }
    }

    #[test]
    fn block_checksums_span_multiple_standard_index_records() {
        let unit = BLOCK_UNIT_BYTES;
        let bytes = vec![b'B'; MAX_DATA_RANGE_BYTES as usize];
        let layer = identify(LayerRangeMap {
            registry_objects: None,
            registry: None,

            zfile: None,
            format_version: 1,
            kind: LayerKind::Block,
            content_sha256: String::new(),
            logical_bytes: MAX_DATA_RANGE_BYTES + unit,
            unit_size_bytes: unit,
            ranges: vec![
                LayerRange {
                    kind: RangeKind::Data,
                    logical_offset: 0,
                    length: MAX_DATA_RANGE_BYTES,
                    source: Some(source(&bytes)),
                },
                LayerRange {
                    kind: RangeKind::Data,
                    logical_offset: MAX_DATA_RANGE_BYTES,
                    length: unit,
                    source: Some(source(&bytes[..unit as usize])),
                },
            ],
        });
        let plan = plan_layer(&layer).unwrap();
        assert_eq!(plan.data.len(), 1);
        assert_eq!(u64_at(&plan.index, 0).unwrap() >> 50, 127 * 128);
        assert_eq!(u64_at(&plan.index, 16).unwrap() & INVALID_OFFSET, 127 * 128);
        assert_eq!(u64_at(&plan.index, 16).unwrap() >> 50, 127 * 128);
        assert_eq!(u64_at(&plan.index, 32).unwrap() >> 50, 3 * 128);
        let reference = blob(&layer, b"not read during descriptor verification");
        let payload = encode_descriptor(&layer, &plan, &reference).unwrap();
        let decoded = decode_descriptor(&payload).unwrap();
        assert_eq!(decoded.ranges.len(), 2);
        assert_eq!(decoded.ranges[0].length, MAX_DATA_RANGE_BYTES);
        assert_eq!(decoded.ranges[1].length, unit);
        assert_eq!(decoded.content_sha256, layer.content_sha256);
        assert_eq!(
            decoded.ranges[1].source.as_ref().unwrap().pack_offset,
            SPACE + MAX_DATA_RANGE_BYTES
        );
        assert_eq!(
            encode_descriptor(&decoded, &plan, &reference).unwrap(),
            payload
        );
        if let Ok(directory) = std::env::var("ROCK_OVERLAYBD_FIXTURE_DIR") {
            std::fs::create_dir_all(&directory).unwrap();
            let mut raw = bytes;
            raw.resize((MAX_DATA_RANGE_BYTES + unit) as usize, b'B');
            let backing = backing(&raw);
            let stream = plan
                .read_at(&backing.file, 0, plan.stored_bytes as usize)
                .unwrap();
            std::fs::write(format!("{directory}/snapd-block16m.lsmt"), stream).unwrap();
            std::fs::write(format!("{directory}/snapd-block16m.raw"), raw).unwrap();
        }
    }

    #[test]
    fn one_tib_sparse_block_geometry_keeps_ranges_and_metadata_bounded() {
        let logical_bytes = 1024_u64 * 1024 * 1024 * 1024;
        let data_source = |length, label: &[u8]| DataSource {
            object_key: "provisional.pack".into(),
            object_sha256: sha256_identity(b"provisional"),
            object_version: None,
            pack_offset: 0,
            encoded_length: length,
            decoded_length: length,
            codec: DataCodec::Raw,
            checksum_sha256: sha256_identity(label),
        };
        // No raw file or logical-size buffer is allocated: only two synthetic
        // authenticated range digests are needed to exercise metadata geometry.
        let layer = identify(LayerRangeMap {
            registry_objects: None,
            registry: None,

            zfile: None,
            format_version: 1,
            kind: LayerKind::Block,
            content_sha256: String::new(),
            logical_bytes,
            unit_size_bytes: BLOCK_UNIT_BYTES,
            ranges: vec![
                LayerRange {
                    kind: RangeKind::Data,
                    logical_offset: 0,
                    length: MAX_DATA_RANGE_BYTES,
                    source: Some(data_source(MAX_DATA_RANGE_BYTES, b"first")),
                },
                LayerRange {
                    kind: RangeKind::Inherit,
                    logical_offset: MAX_DATA_RANGE_BYTES,
                    length: logical_bytes - MAX_DATA_RANGE_BYTES - BLOCK_UNIT_BYTES,
                    source: None,
                },
                LayerRange {
                    kind: RangeKind::Data,
                    logical_offset: logical_bytes - BLOCK_UNIT_BYTES,
                    length: BLOCK_UNIT_BYTES,
                    source: Some(data_source(BLOCK_UNIT_BYTES, b"last")),
                },
            ],
        });
        let plan = plan_layer(&layer).unwrap();
        assert_eq!(plan.data.len(), 2);
        assert_eq!(plan.index.len(), 4096);
        assert_eq!(
            plan.stored_bytes,
            MAX_DATA_RANGE_BYTES + BLOCK_UNIT_BYTES + 12288
        );
        let reference = blob(&layer, b"metadata-only fixture");
        let payload = encode_descriptor(&layer, &plan, &reference).unwrap();
        assert!(payload.len() < 20 * 1024);
        let decoded = decode_descriptor(&payload).unwrap();
        assert_eq!(decoded.logical_bytes, logical_bytes);
        assert_eq!(decoded.ranges.len(), 3);
        assert_eq!(decoded.content_sha256, layer.content_sha256);
        assert_eq!(
            decoded.ranges[2].logical_offset,
            logical_bytes - BLOCK_UNIT_BYTES
        );
    }

    #[test]
    fn one_tib_dense_block_metadata_preserves_compact_checksum_ranges() {
        let logical_bytes = 1024_u64 * 1024 * 1024 * 1024;
        let chunks = logical_bytes / MAX_DATA_RANGE_BYTES;
        assert_eq!(chunks, 65536);
        let synthetic = DataSource {
            object_key: "provisional.pack".into(),
            object_sha256: sha256_identity(b"provisional"),
            object_version: None,
            pack_offset: 0,
            encoded_length: MAX_DATA_RANGE_BYTES,
            decoded_length: MAX_DATA_RANGE_BYTES,
            codec: DataCodec::Raw,
            checksum_sha256: sha256_identity(b"synthetic identical chunk digest"),
        };
        // Exercise the full logical capacity with metadata alone. Never create
        // a backing file, allocate logical_bytes, or read the virtual blob.
        let layer = identify(LayerRangeMap {
            registry_objects: None,
            registry: None,

            zfile: None,
            format_version: 1,
            kind: LayerKind::Block,
            content_sha256: String::new(),
            logical_bytes,
            unit_size_bytes: BLOCK_UNIT_BYTES,
            ranges: (0..chunks)
                .map(|n| LayerRange {
                    kind: RangeKind::Data,
                    logical_offset: n * MAX_DATA_RANGE_BYTES,
                    length: MAX_DATA_RANGE_BYTES,
                    source: Some(synthetic.clone()),
                })
                .collect(),
        });
        let plan = plan_layer(&layer).unwrap();
        assert_eq!(plan.data.len(), 1);
        assert_eq!(plan.data[0].length, logical_bytes);
        assert!(plan.index.len() < 3 * 1024 * 1024);
        let reference = blob(&layer, b"metadata-only full capacity fixture");
        let payload = encode_descriptor(&layer, &plan, &reference).unwrap();
        assert!(payload.len() < 8 * 1024 * 1024);
        let decoded = decode_descriptor(&payload).unwrap();
        assert_eq!(decoded.logical_bytes, logical_bytes);
        assert_eq!(decoded.ranges.len(), chunks as usize);
        assert_eq!(decoded.content_sha256, layer.content_sha256);
        assert!(decoded
            .ranges
            .iter()
            .all(|range| range.length == MAX_DATA_RANGE_BYTES));
    }

    #[test]
    fn block_checksum_runs_stop_at_zero_and_inherit_boundaries() {
        let unit = BLOCK_UNIT_BYTES;
        let bytes = vec![b'B'; unit as usize];
        let ranges = [
            RangeKind::Data,
            RangeKind::Zero,
            RangeKind::Data,
            RangeKind::Inherit,
            RangeKind::Data,
        ]
        .into_iter()
        .enumerate()
        .map(|(n, kind)| LayerRange {
            kind,
            logical_offset: n as u64 * unit,
            length: unit,
            source: (kind == RangeKind::Data).then(|| source(&bytes)),
        })
        .collect();
        let layer = identify(LayerRangeMap {
            registry_objects: None,
            registry: None,

            zfile: None,
            format_version: 1,
            kind: LayerKind::Block,
            content_sha256: String::new(),
            logical_bytes: unit * 5,
            unit_size_bytes: unit,
            ranges,
        });
        let plan = plan_layer(&layer).unwrap();
        let reference = blob(&layer, b"metadata-only fixture");
        let payload = encode_descriptor(&layer, &plan, &reference).unwrap();
        let decoded = decode_descriptor(&payload).unwrap();
        assert_eq!(decoded.ranges.len(), 5);
        assert_eq!(decoded.ranges[1].kind, RangeKind::Zero);
        assert_eq!(decoded.ranges[3].kind, RangeKind::Inherit);
        assert_eq!(decoded.content_sha256, layer.content_sha256);
        let corrupt = mutate_descriptor(&payload, |descriptor| {
            let mut checksums = STANDARD.decode(&descriptor.data_checksums_base64).unwrap();
            checksums[0] ^= 1;
            descriptor.data_checksums_base64 = STANDARD.encode(checksums);
        });
        assert!(decode_descriptor(&corrupt).is_err());
        let mut invalid = layer;
        invalid.ranges[1].kind = RangeKind::Data;
        invalid.ranges[1].source = Some(source(&bytes));
        invalid = identify(invalid);
        assert!(plan_layer(&invalid).is_err());
    }

    #[test]
    fn descriptor_rejects_tampered_standard_metadata_and_object_geometry() {
        let (_, _, payload) = descriptor_fixture();
        for offset in [
            0, 8, 24, 28, 32, 40, 48, 56, 93, 130, 132, 133, 134, 390, 4095,
        ] {
            let corrupt = mutate_descriptor(&payload, |descriptor| {
                let mut header = STANDARD.decode(&descriptor.header_base64).unwrap();
                header[offset] ^= 1;
                descriptor.header_base64 = STANDARD.encode(header);
            });
            assert!(decode_descriptor(&corrupt).is_err(), "header byte {offset}");
            assert!(descriptor_blob(&corrupt).is_err());
        }
        let corrupt = mutate_descriptor(&payload, |descriptor| descriptor.stored_bytes += 1);
        assert!(decode_descriptor(&corrupt).is_err());
        let corrupt = mutate_descriptor(&payload, |descriptor| {
            descriptor.blob.object_key.push_str(".pack");
        });
        assert!(decode_descriptor(&corrupt).is_err());
        let corrupt = mutate_descriptor(&payload, |descriptor| descriptor.logical_bytes = u64::MAX);
        assert!(decode_descriptor(&corrupt).is_err());
        let corrupt = mutate_descriptor(&payload, |descriptor| {
            let mut trailer = STANDARD.decode(&descriptor.trailer_base64).unwrap();
            put_u64(&mut trailer, 32, u64::MAX);
            descriptor.trailer_base64 = STANDARD.encode(trailer);
        });
        assert!(decode_descriptor(&corrupt).is_err());
    }

    #[test]
    fn descriptor_rejects_overlap_padding_physical_bounds_and_checksum_count() {
        let (_, _, payload) = descriptor_fixture();
        for change in 0..5 {
            let corrupt = mutate_descriptor(&payload, |descriptor| {
                let mut index = STANDARD.decode(&descriptor.index_base64).unwrap();
                match change {
                    0 => put_u64(&mut index, 16, 8 << 50), // second mapping overlaps first
                    1 => put_u64(&mut index, 48, 0),       // zero-filled padding is not INVALID
                    2 => put_u64(&mut index, 8, 7),        // DATA inside header
                    3 => put_u64(&mut index, 8, 0x007f_ffff_ffff_ffff), // physical overflow
                    4 => put_u64(&mut index, 8, 8 | (1 << 56)), // unsupported warp tag
                    _ => unreachable!(),
                }
                descriptor.index_base64 = STANDARD.encode(index);
            });
            assert!(
                decode_descriptor(&corrupt).is_err(),
                "mapping mutation {change}"
            );
        }
        for extra in [false, true] {
            let corrupt = mutate_descriptor(&payload, |descriptor| {
                let mut checksums = STANDARD.decode(&descriptor.data_checksums_base64).unwrap();
                if extra {
                    checksums.extend_from_slice(&[0; 32]);
                } else {
                    checksums.truncate(checksums.len() - 32);
                }
                descriptor.data_checksums_base64 = STANDARD.encode(checksums);
            });
            assert!(decode_descriptor(&corrupt).is_err());
        }
    }

    #[test]
    fn descriptor_rejects_noncanonical_json_unknown_fields_and_short_metadata() {
        let (_, _, payload) = descriptor_fixture();
        assert!(decode_descriptor(&payload[..payload.len() - 1]).is_err());
        assert!(decode_descriptor(b"{}").is_err());
        let mut value: serde_json::Value = serde_json::from_slice(&payload).unwrap();
        value["another_range_map"] = serde_json::json!([]);
        assert!(decode_descriptor(&canonical_json_bytes(&value).unwrap()).is_err());
        let corrupt = mutate_descriptor(&payload, |descriptor| {
            descriptor.header_base64 = STANDARD.encode([0; 390]);
        });
        assert!(decode_descriptor(&corrupt).is_err());
        let corrupt = mutate_descriptor(&payload, |descriptor| {
            descriptor.content_sha256 = sha256_identity(b"different identity");
        });
        assert!(decode_descriptor(&corrupt).is_err());
    }

    #[test]
    fn zfile_descriptor_preserves_inner_offsets_and_authenticates_physical_identity() {
        let (logical, raw) = mixed_layer();
        let native = backing(&raw);
        let plan = plan_layer(&logical).unwrap();
        let encoded_file = backing(&[]);
        let encoded = crate::snapshot::format::overlaybd::zfile::encode_with_reader(
            plan.stored_bytes,
            &encoded_file.file,
            |offset, length| plan.read_at(&native.file, offset, length),
        )
        .unwrap();
        let mut physical = vec![0; encoded.index.encoded_bytes as usize];
        encoded_file.file.read_exact_at(&mut physical, 0).unwrap();
        let reference = blob(&logical, &physical);
        assert_eq!(reference.sha256, encoded.sha256);
        let payload = encode_zfile_descriptor(&logical, &plan, &reference, &encoded.index).unwrap();
        let (decoded, actual_blob, physical_size) = decode_descriptor_with_blob(&payload).unwrap();
        assert_eq!(actual_blob, reference);
        assert_eq!(physical_size, physical.len() as u64);
        assert_eq!(decoded.content_sha256, logical.content_sha256);
        assert_eq!(
            compute_layer_content_sha256(&decoded).unwrap(),
            logical.content_sha256
        );
        assert_eq!(plan_layer(&decoded).unwrap(), plan);
        let index = decoded.zfile.as_ref().unwrap();
        for range in &decoded.ranges {
            let Some(source) = &range.source else {
                continue;
            };
            let data = index
                .decode_range(source.pack_offset, range.length, 0, &physical)
                .unwrap();
            assert_eq!(sha256_identity(&data), source.checksum_sha256);
            assert_eq!(source.object_sha256, reference.sha256);
        }
        let wrong_length = mutate_descriptor(&payload, |descriptor| {
            descriptor.zfile.as_mut().unwrap().encoded_bytes += 1;
        });
        assert!(decode_descriptor(&wrong_length).is_err());
        let wrong_index = mutate_descriptor(&payload, |descriptor| {
            let wrapper = descriptor.zfile.as_mut().unwrap();
            let mut jump = STANDARD.decode(&wrapper.jump_table_base64).unwrap();
            jump[0] ^= 1;
            wrapper.jump_table_base64 = STANDARD.encode(jump);
        });
        assert!(decode_descriptor(&wrong_index).is_err());
    }

    #[test]
    fn zfile_metadata_budget_is_checked_before_large_layer_compression() {
        let (layer, _) = mixed_layer();
        let mut plan = plan_layer(&layer).unwrap();
        assert!(fits_zfile_descriptor(&layer, &plan));
        plan.stored_bytes =
            (crate::snapshot::format::overlaybd::zfile::MAX_JUMP_TABLE_BYTES as u64 / 4 + 1) * 4096;
        assert!(!fits_zfile_descriptor(&layer, &plan));
    }
}
