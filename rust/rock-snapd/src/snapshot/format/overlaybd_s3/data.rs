//! Format-authenticated upload artifact identities and scan metadata.
use crate::snapshot::format::overlaybd::zfile::ZfileIndex;
use crate::snapshot::format::overlaybd::MAX_BLOB_BYTES;
use crate::snapshot::transport::{DataPackSource, SnapshotDataUploadError};
use std::sync::Arc;
const MAX_DATA_PACK_BYTES: u64 = 16 * 1024 * 1024;
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub(crate) struct DataPackObjectKey(String);

impl DataPackObjectKey {
    pub(crate) fn new(value: impl Into<String>) -> Result<Self, SnapshotDataUploadError> {
        let value = value.into();
        let segments = value.split('/').collect::<Vec<_>>();
        let ["layers", kind, format, content, "data", object] = segments.as_slice() else {
            return Err(SnapshotDataUploadError::Invalid(
                "DATA pack key is outside the canonical layer data prefix".into(),
            ));
        };
        if !matches!(*kind, "memory" | "block")
            || format
                .parse::<u64>()
                .ok()
                .is_none_or(|version| version == 0)
            || !lower_hex_64(content)
            || !(object.strip_suffix(".pack").is_some_and(lower_hex_64)
                || (*format == "1" && object.strip_suffix(".commit").is_some_and(lower_hex_64)))
        {
            return Err(SnapshotDataUploadError::Invalid(
                "DATA pack key is not canonical".into(),
            ));
        }
        Ok(Self(value))
    }

    pub(crate) fn as_str(&self) -> &str {
        &self.0
    }

    pub(crate) fn is_overlaybd(&self) -> bool {
        self.0.ends_with(".commit")
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct DataPackIdentity {
    pub object_key: DataPackObjectKey,
    pub expected_sha256: String,
    pub length: u64,
}

impl DataPackIdentity {
    pub(crate) fn validate(&self) -> Result<(), SnapshotDataUploadError> {
        if self.length == 0
            || self.length
                > if self.object_key.is_overlaybd() {
                    MAX_BLOB_BYTES
                } else {
                    MAX_DATA_PACK_BYTES
                }
            || !sha256_identity(&self.expected_sha256)
        {
            return Err(SnapshotDataUploadError::Invalid(
                "DATA pack identity length or checksum is invalid".into(),
            ));
        }
        let expected = self.expected_sha256.trim_start_matches("sha256:");
        let actual = self
            .object_key
            .as_str()
            .rsplit('/')
            .next()
            .and_then(|value| {
                value.strip_suffix(if self.object_key.is_overlaybd() {
                    ".commit"
                } else {
                    ".pack"
                })
            });
        if actual != Some(expected) {
            return Err(SnapshotDataUploadError::Invalid(
                "DATA pack key does not bind its checksum".into(),
            ));
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct DataPackSliceIdentity {
    pub offset: u64,
    pub length: u64,
    pub expected_sha256: String,
}

#[derive(Clone)]
pub(crate) struct DataPackUploadRequest {
    pub identity: DataPackIdentity,
    pub source: Arc<dyn DataPackSource>,
    pub slices: Vec<DataPackSliceIdentity>,
    pub zfile: Option<Arc<ZfileIndex>>,
}

impl DataPackUploadRequest {
    pub(crate) fn minimum_scan_buffer_bytes(&self) -> u64 {
        if self.zfile.is_some() {
            (crate::snapshot::format::overlaybd_s3::scan::ZFILE_SCAN_RAW_BATCH_BYTES
                + crate::snapshot::format::overlaybd_s3::scan::ZFILE_SCAN_ENCODED_BATCH_BYTES)
                as u64
        } else {
            0
        }
    }
    pub(crate) async fn verify_source(
        &self,
        chunk_bytes: usize,
        deadline: std::time::Instant,
        cancellation: &tokio::sync::watch::Receiver<bool>,
    ) -> Result<(), SnapshotDataUploadError> {
        crate::snapshot::format::overlaybd_s3::scan::scan_encoded_source(
            self.source.as_ref(),
            &self.identity,
            &self.slices,
            self.zfile.as_deref(),
            chunk_bytes,
            deadline,
            cancellation,
        )
        .await
    }
}

impl std::fmt::Debug for DataPackUploadRequest {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("DataPackUploadRequest")
            .field("identity", &self.identity)
            .finish_non_exhaustive()
    }
}

fn lower_hex_64(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn sha256_identity(value: &str) -> bool {
    value.strip_prefix("sha256:").is_some_and(lower_hex_64)
}
