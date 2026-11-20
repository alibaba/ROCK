//! Publication-scoped logical graph I/O for a Registry destination. Formats own
//! the object map; this adapter copies authenticated ancestry into that one
//! repository and shares the uploader's transfer and buffer admission limits.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex as StdMutex, Weak};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use sha2::{Digest, Sha256};
use tokio::sync::{Mutex, OwnedSemaphorePermit, Semaphore};

use crate::snapshot::format::overlaybd_oci::{
    RegistryObjectBinding, RegistryObjectMap, MAX_SNAPSHOT_OBJECTS,
};
use crate::snapshot::transport::registry::{
    validate_digest, RegistryClient, RegistryWriteTarget, MAX_REGISTRY_METADATA_BYTES,
};
use crate::snapshot::transport::{
    BlobRef, BoundedGetError, ImmutableObjectIdentity, ImmutableObjectStore, ImmutableStreamWriter,
    IoCancellation, NamespaceMutationFence, ObjectStoreError, PhysicalRangeReader,
    PhysicalRangeRequest, PublishFailureKind, RangeReadError, RangeReadFailureClass, RemoteObject,
    RemoteObjectMetadata, ReplayableByteSource, UploadTransactionLease,
};

#[derive(Clone, Debug)]
pub(crate) struct RegistryCopyBudget {
    pub transfer_permits: Arc<Semaphore>,
    pub buffer_permits: Arc<Semaphore>,
    pub chunk_bytes: usize,
    pub attempt_timeout: Duration,
    pub cancellation: IoCancellation,
}

impl RegistryCopyBudget {
    fn deadline(&self) -> Result<Instant, ObjectStoreError> {
        Instant::now()
            .checked_add(self.attempt_timeout)
            .ok_or_else(|| invalid("Registry copy deadline overflows"))
    }

    async fn admit(
        &self,
        length: u64,
        deadline: Instant,
    ) -> Result<(OwnedSemaphorePermit, OwnedSemaphorePermit), ObjectStoreError> {
        let transfer = active(deadline, &self.cancellation, async {
            Arc::clone(&self.transfer_permits)
                .acquire_owned()
                .await
                .map_err(|_| retryable("Registry copy transfer admission is closed"))
        })
        .await?;
        let reservation = length.min(self.chunk_bytes as u64).max(1) * 2;
        let count = u32::try_from(reservation)
            .map_err(|_| invalid("Registry copy buffer reservation overflows"))?;
        let buffer = active(deadline, &self.cancellation, async {
            Arc::clone(&self.buffer_permits)
                .acquire_many_owned(count)
                .await
                .map_err(|_| retryable("Registry copy buffer admission is closed"))
        })
        .await?;
        Ok((transfer, buffer))
    }
}

#[derive(Clone)]
enum CopySource {
    Registry {
        client: Arc<RegistryClient>,
        repository: String,
        blob: BlobRef,
    },
    Physical {
        reader: Arc<dyn PhysicalRangeReader>,
        namespace: String,
        key: String,
        metadata: RemoteObjectMetadata,
    },
}

impl ReplayableByteSource for CopySource {
    fn length(&self) -> u64 {
        match self {
            Self::Registry { blob, .. } => blob.size,
            Self::Physical { metadata, .. } => metadata.size,
        }
    }

    fn read_at<'a>(
        &'a self,
        offset: u64,
        max_bytes: usize,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<u8>, ObjectStoreError>> + Send + 'a>> {
        Box::pin(async move {
            if max_bytes == 0 || max_bytes > 16 * 1024 * 1024 || offset >= self.length() {
                return Err(invalid("Registry copy source range is invalid"));
            }
            let length = (self.length() - offset).min(max_bytes as u64);
            let bytes = match self {
                Self::Registry {
                    client,
                    repository,
                    blob,
                } => client
                    .read_blob_range(repository, &blob.digest, blob.size, offset, length)
                    .await
                    .map_err(ObjectStoreError::from)?,
                Self::Physical {
                    reader,
                    namespace,
                    key,
                    metadata,
                } => reader
                    .read_physical_range(PhysicalRangeRequest {
                        namespace: Some(namespace),
                        object_key: key,
                        object_sha256: &metadata.sha256,
                        object_size: Some(metadata.size),
                        object_version: metadata.version.as_deref(),
                        offset,
                        length,
                    })
                    .await
                    .map_err(range_error)?,
            };
            if bytes.len() as u64 != length {
                return Err(conflict("Registry ancestry copy returned a short range"));
            }
            Ok(bytes)
        })
    }
}

struct BytesSource(Arc<[u8]>);

impl ReplayableByteSource for BytesSource {
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
                .ok_or_else(|| invalid("Registry graph metadata offset is invalid"))?;
            let end = start.saturating_add(max_bytes).min(self.0.len());
            Ok(self.0[start..end].to_vec())
        })
    }
}

