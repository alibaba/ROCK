//! Fail-closed Admin-to-Rust-snapd HTTP/1.1 boundary for snapshot control.

use std::sync::Arc;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use async_trait::async_trait;
use hyper::body::HttpBody;
use hyper::header::{HeaderMap, HeaderValue, CONTENT_LENGTH, CONTENT_TYPE};
use hyper::{Body, Method, Request, Response, StatusCode};
use serde::Serialize;
use serde_json::Value;
use thiserror::Error;

use crate::endpoints::control::protocol::{
    canonical_json, parse_strict, AbortDeleteFenceRequest, CreateBaseImageRequest,
    DeleteSandboxRequest, FinalizePublishedSnapshotRequest, FinalizePublishedSnapshotResult,
    ForkLocalReadyResult, ForkSandboxRequest, ImportOciSnapshotRequest, ImportOciSnapshotResult,
    InvalidateSnapshotRequest, InvalidateSnapshotResult, MutationAcceptedResult,
    PluggerControlRequest, PluggerControlResult, ResolveBaseImageRequest, ResolvedBaseImageResult,
    SandboxStartedResult, SandboxStatusResult, SnapdErrorCode, SnapdReply, SnapshotFenceRequest,
    SnapshotLocalReadyResult, SnapshotSandboxRequest, SnapshotStatusResult,
    SnapshotUsageCheckResult, StartSandboxFromSnapshotRequest, StopSandboxRequest,
    SNAPD_PROTOCOL_VERSION,
};
use crate::observability::metrics::{OperationMetric, OperationOutcome, SnapdMetrics};
use crate::observability::structured_log::{
    StructuredComponent, StructuredEvent, StructuredEventName, StructuredLogLevel,
    StructuredOutcome,
};
use crate::snapshot::image::service::BaseImageIngestionService;
use crate::vm::error::OperationError as SnapshotControlError;
use crate::vm::repository::VmService;

const MAX_BODY_BYTES: u64 = 16 * 1024 * 1024;
const MAX_HEADER_COUNT: usize = 128;
const MAX_HEADER_BYTES: usize = 64 * 1024;
const MAX_REQUEST_ID_BYTES: usize = 128;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RequestMetadata {
    pub request_id: String,
    pub deadline_unix_ms: u64,
}

#[derive(Clone, Debug, Error, Eq, PartialEq)]
pub enum HttpBoundaryError {
    #[error("invalid HTTP request: {0}")]
    Invalid(String),
    #[error("HTTP route was not found")]
    NotFound,
    #[error("HTTP method is not allowed for this route")]
    MethodNotAllowed,
    #[error("request body exceeds the bounded limit")]
    BodyTooLarge,
    #[error("request deadline has expired")]
    DeadlineExpired,
}

#[async_trait]
pub trait SnapdControlApi: Send + Sync + 'static {
    async fn import_oci_snapshot(
        &self,
        _request: ImportOciSnapshotRequest,
        _deadline_unix_ms: u64,
    ) -> Result<ImportOciSnapshotResult, SnapshotControlError> {
        Err(SnapshotControlError::InvalidState(
            "OCI snapshot import is not configured".into(),
        ))
    }

    async fn create_base_image(
        &self,
        request: CreateBaseImageRequest,
    ) -> Result<MutationAcceptedResult, SnapshotControlError>;

    async fn resolve_base_image(
        &self,
        request: ResolveBaseImageRequest,
    ) -> Result<ResolvedBaseImageResult, SnapshotControlError>;

    async fn snapshot_sandbox(
        &self,
        source_id: &str,
        request: SnapshotSandboxRequest,
        deadline_unix_ms: u64,
    ) -> Result<SnapshotLocalReadyResult, SnapshotControlError>;

    async fn fork_sandbox(
        &self,
        source_id: &str,
        request: ForkSandboxRequest,
        deadline_unix_ms: u64,
    ) -> Result<ForkLocalReadyResult, SnapshotControlError>;

    async fn start_sandbox_from_snapshot(
        &self,
        snapshot_id: &str,
        request_id: &str,
        request: StartSandboxFromSnapshotRequest,
        deadline_unix_ms: u64,
    ) -> Result<SandboxStartedResult, SnapshotControlError>;

    async fn start_sandbox_from_snapshot_async(
        self: Arc<Self>,
        _snapshot_id: &str,
        _request_id: &str,
        _request: StartSandboxFromSnapshotRequest,
        _deadline_unix_ms: u64,
    ) -> Result<MutationAcceptedResult, SnapshotControlError> {
        Err(SnapshotControlError::InvalidState(
            "asynchronous start is not supported".into(),
        ))
    }

    async fn snapshot_status(
        &self,
        snapshot_id: &str,
    ) -> Result<SnapshotStatusResult, SnapshotControlError>;

    async fn sandbox_status(
        &self,
        sandbox_id: &str,
    ) -> Result<SandboxStatusResult, SnapshotControlError>;

    async fn plugger_control(
        &self,
        _sandbox_id: &str,
        _request: PluggerControlRequest,
        _deadline_unix_ms: u64,
    ) -> Result<PluggerControlResult, SnapshotControlError> {
        Err(SnapshotControlError::InvalidState(
            "independent snapd-plugger control is unavailable".into(),
        ))
    }

    async fn background_prefetch_status(
        &self,
        _sandbox_id: &str,
    ) -> Result<serde_json::Value, SnapshotControlError> {
        Ok(serde_json::Value::Null)
    }

    async fn stop_sandbox(
        &self,
        sandbox_id: &str,
        request: &StopSandboxRequest,
    ) -> Result<MutationAcceptedResult, SnapshotControlError>;

    async fn delete_sandbox(
        &self,
        sandbox_id: &str,
        request: &DeleteSandboxRequest,
    ) -> Result<MutationAcceptedResult, SnapshotControlError>;

    async fn finalize_published_snapshot(
        &self,
        snapshot_id: &str,
        request: &FinalizePublishedSnapshotRequest,
    ) -> Result<FinalizePublishedSnapshotResult, SnapshotControlError>;

    async fn check_snapshot_usage(
        &self,
        snapshot_id: &str,
        request: &SnapshotFenceRequest,
    ) -> Result<SnapshotUsageCheckResult, SnapshotControlError>;

    async fn abort_delete_fence(
        &self,
        snapshot_id: &str,
        request: &AbortDeleteFenceRequest,
    ) -> Result<MutationAcceptedResult, SnapshotControlError>;

    async fn invalidate_snapshot(
        &self,
        snapshot_id: &str,
        request: &InvalidateSnapshotRequest,
    ) -> Result<InvalidateSnapshotResult, SnapshotControlError>;
}

