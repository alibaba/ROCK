//! Snapshot prefetch policies and execution: attach-time memory, learned hotsets,
//! and post-readiness hydration. All policies use the authorized range loader.
//! Existing admission limits, optional-metadata bounds and wait semantics stay
//! with their policies; speculative reads never replace demand-load validation.

use std::collections::{HashMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, Mutex as StdMutex, OnceLock};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use serde::Serialize;
use sha2::{Digest, Sha256};
use thiserror::Error;
use tokio::sync::{watch, Semaphore};
use tokio::task::JoinSet;

use crate::observability::metrics::SnapdMetrics;
use crate::observability::structured_log::{
    StructuredComponent, StructuredEvent, StructuredEventName, StructuredLogLevel,
    StructuredOutcome,
};
use crate::snapshot::format::image_metadata::{decode_local_hotset, encode_local_hotset};
use crate::snapshot::io::backing::{DataRangeKey, DownloadState, LocalBacking};
use crate::snapshot::metadata::{
    merge_snapshot_hot_ranges, LayerKind as SnapshotLayerKind, RangeKind, SnapshotHotRange,
    SnapshotHotset, SnapshotImageMetadata, SnapshotLayerView, SnapshotMetadataSubject,
    MAX_SNAPSHOT_HOTSET_DECODED_BYTES, MAX_SNAPSHOT_HOTSET_RANGES,
    MAX_SNAPSHOT_IMAGE_METADATA_BYTES,
};
use crate::snapshot::services::ondemand_loader::{
    exact_layer_grant, grant_from_layer_range, remote_load_error_code, target_backing,
    GrantedRange, IndexedRangeGrant, RangeLoadExecutor, RemoteLoadAdmissionController,
    RemoteLoadAdmissionObservation, RemoteLoadAdmissionRun, RemoteLoadError, RemoteLoadRequest,
};
use crate::snapshot::transport::{RangeReadError, RangeReadFailureClass};
use crate::snapshot::{SnapshotImageMetadataReader, SnapshotImageMetadataUploader};
use crate::RuntimeIdentity;

// Post-readiness hydration and demand-read priority.
#[derive(Clone, Debug, Default, Serialize)]
pub struct BackgroundPrefetchStatus {
    pub state: &'static str,
    pub total_ranges: u64,
    pub resident_ranges: u64,
    pub total_bytes: u64,
    pub resident_bytes: u64,
    pub retries: u64,
    pub last_error_code: Option<&'static str>,
}

struct Job {
    cancelled: AtomicBool,
    status: StdMutex<BackgroundPrefetchStatus>,
}

pub(in crate::snapshot) struct BackgroundPrefetch {
    enabled: bool,
    bytes_per_second: u64,
    min_free_bytes: u64,
    admission: Arc<RemoteLoadAdmissionController>,
    // One node-global lane: demand bypasses it; rate pacing holds the lane.
    jobs: StdMutex<HashMap<RuntimeIdentity, Arc<Job>>>,
}

pub(in crate::snapshot) struct DemandGuard(Arc<RangeLoadExecutor>);
impl DemandGuard {
    pub(in crate::snapshot) fn new(service: Arc<RangeLoadExecutor>) -> Self {
        service.demand_loads.fetch_add(1, Ordering::AcqRel);
        Self(service)
    }
}
impl Drop for DemandGuard {
    fn drop(&mut self) {
        self.0.demand_loads.fetch_sub(1, Ordering::AcqRel);
    }
}

impl BackgroundPrefetch {
    pub(in crate::snapshot) fn from_env() -> Result<Self, RemoteLoadError> {
        fn number(name: &str, default: u64, max: u64) -> Result<u64, RemoteLoadError> {
            let value = match std::env::var(name) {
                Ok(value) => value.parse::<u64>().map_err(|_| {
                    RemoteLoadError::InvalidArgument(format!("{name} must be an integer"))
                })?,
                Err(std::env::VarError::NotPresent) => default,
                Err(_) => {
                    return Err(RemoteLoadError::InvalidArgument(format!(
                        "{name} must be UTF-8"
                    )))
                }
            };
            if value > max {
                return Err(RemoteLoadError::InvalidArgument(format!(
                    "{name} exceeds {max}"
                )));
            }
            Ok(value)
        }
        let enabled = number("ROCK_SNAPSHOT_BACKGROUND_PREFETCH", 1, 1)? == 1;
        let bytes_per_second = number(
            "ROCK_SNAPSHOT_BACKGROUND_PREFETCH_BYTES_PER_SECOND",
            16 * 1024 * 1024,
            1024 * 1024 * 1024,
        )?;
        if bytes_per_second == 0 {
            return Err(RemoteLoadError::InvalidArgument(
                "background prefetch rate must be positive".into(),
            ));
        }
        Ok(Self {
            enabled,
            bytes_per_second,
            min_free_bytes: number(
                "ROCK_SNAPSHOT_BACKGROUND_PREFETCH_MIN_FREE_BYTES",
                512 * 1024 * 1024,
                u64::MAX,
            )?,
            admission: RemoteLoadAdmissionController::new(1),
            jobs: StdMutex::new(HashMap::new()),
        })
    }

    pub(in crate::snapshot) fn cancel(&self, runtime: &RuntimeIdentity) {
        if let Some(job) = self
            .jobs
            .lock()
            .expect("background jobs lock")
            .remove(runtime)
        {
            job.cancelled.store(true, Ordering::Release);
        }
    }

    pub(in crate::snapshot) fn cancel_all(&self) {
        for job in self.jobs.lock().expect("background jobs lock").values() {
            job.cancelled.store(true, Ordering::Release);
        }
    }
}

fn space_available(root: &std::path::Path, needed: u64, reserve: u64) -> bool {
    let Ok(file) = File::open(root) else {
        return false;
    };
    let mut stat = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    // SAFETY: file owns a valid descriptor and stat points to writable storage.
    if unsafe { libc::fstatvfs(file.as_raw_fd(), stat.as_mut_ptr()) } != 0 {
        return false;
    }
    // SAFETY: a successful fstatvfs initialized the structure.
    let stat = unsafe { stat.assume_init() };
    (stat.f_bavail as u64).saturating_mul(stat.f_frsize as u64) >= needed.saturating_add(reserve)
}

impl RangeLoadExecutor {
    pub fn background_prefetch_status(
        &self,
        runtime: &RuntimeIdentity,
    ) -> Option<BackgroundPrefetchStatus> {
        self.background
            .jobs
            .lock()
            .expect("background jobs lock")
            .get(runtime)
            .map(|job| job.status.lock().expect("background status lock").clone())
    }

    pub async fn launch_background_prefetch(self: &Arc<Self>, runtime: &RuntimeIdentity) -> bool {
        if !self.background.enabled {
            return false;
        }
        let state = self.state.lock().await;
        if state.closed || state.revoked_runtimes.contains(runtime) {
            return false;
        }
        let job = {
            let mut jobs = self.background.jobs.lock().expect("background jobs lock");
            if jobs.contains_key(runtime) {
                return false;
            }
            let job = Arc::new(Job {
                cancelled: AtomicBool::new(false),
                status: StdMutex::new(BackgroundPrefetchStatus {
                    state: "running".into(),
                    ..Default::default()
                }),
            });
            jobs.insert(runtime.clone(), Arc::clone(&job));
            job
        };
        drop(state);
        let service = Arc::clone(self);
        let runtime = runtime.clone();
        tokio::spawn(async move {
            service.run_background_prefetch(&runtime, &job).await;
        });
        true
    }

    fn emit_background(&self, runtime: &RuntimeIdentity, job: &Job) {
        let status = job.status.lock().expect("background status lock").clone();
        let mut event = StructuredEvent::new(
            if status.state == "failed" {
                StructuredLogLevel::Error
            } else {
                StructuredLogLevel::Info
            },
            StructuredEventName::RuntimeStartStageCompleted,
            StructuredComponent::RemoteLoad,
            if status.state == "failed" {
                StructuredOutcome::Error
            } else {
                StructuredOutcome::Success
            },
        )
        .operation("background_prefetch")
        .phase(status.state)
        .sandbox_id(&runtime.sandbox_id)
        .graph_counts(0, status.total_ranges, status.resident_ranges, 0)
        .range(status.resident_bytes, status.total_bytes);
        if let Some(code) = status.last_error_code {
            event = event.error_code(code);
        }
        self.metrics.emit_structured(event);
    }

