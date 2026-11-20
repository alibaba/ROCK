//! VMM process capabilities and FD-aware runtime control over a client-owned UDS.

use std::collections::HashMap;
use std::io::{self, IoSlice, IoSliceMut, Read, Write};
use std::os::fd::{AsFd, AsRawFd, BorrowedFd, FromRawFd, OwnedFd, RawFd};
use std::os::unix::net::UnixStream;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use nix::sys::socket::{
    recvmsg, sendmsg, ControlMessage, ControlMessageOwned, MsgFlags, SockaddrStorage,
};
use thiserror::Error;

use crate::snapshot::metadata::RockletArtifactCompatibility;
use crate::vm::error::OperationError as SnapshotControlError;
use crate::vm::protocol::{
    decode_message, encode_message, validate_message_fds, ReceivedVmmMessage, ValidateVmmMessage,
    VmmProtocolError, WireEnvelope, MAX_DESCRIPTORS, MAX_FRAME_SIZE,
};
use crate::vm::repository::RuntimeLaunchPlan;
use crate::vm::runtime::launcher::{
    launch_runtime, GuestEndpointInfo, GuestReadinessStats, VmmLaunchError, VmmLaunchSpec,
    VmmRuntimeHandles,
};
use crate::vm::runtime::protocol::{ControlReply, ControlRequest};

pub(crate) type RuntimeLaunchFactory =
    Arc<dyn Fn(&RuntimeLaunchPlan) -> Result<VmmLaunchSpec, SnapshotControlError> + Send + Sync>;

pub(crate) fn validate_launch_rocklet(
    launch_spec: &VmmLaunchSpec,
    expected: Option<&RockletArtifactCompatibility>,
) -> Result<(), SnapshotControlError> {
    let Some(expected) = expected else {
        return Ok(());
    };
    let actual = launch_spec.guest_rocklet.as_ref().ok_or_else(|| {
        SnapshotControlError::IdentityConflict(
            "FULL snapshot requires a verified guest Rocklet artifact on the target node".into(),
        )
    })?;
    if actual.identity.identity != expected.identity
        || actual.identity.version != expected.version
        || format!("sha256:{}", actual.identity.sha256) != expected.raw_sha256
        || actual.identity.logical_bytes != expected.logical_bytes
        || actual.guest_port != expected.guest_port
    {
        return Err(SnapshotControlError::IdentityConflict(
            "snapshot Rocklet artifact pin does not match the verified target-node artifact".into(),
        ));
    }
    Ok(())
}

/// Adapter errors remain independent of core's business/lifecycle errors.
#[derive(Clone, Debug, Error)]
pub enum VmmControllerError {
    #[error("Invalid runtime request: {0}")]
    Invalid(String),
    #[error("VMM protocol failed: {0}")]
    Protocol(String),
    #[error("VMM operation failed: {0}")]
    Vmm(String),
    #[error("Runtime I/O failed: {0}")]
    Io(String),
}

impl From<VmmClientError> for VmmControllerError {
    fn from(error: VmmClientError) -> Self {
        Self::Vmm(error.to_string())
    }
}

impl From<VmmLaunchError> for VmmControllerError {
    fn from(error: VmmLaunchError) -> Self {
        Self::Vmm(error.to_string())
    }
}

/// Process/control capabilities supplied by a VMM implementation. The initial
/// implementation is Cloud Hypervisor; core never owns its HTTP/UDS client.
#[async_trait]
pub trait VMMClientInterface: Send + Sync {
    fn pid(&self) -> u32;
    fn control(&self) -> Arc<dyn VmControlApi>;
    fn has_exited(&self) -> Result<bool, VmmControllerError>;
    fn remote_load_stream(&self) -> Result<Option<UnixStream>, VmmControllerError> {
        Ok(None)
    }
    fn guest_endpoint(&self) -> Option<GuestEndpointInfo> {
        None
    }
    async fn plugger_control(
        &self,
        _action: &str,
        _payload: &serde_json::Value,
        _timeout: Duration,
    ) -> Result<crate::vm::runtime::plugger::PluggerReply, VmmControllerError> {
        Err(VmmControllerError::Invalid(
            "runtime does not provide independent snapd-plugger control".into(),
        ))
    }
    async fn wait_for_guest_rocklet(
        &self,
        _timeout: Duration,
    ) -> Result<GuestReadinessStats, VmmControllerError> {
        Err(VmmControllerError::Invalid(
            "runtime does not provide a guest Rocklet endpoint".into(),
        ))
    }
    async fn wait_for_guest_plugger(
        &self,
        timeout: Duration,
    ) -> Result<GuestReadinessStats, VmmControllerError> {
        let started = Instant::now();
        let mut attempts = 0_u64;
        let health_payload = serde_json::json!({});
        loop {
            let remaining = timeout.saturating_sub(started.elapsed());
            if remaining.is_zero() {
                return Err(VmmControllerError::Invalid(
                    "independent snapd-plugger did not become healthy on VSOCK 22556".into(),
                ));
            }
            attempts += 1;
            let attempt_timeout = remaining.min(Duration::from_secs(2));
            let health = self.plugger_control("health", &health_payload, attempt_timeout);
            if let Ok(Ok(reply)) = tokio::time::timeout(attempt_timeout, health).await {
                if reply.status == 200
                    && reply.body.get("closed") == Some(&serde_json::Value::Bool(false))
                {
                    return Ok(GuestReadinessStats {
                        attempts,
                        elapsed: started.elapsed(),
                        transport_event_wait: Duration::ZERO,
                        transport_event_observed: false,
                        probe_attempts: Vec::new(),
                        proxy_attempts: Vec::new(),
                    });
                }
            }
            tokio::time::sleep(
                Duration::from_millis(50).min(timeout.saturating_sub(started.elapsed())),
            )
            .await;
        }
    }
    async fn cleanup(&self, reason: &str, timeout: Duration) -> Result<(), VmmControllerError>;
}

