//! Format builder for authenticated S3 metadata on the common snapshot identity.
use crate::snapshot::instance::{PersistentSnapshot, Snapshot};
use crate::snapshot::metadata::{SnapshotChainView, SnapshotManifest, SnapshotSpec};
#[cfg(test)]
use crate::snapshot::SnapshotInstance;
use std::sync::Arc;

/// A metadata builder, not a second concrete snapshot or resource owner.
#[derive(Debug)]
pub struct OverlaybdS3Snapshot;

impl OverlaybdS3Snapshot {
    pub(crate) fn new(
        snapshot_id: String,
        spec: SnapshotSpec,
        manifest: SnapshotManifest,
        memory: Option<SnapshotChainView>,
        rootfs: SnapshotChainView,
        vm_state: Option<Vec<u8>>,
    ) -> Result<Arc<Snapshot>, String> {
        Self::new_with_ancestors(
            snapshot_id,
            spec,
            manifest,
            memory,
            rootfs,
            vm_state,
            Vec::new(),
        )
    }

    pub(crate) fn new_with_ancestors(
        snapshot_id: String,
        spec: SnapshotSpec,
        manifest: SnapshotManifest,
        memory: Option<SnapshotChainView>,
        rootfs: SnapshotChainView,
        vm_state: Option<Vec<u8>>,
        ancestor_snapshot_ids: Vec<String>,
    ) -> Result<Arc<Snapshot>, String> {
        let mut seen = std::collections::HashSet::new();
        if ancestor_snapshot_ids.len() > crate::snapshot::metadata::MAX_CHAIN_DEPTH
            || ancestor_snapshot_ids.iter().any(|ancestor| {
                ancestor.is_empty()
                    || ancestor.len() > 4096
                    || ancestor == &snapshot_id
                    || !seen.insert(ancestor)
            })
        {
            return Err("snapshot ancestry is invalid".into());
        }
        Snapshot::from_persistent(
            snapshot_id,
            PersistentSnapshot {
                spec,
                manifest,
                memory,
                rootfs,
                vm_state,
                ancestor_snapshot_ids,
            },
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::snapshot::format::{reader::LayerReadCapability, LayerViewInterface};
    use crate::snapshot::io::range_io::{RangeReadRequest, RangeReader};
    use crate::snapshot::metadata::*;
    use crate::snapshot::transport::RangeReadError;
    use crate::snapshot::ValidateCanonicalModel;
    use crate::RuntimeIdentity;
    use async_trait::async_trait;
    use std::collections::HashMap;
    use std::fs::File;
    use std::os::unix::fs::FileExt;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::time::Duration;

    use crate::snapshot::format::overlaybd::{
        decode_descriptor_with_blob, encode_descriptor, encode_zfile_descriptor, plan_layer,
    };
    use crate::snapshot::io::range_io::{RangeDataKind, RangeReadResult};

    const PROFILES: [RangeMapEncoding; 3] = [
        RangeMapEncoding::OverlaybdLsmtV1,
        RangeMapEncoding::OverlaybdZfileLz4V1,
        RangeMapEncoding::OverlaybdLsmtV1,
    ];

    struct BlobProvider {
        objects: HashMap<String, Vec<u8>>,
        calls: AtomicUsize,
        compressed_calls: AtomicUsize,
    }

    impl BlobProvider {
        fn new(objects: HashMap<String, Vec<u8>>) -> Arc<Self> {
            Arc::new(Self {
                objects,
                calls: AtomicUsize::new(0),
                compressed_calls: AtomicUsize::new(0),
            })
        }
    }

    #[async_trait]
    impl RangeReader for BlobProvider {
        async fn read_range(
            &self,
            request: RangeReadRequest<'_>,
        ) -> Result<RangeReadResult, RangeReadError> {
            self.calls.fetch_add(1, Ordering::Relaxed);
            let blob = self.objects.get(request.object_key).unwrap();
            assert_eq!(request.object_sha256, sha256_identity(blob));
            // Partial logical reads authenticate a complete DATA unit in the
            // inner LSMT stream; logical byte zero is not physical byte zero.
            assert_ne!(request.offset, 0);
            assert_eq!(
                request.length,
                match request.kind {
                    RangeDataKind::Memory => MEMORY_LOAD_UNIT_BYTES,
                    RangeDataKind::Block => 65536,
                }
            );
            let (offset, length) = match request.physical {
                Some(physical) => {
                    self.compressed_calls.fetch_add(1, Ordering::Relaxed);
                    assert_eq!(physical.object_key, request.object_key);
                    assert_eq!(physical.object_sha256, request.object_sha256);
                    (physical.offset, physical.length)
                }
                None => (request.offset, request.length),
            };
            let begin = usize::try_from(offset).unwrap();
            let end = begin.checked_add(usize::try_from(length).unwrap()).unwrap();
            let payload = blob.get(begin..end).unwrap().to_vec();
            Ok(if request.physical.is_some() {
                RangeReadResult::encoded(payload)
            } else {
                RangeReadResult::provider(payload)
            })
        }
    }

    fn scratch() -> File {
        let path =
            std::env::temp_dir().join(format!("metadata-overlaybd-{}", uuid::Uuid::new_v4()));
        let file = File::options()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
            .unwrap();
        std::fs::remove_file(path).unwrap();
        file
    }

    fn payload_byte(ordinal: usize) -> u8 {
        0x5a + 0x11 * ordinal as u8
    }

    fn layer_fixture(
        kind: LayerKind,
        encoding: RangeMapEncoding,
        ordinal: usize,
        zero_only: bool,
    ) -> (SnapshotLayerView, String, Vec<u8>) {
        let unit = match kind {
            LayerKind::Memory => MEMORY_LOAD_UNIT_BYTES,
            LayerKind::Block => 65536,
        };
        let logical_bytes = 3 * unit;
        let mut raw = vec![0; logical_bytes as usize];
        if !zero_only {
            raw[..unit as usize].fill(payload_byte(ordinal));
        }
        let ranges = if zero_only {
            vec![LayerRange {
                kind: RangeKind::Zero,
                logical_offset: 0,
                length: logical_bytes,
                source: None,
            }]
        } else {
            vec![
                LayerRange {
                    kind: RangeKind::Data,
                    logical_offset: 0,
                    length: unit,
                    source: Some(DataSource {
                        object_key: "fixture/source.raw".into(),
                        object_sha256: sha256_identity(&raw),
                        object_version: None,
                        pack_offset: 0,
                        encoded_length: unit,
                        decoded_length: unit,
                        codec: DataCodec::Raw,
                        checksum_sha256: sha256_identity(&raw[..unit as usize]),
                    }),
                },
                LayerRange {
                    kind: RangeKind::Zero,
                    logical_offset: unit,
                    length: unit,
                    source: None,
                },
                LayerRange {
                    kind: RangeKind::Inherit,
                    logical_offset: 2 * unit,
                    length: unit,
                    source: None,
                },
            ]
        };
        let mut map = LayerRangeMap {
            registry_objects: None,
            format_version: 1,
            kind,
            content_sha256: String::new(),
            logical_bytes,
            unit_size_bytes: unit,
            ranges,
            zfile: None,
            registry: None,
        };
        map.content_sha256 = compute_layer_content_sha256(&map).unwrap();
        let source = scratch();
        source.write_all_at(&raw, 0).unwrap();
        let plan = plan_layer(&map).unwrap();
        let inner = plan
            .read_at(&source, 0, plan.stored_bytes as usize)
            .unwrap();
        assert_eq!(inner.len() as u64, plan.stored_bytes);
        let (bytes, zfile) = match encoding {
            RangeMapEncoding::OverlaybdLsmtV1 => (inner, None),
            RangeMapEncoding::OverlaybdZfileLz4V1 => {
                let output = scratch();
                let encoded = crate::snapshot::format::overlaybd::zfile::encode_with_reader(
                    inner.len() as u64,
                    &output,
                    |offset, count| Ok(inner[offset as usize..offset as usize + count].to_vec()),
                )
                .unwrap();
                let mut bytes = vec![0; encoded.index.encoded_bytes as usize];
                output.read_exact_at(&mut bytes, 0).unwrap();
                assert_eq!(sha256_identity(&bytes), encoded.sha256);
                (bytes, Some(encoded.index))
            }
            RangeMapEncoding::RegistryOverlaybdV1 => unreachable!("local blob fixture"),
        };
        let object_sha256 = sha256_identity(&bytes);
        let prefix = format!(
            "layers/{}/1/{}/",
            kind.object_key_component(),
            &map.content_sha256[7..]
        );
        let blob = ObjectRef {
            object_key: format!("{prefix}data/{}.commit", &object_sha256[7..]),
            sha256: object_sha256,
            object_version: None,
        };
        let descriptor = match zfile {
            Some(index) => encode_zfile_descriptor(&map, &plan, &blob, &index).unwrap(),
            None => encode_descriptor(&map, &plan, &blob).unwrap(),
        };
        let (decoded, actual_blob, physical_bytes) =
            decode_descriptor_with_blob(&descriptor).unwrap();
        assert_eq!(actual_blob, blob);
        assert_eq!(physical_bytes, bytes.len() as u64);
        assert_eq!(decoded.content_sha256, map.content_sha256);
        assert_eq!(
            decoded.zfile.is_some(),
            encoding == RangeMapEncoding::OverlaybdZfileLz4V1
        );
        assert_eq!(
            decoded
                .ranges
                .iter()
                .map(|range| range.kind)
                .collect::<Vec<_>>(),
            map.ranges
                .iter()
                .map(|range| range.kind)
                .collect::<Vec<_>>()
        );
        let layer = SnapshotLayerView {
            layer_ref: LayerRef {
                ordinal: ordinal as u64,
                layer_id: format!("{kind:?}-{ordinal}"),
                format_version: 1,
                content_sha256: decoded.content_sha256.clone(),
                range_map: LayerRangeMapRef {
                    object_key: format!("{prefix}{}", encoding.descriptor_name()),
                    sha256: sha256_identity(&descriptor),
                    object_version: None,
                    encoding: Some(encoding),
                    source_ref: None,
                },
            },
            range_map: Arc::new(decoded),
        };
        (layer, blob.object_key, bytes)
    }

    fn chain(
        kind: LayerKind,
        encodings: &[RangeMapEncoding],
        zero_only: bool,
    ) -> (SnapshotChainView, HashMap<String, Vec<u8>>) {
        let mut objects = HashMap::new();
        let layers = encodings
            .iter()
            .enumerate()
            .map(|(ordinal, encoding)| {
                let (layer, key, bytes) = layer_fixture(kind, *encoding, ordinal, zero_only);
                if let Some(previous) = objects.insert(key, bytes.clone()) {
                    assert_eq!(previous, bytes);
                }
                layer
            })
            .collect::<Vec<_>>();
        let unit = layers[0].range_map.unit_size_bytes;
        let chain = SnapshotChainView {
            index: LayerChainIndex {
                format_version: 1,
                kind,
                logical_bytes: 3 * unit,
                unit_size_bytes: unit,
                layers: layers.iter().map(|layer| layer.layer_ref.clone()).collect(),
            },
            layers,
        };
        chain.index.validate().unwrap();
        (chain, objects)
    }

    fn opened(full: bool) -> (Arc<Snapshot>, Arc<BlobProvider>) {
        let (rootfs, mut objects) = chain(LayerKind::Block, &PROFILES, false);
        let memory = full.then(|| {
            let (memory, memory_objects) = chain(LayerKind::Memory, &PROFILES[..2], false);
            objects.extend(memory_objects);
            memory
        });
        let index = |key: &str, chain: &SnapshotChainView| ChainIndexRef {
            logical_bytes: chain.index.logical_bytes,
            index_object_key: key.into(),
            index_sha256: sha256_identity(&canonical_json_bytes(&chain.index).unwrap()),
            object_version: None,
            source_ref: None,
        };
        let rootfs_index = index("rootfs.index", &rootfs);
        let memory_index = memory.as_ref().map(|memory| index("memory.index", memory));
        let manifest_index = |index: &ChainIndexRef| ManifestChainRef {
            object_key: index.index_object_key.clone(),
            sha256: index.index_sha256.clone(),
            logical_bytes: index.logical_bytes,
            object_version: None,
        };
        let manifest = SnapshotManifest {
            format_version: 1,
            snapshot_id: "contract-snapshot".into(),
            snapshot_kind: if full {
                SnapshotKind::Full
            } else {
                SnapshotKind::BaseImage
            },
            memory: memory_index.as_ref().map(manifest_index),
            rootfs: manifest_index(&rootfs_index),
            vm_state: None,
        };
        let snapshot = OverlaybdS3Snapshot::new(
            "contract-snapshot".into(),
            SnapshotSpec {
                registry_snapshot: None,
                format_version: 1,
                compatibility: VmmCompatibility {
                    architecture: "x86_64".into(),
                    vmm_type: "cloud-hypervisor".into(),
                    vmm_state_format_version: None,
                    rocklet_artifact: None,
                },
                provenance: None,
                storage: StorageDescriptor {
                    provider: StorageProvider::S3,
                    endpoint_profile: "fixture".into(),
                    credential_ref: "fixture".into(),
                },
                manifest: ObjectRef {
                    object_key: "manifest.json".into(),
                    sha256: sha256_identity(&canonical_json_bytes(&manifest).unwrap()),
                    object_version: None,
                },
                memory: memory_index,
                rootfs: rootfs_index,
                vm_state: None,
            },
            manifest,
            memory,
            rootfs,
            None,
        )
        .unwrap();
        (snapshot, BlobProvider::new(objects))
    }

    #[tokio::test]
    async fn s3_instance_exposes_mixed_layer_metadata_and_selected_layer_reads() {
        let (snapshot, provider) = opened(true);
        let metadata: &dyn SnapshotInstance = snapshot.as_ref();
        assert_eq!(metadata.snapshot_kind(), SnapshotKind::Full);
        let block = metadata.layer_chain(LayerKind::Block).unwrap().unwrap();
        assert_eq!(
            block
                .layers
                .iter()
                .map(|layer| layer.ordinal)
                .collect::<Vec<_>>(),
            vec![0, 1, 2]
        );
        assert_eq!(
            block
                .layers
                .iter()
                .map(|layer| layer.format.as_str())
                .collect::<Vec<_>>(),
            vec![
                "overlaybd-lsmt-v1",
                "overlaybd-zfile-lz4-v1",
                "overlaybd-lsmt-v1"
            ]
        );
        assert_eq!(
            metadata
                .layer_chain(LayerKind::Memory)
                .unwrap()
                .unwrap()
                .layers
                .len(),
            2
        );
        let context = LazyLoadContext::new(
            provider.clone(),
            RuntimeIdentity::new("fixture-runtime"),
            None,
        );
        // Both the uncompressed and ZFile profiles verify complete DATA units
        // from their real object bytes before returning a partial logical read.
        for (ordinal, layer) in block.layers.iter().enumerate() {
            let result = snapshot
                .layer_reader(LayerKind::Block, &layer.content_identity)
                .unwrap()
                .read_data_range(
                    &context,
                    SnapshotReadRange {
                        kind: LayerKind::Block,
                        layer_content_sha256: &layer.content_identity,
                        logical_offset: 128,
                        length: 512,
                    },
                )
                .await
                .unwrap();
            assert_eq!(result.payload, vec![payload_byte(ordinal); 512]);
        }
        assert_eq!(provider.calls.load(Ordering::Relaxed), 3);
        assert_eq!(provider.compressed_calls.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn zero_data_layers_keep_the_authoritative_codec_and_policy() {
        let (mut chain, _) = chain(LayerKind::Block, &PROFILES, true);
        for (layer, name) in chain.layers.iter().zip([
            "overlaybd-lsmt-v1",
            "overlaybd-zfile-lz4-v1",
            "overlaybd-lsmt-v1",
        ]) {
            let reader = layer.reader().unwrap();
            assert_eq!(reader.format_name(), name);
            assert_eq!(reader.layer_kind(), LayerKind::Block);
            assert_eq!(reader.logical_bytes(), 3 * 65536);
            assert!(reader.allows_p2p());
            assert!(reader.allows_background_prefetch());
        }
        let generic = LayerReadCapability::new(chain.layers.pop().unwrap().range_map);
        assert_eq!(generic.format_name(), "mapped");
    }

    #[tokio::test]
    async fn each_native_and_overlaybd_reader_shares_metadata_and_lazy_capabilities() {
        let (chain, objects) = chain(LayerKind::Block, &PROFILES, false);
        let provider = BlobProvider::new(objects);
        let context =
            LazyLoadContext::new(provider.clone(), RuntimeIdentity::new("mixed-reader"), None);
        for (ordinal, layer) in chain.layers.iter().enumerate() {
            let reader: Arc<dyn LayerViewInterface> = layer.reader().unwrap();
            let result = reader
                .read_data_range(
                    &context,
                    SnapshotReadRange {
                        kind: reader.layer_kind(),
                        layer_content_sha256: reader.content_identity(),
                        logical_offset: 64,
                        length: 128,
                    },
                )
                .await
                .unwrap();
            assert_eq!(result.payload, vec![payload_byte(ordinal); 128]);
        }
        assert_eq!(provider.calls.load(Ordering::Relaxed), 3);
        assert_eq!(provider.compressed_calls.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn s3_instance_composes_metadata_readers_and_publication_state() {
        let (snapshot, _) = opened(true);
        let view: &dyn SnapshotInstance = snapshot.as_ref();
        assert_eq!(view.snapshot_id(), "contract-snapshot");
        assert_eq!(view.snapshot_kind(), SnapshotKind::Full);
        assert_eq!(view.chain_view(LayerKind::Memory).unwrap().layers.len(), 2);
        assert_eq!(view.chain_view(LayerKind::Block).unwrap().layers.len(), 3);
        assert_eq!(view.spec(), snapshot.spec());
        assert_eq!(view.manifest(), snapshot.manifest());
        assert!(view.vm_state().is_none());
        assert!(view
            .layer_reader(
                LayerKind::Block,
                &snapshot.chain_view(LayerKind::Block).unwrap().layers[0]
                    .range_map
                    .content_sha256
            )
            .is_ok());
    }

    struct PendingProvider;

    #[async_trait]
    impl RangeReader for PendingProvider {
        async fn read_range(
            &self,
            _request: RangeReadRequest<'_>,
        ) -> Result<RangeReadResult, RangeReadError> {
            std::future::pending().await
        }
    }

    #[tokio::test]
    async fn provider_io_timeout_remains_a_transient_transport_error() {
        let (snapshot, _) = opened(false);
        let context = LazyLoadContext::new(
            Arc::new(PendingProvider),
            RuntimeIdentity::new("fixture-runtime"),
            None,
        )
        .with_provider_timeout(Duration::from_millis(1));
        let error = snapshot
            .layer_reader(
                LayerKind::Block,
                &snapshot.chain_view(LayerKind::Block).unwrap().layers[0]
                    .range_map
                    .content_sha256,
            )
            .unwrap()
            .read_data_range(
                &context,
                SnapshotReadRange {
                    kind: LayerKind::Block,
                    layer_content_sha256: &snapshot.chain_view(LayerKind::Block).unwrap().layers[0]
                        .range_map
                        .content_sha256,
                    logical_offset: 0,
                    length: 65536,
                },
            )
            .await
            .unwrap_err();
        assert!(matches!(
            error,
            LazyLoadError::Provider(RangeReadError::Failure {
                class: crate::snapshot::transport::RangeReadFailureClass::Transient,
                ..
            })
        ));
    }

    #[tokio::test]
    async fn non_data_and_oversized_reads_fail_before_any_transport_io() {
        let (snapshot, provider) = opened(false);
        assert!(snapshot.layer_chain(LayerKind::Memory).unwrap().is_none());
        let context = LazyLoadContext::new(
            provider.clone(),
            RuntimeIdentity::new("fixture-runtime"),
            None,
        );
        let identity = snapshot.chain_view(LayerKind::Block).unwrap().layers[0]
            .range_map
            .content_sha256
            .clone();
        let unit = snapshot
            .chain_view(LayerKind::Block)
            .unwrap()
            .index
            .unit_size_bytes;
        for offset in [unit, 2 * unit] {
            assert!(matches!(
                snapshot
                    .layer_reader(LayerKind::Block, &identity)
                    .unwrap()
                    .read_data_range(
                        &context,
                        SnapshotReadRange {
                            kind: LayerKind::Block,
                            layer_content_sha256: &identity,
                            logical_offset: offset,
                            length: unit,
                        }
                    )
                    .await,
                Err(LazyLoadError::NonDataRange)
            ));
        }
        assert!(matches!(
            snapshot
                .layer_reader(LayerKind::Block, &identity)
                .unwrap()
                .read_data_range(
                    &context,
                    SnapshotReadRange {
                        kind: LayerKind::Block,
                        layer_content_sha256: &identity,
                        logical_offset: 0,
                        length: MAX_DATA_RANGE_BYTES + 1,
                    }
                )
                .await,
            Err(LazyLoadError::InvalidRange)
        ));
        assert_eq!(provider.calls.load(Ordering::Relaxed), 0);
    }

    #[test]
    fn missing_persistent_encoding_is_rejected_by_views_and_metadata() {
        let (mut snapshot, provider) = opened(false);
        let snapshot = Arc::get_mut(&mut snapshot).unwrap();
        let rootfs = &mut snapshot.persistent_mut().rootfs;
        let mut legacy = serde_json::to_value(&rootfs.layers[0].layer_ref.range_map).unwrap();
        legacy["encoding"] = "binary-v2".into();
        assert!(serde_json::from_value::<LayerRangeMapRef>(legacy).is_err());
        rootfs.index.layers[0].range_map.encoding = None;
        rootfs.layers[0].layer_ref.range_map.encoding = None;
        let layer = &rootfs.layers[0];
        assert!(matches!(
            layer.reader(),
            Err(LazyLoadError::UnsupportedCodec)
        ));
        assert!(!layer.allows_p2p());
        assert!(!layer.allows_background_prefetch());
        assert!(rootfs
            .index
            .validate()
            .unwrap_err()
            .contains("persistent layer encoding is required"));
        let identity = layer.range_map.content_sha256.clone();
        assert!(snapshot
            .layer_chain(LayerKind::Block)
            .unwrap_err()
            .contains("persistent layer encoding is required"));
        assert!(matches!(
            snapshot.layer_reader(LayerKind::Block, &identity),
            Err(LazyLoadError::UnsupportedCodec)
        ));
        assert!(encoding_name(None).is_err());
        assert_eq!(provider.calls.load(Ordering::Relaxed), 0);
    }
}