    async fn run_background_prefetch(self: &Arc<Self>, runtime: &RuntimeIdentity, job: &Job) {
        self.emit_background(runtime, job);
        let mut retry_delay = 1_u64;
        loop {
            let (layers, explicit) = {
                let state = self.state.lock().await;
                if state.closed
                    || state.revoked_runtimes.contains(runtime)
                    || job.cancelled.load(Ordering::Acquire)
                {
                    break;
                }
                let layers = state
                    .live_layers()
                    .filter_map(|(id, layer)| {
                        if &layer.remote_identity().runtime != runtime
                            || layer.revoked.load(Ordering::Acquire)
                        {
                            return None;
                        }
                        let map = layer
                            .range_map
                            .lock()
                            .expect("layer capability lock")
                            .clone()?;
                        // External registry roots must remain demand loaded
                        // after readiness. Background hydration is for snapd's
                        // own immutable artifacts, not a whole registry pull.
                        if !self
                            .snapshot_reader
                            .open_layer(Arc::clone(&map))
                            .allows_background_prefetch()
                        {
                            return None;
                        }
                        Some((id.clone(), Arc::clone(&layer), map))
                    })
                    .collect::<Vec<_>>();
                let explicit = state
                    .targets
                    .values()
                    .filter_map(IndexedRangeGrant::upgrade)
                    .filter(|range| {
                        &range.grant.runtime == runtime
                            && range.grant.source.codec != "registry-rootfs-v1"
                            && range
                                .layer_target
                                .range_map
                                .lock()
                                .expect("layer capability lock")
                                .is_none()
                    })
                    .collect::<Vec<_>>();
                (layers, explicit)
            };
            // Share immutable maps; never materialize one owned grant per page.
            let total_ranges = explicit.len() as u64
                + layers
                    .iter()
                    .map(|(_, _, map)| {
                        map.ranges
                            .iter()
                            .filter(|r| r.kind == RangeKind::Data)
                            .count() as u64
                    })
                    .sum::<u64>();
            let total_bytes = explicit.iter().map(|g| g.grant.length).sum::<u64>()
                + layers
                    .iter()
                    .map(|(_, _, map)| {
                        map.ranges
                            .iter()
                            .filter(|r| r.kind == RangeKind::Data)
                            .map(|r| r.length)
                            .sum::<u64>()
                    })
                    .sum::<u64>();
            {
                let mut status = job.status.lock().expect("background status lock");
                status.state = "running".into();
                status.total_ranges = total_ranges;
                status.total_bytes = total_bytes;
                status.resident_ranges = 0;
                status.resident_bytes = 0;
            }
            let mut failed = false;
            for target in explicit {
                if !self.prefetch_one(runtime, job, target).await {
                    failed = true;
                    break;
                }
                if job.cancelled.load(Ordering::Acquire)
                    || job.status.lock().expect("background status lock").state == "space_paused"
                {
                    break;
                }
            }
            'layers: for (id, layer, map) in layers {
                if failed {
                    break;
                }
                for range in map.ranges.iter().filter(|r| r.kind == RangeKind::Data) {
                    if job.cancelled.load(Ordering::Acquire)
                        || layer.revoked.load(Ordering::Acquire)
                    {
                        break;
                    }
                    let Ok(grant) = grant_from_layer_range(&id, &layer, range) else {
                        failed = true;
                        continue;
                    };
                    if !self
                        .prefetch_one(
                            runtime,
                            job,
                            GrantedRange {
                                grant,
                                layer_target: Arc::clone(&layer),
                            },
                        )
                        .await
                    {
                        failed = true;
                        break 'layers;
                    }
                    if job.status.lock().expect("background status lock").state == "space_paused" {
                        break 'layers;
                    }
                }
                if job.cancelled.load(Ordering::Acquire) {
                    break;
                }
            }
            if job.cancelled.load(Ordering::Acquire) {
                break;
            }
            if job.status.lock().expect("background status lock").state == "failed" {
                self.emit_background(runtime, job);
                return;
            }
            let complete = {
                let mut status = job.status.lock().expect("background status lock");
                let complete = !failed && status.resident_ranges == status.total_ranges;
                if status.state != "space_paused" {
                    status.state = if complete { "complete" } else { "retrying" };
                }
                if !complete {
                    status.retries += 1;
                }
                complete
            };
            self.emit_background(runtime, job);
            if complete {
                return;
            }
            for _ in 0..retry_delay {
                if job.cancelled.load(Ordering::Acquire) {
                    break;
                }
                tokio::time::sleep(Duration::from_secs(1)).await;
            }
            retry_delay = (retry_delay * 2).min(30);
        }
        job.status.lock().expect("background status lock").state = "cancelled".into();
        self.emit_background(runtime, job);
    }

    async fn prefetch_one(
        self: &Arc<Self>,
        runtime: &RuntimeIdentity,
        job: &Job,
        target: GrantedRange,
    ) -> bool {
        let Some(backing) = target_backing(&target.layer_target) else {
            return false;
        };
        let Ok(key) = DataRangeKey::new(
            backing.backing_id.clone(),
            target.grant.target_offset,
            target.grant.length,
        ) else {
            return false;
        };
        if backing.downloads.status(&key).await.state != DownloadState::Resident {
            while self.demand_loads.load(Ordering::Acquire) != 0 {
                if job.cancelled.load(Ordering::Acquire)
                    || target.layer_target.revoked.load(Ordering::Acquire)
                {
                    return false;
                }
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
            let coalesced = self.coalesced_range_with_policy(&target, true).await;
            let bytes = coalesced.length();
            let (run, mut guard) = self.background.admission.begin_run(
                Arc::clone(&self.metrics),
                "background",
                &target.grant.backing_key.content_sha256,
                runtime,
            );
            let Ok(admission) = self.background.admission.acquire(run).await else {
                return false;
            };
            if job.cancelled.load(Ordering::Acquire)
                || target.layer_target.revoked.load(Ordering::Acquire)
            {
                return false;
            }
            if self.demand_loads.load(Ordering::Acquire) != 0 {
                return false;
            }
            if !space_available(
                self.backing_registry.cache_root(),
                bytes,
                self.background.min_free_bytes,
            ) {
                job.status.lock().expect("background status lock").state = "space_paused".into();
                return false;
            }
            // Charge the worst-case coalesced bytes before issuing a read. The
            // node-global permit bounds both I/O and sustained background rate.
            tokio::time::sleep(Duration::from_secs_f64(
                bytes as f64 / self.background.bytes_per_second as f64,
            ))
            .await;
            if job.cancelled.load(Ordering::Acquire)
                || target.layer_target.revoked.load(Ordering::Acquire)
            {
                return false;
            }
            if self.demand_loads.load(Ordering::Acquire) != 0 {
                return false;
            }
            let request = RemoteLoadRequest {
                target_id: target.grant.target_id.clone(),
                target_offset: target.grant.target_offset,
                length: target.grant.length,
                origin: "background_prefetch".into(),
            };
            let result = self
                .load_range_with_admission(runtime, request, admission)
                .await;
            guard.returned();
            if let Err(error) = result {
                let code = remote_load_error_code(&error);
                let mut status = job.status.lock().expect("background status lock");
                status.last_error_code = Some(code);
                // A missing object may still be uploading; retry it. Never
                // retry an immutable identity/integrity or authorization error.
                if matches!(
                    code,
                    "invalid_argument"
                        | "identity_conflict"
                        | "provider_not_configured"
                        | "provider_authorization"
                        | "provider_integrity"
                        | "provider_permanent"
                        | "provider_invalid_range"
                        | "checksum_mismatch"
                        | "unsupported_codec"
                        | "layer_error"
                        | "range_error"
                ) {
                    status.state = "failed";
                }
                return false;
            }
        }
        let mut status = job.status.lock().expect("background status lock");
        status.resident_ranges += 1;
        status.resident_bytes += target.grant.length;
        true
    }
}

// Bounded attach-time loading of the latest memory layer.
#[derive(Debug, Error)]
pub enum AttachPrefetchError {
    #[error("Invalid remote snapshot runtime: {0}")]
    Invalid(String),
    #[error(transparent)]
    RemoteLoad(#[from] RemoteLoadError),
}

/// Compile-time safety envelope for attach-time prefetch. Runtime configuration
/// can only tighten this byte bound and cannot increase the number of queued
/// immutable DATA capabilities.
pub const LATEST_MEMORY_PREFETCH_MAX_BYTES: u64 = 16 * 1024 * 1024;
pub const LATEST_MEMORY_PREFETCH_MAX_DATA_RANGES: usize = 4 * 1024;
pub const LATEST_MEMORY_PREFETCH_MAX_OBJECTS: usize = 2 * 1024;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LatestMemoryPrefetchConfig {
    pub max_encoded_bytes: u64,
    pub concurrency: usize,
}

impl Default for LatestMemoryPrefetchConfig {
    fn default() -> Self {
        Self {
            max_encoded_bytes: LATEST_MEMORY_PREFETCH_MAX_BYTES,
            // Keep the optimization off by default while the same image is
            // evaluated with the explicit 0/1/4/8 A/B matrix.
            concurrency: 0,
        }
    }
}

impl LatestMemoryPrefetchConfig {
    pub fn validate(self) -> Result<Self, AttachPrefetchError> {
        if self.max_encoded_bytes > LATEST_MEMORY_PREFETCH_MAX_BYTES {
            return Err(AttachPrefetchError::Invalid(format!(
                "latest-memory prefetch byte limit exceeds {}",
                LATEST_MEMORY_PREFETCH_MAX_BYTES
            )));
        }
        if !matches!(self.concurrency, 0 | 1 | 4 | 8) {
            return Err(AttachPrefetchError::Invalid(
                "latest-memory prefetch concurrency must be one of 0, 1, 4 or 8".into(),
            ));
        }
        Ok(self)
    }
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
struct PrefetchObjectIdentity {
    object_key: String,
    content_id: String,
    object_version: Option<String>,
}

#[derive(Clone, Debug)]
struct PrefetchItem {
    request: RemoteLoadRequest,
    object: PrefetchObjectIdentity,
    encoded_bytes: u64,
}

type PrefetchTaskResult = (
    PrefetchItem,
    Result<crate::snapshot::io::backing::RangeLoadOutcome, RemoteLoadError>,
);

#[derive(Debug)]
enum PrefetchPlan {
    Skipped(&'static str),
    Eligible(Vec<PrefetchItem>),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum PrefetchFailureClass {
    TransientProvider,
    Authorization,
    Integrity,
    Identity,
    Backing,
    ProviderPermanent,
    Service,
    Worker,
}

impl PrefetchFailureClass {
    const fn label(self) -> &'static str {
        match self {
            Self::TransientProvider => "transient_provider",
            Self::Authorization => "authorization",
            Self::Integrity => "integrity",
            Self::Identity => "identity",
            Self::Backing => "backing",
            Self::ProviderPermanent => "provider_permanent",
            Self::Service => "service",
            Self::Worker => "worker",
        }
    }

    const fn retryable(self) -> bool {
        matches!(self, Self::TransientProvider)
    }
}

#[derive(Debug, Default)]
struct PrefetchRunSummary {
    planned_ranges: u64,
    planned_bytes: u64,
    planned_objects: HashSet<PrefetchObjectIdentity>,
    attempted_ranges: u64,
    completed_ranges: u64,
    failed_ranges: u64,
    attempted_bytes: u64,
    completed_bytes: u64,
    failed_bytes: u64,
    attempted_objects: HashSet<PrefetchObjectIdentity>,
    completed_objects: HashSet<PrefetchObjectIdentity>,
    failed_objects: HashSet<PrefetchObjectIdentity>,
}

/// A shared node-wide prefetch admission window. Cloning callers must share
/// this object rather than create one independent window per consumer.
#[derive(Debug)]
pub(crate) struct LatestMemoryPrefetcher {
    service: Arc<RangeLoadExecutor>,
    metrics: Option<Arc<SnapdMetrics>>,
    latest_memory_prefetch: LatestMemoryPrefetchConfig,
    latest_memory_prefetch_admission: Arc<RemoteLoadAdmissionController>,
}

impl LatestMemoryPrefetcher {
    pub(crate) fn new(
        service: Arc<RangeLoadExecutor>,
        metrics: Option<Arc<SnapdMetrics>>,
        config: LatestMemoryPrefetchConfig,
    ) -> Result<Self, AttachPrefetchError> {
        let config = config.validate()?;
        Ok(Self {
            service,
            metrics,
            latest_memory_prefetch: config,
            latest_memory_prefetch_admission: RemoteLoadAdmissionController::new(
                config.concurrency,
            ),
        })
    }

