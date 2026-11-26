//! Borrowed logical raw views. Owners, FDs, dependency counters and residency
//! remain with Snapshot/LocalBacking; this layer adds no lifetime or byte cache.

use super::*;
use std::fs::File;
use std::os::unix::fs::MetadataExt;

use crate::snapshot::instance::{FrozenLocalLayer, Snapshot};
use crate::snapshot::io::backing::{LocalBacking, LocalBackingHandle};
use crate::snapshot::metadata::{LayerRef, RangeKind};
use crate::snapshot::repository::lineage::{
    RemoteLayerSource, SnapshotLineageEntry, SourceResidency,
};

#[derive(Clone, Copy)]
pub(crate) enum ResidentLayerView<'a> {
    Frozen {
        owner: &'a Snapshot,
        layer: &'a FrozenLocalLayer,
    },
    Remote(&'a RemoteLayerSource),
    MissingRemote {
        kind: LayerKind,
        layer: &'a LayerRef,
        logical_bytes: u64,
        unit_size: u64,
    },
}

pub(crate) struct RawCoveragePage {
    pub offset: u64,
    pub length: u64,
    pub unit_size: u64,
    pub packed_states: Vec<u8>,
}

#[derive(Default)]
pub(crate) struct RawResidency {
    pub any_owned: bool,
    pub complete: bool,
}

pub(crate) fn resident_layer_views(
    snapshot: &Snapshot,
    kind: LayerKind,
) -> Vec<ResidentLayerView<'_>> {
    let mut result = Vec::new();
    for entry in snapshot.lineage().entries() {
        match entry {
            SnapshotLineageEntry::FrozenLocal(parent) => {
                if let Some(layer) = parent.frozen_layers.get(&kind) {
                    result.push(ResidentLayerView::Frozen {
                        owner: parent,
                        layer,
                    });
                }
            }
            SnapshotLineageEntry::Remote {
                kind: source_kind,
                chain_index,
                layers,
            } if *source_kind == kind => {
                for layer in &chain_index.layers {
                    result.push(
                        layers
                            .iter()
                            .find(|source| u64::from(source.ordinal) == layer.ordinal)
                            .map(ResidentLayerView::Remote)
                            .unwrap_or(ResidentLayerView::MissingRemote {
                                kind,
                                layer,
                                logical_bytes: chain_index.logical_bytes,
                                unit_size: chain_index.unit_size_bytes,
                            }),
                    );
                }
            }
            _ => {}
        }
    }
    if let Some(layer) = snapshot.frozen_layers.get(&kind) {
        result.push(ResidentLayerView::Frozen {
            owner: snapshot,
            layer,
        });
    }
    result
}

