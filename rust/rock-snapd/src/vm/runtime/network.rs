//! Privileged network allocation separated from the unprivileged VMM owner.
//!
//! A connection is a lease. Both snapd and CLH retain it, so a daemon crash
//! cannot disconnect an adoptable VM. Every allocation has its own conntrack
//! namespace; identical guest IP/MAC addresses therefore remain fork-safe.

use std::fs::{File, OpenOptions};
use std::io::{self, IoSlice, IoSliceMut, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use nix::sys::socket::{
    getsockopt, recvmsg, sendmsg, sockopt, ControlMessage, ControlMessageOwned, MsgFlags,
    SockaddrStorage,
};

pub const NETWORK_SOCKET: &str = "/run/rock-snapd-network/lease.sock";
pub const GUEST_TAP_FD: i32 = 202;
pub const NETWORK_LEASE_FD: i32 = 203;
pub const NET_ARGUMENT: &str =
    "id=guest-net,fd=202,mac=02:52:4f:43:4b:01,num_queues=2,queue_size=256";
pub const NETWORK_ENV: &str = "ROCK_GUEST_NETWORK";
const UID: u32 = 65532;
const MAX_NETWORKS: usize = 512;
const IP: &str = "/usr/sbin/ip";
const IPTABLES: &str = "/usr/sbin/iptables";
const CONNTRACK: &str = "/usr/sbin/conntrack";
const DNSMASQ: &str = "/usr/sbin/dnsmasq";
const REQUEST: &[u8; 7] = b"RSNET1\n";

pub struct GuestNetworkLease {
    pub tap: OwnedFd,
    pub connection: UnixStream,
}

impl GuestNetworkLease {
    pub fn acquire() -> io::Result<Self> {
        let mut connection = UnixStream::connect(NETWORK_SOCKET)?;
        if getsockopt(&connection, sockopt::PeerCredentials)?.uid() != 0 {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "Network helper is not root",
            ));
        }
        connection.set_read_timeout(Some(Duration::from_secs(60)))?;
        connection.set_write_timeout(Some(Duration::from_secs(10)))?;
        connection.write_all(REQUEST)?;
        let tap = receive_tap(&connection)?;
        connection.set_read_timeout(None)?;
        Ok(Self { tap, connection })
    }
}

fn send_tap(stream: &UnixStream, tap: &OwnedFd) -> io::Result<()> {
    let fds = [tap.as_raw_fd()];
    let slices = [IoSlice::new(&[1])];
    let control = [ControlMessage::ScmRights(&fds)];
    let n = sendmsg::<SockaddrStorage>(
        stream.as_raw_fd(),
        &slices,
        &control,
        MsgFlags::MSG_NOSIGNAL,
        None,
    )?;
    if n != 1 {
        return Err(io::Error::new(
            io::ErrorKind::WriteZero,
            "Network lease reply was not sent",
        ));
    }
    Ok(())
}

fn receive_tap(stream: &UnixStream) -> io::Result<OwnedFd> {
    let mut reply = [0];
    let mut slices = [IoSliceMut::new(&mut reply)];
    let mut control = nix::cmsg_space!([i32; 4]);
    let msg = recvmsg::<SockaddrStorage>(
        stream.as_raw_fd(),
        &mut slices,
        Some(&mut control),
        MsgFlags::MSG_CMSG_CLOEXEC,
    )?;
    let mut fds = Vec::new();
    for c in msg.cmsgs()? {
        if let ControlMessageOwned::ScmRights(raw) = c {
            for fd in raw {
                // SAFETY: SCM_RIGHTS transfers unique descriptors to this process.
                fds.push(unsafe { OwnedFd::from_raw_fd(fd) });
            }
        }
    }
    if msg.bytes != 1
        || msg
            .flags
            .intersects(MsgFlags::MSG_CTRUNC | MsgFlags::MSG_TRUNC)
        || fds.len() != 1
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "Invalid network lease reply",
        ));
    }
    if reply[0] != 1 {
        return Err(io::Error::other("Network allocation failed"));
    }
    Ok(fds.remove(0))
}

