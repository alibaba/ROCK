//! Finite format authentication of one retained upload source. Core supplies
//! admission, cancellation and deadline; no background owner is started here.
use crate::snapshot::format::overlaybd::zfile::ZfileIndex;
use crate::snapshot::format::overlaybd_s3::data::{DataPackIdentity, DataPackSliceIdentity};
use crate::snapshot::transport::{DataPackSource, DataPackSourceError, SnapshotDataUploadError};
use sha2::{Digest, Sha256};
use std::future::Future;
use std::pin::Pin;
use std::time::Instant;
use tokio::sync::watch;
pub(crate) const ZFILE_SCAN_RAW_BATCH_BYTES: usize = 64 * 1024;
pub(crate) const ZFILE_SCAN_ENCODED_BATCH_BYTES: usize = 128 * 1024;
pub(crate) async fn scan_source(
    source: &dyn DataPackSource,
    identity: &DataPackIdentity,
    slices: &[DataPackSliceIdentity],
    chunk_bytes: usize,
    deadline: Instant,
    cancellation: &watch::Receiver<bool>,
) -> Result<(), SnapshotDataUploadError> {
    scan_source_bytes(
        source,
        identity,
        slices,
        chunk_bytes,
        deadline,
        cancellation,
        true,
    )
    .await
}

pub(crate) async fn scan_encoded_source(
    source: &dyn DataPackSource,
    identity: &DataPackIdentity,
    slices: &[DataPackSliceIdentity],
    zfile: Option<&ZfileIndex>,
    chunk_bytes: usize,
    deadline: Instant,
    cancellation: &watch::Receiver<bool>,
) -> Result<(), SnapshotDataUploadError> {
    let Some(index) = zfile else {
        return scan_source(
            source,
            identity,
            slices,
            chunk_bytes,
            deadline,
            cancellation,
        )
        .await;
    };
    if index.encoded_bytes != identity.length || !index.verify || index.block_size != 4096 {
        return Err(SnapshotDataUploadError::Invalid(
            "new ZFile DATA must use the declared physical size and LZ4-4KiB verify1".into(),
        ));
    }
    let physical_slices = index
        .metadata_regions()
        .into_iter()
        .filter(|(_, bytes)| !bytes.is_empty())
        .map(|(offset, bytes)| DataPackSliceIdentity {
            offset,
            length: bytes.len() as u64,
            expected_sha256: format!("sha256:{:x}", Sha256::digest(bytes)),
        })
        .collect::<Vec<_>>();
    // First pass authenticates every compressed byte, including the actual
    // header, jump table and trailer against the authenticated mirrors.
    scan_source(
        source,
        identity,
        &physical_slices,
        chunk_bytes,
        deadline,
        cancellation,
    )
    .await?;
    let decoded = DecodedZfileSource { source, index };
    let mut raw_identity = identity.clone();
    raw_identity.length = index.raw_bytes;
    // Second pass decodes the actual compressed source and checks every
    // DATA unit and all inner standard LSMT metadata, in virtual offsets.
    scan_source_bytes(
        &decoded,
        &raw_identity,
        slices,
        ZFILE_SCAN_RAW_BATCH_BYTES,
        deadline,
        cancellation,
        false,
    )
    .await
}

struct DecodedZfileSource<'a> {
    source: &'a dyn DataPackSource,
    index: &'a ZfileIndex,
}

impl DataPackSource for DecodedZfileSource<'_> {
    fn length(&self) -> u64 {
        self.index.raw_bytes
    }

    fn read_at<'a>(
        &'a self,
        offset: u64,
        max_bytes: usize,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<u8>, DataPackSourceError>> + Send + 'a>> {
        Box::pin(async move {
            if offset > self.index.raw_bytes || max_bytes > ZFILE_SCAN_RAW_BATCH_BYTES {
                return Err(DataPackSourceError::InvalidRange);
            }
            let length = (self.index.raw_bytes - offset).min(max_bytes as u64);
            if length == 0 {
                return Ok(Vec::new());
            }
            let span = self
                .index
                .compressed_span(offset, length)
                .map_err(|error| DataPackSourceError::Io(error.to_string()))?;
            if span.length > ZFILE_SCAN_ENCODED_BATCH_BYTES as u64 {
                return Err(DataPackSourceError::InvalidRange);
            }
            let payload = self
                .source
                .read_at(span.offset, span.length as usize)
                .await?;
            if payload.len() as u64 != span.length {
                return Err(DataPackSourceError::Io(
                    "ZFile source returned a short span".into(),
                ));
            }
            self.index
                .decode_range(offset, length, span.offset, &payload)
                .map_err(|error| DataPackSourceError::Io(error.to_string()))
        })
    }
}