#[derive(Default)]
struct GraphState {
    bindings: BTreeMap<String, RegistryObjectBinding>,
    sources: HashMap<String, CopySource>,
}

#[derive(Clone)]
struct Description {
    binding: RegistryObjectBinding,
    source: Option<CopySource>,
}

pub(crate) struct RegistryGraphStore {
    writer: Arc<RegistryWriteTarget>,
    fallback_store: Option<Arc<dyn ImmutableObjectStore>>,
    fallback_reader: Option<Arc<dyn PhysicalRangeReader>>,
    registry_sources: Vec<Arc<RegistryObjectMap>>,
    planned: HashSet<String>,
    state: StdMutex<GraphState>,
    locks: StdMutex<HashMap<String, Weak<Mutex<()>>>>,
    lease: Option<UploadTransactionLease>,
    budget: RegistryCopyBudget,
}

impl std::fmt::Debug for RegistryGraphStore {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("RegistryGraphStore")
            .field("writer", &self.writer)
            .field("planned_objects", &self.planned.len())
            .field("registry_sources", &self.registry_sources.len())
            .finish_non_exhaustive()
    }
}

impl RegistryGraphStore {
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn new(
        writer: Arc<RegistryWriteTarget>,
        fallback_store: Option<Arc<dyn ImmutableObjectStore>>,
        fallback_reader: Option<Arc<dyn PhysicalRangeReader>>,
        registry_sources: Vec<Arc<RegistryObjectMap>>,
        planned: Vec<RegistryObjectBinding>,
        lease: Option<UploadTransactionLease>,
        budget: RegistryCopyBudget,
    ) -> Result<Arc<Self>, ObjectStoreError> {
        if !(1..=16 * 1024 * 1024).contains(&budget.chunk_bytes)
            || budget.attempt_timeout.is_zero()
            || registry_sources.len() > 64
            || fallback_store.is_some() != fallback_reader.is_some()
            || lease
                .as_ref()
                .is_some_and(|lease| !lease.validates(&writer.fence()))
        {
            return Err(invalid(
                "Registry graph copy configuration or lease is invalid",
            ));
        }
        let mut state = GraphState::default();
        let mut planned_keys = HashSet::new();
        for binding in planned {
            planned_keys.insert(binding.object_key.clone());
            install_binding(&mut state, binding)?;
        }
        // Validate conflicts across all authenticated parents up front. No
        // parent map is a fallback authority for a newly planned object.
        let mut inherited = BTreeMap::new();
        for source in &registry_sources {
            for (key, blob) in source.bindings() {
                let binding = RegistryObjectBinding {
                    object_key: key.clone(),
                    blob: blob.clone(),
                    object_version: source.object_version(key).map(str::to_owned),
                };
                validate_binding(&binding)?;
                if let Some(previous) = inherited.insert(key.clone(), binding.clone()) {
                    compatible(&previous, &binding)?;
                }
                if inherited.len() > MAX_SNAPSHOT_OBJECTS {
                    return Err(invalid("Registry parent object count exceeds its bound"));
                }
                if state.bindings.contains_key(key) {
                    // A new receipt may omit a logical provider version while
                    // an inherited reference still requires it. The common
                    // digest can serve both by retaining the precise version.
                    install_binding(&mut state, binding)?;
                }
            }
        }
        Ok(Arc::new(Self {
            writer,
            fallback_store,
            fallback_reader,
            registry_sources,
            planned: planned_keys,
            state: StdMutex::new(state),
            locks: StdMutex::new(HashMap::new()),
            lease,
            budget,
        }))
    }

    pub(crate) fn bindings(&self) -> Result<Vec<RegistryObjectBinding>, ObjectStoreError> {
        Ok(self
            .state
            .lock()
            .map_err(|_| invalid("Registry graph state is poisoned"))?
            .bindings
            .values()
            .cloned()
            .collect())
    }

    fn key_lock(&self, key: &str) -> Result<Arc<Mutex<()>>, ObjectStoreError> {
        let mut locks = self
            .locks
            .lock()
            .map_err(|_| invalid("Registry graph locks are poisoned"))?;
        locks.retain(|_, lock| lock.strong_count() != 0);
        if let Some(lock) = locks.get(key).and_then(Weak::upgrade) {
            return Ok(lock);
        }
        let lock = Arc::new(Mutex::new(()));
        locks.insert(key.into(), Arc::downgrade(&lock));
        Ok(lock)
    }

