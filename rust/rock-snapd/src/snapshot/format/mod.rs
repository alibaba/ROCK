//! Persistent layer codecs and logical DATA adaptation.
//!
//! Opened snapshots expose ordered logical chains and lazy DATA reads. Layer
//! codecs map authenticated DATA/ZERO/INHERIT coverage onto physical bytes;
//! transport clients own byte delivery, and managers own VM backing residency.
//! The decode/encode dispatch functions below do not perform storage I/O.
//! Public layer, logical range, write-format and metadata-codec contracts live
//! in this module; concrete implementations live in their format submodules.

pub mod codec;
pub mod image_metadata;
pub mod overlaybd;
pub mod overlaybd_oci;
pub mod overlaybd_s3;
pub mod raw;
pub(crate) mod reader;

pub(crate) use self::overlaybd::writer::{EncodedLayer, LayerEncodeError};

use crate::snapshot::transport::StagedObjectWriter;
use std::sync::Arc;

pub use self::codec::{format_for_encoding, DecodedLayer, LayerFormatError};
pub use self::image_metadata::ImageMetadataCodecError;
pub use self::overlaybd_s3::capture::PreparedSnapshotSource;
pub use self::overlaybd_s3::plan::{ImmutableObject, LayerUploadPlan, SnapshotUploadPlan};
pub use crate::snapshot::io::publication::{
    SnapshotPublicationContext, SnapshotUploadError, UploadedSnapshot,
};
use crate::snapshot::metadata::{
    LayerKind, LayerRangeMap, RangeMapEncoding, SnapshotImageMetadata, SnapshotManifest,
    SnapshotMetadataSubject, SnapshotSpec, StorageDescriptor, VmmCompatibility,
};
use crate::snapshot::{LazyLoadInterface, SnapshotInstance};

/// Snapshot envelope decoding is separate from layer encoding and byte I/O.
/// Both profiles produce the same immutable metadata/view representation.
pub(crate) struct DecodedSnapshotMetadata {
    pub metadata: Arc<dyn SnapshotInstance>,
    pub metadata_bytes: usize,
}

#[async_trait::async_trait]
pub(crate) trait SnapshotMetadataFormat: std::fmt::Debug + Send + Sync {
    fn validate_spec_layout(
        &self,
        spec: &SnapshotSpec,
        snapshot_id: &str,
        expected_storage: Option<&StorageDescriptor>,
    ) -> Result<(), crate::snapshot::io::snapshot_io::SnapshotLoadError>;
    async fn load(
        &self,
        io: &crate::snapshot::io::snapshot_io::SnapshotMetadataReadContext<'_>,
        spec: SnapshotSpec,
        snapshot_id: &str,
        verify_data_objects: bool,
    ) -> Result<DecodedSnapshotMetadata, crate::snapshot::io::snapshot_io::SnapshotLoadError>;
}

pub(crate) fn snapshot_metadata_format(spec: &SnapshotSpec) -> &'static dyn SnapshotMetadataFormat {
    match spec.storage.provider {
        crate::snapshot::metadata::StorageProvider::Registry => {
            &overlaybd_oci::OverlaybdOciMetadata
        }
        _ => &overlaybd_s3::OverlaybdS3Metadata,
    }
}

pub(crate) fn validate_snapshot_id(
    snapshot_id: &str,
) -> Result<(), crate::snapshot::io::snapshot_io::SnapshotLoadError> {
    use crate::snapshot::io::snapshot_io::SnapshotLoadError;
    if snapshot_id.is_empty()
        || snapshot_id.len() > 128
        || !snapshot_id.bytes().enumerate().all(|(index, value)| {
            value.is_ascii_alphanumeric() || (index > 0 && matches!(value, b'.' | b'_' | b'-'))
        })
    {
        return Err(SnapshotLoadError::InvalidSnapshotMetadata(
            "snapshot ID is not a canonical path segment".into(),
        ));
    }
    Ok(())
}

