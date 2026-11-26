//! Neutral range plans and source coordination below snapshot formats.
//!
//! Logical peer DATA and encoded provider bytes have separate coordinates and
//! explicit representations. Only the format that produced a plan may decode it.

pub use crate::snapshot::transport::interfaces::read::{
    RangeReadError, RangeReadFailureClass, RangeReadFallbackClass,
};
use crate::RuntimeIdentity;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::Mutex;

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct P2PSource {
    pub snapshot_id: String,
    pub spec_checksum: String,
    pub worker_ip: std::net::IpAddr,
}

#[derive(Debug, Clone, Copy)]
pub struct RangeReadRequest<'a> {
    /// An encoded provider range compiled by the format. Peer requests retain
    /// the logical offset/length below and never reuse these coordinates.
    pub physical: Option<crate::snapshot::transport::PhysicalRangeRequest<'a>>,
    /// Pinned physical source authority. None selects the configured object
    /// transport; Some selects exactly this Registry authority.
    pub authority: Option<&'a str>,
    pub runtime: &'a RuntimeIdentity,
    pub kind: RangeDataKind,
    pub object_key: &'a str,
    pub object_sha256: &'a str,
    /// Exact checksum of the requested encoded range from trusted snapshot
    /// metadata. P2P must validate against this value, not a peer-supplied
    /// checksum alone.
    pub expected_range_sha256: &'a str,
    pub offset: u64,
    pub length: u64,
    pub object_version: Option<&'a str>,
    pub preferred_p2p_source: Option<&'a P2PSource>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RangeDataKind {
    Memory,
    Block,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RangeReadTransport {
    Provider,
    P2P,
}

/// Format-level authentication of decoded DATA. Whole-object SHA validation
/// remains distinct from a source-bound partial container CRC check.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FormatIntegrity {
    Unverified,
    SourceBoundBlockCrc,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RangeReadResult {
    pub representation: RangeRepresentation,
    pub payload: Vec<u8>,
    pub transport: RangeReadTransport,
    /// Produced by the format adapter after source binding and block checks.
    pub format_integrity: FormatIntegrity,
    /// The payload is physical provider bytes read from the local byte cache.
    /// Formats still decode and authenticate it exactly as a provider response.
    pub physical_cache_hit: bool,
}

impl RangeReadResult {
    pub fn provider(payload: Vec<u8>) -> Self {
        Self {
            payload,
            representation: RangeRepresentation::DecodedData,
            transport: RangeReadTransport::Provider,
            format_integrity: FormatIntegrity::Unverified,
            physical_cache_hit: false,
        }
    }

    pub fn encoded(payload: Vec<u8>) -> Self {
        let mut result = Self::provider(payload);
        result.representation = RangeRepresentation::EncodedObject;
        result
    }

    pub fn p2p(payload: Vec<u8>) -> Self {
        Self {
            payload,
            representation: RangeRepresentation::DecodedData,
            transport: RangeReadTransport::P2P,
            format_integrity: FormatIntegrity::Unverified,
            physical_cache_hit: false,
        }
    }
}

/// The returned bytes explicitly name either decoded DATA or a provider object.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RangeRepresentation {
    DecodedData,
    EncodedObject,
}

use async_trait::async_trait;

#[async_trait]
pub trait RangeReader: Send + Sync {
    /// Execute an immutable object byte plan without a VM identity, such as
    /// Registry metadata import. The authority comes from the pinned source.
    async fn read_object_range(
        &self,
        _authority: &str,
        _request: PhysicalRangeRequest<'_>,
    ) -> Result<Vec<u8>, RangeReadError> {
        Err(RangeReadError::failure(
            RangeReadFailureClass::Permanent,
            "object range reads are not configured for this reader",
        ))
    }

    async fn invalidate_object_range(
        &self,
        _authority: &str,
        _request: PhysicalRangeRequest<'_>,
    ) -> Result<bool, RangeReadError> {
        Ok(false)
    }

    async fn read_object_range_uncached(
        &self,
        authority: &str,
        request: PhysicalRangeRequest<'_>,
    ) -> Result<Vec<u8>, RangeReadError> {
        self.read_object_range(authority, request).await
    }

    async fn read_metadata(
        &self,
        store: &dyn crate::snapshot::transport::ImmutableObjectStore,
        key: &str,
        version: Option<&str>,
        max_bytes: u64,
    ) -> Result<
        Option<crate::snapshot::transport::RemoteObject>,
        crate::snapshot::transport::BoundedGetError,
    > {
        store.get_with_metadata(key, version, max_bytes).await
    }
    async fn head_metadata(
        &self,
        store: &dyn crate::snapshot::transport::ImmutableObjectStore,
        key: &str,
    ) -> Result<
        Option<crate::snapshot::transport::RemoteObjectMetadata>,
        crate::snapshot::transport::ObjectStoreError,
    > {
        store.head(key).await
    }

    /// Optional mutable image hints have their own bounded transport contract.
    async fn read_optional_metadata(
        &self,
        store: &dyn crate::snapshot::transport::MetadataObjectStore,
        key: &str,
        max: u64,
    ) -> Result<
        Option<crate::snapshot::transport::RemoteObject>,
        crate::snapshot::transport::BoundedGetError,
    > {
        store.get_bounded(key, max).await
    }

    /// Whether this runtime/source is latched to provider-only reads. Called
    /// before widening a P2P-hinted range, which has no aggregate peer checksum.
    /// A true result must remain true until all runtime reads have drained.
    async fn allows_provider_coalescing(
        &self,
        _runtime: &RuntimeIdentity,
        _source: &P2PSource,
    ) -> bool {
        false
    }

    async fn read_range(
        &self,
        request: RangeReadRequest<'_>,
    ) -> Result<RangeReadResult, RangeReadError>;

    /// Invalidate the physical plan after format authentication fails. True
    /// means this source has a repairable cache, even if a concurrent caller
    /// already removed the entry. A format may then retry once without lookup.
    async fn invalidate_range(
        &self,
        _request: RangeReadRequest<'_>,
    ) -> Result<bool, RangeReadError> {
        Ok(false)
    }

    async fn read_range_uncached(
        &self,
        request: RangeReadRequest<'_>,
    ) -> Result<RangeReadResult, RangeReadError> {
        self.read_range(request).await
    }

    async fn read_range_with_timeout(
        &self,
        request: RangeReadRequest<'_>,
        provider_timeout: Duration,
    ) -> Result<RangeReadResult, RangeReadError> {
        with_provider_timeout(provider_timeout, self.read_range(request)).await
    }

    async fn read_range_uncached_with_timeout(
        &self,
        request: RangeReadRequest<'_>,
        provider_timeout: Duration,
    ) -> Result<RangeReadResult, RangeReadError> {
        with_provider_timeout(provider_timeout, self.read_range_uncached(request)).await
    }

    async fn forget_runtime(&self, _runtime: &RuntimeIdentity) {}
}

use crate::snapshot::transport::interfaces::physical_cache::{
    PhysicalCacheKey, PhysicalRangeCache,
};
use crate::snapshot::transport::p2p::P2PClient;
use crate::snapshot::transport::registry::{
    RegistryClient, RegistryError, RegistryLayerRef, RegistryManifest,
};
use crate::snapshot::transport::{
    DataPackWriteTarget, ImmutableObjectIdentity, IoCancellation, ObjectStoreError,
    PhysicalRangeReader, PhysicalRangeRequest, ReplayableByteSource, SnapshotMetadataWriteTarget,
    UploadTransactionLease, VerifiedWriteReceipt,
};