fn run(binary: &str, arguments: &[String]) -> io::Result<()> {
    let mut child = Command::new(binary)
        .args(arguments)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()?;
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Some(status) = child.try_wait()? {
            if status.success() {
                return Ok(());
            }
            let mut diagnostic = String::new();
            if let Some(stderr) = child.stderr.take() {
                stderr.take(4096).read_to_string(&mut diagnostic)?;
            }
            if binary == CONNTRACK
                && status.code() == Some(1)
                && diagnostic.contains("0 flow entries have been deleted")
            {
                return Ok(());
            }
            return Err(io::Error::other(format!("{binary} failed: {diagnostic}")));
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "Network command timed out",
            ));
        }
        thread::sleep(Duration::from_millis(10));
    }
}

fn args(values: &[&str]) -> Vec<String> {
    values.iter().map(|x| (*x).to_owned()).collect()
}
fn ip(values: &[&str]) -> io::Result<()> {
    run(IP, &args(values))
}

// One worker-local forwarder follows the worker's resolver, including Docker's
// loopback DNS. Only lease veths are listened on; no DHCP or local cache is used.
// The daemon drops privileges itself (unlike dnsmasq's debug --no-daemon mode).
struct DnsForwarder(Child);
impl DnsForwarder {
    fn start() -> io::Result<Self> {
        let mut process = Command::new(DNSMASQ);
        process.args([
            "--keep-in-foreground",
            "--conf-file=/dev/null",
            "--pid-file=",
            "--user=nobody",
            "--group=nogroup",
            "--interface=rsn*",
            "--except-interface=lo",
            "--bind-dynamic",
            "--no-hosts",
            "--cache-size=0",
            "--resolv-file=/etc/resolv.conf",
            "--log-facility=-",
        ]);
        let mut forwarder = Self(process.stdin(Stdio::null()).stdout(Stdio::null()).spawn()?);
        thread::sleep(Duration::from_millis(50));
        if forwarder.0.try_wait()?.is_some() {
            return Err(io::Error::other(
                "Worker DNS forwarder exited during startup",
            ));
        }
        Ok(forwarder)
    }
}
impl Drop for DnsForwarder {
    fn drop(&mut self) {
        if self.0.try_wait().ok().flatten().is_none() {
            // SAFETY: the unreaped child PID cannot be reused before wait.
            unsafe {
                libc::kill(self.0.id() as i32, libc::SIGTERM);
            }
            let deadline = Instant::now() + Duration::from_secs(2);
            while Instant::now() < deadline {
                if self.0.try_wait().ok().flatten().is_some() {
                    return;
                }
                thread::sleep(Duration::from_millis(10));
            }
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
}

struct Allocation {
    namespace: String,
    host_veth: String,
    namespace_created: bool,
    veth_created: bool,
    rules: Vec<Vec<String>>,
    uplink_ip: String,
    reusable: Arc<AtomicBool>,
}

impl Allocation {
    fn new(slot: usize, egress: &str, reusable: Arc<AtomicBool>) -> io::Result<(Self, OwnedFd)> {
        let id = uuid::Uuid::new_v4().simple().to_string();
        let mut allocation = Self {
            namespace: format!("rock-snapd-{id}"),
            host_veth: format!("rsn{}", &id[..12]),
            namespace_created: false,
            veth_created: false,
            rules: Vec::new(),
            uplink_ip: uplink_addresses(slot).1,
            reusable,
        };
        let ns = allocation.namespace.clone();
        let host = allocation.host_veth.clone();
        let (host_ip, uplink_ip) = uplink_addresses(slot);
        ip(&["netns", "add", &ns])?;
        allocation.namespace_created = true;
        ip(&[
            "link", "add", &host, "type", "veth", "peer", "name", "uplink0", "netns", &ns,
        ])?;
        allocation.veth_created = true;
        ip(&["addr", "add", &format!("{host_ip}/30"), "dev", &host])?;
        ip(&["link", "set", &host, "up"])?;
        ip(&["-n", &ns, "link", "set", "lo", "up"])?;
        ip(&[
            "-n",
            &ns,
            "addr",
            "add",
            &format!("{uplink_ip}/30"),
            "dev",
            "uplink0",
        ])?;
        ip(&["-n", &ns, "link", "set", "uplink0", "up"])?;
        ip(&["-n", &ns, "route", "add", "default", "via", &host_ip])?;
        ip(&[
            "netns",
            "exec",
            &ns,
            "/usr/sbin/sysctl",
            "-q",
            "-w",
            "net.ipv4.ip_forward=1",
        ])?;
        let tap = open_tap(&ns)?;
        ip(&["-n", &ns, "addr", "add", "192.168.127.1/30", "dev", "tap0"])?;
        // A restored guest retains its ARP cache, including the gateway MAC.
        ip(&[
            "-n",
            &ns,
            "link",
            "set",
            "tap0",
            "address",
            "02:52:4f:43:4b:fe",
        ])?;
        ip(&["-n", &ns, "link", "set", "tap0", "up"])?;
        // The address retained in guest memory is node-independent. Translate
        // it to this target worker's DNS listener, over both UDP and TCP.
        for protocol in ["udp", "tcp"] {
            ip(&[
                "netns",
                "exec",
                &ns,
                IPTABLES,
                "-w",
                "5",
                "-t",
                "nat",
                "-A",
                "PREROUTING",
                "-i",
                "tap0",
                "-d",
                "192.168.127.1/32",
                "-p",
                protocol,
                "--dport",
                "53",
                "-j",
                "DNAT",
                "--to-destination",
                &format!("{host_ip}:53"),
            ])?;
        }
        for rule in [
            args(&[
                "-t",
                "nat",
                "-A",
                "POSTROUTING",
                "-s",
                "192.168.127.2/32",
                "-o",
                "uplink0",
                "-j",
                "SNAT",
                "--to-source",
                &uplink_ip,
            ]),
            args(&["-P", "FORWARD", "DROP"]),
            args(&[
                "-A", "FORWARD", "-i", "tap0", "-o", "uplink0", "-j", "ACCEPT",
            ]),
            args(&[
                "-A",
                "FORWARD",
                "-i",
                "uplink0",
                "-o",
                "tap0",
                "-m",
                "conntrack",
                "--ctstate",
                "ESTABLISHED,RELATED",
                "-j",
                "ACCEPT",
            ]),
        ] {
            let mut command = args(&["netns", "exec", &ns, IPTABLES, "-w", "5"]);
            command.extend(rule);
            run(IP, &command)?;
        }
        // Outbound traffic may leave only through the worker's egress device.
        // It may not reach the worker itself or another guest's veth endpoint.
        allocation.rule(&["-I", "INPUT", "1", "-i", &host, "-j", "DROP"])?;
        for protocol in ["udp", "tcp"] {
            allocation.rule(&[
                "-I",
                "INPUT",
                "1",
                "-i",
                &host,
                "-s",
                &format!("{uplink_ip}/32"),
                "-d",
                &format!("{host_ip}/32"),
                "-p",
                protocol,
                "--dport",
                "53",
                "-j",
                "ACCEPT",
            ])?;
        }
        allocation.rule(&["-I", "FORWARD", "1", "-i", &host, "-j", "DROP"])?;
        allocation.rule(&[
            "-I",
            "FORWARD",
            "1",
            "-i",
            egress,
            "-o",
            &host,
            "-m",
            "conntrack",
            "--ctstate",
            "ESTABLISHED,RELATED",
            "-j",
            "ACCEPT",
        ])?;
        allocation.rule(&[
            "-I",
            "FORWARD",
            "1",
            "-i",
            &host,
            "-s",
            &format!("{uplink_ip}/32"),
            "-o",
            egress,
            "-j",
            "ACCEPT",
        ])?;
        allocation.rule(&[
            "-t",
            "nat",
            "-A",
            "POSTROUTING",
            "-s",
            &format!("{uplink_ip}/32"),
            "-o",
            egress,
            "-j",
            "MASQUERADE",
        ])?;
        let address = format!("{host_ip}:53").parse().unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            if std::net::TcpStream::connect_timeout(&address, Duration::from_millis(50)).is_ok() {
                break;
            }
            if Instant::now() >= deadline {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "Worker DNS listener was not ready",
                ));
            }
            thread::sleep(Duration::from_millis(25));
        }
        Ok((allocation, tap))
    }

    fn rule(&mut self, values: &[&str]) -> io::Result<()> {
        let mut command = args(&["-w", "5"]);
        command.extend(args(values));
        run(IPTABLES, &command)?;
        let mut deletion = args(values);
        if let Some(i) = deletion.iter().position(|v| v == "-I" || v == "-A") {
            let insertion = deletion[i] == "-I";
            deletion[i] = "-D".into();
            if insertion {
                deletion.remove(i + 2);
            }
        }
        self.rules.push(deletion);
        Ok(())
    }
}

