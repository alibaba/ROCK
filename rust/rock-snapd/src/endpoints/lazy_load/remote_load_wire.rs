//! Binary RemoteLoad wire v2. Keep byte-identical with CLH rock/src/remote_load_wire.rs.
//!
//! Outer framing is u32 LE length. Payload: magic, u16 LE VMM version,
//! length-prefixed correlation ID, u8 message tag, positional fields.
//! Integers are LE; strings are u32 byte length + UTF-8, bounded to 4096 bytes.
//! No serde/JSON, unbounded collection allocation, or trailing extensions.

use std::io;

use super::protocol::{
    DependencyRelease, RemoteLoad, RemoteLoadOrigin, RemoteLoadReady, RemoteLoadReply,
    RemoteLoadRequest,
};
use crate::vm::protocol::{Channel, ErrorCode, MessageHeader, ProtocolError, WireEnvelope};
use crate::vm::runtime::protocol::LayerKind;

const MAGIC: &[u8; 8] = b"RCKLOAD2";

use crate::binary_wire::{invalid, Decoder, Encoder};

pub(crate) trait RemoteLoadBody: Sized {
    fn encode_body(&self, encoder: &mut Encoder) -> io::Result<()>;
    fn decode_body(decoder: &mut Decoder<'_>) -> io::Result<Self>;
}

pub(crate) fn encode<T: RemoteLoadBody>(value: &WireEnvelope<T>) -> io::Result<Vec<u8>> {
    if value.header.channel != Channel::RemoteLoad {
        return Err(invalid("wrong binary channel"));
    }
    let mut encoder = Encoder(Vec::with_capacity(256));
    encoder.0.extend_from_slice(MAGIC);
    encoder
        .0
        .extend_from_slice(&value.header.protocol_version.to_le_bytes());
    encoder.text(&value.header.correlation_id)?;
    value.message.encode_body(&mut encoder)?;
    Ok(encoder.0)
}

pub(crate) fn decode<T: RemoteLoadBody>(bytes: &[u8]) -> io::Result<WireEnvelope<T>> {
    let mut decoder = Decoder(bytes);
    if decoder.take(8)? != MAGIC {
        return Err(invalid("unsupported RemoteLoad binary wire"));
    }
    let protocol_version = u16::from_le_bytes(decoder.take(2)?.try_into().unwrap());
    let correlation_id = decoder.text()?;
    let message = T::decode_body(&mut decoder)?;
    decoder.finish()?;
    Ok(WireEnvelope {
        header: MessageHeader {
            protocol_version,
            channel: Channel::RemoteLoad,
            correlation_id,
        },
        message,
    })
}

impl RemoteLoadBody for RemoteLoadRequest {
    fn encode_body(&self, e: &mut Encoder) -> io::Result<()> {
        match self {
            Self::RemoteLoad(load) => {
                e.byte(1);
                e.text(&load.target_id)?;
                e.u64(load.target_offset);
                e.u64(load.length);
                e.byte(match load.origin {
                    RemoteLoadOrigin::UffdMissing => 0,
                    RemoteLoadOrigin::VirtioBlkReadMiss => 1,
                    RemoteLoadOrigin::DebugOther => 2,
                });
            }
            Self::ReleaseDependencies(release) => {
                e.byte(3);
                encode_release(e, release);
            }
        }
        Ok(())
    }
    fn decode_body(d: &mut Decoder<'_>) -> io::Result<Self> {
        match d.byte()? {
            3 => Ok(Self::ReleaseDependencies(decode_release(d)?)),
            1 => Ok(Self::RemoteLoad(RemoteLoad {
                target_id: d.text()?,
                target_offset: d.u64()?,
                length: d.u64()?,
                origin: match d.byte()? {
                    0 => RemoteLoadOrigin::UffdMissing,
                    1 => RemoteLoadOrigin::VirtioBlkReadMiss,
                    2 => RemoteLoadOrigin::DebugOther,
                    _ => return Err(invalid("unknown RemoteLoad origin")),
                },
            })),
            _ => Err(invalid("unknown RemoteLoad request tag")),
        }
    }
}

impl RemoteLoadBody for RemoteLoadReply {
    fn encode_body(&self, e: &mut Encoder) -> io::Result<()> {
        match self {
            Self::DependenciesReleased(release) => {
                e.byte(4);
                encode_release(e, release);
            }
            Self::RemoteLoadReady(ready) => {
                e.byte(2);
                e.text(&ready.target_id)?;
                for value in [
                    ready.target_offset,
                    ready.length,
                    ready.resident_offset,
                    ready.resident_length,
                ] {
                    e.u64(value);
                }
            }
            Self::Error(error) => {
                e.byte(255);
                e.byte(match error.code {
                    ErrorCode::InvalidArgument => 0,
                    ErrorCode::ProtocolViolation => 1,
                    ErrorCode::StaleRuntimeEpoch => 2,
                    ErrorCode::CapabilityUnsupported => 3,
                    ErrorCode::TargetContentConflict => 4,
                    ErrorCode::ResourceExhausted => 5,
                    ErrorCode::DeadlineExceeded => 6,
                    ErrorCode::Internal => 7,
                });
                e.text(&error.message)?;
                e.boolean(error.retryable);
            }
        }
        Ok(())
    }
    fn decode_body(d: &mut Decoder<'_>) -> io::Result<Self> {
        match d.byte()? {
            4 => Ok(Self::DependenciesReleased(decode_release(d)?)),
            2 => Ok(Self::RemoteLoadReady(RemoteLoadReady {
                target_id: d.text()?,
                target_offset: d.u64()?,
                length: d.u64()?,
                resident_offset: d.u64()?,
                resident_length: d.u64()?,
            })),
            255 => {
                let code = match d.byte()? {
                    0 => ErrorCode::InvalidArgument,
                    1 => ErrorCode::ProtocolViolation,
                    2 => ErrorCode::StaleRuntimeEpoch,
                    3 => ErrorCode::CapabilityUnsupported,
                    4 => ErrorCode::TargetContentConflict,
                    5 => ErrorCode::ResourceExhausted,
                    6 => ErrorCode::DeadlineExceeded,
                    7 => ErrorCode::Internal,
                    _ => return Err(invalid("unknown RemoteLoad error code")),
                };
                Ok(Self::Error(ProtocolError {
                    code,
                    message: d.text()?,
                    retryable: d.boolean()?,
                }))
            }
            _ => Err(invalid("unknown RemoteLoad reply tag")),
        }
    }
}

fn encode_release(e: &mut Encoder, release: &DependencyRelease) {
    e.byte(match release.kind {
        LayerKind::Memory => 0,
        LayerKind::Block => 1,
    });
    for value in [
        release.device,
        release.inode,
        release.offset,
        release.length,
    ] {
        e.u64(value);
    }
}

fn decode_release(d: &mut Decoder<'_>) -> io::Result<DependencyRelease> {
    Ok(DependencyRelease {
        kind: match d.byte()? {
            0 => LayerKind::Memory,
            1 => LayerKind::Block,
            _ => return Err(invalid("unknown dependency layer kind")),
        },
        device: d.u64()?,
        inode: d.u64()?,
        offset: d.u64()?,
        length: d.u64()?,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vm::protocol::VMM_PROTOCOL_VERSION;

    fn hex(bytes: &[u8]) -> String {
        bytes.iter().map(|value| format!("{value:02x}")).collect()
    }

    fn header() -> MessageHeader {
        MessageHeader {
            protocol_version: VMM_PROTOCOL_VERSION,
            channel: Channel::RemoteLoad,
            correlation_id: "golden".into(),
        }
    }

    #[test]
    fn dependency_request_and_ack_have_matching_bounded_binary_frames() {
        let release = DependencyRelease {
            kind: LayerKind::Block,
            device: 7,
            inode: 9,
            offset: 65536,
            length: 65536,
        };
        let request = WireEnvelope {
            header: header(),
            message: RemoteLoadRequest::ReleaseDependencies(release.clone()),
        };
        let ack = WireEnvelope {
            header: header(),
            message: RemoteLoadReply::DependenciesReleased(release),
        };
        let bytes = encode(&request).unwrap();
        assert_eq!(hex(&bytes),"52434b4c4f4144320a0006000000676f6c64656e03010700000000000000090000000000000000000100000000000000010000000000");
        assert_eq!(decode::<RemoteLoadRequest>(&bytes).unwrap(), request);
        assert_eq!(
            decode::<RemoteLoadReply>(&encode(&ack).unwrap()).unwrap(),
            ack
        );
        let mut truncated = bytes.clone();
        truncated.pop();
        assert!(decode::<RemoteLoadRequest>(&truncated).is_err());
        let mut trailing = bytes;
        trailing.push(0);
        assert!(decode::<RemoteLoadRequest>(&trailing).is_err());
    }

    #[test]
    fn inbound_remote_load_request_golden_binary_bytes_are_unchanged() {
        let message = WireEnvelope {
            header: header(),
            message: RemoteLoadRequest::RemoteLoad(RemoteLoad {
                target_id: "disk-0".into(),
                target_offset: 4096,
                length: 65536,
                origin: RemoteLoadOrigin::VirtioBlkReadMiss,
            }),
        };
        let payload = encode(&message).unwrap();
        assert_eq!(hex(&payload), "52434b4c4f4144320a0006000000676f6c64656e01060000006469736b2d300010000000000000000001000000000001");
        assert_eq!(decode::<RemoteLoadRequest>(&payload).unwrap(), message);
    }

    #[test]
    fn inbound_remote_load_shared_error_code_golden_binary_bytes_are_unchanged() {
        let message = WireEnvelope {
            header: header(),
            message: RemoteLoadReply::Error(ProtocolError {
                code: ErrorCode::DeadlineExceeded,
                message: "expired".into(),
                retryable: true,
            }),
        };
        let payload = encode(&message).unwrap();
        assert_eq!(
            hex(&payload),
            "52434b4c4f4144320a0006000000676f6c64656eff06070000006578706972656401"
        );
        assert_eq!(decode::<RemoteLoadReply>(&payload).unwrap(), message);
    }
}