/// Explicit read-source policy. A write target is never selected by this policy.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RangeSourcePolicy {
    ProviderOnly,
    PeerOnly,
    PeerThenProvider,
}

/// A single source coordinator. It never interprets format indexes or codecs.
pub struct RangeIO {
    provider: Option<Arc<dyn PhysicalRangeReader>>,
    peer: Option<Arc<P2PClient>>,
    metrics: Option<Arc<crate::observability::metrics::SnapdMetrics>>,
    configured_registry_clients: HashMap<String, Arc<RegistryClient>>,
    registry_clients: Mutex<HashMap<String, Arc<RegistryClient>>>,
    physical_cache: Option<Arc<dyn PhysicalRangeCache>>,
}

impl std::fmt::Debug for RangeIO {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("RangeIO")
            .field("provider_configured", &self.provider.is_some())
            .field("peer_configured", &self.peer.is_some())
            .field("physical_cache_configured", &self.physical_cache.is_some())
            .finish_non_exhaustive()
    }
}

impl RangeIO {
    pub(crate) fn with_registry_clients(
        mut self: Arc<Self>,
        clients: Vec<Arc<RegistryClient>>,
    ) -> Arc<Self> {
        Arc::get_mut(&mut self)
            .expect("Registry clients are configured before sharing RangeIO")
            .configured_registry_clients
            .extend(
                clients
                    .into_iter()
                    .map(|client| (client.registry().to_owned(), client)),
            );
        self
    }

    pub fn new(provider: Arc<dyn PhysicalRangeReader>, peer: Option<Arc<P2PClient>>) -> Arc<Self> {
        Self::new_with_cache(provider, peer, None)
    }

    pub fn new_with_cache(
        provider: Arc<dyn PhysicalRangeReader>,
        peer: Option<Arc<P2PClient>>,
        physical_cache: Option<Arc<dyn PhysicalRangeCache>>,
    ) -> Arc<Self> {
        Arc::new(Self {
            provider: Some(provider),
            metrics: peer.as_ref().map(|peer| peer.metrics()),
            peer,
            configured_registry_clients: HashMap::new(),
            registry_clients: Mutex::new(HashMap::new()),
            physical_cache,
        })
    }

    /// Publication-only assembly has no implicit read source.
    pub fn for_fixed_targets() -> Arc<Self> {
        Arc::new(Self {
            provider: None,
            peer: None,
            metrics: None,
            configured_registry_clients: HashMap::new(),
            registry_clients: Mutex::new(HashMap::new()),
            physical_cache: None,
        })
    }

    /// Bind configured OCI metadata and physical bytes to one authority.
    pub(crate) fn for_registry_client_with_cache(
        client: Arc<RegistryClient>,
        physical_cache: Option<Arc<dyn PhysicalRangeCache>>,
    ) -> Arc<Self> {
        let registry = client.registry().to_owned();
        Arc::new(Self {
            provider: Some(client.clone()),
            peer: None,
            metrics: None,
            configured_registry_clients: HashMap::from([(registry, client)]),
            registry_clients: Mutex::new(HashMap::new()),
            physical_cache,
        })
    }

    pub(crate) fn physical_cache(&self) -> Option<Arc<dyn PhysicalRangeCache>> {
        self.physical_cache.clone()
    }

    async fn registry_client(&self, registry: &str) -> Result<Arc<RegistryClient>, RegistryError> {
        if let Some(client) = self.configured_registry_clients.get(registry) {
            return Ok(Arc::clone(client));
        }
        // Anonymous discovery is bounded independently of operator-owned
        // clients so cache churn cannot discard configured credentials.
        let mut clients = self.registry_clients.lock().await;
        if let Some(client) = clients.get(registry) {
            return Ok(Arc::clone(client));
        }
        let client = RegistryClient::new(registry)?;
        if clients.len() >= 64 {
            clients.clear();
        }
        clients.insert(registry.to_owned(), Arc::clone(&client));
        Ok(client)
    }

    pub(crate) async fn registry_manifest(
        &self,
        registry: &str,
        repository: &str,
        digest: &str,
    ) -> Result<RegistryManifest, RegistryError> {
        self.registry_client(registry)
            .await?
            .manifest(repository, digest)
            .await
    }

    pub(crate) async fn registry_blob_metadata(
        &self,
        registry: &str,
        repository: &str,
        blob: &RegistryLayerRef,
    ) -> Result<Vec<u8>, RegistryError> {
        self.registry_client(registry)
            .await?
            .read_blob_metadata(repository, blob)
            .await
    }

    async fn physical_source(
        &self,
        authority: Option<&str>,
    ) -> Result<Arc<dyn PhysicalRangeReader>, RangeReadError> {
        if let Some(authority) = authority {
            if let Some(provider) = self
                .provider
                .as_ref()
                .filter(|provider| provider.source_id() == authority)
            {
                return Ok(Arc::clone(provider));
            }
            let client = self.registry_client(authority).await.map_err(|error| {
                RangeReadError::failure(RangeReadFailureClass::Permanent, error.to_string())
            })?;
            return Ok(client);
        }
        self.provider.as_ref().cloned().ok_or_else(|| {
            RangeReadError::failure(
                RangeReadFailureClass::Permanent,
                "range I/O read source is not configured",
            )
        })
    }

    /// One physical-cache boundary for S3 ranges and native OCI blob ranges.
    /// The selected transport, rather than caller-supplied authority text,
    /// supplies the source identity. Format decoding never happens here.
    async fn read_physical_cached(
        &self,
        provider: &dyn PhysicalRangeReader,
        request: PhysicalRangeRequest<'_>,
        bypass_lookup: bool,
        provider_timeout: Option<Duration>,
    ) -> Result<(Vec<u8>, bool), RangeReadError> {
        let cache = self.physical_cache();
        let key = cache
            .as_ref()
            .and_then(|_| PhysicalCacheKey::from_request(provider.source_id(), request).ok());
        if let (Some(cache), Some(key)) = (cache.as_ref(), key.as_ref()) {
            if !bypass_lookup {
                match cache.read(key).await {
                    Ok(Some(payload)) if payload.len() as u64 == request.length => {
                        return Ok((payload, true));
                    }
                    Ok(None) => {}
                    // The backend authenticates its stored byte range. A bad
                    // entry, short response or optional disk error is a miss.
                    Ok(Some(_)) | Err(_) => {
                        let _ = cache.invalidate(key).await;
                    }
                }
            }
        }
        let payload = match provider_timeout {
            Some(timeout) => {
                with_provider_timeout(timeout, provider.read_physical_range(request)).await?
            }
            None => provider.read_physical_range(request).await?,
        };
        if payload.len() as u64 != request.length {
            return Err(RangeReadError::failure(
                RangeReadFailureClass::Integrity,
                "physical source returned an incomplete range",
            ));
        }
        if let (Some(cache), Some(key)) = (cache.as_ref(), key.as_ref()) {
            // Cache bytes remain physical and untrusted by the format until
            // its ordinary CRC/SHA checks pass. A format failure evicts this
            // entry and can make exactly one uncached repair attempt.
            let payload = Arc::new(payload);
            let _ = cache.write(key, Arc::clone(&payload)).await;
            let payload = Arc::try_unwrap(payload).map_err(|_| {
                RangeReadError::failure(
                    RangeReadFailureClass::Permanent,
                    "completed physical cache writer retained its DATA buffer",
                )
            })?;
            return Ok((payload, false));
        }
        Ok((payload, false))
    }