impl Drop for Allocation {
    fn drop(&mut self) {
        for rule in self.rules.iter().rev() {
            let mut command = args(&["-w", "5"]);
            command.extend(rule.clone());
            if let Err(error) = run(IPTABLES, &command) {
                self.reusable.store(false, Ordering::Release);
                eprintln!("Network rule cleanup failed: {error}");
            }
        }
        if self.veth_created {
            if let Err(error) = ip(&["link", "del", &self.host_veth]) {
                self.reusable.store(false, Ordering::Release);
                eprintln!("Network veth cleanup failed: {error}");
            }
        }
        if self.namespace_created {
            if let Err(error) = ip(&["netns", "del", &self.namespace]) {
                self.reusable.store(false, Ordering::Release);
                eprintln!("Network namespace cleanup failed: {error}");
            }
        }
        // The namespace's own conntrack table dies with it. The worker's
        // second NAT table must also forget this /30 before its slot is reused.
        if let Err(error) = run(
            CONNTRACK,
            &args(&["-D", "-f", "ipv4", "-s", &self.uplink_ip]),
        ) {
            self.reusable.store(false, Ordering::Release);
            eprintln!("Network conntrack cleanup failed: {error}");
        }
    }
}

fn uplink_addresses(slot: usize) -> (String, String) {
    let octet = (slot % 64) * 4;
    (
        format!("10.231.{}.{}", slot / 64, octet + 1),
        format!("10.231.{}.{}", slot / 64, octet + 2),
    )
}