/// Shared snapshot identifiers and paths belong to the common envelope model.
pub(crate) fn validate_snapshot_paths(
    spec: &SnapshotSpec,
    snapshot_id: &str,
) -> Result<(), crate::snapshot::io::snapshot_io::SnapshotLoadError> {
    use crate::snapshot::io::snapshot_io::SnapshotLoadError;
    validate_snapshot_id(snapshot_id)?;
    let prefix = format!("snapshots/{snapshot_id}/");
    if spec.manifest.object_key != format!("{prefix}manifest.json")
        || spec.rootfs.index_object_key != format!("{prefix}rootfs.index")
        || spec
            .memory
            .as_ref()
            .is_some_and(|value| value.index_object_key != format!("{prefix}memory.index"))
        || spec
            .vm_state
            .as_ref()
            .is_some_and(|value| value.object_key != format!("{prefix}vm-state.pack"))
    {
        return Err(SnapshotLoadError::InvalidSnapshotMetadata(
            "snapshot metadata uses a non-canonical object path".into(),
        ));
    }
    Ok(())
}

// Format contracts describe logical layers and write artifacts, never task ownership.
pub trait LayerFormatInterface: Send + Sync {
    fn name(&self) -> &'static str;
    fn encoding(&self) -> Option<RangeMapEncoding>;
    fn decode(&self, payload: &[u8]) -> Result<DecodedLayer, LayerFormatError>;
    fn open_reader(&self, map: Arc<LayerRangeMap>) -> Arc<dyn LayerViewInterface>;
    fn allows_p2p(&self) -> bool {
        true
    }
    fn allows_background_prefetch(&self) -> bool {
        true
    }
}

/// Per-layer format metadata shared by concrete local/raw, snapd, OverlayBD
/// and Registry readers. Snapshot IDs and ordering belong to the aggregator.
pub trait LayerMetadataInterface: Send + Sync {
    fn format_name(&self) -> &'static str;
    fn layer_kind(&self) -> LayerKind;
    fn content_identity(&self) -> &str;
    fn logical_bytes(&self) -> u64;
    fn unit_size_bytes(&self) -> u64;
    fn allows_p2p(&self) -> bool;
    fn allows_background_prefetch(&self) -> bool;
}

/// The same concrete object supplies logical metadata and lazy DATA reads.
pub trait LayerViewInterface: LayerMetadataInterface + LazyLoadInterface {}

impl<T: LayerMetadataInterface + LazyLoadInterface> LayerViewInterface for T {}

/// Runtime ownership enters the format layer only as a retained read source.
pub trait SnapshotUploadSource: SnapshotInstance {
    fn registry_sources(&self) -> Vec<Arc<overlaybd_oci::RegistryObjectMap>> {
        Vec::new()
    }

    fn prepare_upload_source(
        &self,
        scratch: &dyn Fn(u64) -> Result<Arc<dyn StagedObjectWriter>, SnapshotUploadError>,
    ) -> Result<PreparedSnapshotSource, SnapshotUploadError>;
}

#[async_trait::async_trait]
pub trait SnapshotUploadInterface: std::fmt::Debug + Send + Sync {
    fn prepare_upload(
        &self,
        source: &dyn SnapshotUploadSource,
        storage: StorageDescriptor,
        compatibility: VmmCompatibility,
        scratch: &dyn Fn(u64) -> Result<Arc<dyn StagedObjectWriter>, SnapshotUploadError>,
    ) -> Result<SnapshotUploadPlan, SnapshotUploadError>;
    fn validate_prepared(&self, plan: &SnapshotUploadPlan) -> Result<(), SnapshotUploadError>;
    async fn validate_uploaded_artifacts(
        &self,
        plan: &SnapshotUploadPlan,
        receipts: &[crate::snapshot::transport::DataPackUploadReceipt],
        context: &SnapshotPublicationContext<'_>,
    ) -> Result<SnapshotManifest, SnapshotUploadError>;