    pub(crate) async fn launch_snapshot_hotset_prefetch(
        &self,
        consumer: &RuntimeIdentity,
    ) -> (usize, u64) {
        self.service
            .launch_snapshot_hotset_prefetch(
                consumer,
                self.latest_memory_prefetch.concurrency,
                self.latest_memory_prefetch.max_encoded_bytes,
            )
            .await
    }

    fn latest_memory_prefetch_plan(
        &self,
        target_id: &str,
        layer: &SnapshotLayerView,
        has_p2p_hint: bool,
        backing_id: &str,
        resident: &HashSet<DataRangeKey>,
    ) -> Result<PrefetchPlan, AttachPrefetchError> {
        if self.latest_memory_prefetch.concurrency == 0 {
            return Ok(PrefetchPlan::Skipped("disabled"));
        }
        if layer.range_map.kind != SnapshotLayerKind::Memory {
            return Ok(PrefetchPlan::Skipped("not_memory"));
        }
        // Until the P2P reader supports a bounded batch/scatter request, one
        // request per 4-KiB DATA range would serialize on its runtime stream
        // and amplify a probe/range miss into thousands of P2P+S3 operations.
        // Demand faults retain the existing whole-runtime P2P fallback path.
        if has_p2p_hint {
            return Ok(PrefetchPlan::Skipped("p2p_hint"));
        }

        let mut encoded_bytes = 0_u64;
        let mut objects = HashSet::new();
        let mut items = Vec::new();
        let mut data_ranges = 0_usize;
        for range in &layer.range_map.ranges {
            if range.kind != RangeKind::Data {
                continue;
            }
            data_ranges += 1;
            let key = DataRangeKey::new(backing_id.to_owned(), range.logical_offset, range.length)
                .map_err(RemoteLoadError::from)?;
            if resident.contains(&key) {
                continue;
            }
            if items.len() == LATEST_MEMORY_PREFETCH_MAX_DATA_RANGES {
                return Ok(PrefetchPlan::Skipped("range_cap"));
            }
            let source = range.source.as_ref().ok_or_else(|| {
                AttachPrefetchError::Invalid(
                    "latest memory layer DATA range has no immutable source".into(),
                )
            })?;
            encoded_bytes = encoded_bytes
                .checked_add(source.encoded_length)
                .ok_or_else(|| {
                    AttachPrefetchError::Invalid(
                        "latest memory layer encoded byte count overflows".into(),
                    )
                })?;
            if encoded_bytes > self.latest_memory_prefetch.max_encoded_bytes {
                return Ok(PrefetchPlan::Skipped("byte_cap"));
            }
            let object = PrefetchObjectIdentity {
                object_key: source.object_key.clone(),
                content_id: sha_hex(&source.object_sha256)?.to_owned(),
                object_version: source.object_version.clone(),
            };
            objects.insert(object.clone());
            if objects.len() > LATEST_MEMORY_PREFETCH_MAX_OBJECTS {
                return Ok(PrefetchPlan::Skipped("object_cap"));
            }
            items.push(PrefetchItem {
                request: RemoteLoadRequest {
                    target_id: target_id.to_owned(),
                    target_offset: range.logical_offset,
                    length: range.length,
                    origin: "attach_prefetch".into(),
                },
                object,
                encoded_bytes: source.encoded_length,
            });
        }
        if items.is_empty() {
            Ok(PrefetchPlan::Skipped(if data_ranges == 0 {
                "no_data"
            } else {
                "already_resident"
            }))
        } else {
            Ok(PrefetchPlan::Eligible(items))
        }
    }

