//! Physical immutable blob ranges, independent of the stored data format.
//!
//! Sources own transport authentication and bounded range validation. A
//! decoder supplies a pinned namespace, digest and size; the source identity
//! prevents an already authenticated descriptor from being read elsewhere.

use serde::{Deserialize, Serialize};
use thiserror::Error;

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BlobRef {
    pub digest: String,
    pub size: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum BlobRangeErrorKind {
    Invalid,
    Authorization,
    Integrity,
    Transient,
    Permanent,
}

#[derive(Clone, Debug, Error)]
#[error("{message}")]
pub struct BlobRangeError {
    pub kind: BlobRangeErrorKind,
    message: String,
}

impl BlobRangeError {
    pub fn new(kind: BlobRangeErrorKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
        }
    }

    pub fn retryable(&self) -> bool {
        self.kind == BlobRangeErrorKind::Transient
    }
}

// BlobRef is descriptor data only. Registry and object-store reads both use
// object_range::PhysicalRangeReader; there is no second transport read trait.
