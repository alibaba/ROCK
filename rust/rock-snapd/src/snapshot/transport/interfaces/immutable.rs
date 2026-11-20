//! Immutable object-store boundary and namespace mutation fencing.

use std::sync::Arc;

use sha2::{Digest, Sha256};
use thiserror::Error;
use tokio::sync::{watch, OwnedRwLockReadGuard, RwLock};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PublishFailureKind {
    Retryable,
    IdentityConflict,
    Fatal,
}

/// A sealed DATA slice differs from its frozen source identity.
///
/// This is distinct from storage, metadata, or whole-object verification
/// failures: the owning snapshot must stop exporting this source cut.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
#[error("sealed DATA checksum differs for {object_key} at logical offset {logical_offset} raw offset {object_offset} length {length}: expected {expected_sha256}, got {actual_sha256}")]
pub struct DataIntegrityFailure {
    pub object_key: String,
    pub logical_offset: u64,
    pub object_offset: u64,
    pub length: u64,
    pub expected_sha256: String,
    pub actual_sha256: String,
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
#[error("{message}")]
pub struct ObjectStoreError {
    pub kind: PublishFailureKind,
    pub message: String,
    pub abort_cleanup_unconfirmed: bool,
    pub data_integrity: Option<DataIntegrityFailure>,
}

impl ObjectStoreError {
    pub fn new(kind: PublishFailureKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
            abort_cleanup_unconfirmed: false,
            data_integrity: None,
        }
    }

    pub fn source_integrity(failure: DataIntegrityFailure) -> Self {
        Self {
            kind: PublishFailureKind::Fatal,
            message: failure.to_string(),
            abort_cleanup_unconfirmed: false,
            data_integrity: Some(failure),
        }
    }

    pub fn with_abort_cleanup_unconfirmed(mut self) -> Self {
        self.abort_cleanup_unconfirmed = true;
        self
    }

