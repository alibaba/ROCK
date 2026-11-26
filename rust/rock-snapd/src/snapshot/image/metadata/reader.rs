//! Bounded, subject-checked reading of optional image metadata.

use crate::snapshot::metadata::{
    sha256_identity, SnapshotImageMetadata, SnapshotMetadataSubject,
    MAX_SNAPSHOT_IMAGE_METADATA_BYTES,
};

use super::{ImageMetadataError, ObjectStoreImageMetadata};
use crate::snapshot::io::range_io::RangeReader;

pub use crate::snapshot::SnapshotImageMetadataReader;

#[async_trait::async_trait]
impl SnapshotImageMetadataReader for ObjectStoreImageMetadata {
    async fn read(
        &self,
        subject: &SnapshotMetadataSubject,
    ) -> Result<Option<SnapshotImageMetadata>, ImageMetadataError> {
        let key = self.codec.object_key(subject)?;
        let Some(object) = self
            .range_io
            .read_optional_metadata(self.store.as_ref(), &key, MAX_SNAPSHOT_IMAGE_METADATA_BYTES)
            .await?
        else {
            return Ok(None);
        };
        if object.payload.len() as u64 > MAX_SNAPSHOT_IMAGE_METADATA_BYTES
            || object.metadata.size != object.payload.len() as u64
            || object.metadata.sha256 != sha256_identity(&object.payload)
        {
            return Err(ImageMetadataError::Invalid(
                "metadata size or SHA differs from the atomic GET body".into(),
            ));
        }
        Ok(Some(self.codec.decode(&object.payload, subject)?))
    }
}
