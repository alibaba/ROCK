//! Shared framing, identity, error and descriptor contracts for CLH socket channels.
//!
//! This module owns no inbound handler, outbound client or VM lifecycle code.
use std::collections::HashSet;
use std::io;
use std::os::fd::{AsRawFd, BorrowedFd, OwnedFd, RawFd};

use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use thiserror::Error;

pub const VMM_PROTOCOL_VERSION: u16 = 10;

pub const MAX_FRAME_SIZE: usize = 16 * 1024 * 1024;

pub const MAX_DESCRIPTORS: usize = 253;

pub(crate) const MAX_IDENTITY_LENGTH: usize = 256;

pub(crate) const MAX_OBJECT_KEY_LENGTH: usize = 4096;

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum VmmProtocolError {
    #[error("invalid {label} JSON: {message}")]
    InvalidJson { label: String, message: String },
    #[error("invalid {label}: {message}")]
    InvalidModel { label: String, message: String },
    #[error("cannot encode {label}: {message}")]
    Encode { label: String, message: String },
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, Hash, PartialEq, Serialize)]
#[serde(deny_unknown_fields, rename_all = "snake_case")]
pub enum Channel {
    RuntimeControl,
    RemoteLoad,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct MessageHeader {
    pub protocol_version: u16,
    pub channel: Channel,
    pub correlation_id: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct WireEnvelope<T> {
    pub header: MessageHeader,
    pub message: T,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields, rename_all = "snake_case")]
pub enum FdRole {
    MemoryRoLayer,
    MemorySharedMapping,
    BlockRoLayer,
    MemoryRangeIndex,
    BlockRangeIndex,
    VmState,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields, rename_all = "snake_case")]
pub enum FdAccess {
    ReadOnly,
    ReadWrite,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct FdRoleDescriptor {
    pub role: FdRole,
    pub access: FdAccess,
    pub content_id: String,
    pub size: u64,
    pub alignment: u64,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields, rename_all = "snake_case")]