    async fn invalidate_physical_for(
        &self,
        provider: &dyn PhysicalRangeReader,
        request: PhysicalRangeRequest<'_>,
    ) -> Result<bool, RangeReadError> {
        let Some(cache) = self.physical_cache() else {
            return Ok(false);
        };
        let Ok(key) = PhysicalCacheKey::from_request(provider.source_id(), request) else {
            return Ok(false);
        };
        // Invalidation failures must not prevent the bounded provider repair
        // from returning correct data. It explicitly bypasses cache lookup.
        let _ = cache.invalidate(&key).await;
        Ok(true)
    }

    fn physical_request(request: RangeReadRequest<'_>) -> PhysicalRangeRequest<'_> {
        request.physical.unwrap_or(PhysicalRangeRequest {
            namespace: None,
            object_size: None,
            object_key: request.object_key,
            object_sha256: request.object_sha256,
            object_version: request.object_version,
            offset: request.offset,
            length: request.length,
        })
    }

    async fn provider_read_with_cache_policy(
        &self,
        request: RangeReadRequest<'_>,
        bypass_lookup: bool,
        provider_timeout: Option<Duration>,
    ) -> Result<RangeReadResult, RangeReadError> {
        if request.length == 0 || request.offset.checked_add(request.length).is_none() {
            return Err(RangeReadError::InvalidRange);
        }
        let provider = self.physical_source(request.authority).await?;
        let physical = Self::physical_request(request);
        let (bytes, physical_cache_hit) = self
            .read_physical_cached(provider.as_ref(), physical, bypass_lookup, provider_timeout)
            .await?;
        let mut result = RangeReadResult::provider(bytes);
        result.physical_cache_hit = physical_cache_hit;
        if request.physical.is_some() {
            result.representation = RangeRepresentation::EncodedObject;
        }
        Ok(result)
    }

    pub async fn read_with_policy(
        &self,
        request: RangeReadRequest<'_>,
        policy: RangeSourcePolicy,
    ) -> Result<RangeReadResult, RangeReadError> {
        self.read_with_policy_and_timeout(request, policy, None)
            .await
    }

    async fn read_with_policy_and_timeout(
        &self,
        request: RangeReadRequest<'_>,
        policy: RangeSourcePolicy,
        provider_timeout: Option<Duration>,
    ) -> Result<RangeReadResult, RangeReadError> {
        if policy == RangeSourcePolicy::ProviderOnly {
            return self
                .provider_read_with_cache_policy(request, false, provider_timeout)
                .await;
        }
        let failure = if request.preferred_p2p_source.is_some() {
            match &self.peer {
                Some(peer) => match peer.read_decoded(request).await {
                    Ok(payload) => return Ok(RangeReadResult::p2p(payload)),
                    Err(error) => (error.class, error.message),
                },
                None => (
                    RangeReadFallbackClass::Unavailable,
                    "peer transport is not configured".into(),
                ),
            }
        } else {
            if policy == RangeSourcePolicy::PeerThenProvider {
                if let Some(metrics) = &self.metrics {
                    metrics
                        .add_p2p_fallback(crate::observability::metrics::P2PFallbackReason::NoHint);
                }
                return self
                    .provider_read_with_cache_policy(request, false, provider_timeout)
                    .await;
            }
            (
                RangeReadFallbackClass::Unavailable,
                "no peer source was selected".into(),
            )
        };
        if policy == RangeSourcePolicy::PeerOnly {
            return Err(match failure.0 {
                RangeReadFallbackClass::Integrity => {
                    RangeReadError::failure(RangeReadFailureClass::Integrity, failure.1)
                }
                RangeReadFallbackClass::NotFound => RangeReadError::NotFound,
                RangeReadFallbackClass::Unavailable
                | RangeReadFallbackClass::Timeout
                | RangeReadFallbackClass::Transport => {
                    RangeReadError::failure(RangeReadFailureClass::Transient, failure.1)
                }
            });
        }
        if let Some(metrics) = &self.metrics {
            use crate::observability::metrics::P2PFallbackReason;
            metrics.add_p2p_fallback(match failure.0 {
                RangeReadFallbackClass::Unavailable => P2PFallbackReason::ProbeMiss,
                RangeReadFallbackClass::Timeout => P2PFallbackReason::Timeout,
                RangeReadFallbackClass::Transport => P2PFallbackReason::Transport,
                RangeReadFallbackClass::NotFound => P2PFallbackReason::RangeMiss,
                RangeReadFallbackClass::Integrity => P2PFallbackReason::Integrity,
            });
        }
        self.provider_read_with_cache_policy(request, false, provider_timeout)
            .await
            .map_err(|error| RangeReadError::fallback(failure.0, failure.1, error))
    }
}

#[async_trait]
impl RangeReader for RangeIO {
    async fn read_object_range(
        &self,
        authority: &str,
        request: PhysicalRangeRequest<'_>,
    ) -> Result<Vec<u8>, RangeReadError> {
        let provider = self.physical_source(Some(authority)).await?;
        self.read_physical_cached(provider.as_ref(), request, false, None)
            .await
            .map(|(payload, _)| payload)
    }

    async fn invalidate_object_range(
        &self,
        authority: &str,
        request: PhysicalRangeRequest<'_>,
    ) -> Result<bool, RangeReadError> {
        let provider = self.physical_source(Some(authority)).await?;
        self.invalidate_physical_for(provider.as_ref(), request)
            .await
    }

    async fn read_object_range_uncached(
        &self,
        authority: &str,
        request: PhysicalRangeRequest<'_>,
    ) -> Result<Vec<u8>, RangeReadError> {
        let provider = self.physical_source(Some(authority)).await?;
        self.read_physical_cached(provider.as_ref(), request, true, None)
            .await
            .map(|(payload, _)| payload)
    }