    fn record(
        &self,
        binding: RegistryObjectBinding,
        source: Option<CopySource>,
    ) -> Result<Description, ObjectStoreError> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| invalid("Registry graph state is poisoned"))?;
        install_binding(&mut state, binding.clone())?;
        if let Some(source) = source {
            state
                .sources
                .entry(binding.object_key.clone())
                .or_insert(source);
        }
        Ok(Description {
            binding: state.bindings[&binding.object_key].clone(),
            source: state.sources.get(&binding.object_key).cloned(),
        })
    }

    async fn describe(&self, key: &str) -> Result<Option<Description>, ObjectStoreError> {
        if key.is_empty() || key.len() > 4096 {
            return Err(invalid("Registry graph key is invalid"));
        }
        {
            let state = self
                .state
                .lock()
                .map_err(|_| invalid("Registry graph state is poisoned"))?;
            if let Some(binding) = state.bindings.get(key) {
                return Ok(Some(Description {
                    binding: binding.clone(),
                    source: state.sources.get(key).cloned(),
                }));
            }
        }
        let mut inherited: Option<Description> = None;
        for parent in &self.registry_sources {
            if let Some(blob) = parent.bindings().get(key) {
                let binding = RegistryObjectBinding {
                    object_key: key.into(),
                    blob: blob.clone(),
                    object_version: parent.object_version(key).map(str::to_owned),
                };
                if let Some(selected) = &mut inherited {
                    compatible(&selected.binding, &binding)?;
                    if selected.binding.object_version.is_none() {
                        selected.binding.object_version = binding.object_version;
                    }
                } else {
                    inherited = Some(Description {
                        binding,
                        source: Some(CopySource::Registry {
                            client: parent.client(),
                            repository: parent.repository().into(),
                            blob: blob.clone(),
                        }),
                    });
                }
            }
        }
        if let Some(inherited) = inherited {
            return self.record(inherited.binding, inherited.source).map(Some);
        }
        // This path is unreachable for every key supplied in `planned`: its
        // descriptor was installed before any publication I/O began.
        if self.planned.contains(key) {
            return Err(conflict("Registry planned object descriptor is missing"));
        }
        let (Some(fallback_store), Some(fallback_reader)) =
            (&self.fallback_store, &self.fallback_reader)
        else {
            // Registry-only workers have no implicit object-store authority.
            // An unknown logical key remains absent rather than selecting a
            // default endpoint or manufacturing a provider capability.
            return Ok(None);
        };
        let metadata = active(
            self.budget.deadline()?,
            &self.budget.cancellation,
            fallback_store.head(key),
        )
        .await?;
        let Some(metadata) = metadata else {
            return Ok(None);
        };
        self.record(
            RegistryObjectBinding {
                object_key: key.into(),
                blob: BlobRef {
                    digest: metadata.sha256.clone(),
                    size: metadata.size,
                },
                object_version: metadata.version.clone(),
            },
            Some(CopySource::Physical {
                reader: Arc::clone(fallback_reader),
                namespace: fallback_store.namespace().into(),
                key: key.into(),
                metadata,
            }),
        )
        .map(Some)
    }

    async fn local_exists(
        &self,
        binding: &RegistryObjectBinding,
    ) -> Result<bool, ObjectStoreError> {
        active(self.budget.deadline()?, &self.budget.cancellation, async {
            self.writer
                .client()
                .head_blob(self.writer.repository(), &binding.blob)
                .await
                .map_err(ObjectStoreError::from)
        })
        .await
    }

    async fn ensure_description(
        &self,
        description: &Description,
    ) -> Result<bool, ObjectStoreError> {
        if self.local_exists(&description.binding).await? {
            return Ok(true);
        }
        let Some(source) = &description.source else {
            return Ok(false);
        };
        let lease = self
            .lease
            .as_ref()
            .ok_or_else(|| invalid("Registry domain store has no publication lease"))?;
        let deadline = self.budget.deadline()?;
        let key_lock = self.key_lock(&description.binding.object_key)?;
        let _key = active(deadline, &self.budget.cancellation, async {
            Ok(key_lock.lock_owned().await)
        })
        .await?;
        if self.local_exists(&description.binding).await? {
            return Ok(true);
        }
        let (_transfer, _buffer) = self
            .budget
            .admit(description.binding.blob.size, deadline)
            .await?;
        self.writer
            .ensure_stream_verified(
                &ImmutableObjectIdentity {
                    object_key: description.binding.object_key.clone(),
                    sha256: description.binding.blob.digest.clone(),
                    length: description.binding.blob.size,
                },
                Arc::new(source.clone()),
                lease.clone(),
                deadline,
                self.budget.cancellation.clone(),
                self.budget.chunk_bytes,
            )
            .await?;
        Ok(true)
    }

    pub(crate) async fn copy_registry_blob(
        &self,
        source_client: Arc<RegistryClient>,
        source_repository: &str,
        blob: &BlobRef,
        object_key: &str,
    ) -> Result<RegistryObjectBinding, ObjectStoreError> {
        let description = self.record(
            RegistryObjectBinding {
                object_key: object_key.into(),
                blob: blob.clone(),
                object_version: None,
            },
            Some(CopySource::Registry {
                client: source_client,
                repository: source_repository.into(),
                blob: blob.clone(),
            }),
        )?;
        if !self.ensure_description(&description).await? {
            return Err(retryable("Registry source copy is not visible"));
        }
        Ok(description.binding)
    }

    fn object_metadata(binding: &RegistryObjectBinding) -> RemoteObjectMetadata {
        RemoteObjectMetadata {
            size: binding.blob.size,
            sha256: binding.blob.digest.clone(),
            version: binding.object_version.clone(),
            last_modified_unix: None,
            etag: None,
        }
    }
}

