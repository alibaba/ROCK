//! Local immutable raw-layer views. Logical offset equals file offset, but an
//! unallocated hole is never evidence of DATA: the caller supplies the frozen
//! snapshot's explicit DATA coverage. Native reads go through LocalBacking so
//! reclaimed DATA is restored before any pread can observe a punched hole.

mod resident;
pub(crate) use resident::{resident_layer_views, ResidentLayerView};

pub use crate::snapshot::instance::{AdoptionFailure, FrozenLocalLayer, LocalSnapshotError};

use std::sync::Arc;

use async_trait::async_trait;

use crate::snapshot::format::LayerMetadataInterface;
use crate::snapshot::io::backing::{BackingError, LocalBackingHandle};
use crate::snapshot::metadata::{
    LayerKind, LazyLoadContext, LazyLoadError, LazyReadAccounting, LazyReadResult,
    SnapshotLayerMetadata, SnapshotReadRange, MAX_DATA_RANGE_BYTES, MAX_RANGES_PER_LAYER,
};
use crate::snapshot::LazyLoadInterface;

#[derive(Debug)]
pub struct RawLayerReader {
    metadata: SnapshotLayerMetadata,
    kind: LayerKind,
    data_ranges: Vec<(u64, u64)>,
    backing: LocalBackingHandle,
}

impl RawLayerReader {
    pub fn new(
        metadata: SnapshotLayerMetadata,
        kind: LayerKind,
        data_ranges: Vec<(u64, u64)>,
        backing: LocalBackingHandle,
    ) -> Result<Arc<Self>, LazyLoadError> {
        if metadata.format != "raw"
            || metadata.content_identity.is_empty()
            || metadata.logical_bytes == 0
            || metadata.unit_size_bytes == 0
            || metadata.logical_bytes % metadata.unit_size_bytes != 0
            || data_ranges.len() > MAX_RANGES_PER_LAYER
            || backing.backing.kind().as_str()
                != match kind {
                    LayerKind::Memory => "memory",
                    LayerKind::Block => "block",
                }
            || backing.backing.logical_size() != metadata.logical_bytes
            || !backing
                .backing
                .unit_size()
                .is_multiple_of(metadata.unit_size_bytes)
        {
            return Err(LazyLoadError::InvalidRange);
        }
        let mut previous_end = 0;
        for &(offset, length) in &data_ranges {
            let end = offset
                .checked_add(length)
                .ok_or(LazyLoadError::InvalidRange)?;
            if length == 0
                || offset < previous_end
                || end > metadata.logical_bytes
                || offset % metadata.unit_size_bytes != 0
                || length % metadata.unit_size_bytes != 0
            {
                return Err(LazyLoadError::InvalidRange);
            }
            previous_end = end;
        }
        Ok(Arc::new(Self {
            metadata,
            kind,
            data_ranges,
            backing,
        }))
    }

    fn validate_data_request(&self, request: SnapshotReadRange<'_>) -> Result<(), LazyLoadError> {
        if request.kind != self.kind
            || request.layer_content_sha256.trim_start_matches("sha256:")
                != self.metadata.content_identity.trim_start_matches("sha256:")
        {
            return Err(LazyLoadError::IdentityConflict);
        }
        let end = request
            .logical_offset
            .checked_add(request.length)
            .filter(|end| {
                request.length > 0
                    && request.length <= MAX_DATA_RANGE_BYTES
                    && *end <= self.metadata.logical_bytes
            })
            .ok_or(LazyLoadError::InvalidRange)?;
        let mut cursor = request.logical_offset;
        let first = self
            .data_ranges
            .partition_point(|(start, length)| start + length <= cursor);
        for &(start, count) in &self.data_ranges[first..] {
            if cursor >= end {
                break;
            }
            if start > cursor {
                return Err(LazyLoadError::NonDataRange);
            }
            cursor = start + count;
        }
        if cursor < end {
            return Err(LazyLoadError::NonDataRange);
        }
        Ok(())
    }

    /// Peer reads reuse the same logical DATA authorization, but a missing
    /// native unit is a miss rather than a request to restore or fetch it.
    pub(crate) async fn read_resident_data(
        &self,
        request: SnapshotReadRange<'_>,
        known_complete: bool,
    ) -> Result<Option<Vec<u8>>, LazyLoadError> {
        self.validate_data_request(request)?;
        self.backing
            .backing
            .read_resident_data(request.logical_offset, request.length, known_complete)
            .await
            .or_else(|error| match error {
                BackingError::BackingClosed => Ok(None),
                error => Err(local_io(error)),
            })
    }
}

impl LayerMetadataInterface for RawLayerReader {
    fn format_name(&self) -> &'static str {
        "raw"
    }
    fn layer_kind(&self) -> LayerKind {
        self.kind
    }
    fn content_identity(&self) -> &str {
        &self.metadata.content_identity
    }
    fn logical_bytes(&self) -> u64 {
        self.metadata.logical_bytes
    }
    fn unit_size_bytes(&self) -> u64 {
        self.metadata.unit_size_bytes
    }
    fn allows_p2p(&self) -> bool {
        false
    }
    fn allows_background_prefetch(&self) -> bool {
        false
    }
}

#[async_trait]
impl LazyLoadInterface for RawLayerReader {
    async fn read_data_range(
        &self,
        _context: &LazyLoadContext,
        request: SnapshotReadRange<'_>,
    ) -> Result<LazyReadResult, LazyLoadError> {
        self.validate_data_request(request)?;
        let payload = self
            .backing
            .backing
            .read_data(request.logical_offset, request.length)
            .await
            .map_err(|error| match error {
                BackingError::Io(error) if error.kind() == std::io::ErrorKind::UnexpectedEof => {
                    LazyLoadError::ShortRead
                }
                error => local_io(error),
            })?;
        Ok(LazyReadResult {
            accounting: LazyReadAccounting {
                cache_bytes: payload.len() as u64,
                ..Default::default()
            },
            payload,
        })
    }
}

