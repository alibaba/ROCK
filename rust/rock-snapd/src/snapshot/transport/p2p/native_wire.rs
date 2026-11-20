//! Bounded native metadata and logical range fields for negotiated P2P v2.

use super::kind_models::{LayerKind, SnapshotKind};
use super::native::{
    NativeCoverageRequest, NativeCoverageResult, NativeLayerIdentityKind, NativeLayerMetadata,
    NativeReadRangeRequest, NativeSnapshotMetadata, MAX_NATIVE_CHAIN_DEPTH,
    MAX_NATIVE_COVERAGE_UNITS,
};
use crate::binary_wire::{invalid, Decoder, Encoder};
use std::io;

pub const NATIVE_METADATA_PAGE_LAYERS: usize = 4;
pub const MAX_NATIVE_METADATA_LAYERS: usize = 2 * MAX_NATIVE_CHAIN_DEPTH;

pub(super) fn encode_metadata(
    e: &mut Encoder,
    cursor: u32,
    total: u32,
    m: &NativeSnapshotMetadata,
) -> io::Result<()> {
    validate_page(cursor, total, m.layers.len())?;
    e.u64(u64::from(cursor));
    e.u64(u64::from(total));
    e.u64(m.metadata_version);
    e.text(&m.snapshot_id)?;
    e.byte(match m.snapshot_kind {
        SnapshotKind::BaseImage => 0,
        SnapshotKind::Full => 1,
    });
    e.text(&m.generation_id)?;
    e.text(&m.metadata_sha256)?;
    e.u64(m.layers.len() as u64);
    for layer in &m.layers {
        e.byte(kind_tag(layer.kind));
        e.u64(u64::from(layer.ordinal));
        e.text(&layer.layer_id)?;
        e.text(&layer.layer_generation_id)?;
        e.byte(match layer.identity_kind {
            NativeLayerIdentityKind::NativeGeneration => 0,
            NativeLayerIdentityKind::CanonicalContent => 1,
        });
        e.text(&layer.content_identity)?;
        e.u64(layer.logical_bytes);
        e.u64(layer.coverage_unit_size);
    }
    Ok(())
}

pub(super) fn decode_metadata(
    d: &mut Decoder<'_>,
) -> io::Result<(u32, u32, NativeSnapshotMetadata)> {
    let cursor = u32_field(d)?;
    let total = u32_field(d)?;
    let metadata_version = d.u64()?;
    let snapshot_id = d.text()?;
    let snapshot_kind = match d.byte()? {
        0 => SnapshotKind::BaseImage,
        1 => SnapshotKind::Full,
        _ => return Err(invalid("unknown native snapshot kind")),
    };
    let generation_id = d.text()?;
    let metadata_sha256 = d.text()?;
    let count = usize::try_from(d.u64()?).map_err(|_| invalid("native layer count overflow"))?;
    validate_page(cursor, total, count)?;
    let mut layers = Vec::with_capacity(count);
    for _ in 0..count {
        layers.push(NativeLayerMetadata {
            kind: decode_kind(d)?,
            ordinal: u32_field(d)?,
            layer_id: d.text()?,
            layer_generation_id: d.text()?,
            identity_kind: match d.byte()? {
                0 => NativeLayerIdentityKind::NativeGeneration,
                1 => NativeLayerIdentityKind::CanonicalContent,
                _ => return Err(invalid("unknown native layer identity kind")),
            },
            content_identity: d.text()?,
            logical_bytes: d.u64()?,
            coverage_unit_size: d.u64()?,
        });
    }
    Ok((
        cursor,
        total,
        NativeSnapshotMetadata {
            metadata_version,
            snapshot_id,
            snapshot_kind,
            generation_id,
            metadata_sha256,
            layers,
        },
    ))
}

fn validate_page(cursor: u32, total: u32, count: usize) -> io::Result<()> {
    if total as usize > MAX_NATIVE_METADATA_LAYERS
        || count > NATIVE_METADATA_PAGE_LAYERS
        || u64::from(cursor) + count as u64 > u64::from(total)
        || (cursor < total && count == 0)
    {
        return Err(invalid(
            "native metadata page is outside its bounded descriptor set",
        ));
    }
    Ok(())
}

