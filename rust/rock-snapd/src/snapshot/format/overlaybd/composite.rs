//! Composite OverlayBD blob view for the pinned external descriptor profile.
//!
//! This composes physical LSMT blobs into one logical BASE layer; it is not a
//! separate physical format or the public snapshot chain. Both descriptor
//! profiles use `super::lsmt` and `super::zfile` for their single-blob layout.
//!
//! This profile trusts the configured TLS registry and verifies ZFile CRCs. A
//! descriptor hash identifies the source and metadata; it is not a hash of all
//! disk bytes, and a range read does not verify the complete registry blob SHA.
//! Native DATA uses 64KiB runtime ranges. The one-million range budget supports
//! about 64GiB of covered DATA, reduced by ZERO runs; sparse logical disks may
//! still reach 1TiB. An oversized native map is rejected before allocation.

use std::fmt;
use std::future::Future;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use serde::{Deserialize, Serialize};

use crate::snapshot::format::overlaybd::zfile::{
    ZfileIndex, ZfileSpan, MAX_ENCODED_SPAN_BYTES, MAX_JUMP_TABLE_BYTES,
};
use crate::snapshot::io::range_io::RangeReader;
#[cfg(test)]
use crate::snapshot::transport::PhysicalRangeReader;
use crate::snapshot::transport::PhysicalRangeRequest;
use crate::snapshot::transport::{BlobRangeError, BlobRangeErrorKind, BlobRef};
use crate::snapshot::transport::{RangeReadError, RangeReadFailureClass};

use crate::snapshot::metadata::{
    canonical_json_bytes, sha256_identity, validate_sha256, DataCodec, DataSource, LayerKind,
    LayerRange, LayerRangeMap, RangeKind, MAX_DATA_RANGE_BYTES, MAX_RANGES_PER_LAYER,
};

const UNIT: u64 = 64 * 1024;
const MAX_LOGICAL_BYTES: u64 = 1024 * 1024 * 1024 * 1024;
const MAX_BLOB_BYTES: u64 = 2 * MAX_LOGICAL_BYTES;
const MAX_LAYERS: usize = 64;
const MAX_DESCRIPTOR_BYTES: usize = 128 * 1024 * 1024;
const MAX_INDEX_BYTES: usize = 16 * 1024 * 1024;
const MAX_METADATA_BYTES: usize = MAX_JUMP_TABLE_BYTES + MAX_INDEX_BYTES + MAX_LAYERS * 9216;
use super::lsmt::{self, u64_at, SECTOR, SPACE};
#[cfg(test)]
use super::lsmt::{INVALID_OFFSET, MAGIC, MAGIC_UUID};

#[derive(Debug)]
pub enum RegistryRootfsReadError {
    Source(BlobRangeError),
    Invalid(String),
    Integrity(String),
}

impl fmt::Display for RegistryRootfsReadError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Source(error) => write!(formatter, "registry rootfs: {error}"),
            Self::Invalid(message) => write!(formatter, "invalid registry rootfs: {message}"),
            Self::Integrity(message) => write!(formatter, "registry rootfs integrity: {message}"),
        }
    }
}

impl std::error::Error for RegistryRootfsReadError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Source(error) => Some(error),
            _ => None,
        }
    }
}

