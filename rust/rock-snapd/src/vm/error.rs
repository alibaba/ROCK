//! Common operation failures and stable control-plane error projections.

use crate::snapshot::image::service::BaseImageIngestionError;
use crate::snapshot::io::snapshot_io::SnapshotLoadError;
use crate::snapshot::services::background_uploader::SnapshotUploadServiceError;
use crate::vm::repository::RuntimeStartError;
use crate::vm::repository::SnapshotCaptureError;
use crate::vm::runtime::prepare::SnapshotRestoreError;
use thiserror::Error;

#[derive(Clone, Debug, Error, Eq, PartialEq)]
pub enum OperationError {
    #[error("snapshot upload is disabled; select an upload_format to enable publication")]
    UploadDisabled,
    #[error("invalid snapshot control request: {0}")]
    Invalid(String),
    #[error("snapshot control identity conflict: {0}")]
    IdentityConflict(String),
    #[error("snapshot control identity conflict: {0}")]
    RetryableIdentityConflict(String),
    #[error("snapshot control object was not found: {0}")]
    NotFound(String),
    #[error("snapshot control state transition is invalid: {0}")]
    InvalidState(String),
    #[error("snapshot control state transition is invalid: {0}")]
    RetryableInvalidState(String),
    #[error("snapshot control operation deadline expired: {0}")]
    Deadline(String),
    #[error("snapshot control runtime operation failed: {0}")]
    Runtime(String),
}

impl OperationError {
    pub const fn retryable(&self) -> bool {
        matches!(
            self,
            Self::RetryableIdentityConflict(_)
                | Self::RetryableInvalidState(_)
                | Self::Deadline(_)
                | Self::Runtime(_)
        )
    }

    pub const fn code(&self) -> &'static str {
        match self {
            Self::UploadDisabled => "UPLOAD_DISABLED",
            Self::Invalid(_) => "INVALID_ARGUMENT",
            Self::IdentityConflict(_) | Self::RetryableIdentityConflict(_) => "IDENTITY_CONFLICT",
            Self::NotFound(_) => "NOT_FOUND",
            Self::InvalidState(_) | Self::RetryableInvalidState(_) => "INVALID_STATE",
            Self::Deadline(_) => "DEADLINE_EXCEEDED",
            Self::Runtime(_) => "INTERNAL",
        }
    }
}

impl From<RuntimeStartError> for OperationError {
    fn from(error: RuntimeStartError) -> Self {
        match error {
            RuntimeStartError::Invalid(message) => Self::Invalid(message),
            RuntimeStartError::IdentityConflict(message) => Self::IdentityConflict(message),
            RuntimeStartError::NotFound(message) => Self::NotFound(message),
            RuntimeStartError::InvalidState(message) => Self::InvalidState(message),
            other => Self::Runtime(other.to_string()),
        }
    }
}

impl From<SnapshotCaptureError> for OperationError {
    fn from(error: SnapshotCaptureError) -> Self {
        match error {
            SnapshotCaptureError::Invalid(message) => Self::Invalid(message),
            SnapshotCaptureError::IdentityConflict(message) => Self::IdentityConflict(message),
            SnapshotCaptureError::Deadline(message) => Self::Deadline(message),
            SnapshotCaptureError::Terminal(message) | SnapshotCaptureError::Protocol(message) => {
                Self::InvalidState(message)
            }
            SnapshotCaptureError::Vmm(message)
            | SnapshotCaptureError::ResultUnknown(message)
            | SnapshotCaptureError::Conversion(message) => Self::Runtime(message),
        }
    }
}

pub(crate) fn snapshot_load_error(error: SnapshotLoadError) -> OperationError {
    match error {
        SnapshotLoadError::InvalidSnapshotMetadata(message)
        | SnapshotLoadError::BoundExceeded(message) => OperationError::Invalid(message),
        SnapshotLoadError::IdentityConflict(message) => OperationError::IdentityConflict(message),
        SnapshotLoadError::MissingObject(message) => OperationError::NotFound(message),
        SnapshotLoadError::Invalidated(message) => OperationError::RetryableInvalidState(message),
        SnapshotLoadError::Store(error) => OperationError::Runtime(error.to_string()),
    }
}

pub(crate) fn snapshot_restore_error(error: SnapshotRestoreError) -> OperationError {
    match error {
        SnapshotRestoreError::Invalid(message) => OperationError::Invalid(message),
        SnapshotRestoreError::Runtime(error) => error.into(),
        SnapshotRestoreError::RemoteLoad(error) => OperationError::Runtime(error.to_string()),
        SnapshotRestoreError::Backing(error) => OperationError::Runtime(error.to_string()),
        SnapshotRestoreError::Io(error) => OperationError::Runtime(error.to_string()),
    }
}

impl From<SnapshotUploadServiceError> for OperationError {
    fn from(error: SnapshotUploadServiceError) -> Self {
        match error {
            SnapshotUploadServiceError::UploadDisabled => Self::UploadDisabled,
            SnapshotUploadServiceError::SourceIntegrity(failure) => {
                Self::InvalidState(failure.to_string())
            }
            SnapshotUploadServiceError::Invalid(message) => Self::Invalid(message),
            SnapshotUploadServiceError::IdentityConflict(message) => {
                Self::IdentityConflict(message)
            }
            SnapshotUploadServiceError::InvalidState(message) => Self::InvalidState(message),
            SnapshotUploadServiceError::NotFound(message) => Self::NotFound(message),
            SnapshotUploadServiceError::Closed => {
                Self::InvalidState("upload service is closed".into())
            }
            SnapshotUploadServiceError::Publish { message, .. } => Self::Runtime(message),
        }
    }
}

impl From<BaseImageIngestionError> for OperationError {
    fn from(error: BaseImageIngestionError) -> Self {
        match error {
            BaseImageIngestionError::Invalid(message) => Self::Invalid(message),
            BaseImageIngestionError::IdentityConflict(message) => Self::IdentityConflict(message),
            BaseImageIngestionError::InvalidState(message) => Self::InvalidState(message),
            BaseImageIngestionError::NotFound(message) => Self::NotFound(message),
            BaseImageIngestionError::Closed => {
                Self::InvalidState("BASE_IMAGE ingestion is closed".into())
            }
            BaseImageIngestionError::Build(error) if error.code == "IDENTITY_CONFLICT" => {
                Self::IdentityConflict(error.message)
            }
            BaseImageIngestionError::Build(error)
                if matches!(
                    error.code.as_str(),
                    "INVALID_ARGUMENT"
                        | "BASE_IMAGE_OCI_REF_INVALID"
                        | "BASE_IMAGE_PLATFORM_INVALID"
                        | "OCI_DIGEST_INVALID"
                        | "BASE_IMAGE_BUILD_KEY_INVALID"
                ) =>
            {
                Self::Invalid(error.message)
            }
            BaseImageIngestionError::Build(error) if error.retryable => {
                Self::Runtime(error.message)
            }
            BaseImageIngestionError::Build(error) => {
                Self::InvalidState(format!("{}: {}", error.code, error.message))
            }
            BaseImageIngestionError::Upload(error) => error.into(),
        }
    }
}