pub(super) fn encode_range(e: &mut Encoder, r: &NativeReadRangeRequest) -> io::Result<()> {
    e.text(&r.generation_id)?;
    e.byte(kind_tag(r.kind));
    e.u64(u64::from(r.ordinal));
    e.text(&r.layer_id)?;
    e.text(&r.layer_generation_id)?;
    e.u64(r.offset);
    e.u64(r.length);
    Ok(())
}
pub(super) fn decode_range(d: &mut Decoder<'_>) -> io::Result<NativeReadRangeRequest> {
    Ok(NativeReadRangeRequest {
        generation_id: d.text()?,
        kind: decode_kind(d)?,
        ordinal: u32_field(d)?,
        layer_id: d.text()?,
        layer_generation_id: d.text()?,
        offset: d.u64()?,
        length: d.u64()?,
    })
}
pub(super) fn encode_coverage_request(
    e: &mut Encoder,
    r: &NativeCoverageRequest,
) -> io::Result<()> {
    encode_range(
        e,
        &NativeReadRangeRequest {
            generation_id: r.generation_id.clone(),
            kind: r.kind,
            ordinal: r.ordinal,
            layer_id: r.layer_id.clone(),
            layer_generation_id: r.layer_generation_id.clone(),
            offset: r.offset,
            length: r.length,
        },
    )
}
pub(super) fn decode_coverage_request(d: &mut Decoder<'_>) -> io::Result<NativeCoverageRequest> {
    let r = decode_range(d)?;
    Ok(NativeCoverageRequest {
        generation_id: r.generation_id,
        kind: r.kind,
        ordinal: r.ordinal,
        layer_id: r.layer_id,
        layer_generation_id: r.layer_generation_id,
        offset: r.offset,
        length: r.length,
    })
}
pub(super) fn encode_coverage(e: &mut Encoder, c: &NativeCoverageResult) -> io::Result<()> {
    validate_coverage(c)?;
    e.u64(c.offset);
    e.u64(c.length);
    e.u64(c.unit_size);
    e.u64(c.packed_states.len() as u64);
    e.0.extend_from_slice(&c.packed_states);
    Ok(())
}
pub(super) fn decode_coverage(d: &mut Decoder<'_>) -> io::Result<NativeCoverageResult> {
    let offset = d.u64()?;
    let length = d.u64()?;
    let unit_size = d.u64()?;
    let count = usize::try_from(d.u64()?).map_err(|_| invalid("native coverage count overflow"))?;
    if count > MAX_NATIVE_COVERAGE_UNITS.div_ceil(4) as usize {
        return Err(invalid("native coverage exceeds bounded page"));
    }
    let c = NativeCoverageResult {
        offset,
        length,
        unit_size,
        packed_states: d.take(count)?.to_vec(),
    };
    validate_coverage(&c)?;
    Ok(c)
}
pub(crate) fn validate_coverage(c: &NativeCoverageResult) -> io::Result<()> {
    if c.unit_size == 0
        || c.length == 0
        || c.offset % c.unit_size != 0
        || c.offset.checked_add(c.length).is_none()
    {
        return Err(invalid("invalid native coverage geometry"));
    }
    let units = c.length.div_ceil(c.unit_size);
    if units > MAX_NATIVE_COVERAGE_UNITS || c.packed_states.len() != units.div_ceil(4) as usize {
        return Err(invalid("invalid native coverage payload size"));
    }
    for index in 0..c.packed_states.len() * 4 {
        let state = (c.packed_states[index / 4] >> ((index % 4) * 2)) & 3;
        if state == 3 || (index as u64 >= units && state != 0) {
            return Err(invalid("invalid native coverage state"));
        }
    }
    Ok(())
}
fn kind_tag(kind: LayerKind) -> u8 {
    match kind {
        LayerKind::Memory => 0,
        LayerKind::Block => 1,
    }
}
fn decode_kind(d: &mut Decoder<'_>) -> io::Result<LayerKind> {
    match d.byte()? {
        0 => Ok(LayerKind::Memory),
        1 => Ok(LayerKind::Block),
        _ => Err(invalid("unknown native layer kind")),
    }
}
fn u32_field(d: &mut Decoder<'_>) -> io::Result<u32> {
    u32::try_from(d.u64()?).map_err(|_| invalid("native integer exceeds u32"))
}

#[cfg(test)]
mod tests {
    use std::fmt::Debug;

    use super::super::native::NATIVE_P2P_METADATA_VERSION;
    use super::super::protocol::{
        P2PAvailabilityResult, P2PBinaryFrame, P2PReadRangeRequest, P2PTcpErrorCode, P2PTcpRequest,
        P2PTcpResponse, P2P_NATIVE_PROTOCOL_VERSION, P2P_PROTOCOL_VERSION,
    };
    use super::*;