pub(crate) async fn selected_guest_readiness(
    runtime: &dyn VMMClientInterface,
    mode: crate::vm::runtime::plugger::GuestReadinessMode,
    timeout: Duration,
) -> Result<GuestReadinessStats, VmmControllerError> {
    match mode {
        crate::vm::runtime::plugger::GuestReadinessMode::Rocklet => {
            runtime.wait_for_guest_rocklet(timeout).await
        }
        crate::vm::runtime::plugger::GuestReadinessMode::PluggerOnly => {
            runtime.wait_for_guest_plugger(timeout).await
        }
    }
}

#[async_trait]
impl VMMClientInterface for VmmRuntimeHandles {
    fn pid(&self) -> u32 {
        self.pid()
    }

    fn control(&self) -> Arc<dyn VmControlApi> {
        self.control.clone()
    }

    fn has_exited(&self) -> Result<bool, VmmControllerError> {
        VmmRuntimeHandles::has_exited(self).map_err(Into::into)
    }

    fn remote_load_stream(&self) -> Result<Option<UnixStream>, VmmControllerError> {
        self.remote_load
            .try_clone()
            .map(Some)
            .map_err(|error| VmmControllerError::Io(error.to_string()))
    }

    fn guest_endpoint(&self) -> Option<GuestEndpointInfo> {
        VmmRuntimeHandles::guest_endpoint(self)
    }

    async fn plugger_control(
        &self,
        action: &str,
        payload: &serde_json::Value,
        timeout: Duration,
    ) -> Result<crate::vm::runtime::plugger::PluggerReply, VmmControllerError> {
        VmmRuntimeHandles::plugger_control(self, action, payload, timeout)
            .await
            .map_err(|error| VmmControllerError::Io(error.to_string()))
    }

    async fn wait_for_guest_rocklet(
        &self,
        timeout: Duration,
    ) -> Result<GuestReadinessStats, VmmControllerError> {
        VmmRuntimeHandles::wait_for_guest_rocklet(self, timeout)
            .await
            .map_err(Into::into)
    }

    async fn cleanup(&self, reason: &str, timeout: Duration) -> Result<(), VmmControllerError> {
        match self.shutdown(reason, timeout).await {
            Ok(_) => Ok(()),
            Err(shutdown_error) => {
                let terminate = self.terminate(timeout).await;
                match terminate {
                    Ok(()) => Err(shutdown_error.into()),
                    Err(terminate_error) => Err(VmmControllerError::Vmm(format!(
                        "{shutdown_error}; fallback cleanup failed: {terminate_error}"
                    ))),
                }
            }
        }
    }
}

/// Built-in VMM factory. Backend selection and concrete process handles remain
/// behind the controller boundary; core receives only client capabilities.
pub fn launch_client(
    spec: VmmLaunchSpec,
) -> Result<Box<dyn VMMClientInterface>, VmmControllerError> {
    launch_runtime(spec)
        .map(|handles| Box::new(handles) as Box<dyn VMMClientInterface>)
        .map_err(Into::into)
}

#[derive(Clone, Debug, Eq, Error, PartialEq)]
pub enum VmmClientError {
    #[error("Runtime-control transport failed: {0}")]
    Transport(String),
    #[error("Runtime-control client state is poisoned")]
    StatePoisoned,
    #[error("Runtime-control blocking task failed: {0}")]
    BlockingTask(String),
    #[error("Runtime-control {operation} submission {correlation_id:?} was already attempted")]
    AlreadySubmitted {
        operation: &'static str,
        correlation_id: String,
    },
}

