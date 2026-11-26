//! Bounded immutable reads and ordered writes to an OCI Distribution registry.
//!
//! Bearer tokens stay in memory, are scoped to one repository, and are never
//! forwarded to a redirected blob host. Object identities always use digests.

use std::collections::HashMap;
use std::future::Future;
#[cfg(test)]
use std::pin::Pin;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use async_trait::async_trait;
use base64::Engine;
use hyper::body::{Bytes, HttpBody};
use hyper::client::HttpConnector;
use hyper::header::{
    HeaderName, ACCEPT, AUTHORIZATION, CONTENT_ENCODING, CONTENT_LENGTH, CONTENT_RANGE,
    CONTENT_TYPE, LOCATION, RANGE, WWW_AUTHENTICATE,
};
use hyper::{Body, Client, Method, Request, Response, StatusCode};
use hyper_rustls::{HttpsConnector, HttpsConnectorBuilder};
use serde_json::Value;
use sha2::{Digest, Sha256};
use thiserror::Error;
use tokio::sync::Mutex;
use url::Url;

use crate::observability::read_path::{ReadPathEvent, ReadPathStage, ReadPathTimer};
pub use crate::snapshot::transport::interfaces::blob::BlobRef as RegistryLayerRef;
use crate::snapshot::transport::{BlobRangeError, BlobRangeErrorKind};
use crate::snapshot::transport::{
    ImmutableObjectIdentity, ImmutableStreamWriter, IoCancellation, NamespaceMutationFence,
    ObjectStoreError, PublishFailureKind, ReplayableByteSource, UploadTransactionLease,
    VerifiedWriteReceipt,
};
use crate::snapshot::transport::{PhysicalRangeReader, PhysicalRangeRequest};
use crate::snapshot::transport::{RangeReadError, RangeReadFailureClass};

pub(crate) const MAX_REGISTRY_READ_BYTES: u64 = 16 * 1024 * 1024;
pub(crate) const MAX_REGISTRY_METADATA_BYTES: u64 = 128 * 1024 * 1024;
const MAX_PHYSICAL_RANGE_BYTES: u64 = 66 * 1024 * 1024;
const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);
const MAX_BLOB_REDIRECTS: usize = 256;
const MAX_BLOB_REDIRECT_URL_BYTES: usize = 16 * 1024;
const BLOB_REDIRECT_TTL: Duration = Duration::from_secs(60);
// Leave room for the ordinary registry fallback inside RemoteLoad's deadline.
const CACHED_BLOB_REDIRECT_TIMEOUT: Duration = Duration::from_secs(5);
const BLOB_REDIRECT_EXPIRY_GRACE_SECONDS: u64 = 5;
const MAX_UPLOAD_CHUNK_BYTES: usize = 16 * 1024 * 1024;
const MANIFEST_ACCEPT: &str = "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json";

static TRANSFER_REQUESTS: AtomicU64 = AtomicU64::new(0);
static TRANSFER_BYTES: AtomicU64 = AtomicU64::new(0);
static RANGE_REQUESTS: AtomicU64 = AtomicU64::new(0);
static RANGE_BYTES: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Copy)]
enum RegistryReadPurpose {
    Range,
    Metadata,
}

impl RegistryReadPurpose {
    fn headers_stage(self) -> ReadPathStage {
        match self {
            Self::Range => ReadPathStage::RegistryRangeHeaders,
            Self::Metadata => ReadPathStage::RegistryMetadataHeaders,
        }
    }

