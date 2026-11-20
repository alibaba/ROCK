//! Default persistent layer writer: standard OverlayBD, optionally ZFile-LZ4.
//!
//! This format implementation consumes the common layer IR and a read-only
//! logical backing. Scratch-file admission belongs to the caller; its original
//! error type passes through unchanged, including capacity and retry policy.

use std::fs::File;
use std::sync::Arc;

use sha2::{Digest, Sha256};
use thiserror::Error;

use crate::snapshot::format::overlaybd::cache::OverlaybdObjectValidator;
use crate::snapshot::format::overlaybd::zfile::{
    encode_with_writer, encoded_size_bound, ZfileError,
};
use crate::snapshot::format::overlaybd::{
    decode_descriptor, encode_descriptor, encode_zfile_descriptor, fits_zfile_descriptor,
    plan_layer, OverlaybdBlobPlan,
};
use crate::snapshot::metadata::{
    sha256_identity, LayerRangeMap, ObjectRef, FORMAT_VERSION_V1, MAX_RANGE_MAP_BYTES,
};
use crate::snapshot::transport::{
    DataIntegrityFailure, ImmutableObjectIdentity, SealedObject, StagedObjectWriter,
};

#[derive(Debug, Error)]
pub(crate) enum LayerEncodeError {
    #[error(transparent)]
    SourceIntegrity(DataIntegrityFailure),
    #[error("{0}")]
    Invalid(String),
    #[error("{0}")]
    Io(String),
    #[error("{0}")]
    CanonicalModel(String),
}

impl From<ZfileError> for LayerEncodeError {
    fn from(error: ZfileError) -> Self {
        match error {
            ZfileError::Io(error) => Self::Io(error.to_string()),
            error => Self::CanonicalModel(error.to_string()),
        }
    }
}

pub(crate) struct EncodedLayer {
    pub blob: ObjectRef,
    pub blob_length: u64,
    pub plan: Arc<OverlaybdBlobPlan>,
    pub sealed_object: Arc<dyn SealedObject>,
    pub range_map: LayerRangeMap,
    pub descriptor: Vec<u8>,
    pub descriptor_sha256: String,
}

pub(in crate::snapshot::format) fn encode<E>(
    logical_layer: LayerRangeMap,
    file: &File,
    create_scratch: &dyn Fn(u64) -> Result<Arc<dyn StagedObjectWriter>, E>,
) -> Result<EncodedLayer, E>
where
    E: From<LayerEncodeError>,
{
    let plan = Arc::new(plan_layer(&logical_layer).map_err(LayerEncodeError::CanonicalModel)?);
    let compressed = fits_zfile_descriptor(&logical_layer, &plan);
    encode_plan(logical_layer, file, plan, compressed, create_scratch)
}

fn encode_plan<E>(
    logical_layer: LayerRangeMap,
    file: &File,
    plan: Arc<OverlaybdBlobPlan>,
    compressed: bool,
    create_scratch: &dyn Fn(u64) -> Result<Arc<dyn StagedObjectWriter>, E>,
) -> Result<EncodedLayer, E>
where
    E: From<LayerEncodeError>,
{
    let capacity = if compressed {
        encoded_size_bound(plan.stored_bytes).map_err(LayerEncodeError::from)?
    } else {
        plan.stored_bytes
    };
    let destination = create_scratch(capacity)?;
    let (blob_sha256, length, zfile) = if compressed {
        let encoded =
            encode_with_writer(plan.stored_bytes, destination.as_ref(), |offset, count| {
                plan.read_at(file, offset, count)
            })
            .map_err(LayerEncodeError::from)?;
        (
            encoded.sha256,
            encoded.index.encoded_bytes,
            Some(Arc::new(encoded.index)),
        )
    } else {
        let mut digest = Sha256::new();
        let mut offset = 0_u64;
        while offset < plan.stored_bytes {
            let payload = plan
                .read_at(file, offset, 8 * 1024 * 1024)
                .map_err(LayerEncodeError::Io)?;
            if payload.is_empty() {
                return Err(LayerEncodeError::Invalid(
                    "OverlayBD virtual blob returned a short read".into(),
                )
                .into());
            }
            destination
                .write_all_at(&payload, offset)
                .map_err(|error| LayerEncodeError::Io(error.to_string()))?;
            offset = offset
                .checked_add(payload.len() as u64)
                .filter(|end| *end <= plan.stored_bytes)
                .ok_or_else(|| {
                    LayerEncodeError::Invalid("OverlayBD blob length overflows".into())
                })?;
            digest.update(&payload);
        }
        (
            format!("sha256:{:x}", digest.finalize()),
            plan.stored_bytes,
            None,
        )
    };
    let content = normalize_sha256(&logical_layer.content_sha256);
    let blob = ObjectRef {
        object_key: format!(
            "layers/{}/{}/{content}/data/{}.commit",
            logical_layer.kind.object_key_component(),
            FORMAT_VERSION_V1,
            normalize_sha256(&blob_sha256)
        ),
        sha256: blob_sha256,
        object_version: None,
    };
    let range_map_bytes = match &zfile {
        Some(index) => encode_zfile_descriptor(&logical_layer, &plan, &blob, index),
        None => encode_descriptor(&logical_layer, &plan, &blob),
    }
    .map_err(LayerEncodeError::CanonicalModel)?;
    if range_map_bytes.len() > MAX_RANGE_MAP_BYTES {
        return Err(LayerEncodeError::Invalid(
            "OverlayBD integrity descriptor exceeds the format byte bound".into(),
        )
        .into());
    }
    let range_map_sha256 = sha256_identity(&range_map_bytes);
    // Use the same decoder as remote restore. This also fixes every DATA
    // physical offset to the standard blob instead of native backing offsets.
    let range_map =
        decode_descriptor(&range_map_bytes).map_err(LayerEncodeError::CanonicalModel)?;
    if range_map.content_sha256 != logical_layer.content_sha256 {
        return Err(LayerEncodeError::Invalid(
            "OverlayBD conversion changed the logical layer identity".into(),
        )
        .into());
    }
    let identity = ImmutableObjectIdentity {
        object_key: blob.object_key.clone(),
        sha256: blob.sha256.clone(),
        length,
    };
    let validator = OverlaybdObjectValidator::from_descriptor(&range_map_bytes)
        .map_err(|error| LayerEncodeError::CanonicalModel(error.to_string()))?;
    let sealed_object = destination
        .seal(&identity, validator.as_ref())
        .map_err(|error| {
            if let Some(failure) = &error.data_integrity {
                LayerEncodeError::SourceIntegrity(failure.clone())
            } else if error.retryable() {
                LayerEncodeError::Io(error.to_string())
            } else {
                LayerEncodeError::CanonicalModel(error.to_string())
            }
        })?;
    Ok(EncodedLayer {
        blob,
        blob_length: length,
        plan,
        sealed_object,
        range_map,
        descriptor: range_map_bytes,
        descriptor_sha256: range_map_sha256,
    })
}

