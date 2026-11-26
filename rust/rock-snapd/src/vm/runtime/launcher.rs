//! Trusted Cloud Hypervisor process launcher for the Rust snapd runtime.

use std::collections::BTreeMap;
use std::ffi::OsString;
use std::fmt;
use std::fs::File;
use std::io::{self, Read, Write};
use std::net::{Ipv4Addr, Shutdown, SocketAddr, TcpListener, TcpStream};
use std::os::fd::{AsRawFd, OwnedFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use nix::errno::Errno;
use nix::sys::socket::{
    connect, getsockopt, socket, sockopt, AddressFamily, SockFlag, SockType, UnixAddr,
};
use sha2::{Digest, Sha256};
use thiserror::Error;

use crate::vm::protocol::{Channel, MessageHeader, WireEnvelope};
use crate::vm::runtime::client::{VmmClientError, VmmControlClient};
use crate::vm::runtime::network::{
    GuestNetworkLease, GUEST_TAP_FD, NETWORK_LEASE_FD, NET_ARGUMENT,
};
use crate::vm::runtime::protocol::{
    ControlReply, ControlRequest, RuntimeSessionStopped, ShutdownRuntimeSession,
};
use crate::RuntimeIdentity;

const API_SOCKET_NAME: &str = "api.sock";
const REMOTE_MISSING_SOCKET_NAME: &str = "remote-missing.sock";
const ROCKLET_VSOCK_SOCKET_NAME: &str = "rocklet.vsock";
const VMM_EVENT_MONITOR_FILE_NAME: &str = "vmm-events.json";
pub(crate) const ROCKLET_ARTIFACT_FD: libc::c_int = 201;
const SOCKET_CONNECT_TIMEOUT: Duration = Duration::from_secs(30);
const GUEST_CONNECT_TIMEOUT: Duration = Duration::from_secs(2);
// Cold restore can fault while servicing both CONNECT and /is_alive. Short,
// frequent probes abandon vsock connections faster than the guest can reap them.
const ROCKLET_READINESS_PROBE_TIMEOUT: Duration = Duration::from_secs(2);
const ROCKLET_READINESS_RETRY_INTERVAL: Duration = Duration::from_millis(100);
const ROCKLET_READINESS_MAX_RETRY_INTERVAL: Duration = Duration::from_secs(1);
const VSOCK_TRANSPORT_EVENT_GRACE: Duration = Duration::from_millis(20);
const VSOCK_HANDSHAKE_MAX_BYTES: usize = 128;
pub(crate) const ROCK_UFFD_LEGACY_ENV: &str = "ROCK_UFFD_LEGACY";
const SAFE_ENVIRONMENT_KEYS: &[&str] = &[
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "RUST_BACKTRACE",
    "RUST_LOG",
    "ROCK_PERF_TRACE_STAGES",
    "ROCK_UFFD_TRACE_FAULTS",
    "ROCK_UFFD_WORKERS",
    "TZ",
];

#[derive(Debug, Error)]
pub enum VmmLaunchError {
    #[error("Invalid VMM launch configuration: {0}")]
    Invalid(String),
    #[error("VMM process launch failed")]
    Spawn(#[source] io::Error),
    #[error("VMM runtime-control operation failed: {0}")]
    Control(String),
    #[error("VMM process lock is poisoned")]
    LockPoisoned,
    #[error("VMM did not exit before its cleanup deadline")]
    ExitTimeout,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RockletArtifactIdentity {
    pub identity: String,
    pub version: String,
    pub sha256: String,
    pub logical_bytes: u64,
}

#[derive(Clone)]
pub struct GuestRockletLaunch {
    pub artifact: Arc<File>,
    pub identity: RockletArtifactIdentity,
    pub guest_port: u32,
    pub vsock_cid: u32,
    pub proxy_port_start: u16,
    pub proxy_port_end: u16,
}

impl fmt::Debug for GuestRockletLaunch {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("GuestRockletLaunch")
            .field("identity", &self.identity)
            .field("guest_port", &self.guest_port)
            .field("vsock_cid", &self.vsock_cid)
            .field(
                "proxy_port_range",
                &(self.proxy_port_start..=self.proxy_port_end),
            )
            .finish()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GuestEndpointInfo {
    pub host_port: u16,
    pub guest_port: u32,
    pub vsock_cid: u32,
    pub artifact: RockletArtifactIdentity,
}

impl From<VmmClientError> for VmmLaunchError {
    fn from(error: VmmClientError) -> Self {
        Self::Control(error.to_string())
    }
}

#[derive(Clone)]
pub struct VmmLaunchSpec {
    pub command: Vec<OsString>,
    pub runtime: RuntimeIdentity,
    pub launch_identity: String,
    pub fault_around_pages: u32,
    pub cache_root: PathBuf,
    pub private_root: PathBuf,
    pub environment: BTreeMap<String, String>,
    pub cwd: Option<PathBuf>,
    pub guest_network: bool,
    pub guest_readiness: crate::vm::runtime::plugger::GuestReadinessMode,
    pub guest_rocklet: Option<GuestRockletLaunch>,
}

impl fmt::Debug for VmmLaunchSpec {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("VmmLaunchSpec")
            .field("command", &self.command)
            .field("runtime", &self.runtime)
            .field("launch_identity", &self.launch_identity)
            .field("fault_around_pages", &self.fault_around_pages)
            .field("cache_root", &self.cache_root)
            .field("private_root", &self.private_root)
            .field("environment", &self.environment)
            .field("cwd", &self.cwd)
            .field("guest_rocklet", &self.guest_rocklet)
            .field("guest_network", &self.guest_network)
            .field("guest_readiness", &self.guest_readiness)
            .finish()
    }
}

impl VmmLaunchSpec {
    pub fn validate(&self) -> Result<(), VmmLaunchError> {
        if self.guest_readiness == crate::vm::runtime::plugger::GuestReadinessMode::PluggerOnly
            && self.guest_rocklet.is_none()
        {
            return invalid("plugger-only readiness requires a guest payload and VSOCK device");
        }
        if self.command.is_empty() {
            return invalid("VMM command must not be empty");
        }
        if self.launch_identity.is_empty()
            || self.launch_identity.contains(',')
            || self.launch_identity.contains('\0')
        {
            return invalid("Launch identity is empty or contains a delimiter");
        }
        if self.launch_identity.len() > 256 {
            return invalid("Launch identity exceeds its protocol bound");
        }
        if self.fault_around_pages == 0
            || u64::from(self.fault_around_pages) * 4096 > 16 * 1024 * 1024
        {
            return invalid("fault_around_pages must fit the RemoteLoad batch bound");
        }
        for (label, path) in [
            ("cache root", self.cache_root.as_path()),
            ("private root", self.private_root.as_path()),
        ] {
            let value = path
                .to_str()
                .ok_or_else(|| VmmLaunchError::Invalid(format!("{label} is not valid UTF-8")))?;
            if !path.is_absolute() || value.contains(',') || value.contains('\0') {
                return invalid(&format!("{label} is not an absolute safe path"));
            }
        }
        for (name, value) in &self.environment {
            if !SAFE_ENVIRONMENT_KEYS.contains(&name.as_str()) && name != ROCK_UFFD_LEGACY_ENV {
                return invalid(&format!("Environment key {name:?} is not allowlisted"));
            }
            if value.contains('\0') {
                return invalid(&format!("Environment value for {name:?} contains NUL"));
            }
            if name == ROCK_UFFD_LEGACY_ENV && !matches!(value.as_str(), "off" | "on") {
                return invalid("uffd_legacy must be off or on");
            }
        }
        if let Some(cwd) = &self.cwd {
            if !cwd.is_absolute() {
                return invalid("VMM working directory must be absolute");
            }
        }
        if let Some(rocklet) = &self.guest_rocklet {
            validate_guest_rocklet(rocklet, &self.command)?;
        }
        if self.guest_network
            && !self
                .command
                .windows(2)
                .any(|args| args[0] == "--net" && args[1] == NET_ARGUMENT)
        {
            return invalid("Guest network must use the fixed network lease descriptor");
        }
        Ok(())
    }

    pub fn config_sha256(&self) -> String {
        let mut digest = Sha256::new();
        if self.guest_readiness == crate::vm::runtime::plugger::GuestReadinessMode::PluggerOnly {
            update_hash_component(&mut digest, b"guest-readiness:plugger-only");
        }
        for value in &self.command {
            update_hash_component(&mut digest, value.as_os_str().as_bytes());
        }
        let fault_around_pages = self.fault_around_pages.to_string();
        for value in [
            self.runtime.sandbox_id.as_bytes(),
            self.launch_identity.as_bytes(),
            fault_around_pages.as_bytes(),
            self.cache_root.as_os_str().as_bytes(),
            self.private_root.as_os_str().as_bytes(),
            self.uffd_legacy().as_bytes(),
        ] {
            update_hash_component(&mut digest, value);
        }
        if let Some(rocklet) = &self.guest_rocklet {
            for value in [
                rocklet.identity.identity.as_bytes(),
                rocklet.identity.version.as_bytes(),
                rocklet.identity.sha256.as_bytes(),
            ] {
                update_hash_component(&mut digest, value);
            }
            for value in [
                rocklet.identity.logical_bytes.to_string(),
                rocklet.guest_port.to_string(),
                rocklet.vsock_cid.to_string(),
                rocklet.proxy_port_start.to_string(),
                rocklet.proxy_port_end.to_string(),
            ] {
                update_hash_component(&mut digest, value.as_bytes());
            }
        }
        format!("{:x}", digest.finalize())
    }

    fn uffd_legacy(&self) -> &str {
        self.environment
            .get(ROCK_UFFD_LEGACY_ENV)
            .map(String::as_str)
            .unwrap_or("off")
    }
}

fn validate_guest_rocklet(
    rocklet: &GuestRockletLaunch,
    command: &[OsString],
) -> Result<(), VmmLaunchError> {
    let metadata = rocklet.artifact.metadata().map_err(VmmLaunchError::Spawn)?;
    if !metadata.is_file()
        || metadata.len() != rocklet.identity.logical_bytes
        || metadata.permissions().mode() & 0o222 != 0
    {
        return invalid("Rocklet artifact is not an immutable regular file of the declared size");
    }
    let descriptor_flags = unsafe { libc::fcntl(rocklet.artifact.as_raw_fd(), libc::F_GETFL) };
    if descriptor_flags < 0 {
        return Err(VmmLaunchError::Spawn(io::Error::last_os_error()));
    }
    if descriptor_flags & libc::O_ACCMODE != libc::O_RDONLY {
        return invalid("Rocklet artifact descriptor is not read-only");
    }
    if !valid_prefixed_sha256(&rocklet.identity.identity)
        || rocklet.identity.version.is_empty()
        || rocklet.identity.version.len() > 128
        || !rocklet
            .identity
            .version
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-' | b'+'))
        || rocklet.identity.sha256.len() != 64
        || !rocklet
            .identity
            .sha256
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        || rocklet.guest_port != 22_555
        || [u32::MAX, 0, 1, 2].contains(&rocklet.vsock_cid)
        || rocklet.proxy_port_start == 0
        || rocklet.proxy_port_start > rocklet.proxy_port_end
        || u32::from(rocklet.proxy_port_end) - u32::from(rocklet.proxy_port_start) + 1 > 16_384
    {
        return invalid("Rocklet artifact identity or guest endpoint is invalid");
    }
    let expected_disk =
        format!("id=rocklet,path=/proc/self/fd/{ROCKLET_ARTIFACT_FD},image_type=raw,readonly=on");
    if !command
        .iter()
        .any(|value| value.as_os_str() == std::ffi::OsStr::new(&expected_disk))
    {
        return invalid("VMM command does not attach the verified Rocklet artifact read-only");
    }
    Ok(())
}

fn valid_prefixed_sha256(value: &str) -> bool {
    value.strip_prefix("sha256:").is_some_and(|digest| {
        digest.len() == 64
            && digest
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    })
}

struct GuestEndpointLease {
    info: GuestEndpointInfo,
    probe_address: SocketAddr,
    stop: Arc<AtomicBool>,
    connections: Arc<Mutex<GuestConnections>>,
    proxy_attempts: Arc<Mutex<Vec<GuestProxyAttemptStats>>>,
    listener_thread: Option<JoinHandle<()>>,
    vsock_path: PathBuf,
}

#[derive(Default)]
struct GuestConnections {
    stopping: bool,
    active: BTreeMap<u64, GuestConnection>,
}

struct GuestConnection {
    tcp: TcpStream,
    vsock: Option<UnixStream>,
}

impl GuestConnections {
    fn register_tcp(&mut self, connection_id: u64, tcp: &TcpStream) -> io::Result<bool> {
        if self.stopping {
            return Ok(false);
        }
        self.active.insert(
            connection_id,
            GuestConnection {
                tcp: tcp.try_clone()?,
                vsock: None,
            },
        );
        Ok(true)
    }

    fn register_vsock(&mut self, connection_id: u64, vsock: &UnixStream) -> io::Result<bool> {
        if self.stopping {
            return Ok(false);
        }
        let Some(connection) = self.active.get_mut(&connection_id) else {
            return Ok(false);
        };
        connection.vsock = Some(vsock.try_clone()?);
        Ok(true)
    }

    fn begin_shutdown(&mut self) {
        self.stopping = true;
        for connection in self.active.values() {
            let _ = connection.tcp.shutdown(Shutdown::Both);
            if let Some(vsock) = &connection.vsock {
                let _ = vsock.shutdown(Shutdown::Both);
            }
        }
    }
}

struct GuestConnectionGuard {
    connection_id: u64,
    connections: Arc<Mutex<GuestConnections>>,
}

impl Drop for GuestConnectionGuard {
    fn drop(&mut self) {
        recover_lock(&self.connections)
            .active
            .remove(&self.connection_id);
    }
}

impl GuestEndpointLease {
    fn bind(rocklet: &GuestRockletLaunch, vsock_path: PathBuf) -> Result<Self, VmmLaunchError> {
        Self::bind_inner(rocklet, vsock_path)
    }

    fn bind_inner(
        rocklet: &GuestRockletLaunch,
        vsock_path: PathBuf,
    ) -> Result<Self, VmmLaunchError> {
        let listener = bind_guest_listener(rocklet)?;
        listener
            .set_nonblocking(true)
            .map_err(VmmLaunchError::Spawn)?;
        let host_port = listener.local_addr().map_err(VmmLaunchError::Spawn)?.port();
        let probe_address = SocketAddr::from((Ipv4Addr::LOCALHOST, host_port));
        let stop = Arc::new(AtomicBool::new(false));
        let transport_ready = Arc::new(AtomicBool::new(false));
        let proxy_attempts = Arc::new(Mutex::new(Vec::new()));
        let connections = Arc::new(Mutex::new(GuestConnections::default()));
        let thread_stop = Arc::clone(&stop);
        let thread_transport_ready = Arc::clone(&transport_ready);
        let thread_proxy_attempts = Arc::clone(&proxy_attempts);
        let thread_connections = Arc::clone(&connections);
        let guest_port = rocklet.guest_port;
        let thread_vsock_path = vsock_path.clone();
        let listener_thread = thread::Builder::new()
            .name(format!("rocklet-listener-{host_port}"))
            .spawn(move || {
                let next_connection = AtomicU64::new(1);
                let mut workers = Vec::new();
                while !thread_stop.load(Ordering::Acquire) {
                    reap_guest_workers(&mut workers);
                    match listener.accept() {
                        Ok((stream, _)) => {
                            if thread_stop.load(Ordering::Acquire) {
                                let _ = stream.shutdown(Shutdown::Both);
                                break;
                            }
                            let connection_id = next_connection.fetch_add(1, Ordering::Relaxed);
                            let registered = recover_lock(&thread_connections)
                                .register_tcp(connection_id, &stream)
                                .unwrap_or(false);
                            if !registered {
                                let _ = stream.shutdown(Shutdown::Both);
                                break;
                            }
                            let path = thread_vsock_path.clone();
                            let active = Arc::clone(&thread_connections);
                            let worker_stop = Arc::clone(&thread_stop);
                            let worker_transport_ready = Arc::clone(&thread_transport_ready);
                            let worker_proxy_attempts = Arc::clone(&thread_proxy_attempts);
                            let guard = GuestConnectionGuard {
                                connection_id,
                                connections: Arc::clone(&active),
                            };

                            let worker = thread::Builder::new()
                                .name(format!("rocklet-forward-{host_port}-{connection_id}"))
                                .spawn(move || {
                                    let _guard = guard;
                                    let mut observation = GuestProxyAttemptStats::default();
                                    let result = forward_guest_connection(
                                        connection_id,
                                        stream,
                                        &path,
                                        guest_port,
                                        &worker_stop,
                                        &worker_transport_ready,
                                        &active,
                                        &mut observation,
                                    );
                                    if result.is_ok() {
                                        observation.error_code = "ok";
                                    } else if observation.error_code.is_empty() {
                                        observation.error_code = "proxy_relay_io";
                                    }
                                    recover_lock(&worker_proxy_attempts).push(observation);
                                });
                            if let Ok(worker) = worker {
                                workers.push(worker);
                            }
                        }
                        Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                            thread::sleep(Duration::from_millis(10));
                        }
                        Err(_) => break,
                    }
                }
                recover_lock(&thread_connections).begin_shutdown();
                for worker in workers {
                    let _ = worker.join();
                }
            })
            .map_err(VmmLaunchError::Spawn)?;
        Ok(Self {
            info: GuestEndpointInfo {
                host_port,
                guest_port: rocklet.guest_port,
                vsock_cid: rocklet.vsock_cid,
                artifact: rocklet.identity.clone(),
            },
            probe_address,
            stop,
            connections,
            proxy_attempts,
            listener_thread: Some(listener_thread),
            vsock_path,
        })
    }

    fn info(&self) -> GuestEndpointInfo {
        self.info.clone()
    }
}

fn bind_guest_listener(rocklet: &GuestRockletLaunch) -> Result<TcpListener, VmmLaunchError> {
    let width = u32::from(rocklet.proxy_port_end) - u32::from(rocklet.proxy_port_start) + 1;
    let start_offset = rocklet.vsock_cid % width;
    let mut last_in_use = None;
    for offset in 0..width {
        let port = u32::from(rocklet.proxy_port_start) + (start_offset + offset) % width;
        match TcpListener::bind(SocketAddr::from((Ipv4Addr::UNSPECIFIED, port as u16))) {
            Ok(listener) => return Ok(listener),
            Err(error) if error.kind() == io::ErrorKind::AddrInUse => last_in_use = Some(error),
            Err(error) => return Err(VmmLaunchError::Spawn(error)),
        }
    }
    Err(VmmLaunchError::Spawn(last_in_use.unwrap_or_else(|| {
        io::Error::new(
            io::ErrorKind::AddrInUse,
            "guest Rocklet proxy port range is exhausted",
        )
    })))
}

fn wait_for_guest_rocklet(
    probe_address: SocketAddr,
    timeout: Duration,
) -> Result<GuestReadinessStats, VmmLaunchError> {
    let started = Instant::now();
    let deadline = Instant::now() + timeout;
    let mut last_error = None;
    let mut attempts = 0_u64;
    let mut probe_attempts = Vec::new();
    let mut retry_interval = ROCKLET_READINESS_RETRY_INTERVAL;
    while Instant::now() < deadline {
        attempts = attempts.saturating_add(1);
        let probe_deadline = deadline.min(Instant::now() + ROCKLET_READINESS_PROBE_TIMEOUT);
        let (result, observation) = probe_guest_rocklet_observed(probe_address, probe_deadline);
        probe_attempts.push(observation);
        match result {
            Ok(()) => {
                return Ok(GuestReadinessStats {
                    attempts,
                    elapsed: started.elapsed(),
                    transport_event_wait: Duration::ZERO,
                    transport_event_observed: false,
                    probe_attempts,
                    proxy_attempts: Vec::new(),
                });
            }
            Err(error) => last_error = Some(error),
        }
        thread::sleep(retry_interval.min(deadline.saturating_duration_since(Instant::now())));
        retry_interval = next_readiness_retry_interval(retry_interval);
    }
    Err(VmmLaunchError::Control(format!(
        "guest Rocklet did not become ready after {attempts} attempts in {:?}: {}",
        started.elapsed(),
        last_error
            .map(|error| error.to_string())
            .unwrap_or_else(|| "readiness deadline expired".into())
    )))
}

fn next_readiness_retry_interval(interval: Duration) -> Duration {
    interval
        .saturating_mul(2)
        .min(ROCKLET_READINESS_MAX_RETRY_INTERVAL)
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GuestReadinessStats {
    pub attempts: u64,
    pub elapsed: Duration,
    pub transport_event_wait: Duration,
    pub transport_event_observed: bool,
    pub probe_attempts: Vec<GuestProbeAttemptStats>,
    pub proxy_attempts: Vec<GuestProxyAttemptStats>,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct GuestProbeAttemptStats {
    pub tcp_connect: Option<Duration>,
    pub http_first_byte: Option<Duration>,
    pub http_full_response: Option<Duration>,
    pub error_code: &'static str,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct GuestProxyAttemptStats {
    pub vsock_connect: Option<Duration>,
    pub connect_handshake: Option<Duration>,
    pub error_code: &'static str,
}

fn wait_for_vsock_transport_ready_event(path: &Path) -> bool {
    let deadline = Instant::now() + VSOCK_TRANSPORT_EVENT_GRACE;
    loop {
        if std::fs::read_to_string(path).is_ok_and(|events| {
            events.split("\n\n").any(|event| {
                event.contains("\"source\": \"rock_vsock\"")
                    && event.contains("\"event\": \"transport_ready\"")
            })
        }) {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        thread::sleep(Duration::from_millis(2));
    }
}

impl Drop for GuestEndpointLease {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        recover_lock(&self.connections).begin_shutdown();
        if let Some(listener) = self.listener_thread.take() {
            let _ = listener.join();
        }
        let _ = std::fs::remove_file(&self.vsock_path);
    }
}

fn recover_lock<T>(mutex: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn reap_guest_workers(workers: &mut Vec<JoinHandle<()>>) {
    let mut index = 0;
    while index < workers.len() {
        if workers[index].is_finished() {
            let worker = workers.swap_remove(index);
            let _ = worker.join();
        } else {
            index += 1;
        }
    }
}

fn forward_guest_connection(
    connection_id: u64,
    tcp: TcpStream,
    vsock_path: &Path,
    guest_port: u32,
    stop: &AtomicBool,
    transport_ready: &AtomicBool,
    active: &Mutex<GuestConnections>,
    observation: &mut GuestProxyAttemptStats,
) -> io::Result<()> {
    if stop.load(Ordering::Acquire) {
        return Err(connection_stopped());
    }
    tcp.set_nodelay(true)?;
    let bootstrap = !transport_ready.load(Ordering::Acquire);
    let connect_timeout = if bootstrap {
        ROCKLET_READINESS_PROBE_TIMEOUT
    } else {
        GUEST_CONNECT_TIMEOUT
    };
    let vsock_started = Instant::now();
    let mut vsock = match connect_guest_vsock(vsock_path, stop, connect_timeout) {
        Ok(vsock) => vsock,
        Err(error) => {
            observation.vsock_connect = Some(vsock_started.elapsed());
            observation.error_code = classify_io_error("vsock_connect", &error);
            return Err(error);
        }
    };
    observation.vsock_connect = Some(vsock_started.elapsed());
    if stop.load(Ordering::Acquire)
        || !recover_lock(active).register_vsock(connection_id, &vsock)?
    {
        let _ = tcp.shutdown(Shutdown::Both);
        let _ = vsock.shutdown(Shutdown::Both);
        return Err(connection_stopped());
    }
    let handshake_started = Instant::now();
    let handshake_result = (|| {
        vsock.set_read_timeout(Some(connect_timeout))?;
        vsock.set_write_timeout(Some(connect_timeout))?;
        writeln!(vsock, "CONNECT {guest_port}")?;
        vsock.flush()?;
        read_bounded_line(&mut vsock, VSOCK_HANDSHAKE_MAX_BYTES)
    })();
    observation.connect_handshake = Some(handshake_started.elapsed());
    let response = match handshake_result {
        Ok(response) => response,
        Err(error) => {
            observation.error_code = classify_io_error("connect_handshake", &error);
            return Err(error);
        }
    };
    if !response.starts_with("OK ") {
        observation.error_code = "connect_handshake_rejected";
        return Err(io::Error::new(
            io::ErrorKind::ConnectionRefused,
            format!("virtio-vsock rejected guest port: {response:?}"),
        ));
    }
    transport_ready.store(true, Ordering::Release);

    if stop.load(Ordering::Acquire) {
        return Err(connection_stopped());
    }
    vsock.set_read_timeout(None)?;
    vsock.set_write_timeout(None)?;

    let mut tcp_to_guest = tcp.try_clone()?;
    let mut guest_writer = vsock.try_clone()?;
    let reverse = thread::spawn(move || {
        let mut guest_reader = vsock;
        let mut tcp_writer = tcp;
        let result = io::copy(&mut guest_reader, &mut tcp_writer);
        let _ = tcp_writer.shutdown(Shutdown::Write);
        result
    });
    let forward = io::copy(&mut tcp_to_guest, &mut guest_writer);
    let _ = guest_writer.shutdown(Shutdown::Write);
    let reverse = reverse
        .join()
        .map_err(|_| io::Error::other("guest endpoint reverse relay panicked"))?;
    forward.and(reverse).map(|_| ())
}

fn connect_guest_vsock(
    path: &Path,
    stop: &AtomicBool,
    timeout: Duration,
) -> io::Result<UnixStream> {
    if stop.load(Ordering::Acquire) {
        return Err(connection_stopped());
    }
    let address = UnixAddr::new(path).map_err(errno_to_io)?;
    let socket_flags = SockFlag::SOCK_CLOEXEC | SockFlag::SOCK_NONBLOCK;
    let descriptor: OwnedFd =
        socket(AddressFamily::Unix, SockType::Stream, socket_flags, None).map_err(errno_to_io)?;
    set_guest_socket_flags(&descriptor)?;
    match connect(descriptor.as_raw_fd(), &address) {
        Ok(()) => {}
        Err(Errno::EINPROGRESS | Errno::EALREADY | Errno::EAGAIN | Errno::EINTR) => {
            let deadline = Instant::now() + timeout;
            loop {
                if stop.load(Ordering::Acquire) {
                    return Err(connection_stopped());
                }
                if Instant::now() >= deadline {
                    return Err(io::Error::new(
                        io::ErrorKind::TimedOut,
                        "virtio-vsock connect exceeded its deadline",
                    ));
                }
                let mut poll_fd = libc::pollfd {
                    fd: descriptor.as_raw_fd(),
                    events: libc::POLLOUT,
                    revents: 0,
                };
                // SAFETY: poll_fd points to one initialized pollfd for this call.
                let result = unsafe { libc::poll(&mut poll_fd, 1, 10) };
                if result < 0 {
                    let error = io::Error::last_os_error();
                    if error.kind() == io::ErrorKind::Interrupted {
                        continue;
                    }
                    return Err(error);
                }
                if result == 0 {
                    continue;
                }
                if poll_fd.revents & libc::POLLNVAL != 0 {
                    return Err(io::Error::from_raw_os_error(libc::EBADF));
                }
                if poll_fd.revents & (libc::POLLOUT | libc::POLLERR | libc::POLLHUP) == 0 {
                    continue;
                }
                let socket_error =
                    getsockopt(&descriptor, sockopt::SocketError).map_err(errno_to_io)?;
                if socket_error != 0 {
                    return Err(io::Error::from_raw_os_error(socket_error));
                }
                break;
            }
        }
        Err(error) => return Err(errno_to_io(error)),
    }
    if stop.load(Ordering::Acquire) {
        return Err(connection_stopped());
    }
    let stream = UnixStream::from(descriptor);
    stream.set_nonblocking(false)?;
    Ok(stream)
}

fn set_guest_socket_flags(descriptor: &OwnedFd) -> io::Result<()> {
    // SOCK_CLOEXEC/SOCK_NONBLOCK are not exposed by nix on every supported
    // Unix target, so set the equivalent descriptor flags portably.
    // SAFETY: descriptor is an owned live socket and fcntl only reads/updates
    // its descriptor-local status flags.
    let status = unsafe { libc::fcntl(descriptor.as_raw_fd(), libc::F_GETFL) };
    if status < 0
        || unsafe {
            libc::fcntl(
                descriptor.as_raw_fd(),
                libc::F_SETFL,
                status | libc::O_NONBLOCK,
            )
        } < 0
    {
        return Err(io::Error::last_os_error());
    }
    let descriptor_flags = unsafe { libc::fcntl(descriptor.as_raw_fd(), libc::F_GETFD) };
    if descriptor_flags < 0
        || unsafe {
            libc::fcntl(
                descriptor.as_raw_fd(),
                libc::F_SETFD,
                descriptor_flags | libc::FD_CLOEXEC,
            )
        } < 0
    {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

fn errno_to_io(error: Errno) -> io::Error {
    io::Error::from_raw_os_error(error as i32)
}

fn connection_stopped() -> io::Error {
    io::Error::new(io::ErrorKind::Interrupted, "guest endpoint is stopping")
}

fn classify_io_error(stage: &'static str, error: &io::Error) -> &'static str {
    match (stage, error.kind()) {
        ("tcp_connect", io::ErrorKind::TimedOut) => "tcp_connect_timeout",
        ("tcp_connect", io::ErrorKind::ConnectionRefused) => "tcp_connect_refused",
        ("tcp_connect", _) => "tcp_connect_io",
        ("vsock_connect", io::ErrorKind::TimedOut) => "vsock_connect_timeout",
        ("vsock_connect", io::ErrorKind::ConnectionRefused) => "vsock_connect_refused",
        ("vsock_connect", _) => "vsock_connect_io",
        ("connect_handshake", io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock) => {
            "connect_handshake_timeout"
        }
        ("connect_handshake", _) => "connect_handshake_io",
        ("http_write", io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock) => "http_write_timeout",
        ("http_write", _) => "http_write_io",
        ("http_first_byte", io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock) => {
            "http_first_byte_timeout"
        }
        ("http_first_byte", io::ErrorKind::UnexpectedEof) => "http_first_byte_eof",
        ("http_first_byte", _) => "http_first_byte_io",
        ("http_full_response", io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock) => {
            "http_full_response_timeout"
        }
        ("http_full_response", _) => "http_full_response_io",
        _ => "io_error",
    }
}

fn read_bounded_line(stream: &mut UnixStream, limit: usize) -> io::Result<String> {
    let mut bytes = Vec::new();
    while bytes.len() < limit {
        let mut byte = [0_u8; 1];
        stream.read_exact(&mut byte)?;
        bytes.push(byte[0]);
        if byte[0] == b'\n' {
            return String::from_utf8(bytes)
                .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "non-UTF8 vsock reply"));
        }
    }
    Err(io::Error::new(
        io::ErrorKind::InvalidData,
        "virtio-vsock handshake exceeded its bound",
    ))
}

fn readiness_time_remaining(deadline: Instant) -> io::Result<Duration> {
    deadline
        .checked_duration_since(Instant::now())
        .filter(|remaining| !remaining.is_zero())
        .ok_or_else(|| io::Error::new(io::ErrorKind::TimedOut, "readiness probe deadline expired"))
}

fn probe_guest_rocklet_observed(
    address: SocketAddr,
    deadline: Instant,
) -> (io::Result<()>, GuestProbeAttemptStats) {
    let mut observation = GuestProbeAttemptStats::default();
    let tcp_started = Instant::now();
    let mut stream = match readiness_time_remaining(deadline)
        .and_then(|remaining| TcpStream::connect_timeout(&address, remaining))
    {
        Ok(stream) => stream,
        Err(error) => {
            observation.tcp_connect = Some(tcp_started.elapsed());
            observation.error_code = classify_io_error("tcp_connect", &error);
            return (Err(error), observation);
        }
    };
    observation.tcp_connect = Some(tcp_started.elapsed());
    if let Err(error) = readiness_time_remaining(deadline)
        .and_then(|remaining| stream.set_write_timeout(Some(remaining)))
    {
        observation.error_code = "tcp_config_io";
        return (Err(error), observation);
    }
    let http_started = Instant::now();
    if let Err(error) = stream
        .write_all(b"GET /is_alive HTTP/1.1\r\nHost: guest-rocklet\r\nConnection: close\r\n\r\n")
    {
        observation.error_code = classify_io_error("http_write", &error);
        return (Err(error), observation);
    }
    let mut response = Vec::new();
    let mut first_byte = [0_u8; 1];
    if let Err(error) = readiness_time_remaining(deadline)
        .and_then(|remaining| stream.set_read_timeout(Some(remaining)))
        .and_then(|()| stream.read_exact(&mut first_byte))
    {
        observation.http_first_byte = Some(http_started.elapsed());
        observation.error_code = classify_io_error("http_first_byte", &error);
        return (Err(error), observation);
    }
    observation.http_first_byte = Some(http_started.elapsed());
    response.push(first_byte[0]);
    // Recompute the remaining budget on every read: SO_RCVTIMEO alone resets
    // on each syscall and would let a trickling peer outlive the start deadline.
    let read_response = (|| -> io::Result<()> {
        let mut buffer = [0_u8; 4096];
        while response.len() < 64 * 1024 {
            stream.set_read_timeout(Some(readiness_time_remaining(deadline)?))?;
            let capacity = buffer.len().min(64 * 1024 - response.len());
            match stream.read(&mut buffer[..capacity]) {
                Ok(0) => break,
                Ok(length) => response.extend_from_slice(&buffer[..length]),
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) => return Err(error),
            }
        }
        Ok(())
    })();
    if let Err(error) = read_response {
        observation.http_full_response = Some(http_started.elapsed());
        observation.error_code = classify_io_error("http_full_response", &error);
        return (Err(error), observation);
    }
    observation.http_full_response = Some(http_started.elapsed());
    let header_end = response
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .map(|offset| offset + 4)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "incomplete HTTP response"));
    let header_end = match header_end {
        Ok(offset) => offset,
        Err(error) => {
            observation.error_code = "http_incomplete_response";
            return (Err(error), observation);
        }
    };
    let header = match std::str::from_utf8(&response[..header_end]) {
        Ok(header) => header,
        Err(_) => {
            observation.error_code = "http_non_utf8_header";
            return (
                Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "non-UTF8 HTTP header",
                )),
                observation,
            );
        }
    };
    if !header.starts_with("HTTP/1.1 200 ")
        || !response[header_end..]
            .windows(b"\"is_alive\":true".len())
            .any(|window| window == b"\"is_alive\":true")
    {
        observation.error_code = "http_unhealthy";
        return (
            Err(io::Error::other(
                "guest Rocklet readiness response was not healthy",
            )),
            observation,
        );
    }
    observation.error_code = "ok";
    (Ok(()), observation)
}

/// Live child process and the two node-local runtime channels it owns.
pub struct VmmRuntimeHandles {
    pid: u32,
    child: Mutex<Option<Child>>,
    pub control: Arc<VmmControlClient>,
    pub remote_load: UnixStream,
    pub runtime: RuntimeIdentity,
    guest_endpoint: Option<GuestEndpointLease>,
    guest_vsock_path: Option<PathBuf>,
    event_monitor_path: Option<PathBuf>,
    _network: Option<GuestNetworkLease>,
}

impl VmmRuntimeHandles {
    pub fn pid(&self) -> u32 {
        self.pid
    }

    pub fn guest_endpoint(&self) -> Option<GuestEndpointInfo> {
        self.guest_endpoint.as_ref().map(GuestEndpointLease::info)
    }

    pub async fn plugger_control(
        &self,
        action: &str,
        payload: &serde_json::Value,
        timeout: Duration,
    ) -> io::Result<crate::vm::runtime::plugger::PluggerReply> {
        let path = self.guest_vsock_path.as_ref().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::NotConnected,
                "runtime has no guest VSOCK device",
            )
        })?;
        crate::vm::runtime::plugger::control(path, action, payload, timeout).await
    }

    pub async fn wait_for_guest_rocklet(
        &self,
        timeout: Duration,
    ) -> Result<GuestReadinessStats, VmmLaunchError> {
        let transport_started = Instant::now();
        let transport_event_observed = if let Some(path) = self.event_monitor_path.clone() {
            tokio::task::spawn_blocking(move || wait_for_vsock_transport_ready_event(&path))
                .await
                .map_err(|error| {
                    VmmLaunchError::Control(format!("vsock readiness task failed: {error}"))
                })?
        } else {
            false
        };
        let transport_event_wait = transport_started.elapsed();
        let endpoint = self.guest_endpoint.as_ref().ok_or_else(|| {
            VmmLaunchError::Invalid("VMM did not expose its guest Rocklet endpoint".into())
        })?;
        let address = endpoint.probe_address;
        let proxy_attempts = Arc::clone(&endpoint.proxy_attempts);
        let mut readiness =
            tokio::task::spawn_blocking(move || wait_for_guest_rocklet(address, timeout))
                .await
                .map_err(|error| {
                    VmmLaunchError::Control(format!("readiness task failed: {error}"))
                })??;
        readiness.transport_event_wait = transport_event_wait;
        readiness.transport_event_observed = transport_event_observed;
        readiness.proxy_attempts = recover_lock(&proxy_attempts).clone();
        Ok(readiness)
    }

    pub fn has_exited(&self) -> Result<bool, VmmLaunchError> {
        let mut child = self
            .child
            .lock()
            .map_err(|_| VmmLaunchError::LockPoisoned)?;
        match child.as_mut() {
            Some(child) => child
                .try_wait()
                .map(|status| status.is_some())
                .map_err(VmmLaunchError::Spawn),
            None => Ok(true),
        }
    }

    pub async fn shutdown(
        &self,
        reason: &str,
        timeout: Duration,
    ) -> Result<RuntimeSessionStopped, VmmLaunchError> {
        if reason.is_empty() || timeout.is_zero() {
            return invalid("Shutdown reason and timeout must be non-empty");
        }
        let deadline_unix_ms = now_unix_ms().saturating_add(
            u64::try_from(timeout.as_millis())
                .unwrap_or(u64::MAX)
                .max(1),
        );
        let correlation_id = stable_correlation("shutdown", &self.runtime, reason);
        let request = WireEnvelope {
            header: MessageHeader {
                protocol_version: crate::vm::protocol::VMM_PROTOCOL_VERSION,
                channel: Channel::RuntimeControl,
                correlation_id,
            },
            message: ControlRequest::ShutdownRuntimeSession(ShutdownRuntimeSession {
                reason: reason.into(),
                deadline_unix_ms,
            }),
        };
        let received = tokio::time::timeout(timeout, self.control.request(request, Vec::new()))
            .await
            .map_err(|_| VmmLaunchError::ExitTimeout)??;
        if !received.fds.is_empty() {
            return Err(VmmLaunchError::Control(
                "RuntimeSessionStopped unexpectedly carried descriptors".into(),
            ));
        }
        match received.envelope.message {
            ControlReply::RuntimeSessionStopped(stopped) => {
                let _ = self.remote_load.shutdown(std::net::Shutdown::Both);
                self.wait_for_exit(timeout).await?;
                Ok(stopped)
            }
            ControlReply::Error(error) => Err(VmmLaunchError::Control(format!(
                "ShutdownRuntimeSession failed: {}",
                error.message
            ))),
            _ => Err(VmmLaunchError::Control(
                "ShutdownRuntimeSession received an unexpected reply".into(),
            )),
        }
    }

    pub async fn terminate(&self, timeout: Duration) -> Result<(), VmmLaunchError> {
        if timeout.is_zero() {
            return invalid("Termination timeout must be positive");
        }
        {
            let mut child = self
                .child
                .lock()
                .map_err(|_| VmmLaunchError::LockPoisoned)?;
            if let Some(child) = child.as_mut() {
                if child.try_wait().map_err(VmmLaunchError::Spawn)?.is_none() {
                    // SAFETY: pid belongs to the still-live Child protected by this lock.
                    if unsafe { libc::kill(child.id() as libc::pid_t, libc::SIGTERM) } != 0 {
                        let error = io::Error::last_os_error();
                        if error.raw_os_error() != Some(libc::ESRCH) {
                            return Err(VmmLaunchError::Spawn(error));
                        }
                    }
                }
            }
        }
        if self.wait_for_exit(timeout).await.is_err() {
            {
                let mut child = self
                    .child
                    .lock()
                    .map_err(|_| VmmLaunchError::LockPoisoned)?;
                if let Some(child) = child.as_mut() {
                    if child.try_wait().map_err(VmmLaunchError::Spawn)?.is_none() {
                        child.kill().map_err(VmmLaunchError::Spawn)?;
                    }
                }
            }
            self.wait_for_exit(timeout).await?;
        }
        Ok(())
    }

    async fn wait_for_exit(&self, timeout: Duration) -> Result<(), VmmLaunchError> {
        let deadline = Instant::now() + timeout;
        loop {
            let exited = {
                let mut child = self
                    .child
                    .lock()
                    .map_err(|_| VmmLaunchError::LockPoisoned)?;
                match child.as_mut() {
                    Some(child) => child.try_wait().map_err(VmmLaunchError::Spawn)?.is_some(),
                    None => true,
                }
            };
            if exited {
                self.child
                    .lock()
                    .map_err(|_| VmmLaunchError::LockPoisoned)?
                    .take();
                return Ok(());
            }
            if Instant::now() >= deadline {
                return Err(VmmLaunchError::ExitTimeout);
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }
}

impl Drop for VmmRuntimeHandles {
    fn drop(&mut self) {
        let _ = self.remote_load.shutdown(std::net::Shutdown::Both);
        if let Ok(child) = self.child.get_mut() {
            if let Some(child) = child.as_mut() {
                if child.try_wait().ok().flatten().is_none() {
                    let _ = child.kill();
                    let _ = child.wait();
                }
            }
        }
        if let Some(path) = &self.event_monitor_path {
            let _ = std::fs::remove_file(path);
        }
    }
}

pub fn launch_runtime(spec: VmmLaunchSpec) -> Result<VmmRuntimeHandles, VmmLaunchError> {
    spec.validate()?;
    prepare_private_root(&spec.private_root)?;
    let (api_path, remote_path) = runtime_socket_paths(&spec)?;
    let rocklet_vsock_path = rocklet_vsock_path(&spec)?;
    let event_monitor_enabled = spec.guest_rocklet.is_some();
    let event_monitor_path = spec.private_root.join(VMM_EVENT_MONITOR_FILE_NAME);
    prepare_runtime_socket(&api_path)?;
    prepare_runtime_socket(&remote_path)?;
    if spec.guest_rocklet.is_some() {
        prepare_runtime_socket(&rocklet_vsock_path)?;
    }
    if event_monitor_enabled {
        prepare_event_monitor_file(&event_monitor_path)?;
    }
    let guest_endpoint = spec
        .guest_rocklet
        .as_ref()
        .map(|rocklet| GuestEndpointLease::bind(rocklet, rocklet_vsock_path.clone()))
        .transpose()?;

    let network = if spec.guest_network {
        Some(GuestNetworkLease::acquire().map_err(VmmLaunchError::Spawn)?)
    } else {
        None
    };
    let runtime_argument = runtime_argument(&spec)?;
    let mut command = Command::new(&spec.command[0]);
    command
        .args(&spec.command[1..])
        .arg("--api-socket")
        .arg(format!("path={}", safe_path("API socket", &api_path)?))
        .arg("--rock-runtime")
        .arg(runtime_argument);
    if event_monitor_enabled {
        command.arg("--event-monitor").arg(format!(
            "path={}",
            safe_path("event monitor", &event_monitor_path)?
        ));
    }
    configure_vmm_environment(&mut command, &spec);
    if let Some(cwd) = &spec.cwd {
        command.current_dir(cwd);
    }
    let mut inherited_fds = Vec::new();
    if let Some(rocklet) = &spec.guest_rocklet {
        inherited_fds.push((
            duplicate_exec_source(rocklet.artifact.as_raw_fd())?,
            ROCKLET_ARTIFACT_FD,
        ));
    }
    if let Some(network) = &network {
        inherited_fds.push((
            duplicate_exec_source(network.tap.as_raw_fd())?,
            GUEST_TAP_FD,
        ));
        inherited_fds.push((
            duplicate_exec_source(network.connection.as_raw_fd())?,
            NETWORK_LEASE_FD,
        ));
    }
    if !inherited_fds.is_empty() {
        // SAFETY: the closure only performs async-signal-safe fcntl/dup2
        // calls between fork and exec. Owned duplicate sources remain alive
        // until Command::spawn returns in the parent.
        unsafe {
            command.pre_exec(move || {
                for (source, target) in &inherited_fds {
                    if libc::dup2(source.as_raw_fd(), *target) < 0 {
                        return Err(io::Error::last_os_error());
                    }
                }
                Ok(())
            });
        }
    }
    let mut child = command.spawn().map_err(VmmLaunchError::Spawn)?;
    let pid = child.id();
    // CLH accepts the independent RemoteLoad data-plane connection before
    // it enables the HTTP control endpoint.
    let remote_parent = match connect_runtime_socket(&remote_path, &mut child, pid) {
        Ok(stream) => stream,
        Err(error) => {
            cleanup_failed_launch(
                &mut child,
                [
                    &api_path,
                    &remote_path,
                    &rocklet_vsock_path,
                    &event_monitor_path,
                ],
            );
            return Err(error);
        }
    };
    let api_parent = match connect_runtime_socket(&api_path, &mut child, pid) {
        Ok(stream) => stream,
        Err(error) => {
            cleanup_failed_launch(
                &mut child,
                [
                    &api_path,
                    &remote_path,
                    &rocklet_vsock_path,
                    &event_monitor_path,
                ],
            );
            return Err(error);
        }
    };
    let control = match VmmControlClient::new(api_parent) {
        Ok(control) => Arc::new(control),
        Err(error) => {
            let mut child = child;
            let _ = child.kill();
            let _ = child.wait();
            for path in [
                &api_path,
                &remote_path,
                &rocklet_vsock_path,
                &event_monitor_path,
            ] {
                let _ = std::fs::remove_file(path);
            }
            return Err(error.into());
        }
    };
    Ok(VmmRuntimeHandles {
        pid,
        child: Mutex::new(Some(child)),
        control,
        remote_load: remote_parent,
        runtime: spec.runtime,
        guest_endpoint,
        guest_vsock_path: spec.guest_rocklet.as_ref().map(|_| rocklet_vsock_path),
        event_monitor_path: event_monitor_enabled.then_some(event_monitor_path),
        _network: network,
    })
}

fn duplicate_exec_source(fd: i32) -> Result<OwnedFd, VmmLaunchError> {
    // SAFETY: F_DUPFD_CLOEXEC creates a new owned FD above all fixed exec
    // destinations, so dup2 cannot overwrite another source descriptor.
    let duplicate = unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, NETWORK_LEASE_FD + 1) };
    if duplicate < 0 {
        return Err(VmmLaunchError::Spawn(io::Error::last_os_error()));
    }
    // SAFETY: the successful fcntl returned a unique descriptor.
    Ok(unsafe { std::os::fd::FromRawFd::from_raw_fd(duplicate) })
}