    fn body_stage(self) -> ReadPathStage {
        match self {
            Self::Range => ReadPathStage::RegistryRangeBody,
            Self::Metadata => ReadPathStage::RegistryMetadataBody,
        }
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct RegistryTransferStats {
    pub requests: u64,
    pub bytes: u64,
    pub range_requests: u64,
    pub range_bytes: u64,
}

pub fn transfer_stats() -> RegistryTransferStats {
    RegistryTransferStats {
        requests: TRANSFER_REQUESTS.load(Ordering::Relaxed),
        bytes: TRANSFER_BYTES.load(Ordering::Relaxed),
        range_requests: RANGE_REQUESTS.load(Ordering::Relaxed),
        range_bytes: RANGE_BYTES.load(Ordering::Relaxed),
    }
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum RegistryErrorKind {
    Invalid,
    Authorization,
    Integrity,
    Transient,
    Permanent,
}

#[derive(Debug, Clone, Error)]
#[error("registry {kind:?}: {message}")]
pub struct RegistryError {
    pub kind: RegistryErrorKind,
    message: String,
    status: Option<StatusCode>,
}

impl RegistryError {
    fn new(kind: RegistryErrorKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
            status: None,
        }
    }
    pub fn retryable(&self) -> bool {
        self.kind == RegistryErrorKind::Transient
    }
    pub(crate) fn is_not_found(&self) -> bool {
        self.status == Some(StatusCode::NOT_FOUND)
    }
    pub fn failure_kind(&self) -> BlobRangeErrorKind {
        match self.kind {
            RegistryErrorKind::Invalid => BlobRangeErrorKind::Invalid,
            RegistryErrorKind::Authorization => BlobRangeErrorKind::Authorization,
            RegistryErrorKind::Integrity => BlobRangeErrorKind::Integrity,
            RegistryErrorKind::Transient => BlobRangeErrorKind::Transient,
            RegistryErrorKind::Permanent => BlobRangeErrorKind::Permanent,
        }
    }
}

impl From<RegistryError> for BlobRangeError {
    fn from(error: RegistryError) -> Self {
        Self::new(error.failure_kind(), error.to_string())
    }
}

impl From<RegistryError> for RangeReadError {
    fn from(error: RegistryError) -> Self {
        let class = match error.kind {
            RegistryErrorKind::Invalid => return Self::InvalidRange,
            RegistryErrorKind::Authorization => RangeReadFailureClass::Authorization,
            RegistryErrorKind::Integrity => RangeReadFailureClass::Integrity,
            RegistryErrorKind::Transient => RangeReadFailureClass::Transient,
            RegistryErrorKind::Permanent => RangeReadFailureClass::Permanent,
        };
        Self::failure(class, error.to_string())
    }
}

impl From<RegistryError> for ObjectStoreError {
    fn from(error: RegistryError) -> Self {
        let kind = match error.kind {
            RegistryErrorKind::Transient => PublishFailureKind::Retryable,
            RegistryErrorKind::Integrity => PublishFailureKind::IdentityConflict,
            RegistryErrorKind::Invalid
            | RegistryErrorKind::Authorization
            | RegistryErrorKind::Permanent => PublishFailureKind::Fatal,
        };
        Self::new(kind, error.to_string())
    }
}

#[derive(Debug, Clone)]
pub(crate) struct RegistryManifest {
    pub digest: String,
    pub payload: Vec<u8>,
    pub value: Value,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub(crate) struct RegistryReference {
    pub registry: String,
    pub repository: String,
    pub reference: String,
}

impl RegistryReference {
    pub(crate) fn parse(reference: &str) -> Result<Self, RegistryError> {
        let reference = reference.strip_prefix("docker://").unwrap_or(reference);
        let (registry, image) = reference
            .split_once('/')
            .ok_or_else(|| invalid("an explicit registry and repository are required"))?;
        validate_registry(registry)?;
        let (repository, reference) = if let Some((repository, digest)) = image.rsplit_once('@') {
            validate_digest(digest)?;
            // An optional tag before @ does not change the pinned digest.
            let repository = repository
                .rsplit_once(':')
                .map_or(repository, |(name, _)| name);
            (repository, digest)
        } else {
            image.rsplit_once(':').unwrap_or((image, "latest"))
        };
        validate_repository(repository)?;
        validate_selector(reference)?;
        Ok(Self {
            registry: registry.to_owned(),
            repository: repository.to_owned(),
            reference: reference.to_owned(),
        })
    }
}

struct CachedToken {
    value: String,
    expires: Instant,
}

#[derive(Clone, Eq, Hash, PartialEq)]
struct BlobRedirectKey {
    repository: String,
    digest: String,
    size: u64,
}

#[derive(Clone)]
struct CachedBlobRedirect {
    url: Url,
    expires: Instant,
}

#[derive(Clone)]
struct RegistryResponseUrl(Url);

pub struct RegistryClient {
    registry: String,
    origin: Url,
    http: Client<HttpsConnector<HttpConnector>>,
    tokens: Mutex<HashMap<String, CachedToken>>,
    blob_redirects: Mutex<HashMap<BlobRedirectKey, CachedBlobRedirect>>,
    blob_redirect_cache_enabled: bool,
    allow_http: bool,
    credentials: Option<(String, String)>,
    trusted_auth_realm: Option<Url>,
}

#[async_trait]
impl PhysicalRangeReader for RegistryClient {
    fn source_id(&self) -> &str {
        self.registry()
    }

    async fn read_physical_range(
        &self,
        request: PhysicalRangeRequest<'_>,
    ) -> Result<Vec<u8>, RangeReadError> {
        request.validate()?;
        let repository = request.namespace.ok_or(RangeReadError::InvalidRange)?;
        let size = request.object_size.ok_or(RangeReadError::InvalidRange)?;
        if request.object_key != request.object_sha256 || request.object_version.is_some() {
            return Err(RangeReadError::failure(
                RangeReadFailureClass::Integrity,
                "registry key must equal its pinned digest and cannot select a version",
            ));
        }
        if request.length > MAX_PHYSICAL_RANGE_BYTES {
            return Err(RangeReadError::InvalidRange);
        }
        // A bounded format plan can span several HTTP windows. Assemble the
        // exact physical span here so the outer cache records its real bytes,
        // while each provider request retains the independent 16 MiB bound.
        let mut bytes = Vec::with_capacity(request.length as usize);
        while (bytes.len() as u64) < request.length {
            let consumed = bytes.len() as u64;
            let length = (request.length - consumed).min(MAX_REGISTRY_READ_BYTES);
            bytes.extend_from_slice(
                &self
                    .read_blob_range(
                        repository,
                        request.object_key,
                        size,
                        request.offset + consumed,
                        length,
                    )
                    .await
                    .map_err(RangeReadError::from)?,
            );
        }
        Ok(bytes)
    }
}

impl std::fmt::Debug for RegistryClient {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("RegistryClient")
            .field("registry", &self.registry)
            .finish_non_exhaustive()
    }
}

impl RegistryClient {
    pub fn new(registry: &str) -> Result<Arc<Self>, RegistryError> {
        Self::new_configured(registry, false, None, None)
    }

    /// Configure one explicit registry authority. HTTP is an operator-owned
    /// opt-in for private registries; default callers retain HTTPS-only reads.
    /// Credentials are sent only to this origin, including its token endpoint.
    pub fn new_configured(
        registry: &str,
        allow_http: bool,
        username: Option<String>,
        password: Option<String>,
    ) -> Result<Arc<Self>, RegistryError> {
        Self::new_configured_with_auth_realm(registry, allow_http, username, password, None)
    }

    /// Explicitly authorize credentials at one complete HTTPS token endpoint.
    /// This capability does not change blob/CDN redirect authorization.
    pub fn new_configured_with_auth_realm(
        registry: &str,
        allow_http: bool,
        username: Option<String>,
        password: Option<String>,
        auth_realm: Option<&str>,
    ) -> Result<Arc<Self>, RegistryError> {
        validate_registry(registry)?;
        let trusted_auth_realm = auth_realm.map(parse_auth_realm).transpose()?;
        let credentials = match (username, password) {
            (None, None) => None,
            (Some(username), Some(password))
                if !username.is_empty()
                    && username.len() <= 4096
                    && !username.contains(':')
                    && password.len() <= 64 * 1024 =>
            {
                Some((username, password))
            }
            _ => {
                return Err(invalid(
                    "registry credentials require a bounded username/password pair",
                ));
            }
        };
        let scheme = if allow_http { "http" } else { "https" };
        let origin = Url::parse(&format!("{scheme}://{registry}/"))
            .map_err(|_| invalid("invalid registry origin"))?;
        let mut client = Self::with_origin(registry.to_owned(), origin, allow_http);
        let unique = Arc::get_mut(&mut client).expect("new registry client is uniquely owned");
        unique.credentials = credentials;
        unique.trusted_auth_realm = trusted_auth_realm;
        Ok(client)
    }

    fn with_origin(registry: String, origin: Url, allow_http: bool) -> Arc<Self> {
        let connector = HttpsConnectorBuilder::new()
            .with_native_roots()
            .https_or_http()
            .enable_http1()
            .build();
        Arc::new(Self {
            registry,
            origin,
            http: Client::builder().pool_max_idle_per_host(8).build(connector),
            tokens: Mutex::new(HashMap::new()),
            blob_redirects: Mutex::new(HashMap::new()),
            // Evaluate the operator-owned switch once per client, never per
            // page read. Signed URLs stay process-local and are not logged.
            blob_redirect_cache_enabled: std::env::var_os("ROCK_REGISTRY_BLOB_REDIRECT_CACHE")
                .as_deref()
                == Some(std::ffi::OsStr::new("1")),
            allow_http,
            credentials: None,
            trusted_auth_realm: None,
        })
    }

    pub fn registry(&self) -> &str {
        &self.registry
    }

    #[cfg(test)]
    pub(crate) fn test_http_client(origin: &str) -> Arc<Self> {
        let origin = Url::parse(origin).expect("test origin");
        let registry = origin[url::Position::BeforeHost..url::Position::AfterPort].to_owned();
        Self::with_origin(registry, origin, true)
    }

    pub(crate) async fn manifest(
        &self,
        repository: &str,
        reference: &str,
    ) -> Result<RegistryManifest, RegistryError> {
        validate_repository(repository)?;
        validate_selector(reference)?;
        let url = self.endpoint(&format!("v2/{repository}/manifests/{reference}"))?;
        let response = self
            .authorized_get(url, repository, None, RegistryReadPurpose::Metadata)
            .await?;
        if response.status() != StatusCode::OK {
            return Err(status_error(response.status()));
        }
        let advertised = response
            .headers()
            .get("docker-content-digest")
            .and_then(|value| value.to_str().ok())
            .map(str::to_owned);
        let payload = profiled_body(
            response,
            MAX_REGISTRY_READ_BYTES,
            ReadPathStage::RegistryMetadataBody,
        )
        .await?;
        let digest = digest(&payload);
        if (reference.starts_with("sha256:") && reference != digest)
            || advertised.is_some_and(|value| value != digest)
        {
            return Err(integrity(
                "manifest digest differs from its immutable identity",
            ));
        }
        let value: Value = serde_json::from_slice(&payload)
            .map_err(|_| invalid("registry manifest is not JSON"))?;
        if !value.is_object() || value.get("schemaVersion").and_then(Value::as_u64) != Some(2) {
            return Err(invalid("unsupported registry manifest schema"));
        }
        Ok(RegistryManifest {
            digest,
            payload,
            value,
        })
    }

    pub(crate) async fn resolve_manifest(
        &self,
        repository: &str,
        reference: &str,
        platform: &str,
    ) -> Result<RegistryManifest, RegistryError> {
        let manifest = self.manifest(repository, reference).await?;
        self.select_manifest(repository, manifest, platform).await
    }

    pub(crate) async fn select_manifest(
        &self,
        repository: &str,
        manifest: RegistryManifest,
        platform: &str,
    ) -> Result<RegistryManifest, RegistryError> {
        let Some(manifests) = manifest.value.get("manifests") else {
            return Ok(manifest);
        };
        let manifests = manifests
            .as_array()
            .filter(|items| !items.is_empty() && items.len() <= 128)
            .ok_or_else(|| invalid("manifest index count exceeds its bound"))?;
        let parts: Vec<_> = platform.split('/').collect();
        if !(2..=3).contains(&parts.len()) {
            return Err(invalid("invalid registry platform"));
        }
        let matches = manifests
            .iter()
            .filter(|item| {
                let Some(candidate) = item.get("platform") else {
                    return false;
                };
                candidate.get("os").and_then(Value::as_str) == Some(parts[0])
                    && candidate.get("architecture").and_then(Value::as_str) == Some(parts[1])
                    && candidate.get("variant").and_then(Value::as_str) == parts.get(2).copied()
            })
            .collect::<Vec<_>>();
        if matches.len() != 1 {
            return Err(invalid(
                "registry index must contain one exact platform manifest",
            ));
        }
        let selected = layer_ref(matches[0])?;
        if selected.size > MAX_REGISTRY_READ_BYTES {
            return Err(invalid("platform manifest exceeds metadata bound"));
        }
        let resolved = self.manifest(repository, &selected.digest).await?;
        if resolved.payload.len() as u64 != selected.size {
            return Err(integrity("platform manifest length differs from index"));
        }
        if resolved.value.get("manifests").is_some() {
            return Err(invalid("nested registry indexes are unsupported"));
        }
        Ok(resolved)
    }

    pub async fn read_blob_range(
        &self,
        repository: &str,
        digest: &str,
        size: u64,
        offset: u64,
        length: u64,
    ) -> Result<Vec<u8>, RegistryError> {
        self.read_blob_range_profiled(
            repository,
            digest,
            size,
            offset,
            length,
            RegistryReadPurpose::Range,
        )
        .await
    }

    #[allow(clippy::too_many_arguments)]
    async fn read_blob_range_profiled(
        &self,
        repository: &str,
        digest: &str,
        size: u64,
        offset: u64,
        length: u64,
        purpose: RegistryReadPurpose,
    ) -> Result<Vec<u8>, RegistryError> {
        validate_repository(repository)?;
        validate_digest(digest)?;
        let end = offset
            .checked_add(length)
            .filter(|end| *end <= size)
            .ok_or_else(|| invalid("registry blob range exceeds object"))?;
        if length == 0 || length > MAX_REGISTRY_READ_BYTES {
            return Err(invalid("registry range length must be 1..=16MiB"));
        }
        let read_timer = matches!(purpose, RegistryReadPurpose::Range)
            .then(|| ReadPathTimer::start(ReadPathStage::RegistryRangeRead));
        let key = (self.blob_redirect_cache_enabled
            && matches!(purpose, RegistryReadPurpose::Range))
        .then(|| BlobRedirectKey {
            repository: repository.to_owned(),
            digest: digest.to_owned(),
            size,
        });
        let cached = match &key {
            Some(key) => self.cached_blob_redirect(key).await,
            None => None,
        };
        let mut used_cached = false;
        let response = if let Some(cached) = &cached {
            ReadPathEvent::RegistryBlobRedirectCacheHit.record();
            let attempt = tokio::time::timeout(
                CACHED_BLOB_REDIRECT_TIMEOUT,
                self.get_following_redirects(
                    cached.url.clone(),
                    None,
                    Some((offset, end - 1)),
                    "application/octet-stream",
                    purpose,
                    false,
                ),
            )
            .await
            .unwrap_or_else(|_| Err(transient("cached blob route timed out")));
            match attempt {
                Ok(response) if !cached_blob_route_retryable(response.status()) => {
                    used_cached = true;
                    Some(response)
                }
                Ok(response) => {
                    drop(response);
                    self.invalidate_blob_redirect(key.as_ref().unwrap(), &cached.url)
                        .await;
                    ReadPathEvent::RegistryBlobRedirectCacheFallback.record();
                    None
                }
                Err(error) if error.kind == RegistryErrorKind::Transient => {
                    self.invalidate_blob_redirect(key.as_ref().unwrap(), &cached.url)
                        .await;
                    ReadPathEvent::RegistryBlobRedirectCacheFallback.record();
                    None
                }
                Err(error) => return Err(error),
            }
        } else {
            if key.is_some() {
                ReadPathEvent::RegistryBlobRedirectCacheMiss.record();
            }
            None
        };
        // A rejected/expired cached route gets one ordinary authorized GET.
        // No retry is performed after response validation or body consumption.
        let response = match response {
            Some(response) => response,
            None => {
                let url = self.endpoint(&format!("v2/{repository}/blobs/{digest}"))?;
                self.authorized_get(url, repository, Some((offset, end - 1)), purpose)
                    .await?
            }
        };
        let effective_url = response
            .extensions()
            .get::<RegistryResponseUrl>()
            .map(|value| value.0.clone());
        let result = async {
            if response.status() != StatusCode::PARTIAL_CONTENT {
                return Err(if response.status().is_success() {
                    integrity("registry ignored bounded Range request")
                } else {
                    status_error(response.status())
                });
            }
            let expected = format!("bytes {offset}-{}/{size}", end - 1);
            if response
                .headers()
                .get("content-range")
                .and_then(|value| value.to_str().ok())
                != Some(expected.as_str())
            {
                return Err(integrity(
                    "registry Content-Range differs from requested immutable span",
                ));
            }
            if response
                .headers()
                .get("docker-content-digest")
                .is_some_and(|advertised| advertised.to_str().ok() != Some(digest))
            {
                return Err(integrity("blob response digest differs from source"));
            }
            let payload = profiled_body(response, length, purpose.body_stage()).await?;
            if payload.len() as u64 != length {
                return Err(integrity("registry returned a short blob range"));
            }
            Ok(payload)
        }
        .await;
        let payload = match result {
            Ok(payload) => payload,
            Err(error) => {
                if used_cached {
                    self.invalidate_blob_redirect(
                        key.as_ref().unwrap(),
                        &cached.as_ref().unwrap().url,
                    )
                    .await;
                }
                return Err(error);
            }
        };
        if !used_cached {
            if let (Some(key), Some(url)) = (key, effective_url) {
                self.cache_blob_redirect(key, url).await;
            }
        }
        if let Some(timer) = read_timer {
            timer.finish(payload.len() as u64, true);
        }
        Ok(payload)
    }

    pub(crate) async fn read_blob_metadata(
        &self,
        repository: &str,
        source: &RegistryLayerRef,
    ) -> Result<Vec<u8>, RegistryError> {
        self.read_blob_bounded(repository, source, MAX_REGISTRY_READ_BYTES)
            .await
    }

    /// Fetch a bounded metadata blob in physical windows, authenticating the
    /// complete digest. Large layer DATA continues to use range reads only.
    pub(crate) async fn read_blob_bounded(
        &self,
        repository: &str,
        source: &RegistryLayerRef,
        max_bytes: u64,
    ) -> Result<Vec<u8>, RegistryError> {
        validate_repository(repository)?;
        validate_digest(&source.digest)?;
        if source.size == 0 || source.size > max_bytes || max_bytes > MAX_REGISTRY_METADATA_BYTES {
            return Err(invalid("registry metadata blob exceeds its byte bound"));
        }
        let mut bytes = Vec::with_capacity(source.size as usize);
        while (bytes.len() as u64) < source.size {
            let offset = bytes.len() as u64;
            let length = (source.size - offset).min(MAX_REGISTRY_READ_BYTES);
            bytes.extend_from_slice(
                &self
                    .read_blob_range_profiled(
                        repository,
                        &source.digest,
                        source.size,
                        offset,
                        length,
                        RegistryReadPurpose::Metadata,
                    )
                    .await?,
            );
        }
        if digest(&bytes) != source.digest {
            return Err(integrity("metadata blob SHA256 differs from descriptor"));
        }
        Ok(bytes)
    }

    /// Check a descriptor in this repository without admitting missing content
    /// from another provider. Only a genuine 404 means that the blob is absent.
    pub(crate) async fn head_blob(
        &self,
        repository: &str,
        source: &RegistryLayerRef,
    ) -> Result<bool, RegistryError> {
        validate_repository(repository)?;
        validate_digest(&source.digest)?;
        let url = self.endpoint(&format!("v2/{repository}/blobs/{}", source.digest))?;
        let (_sender, receiver) = tokio::sync::watch::channel(false);
        let cancellation = IoCancellation::new(receiver);
        let response = self
            .authorized_request(
                Method::HEAD,
                url,
                repository,
                "pull",
                &[],
                Bytes::new(),
                Instant::now() + REQUEST_TIMEOUT,
                &cancellation,
            )
            .await?;
        match response.status() {
            StatusCode::OK => {
                validate_response_identity(&response, &source.digest, source.size)?;
                Ok(true)
            }
            StatusCode::NOT_FOUND => Ok(false),
            status => Err(status_error(status)),
        }
    }

    fn endpoint(&self, path: &str) -> Result<Url, RegistryError> {
        self.origin
            .join(path)
            .map_err(|_| invalid("invalid registry endpoint"))
    }

    async fn authorized_get(
        &self,
        url: Url,
        repository: &str,
        range: Option<(u64, u64)>,
        purpose: RegistryReadPurpose,
    ) -> Result<Response<Body>, RegistryError> {
        let accept = if range.is_some() {
            "application/octet-stream"
        } else {
            MANIFEST_ACCEPT
        };
        let key = token_key(repository, "pull");
        let cache_timer = ReadPathTimer::start(ReadPathStage::RegistryAuthCacheLookup);
        let token = self
            .tokens
            .lock()
            .await
            .get(&key)
            .filter(|token| token.expires > Instant::now())
            .map(|token| token.value.clone());
        cache_timer.finish(0, true);
        if token.is_some() {
            ReadPathEvent::RegistryAuthCacheHit.record();
        } else {
            ReadPathEvent::RegistryAuthCacheMiss.record();
        }
        let response = self
            .get_following_redirects(url.clone(), token.as_deref(), range, accept, purpose, true)
            .await?;
        if response.status() != StatusCode::UNAUTHORIZED {
            return resource_response(response, &Method::GET);
        }
        ReadPathEvent::RegistryUnauthorized.record();
        let challenge = response
            .headers()
            .get(WWW_AUTHENTICATE)
            .and_then(|value| value.to_str().ok())
            .ok_or_else(|| authorization("registry did not provide a Bearer challenge"))?
            .to_owned();
        drop(response);
        self.tokens.lock().await.remove(&key);
        let token = self.refresh_token(repository, &challenge, "pull").await?;
        let response = self
            .get_following_redirects(url, Some(&token), range, accept, purpose, true)
            .await?;
        resource_response(response, &Method::GET)
    }

    async fn refresh_token(
        &self,
        repository: &str,
        challenge: &str,
        actions: &str,
    ) -> Result<String, RegistryError> {
        let auth_timer = ReadPathTimer::start(ReadPathStage::RegistryAuthExchange);
        let fields = bearer_parameters(challenge)?;
        let mut realm = Url::parse(
            fields
                .get("realm")
                .ok_or_else(|| authorization("Bearer challenge has no realm"))?,
        )
        .map_err(|_| authorization("invalid Bearer realm"))?;
        self.validate_url(&realm)?;
        if realm
            .query_pairs()
            .any(|(key, _)| matches!(key.as_ref(), "scope" | "service"))
        {
            return Err(authorization(
                "Bearer realm already contains scope or service",
            ));
        }
        // Bind trust to the challenge's complete endpoint before appending
        // service/scope. An explicitly configured realm never grants trust to
        // another path at the same host.
        let send_basic = self.trusted_auth_realm.as_ref().map_or_else(
            || realm.origin() == self.origin.origin(),
            |trusted| trusted == &realm,
        );
        {
            let mut query = realm.query_pairs_mut();
            if let Some(service) = fields.get("service") {
                query.append_pair("service", service);
            }
            query.append_pair("scope", &format!("repository:{repository}:{actions}"));
        }
        let mut request = Request::get(realm.as_str())
            .header(ACCEPT, "application/json")
            .header("accept-encoding", "identity");
        if send_basic {
            if let Some(value) = self.basic_authorization() {
                request = request.header(AUTHORIZATION, value);
            }
        }
        let request = request
            .body(Body::empty())
            .map_err(|_| invalid("registry token request headers are invalid"))?;
        // Token redirects never inherit credential trust, including redirects
        // to another path on the configured token host.
        let response = tokio::time::timeout(REQUEST_TIMEOUT, self.http.request(request))
            .await
            .map_err(|_| transient("registry token endpoint timed out"))?
            .map_err(|_| transient("registry token endpoint transport failed"))?;
        if response.status().is_redirection() {
            return Err(authorization("token endpoint redirect is not permitted"));
        }
        if response.status() != StatusCode::OK {
            return Err(status_error_in_phase(response.status(), "token endpoint"));
        }
        let bytes = bounded_body(response, 1024 * 1024).await?;
        let value: Value = serde_json::from_slice(&bytes)
            .map_err(|_| authorization("Bearer response is not JSON"))?;
        let token = value
            .get("token")
            .or_else(|| value.get("access_token"))
            .and_then(Value::as_str)
            .filter(|value| {
                !value.is_empty()
                    && value.len() <= 64 * 1024
                    && value.bytes().all(|byte| byte.is_ascii_graphic())
            })
            .ok_or_else(|| authorization("Bearer response contains no valid token"))?
            .to_owned();
        let seconds = value
            .get("expires_in")
            .and_then(Value::as_u64)
            .unwrap_or(300)
            .clamp(1, 86_400);
        let ttl = Duration::from_secs(seconds.saturating_sub(10).max(1));
        let mut tokens = self.tokens.lock().await;
        tokens.retain(|_, token| token.expires > Instant::now());
        if tokens.len() >= 256 {
            tokens.clear();
        }
        tokens.insert(
            token_key(repository, actions),
            CachedToken {
                value: token.clone(),
                expires: Instant::now() + ttl,
            },
        );
        auth_timer.finish(bytes.len() as u64, true);
        Ok(token)
    }

    async fn get_following_redirects(
        &self,
        mut url: Url,
        token: Option<&str>,
        range: Option<(u64, u64)>,
        accept: &str,
        purpose: RegistryReadPurpose,
        authorized_origin: bool,
    ) -> Result<Response<Body>, RegistryError> {
        for redirects in 0..=4 {
            self.validate_url(&url)?;
            let mut request = Request::get(url.as_str())
                .header(ACCEPT, accept)
                .header("accept-encoding", "identity");
            // The token authorizes the registry, never a CDN or token realm.
            if authorized_origin && url.origin() == self.origin.origin() {
                if let Some(token) = token {
                    request = request.header(AUTHORIZATION, format!("Bearer {token}"));
                } else if let Some(value) = self.basic_authorization() {
                    request = request.header(AUTHORIZATION, value);
                }
            }
            if let Some((start, end)) = range {
                request = request.header(RANGE, format!("bytes={start}-{end}"));
            }
            let request = request
                .body(Body::empty())
                .map_err(|_| invalid("registry request headers are invalid"))?;
            let headers_timer = ReadPathTimer::start(purpose.headers_stage());
            let mut response = tokio::time::timeout(REQUEST_TIMEOUT, self.http.request(request))
                .await
                .map_err(|_| transient("registry request timed out"))?
                .map_err(|_| transient("registry transport failed"))?;
            // A 401 or redirect is still a completed transport-to-headers
            // operation; finite event counters retain its protocol outcome.
            headers_timer.finish(0, true);
            response
                .extensions_mut()
                .insert(RegistryResponseUrl(url.clone()));
            if !matches!(
                response.status(),
                StatusCode::MOVED_PERMANENTLY
                    | StatusCode::FOUND
                    | StatusCode::SEE_OTHER
                    | StatusCode::TEMPORARY_REDIRECT
                    | StatusCode::PERMANENT_REDIRECT
            ) {
                return Ok(response);
            }
            ReadPathEvent::RegistryRedirect.record();
            if redirects == 4 {
                return Err(invalid("registry redirect count exceeds bound"));
            }
            let location = response
                .headers()
                .get(LOCATION)
                .and_then(|value| value.to_str().ok())
                .ok_or_else(|| invalid("registry redirect has no Location"))?;
            url = url
                .join(location)
                .map_err(|_| invalid("registry redirect URL is invalid"))?;
        }
        unreachable!()
    }

    async fn cached_blob_redirect(&self, key: &BlobRedirectKey) -> Option<CachedBlobRedirect> {
        let mut redirects = self.blob_redirects.lock().await;
        let now = Instant::now();
        redirects.retain(|_, entry| entry.expires > now);
        redirects.get(key).cloned()
    }

    async fn invalidate_blob_redirect(&self, key: &BlobRedirectKey, url: &Url) {
        let mut redirects = self.blob_redirects.lock().await;
        // A stale concurrent failure must not evict a freshly learned URL.
        if redirects.get(key).is_some_and(|entry| &entry.url == url) {
            redirects.remove(key);
        }
    }

    async fn cache_blob_redirect(&self, key: BlobRedirectKey, url: Url) {
        if url.origin() == self.origin.origin() || self.validate_url(&url).is_err() {
            return;
        }
        let Some(now) = SystemTime::now().duration_since(UNIX_EPOCH).ok() else {
            return;
        };
        let Some(ttl) = blob_redirect_ttl(&url, now.as_secs()) else {
            return;
        };
        let now = Instant::now();
        let mut redirects = self.blob_redirects.lock().await;
        redirects.retain(|_, entry| entry.expires > now);
        if redirects.len() >= MAX_BLOB_REDIRECTS && !redirects.contains_key(&key) {
            if let Some(oldest) = redirects
                .iter()
                .min_by_key(|(_, entry)| entry.expires)
                .map(|(key, _)| key.clone())
            {
                redirects.remove(&oldest);
            }
        }
        redirects.insert(
            key,
            CachedBlobRedirect {
                url,
                expires: now + ttl,
            },
        );
    }

    fn basic_authorization(&self) -> Option<String> {
        self.credentials.as_ref().map(|(username, password)| {
            format!(
                "Basic {}",
                base64::engine::general_purpose::STANDARD.encode(format!("{username}:{password}"))
            )
        })
    }

    #[allow(clippy::too_many_arguments)]
    async fn authorized_request(
        &self,
        method: Method,
        url: Url,
        repository: &str,
        actions: &str,
        headers: &[(HeaderName, String)],
        body: Bytes,
        deadline: Instant,
        cancellation: &IoCancellation,
    ) -> Result<Response<Body>, RegistryError> {
        let key = token_key(repository, actions);
        let token = self
            .tokens
            .lock()
            .await
            .get(&key)
            .filter(|token| token.expires > Instant::now())
            .map(|token| token.value.clone());
        let response = self
            .request_following_redirects(
                method.clone(),
                url.clone(),
                token.as_deref(),
                headers,
                body.clone(),
                deadline,
                cancellation,
            )
            .await?;
        if response.status() != StatusCode::UNAUTHORIZED || url.origin() != self.origin.origin() {
            return resource_response(response, &method);
        }
        let challenge = response
            .headers()
            .get(WWW_AUTHENTICATE)
            .and_then(|value| value.to_str().ok())
            .ok_or_else(|| authorization("registry did not provide a Bearer challenge"))?
            .to_owned();
        drop(response);
        self.tokens.lock().await.remove(&key);
        let token = active_registry_operation(
            deadline,
            cancellation,
            self.refresh_token(repository, &challenge, actions),
        )
        .await?;
        let response = self
            .request_following_redirects(
                method.clone(),
                url,
                Some(&token),
                headers,
                body,
                deadline,
                cancellation,
            )
            .await?;
        resource_response(response, &method)
    }

    #[allow(clippy::too_many_arguments)]
    async fn request_following_redirects(
        &self,
        method: Method,
        mut url: Url,
        token: Option<&str>,
        headers: &[(HeaderName, String)],
        body: Bytes,
        deadline: Instant,
        cancellation: &IoCancellation,
    ) -> Result<Response<Body>, RegistryError> {
        for redirects in 0..=4 {
            self.validate_url(&url)?;
            let mut request = Request::builder()
                .method(method.clone())
                .uri(url.as_str())
                .header("accept-encoding", "identity");
            for (name, value) in headers {
                request = request.header(name, value);
            }
            // Upload Locations may use a different service or signed URL.
            // Neither bearer nor basic credentials leave the bound origin.
            if url.origin() == self.origin.origin() {
                if let Some(token) = token {
                    request = request.header(AUTHORIZATION, format!("Bearer {token}"));
                } else if let Some(value) = self.basic_authorization() {
                    request = request.header(AUTHORIZATION, value);
                }
            }
            let request = request
                .body(Body::from(body.clone()))
                .map_err(|_| invalid("registry write request headers are invalid"))?;
            let mut response = active_registry_operation(deadline, cancellation, async {
                self.http
                    .request(request)
                    .await
                    .map_err(|_| transient("registry write transport failed"))
            })
            .await?;
            response
                .extensions_mut()
                .insert(RegistryResponseUrl(url.clone()));
            if !response.status().is_redirection() {
                return Ok(response);
            }
            if redirects == 4 {
                return Err(invalid("registry redirect count exceeds bound"));
            }
            if !matches!(
                response.status(),
                StatusCode::TEMPORARY_REDIRECT | StatusCode::PERMANENT_REDIRECT
            ) && method != Method::HEAD
            {
                return Err(invalid("registry write redirect must preserve its method"));
            }
            url = self.response_location(&url, &response)?;
        }
        unreachable!()
    }

    fn response_location(
        &self,
        previous: &Url,
        response: &Response<Body>,
    ) -> Result<Url, RegistryError> {
        let location = response
            .headers()
            .get(LOCATION)
            .and_then(|value| value.to_str().ok())
            .ok_or_else(|| invalid("registry upload response has no Location"))?;
        let base = response
            .extensions()
            .get::<RegistryResponseUrl>()
            .map_or(previous, |effective| &effective.0);
        let url = base
            .join(location)
            .map_err(|_| invalid("registry upload Location is invalid"))?;
        self.validate_url(&url)?;
        Ok(url)
    }

    fn validate_url(&self, url: &Url) -> Result<(), RegistryError> {
        if !(url.scheme() == "https" || (self.allow_http && url.scheme() == "http"))
            || url.host_str().is_none()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.fragment().is_some()
        {
            return Err(invalid(
                "registry URLs require HTTPS without userinfo or fragments",
            ));
        }
        Ok(())
    }
}

fn cached_blob_route_retryable(status: StatusCode) -> bool {
    matches!(
        status,
        StatusCode::BAD_REQUEST
            | StatusCode::UNAUTHORIZED
            | StatusCode::FORBIDDEN
            | StatusCode::NOT_FOUND
            | StatusCode::GONE
            | StatusCode::RANGE_NOT_SATISFIABLE
            | StatusCode::TOO_MANY_REQUESTS
    ) || status.is_server_error()
}

/// A successful signed blob URL is a short-lived read capability. The local
/// deadline never slides on a hit and does not outlive recognized signatures.
fn blob_redirect_ttl(url: &Url, now_epoch_seconds: u64) -> Option<Duration> {
    if url.as_str().len() > MAX_BLOB_REDIRECT_URL_BYTES {
        return None;
    }
    let mut fields = HashMap::new();
    for (key, value) in url.query_pairs() {
        let key = key.to_ascii_lowercase();
        if matches!(key.as_str(), "range" | "offset" | "start" | "end") {
            return None;
        }
        if matches!(
            key.as_str(),
            "x-oss-additional-headers" | "x-amz-signedheaders" | "x-goog-signedheaders"
        ) && value
            .split(';')
            .any(|name| name.eq_ignore_ascii_case("range"))
        {
            return None;
        }
        if matches!(
            key.as_str(),
            "expires"
                | "x-oss-date"
                | "x-oss-expires"
                | "x-amz-date"
                | "x-amz-expires"
                | "x-goog-date"
                | "x-goog-expires"
        ) {
            if fields.insert(key, value.into_owned()).is_some() {
                return None;
            }
        }
    }
    let mut ttl = BLOB_REDIRECT_TTL.as_secs();
    let mut cap = |expires: u64| -> Option<()> {
        ttl = ttl.min(
            expires
                .checked_sub(now_epoch_seconds.checked_add(BLOB_REDIRECT_EXPIRY_GRACE_SECONDS)?)?,
        );
        (ttl > 0).then_some(())
    };
    if let Some(expires) = fields.get("expires") {
        cap(expires.parse().ok()?)?;
    }
    for prefix in ["x-oss", "x-amz", "x-goog"] {
        match (
            fields.get(&format!("{prefix}-date")),
            fields.get(&format!("{prefix}-expires")),
        ) {
            (None, None) => {}
            (Some(date), Some(seconds)) => {
                cap(basic_utc_epoch_seconds(date)?.checked_add(seconds.parse().ok()?)?)?
            }
            _ => return None,
        }
    }
    Some(Duration::from_secs(ttl))
}

/// Parse the bounded UTC basic timestamp used by OSS/AWS/Google V4 signatures.
/// Civil-day arithmetic avoids a timezone-dependent parser or extra dependency.
fn basic_utc_epoch_seconds(value: &str) -> Option<u64> {
    let bytes = value.as_bytes();
    if bytes.len() != 16 || bytes[8] != b'T' || bytes[15] != b'Z' {
        return None;
    }
    if !bytes
        .iter()
        .enumerate()
        .all(|(index, byte)| matches!(index, 8 | 15) || byte.is_ascii_digit())
    {
        return None;
    }
    let number = |start: usize, end: usize| value[start..end].parse::<i64>().ok();
    let (year, month, day) = (number(0, 4)?, number(4, 6)?, number(6, 8)?);
    let (hour, minute, second) = (number(9, 11)?, number(11, 13)?, number(13, 15)?);
    if !(1970..=9999).contains(&year)
        || !(1..=12).contains(&month)
        || !(0..=23).contains(&hour)
        || !(0..=59).contains(&minute)
        || !(0..=59).contains(&second)
    {
        return None;
    }
    let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    let days_in_month = [
        31,
        if leap { 29 } else { 28 },
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ][(month - 1) as usize];
    if !(1..=days_in_month).contains(&day) {
        return None;
    }
    let adjusted_year = year - i64::from(month <= 2);
    let era = adjusted_year.div_euclid(400);
    let year_of_era = adjusted_year - era * 400;
    let adjusted_month = month + if month > 2 { -3 } else { 9 };
    let day_of_year = (153 * adjusted_month + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    let days = era * 146097 + day_of_era - 719468;
    u64::try_from(days * 86400 + hour * 3600 + minute * 60 + second).ok()
}

fn token_key(repository: &str, actions: &str) -> String {
    format!("{repository}\0{actions}")
}

/// One explicit credential destination, independent of the Registry/CDN
/// origins. Require canonical spelling so path or authority normalization
/// cannot silently broaden what an operator configured.
pub(crate) fn parse_auth_realm(value: &str) -> Result<Url, RegistryError> {
    if value.is_empty() || value.len() > 4096 {
        return Err(invalid("registry auth realm must be a bounded HTTPS URL"));
    }
    let realm = Url::parse(value)
        .map_err(|_| invalid("registry auth realm must be a canonical HTTPS URL"))?;
    if realm.scheme() != "https"
        || realm.host_str().is_none()
        || !realm.username().is_empty()
        || realm.password().is_some()
        || realm.query().is_some()
        || realm.fragment().is_some()
        || realm.as_str() != value
    {
        return Err(invalid(
            "registry auth realm requires a canonical HTTPS URL without userinfo, query or fragment",
        ));
    }
    Ok(realm)
}

async fn active_registry_operation<T, E>(
    deadline: Instant,
    cancellation: &IoCancellation,
    operation: impl Future<Output = Result<T, E>>,
) -> Result<T, E>
where
    E: From<RegistryError>,
{
    if cancellation.is_cancelled() || Instant::now() >= deadline {
        return Err(transient("registry operation was cancelled or timed out").into());
    }
    let remaining = deadline.saturating_duration_since(Instant::now());
    let limit = tokio::time::sleep(remaining.min(REQUEST_TIMEOUT));
    tokio::pin!(limit);
    tokio::pin!(operation);
    loop {
        tokio::select! {
            result = &mut operation => {
                if cancellation.is_cancelled() || Instant::now() >= deadline {
                    return Err(transient("registry operation was cancelled or timed out").into());
                }
                return result;
            }
            _ = &mut limit => {
                return Err(transient("registry operation timed out").into());
            }
            _ = tokio::time::sleep(Duration::from_millis(25)) => {
                if cancellation.is_cancelled() {
                    return Err(transient("registry operation was cancelled").into());
                }
            }
        }
    }
}

/// One daemon-selected repository and mutation fence. Logical object keys stay
/// in receipts; physical blobs are always addressed by their SHA256 digest.
/// The format owns config/manifest/index structure and final publication order.
pub(crate) struct RegistryWriteTarget {
    client: Arc<RegistryClient>,
    repository: String,
    namespace: String,
    write_domain_id: String,
    fence: Arc<NamespaceMutationFence>,
}

impl std::fmt::Debug for RegistryWriteTarget {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("RegistryWriteTarget")
            .field("namespace", &self.namespace)
            .field("write_domain_id", &self.write_domain_id)
            .finish_non_exhaustive()
    }
}

impl RegistryWriteTarget {
    pub(crate) fn new(
        client: Arc<RegistryClient>,
        repository: String,
        write_domain_id: String,
        fence: Arc<NamespaceMutationFence>,
    ) -> Result<Arc<Self>, RegistryError> {
        validate_repository(&repository)?;
        let namespace = format!("{}/{}", client.registry(), repository);
        if write_domain_id.is_empty()
            || write_domain_id.len() > 64 * 1024
            || fence.namespace() != namespace
        {
            return Err(invalid(
                "registry target is outside its canonical write domain",
            ));
        }
        Ok(Arc::new(Self {
            client,
            repository,
            namespace,
            write_domain_id,
            fence,
        }))
    }

    pub(crate) fn client(&self) -> &Arc<RegistryClient> {
        &self.client
    }

    pub(crate) fn repository(&self) -> &str {
        &self.repository
    }

    fn validate_lease(&self, lease: &UploadTransactionLease) -> Result<(), ObjectStoreError> {
        if !lease.validates(&self.fence) {
            return Err(ObjectStoreError::new(
                PublishFailureKind::IdentityConflict,
                "registry write lease belongs to another mutation fence",
            ));
        }
        Ok(())
    }

    /// Publish exact manifest bytes under a digest or tag. Immutable digest
    /// publication can be retried; a tag deliberately points at a whole index.
    /// This method authenticates bytes, while the format validates references
    /// and calls it only after their blobs and child manifests are verified.
    #[allow(clippy::too_many_arguments)]
    pub(crate) async fn put_manifest(
        &self,
        reference: &str,
        media_type: &str,
        payload: &[u8],
        lease: &UploadTransactionLease,
        deadline: Instant,
        cancellation: &IoCancellation,
    ) -> Result<RegistryManifest, ObjectStoreError> {
        self.validate_lease(lease)?;
        validate_selector(reference)?;
        if payload.is_empty() || payload.len() as u64 > MAX_REGISTRY_READ_BYTES {
            return Err(invalid("registry manifest write exceeds its byte bound").into());
        }
        let value: Value = serde_json::from_slice(payload)
            .map_err(|_| invalid("registry manifest write is not JSON"))?;
        if value.get("schemaVersion").and_then(Value::as_u64) != Some(2)
            || value.get("mediaType").and_then(Value::as_str) != Some(media_type)
            || !matches!(
                media_type,
                "application/vnd.oci.image.manifest.v1+json"
                    | "application/vnd.oci.image.index.v1+json"
                    | "application/vnd.docker.distribution.manifest.v2+json"
                    | "application/vnd.docker.distribution.manifest.list.v2+json"
            )
        {
            return Err(invalid("registry manifest schema or media type is invalid").into());
        }
        let expected = digest(payload);
        if reference.starts_with("sha256:") && reference != expected {
            return Err(integrity("manifest write digest differs from its bytes").into());
        }
        let url = self
            .client
            .endpoint(&format!("v2/{}/manifests/{reference}", self.repository))?;
        if reference.starts_with("sha256:") {
            let head = self
                .client
                .authorized_request(
                    Method::HEAD,
                    url.clone(),
                    &self.repository,
                    "pull,push",
                    &[],
                    Bytes::new(),
                    deadline,
                    cancellation,
                )
                .await?;
            match head.status() {
                StatusCode::OK => {
                    validate_response_identity(&head, &expected, payload.len() as u64)?;
                    return self
                        .verify_manifest(reference, payload, deadline, cancellation)
                        .await;
                }
                StatusCode::NOT_FOUND => {}
                status => return Err(status_error(status).into()),
            }
        }
        let result = self
            .client
            .authorized_request(
                Method::PUT,
                url,
                &self.repository,
                "pull,push",
                &[
                    (CONTENT_TYPE, media_type.to_owned()),
                    (CONTENT_LENGTH, payload.len().to_string()),
                ],
                Bytes::copy_from_slice(payload),
                deadline,
                cancellation,
            )
            .await;
        match result {
            Ok(response) if response.status() == StatusCode::CREATED => {
                validate_advertised_digest(&response, &expected)?;
            }
            Ok(response) => {
                let error = status_error(response.status());
                if !error.retryable() {
                    return Err(error.into());
                }
                // Completion can be ambiguous; resolve it by immutable bytes.
                return resolve_ambiguous_write(
                    self.verify_manifest(reference, payload, deadline, cancellation)
                        .await,
                    error,
                    reference.starts_with("sha256:"),
                );
            }
            Err(error) => {
                return resolve_ambiguous_write(
                    self.verify_manifest(reference, payload, deadline, cancellation)
                        .await,
                    error,
                    reference.starts_with("sha256:"),
                );
            }
        }
        self.verify_manifest(reference, payload, deadline, cancellation)
            .await
    }

    async fn verify_manifest(
        &self,
        reference: &str,
        payload: &[u8],
        deadline: Instant,
        cancellation: &IoCancellation,
    ) -> Result<RegistryManifest, ObjectStoreError> {
        let manifest = active_registry_operation(
            deadline,
            cancellation,
            self.client.manifest(&self.repository, reference),
        )
        .await?;
        if manifest.payload != payload {
            return Err(integrity("registry manifest read-back differs from publication").into());
        }
        Ok(manifest)
    }

    async fn head_blob(
        &self,
        identity: &ImmutableObjectIdentity,
        deadline: Instant,
        cancellation: &IoCancellation,
    ) -> Result<bool, ObjectStoreError> {
        let url = self
            .client
            .endpoint(&format!("v2/{}/blobs/{}", self.repository, identity.sha256))?;
        let response = self
            .client
            .authorized_request(
                Method::HEAD,
                url,
                &self.repository,
                "pull,push",
                &[],
                Bytes::new(),
                deadline,
                cancellation,
            )
            .await?;
        match response.status() {
            StatusCode::OK => {
                validate_response_identity(&response, &identity.sha256, identity.length)?;
                Ok(true)
            }
            StatusCode::NOT_FOUND => Ok(false),
            status => Err(status_error(status).into()),
        }
    }

    async fn verify_blob(
        &self,
        identity: &ImmutableObjectIdentity,
        deadline: Instant,
        cancellation: &IoCancellation,
        chunk_bytes: usize,
    ) -> Result<VerifiedWriteReceipt, ObjectStoreError> {
        if !self.head_blob(identity, deadline, cancellation).await? {
            return Err(transient("completed registry blob is not visible").into());
        }
        let mut offset = 0;
        let mut checksum = Sha256::new();
        while offset < identity.length {
            let take = (identity.length - offset).min(chunk_bytes as u64);
            let bytes = active_registry_operation(
                deadline,
                cancellation,
                self.client.read_blob_range(
                    &self.repository,
                    &identity.sha256,
                    identity.length,
                    offset,
                    take,
                ),
            )
            .await?;
            checksum.update(&bytes);
            offset += take;
        }
        if format!("sha256:{:x}", checksum.finalize()) != identity.sha256 {
            return Err(integrity("registry blob read-back checksum differs from source").into());
        }
        Ok(VerifiedWriteReceipt {
            object_key: identity.object_key.clone(),
            sha256: identity.sha256.clone(),
            length: identity.length,
            object_version: None,
        })
    }

    async fn abort_upload(&self, location: Url) -> bool {
        let (_sender, receiver) = tokio::sync::watch::channel(false);
        let cancellation = IoCancellation::new(receiver);
        matches!(
            self.client
                .authorized_request(
                    Method::DELETE,
                    location,
                    &self.repository,
                    "pull,push",
                    &[(CONTENT_LENGTH, "0".into())],
                    Bytes::new(),
                    Instant::now() + REQUEST_TIMEOUT,
                    &cancellation,
                )
                .await,
            Ok(response) if matches!(response.status(), StatusCode::NO_CONTENT | StatusCode::NOT_FOUND)
        )
    }

    #[allow(clippy::too_many_arguments)]
    async fn transfer_blob(
        &self,
        identity: &ImmutableObjectIdentity,
        source: Arc<dyn ReplayableByteSource>,
        location: &mut Url,
        minimum_chunk: u64,
        deadline: Instant,
        cancellation: &IoCancellation,
        chunk_bytes: usize,
    ) -> Result<VerifiedWriteReceipt, ObjectStoreError> {
        if minimum_chunk > chunk_bytes as u64 && identity.length > chunk_bytes as u64 {
            return Err(
                invalid("registry minimum upload chunk exceeds the configured bound").into(),
            );
        }
        let mut offset = 0;
        let mut checksum = Sha256::new();
        while offset < identity.length {
            let take = (identity.length - offset).min(chunk_bytes as u64) as usize;
            let bytes =
                active_registry_operation(deadline, cancellation, source.read_at(offset, take))
                    .await?;
            if bytes.len() != take {
                return Err(invalid(
                    "registry replayable source returned a short or oversized chunk",
                )
                .into());
            }
            checksum.update(&bytes);
            let end = offset + take as u64 - 1;
            let response = self
                .client
                .authorized_request(
                    Method::PATCH,
                    location.clone(),
                    &self.repository,
                    "pull,push",
                    &[
                        (CONTENT_TYPE, "application/octet-stream".into()),
                        (CONTENT_LENGTH, take.to_string()),
                        (CONTENT_RANGE, format!("{offset}-{end}")),
                    ],
                    Bytes::from(bytes),
                    deadline,
                    cancellation,
                )
                .await?;
            if response.status() != StatusCode::ACCEPTED {
                return Err(status_error(response.status()).into());
            }
            *location = self.client.response_location(location, &response)?;
            let expected = format!("0-{end}");
            if response
                .headers()
                .get(RANGE)
                .and_then(|value| value.to_str().ok())
                != Some(expected.as_str())
            {
                return Err(
                    integrity("registry upload acknowledgement differs from its chunk").into(),
                );
            }
            offset = end + 1;
        }
        if format!("sha256:{:x}", checksum.finalize()) != identity.sha256 {
            return Err(integrity("registry source checksum differs from upload identity").into());
        }
        let mut complete = location.clone();
        if complete.query_pairs().any(|(name, _)| name == "digest") {
            return Err(
                invalid("registry upload Location already contains a digest selector").into(),
            );
        }
        complete
            .query_pairs_mut()
            .append_pair("digest", &identity.sha256);
        let result = self
            .client
            .authorized_request(
                Method::PUT,
                complete,
                &self.repository,
                "pull,push",
                &[(CONTENT_LENGTH, "0".into())],
                Bytes::new(),
                deadline,
                cancellation,
            )
            .await;
        match result {
            Ok(response) if response.status() == StatusCode::CREATED => {
                validate_advertised_digest(&response, &identity.sha256)?;
            }
            Ok(response) => {
                let error = status_error(response.status());
                if !error.retryable() {
                    return Err(error.into());
                }
                return resolve_ambiguous_write(
                    self.verify_blob(identity, deadline, cancellation, chunk_bytes)
                        .await,
                    error,
                    true,
                );
            }
            Err(error) => {
                return resolve_ambiguous_write(
                    self.verify_blob(identity, deadline, cancellation, chunk_bytes)
                        .await,
                    error,
                    true,
                );
            }
        }
        self.verify_blob(identity, deadline, cancellation, chunk_bytes)
            .await
    }
}

#[async_trait]
impl ImmutableStreamWriter for RegistryWriteTarget {
    fn namespace(&self) -> &str {
        &self.namespace
    }

    fn fence(&self) -> Arc<NamespaceMutationFence> {
        Arc::clone(&self.fence)
    }

    fn write_domain_id(&self) -> &str {
        &self.write_domain_id
    }

    async fn ensure_stream_verified(
        &self,
        identity: &ImmutableObjectIdentity,
        source: Arc<dyn ReplayableByteSource>,
        lease: UploadTransactionLease,
        deadline: Instant,
        cancellation: IoCancellation,
        chunk_bytes: usize,
    ) -> Result<VerifiedWriteReceipt, ObjectStoreError> {
        self.validate_lease(&lease)?;
        validate_digest(&identity.sha256)?;
        if identity.object_key.is_empty()
            || identity.object_key.len() > 64 * 1024
            || identity.length == 0
            || identity.length != source.length()
            || !(1..=MAX_UPLOAD_CHUNK_BYTES).contains(&chunk_bytes)
        {
            return Err(invalid("registry replayable source or chunk geometry is invalid").into());
        }
        if self.head_blob(identity, deadline, &cancellation).await? {
            return self
                .verify_blob(identity, deadline, &cancellation, chunk_bytes)
                .await;
        }
        let start = self
            .client
            .endpoint(&format!("v2/{}/blobs/uploads/", self.repository))?;
        let response = self
            .client
            .authorized_request(
                Method::POST,
                start.clone(),
                &self.repository,
                "pull,push",
                &[(CONTENT_LENGTH, "0".into())],
                Bytes::new(),
                deadline,
                &cancellation,
            )
            .await
            .map_err(|error| ObjectStoreError::from(error).with_abort_cleanup_unconfirmed())?;
        if response.status() != StatusCode::ACCEPTED {
            return Err(status_error(response.status()).into());
        }
        let mut location = self
            .client
            .response_location(&start, &response)
            .map_err(|error| ObjectStoreError::from(error).with_abort_cleanup_unconfirmed())?;
        let minimum_chunk = response.headers().get("oci-chunk-min-length").map(|value| {
            value
                .to_str()
                .ok()
                .and_then(|value| value.parse::<u64>().ok())
        });
        drop(response);
        let minimum_chunk = match minimum_chunk {
            Some(value) => match value {
                Some(value) => value,
                None => {
                    if self.abort_upload(location).await {
                        return Err(integrity("registry minimum chunk header is invalid").into());
                    }
                    return Err(ObjectStoreError::from(integrity(
                        "registry minimum chunk header is invalid",
                    ))
                    .with_abort_cleanup_unconfirmed());
                }
            },
            None => 0,
        };
        let result = self
            .transfer_blob(
                identity,
                source,
                &mut location,
                minimum_chunk,
                deadline,
                &cancellation,
                chunk_bytes,
            )
            .await;
        if result.is_err() && !self.abort_upload(location).await {
            return result.map_err(ObjectStoreError::with_abort_cleanup_unconfirmed);
        }
        result
    }
}

#[cfg(test)]
struct RegistryBytesSource(Arc<[u8]>);

fn resolve_ambiguous_write<T>(
    verification: Result<T, ObjectStoreError>,
    original: RegistryError,
    immutable: bool,
) -> Result<T, ObjectStoreError> {
    match verification {
        Ok(value) => Ok(value),
        Err(error) if immutable && error.kind == PublishFailureKind::IdentityConflict => Err(error),
        Err(_) => Err(original.into()),
    }
}

#[cfg(test)]
impl ReplayableByteSource for RegistryBytesSource {
    fn length(&self) -> u64 {
        self.0.len() as u64
    }

    fn read_at<'a>(
        &'a self,
        offset: u64,
        max_bytes: usize,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<u8>, ObjectStoreError>> + Send + 'a>> {
        Box::pin(async move {
            let start = usize::try_from(offset)
                .ok()
                .filter(|start| *start <= self.0.len())
                .ok_or_else(|| {
                    ObjectStoreError::from(invalid("registry metadata source offset is invalid"))
                })?;
            let end = start.saturating_add(max_bytes).min(self.0.len());
            Ok(self.0[start..end].to_vec())
        })
    }
}

fn validate_advertised_digest(
    response: &Response<Body>,
    expected: &str,
) -> Result<(), RegistryError> {
    if response
        .headers()
        .get("docker-content-digest")
        .is_some_and(|value| value.to_str().ok() != Some(expected))
    {
        return Err(integrity(
            "registry response digest differs from immutable identity",
        ));
    }
    Ok(())
}

fn validate_response_identity(
    response: &Response<Body>,
    expected_digest: &str,
    expected_length: u64,
) -> Result<(), RegistryError> {
    validate_advertised_digest(response, expected_digest)?;
    let length = response
        .headers()
        .get(CONTENT_LENGTH)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<u64>().ok())
        .ok_or_else(|| integrity("registry HEAD response has no valid Content-Length"))?;
    if length != expected_length {
        return Err(integrity(
            "registry HEAD length differs from immutable identity",
        ));
    }
    Ok(())
}