    fn root_metadata<'a>(
        &self,
        plan: &'a SnapshotUploadPlan,
    ) -> Result<&'a ImmutableObject, SnapshotUploadError>;
    fn validate_published(&self, published: &UploadedSnapshot) -> Result<(), SnapshotUploadError>;
}

pub trait SnapshotImageMetadataCodec: std::fmt::Debug + Send + Sync {
    fn object_key(
        &self,
        subject: &SnapshotMetadataSubject,
    ) -> Result<String, ImageMetadataCodecError>;
    fn encode(&self, metadata: &SnapshotImageMetadata) -> Result<Vec<u8>, ImageMetadataCodecError>;
    fn decode(
        &self,
        payload: &[u8],
        subject: &SnapshotMetadataSubject,
    ) -> Result<SnapshotImageMetadata, ImageMetadataCodecError>;
}

/// Decode only the format explicitly selected by authenticated chain metadata.
/// Never guess a different format after an invalid or mismatched descriptor.
pub(crate) fn decode_layer(
    encoding: Option<RangeMapEncoding>,
    payload: &[u8],
) -> Result<DecodedLayer, LayerFormatError> {
    let encoding = encoding
        .ok_or_else(|| LayerFormatError::Invalid("persistent layer encoding is required".into()))?;
    codec::format_for_encoding(encoding).decode(payload)
}

/// The default write policy is a standard OverlayBD layer with ZFile-LZ4 when
/// its authenticated descriptor fits. The caller owns backing/scratch lifetime.
pub(crate) fn encode_layer<E>(
    layer: LayerRangeMap,
    backing: &std::fs::File,
    create_scratch: &dyn Fn(u64) -> Result<Arc<dyn StagedObjectWriter>, E>,
) -> Result<EncodedLayer, E>
where
    E: From<LayerEncodeError>,
{
    overlaybd::writer::encode(layer, backing, create_scratch)
}

#[cfg(test)]
mod tests {
    use super::*;

    use std::fs::File;
    use std::sync::Arc;

    use crate::snapshot::format::overlaybd::{
        encode_descriptor, encode_zfile_descriptor, plan_layer,
    };
    use crate::snapshot::metadata::{
        canonical_json_bytes, compute_layer_content_sha256, sha256_identity, LayerKind, LayerRange,
        ObjectRef, RangeKind,
    };

    fn zero_layer() -> LayerRangeMap {
        let mut layer = LayerRangeMap {
            registry_objects: None,
            format_version: 1,
            kind: LayerKind::Block,
            content_sha256: String::new(),
            logical_bytes: 65536,
            unit_size_bytes: 65536,
            ranges: vec![LayerRange {
                kind: RangeKind::Zero,
                logical_offset: 0,
                length: 65536,
                source: None,
            }],
            zfile: None,
            registry: None,
        };
        layer.content_sha256 = compute_layer_content_sha256(&layer).unwrap();
        layer
    }

    fn scratch() -> File {
        let path = std::env::temp_dir().join(format!("snapd-format-{}", uuid::Uuid::new_v4()));
        let file = File::options()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
            .unwrap();
        std::fs::remove_file(path).unwrap();
        file
    }

    fn descriptor(layer: &LayerRangeMap, compressed: bool) -> Vec<u8> {
        let plan = plan_layer(layer).unwrap();
        let source = scratch();
        let (hash, encoded) = if compressed {
            let output = scratch();
            let encoded = crate::snapshot::format::overlaybd::zfile::encode_with_reader(
                plan.stored_bytes,
                &output,
                |offset, count| plan.read_at(&source, offset, count),
            )
            .unwrap();
            (encoded.sha256, Some(encoded.index))
        } else {
            let bytes = plan
                .read_at(&source, 0, plan.stored_bytes as usize)
                .unwrap();
            (sha256_identity(&bytes), None)
        };
        let blob = ObjectRef {
            object_key: format!(
                "layers/block/1/{}/data/{}.commit",
                &layer.content_sha256[7..],
                &hash[7..]
            ),
            sha256: hash,
            object_version: None,
        };
        if let Some(index) = encoded {
            encode_zfile_descriptor(layer, &plan, &blob, &index).unwrap()
        } else {
            encode_descriptor(layer, &plan, &blob).unwrap()
        }
    }