    fn strict_round_trip<T: P2PBinaryFrame + PartialEq + Debug>(message: &T) -> Vec<u8> {
        let bytes = message.to_binary().unwrap();
        assert_eq!(T::from_binary(&bytes).unwrap(), *message);
        for end in 0..bytes.len() {
            assert!(
                T::from_binary(&bytes[..end]).is_err(),
                "accepted truncation at {end}"
            );
        }
        let mut trailing = bytes.clone();
        trailing.push(0);
        assert!(T::from_binary(&trailing).is_err());
        bytes
    }

    fn golden<T: P2PBinaryFrame + PartialEq + Debug>(message: T, expected: &str) {
        let bytes = strict_round_trip(&message);
        let actual: String = bytes.iter().map(|value| format!("{value:02x}")).collect();
        assert_eq!(actual, expected);
    }

    #[test]
    fn legacy_v1_request_and_response_golden_bytes_are_unchanged() {
        assert_eq!(P2P_PROTOCOL_VERSION, 1);
        assert_eq!(P2P_NATIVE_PROTOCOL_VERSION, 2);
        golden(
            P2PTcpRequest::Open {
                protocol_version: 1,
                request_id: "r".into(),
                deadline_unix_ms: 2,
            },
            "01010000000000000001000000720200000000000000",
        );
        golden(
            P2PTcpRequest::Availability {
                request_id: "r".into(),
                deadline_unix_ms: 2,
                snapshot_id: "s".into(),
                spec_checksum: "c".into(),
                selection_only: true,
            },
            "02010000007202000000000000000100000073010000006301",
        );
        golden(P2PTcpRequest::ReadRange {
            request_id:"r".into(),deadline_unix_ms:2,snapshot_id:"s".into(),
            request:P2PReadRangeRequest {spec_checksum:"c".into(),object_key:"o".into(),
                object_sha256:"h".into(),object_version:Some("v".into()),offset:3,length:4},
        },"030100000072020000000000000001000000730100000063010000006f010000006801010000007603000000000000000400000000000000");
        golden(
            P2PTcpResponse::Ready {
                request_id: "r".into(),
                protocol_version: 1,
            },
            "8101000000720100000000000000",
        );
        golden(
            P2PTcpResponse::Availability {
                request_id: "r".into(),
                availability: P2PAvailabilityResult {
                    snapshot_id: "s".into(),
                    spec_checksum: "c".into(),
                    available: true,
                    complete: false,
                    servable_objects: 5,
                },
            },
            "8201000000720100000073010000006301000500000000000000",
        );
        golden(
            P2PTcpResponse::Range {
                request_id: "r".into(),
                length: 4,
                range_sha256: "h".into(),
            },
            "83010000007204000000000000000100000068",
        );
        golden(
            P2PTcpResponse::Error {
                request_id: "r".into(),
                code: P2PTcpErrorCode::NotFound,
                retryable: false,
                message: "miss".into(),
            },
            "8401000000720100040000006d697373",
        );
    }