pub(crate) fn layer_ref(value: &Value) -> Result<RegistryLayerRef, RegistryError> {
    let digest = value
        .get("digest")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid("manifest descriptor has no digest"))?;
    validate_digest(digest)?;
    let size = value
        .get("size")
        .and_then(Value::as_u64)
        .filter(|size| *size > 0 && *size <= 2 * 1024_u64.pow(4))
        .ok_or_else(|| invalid("manifest descriptor size is outside bounds"))?;
    Ok(RegistryLayerRef {
        digest: digest.to_owned(),
        size,
    })
}

async fn profiled_body(
    response: Response<Body>,
    limit: u64,
    stage: ReadPathStage,
) -> Result<Vec<u8>, RegistryError> {
    let timer = ReadPathTimer::start(stage);
    let result = bounded_body(response, limit).await;
    timer.finish(
        result.as_ref().map_or(0, |bytes| bytes.len() as u64),
        result.is_ok(),
    );
    result
}

async fn bounded_body(response: Response<Body>, limit: u64) -> Result<Vec<u8>, RegistryError> {
    let is_range = response.status() == StatusCode::PARTIAL_CONTENT;
    if response
        .headers()
        .get(CONTENT_ENCODING)
        .is_some_and(|value| value != "identity")
    {
        return Err(integrity("registry unexpectedly encoded the response"));
    }
    if let Some(length) = response.headers().get(CONTENT_LENGTH) {
        let length = length
            .to_str()
            .ok()
            .and_then(|value| value.parse::<u64>().ok())
            .ok_or_else(|| integrity("registry Content-Length is invalid"))?;
        if length > limit {
            return Err(integrity("registry response exceeds its byte bound"));
        }
    }
    tokio::time::timeout(REQUEST_TIMEOUT, async move {
        let mut body = response.into_body();
        let mut output = Vec::new();
        while let Some(chunk) = body.data().await {
            let chunk = chunk.map_err(|_| transient("registry response stream failed"))?;
            if (output.len() as u64)
                .checked_add(chunk.len() as u64)
                .is_none_or(|length| length > limit)
            {
                return Err(integrity("registry response exceeds its byte bound"));
            }
            output.extend_from_slice(&chunk);
        }
        TRANSFER_REQUESTS.fetch_add(1, Ordering::Relaxed);
        TRANSFER_BYTES.fetch_add(output.len() as u64, Ordering::Relaxed);
        if is_range {
            RANGE_REQUESTS.fetch_add(1, Ordering::Relaxed);
            RANGE_BYTES.fetch_add(output.len() as u64, Ordering::Relaxed);
        }
        Ok(output)
    })
    .await
    .map_err(|_| transient("registry body timed out"))?
}

