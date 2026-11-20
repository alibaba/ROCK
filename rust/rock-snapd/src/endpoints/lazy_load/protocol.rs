//! Strict CLH-to-snapd lazy-load request/reply payloads.
//!
//! The wire names, fields, validation and version remain shared with CLH.
use serde::{Deserialize, Serialize};

use crate::vm::protocol::{
    validate_protocol_error, validate_range, validate_text, Channel, ProtocolError,
    ValidateVmmMessage, MAX_IDENTITY_LENGTH,
};
use crate::vm::runtime::protocol::LayerKind;

/// Successful COW released this runtime's dependency on one native inode.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DependencyRelease {
    pub kind: LayerKind,
    pub device: u64,
    pub inode: u64,
    pub offset: u64,
    pub length: u64,
}

impl DependencyRelease {
    pub fn validate(&self) -> Result<(), String> {
        validate_range(self.offset, self.length, "dependency range")?;
        let unit = match self.kind {
            LayerKind::Memory => 4096,
            LayerKind::Block => 65536,
        };
        let alignment = match self.kind {
            LayerKind::Memory => 4096,
            LayerKind::Block => 512,
        };
        if self.inode == 0 || self.offset % unit != 0 || self.length % alignment != 0 {
            return Err("dependency release identity or alignment is invalid".into());
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields, rename_all = "snake_case")]
pub enum RemoteLoadOrigin {
    UffdMissing,
    VirtioBlkReadMiss,
    DebugOther,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RemoteLoad {
    pub target_id: String,
    pub target_offset: u64,
    pub length: u64,
    pub origin: RemoteLoadOrigin,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RemoteLoadReady {
    pub target_id: String,
    pub target_offset: u64,
    pub length: u64,
    pub resident_offset: u64,
    pub resident_length: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(
    deny_unknown_fields,
    tag = "type",
    content = "body",
    rename_all = "snake_case"
)]
pub enum RemoteLoadRequest {
    RemoteLoad(RemoteLoad),
    ReleaseDependencies(DependencyRelease),
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(
    deny_unknown_fields,
    tag = "type",
    content = "body",
    rename_all = "snake_case"
)]
pub enum RemoteLoadReply {
    RemoteLoadReady(RemoteLoadReady),
    DependenciesReleased(DependencyRelease),
    Error(ProtocolError),
}

impl ValidateVmmMessage for RemoteLoadRequest {
    const CHANNEL: Channel = Channel::RemoteLoad;

    fn validate(&self) -> Result<(), String> {
        match self {
            Self::ReleaseDependencies(value) => value.validate(),
            Self::RemoteLoad(value) => {
                validate_text("target ID", &value.target_id, MAX_IDENTITY_LENGTH)?;
                validate_range(value.target_offset, value.length, "target range")?;
                Ok(())
            }
        }
    }
}

impl ValidateVmmMessage for RemoteLoadReply {
    const CHANNEL: Channel = Channel::RemoteLoad;

    fn validate(&self) -> Result<(), String> {
        match self {
            Self::DependenciesReleased(value) => value.validate(),
            Self::RemoteLoadReady(value) => {
                validate_text("target ID", &value.target_id, MAX_IDENTITY_LENGTH)?;
                validate_range(value.target_offset, value.length, "target range")?;
                validate_range(
                    value.resident_offset,
                    value.resident_length,
                    "resident range",
                )?;
                let target_end = value
                    .target_offset
                    .checked_add(value.length)
                    .ok_or_else(|| "target range overflows".to_string())?;
                let resident_end = value
                    .resident_offset
                    .checked_add(value.resident_length)
                    .ok_or_else(|| "resident range overflows".to_string())?;
                if value.resident_offset > value.target_offset || resident_end < target_end {
                    return Err("resident range must contain the requested target range".into());
                }
                Ok(())
            }
            Self::Error(value) => validate_protocol_error(value),
        }
    }
}