    pub fn retryable(&self) -> bool {
        self.kind == PublishFailureKind::Retryable
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct RemoteObjectMetadata {
    pub size: u64,
    pub sha256: String,
    pub version: Option<String>,
    pub last_modified_unix: Option<f64>,
    pub etag: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct RemoteObject {
    pub payload: Vec<u8>,
    pub metadata: RemoteObjectMetadata,
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum BoundedGetError {
    #[error("immutable object {object_key:?} is {actual_bytes} bytes, exceeding the {max_bytes}-byte read bound")]
    BoundExceeded {
        object_key: String,
        actual_bytes: u64,
        max_bytes: u64,
    },
    #[error("immutable object store does not implement bounded atomic GET for {object_key:?}")]
    Unsupported { object_key: String },
    #[error(transparent)]
    Store(#[from] ObjectStoreError),
}

#[derive(Debug)]
pub struct UploadFenceGuard {
    _guard: OwnedRwLockReadGuard<()>,
}

#[derive(Clone, Debug)]
pub struct UploadTransactionLease {
    fence: Arc<NamespaceMutationFence>,
    _guard: Arc<UploadFenceGuard>,
}

impl UploadTransactionLease {
    pub async fn acquire(fence: Arc<NamespaceMutationFence>) -> Self {
        let guard = Arc::clone(&fence).upload().await;
        Self {
            fence,
            _guard: Arc::new(guard),
        }
    }

    pub fn validates(&self, fence: &Arc<NamespaceMutationFence>) -> bool {
        Arc::ptr_eq(&self.fence, fence)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ImmutableObjectIdentity {
    pub object_key: String,
    pub sha256: String,
    pub length: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct VerifiedWriteReceipt {
    pub object_key: String,
    pub sha256: String,
    pub length: u64,
    pub object_version: Option<String>,
}

pub use crate::snapshot::transport::ReplayableByteSource;

#[derive(Clone, Debug)]
pub struct IoCancellation {
    receiver: watch::Receiver<bool>,
}

impl IoCancellation {
    pub fn new(receiver: watch::Receiver<bool>) -> Self {
        Self { receiver }
    }

    pub fn is_cancelled(&self) -> bool {
        *self.receiver.borrow()
    }
}

pub use crate::snapshot::transport::ImmutableStreamWriter;

/// Opaque, domain-bound capability for Snapshot metadata publication.
///
/// The capture layer can conditionally publish and verify metadata through
/// this handle, but cannot list or delete objects.  Construction is limited
/// to the daemon-owned canonical write-domain registry.
#[derive(Clone)]
pub struct SnapshotMetadataWriteTarget {
    store: Arc<dyn ImmutableObjectStore>,
    write_target_identity: String,
    fence: Arc<NamespaceMutationFence>,
}

impl std::fmt::Debug for SnapshotMetadataWriteTarget {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("SnapshotMetadataWriteTarget")
            .field("namespace", &self.store.namespace())
            .field("write_target_identity", &self.write_target_identity)
            .finish_non_exhaustive()
    }
}

impl SnapshotMetadataWriteTarget {
    pub(crate) fn new(
        store: Arc<dyn ImmutableObjectStore>,
        write_target_identity: String,
        fence: Arc<NamespaceMutationFence>,
    ) -> Result<Self, ObjectStoreError> {
        if write_target_identity.is_empty()
            || store.namespace() != fence.namespace()
            || !Arc::ptr_eq(&store.fence(), &fence)
        {
            return Err(ObjectStoreError::new(
                PublishFailureKind::IdentityConflict,
                "metadata target is outside its canonical write domain",
            ));
        }
        Ok(Self {
            store,
            write_target_identity,
            fence,
        })
    }

    pub(crate) fn namespace(&self) -> &str {
        self.store.namespace()
    }

    pub(crate) fn write_target_identity(&self) -> &str {
        &self.write_target_identity
    }

    pub(crate) fn fence(&self) -> Arc<NamespaceMutationFence> {
        Arc::clone(&self.fence)
    }

    pub(crate) async fn ensure_verified(
        &self,
        object_key: &str,
        payload: &[u8],
        expected_sha256: &str,
        lease: &UploadTransactionLease,
    ) -> Result<(), ObjectStoreError> {
        if !lease.validates(&self.fence) {
            return Err(ObjectStoreError::new(
                PublishFailureKind::IdentityConflict,
                "metadata write lease belongs to another write domain",
            ));
        }
        ensure_immutable_object(self.store.as_ref(), object_key, payload, expected_sha256).await
    }

    pub(crate) async fn head(
        &self,
        object_key: &str,
        lease: &UploadTransactionLease,
    ) -> Result<Option<RemoteObjectMetadata>, ObjectStoreError> {
        if !lease.validates(&self.fence) {
            return Err(ObjectStoreError::new(
                PublishFailureKind::IdentityConflict,
                "metadata read lease belongs to another write domain",
            ));
        }
        self.store.head(object_key).await
    }

    pub(crate) async fn get(
        &self,
        object_key: &str,
        lease: &UploadTransactionLease,
    ) -> Result<Vec<u8>, ObjectStoreError> {
        if !lease.validates(&self.fence) {
            return Err(ObjectStoreError::new(
                PublishFailureKind::IdentityConflict,
                "metadata read lease belongs to another write domain",
            ));
        }
        self.store.get(object_key).await
    }
}

#[derive(Debug)]
pub struct NamespaceMutationFence {
    namespace: String,
    lock: Arc<RwLock<()>>,
}

impl NamespaceMutationFence {
    pub fn new(namespace: impl Into<String>) -> Result<Arc<Self>, ObjectStoreError> {
        let namespace = namespace.into();
        if namespace.is_empty() {
            return Err(ObjectStoreError::new(
                PublishFailureKind::Fatal,
                "fence namespace is required",
            ));
        }
        Ok(Arc::new(Self {
            namespace,
            lock: Arc::new(RwLock::new(())),
        }))
    }

    pub fn namespace(&self) -> &str {
        &self.namespace
    }

    pub async fn upload(self: &Arc<Self>) -> UploadFenceGuard {
        UploadFenceGuard {
            _guard: Arc::clone(&self.lock).read_owned().await,
        }
    }
}

pub use crate::snapshot::transport::ImmutableObjectStore;

pub async fn ensure_immutable_object(
    store: &dyn ImmutableObjectStore,
    object_key: &str,
    payload: &[u8],
    expected_sha256: &str,
) -> Result<(), ObjectStoreError> {
    let actual = format!("sha256:{:x}", Sha256::digest(payload));
    if actual != expected_sha256 {
        return Err(ObjectStoreError::new(
            PublishFailureKind::Fatal,
            format!("local object checksum mismatch for {object_key:?}"),
        ));
    }
    if let Some(existing) = store.head(object_key).await? {
        return validate_existing(store, object_key, payload, expected_sha256, &existing).await;
    }
    if store
        .put_if_absent(object_key, payload, expected_sha256)
        .await?
    {
        return Ok(());
    }
    let existing = store.head(object_key).await?.ok_or_else(|| {
        ObjectStoreError::new(
            PublishFailureKind::Retryable,
            "conditional PUT lost without visible object",
        )
    })?;
    validate_existing(store, object_key, payload, expected_sha256, &existing).await
}

async fn validate_existing(
    store: &dyn ImmutableObjectStore,
    object_key: &str,
    payload: &[u8],
    expected_sha256: &str,
    metadata: &RemoteObjectMetadata,
) -> Result<(), ObjectStoreError> {
    if metadata.size != payload.len() as u64 || metadata.sha256 != expected_sha256 {
        return Err(ObjectStoreError::new(
            PublishFailureKind::IdentityConflict,
            format!("immutable object identity conflict for {object_key:?}"),
        ));
    }
    let remote = store.get(object_key).await?;
    if remote.len() != payload.len()
        || format!("sha256:{:x}", Sha256::digest(&remote)) != expected_sha256
    {
        return Err(ObjectStoreError::new(
            PublishFailureKind::IdentityConflict,
            format!("immutable object checksum conflict for {object_key:?}"),
        ));
    }
    Ok(())
}