fn bearer_parameters(challenge: &str) -> Result<HashMap<String, String>, RegistryError> {
    let (scheme, mut rest) = challenge
        .split_once(' ')
        .ok_or_else(|| authorization("invalid registry authentication challenge"))?;
    if !scheme.eq_ignore_ascii_case("bearer") {
        return Err(authorization(
            "registry requires unsupported authentication",
        ));
    }
    let mut parameters = HashMap::new();
    while !rest.trim().is_empty() {
        rest = rest.trim_start_matches(|byte: char| byte.is_ascii_whitespace() || byte == ',');
        let (key, value) = rest
            .split_once('=')
            .ok_or_else(|| authorization("invalid Bearer parameters"))?;
        let key = key.trim().to_ascii_lowercase();
        if key.is_empty() {
            return Err(authorization("empty Bearer parameter"));
        }
        let value = value.trim_start();
        let (value, remaining) = if let Some(value) = value.strip_prefix('"') {
            let end = value
                .find('"')
                .ok_or_else(|| authorization("unterminated Bearer parameter"))?;
            (&value[..end], &value[end + 1..])
        } else {
            value.split_once(',').unwrap_or((value, ""))
        };
        if value.contains(['\\', '\r', '\n'])
            || parameters.insert(key, value.trim().to_owned()).is_some()
        {
            return Err(authorization("invalid or duplicate Bearer parameter"));
        }
        rest = remaining;
    }
    Ok(parameters)
}

