//! Versioned image metadata and compatibility encoding for local hotset hints.

use std::io;

use thiserror::Error;

use crate::snapshot::metadata::{
    canonical_json_bytes, parse_canonical_model, SnapshotHotRange, SnapshotHotset,
    SnapshotImageMetadata, SnapshotMetadataSubject, MAX_SNAPSHOT_HOTSET_RANGES,
    MAX_SNAPSHOT_IMAGE_METADATA_BYTES,
};

#[derive(Debug, Error)]
pub enum ImageMetadataCodecError {
    #[error("invalid snapshot image metadata: {0}")]
    Invalid(String),
    #[error("snapshot image metadata exceeds bound: {0}")]
    Bound(String),
    #[error("local hotset codec I/O failed: {0}")]
    Io(#[from] io::Error),
}

pub use crate::snapshot::format::SnapshotImageMetadataCodec;

#[derive(Clone, Copy, Debug, Default)]
pub struct CanonicalImageMetadataV1;

impl SnapshotImageMetadataCodec for CanonicalImageMetadataV1 {
    fn object_key(
        &self,
        subject: &SnapshotMetadataSubject,
    ) -> Result<String, ImageMetadataCodecError> {
        subject
            .validate()
            .map_err(ImageMetadataCodecError::Invalid)?;
        Ok(format!(
            "snapshots/{}/metadata/{}/image-metadata-v1.json",
            subject.snapshot_id,
            subject.spec_checksum.trim_start_matches("sha256:")
        ))
    }

    fn encode(&self, metadata: &SnapshotImageMetadata) -> Result<Vec<u8>, ImageMetadataCodecError> {
        metadata
            .validate()
            .map_err(ImageMetadataCodecError::Invalid)?;
        let payload = canonical_json_bytes(metadata)
            .map_err(|error| ImageMetadataCodecError::Invalid(error.to_string()))?;
        check_size(&payload)?;
        Ok(payload)
    }