impl From<BlobRangeError> for RegistryRootfsReadError {
    fn from(error: BlobRangeError) -> Self {
        Self::Source(error)
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct LayerDescriptor {
    digest: String,
    size: u64,
    zfile_header_base64: String,
    zfile_trailer_base64: String,
    zfile_jumptable_base64: String,
    lsmt_header_base64: String,
    lsmt_trailer_base64: String,
    lsmt_index_base64: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Descriptor {
    format_version: u64,
    integrity: String,
    registry: String,
    repository: String,
    manifest_digest: String,
    platform: String,
    logical_bytes: u64,
    layers: Vec<LayerDescriptor>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RegistryRootfsLayer {
    pub digest: String,
    pub size: u64,
    pub zfile: ZfileIndex,
    lsmt_header: Vec<u8>,
    lsmt_trailer: Vec<u8>,
    lsmt_index: Vec<u8>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct Extent {
    offset: u64,
    length: u64,
    physical: u64,
    layer: usize,
    zero: bool,
}

impl Extent {
    fn end(self) -> u64 {
        self.offset + self.length
    }

    fn clipped(self, begin: u64, end: u64) -> Self {
        debug_assert!(self.offset <= begin && begin < end && end <= self.end());
        Self {
            offset: begin,
            length: end - begin,
            physical: if self.zero {
                self.physical
            } else {
                self.physical + begin - self.offset
            },
            ..self
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RegistryRootfs {
    registry: String,
    repository: String,
    manifest_digest: String,
    platform: String,
    logical_bytes: u64,
    layers: Vec<RegistryRootfsLayer>,
    // The final map resolves all 512-byte layer boundaries. Missing extents
    // are zero; explicit ZERO extents also mask older layers during merging.
    extents: Vec<Extent>,
    identity: String,
}

fn require(condition: bool, message: &str) -> Result<(), String> {
    if condition {
        Ok(())
    } else {
        Err(message.into())
    }
}

fn decode_mirror(value: &str, limit: usize) -> Result<Vec<u8>, String> {
    require(
        value.len() <= limit.div_ceil(3) * 4,
        "metadata mirror exceeds limit",
    )?;
    let bytes = STANDARD.decode(value).map_err(|error| error.to_string())?;
    require(
        bytes.len() <= limit && STANDARD.encode(&bytes) == value,
        "invalid canonical metadata base64",
    )?;
    Ok(bytes)
}

fn validate_origin(
    registry: &str,
    repository: &str,
    platform: &str,
    digest: &str,
) -> Result<(), String> {
    validate_sha256(digest)?;
    require(
        !registry.is_empty()
            && registry.len() <= 255
            && registry
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b".-:[]".contains(&b)),
        "invalid registry authority",
    )?;
    require(
        !repository.is_empty()
            && repository.len() <= 1024
            && repository.split('/').all(|part| {
                !part.is_empty()
                    && part != "."
                    && part != ".."
                    && part.bytes().all(|b| {
                        b.is_ascii_lowercase() || b.is_ascii_digit() || b"._-".contains(&b)
                    })
            }),
        "invalid registry repository",
    )?;
    require(
        !platform.is_empty()
            && platform.len() <= 128
            && platform
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"/_-.".contains(&b)),
        "invalid registry platform",
    )
}

fn lsmt_geometry(
    header: &[u8],
    trailer: &[u8],
    raw_bytes: u64,
) -> Result<(u64, usize, u64), String> {
    lsmt::metadata(header, true)?;
    let metadata = lsmt::metadata(trailer, false)?;
    require(
        raw_bytes >= 2 * SPACE && raw_bytes.is_multiple_of(SECTOR),
        "invalid LSMT file length",
    )?;
    let offset = metadata.index_offset;
    let count = metadata.index_records;
    let logical = metadata.logical_bytes;
    require(
        logical > 0 && logical <= MAX_LOGICAL_BYTES && logical.is_multiple_of(UNIT),
        "LSMT disk geometry is unsupported by the block runtime",
    )?;
    let bytes = count
        .checked_mul(16)
        .ok_or_else(|| "LSMT index overflows".to_owned())?;
    require(
        bytes <= MAX_INDEX_BYTES as u64
            && offset >= SPACE
            && offset.is_multiple_of(SECTOR)
            && offset
                .checked_add(bytes)
                .is_some_and(|end| end <= raw_bytes - SPACE),
        "LSMT index exceeds data file or metadata bounds",
    )?;
    Ok((offset, bytes as usize, logical))
}

fn parse_extents(
    layer: &RegistryRootfsLayer,
    ordinal: usize,
) -> Result<(u64, Vec<Extent>), String> {
    let (index_offset, bytes, logical) = lsmt_geometry(
        &layer.lsmt_header,
        &layer.lsmt_trailer,
        layer.zfile.raw_bytes,
    )?;
    require(
        layer.lsmt_index.len() == bytes,
        "LSMT mirrored index length differs",
    )?;
    let mut result = Vec::new();
    let mut previous_end = 0;
    let mut padding = false;
    for record in layer.lsmt_index.chunks_exact(16) {
        let lsmt::Mapping::Extent {
            logical: offset,
            length,
            physical,
            zero,
            ..
        } = lsmt::mapping(record)?
        else {
            padding = true;
            continue;
        };
        require(!padding, "LSMT mapping follows index padding")?;
        let end = offset
            .checked_add(length)
            .ok_or_else(|| "LSMT extent overflows".to_owned())?;
        require(
            length > 0
                && offset >= previous_end
                && end <= logical
                && (zero
                    || (physical >= SPACE
                        && physical
                            .checked_add(length)
                            .is_some_and(|end| end <= index_offset))),
            "invalid LSMT mapping order, geometry, or physical bounds",
        )?;
        // Ordinary files address their own data regardless of a stored origin
        // tag. Upstream do_load_index likewise resets ordinary-file tags to 0.
        push_extent(
            &mut result,
            Extent {
                offset,
                length,
                physical,
                layer: ordinal,
                zero,
            },
        )?;
        previous_end = end;
    }
    Ok((logical, result))
}

fn push_extent(output: &mut Vec<Extent>, next: Extent) -> Result<(), String> {
    if let Some(previous) = output.last_mut().filter(|previous| {
        previous.end() == next.offset
            && previous.zero == next.zero
            && (next.zero
                || (previous.layer == next.layer
                    && previous.physical.checked_add(previous.length) == Some(next.physical)))
    }) {
        previous.length += next.length;
        return Ok(());
    }
    require(
        output.len() < MAX_RANGES_PER_LAYER,
        "composed LSMT extent count exceeds limit",
    )?;
    output.push(next);
    Ok(())
}

/// Overlay ordered, nonoverlapping upper extents over a lower map in linear
/// time. A missing upper extent inherits lower; an explicit ZERO masks lower.
fn overlay(lower: &[Extent], upper: &[Extent]) -> Result<Vec<Extent>, String> {
    let mut output = Vec::new();
    let mut index = 0;
    let mut cursor = lower.first().map_or(0, |extent| extent.offset);
    for next in upper {
        while let Some(current) = lower
            .get(index)
            .copied()
            .filter(|current| current.end() <= next.offset)
        {
            if cursor < current.end() {
                push_extent(&mut output, current.clipped(cursor, current.end()))?;
            }
            index += 1;
            cursor = lower.get(index).map_or(0, |extent| extent.offset);
        }
        if let Some(current) = lower
            .get(index)
            .copied()
            .filter(|current| cursor < next.offset && current.offset < next.offset)
        {
            push_extent(&mut output, current.clipped(cursor, next.offset))?;
        }
        push_extent(&mut output, *next)?;
        while lower
            .get(index)
            .is_some_and(|current| current.end() <= next.end())
        {
            index += 1;
        }
        cursor = lower
            .get(index)
            .map_or(0, |current| current.offset.max(next.end()));
    }
    while let Some(current) = lower.get(index).copied() {
        if cursor < current.end() {
            push_extent(&mut output, current.clipped(cursor, current.end()))?;
        }
        index += 1;
        cursor = lower.get(index).map_or(0, |extent| extent.offset);
    }
    Ok(output)
}

fn runtime_range_count(intervals: &[(u64, u64)], logical_bytes: u64) -> Result<usize, String> {
    let mut count = 0_u64;
    let mut cursor = 0;
    for &(begin, end) in intervals {
        count = count
            .checked_add(u64::from(begin > cursor))
            .and_then(|count| count.checked_add((end - begin) / UNIT))
            .ok_or_else(|| "native rootfs runtime range count overflows".to_owned())?;
        cursor = end;
    }
    count = count
        .checked_add(u64::from(cursor < logical_bytes))
        .ok_or_else(|| "native rootfs runtime range count overflows".to_owned())?;
    require(
        count > 0 && count <= MAX_RANGES_PER_LAYER as u64,
        "native rootfs exceeds the 1048576 runtime range budget: 64KiB DATA units support about 64GiB of covered DATA, reduced by ZERO runs",
    )?;
    Ok(count as usize)
}

async fn invalidate_physical(
    source: &dyn RangeReader,
    registry: &str,
    repository: &str,
    layer: &BlobRef,
    offset: u64,
    length: u64,
) -> bool {
    let mut at = 0;
    let mut repair = false;
    while at < length {
        let take = (length - at).min(MAX_DATA_RANGE_BYTES);
        repair |= source
            .invalidate_object_range(
                registry,
                PhysicalRangeRequest {
                    namespace: Some(repository),
                    object_key: &layer.digest,
                    object_sha256: &layer.digest,
                    object_size: Some(layer.size),
                    object_version: None,
                    offset: offset + at,
                    length: take,
                },
            )
            .await
            .unwrap_or(false);
        at += take;
    }
    repair
}

async fn read_decoded_physical(
    source: &dyn RangeReader,
    registry: &str,
    repository: &str,
    layer: &BlobRef,
    span: ZfileSpan,
) -> Result<Vec<u8>, RegistryRootfsReadError> {
    for retry in 0..=1 {
        let bytes = read_physical_with(
            source,
            registry,
            repository,
            layer,
            span.offset,
            span.length,
            retry != 0,
        )
        .await?;
        match span.decode(&bytes) {
            Ok(decoded) => return Ok(decoded),
            Err(error) => {
                let repair = invalidate_physical(
                    source,
                    registry,
                    repository,
                    layer,
                    span.offset,
                    span.length,
                )
                .await;
                if retry == 0 && repair {
                    continue;
                }
                return Err(RegistryRootfsReadError::Integrity(error.to_string()));
            }
        }
    }
    unreachable!("physical cache repair is bounded to one retry")
}

async fn read_physical_with(
    source: &dyn RangeReader,
    registry: &str,
    repository: &str,
    layer: &BlobRef,
    offset: u64,
    length: u64,
    uncached: bool,
) -> Result<Vec<u8>, RegistryRootfsReadError> {
    if length > MAX_ENCODED_SPAN_BYTES
        || offset
            .checked_add(length)
            .is_none_or(|end| end > layer.size)
    {
        return Err(RegistryRootfsReadError::Invalid(
            "physical read exceeds bounded blob range".into(),
        ));
    }
    let mut result = Vec::with_capacity(length as usize);
    while (result.len() as u64) < length {
        let take = (length - result.len() as u64).min(MAX_DATA_RANGE_BYTES);
        let request = PhysicalRangeRequest {
            namespace: Some(repository),
            object_key: &layer.digest,
            object_sha256: &layer.digest,
            object_size: Some(layer.size),
            object_version: None,
            offset: offset + result.len() as u64,
            length: take,
        };
        let bytes = if uncached {
            source.read_object_range_uncached(registry, request).await
        } else {
            source.read_object_range(registry, request).await
        }
        .map_err(|error| {
            let kind = match &error {
                RangeReadError::Failure {
                    class: RangeReadFailureClass::Authorization,
                    ..
                } => BlobRangeErrorKind::Authorization,
                RangeReadError::Failure {
                    class: RangeReadFailureClass::Integrity,
                    ..
                } => BlobRangeErrorKind::Integrity,
                RangeReadError::Failure {
                    class: RangeReadFailureClass::Transient,
                    ..
                } => BlobRangeErrorKind::Transient,
                RangeReadError::InvalidRange => BlobRangeErrorKind::Invalid,
                _ => BlobRangeErrorKind::Permanent,
            };
            BlobRangeError::new(kind, error.to_string())
        })?;
        if bytes.len() as u64 != take {
            return Err(RegistryRootfsReadError::Integrity(
                "short registry range".into(),
            ));
        }
        result.extend_from_slice(&bytes);
    }
    Ok(result)
}

async fn read_inner(
    source: &dyn RangeReader,
    registry: &str,
    repository: &str,
    layer: &BlobRef,
    zfile: &ZfileIndex,
    offset: u64,
    length: u64,
) -> Result<Vec<u8>, RegistryRootfsReadError> {
    let span = zfile
        .compressed_span(offset, length)
        .map_err(|error| RegistryRootfsReadError::Invalid(error.to_string()))?;
    read_decoded_physical(source, registry, repository, layer, span).await
}

#[derive(Default)]
struct ImportBudget {
    jump_bytes: AtomicUsize,
    index_bytes: AtomicUsize,
}

fn reserve_metadata(
    counter: &AtomicUsize,
    bytes: usize,
    maximum: usize,
    message: &str,
) -> Result<(), RegistryRootfsReadError> {
    counter
        .fetch_update(Ordering::AcqRel, Ordering::Acquire, |current| {
            current.checked_add(bytes).filter(|total| *total <= maximum)
        })
        .map(|_| ())
        .map_err(|_| RegistryRootfsReadError::Invalid(message.into()))
}

async fn read_zfile_index(
    source: &dyn RangeReader,
    registry: &str,
    repository: &str,
    layer: &BlobRef,
    budget: &ImportBudget,
) -> Result<ZfileIndex, RegistryRootfsReadError> {
    for retry in 0..=1 {
        let mut spans = vec![(0, 512), (layer.size - 512, 512)];
        let mut reserved = None;
        let loaded = async {
            let header =
                read_physical_with(source, registry, repository, layer, 0, 512, retry != 0).await?;
            let trailer = read_physical_with(
                source,
                registry,
                repository,
                layer,
                layer.size - 512,
                512,
                retry != 0,
            )
            .await?;
            let count = u64_at(&trailer, 48).map_err(RegistryRootfsReadError::Invalid)?;
            let length = count
                .checked_mul(4)
                .filter(|length| *length <= MAX_JUMP_TABLE_BYTES as u64)
                .ok_or_else(|| {
                    RegistryRootfsReadError::Invalid("native jump table exceeds limit".into())
                })?;
            let offset = u64_at(&trailer, 40).map_err(RegistryRootfsReadError::Invalid)?;
            if offset
                .checked_add(length)
                .is_none_or(|end| end > layer.size)
            {
                return Err(RegistryRootfsReadError::Invalid(
                    "native jump table exceeds blob".into(),
                ));
            }
            reserve_metadata(
                &budget.jump_bytes,
                length as usize,
                MAX_JUMP_TABLE_BYTES,
                "rootfs jump tables exceed budget",
            )?;
            reserved = Some(length as usize);
            spans.push((offset, length));
            let table = read_physical_with(
                source,
                registry,
                repository,
                layer,
                offset,
                length,
                retry != 0,
            )
            .await?;
            let zfile = ZfileIndex::from_owned_mirrors(header, trailer, table)
                .map_err(|error| RegistryRootfsReadError::Integrity(error.to_string()))?;
            if zfile.encoded_bytes != layer.size || !zfile.verify {
                return Err(RegistryRootfsReadError::Invalid(
                    "native ZFile length differs or verify1 is absent".into(),
                ));
            }
            if zfile.raw_bytes < 2 * SPACE {
                return Err(RegistryRootfsReadError::Invalid(
                    "native LSMT file is truncated".into(),
                ));
            }
            Ok(zfile)
        }
        .await;
        match loaded {
            Ok(index) => return Ok(index),
            Err(error) => {
                if let Some(bytes) = reserved {
                    budget.jump_bytes.fetch_sub(bytes, Ordering::AcqRel);
                }
                let format_error = matches!(
                    &error,
                    RegistryRootfsReadError::Invalid(_) | RegistryRootfsReadError::Integrity(_)
                );
                let mut repair = false;
                if format_error {
                    for (offset, length) in spans {
                        repair |= invalidate_physical(
                            source, registry, repository, layer, offset, length,
                        )
                        .await;
                    }
                }
                if retry == 0 && repair {
                    continue;
                }
                return Err(error);
            }
        }
    }
    unreachable!("physical cache metadata repair is bounded to one retry")
}

async fn import_layer_metadata(
    source: &dyn RangeReader,
    registry: &str,
    repository: &str,
    layer: Option<&BlobRef>,
    budget: &ImportBudget,
) -> Result<Option<(u64, LayerDescriptor)>, RegistryRootfsReadError> {
    let Some(layer) = layer else {
        return Ok(None);
    };
    validate_sha256(&layer.digest).map_err(RegistryRootfsReadError::Invalid)?;
    if layer.size < 1024 || layer.size > MAX_BLOB_BYTES {
        return Err(RegistryRootfsReadError::Invalid(
            "invalid native blob length".into(),
        ));
    }
    let zfile = read_zfile_index(source, registry, repository, layer, budget).await?;
    let lsmt_header = read_inner(source, registry, repository, layer, &zfile, 0, SPACE).await?;
    let lsmt_trailer = read_inner(
        source,
        registry,
        repository,
        layer,
        &zfile,
        zfile.raw_bytes - SPACE,
        SPACE,
    )
    .await?;
    let (offset, length, logical) = lsmt_geometry(&lsmt_header, &lsmt_trailer, zfile.raw_bytes)
        .map_err(RegistryRootfsReadError::Invalid)?;
    reserve_metadata(
        &budget.index_bytes,
        length,
        MAX_INDEX_BYTES,
        "rootfs LSMT indexes exceed budget",
    )?;
    let index = read_inner(
        source,
        registry,
        repository,
        layer,
        &zfile,
        offset,
        length as u64,
    )
    .await?;
    Ok(Some((
        logical,
        LayerDescriptor {
            digest: layer.digest.clone(),
            size: layer.size,
            zfile_header_base64: STANDARD.encode(&zfile.header),
            zfile_trailer_base64: STANDARD.encode(&zfile.trailer),
            zfile_jumptable_base64: STANDARD.encode(&zfile.jump_table),
            lsmt_header_base64: STANDARD.encode(lsmt_header),
            lsmt_trailer_base64: STANDARD.encode(lsmt_trailer),
            lsmt_index_base64: STANDARD.encode(index),
        },
    )))
}

impl RegistryRootfs {
    pub fn registry(&self) -> &str {
        &self.registry
    }
    pub fn repository(&self) -> &str {
        &self.repository
    }
    pub fn manifest_digest(&self) -> &str {
        &self.manifest_digest
    }
    pub fn platform(&self) -> &str {
        &self.platform
    }
    pub fn logical_bytes(&self) -> u64 {
        self.logical_bytes
    }

    pub fn layers(&self) -> &[RegistryRootfsLayer] {
        &self.layers
    }

    /// Descriptor/source identity, not a complete logical-disk DATA digest.
    pub fn content_sha256(&self) -> Result<String, String> {
        Ok(self.identity.clone())
    }

    pub fn logical_object_key(&self) -> Result<String, String> {
        Ok(format!("registry-rootfs/{}.logical", &self.identity[7..]))
    }

    pub fn retained_bytes(&self) -> usize {
        std::mem::size_of::<Self>()
            + self.registry.capacity()
            + self.repository.capacity()
            + self.manifest_digest.capacity()
            + self.platform.capacity()
            + self.identity.capacity()
            + self.extents.capacity() * std::mem::size_of::<Extent>()
            + self.layers.capacity() * std::mem::size_of::<RegistryRootfsLayer>()
            + self
                .layers
                .iter()
                .map(|layer| {
                    layer.digest.capacity()
                        + layer.zfile.retained_bytes()
                        + layer.lsmt_header.capacity()
                        + layer.lsmt_trailer.capacity()
                        + layer.lsmt_index.capacity()
                })
                .sum::<usize>()
    }

    pub async fn import(
        source: &dyn RangeReader,
        registry: &str,
        repository: &str,
        manifest_digest: &str,
        platform: &str,
        layers: &[BlobRef],
    ) -> Result<Arc<Self>, String> {
        Self::import_typed(
            source,
            registry,
            repository,
            manifest_digest,
            platform,
            layers,
        )
        .await
        .map_err(|error| error.to_string())
    }

    pub async fn import_typed(
        source: &dyn RangeReader,
        registry: &str,
        repository: &str,
        manifest_digest: &str,
        platform: &str,
        layers: &[BlobRef],
    ) -> Result<Arc<Self>, RegistryRootfsReadError> {
        validate_origin(registry, repository, platform, manifest_digest)
            .map_err(RegistryRootfsReadError::Invalid)?;
        if layers.is_empty() || layers.len() > MAX_LAYERS {
            return Err(RegistryRootfsReadError::Invalid(
                "native layer count must be 1..=64".into(),
            ));
        }
        let mut descriptor = Descriptor {
            format_version: 1,
            integrity: "trusted-registry-tls-zfile-crc32c-v1".into(),
            registry: registry.into(),
            repository: repository.into(),
            manifest_digest: manifest_digest.into(),
            platform: platform.into(),
            logical_bytes: 0,
            layers: Vec::new(),
        };
        let budget = ImportBudget::default();
        // At most four layers read metadata concurrently. Results are appended
        // in manifest order; shared budgets reserve before each allocation.
        for group in layers.chunks(4) {
            let (a, b, c, d) = tokio::join!(
                import_layer_metadata(source, registry, repository, group.first(), &budget),
                import_layer_metadata(source, registry, repository, group.get(1), &budget),
                import_layer_metadata(source, registry, repository, group.get(2), &budget),
                import_layer_metadata(source, registry, repository, group.get(3), &budget),
            );
            for result in [a, b, c, d] {
                let Some((logical, layer)) = result? else {
                    continue;
                };
                if descriptor.logical_bytes != 0 && descriptor.logical_bytes != logical {
                    return Err(RegistryRootfsReadError::Invalid(
                        "native layers have different logical sizes".into(),
                    ));
                }
                descriptor.logical_bytes = logical;
                descriptor.layers.push(layer);
            }
        }
        Self::from_descriptor(descriptor).map_err(RegistryRootfsReadError::Invalid)
    }

    fn from_descriptor(descriptor: Descriptor) -> Result<Arc<Self>, String> {
        require(
            descriptor.format_version == 1
                && descriptor.integrity == "trusted-registry-tls-zfile-crc32c-v1",
            "unsupported registry rootfs descriptor profile",
        )?;
        validate_origin(
            &descriptor.registry,
            &descriptor.repository,
            &descriptor.platform,
            &descriptor.manifest_digest,
        )?;
        require(
            !descriptor.layers.is_empty() && descriptor.layers.len() <= MAX_LAYERS,
            "native layer count must be 1..=64",
        )?;
        let mut layers = Vec::new();
        let mut extents = Vec::new();
        let mut jump_bytes = 0_usize;
        let mut index_bytes = 0_usize;
        for entry in &descriptor.layers {
            validate_sha256(&entry.digest)?;
            require(
                entry.size >= 1024 && entry.size <= MAX_BLOB_BYTES,
                "invalid native blob length",
            )?;
            let jump = decode_mirror(&entry.zfile_jumptable_base64, MAX_JUMP_TABLE_BYTES)?;
            jump_bytes = jump_bytes
                .checked_add(jump.len())
                .ok_or_else(|| "jump table budget overflows".to_owned())?;
            require(
                jump_bytes <= MAX_JUMP_TABLE_BYTES,
                "rootfs jump tables exceed budget",
            )?;
            let zfile = ZfileIndex::from_owned_mirrors(
                decode_mirror(&entry.zfile_header_base64, 512)?,
                decode_mirror(&entry.zfile_trailer_base64, 512)?,
                jump,
            )
            .map_err(|error| error.to_string())?;
            require(
                zfile.encoded_bytes == entry.size && zfile.verify,
                "native ZFile length differs or verify1 is absent",
            )?;
            let lsmt_index = decode_mirror(&entry.lsmt_index_base64, MAX_INDEX_BYTES)?;
            index_bytes = index_bytes
                .checked_add(lsmt_index.len())
                .ok_or_else(|| "LSMT index budget overflows".to_owned())?;
            require(
                index_bytes <= MAX_INDEX_BYTES,
                "rootfs LSMT indexes exceed budget",
            )?;
            let layer = RegistryRootfsLayer {
                digest: entry.digest.clone(),
                size: entry.size,
                zfile,
                lsmt_header: decode_mirror(&entry.lsmt_header_base64, SPACE as usize)?,
                lsmt_trailer: decode_mirror(&entry.lsmt_trailer_base64, SPACE as usize)?,
                lsmt_index,
            };
            let (logical, upper) = parse_extents(&layer, layers.len())?;
            require(
                logical == descriptor.logical_bytes,
                "native layers have different logical sizes",
            )?;
            extents = overlay(&extents, &upper)?;
            layers.push(layer);
        }
        require(
            jump_bytes + index_bytes + layers.len() * 9216 <= MAX_METADATA_BYTES,
            "rootfs metadata exceeds budget",
        )?;
        let payload = canonical_json_bytes(&descriptor).map_err(|error| error.to_string())?;
        require(
            payload.len() <= MAX_DESCRIPTOR_BYTES,
            "rootfs descriptor exceeds 128MiB",
        )?;
        Ok(Arc::new(Self {
            registry: descriptor.registry,
            repository: descriptor.repository,
            manifest_digest: descriptor.manifest_digest,
            platform: descriptor.platform,
            logical_bytes: descriptor.logical_bytes,
            layers,
            extents,
            identity: sha256_identity(&payload),
        }))
    }

    fn descriptor(&self) -> Descriptor {
        Descriptor {
            format_version: 1,
            integrity: "trusted-registry-tls-zfile-crc32c-v1".into(),
            registry: self.registry.clone(),
            repository: self.repository.clone(),
            manifest_digest: self.manifest_digest.clone(),
            platform: self.platform.clone(),
            logical_bytes: self.logical_bytes,
            layers: self
                .layers
                .iter()
                .map(|layer| LayerDescriptor {
                    digest: layer.digest.clone(),
                    size: layer.size,
                    zfile_header_base64: STANDARD.encode(&layer.zfile.header),
                    zfile_trailer_base64: STANDARD.encode(&layer.zfile.trailer),
                    zfile_jumptable_base64: STANDARD.encode(&layer.zfile.jump_table),
                    lsmt_header_base64: STANDARD.encode(&layer.lsmt_header),
                    lsmt_trailer_base64: STANDARD.encode(&layer.lsmt_trailer),
                    lsmt_index_base64: STANDARD.encode(&layer.lsmt_index),
                })
                .collect(),
        }
    }

    pub fn encode_descriptor(&self) -> Result<Vec<u8>, String> {
        let payload =
            canonical_json_bytes(&self.descriptor()).map_err(|error| error.to_string())?;
        require(
            payload.len() <= MAX_DESCRIPTOR_BYTES && sha256_identity(&payload) == self.identity,
            "registry descriptor/source identity differs",
        )?;
        Ok(payload)
    }

    pub fn build_map(self: &Arc<Self>) -> Result<LayerRangeMap, String> {
        let mut intervals: Vec<(u64, u64)> = Vec::new();
        for extent in self.extents.iter().filter(|extent| !extent.zero) {
            let begin = extent.offset / UNIT * UNIT;
            let end = extent.end().div_ceil(UNIT) * UNIT;
            if let Some(previous) = intervals.last_mut().filter(|previous| previous.1 >= begin) {
                previous.1 = previous.1.max(end);
            } else {
                intervals.push((begin, end));
            }
        }
        // Count from merged coverage intervals before allocating any per-unit
        // source strings. Holes stay one ZERO run, never millions of entries.
        let range_count = runtime_range_count(&intervals, self.logical_bytes)?;
        let mut ranges = Vec::with_capacity(range_count);
        let mut cursor = 0;
        let key = self.logical_object_key()?;
        for (begin, end) in intervals {
            if begin > cursor {
                ranges.push(LayerRange {
                    kind: RangeKind::Zero,
                    logical_offset: cursor,
                    length: begin - cursor,
                    source: None,
                });
            }
            cursor = begin;
            while cursor < end {
                let length = UNIT;
                ranges.push(LayerRange {
                    kind: RangeKind::Data,
                    logical_offset: cursor,
                    length,
                    source: Some(DataSource {
                        object_key: key.clone(),
                        object_sha256: self.identity.clone(),
                        object_version: None,
                        pack_offset: cursor,
                        encoded_length: length,
                        decoded_length: length,
                        codec: DataCodec::RegistryRootfs,
                        checksum_sha256: String::new(),
                    }),
                });
                cursor += length;
            }
        }
        if cursor < self.logical_bytes {
            ranges.push(LayerRange {
                kind: RangeKind::Zero,
                logical_offset: cursor,
                length: self.logical_bytes - cursor,
                source: None,
            });
        }
        debug_assert_eq!(ranges.len(), range_count);
        Ok(LayerRangeMap {
            registry_objects: None,
            format_version: 1,
            kind: LayerKind::Block,
            content_sha256: self.identity.clone(),
            logical_bytes: self.logical_bytes,
            unit_size_bytes: UNIT,
            ranges,
            zfile: None,
            registry: Some(Arc::clone(self)),
        })
    }

    pub fn validate_map(self: &Arc<Self>, map: &LayerRangeMap) -> Result<(), String> {
        let expected = self.build_map()?;
        require(
            map.format_version == 1
                && map.kind == LayerKind::Block
                && map.content_sha256 == self.identity
                && map.logical_bytes == self.logical_bytes
                && map.unit_size_bytes == UNIT
                && map.zfile.is_none()
                && map
                    .registry
                    .as_ref()
                    .is_some_and(|pinned| pinned.identity == self.identity)
                && map.ranges == expected.ranges,
            "range map differs from pinned registry rootfs descriptor",
        )
    }

    pub async fn read_logical(
        &self,
        source: &dyn RangeReader,
        offset: u64,
        length: u64,
    ) -> Result<Vec<u8>, RegistryRootfsReadError> {
        self.read_logical_with(offset, length, |ordinal, span| async move {
            let layer = &self.layers[ordinal];
            let reference = BlobRef {
                digest: layer.digest.clone(),
                size: layer.size,
            };
            read_decoded_physical(source, &self.registry, &self.repository, &reference, span).await
        })
        .await
    }

    /// All Registry blobs use the same physical-cache coordinator as owned
    /// objects. Decode, source binding and CRC remain format responsibilities.
    pub(crate) async fn read_logical_ranges(
        &self,
        reader: &dyn crate::snapshot::io::range_io::RangeReader,
        runtime: &crate::RuntimeIdentity,
        offset: u64,
        length: u64,
        provider_timeout: Duration,
    ) -> Result<Vec<u8>, RegistryRootfsReadError> {
        use crate::snapshot::io::range_io::{RangeReadTransport, RangeRepresentation};
        self.read_logical_with(offset, length, |ordinal, span| async move {
            let layer = &self.layers[ordinal];
            if span.length > MAX_ENCODED_SPAN_BYTES
                || span.offset.checked_add(span.length).is_none_or(|end| end > layer.size) {
                return Err(RegistryRootfsReadError::Invalid("physical read exceeds bounded blob range".into()));
            }
            for retry in 0..=1 {
                let mut output = Vec::with_capacity(span.length as usize);
                while (output.len() as u64) < span.length {
                    let take = (span.length - output.len() as u64).min(MAX_DATA_RANGE_BYTES);
                    let request = registry_range_request(runtime, &self.registry, &self.repository, layer,
                        span.offset + output.len() as u64, take);
                    let result = if retry == 0 {
                        reader.read_range_with_timeout(request, provider_timeout).await
                    } else { reader.read_range_uncached_with_timeout(request, provider_timeout).await }
                        .map_err(registry_range_error)?;
                    if result.transport != RangeReadTransport::Provider
                        || result.representation != RangeRepresentation::EncodedObject
 || result.payload.len() as u64 != take {
                        return Err(RegistryRootfsReadError::Integrity(
                            "registry physical source returned non-encoded bytes or an incomplete span".into()));
                    }
                    output.extend_from_slice(&result.payload);
                }
                match span.decode(&output) {
                    Ok(decoded) => return Ok(decoded),
                    Err(error) => {
                        let mut repair = false;
                        let mut at = 0;
                        while at < span.length {
                            let take = (span.length - at).min(MAX_DATA_RANGE_BYTES);
                            let request = registry_range_request(runtime, &self.registry, &self.repository, layer, span.offset + at, take);
                            repair |= reader.invalidate_range(request).await.unwrap_or(false);
                            at += take;
                        }
                        if retry == 0 && repair { continue; }
                        return Err(RegistryRootfsReadError::Integrity(error.to_string()));
                    }
                }
            }
            unreachable!("physical cache repair is bounded to one retry")
        }).await
    }

    async fn read_logical_with<F, Fut>(
        &self,
        offset: u64,
        length: u64,
        mut fetch: F,
    ) -> Result<Vec<u8>, RegistryRootfsReadError>
    where
        F: FnMut(usize, ZfileSpan) -> Fut,
        Fut: Future<Output = Result<Vec<u8>, RegistryRootfsReadError>>,
    {
        if length > MAX_DATA_RANGE_BYTES
            || offset
                .checked_add(length)
                .is_none_or(|end| end > self.logical_bytes)
        {
            return Err(RegistryRootfsReadError::Invalid(
                "logical range exceeds disk or 16MiB window".into(),
            ));
        }
        let mut output = vec![0; length as usize];
        if length == 0 {
            return Ok(output);
        }
        let end = offset + length;
        let first = self
            .extents
            .partition_point(|extent| extent.end() <= offset);
        for extent in self.extents[first..]
            .iter()
            .take_while(|extent| extent.offset < end)
        {
            if extent.zero {
                continue;
            }
            let begin = extent.offset.max(offset);
            let stop = extent.end().min(end);
            let physical = extent.physical + begin - extent.offset;
            let span = self.layers[extent.layer]
                .zfile
                .compressed_span(physical, stop - begin)
                .map_err(|error| RegistryRootfsReadError::Invalid(error.to_string()))?;
            let decoded = fetch(extent.layer, span).await?;
            if decoded.len() as u64 != stop - begin {
                return Err(RegistryRootfsReadError::Integrity(
                    "decoded range length differs from plan".into(),
                ));
            }
            output[(begin - offset) as usize..(stop - offset) as usize].copy_from_slice(&decoded);
        }
        Ok(output)
    }
}

fn registry_range_request<'a>(
    runtime: &'a crate::RuntimeIdentity,
    registry: &'a str,
    repository: &'a str,
    layer: &'a RegistryRootfsLayer,
    offset: u64,
    length: u64,
) -> crate::snapshot::io::range_io::RangeReadRequest<'a> {
    use crate::snapshot::io::range_io::{RangeDataKind, RangeReadRequest};
    RangeReadRequest {
        runtime,
        kind: RangeDataKind::Block,
        object_key: &layer.digest,
        object_sha256: &layer.digest,
        expected_range_sha256: "",
        object_version: None,
        offset,
        length,
        preferred_p2p_source: None,
        authority: Some(registry),
        physical: Some(PhysicalRangeRequest {
            namespace: Some(repository),
            object_key: &layer.digest,
            object_sha256: &layer.digest,
            object_size: Some(layer.size),
            object_version: None,
            offset,
            length,
        }),
    }
}

fn registry_range_error(error: RangeReadError) -> RegistryRootfsReadError {
    let kind = match &error {
        RangeReadError::Failure {
            class: RangeReadFailureClass::Authorization,
            ..
        } => BlobRangeErrorKind::Authorization,
        RangeReadError::Failure {
            class: RangeReadFailureClass::Integrity,
            ..
        } => BlobRangeErrorKind::Integrity,
        RangeReadError::Failure {
            class: RangeReadFailureClass::Transient,
            ..
        } => BlobRangeErrorKind::Transient,
        RangeReadError::InvalidRange => BlobRangeErrorKind::Invalid,
        _ => BlobRangeErrorKind::Permanent,
    };
    RegistryRootfsReadError::Source(BlobRangeError::new(kind, error.to_string()))
}

pub fn decode_descriptor(payload: &[u8]) -> Result<Arc<RegistryRootfs>, String> {
    require(
        payload.len() <= MAX_DESCRIPTOR_BYTES,
        "registry descriptor exceeds 128MiB",
    )?;
    let descriptor: Descriptor =
        serde_json::from_slice(payload).map_err(|error| error.to_string())?;
    require(
        canonical_json_bytes(&descriptor).map_err(|error| error.to_string())? == payload,
        "registry descriptor is not canonical JSON",
    )?;
    RegistryRootfs::from_descriptor(descriptor)
}

#[cfg(test)]
pub(crate) fn test_registry_rootfs() -> Arc<RegistryRootfs> {
    test_registry_rootfs_with_units(4)
}

#[cfg(test)]
pub(crate) fn test_registry_rootfs_with_units(units: u64) -> Arc<RegistryRootfs> {
    assert!((1..=64).contains(&units));
    tests::fixture(
        units * UNIT,
        vec![vec![tests::Mapping::data(0, units * UNIT, 0x5a)]],
    )
    .0
}

#[cfg(test)]
pub(crate) fn test_registry_rootfs_fixture(units: u64) -> (Arc<RegistryRootfs>, Vec<u8>) {
    assert!((1..=64).contains(&units));
    let (rootfs, mut blobs) = tests::fixture(
        units * UNIT,
        vec![vec![tests::Mapping::data(0, units * UNIT, 0x5a)]],
    );
    (rootfs, blobs.remove(0))
}

#[cfg(test)]
pub(crate) fn test_registry_blob() -> (BlobRef, Vec<u8>) {
    tests::transport_fixture()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs::{File, OpenOptions};
    use std::os::unix::fs::FileExt;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, AtomicU8, AtomicUsize, Ordering};

    use crate::snapshot::format::overlaybd::zfile::encode_with_reader;
    use crate::snapshot::transport::BlobRangeErrorKind;

    static TEMP_ID: AtomicU64 = AtomicU64::new(0);

    struct Temporary {
        path: PathBuf,
        file: File,
    }

    impl Temporary {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "rock-registry-rootfs-{}-{}",
                std::process::id(),
                TEMP_ID.fetch_add(1, Ordering::Relaxed)
            ));
            let file = OpenOptions::new()
                .read(true)
                .write(true)
                .create_new(true)
                .open(&path)
                .unwrap();
            Self { path, file }
        }
    }