pub enum SocketFdRole {
    RemoteLoadChannel,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SocketFdRoleDescriptor {
    pub role: SocketFdRole,
    pub content_id: String,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields, rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ErrorCode {
    InvalidArgument,
    ProtocolViolation,
    StaleRuntimeEpoch,
    CapabilityUnsupported,
    TargetContentConflict,
    ResourceExhausted,
    DeadlineExceeded,
    Internal,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProtocolError {
    pub code: ErrorCode,
    pub message: String,
    pub retryable: bool,
}

pub trait ValidateVmmMessage {
    const CHANNEL: Channel;

    fn validate(&self) -> Result<(), String>;

    fn validate_protocol_version(&self, version: u16) -> Result<(), String> {
        validate_supported_protocol_version(version)
    }

    fn fd_roles(&self) -> &[FdRoleDescriptor] {
        &[]
    }

    fn socket_fd_roles(&self) -> &[SocketFdRoleDescriptor] {
        &[]
    }
}

/// Validate the concrete descriptors against the message's canonical role
/// declarations. Both the legacy transport and HTTP-over-UDS use this one
/// boundary so changing the carrier cannot weaken descriptor semantics.
pub fn validate_message_fds(
    fds: &[BorrowedFd<'_>],
    roles: &[FdRoleDescriptor],
    socket_roles: &[SocketFdRoleDescriptor],
) -> Result<(), String> {
    if !roles.is_empty() && !socket_roles.is_empty() {
        return Err("one message cannot mix regular and socket FD roles".into());
    }
    if !socket_roles.is_empty() {
        if fds.len() != socket_roles.len() {
            return Err("SCM_RIGHTS socket FD count does not match socket_fd_roles".into());
        }
        for fd in fds {
            validate_unix_stream_fd(fd.as_raw_fd())?;
        }
        return Ok(());
    }
    if fds.len() != roles.len() {
        return Err("SCM_RIGHTS count does not match fd_roles".into());
    }
    for (fd, role) in fds.iter().zip(roles) {
        validate_regular_fd(fd.as_raw_fd(), role)?;
    }
    Ok(())
}

fn validate_unix_stream_fd(fd: RawFd) -> Result<(), String> {
    let metadata = descriptor_metadata(fd)?;
    if metadata.st_mode & libc::S_IFMT != libc::S_IFSOCK {
        return Err("FD is not a socket".into());
    }
    let mut socket_type = 0;
    let mut socket_type_len = std::mem::size_of::<libc::c_int>() as libc::socklen_t;
    // SAFETY: socket_type and its length are valid getsockopt output buffers.
    if unsafe {
        libc::getsockopt(
            fd,
            libc::SOL_SOCKET,
            libc::SO_TYPE,
            (&mut socket_type as *mut libc::c_int).cast(),
            &mut socket_type_len,
        )
    } != 0
    {
        return Err(io::Error::last_os_error().to_string());
    }
    let mut address = unsafe { std::mem::zeroed::<libc::sockaddr_storage>() };
    let mut address_len = std::mem::size_of::<libc::sockaddr_storage>() as libc::socklen_t;
    // SAFETY: address and length are valid getsockname output buffers.
    if unsafe {
        libc::getsockname(
            fd,
            (&mut address as *mut libc::sockaddr_storage).cast(),
            &mut address_len,
        )
    } != 0
    {
        return Err(io::Error::last_os_error().to_string());
    }
    if libc::c_int::from(address.ss_family) != libc::AF_UNIX || socket_type != libc::SOCK_STREAM {
        return Err("FD is not an AF_UNIX SOCK_STREAM socket".into());
    }
    validate_cloexec(fd)
}

fn validate_regular_fd(fd: RawFd, role: &FdRoleDescriptor) -> Result<(), String> {
    // SAFETY: F_GETFL reads flags from a live borrowed descriptor.
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 {
        return Err(io::Error::last_os_error().to_string());
    }
    let expected_access = match role.access {
        FdAccess::ReadOnly => libc::O_RDONLY,
        FdAccess::ReadWrite => libc::O_RDWR,
    };
    if flags & libc::O_ACCMODE != expected_access {
        return Err("FD access does not match role".into());
    }
    let metadata = descriptor_metadata(fd)?;
    if metadata.st_mode & libc::S_IFMT != libc::S_IFREG {
        return Err("FD is not a regular file or memfd".into());
    }
    if metadata.st_size < 0 || metadata.st_size as u64 != role.size {
        return Err("FD size does not match role".into());
    }
    if role.alignment == 0 || !role.size.is_multiple_of(role.alignment) {
        return Err("FD role geometry is invalid".into());
    }
    validate_cloexec(fd)
}

fn descriptor_metadata(fd: RawFd) -> Result<libc::stat, String> {
    // SAFETY: metadata is initialized by fstat on success.
    let mut metadata = unsafe { std::mem::zeroed::<libc::stat>() };
    if unsafe { libc::fstat(fd, &mut metadata) } != 0 {
        return Err(io::Error::last_os_error().to_string());
    }
    Ok(metadata)
}

fn validate_cloexec(fd: RawFd) -> Result<(), String> {
    // SAFETY: F_GETFD reads flags from a live borrowed descriptor.
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags < 0 {
        return Err(io::Error::last_os_error().to_string());
    }
    if flags & libc::FD_CLOEXEC == 0 {
        return Err("FD is not CLOEXEC".into());
    }
    Ok(())
}

pub fn decode_message<T>(payload: &[u8], label: &str) -> Result<WireEnvelope<T>, VmmProtocolError>
where
    T: DeserializeOwned + ValidateVmmMessage,
{
    if payload.len() > MAX_FRAME_SIZE {
        return Err(model_error(label, "frame exceeds maximum size"));
    }
    let envelope: WireEnvelope<T> =
        serde_json::from_slice(payload).map_err(|error| VmmProtocolError::InvalidJson {
            label: label.into(),
            message: error.to_string(),
        })?;
    validate_header(&envelope.header, T::CHANNEL)
        .and_then(|()| {
            envelope
                .message
                .validate_protocol_version(envelope.header.protocol_version)
        })
        .and_then(|()| envelope.message.validate())
        .map_err(|message| model_error(label, message))?;
    Ok(envelope)
}

pub fn encode_message<T>(
    envelope: &WireEnvelope<T>,
    label: &str,
) -> Result<Vec<u8>, VmmProtocolError>
where
    T: Serialize + ValidateVmmMessage,
{
    validate_header(&envelope.header, T::CHANNEL)
        .and_then(|()| {
            envelope
                .message
                .validate_protocol_version(envelope.header.protocol_version)
        })
        .and_then(|()| envelope.message.validate())
        .map_err(|message| model_error(label, message))?;
    let payload = serde_json::to_vec(envelope).map_err(|error| VmmProtocolError::Encode {
        label: label.into(),
        message: error.to_string(),
    })?;
    if payload.len() > MAX_FRAME_SIZE {
        return Err(model_error(label, "frame exceeds maximum size"));
    }
    Ok(payload)
}

pub(crate) fn validate_header(header: &MessageHeader, channel: Channel) -> Result<(), String> {
    validate_supported_protocol_version(header.protocol_version)?;
    if header.channel != channel {
        return Err("message belongs to the wrong channel".into());
    }
    validate_text(
        "correlation ID",
        &header.correlation_id,
        MAX_IDENTITY_LENGTH,
    )
}

fn validate_supported_protocol_version(version: u16) -> Result<(), String> {
    if version == VMM_PROTOCOL_VERSION {
        Ok(())
    } else {
        Err(format!("unsupported protocol version: {version}"))
    }
}

pub(crate) fn validate_protocol_error(value: &ProtocolError) -> Result<(), String> {
    validate_text("protocol error", &value.message, MAX_OBJECT_KEY_LENGTH)
}

pub(crate) fn validate_fd_roles(values: &[FdRoleDescriptor]) -> Result<(), String> {
    if values.len() > MAX_DESCRIPTORS {
        return Err("too many FD roles".into());
    }
    for value in values {
        validate_text("FD content ID", &value.content_id, MAX_IDENTITY_LENGTH)?;
        validate_size_alignment(value.size, value.alignment, "FD")?;
    }
    Ok(())
}

pub(crate) struct FdExpectation<'a> {
    role: FdRole,
    access: FdAccess,
    content_id: &'a str,
    size: u64,
    alignment: u64,
}

impl<'a> FdExpectation<'a> {
    pub(crate) fn new(
        role: FdRole,
        access: FdAccess,
        content_id: &'a str,
        size: u64,
        alignment: u64,
    ) -> Self {
        Self {
            role,
            access,
            content_id,
            size,
            alignment,
        }
    }
}

pub(crate) fn validate_fd_reference(
    index: u32,
    roles: &[FdRoleDescriptor],
    expected: FdExpectation<'_>,
    referenced: &mut HashSet<u32>,
) -> Result<(), String> {
    let descriptor = roles
        .get(index as usize)
        .ok_or_else(|| "FD index is outside fd_roles".to_string())?;
    if !referenced.insert(index) {
        return Err("FD role is referenced more than once".into());
    }
    if descriptor.role != expected.role
        || descriptor.access != expected.access
        || descriptor.content_id != expected.content_id
        || descriptor.size != expected.size
        || descriptor.alignment != expected.alignment
    {
        return Err("FD descriptor does not match its message reference".into());
    }
    Ok(())
}

pub(crate) fn validate_all_fds_referenced(
    roles: &[FdRoleDescriptor],
    referenced: &HashSet<u32>,
) -> Result<(), String> {
    if roles.len() != referenced.len() {
        return Err("every FD role must be referenced exactly once".into());
    }
    Ok(())
}

pub(crate) fn validate_text(label: &str, value: &str, max_bytes: usize) -> Result<(), String> {
    if value.is_empty() || value.len() > max_bytes {
        return Err(format!("{label} length must be 1..={max_bytes} bytes"));
    }
    Ok(())
}

pub(crate) fn validate_collection(
    label: &str,
    length: usize,
    nonempty: bool,
) -> Result<(), String> {
    if length > MAX_DESCRIPTORS || (nonempty && length == 0) {
        return Err(format!("{label} count is outside protocol bounds"));
    }
    Ok(())
}

pub(crate) fn validate_unique_texts(
    label: &str,
    values: &[String],
    nonempty: bool,
) -> Result<(), String> {
    validate_collection(label, values.len(), nonempty)?;
    let mut unique = HashSet::new();
    for value in values {
        validate_text(label, value, MAX_IDENTITY_LENGTH)?;
        if !unique.insert(value) {
            return Err(format!("duplicate {label}"));
        }
    }
    Ok(())
}

pub(crate) fn validate_size_alignment(
    size: u64,
    alignment: u64,
    label: &str,
) -> Result<(), String> {
    if size == 0
        || alignment == 0
        || !alignment.is_power_of_two()
        || !size.is_multiple_of(alignment)
    {
        return Err(format!("invalid {label} size or alignment"));
    }
    Ok(())
}

pub(crate) fn validate_range(offset: u64, length: u64, label: &str) -> Result<(), String> {
    if length == 0 || offset.checked_add(length).is_none() {
        return Err(format!("{label} is empty or overflows"));
    }
    Ok(())
}

pub(crate) fn validate_deadline(value: u64) -> Result<(), String> {
    if value == 0 {
        return Err("deadline must be nonzero".into());
    }
    Ok(())
}

pub(crate) fn validate_hex_sha256(value: &str, lowercase: bool) -> Result<(), String> {
    if value.len() != 64
        || !value.bytes().all(|byte| {
            byte.is_ascii_digit()
                || (b'a'..=b'f').contains(&byte)
                || (!lowercase && (b'A'..=b'F').contains(&byte))
        })
    {
        return Err("invalid SHA-256 identity".into());
    }
    Ok(())
}

fn model_error(label: &str, message: impl Into<String>) -> VmmProtocolError {
    VmmProtocolError::InvalidModel {
        label: label.into(),
        message: message.into(),
    }
}

#[derive(Debug)]
pub struct ReceivedVmmMessage<T> {
    pub envelope: WireEnvelope<T>,
    pub fds: Vec<OwnedFd>,
}

impl<T> ReceivedVmmMessage<T> {
    pub fn into_parts(self) -> (WireEnvelope<T>, Vec<OwnedFd>) {
        (self.envelope, self.fds)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::CString;
    use std::fs::File;
    use std::os::fd::{AsFd, FromRawFd};
    use std::os::unix::net::UnixStream;

    fn readonly_memory() -> File {
        let name = CString::new("shared-protocol-fd-test").unwrap();
        // SAFETY: the static name is NUL-terminated and the returned FD is owned once.
        let fd = unsafe { libc::memfd_create(name.as_ptr(), libc::MFD_CLOEXEC) };
        assert!(fd >= 0);
        let writable = unsafe { File::from_raw_fd(fd) };
        writable.set_len(4096).unwrap();
        File::open(format!("/proc/self/fd/{}", writable.as_raw_fd())).unwrap()
    }

    fn memory_role() -> FdRoleDescriptor {
        FdRoleDescriptor {
            role: FdRole::MemoryRoLayer,
            access: FdAccess::ReadOnly,
            content_id: "memory-content".into(),
            size: 4096,
            alignment: 4096,
        }
    }

    #[test]
    fn shared_fd_access_size_count_and_cloexec_contract_is_preserved() {
        let file = readonly_memory();
        let role = memory_role();
        validate_message_fds(&[file.as_fd()], std::slice::from_ref(&role), &[]).unwrap();
        assert_eq!(
            validate_message_fds(&[], std::slice::from_ref(&role), &[]).unwrap_err(),
            "SCM_RIGHTS count does not match fd_roles"
        );
        let mut wrong = role.clone();
        wrong.access = FdAccess::ReadWrite;
        assert_eq!(
            validate_message_fds(&[file.as_fd()], &[wrong], &[]).unwrap_err(),
            "FD access does not match role"
        );
        let mut wrong = role.clone();
        wrong.size = 8192;
        assert_eq!(
            validate_message_fds(&[file.as_fd()], &[wrong], &[]).unwrap_err(),
            "FD size does not match role"
        );
        // SAFETY: only this test's owned descriptor is changed, without closing it.
        assert_eq!(
            unsafe { libc::fcntl(file.as_raw_fd(), libc::F_SETFD, 0) },
            0
        );
        assert_eq!(
            validate_message_fds(&[file.as_fd()], &[role], &[]).unwrap_err(),
            "FD is not CLOEXEC"
        );
    }

    #[test]
    fn shared_socket_fd_roles_still_require_unix_stream_and_no_mixed_roles() {
        let (stream, _peer) = UnixStream::pair().unwrap();
        let socket = SocketFdRoleDescriptor {
            role: SocketFdRole::RemoteLoadChannel,
            content_id: "remote-load-channel".into(),
        };
        validate_message_fds(&[stream.as_fd()], &[], std::slice::from_ref(&socket)).unwrap();
        assert_eq!(
            validate_message_fds(
                &[stream.as_fd()],
                &[memory_role()],
                std::slice::from_ref(&socket)
            )
            .unwrap_err(),
            "one message cannot mix regular and socket FD roles"
        );
        let tcp = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        assert_eq!(
            validate_message_fds(&[tcp.as_fd()], &[], &[socket]).unwrap_err(),
            "FD is not an AF_UNIX SOCK_STREAM socket"
        );
    }
}
