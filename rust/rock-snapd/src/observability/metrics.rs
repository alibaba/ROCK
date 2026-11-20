//! Fixed-cardinality Prometheus instrumentation for the Rust node daemon.
//!
//! Runtime identities, snapshot IDs, object keys and tenant names are
//! deliberately absent from labels.  The release collector consumes these
//! cumulative process-local series across explicit process-run boundaries.

use std::array;
use std::fmt::Write as _;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, OnceLock, Weak};
use std::time::Duration;

use thiserror::Error;

use crate::observability::structured_log::{StructuredEvent, StructuredLogger};

const FINITE_BUCKET_NS: [u64; 8] = [
    1_000_000,
    5_000_000,
    10_000_000,
    25_000_000,
    50_000_000,
    100_000_000,
    250_000_000,
    1_000_000_000,
];
const FINITE_BUCKET_LABELS: [&str; 8] = [
    "0.001", "0.005", "0.01", "0.025", "0.05", "0.1", "0.25", "1",
];
const BUCKET_COUNT: usize = FINITE_BUCKET_NS.len() + 1;
const DATA_UPLOAD_BUCKET_NS: [u64; 10] = [
    100_000_000,
    500_000_000,
    1_000_000_000,
    2_500_000_000,
    5_000_000_000,
    10_000_000_000,
    30_000_000_000,
    60_000_000_000,
    120_000_000_000,
    300_000_000_000,
];
const DATA_UPLOAD_BUCKET_LABELS: [&str; 10] = [
    "0.1", "0.5", "1", "2.5", "5", "10", "30", "60", "120", "300",
];

