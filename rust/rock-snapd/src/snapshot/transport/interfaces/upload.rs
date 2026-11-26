//! Physical replayable upload sources, immutable targets and write receipts.
//! Scheduling stays in core; this module never interprets logical guest ranges.
use crate::snapshot::transport::ImmutableStreamWriter;
use std::sync::Arc;
use thiserror::Error;
#[derive(Debug, Error, Clone, Eq, PartialEq)]
pub enum DataPackSourceError {
    #[error("DATA source I/O failed: {0}")]
    Io(String),
    #[error("DATA source range is invalid")]
    InvalidRange,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DataPackUploadReceipt {
    pub object_key: String,
    pub sha256: String,
    pub length: u64,
    pub object_version: Option<String>,
}

#[derive(Clone)]
pub struct DataPackWriteTarget {
    pub(crate) writer: Arc<dyn ImmutableStreamWriter>,
    write_target_identity: String,
}

impl std::fmt::Debug for DataPackWriteTarget {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("DataPackWriteTarget")
            .field("write_target_identity", &self.write_target_identity)
            .finish_non_exhaustive()
    }
}

impl DataPackWriteTarget {
    pub(crate) fn new(
        writer: Arc<dyn ImmutableStreamWriter>,
        write_target_identity: String,
        fence: Arc<crate::snapshot::transport::NamespaceMutationFence>,
    ) -> Result<Self, SnapshotDataUploadError> {
        if write_target_identity.is_empty()
            || writer.write_domain_id() != write_target_identity
            || writer.namespace() != fence.namespace()
            || !Arc::ptr_eq(&writer.fence(), &fence)
        {
            return Err(SnapshotDataUploadError::IdentityConflict(
                "DATA target is outside its canonical write domain".into(),
            ));
        }
        Ok(Self {
            writer,
            write_target_identity,
        })
    }

    pub(crate) fn namespace(&self) -> &str {
        self.writer.namespace()
    }

    pub(crate) fn fence(&self) -> Arc<crate::snapshot::transport::NamespaceMutationFence> {
        self.writer.fence()
    }

    pub(crate) fn write_target_identity(&self) -> &str {
        &self.write_target_identity
    }
}

#[derive(Debug, Error, Clone, Eq, PartialEq)]
pub enum SnapshotDataUploadError {
    #[error("invalid DATA upload request: {0}")]
    Invalid(String),
    #[error("DATA upload identity conflict: {0}")]
    IdentityConflict(String),
    #[error("DATA upload resources are exhausted")]
    Busy,
    #[error("DATA upload was cancelled")]
    Cancelled,
    #[error("DATA upload timed out")]
    Timeout,
    #[error("DATA upload backend failed: {message}")]
    Backend {
        message: String,
        retryable: bool,
        abort_cleanup_unconfirmed: bool,
    },
    #[error("DATA upload worker terminated: {0}")]
    Worker(String),
    #[error("DATA upload service is closed")]
    Closed,
}

impl SnapshotDataUploadError {
    pub(crate) fn retryable(&self) -> bool {
        matches!(
            self,
            Self::Busy
                | Self::Cancelled
                | Self::Timeout
                | Self::Backend {
                    retryable: true,
                    ..
                }
                | Self::Worker(_)
        )
    }
}