    pub(crate) async fn prefetch_latest_memory_layer(
        &self,
        snapshot_id: &str,
        runtime: &RuntimeIdentity,
        target_id: &str,
        layer: &SnapshotLayerView,
        has_p2p_hint: bool,
        backing: &Arc<LocalBacking>,
    ) -> Result<(), AttachPrefetchError> {
        let started = Instant::now();
        // Keep the disabled/non-memory/P2P paths genuinely cheap.  In
        // particular, p0 is the benchmark control and must not clone the
        // backing's complete resident set before deciding to do no work.
        let early_skip = if self.latest_memory_prefetch.concurrency == 0 {
            Some("disabled")
        } else if layer.range_map.kind != SnapshotLayerKind::Memory {
            Some("not_memory")
        } else if has_p2p_hint {
            Some("p2p_hint")
        } else {
            None
        };
        if let Some(reason) = early_skip {
            self.emit_prefetch_summary(
                snapshot_id,
                runtime,
                &layer.layer_ref.layer_id,
                "skipped",
                reason,
                None,
                started.elapsed(),
                &PrefetchRunSummary::default(),
                &RemoteLoadAdmissionObservation::default(),
            );
            return Ok(());
        }
        // Take one immutable residency snapshot before scheduling. Subsequent
        // demand loads may race and safely join/cache-hit, but already-hot
        // latest-layer ranges never consume speculative admission slots.
        let resident = backing.downloads.resident_ranges().await;
        let plan = self.latest_memory_prefetch_plan(
            target_id,
            layer,
            has_p2p_hint,
            &backing.backing_id,
            &resident,
        )?;
        let PrefetchPlan::Eligible(items) = plan else {
            let PrefetchPlan::Skipped(reason) = plan else {
                unreachable!("prefetch plan is either eligible or skipped")
            };
            self.emit_prefetch_summary(
                snapshot_id,
                runtime,
                &layer.layer_ref.layer_id,
                "skipped",
                reason,
                None,
                started.elapsed(),
                &PrefetchRunSummary::default(),
                &RemoteLoadAdmissionObservation::default(),
            );
            return Ok(());
        };

        let mut summary = PrefetchRunSummary {
            planned_ranges: items.len().try_into().unwrap_or(u64::MAX),
            planned_bytes: items.iter().fold(0_u64, |total, item| {
                total.saturating_add(item.encoded_bytes)
            }),
            planned_objects: items.iter().map(|item| item.object.clone()).collect(),
            ..PrefetchRunSummary::default()
        };
        let (admission_run, mut admission_caller) =
            self.latest_memory_prefetch_admission.begin_run(
                self.service.metrics(),
                snapshot_id,
                &layer.layer_ref.layer_id,
                runtime,
            );
        let concurrency = self.latest_memory_prefetch.concurrency.min(items.len());
        debug_assert!(concurrency > 0);
        let mut pending = items.into_iter();
        let mut loads = JoinSet::new();
        for _ in 0..concurrency {
            if let Some(item) = pending.next() {
                record_prefetch_attempt(&mut summary, &item);
                spawn_prefetch_load(
                    &mut loads,
                    Arc::clone(&self.service),
                    Arc::clone(&self.latest_memory_prefetch_admission),
                    Arc::clone(&admission_run),
                    runtime.clone(),
                    item,
                );
            }
        }

        let mut first_fatal = None;
        let mut failure_class = None;
        let mut stop_scheduling = false;
        while let Some(joined) = loads.join_next().await {
            match joined {
                Ok((item, result)) => match result {
                    Ok(_) => {
                        summary.completed_ranges = summary.completed_ranges.saturating_add(1);
                        summary.completed_bytes =
                            summary.completed_bytes.saturating_add(item.encoded_bytes);
                        summary.completed_objects.insert(item.object);
                    }
                    Err(error) => {
                        summary.failed_ranges = summary.failed_ranges.saturating_add(1);
                        summary.failed_bytes =
                            summary.failed_bytes.saturating_add(item.encoded_bytes);
                        summary.failed_objects.insert(item.object);
                        let class = classify_prefetch_failure(&error);
                        // A transient provider failure deliberately falls back
                        // to demand loading. Do not amplify one outage into a
                        // best-effort scan of every remaining DATA range; only
                        // drain work already admitted by the bounded window.
                        stop_scheduling = true;
                        if !class.retryable() && first_fatal.is_none() {
                            first_fatal = Some(error);
                            failure_class = Some(class);
                        } else if failure_class.is_none() {
                            failure_class = Some(class);
                        }
                    }
                },
                Err(error) => {
                    stop_scheduling = true;
                    let failure = RemoteLoadError::Worker(error.to_string());
                    if first_fatal.is_none() {
                        first_fatal = Some(failure);
                        failure_class = Some(PrefetchFailureClass::Worker);
                    }
                }
            }
            if !stop_scheduling {
                if let Some(item) = pending.next() {
                    record_prefetch_attempt(&mut summary, &item);
                    spawn_prefetch_load(
                        &mut loads,
                        Arc::clone(&self.service),
                        Arc::clone(&self.latest_memory_prefetch_admission),
                        Arc::clone(&admission_run),
                        runtime.clone(),
                        item,
                    );
                }
            }
        }
        summary
            .completed_objects
            .retain(|object| !summary.failed_objects.contains(object));
        admission_caller.returned();
        drop(admission_caller);
        let admission = admission_run.observation();

        if let Some(error) = first_fatal {
            self.emit_prefetch_summary(
                snapshot_id,
                runtime,
                &layer.layer_ref.layer_id,
                "eligible",
                "fail_closed",
                failure_class,
                started.elapsed(),
                &summary,
                &admission,
            );
            return Err(error.into());
        }
        let phase = if summary.failed_ranges == 0 {
            "completed"
        } else {
            "demand_fallback"
        };
        self.emit_prefetch_summary(
            snapshot_id,
            runtime,
            &layer.layer_ref.layer_id,
            "eligible",
            phase,
            failure_class,
            started.elapsed(),
            &summary,
            &admission,
        );
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn emit_prefetch_summary(
        &self,
        snapshot_id: &str,
        runtime: &RuntimeIdentity,
        layer_id: &str,
        decision: &'static str,
        phase: &'static str,
        failure_class: Option<PrefetchFailureClass>,
        duration: Duration,
        summary: &PrefetchRunSummary,
        admission: &RemoteLoadAdmissionObservation,
    ) {
        let Some(metrics) = &self.metrics else {
            return;
        };
        let level = match (decision, failure_class) {
            ("skipped", _) | (_, None) => StructuredLogLevel::Info,
            (_, Some(class)) if class.retryable() => StructuredLogLevel::Warning,
            _ => StructuredLogLevel::Error,
        };
        let outcome = match (decision, failure_class) {
            ("skipped", _) => StructuredOutcome::Rejected,
            (_, Some(class)) if !class.retryable() => StructuredOutcome::Error,
            _ => StructuredOutcome::Success,
        };
        let mut event = StructuredEvent::new(
            level,
            StructuredEventName::LatestMemoryPrefetchCompleted,
            StructuredComponent::Runtime,
            outcome,
        )
        .operation("latest_memory_prefetch")
        .phase(phase)
        .snapshot_id(snapshot_id)
        .layer_id(layer_id)
        .sandbox_id(&runtime.sandbox_id)
        .duration_micros(duration)
        .prefetch_summary(
            decision,
            self.latest_memory_prefetch
                .concurrency
                .try_into()
                .unwrap_or(u64::MAX),
            self.latest_memory_prefetch.max_encoded_bytes,
            summary.planned_ranges,
            summary.planned_objects.len().try_into().unwrap_or(u64::MAX),
            summary.planned_bytes,
            summary.attempted_ranges,
            summary.completed_ranges,
            summary.failed_ranges,
            summary
                .attempted_objects
                .len()
                .try_into()
                .unwrap_or(u64::MAX),
            summary
                .completed_objects
                .len()
                .try_into()
                .unwrap_or(u64::MAX),
            summary.failed_objects.len().try_into().unwrap_or(u64::MAX),
            summary.attempted_bytes,
            summary.completed_bytes,
            summary.failed_bytes,
        )
        .prefetch_admission(
            admission
                .node_active_at_start
                .try_into()
                .unwrap_or(u64::MAX),
            admission.node_max_active.try_into().unwrap_or(u64::MAX),
            admission.caller_cancelled,
            admission.accepted_drained,
        );
        if let Some(class) = failure_class {
            event = event.failure_class(class.label()).error_code(class.label());
        }
        metrics.emit_structured(event);
    }
}

fn spawn_prefetch_load(
    loads: &mut JoinSet<PrefetchTaskResult>,
    service: Arc<RangeLoadExecutor>,
    admission: Arc<RemoteLoadAdmissionController>,
    admission_run: Arc<RemoteLoadAdmissionRun>,
    runtime: RuntimeIdentity,
    item: PrefetchItem,
) {
    loads.spawn(async move {
        let request = item.request.clone();
        let result = match admission.acquire(admission_run).await {
            Ok(permit) => {
                // Ownership of the permit moves into the service-owned
                // completion monitor. Dropping/aborting this caller therefore
                // cannot release node admission while provider I/O continues.
                let result = service
                    .load_range_with_admission(&runtime, request, permit)
                    .await;
                result
            }
            Err(_) => Err(RemoteLoadError::Closed),
        };
        (item, result)
    });
}

fn record_prefetch_attempt(summary: &mut PrefetchRunSummary, item: &PrefetchItem) {
    summary.attempted_ranges = summary.attempted_ranges.saturating_add(1);
    summary.attempted_bytes = summary.attempted_bytes.saturating_add(item.encoded_bytes);
    summary.attempted_objects.insert(item.object.clone());
}

fn classify_prefetch_failure(error: &RemoteLoadError) -> PrefetchFailureClass {
    match error {
        RemoteLoadError::Provider(RangeReadError::Fallback { final_error, .. }) => {
            classify_range_read_failure(final_error)
        }
        RemoteLoadError::Provider(RangeReadError::Failure { class, .. }) => {
            classify_provider_failure(*class)
        }
        RemoteLoadError::Provider(RangeReadError::NotFound | RangeReadError::InvalidRange) => {
            PrefetchFailureClass::ProviderPermanent
        }
        RemoteLoadError::ChecksumMismatch
        | RemoteLoadError::ShortRead
        | RemoteLoadError::UnsupportedCodec => PrefetchFailureClass::Integrity,
        RemoteLoadError::InvalidArgument(_)
        | RemoteLoadError::TargetNotFound
        | RemoteLoadError::IdentityConflict
        | RemoteLoadError::RuntimeRevoked => PrefetchFailureClass::Identity,
        RemoteLoadError::WriteNoProgress
        | RemoteLoadError::Write(_)
        | RemoteLoadError::Backing(_)
        | RemoteLoadError::Layer(_)
        | RemoteLoadError::Range(_) => PrefetchFailureClass::Backing,
        RemoteLoadError::Closed | RemoteLoadError::ProviderNotConfigured => {
            PrefetchFailureClass::Service
        }
        RemoteLoadError::Worker(_) => PrefetchFailureClass::Worker,
    }
}

fn classify_range_read_failure(error: &RangeReadError) -> PrefetchFailureClass {
    match error {
        RangeReadError::NotFound | RangeReadError::InvalidRange => {
            PrefetchFailureClass::ProviderPermanent
        }
        RangeReadError::Failure { class, .. } => classify_provider_failure(*class),
        RangeReadError::Fallback { final_error, .. } => classify_range_read_failure(final_error),
    }
}

fn classify_provider_failure(class: RangeReadFailureClass) -> PrefetchFailureClass {
    match class {
        RangeReadFailureClass::Transient => PrefetchFailureClass::TransientProvider,
        RangeReadFailureClass::Authorization => PrefetchFailureClass::Authorization,
        RangeReadFailureClass::Integrity => PrefetchFailureClass::Integrity,
        RangeReadFailureClass::Permanent => PrefetchFailureClass::ProviderPermanent,
    }
}

fn sha_hex(identity: &str) -> Result<&str, AttachPrefetchError> {
    identity.strip_prefix("sha256:").ok_or_else(|| {
        AttachPrefetchError::Invalid(format!("invalid SHA-256 identity {identity:?}"))
    })
}

// Hotset collection and optional remote performance metadata.
const REMOTE_READ_TIMEOUT: Duration = Duration::from_millis(250);
const REMOTE_UPLOAD_TIMEOUT: Duration = Duration::from_secs(5);
const METADATA_IO_CONCURRENCY: usize = 2;

struct MetadataIo {
    reader: Arc<dyn SnapshotImageMetadataReader>,
    uploader: Arc<dyn SnapshotImageMetadataUploader>,
}

/// Owns only immutable profile identities/ranges and performance metadata IO.
/// Background uploads deliberately never capture a VM, granted target, backing
/// FD or the demand-load service, so runtime teardown is independent of OSS.
pub(in crate::snapshot) struct SnapshotHotsetService {
    cache: HotsetCache,
    metrics: Option<Arc<SnapdMetrics>>,
    metadata: OnceLock<MetadataIo>,
    reads: Arc<Semaphore>,
    uploads: Arc<Semaphore>,
    closed: AtomicBool,
    cancelled: watch::Sender<bool>,
}

impl SnapshotHotsetService {
    pub(in crate::snapshot) fn new(root: PathBuf) -> Self {
        let (cancelled, _) = watch::channel(false);
        Self {
            cache: HotsetCache::new(root),
            metrics: None,
            metadata: OnceLock::new(),
            reads: Arc::new(Semaphore::new(METADATA_IO_CONCURRENCY)),
            uploads: Arc::new(Semaphore::new(METADATA_IO_CONCURRENCY)),
            closed: AtomicBool::new(false),
            cancelled,
        }
    }

    pub(in crate::snapshot) fn with_metrics(mut self, metrics: Arc<SnapdMetrics>) -> Self {
        self.metrics = Some(metrics);
        self
    }

    pub(in crate::snapshot) fn configure_metadata(
        &self,
        reader: Arc<dyn SnapshotImageMetadataReader>,
        uploader: Arc<dyn SnapshotImageMetadataUploader>,
    ) -> Result<(), String> {
        if self.closed.load(Ordering::Acquire) {
            return Err("hotset service is closed".into());
        }
        self.metadata
            .set(MetadataIo { reader, uploader })
            .map_err(|_| "snapshot image metadata is already configured".into())
    }

    pub(in crate::snapshot) fn begin(
        &self,
        runtime: &RuntimeIdentity,
        subject: &SnapshotMetadataSubject,
    ) -> usize {
        if self.closed.load(Ordering::Acquire) || subject.validate().is_err() {
            return 0;
        }
        self.cache.begin(runtime, subject)
    }

    pub(in crate::snapshot) fn record(&self, runtime: &RuntimeIdentity, range: SnapshotHotRange) {
        if !self.closed.load(Ordering::Acquire) {
            self.cache.record(runtime, range);
        }
    }

    pub(in crate::snapshot) fn previous(
        &self,
        runtime: &RuntimeIdentity,
    ) -> Arc<Vec<SnapshotHotRange>> {
        self.cache.previous(runtime)
    }

    pub(in crate::snapshot) fn finish(&self, runtime: &RuntimeIdentity, publish: bool) -> usize {
        let Some((subject, ranges)) = self.cache.finish(runtime, publish) else {
            return 0;
        };
        let count = ranges.len();
        if self.closed.load(Ordering::Acquire) {
            return count;
        }
        let Some(io) = self.metadata.get() else {
            return count;
        };
        let Ok(permit) = Arc::clone(&self.uploads).try_acquire_owned() else {
            emit_metadata(self.metrics.as_ref(), &subject.snapshot_id, "upload_busy");
            return count;
        };
        let Ok(executor) = tokio::runtime::Handle::try_current() else {
            emit_metadata(
                self.metrics.as_ref(),
                &subject.snapshot_id,
                "upload_unavailable",
            );
            return count;
        };
        let metadata = SnapshotImageMetadata {
            format_version: 1,
            snapshot_id: subject.snapshot_id,
            spec_checksum: subject.spec_checksum,
            hotset: Some(SnapshotHotset {
                ranges: ranges.as_ref().clone(),
            }),
        };
        let uploader = Arc::clone(&io.uploader);
        let metrics = self.metrics.clone();
        let mut cancelled = self.cancelled.subscribe();
        executor.spawn(async move {
            let _permit = permit;
            if *cancelled.borrow() {
                emit_metadata(metrics.as_ref(), &metadata.snapshot_id, "upload_cancelled");
                return;
            }
            let phase = tokio::select! {
                _ = cancelled.changed() => "upload_cancelled",
                result = tokio::time::timeout(REMOTE_UPLOAD_TIMEOUT, uploader.upload(&metadata)) => {
                    match result {
                        Ok(Ok(())) => "uploaded",
                        Ok(Err(_)) => "upload_failed",
                        Err(_) => "upload_timeout",
                    }
                },
            };
            emit_metadata(metrics.as_ref(), &metadata.snapshot_id, phase);
        });
        count
    }

