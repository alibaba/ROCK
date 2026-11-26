//! Physical byte transports and local disk policy.
//!
//! S3 and Registry share [`PhysicalRangeReader`]. Immutable remote publication
//! uses [`ImmutableStreamWriter`]; local staged random writes use
//! [`MutableRangeWriter`]. The public contracts are defined in this module;
//! `interfaces` retains supporting types and compatible former import paths.
//! The peer adapter is an explicitly logical decoded-DATA path, composed above
//! format decoding.

pub mod interfaces;
pub mod p2p;
pub(crate) mod posix_file;
pub mod registry;
pub mod s3;

pub(crate) use posix_file::create_temporary_staged_object as temporary_staged_object;

use std::future::Future;
use std::io;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Instant;

use async_trait::async_trait;

pub use self::interfaces::blob::{BlobRangeError, BlobRangeErrorKind, BlobRef};
pub use self::interfaces::immutable::{
    ensure_immutable_object, BoundedGetError, DataIntegrityFailure, ImmutableObjectIdentity,
    IoCancellation, NamespaceMutationFence, ObjectStoreError, PublishFailureKind, RemoteObject,
    RemoteObjectMetadata, SnapshotMetadataWriteTarget, UploadFenceGuard, UploadTransactionLease,
    VerifiedWriteReceipt,
};
pub use self::interfaces::metadata::MAX_METADATA_OBJECT_BYTES;
pub use self::interfaces::object_range::PhysicalRangeRequest;
pub use self::interfaces::physical_cache::{PhysicalCacheKey, PhysicalRangeCache};
pub use self::interfaces::read::{
    RangeReadError, RangeReadFailureClass, RangeReadFallbackClass, RangeReadFallbackSource,
};
pub use self::interfaces::upload::{
    DataPackSourceError, DataPackUploadReceipt, DataPackWriteTarget, SnapshotDataUploadError,
};

// Transport contracts exchange physical bytes and receipts, without format parsing.
#[async_trait]
pub trait PhysicalRangeReader: Send + Sync {
    /// Fixed transport authority. The I/O coordinator binds the selected
    /// backend to a pinned source before passing physical object plans down.
    fn source_id(&self) -> &str;

    /// Return exactly the requested physical bytes or a typed transport error.
    /// Decoding and logical DATA authentication belong to the format layer.
    async fn read_physical_range(
        &self,
        request: PhysicalRangeRequest<'_>,
    ) -> Result<Vec<u8>, RangeReadError>;
}

pub trait ReplayableByteSource: Send + Sync {
    fn length(&self) -> u64;

    fn read_at<'a>(
        &'a self,
        offset: u64,
        max_bytes: usize,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<u8>, ObjectStoreError>> + Send + 'a>>;
}

/// Publish a complete, content-identified immutable object and return a
/// verified receipt. This is deliberately separate from local random writes:
/// partial data and staging completion must never imply remote publication.
#[async_trait]
pub trait ImmutableStreamWriter: Send + Sync + std::fmt::Debug {
    fn namespace(&self) -> &str;
    fn fence(&self) -> Arc<NamespaceMutationFence>;
    fn write_domain_id(&self) -> &str;

    async fn ensure_stream_verified(
        &self,
        identity: &ImmutableObjectIdentity,
        source: Arc<dyn ReplayableByteSource>,
        lease: UploadTransactionLease,
        deadline: Instant,
        cancellation: IoCancellation,
        chunk_bytes: usize,
    ) -> Result<VerifiedWriteReceipt, ObjectStoreError>;
}

#[async_trait]
pub trait ImmutableObjectStore: Send + Sync + std::fmt::Debug {
    fn namespace(&self) -> &str;
    fn fence(&self) -> Arc<NamespaceMutationFence>;

    async fn head(
        &self,
        object_key: &str,
    ) -> Result<Option<RemoteObjectMetadata>, ObjectStoreError>;
    async fn put_if_absent(
        &self,
        object_key: &str,
        payload: &[u8],
        sha256: &str,
    ) -> Result<bool, ObjectStoreError>;
    /// Fetch an immutable object and the identity metadata carried by the same
    /// provider response. When `object_version` is present, the provider must
    /// address that exact immutable version rather than the current object.
    async fn get_with_metadata(
        &self,
        object_key: &str,
        object_version: Option<&str>,
        max_bytes: u64,
    ) -> Result<Option<RemoteObject>, BoundedGetError> {
        let _ = (object_version, max_bytes);
        // Keep third-party implementations source-compatible without silently
        // falling back to an unbounded HEAD -> GET sequence. Implementations
        // must opt in once they can enforce the payload bound while reading.
        Err(BoundedGetError::Unsupported {
            object_key: object_key.to_owned(),
        })
    }
    async fn get(&self, object_key: &str) -> Result<Vec<u8>, ObjectStoreError>;
}

pub trait MutableRangeWriter: Send + Sync {
    /// Blocking local I/O; async callers run this under their blocking gate.
    /// Return only after every byte has been written at the physical offset.
    fn write_all_at(&self, payload: &[u8], offset: u64) -> io::Result<()>;
}

/// Format-independent destination for one bounded, unpublished physical object.
/// Writers retain ownership until validation and atomic local publication succeed.
pub trait StagedObjectWriter: MutableRangeWriter + std::fmt::Debug {
    fn length(&self) -> io::Result<u64>;

    /// Blocking local I/O; callers execute sealing on their blocking workers.
    fn seal(
        &self,
        identity: &ImmutableObjectIdentity,
        validator: &dyn StagedObjectValidator,
    ) -> Result<Arc<dyn SealedObject>, ObjectStoreError>;
}

/// The format authenticates its layout; transport authenticates physical SHA/size.
pub trait StagedObjectValidator: Send + Sync {
    fn validate_file(&self, file: &std::fs::File) -> Result<(), ObjectStoreError>;
}

/// A replayable, read-only physical object. A live handle pins the local inode.
pub trait SealedObject: ReplayableByteSource + std::fmt::Debug {
    fn file(&self) -> &std::fs::File;
    fn mark_remote_committed(&self);
}

pub(crate) trait DataPackSource: Send + Sync {
    fn length(&self) -> u64;

    fn mark_remote_committed(&self) {}

    fn read_at<'a>(
        &'a self,
        offset: u64,
        max_bytes: usize,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<u8>, DataPackSourceError>> + Send + 'a>>;
}

#[async_trait]
pub trait MetadataObjectStore: std::fmt::Debug + Send + Sync {
    /// Identity of the configured namespace; request payloads cannot select it.
    fn namespace(&self) -> &str;

    /// Read bytes and provider identity from the same bounded response.
    async fn get_bounded(
        &self,
        object_key: &str,
        max_bytes: u64,
    ) -> Result<Option<RemoteObject>, BoundedGetError>;

    /// Replace one complete metadata object. Concurrent successful writers use
    /// last-writer-wins semantics; immutable objects use ImmutableObjectStore.
    async fn replace(
        &self,
        object_key: &str,
        payload: &[u8],
        expected_sha256: &str,
    ) -> Result<(), ObjectStoreError>;
}