fn configure_vmm_environment(command: &mut Command, spec: &VmmLaunchSpec) {
    command.env_clear();
    for key in SAFE_ENVIRONMENT_KEYS {
        if let Some(value) = std::env::var_os(key) {
            command.env(key, value);
        }
    }
    // uffd_legacy is intentionally not an ambient-safe key: only the validated
    // launch spec may select legacy behavior for a VMM child.
    command
        .env(ROCK_UFFD_LEGACY_ENV, spec.uffd_legacy())
        .envs(&spec.environment);
}

fn prepare_private_root(path: &Path) -> Result<(), VmmLaunchError> {
    std::fs::create_dir_all(path).map_err(VmmLaunchError::Spawn)?;
    let metadata = std::fs::symlink_metadata(path).map_err(VmmLaunchError::Spawn)?;
    if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
        return invalid("VMM private root must be a real directory");
    }
    if metadata.uid() != unsafe { libc::geteuid() } {
        return invalid("VMM private root is not owned by the launcher uid");
    }
    if metadata.permissions().mode() & 0o777 != 0o700 {
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))
            .map_err(VmmLaunchError::Spawn)?;
    }
    let verified = std::fs::symlink_metadata(path).map_err(VmmLaunchError::Spawn)?;
    if !verified.file_type().is_dir()
        || verified.file_type().is_symlink()
        || verified.uid() != unsafe { libc::geteuid() }
        || verified.permissions().mode() & 0o777 != 0o700
    {
        return invalid("VMM private root did not remain an owner-only directory");
    }
    Ok(())
}