/// Protocol adapter that forwards each request to its domain service.
pub struct SnapdControlServices {
    vm: Arc<VmService>,
    images: Arc<BaseImageIngestionService>,
}

impl SnapdControlServices {
    pub fn new(vm: Arc<VmService>, images: Arc<BaseImageIngestionService>) -> Arc<Self> {
        Arc::new(Self { vm, images })
    }
}

#[async_trait]
impl SnapdControlApi for SnapdControlServices {
    async fn import_oci_snapshot(
        &self,
        request: ImportOciSnapshotRequest,
        deadline_unix_ms: u64,
    ) -> Result<ImportOciSnapshotResult, SnapshotControlError> {
        let status = self
            .vm
            .import_oci_snapshot(&request.oci_ref, deadline_unix_ms)
            .await?;
        ImportOciSnapshotResult::try_from(status).map_err(SnapshotControlError::InvalidState)
    }

    async fn start_sandbox_from_snapshot_async(
        self: Arc<Self>,
        snapshot_id: &str,
        request_id: &str,
        request: StartSandboxFromSnapshotRequest,
        deadline_unix_ms: u64,
    ) -> Result<MutationAcceptedResult, SnapshotControlError> {
        self.vm
            .clone()
            .start_sandbox_from_snapshot_async(
                snapshot_id,
                request_id,
                request.into(),
                deadline_unix_ms,
            )
            .await
            .map(Into::into)
    }

    async fn create_base_image(
        &self,
        request: CreateBaseImageRequest,
    ) -> Result<MutationAcceptedResult, SnapshotControlError> {
        self.vm
            .create_base_image(request.into())
            .await
            .map(Into::into)
    }

    async fn resolve_base_image(
        &self,
        request: ResolveBaseImageRequest,
    ) -> Result<ResolvedBaseImageResult, SnapshotControlError> {
        self.images
            .resolve(request.into())
            .await
            .map(Into::into)
            .map_err(Into::into)
    }

    async fn snapshot_sandbox(
        &self,
        source_id: &str,
        request: SnapshotSandboxRequest,
        deadline_unix_ms: u64,
    ) -> Result<SnapshotLocalReadyResult, SnapshotControlError> {
        self.vm
            .snapshot_sandbox(source_id, request.into(), deadline_unix_ms)
            .await
            .map(Into::into)
    }

    async fn fork_sandbox(
        &self,
        source_id: &str,
        request: ForkSandboxRequest,
        deadline_unix_ms: u64,
    ) -> Result<ForkLocalReadyResult, SnapshotControlError> {
        self.vm
            .fork_sandbox(source_id, request.into(), deadline_unix_ms)
            .await
            .map(Into::into)
    }

    async fn start_sandbox_from_snapshot(
        &self,
        snapshot_id: &str,
        request_id: &str,
        request: StartSandboxFromSnapshotRequest,
        deadline_unix_ms: u64,
    ) -> Result<SandboxStartedResult, SnapshotControlError> {
        self.vm
            .start_sandbox_from_snapshot(snapshot_id, request_id, request.into(), deadline_unix_ms)
            .await
            .map(Into::into)
    }

    async fn snapshot_status(
        &self,
        snapshot_id: &str,
    ) -> Result<SnapshotStatusResult, SnapshotControlError> {
        self.vm.snapshot_status(snapshot_id).await.map(Into::into)
    }

    async fn sandbox_status(
        &self,
        sandbox_id: &str,
    ) -> Result<SandboxStatusResult, SnapshotControlError> {
        self.vm.sandbox_status(sandbox_id).await.map(Into::into)
    }

    async fn plugger_control(
        &self,
        sandbox_id: &str,
        request: PluggerControlRequest,
        deadline_unix_ms: u64,
    ) -> Result<PluggerControlResult, SnapshotControlError> {
        self.vm
            .plugger_control(
                sandbox_id,
                &request.action,
                &request.payload,
                deadline_unix_ms,
            )
            .await
            .map(Into::into)
    }

    async fn background_prefetch_status(
        &self,
        sandbox_id: &str,
    ) -> Result<serde_json::Value, SnapshotControlError> {
        self.vm.background_prefetch_status(sandbox_id).await
    }

    async fn stop_sandbox(
        &self,
        sandbox_id: &str,
        request: &StopSandboxRequest,
    ) -> Result<MutationAcceptedResult, SnapshotControlError> {
        self.vm
            .stop_sandbox(sandbox_id, &request.into())
            .await
            .map(Into::into)
    }

