//! Immutable snapshot ancestry and retained native DATA sources.

use std::collections::{HashMap, HashSet};
use std::os::fd::OwnedFd;
use std::sync::Arc;

use crate::snapshot::format::reader::LayerReadCapability;
use crate::snapshot::io::backing::DataRangeResidencyRegistry;
use crate::snapshot::metadata::{
    LayerChainIndex, LayerKind, LayerRangeMap, LazyLoadError, RangeMapEncoding, SnapshotProvenance,
};
use crate::snapshot::{LazyLoadInterface, SnapshotInstance};

use super::Snapshot;

/// A local sparse file is either complete or backed by authenticated demand
/// loading. This describes bytes, never a VMM attachment or process.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum SourceResidency {
    Complete,
    OnDemand,
}

#[derive(Clone, Debug)]
pub(crate) struct SourceResidencyHandle {
    pub backing_id: String,
    pub registry: Arc<DataRangeResidencyRegistry>,
}

/// A snapshot retains only immutable logical coverage and native byte sources.
/// The caller's attachment indices, target grants and control state stay with
/// that caller. The FD Arcs preserve the original inode without copying DATA.
#[derive(Clone, Debug)]
pub(crate) struct RemoteLayerSource {
    pub ordinal: u32,
    pub layer_id: String,
    pub range_map: Arc<LayerRangeMap>,
    pub backing: Arc<OwnedFd>,
    pub shared_mapping: Option<Arc<OwnedFd>>,
    pub residency: SourceResidency,
    pub dynamic_residency: Option<SourceResidencyHandle>,
}

impl RemoteLayerSource {
    pub(crate) fn matches_content_identity(&self, identity: &str) -> bool {
        self.range_map.content_sha256.trim_start_matches("sha256:")
            == identity.trim_start_matches("sha256:")
    }

    pub(crate) fn format_reader(
        &self,
        encoding: Option<RangeMapEncoding>,
    ) -> Result<Arc<dyn LazyLoadInterface>, LazyLoadError> {
        let encoding = encoding.ok_or(LazyLoadError::UnsupportedCodec)?;
        Ok(LayerReadCapability::for_encoding(
            Arc::clone(&self.range_map),
            encoding,
        ))
    }
}

#[derive(Clone, Debug, Default)]
pub(crate) struct SnapshotLineage {
    remote_snapshot_id: Option<String>,
    entries: Vec<SnapshotLineageEntry>,
    provenance: Option<SnapshotProvenance>,
}

#[derive(Clone, Debug)]
pub(crate) enum SnapshotLineageEntry {
    Remote {
        kind: LayerKind,
        chain_index: LayerChainIndex,
        layers: Vec<RemoteLayerSource>,
    },
    FrozenLocal(Arc<Snapshot>),
}

impl SnapshotLineage {
    pub(crate) fn from_remote(
        source_snapshot_id: &str,
        parent_chains: &HashMap<LayerKind, LayerChainIndex>,
        layers: &[RemoteLayerSource],
    ) -> Self {
        let mut entries = Vec::new();
        for kind in [LayerKind::Memory, LayerKind::Block] {
            let Some(chain_index) = parent_chains.get(&kind) else {
                continue;
            };
            entries.push(SnapshotLineageEntry::Remote {
                kind,
                chain_index: chain_index.clone(),
                layers: layers
                    .iter()
                    .filter(|layer| layer.range_map.kind == kind)
                    .cloned()
                    .collect(),
            });
        }
        Self {
            remote_snapshot_id: Some(source_snapshot_id.to_owned()),
            entries,
            provenance: None,
        }
    }

    pub(crate) fn with_provenance(mut self, provenance: Option<SnapshotProvenance>) -> Self {
        self.provenance = provenance;
        self
    }

    pub(crate) fn provenance(&self) -> Option<&SnapshotProvenance> {
        self.provenance.as_ref()
    }

    pub(crate) fn with_frozen(&self, snapshot: Arc<Snapshot>) -> Self {
        let mut lineage = self.clone();
        if lineage.provenance.is_none() {
            lineage.provenance = snapshot.provenance().cloned();
        }
        lineage
            .entries
            .push(SnapshotLineageEntry::FrozenLocal(snapshot));
        lineage
    }

    pub(crate) fn entries(&self) -> &[SnapshotLineageEntry] {
        &self.entries
    }

    pub(crate) fn frozen_snapshots(&self) -> impl Iterator<Item = &Arc<Snapshot>> {
        self.entries.iter().filter_map(|entry| match entry {
            SnapshotLineageEntry::FrozenLocal(snapshot) => Some(snapshot),
            SnapshotLineageEntry::Remote { .. } => None,
        })
    }

    pub(crate) fn ancestor_snapshot_ids(&self) -> Vec<String> {
        let mut seen = HashSet::new();
        self.remote_snapshot_id
            .iter()
            .chain(
                self.frozen_snapshots()
                    .map(|snapshot| &snapshot.snapshot_id),
            )
            .filter(|snapshot_id| !snapshot_id.is_empty() && seen.insert((*snapshot_id).clone()))
            .cloned()
            .collect()
    }

