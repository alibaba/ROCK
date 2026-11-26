//! Single-blob sealed LSMT wire parsing shared by all OverlayBD profiles.
//!
//! This module decodes the standard layout. Descriptor adapters retain their
//! own trust model, allocation budgets, logical alignment and canonical-byte
//! checks. In particular, parsing an external ordinary-file origin tag does
//! not authorize it in an owned snapshot's canonical index.

pub(super) const SECTOR: u64 = 512;
pub(super) const SPACE: u64 = 4096;
pub(super) const INVALID_OFFSET: u64 = (1_u64 << 50) - 1;
pub(super) const MAGIC: &[u8; 8] = b"LSMT\0\x01\x02\0";
pub(super) const MAGIC_UUID: [u8; 16] = [
    0x65, 0x7e, 0x63, 0xd2, 0x94, 0x44, 0x08, 0x4c, 0xa2, 0xd2, 0xc8, 0xec, 0x4f, 0xcf, 0xae, 0x8a,
];

fn require(condition: bool, message: &str) -> Result<(), String> {
    if condition {
        Ok(())
    } else {
        Err(message.into())
    }
}

pub(super) fn u64_at(bytes: &[u8], offset: usize) -> Result<u64, String> {
    Ok(u64::from_le_bytes(
        bytes
            .get(offset..offset + 8)
            .ok_or_else(|| "truncated LSMT integer".to_owned())?
            .try_into()
            .map_err(|_| "invalid LSMT integer width".to_owned())?,
    ))
}

fn u32_at(bytes: &[u8], offset: usize) -> Result<u32, String> {
    Ok(u32::from_le_bytes(
        bytes
            .get(offset..offset + 4)
            .ok_or_else(|| "truncated LSMT integer".to_owned())?
            .try_into()
            .map_err(|_| "invalid LSMT integer width".to_owned())?,
    ))
}

#[derive(Clone, Copy, Debug)]
pub(super) struct Metadata {
    pub index_offset: u64,
    pub index_records: u64,
    pub logical_bytes: u64,
}

pub(super) fn metadata(bytes: &[u8], header: bool) -> Result<Metadata, String> {
    require(
        bytes.len() == SPACE as usize,
        "LSMT metadata must be 4096 bytes",
    )?;
    require(
        &bytes[..8] == MAGIC && bytes[8..24] == MAGIC_UUID && u32_at(bytes, 24)? == 390,
        "invalid LSMT magic/structure size",
    )?;
    let flags = u32_at(bytes, 28)?;
    require(
        (flags & 1 != 0) == header
            && flags & 2 != 0
            && flags & !7 == 0
            && (header || flags & 4 != 0),
        "only ordinary sealed LSMT data files are supported",
    )?;
    require(
        bytes[132] <= 1 && bytes[133] <= 1,
        "unsupported LSMT version",
    )?;
    Ok(Metadata {
        index_offset: u64_at(bytes, 32)?,
        index_records: u64_at(bytes, 40)?,
        logical_bytes: u64_at(bytes, 48)?,
    })
}

/// Lossless interpretation of a 16-byte standard mapping record. Padding and
/// origin tags are retained so each descriptor profile can validate them.
#[derive(Clone, Copy, Debug)]
pub(super) enum Mapping {
    Padding {
        low: u64,
        high: u64,
    },
    Extent {
        logical: u64,
        length: u64,
        physical: u64,
        zero: bool,
        origin: u8,
    },
}

pub(super) fn mapping(record: &[u8]) -> Result<Mapping, String> {
    require(record.len() == 16, "LSMT mapping must be 16 bytes")?;
    let low = u64_at(record, 0)?;
    let high = u64_at(record, 8)?;
    let sector = low & INVALID_OFFSET;
    if sector == INVALID_OFFSET {
        return Ok(Mapping::Padding { low, high });
    }
    let logical = sector
        .checked_mul(SECTOR)
        .ok_or_else(|| "LSMT logical offset overflows".to_owned())?;
    let physical = (high & ((1_u64 << 55) - 1))
        .checked_mul(SECTOR)
        .ok_or_else(|| "LSMT physical offset overflows".to_owned())?;
    Ok(Mapping::Extent {
        logical,
        length: (low >> 50) * SECTOR,
        physical,
        zero: high & (1 << 55) != 0,
        origin: (high >> 56) as u8,
    })
}