    async fn delete_sandbox(
        &self,
        sandbox_id: &str,
        _request: &DeleteSandboxRequest,
    ) -> Result<MutationAcceptedResult, SnapshotControlError> {
        self.vm.delete_sandbox(sandbox_id).await.map(Into::into)
    }

    async fn finalize_published_snapshot(
        &self,
        snapshot_id: &str,
        request: &FinalizePublishedSnapshotRequest,
    ) -> Result<FinalizePublishedSnapshotResult, SnapshotControlError> {
        self.vm
            .finalize_published_snapshot(snapshot_id, &request.into())
            .await
            .map(Into::into)
    }

    async fn check_snapshot_usage(
        &self,
        snapshot_id: &str,
        request: &SnapshotFenceRequest,
    ) -> Result<SnapshotUsageCheckResult, SnapshotControlError> {
        self.vm
            .check_snapshot_usage(snapshot_id, &request.into())
            .await
            .map(Into::into)
    }

    async fn abort_delete_fence(
        &self,
        snapshot_id: &str,
        request: &AbortDeleteFenceRequest,
    ) -> Result<MutationAcceptedResult, SnapshotControlError> {
        self.vm
            .abort_delete_fence(snapshot_id, &request.into())
            .await
            .map(Into::into)
    }

    async fn invalidate_snapshot(
        &self,
        snapshot_id: &str,
        request: &InvalidateSnapshotRequest,
    ) -> Result<InvalidateSnapshotResult, SnapshotControlError> {
        self.vm
            .invalidate_snapshot(snapshot_id, &request.into())
            .await
            .map(Into::into)
    }
}

pub struct SnapdControlHttpService<A: SnapdControlApi> {
    api: Arc<A>,
    metrics: Option<Arc<SnapdMetrics>>,
}

impl<A: SnapdControlApi> SnapdControlHttpService<A> {
    pub fn new(api: Arc<A>) -> Arc<Self> {
        Self::new_with_metrics(api, None)
    }

    pub fn new_with_metrics(api: Arc<A>, metrics: Option<Arc<SnapdMetrics>>) -> Arc<Self> {
        Arc::new(Self { api, metrics })
    }

    pub(crate) async fn handle(&self, request: Request<Body>) -> Response<Body> {
        if is_diagnostics_path(request.uri().path()) {
            return diagnostic_response(request, false, self.metrics.as_deref());
        }
        self.handle_control(request).await
    }

    async fn handle_control(&self, request: Request<Body>) -> Response<Body> {
        let operation = http_operation(request.method(), request.uri().path());
        let request_id = bounded_request_id(request.headers()).map(str::to_owned);
        let snapshot_id = http_snapshot_id(request.uri().path()).map(str::to_owned);
        let started = Instant::now();
        let response = self.handle_inner(request).await;
        if let Some(metrics) = &self.metrics {
            let outcome = if response.status().is_success() {
                OperationOutcome::Success
            } else if response.status().is_client_error() {
                OperationOutcome::Rejected
            } else {
                OperationOutcome::Error
            };
            let elapsed = started.elapsed();
            metrics.observe_operation(OperationMetric::HttpControl, outcome, elapsed);
            if operation != OperationMetric::HttpControl {
                metrics.observe_operation(operation, outcome, elapsed);
            }
            let (level, structured_outcome, error_code) = if response.status().is_success() {
                (StructuredLogLevel::Info, StructuredOutcome::Success, None)
            } else if response.status().is_client_error() {
                (
                    StructuredLogLevel::Warning,
                    StructuredOutcome::Rejected,
                    Some("http_client_error"),
                )
            } else {
                (
                    StructuredLogLevel::Error,
                    StructuredOutcome::Error,
                    Some("http_server_error"),
                )
            };
            let mut event = StructuredEvent::new(
                level,
                StructuredEventName::HttpRequestCompleted,
                StructuredComponent::Http,
                structured_outcome,
            )
            .operation(operation.label());
            if let Some(value) = error_code {
                event = event.error_code(value);
            }
            if let Some(value) = request_id.as_deref() {
                event = event.request_id(value);
            }
            if let Some(value) = snapshot_id.as_deref() {
                event = event.snapshot_id(value);
            }
            metrics.emit_structured(event);
        }
        response
    }

    async fn handle_inner(&self, request: Request<Body>) -> Response<Body> {
        let fallback_request_id = bounded_request_id(request.headers()).unwrap_or("unknown");
        if let Err(error) = validate_header_bounds(request.headers()) {
            return boundary_error(fallback_request_id, error);
        }
        let metadata = match request_metadata(request.headers()) {
            Ok(metadata) => metadata,
            Err(error) => return boundary_error(fallback_request_id, error),
        };
        let (parts, body) = request.into_parts();
        let success_status =
            if parts.method == Method::POST && parts.uri.path().ends_with("/start-async") {
                StatusCode::ACCEPTED
            } else {
                StatusCode::OK
            };
        let result = self
            .dispatch(
                parts.method,
                parts.uri.path(),
                parts.uri.query(),
                parts.headers,
                body,
                &metadata,
            )
            .await;
        match result {
            Ok(value) => reply_response(
                success_status,
                SnapdReply {
                    request_id: metadata.request_id,
                    code: SnapdErrorCode::Ok,
                    retryable: false,
                    error_message: None,
                    result: Some(value),
                },
            ),
            Err(DispatchError::Boundary(error)) => boundary_error(&metadata.request_id, error),
            Err(DispatchError::SnapshotControl(error)) => {
                snapshot_control_error(&metadata.request_id, error)
            }
        }
    }

