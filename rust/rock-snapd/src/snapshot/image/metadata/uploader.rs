//! Finite best-effort publication of learned image metadata.
//!
//! A read/merge/replace cycle preserves old-first hints when observed. The
//! transport replaces one complete object atomically; concurrent writers use
//! last-writer-wins semantics and this API does not claim a CAS merge.

use crate::snapshot::metadata::{
    merge_snapshot_hot_ranges, sha256_identity, SnapshotHotset, SnapshotImageMetadata,
};

use super::{ImageMetadataError, ObjectStoreImageMetadata, SnapshotImageMetadataReader};
use crate::snapshot::io::range_io::RangeWriter;

pub use crate::snapshot::SnapshotImageMetadataUploader;

#[async_trait::async_trait]
impl SnapshotImageMetadataUploader for ObjectStoreImageMetadata {
    async fn upload(&self, metadata: &SnapshotImageMetadata) -> Result<(), ImageMetadataError> {
        metadata.validate().map_err(ImageMetadataError::Invalid)?;
        let subject = metadata.subject().map_err(ImageMetadataError::Invalid)?;
        let mut merged = metadata.clone();
        let previous = match self.read(&subject).await {
            Ok(previous) => previous,
            Err(error) if error.is_invalid_hint() => None,
            Err(error) => return Err(error),
        };
        if let Some(previous) = previous {
            if previous.hotset.is_some() || metadata.hotset.is_some() {
                merged.hotset = Some(SnapshotHotset {
                    ranges: merge_snapshot_hot_ranges(
                        previous
                            .hotset
                            .as_ref()
                            .map(|h| h.ranges.as_slice())
                            .unwrap_or(&[]),
                        metadata
                            .hotset
                            .as_ref()
                            .map(|h| h.ranges.as_slice())
                            .unwrap_or(&[]),
                    ),
                });
            }
        }
        let payload = self.codec.encode(&merged)?;
        let key = self.codec.object_key(&subject)?;
        self.range_io
            .replace_optional_metadata(
                self.store.as_ref(),
                &key,
                &payload,
                &sha256_identity(&payload),
            )
            .await?;
        Ok(())
    }
}