    /// Called only by enabled replay. A local profile avoids all remote IO;
    /// overload, missing/corrupt hints, errors and timeout are ordinary misses.
    pub(in crate::snapshot) async fn load_remote(&self, runtime: &RuntimeIdentity) {
        if self.closed.load(Ordering::Acquire) {
            return;
        }
        let Some(io) = self.metadata.get() else {
            return;
        };
        let Some(subject) = self.cache.remote_subject(runtime) else {
            return;
        };
        let Ok(_permit) = self.reads.try_acquire() else {
            emit_metadata(self.metrics.as_ref(), &subject.snapshot_id, "read_busy");
            return;
        };
        let mut cancelled = self.cancelled.subscribe();
        if *cancelled.borrow() {
            return;
        }
        let metadata = tokio::select! {
            _ = cancelled.changed() => {
                emit_metadata(self.metrics.as_ref(), &subject.snapshot_id, "read_cancelled");
                return;
            },
            result = tokio::time::timeout(REMOTE_READ_TIMEOUT, io.reader.read(&subject)) => {
                match result {
                    Ok(Ok(Some(metadata))) => metadata,
                    Ok(Ok(None)) => {
                        emit_metadata(self.metrics.as_ref(), &subject.snapshot_id, "remote_miss");
                        return;
                    },
                    Ok(Err(_)) => {
                        emit_metadata(self.metrics.as_ref(), &subject.snapshot_id, "read_failed");
                        return;
                    },
                    Err(_) => {
                        emit_metadata(self.metrics.as_ref(), &subject.snapshot_id, "read_timeout");
                        return;
                    },
                }
            },
        };
        if self.closed.load(Ordering::Acquire) {
            emit_metadata(
                self.metrics.as_ref(),
                &subject.snapshot_id,
                "read_cancelled",
            );
            return;
        }
        if metadata.validate_for(&subject).is_err() {
            emit_metadata(self.metrics.as_ref(), &subject.snapshot_id, "read_failed");
            return;
        }
        if let Some(hotset) = metadata.hotset.filter(|hotset| !hotset.ranges.is_empty()) {
            self.cache.install_remote(runtime, &subject, hotset.ranges);
            emit_metadata(self.metrics.as_ref(), &subject.snapshot_id, "remote_hit");
        } else {
            emit_metadata(self.metrics.as_ref(), &subject.snapshot_id, "remote_miss");
        }
    }

    pub(in crate::snapshot) fn close(&self) {
        self.closed.store(true, Ordering::Release);
        self.cancelled.send_replace(true);
        self.reads.close();
        self.uploads.close();
        self.cache.clear_active();
    }
}

/// A bounded boot/profile event, with no raw provider errors, URLs, credentials
/// or new high-cardinality metric labels.
fn emit_metadata(metrics: Option<&Arc<SnapdMetrics>>, snapshot_id: &str, phase: &'static str) {
    let Some(metrics) = metrics else {
        return;
    };
    let success = matches!(phase, "remote_hit" | "remote_miss" | "uploaded");
    metrics.emit_structured(
        StructuredEvent::new(
            if success {
                StructuredLogLevel::Info
            } else {
                StructuredLogLevel::Warning
            },
            StructuredEventName::RuntimeStartStageCompleted,
            StructuredComponent::RemoteLoad,
            if success {
                StructuredOutcome::Success
            } else {
                StructuredOutcome::Rejected
            },
        )
        .operation("snapshot_image_metadata")
        .phase(phase)
        .snapshot_id(snapshot_id),
    );
}

// Rebuildable local hotset profiles and per-start collection.
static TEMP_ID: AtomicUsize = AtomicUsize::new(1);

#[derive(Debug)]
struct ActiveHotset {
    subject: SnapshotMetadataSubject,
    previous: Arc<Vec<SnapshotHotRange>>,
    captured: Vec<SnapshotHotRange>,
    seen: HashSet<SnapshotHotRange>,
    decoded_bytes: u64,
}

#[derive(Debug, Default)]
struct State {
    active: HashMap<RuntimeIdentity, ActiveHotset>,
    cached: HashMap<String, Arc<Vec<SnapshotHotRange>>>,
}

#[derive(Debug)]
struct HotsetCache {
    root: PathBuf,
    state: Mutex<State>,
}

impl HotsetCache {
    fn new(root: PathBuf) -> Self {
        Self {
            root,
            state: Mutex::new(State::default()),
        }
    }

    fn begin(&self, runtime: &RuntimeIdentity, subject: &SnapshotMetadataSubject) -> usize {
        let identity = subject.local_identity();
        let cached = {
            let state = self.state.lock().expect("snapshot hotset lock poisoned");
            state.cached.get(&identity).cloned()
        };
        let previous = cached
            .or_else(|| self.load(&identity))
            .unwrap_or_else(|| Arc::new(Vec::new()));
        let count = previous.len();
        let mut state = self.state.lock().expect("snapshot hotset lock poisoned");
        state.cached.insert(identity, Arc::clone(&previous));
        state.active.insert(
            runtime.clone(),
            ActiveHotset {
                subject: subject.clone(),
                previous,
                captured: Vec::new(),
                seen: HashSet::new(),
                decoded_bytes: 0,
            },
        );
        count
    }

    fn previous(&self, runtime: &RuntimeIdentity) -> Arc<Vec<SnapshotHotRange>> {
        self.state
            .lock()
            .expect("snapshot hotset lock poisoned")
            .active
            .get(runtime)
            .map(|active| Arc::clone(&active.previous))
            .unwrap_or_else(|| Arc::new(Vec::new()))
    }

    fn remote_subject(&self, runtime: &RuntimeIdentity) -> Option<SnapshotMetadataSubject> {
        let state = self.state.lock().expect("snapshot hotset lock poisoned");
        let active = state.active.get(runtime)?;
        active.previous.is_empty().then(|| active.subject.clone())
    }

    fn install_remote(
        &self,
        runtime: &RuntimeIdentity,
        subject: &SnapshotMetadataSubject,
        ranges: Vec<SnapshotHotRange>,
    ) {
        if ranges.is_empty() {
            return;
        }
        let ranges = Arc::new(ranges);
        let identity = subject.local_identity();
        {
            let mut state = self.state.lock().expect("snapshot hotset lock poisoned");
            let Some(active) = state.active.get_mut(runtime) else {
                return;
            };
            // A late remote response cannot resurrect a finished/reused start
            // or replace a local profile captured while it was in flight.
            if !active.previous.is_empty() || active.subject.local_identity() != identity {
                return;
            }
            active.previous = Arc::clone(&ranges);
            state.cached.insert(identity.clone(), Arc::clone(&ranges));
        }
        // Remote adoption is on the start-time hint path: keep it in RAM.
        // Successful finish retains the existing local persistence policy.
    }

    fn record(&self, runtime: &RuntimeIdentity, range: SnapshotHotRange) {
        let mut state = self.state.lock().expect("snapshot hotset lock poisoned");
        let Some(active) = state.active.get_mut(runtime) else {
            return;
        };
        if active.captured.len() >= MAX_SNAPSHOT_HOTSET_RANGES
            || active.seen.contains(&range)
            || active
                .decoded_bytes
                .checked_add(range.length)
                .is_none_or(|bytes| bytes > MAX_SNAPSHOT_HOTSET_DECODED_BYTES)
        {
            return;
        }
        active.decoded_bytes += range.length;
        active.seen.insert(range.clone());
        active.captured.push(range);
    }

    fn finish(
        &self,
        runtime: &RuntimeIdentity,
        publish: bool,
    ) -> Option<(SnapshotMetadataSubject, Arc<Vec<SnapshotHotRange>>)> {
        let active = self
            .state
            .lock()
            .expect("snapshot hotset lock poisoned")
            .active
            .remove(runtime)?;
        if !publish {
            return None;
        }
        let merged = merge_snapshot_hot_ranges(&active.previous, &active.captured);
        if merged.is_empty() {
            return None;
        }
        let ranges = Arc::new(merged);
        let identity = active.subject.local_identity();
        self.state
            .lock()
            .expect("snapshot hotset lock poisoned")
            .cached
            .insert(identity.clone(), Arc::clone(&ranges));
        let _ = self.store(&identity, &ranges);
        Some((active.subject, ranges))
    }

    fn clear_active(&self) {
        self.state
            .lock()
            .expect("snapshot hotset lock poisoned")
            .active
            .clear();
    }

    fn path(&self, identity: &str) -> PathBuf {
        let digest = format!("{:x}", Sha256::digest(identity.as_bytes()));
        self.root.join(format!("{digest}.bin"))
    }

    fn load(&self, identity: &str) -> Option<Arc<Vec<SnapshotHotRange>>> {
        let path = self.path(identity);
        let metadata = fs::symlink_metadata(&path).ok()?;
        if !metadata.is_file() || metadata.len() > MAX_SNAPSHOT_IMAGE_METADATA_BYTES {
            return None;
        }
        let payload = fs::read(path).ok()?;
        decode_local_hotset(&payload, identity).ok().map(Arc::new)
    }