    async fn dispatch(
        &self,
        method: Method,
        path: &str,
        query: Option<&str>,
        headers: HeaderMap,
        body: Body,
        metadata: &RequestMetadata,
    ) -> Result<Value, DispatchError> {
        let segments = path.split('/').collect::<Vec<_>>();
        match (method, segments.as_slice()) {
            (Method::POST, ["", "v1", "snapd", "snapshots", "import-oci"]) => {
                reject_query(query)?;
                let request = read_json::<ImportOciSnapshotRequest>(headers, body).await?;
                to_value(
                    self.api
                        .import_oci_snapshot(request, metadata.deadline_unix_ms)
                        .await?,
                )
            }
            (Method::POST, ["", "v1", "snapd", "base-images", "resolve"]) => {
                reject_query(query)?;
                let request = read_json::<ResolveBaseImageRequest>(headers, body).await?;
                to_value(self.api.resolve_base_image(request).await?)
            }
            (Method::POST, ["", "v1", "snapd", "base-images"]) => {
                reject_query(query)?;
                let request = read_json::<CreateBaseImageRequest>(headers, body).await?;
                to_value(self.api.create_base_image(request).await?)
            }
            (Method::POST, ["", "v1", "snapd", "sandboxes", source, "snapshot"]) => {
                reject_query(query)?;
                validate_path_identity(source)?;
                let request = read_json::<SnapshotSandboxRequest>(headers, body).await?;
                to_value(
                    self.api
                        .snapshot_sandbox(source, request, metadata.deadline_unix_ms)
                        .await?,
                )
            }
            (Method::POST, ["", "v1", "snapd", "sandboxes", source, "fork"]) => {
                reject_query(query)?;
                validate_path_identity(source)?;
                let request = read_json::<ForkSandboxRequest>(headers, body).await?;
                to_value(
                    self.api
                        .fork_sandbox(source, request, metadata.deadline_unix_ms)
                        .await?,
                )
            }
            (Method::POST, ["", "v1", "snapd", "sandboxes", sandbox, "stop"]) => {
                reject_query(query)?;
                validate_path_identity(sandbox)?;
                let request = read_json::<StopSandboxRequest>(headers, body).await?;
                to_value(self.api.stop_sandbox(sandbox, &request).await?)
            }
            (Method::DELETE, ["", "v1", "snapd", "sandboxes", sandbox]) => {
                reject_query(query)?;
                validate_path_identity(sandbox)?;
                let request = read_json::<DeleteSandboxRequest>(headers, body).await?;
                to_value(self.api.delete_sandbox(sandbox, &request).await?)
            }
            (Method::POST, ["", "v1", "snapd", "snapshots", snapshot, "finalize"]) => {
                reject_query(query)?;
                validate_path_identity(snapshot)?;
                let request = read_json::<FinalizePublishedSnapshotRequest>(headers, body).await?;
                to_value(
                    self.api
                        .finalize_published_snapshot(snapshot, &request)
                        .await?,
                )
            }
            (Method::POST, ["", "v1", "snapd", "snapshots", snapshot, "start-async"]) => {
                reject_query(query)?;
                validate_path_identity(snapshot)?;
                let request = read_json::<StartSandboxFromSnapshotRequest>(headers, body).await?;
                to_value(
                    self.api
                        .clone()
                        .start_sandbox_from_snapshot_async(
                            snapshot,
                            &metadata.request_id,
                            request,
                            metadata.deadline_unix_ms,
                        )
                        .await?,
                )
            }
            (Method::POST, ["", "v1", "snapd", "snapshots", snapshot, "start"]) => {
                reject_query(query)?;
                validate_path_identity(snapshot)?;
                let request = read_json::<StartSandboxFromSnapshotRequest>(headers, body).await?;
                to_value(
                    self.api
                        .start_sandbox_from_snapshot(
                            snapshot,
                            &metadata.request_id,
                            request,
                            metadata.deadline_unix_ms,
                        )
                        .await?,
                )
            }
            (Method::POST, ["", "v1", "snapd", "snapshots", snapshot, "check-usage"]) => {
                reject_query(query)?;
                validate_path_identity(snapshot)?;
                let request = read_json::<SnapshotFenceRequest>(headers, body).await?;
                to_value(self.api.check_snapshot_usage(snapshot, &request).await?)
            }
            (Method::POST, ["", "v1", "snapd", "snapshots", snapshot, "abort-delete-fence"]) => {
                reject_query(query)?;
                validate_path_identity(snapshot)?;
                let request = read_json::<AbortDeleteFenceRequest>(headers, body).await?;
                to_value(self.api.abort_delete_fence(snapshot, &request).await?)
            }
            (Method::POST, ["", "v1", "snapd", "snapshots", snapshot, "invalidate"]) => {
                reject_query(query)?;
                validate_path_identity(snapshot)?;
                let request = read_json::<InvalidateSnapshotRequest>(headers, body).await?;
                to_value(self.api.invalidate_snapshot(snapshot, &request).await?)
            }
            (Method::POST, ["", "v1", "snapd", "sandboxes", sandbox, "plugger"]) => {
                reject_query(query)?;
                validate_path_identity(sandbox)?;
                let request = read_json::<PluggerControlRequest>(headers, body).await?;
                to_value(
                    self.api
                        .plugger_control(sandbox, request, metadata.deadline_unix_ms)
                        .await?,
                )
            }
            (Method::GET, ["", "v1", "snapd", "sandboxes", sandbox, "prefetch"]) => {
                ensure_empty_body(&headers, body).await?;
                validate_path_identity(sandbox)?;
                reject_query(query)?;
                to_value(self.api.background_prefetch_status(sandbox).await?)
            }
            (Method::GET, ["", "v1", "snapd", "sandboxes", sandbox, "status"]) => {
                ensure_empty_body(&headers, body).await?;
                validate_path_identity(sandbox)?;
                reject_query(query)?;
                to_value(self.api.sandbox_status(sandbox).await?)
            }
            (Method::GET, ["", "v1", "snapd", "snapshots", snapshot, "status"]) => {
                ensure_empty_body(&headers, body).await?;
                validate_path_identity(snapshot)?;
                reject_query(query)?;
                to_value(self.api.snapshot_status(snapshot).await?)
            }
            (_, segments) if is_known_control_path(segments) => {
                Err(HttpBoundaryError::MethodNotAllowed.into())
            }
            _ => Err(HttpBoundaryError::NotFound.into()),
        }
    }
}