    async fn allows_provider_coalescing(
        &self,
        runtime: &RuntimeIdentity,
        source: &P2PSource,
    ) -> bool {
        match &self.peer {
            Some(peer) => peer.allows_provider_coalescing(runtime, source).await,
            None => true,
        }
    }
    async fn read_range(
        &self,
        request: RangeReadRequest<'_>,
    ) -> Result<RangeReadResult, RangeReadError> {
        self.read_with_policy(request, RangeSourcePolicy::PeerThenProvider)
            .await
    }
    async fn invalidate_range(
        &self,
        request: RangeReadRequest<'_>,
    ) -> Result<bool, RangeReadError> {
        let provider = self.physical_source(request.authority).await?;
        self.invalidate_physical_for(provider.as_ref(), Self::physical_request(request))
            .await
    }
    async fn read_range_uncached(
        &self,
        request: RangeReadRequest<'_>,
    ) -> Result<RangeReadResult, RangeReadError> {
        // Repair reads are provider-only. Decoded P2P data never participates
        // in a physical cache lookup or manufactures encoded cache bytes.
        self.provider_read_with_cache_policy(request, true, None)
            .await
    }
    async fn read_range_with_timeout(
        &self,
        request: RangeReadRequest<'_>,
        provider_timeout: Duration,
    ) -> Result<RangeReadResult, RangeReadError> {
        self.read_with_policy_and_timeout(
            request,
            RangeSourcePolicy::PeerThenProvider,
            Some(provider_timeout),
        )
        .await
    }
    async fn read_range_uncached_with_timeout(
        &self,
        request: RangeReadRequest<'_>,
        provider_timeout: Duration,
    ) -> Result<RangeReadResult, RangeReadError> {
        self.provider_read_with_cache_policy(request, true, Some(provider_timeout))
            .await
    }
    async fn forget_runtime(&self, runtime: &RuntimeIdentity) {
        if let Some(peer) = &self.peer {
            peer.forget_runtime(runtime).await;
        }
    }
}

/// Fixed-domain immutable writes. Reader fallback policy never changes a target.
#[async_trait]
pub trait RangeWriter: std::fmt::Debug + Send + Sync {
    /// Validation reads remain in the admitted write domain and retain its lease.
    async fn head_write_target(
        &self,
        target: &SnapshotMetadataWriteTarget,
        key: &str,
        lease: &UploadTransactionLease,
    ) -> Result<Option<crate::snapshot::transport::RemoteObjectMetadata>, ObjectStoreError> {
        target.head(key, lease).await
    }
    async fn read_write_target(
        &self,
        target: &SnapshotMetadataWriteTarget,
        key: &str,
        lease: &UploadTransactionLease,
    ) -> Result<Vec<u8>, ObjectStoreError> {
        target.get(key, lease).await
    }
    /// Replaceable hints are distinct from content-addressed immutable objects.
    async fn replace_optional_metadata(
        &self,
        store: &dyn crate::snapshot::transport::MetadataObjectStore,
        key: &str,
        payload: &[u8],
        sha: &str,
    ) -> Result<(), ObjectStoreError> {
        store.replace(key, payload, sha).await
    }

    async fn ensure_stream_verified(
        &self,
        target: &DataPackWriteTarget,
        identity: &ImmutableObjectIdentity,
        source: Arc<dyn ReplayableByteSource>,
        lease: UploadTransactionLease,
        deadline: Instant,
        cancellation: IoCancellation,
        chunk_bytes: usize,
    ) -> Result<VerifiedWriteReceipt, ObjectStoreError>;
    async fn ensure_metadata_verified(
        &self,
        target: &SnapshotMetadataWriteTarget,
        key: &str,
        payload: &[u8],
        sha: &str,
        lease: &UploadTransactionLease,
    ) -> Result<(), ObjectStoreError>;
}

#[async_trait]
impl RangeWriter for RangeIO {
    async fn ensure_stream_verified(
        &self,
        target: &DataPackWriteTarget,
        identity: &ImmutableObjectIdentity,
        source: Arc<dyn ReplayableByteSource>,
        lease: UploadTransactionLease,
        deadline: Instant,
        cancellation: IoCancellation,
        chunk_bytes: usize,
    ) -> Result<VerifiedWriteReceipt, ObjectStoreError> {
        target
            .writer
            .ensure_stream_verified(identity, source, lease, deadline, cancellation, chunk_bytes)
            .await
    }
    async fn ensure_metadata_verified(
        &self,
        target: &SnapshotMetadataWriteTarget,
        key: &str,
        payload: &[u8],
        sha: &str,
        lease: &UploadTransactionLease,
    ) -> Result<(), ObjectStoreError> {
        target.ensure_verified(key, payload, sha, lease).await
    }
}

pub(crate) async fn with_provider_timeout<T>(
    timeout: std::time::Duration,
    operation: impl std::future::Future<Output = Result<T, RangeReadError>>,
) -> Result<T, RangeReadError> {
    tokio::time::timeout(timeout, operation)
        .await
        .map_err(|_| {
            RangeReadError::failure(
                RangeReadFailureClass::Transient,
                "immutable range provider timed out",
            )
        })?
}

#[cfg(test)]
mod registry_client_tests {
    use super::*;
    use base64::Engine;
    use hyper::service::{make_service_fn, service_fn};
    use hyper::{Body, Response};
    use std::convert::Infallible;

    struct PrivateRegistry {
        client: Arc<RegistryClient>,
        blob: crate::snapshot::transport::BlobRef,
        server: tokio::task::JoinHandle<()>,
    }

    impl Drop for PrivateRegistry {
        fn drop(&mut self) {
            self.server.abort();
        }
    }

    fn private_registry(username: &str, password: &str, payload: &[u8]) -> PrivateRegistry {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let authority = listener.local_addr().unwrap().to_string();
        let client = RegistryClient::new_configured(
            &authority,
            true,
            Some(username.into()),
            Some(password.into()),
        )
        .unwrap();
        let digest = crate::snapshot::metadata::sha256_identity(payload);
        let blob = crate::snapshot::transport::BlobRef {
            digest: digest.clone(),
            size: payload.len() as u64,
        };
        let payload = payload.to_vec();
        let authorization = format!(
            "Basic {}",
            base64::engine::general_purpose::STANDARD.encode(format!("{username}:{password}"))
        );
        let service = make_service_fn(move |_| {
            let payload = payload.clone();
            let digest = digest.clone();
            let authorization = authorization.clone();
            async move {
                Ok::<_, Infallible>(service_fn(move |request: hyper::Request<Body>| {
                    let authorized = request
                        .headers()
                        .get("authorization")
                        .and_then(|value| value.to_str().ok())
                        == Some(authorization.as_str());
                    let response = if !authorized {
                        Response::builder()
                            .status(401)
                            .header("www-authenticate", "Basic realm=\"private-fixture\"")
                            .body(Body::empty())
                            .unwrap()
                    } else {
                        assert_eq!(
                            request.uri().path(),
                            format!("/v2/team/snapshots/blobs/{digest}")
                        );
                        let mut response =
                            Response::builder().header("docker-content-digest", digest.as_str());
                        let bytes = if let Some(range) = request.headers().get("range") {
                            let (start, end) = range
                                .to_str()
                                .unwrap()
                                .strip_prefix("bytes=")
                                .unwrap()
                                .split_once('-')
                                .unwrap();
                            let start: usize = start.parse().unwrap();
                            let end: usize = end.parse().unwrap();
                            assert!(start <= end && end < payload.len());
                            response = response.status(206).header(
                                "content-range",
                                format!("bytes {start}-{end}/{}", payload.len()),
                            );
                            payload[start..=end].to_vec()
                        } else {
                            payload.clone()
                        };
                        response
                            .header("content-length", bytes.len())
                            .body(Body::from(bytes))
                            .unwrap()
                    };
                    async move { Ok::<_, Infallible>(response) }
                }))
            }
        });
        let server = tokio::spawn(async move {
            hyper::Server::from_tcp(listener)
                .unwrap()
                .serve(service)
                .await
                .unwrap();
        });
        PrivateRegistry {
            client,
            blob,
            server,
        }
    }