struct NamespaceGuard(File);
impl Drop for NamespaceGuard {
    fn drop(&mut self) {
        // SAFETY: this is the calling OS thread's original, pinned namespace.
        // Continuing privileged cleanup in a different namespace is unsafe.
        if unsafe { libc::setns(self.0.as_raw_fd(), libc::CLONE_NEWNET) } != 0 {
            std::process::abort();
        }
    }
}

fn open_tap(namespace: &str) -> io::Result<OwnedFd> {
    let original = File::open("/proc/thread-self/ns/net")?;
    let target = File::open(format!("/run/netns/{namespace}"))?;
    // SAFETY: each lease runs on a dedicated blocking OS thread. No async task
    // or other thread changes namespace, and the guard always restores it.
    if unsafe { libc::setns(target.as_raw_fd(), libc::CLONE_NEWNET) } != 0 {
        return Err(io::Error::last_os_error());
    }
    let _guard = NamespaceGuard(original);
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open("/dev/net/tun")?;
    // SAFETY: zero is a valid initial ifreq; name and flags are set before ioctl.
    let mut request: libc::ifreq = unsafe { std::mem::zeroed() };
    for (dst, src) in request.ifr_name.iter_mut().zip(b"tap0\0") {
        *dst = *src as libc::c_char;
    }
    request.ifr_ifru.ifru_flags =
        (libc::IFF_TAP | libc::IFF_NO_PI | libc::IFF_VNET_HDR) as libc::c_short;
    // SAFETY: the kernel receives a valid ifreq and a live TUN descriptor.
    if unsafe { libc::ioctl(file.as_raw_fd(), libc::TUNSETIFF, &request) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(file.into())
}

struct Slot {
    index: usize,
    slots: Arc<Mutex<Vec<bool>>>,
    reusable: Arc<AtomicBool>,
}
impl Drop for Slot {
    fn drop(&mut self) {
        // Quarantine an uplink whose cleanup failed: reusing its address could
        // attach a new clone to stale firewall or conntrack state.
        if self.reusable.load(Ordering::Acquire) {
            self.slots.lock().unwrap_or_else(|e| e.into_inner())[self.index] = false;
        }
    }
}

/// Run as root in the worker network namespace, with NET_ADMIN and SYS_ADMIN.
pub fn serve(stop: Arc<AtomicBool>) -> io::Result<()> {
    // SAFETY: geteuid has no arguments or memory safety requirements.
    if unsafe { libc::geteuid() } != 0 {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "Network helper requires root",
        ));
    }
    let routes = Command::new(IP)
        .args(["-j", "route", "get", "1.1.1.1"])
        .output()?;
    let routes: serde_json::Value = serde_json::from_slice(&routes.stdout)?;
    let egress = routes[0]["dev"]
        .as_str()
        .ok_or_else(|| io::Error::other("No IPv4 egress device"))?
        .to_owned();
    let links = Command::new(IP).args(["-j", "link", "show"]).output()?;
    let links: serde_json::Value = serde_json::from_slice(&links.stdout)?;
    if links
        .as_array()
        .into_iter()
        .flatten()
        .any(|l| l["ifname"].as_str().is_some_and(|n| n.starts_with("rsn")))
    {
        return Err(io::Error::other(
            "Existing network leases must be drained before restarting the helper",
        ));
    }
    let mut dns = DnsForwarder::start()?;
    let root = std::path::Path::new(NETWORK_SOCKET).parent().unwrap();
    std::fs::create_dir_all(root)?;
    let metadata = std::fs::symlink_metadata(root)?;
    if !metadata.is_dir() || metadata.uid() != 0 || metadata.mode() & 0o022 != 0 {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "Unsafe network helper directory",
        ));
    }
    std::fs::set_permissions(root, std::fs::Permissions::from_mode(0o755))?;
    if let Ok(metadata) = std::fs::symlink_metadata(NETWORK_SOCKET) {
        use std::os::unix::fs::FileTypeExt;
        if !metadata.file_type().is_socket() || UnixStream::connect(NETWORK_SOCKET).is_ok() {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "Network helper socket already exists",
            ));
        }
        std::fs::remove_file(NETWORK_SOCKET)?;
    }
    let listener = UnixListener::bind(NETWORK_SOCKET)?;
    let socket_name = std::ffi::CString::new(NETWORK_SOCKET).unwrap();
    // SAFETY: the fixed socket path is NUL-terminated and owned by this helper.
    if unsafe { libc::chown(socket_name.as_ptr(), UID, UID) } != 0 {
        return Err(io::Error::last_os_error());
    }
    std::fs::set_permissions(NETWORK_SOCKET, std::fs::Permissions::from_mode(0o600))?;
    listener.set_nonblocking(true)?;
    std::fs::write("/proc/sys/net/ipv4/ip_forward", "1")?;
    let slots = Arc::new(Mutex::new(vec![false; MAX_NETWORKS]));
    let mut workers = Vec::new();
    let mut dns_failed = false;
    while !stop.load(Ordering::Acquire) {
        if dns.0.try_wait()?.is_some() {
            dns_failed = true;
            stop.store(true, Ordering::Release);
            break;
        }
        workers.retain(|worker: &thread::JoinHandle<()>| !worker.is_finished());
        match listener.accept() {
            Ok((mut connection, _)) => {
                if getsockopt(&connection, sockopt::PeerCredentials)?.uid() != UID {
                    continue;
                }
                let mut taken = slots.lock().unwrap_or_else(|e| e.into_inner());
                let Some(index) = taken.iter().position(|v| !*v) else {
                    continue;
                };
                taken[index] = true;
                drop(taken);
                let slot = Slot {
                    index,
                    slots: Arc::clone(&slots),
                    reusable: Arc::new(AtomicBool::new(true)),
                };
                let egress = egress.clone();
                let stop = Arc::clone(&stop);
                workers.push(thread::spawn(move || {
                    let _slot = slot;
                    let result = (|| -> io::Result<()> {
                        connection.set_read_timeout(Some(Duration::from_secs(5)))?;
                        let mut request = [0; 7];
                        connection.read_exact(&mut request)?;
                        if &request != REQUEST {
                            return Err(io::Error::new(
                                io::ErrorKind::InvalidInput,
                                "Invalid network lease request",
                            ));
                        }
                        let (_allocation, tap) =
                            Allocation::new(index, &egress, Arc::clone(&_slot.reusable))?;
                        send_tap(&connection, &tap)?;
                        connection.set_read_timeout(Some(Duration::from_millis(250)))?;
                        loop {
                            if stop.load(Ordering::Acquire) {
                                break;
                            }
                            match connection.read(&mut [0]) {
                                Ok(0) => break,
                                Ok(_) => {
                                    return Err(io::Error::other(
                                        "Unexpected network lease payload",
                                    ))
                                }
                                Err(e)
                                    if matches!(
                                        e.kind(),
                                        io::ErrorKind::WouldBlock
                                            | io::ErrorKind::TimedOut
                                            | io::ErrorKind::Interrupted
                                    ) => {}
                                Err(e) => return Err(e),
                            }
                        }
                        Ok(())
                    })();
                    if let Err(error) = result {
                        eprintln!("Network lease failed: {error}");
                    }
                }));
            }
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(50))
            }
            Err(e) => return Err(e),
        }
    }
    for worker in workers {
        let _ = worker.join();
    }
    std::fs::remove_file(NETWORK_SOCKET)?;
    if dns_failed {
        Err(io::Error::other("Worker DNS forwarder exited"))
    } else {
        Ok(())
    }
}
