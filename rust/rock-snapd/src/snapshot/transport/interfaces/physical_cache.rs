//! Optional cache of original physical object bytes, without format decoding.

use std::io;
use std::sync::Arc;

use async_trait::async_trait;
use serde::{Deserialize, Serialize};

use super::object_range::PhysicalRangeRequest;
use crate::snapshot::transport::{RangeReadError, RangeReadFailureClass};

/// A cache capability is bound to the actually selected transport, never a
/// caller-provided redirect. Optional size does not weaken digest/version identity.
#[derive(Clone, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PhysicalCacheKey {
    pub source_id: String,
    pub namespace: Option<String>,
    pub object_key: String,
    pub object_sha256: String,
    pub object_size: Option<u64>,
    pub object_version: Option<String>,
    pub offset: u64,
    pub length: u64,
}

impl PhysicalCacheKey {
    pub fn from_request(
        source_id: &str,
        request: PhysicalRangeRequest<'_>,
    ) -> Result<Self, RangeReadError> {
        request.validate()?;
        if source_id.is_empty() || source_id.len() > 65536 {
            return Err(RangeReadError::failure(
                RangeReadFailureClass::Integrity,
                "physical cache source identity is empty or too long",
            ));
        }
        Ok(Self {
            source_id: source_id.into(),
            namespace: request.namespace.map(str::to_owned),
            object_key: request.object_key.into(),
            object_sha256: request
                .object_sha256
                .strip_prefix("sha256:")
                .unwrap_or(request.object_sha256)
                .into(),
            object_size: request.object_size,
            object_version: request.object_version.map(str::to_owned),
            offset: request.offset,
            length: request.length,
        })
    }

    pub fn validate(&self) -> Result<(), RangeReadError> {
        Self::from_request(
            &self.source_id,
            PhysicalRangeRequest {
                namespace: self.namespace.as_deref(),
                object_key: &self.object_key,
                object_sha256: &self.object_sha256,
                object_size: self.object_size,
                object_version: self.object_version.as_deref(),
                offset: self.offset,
                length: self.length,
            },
        )
        .map(|_| ())
    }
}

/// Cache failures are advisory: the I/O coordinator decides whether to bypass
/// them. Formats must still authenticate decoded DATA after a physical hit.
#[async_trait]
pub trait PhysicalRangeCache: std::fmt::Debug + Send + Sync {
    async fn read(&self, key: &PhysicalCacheKey) -> io::Result<Option<Vec<u8>>>;
    /// The backend must release this Arc before returning. Coordinators move
    /// the provider Vec through it and recover the same allocation afterwards.
    async fn write(&self, key: &PhysicalCacheKey, payload: Arc<Vec<u8>>) -> io::Result<()>;
    async fn invalidate(&self, key: &PhysicalCacheKey) -> io::Result<()>;
}