#[async_trait]
impl ImmutableObjectStore for RegistryGraphStore {
    fn namespace(&self) -> &str {
        self.writer.namespace()
    }
    fn fence(&self) -> Arc<NamespaceMutationFence> {
        self.writer.fence()
    }

    async fn head(&self, key: &str) -> Result<Option<RemoteObjectMetadata>, ObjectStoreError> {
        let Some(description) = self.describe(key).await? else {
            return Ok(None);
        };
        if self.ensure_description(&description).await? {
            Ok(Some(Self::object_metadata(&description.binding)))
        } else {
            Ok(None)
        }
    }

    async fn put_if_absent(
        &self,
        key: &str,
        payload: &[u8],
        sha256: &str,
    ) -> Result<bool, ObjectStoreError> {
        let lease = self
            .lease
            .as_ref()
            .ok_or_else(|| invalid("Registry domain store has no publication lease"))?;
        if payload.is_empty() || payload.len() as u64 > MAX_REGISTRY_METADATA_BYTES {
            return Err(invalid("Registry graph metadata exceeds its write bound"));
        }
        if format!("sha256:{:x}", Sha256::digest(payload)) != sha256 {
            return Err(conflict("Registry metadata payload differs from its SHA"));
        }
        let description = self.record(
            RegistryObjectBinding {
                object_key: key.into(),
                blob: BlobRef {
                    digest: sha256.into(),
                    size: payload.len() as u64,
                },
                object_version: None,
            },
            None,
        )?;
        let deadline = self.budget.deadline()?;
        let lock = self.key_lock(key)?;
        let _key = active(deadline, &self.budget.cancellation, async {
            Ok(lock.lock_owned().await)
        })
        .await?;
        if self.local_exists(&description.binding).await? {
            return Ok(false);
        }
        let (_transfer, _buffer) = self.budget.admit(payload.len() as u64, deadline).await?;
        self.writer
            .ensure_stream_verified(
                &ImmutableObjectIdentity {
                    object_key: key.into(),
                    sha256: sha256.into(),
                    length: payload.len() as u64,
                },
                Arc::new(BytesSource(Arc::from(payload))),
                lease.clone(),
                deadline,
                self.budget.cancellation.clone(),
                self.budget.chunk_bytes,
            )
            .await?;
        Ok(true)
    }

    async fn get_with_metadata(
        &self,
        key: &str,
        version: Option<&str>,
        max: u64,
    ) -> Result<Option<RemoteObject>, BoundedGetError> {
        let Some(description) = self.describe(key).await? else {
            return Ok(None);
        };
        let binding = &description.binding;
        if version.is_some_and(|version| binding.object_version.as_deref() != Some(version)) {
            return Err(conflict("Registry graph object logical version differs").into());
        }
        if binding.blob.size > max || binding.blob.size > MAX_REGISTRY_METADATA_BYTES {
            return Err(BoundedGetError::BoundExceeded {
                object_key: key.into(),
                actual_bytes: binding.blob.size,
                max_bytes: max.min(MAX_REGISTRY_METADATA_BYTES),
            });
        }
        if !self.ensure_description(&description).await? {
            return Ok(None);
        }
        let deadline = self.budget.deadline()?;
        let (_transfer, _buffer) = self.budget.admit(binding.blob.size, deadline).await?;
        let mut payload = Vec::with_capacity(binding.blob.size as usize);
        while (payload.len() as u64) < binding.blob.size {
            let offset = payload.len() as u64;
            let length = (binding.blob.size - offset).min(self.budget.chunk_bytes as u64);
            let bytes = active(deadline, &self.budget.cancellation, async {
                self.writer
                    .client()
                    .read_blob_range(
                        self.writer.repository(),
                        &binding.blob.digest,
                        binding.blob.size,
                        offset,
                        length,
                    )
                    .await
                    .map_err(ObjectStoreError::from)
            })
            .await?;
            payload.extend_from_slice(&bytes);
        }
        if format!("sha256:{:x}", Sha256::digest(&payload)) != binding.blob.digest {
            return Err(conflict("Registry graph metadata read-back checksum differs").into());
        }
        Ok(Some(RemoteObject {
            payload,
            metadata: Self::object_metadata(binding),
        }))
    }

    async fn get(&self, key: &str) -> Result<Vec<u8>, ObjectStoreError> {
        self.get_with_metadata(key, None, MAX_REGISTRY_METADATA_BYTES)
            .await
            .map_err(|error| match error {
                BoundedGetError::Store(error) => error,
                other => invalid(&other.to_string()),
            })?
            .map(|object| object.payload)
            .ok_or_else(|| retryable("Registry graph object is absent"))
    }
}

