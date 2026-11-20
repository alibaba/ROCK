//! Blocking, descriptor-owning Unix transport for the VMM protocol.

use std::io::{self, IoSlice, IoSliceMut, Read, Write};
use std::net::Shutdown;
use std::os::fd::{AsFd, AsRawFd, BorrowedFd, FromRawFd, OwnedFd, RawFd};
use std::os::unix::net::UnixStream;

use nix::sys::socket::{
    recvmsg, sendmsg, ControlMessage, ControlMessageOwned, MsgFlags, SockaddrStorage,
};

use super::protocol::{RemoteLoadReply, RemoteLoadRequest};
use super::remote_load_wire::{self, RemoteLoadBody};
use crate::vm::protocol::{
    validate_header, validate_message_fds, FdRoleDescriptor, SocketFdRoleDescriptor,
    ValidateVmmMessage, VmmProtocolError, WireEnvelope, MAX_DESCRIPTORS, MAX_FRAME_SIZE,
};

use crate::vm::protocol::ReceivedVmmMessage;

#[derive(Debug)]
struct FramedChannel {
    stream: Option<UnixStream>,
}

impl FramedChannel {
    fn new(stream: UnixStream) -> io::Result<Self> {
        stream.set_nonblocking(false)?;
        stream.set_cloexec()?;
        Ok(Self {
            stream: Some(stream),
        })
    }

    fn send<T>(
        &mut self,
        envelope: &WireEnvelope<T>,
        fds: &[BorrowedFd<'_>],
        label: &str,
    ) -> io::Result<()>
    where
        T: RemoteLoadBody + ValidateVmmMessage,
    {
        let result = (|| {
            validate_binary_envelope(envelope, label).map_err(protocol_input)?;
            let payload = remote_load_wire::encode(envelope)?;
            validate_outbound_fds(
                fds,
                envelope.message.fd_roles(),
                envelope.message.socket_fd_roles(),
            )?;
            match self.stream.as_mut() {
                Some(stream) => send_frame(stream, &payload, fds),
                None => Err(poisoned_error()),
            }
        })();
        if result.is_err() {
            self.poison();
        }
        result
    }

    fn receive<T>(&mut self, label: &str) -> io::Result<ReceivedVmmMessage<T>>
    where
        T: RemoteLoadBody + ValidateVmmMessage,
    {
        let result = match self.stream.as_mut() {
            Some(stream) => receive_frame(stream),
            None => Err(poisoned_error()),
        };
        let (payload, fds) = match result {
            Ok(value) => value,
            Err(error) => {
                self.poison();
                return Err(error);
            }
        };
        let envelope: WireEnvelope<T> = match remote_load_wire::decode(&payload) {
            Ok(value) => value,
            Err(error) => {
                self.poison();
                return Err(error);
            }
        };
        if let Err(error) = validate_binary_envelope(&envelope, label) {
            self.poison();
            return Err(protocol_data(error));
        }
        if let Err(error) = validate_received_fds(
            &fds,
            envelope.message.fd_roles(),
            envelope.message.socket_fd_roles(),
        ) {
            self.poison();
            return Err(error);
        }
        Ok(ReceivedVmmMessage { envelope, fds })
    }

    fn poison(&mut self) {
        if let Some(stream) = self.stream.take() {
            let _ = stream.shutdown(Shutdown::Both);
        }
    }

    fn is_poisoned(&self) -> bool {
        self.stream.is_none()
    }
}

fn validate_binary_envelope<T: ValidateVmmMessage>(
    envelope: &WireEnvelope<T>,
    label: &str,
) -> Result<(), VmmProtocolError> {
    validate_header(&envelope.header, T::CHANNEL)
        .and_then(|()| {
            envelope
                .message
                .validate_protocol_version(envelope.header.protocol_version)
        })
        .and_then(|()| envelope.message.validate())
        .map_err(|message| VmmProtocolError::InvalidModel {
            label: label.into(),
            message,
        })
}

#[derive(Debug)]
pub struct RemoteLoadTransport {
    channel: FramedChannel,
}

impl RemoteLoadTransport {
    pub fn new(stream: UnixStream) -> io::Result<Self> {
        Ok(Self {
            channel: FramedChannel::new(stream)?,
        })
    }

    pub fn receive_request(&mut self) -> io::Result<ReceivedVmmMessage<RemoteLoadRequest>> {
        self.channel.receive("remote-load request")
    }

    /// Symmetric test/VMM-side operation. Production snapd uses
    /// `receive_request`; the shared framing remains byte-identical.
    pub fn send_request(&mut self, request: &WireEnvelope<RemoteLoadRequest>) -> io::Result<()> {
        self.channel.send(request, &[], "remote-load request")
    }

    pub fn send_reply(&mut self, reply: &WireEnvelope<RemoteLoadReply>) -> io::Result<()> {
        self.channel.send(reply, &[], "remote-load reply")
    }

    /// Symmetric test/VMM-side operation paired with `send_request`.
    pub fn receive_reply(&mut self) -> io::Result<ReceivedVmmMessage<RemoteLoadReply>> {
        self.channel.receive("remote-load reply")
    }

    pub fn is_poisoned(&self) -> bool {
        self.channel.is_poisoned()
    }

