//! Opt-in, process-wide read-path wall-clock profiling.
//!
//! Enabled only by `ROCK_SNAPSHOT_READ_PATH_PROFILE=1`. Labels come from finite
//! enums: object identities, URLs and credentials never enter these metrics.
//! Timings can nest: `registry_range_read` includes auth, header and body work;
//! `registry_auth_exchange` includes token response/body parsing and insertion.
//! Header time includes client scheduling, DNS/connect/TLS and server wait, not
//! just network RTT. Sum durations overlap across concurrent operations and must
//! not be added to infer startup wall time. `busy_seconds_total` is the union of
//! active intervals for one stage, including current operations at scrape time.

use std::fmt::Write;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::Instant;

#[derive(Clone, Copy, Debug)]
#[repr(usize)]
pub(crate) enum ReadPathStage {
    RegistryRangeHeaders,
    RegistryRangeBody,
    RegistryRangeRead,
    RegistryMetadataHeaders,
    RegistryMetadataBody,
    RegistryAuthExchange,
    RegistryAuthCacheLookup,
    ZfileDecode,
    DataIntegrityCheck,
    BackingWrite,
    CacheRead,
    CacheWrite,
}

impl ReadPathStage {
    const ALL: [Self; 12] = [
        Self::RegistryRangeHeaders,
        Self::RegistryRangeBody,
        Self::RegistryRangeRead,
        Self::RegistryMetadataHeaders,
        Self::RegistryMetadataBody,
        Self::RegistryAuthExchange,
        Self::RegistryAuthCacheLookup,
        Self::ZfileDecode,
        Self::DataIntegrityCheck,
        Self::BackingWrite,
        Self::CacheRead,
        Self::CacheWrite,
    ];

    fn label(self) -> &'static str {
        match self {
            Self::RegistryRangeHeaders => "registry_range_headers",
            Self::RegistryRangeBody => "registry_range_body",
            Self::RegistryRangeRead => "registry_range_read",
            Self::RegistryMetadataHeaders => "registry_metadata_headers",
            Self::RegistryMetadataBody => "registry_metadata_body",
            Self::RegistryAuthExchange => "registry_auth_exchange",
            Self::RegistryAuthCacheLookup => "registry_auth_cache_lookup",
            Self::ZfileDecode => "zfile_decode",
            Self::DataIntegrityCheck => "data_integrity_check",
            Self::BackingWrite => "backing_write",
            Self::CacheRead => "cache_read",
            Self::CacheWrite => "cache_write",
        }
    }
}

#[derive(Clone, Copy)]
#[repr(usize)]
pub(crate) enum ReadPathEvent {
    RegistryAuthCacheHit,
    RegistryAuthCacheMiss,
    RegistryUnauthorized,
    RegistryRedirect,
    RegistryBlobRedirectCacheHit,
    RegistryBlobRedirectCacheMiss,
    RegistryBlobRedirectCacheFallback,
}

impl ReadPathEvent {
    const ALL: [Self; 7] = [
        Self::RegistryAuthCacheHit,
        Self::RegistryAuthCacheMiss,
        Self::RegistryUnauthorized,
        Self::RegistryRedirect,
        Self::RegistryBlobRedirectCacheHit,
        Self::RegistryBlobRedirectCacheMiss,
        Self::RegistryBlobRedirectCacheFallback,
    ];

    fn label(self) -> &'static str {
        match self {
            Self::RegistryAuthCacheHit => "registry_auth_cache_hit",
            Self::RegistryAuthCacheMiss => "registry_auth_cache_miss",
            Self::RegistryUnauthorized => "registry_unauthorized",
            Self::RegistryRedirect => "registry_redirect",
            Self::RegistryBlobRedirectCacheHit => "registry_blob_redirect_cache_hit",
            Self::RegistryBlobRedirectCacheMiss => "registry_blob_redirect_cache_miss",
            Self::RegistryBlobRedirectCacheFallback => "registry_blob_redirect_cache_fallback",
        }
    }

    pub(crate) fn record(self) {
        if enabled() {
            profiler().events[self as usize].fetch_add(1, Ordering::Relaxed);
        }
    }
}

