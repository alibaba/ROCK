//! Positional binary metadata for ROCKP2P2. DATA bytes follow Range unchanged.
//! Outer u32 BE length; fields use the bounded LE codec also used by RemoteLoad.
use std::io;

use crate::binary_wire::{invalid, Decoder, Encoder};
use serde::{Deserialize, Serialize};

pub const P2P_PROTOCOL_VERSION: u64 = 1;
/// Additive native logical-layer operations. Canonical v1 stays byte-identical.
pub const P2P_NATIVE_PROTOCOL_VERSION: u64 = 2;
pub use super::native::{
    NativeCoverageRequest, NativeCoverageResult, NativeReadRangeRequest, NativeSnapshotMetadata,
};
use super::native_wire;

pub const P2P_TCP_MAGIC: &[u8; 8] = b"ROCKP2P2";
pub const P2P_TCP_MAX_FRAME_BYTES: usize = 64 * 1024;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "operation", rename_all = "snake_case", deny_unknown_fields)]
pub enum P2PTcpRequest {
    NativeMetadata {
        request_id: String,
        deadline_unix_ms: u64,
        snapshot_id: String,
        generation_id: Option<String>,
        cursor: u32,
    },
    NativeCoverage {
        request_id: String,
        deadline_unix_ms: u64,
        snapshot_id: String,
        request: NativeCoverageRequest,
    },
    NativeReadRange {
        request_id: String,
        deadline_unix_ms: u64,
        snapshot_id: String,
        request: NativeReadRangeRequest,
    },
    Open {
        protocol_version: u64,
        request_id: String,
        deadline_unix_ms: u64,
    },
    Availability {
        request_id: String,
        deadline_unix_ms: u64,
        snapshot_id: String,
        spec_checksum: String,
        #[serde(default, skip_serializing_if = "std::ops::Not::not")]
        selection_only: bool,
    },
    ReadRange {
        request_id: String,
        deadline_unix_ms: u64,
        snapshot_id: String,
        request: P2PReadRangeRequest,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum P2PTcpErrorCode {
    InvalidArgument,
    NotFound,
    IdentityConflict,
    DeadlineExceeded,
    Internal,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "result", rename_all = "snake_case", deny_unknown_fields)]
pub enum P2PTcpResponse {
    /// The checksum binds the complete descriptor set assembled across pages.
    NativeMetadata {
        request_id: String,
        cursor: u32,
        total_layers: u32,
        metadata: NativeSnapshotMetadata,
    },
    NativeCoverage {
        request_id: String,
        coverage: NativeCoverageResult,
    },
    Ready {
        request_id: String,
        protocol_version: u64,
    },
    Availability {
        request_id: String,
        availability: P2PAvailabilityResult,
    },
    Range {
        request_id: String,
        length: u64,
        range_sha256: String,
    },
    Error {
        request_id: String,
        code: P2PTcpErrorCode,
        retryable: bool,
        message: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct P2PAvailabilityResult {
    pub snapshot_id: String,
    pub spec_checksum: String,
    pub available: bool,
    pub complete: bool,
    pub servable_objects: usize,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct P2PReadRangeRequest {
    pub spec_checksum: String,
    pub object_key: String,
    pub object_sha256: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub object_version: Option<String>,
    pub offset: u64,
    pub length: u64,
}

pub trait P2PBinaryFrame: Sized {
    fn to_binary(&self) -> io::Result<Vec<u8>>;
    fn from_binary(bytes: &[u8]) -> io::Result<Self>;
}

impl P2PBinaryFrame for P2PTcpRequest {
    fn to_binary(&self) -> io::Result<Vec<u8>> {
        let mut e = Encoder::default();
        match self {
            Self::NativeMetadata {
                request_id,
                deadline_unix_ms,
                snapshot_id,
                generation_id,
                cursor,
            } => {
                e.byte(4);
                e.text(request_id)?;
                e.u64(*deadline_unix_ms);
                e.text(snapshot_id)?;
                e.boolean(generation_id.is_some());
                if let Some(generation) = generation_id {
                    e.text(generation)?;
                }
                e.u64(u64::from(*cursor));
            }
            Self::NativeCoverage {
                request_id,
                deadline_unix_ms,
                snapshot_id,
                request,
            } => {
                e.byte(5);
                e.text(request_id)?;
                e.u64(*deadline_unix_ms);
                e.text(snapshot_id)?;
                native_wire::encode_coverage_request(&mut e, request)?;
            }
            Self::NativeReadRange {
                request_id,
                deadline_unix_ms,
                snapshot_id,
                request,
            } => {
                e.byte(6);
                e.text(request_id)?;
                e.u64(*deadline_unix_ms);
                e.text(snapshot_id)?;
                native_wire::encode_range(&mut e, request)?;
            }
            Self::Open {
                protocol_version,
                request_id,
                deadline_unix_ms,
            } => {
                e.byte(1);
                e.u64(*protocol_version);
                e.text(request_id)?;
                e.u64(*deadline_unix_ms);
            }
            Self::Availability {
                request_id,
                deadline_unix_ms,
                snapshot_id,
                spec_checksum,
                selection_only,
            } => {
                e.byte(2);
                e.text(request_id)?;
                e.u64(*deadline_unix_ms);
                e.text(snapshot_id)?;
                e.text(spec_checksum)?;
                e.boolean(*selection_only);
            }
            Self::ReadRange {
                request_id,
                deadline_unix_ms,
                snapshot_id,
                request,
            } => {
                e.byte(3);
                e.text(request_id)?;
                e.u64(*deadline_unix_ms);
                e.text(snapshot_id)?;
                e.text(&request.spec_checksum)?;
                e.text(&request.object_key)?;
                e.text(&request.object_sha256)?;
                e.boolean(request.object_version.is_some());
                if let Some(version) = &request.object_version {
                    e.text(version)?;
                }
                e.u64(request.offset);
                e.u64(request.length);
            }
        }
        Ok(e.0)
    }
    fn from_binary(bytes: &[u8]) -> io::Result<Self> {
        let mut d = Decoder(bytes);
        let result = match d.byte()? {
            4 => Self::NativeMetadata {
                request_id: d.text()?,
                deadline_unix_ms: d.u64()?,
                snapshot_id: d.text()?,
                generation_id: if d.boolean()? { Some(d.text()?) } else { None },
                cursor: u32::try_from(d.u64()?)
                    .map_err(|_| invalid("native metadata cursor exceeds u32"))?,
            },
            5 => Self::NativeCoverage {
                request_id: d.text()?,
                deadline_unix_ms: d.u64()?,
                snapshot_id: d.text()?,
                request: native_wire::decode_coverage_request(&mut d)?,
            },
            6 => Self::NativeReadRange {
                request_id: d.text()?,
                deadline_unix_ms: d.u64()?,
                snapshot_id: d.text()?,
                request: native_wire::decode_range(&mut d)?,
            },
            1 => Self::Open {
                protocol_version: d.u64()?,
                request_id: d.text()?,
                deadline_unix_ms: d.u64()?,
            },
            2 => Self::Availability {
                request_id: d.text()?,
                deadline_unix_ms: d.u64()?,
                snapshot_id: d.text()?,
                spec_checksum: d.text()?,
                selection_only: d.boolean()?,
            },
            3 => Self::ReadRange {
                request_id: d.text()?,
                deadline_unix_ms: d.u64()?,
                snapshot_id: d.text()?,
                request: P2PReadRangeRequest {
                    spec_checksum: d.text()?,
                    object_key: d.text()?,
                    object_sha256: d.text()?,
                    object_version: if d.boolean()? { Some(d.text()?) } else { None },
                    offset: d.u64()?,
                    length: d.u64()?,
                },
            },
            _ => return Err(invalid("unknown P2P2 request tag")),
        };
        d.finish()?;
        Ok(result)
    }
}

impl P2PBinaryFrame for P2PTcpResponse {
    fn to_binary(&self) -> io::Result<Vec<u8>> {
        let mut e = Encoder::default();
        match self {
            Self::NativeMetadata {
                request_id,
                cursor,
                total_layers,
                metadata,
            } => {
                e.byte(133);
                e.text(request_id)?;
                native_wire::encode_metadata(&mut e, *cursor, *total_layers, metadata)?;
            }
            Self::NativeCoverage {
                request_id,
                coverage,
            } => {
                e.byte(134);
                e.text(request_id)?;
                native_wire::encode_coverage(&mut e, coverage)?;
            }
            Self::Ready {
                request_id,
                protocol_version,
            } => {
                e.byte(129);
                e.text(request_id)?;
                e.u64(*protocol_version);
            }
            Self::Availability {
                request_id,
                availability,
            } => {
                e.byte(130);
                e.text(request_id)?;
                e.text(&availability.snapshot_id)?;
                e.text(&availability.spec_checksum)?;
                e.boolean(availability.available);
                e.boolean(availability.complete);
                e.u64(availability.servable_objects as u64);
            }
            Self::Range {
                request_id,
                length,
                range_sha256,
            } => {
                e.byte(131);
                e.text(request_id)?;
                e.u64(*length);
                e.text(range_sha256)?;
            }
            Self::Error {
                request_id,
                code,
                retryable,
                message,
            } => {
                e.byte(132);
                e.text(request_id)?;
                e.byte(match code {
                    P2PTcpErrorCode::InvalidArgument => 0,
                    P2PTcpErrorCode::NotFound => 1,
                    P2PTcpErrorCode::IdentityConflict => 2,
                    P2PTcpErrorCode::DeadlineExceeded => 3,
                    P2PTcpErrorCode::Internal => 4,
                });
                e.boolean(*retryable);
                e.text(message)?;
            }
        }
        Ok(e.0)
    }
    fn from_binary(bytes: &[u8]) -> io::Result<Self> {
        let mut d = Decoder(bytes);
        let result = match d.byte()? {
            133 => {
                let request_id = d.text()?;
                let (cursor, total_layers, metadata) = native_wire::decode_metadata(&mut d)?;
                Self::NativeMetadata {
                    request_id,
                    cursor,
                    total_layers,
                    metadata,
                }
            }
            134 => Self::NativeCoverage {
                request_id: d.text()?,
                coverage: native_wire::decode_coverage(&mut d)?,
            },
            129 => Self::Ready {
                request_id: d.text()?,
                protocol_version: d.u64()?,
            },
            130 => Self::Availability {
                request_id: d.text()?,
                availability: P2PAvailabilityResult {
                    snapshot_id: d.text()?,
                    spec_checksum: d.text()?,
                    available: d.boolean()?,
                    complete: d.boolean()?,
                    servable_objects: usize::try_from(d.u64()?)
                        .map_err(|_| invalid("P2P object count overflow"))?,
                },
            },
            131 => Self::Range {
                request_id: d.text()?,
                length: d.u64()?,
                range_sha256: d.text()?,
            },
            132 => Self::Error {
                request_id: d.text()?,
                code: match d.byte()? {
                    0 => P2PTcpErrorCode::InvalidArgument,
                    1 => P2PTcpErrorCode::NotFound,
                    2 => P2PTcpErrorCode::IdentityConflict,
                    3 => P2PTcpErrorCode::DeadlineExceeded,
                    4 => P2PTcpErrorCode::Internal,
                    _ => return Err(invalid("unknown P2P2 error code")),
                },
                retryable: d.boolean()?,
                message: d.text()?,
            },
            _ => return Err(invalid("unknown P2P2 response tag")),
        };
        d.finish()?;
        Ok(result)
    }
}