    fn decode(
        &self,
        payload: &[u8],
        subject: &SnapshotMetadataSubject,
    ) -> Result<SnapshotImageMetadata, ImageMetadataCodecError> {
        check_size(payload)?;
        subject
            .validate()
            .map_err(ImageMetadataCodecError::Invalid)?;
        let metadata: SnapshotImageMetadata =
            parse_canonical_model(payload, "snapshot image metadata")
                .map_err(|error| ImageMetadataCodecError::Invalid(error.to_string()))?;
        metadata
            .validate_for(subject)
            .map_err(ImageMetadataCodecError::Invalid)?;
        Ok(metadata)
    }
}

fn check_size(payload: &[u8]) -> Result<(), ImageMetadataCodecError> {
    if payload.len() as u64 > MAX_SNAPSHOT_IMAGE_METADATA_BYTES {
        return Err(ImageMetadataCodecError::Bound(
            "encoded object bytes".into(),
        ));
    }
    Ok(())
}

/// Preserve the RCKHOT02 layout already used by node-local learned hints.
pub fn encode_local_hotset(
    identity: &str,
    ranges: &[SnapshotHotRange],
) -> Result<Vec<u8>, ImageMetadataCodecError> {
    use crate::binary_wire::Encoder;
    SnapshotHotset {
        ranges: ranges.to_vec(),
    }
    .validate()
    .map_err(ImageMetadataCodecError::Invalid)?;
    let mut encoder = Encoder(b"RCKHOT02".to_vec());
    encoder.text(identity)?;
    encoder.u64(ranges.len() as u64);
    for range in ranges {
        encoder.text(&range.layer_content_sha256)?;
        encoder.u64(range.target_offset);
        encoder.u64(range.length);
    }
    check_size(&encoder.0)?;
    Ok(encoder.0)
}

pub fn decode_local_hotset(
    payload: &[u8],
    expected_identity: &str,
) -> Result<Vec<SnapshotHotRange>, ImageMetadataCodecError> {
    use crate::binary_wire::Decoder;
    check_size(payload)?;
    let mut decoder = Decoder(payload);
    if decoder.take(8)? != b"RCKHOT02" || decoder.text()? != expected_identity {
        return Err(ImageMetadataCodecError::Invalid(
            "local hotset identity or version differs".into(),
        ));
    }
    let count = decoder.u64()?;
    if count > MAX_SNAPSHOT_HOTSET_RANGES as u64 {
        return Err(ImageMetadataCodecError::Bound("local range count".into()));
    }
    let mut ranges = Vec::with_capacity(count as usize);
    for _ in 0..count {
        ranges.push(SnapshotHotRange {
            layer_content_sha256: decoder.text()?,
            target_offset: decoder.u64()?,
            length: decoder.u64()?,
        });
    }
    decoder.finish()?;
    SnapshotHotset {
        ranges: ranges.clone(),
    }
    .validate()
    .map_err(ImageMetadataCodecError::Invalid)?;
    Ok(ranges)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::snapshot::metadata::{merge_snapshot_hot_ranges, MAX_SNAPSHOT_HOTSET_DECODED_BYTES};

    fn subject() -> SnapshotMetadataSubject {
        SnapshotMetadataSubject::new("image-01", format!("sha256:{}", "a".repeat(64))).unwrap()
    }

    fn range(offset: u64, length: u64) -> SnapshotHotRange {
        SnapshotHotRange {
            layer_content_sha256: format!("sha256:{}", "b".repeat(64)),
            target_offset: offset,
            length,
        }
    }

    fn metadata() -> SnapshotImageMetadata {
        let subject = subject();
        SnapshotImageMetadata {
            format_version: 1,
            snapshot_id: subject.snapshot_id,
            spec_checksum: subject.spec_checksum,
            hotset: Some(SnapshotHotset {
                ranges: vec![range(4096, 4096)],
            }),
        }
    }

    #[test]
    fn image_metadata_codec_binds_subject_path_version_and_canonical_bytes() {
        let codec = CanonicalImageMetadataV1;
        let value = metadata();
        assert_eq!(
            subject().local_identity(),
            format!("image-01\nsha256:{}", "a".repeat(64))
        );
        assert_eq!(
            codec.object_key(&subject()).unwrap(),
            format!(
                "snapshots/image-01/metadata/{}/image-metadata-v1.json",
                "a".repeat(64)
            )
        );
        let payload = codec.encode(&value).unwrap();
        assert_eq!(codec.decode(&payload, &subject()).unwrap(), value);
        let other = SnapshotMetadataSubject::new("image-02", subject().spec_checksum).unwrap();
        assert!(codec.decode(&payload, &other).is_err());
        let changed =
            SnapshotMetadataSubject::new("image-01", format!("sha256:{}", "c".repeat(64))).unwrap();
        assert!(codec.decode(&payload, &changed).is_err());
        let mut invalid = value.clone();
        invalid.format_version = 2;
        assert!(codec.encode(&invalid).is_err());
        let mut object = serde_json::to_value(&value).unwrap();
        object["unknown"] = true.into();
        assert!(codec
            .decode(&canonical_json_bytes(&object).unwrap(), &subject())
            .is_err());
        assert!(codec
            .decode(&serde_json::to_vec_pretty(&value).unwrap(), &subject())
            .is_err());
        for id in ["", "../escape", "a/b", "with\nnewline"] {
            assert!(SnapshotMetadataSubject::new(id, subject().spec_checksum.clone()).is_err());
        }
        assert!(SnapshotMetadataSubject::new("image-01", "sha256:bad").is_err());
    }

    #[test]
    fn image_metadata_codec_rejects_unbounded_or_invalid_range_hints() {
        let codec = CanonicalImageMetadataV1;
        let mut value = metadata();
        for invalid in [
            range(0, 0),
            range(u64::MAX, 1),
            range(0, MAX_SNAPSHOT_HOTSET_DECODED_BYTES + 1),
        ] {
            value.hotset = Some(SnapshotHotset {
                ranges: vec![invalid],
            });
            assert!(codec.encode(&value).is_err());
        }
        value.hotset = Some(SnapshotHotset {
            ranges: vec![range(0, 1); MAX_SNAPSHOT_HOTSET_RANGES + 1],
        });
        assert!(codec.encode(&value).is_err());
        assert!(codec
            .decode(
                &vec![b' '; MAX_SNAPSHOT_IMAGE_METADATA_BYTES as usize + 1],
                &subject()
            )
            .is_err());
        let bad = SnapshotHotRange {
            layer_content_sha256: "not-a-layer".into(),
            target_offset: 0,
            length: 4096,
        };
        assert!(bad.validate().is_err());
    }

    #[test]
    fn local_hotset_codec_reads_and_writes_the_existing_rckhot02_layout() {
        let identity = subject().local_identity();
        // This fixed layout was emitted before the remote metadata feature.
        let mut legacy = b"RCKHOT02".to_vec();
        fn text(output: &mut Vec<u8>, value: &str) {
            output.extend_from_slice(&(value.len() as u32).to_le_bytes());
            output.extend_from_slice(value.as_bytes());
        }
        text(&mut legacy, &identity);
        legacy.extend_from_slice(&1_u64.to_le_bytes());
        let expected = range(4096, 4096);
        text(&mut legacy, &expected.layer_content_sha256);
        legacy.extend_from_slice(&4096_u64.to_le_bytes());
        legacy.extend_from_slice(&4096_u64.to_le_bytes());
        assert_eq!(
            decode_local_hotset(&legacy, &identity).unwrap(),
            vec![expected.clone()]
        );
        assert_eq!(encode_local_hotset(&identity, &[expected]).unwrap(), legacy);
        assert!(decode_local_hotset(&legacy, "another snapshot").is_err());
        assert!(decode_local_hotset(&legacy[..legacy.len() - 1], &identity).is_err());
        let mut trailing = legacy.clone();
        trailing.push(0);
        assert!(decode_local_hotset(&trailing, &identity).is_err());
        assert!(decode_local_hotset(b"{\"ranges\":[]}", &identity).is_err());
        let legacy_bare = SnapshotHotRange {
            layer_content_sha256: "A".repeat(64),
            ..range(0, 4096)
        };
        let payload = encode_local_hotset(&identity, &[legacy_bare.clone()]).unwrap();
        assert_eq!(
            decode_local_hotset(&payload, &identity).unwrap(),
            vec![legacy_bare]
        );
    }

    #[test]
    fn hotset_merge_keeps_old_first_order_exact_deduplication_and_bounds() {
        let a = range(0, 4096);
        let b = range(4096, 4096);
        assert_eq!(
            merge_snapshot_hot_ranges(&[a.clone()], &[a.clone(), b.clone()]),
            vec![a, b]
        );
        let full = range(0, MAX_SNAPSHOT_HOTSET_DECODED_BYTES);
        assert_eq!(
            merge_snapshot_hot_ranges(&[full.clone()], &[range(999, 1)]),
            vec![full]
        );
        let many = (0..MAX_SNAPSHOT_HOTSET_RANGES)
            .map(|offset| range(offset as u64, 1))
            .collect::<Vec<_>>();
        assert_eq!(merge_snapshot_hot_ranges(&many, &[range(99999, 1)]), many);
        assert_eq!(
            merge_snapshot_hot_ranges(&[range(0, 0)], &[range(0, 1)]),
            vec![range(0, 1)]
        );
    }
}