fn validate_registry(registry: &str) -> Result<(), RegistryError> {
    if registry.is_empty()
        || registry.len() > 253
        || !registry.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-' | b':' | b'[' | b']')
        })
    {
        return Err(invalid("invalid registry authority"));
    }
    let parsed = Url::parse(&format!("https://{registry}/"))
        .map_err(|_| invalid("invalid registry authority"))?;
    if parsed.host_str().is_none() || !parsed.username().is_empty() || parsed.password().is_some() {
        return Err(invalid("invalid registry authority"));
    }
    Ok(())
}

fn validate_repository(repository: &str) -> Result<(), RegistryError> {
    if repository.is_empty()
        || repository.len() > 1024
        || repository.split('/').any(|part| {
            part.is_empty()
                || part == "."
                || part == ".."
                || !part.bytes().all(|byte| {
                    byte.is_ascii_lowercase()
                        || byte.is_ascii_digit()
                        || matches!(byte, b'.' | b'_' | b'-')
                })
        })
    {
        return Err(invalid("invalid registry repository"));
    }
    Ok(())
}

fn validate_selector(reference: &str) -> Result<(), RegistryError> {
    if reference.starts_with("sha256:") {
        return validate_digest(reference);
    }
    if reference.is_empty()
        || reference.len() > 128
        || !reference
            .as_bytes()
            .first()
            .is_some_and(|byte| byte.is_ascii_alphanumeric() || *byte == b'_')
        || !reference
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
    {
        return Err(invalid("invalid registry tag"));
    }
    Ok(())
}

pub(crate) fn validate_digest(digest: &str) -> Result<(), RegistryError> {
    if digest.strip_prefix("sha256:").is_none_or(|hex| {
        hex.len() != 64
            || !hex
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    }) {
        return Err(invalid("registry digest must be canonical SHA256"));
    }
    Ok(())
}

fn digest(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}
fn invalid(message: &str) -> RegistryError {
    RegistryError::new(RegistryErrorKind::Invalid, message)
}
fn integrity(message: &str) -> RegistryError {
    RegistryError::new(RegistryErrorKind::Integrity, message)
}
fn authorization(message: &str) -> RegistryError {
    RegistryError::new(RegistryErrorKind::Authorization, message)
}
fn transient(message: &str) -> RegistryError {
    RegistryError::new(RegistryErrorKind::Transient, message)
}
fn status_error(status: StatusCode) -> RegistryError {
    let kind = if matches!(status, StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN) {
        RegistryErrorKind::Authorization
    } else if status == StatusCode::TOO_MANY_REQUESTS || status.is_server_error() {
        RegistryErrorKind::Transient
    } else {
        RegistryErrorKind::Permanent
    };
    let mut error = RegistryError::new(kind, format!("HTTP {}", status.as_u16()));
    error.status = Some(status);
    error
}

fn status_error_in_phase(status: StatusCode, phase: &str) -> RegistryError {
    let mut error = status_error(status);
    error.message = format!("{phase}: {}", error.message);
    error
}