fn validate_binding(binding: &RegistryObjectBinding) -> Result<(), ObjectStoreError> {
    validate_digest(&binding.blob.digest)?;
    if binding.object_key.is_empty()
        || binding.object_key.len() > 4096
        || binding.blob.size == 0
        || binding
            .object_version
            .as_ref()
            .is_some_and(|version| version.is_empty() || version.len() > 1024)
    {
        return Err(invalid("Registry graph object binding is invalid"));
    }
    Ok(())
}

fn compatible(
    left: &RegistryObjectBinding,
    right: &RegistryObjectBinding,
) -> Result<(), ObjectStoreError> {
    if left.blob != right.blob
        || matches!((&left.object_version,&right.object_version),(Some(left),Some(right)) if left!=right)
    {
        return Err(conflict(
            "Registry graph key has conflicting SHA, size or version",
        ));
    }
    Ok(())
}

fn install_binding(
    state: &mut GraphState,
    binding: RegistryObjectBinding,
) -> Result<(), ObjectStoreError> {
    validate_binding(&binding)?;
    if let Some(previous) = state.bindings.get_mut(&binding.object_key) {
        compatible(previous, &binding)?;
        if previous.object_version.is_none() {
            previous.object_version = binding.object_version;
        }
    } else {
        if state.bindings.len() >= MAX_SNAPSHOT_OBJECTS {
            return Err(invalid("Registry graph object count exceeds its bound"));
        }
        state.bindings.insert(binding.object_key.clone(), binding);
    }
    Ok(())
}

async fn active<T>(
    deadline: Instant,
    cancellation: &IoCancellation,
    operation: impl Future<Output = Result<T, ObjectStoreError>>,
) -> Result<T, ObjectStoreError> {
    if cancellation.is_cancelled() || Instant::now() >= deadline {
        return Err(retryable("Registry graph I/O was cancelled or timed out"));
    }
    let timer = tokio::time::sleep(deadline.saturating_duration_since(Instant::now()));
    tokio::pin!(timer);
    tokio::pin!(operation);
    loop {
        tokio::select! {
            result=&mut operation => {if cancellation.is_cancelled() || Instant::now()>=deadline {return Err(retryable("Registry graph I/O was cancelled or timed out"));} return result;}
            _=&mut timer => return Err(retryable("Registry graph I/O timed out")),
            _=tokio::time::sleep(Duration::from_millis(25)) => if cancellation.is_cancelled() {return Err(retryable("Registry graph I/O was cancelled"));},
        }
    }
}