fn runtime_argument(spec: &VmmLaunchSpec) -> Result<String, VmmLaunchError> {
    let cache = safe_path("cache root", &spec.cache_root)?;
    let private = safe_path("private root", &spec.private_root)?;
    let (_, remote_path) = runtime_socket_paths(spec)?;
    let values = [
        format!("launch_identity={}", spec.launch_identity),
        format!("parent_pid={}", std::process::id()),
        format!(
            "remote_missing_socket={}",
            safe_path("remote-missing socket", &remote_path)?
        ),
        format!("cache_root={cache}"),
        format!("private_root={private}"),
        format!("config_sha256={}", spec.config_sha256()),
        format!("fault_around_pages={}", spec.fault_around_pages),
    ];
    Ok(values.join(","))
}

fn runtime_socket_paths(spec: &VmmLaunchSpec) -> Result<(PathBuf, PathBuf), VmmLaunchError> {
    let api = spec.private_root.join(API_SOCKET_NAME);
    let remote = spec.private_root.join(REMOTE_MISSING_SOCKET_NAME);
    for (label, path) in [("API socket", &api), ("remote-missing socket", &remote)] {
        safe_path(label, path)?;
        if path.as_os_str().as_bytes().len() >= 108 {
            return invalid(&format!("{label} exceeds sockaddr_un sun_path"));
        }
    }
    Ok((api, remote))
}