    #[tokio::test]
    async fn configured_upload_identity_survives_anonymous_registry_discovery() {
        let fixture = private_registry("snapshot-writer", "fixture-secret", &[1, 2, 3, 4]);
        let ranges =
            RangeIO::for_fixed_targets().with_registry_clients(vec![Arc::clone(&fixture.client)]);
        for pass in 0..2 {
            if pass == 1 {
                for index in 0..70 {
                    // Discovery constructs clients without network requests.
                    ranges
                        .registry_client(&format!("anonymous-{index}.example"))
                        .await
                        .unwrap();
                }
                assert!(Arc::ptr_eq(
                    &fixture.client,
                    &ranges
                        .registry_client(fixture.client.registry())
                        .await
                        .unwrap()
                ));
                assert!(ranges.registry_clients.lock().await.len() <= 64);
            }
            assert_eq!(
                ranges
                    .registry_blob_metadata(
                        fixture.client.registry(),
                        "team/snapshots",
                        &fixture.blob
                    )
                    .await
                    .unwrap(),
                [1, 2, 3, 4]
            );
            assert_eq!(
                ranges
                    .read_object_range(
                        fixture.client.registry(),
                        PhysicalRangeRequest {
                            namespace: Some("team/snapshots"),
                            object_key: &fixture.blob.digest,
                            object_sha256: &fixture.blob.digest,
                            object_size: Some(fixture.blob.size),
                            object_version: None,
                            offset: 1,
                            length: 2,
                        },
                    )
                    .await
                    .unwrap(),
                [2, 3]
            );
        }
    }
}

#[cfg(test)]
mod physical_cache_tests {
    use super::*;
    use std::io;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    use std::sync::Mutex as StdMutex;

    #[derive(Debug, Default)]
    struct Cache {
        entries: StdMutex<HashMap<PhysicalCacheKey, Vec<u8>>>,
        reads: AtomicUsize,
        writes: AtomicUsize,
        invalidations: AtomicUsize,
        fail: AtomicBool,
        slow_read: AtomicBool,
        write_pointer: AtomicUsize,
    }

    #[async_trait]
    impl PhysicalRangeCache for Cache {
        async fn read(&self, key: &PhysicalCacheKey) -> io::Result<Option<Vec<u8>>> {
            self.reads.fetch_add(1, Ordering::SeqCst);
            if self.slow_read.load(Ordering::SeqCst) {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
            if self.fail.load(Ordering::SeqCst) {
                return Err(io::Error::other("cache disk unavailable"));
            }
            Ok(self.entries.lock().unwrap().get(key).cloned())
        }

        async fn write(&self, key: &PhysicalCacheKey, payload: Arc<Vec<u8>>) -> io::Result<()> {
            self.writes.fetch_add(1, Ordering::SeqCst);
            self.write_pointer
                .store(payload.as_ptr() as usize, Ordering::SeqCst);
            if self.fail.load(Ordering::SeqCst) {
                return Err(io::Error::other("cache disk unavailable"));
            }
            self.entries
                .lock()
                .unwrap()
                .insert(key.clone(), payload.as_ref().clone());
            Ok(())
        }

        async fn invalidate(&self, key: &PhysicalCacheKey) -> io::Result<()> {
            self.invalidations.fetch_add(1, Ordering::SeqCst);
            self.entries.lock().unwrap().remove(key);
            if self.fail.load(Ordering::SeqCst) {
                return Err(io::Error::other("cache disk unavailable"));
            }
            Ok(())
        }
    }

    struct Source {
        id: &'static str,
        payload: Vec<u8>,
        calls: AtomicUsize,
        returned_pointer: AtomicUsize,
    }

    #[async_trait]
    impl PhysicalRangeReader for Source {
        fn source_id(&self) -> &str {
            self.id
        }

        async fn read_physical_range(
            &self,
            request: PhysicalRangeRequest<'_>,
        ) -> Result<Vec<u8>, RangeReadError> {
            request.validate()?;
            self.calls.fetch_add(1, Ordering::SeqCst);
            let payload = self.payload.clone();
            self.returned_pointer
                .store(payload.as_ptr() as usize, Ordering::SeqCst);
            Ok(payload)
        }
    }

    fn source(id: &'static str, payload: &[u8]) -> Arc<Source> {
        Arc::new(Source {
            id,
            payload: payload.to_vec(),
            calls: AtomicUsize::new(0),
            returned_pointer: AtomicUsize::new(0),
        })
    }

    fn request(runtime: &RuntimeIdentity) -> RangeReadRequest<'_> {
        RangeReadRequest {
            physical: None,
            authority: None,
            runtime,
            kind: RangeDataKind::Block,
            object_key: "immutable.commit",
            object_sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            expected_range_sha256: "",
            offset: 4096,
            length: 4,
            object_version: Some("version-1"),
            preferred_p2p_source: None,
        }
    }

