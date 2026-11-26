//! Lazy logical DATA capability shared by OverlayBD descriptor profiles.

use crate::snapshot::format::reader::MappedLayerReader;
use crate::snapshot::format::LayerMetadataInterface;
use crate::snapshot::metadata::{
    LayerKind, LayerRangeMap, LazyLoadContext, LazyLoadError, LazyReadResult, SnapshotReadRange,
};
use crate::snapshot::LazyLoadInterface;
use async_trait::async_trait;
use std::sync::Arc;

#[derive(Debug)]
pub struct OverlaybdLayerReader {
    inner: MappedLayerReader,
    peer_reads: bool,
}

impl OverlaybdLayerReader {
    pub(crate) fn new(map: Arc<LayerRangeMap>, format_name: &'static str) -> Self {
        Self {
            inner: MappedLayerReader { map, format_name },
            peer_reads: true,
        }
    }

    /// The external descriptor binds trusted source + block CRC integrity,
    /// rather than the per-range SHA profile required by authenticated peers.
    pub(crate) fn external(map: Arc<LayerRangeMap>, format_name: &'static str) -> Self {
        Self {
            inner: MappedLayerReader { map, format_name },
            peer_reads: false,
        }
    }
}

impl LayerMetadataInterface for OverlaybdLayerReader {
    fn format_name(&self) -> &'static str {
        self.inner.format_name
    }
    fn layer_kind(&self) -> LayerKind {
        self.inner.map.kind
    }
    fn content_identity(&self) -> &str {
        &self.inner.map.content_sha256
    }
    fn logical_bytes(&self) -> u64 {
        self.inner.map.logical_bytes
    }
    fn unit_size_bytes(&self) -> u64 {
        self.inner.map.unit_size_bytes
    }
    fn allows_p2p(&self) -> bool {
        self.peer_reads && self.inner.map.registry.is_none()
    }
    fn allows_background_prefetch(&self) -> bool {
        self.peer_reads && self.inner.map.registry.is_none()
    }
}

#[async_trait]
impl LazyLoadInterface for OverlaybdLayerReader {
    async fn read_data_range(
        &self,
        context: &LazyLoadContext,
        request: SnapshotReadRange<'_>,
    ) -> Result<LazyReadResult, LazyLoadError> {
        self.inner.read_data_range(context, request).await
    }
}