fn rocklet_vsock_path(spec: &VmmLaunchSpec) -> Result<PathBuf, VmmLaunchError> {
    let path = spec.private_root.join(ROCKLET_VSOCK_SOCKET_NAME);
    safe_path("Rocklet vsock socket", &path)?;
    if path.as_os_str().as_bytes().len() >= 108 {
        return invalid("Rocklet vsock socket exceeds sockaddr_un sun_path");
    }
    Ok(path)
}

fn prepare_runtime_socket(path: &Path) -> Result<(), VmmLaunchError> {
    match std::fs::symlink_metadata(path) {
        Ok(metadata) if !metadata.file_type().is_socket() => {
            invalid("Runtime endpoint path is occupied by a non-socket inode")
        }
        Ok(_) => match UnixStream::connect(path) {
            Ok(stream) => {
                drop(stream);
                invalid("Runtime endpoint still has a live listener")
            }
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::ConnectionRefused | io::ErrorKind::NotFound
                ) =>
            {
                std::fs::remove_file(path).map_err(VmmLaunchError::Spawn)
            }
            Err(error) => Err(VmmLaunchError::Spawn(error)),
        },
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(VmmLaunchError::Spawn(error)),
    }
}

fn prepare_event_monitor_file(path: &Path) -> Result<(), VmmLaunchError> {
    match std::fs::symlink_metadata(path) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Ok(_) => invalid("VMM event monitor path is already occupied"),
        Err(error) => Err(VmmLaunchError::Spawn(error)),
    }
}