impl From<HttpControlError> for VmmClientError {
    fn from(error: HttpControlError) -> Self {
        Self::Transport(error.to_string())
    }
}

/// One keep-alive RuntimeControl HTTP session.
///
/// The protocol is request/reply, so concurrent async callers are serialized
/// on one blocking Unix stream. Cancellation drops only the waiter; the
/// blocking transaction still drains its response before a later request can
/// reuse the connection.
pub struct VmmControlClient {
    inner: Arc<Mutex<UnixStream>>,
    submissions: Arc<Mutex<HashMap<SubmissionKey, SubmissionOutcome>>>,
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
struct SubmissionKey {
    operation: &'static str,
    correlation_id: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SubmissionOutcome {
    InFlight,
    Completed,
    Unknown,
}

impl VmmControlClient {
    pub fn new(stream: UnixStream) -> Result<Self, VmmClientError> {
        stream
            .set_nonblocking(false)
            .map_err(HttpControlError::Io)?;
        set_fd_cloexec(stream.as_raw_fd()).map_err(HttpControlError::Io)?;
        Ok(Self {
            inner: Arc::new(Mutex::new(stream)),
            submissions: Arc::new(Mutex::new(HashMap::new())),
        })
    }

    pub async fn request(
        &self,
        request: WireEnvelope<ControlRequest>,
        fds: Vec<OwnedFd>,
    ) -> Result<ReceivedVmmMessage<ControlReply>, VmmClientError> {
        let submission = operation_name(&request.message).map(|operation| SubmissionKey {
            operation,
            correlation_id: request.header.correlation_id.clone(),
        });
        if let Some(submission) = &submission {
            let mut submissions = self
                .submissions
                .lock()
                .map_err(|_| VmmClientError::StatePoisoned)?;
            if submissions
                .insert(submission.clone(), SubmissionOutcome::InFlight)
                .is_some()
            {
                return Err(VmmClientError::AlreadySubmitted {
                    operation: submission.operation,
                    correlation_id: submission.correlation_id.clone(),
                });
            }
        }
        let inner = Arc::clone(&self.inner);
        let submissions = Arc::clone(&self.submissions);
        let blocking = tokio::task::spawn_blocking(move || {
            let correlation_id = request.header.correlation_id.clone();
            let borrowed = fds.iter().map(AsFd::as_fd).collect::<Vec<_>>();
            let result = (|| {
                let mut stream = inner.lock().map_err(|_| VmmClientError::StatePoisoned)?;
                let response = request_blocking(&mut stream, &request, &borrowed)?;
                if response.envelope.header.correlation_id != correlation_id {
                    return Err(VmmClientError::Transport(
                        "HTTP response correlation does not match its request".into(),
                    ));
                }
                Ok(response)
            })();
            if let Some(submission) = submission {
                if let Ok(mut state) = submissions.lock() {
                    state.insert(
                        submission,
                        if result.is_ok() {
                            SubmissionOutcome::Completed
                        } else {
                            SubmissionOutcome::Unknown
                        },
                    );
                }
            }
            result
        });
        blocking
            .await
            .map_err(|error| VmmClientError::BlockingTask(error.to_string()))?
    }
}

/// Typed runtime-control boundary shared by VM management and snapshot capture.
///
/// The adapter owns descriptor transfer and correlation checking. Business
/// services supply the protocol request and retain their lifecycle decisions.
#[async_trait]
pub trait VmControlApi: Send + Sync {
    async fn request(
        &self,
        request: WireEnvelope<ControlRequest>,
        fds: Vec<OwnedFd>,
    ) -> Result<ReceivedVmmMessage<ControlReply>, VmmClientError>;
}

#[async_trait]
impl VmControlApi for VmmControlClient {
    async fn request(
        &self,
        request: WireEnvelope<ControlRequest>,
        fds: Vec<OwnedFd>,
    ) -> Result<ReceivedVmmMessage<ControlReply>, VmmClientError> {
        VmmControlClient::request(self, request, fds).await
    }
}

const fn operation_name(request: &ControlRequest) -> Option<&'static str> {
    match request {
        ControlRequest::StartRuntime(_) => Some("StartRuntime"),
        ControlRequest::CaptureGeneration(_) => Some("CaptureGeneration"),
        ControlRequest::FinalizePublishedGeneration(_) => Some("FinalizePublishedGeneration"),
        ControlRequest::ShutdownRuntimeSession(_) => Some("ShutdownRuntimeSession"),
        _ => None,
    }
}

const RUNTIME_CONTROL_PATH: &str = "/api/v1/vm.rock-runtime-control";
const MAX_HTTP_CONTROL_FDS: usize = MAX_DESCRIPTORS;
const MAX_HTTP_HEADER_BYTES: usize = 64 * 1024;
const FIRST_READ_BYTES: usize = 64 * 1024;

#[derive(Debug, Error)]
enum HttpControlError {
    #[error("invalid runtime-control HTTP request: {0}")]
    InvalidRequest(String),
    #[error("invalid runtime-control HTTP response: {0}")]
    InvalidResponse(String),
    #[error("runtime-control HTTP transport failed")]
    Io(#[source] io::Error),
    #[error("runtime-control protocol failed")]
    Protocol(#[source] VmmProtocolError),
    #[error("runtime-control endpoint returned HTTP status {0}")]
    HttpStatus(u16),
}

#[derive(Debug)]
struct HttpResponseWithFds {
    status_code: u16,
    body: Vec<u8>,
    files: Vec<OwnedFd>,
}

/// One blocking typed HTTP transaction on the client-owned keep-alive stream.
fn request_blocking(
    stream: &mut UnixStream,
    request: &WireEnvelope<ControlRequest>,
    request_files: &[BorrowedFd<'_>],
) -> Result<ReceivedVmmMessage<ControlReply>, HttpControlError> {
    validate_message_fds(
        request_files,
        request.message.fd_roles(),
        request.message.socket_fd_roles(),
    )
    .map_err(HttpControlError::InvalidRequest)?;
    let body = canonical_request_body(request)?;
    if body.len() > MAX_FRAME_SIZE {
        return Err(HttpControlError::InvalidRequest(
            "JSON body exceeds the runtime-control payload bound".into(),
        ));
    }
    if request_files.len() > MAX_HTTP_CONTROL_FDS {
        return Err(HttpControlError::InvalidRequest(
            "request exceeds the single-message SCM_RIGHTS bound".into(),
        ));
    }
    send_request(stream, &body, request_files)?;
    let response = receive_response(stream)?;
    decode_control_response(response)
}

fn canonical_request_body(
    request: &WireEnvelope<ControlRequest>,
) -> Result<Vec<u8>, HttpControlError> {
    encode_message(request, "HTTP runtime-control request").map_err(HttpControlError::Protocol)
}

fn decode_control_response(
    response: HttpResponseWithFds,
) -> Result<ReceivedVmmMessage<ControlReply>, HttpControlError> {
    if response.status_code != 200 {
        if !response.files.is_empty() {
            return Err(HttpControlError::InvalidResponse(
                "non-success HTTP response carried SCM_RIGHTS descriptors".into(),
            ));
        }
        return Err(HttpControlError::HttpStatus(response.status_code));
    }
    let envelope: WireEnvelope<ControlReply> =
        decode_message(&response.body, "HTTP runtime-control response")
            .map_err(HttpControlError::Protocol)?;
    let borrowed = response.files.iter().map(AsFd::as_fd).collect::<Vec<_>>();
    validate_message_fds(
        &borrowed,
        envelope.message.fd_roles(),
        envelope.message.socket_fd_roles(),
    )
    .map_err(HttpControlError::InvalidResponse)?;
    Ok(ReceivedVmmMessage {
        envelope,
        fds: response.files,
    })
}

fn send_request(
    stream: &mut UnixStream,
    body: &[u8],
    request_files: &[BorrowedFd<'_>],
) -> Result<(), HttpControlError> {
    let header = format!(
        "PUT {RUNTIME_CONTROL_PATH} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nAccept: application/json\r\nContent-Length: {}\r\nConnection: keep-alive\r\n\r\n",
        body.len()
    );
    let slices = [IoSlice::new(header.as_bytes()), IoSlice::new(body)];
    let raw_fds = request_files
        .iter()
        .map(AsRawFd::as_raw_fd)
        .collect::<Vec<_>>();
    let control = if raw_fds.is_empty() {
        Vec::new()
    } else {
        vec![ControlMessage::ScmRights(&raw_fds)]
    };
    let sent = loop {
        match sendmsg::<SockaddrStorage>(stream.as_raw_fd(), &slices, &control, send_flags(), None)
        {
            Ok(sent) => break sent,
            Err(nix::errno::Errno::EINTR) => continue,
            Err(error) => return Err(HttpControlError::Io(errno_error(error))),
        }
    };
    if sent == 0 {
        return Err(HttpControlError::Io(io::Error::new(
            io::ErrorKind::WriteZero,
            "HTTP sendmsg made no progress",
        )));
    }
    write_iov_remainder(stream, header.as_bytes(), body, sent).map_err(HttpControlError::Io)?;
    stream.flush().map_err(HttpControlError::Io)
}

fn write_iov_remainder(
    stream: &mut UnixStream,
    header: &[u8],
    body: &[u8],
    sent: usize,
) -> io::Result<()> {
    let total = header
        .len()
        .checked_add(body.len())
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "HTTP request overflows"))?;
    if sent >= total {
        return Ok(());
    }
    if sent < header.len() {
        stream.write_all(&header[sent..])?;
        stream.write_all(body)
    } else {
        stream.write_all(&body[sent - header.len()..])
    }
}

fn receive_response(stream: &mut UnixStream) -> Result<HttpResponseWithFds, HttpControlError> {
    let mut first = vec![0_u8; FIRST_READ_BYTES];
    let mut slices = [IoSliceMut::new(&mut first)];
    let mut control_space = nix::cmsg_space!([RawFd; MAX_HTTP_CONTROL_FDS]);
    let message = loop {
        match recvmsg::<SockaddrStorage>(
            stream.as_raw_fd(),
            &mut slices,
            Some(&mut control_space),
            receive_flags(),
        ) {
            Ok(message) => break message,
            Err(nix::errno::Errno::EINTR) => continue,
            Err(error) => return Err(HttpControlError::Io(errno_error(error))),
        }
    };
    if message.bytes == 0 {
        return Err(HttpControlError::Io(io::Error::new(
            io::ErrorKind::UnexpectedEof,
            "runtime-control endpoint closed before the response",
        )));
    }
    let truncated = message
        .flags
        .intersects(MsgFlags::MSG_CTRUNC | MsgFlags::MSG_TRUNC);
    let mut files = Vec::new();
    for control in message
        .cmsgs()
        .map_err(errno_error)
        .map_err(HttpControlError::Io)?
    {
        match control {
            ControlMessageOwned::ScmRights(raw_fds) => {
                if files.len() + raw_fds.len() > MAX_HTTP_CONTROL_FDS {
                    close_raw_fds(raw_fds);
                    return Err(HttpControlError::InvalidResponse(
                        "response exceeds the single-message SCM_RIGHTS bound".into(),
                    ));
                }
                for raw_fd in raw_fds {
                    if let Err(error) = set_fd_cloexec(raw_fd) {
                        // SAFETY: this descriptor has not been transferred to OwnedFd.
                        unsafe { libc::close(raw_fd) };
                        return Err(HttpControlError::Io(error));
                    }
                    // SAFETY: SCM_RIGHTS returned a fresh descriptor owned here.
                    files.push(unsafe { OwnedFd::from_raw_fd(raw_fd) });
                }
            }
            _ => {
                return Err(HttpControlError::InvalidResponse(
                    "unexpected ancillary data in HTTP response".into(),
                ));
            }
        }
    }
    let bytes = message.bytes;
    if truncated {
        return Err(HttpControlError::InvalidResponse(
            "HTTP response or ancillary data was truncated".into(),
        ));
    }
    first.truncate(bytes);
    let (status_code, body) = read_response_bytes(stream, first)?;
    Ok(HttpResponseWithFds {
        status_code,
        body,
        files,
    })
}

fn read_response_bytes(
    stream: &mut UnixStream,
    mut bytes: Vec<u8>,
) -> Result<(u16, Vec<u8>), HttpControlError> {
    let header_end = loop {
        if let Some(offset) = find_subslice(&bytes, b"\r\n\r\n") {
            break offset + 4;
        }
        if bytes.len() >= MAX_HTTP_HEADER_BYTES {
            return Err(HttpControlError::InvalidResponse(
                "HTTP response headers exceed the configured bound".into(),
            ));
        }
        let mut chunk = [0_u8; 4096];
        let count = stream.read(&mut chunk).map_err(HttpControlError::Io)?;
        if count == 0 {
            return Err(HttpControlError::InvalidResponse(
                "HTTP response ended before its headers".into(),
            ));
        }
        bytes.extend_from_slice(&chunk[..count]);
    };
    if header_end > MAX_HTTP_HEADER_BYTES {
        return Err(HttpControlError::InvalidResponse(
            "HTTP response headers exceed the configured bound".into(),
        ));
    }
    let header = std::str::from_utf8(&bytes[..header_end]).map_err(|_| {
        HttpControlError::InvalidResponse("HTTP response headers are not UTF-8".into())
    })?;
    let mut lines = header[..header.len() - 4].split("\r\n");
    let status_line = lines.next().ok_or_else(|| {
        HttpControlError::InvalidResponse("HTTP response has no status line".into())
    })?;
    let mut status = status_line.split_ascii_whitespace();
    if status.next() != Some("HTTP/1.1") {
        return Err(HttpControlError::InvalidResponse(
            "HTTP response uses an unsupported protocol".into(),
        ));
    }
    let status_code = status
        .next()
        .ok_or_else(|| HttpControlError::InvalidResponse("HTTP status is missing".into()))?
        .parse::<u16>()
        .map_err(|_| HttpControlError::InvalidResponse("HTTP status is invalid".into()))?;
    let mut content_length = None;
    for line in lines {
        let Some((name, value)) = line.split_once(':') else {
            return Err(HttpControlError::InvalidResponse(
                "HTTP response contains a malformed header".into(),
            ));
        };
        if name.eq_ignore_ascii_case("transfer-encoding") {
            return Err(HttpControlError::InvalidResponse(
                "chunked runtime-control responses are unsupported".into(),
            ));
        }
        if name.eq_ignore_ascii_case("content-length") {
            if content_length.is_some() {
                return Err(HttpControlError::InvalidResponse(
                    "HTTP response repeats Content-Length".into(),
                ));
            }
            content_length = Some(value.trim().parse::<usize>().map_err(|_| {
                HttpControlError::InvalidResponse("HTTP Content-Length is invalid".into())
            })?);
        }
    }
    let content_length = content_length.ok_or_else(|| {
        HttpControlError::InvalidResponse("HTTP response is missing Content-Length".into())
    })?;
    if content_length > MAX_FRAME_SIZE {
        return Err(HttpControlError::InvalidResponse(
            "HTTP response body exceeds the runtime-control payload bound".into(),
        ));
    }
    let total = header_end.checked_add(content_length).ok_or_else(|| {
        HttpControlError::InvalidResponse("HTTP response length overflows".into())
    })?;
    while bytes.len() < total {
        let remaining = total - bytes.len();
        let mut chunk = vec![0_u8; remaining.min(64 * 1024)];
        let count = stream.read(&mut chunk).map_err(HttpControlError::Io)?;
        if count == 0 {
            return Err(HttpControlError::InvalidResponse(
                "HTTP response body ended before Content-Length".into(),
            ));
        }
        bytes.extend_from_slice(&chunk[..count]);
    }
    if bytes.len() != total {
        return Err(HttpControlError::InvalidResponse(
            "HTTP response contains bytes beyond Content-Length".into(),
        ));
    }
    Ok((status_code, bytes.split_off(header_end)))
}

fn find_subslice(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}

fn close_raw_fds(raw_fds: Vec<RawFd>) {
    for raw_fd in raw_fds {
        // SAFETY: these descriptors arrived through SCM_RIGHTS and are not owned elsewhere.
        unsafe { libc::close(raw_fd) };
    }
}

fn errno_error(error: nix::errno::Errno) -> io::Error {
    io::Error::from_raw_os_error(error as i32)
}

fn set_fd_cloexec(fd: RawFd) -> io::Result<()> {
    // SAFETY: fcntl only reads and updates flags on a live descriptor.
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

fn send_flags() -> MsgFlags {
    MsgFlags::MSG_NOSIGNAL
}

fn receive_flags() -> MsgFlags {
    MsgFlags::MSG_CMSG_CLOEXEC
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vm::protocol::{Channel, MessageHeader, VMM_PROTOCOL_VERSION};
    use crate::vm::runtime::protocol::{RuntimeSessionStopped, ShutdownRuntimeSession};

    const TEST_TIMEOUT: Duration = Duration::from_secs(5);

    struct ReadinessOnlyRuntime(Mutex<Vec<&'static str>>);

    #[async_trait]
    impl VMMClientInterface for ReadinessOnlyRuntime {
        fn pid(&self) -> u32 {
            0
        }
        fn control(&self) -> Arc<dyn VmControlApi> {
            unreachable!("readiness must not use runtime control")
        }
        fn has_exited(&self) -> Result<bool, VmmControllerError> {
            Ok(false)
        }
        async fn wait_for_guest_rocklet(
            &self,
            _timeout: Duration,
        ) -> Result<GuestReadinessStats, VmmControllerError> {
            self.0.lock().unwrap().push("rocklet");
            Err(VmmControllerError::Invalid(
                "Rocklet is intentionally absent".into(),
            ))
        }
        async fn wait_for_guest_plugger(
            &self,
            _timeout: Duration,
        ) -> Result<GuestReadinessStats, VmmControllerError> {
            self.0.lock().unwrap().push("plugger");
            Ok(GuestReadinessStats {
                attempts: 1,
                elapsed: Duration::ZERO,
                transport_event_wait: Duration::ZERO,
                transport_event_observed: false,
                probe_attempts: Vec::new(),
                proxy_attempts: Vec::new(),
            })
        }
        async fn cleanup(
            &self,
            _reason: &str,
            _timeout: Duration,
        ) -> Result<(), VmmControllerError> {
            Ok(())
        }
    }

    #[tokio::test]
    async fn explicit_plugger_readiness_never_falls_back_to_rocklet() {
        let runtime = ReadinessOnlyRuntime(Mutex::new(Vec::new()));
        selected_guest_readiness(
            &runtime,
            crate::vm::runtime::plugger::GuestReadinessMode::PluggerOnly,
            Duration::from_secs(1),
        )
        .await
        .unwrap();
        assert_eq!(*runtime.0.lock().unwrap(), vec!["plugger"]);
        assert!(selected_guest_readiness(
            &runtime,
            crate::vm::runtime::plugger::GuestReadinessMode::default(),
            Duration::from_secs(1)
        )
        .await
        .is_err());
        assert_eq!(*runtime.0.lock().unwrap(), vec!["plugger", "rocklet"]);
    }

    fn control_pair() -> (Arc<VmmControlClient>, UnixStream) {
        let (stream, server) = UnixStream::pair().unwrap();
        for socket in [&stream, &server] {
            socket.set_read_timeout(Some(TEST_TIMEOUT)).unwrap();
            socket.set_write_timeout(Some(TEST_TIMEOUT)).unwrap();
        }
        stream.set_nonblocking(true).unwrap();
        let fd = stream.as_raw_fd();
        // SAFETY: stream owns this live descriptor throughout construction.
        let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
        assert!(flags >= 0);
        // SAFETY: this updates flags on the same live, test-owned descriptor.
        assert_eq!(
            unsafe { libc::fcntl(fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC) },
            0
        );
        let client = Arc::new(VmmControlClient::new(stream).unwrap());
        // SAFETY: client now owns the unchanged descriptor.
        let status = unsafe { libc::fcntl(fd, libc::F_GETFL) };
        assert!(status >= 0);
        assert_eq!(status & libc::O_NONBLOCK, 0);
        // SAFETY: client still owns the live descriptor.
        let descriptor_flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
        assert!(descriptor_flags >= 0);
        assert_ne!(descriptor_flags & libc::FD_CLOEXEC, 0);
        (client, server)
    }

    fn shutdown(correlation_id: &str) -> WireEnvelope<ControlRequest> {
        WireEnvelope {
            header: MessageHeader {
                protocol_version: VMM_PROTOCOL_VERSION,
                channel: Channel::RuntimeControl,
                correlation_id: correlation_id.into(),
            },
            message: ControlRequest::ShutdownRuntimeSession(ShutdownRuntimeSession {
                reason: "client-regression".into(),
                deadline_unix_ms: 7,
            }),
        }
    }

    /// The fake endpoint consumes complete requests and replies independently
    /// of the production response parser; it never uses client transport code.
    fn read_request(server: &mut UnixStream) -> WireEnvelope<ControlRequest> {
        let mut header = Vec::new();
        while !header.ends_with(b"\r\n\r\n") {
            assert!(header.len() < 4096, "fake endpoint header bound");
            let mut byte = [0];
            server.read_exact(&mut byte).unwrap();
            header.push(byte[0]);
        }
        let header = String::from_utf8(header).unwrap();
        assert!(header.starts_with("PUT /api/v1/vm.rock-runtime-control HTTP/1.1\r\n"));
        let length = header
            .lines()
            .find_map(|line| line.strip_prefix("Content-Length: "))
            .unwrap()
            .parse::<usize>()
            .unwrap();
        let mut body = vec![0; length];
        server.read_exact(&mut body).unwrap();
        serde_json::from_slice(&body).unwrap()
    }

    fn write_reply(
        server: &mut UnixStream,
        correlation_id: &str,
        exit_reason: &str,
        descriptors: &[BorrowedFd<'_>],
    ) {
        let response = WireEnvelope {
            header: MessageHeader {
                protocol_version: VMM_PROTOCOL_VERSION,
                channel: Channel::RuntimeControl,
                correlation_id: correlation_id.into(),
            },
            message: ControlReply::RuntimeSessionStopped(RuntimeSessionStopped {
                exit_reason: Some(exit_reason.into()),
            }),
        };
        let body = serde_json::to_vec(&response).unwrap();
        let mut bytes = format!(
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: keep-alive\r\n\r\n",
            body.len()
        )
        .into_bytes();
        bytes.extend_from_slice(&body);
        let raw = descriptors
            .iter()
            .map(AsRawFd::as_raw_fd)
            .collect::<Vec<_>>();
        let ancillary = if raw.is_empty() {
            Vec::new()
        } else {
            vec![ControlMessage::ScmRights(&raw)]
        };
        let sent = sendmsg::<SockaddrStorage>(
            server.as_raw_fd(),
            &[IoSlice::new(&bytes)],
            &ancillary,
            MsgFlags::MSG_NOSIGNAL,
            None,
        )
        .unwrap();
        assert!(sent > 0);
        server.write_all(&bytes[sent..]).unwrap();
        server.flush().unwrap();
    }

    async fn join_endpoint<T: Send + 'static>(server: std::thread::JoinHandle<T>) -> T {
        tokio::time::timeout(
            TEST_TIMEOUT,
            tokio::task::spawn_blocking(move || server.join().unwrap()),
        )
        .await
        .unwrap()
        .unwrap()
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn control_waiter_cancellation_drains_response_and_preserves_one_shot_submission() {
        let (client, mut server) = control_pair();
        let (seen_tx, seen_rx) = tokio::sync::oneshot::channel();
        let (reply_tx, reply_rx) = std::sync::mpsc::channel();
        let endpoint = std::thread::spawn(move || {
            let first = read_request(&mut server);
            assert_eq!(first, shutdown("cancelled-waiter"));
            seen_tx.send(()).unwrap();
            reply_rx.recv_timeout(TEST_TIMEOUT).unwrap();
            write_reply(&mut server, "cancelled-waiter", "first-response", &[]);
            let second = read_request(&mut server);
            assert_eq!(second, shutdown("next-request"));
            write_reply(&mut server, "next-request", "second-response", &[]);
            vec![first.header.correlation_id, second.header.correlation_id]
        });
        let first = tokio::spawn({
            let client = Arc::clone(&client);
            async move {
                client
                    .request(shutdown("cancelled-waiter"), Vec::new())
                    .await
            }
        });
        tokio::time::timeout(TEST_TIMEOUT, seen_rx)
            .await
            .unwrap()
            .unwrap();
        first.abort();
        assert!(first.await.unwrap_err().is_cancelled());
        let duplicate = tokio::time::timeout(
            Duration::from_secs(1),
            client.request(shutdown("cancelled-waiter"), Vec::new()),
        )
        .await
        .unwrap();
        assert!(matches!(duplicate, Err(VmmClientError::AlreadySubmitted {
            operation: "ShutdownRuntimeSession", correlation_id,
        }) if correlation_id == "cancelled-waiter"));
        let second = tokio::spawn({
            let client = Arc::clone(&client);
            async move { client.request(shutdown("next-request"), Vec::new()).await }
        });
        reply_tx.send(()).unwrap();
        let response = tokio::time::timeout(TEST_TIMEOUT, second)
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        assert_eq!(response.envelope.header.correlation_id, "next-request");
        assert!(matches!(response.envelope.message,
            ControlReply::RuntimeSessionStopped(RuntimeSessionStopped { exit_reason: Some(reason) })
            if reason == "second-response"));
        assert!(response.fds.is_empty());
        assert!(matches!(
            client
                .request(shutdown("cancelled-waiter"), Vec::new())
                .await,
            Err(VmmClientError::AlreadySubmitted { .. })
        ));
        assert_eq!(
            join_endpoint(endpoint).await,
            vec!["cancelled-waiter".to_owned(), "next-request".to_owned()]
        );
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn control_response_rejects_wrong_correlation_and_closes_unexpected_fds() {
        let (client, mut server) = control_pair();
        let endpoint = std::thread::spawn(move || {
            assert_eq!(read_request(&mut server), shutdown("expected"));
            write_reply(&mut server, "wrong-response", "mismatched", &[]);
        });
        let result = tokio::time::timeout(
            TEST_TIMEOUT,
            client.request(shutdown("expected"), Vec::new()),
        )
        .await
        .unwrap();
        assert!(matches!(result, Err(VmmClientError::Transport(message))
            if message == "HTTP response correlation does not match its request"));
        assert!(matches!(
            client.request(shutdown("expected"), Vec::new()).await,
            Err(VmmClientError::AlreadySubmitted { .. })
        ));
        join_endpoint(endpoint).await;

        let (client, mut server) = control_pair();
        let (transferred, mut peer) = UnixStream::pair().unwrap();
        peer.set_read_timeout(Some(TEST_TIMEOUT)).unwrap();
        let endpoint = std::thread::spawn(move || {
            assert_eq!(read_request(&mut server), shutdown("unexpected-fd"));
            write_reply(
                &mut server,
                "unexpected-fd",
                "invalid-fd-role",
                &[transferred.as_fd()],
            );
            drop(transferred);
        });
        let result = tokio::time::timeout(
            TEST_TIMEOUT,
            client.request(shutdown("unexpected-fd"), Vec::new()),
        )
        .await
        .unwrap();
        assert!(matches!(result, Err(VmmClientError::Transport(message))
            if message.contains("SCM_RIGHTS count does not match fd_roles")));
        join_endpoint(endpoint).await;
        // The sender's descriptor is gone; EOF proves the rejected response's
        // received OwnedFd was also released rather than leaked by the client.
        let closed = tokio::task::spawn_blocking(move || peer.read(&mut [0]))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(closed, 0);
    }
}