fn is_known_control_path(segments: &[&str]) -> bool {
    match segments {
        ["", "v1", "snapd", "snapshots", "import-oci"] => true,
        ["", "v1", "snapd", "base-images"] | ["", "v1", "snapd", "base-images", "resolve"] => true,
        ["", "v1", "snapd", "sandboxes", sandbox] => !sandbox.contains(':'),
        ["", "v1", "snapd", "sandboxes", _, "snapshot" | "fork" | "stop" | "status" | "prefetch" | "plugger"] => {
            true
        }
        ["", "v1", "snapd", "snapshots", _, "finalize" | "start" | "start-async" | "check-usage" | "abort-delete-fence"
        | "invalidate" | "status"] => true,
        _ => false,
    }
}

fn validate_header_bounds(headers: &HeaderMap) -> Result<(), HttpBoundaryError> {
    if headers.len() > MAX_HEADER_COUNT
        || headers
            .iter()
            .map(|(name, value)| name.as_str().len() + value.as_bytes().len())
            .sum::<usize>()
            > MAX_HEADER_BYTES
    {
        Err(HttpBoundaryError::Invalid(
            "request headers exceed the bounded limit".into(),
        ))
    } else {
        Ok(())
    }
}

fn is_diagnostics_path(path: &str) -> bool {
    matches!(path, "/healthz" | "/metrics")
}

fn diagnostic_response(
    request: Request<Body>,
    adoption_pending: bool,
    metrics: Option<&SnapdMetrics>,
) -> Response<Body> {
    let path = request.uri().path();
    let query_absent = request.uri().query().is_none();
    match (request.method(), path, query_absent) {
        (&Method::GET, "/healthz", true) => {
            if adoption_pending {
                text_response(StatusCode::SERVICE_UNAVAILABLE, "adoption_pending\n")
            } else {
                text_response(StatusCode::OK, "ok\n")
            }
        }
        (&Method::GET, "/metrics", true) => match metrics {
            Some(metrics) => text_response(StatusCode::OK, &metrics.render(adoption_pending)),
            None => text_response(StatusCode::SERVICE_UNAVAILABLE, "metrics_unavailable\n"),
        },
        _ => text_response(StatusCode::NOT_FOUND, "not_found\n"),
    }
}

fn text_response(status: StatusCode, payload: &str) -> Response<Body> {
    Response::builder()
        .status(status)
        .header("content-type", "text/plain; charset=utf-8")
        .header("cache-control", "no-store")
        .body(Body::from(payload.to_owned()))
        .expect("fixed diagnostics response is valid")
}

fn http_operation(method: &Method, path: &str) -> OperationMetric {
    if matches!(
        path,
        "/v1/snapd/base-images" | "/v1/snapd/base-images/resolve"
    ) {
        OperationMetric::CreateBaseImage
    } else if path.ends_with("/snapshot") {
        OperationMetric::SnapshotSandbox
    } else if path.ends_with("/fork") {
        OperationMetric::ForkSandbox
    } else if path.ends_with("/start") || path.ends_with("/start-async") {
        OperationMetric::StartSandbox
    } else if path.ends_with("/stop") || (*method == Method::DELETE && path.contains("/sandboxes/"))
    {
        OperationMetric::StopSandbox
    } else if path.ends_with("/finalize") {
        OperationMetric::FinalizeSnapshot
    } else if path.ends_with("/invalidate") {
        OperationMetric::DeleteSnapshot
    } else {
        OperationMetric::HttpControl
    }
}

enum DispatchError {
    Boundary(HttpBoundaryError),
    SnapshotControl(SnapshotControlError),
}

impl From<HttpBoundaryError> for DispatchError {
    fn from(error: HttpBoundaryError) -> Self {
        Self::Boundary(error)
    }
}

impl From<SnapshotControlError> for DispatchError {
    fn from(error: SnapshotControlError) -> Self {
        Self::SnapshotControl(error)
    }
}

async fn read_json<T>(headers: HeaderMap, body: Body) -> Result<T, HttpBoundaryError>
where
    T: serde::de::DeserializeOwned + crate::endpoints::control::protocol::ValidateProtocol,
{
    let content_type = single_header(&headers, CONTENT_TYPE)?;
    if content_type != "application/json" {
        return Err(HttpBoundaryError::Invalid(
            "Content-Type must be application/json".into(),
        ));
    }
    let length = content_length(&headers)?;
    if length == 0 {
        return Err(HttpBoundaryError::Invalid("JSON body is empty".into()));
    }
    let payload = read_bounded_body(body, length).await?;
    parse_strict("Admin request", &payload)
        .map_err(|error| HttpBoundaryError::Invalid(error.to_string()))
}