#[derive(Debug, Error)]
pub enum MetricsError {
    #[error("metrics provider label is not canonical")]
    InvalidProvider,
    #[error("structured logger is already installed")]
    LoggerAlreadyInstalled,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DataKind {
    Memory,
    Block,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum P2PProbeOutcome {
    Available,
    Unavailable,
    Error,
}

impl P2PProbeOutcome {
    const ALL: [Self; 3] = [Self::Available, Self::Unavailable, Self::Error];

    const fn index(self) -> usize {
        self as usize
    }

    const fn label(self) -> &'static str {
        match self {
            Self::Available => "available",
            Self::Unavailable => "unavailable",
            Self::Error => "error",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum P2PRangeOutcome {
    Hit,
    Miss,
    Timeout,
    TransportError,
    IntegrityError,
}

impl P2PRangeOutcome {
    const ALL: [Self; 5] = [
        Self::Hit,
        Self::Miss,
        Self::Timeout,
        Self::TransportError,
        Self::IntegrityError,
    ];

    const fn index(self) -> usize {
        self as usize
    }

    const fn label(self) -> &'static str {
        match self {
            Self::Hit => "hit",
            Self::Miss => "miss",
            Self::Timeout => "timeout",
            Self::TransportError => "transport_error",
            Self::IntegrityError => "integrity_error",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum P2PFallbackReason {
    NoHint,
    ProbeMiss,
    RangeMiss,
    Timeout,
    Transport,
    Integrity,
}

impl P2PFallbackReason {
    const ALL: [Self; 6] = [
        Self::NoHint,
        Self::ProbeMiss,
        Self::RangeMiss,
        Self::Timeout,
        Self::Transport,
        Self::Integrity,
    ];

    const fn index(self) -> usize {
        self as usize
    }

    const fn label(self) -> &'static str {
        match self {
            Self::NoHint => "no_hint",
            Self::ProbeMiss => "probe_miss",
            Self::RangeMiss => "range_miss",
            Self::Timeout => "timeout",
            Self::Transport => "transport",
            Self::Integrity => "integrity",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OperationMetric {
    HttpControl,
    CreateBaseImage,
    SnapshotSandbox,
    ForkSandbox,
    StartSandbox,
    StopSandbox,
    FinalizeSnapshot,
    DeleteSnapshot,
    RuntimeAdoption,
}

impl OperationMetric {
    const ALL: [Self; 9] = [
        Self::HttpControl,
        Self::CreateBaseImage,
        Self::SnapshotSandbox,
        Self::ForkSandbox,
        Self::StartSandbox,
        Self::StopSandbox,
        Self::FinalizeSnapshot,
        Self::DeleteSnapshot,
        Self::RuntimeAdoption,
    ];

    const fn index(self) -> usize {
        self as usize
    }

    pub(crate) const fn label(self) -> &'static str {
        match self {
            Self::HttpControl => "http_control",
            Self::CreateBaseImage => "create_base_image",
            Self::SnapshotSandbox => "snapshot_sandbox",
            Self::ForkSandbox => "fork_sandbox",
            Self::StartSandbox => "start_sandbox",
            Self::StopSandbox => "stop_sandbox",
            Self::FinalizeSnapshot => "finalize_snapshot",
            Self::DeleteSnapshot => "delete_snapshot",
            Self::RuntimeAdoption => "runtime_adoption",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OperationOutcome {
    Success,
    Error,
    Rejected,
}

impl OperationOutcome {
    const ALL: [Self; 3] = [Self::Success, Self::Error, Self::Rejected];

    const fn index(self) -> usize {
        self as usize
    }

    const fn label(self) -> &'static str {
        match self {
            Self::Success => "success",
            Self::Error => "error",
            Self::Rejected => "rejected",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QueueComponent {
    Upload,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DataUploadOutcome {
    Success,
    Busy,
    Cancelled,
    Timeout,
    RetryableError,
    IntegrityError,
    IdentityConflict,
    AbortCleanupUnconfirmed,
}

impl DataUploadOutcome {
    const ALL: [Self; 8] = [
        Self::Success,
        Self::Busy,
        Self::Cancelled,
        Self::Timeout,
        Self::RetryableError,
        Self::IntegrityError,
        Self::IdentityConflict,
        Self::AbortCleanupUnconfirmed,
    ];

    const fn index(self) -> usize {
        self as usize
    }

    const fn label(self) -> &'static str {
        match self {
            Self::Success => "success",
            Self::Busy => "busy",
            Self::Cancelled => "cancelled",
            Self::Timeout => "timeout",
            Self::RetryableError => "retryable_error",
            Self::IntegrityError => "integrity_error",
            Self::IdentityConflict => "identity_conflict",
            Self::AbortCleanupUnconfirmed => "abort_cleanup_unconfirmed",
        }
    }
}

impl QueueComponent {
    const ALL: [Self; 1] = [Self::Upload];

    const fn index(self) -> usize {
        self as usize
    }

    const fn label(self) -> &'static str {
        match self {
            Self::Upload => "upload",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LeaseKind {
    Backing,
    Runtime,
    Upload,
}

impl LeaseKind {
    const ALL: [Self; 3] = [Self::Backing, Self::Runtime, Self::Upload];

    const fn index(self) -> usize {
        self as usize
    }

    const fn label(self) -> &'static str {
        match self {
            Self::Backing => "backing",
            Self::Runtime => "runtime",
            Self::Upload => "upload",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RecoveryState {
    Pending,
    Succeeded,
    Failed,
}

impl RecoveryState {
    const ALL: [Self; 3] = [Self::Pending, Self::Succeeded, Self::Failed];

    const fn index(self) -> usize {
        self as usize
    }

    const fn label(self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::Succeeded => "succeeded",
            Self::Failed => "failed",
        }
    }
}

impl DataKind {
    const ALL: [Self; 2] = [Self::Memory, Self::Block];

    const fn index(self) -> usize {
        match self {
            Self::Memory => 0,
            Self::Block => 1,
        }
    }

    const fn label(self) -> &'static str {
        match self {
            Self::Memory => "memory",
            Self::Block => "block",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DownloadOutcomeMetric {
    Loaded,
    CacheHit,
    Joined,
    Failed,
}

impl DownloadOutcomeMetric {
    const ALL: [Self; 4] = [Self::Loaded, Self::CacheHit, Self::Joined, Self::Failed];

    const fn index(self) -> usize {
        match self {
            Self::Loaded => 0,
            Self::CacheHit => 1,
            Self::Joined => 2,
            Self::Failed => 3,
        }
    }

    const fn label(self) -> &'static str {
        match self {
            Self::Loaded => "loaded",
            Self::CacheHit => "cache_hit",
            Self::Joined => "joined",
            Self::Failed => "failed",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Origin {
    UffdMissing,
    VirtioBlockReadMiss,
    DebugOther,
}

impl Origin {
    const ALL: [Self; 3] = [
        Self::UffdMissing,
        Self::VirtioBlockReadMiss,
        Self::DebugOther,
    ];

    fn from_request(value: &str) -> Self {
        match value {
            "uffd_missing" => Self::UffdMissing,
            "virtio_blk_read_miss" => Self::VirtioBlockReadMiss,
            _ => Self::DebugOther,
        }
    }

    const fn index(self) -> usize {
        match self {
            Self::UffdMissing => 0,
            Self::VirtioBlockReadMiss => 1,
            Self::DebugOther => 2,
        }
    }

    const fn label(self) -> &'static str {
        match self {
            Self::UffdMissing => "uffd_missing",
            Self::VirtioBlockReadMiss => "virtio_blk_read_miss",
            Self::DebugOther => "debug_other",
        }
    }
}

#[derive(Debug)]
struct Histogram {
    buckets: [AtomicU64; BUCKET_COUNT],
    count: AtomicU64,
    sum_ns: AtomicU64,
}

impl Default for Histogram {
    fn default() -> Self {
        Self {
            buckets: array::from_fn(|_| AtomicU64::new(0)),
            count: AtomicU64::new(0),
            sum_ns: AtomicU64::new(0),
        }
    }
}

impl Histogram {
    fn observe_ns(&self, value: u64) {
        for (index, upper) in FINITE_BUCKET_NS.iter().enumerate() {
            if value <= *upper {
                self.buckets[index].fetch_add(1, Ordering::Relaxed);
            }
        }
        self.buckets[BUCKET_COUNT - 1].fetch_add(1, Ordering::Relaxed);
        self.count.fetch_add(1, Ordering::Relaxed);
        self.sum_ns.fetch_add(value, Ordering::Relaxed);
    }

    fn render(&self, output: &mut String, name: &str, labels: &str) {
        for (index, boundary) in FINITE_BUCKET_LABELS.iter().enumerate() {
            let _ = writeln!(
                output,
                "{name}_bucket{{{labels},le=\"{boundary}\"}} {}",
                self.buckets[index].load(Ordering::Relaxed)
            );
        }
        let _ = writeln!(
            output,
            "{name}_bucket{{{labels},le=\"+Inf\"}} {}",
            self.buckets[BUCKET_COUNT - 1].load(Ordering::Relaxed)
        );
        let count = self.count.load(Ordering::Relaxed);
        let sum = self.sum_ns.load(Ordering::Relaxed) as f64 / 1_000_000_000.0;
        let _ = writeln!(output, "{name}_sum{{{labels}}} {sum:.9}");
        let _ = writeln!(output, "{name}_count{{{labels}}} {count}");
    }
}

#[derive(Debug)]
struct DataUploadHistogram {
    buckets: [AtomicU64; DATA_UPLOAD_BUCKET_NS.len() + 1],
    count: AtomicU64,
    sum_ns: AtomicU64,
}

impl Default for DataUploadHistogram {
    fn default() -> Self {
        Self {
            buckets: array::from_fn(|_| AtomicU64::new(0)),
            count: AtomicU64::new(0),
            sum_ns: AtomicU64::new(0),
        }
    }
}

impl DataUploadHistogram {
    fn observe(&self, elapsed: Duration) {
        let value = duration_ns(elapsed);
        for (index, upper) in DATA_UPLOAD_BUCKET_NS.iter().enumerate() {
            if value <= *upper {
                self.buckets[index].fetch_add(1, Ordering::Relaxed);
            }
        }
        self.buckets[DATA_UPLOAD_BUCKET_NS.len()].fetch_add(1, Ordering::Relaxed);
        self.count.fetch_add(1, Ordering::Relaxed);
        self.sum_ns.fetch_add(value, Ordering::Relaxed);
    }

    fn render(&self, output: &mut String) {
        for (index, boundary) in DATA_UPLOAD_BUCKET_LABELS.iter().enumerate() {
            let _ = writeln!(
                output,
                "rock_snapd_data_upload_duration_seconds_bucket{{le=\"{boundary}\"}} {}",
                self.buckets[index].load(Ordering::Relaxed)
            );
        }
        let _ = writeln!(
            output,
            "rock_snapd_data_upload_duration_seconds_bucket{{le=\"+Inf\"}} {}",
            self.buckets[DATA_UPLOAD_BUCKET_NS.len()].load(Ordering::Relaxed)
        );
        let _ = writeln!(
            output,
            "rock_snapd_data_upload_duration_seconds_sum {:.9}",
            self.sum_ns.load(Ordering::Relaxed) as f64 / 1_000_000_000.0
        );
        let _ = writeln!(
            output,
            "rock_snapd_data_upload_duration_seconds_count {}",
            self.count.load(Ordering::Relaxed)
        );
    }
}

#[derive(Debug)]
pub struct SnapdMetrics {
    provider: String,
    pause: Histogram,
    remote_load: [Histogram; 2],
    remote_missing_lag: [Histogram; 2],
    provider_bytes: [AtomicU64; 2],
    local_cache_bytes: [AtomicU64; 2],
    registry_logical_bytes: [AtomicU64; 2],
    provider_errors: [AtomicU64; 2],
    provider_retries: [AtomicU64; 2],
    p2p_probe: [AtomicU64; 3],
    p2p_ranges: [AtomicU64; 5],
    p2p_fallbacks: [AtomicU64; 6],
    p2p_bytes: [AtomicU64; 2],
    p2p_source_ranges: AtomicU64,
    p2p_latency: [Histogram; 2],
    downloads: [[[AtomicU64; 4]; 3]; 2],
    operations: [[AtomicU64; 3]; 10],
    operation_latency: [Histogram; 10],
    queue_depth: [AtomicU64; 1],
    inflight: [AtomicU64; 2],
    rejections: [AtomicU64; 1],
    dirty_pages: [AtomicU64; 2],
    runtime_chain_depth: [AtomicU64; 2],
    capture_chain_depth: [AtomicU64; 2],
    fork_children: AtomicU64,
    upload_backlog: AtomicU64,
    data_upload_queued: AtomicU64,
    data_upload_inflight: AtomicU64,
    data_upload_bytes_inflight: AtomicU64,
    data_upload_bytes_total: AtomicU64,
    data_upload_attempts: [AtomicU64; 8],
    data_upload_duration: DataUploadHistogram,
    leases: [AtomicU64; 3],
    integrity_violations: AtomicU64,
    recovery: [AtomicU64; 3],
    structured_logger: OnceLock<Arc<StructuredLogger>>,
    disk_sources: OnceLock<DiskMetricSources>,
}

#[derive(Debug)]
struct DiskMetricSources {
    cache: Option<Weak<crate::snapshot::transport::posix_file::LocalByteCache>>,
    backings: Weak<crate::snapshot::io::backing::LocalBackingRegistry>,
}

impl SnapdMetrics {
    pub fn new(provider: &str) -> Result<Arc<Self>, MetricsError> {
        if provider.is_empty()
            || !provider
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.'))
        {
            return Err(MetricsError::InvalidProvider);
        }
        Ok(Arc::new(Self {
            provider: provider.into(),
            pause: Histogram::default(),
            remote_load: array::from_fn(|_| Histogram::default()),
            remote_missing_lag: array::from_fn(|_| Histogram::default()),
            provider_bytes: array::from_fn(|_| AtomicU64::new(0)),
            local_cache_bytes: array::from_fn(|_| AtomicU64::new(0)),
            registry_logical_bytes: array::from_fn(|_| AtomicU64::new(0)),
            provider_errors: array::from_fn(|_| AtomicU64::new(0)),
            provider_retries: array::from_fn(|_| AtomicU64::new(0)),
            p2p_probe: array::from_fn(|_| AtomicU64::new(0)),
            p2p_ranges: array::from_fn(|_| AtomicU64::new(0)),
            p2p_fallbacks: array::from_fn(|_| AtomicU64::new(0)),
            p2p_bytes: array::from_fn(|_| AtomicU64::new(0)),
            p2p_source_ranges: AtomicU64::new(0),
            p2p_latency: array::from_fn(|_| Histogram::default()),
            downloads: array::from_fn(|_| {
                array::from_fn(|_| array::from_fn(|_| AtomicU64::new(0)))
            }),
            operations: array::from_fn(|_| array::from_fn(|_| AtomicU64::new(0))),
            operation_latency: array::from_fn(|_| Histogram::default()),
            queue_depth: array::from_fn(|_| AtomicU64::new(0)),
            inflight: array::from_fn(|_| AtomicU64::new(0)),
            rejections: array::from_fn(|_| AtomicU64::new(0)),
            dirty_pages: array::from_fn(|_| AtomicU64::new(0)),
            runtime_chain_depth: array::from_fn(|_| AtomicU64::new(0)),
            capture_chain_depth: array::from_fn(|_| AtomicU64::new(0)),
            fork_children: AtomicU64::new(0),
            upload_backlog: AtomicU64::new(0),
            data_upload_queued: AtomicU64::new(0),
            data_upload_inflight: AtomicU64::new(0),
            data_upload_bytes_inflight: AtomicU64::new(0),
            data_upload_bytes_total: AtomicU64::new(0),
            data_upload_attempts: array::from_fn(|_| AtomicU64::new(0)),
            data_upload_duration: DataUploadHistogram::default(),
            leases: array::from_fn(|_| AtomicU64::new(0)),
            integrity_violations: AtomicU64::new(0),
            recovery: array::from_fn(|_| AtomicU64::new(0)),
            structured_logger: OnceLock::new(),
            disk_sources: OnceLock::new(),
        }))
    }

    pub(crate) fn install_disk_sources(
        &self,
        cache: Option<&Arc<crate::snapshot::transport::posix_file::LocalByteCache>>,
        backings: &Arc<crate::snapshot::io::backing::LocalBackingRegistry>,
    ) {
        self.disk_sources.get_or_init(|| DiskMetricSources {
            cache: cache.map(Arc::downgrade),
            backings: Arc::downgrade(backings),
        });
    }

    fn render_disk_usage(&self, output: &mut String) {
        let Some(sources) = self.disk_sources.get() else {
            return;
        };
        let mut cache_error = false;
        if let Some(cache) = &sources.cache {
            match cache.upgrade().map(|cache| cache.stats()) {
                Some(Ok(stats)) => {
                    for (name, value) in [
                        ("limit_bytes", stats.max_bytes),
                        ("allocated_bytes", stats.used_bytes),
                        ("pinned_bytes", stats.pinned_bytes),
                        ("staging_bytes", stats.staging_bytes),
                        ("entries", stats.entries),
                    ] {
                        let _ = writeln!(output, "# TYPE rock_snapd_disk_cache_{name} gauge");
                        let _ = writeln!(output, "rock_snapd_disk_cache_{name} {value}");
                    }
                    for (name, value) in
                        [("evictions", stats.evictions), ("bypasses", stats.bypasses)]
                    {
                        let _ =
                            writeln!(output, "# TYPE rock_snapd_disk_cache_{name}_total counter");
                        let _ = writeln!(output, "rock_snapd_disk_cache_{name}_total {value}");
                    }
                }
                _ => cache_error = true,
            }
        }
        let backing_error = match sources.backings.upgrade().map(|backings| backings.stats()) {
            Some(Ok(stats)) => {
                for (name, value) in [
                    ("allocated_bytes", stats.allocated_bytes),
                    ("files", stats.files),
                ] {
                    let _ = writeln!(output, "# TYPE rock_snapd_backing_{name} gauge");
                    let _ = writeln!(
                        output,
                        "rock_snapd_backing_{name}{{scope=\"shared_readonly\"}} {value}"
                    );
                }
                false
            }
            _ => true,
        };
        output.push_str("# TYPE rock_snapd_disk_accounting_errors gauge\n");
        for (scope, failed) in [("cache", cache_error), ("shared_readonly", backing_error)] {
            let _ = writeln!(
                output,
                "rock_snapd_disk_accounting_errors{{scope=\"{scope}\"}} {}",
                u8::from(failed)
            );
        }
    }

    pub fn install_structured_logger(
        &self,
        logger: Arc<StructuredLogger>,
    ) -> Result<(), MetricsError> {
        self.structured_logger
            .set(logger)
            .map_err(|_| MetricsError::LoggerAlreadyInstalled)
    }

    pub fn emit_structured(&self, event: StructuredEvent) {
        if let Some(logger) = self.structured_logger.get() {
            let _ = logger.emit(&event);
        }
    }

    pub fn observe_pause_ns(&self, value: u64) {
        self.pause.observe_ns(value);
    }

    pub fn observe_remote_load(&self, kind: DataKind, elapsed: Duration) {
        self.remote_load[kind.index()].observe_ns(duration_ns(elapsed));
    }

    pub fn observe_remote_missing_lag(&self, kind: DataKind, elapsed: Duration) {
        self.remote_missing_lag[kind.index()].observe_ns(duration_ns(elapsed));
    }

    pub fn observe_download(&self, kind: DataKind, origin: &str, outcome: DownloadOutcomeMetric) {
        let origin = Origin::from_request(origin);
        self.downloads[kind.index()][origin.index()][outcome.index()]
            .fetch_add(1, Ordering::Relaxed);
    }

    pub fn add_provider_bytes(&self, kind: DataKind, value: u64) {
        self.provider_bytes[kind.index()].fetch_add(value, Ordering::Relaxed);
    }

    pub(crate) fn add_registry_logical_bytes(&self, kind: DataKind, value: u64) {
        self.registry_logical_bytes[kind.index()].fetch_add(value, Ordering::Relaxed);
    }

    pub(crate) fn add_local_cache_bytes(&self, kind: DataKind, value: u64) {
        self.local_cache_bytes[kind.index()].fetch_add(value, Ordering::Relaxed);
    }

    pub fn add_provider_error(&self, kind: DataKind) {
        self.provider_errors[kind.index()].fetch_add(1, Ordering::Relaxed);
    }

    pub fn add_provider_retry(&self, kind: DataKind) {
        self.provider_retries[kind.index()].fetch_add(1, Ordering::Relaxed);
    }

    pub fn add_p2p_probe(&self, outcome: P2PProbeOutcome) {
        self.p2p_probe[outcome.index()].fetch_add(1, Ordering::Relaxed);
    }

    pub fn observe_p2p_range(&self, kind: DataKind, outcome: P2PRangeOutcome, elapsed: Duration) {
        self.p2p_ranges[outcome.index()].fetch_add(1, Ordering::Relaxed);
        self.p2p_latency[kind.index()].observe_ns(duration_ns(elapsed));
    }

    pub fn add_p2p_fallback(&self, reason: P2PFallbackReason) {
        self.p2p_fallbacks[reason.index()].fetch_add(1, Ordering::Relaxed);
    }

    pub fn add_p2p_bytes(&self, kind: DataKind, value: u64) {
        self.p2p_bytes[kind.index()].fetch_add(value, Ordering::Relaxed);
    }

    /// Successful source reads, without allocating per-range log metadata.
    pub fn add_p2p_source_range(&self) {
        self.p2p_source_ranges.fetch_add(1, Ordering::Relaxed);
    }

    pub fn observe_operation(
        &self,
        operation: OperationMetric,
        outcome: OperationOutcome,
        elapsed: Duration,
    ) {
        self.operations[operation.index()][outcome.index()].fetch_add(1, Ordering::Relaxed);
        self.operation_latency[operation.index()].observe_ns(duration_ns(elapsed));
    }

    pub fn set_queue_depth(&self, component: QueueComponent, value: u64) {
        self.queue_depth[component.index()].store(value, Ordering::Relaxed);
    }

    pub fn add_queue_depth(&self, component: QueueComponent, delta: i64) {
        adjust_gauge(&self.queue_depth[component.index()], delta);
    }

    pub fn add_inflight(&self, kind: DataKind, delta: i64) {
        adjust_gauge(&self.inflight[kind.index()], delta);
    }

    pub fn add_rejection(&self, component: QueueComponent) {
        self.rejections[component.index()].fetch_add(1, Ordering::Relaxed);
    }

    pub fn set_dirty_pages(&self, kind: DataKind, value: u64) {
        self.dirty_pages[kind.index()].store(value, Ordering::Relaxed);
    }

    pub fn set_chain_depth(&self, kind: DataKind, value: u64) {
        self.capture_chain_depth[kind.index()].store(value, Ordering::Relaxed);
    }

    pub fn set_runtime_chain_depth(&self, kind: DataKind, value: u64) {
        self.runtime_chain_depth[kind.index()].store(value, Ordering::Relaxed);
    }

    pub fn set_fork_children(&self, value: u64) {
        self.fork_children.store(value, Ordering::Relaxed);
    }

    pub fn set_upload_backlog(&self, value: u64) {
        self.upload_backlog.store(value, Ordering::Relaxed);
        self.set_queue_depth(QueueComponent::Upload, value);
    }

    pub fn data_upload_admitted(&self, length: u64) {
        self.data_upload_queued.fetch_add(1, Ordering::Relaxed);
        self.data_upload_bytes_inflight
            .fetch_add(length, Ordering::Relaxed);
    }

    pub fn data_upload_started(&self) {
        self.data_upload_queued.fetch_sub(1, Ordering::Relaxed);
        self.data_upload_inflight.fetch_add(1, Ordering::Relaxed);
    }

    pub fn data_upload_finished(
        &self,
        length: u64,
        started: bool,
        outcome: DataUploadOutcome,
        elapsed: Duration,
    ) {
        if started {
            self.data_upload_inflight.fetch_sub(1, Ordering::Relaxed);
        } else {
            self.data_upload_queued.fetch_sub(1, Ordering::Relaxed);
        }
        self.data_upload_bytes_inflight
            .fetch_sub(length, Ordering::Relaxed);
        if outcome == DataUploadOutcome::Success {
            self.data_upload_bytes_total
                .fetch_add(length, Ordering::Relaxed);
        }
        self.data_upload_attempts[outcome.index()].fetch_add(1, Ordering::Relaxed);
        self.data_upload_duration.observe(elapsed);
    }

    pub fn record_data_upload_rejected(&self, outcome: DataUploadOutcome) {
        self.data_upload_attempts[outcome.index()].fetch_add(1, Ordering::Relaxed);
    }

    pub fn add_lease(&self, kind: LeaseKind, delta: i64) {
        adjust_gauge(&self.leases[kind.index()], delta);
    }

    pub fn add_integrity_violation(&self) {
        self.integrity_violations.fetch_add(1, Ordering::Relaxed);
    }

    pub fn add_recovery(&self, state: RecoveryState, delta: i64) {
        adjust_gauge(&self.recovery[state.index()], delta);
    }

    pub fn render(&self, adoption_pending: bool) -> String {
        let mut output = String::with_capacity(16 * 1024);
        self.render_disk_usage(&mut output);
        output.push_str("# TYPE rock_snapd_build_info gauge\n");
        output.push_str("rock_snapd_build_info{implementation=\"rust\"} 1\n");
        output.push_str("# TYPE rock_snapd_adoption_pending gauge\n");
        let _ = writeln!(
            output,
            "rock_snapd_adoption_pending {}",
            usize::from(adoption_pending)
        );
        output.push_str("# TYPE rock_snapd_pause_seconds histogram\n");
        self.pause.render(
            &mut output,
            "rock_snapd_pause_seconds",
            "operation=\"snapshot_sandbox\"",
        );
        output.push_str("# TYPE rock_snapd_remote_load_latency_seconds histogram\n");
        output.push_str("# TYPE rock_snapd_remote_missing_loop_lag_seconds histogram\n");
        output.push_str("# TYPE rock_snapd_provider_bytes_total counter\n");
        output.push_str("# TYPE rock_snapd_local_cache_bytes_total counter\n");
        output.push_str("# TYPE rock_snapd_provider_errors_total counter\n");
        output.push_str("# TYPE rock_snapd_provider_retries_total counter\n");
        output.push_str("# TYPE rock_snapd_download_total counter\n");
        output.push_str("# TYPE rock_snapd_p2p_probe_total counter\n");
        output.push_str("# TYPE rock_snapd_p2p_range_requests_total counter\n");
        output.push_str("# TYPE rock_snapd_p2p_bytes_total counter\n");
        output.push_str("# TYPE rock_snapd_p2p_source_ranges_total counter\n");
        let _ = writeln!(
            output,
            "rock_snapd_p2p_source_ranges_total {}",
            self.p2p_source_ranges.load(Ordering::Relaxed)
        );
        output.push_str("# TYPE rock_snapd_p2p_fallback_total counter\n");
        output.push_str("# TYPE rock_snapd_p2p_range_latency_seconds histogram\n");
        for kind in DataKind::ALL {
            let common = format!("kind=\"{}\",provider=\"{}\"", kind.label(), self.provider);
            self.remote_load[kind.index()].render(
                &mut output,
                "rock_snapd_remote_load_latency_seconds",
                &common,
            );
            self.remote_missing_lag[kind.index()].render(
                &mut output,
                "rock_snapd_remote_missing_loop_lag_seconds",
                &format!("kind=\"{}\"", kind.label()),
            );
            let _ = writeln!(
                output,
                "rock_snapd_provider_bytes_total{{{common}}} {}",
                self.provider_bytes[kind.index()].load(Ordering::Relaxed)
            );
            let _ = writeln!(
                output,
                "rock_snapd_local_cache_bytes_total{{kind=\"{}\"}} {}",
                kind.label(),
                self.local_cache_bytes[kind.index()].load(Ordering::Relaxed)
            );
            let _ = writeln!(
                output,
                "rock_snapd_provider_errors_total{{{common}}} {}",
                self.provider_errors[kind.index()].load(Ordering::Relaxed)
            );
            let _ = writeln!(
                output,
                "rock_snapd_provider_retries_total{{{common}}} {}",
                self.provider_retries[kind.index()].load(Ordering::Relaxed)
            );
            let _ = writeln!(
                output,
                "rock_snapd_p2p_bytes_total{{kind=\"{}\"}} {}",
                kind.label(),
                self.p2p_bytes[kind.index()].load(Ordering::Relaxed)
            );
            self.p2p_latency[kind.index()].render(
                &mut output,
                "rock_snapd_p2p_range_latency_seconds",
                &format!("kind=\"{}\"", kind.label()),
            );
            for origin in Origin::ALL {
                for outcome in DownloadOutcomeMetric::ALL {
                    let value = self.downloads[kind.index()][origin.index()][outcome.index()]
                        .load(Ordering::Relaxed);
                    let _ = writeln!(
                        output,
                        "rock_snapd_download_total{{kind=\"{}\",origin=\"{}\",outcome=\"{}\"}} {value}",
                        kind.label(),
                        origin.label(),
                        outcome.label(),
                    );
                }
            }
        }
        for outcome in P2PProbeOutcome::ALL {
            let _ = writeln!(
                output,
                "rock_snapd_p2p_probe_total{{outcome=\"{}\"}} {}",
                outcome.label(),
                self.p2p_probe[outcome.index()].load(Ordering::Relaxed)
            );
        }
        for outcome in P2PRangeOutcome::ALL {
            let _ = writeln!(
                output,
                "rock_snapd_p2p_range_requests_total{{outcome=\"{}\"}} {}",
                outcome.label(),
                self.p2p_ranges[outcome.index()].load(Ordering::Relaxed)
            );
        }
        for reason in P2PFallbackReason::ALL {
            let _ = writeln!(
                output,
                "rock_snapd_p2p_fallback_total{{reason=\"{}\"}} {}",
                reason.label(),
                self.p2p_fallbacks[reason.index()].load(Ordering::Relaxed)
            );
        }
        output.push_str("# TYPE rock_snapd_operations_total counter\n");
        output.push_str("# TYPE rock_snapd_operation_latency_seconds histogram\n");
        for operation in OperationMetric::ALL {
            for outcome in OperationOutcome::ALL {
                let _ = writeln!(
                    output,
                    "rock_snapd_operations_total{{operation=\"{}\",outcome=\"{}\"}} {}",
                    operation.label(),
                    outcome.label(),
                    self.operations[operation.index()][outcome.index()].load(Ordering::Relaxed),
                );
            }
            self.operation_latency[operation.index()].render(
                &mut output,
                "rock_snapd_operation_latency_seconds",
                &format!("operation=\"{}\"", operation.label()),
            );
        }
        output.push_str("# TYPE rock_snapd_queue_depth gauge\n");
        output.push_str("# TYPE rock_snapd_rejections_total counter\n");
        for component in QueueComponent::ALL {
            let _ = writeln!(
                output,
                "rock_snapd_queue_depth{{component=\"{}\"}} {}",
                component.label(),
                self.queue_depth[component.index()].load(Ordering::Relaxed),
            );
            let _ = writeln!(
                output,
                "rock_snapd_rejections_total{{component=\"{}\"}} {}",
                component.label(),
                self.rejections[component.index()].load(Ordering::Relaxed),
            );
        }
        output.push_str("# TYPE rock_snapd_inflight gauge\n");
        output.push_str("# TYPE rock_snapd_dirty_pages gauge\n");
        output.push_str("# TYPE rock_snapd_chain_depth gauge\n");
        for kind in DataKind::ALL {
            let index = kind.index();
            let _ = writeln!(
                output,
                "rock_snapd_inflight{{operation=\"remote_load\",kind=\"{}\"}} {}",
                kind.label(),
                self.inflight[index].load(Ordering::Relaxed),
            );
            for (name, value) in [
                (
                    "rock_snapd_dirty_pages",
                    self.dirty_pages[index].load(Ordering::Relaxed),
                ),
                (
                    "rock_snapd_chain_depth",
                    self.runtime_chain_depth[index]
                        .load(Ordering::Relaxed)
                        .max(self.capture_chain_depth[index].load(Ordering::Relaxed)),
                ),
            ] {
                let _ = writeln!(output, "{name}{{kind=\"{}\"}} {value}", kind.label());
            }
        }
        output.push_str("# TYPE rock_snapd_fork_children gauge\n");
        output.push_str("# TYPE rock_snapd_upload_backlog gauge\n");
        output.push_str("# TYPE rock_snapd_leases gauge\n");
        output.push_str("# TYPE rock_snapd_integrity_violations_total counter\n");
        output.push_str("# TYPE rock_snapd_recovery gauge\n");
        let _ = writeln!(
            output,
            "rock_snapd_fork_children {}",
            self.fork_children.load(Ordering::Relaxed)
        );
        let _ = writeln!(
            output,
            "rock_snapd_upload_backlog {}",
            self.upload_backlog.load(Ordering::Relaxed)
        );
        output.push_str("# TYPE rock_snapd_data_upload_queued gauge\n");
        output.push_str("# TYPE rock_snapd_data_upload_inflight gauge\n");
        output.push_str("# TYPE rock_snapd_data_upload_bytes_inflight gauge\n");
        output.push_str("# TYPE rock_snapd_data_upload_bytes_total counter\n");
        output.push_str("# TYPE rock_snapd_data_upload_attempts_total counter\n");
        output.push_str("# TYPE rock_snapd_data_upload_duration_seconds histogram\n");
        let _ = writeln!(
            output,
            "rock_snapd_data_upload_queued {}",
            self.data_upload_queued.load(Ordering::Relaxed)
        );
        let _ = writeln!(
            output,
            "rock_snapd_data_upload_inflight {}",
            self.data_upload_inflight.load(Ordering::Relaxed)
        );
        let _ = writeln!(
            output,
            "rock_snapd_data_upload_bytes_inflight {}",
            self.data_upload_bytes_inflight.load(Ordering::Relaxed)
        );
        let _ = writeln!(
            output,
            "rock_snapd_data_upload_bytes_total {}",
            self.data_upload_bytes_total.load(Ordering::Relaxed)
        );
        for outcome in DataUploadOutcome::ALL {
            let _ = writeln!(
                output,
                "rock_snapd_data_upload_attempts_total{{outcome=\"{}\"}} {}",
                outcome.label(),
                self.data_upload_attempts[outcome.index()].load(Ordering::Relaxed)
            );
        }
        self.data_upload_duration.render(&mut output);
        crate::observability::read_path::render(&mut output);
        let registry = crate::snapshot::transport::registry::transfer_stats();
        for (name, value) in [
            ("requests", registry.requests),
            ("bytes", registry.bytes),
            ("range_requests", registry.range_requests),
            ("range_bytes", registry.range_bytes),
        ] {
            let _ = writeln!(output, "# TYPE rock_snapd_registry_{name}_total counter");
            let _ = writeln!(output, "rock_snapd_registry_{name}_total {value}");
        }
        output.push_str("# TYPE rock_snapd_registry_logical_bytes_total counter\n");
        for kind in DataKind::ALL {
            let _ = writeln!(
                output,
                "rock_snapd_registry_logical_bytes_total{{kind=\"{}\"}} {}",
                kind.label(),
                self.registry_logical_bytes[kind.index()].load(Ordering::Relaxed)
            );
        }
        for kind in LeaseKind::ALL {
            let _ = writeln!(
                output,
                "rock_snapd_leases{{kind=\"{}\"}} {}",
                kind.label(),
                self.leases[kind.index()].load(Ordering::Relaxed),
            );
        }
        let _ = writeln!(
            output,
            "rock_snapd_integrity_violations_total {}",
            self.integrity_violations.load(Ordering::Relaxed)
        );
        for state in RecoveryState::ALL {
            let _ = writeln!(
                output,
                "rock_snapd_recovery{{state=\"{}\"}} {}",
                state.label(),
                self.recovery[state.index()].load(Ordering::Relaxed),
            );
        }
        output
    }
}

fn adjust_gauge(value: &AtomicU64, delta: i64) {
    let _ = value.fetch_update(Ordering::Relaxed, Ordering::Relaxed, |current| {
        if delta >= 0 {
            Some(current.saturating_add(delta as u64))
        } else {
            Some(current.saturating_sub(delta.unsigned_abs()))
        }
    });
}

fn duration_ns(value: Duration) -> u64 {
    value.as_nanos().try_into().unwrap_or(u64::MAX)
}