fn range_error(error: RangeReadError) -> ObjectStoreError {
    let kind = match &error {
        RangeReadError::NotFound
        | RangeReadError::Failure {
            class: RangeReadFailureClass::Transient,
            ..
        } => PublishFailureKind::Retryable,
        RangeReadError::Failure {
            class: RangeReadFailureClass::Integrity,
            ..
        } => PublishFailureKind::IdentityConflict,
        RangeReadError::Fallback { final_error, .. } => {
            return range_error((**final_error).clone())
        }
        _ => PublishFailureKind::Fatal,
    };
    ObjectStoreError::new(kind, error.to_string())
}
fn invalid(message: &str) -> ObjectStoreError {
    ObjectStoreError::new(PublishFailureKind::Fatal, message)
}
fn conflict(message: &str) -> ObjectStoreError {
    ObjectStoreError::new(PublishFailureKind::IdentityConflict, message)
}
fn retryable(message: &str) -> ObjectStoreError {
    ObjectStoreError::new(PublishFailureKind::Retryable, message)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::convert::Infallible;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use hyper::service::{make_service_fn, service_fn};
    use hyper::{Body, Response};

    struct MissingRegistry {
        client: Arc<RegistryClient>,
        requests: Arc<AtomicUsize>,
        task: tokio::task::JoinHandle<()>,
    }

    impl Drop for MissingRegistry {
        fn drop(&mut self) {
            self.task.abort();
        }
    }

    fn missing_registry() -> MissingRegistry {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let origin = format!("http://{}", listener.local_addr().unwrap());
        let client = RegistryClient::test_http_client(&origin);
        let requests = Arc::new(AtomicUsize::new(0));
        let observed = Arc::clone(&requests);
        let service = make_service_fn(move |_| {
            let observed = Arc::clone(&observed);
            async move {
                Ok::<_, Infallible>(service_fn(move |_| {
                    observed.fetch_add(1, Ordering::Relaxed);
                    async {
                        Ok::<_, Infallible>(
                            Response::builder().status(404).body(Body::empty()).unwrap(),
                        )
                    }
                }))
            }
        });
        let task = tokio::spawn(async move {
            hyper::Server::from_tcp(listener)
                .unwrap()
                .serve(service)
                .await
                .unwrap();
        });
        MissingRegistry {
            client,
            requests,
            task,
        }
    }

    struct Origin {
        fence: Arc<NamespaceMutationFence>,
        heads: AtomicUsize,
    }

    impl std::fmt::Debug for Origin {
        fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            formatter.write_str("Origin")
        }
    }

    #[async_trait]
    impl ImmutableObjectStore for Origin {
        fn namespace(&self) -> &str {
            "source-bucket"
        }
        fn fence(&self) -> Arc<NamespaceMutationFence> {
            Arc::clone(&self.fence)
        }
        async fn head(&self, _: &str) -> Result<Option<RemoteObjectMetadata>, ObjectStoreError> {
            self.heads.fetch_add(1, Ordering::Relaxed);
            Ok(None)
        }
        async fn put_if_absent(
            &self,
            _: &str,
            _: &[u8],
            _: &str,
        ) -> Result<bool, ObjectStoreError> {
            panic!("Registry export must not write its fallback provider")
        }
        async fn get(&self, _: &str) -> Result<Vec<u8>, ObjectStoreError> {
            panic!("Registry ancestry must not use an unbounded fallback GET")
        }
    }

    #[derive(Debug, Eq, PartialEq)]
    struct Read {
        namespace: String,
        key: String,
        sha: String,
        size: u64,
        version: Option<String>,
        offset: u64,
        length: u64,
    }

    struct Physical {
        reads: StdMutex<Vec<Read>>,
        short: bool,
    }

    #[async_trait]
    impl PhysicalRangeReader for Physical {
        fn source_id(&self) -> &str {
            "test-source"
        }
        async fn read_physical_range(
            &self,
            request: PhysicalRangeRequest<'_>,
        ) -> Result<Vec<u8>, RangeReadError> {
            request.validate()?;
            self.reads.lock().unwrap().push(Read {
                namespace: request.namespace.unwrap().into(),
                key: request.object_key.into(),
                sha: request.object_sha256.into(),
                size: request.object_size.unwrap(),
                version: request.object_version.map(str::to_owned),
                offset: request.offset,
                length: request.length,
            });
            let length = request.length as usize - usize::from(self.short);
            Ok((request.offset..request.offset + length as u64)
                .map(|value| value as u8)
                .collect())
        }
    }

    fn budget() -> (RegistryCopyBudget, tokio::sync::watch::Sender<bool>) {
        let (sender, receiver) = tokio::sync::watch::channel(false);
        (
            RegistryCopyBudget {
                transfer_permits: Arc::new(Semaphore::new(1)),
                buffer_permits: Arc::new(Semaphore::new(16)),
                chunk_bytes: 8,
                attempt_timeout: Duration::from_secs(2),
                cancellation: IoCancellation::new(receiver),
            },
            sender,
        )
    }

    fn binding(key: &str, bytes: u64, digit: char, version: Option<&str>) -> RegistryObjectBinding {
        RegistryObjectBinding {
            object_key: key.into(),
            blob: BlobRef {
                digest: format!("sha256:{}", digit.to_string().repeat(64)),
                size: bytes,
            },
            object_version: version.map(str::to_owned),
        }
    }

    fn target(client: Arc<RegistryClient>) -> Arc<RegistryWriteTarget> {
        let fence = NamespaceMutationFence::new(format!("{}/output", client.registry())).unwrap();
        RegistryWriteTarget::new(client, "output".into(), "test-output".into(), fence).unwrap()
    }

    fn origin() -> Arc<Origin> {
        Arc::new(Origin {
            fence: NamespaceMutationFence::new("source-bucket").unwrap(),
            heads: AtomicUsize::new(0),
        })
    }

    fn physical(short: bool) -> Arc<Physical> {
        Arc::new(Physical {
            reads: StdMutex::new(Vec::new()),
            short,
        })
    }

    #[tokio::test]
    async fn planned_missing_objects_never_probe_fallback_provider() {
        let registry = missing_registry();
        let output = target(Arc::clone(&registry.client));
        let fallback = origin();
        let reader = physical(false);
        let (budget, _sender) = budget();
        let lease = UploadTransactionLease::acquire(output.fence()).await;
        let store = RegistryGraphStore::new(
            output,
            Some(fallback.clone()),
            Some(reader.clone()),
            Vec::new(),
            vec![binding("new/head", 4, 'a', None)],
            Some(lease),
            budget,
        )
        .unwrap();
        assert!(store.head("new/head").await.unwrap().is_none());
        assert!(store
            .get_with_metadata("new/head", None, 16)
            .await
            .unwrap()
            .is_none());
        assert_eq!(fallback.heads.load(Ordering::Relaxed), 0);
        assert!(reader.reads.lock().unwrap().is_empty());
        assert_eq!(registry.requests.load(Ordering::Relaxed), 2);
        assert!(store.head("unknown-parent-key").await.unwrap().is_none());
        assert_eq!(fallback.heads.load(Ordering::Relaxed), 1);
    }

    #[tokio::test]
    async fn registry_only_workers_have_no_implicit_fallback_authority() {
        let registry = missing_registry();
        let output = target(Arc::clone(&registry.client));
        let (budget, _sender) = budget();
        let store = RegistryGraphStore::new(
            Arc::clone(&output),
            None,
            None,
            Vec::new(),
            vec![binding("planned/head", 4, 'a', None)],
            None,
            budget.clone(),
        )
        .unwrap();
        assert!(store.head("unknown/ancestor").await.unwrap().is_none());
        assert!(store
            .get_with_metadata("unknown/ancestor", None, 16)
            .await
            .unwrap()
            .is_none());
        assert_eq!(registry.requests.load(Ordering::Relaxed), 0);
        assert!(store.head("planned/head").await.unwrap().is_none());
        assert_eq!(registry.requests.load(Ordering::Relaxed), 1);
        assert!(RegistryGraphStore::new(
            Arc::clone(&output),
            Some(origin()),
            None,
            Vec::new(),
            Vec::new(),
            None,
            budget.clone()
        )
        .is_err());
        assert!(RegistryGraphStore::new(
            output,
            None,
            Some(physical(false)),
            Vec::new(),
            Vec::new(),
            None,
            budget
        )
        .is_err());
        assert_eq!(registry.requests.load(Ordering::Relaxed), 1);
    }

    #[tokio::test]
    async fn metadata_bound_and_logical_version_fail_before_copy_io() {
        let registry = missing_registry();
        let output = target(Arc::clone(&registry.client));
        let fallback = origin();
        let (budget, _sender) = budget();
        let store = RegistryGraphStore::new(
            output,
            Some(fallback.clone()),
            Some(physical(false)),
            Vec::new(),
            vec![binding("metadata", 4096, 'a', Some("pinned-version"))],
            None,
            budget,
        )
        .unwrap();
        assert!(matches!(
            store.get_with_metadata("metadata", None, 16).await,
            Err(BoundedGetError::BoundExceeded {
                actual_bytes: 4096,
                ..
            })
        ));
        assert!(matches!(
            store
                .get_with_metadata("metadata", Some("other-version"), 8192)
                .await,
            Err(BoundedGetError::Store(ObjectStoreError {
                kind: PublishFailureKind::IdentityConflict,
                ..
            }))
        ));
        assert_eq!(registry.requests.load(Ordering::Relaxed), 0);
        assert_eq!(fallback.heads.load(Ordering::Relaxed), 0);
    }

    #[tokio::test]
    async fn conflicting_parent_or_planned_bindings_are_rejected_before_io() {
        let registry = missing_registry();
        let first = RegistryObjectMap::new(
            Arc::clone(&registry.client),
            "first".into(),
            &[binding("shared-key", 4, 'a', Some("one"))],
            None,
        )
        .unwrap();
        let second = RegistryObjectMap::new(
            Arc::clone(&registry.client),
            "second".into(),
            &[binding("shared-key", 4, 'b', Some("one"))],
            None,
        )
        .unwrap();
        let (budget, _sender) = budget();
        let output = target(Arc::clone(&registry.client));
        assert!(RegistryGraphStore::new(
            Arc::clone(&output),
            Some(origin()),
            Some(physical(false)),
            vec![Arc::clone(&first), second],
            Vec::new(),
            None,
            budget.clone()
        )
        .is_err());
        assert!(RegistryGraphStore::new(
            Arc::clone(&output),
            Some(origin()),
            Some(physical(false)),
            vec![Arc::clone(&first)],
            vec![binding("shared-key", 5, 'a', None)],
            None,
            budget.clone()
        )
        .is_err());
        let wrong_version = RegistryObjectMap::new(
            Arc::clone(&registry.client),
            "third".into(),
            &[binding("shared-key", 4, 'a', Some("two"))],
            None,
        )
        .unwrap();
        assert!(RegistryGraphStore::new(
            output,
            Some(origin()),
            Some(physical(false)),
            vec![first, wrong_version],
            Vec::new(),
            None,
            budget
        )
        .is_err());
        assert_eq!(registry.requests.load(Ordering::Relaxed), 0);
    }

    #[tokio::test]
    async fn inherited_versions_survive_unversioned_plans_and_parent_ordering() {
        let registry = missing_registry();
        let unversioned = RegistryObjectMap::new(
            Arc::clone(&registry.client),
            "unversioned".into(),
            &[binding("shared-key", 4, 'a', None)],
            None,
        )
        .unwrap();
        let versioned = RegistryObjectMap::new(
            Arc::clone(&registry.client),
            "versioned".into(),
            &[binding(
                "shared-key",
                4,
                'a',
                Some("original-provider-version"),
            )],
            None,
        )
        .unwrap();
        let (budget, _sender) = budget();
        let output = target(Arc::clone(&registry.client));
        let fallback = origin();
        let store = RegistryGraphStore::new(
            Arc::clone(&output),
            Some(fallback.clone()),
            Some(physical(false)),
            vec![Arc::clone(&unversioned), Arc::clone(&versioned)],
            vec![binding("shared-key", 4, 'a', None)],
            None,
            budget.clone(),
        )
        .unwrap();
        assert_eq!(
            store.bindings().unwrap()[0].object_version.as_deref(),
            Some("original-provider-version")
        );
        // A valid inherited version must be accepted even when the newly
        // planned destination is not present yet; this must remain a local miss.
        assert!(store
            .get_with_metadata("shared-key", Some("original-provider-version"), 8)
            .await
            .unwrap()
            .is_none());
        assert_eq!(fallback.heads.load(Ordering::Relaxed), 0);
        let inherited = RegistryGraphStore::new(
            output,
            Some(fallback),
            Some(physical(false)),
            vec![unversioned, versioned],
            Vec::new(),
            None,
            budget,
        )
        .unwrap();
        // The byte bound is evaluated after accepting the authenticated
        // version, without reading either destination or original Registry.
        assert!(matches!(
            inherited
                .get_with_metadata("shared-key", Some("original-provider-version"), 1)
                .await,
            Err(BoundedGetError::BoundExceeded {
                actual_bytes: 4,
                ..
            })
        ));
        assert_eq!(
            inherited.bindings().unwrap()[0].object_version.as_deref(),
            Some("original-provider-version")
        );
        assert_eq!(registry.requests.load(Ordering::Relaxed), 1);
    }

    #[tokio::test]
    async fn copy_sources_pin_sha_size_version_and_reject_short_physical_reads() {
        let reader = physical(false);
        let metadata = RemoteObjectMetadata {
            size: 64,
            sha256: format!("sha256:{}", "a".repeat(64)),
            version: Some("frozen-object-version".into()),
            last_modified_unix: None,
            etag: None,
        };
        let source = CopySource::Physical {
            reader: reader.clone(),
            namespace: "source-bucket".into(),
            key: "inherited/data".into(),
            metadata: metadata.clone(),
        };
        assert_eq!(source.read_at(7, 4).await.unwrap(), vec![7, 8, 9, 10]);
        assert_eq!(
            reader.reads.lock().unwrap()[0],
            Read {
                namespace: "source-bucket".into(),
                key: "inherited/data".into(),
                sha: metadata.sha256.clone(),
                size: 64,
                version: metadata.version.clone(),
                offset: 7,
                length: 4,
            }
        );
        assert!(source.read_at(64, 4).await.is_err());
        assert!(source.read_at(0, 16 * 1024 * 1024 + 1).await.is_err());
        let short = CopySource::Physical {
            reader: physical(true),
            namespace: "source-bucket".into(),
            key: "inherited/data".into(),
            metadata,
        };
        assert_eq!(
            short.read_at(7, 4).await.unwrap_err().kind,
            PublishFailureKind::IdentityConflict
        );
    }

    #[tokio::test]
    async fn shared_permit_waits_observe_cancellation_and_return_admission() {
        let (budget, sender) = budget();
        let held = Arc::clone(&budget.transfer_permits)
            .acquire_owned()
            .await
            .unwrap();
        let waiting = budget.clone();
        let task =
            tokio::spawn(async move { waiting.admit(32, waiting.deadline().unwrap()).await });
        sender.send(true).unwrap();
        assert!(tokio::time::timeout(Duration::from_secs(1), task)
            .await
            .unwrap()
            .unwrap()
            .is_err());
        assert_eq!(budget.buffer_permits.available_permits(), 16);
        drop(held);
        assert_eq!(budget.transfer_permits.available_permits(), 1);

        let (budget, sender) = super::tests::budget();
        let held = Arc::clone(&budget.buffer_permits)
            .acquire_many_owned(16)
            .await
            .unwrap();
        let waiting = budget.clone();
        let task =
            tokio::spawn(async move { waiting.admit(32, waiting.deadline().unwrap()).await });
        tokio::time::timeout(Duration::from_secs(1), async {
            while budget.transfer_permits.available_permits() != 0 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        sender.send(true).unwrap();
        assert!(tokio::time::timeout(Duration::from_secs(1), task)
            .await
            .unwrap()
            .unwrap()
            .is_err());
        assert_eq!(budget.transfer_permits.available_permits(), 1);
        drop(held);
        assert_eq!(budget.buffer_permits.available_permits(), 16);
    }

    #[tokio::test]
    async fn admission_deadline_bounds_a_waiting_copy() {
        let (budget, _sender) = budget();
        let _held = Arc::clone(&budget.transfer_permits)
            .acquire_owned()
            .await
            .unwrap();
        let error = tokio::time::timeout(
            Duration::from_secs(1),
            budget.admit(32, Instant::now() + Duration::from_millis(30)),
        )
        .await
        .unwrap()
        .unwrap_err();
        assert_eq!(error.kind, PublishFailureKind::Retryable);
        assert_eq!(budget.buffer_permits.available_permits(), 16);
    }
}