fn connect_runtime_socket(
    path: &Path,
    child: &mut Child,
    expected_pid: u32,
) -> Result<UnixStream, VmmLaunchError> {
    let deadline = Instant::now() + SOCKET_CONNECT_TIMEOUT;
    loop {
        if child.try_wait().map_err(VmmLaunchError::Spawn)?.is_some() {
            return Err(VmmLaunchError::Control(
                "VMM exited before binding its named runtime sockets".into(),
            ));
        }
        match UnixStream::connect(path) {
            Ok(stream) => {
                validate_runtime_peer(&stream, expected_pid)?;
                stream
                    .set_nonblocking(false)
                    .map_err(VmmLaunchError::Spawn)?;
                return Ok(stream);
            }
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::NotFound
                        | io::ErrorKind::ConnectionRefused
                        | io::ErrorKind::WouldBlock
                ) =>
            {
                if Instant::now() >= deadline {
                    return Err(VmmLaunchError::Control(
                        "VMM did not accept its named runtime socket before the deadline".into(),
                    ));
                }
                std::thread::sleep(Duration::from_millis(5));
            }
            Err(error) => return Err(VmmLaunchError::Spawn(error)),
        }
    }
}

fn validate_runtime_peer(stream: &UnixStream, expected_pid: u32) -> Result<(), VmmLaunchError> {
    let mut credentials = libc::ucred {
        pid: 0,
        uid: 0,
        gid: 0,
    };
    let mut length = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    // SAFETY: credentials and length are valid SO_PEERCRED output buffers.
    if unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            (&mut credentials as *mut libc::ucred).cast(),
            &mut length,
        )
    } == -1
    {
        return Err(VmmLaunchError::Spawn(io::Error::last_os_error()));
    }
    if credentials.pid != expected_pid as i32 || credentials.uid != unsafe { libc::geteuid() } {
        return invalid("Runtime endpoint peer does not match the launched VMM");
    }
    Ok(())
}