    #[tokio::test]
    async fn logical_and_object_reads_use_the_same_cache_entry() {
        let cache = Arc::new(Cache::default());
        let provider = source("s3://endpoint/bucket", &[1, 2, 3, 4]);
        let ranges = RangeIO::new_with_cache(provider.clone(), None, Some(cache.clone()));
        let runtime = RuntimeIdentity::new("one-physical-entry");
        let request = request(&runtime);
        let first = ranges.read_range(request).await.unwrap();
        assert!(!first.physical_cache_hit);
        assert_eq!(
            first.payload.as_ptr() as usize,
            provider.returned_pointer.load(Ordering::SeqCst)
        );
        assert_eq!(
            first.payload.as_ptr() as usize,
            cache.write_pointer.load(Ordering::SeqCst),
            "cache publication and returned DATA must use the provider's original allocation"
        );
        let physical = RangeIO::physical_request(request);
        assert_eq!(
            ranges
                .read_object_range(provider.id, physical)
                .await
                .unwrap(),
            [1, 2, 3, 4]
        );
        assert!(ranges.read_range(request).await.unwrap().physical_cache_hit);
        assert_eq!(provider.calls.load(Ordering::SeqCst), 1);
        assert_eq!(cache.entries.lock().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn physical_cache_lookup_does_not_consume_the_provider_deadline() {
        let cache = Arc::new(Cache::default());
        let provider = source("s3://endpoint/bucket", &[1, 2, 3, 4]);
        let ranges = RangeIO::new_with_cache(provider.clone(), None, Some(cache.clone()));
        let runtime = RuntimeIdentity::new("cache-deadline");
        let request = request(&runtime);
        ranges.read_range(request).await.unwrap();
        cache.slow_read.store(true, Ordering::SeqCst);
        let read = ranges
            .read_range_with_timeout(request, Duration::from_nanos(1))
            .await
            .unwrap();
        assert!(read.physical_cache_hit);
        assert_eq!(read.payload, [1, 2, 3, 4]);
        assert_eq!(provider.calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn cache_keys_bind_actual_source_namespace_version_and_physical_coordinates() {
        let cache = Arc::new(Cache::default());
        let first_source = source("s3://first/bucket", &[1, 2, 3, 4]);
        let second_source = source("registry.test", &[5, 6, 7, 8]);
        let first = RangeIO::new_with_cache(first_source.clone(), None, Some(cache.clone()));
        let second = RangeIO::new_with_cache(second_source.clone(), None, Some(cache.clone()));
        let runtime = RuntimeIdentity::new("physical-identities");
        let base = request(&runtime);
        assert_eq!(first.read_range(base).await.unwrap().payload, [1, 2, 3, 4]);
        assert_eq!(second.read_range(base).await.unwrap().payload, [5, 6, 7, 8]);
        assert!(first.read_range(base).await.unwrap().physical_cache_hit);
        let physical = RangeIO::physical_request(base);
        for different in [
            PhysicalRangeRequest {
                namespace: Some("different-repository"),
                ..physical
            },
            PhysicalRangeRequest {
                object_version: Some("version-2"),
                ..physical
            },
            PhysicalRangeRequest {
                offset: physical.offset + 1,
                ..physical
            },
        ] {
            first
                .read_range(RangeReadRequest {
                    physical: Some(different),
                    ..base
                })
                .await
                .unwrap();
        }
        assert_eq!(first_source.calls.load(Ordering::SeqCst), 4);
        assert_eq!(second_source.calls.load(Ordering::SeqCst), 1);
        assert_eq!(cache.entries.lock().unwrap().len(), 5);
    }

    #[tokio::test]
    async fn optional_cache_errors_fall_back_and_short_hits_are_repaired_once() {
        let cache = Arc::new(Cache::default());
        let provider = source("s3://endpoint/bucket", &[1, 2, 3, 4]);
        let ranges = RangeIO::new_with_cache(provider.clone(), None, Some(cache.clone()));
        let runtime = RuntimeIdentity::new("cache-errors");
        let request = request(&runtime);
        cache.fail.store(true, Ordering::SeqCst);
        assert_eq!(
            ranges.read_range(request).await.unwrap().payload,
            [1, 2, 3, 4]
        );
        assert_eq!(provider.calls.load(Ordering::SeqCst), 1);
        cache.fail.store(false, Ordering::SeqCst);
        let key = PhysicalCacheKey::from_request(provider.id, RangeIO::physical_request(request))
            .unwrap();
        cache.entries.lock().unwrap().insert(key, vec![9; 3]);
        let repaired = ranges.read_range(request).await.unwrap();
        assert_eq!(repaired.payload, [1, 2, 3, 4]);
        assert!(!repaired.physical_cache_hit);
        assert!(ranges.read_range(request).await.unwrap().physical_cache_hit);
        assert_eq!(provider.calls.load(Ordering::SeqCst), 2);
        assert_eq!(cache.invalidations.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn format_invalidation_and_uncached_repair_do_not_reuse_bad_cached_bytes() {
        let cache = Arc::new(Cache::default());
        let provider = source("registry.test", &[1, 2, 3, 4]);
        let ranges = RangeIO::new_with_cache(provider.clone(), None, Some(cache.clone()));
        let runtime = RuntimeIdentity::new("format-repair");
        let base = request(&runtime);
        let physical = PhysicalRangeRequest {
            namespace: Some("images/rootfs"),
            object_size: Some(8192),
            ..RangeIO::physical_request(base)
        };
        let request = RangeReadRequest {
            authority: Some("registry.test"),
            physical: Some(physical),
            ..base
        };
        let key = PhysicalCacheKey::from_request(provider.id, physical).unwrap();
        cache
            .entries
            .lock()
            .unwrap()
            .insert(key.clone(), vec![9; 4]);
        let bad = ranges.read_range(request).await.unwrap();
        assert!(bad.physical_cache_hit);
        assert_eq!(bad.payload, [9; 4]);
        assert!(ranges.invalidate_range(request).await.unwrap());
        let repaired = ranges.read_range_uncached(request).await.unwrap();
        assert!(!repaired.physical_cache_hit);
        assert_eq!(repaired.payload, [1, 2, 3, 4]);
        assert_eq!(provider.calls.load(Ordering::SeqCst), 1);
        assert_eq!(
            cache.entries.lock().unwrap().get(&key).unwrap(),
            &[1, 2, 3, 4]
        );
        assert!(ranges
            .invalidate_object_range(provider.id, physical)
            .await
            .unwrap());
        assert_eq!(
            ranges
                .read_object_range_uncached(provider.id, physical)
                .await
                .unwrap(),
            [1, 2, 3, 4]
        );
        assert_eq!(provider.calls.load(Ordering::SeqCst), 2);
        let bare = RangeIO::new(provider, None);
        assert!(!bare.invalidate_range(request).await.unwrap());
        assert!(!bare
            .invalidate_object_range("registry.test", physical)
            .await
            .unwrap());
    }

    #[tokio::test]
    async fn short_provider_bytes_are_never_published_to_the_physical_cache() {
        let cache = Arc::new(Cache::default());
        let provider = source("s3://endpoint/bucket", &[1, 2, 3]);
        let ranges = RangeIO::new_with_cache(provider.clone(), None, Some(cache.clone()));
        let runtime = RuntimeIdentity::new("short-origin");
        assert!(matches!(
            ranges.read_range(request(&runtime)).await,
            Err(RangeReadError::Failure {
                class: RangeReadFailureClass::Integrity,
                ..
            })
        ));
        assert_eq!(provider.calls.load(Ordering::SeqCst), 1);
        assert_eq!(cache.writes.load(Ordering::SeqCst), 0);
        assert!(cache.entries.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn verified_peer_data_never_reads_or_populates_the_physical_cache() {
        use crate::snapshot::transport::p2p::protocol::{
            P2PAvailabilityResult, P2PTcpRequest, P2PTcpResponse, P2P_PROTOCOL_VERSION,
            P2P_TCP_MAGIC,
        };
        use crate::snapshot::transport::p2p::tcp::{read_tcp_frame, write_tcp_frame};
        use crate::snapshot::transport::p2p::P2PClientConfig;
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        use tokio::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let checksum = crate::snapshot::metadata::sha256_identity(&[1, 2, 3, 4]);
        let server_checksum = checksum.clone();
        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut magic = [0; 8];
            stream.read_exact(&mut magic).await.unwrap();
            assert_eq!(&magic, P2P_TCP_MAGIC);
            let P2PTcpRequest::Open {
                request_id,
                protocol_version,
                ..
            } = read_tcp_frame::<_, P2PTcpRequest>(&mut stream)
                .await
                .unwrap()
            else {
                panic!("expected peer opening")
            };
            assert_eq!(protocol_version, P2P_PROTOCOL_VERSION);
            write_tcp_frame(
                &mut stream,
                &P2PTcpResponse::Ready {
                    request_id,
                    protocol_version,
                },
            )
            .await
            .unwrap();
            let P2PTcpRequest::Availability {
                request_id,
                snapshot_id,
                spec_checksum,
                ..
            } = read_tcp_frame::<_, P2PTcpRequest>(&mut stream)
                .await
                .unwrap()
            else {
                panic!("expected peer probe")
            };
            write_tcp_frame(
                &mut stream,
                &P2PTcpResponse::Availability {
                    request_id,
                    availability: P2PAvailabilityResult {
                        snapshot_id,
                        spec_checksum,
                        available: true,
                        complete: false,
                        servable_objects: 1,
                    },
                },
            )
            .await
            .unwrap();
            let P2PTcpRequest::ReadRange { request_id, .. } =
                read_tcp_frame::<_, P2PTcpRequest>(&mut stream)
                    .await
                    .unwrap()
            else {
                panic!("expected peer DATA read")
            };
            write_tcp_frame(
                &mut stream,
                &P2PTcpResponse::Range {
                    request_id,
                    length: 4,
                    range_sha256: server_checksum,
                },
            )
            .await
            .unwrap();
            stream.write_all(&[1, 2, 3, 4]).await.unwrap();
        });
        let metrics =
            crate::observability::metrics::SnapdMetrics::new("peer-cache-bypass").unwrap();
        let peer = P2PClient::new(
            P2PClientConfig {
                enabled: true,
                port,
                probe_timeout: Duration::from_secs(2),
                range_timeout: Duration::from_secs(2),
            },
            metrics,
        )
        .unwrap();
        let provider = source("s3://endpoint/bucket", &[5, 6, 7, 8]);
        let cache = Arc::new(Cache::default());
        let ranges = RangeIO::new_with_cache(provider.clone(), Some(peer), Some(cache.clone()));
        let runtime = RuntimeIdentity::new("peer-cache-bypass");
        let hint = P2PSource {
            snapshot_id: "snapshot".into(),
            spec_checksum: "checksum".into(),
            worker_ip: "127.0.0.1".parse().unwrap(),
        };
        let read = ranges
            .read_range(RangeReadRequest {
                preferred_p2p_source: Some(&hint),
                expected_range_sha256: &checksum,
                ..request(&runtime)
            })
            .await
            .unwrap();
        assert_eq!(read.transport, RangeReadTransport::P2P);
        assert_eq!(read.payload, [1, 2, 3, 4]);
        assert!(!read.physical_cache_hit);
        assert_eq!(provider.calls.load(Ordering::SeqCst), 0);
        assert_eq!(cache.reads.load(Ordering::SeqCst), 0);
        assert_eq!(cache.writes.load(Ordering::SeqCst), 0);
        tokio::time::timeout(Duration::from_secs(2), server)
            .await
            .unwrap()
            .unwrap();
    }
}

#[cfg(test)]
mod object_transport_tests {
    use super::*;
    use crate::snapshot::io::range_io::{RangeIO, RangeReader};

    use std::sync::atomic::{AtomicUsize, Ordering};

    use crate::snapshot::io::range_io::{P2PSource, RangeDataKind, RangeReadTransport};
    use crate::RuntimeIdentity;

    struct PhysicalProvider {
        calls: AtomicUsize,
        reply: Result<Vec<u8>, RangeReadError>,
    }

    #[async_trait]
    impl PhysicalRangeReader for PhysicalProvider {
        fn source_id(&self) -> &str {
            "physical.test"
        }
        async fn read_physical_range(
            &self,
            request: PhysicalRangeRequest<'_>,
        ) -> Result<Vec<u8>, RangeReadError> {
            self.calls.fetch_add(1, Ordering::Relaxed);
            assert_eq!(request.object_key, "fixture.commit");
            assert_eq!(request.object_sha256, "complete-object-sha");
            assert_eq!(request.object_version, Some("version-1"));
            assert_eq!((request.offset, request.length), (8192, 4));
            self.reply.clone()
        }
    }

    fn request(runtime: &RuntimeIdentity) -> RangeReadRequest<'_> {
        RangeReadRequest {
            physical: None,
            authority: None,
            runtime,
            kind: RangeDataKind::Block,
            object_key: "fixture.commit",
            object_sha256: "complete-object-sha",
            expected_range_sha256: "logical-unit-checksum",
            offset: 8192,
            length: 4,
            object_version: Some("version-1"),
            preferred_p2p_source: None,
        }
    }

    fn provider(reply: Result<Vec<u8>, RangeReadError>) -> Arc<PhysicalProvider> {
        Arc::new(PhysicalProvider {
            calls: AtomicUsize::new(0),
            reply,
        })
    }

    #[tokio::test]
    async fn physical_transport_receives_only_object_identity_and_physical_range() {
        let physical = provider(Ok(vec![1, 2, 3, 4]));
        let bridge = RangeIO::new(physical.clone(), None);
        let runtime = RuntimeIdentity::new("physical-bridge");
        let result = bridge.read_range(request(&runtime)).await.unwrap();
        assert_eq!(result.payload, [1, 2, 3, 4]);
        assert_eq!(result.transport, RangeReadTransport::Provider);
        assert!(!result.physical_cache_hit);
        assert_eq!(physical.calls.load(Ordering::Relaxed), 1);
    }

    #[tokio::test]
    async fn peer_only_policy_never_reaches_physical_transport() {
        let physical = provider(Ok(vec![1, 2, 3, 4]));
        let reader = RangeIO::new(physical.clone(), None);
        let runtime = RuntimeIdentity::new("peer-only");
        let peer = P2PSource {
            snapshot_id: "snapshot".into(),
            spec_checksum: "checksum".into(),
            worker_ip: "127.0.0.1".parse().unwrap(),
        };
        let request = RangeReadRequest {
            preferred_p2p_source: Some(&peer),
            ..request(&runtime)
        };
        assert!(reader
            .read_with_policy(request, RangeSourcePolicy::PeerOnly)
            .await
            .is_err());
        assert_eq!(physical.calls.load(Ordering::Relaxed), 0);
    }

    #[tokio::test]
    async fn fallback_metrics_count_only_an_executed_fallback_policy() {
        use crate::snapshot::transport::p2p::P2PClientConfig;
        let physical = provider(Ok(vec![1, 2, 3, 4]));
        let metrics =
            crate::observability::metrics::SnapdMetrics::new("range-policy-metrics").unwrap();
        let peer_client = P2PClient::new(
            P2PClientConfig {
                enabled: false,
                port: 12345,
                probe_timeout: std::time::Duration::from_millis(100),
                range_timeout: std::time::Duration::from_millis(100),
            },
            Arc::clone(&metrics),
        )
        .unwrap();
        let reader = RangeIO::new(physical.clone(), Some(peer_client));
        let runtime = RuntimeIdentity::new("range-policy-metrics");
        let peer = P2PSource {
            snapshot_id: "snapshot".into(),
            spec_checksum: "checksum".into(),
            worker_ip: "127.0.0.1".parse().unwrap(),
        };
        let request = RangeReadRequest {
            preferred_p2p_source: Some(&peer),
            ..request(&runtime)
        };
        assert!(reader
            .read_with_policy(request, RangeSourcePolicy::PeerOnly)
            .await
            .is_err());
        assert_eq!(physical.calls.load(Ordering::Relaxed), 0);
        assert!(metrics
            .render(false)
            .contains("rock_snapd_p2p_fallback_total{reason=\"probe_miss\"} 0\n"));
        let read = reader
            .read_with_policy(request, RangeSourcePolicy::PeerThenProvider)
            .await
            .unwrap();
        assert_eq!(read.transport, RangeReadTransport::Provider);
        assert_eq!(physical.calls.load(Ordering::Relaxed), 1);
        assert!(metrics
            .render(false)
            .contains("rock_snapd_p2p_fallback_total{reason=\"probe_miss\"} 1\n"));
    }

    #[tokio::test]
    async fn encoded_plan_never_reuses_decoded_peer_coordinates_for_provider() {
        let physical = provider(Ok(vec![1, 2, 3, 4]));
        let reader = RangeIO::new(physical.clone(), None);
        let runtime = RuntimeIdentity::new("separate-coordinates");
        let base = request(&runtime);
        let request = RangeReadRequest {
            offset: 123,
            length: 99,
            physical: Some(PhysicalRangeRequest {
                namespace: None,
                object_size: None,
                object_key: base.object_key,
                object_sha256: base.object_sha256,
                object_version: base.object_version,
                offset: 8192,
                length: 4,
            }),
            ..base
        };
        let read = reader
            .read_with_policy(request, RangeSourcePolicy::ProviderOnly)
            .await
            .unwrap();
        assert_eq!(read.payload, [1, 2, 3, 4]);
        assert_eq!(read.representation, RangeRepresentation::EncodedObject);
        assert_eq!(physical.calls.load(Ordering::Relaxed), 1);
    }

    #[tokio::test]
    async fn invalid_ranges_and_short_physical_responses_fail_closed() {
        let physical = provider(Ok(vec![1, 2, 3]));
        let bridge = RangeIO::new(physical.clone(), None);
        let runtime = RuntimeIdentity::new("physical-bridge-short");
        for invalid in [
            RangeReadRequest {
                length: 0,
                ..request(&runtime)
            },
            RangeReadRequest {
                offset: u64::MAX,
                ..request(&runtime)
            },
        ] {
            assert_eq!(
                bridge.read_range(invalid).await,
                Err(RangeReadError::InvalidRange)
            );
        }
        assert_eq!(physical.calls.load(Ordering::Relaxed), 0);
        assert!(matches!(
            bridge.read_range(request(&runtime)).await,
            Err(RangeReadError::Failure {
                class: RangeReadFailureClass::Integrity,
                ..
            })
        ));
    }

    #[tokio::test]
    async fn typed_physical_errors_are_preserved() {
        let error = RangeReadError::failure(RangeReadFailureClass::Authorization, "denied");
        let physical = provider(Err(error.clone()));
        let bridge = RangeIO::new(physical, None);
        let runtime = RuntimeIdentity::new("physical-bridge-error");
        assert_eq!(bridge.read_range(request(&runtime)).await, Err(error));
    }

    #[tokio::test]
    async fn peer_only_tcp_errors_preserve_integrity_and_not_found_without_fallback() {
        use crate::snapshot::transport::p2p::protocol::{
            P2PAvailabilityResult, P2PTcpErrorCode, P2PTcpRequest, P2PTcpResponse,
            P2P_PROTOCOL_VERSION, P2P_TCP_MAGIC,
        };
        use crate::snapshot::transport::p2p::tcp::{read_tcp_frame, write_tcp_frame};
        use crate::snapshot::transport::p2p::P2PClientConfig;
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        use tokio::net::TcpListener;

        for corrupt_payload in [false, true] {
            let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let port = listener.local_addr().unwrap().port();
            let checksum = crate::snapshot::metadata::sha256_identity(&[1, 2, 3, 4]);
            let response_checksum = checksum.clone();
            let server = tokio::spawn(async move {
                let (mut stream, _) = listener.accept().await.unwrap();
                let mut magic = [0; 8];
                stream.read_exact(&mut magic).await.unwrap();
                assert_eq!(&magic, P2P_TCP_MAGIC);
                let opening = read_tcp_frame::<_, P2PTcpRequest>(&mut stream)
                    .await
                    .unwrap();
                let P2PTcpRequest::Open {
                    request_id,
                    protocol_version,
                    ..
                } = opening
                else {
                    panic!("expected peer protocol opening")
                };
                assert_eq!(protocol_version, P2P_PROTOCOL_VERSION);
                write_tcp_frame(
                    &mut stream,
                    &P2PTcpResponse::Ready {
                        request_id,
                        protocol_version: P2P_PROTOCOL_VERSION,
                    },
                )
                .await
                .unwrap();
                let probe = read_tcp_frame::<_, P2PTcpRequest>(&mut stream)
                    .await
                    .unwrap();
                let P2PTcpRequest::Availability {
                    request_id,
                    snapshot_id,
                    spec_checksum,
                    selection_only,
                    ..
                } = probe
                else {
                    panic!("expected peer selection probe")
                };
                assert!(selection_only);
                write_tcp_frame(
                    &mut stream,
                    &P2PTcpResponse::Availability {
                        request_id,
                        availability: P2PAvailabilityResult {
                            snapshot_id,
                            spec_checksum,
                            available: true,
                            complete: false,
                            servable_objects: 1,
                        },
                    },
                )
                .await
                .unwrap();
                let range = read_tcp_frame::<_, P2PTcpRequest>(&mut stream)
                    .await
                    .unwrap();
                let P2PTcpRequest::ReadRange {
                    request_id,
                    request,
                    ..
                } = range
                else {
                    panic!("expected decoded DATA range request")
                };
                assert_eq!((request.offset, request.length), (8192, 4));
                assert_eq!(request.object_key, "fixture.commit");
                if corrupt_payload {
                    // The declared digest matches trusted metadata, but the
                    // actual peer bytes fail its checksum after transfer.
                    write_tcp_frame(
                        &mut stream,
                        &P2PTcpResponse::Range {
                            request_id,
                            length: 4,
                            range_sha256: response_checksum,
                        },
                    )
                    .await
                    .unwrap();
                    stream.write_all(&[1, 2, 3, 5]).await.unwrap();
                } else {
                    write_tcp_frame(
                        &mut stream,
                        &P2PTcpResponse::Error {
                            request_id,
                            code: P2PTcpErrorCode::NotFound,
                            retryable: false,
                            message: "peer DATA range is not resident".into(),
                        },
                    )
                    .await
                    .unwrap();
                }
            });
            let metrics =
                crate::observability::metrics::SnapdMetrics::new("peer-only-errors").unwrap();
            let peer_client = P2PClient::new(
                P2PClientConfig {
                    enabled: true,
                    port,
                    probe_timeout: std::time::Duration::from_secs(2),
                    range_timeout: std::time::Duration::from_secs(2),
                },
                Arc::clone(&metrics),
            )
            .unwrap();
            let physical = provider(Ok(vec![1, 2, 3, 4]));
            let reader = RangeIO::new(physical.clone(), Some(peer_client));
            let runtime = RuntimeIdentity::new("peer-only-errors");
            let peer = P2PSource {
                snapshot_id: "snapshot".into(),
                spec_checksum: "checksum".into(),
                worker_ip: "127.0.0.1".parse().unwrap(),
            };
            let request = RangeReadRequest {
                preferred_p2p_source: Some(&peer),
                expected_range_sha256: &checksum,
                ..request(&runtime)
            };
            let error = tokio::time::timeout(
                std::time::Duration::from_secs(3),
                reader.read_with_policy(request, RangeSourcePolicy::PeerOnly),
            )
            .await
            .unwrap()
            .unwrap_err();
            if corrupt_payload {
                assert!(matches!(
                    error,
                    RangeReadError::Failure {
                        class: RangeReadFailureClass::Integrity,
                        ..
                    }
                ));
            } else {
                assert_eq!(error, RangeReadError::NotFound);
            }
            assert_eq!(physical.calls.load(Ordering::Relaxed), 0);
            let rendered = metrics.render(false);
            let fallbacks = rendered
                .lines()
                .filter(|line| line.starts_with("rock_snapd_p2p_fallback_total{"))
                .collect::<Vec<_>>();
            assert_eq!(fallbacks.len(), 6);
            assert!(fallbacks.iter().all(|line| line.ends_with(" 0")));
            tokio::time::timeout(std::time::Duration::from_secs(3), server)
                .await
                .unwrap()
                .unwrap();
        }
    }
}