    #[test]
    fn native_operation_tags_and_positional_fields_have_fixed_golden_bytes() {
        // Short identities isolate the positional codec; semantic source UUID
        // validation happens after descriptor pages have been assembled.
        golden(
            P2PTcpRequest::NativeMetadata {
                request_id: "r".into(),
                deadline_unix_ms: 2,
                snapshot_id: "s".into(),
                generation_id: Some("g".into()),
                cursor: 3,
            },
            "040100000072020000000000000001000000730101000000670300000000000000",
        );
        let request = NativeReadRangeRequest {
            generation_id: "g".into(),
            kind: LayerKind::Block,
            ordinal: 7,
            layer_id: "l".into(),
            layer_generation_id: "n".into(),
            offset: 512,
            length: 512,
        };
        golden(P2PTcpRequest::NativeReadRange {request_id:"r".into(),deadline_unix_ms:2,
            snapshot_id:"s".into(),request:request.clone()},
            "060100000072020000000000000001000000730100000067010700000000000000010000006c010000006e00020000000000000002000000000000");
        golden(P2PTcpRequest::NativeCoverage {request_id:"r".into(),deadline_unix_ms:2,
            snapshot_id:"s".into(),request:NativeCoverageRequest {generation_id:request.generation_id,
                kind:request.kind,ordinal:request.ordinal,layer_id:request.layer_id,
                layer_generation_id:request.layer_generation_id,offset:request.offset,length:request.length}},
            "050100000072020000000000000001000000730100000067010700000000000000010000006c010000006e00020000000000000002000000000000");
        golden(
            P2PTcpResponse::NativeCoverage {
                request_id: "r".into(),
                coverage: NativeCoverageResult {
                    offset: 0,
                    length: 12288,
                    unit_size: 4096,
                    packed_states: vec![0b00_10_01],
                },
            },
            "860100000072000000000000000000300000000000000010000000000000010000000000000009",
        );
        golden(P2PTcpResponse::NativeMetadata {request_id:"r".into(),cursor:0,total_layers:1,
            metadata:NativeSnapshotMetadata {metadata_version:1,snapshot_id:"s".into(),
                snapshot_kind:SnapshotKind::Full,generation_id:"g".into(),metadata_sha256:"h".into(),
                layers:vec![NativeLayerMetadata {kind:LayerKind::Memory,ordinal:0,layer_id:"l".into(),
                    layer_generation_id:"n".into(),identity_kind:NativeLayerIdentityKind::NativeGeneration,
                    content_identity:"i".into(),logical_bytes:4096,coverage_unit_size:4096}]}},
            "850100000072000000000000000001000000000000000100000000000000010000007301010000006701000000680100000000000000000000000000000000010000006c010000006e00010000006900100000000000000010000000000000");
        strict_round_trip(&P2PTcpRequest::NativeMetadata {
            request_id: "r".into(),
            deadline_unix_ms: 2,
            snapshot_id: "s".into(),
            generation_id: None,
            cursor: 0,
        });
    }

    fn metadata() -> NativeSnapshotMetadata {
        let mut metadata = NativeSnapshotMetadata {
            metadata_version: NATIVE_P2P_METADATA_VERSION,
            snapshot_id: "native-source".into(),
            snapshot_kind: SnapshotKind::Full,
            generation_id: uuid::Uuid::from_u128(1).to_string(),
            metadata_sha256: String::new(),
            layers: (0..4)
                .map(|ordinal| {
                    let generation = uuid::Uuid::from_u128(u128::from(ordinal) + 2).to_string();
                    NativeLayerMetadata {
                        kind: LayerKind::Memory,
                        ordinal,
                        layer_id: format!("layer-{ordinal}"),
                        layer_generation_id: generation.clone(),
                        identity_kind: NativeLayerIdentityKind::NativeGeneration,
                        content_identity: format!("native-generation:{generation}"),
                        logical_bytes: 4096,
                        coverage_unit_size: 4096,
                    }
                })
                .collect(),
        };
        metadata.metadata_sha256 = metadata.descriptor_checksum().unwrap();
        metadata.validate().unwrap();
        metadata
    }

    fn metadata_body(metadata: &NativeSnapshotMetadata) -> Vec<u8> {
        let mut encoder = Encoder::default();
        encode_metadata(&mut encoder, 0, 4, metadata).unwrap();
        encoder.0
    }

    #[test]
    fn metadata_pages_round_trip_at_four_layers_and_128_total_limit() {
        let metadata = metadata();
        strict_round_trip(&P2PTcpResponse::NativeMetadata {
            request_id: "page".into(),
            cursor: 0,
            total_layers: 4,
            metadata: metadata.clone(),
        });
        let mut encoded = Encoder::default();
        encode_metadata(&mut encoded, 124, 128, &metadata).unwrap();
        let mut decoder = Decoder(&encoded.0);
        let (cursor, total, decoded) = decode_metadata(&mut decoder).unwrap();
        decoder.finish().unwrap();
        assert_eq!((cursor, total, decoded), (124, 128, metadata.clone()));
        for (cursor, total) in [(0, 129), (127, 128), (u32::MAX, u32::MAX)] {
            assert!(encode_metadata(&mut Encoder::default(), cursor, total, &metadata).is_err());
        }
        let mut too_many = metadata;
        too_many.layers.push(too_many.layers[0].clone());
        assert!(encode_metadata(&mut Encoder::default(), 0, 5, &too_many).is_err());
    }