#[derive(Clone, Copy, Default)]
struct StageStats {
    count: u64,
    failures: u64,
    nanos: u64,
    max_nanos: u64,
    bytes: u64,
    active: u64,
    max_active: u64,
    busy_nanos: u64,
    busy_since: Option<Instant>,
    busy_observed_until: Option<Instant>,
}

#[derive(Default)]
struct Profiler {
    stages: Mutex<[StageStats; 12]>,
    events: [AtomicU64; 7],
}

fn elapsed_nanos(from: Instant, to: Instant) -> u64 {
    to.saturating_duration_since(from)
        .as_nanos()
        .try_into()
        .unwrap_or(u64::MAX)
}

impl Profiler {
    fn begin(&self, stage: ReadPathStage, now: Instant) {
        let mut stages = self
            .stages
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let stats = &mut stages[stage as usize];
        if stats.active == 0 {
            stats.busy_since = Some(now);
            stats.busy_observed_until = Some(now);
        } else if let Some(since) = &mut stats.busy_since {
            // Concurrent timer starts can acquire this tiny profiling lock
            // out of timestamp order.
            *since = (*since).min(now);
        }
        stats.active = stats.active.saturating_add(1);
        stats.max_active = stats.max_active.max(stats.active);
    }

    fn finish(
        &self,
        stage: ReadPathStage,
        start: Instant,
        end: Instant,
        bytes: u64,
        success: bool,
    ) {
        let mut stages = self
            .stages
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let stats = &mut stages[stage as usize];
        let nanos = elapsed_nanos(start, end);
        stats.count = stats.count.saturating_add(1);
        stats.failures = stats.failures.saturating_add(u64::from(!success));
        stats.nanos = stats.nanos.saturating_add(nanos);
        stats.max_nanos = stats.max_nanos.max(nanos);
        stats.bytes = stats.bytes.saturating_add(bytes);
        stats.busy_observed_until =
            Some(stats.busy_observed_until.map_or(end, |last| last.max(end)));
        stats.active = stats.active.saturating_sub(1);
        if stats.active == 0 {
            if let Some(since) = stats.busy_since.take() {
                // Include a scrape that observed this span as active, even
                // when its completion timestamp preceded lock acquisition.
                let until = stats.busy_observed_until.take().unwrap_or(end);
                stats.busy_nanos = stats.busy_nanos.saturating_add(elapsed_nanos(since, until));
            }
        }
    }

    fn snapshot(&self, now: Instant) -> [StageStats; 12] {
        let mut live = self
            .stages
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        for stats in live.iter_mut() {
            if stats.active > 0 {
                stats.busy_observed_until =
                    Some(stats.busy_observed_until.map_or(now, |last| last.max(now)));
            }
        }
        let mut stages = *live;
        for stats in &mut stages {
            if let Some(since) = stats.busy_since {
                stats.busy_nanos = stats.busy_nanos.saturating_add(elapsed_nanos(
                    since,
                    stats.busy_observed_until.unwrap_or(now),
                ));
            }
        }
        stages
    }

    fn render(&self, output: &mut String) {
        let snapshot = self.snapshot(Instant::now());
        for (name, kind) in [
            ("seconds", "summary"),
            ("seconds_max", "gauge"),
            ("bytes_total", "counter"),
            ("failures_total", "counter"),
            ("active", "gauge"),
            ("max_active", "gauge"),
            ("busy_seconds_total", "counter"),
        ] {
            let _ = writeln!(output, "# TYPE rock_snapd_read_path_{name} {kind}");
        }
        for stage in ReadPathStage::ALL {
            let stats = snapshot[stage as usize];
            let label = stage.label();
            let _ = writeln!(
                output,
                "rock_snapd_read_path_seconds_count{{stage=\"{label}\"}} {}",
                stats.count
            );
            let _ = writeln!(
                output,
                "rock_snapd_read_path_seconds_sum{{stage=\"{label}\"}} {:.9}",
                stats.nanos as f64 / 1e9
            );
            let _ = writeln!(
                output,
                "rock_snapd_read_path_seconds_max{{stage=\"{label}\"}} {:.9}",
                stats.max_nanos as f64 / 1e9
            );
            let _ = writeln!(
                output,
                "rock_snapd_read_path_bytes_total{{stage=\"{label}\"}} {}",
                stats.bytes
            );
            let _ = writeln!(
                output,
                "rock_snapd_read_path_failures_total{{stage=\"{label}\"}} {}",
                stats.failures
            );
            let _ = writeln!(
                output,
                "rock_snapd_read_path_active{{stage=\"{label}\"}} {}",
                stats.active
            );
            let _ = writeln!(
                output,
                "rock_snapd_read_path_max_active{{stage=\"{label}\"}} {}",
                stats.max_active
            );
            let _ = writeln!(
                output,
                "rock_snapd_read_path_busy_seconds_total{{stage=\"{label}\"}} {:.9}",
                stats.busy_nanos as f64 / 1e9
            );
        }
        let _ = writeln!(output, "# TYPE rock_snapd_read_path_events_total counter");
        for event in ReadPathEvent::ALL {
            let _ = writeln!(
                output,
                "rock_snapd_read_path_events_total{{event=\"{}\"}} {}",
                event.label(),
                self.events[event as usize].load(Ordering::Relaxed)
            );
        }
    }
}