    fn store(&self, identity: &str, ranges: &[SnapshotHotRange]) -> io::Result<()> {
        let payload = encode_local_hotset(identity, ranges)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error.to_string()))?;
        fs::create_dir_all(&self.root)?;
        fs::set_permissions(&self.root, fs::Permissions::from_mode(0o700))?;
        let final_path = self.path(identity);
        let temporary = self.root.join(format!(
            ".{}.{}.tmp",
            final_path
                .file_stem()
                .and_then(|s| s.to_str())
                .unwrap_or("hotset"),
            TEMP_ID.fetch_add(1, Ordering::Relaxed)
        ));
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temporary)?;
        let result = (|| {
            output.write_all(&payload)?;
            output.sync_all()?;
            fs::rename(&temporary, final_path)
        })();
        if result.is_err() {
            let _ = fs::remove_file(temporary);
        }
        result
    }
}

// Replay of previously observed ranges through authorized demand loading.
const SNAPSHOT_HOTSET_MAX_PREFETCH_REQUESTS: usize = 2 * 1024;

/// Policy for speculative reads. The granted demand-load service remains the
/// authority for every DATA read and backing write.
#[async_trait]
pub(in crate::snapshot) trait SnapshotPrefetcher: Send + Sync {
    async fn prefetch(
        &self,
        service: &Arc<RangeLoadExecutor>,
        runtime: &RuntimeIdentity,
        previous: Arc<Vec<SnapshotHotRange>>,
        concurrency: usize,
        byte_limit: u64,
    ) -> (usize, u64);
}

#[derive(Debug, Default)]
pub(in crate::snapshot) struct HotsetPrefetcher;

#[async_trait]
impl SnapshotPrefetcher for HotsetPrefetcher {
    async fn prefetch(
        &self,
        service: &Arc<RangeLoadExecutor>,
        runtime: &RuntimeIdentity,
        previous: Arc<Vec<SnapshotHotRange>>,
        concurrency: usize,
        byte_limit: u64,
    ) -> (usize, u64) {
        if concurrency == 0 || byte_limit == 0 {
            return (0, 0);
        }
        if previous.is_empty() {
            return (0, 0);
        }
        let mut requests = Vec::new();
        let mut groups = HashSet::new();
        let mut planned_bytes = 0_u64;
        for entry in previous.iter() {
            if requests.len() >= SNAPSHOT_HOTSET_MAX_PREFETCH_REQUESTS {
                break;
            }
            let Some(target) = hotset_target(service, runtime, entry).await else {
                continue;
            };
            let Some(backing) = target_backing(&target.layer_target) else {
                continue;
            };
            let Ok(exact_key) = DataRangeKey::new(
                backing.backing_id.clone(),
                target.grant.target_offset,
                target.grant.length,
            ) else {
                continue;
            };
            if backing.downloads.status(&exact_key).await.state == DownloadState::Resident {
                continue;
            }
            let coalesced = service.coalesced_range(&target).await;
            let first = coalesced.first();
            let group = (
                first.target_id.clone(),
                first.source.object_key.clone(),
                first.source.object_offset,
                coalesced.length(),
            );
            if !groups.insert(group) {
                continue;
            }
            let Some(next_bytes) = planned_bytes.checked_add(coalesced.length()) else {
                break;
            };
            if next_bytes > byte_limit {
                break;
            }
            planned_bytes = next_bytes;
            requests.push(RemoteLoadRequest {
                target_id: target.grant.target_id.clone(),
                target_offset: target.grant.target_offset,
                length: target.grant.length,
                origin: "snapshot_hotset_prefetch".into(),
            });
        }
        if requests.is_empty() {
            return (0, 0);
        }
        let planned_requests = requests.len();
        service.metrics.emit_structured(
            StructuredEvent::new(
                StructuredLogLevel::Info,
                StructuredEventName::RuntimeStartStageCompleted,
                StructuredComponent::RemoteLoad,
                StructuredOutcome::Success,
            )
            .operation("snapshot_hotset_prefetch")
            .phase("launched")
            .sandbox_id(&runtime.sandbox_id)
            .prefetch_summary(
                "eligible",
                concurrency as u64,
                byte_limit,
                planned_requests as u64,
                planned_requests as u64,
                planned_bytes,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
            ),
        );
        let service = Arc::clone(service);
        let runtime = runtime.clone();
        tokio::spawn(async move {
            let started = Instant::now();
            let permits = Arc::new(Semaphore::new(concurrency));
            let mut tasks = tokio::task::JoinSet::new();
            for request in requests {
                let service = Arc::clone(&service);
                let runtime = runtime.clone();
                let permits = Arc::clone(&permits);
                tasks.spawn(async move {
                    let permit = permits
                        .acquire_owned()
                        .await
                        .map_err(|_| RemoteLoadError::Closed)?;
                    let result = service
                        .load_range(&runtime, request, None)
                        .await
                        .map(|result| result.outcome);
                    drop(permit);
                    result
                });
            }
            let mut completed = 0_u64;
            let mut failed = 0_u64;
            while let Some(result) = tasks.join_next().await {
                match result {
                    Ok(Ok(_)) => completed += 1,
                    _ => failed += 1,
                }
            }
            service.metrics.emit_structured(
                StructuredEvent::new(
                    if failed == 0 {
                        StructuredLogLevel::Info
                    } else {
                        StructuredLogLevel::Warning
                    },
                    StructuredEventName::RuntimeStartStageCompleted,
                    StructuredComponent::RemoteLoad,
                    if failed == 0 {
                        StructuredOutcome::Success
                    } else {
                        StructuredOutcome::Error
                    },
                )
                .operation("snapshot_hotset_prefetch")
                .phase("completed")
                .sandbox_id(&runtime.sandbox_id)
                .duration_micros(started.elapsed())
                .prefetch_summary(
                    if failed == 0 { "completed" } else { "partial" },
                    concurrency as u64,
                    byte_limit,
                    planned_requests as u64,
                    planned_requests as u64,
                    planned_bytes,
                    planned_requests as u64,
                    completed,
                    failed,
                    planned_requests as u64,
                    completed,
                    failed,
                    planned_bytes,
                    planned_bytes,
                    0,
                ),
            );
        });
        (planned_requests, planned_bytes)
    }
}

async fn hotset_target(
    service: &RangeLoadExecutor,
    runtime: &RuntimeIdentity,
    entry: &SnapshotHotRange,
) -> Option<GrantedRange> {
    let state = service.state.lock().await;
    for (target_id, layer) in state.live_layers() {
        if &layer.remote_identity().runtime != runtime
            || layer.revoked.load(Ordering::Acquire)
            || layer.remote_identity().backing_key.content_sha256 != entry.layer_content_sha256
        {
            continue;
        }
        let grant = exact_layer_grant(target_id, &layer, entry.target_offset, entry.length)
            .ok()
            .flatten()?;
        return Some(GrantedRange {
            grant,
            layer_target: Arc::clone(&layer),
        });
    }
    None
}

impl RangeLoadExecutor {
    pub fn begin_snapshot_hotset(
        &self,
        snapshot_id: &str,
        spec_checksum: &str,
        runtime: &RuntimeIdentity,
    ) -> usize {
        let ranges = SnapshotMetadataSubject::new(snapshot_id, spec_checksum)
            .map(|subject| self.hotsets.begin(runtime, &subject))
            .unwrap_or_else(|_| self.hotsets.finish(runtime, false));
        self.metrics.emit_structured(
            StructuredEvent::new(
                StructuredLogLevel::Info,
                StructuredEventName::RuntimeStartStageCompleted,
                StructuredComponent::RemoteLoad,
                StructuredOutcome::Success,
            )
            .operation("snapshot_hotset_capture")
            .phase(if ranges == 0 { "cold" } else { "loaded" })
            .snapshot_id(snapshot_id)
            .sandbox_id(&runtime.sandbox_id)
            .graph_counts(0, ranges as u64, ranges as u64, 0),
        );
        ranges
    }

    pub fn finish_snapshot_hotset(&self, runtime: &RuntimeIdentity, publish: bool) -> usize {
        let ranges = self.hotsets.finish(runtime, publish);
        self.metrics.emit_structured(
            StructuredEvent::new(
                StructuredLogLevel::Info,
                StructuredEventName::RuntimeStartStageCompleted,
                StructuredComponent::RemoteLoad,
                StructuredOutcome::Success,
            )
            .operation("snapshot_hotset_capture")
            .phase(if publish { "published" } else { "discarded" })
            .sandbox_id(&runtime.sandbox_id)
            .graph_counts(0, ranges as u64, ranges as u64, 0),
        );
        ranges
    }

    pub(crate) fn configure_snapshot_image_metadata(
        &self,
        reader: Arc<dyn SnapshotImageMetadataReader>,
        uploader: Arc<dyn SnapshotImageMetadataUploader>,
    ) -> Result<(), RemoteLoadError> {
        self.hotsets
            .configure_metadata(reader, uploader)
            .map_err(RemoteLoadError::InvalidArgument)
    }

    pub async fn launch_snapshot_hotset_prefetch(
        self: &Arc<Self>,
        runtime: &RuntimeIdentity,
        concurrency: usize,
        byte_limit: u64,
    ) -> (usize, u64) {
        if concurrency == 0 || byte_limit == 0 {
            return (0, 0);
        }
        self.hotsets.load_remote(runtime).await;
        HotsetPrefetcher
            .prefetch(
                self,
                runtime,
                self.hotsets.previous(runtime),
                concurrency,
                byte_limit,
            )
            .await
    }
}

#[cfg(test)]
mod tests {
    //! Local-first optional metadata IO and real cross-node authorized replay.

    use super::*;
    use std::collections::HashMap;
    use std::fs;
    use std::os::unix::fs::FileExt;
    use std::sync::atomic::AtomicUsize;
    use std::sync::Mutex;

    use crate::snapshot::image::metadata::{ImageMetadataError, ObjectStoreImageMetadata};
    use crate::snapshot::io::backing::{
        DataRangeKey, DownloadState, LayerBackingIdentity, LayerKind, LocalBackingRegistry,
        RangeLoadOutcome,
    };
    use crate::snapshot::io::range_io::RangeReadError;
    use crate::snapshot::io::range_io::{RangeReadRequest, RangeReadResult, RangeReader};
    use crate::snapshot::io::snapshot_io::SnapshotIo;
    use crate::snapshot::metadata::{
        compute_layer_content_sha256, sha256_identity, DataCodec, DataSource, LayerRange,
        LayerRangeMap, RangeKind,
    };
    use crate::snapshot::services::ondemand_loader::{
        RangeLoadExecutor, RemoteLayerTargetGrant, RemoteLoadRequest, DEFAULT_MAX_RANGE_BYTES,
    };
    use crate::snapshot::transport::MetadataObjectStore;
    use crate::snapshot::transport::{
        BoundedGetError, ObjectStoreError, RemoteObject, RemoteObjectMetadata,
    };