fn cleanup_failed_launch<const N: usize>(child: &mut Child, paths: [&PathBuf; N]) {
    let _ = child.kill();
    let _ = child.wait();
    for path in paths {
        let _ = std::fs::remove_file(path);
    }
}

fn update_hash_component(digest: &mut Sha256, value: &[u8]) {
    digest.update((value.len() as u64).to_le_bytes());
    digest.update(value);
}

fn safe_path<'a>(label: &str, path: &'a Path) -> Result<&'a str, VmmLaunchError> {
    let value = path
        .to_str()
        .ok_or_else(|| VmmLaunchError::Invalid(format!("{label} is not valid UTF-8")))?;
    if value.is_empty() || value.contains(',') || value.contains('\0') {
        return invalid(&format!("{label} contains a runtime delimiter"));
    }
    Ok(value)
}

fn stable_correlation(operation: &str, runtime: &RuntimeIdentity, identity: &str) -> String {
    let value = format!("{operation}\0{}\0{identity}", runtime.sandbox_id);
    format!("{operation}-{:x}", Sha256::digest(value.as_bytes()))
}

fn now_unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .try_into()
        .unwrap_or(u64::MAX)
}

fn invalid<T>(message: &str) -> Result<T, VmmLaunchError> {
    Err(VmmLaunchError::Invalid(message.into()))
}