    pub fn close(&mut self) {
        self.channel.poison();
    }
}

fn send_frame(stream: &mut UnixStream, payload: &[u8], fds: &[BorrowedFd<'_>]) -> io::Result<()> {
    let length = u32::try_from(payload.len())
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "frame exceeds u32"))?;
    if payload.len() > MAX_FRAME_SIZE {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "frame exceeds maximum size",
        ));
    }
    let header = length.to_le_bytes();
    let slices = [IoSlice::new(&header), IoSlice::new(payload)];
    let raw_fds = fds.iter().map(AsRawFd::as_raw_fd).collect::<Vec<_>>();
    let control = if raw_fds.is_empty() {
        Vec::new()
    } else {
        vec![ControlMessage::ScmRights(&raw_fds)]
    };
    let sent =
        sendmsg::<SockaddrStorage>(stream.as_raw_fd(), &slices, &control, send_flags(), None)
            .map_err(errno_error)?;
    if sent == 0 {
        return Err(io::Error::new(
            io::ErrorKind::WriteZero,
            "sendmsg made no progress",
        ));
    }
    let frame_length = header.len() + payload.len();
    if sent < frame_length {
        if sent < header.len() {
            stream.write_all(&header[sent..])?;
            stream.write_all(payload)?;
        } else {
            stream.write_all(&payload[sent - header.len()..])?;
        }
    }
    Ok(())
}

fn receive_frame(stream: &mut UnixStream) -> io::Result<(Vec<u8>, Vec<OwnedFd>)> {
    let mut header = [0_u8; 4];
    let mut slices = [IoSliceMut::new(&mut header)];
    let mut control_space = nix::cmsg_space!([RawFd; MAX_DESCRIPTORS]);
    let message = recvmsg::<SockaddrStorage>(
        stream.as_raw_fd(),
        &mut slices,
        Some(&mut control_space),
        receive_flags(),
    )
    .map_err(errno_error)?;
    if message.bytes == 0 {
        return Err(io::Error::new(
            io::ErrorKind::UnexpectedEof,
            "VMM channel reached EOF",
        ));
    }
    if message
        .flags
        .intersects(MsgFlags::MSG_CTRUNC | MsgFlags::MSG_TRUNC)
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "frame or descriptor data was truncated",
        ));
    }
    let mut received = Vec::new();
    for control in message.cmsgs().map_err(errno_error)? {
        match control {
            ControlMessageOwned::ScmRights(raw_fds) => {
                if received.len() + raw_fds.len() > MAX_DESCRIPTORS {
                    close_raw_fds(raw_fds);
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "too many SCM_RIGHTS descriptors",
                    ));
                }
                for raw_fd in raw_fds {
                    if let Err(error) = set_fd_cloexec(raw_fd) {
                        // SAFETY: the descriptor has not been transferred to
                        // OwnedFd yet.
                        unsafe {
                            libc::close(raw_fd);
                        }
                        return Err(error);
                    }
                    // SAFETY: SCM_RIGHTS returned a new descriptor owned by this process.
                    received.push(unsafe { OwnedFd::from_raw_fd(raw_fd) });
                }
            }
            _ => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "unexpected ancillary data",
                ));
            }
        }
    }
    let bytes = message.bytes;
    if bytes < header.len() {
        stream.read_exact(&mut header[bytes..])?;
    }
    let length = u32::from_le_bytes(header) as usize;
    if length > MAX_FRAME_SIZE {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "frame exceeds maximum size",
        ));
    }
    let mut payload = vec![0_u8; length];
    stream.read_exact(&mut payload)?;
    Ok((payload, received))
}

fn validate_outbound_fds(
    fds: &[BorrowedFd<'_>],
    roles: &[FdRoleDescriptor],
    socket_roles: &[SocketFdRoleDescriptor],
) -> io::Result<()> {
    validate_message_fds(fds, roles, socket_roles)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))
}

fn validate_received_fds(
    fds: &[OwnedFd],
    roles: &[FdRoleDescriptor],
    socket_roles: &[SocketFdRoleDescriptor],
) -> io::Result<()> {
    let borrowed = fds.iter().map(AsFd::as_fd).collect::<Vec<_>>();
    validate_message_fds(&borrowed, roles, socket_roles)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
}

fn close_raw_fds(raw_fds: Vec<RawFd>) {
    for raw_fd in raw_fds {
        // SAFETY: these descriptors arrived through SCM_RIGHTS and have not
        // been transferred into OwnedFd.
        unsafe {
            libc::close(raw_fd);
        }
    }
}

fn errno_error(error: nix::errno::Errno) -> io::Error {
    io::Error::from_raw_os_error(error as i32)
}

fn protocol_input(error: VmmProtocolError) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, error)
}

fn protocol_data(error: VmmProtocolError) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, error)
}

fn poisoned_error() -> io::Error {
    io::Error::new(io::ErrorKind::BrokenPipe, "VMM channel is poisoned")
}

fn send_flags() -> MsgFlags {
    MsgFlags::MSG_NOSIGNAL
}

fn receive_flags() -> MsgFlags {
    MsgFlags::MSG_CMSG_CLOEXEC
}

fn set_fd_cloexec(fd: RawFd) -> io::Result<()> {
    // SAFETY: fcntl only reads/updates descriptor flags.
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

trait UnixStreamExt {
    fn set_cloexec(&self) -> io::Result<()>;
}

impl UnixStreamExt for UnixStream {
    fn set_cloexec(&self) -> io::Result<()> {
        // SAFETY: fcntl only reads/updates flags on this live descriptor.
        let flags = unsafe { libc::fcntl(self.as_raw_fd(), libc::F_GETFD) };
        if flags < 0
            || unsafe { libc::fcntl(self.as_raw_fd(), libc::F_SETFD, flags | libc::FD_CLOEXEC) } < 0
        {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }
}