    struct TestRoot(PathBuf);
    impl TestRoot {
        fn new() -> Self {
            Self(std::env::temp_dir().join(format!("snapd-hotset-{}", uuid::Uuid::new_v4())))
        }
    }
    impl Drop for TestRoot {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn subject() -> SnapshotMetadataSubject {
        SnapshotMetadataSubject::new(
            "snapshot-hotset-integration",
            sha256_identity(b"exact spec"),
        )
        .unwrap()
    }
    fn hot_range(offset: u64) -> SnapshotHotRange {
        SnapshotHotRange {
            layer_content_sha256: "a".repeat(64),
            target_offset: offset,
            length: 4096,
        }
    }
    fn metadata(
        subject: &SnapshotMetadataSubject,
        ranges: Vec<SnapshotHotRange>,
    ) -> SnapshotImageMetadata {
        SnapshotImageMetadata {
            format_version: 1,
            snapshot_id: subject.snapshot_id.clone(),
            spec_checksum: subject.spec_checksum.clone(),
            hotset: Some(SnapshotHotset { ranges }),
        }
    }

    #[derive(Debug, Default)]
    struct HintIo {
        value: Mutex<Option<SnapshotImageMetadata>>,
        reads: AtomicUsize,
        uploads: AtomicUsize,
        active_reads: AtomicUsize,
        active_uploads: AtomicUsize,
        block_read: AtomicBool,
        block_upload: AtomicBool,
        corrupt: AtomicBool,
    }
    struct ActiveIo<'a>(&'a AtomicUsize);
    impl Drop for ActiveIo<'_> {
        fn drop(&mut self) {
            self.0.fetch_sub(1, Ordering::SeqCst);
        }
    }
    #[async_trait::async_trait]
    impl SnapshotImageMetadataReader for HintIo {
        async fn read(
            &self,
            _: &SnapshotMetadataSubject,
        ) -> Result<Option<SnapshotImageMetadata>, ImageMetadataError> {
            self.reads.fetch_add(1, Ordering::SeqCst);
            self.active_reads.fetch_add(1, Ordering::SeqCst);
            let _active = ActiveIo(&self.active_reads);
            if self.block_read.load(Ordering::SeqCst) {
                std::future::pending::<()>().await;
            }
            if self.corrupt.load(Ordering::SeqCst) {
                return Err(ImageMetadataError::Invalid("bad fixture".into()));
            }
            Ok(self.value.lock().unwrap().clone())
        }
    }
    #[async_trait::async_trait]
    impl SnapshotImageMetadataUploader for HintIo {
        async fn upload(&self, metadata: &SnapshotImageMetadata) -> Result<(), ImageMetadataError> {
            self.uploads.fetch_add(1, Ordering::SeqCst);
            self.active_uploads.fetch_add(1, Ordering::SeqCst);
            let _active = ActiveIo(&self.active_uploads);
            if self.block_upload.load(Ordering::SeqCst) {
                std::future::pending::<()>().await;
            }
            *self.value.lock().unwrap() = Some(metadata.clone());
            Ok(())
        }
    }

    async fn wait_until(mut ready: impl FnMut() -> bool) {
        tokio::time::timeout(Duration::from_secs(2), async {
            while !ready() {
                tokio::time::sleep(Duration::from_millis(2)).await;
            }
        })
        .await
        .expect("fixture did not reach bounded terminal condition");
    }

    #[tokio::test]
    async fn local_profile_uses_compatible_codec_and_skips_remote_get() {
        let root = TestRoot::new();
        let source = SnapshotHotsetService::new(root.0.clone());
        let runtime = RuntimeIdentity::new("local-capture");
        let subject = subject();
        assert_eq!(source.begin(&runtime, &subject), 0);
        source.record(&runtime, hot_range(0));
        source.record(&runtime, hot_range(4096));
        source.record(&runtime, hot_range(0));
        assert_eq!(source.finish(&runtime, true), 2);
        drop(source);
        // New in-memory cache, same persisted RCKHOT02 file.
        let reloaded = SnapshotHotsetService::new(root.0.clone());
        let io = Arc::new(HintIo::default());
        reloaded.configure_metadata(io.clone(), io.clone()).unwrap();
        assert_eq!(reloaded.begin(&runtime, &subject), 2);
        reloaded.load_remote(&runtime).await;
        assert_eq!(io.reads.load(Ordering::SeqCst), 0);
        reloaded.record(&runtime, hot_range(8192));
        assert_eq!(reloaded.finish(&runtime, true), 3);
        assert_eq!(reloaded.begin(&runtime, &subject), 3);
        assert_eq!(
            reloaded.previous(&runtime).as_slice(),
            &[hot_range(0), hot_range(4096), hot_range(8192)]
        );
        reloaded.close();
    }

    #[tokio::test]
    async fn missing_corrupt_and_wrong_subject_are_optional_hint_misses() {
        let root = TestRoot::new();
        let service = SnapshotHotsetService::new(root.0.clone());
        let io = Arc::new(HintIo::default());
        service.configure_metadata(io.clone(), io.clone()).unwrap();
        let runtime = RuntimeIdentity::new("optional-miss");
        let subject = subject();
        for mode in 0..3 {
            service.begin(&runtime, &subject);
            io.corrupt.store(mode == 1, Ordering::SeqCst);
            if mode == 2 {
                let foreign =
                    SnapshotMetadataSubject::new("different-snapshot", &subject.spec_checksum)
                        .unwrap();
                *io.value.lock().unwrap() = Some(metadata(&foreign, vec![hot_range(0)]));
            }
            service.load_remote(&runtime).await;
            assert!(service.previous(&runtime).is_empty());
            assert_eq!(service.finish(&runtime, false), 0);
        }
        assert_eq!(io.reads.load(Ordering::SeqCst), 3);
    }