    pub(crate) fn depth(&self, kind: LayerKind) -> u64 {
        self.entries
            .iter()
            .map(|entry| match entry {
                SnapshotLineageEntry::Remote {
                    kind: entry_kind,
                    layers,
                    ..
                } if *entry_kind == kind => layers.len() as u64,
                SnapshotLineageEntry::FrozenLocal(snapshot)
                    if snapshot.frozen_layers.contains_key(&kind) =>
                {
                    1
                }
                _ => 0,
            })
            .sum()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::snapshot::format::overlaybd::{
        decode_descriptor_with_blob, encode_descriptor, plan_layer,
    };
    use crate::snapshot::io::range_io::{RangeReadRequest, RangeReadResult, RangeReader};
    use crate::snapshot::metadata::{
        compute_layer_content_sha256, sha256_identity, DataCodec, DataSource, LayerRange,
        LayerRangeMapRef, LayerRef, LazyLoadContext, ObjectRef, RangeKind, SnapshotReadRange,
    };
    use crate::snapshot::transport::RangeReadError;
    use crate::snapshot::ValidateCanonicalModel;
    use std::os::unix::fs::FileExt;

    struct RemoteBytes {
        object: ObjectRef,
        blob: Vec<u8>,
    }

    #[async_trait::async_trait]
    impl RangeReader for RemoteBytes {
        async fn read_range(
            &self,
            request: RangeReadRequest<'_>,
        ) -> Result<RangeReadResult, RangeReadError> {
            assert_eq!(request.object_key, self.object.object_key);
            assert_eq!(request.object_sha256, self.object.sha256);
            assert_eq!(request.length, 65536);
            assert!(
                request.offset > 0,
                "DATA starts after the standard LSMT header"
            );
            let begin = request.offset as usize;
            let end = begin + request.length as usize;
            Ok(RangeReadResult::provider(self.blob[begin..end].to_vec()))
        }
    }

    #[tokio::test]
    async fn remote_ancestor_uses_snapshot_read_capability_without_a_vm_binding() {
        let raw = vec![0x5a; 65536];
        let mut map = LayerRangeMap {
            registry_objects: None,
            format_version: 1,
            kind: LayerKind::Block,
            content_sha256: sha256_identity(b"provisional"),
            logical_bytes: 65536,
            unit_size_bytes: 65536,
            ranges: vec![LayerRange {
                kind: RangeKind::Data,
                logical_offset: 0,
                length: 65536,
                source: Some(DataSource {
                    object_key: "capture.raw".into(),
                    object_sha256: sha256_identity(&raw),
                    object_version: None,
                    pack_offset: 0,
                    encoded_length: 65536,
                    decoded_length: 65536,
                    codec: DataCodec::Raw,
                    checksum_sha256: sha256_identity(&raw),
                }),
            }],
            zfile: None,
            registry: None,
        };
        map.content_sha256 = compute_layer_content_sha256(&map).unwrap();
        let content = map.content_sha256.clone();
        let backing = crate::snapshot::repository::frozen_layer::create_temporary_zfile().unwrap();
        backing.write_all_at(&raw, 0).unwrap();
        let plan = plan_layer(&map).unwrap();
        let blob = plan
            .read_at(&backing, 0, plan.stored_bytes as usize)
            .unwrap();
        let prefix = format!("layers/block/1/{}/", &content[7..]);
        let object = ObjectRef {
            object_key: format!("{prefix}data/{}.commit", &sha256_identity(&blob)[7..]),
            sha256: sha256_identity(&blob),
            object_version: None,
        };
        let descriptor = encode_descriptor(&map, &plan, &object).unwrap();
        let (map, _, _) = decode_descriptor_with_blob(&descriptor).unwrap();
        let encoding = RangeMapEncoding::OverlaybdLsmtV1;
        let source = RemoteLayerSource {
            ordinal: 0,
            layer_id: "ancestor-block".into(),
            range_map: Arc::new(map),
            // This FD contains no DATA. The authenticated lazy reader must use
            // the transport capability instead of treating a sparse FD as full.
            backing: Arc::new(std::fs::File::open("/dev/null").unwrap().into()),
            shared_mapping: None,
            residency: SourceResidency::OnDemand,
            dynamic_residency: None,
        };
        let chains = HashMap::from([(
            LayerKind::Block,
            LayerChainIndex {
                format_version: 1,
                kind: LayerKind::Block,
                logical_bytes: 65536,
                unit_size_bytes: 65536,
                layers: vec![LayerRef {
                    ordinal: 0,
                    layer_id: source.layer_id.clone(),
                    format_version: 1,
                    content_sha256: content.clone(),
                    range_map: LayerRangeMapRef {
                        object_key: format!("{prefix}{}", encoding.descriptor_name()),
                        sha256: sha256_identity(&descriptor),
                        object_version: None,
                        encoding: Some(encoding),
                        source_ref: None,
                    },
                }],
            },
        )]);
        chains[&LayerKind::Block].validate().unwrap();
        let lineage = SnapshotLineage::from_remote("ancestor-snapshot", &chains, &[source]);
        assert_eq!(lineage.ancestor_snapshot_ids(), ["ancestor-snapshot"]);
        assert_eq!(lineage.depth(LayerKind::Block), 1);
        let SnapshotLineageEntry::Remote { layers, .. } = &lineage.entries()[0] else {
            unreachable!()
        };
        let reader = layers[0].format_reader(Some(encoding)).unwrap();
        let context = LazyLoadContext::new(
            Arc::new(RemoteBytes { object, blob }),
            crate::RuntimeIdentity::new("reader"),
            None,
        );
        let read = reader
            .read_data_range(
                &context,
                SnapshotReadRange {
                    kind: LayerKind::Block,
                    layer_content_sha256: &content,
                    logical_offset: 123,
                    length: 64,
                },
            )
            .await
            .unwrap();
        assert_eq!(read.payload, [0x5a; 64]);
    }
}