    #[test]
    fn metadata_decoder_rejects_unbounded_counts_integers_strings_and_tags() {
        let metadata = metadata();
        let bytes = metadata_body(&metadata);
        let count_offset = 24
            + 4
            + metadata.snapshot_id.len()
            + 1
            + 4
            + metadata.generation_id.len()
            + 4
            + metadata.metadata_sha256.len();
        for (offset, value) in [
            (0, u64::MAX),
            (8, 129),
            (count_offset, 5),
            (count_offset, u64::MAX),
            (count_offset + 8 + 1, u64::MAX),
        ] {
            let mut bad = bytes.clone();
            bad[offset..offset + 8].copy_from_slice(&value.to_le_bytes());
            assert!(
                decode_metadata(&mut Decoder(&bad)).is_err(),
                "accepted bad integer at {offset}"
            );
        }
        let first = &metadata.layers[0];
        let identity_tag = count_offset
            + 8
            + 1
            + 8
            + 4
            + first.layer_id.len()
            + 4
            + first.layer_generation_id.len();
        for (offset, value) in [
            (24 + 4 + metadata.snapshot_id.len(), 2),
            (count_offset + 8, 2),
            (identity_tag, 2),
        ] {
            let mut bad = bytes.clone();
            bad[offset] = value;
            assert!(decode_metadata(&mut Decoder(&bad)).is_err());
        }
        let mut oversized_text = bytes;
        oversized_text[24..28].copy_from_slice(&4097_u32.to_le_bytes());
        assert!(decode_metadata(&mut Decoder(&oversized_text)).is_err());
        let request = P2PTcpRequest::NativeMetadata {
            request_id: "r".into(),
            deadline_unix_ms: 2,
            snapshot_id: "s".into(),
            generation_id: None,
            cursor: 0,
        };
        let mut request = request.to_binary().unwrap();
        let end = request.len();
        request[end - 8..].copy_from_slice(&u64::MAX.to_le_bytes());
        assert!(P2PTcpRequest::from_binary(&request).is_err());
    }

    #[test]
    fn native_range_decoder_rejects_invalid_kind_ordinal_and_identity_lengths() {
        let request = NativeReadRangeRequest {
            generation_id: "g".into(),
            kind: LayerKind::Memory,
            ordinal: 0,
            layer_id: "l".into(),
            layer_generation_id: "n".into(),
            offset: 0,
            length: 4096,
        };
        let mut encoder = Encoder::default();
        encode_range(&mut encoder, &request).unwrap();
        let mut unknown_kind = encoder.0.clone();
        unknown_kind[5] = 2;
        assert!(decode_range(&mut Decoder(&unknown_kind)).is_err());
        let mut ordinal = encoder.0.clone();
        ordinal[6..14].copy_from_slice(&u64::MAX.to_le_bytes());
        assert!(decode_range(&mut Decoder(&ordinal)).is_err());
        let mut identity = encoder.0;
        identity[..4].copy_from_slice(&u32::MAX.to_le_bytes());
        assert!(decode_range(&mut Decoder(&identity)).is_err());
    }

    #[test]
    fn coverage_decoder_bounds_units_storage_and_unused_bits() {
        let maximum = NativeCoverageResult {
            offset: 0,
            length: 4096 * MAX_NATIVE_COVERAGE_UNITS,
            unit_size: 4096,
            packed_states: vec![0x55; MAX_NATIVE_COVERAGE_UNITS.div_ceil(4) as usize],
        };
        strict_round_trip(&P2PTcpResponse::NativeCoverage {
            request_id: "max".into(),
            coverage: maximum.clone(),
        });
        let mut excessive = maximum;
        excessive.length += 4096;
        assert!(validate_coverage(&excessive).is_err());
        let coverage = NativeCoverageResult {
            offset: 0,
            length: 4096,
            unit_size: 4096,
            packed_states: vec![1],
        };
        let mut encoder = Encoder::default();
        encode_coverage(&mut encoder, &coverage).unwrap();
        for (offset, value) in [
            (0, u64::MAX),
            (8, 0),
            (8, 4096 * (MAX_NATIVE_COVERAGE_UNITS + 1)),
            (16, 0),
            (24, 1025),
            (24, u64::MAX),
            (24, 0),
        ] {
            let mut bad = encoder.0.clone();
            bad[offset..offset + 8].copy_from_slice(&value.to_le_bytes());
            assert!(decode_coverage(&mut Decoder(&bad)).is_err());
        }
        for states in [3, 0b01_01, 0b11_00] {
            let mut bad = encoder.0.clone();
            *bad.last_mut().unwrap() = states;
            assert!(decode_coverage(&mut Decoder(&bad)).is_err());
        }
        let mut extra = encoder.0;
        extra[24..32].copy_from_slice(&2_u64.to_le_bytes());
        extra.push(0);
        assert!(decode_coverage(&mut Decoder(&extra)).is_err());
    }
}