    impl Drop for Temporary {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.path);
        }
    }

    #[derive(Clone, Copy)]
    pub(super) struct Mapping {
        offset: u64,
        length: u64,
        value: Option<u8>,
        tag: u8,
    }

    impl Mapping {
        pub(super) fn data(offset: u64, length: u64, value: u8) -> Self {
            Self {
                offset,
                length,
                value: Some(value),
                tag: 0,
            }
        }
        fn zero(offset: u64, length: u64) -> Self {
            Self {
                offset,
                length,
                value: None,
                tag: 0,
            }
        }
    }

    fn put_u64(bytes: &mut [u8], position: usize, value: u64) {
        bytes[position..position + 8].copy_from_slice(&value.to_le_bytes());
    }

    fn raw_layer(logical: u64, mappings: &[Mapping]) -> (LayerDescriptor, Vec<u8>) {
        let mut raw = vec![0; SPACE as usize];
        let mut index = Vec::new();
        for mapping in mappings {
            assert!(mapping.offset.is_multiple_of(SECTOR) && mapping.length.is_multiple_of(SECTOR));
            let physical = raw.len() as u64;
            if let Some(value) = mapping.value {
                raw.resize(raw.len() + mapping.length as usize, value);
            }
            let low = (mapping.offset / SECTOR) | ((mapping.length / SECTOR) << 50);
            let high = (physical / SECTOR)
                | (u64::from(mapping.value.is_none()) << 55)
                | (u64::from(mapping.tag) << 56);
            index.extend_from_slice(&low.to_le_bytes());
            index.extend_from_slice(&high.to_le_bytes());
        }
        let index_offset = raw.len() as u64;
        if !index.is_empty() {
            while index.len() % SPACE as usize != 0 {
                index.extend_from_slice(&INVALID_OFFSET.to_le_bytes());
                index.extend_from_slice(&0_u64.to_le_bytes());
            }
        }
        let metadata = |header: bool| {
            let mut bytes = vec![0; SPACE as usize];
            bytes[..8].copy_from_slice(MAGIC);
            bytes[8..24].copy_from_slice(&MAGIC_UUID);
            bytes[24..28].copy_from_slice(&390_u32.to_le_bytes());
            bytes[28..32].copy_from_slice(&(if header { 7_u32 } else { 6 }).to_le_bytes());
            put_u64(&mut bytes, 32, index_offset);
            put_u64(&mut bytes, 40, (index.len() / 16) as u64);
            put_u64(&mut bytes, 48, logical);
            bytes[132] = 1;
            bytes[133] = 1;
            bytes
        };
        let header = metadata(true);
        let trailer = metadata(false);
        raw[..SPACE as usize].copy_from_slice(&header);
        raw.extend_from_slice(&index);
        raw.extend_from_slice(&trailer);
        let file = Temporary::new();
        let encoded = encode_with_reader(raw.len() as u64, &file.file, |offset, length| {
            Ok(raw[offset as usize..offset as usize + length].to_vec())
        })
        .unwrap();
        let mut blob = vec![0; encoded.index.encoded_bytes as usize];
        file.file.read_exact_at(&mut blob, 0).unwrap();
        (
            LayerDescriptor {
                digest: encoded.sha256,
                size: blob.len() as u64,
                zfile_header_base64: STANDARD.encode(encoded.index.header),
                zfile_trailer_base64: STANDARD.encode(encoded.index.trailer),
                zfile_jumptable_base64: STANDARD.encode(encoded.index.jump_table),
                lsmt_header_base64: STANDARD.encode(header),
                lsmt_trailer_base64: STANDARD.encode(trailer),
                lsmt_index_base64: STANDARD.encode(index),
            },
            blob,
        )
    }

    pub(super) fn fixture(
        logical: u64,
        mappings: Vec<Vec<Mapping>>,
    ) -> (Arc<RegistryRootfs>, Vec<Vec<u8>>) {
        let (layers, blobs) = mappings
            .iter()
            .map(|mappings| raw_layer(logical, mappings))
            .unzip();
        let descriptor = Descriptor {
            format_version: 1,
            integrity: "trusted-registry-tls-zfile-crc32c-v1".into(),
            registry: "example.com".into(),
            repository: "images/rootfs".into(),
            manifest_digest: sha256_identity(b"fixture manifest"),
            platform: "linux/amd64".into(),
            logical_bytes: logical,
            layers,
        };
        (RegistryRootfs::from_descriptor(descriptor).unwrap(), blobs)
    }

    async fn read_fixture(
        rootfs: &RegistryRootfs,
        blobs: &[Vec<u8>],
        offset: u64,
        length: u64,
    ) -> Result<Vec<u8>, RegistryRootfsReadError> {
        rootfs
            .read_logical_with(offset, length, |ordinal, span| async move {
                span.decode(
                    &blobs[ordinal][span.offset as usize..(span.offset + span.length) as usize],
                )
                .map_err(|error| RegistryRootfsReadError::Integrity(error.to_string()))
            })
            .await
    }

    #[tokio::test]
    async fn sector_mappings_compose_top_layers_zero_and_upper_holes() {
        let (rootfs, blobs) = fixture(
            3 * UNIT,
            vec![
                vec![Mapping::data(0, 2 * UNIT, 0x11)],
                vec![Mapping::data(512, 512, 0x22), Mapping::zero(1024, 512)],
                vec![Mapping::data(UNIT + 512, 512, 0x33)],
            ],
        );
        let mut expected = vec![0x11; (2 * UNIT) as usize];
        expected[512..1024].fill(0x22);
        expected[1024..1536].fill(0);
        expected[(UNIT + 512) as usize..(UNIT + 1024) as usize].fill(0x33);
        expected.resize((3 * UNIT) as usize, 0);
        assert_eq!(
            read_fixture(&rootfs, &blobs, 0, 3 * UNIT).await.unwrap(),
            expected
        );
        assert_eq!(
            read_fixture(&rootfs, &blobs, 500, UNIT + 1100)
                .await
                .unwrap(),
            expected[500..(UNIT + 1600) as usize]
        );
        let map = rootfs.build_map().unwrap();
        assert_eq!(map.ranges.len(), 3);
        assert_eq!(map.ranges[0].length, UNIT);
        assert_eq!(map.ranges[1].length, UNIT);
        assert_eq!(map.ranges[2].kind, RangeKind::Zero);
        rootfs.validate_map(&map).unwrap();
    }

    #[tokio::test]
    async fn source_tags_are_ignored_and_crc_failure_is_fail_closed() {
        let mut mapping = Mapping::data(512, 512, 0x45);
        mapping.tag = 237;
        let (rootfs, mut blobs) = fixture(UNIT, vec![vec![mapping]]);
        assert_eq!(
            read_fixture(&rootfs, &blobs, 512, 512).await.unwrap(),
            vec![0x45; 512]
        );
        let span = rootfs.layers[0].zfile.compressed_span(SPACE, 512).unwrap();
        blobs[0][span.offset as usize] ^= 1;
        assert!(matches!(
            read_fixture(&rootfs, &blobs, 0, UNIT).await,
            Err(RegistryRootfsReadError::Integrity(_))
        ));
    }

    #[tokio::test]
    async fn fully_masked_data_and_empty_upper_need_no_data_read() {
        let (rootfs, _) = fixture(
            UNIT,
            vec![
                vec![Mapping::data(0, UNIT, 1)],
                vec![Mapping::zero(0, UNIT)],
                vec![],
            ],
        );
        let map = rootfs.build_map().unwrap();
        assert_eq!(map.ranges.len(), 1);
        assert_eq!(map.ranges[0].kind, RangeKind::Zero);
        let bytes = rootfs
            .read_logical_with(0, UNIT, |_, _| async {
                panic!("ZERO must not read a source")
            })
            .await
            .unwrap();
        assert_eq!(bytes, vec![0; UNIT as usize]);
        assert!(rootfs
            .read_logical_with(0, 0, |_, _| async {
                panic!("empty read must not read a source")
            })
            .await
            .unwrap()
            .is_empty());
        assert!(rootfs
            .read_logical_with(UNIT, 1, |_, _| async { Ok(Vec::new()) })
            .await
            .is_err());
        assert!(rootfs
            .read_logical_with(0, MAX_DATA_RANGE_BYTES + 1, |_, _| async { Ok(Vec::new()) })
            .await
            .is_err());
    }

    #[test]
    fn sparse_one_tib_map_does_not_enumerate_load_units() {
        let (rootfs, _) = fixture(
            MAX_LOGICAL_BYTES,
            vec![vec![
                Mapping::data(512, 512, 7),
                Mapping::data(MAX_LOGICAL_BYTES - 512, 512, 8),
            ]],
        );
        let payload = rootfs.encode_descriptor().unwrap();
        let decoded = decode_descriptor(&payload).unwrap();
        assert_eq!(rootfs, decoded);
        let map = decoded.build_map().unwrap();
        assert_eq!(map.ranges.len(), 3);
        assert_eq!(map.ranges[0].length, UNIT);
        assert_eq!(map.ranges[1].length, MAX_LOGICAL_BYTES - 2 * UNIT);
        assert_eq!(map.ranges[2].logical_offset, MAX_LOGICAL_BYTES - UNIT);
        assert!(decoded.retained_bytes() < 32 * 1024);
        assert_eq!(map.content_sha256, sha256_identity(&payload));
    }

    #[test]
    fn runtime_data_ranges_are_single_units_with_exact_vector_capacity() {
        let (rootfs, _) = fixture(
            8 * UNIT,
            vec![vec![
                Mapping::data(512, 2 * UNIT, 7),
                Mapping::data(6 * UNIT + 512, 512, 8),
            ]],
        );
        let map = rootfs.build_map().unwrap();
        assert_eq!(map.ranges.capacity(), map.ranges.len());
        let data: Vec<_> = map
            .ranges
            .iter()
            .filter(|range| range.kind == RangeKind::Data)
            .collect();
        assert_eq!(data.len(), 4);
        for range in data {
            assert_eq!(range.length, UNIT);
            let source = range.source.as_ref().unwrap();
            assert_eq!(source.pack_offset, range.logical_offset);
            assert_eq!(source.encoded_length, UNIT);
            assert_eq!(source.decoded_length, UNIT);
        }
        assert_eq!(map.ranges[3].kind, RangeKind::Zero);
        assert_eq!(map.ranges[3].length, 3 * UNIT);
        rootfs.validate_map(&map).unwrap();
    }

    #[test]
    fn runtime_range_budget_preflights_dense_coverage_without_data_or_range_allocation() {
        let max_data = MAX_RANGES_PER_LAYER as u64 * UNIT;
        // These are just one pair of u64s. No 64GiB disk, DATA buffer or
        // million-entry runtime map is constructed, even for the reject case.
        assert_eq!(
            runtime_range_count(&[(0, max_data)], max_data).unwrap(),
            MAX_RANGES_PER_LAYER
        );
        assert!(runtime_range_count(&[(0, max_data)], MAX_LOGICAL_BYTES).is_err());
        assert_eq!(
            runtime_range_count(&[(0, max_data - UNIT)], MAX_LOGICAL_BYTES).unwrap(),
            MAX_RANGES_PER_LAYER
        );
        assert!(runtime_range_count(&[(0, max_data + UNIT)], MAX_LOGICAL_BYTES).is_err());
        assert_eq!(runtime_range_count(&[], MAX_LOGICAL_BYTES).unwrap(), 1);
        assert_eq!(
            runtime_range_count(
                &[(0, UNIT), (MAX_LOGICAL_BYTES - UNIT, MAX_LOGICAL_BYTES)],
                MAX_LOGICAL_BYTES
            )
            .unwrap(),
            3
        );
    }

    #[test]
    fn map_source_identity_and_descriptor_are_strictly_pinned() {
        let rootfs = super::test_registry_rootfs();
        let mut map = rootfs.build_map().unwrap();
        assert!(map.ranges[0]
            .source
            .as_ref()
            .unwrap()
            .checksum_sha256
            .is_empty());
        map.ranges[0].source.as_mut().unwrap().pack_offset += UNIT;
        assert!(rootfs.validate_map(&map).is_err());
        let payload = rootfs.encode_descriptor().unwrap();
        let mut value: serde_json::Value = serde_json::from_slice(&payload).unwrap();
        value["unknown"] = true.into();
        assert!(decode_descriptor(&canonical_json_bytes(&value).unwrap()).is_err());
        assert!(decode_descriptor(&payload[..payload.len() - 1]).is_err());
    }

    #[test]
    fn descriptor_rejects_bad_lsmt_geometry_overlap_and_zfile_profile() {
        let (rootfs, _) = fixture(
            UNIT,
            vec![vec![Mapping::data(0, 512, 1), Mapping::data(1024, 512, 2)]],
        );
        let original = rootfs.descriptor();
        for alteration in 0..5 {
            let mut descriptor = original.clone();
            let layer = &mut descriptor.layers[0];
            match alteration {
                0 => {
                    let mut bytes = STANDARD.decode(&layer.lsmt_header_base64).unwrap();
                    bytes[0] ^= 1;
                    layer.lsmt_header_base64 = STANDARD.encode(bytes);
                }
                1 => {
                    let mut bytes = STANDARD.decode(&layer.lsmt_index_base64).unwrap();
                    put_u64(&mut bytes, 16, 1 << 50);
                    layer.lsmt_index_base64 = STANDARD.encode(bytes);
                }
                2 => {
                    let mut bytes = STANDARD.decode(&layer.lsmt_index_base64).unwrap();
                    put_u64(&mut bytes, 8, (1_u64 << 55) - 1);
                    layer.lsmt_index_base64 = STANDARD.encode(bytes);
                }
                3 => descriptor.logical_bytes += UNIT,
                _ => {
                    let mut bytes = STANDARD.decode(&layer.zfile_trailer_base64).unwrap();
                    bytes[88] = 0;
                    layer.zfile_trailer_base64 = STANDARD.encode(bytes);
                }
            }
            assert!(
                decode_descriptor(&canonical_json_bytes(&descriptor).unwrap()).is_err(),
                "alteration {alteration}"
            );
        }
    }

    #[test]
    fn overlay_matches_sector_oracle_across_fragmented_layers() {
        // Deterministic pseudo-random intervals exercise split prefixes/suffixes,
        // touching ranges and gaps without one production entry per disk unit.
        let mut state = 7_u64;
        let mut lower = Vec::new();
        let mut oracle = vec![None; 128];
        for layer in 0..16 {
            let mut upper = Vec::new();
            let mut cursor = 0;
            while cursor < oracle.len() {
                state = state.wrapping_mul(6364136223846793005).wrapping_add(1);
                let length = (state as usize % 7 + 1).min(oracle.len() - cursor);
                if state >> 32 & 3 != 0 {
                    let zero = state >> 35 & 3 == 0;
                    upper.push(Extent {
                        offset: cursor as u64 * SECTOR,
                        length: length as u64 * SECTOR,
                        physical: (1000 + cursor) as u64 * SECTOR,
                        layer,
                        zero,
                    });
                    oracle[cursor..cursor + length].fill(Some((layer, zero)));
                }
                cursor += length;
            }
            lower = overlay(&lower, &upper).unwrap();
            let mut actual = vec![None; oracle.len()];
            for extent in &lower {
                actual[(extent.offset / SECTOR) as usize..(extent.end() / SECTOR) as usize]
                    .fill(Some((extent.layer, extent.zero)));
            }
            // ZERO origins may coalesce across layers; their content is identical.
            let content = |value: Option<(usize, bool)>| {
                value.map(|(layer, zero)| if zero { usize::MAX } else { layer })
            };
            assert_eq!(
                actual.into_iter().map(content).collect::<Vec<_>>(),
                oracle.iter().copied().map(content).collect::<Vec<_>>()
            );
        }
    }

    pub(super) fn transport_fixture() -> (BlobRef, Vec<u8>) {
        let (entry, blob) = raw_layer(
            9 * 1024 * 1024,
            &[
                Mapping::data(0, 4 * 1024 * 1024, 0x61),
                Mapping::data(4 * 1024 * 1024, 4 * 1024 * 1024, 0x62),
            ],
        );
        (
            BlobRef {
                digest: entry.digest,
                size: entry.size,
            },
            blob,
        )
    }

    struct MemorySource {
        identity: &'static str,
        reference: BlobRef,
        blob: Vec<u8>,
        transferred: AtomicUsize,
        mode: AtomicU8,
    }

    #[async_trait::async_trait]
    impl PhysicalRangeReader for MemorySource {
        fn source_id(&self) -> &str {
            self.identity
        }

        async fn read_physical_range(
            &self,
            request: PhysicalRangeRequest<'_>,
        ) -> Result<Vec<u8>, RangeReadError> {
            let offset = request.offset;
            let length = request.length;
            assert_eq!(request.namespace, Some("images/rootfs"));
            assert_eq!(request.object_key, self.reference.digest);
            assert_eq!(request.object_sha256, self.reference.digest);
            assert_eq!(request.object_size, Some(self.reference.size));
            assert!(length <= MAX_DATA_RANGE_BYTES);
            let mode = self.mode.load(Ordering::Relaxed);
            if mode >= 2 {
                return Err(RangeReadError::failure(
                    if mode == 2 {
                        RangeReadFailureClass::Authorization
                    } else {
                        RangeReadFailureClass::Transient
                    },
                    "injected physical source failure",
                ));
            }
            let mut payload = self.blob[offset as usize..(offset + length) as usize].to_vec();
            if mode == 1 {
                payload[0] ^= 1;
            }
            self.transferred.fetch_add(payload.len(), Ordering::Relaxed);
            Ok(payload)
        }
    }

    #[async_trait::async_trait]
    impl RangeReader for MemorySource {
        async fn read_object_range(
            &self,
            authority: &str,
            request: PhysicalRangeRequest<'_>,
        ) -> Result<Vec<u8>, RangeReadError> {
            if authority != self.identity {
                return Err(RangeReadError::InvalidRange);
            }
            self.read_physical_range(request).await
        }

        async fn read_range(
            &self,
            _request: crate::snapshot::io::range_io::RangeReadRequest<'_>,
        ) -> Result<crate::snapshot::io::range_io::RangeReadResult, RangeReadError> {
            Err(RangeReadError::InvalidRange)
        }
    }

    #[tokio::test]
    async fn generic_physical_cache_repairs_native_metadata_and_data_on_both_read_interfaces() {
        use crate::snapshot::io::range_io::RangeIO;
        use crate::snapshot::transport::interfaces::physical_cache::{
            PhysicalCacheKey, PhysicalRangeCache,
        };
        use crate::snapshot::transport::posix_file::LocalFileCache;
        struct CacheRoot(PathBuf);
        impl Drop for CacheRoot {
            fn drop(&mut self) {
                let _ = std::fs::remove_dir_all(&self.0);
            }
        }
        let directory = CacheRoot(
            std::fs::canonicalize(std::env::temp_dir())
                .unwrap()
                .join(format!("native-physical-cache-{}", uuid::Uuid::new_v4())),
        );
        let (reference, blob) = transport_fixture();
        let source = Arc::new(MemorySource {
            identity: "blobs.example",
            reference,
            blob,
            transferred: AtomicUsize::new(0),
            mode: AtomicU8::new(0),
        });
        let cache = LocalFileCache::new(directory.0.clone(), "native-physical").unwrap();
        let ranges = RangeIO::new_with_cache(source.clone(), None, Some(cache.clone()));
        let key = |offset, length| {
            PhysicalCacheKey::from_request(
                source.identity,
                PhysicalRangeRequest {
                    namespace: Some("images/rootfs"),
                    object_key: &source.reference.digest,
                    object_sha256: &source.reference.digest,
                    object_size: Some(source.reference.size),
                    object_version: None,
                    offset,
                    length,
                },
            )
            .unwrap()
        };
        let manifest = sha256_identity(b"cached native manifest");
        let rootfs = RegistryRootfs::import_typed(
            ranges.as_ref(),
            "blobs.example",
            "images/rootfs",
            &manifest,
            "linux/amd64",
            std::slice::from_ref(&source.reference),
        )
        .await
        .unwrap();
        let original_identity = rootfs.content_sha256().unwrap();
        let mut bad_header = source.blob[..512].to_vec();
        bad_header[0] ^= 1;
        cache
            .write(&key(0, 512), Arc::new(bad_header))
            .await
            .unwrap();
        let before = source.transferred.load(Ordering::SeqCst);
        let repaired = RegistryRootfs::import_typed(
            ranges.as_ref(),
            "blobs.example",
            "images/rootfs",
            &manifest,
            "linux/amd64",
            std::slice::from_ref(&source.reference),
        )
        .await
        .unwrap();
        assert_eq!(repaired.content_sha256().unwrap(), original_identity);
        assert!(
            source.transferred.load(Ordering::SeqCst) > before,
            "bad cached import mirrors must re-read metadata"
        );
        let extent = &rootfs.extents[0];
        let span = rootfs.layers[extent.layer]
            .zfile
            .compressed_span(extent.physical, 512)
            .unwrap();
        assert_eq!(
            rootfs.read_logical(ranges.as_ref(), 0, 512).await.unwrap(),
            vec![0x61; 512]
        );
        let warm = source.transferred.load(Ordering::SeqCst);
        assert_eq!(
            rootfs.read_logical(ranges.as_ref(), 0, 512).await.unwrap(),
            vec![0x61; 512]
        );
        assert_eq!(
            source.transferred.load(Ordering::SeqCst),
            warm,
            "physical source interface must hit the shared cache"
        );
        let mut corrupt =
            source.blob[span.offset as usize..(span.offset + span.length) as usize].to_vec();
        corrupt[0] ^= 1;
        cache
            .write(&key(span.offset, span.length), Arc::new(corrupt.clone()))
            .await
            .unwrap();
        assert_eq!(
            rootfs.read_logical(ranges.as_ref(), 0, 512).await.unwrap(),
            vec![0x61; 512]
        );
        assert_eq!(
            source.transferred.load(Ordering::SeqCst) - warm,
            span.length as usize,
            "direct physical CRC repair makes exactly one origin read"
        );
        let runtime = crate::RuntimeIdentity::new("native-cached-runtime");
        let before = source.transferred.load(Ordering::SeqCst);
        assert_eq!(
            rootfs
                .read_logical_ranges(ranges.as_ref(), &runtime, 0, 512, Duration::from_secs(30))
                .await
                .unwrap(),
            vec![0x61; 512]
        );
        assert_eq!(
            source.transferred.load(Ordering::SeqCst),
            before,
            "logical and direct readers must share physical bytes"
        );
        cache
            .write(&key(span.offset, span.length), Arc::new(corrupt))
            .await
            .unwrap();
        assert_eq!(
            rootfs
                .read_logical_ranges(ranges.as_ref(), &runtime, 0, 512, Duration::from_secs(30))
                .await
                .unwrap(),
            vec![0x61; 512]
        );
        assert_eq!(
            source.transferred.load(Ordering::SeqCst) - before,
            span.length as usize,
            "runtime CRC repair makes exactly one origin read"
        );
    }

    #[tokio::test]
    async fn injected_physical_source_import_and_read_preserve_crc_identity_and_errors() {
        let (reference, blob) = transport_fixture();
        let mut source = MemorySource {
            identity: "blobs.example",
            reference,
            blob,
            transferred: AtomicUsize::new(0),
            mode: AtomicU8::new(0),
        };
        let rootfs = RegistryRootfs::import_typed(
            &source,
            "blobs.example",
            "images/rootfs",
            &sha256_identity(b"in-memory manifest"),
            "linux/amd64",
            std::slice::from_ref(&source.reference),
        )
        .await
        .unwrap();
        assert!(source.transferred.load(Ordering::Relaxed) < source.blob.len() / 2);
        let bytes = rootfs
            .read_logical(&source, 4 * 1024 * 1024 - 512, 1024)
            .await
            .unwrap();
        assert_eq!(&bytes[..512], &[0x61; 512]);
        assert_eq!(&bytes[512..], &[0x62; 512]);
        source.mode.store(1, Ordering::Relaxed);
        assert!(matches!(
            rootfs.read_logical(&source, 0, 512).await,
            Err(RegistryRootfsReadError::Integrity(_))
        ));
        for (mode, expected) in [
            (2, BlobRangeErrorKind::Authorization),
            (3, BlobRangeErrorKind::Transient),
        ] {
            source.mode.store(mode, Ordering::Relaxed);
            assert!(matches!(
                rootfs.read_logical(&source, 0, 512).await,
                Err(RegistryRootfsReadError::Source(error)) if error.kind == expected
                    && error.retryable() == (mode == 3)
            ));
        }
        let before = source.transferred.load(Ordering::Relaxed);
        source.identity = "different.example";
        assert!(matches!(
            rootfs.read_logical(&source, 0, 512).await,
            Err(RegistryRootfsReadError::Source(error)) if error.kind == BlobRangeErrorKind::Invalid
        ));
        assert_eq!(source.transferred.load(Ordering::Relaxed), before);
    }

    #[tokio::test]
    async fn layer_metadata_import_is_bounded_and_preserves_manifest_order() {
        struct ConcurrentSource {
            references: Vec<BlobRef>,
            blob: Vec<u8>,
            active: AtomicUsize,
            peak: AtomicUsize,
        }
        #[async_trait::async_trait]
        impl RangeReader for ConcurrentSource {
            async fn read_object_range(
                &self,
                authority: &str,
                request: PhysicalRangeRequest<'_>,
            ) -> Result<Vec<u8>, RangeReadError> {
                assert_eq!(authority, "blobs.example");
                let offset = request.offset;
                let length = request.length;
                assert_eq!(request.namespace, Some("images/rootfs"));
                assert!(self
                    .references
                    .iter()
                    .any(|blob| blob.digest == request.object_key
                        && Some(blob.size) == request.object_size));
                let active = self.active.fetch_add(1, Ordering::SeqCst) + 1;
                self.peak.fetch_max(active, Ordering::SeqCst);
                tokio::task::yield_now().await;
                let payload = self.blob[offset as usize..(offset + length) as usize].to_vec();
                self.active.fetch_sub(1, Ordering::SeqCst);
                Ok(payload)
            }

            async fn read_range(
                &self,
                _request: crate::snapshot::io::range_io::RangeReadRequest<'_>,
            ) -> Result<crate::snapshot::io::range_io::RangeReadResult, RangeReadError>
            {
                Err(RangeReadError::InvalidRange)
            }
        }
        let (reference, blob) = transport_fixture();
        let references = (0..7)
            .map(|ordinal| BlobRef {
                digest: sha256_identity(format!("layer-{ordinal}").as_bytes()),
                size: reference.size,
            })
            .collect::<Vec<_>>();
        let source = ConcurrentSource {
            references: references.clone(),
            blob,
            active: AtomicUsize::new(0),
            peak: AtomicUsize::new(0),
        };
        let rootfs = RegistryRootfs::import_typed(
            &source,
            "blobs.example",
            "images/rootfs",
            &sha256_identity(b"manifest"),
            "linux/amd64",
            &references,
        )
        .await
        .unwrap();
        assert_eq!(
            rootfs
                .layers()
                .iter()
                .map(|layer| &layer.digest)
                .collect::<Vec<_>>(),
            references
                .iter()
                .map(|layer| &layer.digest)
                .collect::<Vec<_>>()
        );
        assert_eq!(source.active.load(Ordering::SeqCst), 0);
        assert_eq!(source.peak.load(Ordering::SeqCst), 4);
    }
}