async fn ensure_empty_body(headers: &HeaderMap, body: Body) -> Result<(), HttpBoundaryError> {
    if content_length(headers)? != 0 {
        return Err(HttpBoundaryError::Invalid(
            "GET request body must be empty".into(),
        ));
    }
    let payload = read_bounded_body(body, 0).await?;
    if payload.is_empty() {
        Ok(())
    } else {
        Err(HttpBoundaryError::Invalid(
            "GET request body must be empty".into(),
        ))
    }
}

async fn read_bounded_body(mut body: Body, expected: u64) -> Result<Vec<u8>, HttpBoundaryError> {
    if expected > MAX_BODY_BYTES {
        return Err(HttpBoundaryError::BodyTooLarge);
    }
    let mut payload = Vec::with_capacity(expected as usize);
    while let Some(chunk) = body.data().await {
        let chunk = chunk.map_err(|error| HttpBoundaryError::Invalid(error.to_string()))?;
        if payload.len().saturating_add(chunk.len()) > expected as usize {
            return Err(HttpBoundaryError::Invalid(
                "body exceeds Content-Length".into(),
            ));
        }
        payload.extend_from_slice(&chunk);
    }
    if payload.len() as u64 != expected {
        return Err(HttpBoundaryError::Invalid(
            "body differs from Content-Length".into(),
        ));
    }
    Ok(payload)
}

fn request_metadata(headers: &HeaderMap) -> Result<RequestMetadata, HttpBoundaryError> {
    if single_header(headers, "x-rock-snapshot-protocol-version")?
        != SNAPD_PROTOCOL_VERSION.to_string()
    {
        return Err(HttpBoundaryError::Invalid(
            "unsupported snapshot protocol version".into(),
        ));
    }
    let request_id = single_header(headers, "x-request-id")?;
    if !valid_request_id(request_id) {
        return Err(HttpBoundaryError::Invalid("request ID is invalid".into()));
    }
    let deadline = single_header(headers, "x-deadline-unix-ms")?
        .parse::<u64>()
        .map_err(|_| HttpBoundaryError::Invalid("deadline is not u64".into()))?;
    if deadline <= now_unix_ms() {
        return Err(HttpBoundaryError::DeadlineExpired);
    }
    Ok(RequestMetadata {
        request_id: request_id.into(),
        deadline_unix_ms: deadline,
    })
}

fn bounded_request_id(headers: &HeaderMap) -> Option<&str> {
    let value = headers.get("x-request-id")?.to_str().ok()?;
    valid_request_id(value).then_some(value)
}

fn valid_request_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= MAX_REQUEST_ID_BYTES
        && value.bytes().all(|byte| byte.is_ascii_graphic())
}

fn http_snapshot_id(path: &str) -> Option<&str> {
    let segments = path.split('/').collect::<Vec<_>>();
    let value = match segments.as_slice() {
        ["", "v1", "snapd", "snapshots", "import-oci"] => return None,
        ["", "v1", "snapd", "snapshots", value, ..] => *value,
        _ => return None,
    };
    validate_path_identity(value).is_ok().then_some(value)
}

fn single_header(
    headers: &HeaderMap,
    name: impl hyper::header::AsHeaderName,
) -> Result<&str, HttpBoundaryError> {
    let mut values = headers.get_all(name).iter();
    let value = values
        .next()
        .ok_or_else(|| HttpBoundaryError::Invalid("required header is absent".into()))?;
    if values.next().is_some() {
        return Err(HttpBoundaryError::Invalid(
            "required header is duplicated".into(),
        ));
    }
    value
        .to_str()
        .map_err(|_| HttpBoundaryError::Invalid("header is not valid text".into()))
}

fn content_length(headers: &HeaderMap) -> Result<u64, HttpBoundaryError> {
    if headers.contains_key("transfer-encoding") {
        return Err(HttpBoundaryError::Invalid(
            "Transfer-Encoding is forbidden".into(),
        ));
    }
    match headers.get(CONTENT_LENGTH) {
        Some(_) => single_header(headers, CONTENT_LENGTH)?
            .parse::<u64>()
            .map_err(|_| HttpBoundaryError::Invalid("Content-Length is invalid".into())),
        None => Ok(0),
    }
}

fn reject_query(query: Option<&str>) -> Result<(), HttpBoundaryError> {
    if query.is_some() {
        Err(HttpBoundaryError::Invalid(
            "mutation route must not contain a query".into(),
        ))
    } else {
        Ok(())
    }
}

fn validate_path_identity(value: &str) -> Result<(), HttpBoundaryError> {
    if value.is_empty() || value.len() > 4096 || value.contains(['/', '%', '?', '#']) {
        Err(HttpBoundaryError::Invalid(
            "path identity is invalid".into(),
        ))
    } else {
        Ok(())
    }
}

fn to_value<T: Serialize>(value: T) -> Result<Value, DispatchError> {
    serde_json::to_value(value).map_err(|error| {
        DispatchError::SnapshotControl(SnapshotControlError::Runtime(format!(
            "could not encode response: {error}"
        )))
    })
}