fn local_io(error: impl std::fmt::Display) -> LazyLoadError {
    LazyLoadError::LocalIo(error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::snapshot::io::range_io::{RangeReadRequest, RangeReadResult, RangeReader};
    use crate::snapshot::repository::frozen_layer::{
        FrozenLayerMetadata, FrozenLayerPin, PackedOwnershipMap,
    };
    use crate::snapshot::transport::RangeReadError;
    use crate::RuntimeIdentity;
    use std::fs::File;
    use std::os::unix::fs::FileExt;
    use std::sync::atomic::{AtomicUsize, Ordering};

    struct NoProvider(AtomicUsize);
    #[async_trait]
    impl RangeReader for NoProvider {
        async fn read_range(
            &self,
            _request: RangeReadRequest<'_>,
        ) -> Result<RangeReadResult, RangeReadError> {
            self.0.fetch_add(1, Ordering::Relaxed);
            panic!("raw reads must not use a provider")
        }
    }
    fn fixture() -> (SnapshotLayerMetadata, LocalBackingHandle) {
        let path = std::env::temp_dir().join(format!("snapd-raw-{}", uuid::Uuid::new_v4()));
        let file = File::options()
            .create_new(true)
            .read(true)
            .write(true)
            .open(&path)
            .unwrap();
        std::fs::remove_file(path).unwrap();
        file.set_len(12288).unwrap();
        file.write_all_at(&vec![0x71; 4096], 4096).unwrap();
        (
            SnapshotLayerMetadata {
                ordinal: 0,
                layer_id: "raw-local".into(),
                content_identity: "provisional-snapshot:1".into(),
                format: "raw".into(),
                logical_bytes: 12288,
                unit_size_bytes: 4096,
            },
            LocalBackingHandle::from_native(FrozenLayerPin::new(
                FrozenLayerMetadata {
                    kind: LayerKind::Memory,
                    layer_id: "raw-local".into(),
                    provisional_content_id: "provisional-snapshot:1".into(),
                    logical_size: 12288,
                    alignment: 4096,
                },
                PackedOwnershipMap {
                    kind: LayerKind::Memory,
                    snapshot_id: "raw".into(),
                    format_version: 1,
                    logical_size: 12288,
                    granularity: 4096,
                    packed_states: vec![4],
                },
                file.into(),
                false,
            ))
            .unwrap(),
        )
    }

    #[tokio::test]
    async fn raw_reader_uses_logical_file_offsets_and_never_fetches_a_provider() {
        let (metadata, fd) = fixture();
        let reader =
            RawLayerReader::new(metadata, LayerKind::Memory, vec![(4096, 4096)], fd).unwrap();
        let provider = Arc::new(NoProvider(AtomicUsize::new(0)));
        let context = LazyLoadContext::new(provider.clone(), RuntimeIdentity::new("raw"), None);
        let request = SnapshotReadRange {
            kind: LayerKind::Memory,
            layer_content_sha256: "provisional-snapshot:1",
            logical_offset: 4096,
            length: 4096,
        };
        assert_eq!(
            reader
                .read_data_range(&context, request)
                .await
                .unwrap()
                .payload,
            vec![0x71; 4096]
        );
        for (offset, length) in [(0, 4096), (8192, 4096), (4096, 8192)] {
            assert!(matches!(
                reader
                    .read_data_range(
                        &context,
                        SnapshotReadRange {
                            logical_offset: offset,
                            length,
                            ..request
                        }
                    )
                    .await,
                Err(LazyLoadError::NonDataRange)
            ));
        }
        assert_eq!(provider.0.load(Ordering::Relaxed), 0);
    }

    #[tokio::test]
    async fn raw_reader_rejects_truncation_after_open_without_returning_zero_padding() {
        let (metadata, fd) = fixture();
        let shrink = fd.backing.try_clone_memory_mapping_file().unwrap();
        let reader =
            RawLayerReader::new(metadata, LayerKind::Memory, vec![(4096, 4096)], fd).unwrap();
        // The initial read can return 2048 bytes; the following pread reaches
        // EOF. The result must remain an error, not a zero-padded DATA buffer.
        shrink.set_len(6144).unwrap();
        let provider = Arc::new(NoProvider(AtomicUsize::new(0)));
        let context = LazyLoadContext::new(
            provider.clone(),
            RuntimeIdentity::new("raw-truncated"),
            None,
        );
        assert!(matches!(
            reader
                .read_data_range(
                    &context,
                    SnapshotReadRange {
                        kind: LayerKind::Memory,
                        layer_content_sha256: "provisional-snapshot:1",
                        logical_offset: 4096,
                        length: 4096,
                    }
                )
                .await,
            Err(LazyLoadError::ShortRead)
        ));
        assert_eq!(provider.0.load(Ordering::Relaxed), 0);
    }

    #[test]
    fn raw_reader_rejects_overlap_out_of_bounds_and_wrong_file_geometry() {
        for ranges in [
            vec![(4096, 8192), (8192, 4096)],
            vec![(8192, 8192)],
            vec![(1, 4096)],
        ] {
            let (metadata, fd) = fixture();
            assert!(matches!(
                RawLayerReader::new(metadata, LayerKind::Block, ranges, fd),
                Err(LazyLoadError::InvalidRange)
            ));
        }
        let (mut metadata, fd) = fixture();
        metadata.logical_bytes = 16384;
        assert!(matches!(
            RawLayerReader::new(metadata, LayerKind::Block, vec![], fd),
            Err(LazyLoadError::InvalidRange)
        ));
    }
}