#[allow(clippy::too_many_arguments)]
async fn scan_source_bytes(
    source: &dyn DataPackSource,
    identity: &DataPackIdentity,
    slices: &[DataPackSliceIdentity],
    chunk_bytes: usize,
    deadline: Instant,
    cancellation: &watch::Receiver<bool>,
    verify_full_hash: bool,
) -> Result<(), SnapshotDataUploadError> {
    let mut previous_end = 0_u64;
    // Layer range maps are ordered by guest logical offset, while several
    // guest ranges may be packed in a different physical order.  Scan the
    // immutable pack in its own canonical offset order and still reject any
    // genuine overlap.
    let mut ordered_slices = slices.iter().collect::<Vec<_>>();
    ordered_slices.sort_by(|left, right| {
        (left.offset, left.length, &left.expected_sha256).cmp(&(
            right.offset,
            right.length,
            &right.expected_sha256,
        ))
    });
    // Content deduplication may make several guest logical ranges reference
    // the exact same physical pack slice.  It is one immutable identity, not
    // an overlap; only partially overlapping or conflicting identities fail.
    ordered_slices.dedup();
    for slice in &ordered_slices {
        let end = slice
            .offset
            .checked_add(slice.length)
            .ok_or_else(|| SnapshotDataUploadError::Invalid("DATA slice range overflow".into()))?;
        if slice.length == 0
            || slice.offset < previous_end
            || end > identity.length
            || !sha256_identity(&slice.expected_sha256)
        {
            return Err(SnapshotDataUploadError::Invalid(
                "DATA slice identities are invalid or overlapping".into(),
            ));
        }
        previous_end = end;
    }
    let mut offset = 0_u64;
    let mut digest = Sha256::new();
    let mut slice_position = 0_usize;
    let mut slice_digest = Sha256::new();
    let mut slice_seen = 0_u64;
    while offset < identity.length {
        if *cancellation.borrow() {
            return Err(SnapshotDataUploadError::Cancelled);
        }
        if Instant::now() >= deadline {
            return Err(SnapshotDataUploadError::Timeout);
        }
        let requested = usize::try_from((identity.length - offset).min(chunk_bytes as u64))
            .unwrap_or(chunk_bytes);
        let chunk = source
            .read_at(offset, requested)
            .await
            .map_err(|error| SnapshotDataUploadError::Invalid(error.to_string()))?;
        if chunk.is_empty() || chunk.len() > requested {
            return Err(SnapshotDataUploadError::Invalid(
                "DATA source ended before its declared length".into(),
            ));
        }
        let chunk_end = offset.checked_add(chunk.len() as u64).ok_or_else(|| {
            SnapshotDataUploadError::Invalid("DATA source offset overflow".into())
        })?;
        while let Some(slice) = ordered_slices.get(slice_position) {
            if slice.offset >= chunk_end {
                break;
            }
            let slice_end = slice.offset + slice.length;
            let start = offset.max(slice.offset);
            let end = chunk_end.min(slice_end);
            if start < end {
                let local_start = usize::try_from(start - offset).map_err(|_| {
                    SnapshotDataUploadError::Invalid("DATA slice offset exceeds usize".into())
                })?;
                let local_end = usize::try_from(end - offset).map_err(|_| {
                    SnapshotDataUploadError::Invalid("DATA slice end exceeds usize".into())
                })?;
                slice_digest.update(&chunk[local_start..local_end]);
                slice_seen += end - start;
            }
            if slice_end > chunk_end {
                break;
            }
            let actual = format!("sha256:{:x}", slice_digest.finalize_reset());
            if slice_seen != slice.length || actual != slice.expected_sha256 {
                return Err(SnapshotDataUploadError::Invalid(format!(
                    "DATA slice checksum mismatch for {} at offset {} length {}: expected {}, got {} over {} bytes",
                    identity.object_key.as_str(),
                    slice.offset,
                    slice.length,
                    slice.expected_sha256,
                    actual,
                    slice_seen,
                )));
            }
            slice_seen = 0;
            slice_position += 1;
        }
        offset = chunk_end;
        if verify_full_hash {
            digest.update(&chunk);
        }
    }
    if slice_position != ordered_slices.len() || slice_seen != 0 {
        return Err(SnapshotDataUploadError::Invalid(
            "DATA source did not cover all declared slices".into(),
        ));
    }
    if !source
        .read_at(identity.length, 1)
        .await
        .map_err(|error| SnapshotDataUploadError::Invalid(error.to_string()))?
        .is_empty()
    {
        return Err(SnapshotDataUploadError::Invalid(format!(
            "DATA source exceeds declared length for {}",
            identity.object_key.as_str(),
        )));
    }
    let actual = format!("sha256:{:x}", digest.finalize());
    if verify_full_hash && actual != identity.expected_sha256 {
        return Err(SnapshotDataUploadError::Invalid(format!(
            "DATA pack checksum mismatch for {}: expected {}, got {}",
            identity.object_key.as_str(),
            identity.expected_sha256,
            actual,
        )));
    }
    Ok(())
}

fn lower_hex_64(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn sha256_identity(value: &str) -> bool {
    value.strip_prefix("sha256:").is_some_and(lower_hex_64)
}