impl ResidentLayerView<'_> {
    fn metadata(self, ordinal: u32) -> SnapshotLayerMetadata {
        let (layer_id, content_identity, logical_bytes, unit_size_bytes) = match self {
            Self::Frozen { layer, .. } => (
                &layer.layer_id,
                &layer.provisional_content_id,
                layer.logical_bytes,
                layer.unit_size_bytes,
            ),
            Self::Remote(layer) => (
                &layer.layer_id,
                &layer.range_map.content_sha256,
                layer.range_map.logical_bytes,
                layer.range_map.unit_size_bytes,
            ),
            Self::MissingRemote {
                layer,
                logical_bytes,
                unit_size,
                ..
            } => (
                &layer.layer_id,
                &layer.content_sha256,
                logical_bytes,
                unit_size,
            ),
        };
        SnapshotLayerMetadata {
            ordinal: u64::from(ordinal),
            layer_id: layer_id.clone(),
            content_identity: content_identity.clone(),
            format: "raw".into(),
            logical_bytes,
            unit_size_bytes,
        }
    }

    fn kind(self) -> LayerKind {
        match self {
            Self::Frozen { layer, .. } => layer.kind,
            Self::Remote(layer) => layer.range_map.kind,
            Self::MissingRemote { kind, .. } => kind,
        }
    }

    fn checked_range(self, offset: u64, length: u64) -> Result<(u64, u64), LazyLoadError> {
        let metadata = self.metadata(0);
        let end = offset
            .checked_add(length)
            .filter(|end| length != 0 && *end <= metadata.logical_bytes)
            .ok_or(LazyLoadError::InvalidRange)?;
        if metadata.unit_size_bytes == 0
            || !metadata
                .logical_bytes
                .is_multiple_of(metadata.unit_size_bytes)
        {
            return Err(LazyLoadError::InvalidRange);
        }
        Ok((end, metadata.unit_size_bytes))
    }

    fn state_at(self, offset: u64) -> Result<Option<u8>, LazyLoadError> {
        match self {
            Self::MissingRemote { .. } => Ok(None),
            Self::Frozen { layer, .. } => {
                let unit = offset / layer.unit_size_bytes;
                let state = layer
                    .ownership()
                    .packed_states
                    .get(unit as usize / 4)
                    .map(|byte| (*byte >> ((unit % 4) * 2)) & 3)
                    .ok_or(LazyLoadError::IdentityConflict)?;
                if state > 2 {
                    return Err(LazyLoadError::IdentityConflict);
                }
                Ok(Some(state))
            }
            Self::Remote(layer) => {
                let ranges = &layer.range_map.ranges;
                let first =
                    ranges.partition_point(|range| range.logical_offset + range.length <= offset);
                Ok(ranges
                    .get(first)
                    .filter(|range| range.logical_offset <= offset)
                    .map(|range| match range.kind {
                        RangeKind::Inherit => 0,
                        RangeKind::Data => 1,
                        RangeKind::Zero => 2,
                    }))
            }
        }
    }

    fn run_end(self, offset: u64, limit: u64, unit: u64, state: u8) -> Result<u64, LazyLoadError> {
        match self {
            Self::MissingRemote { .. } => Err(LazyLoadError::NonDataRange),
            Self::Remote(layer) => {
                let first = layer
                    .range_map
                    .ranges
                    .partition_point(|range| range.logical_offset + range.length <= offset);
                let range = layer
                    .range_map
                    .ranges
                    .get(first)
                    .ok_or(LazyLoadError::NonDataRange)?;
                Ok((range.logical_offset + range.length).min(limit))
            }
            Self::Frozen { .. } => {
                let mut end = (offset / unit + 1) * unit;
                while end < limit && self.state_at(end)? == Some(state) {
                    end += unit;
                }
                Ok(end.min(limit))
            }
        }
    }

    fn backing(self) -> Result<Option<(LocalBackingHandle, bool)>, LazyLoadError> {
        Ok(match self {
            Self::MissingRemote { .. } => None,
            Self::Frozen { owner, layer } => {
                Some((owner.backing_handle(layer.kind).map_err(local_io)?, true))
            }
            Self::Remote(layer) => {
                let metadata = File::from(layer.backing.as_ref().try_clone().map_err(local_io)?)
                    .metadata()
                    .map_err(local_io)?;
                LocalBacking::lookup_physical(metadata.dev(), metadata.ino()).map(|backing| {
                    (
                        backing.lease(),
                        layer.residency == SourceResidency::Complete,
                    )
                })
            }
        })
    }

    pub(crate) fn coverage(
        self,
        offset: u64,
        length: u64,
        max_units: u64,
    ) -> Result<Option<RawCoveragePage>, LazyLoadError> {
        let (end, unit) = self.checked_range(offset, length)?;
        if offset % unit != 0 || length % unit != 0 || length / unit > max_units {
            return Err(LazyLoadError::InvalidRange);
        }
        let mut packed_states = vec![0; (length / unit).div_ceil(4) as usize];
        for (index, position) in (offset..end).step_by(unit as usize).enumerate() {
            let Some(state) = self.state_at(position)? else {
                return Ok(None);
            };
            packed_states[index / 4] |= state << ((index % 4) * 2);
        }
        Ok(Some(RawCoveragePage {
            offset,
            length,
            unit_size: unit,
            packed_states,
        }))
    }

    /// Resolve only explicit DATA/ZERO. INHERIT and nonresident DATA are misses;
    /// this never restores backing holes or invokes a remote/encoded reader.
    pub(crate) async fn read_resident(
        self,
        ordinal: u32,
        offset: u64,
        length: u64,
    ) -> Result<Option<Vec<u8>>, LazyLoadError> {
        let (end, unit) = self.checked_range(offset, length)?;
        if length > MAX_DATA_RANGE_BYTES {
            return Err(LazyLoadError::InvalidRange);
        }
        let limit = end.div_ceil(unit) * unit;
        let mut runs = Vec::new();
        let mut cursor = offset / unit * unit;
        while cursor < limit {
            let Some(state) = self.state_at(cursor)? else {
                return Ok(None);
            };
            if state == 0 {
                return Ok(None);
            }
            let next = self.run_end(cursor, limit, unit, state)?;
            runs.push((state, cursor, next));
            cursor = next;
        }
        let data = runs
            .iter()
            .filter(|(state, _, _)| *state == 1)
            .map(|(_, start, end)| (*start, end - start))
            .collect::<Vec<_>>();
        let metadata = self.metadata(ordinal);
        let reader = if data.is_empty() {
            None
        } else {
            let Some((backing, complete)) = self.backing()? else {
                return Ok(None);
            };
            let pin = match backing.backing.pin_native_io() {
                Ok(pin) => pin,
                Err(BackingError::BackingClosed) => return Ok(None),
                Err(error) => return Err(local_io(error)),
            };
            Some((
                RawLayerReader::new(metadata.clone(), self.kind(), data, backing)?,
                complete,
                pin,
            ))
        };
        let mut payload = Vec::with_capacity(length as usize);
        for (state, start, stop) in runs {
            let position = start.max(offset);
            let count = stop.min(end) - position;
            if state == 2 {
                payload.resize(payload.len() + count as usize, 0);
            } else {
                let (reader, complete, _pin) =
                    reader.as_ref().expect("DATA has a logical raw reader");
                let Some(bytes) = reader
                    .read_resident_data(
                        SnapshotReadRange {
                            kind: self.kind(),
                            layer_content_sha256: &metadata.content_identity,
                            logical_offset: position,
                            length: count,
                        },
                        *complete,
                    )
                    .await?
                else {
                    return Ok(None);
                };
                payload.extend(bytes);
            }
        }
        Ok(Some(payload))
    }

    pub(crate) async fn residency(
        self,
        selection_only: bool,
    ) -> Result<RawResidency, LazyLoadError> {
        let metadata = self.metadata(0);
        let unit = metadata.unit_size_bytes;
        self.checked_range(0, metadata.logical_bytes)?;
        let backing = self.backing()?;
        let mut cursor = 0;
        let mut result = RawResidency {
            any_owned: false,
            complete: true,
        };
        while cursor < metadata.logical_bytes {
            let Some(state) = self.state_at(cursor)? else {
                result.complete = false;
                break;
            };
            let limit = if selection_only {
                (cursor + unit).min(metadata.logical_bytes)
            } else {
                metadata.logical_bytes
            };
            let end = self.run_end(cursor, limit, unit, state)?;
            let resident = match state {
                0 => {
                    cursor = end;
                    continue;
                }
                2 => true,
                _ => match &backing {
                    Some((backing, complete)) => backing
                        .backing
                        .contains_resident_data(cursor, end - cursor, *complete)
                        .await
                        .unwrap_or(false),
                    None => false,
                },
            };
            if resident {
                result.any_owned = true;
            } else {
                result.complete = false;
                if let Some((backing, complete)) = &backing {
                    result.any_owned |= backing
                        .backing
                        .contains_any_resident_data(cursor, end - cursor, *complete)
                        .await
                        .unwrap_or(false);
                }
            }
            if selection_only && result.any_owned {
                result.complete = false;
                return Ok(result);
            }
            cursor = end;
        }
        Ok(result)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::snapshot::io::backing::BackingRecovery;
    use crate::snapshot::metadata::SnapshotProvenance;
    use crate::snapshot::repository::frozen_layer::{
        FrozenLayerMetadata, FrozenLayerPin, PackedOwnershipMap,
    };
    use crate::snapshot::transport::posix_file;
    use std::future::{poll_fn, Future};
    use std::os::unix::fs::FileExt;
    use std::task::Poll;
    use tokio::sync::Notify;

    #[derive(Debug)]
    struct Recovery {
        bytes: Vec<u8>,
        entered: Notify,
        release: Notify,
    }
    #[async_trait]
    impl BackingRecovery for Recovery {
        async fn read_data(&self, offset: u64, length: u64) -> Result<Vec<u8>, BackingError> {
            if offset == 0 {
                self.entered.notify_one();
                self.release.notified().await;
            }
            Ok(self.bytes[offset as usize..(offset + length) as usize].to_vec())
        }
    }

    #[tokio::test]
    async fn one_resident_read_pins_all_data_runs_until_zero_assembly_completes() {
        const UNIT: u64 = 65536;
        let mut bytes = vec![0; 3 * UNIT as usize];
        bytes[..UNIT as usize].fill(0x41);
        bytes[2 * UNIT as usize..].fill(0x43);
        let file = posix_file::memory_file(c"raw-multi-run-pin", 3 * UNIT).unwrap();
        file.write_all_at(&bytes, 0).unwrap();
        let snapshot = Snapshot::from_base_layer(
            "multi-run-pin".into(),
            FrozenLayerPin::new(
                FrozenLayerMetadata {
                    kind: LayerKind::Block,
                    layer_id: "multi-run-pin".into(),
                    provisional_content_id: "native:multi-run".into(),
                    logical_size: 3 * UNIT,
                    alignment: UNIT,
                },
                PackedOwnershipMap {
                    kind: LayerKind::Block,
                    snapshot_id: "multi-run-pin".into(),
                    format_version: 1,
                    logical_size: 3 * UNIT,
                    granularity: UNIT,
                    packed_states: vec![0b011001],
                },
                file.into(),
                false,
            ),
            SnapshotProvenance {
                oci_ref: None,
                oci_digest: None,
            },
        )
        .unwrap();
        let handle = snapshot.backing_handle(LayerKind::Block).unwrap();
        let recovery = Arc::new(Recovery {
            bytes: bytes.clone(),
            entered: Notify::new(),
            release: Notify::new(),
        });
        handle.backing.install_recovery(recovery.clone());
        assert_eq!(
            handle.backing.reclaim_unused().await.unwrap(),
            vec![(0, UNIT), (2 * UNIT, UNIT)]
        );
        handle
            .backing
            .register_consumer("runtime", &[(0, UNIT), (2 * UNIT, UNIT)])
            .unwrap();
        handle
            .backing
            .restore_ranges(&[(2 * UNIT, UNIT)])
            .await
            .unwrap();
        let restore_backing = Arc::clone(&handle.backing);
        let restore = tokio::spawn(async move {
            restore_backing.restore_ranges(&[(0, UNIT)]).await.unwrap();
        });
        recovery.entered.notified().await;
        let view = resident_layer_views(&snapshot, LayerKind::Block)[0];
        let mut read = Box::pin(view.read_resident(0, 0, 3 * UNIT));
        poll_fn(|context| match read.as_mut().poll(context) {
            Poll::Pending => Poll::Ready(()),
            Poll::Ready(_) => panic!("restore gate must hold the logical read"),
        })
        .await;
        handle.backing.release_consumer("runtime");
        let reclaim_backing = Arc::clone(&handle.backing);
        let mut reclaim = Box::pin(reclaim_backing.reclaim_unused());
        poll_fn(|context| match reclaim.as_mut().poll(context) {
            Poll::Pending => Poll::Ready(()),
            Poll::Ready(_) => panic!("restore gate must queue the reclaimer"),
        })
        .await;
        // FIFO I/O ordering gives the queued reclaimer a turn between the
        // first and second DATA runs. The logical read must retain its outer
        // lease while assembling the intervening ZERO run.
        recovery.release.notify_one();
        restore.await.unwrap();
        let (payload, reclaimed) = tokio::join!(read, reclaim);
        assert!(reclaimed.unwrap().is_empty());
        assert_eq!(payload.unwrap(), Some(bytes));
    }
}