fn enabled() -> bool {
    static ENABLED: OnceLock<bool> = OnceLock::new();
    *ENABLED.get_or_init(|| {
        std::env::var_os("ROCK_SNAPSHOT_READ_PATH_PROFILE").as_deref()
            == Some(std::ffi::OsStr::new("1"))
    })
}

fn profiler() -> &'static Profiler {
    static PROFILER: OnceLock<Profiler> = OnceLock::new();
    PROFILER.get_or_init(Profiler::default)
}

pub(crate) struct ReadPathTimer {
    stage: ReadPathStage,
    started: Option<Instant>,
}

impl ReadPathTimer {
    pub(crate) fn start(stage: ReadPathStage) -> Self {
        let started = enabled().then(Instant::now);
        if let Some(now) = started {
            profiler().begin(stage, now);
        }
        Self { stage, started }
    }

    pub(crate) fn finish(mut self, bytes: u64, success: bool) {
        if let Some(started) = self.started.take() {
            profiler().finish(self.stage, started, Instant::now(), bytes, success);
        }
    }
}

impl Drop for ReadPathTimer {
    fn drop(&mut self) {
        if let Some(started) = self.started.take() {
            profiler().finish(self.stage, started, Instant::now(), 0, false);
        }
    }
}

pub(crate) fn render(output: &mut String) {
    let _ = writeln!(output, "# TYPE rock_snapd_read_path_profile_enabled gauge");
    let _ = writeln!(
        output,
        "rock_snapd_read_path_profile_enabled {}",
        u8::from(enabled())
    );
    if enabled() {
        profiler().render(output);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn overlapping_spans_keep_sum_union_and_active_distinct() {
        let profiler = Profiler::default();
        let start = Instant::now();
        let stage = ReadPathStage::RegistryRangeHeaders;
        profiler.begin(stage, start);
        profiler.begin(stage, start + Duration::from_millis(10));
        profiler.finish(stage, start, start + Duration::from_millis(30), 7, true);
        let active = profiler.snapshot(start + Duration::from_millis(40))[stage as usize];
        assert_eq!(active.active, 1);
        assert_eq!(active.busy_nanos, 40_000_000);
        profiler.finish(
            stage,
            start + Duration::from_millis(10),
            start + Duration::from_millis(50),
            3,
            false,
        );
        let stats = profiler.snapshot(start + Duration::from_millis(60))[stage as usize];
        assert_eq!((stats.count, stats.failures, stats.bytes), (2, 1, 10));
        assert_eq!(
            (stats.nanos, stats.max_nanos, stats.busy_nanos),
            (70_000_000, 40_000_000, 50_000_000)
        );
        assert_eq!((stats.active, stats.max_active), (0, 2));
    }

    #[test]
    fn rendering_uses_only_finite_stage_and_event_labels() {
        let profiler = Profiler::default();
        let mut output = String::new();
        profiler.render(&mut output);
        assert_eq!(
            output
                .matches("rock_snapd_read_path_seconds_count{stage=")
                .count(),
            ReadPathStage::ALL.len()
        );
        assert_eq!(
            output
                .matches("rock_snapd_read_path_events_total{event=")
                .count(),
            ReadPathEvent::ALL.len()
        );
        assert!(!output.contains("repository="));
        assert!(!output.contains("url="));
    }
}