fn snapshot_control_error(request_id: &str, error: SnapshotControlError) -> Response<Body> {
    let status = match error {
        SnapshotControlError::Invalid(_) => StatusCode::BAD_REQUEST,
        SnapshotControlError::NotFound(_) => StatusCode::NOT_FOUND,
        SnapshotControlError::IdentityConflict(_)
        | SnapshotControlError::RetryableIdentityConflict(_)
        | SnapshotControlError::InvalidState(_)
        | SnapshotControlError::UploadDisabled
        | SnapshotControlError::RetryableInvalidState(_) => StatusCode::CONFLICT,
        SnapshotControlError::Deadline(_) => StatusCode::REQUEST_TIMEOUT,
        SnapshotControlError::Runtime(_) => StatusCode::INTERNAL_SERVER_ERROR,
    };
    let code = match error.code() {
        "UPLOAD_DISABLED" => SnapdErrorCode::UploadDisabled,
        "INVALID_ARGUMENT" => SnapdErrorCode::InvalidArgument,
        "NOT_FOUND" => SnapdErrorCode::NotFound,
        "IDENTITY_CONFLICT" => SnapdErrorCode::IdentityConflict,

        "INVALID_STATE" => SnapdErrorCode::InvalidState,
        "DEADLINE_EXCEEDED" => SnapdErrorCode::DeadlineExceeded,
        _ => SnapdErrorCode::Internal,
    };
    reply_response(
        status,
        SnapdReply {
            request_id: request_id.into(),
            code,
            retryable: error.retryable(),
            error_message: Some(error.to_string()),
            result: None,
        },
    )
}

fn boundary_error(request_id: &str, error: HttpBoundaryError) -> Response<Body> {
    let status = match error {
        HttpBoundaryError::NotFound => StatusCode::NOT_FOUND,
        HttpBoundaryError::MethodNotAllowed => StatusCode::METHOD_NOT_ALLOWED,
        HttpBoundaryError::BodyTooLarge => StatusCode::PAYLOAD_TOO_LARGE,
        HttpBoundaryError::DeadlineExpired => StatusCode::REQUEST_TIMEOUT,
        HttpBoundaryError::Invalid(_) => StatusCode::BAD_REQUEST,
    };
    let code = if status == StatusCode::NOT_FOUND {
        SnapdErrorCode::NotFound
    } else if status == StatusCode::REQUEST_TIMEOUT {
        SnapdErrorCode::DeadlineExceeded
    } else {
        SnapdErrorCode::InvalidArgument
    };
    reply_response(
        status,
        SnapdReply {
            request_id: request_id.into(),
            code,
            retryable: false,
            error_message: Some(error.to_string()),
            result: None,
        },
    )
}

fn reply_response(status: StatusCode, reply: SnapdReply) -> Response<Body> {
    let payload = canonical_json(&reply).unwrap_or_else(|_| {
        b"{\"code\":\"INTERNAL\",\"error_message\":\"reply encoding failed\",\"request_id\":\"unknown\",\"retryable\":false}\n".to_vec()
    });
    Response::builder()
        .status(status)
        .header(CONTENT_TYPE, HeaderValue::from_static("application/json"))
        .header(CONTENT_LENGTH, payload.len())
        .header("cache-control", "no-store")
        .header("connection", "close")
        .body(Body::from(payload))
        .expect("fixed response headers are valid")
}

fn now_unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .try_into()
        .unwrap_or(u64::MAX)
}

#[cfg(test)]
mod tests {
    use super::*;
    use hyper::body::to_bytes;

    #[tokio::test]
    async fn upload_disabled_is_a_stable_non_retryable_control_error() {
        let error: SnapshotControlError = crate::snapshot::services::background_uploader::SnapshotUploadServiceError::UploadDisabled.into();
        let reply = snapshot_control_error("disabled-upload-request", error);
        assert_eq!(reply.status(), StatusCode::CONFLICT);
        let payload: Value =
            serde_json::from_slice(&to_bytes(reply.into_body()).await.unwrap()).unwrap();
        assert_eq!(payload["code"], "UPLOAD_DISABLED");
        assert_eq!(payload["retryable"], false);
    }

    struct PluggerApi;