fn normalize_sha256(value: &str) -> &str {
    value.strip_prefix("sha256:").unwrap_or(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::FileExt;

    use crate::snapshot::metadata::{
        compute_layer_content_sha256, DataCodec, DataSource, LayerKind, LayerRange, RangeKind,
    };
    use crate::snapshot::transport::posix_file::LocalFileCache;

    #[tokio::test]
    async fn compressed_and_raw_profiles_upload_sealed_bytes_independent_of_native_source() {
        for compressed in [true, false] {
            let directory = tempfile::tempdir().unwrap();
            let cache = LocalFileCache::new(directory.path().to_owned(), "writer-test").unwrap();
            let native = tempfile::tempfile().unwrap();
            native.set_len(8192).unwrap();
            let data = vec![0x6d; 4096];
            native.write_all_at(&data, 0).unwrap();
            let checksum = sha256_identity(&data);
            let mut layer = LayerRangeMap {
                registry_objects: None,
                format_version: 1,
                kind: LayerKind::Memory,
                content_sha256: String::new(),
                logical_bytes: 8192,
                unit_size_bytes: 4096,
                ranges: vec![
                    LayerRange {
                        kind: RangeKind::Data,
                        logical_offset: 0,
                        length: 4096,
                        source: Some(DataSource {
                            object_key: "pending".into(),
                            object_sha256: checksum.clone(),
                            object_version: None,
                            pack_offset: 0,
                            encoded_length: 4096,
                            decoded_length: 4096,
                            codec: DataCodec::Raw,
                            checksum_sha256: checksum,
                        }),
                    },
                    LayerRange {
                        kind: RangeKind::Zero,
                        logical_offset: 4096,
                        length: 4096,
                        source: None,
                    },
                ],
                registry: None,
                zfile: None,
            };
            layer.content_sha256 = compute_layer_content_sha256(&layer).unwrap();
            let plan = Arc::new(plan_layer(&layer).unwrap());
            let expected = plan
                .read_at(&native, 0, plan.stored_bytes as usize)
                .unwrap();
            let encoded =
                encode_plan::<LayerEncodeError>(layer, &native, plan, compressed, &|bound| {
                    cache
                        .create_staged_object(bound)
                        .map_err(|error| LayerEncodeError::Io(error.to_string()))
                })
                .unwrap();
            native.write_all_at(&vec![0xff; 4096], 0).unwrap();
            drop(native);
            let physical = encoded
                .sealed_object
                .read_at(0, encoded.blob_length as usize)
                .await
                .unwrap();
            assert_eq!(sha256_identity(&physical), encoded.blob.sha256);
            let decoded = if let Some(index) = &encoded.range_map.zfile {
                assert!(physical.len() as u64 <= encoded_size_bound(index.raw_bytes).unwrap());
                index
                    .decode_range(0, index.raw_bytes, 0, &physical)
                    .unwrap()
            } else {
                physical.clone()
            };
            assert_eq!(decoded, expected);
            assert_eq!(encoded.range_map.zfile.is_some(), compressed);
            encoded.sealed_object.mark_remote_committed();
        }
    }
}