    #[tokio::test]
    async fn remote_hint_read_has_timeout_and_close_cancels_pending_io() {
        let root = TestRoot::new();
        let service = Arc::new(SnapshotHotsetService::new(root.0.clone()));
        let io = Arc::new(HintIo::default());
        io.block_read.store(true, Ordering::SeqCst);
        service.configure_metadata(io.clone(), io.clone()).unwrap();
        let runtime = RuntimeIdentity::new("read-timeout");
        service.begin(&runtime, &subject());
        tokio::time::timeout(Duration::from_secs(1), service.load_remote(&runtime))
            .await
            .unwrap();
        assert!(service.previous(&runtime).is_empty());
        assert_eq!(io.active_reads.load(Ordering::SeqCst), 0);

        let service_task = Arc::clone(&service);
        let runtime_task = runtime.clone();
        let task = tokio::spawn(async move {
            service_task.load_remote(&runtime_task).await;
        });
        wait_until(|| io.active_reads.load(Ordering::SeqCst) == 1).await;
        service.close();
        tokio::time::timeout(Duration::from_millis(100), task)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(io.active_reads.load(Ordering::SeqCst), 0);
        service.load_remote(&runtime).await;
        assert_eq!(io.reads.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn metadata_admission_limits_and_close_cancel_uploads() {
        let root = TestRoot::new();
        let service = Arc::new(SnapshotHotsetService::new(root.0.clone()));
        let io = Arc::new(HintIo::default());
        io.block_read.store(true, Ordering::SeqCst);
        io.block_upload.store(true, Ordering::SeqCst);
        service.configure_metadata(io.clone(), io.clone()).unwrap();
        assert!(service.configure_metadata(io.clone(), io.clone()).is_err());
        let mut reads = Vec::new();
        for i in 0..2 {
            let runtime = RuntimeIdentity::new(format!("bounded-read-{i}"));
            service.begin(&runtime, &subject());
            let service = Arc::clone(&service);
            reads.push(tokio::spawn(async move {
                service.load_remote(&runtime).await;
            }));
        }
        wait_until(|| io.active_reads.load(Ordering::SeqCst) == 2).await;
        let overflow = RuntimeIdentity::new("overflow");
        service.begin(&overflow, &subject());
        service.load_remote(&overflow).await;
        assert_eq!(io.reads.load(Ordering::SeqCst), 2);
        // Finish admits only two background uploads; the third still persists locally.
        for i in 0..3 {
            let runtime = RuntimeIdentity::new(format!("bounded-upload-{i}"));
            service.begin(&runtime, &subject());
            service.record(&runtime, hot_range(i * 4096));
            assert!(service.finish(&runtime, true) > 0);
        }
        wait_until(|| io.active_uploads.load(Ordering::SeqCst) == 2).await;
        assert_eq!(io.uploads.load(Ordering::SeqCst), 2);
        service.close();
        for read in reads {
            tokio::time::timeout(Duration::from_secs(1), read)
                .await
                .unwrap()
                .unwrap();
        }
        wait_until(|| io.active_uploads.load(Ordering::SeqCst) == 0).await;
    }

    #[derive(Debug, Default)]
    struct MemoryMetadataStore {
        objects: Mutex<HashMap<String, RemoteObject>>,
        reads: AtomicUsize,
        writes: AtomicUsize,
    }
    #[async_trait::async_trait]
    impl MetadataObjectStore for MemoryMetadataStore {
        fn namespace(&self) -> &str {
            "shared-hotset-test-domain"
        }
        async fn get_bounded(
            &self,
            key: &str,
            max_bytes: u64,
        ) -> Result<Option<RemoteObject>, BoundedGetError> {
            self.reads.fetch_add(1, Ordering::SeqCst);
            let value = self.objects.lock().unwrap().get(key).cloned();
            if let Some(value) = &value {
                if value.payload.len() as u64 > max_bytes {
                    return Err(BoundedGetError::BoundExceeded {
                        object_key: key.into(),
                        actual_bytes: value.payload.len() as u64,
                        max_bytes,
                    });
                }
            }
            Ok(value)
        }
        async fn replace(
            &self,
            key: &str,
            payload: &[u8],
            expected_sha256: &str,
        ) -> Result<(), ObjectStoreError> {
            assert_eq!(sha256_identity(payload), expected_sha256);
            let metadata = RemoteObjectMetadata {
                size: payload.len() as u64,
                sha256: expected_sha256.into(),
                version: None,
                last_modified_unix: None,
                etag: None,
            };
            self.objects.lock().unwrap().insert(
                key.into(),
                RemoteObject {
                    payload: payload.to_vec(),
                    metadata,
                },
            );
            self.writes.fetch_add(1, Ordering::SeqCst);
            Ok(())
        }
    }

    #[derive(Debug)]
    struct PayloadProvider {
        payload: Vec<u8>,
        reads: AtomicUsize,
    }
    #[async_trait::async_trait]
    impl RangeReader for PayloadProvider {
        async fn read_range(
            &self,
            request: RangeReadRequest<'_>,
        ) -> Result<RangeReadResult, RangeReadError> {
            self.reads.fetch_add(1, Ordering::SeqCst);
            let start = request.offset as usize;
            Ok(RangeReadResult::provider(
                self.payload[start..start + request.length as usize].to_vec(),
            ))
        }
    }

    fn layer(payload: &[u8]) -> Arc<LayerRangeMap> {
        let mut map = LayerRangeMap {
            registry_objects: None,
            format_version: 1,
            kind: crate::snapshot::metadata::LayerKind::Memory,
            content_sha256: String::new(),
            logical_bytes: 8192,
            unit_size_bytes: 4096,
            ranges: vec![
                LayerRange {
                    kind: RangeKind::Zero,
                    logical_offset: 0,
                    length: 4096,
                    source: None,
                },
                LayerRange {
                    kind: RangeKind::Data,
                    logical_offset: 4096,
                    length: 4096,
                    source: Some(DataSource {
                        object_key: "hotset/test-memory.pack".into(),
                        object_sha256: sha256_identity(payload),
                        object_version: None,
                        pack_offset: 0,
                        encoded_length: 4096,
                        decoded_length: 4096,
                        codec: DataCodec::Raw,
                        checksum_sha256: sha256_identity(payload),
                    }),
                },
            ],
            zfile: None,
            registry: None,
        };
        map.content_sha256 = compute_layer_content_sha256(&map).unwrap();
        Arc::new(map)
    }

    fn node(root: &TestRoot, name: &str, provider: Arc<PayloadProvider>) -> Arc<RangeLoadExecutor> {
        let registry = LocalBackingRegistry::new(root.0.join("backings"), name).unwrap();
        RangeLoadExecutor::new(
            registry,
            SnapshotIo::data_only(),
            HashMap::from([("fixture".into(), provider as Arc<dyn RangeReader>)]),
            DEFAULT_MAX_RANGE_BYTES,
        )
        .unwrap()
    }
    async fn grant(
        service: &Arc<RangeLoadExecutor>,
        runtime: &RuntimeIdentity,
        target: &str,
        map: Arc<LayerRangeMap>,
    ) -> Arc<crate::snapshot::io::layer_resources::LayerResources> {
        let (resources, _) = service
            .prepare_target(RemoteLayerTargetGrant {
                runtime: runtime.clone(),
                target_id: target.into(),
                backing_key: LayerBackingIdentity {
                    kind: LayerKind::Memory,
                    format_version: 1,
                    logical_size: map.logical_bytes,
                    unit_size: map.unit_size_bytes,
                    content_sha256: map.content_sha256.trim_start_matches("sha256:").into(),
                },
                provider: "fixture".into(),
                authorization: None,
            })
            .await
            .unwrap();
        service
            .install_layer_capability(target, runtime, map, None)
            .await
            .unwrap();
        resources
    }
    fn demand(target: &str) -> RemoteLoadRequest {
        RemoteLoadRequest {
            target_id: target.into(),
            target_offset: 4096,
            length: 4096,
            origin: "uffd_missing".into(),
        }
    }

    #[tokio::test]
    async fn two_independent_nodes_publish_then_replay_remote_hint_into_new_grants() {
        let root_a = TestRoot::new();
        let root_b = TestRoot::new();
        let provider = Arc::new(PayloadProvider {
            payload: vec![0xa7; 4096],
            reads: AtomicUsize::new(0),
        });
        let map = layer(&provider.payload);
        let store = Arc::new(MemoryMetadataStore::default());
        let subject = subject();
        let runtime_a = RuntimeIdentity::new("node-a-runtime");
        let service_a = node(&root_a, "node-a", provider.clone());
        let io_a = ObjectStoreImageMetadata::new(store.clone());
        service_a
            .configure_snapshot_image_metadata(io_a.clone(), io_a)
            .unwrap();
        assert_eq!(
            service_a.begin_snapshot_hotset(
                &subject.snapshot_id,
                &subject.spec_checksum,
                &runtime_a
            ),
            0
        );
        let _resources_a = grant(&service_a, &runtime_a, "a-original-target", map.clone()).await;
        assert_eq!(
            service_a
                .remote_load(&runtime_a, demand("a-original-target"))
                .await
                .unwrap(),
            RangeLoadOutcome::Loaded
        );
        assert_eq!(provider.reads.load(Ordering::SeqCst), 1);
        assert_eq!(service_a.finish_snapshot_hotset(&runtime_a, true), 1);
        wait_until(|| store.writes.load(Ordering::SeqCst) == 1).await;
        let weak_a = Arc::downgrade(&service_a);
        drop(service_a);
        assert!(weak_a.upgrade().is_none());

        // B has no A cache/FDs/runtime identity. It opens the same immutable layer
        // under its own capability and downloads DATA only through that new grant.
        let service_b = node(&root_b, "node-b", provider.clone());
        let io_b = ObjectStoreImageMetadata::new(store.clone());
        service_b
            .configure_snapshot_image_metadata(io_b.clone(), io_b)
            .unwrap();
        let runtime_b = RuntimeIdentity::new("different-node-b-runtime");
        assert_eq!(
            service_b.begin_snapshot_hotset(
                &subject.snapshot_id,
                &subject.spec_checksum,
                &runtime_b
            ),
            0
        );
        let _resources_b = grant(&service_b, &runtime_b, "b-new-target", map).await;
        let gets_before = store.reads.load(Ordering::SeqCst);
        assert_eq!(
            service_b
                .launch_snapshot_hotset_prefetch(&runtime_b, 0, 16 * 1024 * 1024)
                .await,
            (0, 0)
        );
        assert_eq!(
            store.reads.load(Ordering::SeqCst),
            gets_before,
            "default replay must not GET metadata"
        );
        // Remote hints are loaded even for a budget-too-small attempt; their DATA
        // cannot bypass the actual replay byte budget.
        assert_eq!(
            service_b
                .launch_snapshot_hotset_prefetch(&runtime_b, 1, 4095)
                .await,
            (0, 0)
        );
        assert_eq!(provider.reads.load(Ordering::SeqCst), 1);
        let gets_after_hint = store.reads.load(Ordering::SeqCst);
        assert_eq!(gets_after_hint, gets_before + 1);
        assert!(
            !root_b.0.join("snapshot-hotsets").exists(),
            "adopting remote hints must not create/fsync local profile files on the start path"
        );
        assert_eq!(
            service_b
                .launch_snapshot_hotset_prefetch(&runtime_b, 1, 4096)
                .await,
            (1, 4096)
        );
        assert_eq!(
            store.reads.load(Ordering::SeqCst),
            gets_after_hint,
            "loaded local profile must avoid another GET"
        );
        let backing = service_b
            .backing_for_target("b-new-target", &runtime_b)
            .await
            .unwrap();
        let key = DataRangeKey::new(&backing.backing_id, 4096, 4096).unwrap();
        tokio::time::timeout(Duration::from_secs(2), async {
            while backing.downloads.status(&key).await.state != DownloadState::Resident {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        let mut bytes = vec![0; 4096];
        backing
            .try_clone_file()
            .unwrap()
            .read_exact_at(&mut bytes, 4096)
            .unwrap();
        assert_eq!(bytes, provider.payload);
        let provider_reads = provider.reads.load(Ordering::SeqCst);
        assert_eq!(provider_reads, 2);
        assert_eq!(
            service_b
                .remote_load(&runtime_b, demand("b-new-target"))
                .await
                .unwrap(),
            RangeLoadOutcome::CacheHit
        );
        assert_eq!(provider.reads.load(Ordering::SeqCst), provider_reads);
        assert_eq!(service_b.finish_snapshot_hotset(&runtime_b, true), 1);
        assert!(
            root_b.0.join("snapshot-hotsets").is_dir(),
            "successful finish retains the existing local profile persistence policy"
        );
        service_b.close().await;
    }

    #[tokio::test]
    async fn pending_metadata_upload_does_not_retain_demand_service_or_backing_registry() {
        let root = TestRoot::new();
        let provider = Arc::new(PayloadProvider {
            payload: vec![0x35; 4096],
            reads: AtomicUsize::new(0),
        });
        let service = node(&root, "upload-fd-test", provider.clone());
        let io = Arc::new(HintIo::default());
        io.block_upload.store(true, Ordering::SeqCst);
        service
            .configure_snapshot_image_metadata(io.clone(), io.clone())
            .unwrap();
        let runtime = RuntimeIdentity::new("upload-does-not-pin-runtime");
        let subject = subject();
        service.begin_snapshot_hotset(&subject.snapshot_id, &subject.spec_checksum, &runtime);
        let _resources = grant(
            &service,
            &runtime,
            "target-not-pinned-by-upload",
            layer(&provider.payload),
        )
        .await;
        service
            .remote_load(&runtime, demand("target-not-pinned-by-upload"))
            .await
            .unwrap();
        assert_eq!(service.finish_snapshot_hotset(&runtime, true), 1);
        wait_until(|| io.active_uploads.load(Ordering::SeqCst) == 1).await;
        let weak_service = Arc::downgrade(&service);
        let weak_registry = Arc::downgrade(&service.backing_registry);
        drop(service);
        assert!(weak_service.upgrade().is_none());
        assert!(weak_registry.upgrade().is_none());
        // Dropping the sender also cancels its hint IO; no metadata task keeps
        // target/backing FDs alive while cancellation drains.
        wait_until(|| io.active_uploads.load(Ordering::SeqCst) == 0).await;
    }
}