    #[allow(unused_variables)]
    #[async_trait]
    impl SnapdControlApi for PluggerApi {
        async fn plugger_control(
            &self,
            sandbox_id: &str,
            request: PluggerControlRequest,
            _deadline_unix_ms: u64,
        ) -> Result<PluggerControlResult, SnapshotControlError> {
            assert_eq!(sandbox_id, "source-1");
            assert_eq!(request.action, "configure");
            assert_eq!(request.payload["upstream_url"], "http://relay/inference");
            Ok(PluggerControlResult {
                status: 200,
                body: serde_json::json!({"configured":true,"inflight":0}),
            })
        }
        async fn create_base_image(
            &self,
            request: CreateBaseImageRequest,
        ) -> Result<MutationAcceptedResult, SnapshotControlError> {
            unreachable!("plugger route must not call another API")
        }
        async fn resolve_base_image(
            &self,
            request: ResolveBaseImageRequest,
        ) -> Result<ResolvedBaseImageResult, SnapshotControlError> {
            unreachable!("plugger route must not call another API")
        }
        async fn snapshot_sandbox(
            &self,
            source_id: &str,
            request: SnapshotSandboxRequest,
            deadline_unix_ms: u64,
        ) -> Result<SnapshotLocalReadyResult, SnapshotControlError> {
            unreachable!("plugger route must not call another API")
        }
        async fn fork_sandbox(
            &self,
            source_id: &str,
            request: ForkSandboxRequest,
            deadline_unix_ms: u64,
        ) -> Result<ForkLocalReadyResult, SnapshotControlError> {
            unreachable!("plugger route must not call another API")
        }
        async fn start_sandbox_from_snapshot(
            &self,
            snapshot_id: &str,
            request_id: &str,
            request: StartSandboxFromSnapshotRequest,
            deadline_unix_ms: u64,
        ) -> Result<SandboxStartedResult, SnapshotControlError> {
            unreachable!("plugger route must not call another API")
        }
        async fn snapshot_status(
            &self,
            snapshot_id: &str,
        ) -> Result<SnapshotStatusResult, SnapshotControlError> {
            unreachable!("plugger route must not call another API")
        }
        async fn sandbox_status(
            &self,
            sandbox_id: &str,
        ) -> Result<SandboxStatusResult, SnapshotControlError> {
            unreachable!("plugger route must not call another API")
        }
        async fn stop_sandbox(
            &self,
            sandbox_id: &str,
            request: &StopSandboxRequest,
        ) -> Result<MutationAcceptedResult, SnapshotControlError> {
            unreachable!("plugger route must not call another API")
        }
        async fn delete_sandbox(
            &self,
            sandbox_id: &str,
            request: &DeleteSandboxRequest,
        ) -> Result<MutationAcceptedResult, SnapshotControlError> {
            unreachable!("plugger route must not call another API")
        }
        async fn finalize_published_snapshot(
            &self,
            snapshot_id: &str,
            request: &FinalizePublishedSnapshotRequest,
        ) -> Result<FinalizePublishedSnapshotResult, SnapshotControlError> {
            unreachable!("plugger route must not call another API")
        }
        async fn check_snapshot_usage(
            &self,
            snapshot_id: &str,
            request: &SnapshotFenceRequest,
        ) -> Result<SnapshotUsageCheckResult, SnapshotControlError> {
            unreachable!("plugger route must not call another API")
        }
        async fn abort_delete_fence(
            &self,
            snapshot_id: &str,
            request: &AbortDeleteFenceRequest,
        ) -> Result<MutationAcceptedResult, SnapshotControlError> {
            unreachable!("plugger route must not call another API")
        }
        async fn invalidate_snapshot(
            &self,
            snapshot_id: &str,
            request: &InvalidateSnapshotRequest,
        ) -> Result<InvalidateSnapshotResult, SnapshotControlError> {
            unreachable!("plugger route must not call another API")
        }
    }

    fn headers(builder: hyper::http::request::Builder) -> hyper::http::request::Builder {
        builder
            .header("x-rock-snapshot-protocol-version", "1")
            .header("x-request-id", "request-1")
            .header("x-deadline-unix-ms", now_unix_ms() + 30_000)
    }

    async fn decoded(response: Response<Body>) -> (StatusCode, SnapdReply) {
        let status = response.status();
        let payload = to_bytes(response.into_body()).await.unwrap();
        let reply: SnapdReply = serde_json::from_slice(&payload).unwrap();
        (status, reply)
    }

    #[tokio::test]
    async fn full_oci_import_is_strict_and_never_dispatches_to_base_images() {
        let service = SnapdControlHttpService::new(Arc::new(PluggerApi));
        for (body, expected) in [
            (
                serde_json::json!({"oci_ref":"registry.example/team/image:checkpoint"}),
                StatusCode::CONFLICT,
            ),
            (
                serde_json::json!({"oci_ref":"image"}),
                StatusCode::BAD_REQUEST,
            ),
            (
                serde_json::json!({"oci_ref":"registry.example/team/image:checkpoint", "password":"do-not-accept"}),
                StatusCode::BAD_REQUEST,
            ),
        ] {
            let payload = serde_json::to_vec(&body).unwrap();
            let request = headers(Request::builder())
                .method(Method::POST)
                .uri("/v1/snapd/snapshots/import-oci")
                .header(CONTENT_TYPE, "application/json")
                .header(CONTENT_LENGTH, payload.len())
                .body(Body::from(payload))
                .unwrap();
            let (status, reply) = decoded(service.handle(request).await).await;
            assert_eq!(status, expected);
            if status == StatusCode::CONFLICT {
                assert_eq!(reply.code, SnapdErrorCode::InvalidState);
            }
        }
        let request = headers(Request::builder())
            .method(Method::GET)
            .uri("/v1/snapd/snapshots/import-oci")
            .body(Body::empty())
            .unwrap();
        assert_eq!(
            service.handle(request).await.status(),
            StatusCode::METHOD_NOT_ALLOWED
        );
    }

    #[tokio::test]
    async fn plugger_route_validates_and_dispatches_without_a_rocklet_route() {
        let service = SnapdControlHttpService::new(Arc::new(PluggerApi));
        for (body, expected) in [
            (
                serde_json::json!({"action":"configure","payload":{"upstream_url":"http://relay/inference"}}),
                StatusCode::OK,
            ),
            (
                serde_json::json!({"action":"exec","payload":{}}),
                StatusCode::BAD_REQUEST,
            ),
            (
                serde_json::json!({"action":"configure","payload":[],"unknown":true}),
                StatusCode::BAD_REQUEST,
            ),
        ] {
            let payload = serde_json::to_vec(&body).unwrap();
            let request = headers(Request::builder())
                .method(Method::POST)
                .uri("/v1/snapd/sandboxes/source-1/plugger")
                .header(CONTENT_TYPE, "application/json")
                .header(CONTENT_LENGTH, payload.len())
                .body(Body::from(payload))
                .unwrap();
            let (status, reply) = decoded(service.handle(request).await).await;
            assert_eq!(status, expected);
            if status == StatusCode::OK {
                assert_eq!(reply.result.unwrap()["body"]["configured"], true);
            }
        }
        let request = headers(Request::builder())
            .method(Method::GET)
            .uri("/v1/snapd/sandboxes/source-1/plugger")
            .body(Body::empty())
            .unwrap();
        let (status, _) = decoded(service.handle(request).await).await;
        assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED);
    }
}
