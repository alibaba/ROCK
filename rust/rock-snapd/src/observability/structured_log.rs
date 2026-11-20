//! Bounded structured JSONL events for the Rust snapd daemon.
//!
//! The event schema intentionally has no arbitrary message, URL, header,
//! credential or payload field.  Callers may attach only correlation
//! identities that the snapshot protocol already treats as non-secret.

use std::fmt;
use std::io::{self, Write};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::Serialize;

const MAX_CORRELATION_BYTES: usize = 4096;
const MAX_REQUEST_ID_BYTES: usize = 128;

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum StructuredLogLevel {
    Info,
    Warning,
    Error,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum StructuredEventName {
    DaemonLifecycle,
    HttpRequestCompleted,
    RemoteLoadCompleted,
    UploadCompleted,
    RuntimeRecoveryCompleted,
    RuntimeStartStageCompleted,
    LatestMemoryPrefetchCompleted,
    LatestMemoryPrefetchAdmissionCompleted,
    P2PProbeCompleted,
    P2PRangeCompleted,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum StructuredComponent {
    Daemon,
    Http,
    RemoteLoad,
    Upload,
    Recovery,
    Runtime,
    P2P,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum StructuredOutcome {
    Started,
    Success,
    Rejected,
    Error,
    Stopped,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct StructuredEvent {
    pub timestamp_unix_ms: u64,
    pub level: StructuredLogLevel,
    pub event: StructuredEventName,
    pub component: StructuredComponent,
    pub outcome: StructuredOutcome,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub operation: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error_code: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub snapshot_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub layer_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub object_sha256: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sandbox_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub request_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub range_offset: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub range_length: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub phase: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub duration_ms: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub duration_us: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub total_duration_ms: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub attempts: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub layer_count: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub range_count: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data_range_count: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub unique_object_count: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub prefetch_decision: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub failure_class: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub prefetch_concurrency: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub prefetch_byte_limit: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub planned_ranges: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub planned_objects: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub planned_bytes: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub attempted_ranges: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub completed_ranges: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub failed_ranges: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub attempted_objects: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub completed_objects: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub failed_objects: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub attempted_bytes: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub completed_bytes: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub failed_bytes: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub node_active_at_start: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub node_max_active: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub caller_cancelled: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub accepted_drained: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub vmm_pid: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub host_port: Option<u16>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub vsock_cid: Option<u32>,
}

impl StructuredEvent {
    pub fn new(
        level: StructuredLogLevel,
        event: StructuredEventName,
        component: StructuredComponent,
        outcome: StructuredOutcome,
    ) -> Self {
        Self {
            timestamp_unix_ms: now_unix_ms(),
            level,
            event,
            component,
            outcome,
            operation: None,
            error_code: None,
            snapshot_id: None,
            layer_id: None,
            object_sha256: None,
            sandbox_id: None,
            request_id: None,
            range_offset: None,
            range_length: None,
            phase: None,
            duration_ms: None,
            duration_us: None,
            total_duration_ms: None,
            attempts: None,
            layer_count: None,
            range_count: None,
            data_range_count: None,
            unique_object_count: None,
            prefetch_decision: None,
            failure_class: None,
            prefetch_concurrency: None,
            prefetch_byte_limit: None,
            planned_ranges: None,
            planned_objects: None,
            planned_bytes: None,
            attempted_ranges: None,
            completed_ranges: None,
            failed_ranges: None,
            attempted_objects: None,
            completed_objects: None,
            failed_objects: None,
            attempted_bytes: None,
            completed_bytes: None,
            failed_bytes: None,
            node_active_at_start: None,
            node_max_active: None,
            caller_cancelled: None,
            accepted_drained: None,
            vmm_pid: None,
            host_port: None,
            vsock_cid: None,
        }
    }

    pub fn operation(mut self, operation: &'static str) -> Self {
        self.operation = Some(operation);
        self
    }

    pub fn error_code(mut self, error_code: &'static str) -> Self {
        self.error_code = Some(error_code);
        self
    }

    pub fn snapshot_id(mut self, value: &str) -> Self {
        self.snapshot_id = bounded_identity(value, MAX_CORRELATION_BYTES);
        self
    }

    pub fn layer_id(mut self, value: &str) -> Self {
        self.layer_id = bounded_identity(value, MAX_CORRELATION_BYTES);
        self
    }

    pub fn object_sha256(mut self, value: &str) -> Self {
        self.object_sha256 = bounded_identity(value, 128);
        self
    }

    pub fn sandbox_id(mut self, value: &str) -> Self {
        self.sandbox_id = bounded_identity(value, MAX_CORRELATION_BYTES);
        self
    }

    pub fn request_id(mut self, value: &str) -> Self {
        self.request_id = bounded_identity(value, MAX_REQUEST_ID_BYTES);
        self
    }

    pub fn range(mut self, offset: u64, length: u64) -> Self {
        self.range_offset = Some(offset);
        self.range_length = Some(length);
        self
    }

    pub fn phase(mut self, value: &'static str) -> Self {
        self.phase = Some(value);
        self
    }

    pub fn duration(mut self, value: std::time::Duration) -> Self {
        self.duration_ms = Some(duration_ms(value));
        self
    }

    pub fn duration_micros(mut self, value: std::time::Duration) -> Self {
        self.duration_us = Some(value.as_micros().try_into().unwrap_or(u64::MAX));
        self
    }

    pub fn total_duration(mut self, value: std::time::Duration) -> Self {
        self.total_duration_ms = Some(duration_ms(value));
        self
    }

    pub fn attempts(mut self, value: u64) -> Self {
        self.attempts = Some(value);
        self
    }

    pub fn graph_counts(
        mut self,
        layers: u64,
        ranges: u64,
        data_ranges: u64,
        unique_objects: u64,
    ) -> Self {
        self.layer_count = Some(layers);
        self.range_count = Some(ranges);
        self.data_range_count = Some(data_ranges);
        self.unique_object_count = Some(unique_objects);
        self
    }

    #[allow(clippy::too_many_arguments)]
    pub fn prefetch_summary(
        mut self,
        decision: &'static str,
        concurrency: u64,
        byte_limit: u64,
        planned_ranges: u64,
        planned_objects: u64,
        planned_bytes: u64,
        attempted_ranges: u64,
        completed_ranges: u64,
        failed_ranges: u64,
        attempted_objects: u64,
        completed_objects: u64,
        failed_objects: u64,
        attempted_bytes: u64,
        completed_bytes: u64,
        failed_bytes: u64,
    ) -> Self {
        self.prefetch_decision = Some(decision);
        self.prefetch_concurrency = Some(concurrency);
        self.prefetch_byte_limit = Some(byte_limit);
        self.planned_ranges = Some(planned_ranges);
        self.planned_objects = Some(planned_objects);
        self.planned_bytes = Some(planned_bytes);
        self.attempted_ranges = Some(attempted_ranges);
        self.completed_ranges = Some(completed_ranges);
        self.failed_ranges = Some(failed_ranges);
        self.attempted_objects = Some(attempted_objects);
        self.completed_objects = Some(completed_objects);
        self.failed_objects = Some(failed_objects);
        self.attempted_bytes = Some(attempted_bytes);
        self.completed_bytes = Some(completed_bytes);
        self.failed_bytes = Some(failed_bytes);
        self
    }

    pub fn failure_class(mut self, value: &'static str) -> Self {
        self.failure_class = Some(value);
        self
    }

    pub fn prefetch_admission(
        mut self,
        node_active_at_start: u64,
        node_max_active: u64,
        caller_cancelled: bool,
        accepted_drained: bool,
    ) -> Self {
        self.node_active_at_start = Some(node_active_at_start);
        self.node_max_active = Some(node_max_active);
        self.caller_cancelled = Some(caller_cancelled);
        self.accepted_drained = Some(accepted_drained);
        self
    }

    pub fn vmm_pid(mut self, value: u32) -> Self {
        self.vmm_pid = Some(value);
        self
    }

    pub fn host_port(mut self, value: u16) -> Self {
        self.host_port = Some(value);
        self
    }

    pub fn vsock_cid(mut self, value: u32) -> Self {
        self.vsock_cid = Some(value);
        self
    }
}

fn duration_ms(value: std::time::Duration) -> u64 {
    value.as_millis().try_into().unwrap_or(u64::MAX)
}

pub trait StructuredLogWriter: fmt::Debug + Send + Sync + 'static {
    fn write_line(&self, line: &[u8]) -> io::Result<()>;
}

#[derive(Debug, Default)]
pub struct StderrStructuredLogWriter;

impl StructuredLogWriter for StderrStructuredLogWriter {
    fn write_line(&self, line: &[u8]) -> io::Result<()> {
        let stderr = io::stderr();
        let mut locked = stderr.lock();
        locked.write_all(line)?;
        locked.flush()
    }
}

#[derive(Debug)]
pub struct StructuredLogger {
    writer: Arc<dyn StructuredLogWriter>,
}

impl StructuredLogger {
    pub fn stderr() -> Arc<Self> {
        Self::new(Arc::new(StderrStructuredLogWriter))
    }

    pub fn new(writer: Arc<dyn StructuredLogWriter>) -> Arc<Self> {
        Arc::new(Self { writer })
    }

    pub fn emit(&self, event: &StructuredEvent) -> io::Result<()> {
        let mut line = serde_json::to_vec(event).map_err(io::Error::other)?;
        line.push(b'\n');
        self.writer.write_line(&line)
    }
}

pub fn emit_stderr(event: StructuredEvent) {
    let _ = StructuredLogger::stderr().emit(&event);
}

fn bounded_identity(value: &str, maximum: usize) -> Option<String> {
    (!value.is_empty() && value.len() <= maximum && !value.chars().any(char::is_control))
        .then(|| value.to_owned())
}

fn now_unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .try_into()
        .unwrap_or(u64::MAX)
}