fn resource_response(
    response: Response<Body>,
    method: &Method,
) -> Result<Response<Body>, RegistryError> {
    if matches!(
        response.status(),
        StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN
    ) {
        Err(status_error_in_phase(
            response.status(),
            &format!("resource {method}"),
        ))
    } else {
        Ok(response)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use hyper::service::{make_service_fn, service_fn};
    use std::convert::Infallible;
    use std::sync::atomic::AtomicBool;

    struct Fixture {
        client: Arc<RegistryClient>,
        server: tokio::task::JoinHandle<()>,
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            self.server.abort();
        }
    }

    fn fixture<F>(handler: F) -> Fixture
    where
        F: Fn(Request<Body>, &str) -> Response<Body> + Send + Sync + 'static,
    {
        let socket = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        socket.set_nonblocking(true).unwrap();
        let address = socket.local_addr().unwrap();
        let origin = format!("http://{address}");
        let mut client =
            RegistryClient::with_origin(address.to_string(), Url::parse(&origin).unwrap(), true);
        // Fixtures opt in explicitly instead of inheriting the process switch.
        Arc::get_mut(&mut client)
            .unwrap()
            .blob_redirect_cache_enabled = false;
        let handler = Arc::new(handler);
        let service = make_service_fn(move |_| {
            let handler = Arc::clone(&handler);
            let origin = origin.clone();
            async move {
                Ok::<_, Infallible>(service_fn(move |request| {
                    let response = handler(request, &origin);
                    async move { Ok::<_, Infallible>(response) }
                }))
            }
        });
        let server = tokio::spawn(async move {
            hyper::Server::from_tcp(socket)
                .unwrap()
                .serve(service)
                .await
                .unwrap();
        });
        Fixture { client, server }
    }

    fn partial(body: &str) -> Response<Body> {
        Response::builder()
            .status(206)
            .header("content-range", "bytes 2-4/8")
            .body(Body::from(body.to_owned()))
            .unwrap()
    }

    #[tokio::test]
    async fn physical_reader_requires_pinned_registry_identity_before_io() {
        let calls = Arc::new(AtomicU64::new(0));
        let observed = Arc::clone(&calls);
        let fixture = fixture(move |request, _| {
            observed.fetch_add(1, Ordering::Relaxed);
            assert!(request
                .uri()
                .path()
                .starts_with("/v2/team/test/blobs/sha256:"));
            assert_eq!(request.headers().get(RANGE).unwrap(), "bytes=2-4");
            partial("234")
        });
        let digest = format!("sha256:{}", "a".repeat(64));
        let other_digest = format!("sha256:{}", "b".repeat(64));
        let request = PhysicalRangeRequest {
            namespace: Some("team/test"),
            object_key: &digest,
            object_sha256: &digest,
            object_size: Some(8),
            object_version: None,
            offset: 2,
            length: 3,
        };
        let reader: &dyn PhysicalRangeReader = fixture.client.as_ref();
        assert_eq!(reader.source_id(), fixture.client.registry());
        for invalid in [
            PhysicalRangeRequest {
                namespace: None,
                ..request
            },
            PhysicalRangeRequest {
                object_size: None,
                ..request
            },
            PhysicalRangeRequest {
                object_sha256: &other_digest,
                ..request
            },
            PhysicalRangeRequest {
                object_version: Some("version"),
                ..request
            },
            PhysicalRangeRequest {
                offset: 7,
                ..request
            },
        ] {
            assert!(reader.read_physical_range(invalid).await.is_err());
        }
        assert_eq!(calls.load(Ordering::Relaxed), 0);
        assert_eq!(reader.read_physical_range(request).await.unwrap(), b"234");
        assert_eq!(calls.load(Ordering::Relaxed), 1);
        // A different advertised total is rejected even though the requested
        // partial span would fit in both sizes.
        assert!(matches!(
            reader
                .read_physical_range(PhysicalRangeRequest {
                    object_size: Some(9),
                    ..request
                })
                .await,
            Err(RangeReadError::Failure {
                class: RangeReadFailureClass::Integrity,
                ..
            })
        ));
    }

    #[tokio::test]
    async fn exact_ranges_reject_whole_short_misaddressed_and_encoded_responses() {
        let fixture = fixture(|request, _| {
            assert_eq!(
                request.headers().get(ACCEPT).unwrap(),
                "application/octet-stream"
            );
            assert_eq!(request.headers().get(RANGE).unwrap(), "bytes=2-4");
            match request.uri().path().split('/').nth(2).unwrap() {
                "good" => partial("234"),
                "whole" => Response::new(Body::from("01234567")),
                "short" => partial("23"),
                "wrong" => Response::builder()
                    .status(206)
                    .header("content-range", "bytes 1-3/8")
                    .body(Body::from("123"))
                    .unwrap(),
                "encoded" => Response::builder()
                    .status(206)
                    .header("content-range", "bytes 2-4/8")
                    .header(CONTENT_ENCODING, "gzip")
                    .body(Body::from("234"))
                    .unwrap(),
                _ => partial("2345"),
            }
        });
        let digest = format!("sha256:{}", "0".repeat(64));
        assert_eq!(
            fixture
                .client
                .read_blob_range("good", &digest, 8, 2, 3)
                .await
                .unwrap(),
            b"234"
        );
        for repository in ["whole", "short", "wrong", "encoded", "oversized"] {
            let error = fixture
                .client
                .read_blob_range(repository, &digest, 8, 2, 3)
                .await
                .unwrap_err();
            assert_eq!(
                error.kind,
                RegistryErrorKind::Integrity,
                "{repository}: {error}"
            );
        }
    }

    #[tokio::test]
    async fn physical_reader_splits_large_spans_into_bounded_http_windows() {
        let length = 17 * 1024 * 1024;
        let payload = Arc::new(
            (0..length + 137)
                .map(|offset| (offset % 251) as u8)
                .collect::<Vec<_>>(),
        );
        let expected_digest = digest(&payload);
        let source = Arc::clone(&payload);
        let observed = Arc::new(std::sync::Mutex::new(Vec::new()));
        let requests = Arc::clone(&observed);
        let fixture = fixture(move |request, _| {
            let range = request.headers().get(RANGE).unwrap().to_str().unwrap();
            let (start, end) = range
                .strip_prefix("bytes=")
                .unwrap()
                .split_once('-')
                .unwrap();
            let start: usize = start.parse().unwrap();
            let end: usize = end.parse().unwrap();
            assert!(end - start + 1 <= MAX_REGISTRY_READ_BYTES as usize);
            requests.lock().unwrap().push((start, end));
            Response::builder()
                .status(206)
                .header(
                    "content-range",
                    format!("bytes {start}-{end}/{}", source.len()),
                )
                .header("docker-content-digest", digest(&source))
                .body(Body::from(source[start..=end].to_vec()))
                .unwrap()
        });
        let reader: &dyn PhysicalRangeReader = fixture.client.as_ref();
        let request = PhysicalRangeRequest {
            namespace: Some("team/test"),
            object_key: &expected_digest,
            object_sha256: &expected_digest,
            object_size: Some(payload.len() as u64),
            object_version: None,
            offset: 7,
            length: length as u64,
        };
        let bytes = reader.read_physical_range(request).await.unwrap();
        assert_eq!(bytes, payload[7..7 + length]);
        assert_eq!(
            *observed.lock().unwrap(),
            vec![
                (7, 7 + 16 * 1024 * 1024 - 1),
                (7 + 16 * 1024 * 1024, 7 + length - 1)
            ]
        );
        assert_eq!(
            reader
                .read_physical_range(PhysicalRangeRequest {
                    object_size: Some(MAX_PHYSICAL_RANGE_BYTES + 1),
                    offset: 0,
                    length: MAX_PHYSICAL_RANGE_BYTES + 1,
                    ..request
                })
                .await
                .unwrap_err(),
            RangeReadError::InvalidRange
        );
        assert_eq!(observed.lock().unwrap().len(), 2);
        // The direct HTTP primitive still refuses a single oversized request.
        assert!(fixture
            .client
            .read_blob_range(
                "team/test",
                &expected_digest,
                payload.len() as u64,
                7,
                length as u64
            )
            .await
            .is_err());
        assert_eq!(observed.lock().unwrap().len(), 2);
    }

    fn dynamic_partial(request: &Request<Body>) -> Response<Body> {
        assert!(request.headers().get(AUTHORIZATION).is_none());
        let (start, end) = request
            .headers()
            .get(RANGE)
            .unwrap()
            .to_str()
            .unwrap()
            .strip_prefix("bytes=")
            .unwrap()
            .split_once('-')
            .unwrap();
        let (start, end): (usize, usize) = (start.parse().unwrap(), end.parse().unwrap());
        let bytes = b"01234567";
        Response::builder()
            .status(206)
            .header(
                "content-range",
                format!("bytes {start}-{end}/{}", bytes.len()),
            )
            .body(Body::from(bytes[start..=end].to_vec()))
            .unwrap()
    }

    fn enable_blob_redirect_cache(fixture: &mut Fixture) {
        Arc::get_mut(&mut fixture.client)
            .unwrap()
            .blob_redirect_cache_enabled = true;
    }

    #[tokio::test]
    async fn blob_redirect_reuse_keeps_identity_and_credentials_scoped() {
        let cdn_calls = Arc::new(AtomicU64::new(0));
        let observed = Arc::clone(&cdn_calls);
        let cdn = fixture(move |request, _| {
            observed.fetch_add(1, Ordering::Relaxed);
            dynamic_partial(&request)
        });
        let location = format!("{}blob?opaque=fixture-only", cdn.client.origin);
        let origin_calls = Arc::new(AtomicU64::new(0));
        let observed = Arc::clone(&origin_calls);
        let mut registry = fixture(move |request, _| {
            observed.fetch_add(1, Ordering::Relaxed);
            assert_eq!(
                request.headers().get(AUTHORIZATION).unwrap(),
                "Bearer fixture-pull-token"
            );
            Response::builder()
                .status(307)
                .header(LOCATION, &location)
                .body(Body::empty())
                .unwrap()
        });
        enable_blob_redirect_cache(&mut registry);
        Arc::get_mut(&mut registry.client).unwrap().credentials =
            Some(("fixture-user".into(), "fixture-password".into()));
        registry.client.tokens.lock().await.insert(
            token_key("team/test", "pull"),
            CachedToken {
                value: "fixture-pull-token".into(),
                expires: Instant::now() + Duration::from_secs(60),
            },
        );
        let sha = format!("sha256:{}", "0".repeat(64));
        assert_eq!(
            registry
                .client
                .read_blob_range("team/test", &sha, 8, 0, 2)
                .await
                .unwrap(),
            b"01"
        );
        let key = BlobRedirectKey {
            repository: "team/test".into(),
            digest: sha.clone(),
            size: 8,
        };
        let expires = registry
            .client
            .blob_redirects
            .lock()
            .await
            .get(&key)
            .unwrap()
            .expires;
        assert_eq!(
            registry
                .client
                .read_blob_range("team/test", &sha, 8, 4, 3)
                .await
                .unwrap(),
            b"456"
        );
        assert_eq!(origin_calls.load(Ordering::Relaxed), 1);
        assert_eq!(cdn_calls.load(Ordering::Relaxed), 2);
        assert_eq!(
            registry
                .client
                .blob_redirects
                .lock()
                .await
                .get(&key)
                .unwrap()
                .expires,
            expires
        );
        assert!(registry
            .client
            .cached_blob_redirect(&BlobRedirectKey {
                size: 9,
                ..key.clone()
            })
            .await
            .is_none());
        assert!(registry
            .client
            .cached_blob_redirect(&BlobRedirectKey {
                repository: "another/repo".into(),
                ..key
            })
            .await
            .is_none());
    }

    #[tokio::test]
    async fn cached_blob_redirect_403_refreshes_once_through_registry() {
        let cdn_calls = Arc::new(AtomicU64::new(0));
        let deny_old = Arc::new(AtomicBool::new(false));
        let calls = Arc::clone(&cdn_calls);
        let denied = Arc::clone(&deny_old);
        let cdn = fixture(move |request, _| {
            calls.fetch_add(1, Ordering::Relaxed);
            assert!(request.headers().get(AUTHORIZATION).is_none());
            if denied.load(Ordering::Relaxed) && request.uri().query() == Some("generation=1") {
                return Response::builder().status(403).body(Body::empty()).unwrap();
            }
            dynamic_partial(&request)
        });
        let origin_calls = Arc::new(AtomicU64::new(0));
        let calls = Arc::clone(&origin_calls);
        let location = cdn.client.origin.clone();
        let mut registry = fixture(move |_, _| {
            let generation = calls.fetch_add(1, Ordering::Relaxed) + 1;
            Response::builder()
                .status(307)
                .header(LOCATION, format!("{location}blob?generation={generation}"))
                .body(Body::empty())
                .unwrap()
        });
        enable_blob_redirect_cache(&mut registry);
        let sha = format!("sha256:{}", "0".repeat(64));
        registry
            .client
            .read_blob_range("team/test", &sha, 8, 2, 3)
            .await
            .unwrap();
        deny_old.store(true, Ordering::Relaxed);
        registry
            .client
            .read_blob_range("team/test", &sha, 8, 4, 2)
            .await
            .unwrap();
        registry
            .client
            .read_blob_range("team/test", &sha, 8, 0, 1)
            .await
            .unwrap();
        assert_eq!(origin_calls.load(Ordering::Relaxed), 2);
        assert_eq!(cdn_calls.load(Ordering::Relaxed), 4);
    }

    #[tokio::test]
    async fn cached_blob_redirect_transport_failure_uses_one_registry_fallback() {
        let cdn = fixture(|request, _| dynamic_partial(&request));
        let origin_calls = Arc::new(AtomicU64::new(0));
        let calls = Arc::clone(&origin_calls);
        let location = format!("{}blob", cdn.client.origin);
        let mut registry = fixture(move |_, _| {
            calls.fetch_add(1, Ordering::Relaxed);
            Response::builder()
                .status(307)
                .header(LOCATION, &location)
                .body(Body::empty())
                .unwrap()
        });
        enable_blob_redirect_cache(&mut registry);
        let sha = format!("sha256:{}", "0".repeat(64));
        let socket = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let unavailable =
            Url::parse(&format!("http://{}/blob", socket.local_addr().unwrap())).unwrap();
        drop(socket);
        registry
            .client
            .cache_blob_redirect(
                BlobRedirectKey {
                    repository: "team/test".into(),
                    digest: sha.clone(),
                    size: 8,
                },
                unavailable,
            )
            .await;
        assert_eq!(
            registry
                .client
                .read_blob_range("team/test", &sha, 8, 4, 2)
                .await
                .unwrap(),
            b"45"
        );
        assert_eq!(origin_calls.load(Ordering::Relaxed), 1);
    }

    #[tokio::test]
    async fn cached_blob_redirect_integrity_failure_is_not_retried() {
        let bad = Arc::new(AtomicBool::new(false));
        let corrupt = Arc::clone(&bad);
        let cdn = fixture(move |request, _| {
            if corrupt.load(Ordering::Relaxed) {
                return Response::builder()
                    .status(206)
                    .header("content-range", "bytes 0-2/8")
                    .body(Body::from("234"))
                    .unwrap();
            }
            dynamic_partial(&request)
        });
        let origin_calls = Arc::new(AtomicU64::new(0));
        let calls = Arc::clone(&origin_calls);
        let location = format!("{}blob", cdn.client.origin);
        let mut registry = fixture(move |_, _| {
            calls.fetch_add(1, Ordering::Relaxed);
            Response::builder()
                .status(307)
                .header(LOCATION, &location)
                .body(Body::empty())
                .unwrap()
        });
        enable_blob_redirect_cache(&mut registry);
        let sha = format!("sha256:{}", "0".repeat(64));
        registry
            .client
            .read_blob_range("team/test", &sha, 8, 2, 3)
            .await
            .unwrap();
        bad.store(true, Ordering::Relaxed);
        let error = registry
            .client
            .read_blob_range("team/test", &sha, 8, 2, 3)
            .await
            .unwrap_err();
        assert_eq!(error.kind, RegistryErrorKind::Integrity);
        assert_eq!(origin_calls.load(Ordering::Relaxed), 1);
        assert!(registry.client.blob_redirects.lock().await.is_empty());
    }

    #[tokio::test]
    async fn cached_blob_redirect_concurrent_ranges_share_only_the_url() {
        let cdn = fixture(|request, _| dynamic_partial(&request));
        let origin_calls = Arc::new(AtomicU64::new(0));
        let calls = Arc::clone(&origin_calls);
        let location = format!("{}blob", cdn.client.origin);
        let mut registry = fixture(move |_, _| {
            calls.fetch_add(1, Ordering::Relaxed);
            Response::builder()
                .status(307)
                .header(LOCATION, &location)
                .body(Body::empty())
                .unwrap()
        });
        enable_blob_redirect_cache(&mut registry);
        let sha = format!("sha256:{}", "0".repeat(64));
        registry
            .client
            .read_blob_range("team/test", &sha, 8, 0, 1)
            .await
            .unwrap();
        let mut tasks = tokio::task::JoinSet::new();
        for offset in 0..8 {
            let client = Arc::clone(&registry.client);
            let sha = sha.clone();
            tasks.spawn(async move {
                assert_eq!(
                    client
                        .read_blob_range("team/test", &sha, 8, offset, 1)
                        .await
                        .unwrap(),
                    vec![b'0' + offset as u8]
                );
            });
        }
        while let Some(result) = tasks.join_next().await {
            result.unwrap();
        }
        assert_eq!(origin_calls.load(Ordering::Relaxed), 1);
    }

    #[tokio::test]
    async fn blob_redirect_cache_is_bounded_expiring_and_url_validated() {
        let client = RegistryClient::test_http_client("http://registry.invalid/");
        let location = Url::parse("http://blob.invalid/data").unwrap();
        for number in 0..=MAX_BLOB_REDIRECTS {
            client
                .cache_blob_redirect(
                    BlobRedirectKey {
                        repository: "team/test".into(),
                        digest: format!("sha256:{number:064x}"),
                        size: 8,
                    },
                    location.clone(),
                )
                .await;
        }
        assert_eq!(client.blob_redirects.lock().await.len(), MAX_BLOB_REDIRECTS);
        let key = BlobRedirectKey {
            repository: "team/test".into(),
            digest: format!("sha256:{:064x}", MAX_BLOB_REDIRECTS),
            size: 8,
        };
        client
            .blob_redirects
            .lock()
            .await
            .get_mut(&key)
            .unwrap()
            .expires = Instant::now() - Duration::from_secs(1);
        assert!(client.cached_blob_redirect(&key).await.is_none());
        for value in [
            "http://user:secret@blob.invalid/data",
            "http://blob.invalid/data#fragment",
            "ftp://blob.invalid/data",
            "http://registry.invalid/data",
        ] {
            client
                .cache_blob_redirect(key.clone(), Url::parse(value).unwrap())
                .await;
            assert!(client.cached_blob_redirect(&key).await.is_none());
        }
    }

    #[test]
    fn signed_blob_redirect_expiry_and_range_binding_are_conservative() {
        let now = 1_700_000_000;
        let ttl = |query: &str| {
            blob_redirect_ttl(
                &Url::parse(&format!("https://blob.invalid/data?{query}")).unwrap(),
                now,
            )
        };
        assert_eq!(ttl("Expires=1700000030"), Some(Duration::from_secs(25)));
        assert_eq!(ttl("Expires=1700001000"), Some(BLOB_REDIRECT_TTL));
        assert_eq!(ttl("Expires=1700000005"), None);
        assert_eq!(ttl("Expires=1699999999"), None);
        assert_eq!(ttl("Expires=1700000030&Expires=1700000060"), None);
        assert_eq!(
            ttl("X-Amz-Date=20231114T221320Z&X-Amz-Expires=30"),
            Some(Duration::from_secs(25))
        );
        assert_eq!(
            ttl("x-oss-date=20231114T221320Z&x-oss-expires=60"),
            Some(Duration::from_secs(55))
        );
        assert_eq!(
            ttl("X-Goog-Date=20231114T221320Z&X-Goog-Expires=60&X-Goog-SignedHeaders=host%3Brange"),
            None
        );
        assert_eq!(ttl("range=bytes%3D0-7"), None);
        assert_eq!(ttl("x-oss-additional-headers=range"), None);
        assert_eq!(ttl("x-oss-date=20230229T000000Z&x-oss-expires=60"), None);
        assert_eq!(basic_utc_epoch_seconds("19700101T000000Z"), Some(0));
        assert_eq!(basic_utc_epoch_seconds("20231114T221320Z"), Some(now));
        assert_eq!(
            basic_utc_epoch_seconds("20240229T000000Z"),
            Some(1_709_164_800)
        );
        assert_eq!(basic_utc_epoch_seconds("20240230T000000Z"), None);
        assert_eq!(basic_utc_epoch_seconds("20231114T221360Z"), None);
        assert!(ttl(&format!(
            "opaque={}",
            "x".repeat(MAX_BLOB_REDIRECT_URL_BYTES)
        ))
        .is_none());
    }

    #[tokio::test]
    async fn bearer_tokens_are_cached_refreshed_once_and_never_forwarded_to_redirects() {
        let cdn = fixture(|request, _| {
            assert!(request.headers().get(AUTHORIZATION).is_none());
            assert_eq!(request.headers().get(RANGE).unwrap(), "bytes=2-4");
            partial("234")
        });
        let cdn_origin = cdn.client.origin.to_string();
        let token_requests = Arc::new(AtomicU64::new(0));
        let reject_old = Arc::new(AtomicBool::new(false));
        let requests = Arc::clone(&token_requests);
        let rejected = Arc::clone(&reject_old);
        let registry = fixture(move |request, origin| {
            if request.uri().path() == "/token" {
                assert_eq!(request.headers().get(ACCEPT).unwrap(), "application/json");
                assert!(request.headers().get(AUTHORIZATION).is_none());
                assert!(request
                    .uri()
                    .query()
                    .unwrap()
                    .contains("scope=repository%3Ateam%2Ftest%3Apull"));
                let generation = requests.fetch_add(1, Ordering::Relaxed) + 1;
                return Response::new(Body::from(format!(
                    "{{\"token\":\"secret-{generation}\",\"expires_in\":300}}"
                )));
            }
            let token = request
                .headers()
                .get(AUTHORIZATION)
                .and_then(|value| value.to_str().ok());
            if token.is_none()
                || (rejected.load(Ordering::Relaxed) && token == Some("Bearer secret-1"))
            {
                return Response::builder().status(401).header(WWW_AUTHENTICATE, format!("Bearer realm=\"{origin}/token\",service=\"registry\",scope=\"repository:team/test:pull\"")).body(Body::empty()).unwrap();
            }
            Response::builder()
                .status(307)
                .header(LOCATION, format!("{cdn_origin}blob?opaque=redacted"))
                .body(Body::empty())
                .unwrap()
        });
        let digest = format!("sha256:{}", "0".repeat(64));
        for _ in 0..2 {
            assert_eq!(
                registry
                    .client
                    .read_blob_range("team/test", &digest, 8, 2, 3)
                    .await
                    .unwrap(),
                b"234"
            );
        }
        assert_eq!(token_requests.load(Ordering::Relaxed), 1);
        reject_old.store(true, Ordering::Relaxed);
        registry
            .client
            .read_blob_range("team/test", &digest, 8, 2, 3)
            .await
            .unwrap();
        assert_eq!(token_requests.load(Ordering::Relaxed), 2);
    }

    #[test]
    fn configured_auth_realm_requires_one_canonical_https_endpoint() {
        let endpoint = "https://dockerauth.cn-shanghai.aliyuncs.com/auth";
        assert_eq!(parse_auth_realm(endpoint).unwrap().as_str(), endpoint);
        let configured = RegistryClient::new_configured_with_auth_realm(
            "registry.example",
            false,
            Some("writer".into()),
            Some("fixture-password".into()),
            Some(endpoint),
        )
        .unwrap();
        assert_eq!(
            configured.trusted_auth_realm.as_ref().unwrap().as_str(),
            endpoint
        );
        assert!(RegistryClient::new_configured(
            "registry.example",
            false,
            Some("writer".into()),
            Some("fixture-password".into()),
        )
        .unwrap()
        .trusted_auth_realm
        .is_none());
        for invalid in [
            "http://auth.example/auth",
            "auth.example/auth",
            "https://auth.example",
            "https://user@auth.example/auth",
            "https://user:password@auth.example/auth",
            "https://auth.example/auth?",
            "https://auth.example/auth?service=other",
            "https://auth.example/auth#",
            "https://auth.example/auth#fragment",
            "https://auth.example/a/../auth",
            " https://auth.example/auth",
            "https://AUTH.EXAMPLE/auth",
        ] {
            assert!(parse_auth_realm(invalid).is_err(), "accepted {invalid}");
            assert!(
                RegistryClient::new_configured_with_auth_realm(
                    "registry.example",
                    true,
                    None,
                    None,
                    Some(invalid),
                )
                .is_err(),
                "HTTP Registry opt-in must not weaken auth realm validation"
            );
        }
    }

    #[tokio::test]
    async fn trusted_cross_origin_tokens_keep_scopes_cached_and_cdn_credentials_stripped() {
        let scopes = Arc::new(std::sync::Mutex::new(Vec::new()));
        let observed = Arc::clone(&scopes);
        let basic = format!(
            "Basic {}",
            base64::engine::general_purpose::STANDARD.encode("writer:fixture-password")
        );
        let auth = fixture(move |request, origin| {
            assert_eq!(request.uri().path(), "/auth");
            assert_eq!(
                request.headers().get(AUTHORIZATION).unwrap(),
                basic.as_str()
            );
            let url = Url::parse(&format!("{origin}{}", request.uri())).unwrap();
            let query = url.query_pairs().collect::<HashMap<_, _>>();
            assert_eq!(query.get("service").unwrap(), "fixture-registry");
            let scope = query.get("scope").unwrap().to_string();
            let token = if scope.ends_with(":pull,push") {
                "push-token"
            } else {
                "pull-token"
            };
            observed.lock().unwrap().push(scope);
            Response::new(Body::from(
                serde_json::json!({"token":token,"expires_in":300}).to_string(),
            ))
        });
        let realm = auth.client.origin.join("auth").unwrap();
        let challenge_realm = realm.clone();
        let cdn = fixture(|request, _| {
            assert!(request.headers().get(AUTHORIZATION).is_none());
            partial("234")
        });
        let cdn_url = cdn.client.origin.join("blob").unwrap().to_string();
        let mut registry = fixture(move |request, _| {
            let expected = if request.method() == Method::HEAD {
                "Bearer push-token"
            } else {
                "Bearer pull-token"
            };
            if request
                .headers()
                .get(AUTHORIZATION)
                .and_then(|v| v.to_str().ok())
                != Some(expected)
            {
                return Response::builder()
                    .status(401)
                    .header(
                        WWW_AUTHENTICATE,
                        format!("Bearer realm=\"{challenge_realm}\",service=\"fixture-registry\""),
                    )
                    .body(Body::empty())
                    .unwrap();
            }
            if request.method() == Method::HEAD {
                Response::builder().status(404).body(Body::empty()).unwrap()
            } else {
                Response::builder()
                    .status(307)
                    .header(LOCATION, cdn_url.as_str())
                    .body(Body::empty())
                    .unwrap()
            }
        });
        // Only this private loopback fixture bypasses the production HTTPS
        // parser; constructor tests above enforce the public trust boundary.
        let client = Arc::get_mut(&mut registry.client).unwrap();
        client.credentials = Some(("writer".into(), "fixture-password".into()));
        client.trusted_auth_realm = Some(realm);
        let digest = format!("sha256:{}", "0".repeat(64));
        for _ in 0..2 {
            assert_eq!(
                registry
                    .client
                    .read_blob_range("team/test", &digest, 8, 2, 3)
                    .await
                    .unwrap(),
                b"234"
            );
            let (_sender, receiver) = tokio::sync::watch::channel(false);
            let cancellation = IoCancellation::new(receiver);
            let response = registry
                .client
                .authorized_request(
                    Method::HEAD,
                    registry
                        .client
                        .endpoint(&format!("v2/team/test/blobs/{digest}"))
                        .unwrap(),
                    "team/test",
                    "pull,push",
                    &[],
                    Bytes::new(),
                    Instant::now() + REQUEST_TIMEOUT,
                    &cancellation,
                )
                .await
                .unwrap();
            assert_eq!(response.status(), StatusCode::NOT_FOUND);
        }
        assert_eq!(
            *scopes.lock().unwrap(),
            [
                "repository:team/test:pull",
                "repository:team/test:pull,push"
            ]
        );
    }

    #[tokio::test]
    async fn unconfigured_or_different_auth_realm_never_receives_basic_credentials() {
        let calls = Arc::new(AtomicU64::new(0));
        let observed = Arc::clone(&calls);
        let auth = fixture(move |request, _| {
            observed.fetch_add(1, Ordering::Relaxed);
            assert!(request.headers().get(AUTHORIZATION).is_none());
            Response::builder().status(401).body(Body::empty()).unwrap()
        });
        let realm = auth.client.origin.join("auth").unwrap();
        for trusted in [
            None,
            Some(auth.client.origin.join("another-path").unwrap()),
            Some(Url::parse("https://different-auth.example/auth").unwrap()),
        ] {
            let challenge_realm = realm.clone();
            let mut registry = fixture(move |_, _| {
                Response::builder()
                    .status(401)
                    .header(
                        WWW_AUTHENTICATE,
                        format!("Bearer realm=\"{challenge_realm}\",service=\"fixture\""),
                    )
                    .body(Body::empty())
                    .unwrap()
            });
            let client = Arc::get_mut(&mut registry.client).unwrap();
            client.credentials = Some(("writer".into(), "fixture-password".into()));
            client.trusted_auth_realm = trusted;
            let error = registry
                .client
                .read_blob_range("team/test", &format!("sha256:{}", "0".repeat(64)), 8, 2, 3)
                .await
                .unwrap_err();
            assert_eq!(error.kind, RegistryErrorKind::Authorization);
            assert!(error.to_string().contains("token endpoint: HTTP 401"));
            assert!(!error.to_string().contains("fixture-password"));
        }
        assert_eq!(calls.load(Ordering::Relaxed), 3);
    }

    #[tokio::test]
    async fn token_endpoint_redirects_never_reach_another_host_or_path() {
        let followed = Arc::new(AtomicU64::new(0));
        let destination_calls = Arc::clone(&followed);
        let destination = fixture(move |_, _| {
            destination_calls.fetch_add(1, Ordering::Relaxed);
            Response::new(Body::empty())
        });
        let location = Arc::new(std::sync::Mutex::new(
            destination.client.origin.join("other").unwrap().to_string(),
        ));
        let target = Arc::clone(&location);
        let same_host_calls = Arc::clone(&followed);
        let auth = fixture(move |request, _| {
            if request.uri().path() != "/auth" {
                same_host_calls.fetch_add(1, Ordering::Relaxed);
                return Response::new(Body::empty());
            }
            assert!(request
                .headers()
                .get(AUTHORIZATION)
                .unwrap()
                .to_str()
                .unwrap()
                .starts_with("Basic "));
            Response::builder()
                .status(307)
                .header(LOCATION, target.lock().unwrap().as_str())
                .body(Body::empty())
                .unwrap()
        });
        let realm = auth.client.origin.join("auth").unwrap();
        let mut client = RegistryClient::with_origin(
            "registry.example".into(),
            Url::parse("https://registry.example/").unwrap(),
            true,
        );
        let unique = Arc::get_mut(&mut client).unwrap();
        unique.credentials = Some(("writer".into(), "fixture-password".into()));
        unique.trusted_auth_realm = Some(realm.clone());
        for target in [
            destination.client.origin.join("other").unwrap(),
            auth.client.origin.join("another-path").unwrap(),
        ] {
            *location.lock().unwrap() = target.to_string();
            let error = client
                .refresh_token(
                    "team/test",
                    &format!("Bearer realm=\"{realm}\",service=\"fixture\""),
                    "pull,push",
                )
                .await
                .unwrap_err();
            assert_eq!(error.kind, RegistryErrorKind::Authorization);
            assert!(error
                .to_string()
                .contains("token endpoint redirect is not permitted"));
        }
        assert_eq!(followed.load(Ordering::Relaxed), 0);
    }

    #[tokio::test]
    async fn same_origin_basic_token_refresh_preserves_legacy_behavior_and_resource_phase() {
        let token_calls = Arc::new(AtomicU64::new(0));
        let observed = Arc::clone(&token_calls);
        let reject = Arc::new(AtomicBool::new(false));
        let rejected = Arc::clone(&reject);
        let mut registry = fixture(move |request, origin| {
            if request.uri().path() == "/token" {
                assert!(request
                    .headers()
                    .get(AUTHORIZATION)
                    .unwrap()
                    .to_str()
                    .unwrap()
                    .starts_with("Basic "));
                observed.fetch_add(1, Ordering::Relaxed);
                return Response::new(Body::from(r#"{"token":"fixture-bearer","expires_in":300}"#));
            }
            if !rejected.load(Ordering::Relaxed)
                && request
                    .headers()
                    .get(AUTHORIZATION)
                    .and_then(|v| v.to_str().ok())
                    == Some("Bearer fixture-bearer")
            {
                return partial("234");
            }
            Response::builder()
                .status(401)
                .header(
                    WWW_AUTHENTICATE,
                    format!("Bearer realm=\"{origin}/token\",service=\"fixture\""),
                )
                .body(Body::empty())
                .unwrap()
        });
        Arc::get_mut(&mut registry.client).unwrap().credentials =
            Some(("writer".into(), "fixture-password".into()));
        let digest = format!("sha256:{}", "0".repeat(64));
        for _ in 0..2 {
            assert_eq!(
                registry
                    .client
                    .read_blob_range("team/test", &digest, 8, 2, 3)
                    .await
                    .unwrap(),
                b"234"
            );
        }
        assert_eq!(token_calls.load(Ordering::Relaxed), 1);
        reject.store(true, Ordering::Relaxed);
        let error = registry
            .client
            .read_blob_range("team/test", &digest, 8, 2, 3)
            .await
            .unwrap_err();
        assert_eq!(token_calls.load(Ordering::Relaxed), 2);
        assert!(error.to_string().contains("resource GET: HTTP 401"));
        assert!(!error.to_string().contains("fixture-bearer"));
        assert!(!error.to_string().contains("fixture-password"));
    }

    #[test]
    fn source_parsing_and_errors_preserve_identity_without_credentials() {
        let digest = format!("sha256:{}", "a".repeat(64));
        let reference = RegistryReference::parse(&format!(
            "registry.example/team/test:stable_accelerated@{digest}"
        ))
        .unwrap();
        assert_eq!(reference.repository, "team/test");
        assert_eq!(reference.reference, digest);
        for value in [
            "https://registry.example/team/test:v",
            "user@registry.example/team/test:v",
            "registry.example/team/../test:v",
            "registry.example/team/test:v?secret=1",
        ] {
            assert!(RegistryReference::parse(value).is_err(), "{value}");
        }
        assert_eq!(
            status_error(StatusCode::FORBIDDEN).failure_kind(),
            BlobRangeErrorKind::Authorization
        );
        assert!(status_error(StatusCode::SERVICE_UNAVAILABLE).retryable());
        assert!(!integrity("short range").retryable());
        assert!(RegistryClient::new("registry.example")
            .unwrap()
            .validate_url(&Url::parse("http://registry.example/token").unwrap())
            .is_err());
    }

    #[tokio::test]
    async fn manifest_platform_selection_checks_the_selected_payload_digest_and_size() {
        let payload = br#"{"schemaVersion":2,"config":{},"layers":[]}"#.to_vec();
        let identity = digest(&payload);
        let selected = identity.clone();
        let fixture = fixture(move |request, _| {
            if request.uri().path().ends_with("/index") {
                return Response::new(Body::from(serde_json::json!({
                    "schemaVersion":2, "manifests":[{"digest":selected,"size":payload.len(),"platform":{"os":"linux","architecture":"amd64"}}]
                }).to_string()));
            }
            Response::new(Body::from(payload.clone()))
        });
        assert_eq!(
            fixture
                .client
                .resolve_manifest("team/test", "index", "linux/amd64")
                .await
                .unwrap()
                .digest,
            identity
        );
        assert_eq!(
            fixture
                .client
                .resolve_manifest("team/test", "index", "linux/arm64")
                .await
                .unwrap_err()
                .kind,
            RegistryErrorKind::Invalid
        );
        let wrong = format!("sha256:{}", "0".repeat(64));
        assert_eq!(
            fixture
                .client
                .manifest("team/test", &wrong)
                .await
                .unwrap_err()
                .kind,
            RegistryErrorKind::Integrity
        );
    }

    mod write {
        use super::*;

        #[derive(Default)]
        struct WriteState {
            blobs: HashMap<String, Vec<u8>>,
            uploads: HashMap<String, Vec<u8>>,
            manifests: HashMap<String, (String, Vec<u8>)>,
            starts: usize,
            patches: Vec<usize>,
            aborts: usize,
            fail_patch: bool,
            deny_abort: bool,
            ambiguous_complete: bool,
            ambiguous_manifest: bool,
            corrupt_reads: bool,
            minimum_chunk: Option<String>,
            upload_origin: Option<String>,
            post_redirect: Option<String>,
            relative_locations: bool,
            observed_auth: Vec<(String, bool)>,
            require_bearer: bool,
            token_scopes: Vec<String>,
        }

        struct WriteFixture {
            target: Arc<RegistryWriteTarget>,
            state: Arc<Mutex<WriteState>>,
            server: tokio::task::JoinHandle<()>,
        }

        impl Drop for WriteFixture {
            fn drop(&mut self) {
                self.server.abort();
            }
        }

        fn write_fixture(state: Arc<Mutex<WriteState>>, credentials: bool) -> WriteFixture {
            let socket = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
            socket.set_nonblocking(true).unwrap();
            let authority = socket.local_addr().unwrap().to_string();
            let origin = format!("http://{authority}");
            let client = RegistryClient::new_configured(
                &authority,
                true,
                credentials.then(|| "writer".into()),
                credentials.then(|| "not-a-real-password".into()),
            )
            .unwrap();
            let namespace = format!("{authority}/team/test");
            let target = RegistryWriteTarget::new(
                client,
                "team/test".into(),
                "test-registry-writer".into(),
                NamespaceMutationFence::new(namespace).unwrap(),
            )
            .unwrap();
            let observed = Arc::clone(&state);
            let service = make_service_fn(move |_| {
                let state = Arc::clone(&observed);
                let origin = origin.clone();
                async move {
                    Ok::<_, Infallible>(service_fn(move |request| {
                        let state = Arc::clone(&state);
                        let origin = origin.clone();
                        async move { Ok::<_, Infallible>(write_request(request, state, origin).await) }
                    }))
                }
            });
            let server = tokio::spawn(async move {
                hyper::Server::from_tcp(socket)
                    .unwrap()
                    .serve(service)
                    .await
                    .unwrap();
            });
            WriteFixture {
                target,
                state,
                server,
            }
        }

        fn empty(status: StatusCode) -> Response<Body> {
            Response::builder()
                .status(status)
                .body(Body::empty())
                .unwrap()
        }

        async fn write_request(
            request: Request<Body>,
            shared: Arc<Mutex<WriteState>>,
            origin: String,
        ) -> Response<Body> {
            let (parts, body) = request.into_parts();
            let bytes = hyper::body::to_bytes(body).await.unwrap();
            let path = parts.uri.path();
            let mut state = shared.lock().await;
            state
                .observed_auth
                .push((origin.clone(), parts.headers.contains_key(AUTHORIZATION)));
            let url = Url::parse(&format!("{origin}{}", parts.uri)).unwrap();
            if path == "/token" {
                let scope = url
                    .query_pairs()
                    .find(|(name, _)| name == "scope")
                    .unwrap()
                    .1
                    .into_owned();
                state.token_scopes.push(scope.clone());
                let token = if scope.ends_with(":pull,push") {
                    "push-token"
                } else {
                    "pull-token"
                };
                return Response::new(Body::from(
                    serde_json::json!({"token":token,"expires_in":300}).to_string(),
                ));
            }
            if state.require_bearer {
                let actions = if parts.method == Method::GET {
                    "pull"
                } else {
                    "pull,push"
                };
                let expected = if actions == "pull" {
                    "Bearer pull-token"
                } else {
                    "Bearer push-token"
                };
                if parts
                    .headers
                    .get(AUTHORIZATION)
                    .and_then(|value| value.to_str().ok())
                    != Some(expected)
                {
                    return Response::builder().status(401)
                        .header(WWW_AUTHENTICATE, format!("Bearer realm=\"{origin}/token\",service=\"test\",scope=\"repository:team/test:{actions}\""))
                        .body(Body::empty()).unwrap();
                }
            }
            if path == "/v2/team/test/blobs/uploads/" && parts.method == Method::POST {
                if let Some(redirect) = state
                    .post_redirect
                    .as_ref()
                    .filter(|target| *target != &origin)
                {
                    return Response::builder()
                        .status(307)
                        .header(LOCATION, format!("{redirect}{path}"))
                        .body(Body::empty())
                        .unwrap();
                }
                state.starts += 1;
                let id = format!("session-{}", state.starts);
                state.uploads.insert(id.clone(), Vec::new());
                let upload_origin = state.upload_origin.as_deref().unwrap_or(&origin);
                let location = format!("/v2/team/test/blobs/uploads/{id}?opaque=0");
                let location = if state.relative_locations {
                    location
                } else {
                    format!("{upload_origin}{location}")
                };
                let mut response = Response::builder().status(202).header(LOCATION, location);
                if let Some(minimum) = &state.minimum_chunk {
                    response = response.header("oci-chunk-min-length", minimum);
                }
                return response.body(Body::empty()).unwrap();
            }
            if let Some(id) = path.strip_prefix("/v2/team/test/blobs/uploads/") {
                if parts.method == Method::DELETE {
                    state.aborts += 1;
                    if state.deny_abort {
                        return empty(StatusCode::INTERNAL_SERVER_ERROR);
                    }
                    state.uploads.remove(id);
                    return empty(StatusCode::NO_CONTENT);
                }
                if !state.uploads.contains_key(id) {
                    return empty(StatusCode::NOT_FOUND);
                }
                assert!(url.query_pairs().any(|(key, _)| key == "opaque"));
                if parts.method == Method::PATCH {
                    if state.fail_patch {
                        return empty(StatusCode::SERVICE_UNAVAILABLE);
                    }
                    let upload = state.uploads.get_mut(id).unwrap();
                    let start = upload.len();
                    let end = start + bytes.len() - 1;
                    assert_eq!(
                        parts.headers.get(CONTENT_RANGE).unwrap(),
                        format!("{start}-{end}").as_str()
                    );
                    upload.extend_from_slice(&bytes);
                    state.patches.push(bytes.len());
                    let location = format!("{path}?opaque={}", end + 1);
                    let location = if state.relative_locations {
                        location
                    } else {
                        format!("{origin}{location}")
                    };
                    return Response::builder()
                        .status(202)
                        .header(LOCATION, location)
                        .header(RANGE, format!("0-{end}"))
                        .body(Body::empty())
                        .unwrap();
                }
                if parts.method == Method::PUT {
                    let expected = url
                        .query_pairs()
                        .find(|(key, _)| key == "digest")
                        .unwrap()
                        .1
                        .into_owned();
                    let data = state.uploads.remove(id).unwrap();
                    assert_eq!(digest(&data), expected);
                    state.blobs.insert(expected.clone(), data);
                    if std::mem::take(&mut state.ambiguous_complete) {
                        return empty(StatusCode::SERVICE_UNAVAILABLE);
                    }
                    return Response::builder()
                        .status(201)
                        .header("docker-content-digest", expected)
                        .header(LOCATION, format!("{origin}/v2/team/test/blobs/committed"))
                        .body(Body::empty())
                        .unwrap();
                }
            }
            if let Some(identity) = path.strip_prefix("/v2/team/test/blobs/") {
                let Some(data) = state.blobs.get(identity) else {
                    return empty(StatusCode::NOT_FOUND);
                };
                if parts.method == Method::HEAD {
                    return Response::builder()
                        .status(200)
                        .header(CONTENT_LENGTH, data.len())
                        .header("docker-content-digest", identity)
                        .body(Body::empty())
                        .unwrap();
                }
                let requested = parts
                    .headers
                    .get(RANGE)
                    .unwrap()
                    .to_str()
                    .unwrap()
                    .strip_prefix("bytes=")
                    .unwrap();
                let (start, end) = requested.split_once('-').unwrap();
                let start: usize = start.parse().unwrap();
                let end: usize = end.parse().unwrap();
                let mut payload = data[start..=end].to_vec();
                if state.corrupt_reads {
                    payload[0] ^= 1;
                }
                return Response::builder()
                    .status(206)
                    .header(
                        "content-range",
                        format!("bytes {start}-{end}/{}", data.len()),
                    )
                    .header("docker-content-digest", identity)
                    .body(Body::from(payload))
                    .unwrap();
            }
            if let Some(reference) = path.strip_prefix("/v2/team/test/manifests/") {
                if parts.method == Method::PUT {
                    let expected = digest(&bytes);
                    let media_type = parts
                        .headers
                        .get(CONTENT_TYPE)
                        .unwrap()
                        .to_str()
                        .unwrap()
                        .to_owned();
                    state
                        .manifests
                        .insert(reference.into(), (media_type.clone(), bytes.to_vec()));
                    state
                        .manifests
                        .insert(expected.clone(), (media_type, bytes.to_vec()));
                    if std::mem::take(&mut state.ambiguous_manifest) {
                        return empty(StatusCode::SERVICE_UNAVAILABLE);
                    }
                    return Response::builder()
                        .status(201)
                        .header("docker-content-digest", expected)
                        .header(LOCATION, format!("{origin}{path}"))
                        .body(Body::empty())
                        .unwrap();
                }
                let Some((media_type, payload)) = state.manifests.get(reference) else {
                    return empty(StatusCode::NOT_FOUND);
                };
                return Response::builder()
                    .status(200)
                    .header(CONTENT_TYPE, media_type)
                    .header(CONTENT_LENGTH, payload.len())
                    .header("docker-content-digest", digest(payload))
                    .body(if parts.method == Method::HEAD {
                        Body::empty()
                    } else {
                        Body::from(payload.clone())
                    })
                    .unwrap();
            }
            empty(StatusCode::NOT_FOUND)
        }

        fn cancellation() -> (tokio::sync::watch::Sender<bool>, IoCancellation) {
            let (sender, receiver) = tokio::sync::watch::channel(false);
            (sender, IoCancellation::new(receiver))
        }

        async fn upload(
            target: &Arc<RegistryWriteTarget>,
            payload: &[u8],
            chunk_bytes: usize,
        ) -> Result<VerifiedWriteReceipt, ObjectStoreError> {
            let (_sender, cancel) = cancellation();
            target
                .ensure_stream_verified(
                    &ImmutableObjectIdentity {
                        object_key: "logical/layers/data".into(),
                        sha256: digest(payload),
                        length: payload.len() as u64,
                    },
                    Arc::new(RegistryBytesSource(Arc::from(payload))),
                    UploadTransactionLease::acquire(target.fence()).await,
                    Instant::now() + Duration::from_secs(10),
                    cancel,
                    chunk_bytes,
                )
                .await
        }

        #[tokio::test]
        async fn bounded_blob_upload_reuses_immutable_data_and_preserves_logical_key() {
            let fixture = write_fixture(Arc::default(), false);
            let payload = b"immutable-layer-contents-with-several-chunks";
            let receipt = upload(&fixture.target, payload, 7).await.unwrap();
            assert_eq!(receipt.object_key, "logical/layers/data");
            assert_eq!(receipt.sha256, digest(payload));
            assert_eq!(receipt.length, payload.len() as u64);
            assert_eq!(fixture.state.lock().await.starts, 1);
            assert!(fixture
                .state
                .lock()
                .await
                .patches
                .iter()
                .all(|length| *length <= 7));
            assert_eq!(upload(&fixture.target, payload, 7).await.unwrap(), receipt);
            assert_eq!(fixture.state.lock().await.starts, 1);
            let read = fixture
                .target
                .client()
                .read_blob_bounded(
                    "team/test",
                    &RegistryLayerRef {
                        digest: receipt.sha256,
                        size: receipt.length,
                    },
                    1024,
                )
                .await
                .unwrap();
            assert_eq!(read, payload);
            assert!(fixture
                .target
                .client
                .head_blob(
                    "team/test",
                    &RegistryLayerRef {
                        digest: digest(payload),
                        size: payload.len() as u64
                    }
                )
                .await
                .unwrap());
            assert!(!fixture
                .target
                .client
                .head_blob(
                    "team/test",
                    &RegistryLayerRef {
                        digest: digest(b"absent"),
                        size: 6
                    }
                )
                .await
                .unwrap());
            assert!(fixture
                .target
                .client
                .head_blob(
                    "team/test",
                    &RegistryLayerRef {
                        digest: digest(payload),
                        size: 1
                    }
                )
                .await
                .is_err());
        }

        #[tokio::test]
        async fn completion_ambiguity_is_resolved_by_cryptographic_readback() {
            let shared = Arc::new(Mutex::new(WriteState {
                ambiguous_complete: true,
                ..Default::default()
            }));
            let fixture = write_fixture(shared, false);
            upload(&fixture.target, b"committed-before-disconnect", 8)
                .await
                .unwrap();
            assert_eq!(fixture.state.lock().await.aborts, 0);
        }

        #[tokio::test]
        async fn failures_abort_partial_upload_and_report_unconfirmed_cleanup() {
            let shared = Arc::new(Mutex::new(WriteState {
                fail_patch: true,
                ..Default::default()
            }));
            let fixture = write_fixture(shared, false);
            let error = upload(&fixture.target, b"must-not-be-published", 4)
                .await
                .unwrap_err();
            assert!(error.retryable());
            assert!(!error.abort_cleanup_unconfirmed);
            assert!(fixture.state.lock().await.blobs.is_empty());
            assert!(fixture.state.lock().await.uploads.is_empty());
            fixture.state.lock().await.deny_abort = true;
            let error = upload(&fixture.target, b"must-not-be-published", 4)
                .await
                .unwrap_err();
            assert!(error.abort_cleanup_unconfirmed);
            fixture.state.lock().await.fail_patch = false;
            fixture.state.lock().await.deny_abort = false;
            upload(&fixture.target, b"must-not-be-published", 4)
                .await
                .unwrap();
        }

        #[tokio::test]
        async fn source_identity_and_remote_readback_are_authenticated() {
            let fixture = write_fixture(Arc::default(), false);
            let (_sender, cancel) = cancellation();
            let error = fixture
                .target
                .ensure_stream_verified(
                    &ImmutableObjectIdentity {
                        object_key: "logical".into(),
                        sha256: digest(b"wrong"),
                        length: 4,
                    },
                    Arc::new(RegistryBytesSource(Arc::from(&b"data"[..]))),
                    UploadTransactionLease::acquire(fixture.target.fence()).await,
                    Instant::now() + Duration::from_secs(10),
                    cancel,
                    3,
                )
                .await
                .unwrap_err();
            assert_eq!(error.kind, PublishFailureKind::IdentityConflict);
            assert!(fixture.state.lock().await.blobs.is_empty());
            assert_eq!(fixture.state.lock().await.aborts, 1);
            fixture.state.lock().await.corrupt_reads = true;
            let error = upload(&fixture.target, b"remote-data", 4)
                .await
                .unwrap_err();
            assert_eq!(error.kind, PublishFailureKind::IdentityConflict);
        }

        #[tokio::test]
        async fn lease_and_chunk_bounds_reject_before_network_io() {
            let fixture = write_fixture(Arc::default(), false);
            let foreign = NamespaceMutationFence::new(fixture.target.namespace()).unwrap();
            let (_sender, cancel) = cancellation();
            let identity = ImmutableObjectIdentity {
                object_key: "logical".into(),
                sha256: digest(b"data"),
                length: 4,
            };
            let error = fixture
                .target
                .ensure_stream_verified(
                    &identity,
                    Arc::new(RegistryBytesSource(Arc::from(&b"data"[..]))),
                    UploadTransactionLease::acquire(foreign).await,
                    Instant::now() + Duration::from_secs(10),
                    cancel,
                    4,
                )
                .await
                .unwrap_err();
            assert_eq!(error.kind, PublishFailureKind::IdentityConflict);
            assert!(upload(&fixture.target, b"data", 0).await.is_err());
            assert!(fixture.state.lock().await.observed_auth.is_empty());
            fixture.state.lock().await.minimum_chunk = Some("16".into());
            assert!(upload(&fixture.target, b"larger-than-one-chunk", 4)
                .await
                .is_err());
            assert_eq!(fixture.state.lock().await.aborts, 1);
        }

        struct WaitingSource(Arc<AtomicBool>);

        impl ReplayableByteSource for WaitingSource {
            fn length(&self) -> u64 {
                4
            }
            fn read_at<'a>(
                &'a self,
                _: u64,
                _: usize,
            ) -> Pin<Box<dyn Future<Output = Result<Vec<u8>, ObjectStoreError>> + Send + 'a>>
            {
                Box::pin(async move {
                    self.0.store(true, Ordering::Release);
                    std::future::pending().await
                })
            }
        }

        #[tokio::test]
        async fn cancelled_pending_source_is_bounded_and_aborts_its_session() {
            let fixture = write_fixture(Arc::default(), false);
            let observed = Arc::new(AtomicBool::new(false));
            let source = Arc::new(WaitingSource(Arc::clone(&observed)));
            let target = Arc::clone(&fixture.target);
            let (sender, cancel) = cancellation();
            let lease = UploadTransactionLease::acquire(target.fence()).await;
            let task = tokio::spawn(async move {
                target
                    .ensure_stream_verified(
                        &ImmutableObjectIdentity {
                            object_key: "pending".into(),
                            sha256: digest(b"data"),
                            length: 4,
                        },
                        source,
                        lease,
                        Instant::now() + Duration::from_secs(5),
                        cancel,
                        4,
                    )
                    .await
            });
            tokio::time::timeout(Duration::from_secs(2), async {
                while !observed.load(Ordering::Acquire) {
                    tokio::task::yield_now().await;
                }
            })
            .await
            .unwrap();
            sender.send(true).unwrap();
            let error = tokio::time::timeout(Duration::from_secs(1), task)
                .await
                .unwrap()
                .unwrap()
                .unwrap_err();
            assert!(error.retryable());
            assert_eq!(fixture.state.lock().await.aborts, 1);
            assert!(fixture.state.lock().await.uploads.is_empty());
        }

        #[tokio::test]
        async fn upload_locations_preserve_queries_without_forwarding_credentials() {
            let shared = Arc::new(Mutex::new(WriteState::default()));
            let cdn = write_fixture(Arc::clone(&shared), false);
            shared.lock().await.upload_origin = Some(
                cdn.target
                    .client
                    .origin
                    .as_str()
                    .trim_end_matches('/')
                    .to_owned(),
            );
            let fixture = write_fixture(Arc::clone(&shared), true);
            upload(&fixture.target, b"credential-isolation", 5)
                .await
                .unwrap();
            let state = shared.lock().await;
            let cdn_origin = cdn.target.client.origin.as_str().trim_end_matches('/');
            assert!(state
                .observed_auth
                .iter()
                .any(|(origin, auth)| origin == cdn_origin && !auth));
            assert!(state
                .observed_auth
                .iter()
                .filter(|(origin, _)| origin == cdn_origin)
                .all(|(_, auth)| !auth));
            assert!(!format!("{:?}", fixture.target.client).contains("not-a-real-password"));
        }

        #[tokio::test]
        async fn redirected_upload_resolves_relative_locations_against_effective_origin() {
            let shared = Arc::new(Mutex::new(WriteState {
                relative_locations: true,
                ..Default::default()
            }));
            let cdn = write_fixture(Arc::clone(&shared), false);
            let cdn_origin = cdn
                .target
                .client
                .origin
                .as_str()
                .trim_end_matches('/')
                .to_owned();
            shared.lock().await.post_redirect = Some(cdn_origin.clone());
            let fixture = write_fixture(Arc::clone(&shared), true);
            upload(&fixture.target, b"relative-location-after-redirect", 5)
                .await
                .unwrap();
            assert!(shared
                .lock()
                .await
                .observed_auth
                .iter()
                .filter(|(origin, _)| origin == &cdn_origin)
                .all(|(_, auth)| !auth));
        }

        #[tokio::test]
        async fn pull_and_push_bearer_scopes_have_independent_cached_tokens() {
            let shared = Arc::new(Mutex::new(WriteState {
                require_bearer: true,
                ..Default::default()
            }));
            let fixture = write_fixture(shared, false);
            upload(&fixture.target, b"authenticated-data", 5)
                .await
                .unwrap();
            upload(&fixture.target, b"authenticated-data", 5)
                .await
                .unwrap();
            let state = fixture.state.lock().await;
            assert_eq!(state.token_scopes.len(), 2);
            assert!(state
                .token_scopes
                .contains(&"repository:team/test:pull".into()));
            assert!(state
                .token_scopes
                .contains(&"repository:team/test:pull,push".into()));
        }

        #[tokio::test]
        async fn manifests_are_verified_before_publishing_the_tag() {
            let fixture = write_fixture(Arc::default(), false);
            let media_type = "application/vnd.oci.image.index.v1+json";
            let payload = serde_json::to_vec(
                &serde_json::json!({"schemaVersion":2,"mediaType":media_type,"manifests":[]}),
            )
            .unwrap();
            let expected = digest(&payload);
            let lease = UploadTransactionLease::acquire(fixture.target.fence()).await;
            let (_sender, cancel) = cancellation();
            let deadline = Instant::now() + Duration::from_secs(10);
            fixture.state.lock().await.ambiguous_manifest = true;
            let immutable = fixture
                .target
                .put_manifest(&expected, media_type, &payload, &lease, deadline, &cancel)
                .await
                .unwrap();
            let tagged = fixture
                .target
                .put_manifest("latest", media_type, &payload, &lease, deadline, &cancel)
                .await
                .unwrap();
            assert_eq!(immutable.digest, tagged.digest);
            assert_eq!(fixture.state.lock().await.manifests["latest"].1, payload);
            let wrong = digest(b"other");
            assert!(fixture
                .target
                .put_manifest(&wrong, media_type, &payload, &lease, deadline, &cancel)
                .await
                .is_err());
            assert!(fixture
                .target
                .put_manifest(
                    "bad",
                    "application/octet-stream",
                    &payload,
                    &lease,
                    deadline,
                    &cancel
                )
                .await
                .is_err());
            assert!(!fixture.state.lock().await.manifests.contains_key("bad"));
        }
    }
}