    #[test]
    fn explicit_formats_produce_the_same_common_layer_without_transport() {
        let layer = zero_layer();
        let native = crate::snapshot::format::overlaybd::composite::test_registry_rootfs();
        for (encoding, payload, expected) in [
            (
                RangeMapEncoding::OverlaybdLsmtV1,
                descriptor(&layer, false),
                layer.clone(),
            ),
            (
                RangeMapEncoding::OverlaybdZfileLz4V1,
                descriptor(&layer, true),
                layer.clone(),
            ),
            (
                RangeMapEncoding::RegistryOverlaybdV1,
                native.encode_descriptor().unwrap(),
                native.build_map().unwrap(),
            ),
        ] {
            let decoded = decode_layer(Some(encoding), &payload).unwrap();
            assert_eq!(decoded.range_map.ranges, expected.ranges);
            assert_eq!(decoded.range_map.content_sha256, expected.content_sha256);
            assert_eq!(decoded.range_map.logical_bytes, expected.logical_bytes);
            assert_eq!(
                decoded.blob.is_some(),
                matches!(
                    encoding,
                    RangeMapEncoding::OverlaybdLsmtV1 | RangeMapEncoding::OverlaybdZfileLz4V1
                )
            );
            assert_eq!(format_for_encoding(encoding).encoding(), Some(encoding));
        }
    }

    #[test]
    fn missing_encoding_rejects_even_a_valid_overlaybd_descriptor() {
        let payload = descriptor(&zero_layer(), false);
        assert_eq!(
            decode_layer(None, &payload).unwrap_err(),
            LayerFormatError::Invalid("persistent layer encoding is required".into())
        );
    }

    #[test]
    fn overlaybd_container_must_match_the_declared_format() {
        let layer = zero_layer();
        for (encoding, payload) in [
            (RangeMapEncoding::OverlaybdLsmtV1, descriptor(&layer, true)),
            (
                RangeMapEncoding::OverlaybdZfileLz4V1,
                descriptor(&layer, false),
            ),
        ] {
            assert_eq!(
                decode_layer(Some(encoding), &payload).unwrap_err(),
                LayerFormatError::EncodingMismatch
            );
        }
    }

    #[test]
    fn external_rootfs_retains_its_capability_without_an_owned_blob() {
        let native = crate::snapshot::format::overlaybd::composite::test_registry_rootfs();
        let descriptor = native.encode_descriptor().unwrap();
        let decoded =
            decode_layer(Some(RangeMapEncoding::RegistryOverlaybdV1), &descriptor).unwrap();
        assert!(decoded.blob.is_none());
        let pinned = decoded.range_map.registry.as_ref().unwrap();
        assert_eq!(pinned.content_sha256(), native.content_sha256());
        assert_eq!(pinned.as_ref(), Arc::clone(&native).as_ref());
        pinned.validate_map(&decoded.range_map).unwrap();
    }

    #[test]
    fn invalid_or_wrongly_labelled_payload_never_falls_back_to_another_decoder() {
        let payload = canonical_json_bytes(&zero_layer()).unwrap();
        for encoding in [
            Some(RangeMapEncoding::OverlaybdLsmtV1),
            Some(RangeMapEncoding::OverlaybdZfileLz4V1),
            Some(RangeMapEncoding::RegistryOverlaybdV1),
        ] {
            assert!(decode_layer(encoding, &payload).is_err());
        }
        for encoding in [
            None,
            Some(RangeMapEncoding::OverlaybdLsmtV1),
            Some(RangeMapEncoding::OverlaybdZfileLz4V1),
            Some(RangeMapEncoding::RegistryOverlaybdV1),
        ] {
            assert!(decode_layer(encoding, b"{}").is_err());
        }
    }
}
