//! Shared publication results, errors and finite transaction capabilities.
//! Snapshot formats construct artifacts; SnapshotPublisher owns execution.

use crate::snapshot::metadata::{SnapshotManifest, SnapshotSpec};
use crate::snapshot::transport::ObjectStoreError;
use thiserror::Error;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UploadedSnapshot {
    pub spec: SnapshotSpec,
    pub manifest: SnapshotManifest,
    pub(crate) registry_objects:
        Option<std::sync::Arc<crate::snapshot::format::overlaybd_oci::RegistryObjectMap>>,
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum SnapshotUploadError {
    #[error(transparent)]
    SourceIntegrity(crate::snapshot::transport::DataIntegrityFailure),
    #[error("invalid upload plan: {0}")]
    InvalidPlan(String),
    #[error("upload identity conflict: {0}")]
    IdentityConflict(String),
    #[error(transparent)]
    Store(#[from] ObjectStoreError),
    #[error("uploader is closed")]
    Closed,
    #[error("upload worker terminated: {0}")]
    Worker(String),
    #[error("snapshot DATA upload failed: {message}")]
    Data { message: String, retryable: bool },
}

impl SnapshotUploadError {
    pub fn retryable(&self) -> bool {
        matches!(self, Self::Store(error) if error.retryable())
            || matches!(
                self,
                Self::Data {
                    retryable: true,
                    ..
                }
            )
    }
}

/// Retained transport capabilities for one finite read-back validation.
/// Constructed by snapshot I/O for its active transaction; no scheduler state escapes.
pub struct SnapshotPublicationContext<'a> {
    pub(crate) target: &'a crate::snapshot::transport::SnapshotMetadataWriteTarget,
    pub(crate) lease: &'a crate::snapshot::transport::UploadTransactionLease,
    pub(crate) resolver: &'a dyn crate::snapshot::SourceResolver,
    pub(crate) range_io: &'a dyn crate::snapshot::io::range_io::RangeWriter,
}
impl<'a> SnapshotPublicationContext<'a> {
    pub(crate) fn new(
        target: &'a crate::snapshot::transport::SnapshotMetadataWriteTarget,
        lease: &'a crate::snapshot::transport::UploadTransactionLease,
        resolver: &'a dyn crate::snapshot::SourceResolver,
        range_io: &'a dyn crate::snapshot::io::range_io::RangeWriter,
    ) -> Self {
        Self {
            target,
            lease,
            resolver,
            range_io,
        }
    }
}
