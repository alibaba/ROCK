use super::*;
use std::os::fd::AsRawFd;
use std::os::unix::fs::FileExt;
use std::sync::atomic::{AtomicUsize, Ordering};

use crate::snapshot::metadata::{
    compute_layer_content_sha256, DataCodec, DataSource, LayerRange, LayerRangeMapRef, LayerRef,
    RockletArtifactCompatibility, SnapshotProvenance, StorageDescriptor, StorageProvider,
    VmmCompatibility,
};
use crate::snapshot::repository::frozen_layer::{
    CapturedSnapshotPin, FrozenLayerMetadata, FrozenLayerPin, PackedOwnershipMap, VmStateMetadata,
    VmStatePin,
};
use crate::snapshot::repository::lineage::{
    RemoteLayerSource, SnapshotLineage, SourceResidency, SourceResidencyHandle,
};
use crate::snapshot::transport::posix_file::{self, LocalFileCache};

fn frozen(id: &str, kind: LayerKind, states: &[u8], byte: u8) -> FrozenLayerPin {
    let unit = match kind {
        LayerKind::Memory => 4096,
        LayerKind::Block => 65536,
    };
    let size = states.len() as u64 * unit;
    let file = posix_file::memory_file(c"native-peer-test", size).unwrap();
    let mut packed = vec![0; states.len().div_ceil(4)];
    for (index, &state) in states.iter().enumerate() {
        packed[index / 4] |= state << ((index % 4) * 2);
        if state == 1 {
            file.write_all_at(&vec![byte; unit as usize], index as u64 * unit)
                .unwrap();
        }
    }
    FrozenLayerPin::new(
        FrozenLayerMetadata {
            kind,
            layer_id: format!("{id}-{kind:?}"),
            provisional_content_id: format!("capture:{id}:{kind:?}"),
            logical_size: size,
            alignment: unit,
        },
        PackedOwnershipMap {
            kind,
            snapshot_id: id.to_owned(),
            format_version: 1,
            logical_size: size,
            granularity: unit,
            packed_states: packed,
        },
        file.into(),
        false,
    )
}

fn base(id: &str, states: &[u8]) -> Arc<Snapshot> {
    Snapshot::from_base_layer(
        id.to_owned(),
        frozen(id, LayerKind::Block, states, 0x61),
        SnapshotProvenance {
            oci_ref: None,
            oci_digest: None,
        },
    )
    .unwrap()
}

fn full(id: &str, lineage: SnapshotLineage, block: &[u8]) -> Arc<Snapshot> {
    let state = posix_file::memory_file(c"native-peer-state", 4096).unwrap();
    let bytes = vec![0x53; 4096];
    state.write_all_at(&bytes, 0).unwrap();
    let readonly = posix_file::reopen_readonly(state.as_raw_fd()).unwrap();
    let pin = CapturedSnapshotPin::new(
        id.to_owned(),
        vec![
            frozen(id, LayerKind::Memory, &[1, 2, 0, 1], 0x71),
            frozen(id, LayerKind::Block, block, 0x72),
        ],
        VmStatePin::new(
            VmStateMetadata {
                format_version: 1,
                content_id: sha256_identity(&bytes),
                size: 4096,
                alignment: 4096,
            },
            readonly.into(),
        ),
    )
    .unwrap();
    Snapshot::from_capture(
        pin,
        lineage,
        RockletArtifactCompatibility {
            identity: "peer-rocklet".into(),
            version: "test".into(),
            raw_sha256: sha256_identity(b"rocklet"),
            logical_bytes: 4096,
            guest_port: 1234,
        },
    )
    .unwrap()
}

fn read_request(
    metadata: &NativeSnapshotMetadata,
    kind: LayerKind,
    ordinal: u32,
    offset: u64,
    length: u64,
) -> NativeReadRangeRequest {
    let layer = metadata
        .layers
        .iter()
        .find(|layer| layer.kind == kind && layer.ordinal == ordinal)
        .unwrap();
    NativeReadRangeRequest {
        generation_id: metadata.generation_id.clone(),
        kind,
        ordinal,
        layer_id: layer.layer_id.clone(),
        layer_generation_id: layer.layer_generation_id.clone(),
        offset,
        length,
    }
}

#[tokio::test]
async fn raw_local_ready_serves_when_compressed_cache_admission_is_exhausted() {
    let snapshot = full("quota-raw", SnapshotLineage::default(), &[1, 2, 0, 1]);
    let directory = tempfile::tempdir().unwrap();
    let cache =
        LocalFileCache::new_with_limit(directory.path().to_owned(), "native-quota", 1).unwrap();
    assert!(cache.create_staged_object(65536).unwrap_err().is_capacity());
    let before = cache.stats().unwrap();
    let registry = P2PSnapshotServeRegistry::new();
    let metadata = registry.register_native(&snapshot).await.unwrap();
    metadata.validate().unwrap();
    assert!(snapshot.spec().is_none());
    assert!(snapshot.manifest().is_none());
    assert!(metadata.layers.iter().all(|layer| layer.identity_kind
        == NativeLayerIdentityKind::NativeGeneration
        && layer.content_identity.starts_with("native-generation:")
        && !layer.content_identity.starts_with("sha256:")));
    let data = read_request(&metadata, LayerKind::Memory, 0, 0, 4096);
    assert_eq!(
        registry
            .read_native_range(&snapshot.snapshot_id, &data)
            .await
            .unwrap(),
        vec![0x71; 4096]
    );
    let zero = read_request(&metadata, LayerKind::Memory, 0, 4096, 4096);
    assert_eq!(
        registry
            .read_native_range(&snapshot.snapshot_id, &zero)
            .await
            .unwrap(),
        vec![0; 4096]
    );
    let inherit = read_request(&metadata, LayerKind::Memory, 0, 8192, 4096);
    assert!(matches!(
        registry
            .read_native_range(&snapshot.snapshot_id, &inherit)
            .await,
        Err(P2PServeError::NotFound)
    ));
    let coverage = registry
        .native_coverage(
            &snapshot.snapshot_id,
            &NativeCoverageRequest {
                generation_id: metadata.generation_id.clone(),
                kind: LayerKind::Memory,
                ordinal: 0,
                layer_id: data.layer_id.clone(),
                layer_generation_id: data.layer_generation_id.clone(),
                offset: 0,
                length: 16384,
            },
        )
        .await
        .unwrap();
    assert_eq!(coverage.packed_states, [0b01001001]);
    assert_eq!(
        cache.stats().unwrap(),
        before,
        "native bytes must never enter the compressed LRU quota"
    );
    assert_eq!(
        (before.used_bytes, before.pinned_bytes, before.staging_bytes),
        (0, 0, 0)
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn blocked_and_failed_encoding_does_not_block_or_remove_native_peer_data() {
    let snapshot = base("blocked-raw", &[1, 2]);
    let encoder = Arc::clone(&snapshot);
    let (entered, started) = tokio::sync::oneshot::channel();
    let (release, wait_release) = std::sync::mpsc::channel();
    let attempts = Arc::new(AtomicUsize::new(0));
    let encode_attempts = Arc::clone(&attempts);
    let encode = tokio::task::spawn_blocking(move || {
        let entered = std::sync::Mutex::new(Some(entered));
        encoder.upload_plan_with_scratch(
            StorageDescriptor {
                provider: StorageProvider::S3,
                endpoint_profile: "offline".into(),
                credential_ref: "none".into(),
            },
            VmmCompatibility {
                architecture: "x86_64".into(),
                vmm_type: "cloud-hypervisor".into(),
                vmm_state_format_version: None,
                rocklet_artifact: None,
            },
            &|_| {
                encode_attempts.fetch_add(1, Ordering::Relaxed);
                if let Some(entered) = entered.lock().unwrap().take() {
                    entered.send(()).unwrap();
                }
                wait_release.recv().unwrap();
                Err(
                    crate::snapshot::repository::frozen_layer::FrozenLayerError::Io(
                        "deliberate encoder admission failure".into(),
                    ),
                )
            },
        )
    });
    started.await.unwrap();
    let registry = P2PSnapshotServeRegistry::new();
    let metadata = tokio::time::timeout(
        std::time::Duration::from_secs(2),
        registry.register_native(&snapshot),
    )
    .await
    .unwrap()
    .unwrap();
    let request = read_request(&metadata, LayerKind::Block, 0, 0, 65536);
    let payload = tokio::time::timeout(
        std::time::Duration::from_secs(2),
        registry.read_native_range(&snapshot.snapshot_id, &request),
    )
    .await
    .unwrap()
    .unwrap();
    assert_eq!(payload, vec![0x61; 65536]);
    assert_eq!(attempts.load(Ordering::Relaxed), 1);
    release.send(()).unwrap();
    assert!(encode.await.unwrap().is_err());
    assert!(snapshot.spec().is_none());
    assert_eq!(
        registry
            .native_metadata(&snapshot.snapshot_id, Some(&metadata.generation_id))
            .await
            .unwrap(),
        metadata
    );
    assert_eq!(
        registry
            .read_native_range(&snapshot.snapshot_id, &request)
            .await
            .unwrap(),
        vec![0x61; 65536]
    );
    assert_eq!(
        attempts.load(Ordering::Relaxed),
        1,
        "native peer reads never retry encoding"
    );
}

#[tokio::test]
async fn ancestor_data_integrity_failure_revokes_existing_child_native_tokens() {
    use crate::snapshot::instance::LocalSnapshotError;
    use crate::snapshot::repository::frozen_layer::FrozenLayerError;
    use crate::snapshot::repository::SnapshotRepository;

    let parent = base("integrity-parent", &[1, 2, 0, 1]);
    let child = full(
        "integrity-child",
        SnapshotLineage::default().with_frozen(Arc::clone(&parent)),
        &[0, 1, 2, 0],
    );
    let unrelated = base("integrity-unrelated", &[1]);
    let catalog = SnapshotRepository::new();
    for snapshot in [&parent, &child, &unrelated] {
        catalog.register_raw(Arc::clone(snapshot)).unwrap();
    }
    let registry = P2PSnapshotServeRegistry::new();
    let parent_metadata = registry.register_native(&parent).await.unwrap();
    let child_metadata = registry.register_native(&child).await.unwrap();
    let unrelated_metadata = registry.register_native(&unrelated).await.unwrap();
    let parent_request = read_request(&parent_metadata, LayerKind::Block, 0, 0, 65536);
    let inherited_request = read_request(&child_metadata, LayerKind::Block, 0, 0, 65536);
    let child_request = read_request(&child_metadata, LayerKind::Block, 1, 65536, 65536);
    assert_eq!(
        registry
            .read_native_range(&child.snapshot_id, &inherited_request)
            .await
            .unwrap(),
        vec![0x61; 65536]
    );
    assert_eq!(
        registry
            .read_native_range(&child.snapshot_id, &child_request)
            .await
            .unwrap(),
        vec![0x72; 65536]
    );

    let parent_raw = parent.backing_handle(LayerKind::Block).unwrap();
    let inode = parent_raw.backing.physical_identity().unwrap();
    let writable = parent_raw.backing.try_clone_file().unwrap();
    let directory = tempfile::tempdir().unwrap();
    let cache = LocalFileCache::new(directory.path().to_owned(), "ancestor-integrity").unwrap();
    let error = parent
        .upload_plan_with_scratch(
            StorageDescriptor {
                provider: StorageProvider::S3,
                endpoint_profile: "offline".into(),
                credential_ref: "none".into(),
            },
            VmmCompatibility {
                architecture: "x86_64".into(),
                vmm_type: "cloud-hypervisor".into(),
                vmm_state_format_version: None,
                rocklet_artifact: None,
            },
            &|capacity| {
                // Admission follows source hashing and precedes the writer's
                // second read. Preserve a real writable alias to the parent.
                writable.write_all_at(&[0x6d], 17).unwrap();
                cache
                    .create_staged_object(capacity)
                    .map_err(|error| FrozenLayerError::Io(error.to_string()))
            },
        )
        .unwrap_err();
    let LocalSnapshotError::SourceIntegrity(failure) = error else {
        panic!("changed parent DATA must retain its typed integrity cause");
    };
    assert_eq!(failure.logical_offset, 0);
    assert_eq!(failure.length, 65536);
    assert!(parent.is_closed().unwrap());
    assert!(!child.is_closed().unwrap());
    assert!(child
        .lifecycle()
        .lock()
        .unwrap()
        .source_integrity_failure
        .is_none());
    assert!(matches!(
        child.ensure_source_integrity(),
        Err(LocalSnapshotError::SourceIntegrity(inherited)) if inherited == failure
    ));

    // Neither an old inherited-range token nor a token for unchanged child
    // DATA exempts the entire child cut from its ancestor's quarantine.
    for (snapshot, metadata, request) in [
        (&parent, &parent_metadata, &parent_request),
        (&child, &child_metadata, &inherited_request),
        (&child, &child_metadata, &child_request),
    ] {
        assert!(matches!(
            registry
                .native_metadata(&snapshot.snapshot_id, Some(&metadata.generation_id))
                .await,
            Err(P2PServeError::Invalid(_))
        ));
        assert!(matches!(
            registry
                .read_native_range(&snapshot.snapshot_id, request)
                .await,
            Err(P2PServeError::Invalid(_))
        ));
    }

    assert!(catalog.get(&parent.snapshot_id).is_some());
    let retained = parent.backing_handle(LayerKind::Block).unwrap();
    assert!(Arc::ptr_eq(&parent_raw.backing, &retained.backing));
    assert_eq!(retained.backing.physical_identity().unwrap(), inode);
    assert!(retained.backing.reclaim_unused().await.unwrap().is_empty());
    assert!(!retained.backing.has_reclaimed_data());
    let mut expected = vec![0x61; 65536];
    expected[17] = 0x6d;
    assert_eq!(
        retained.backing.read_data(0, 65536).await.unwrap(),
        expected
    );

    assert_eq!(
        registry
            .native_metadata(
                &unrelated.snapshot_id,
                Some(&unrelated_metadata.generation_id)
            )
            .await
            .unwrap(),
        unrelated_metadata
    );
    assert_eq!(
        registry
            .read_native_range(
                &unrelated.snapshot_id,
                &read_request(&unrelated_metadata, LayerKind::Block, 0, 0, 65536)
            )
            .await
            .unwrap(),
        vec![0x61; 65536]
    );
}

#[derive(Debug)]
struct MustNotRestore;
#[async_trait::async_trait]
impl crate::snapshot::io::backing::BackingRecovery for MustNotRestore {
    async fn read_data(
        &self,
        _offset: u64,
        _length: u64,
    ) -> Result<Vec<u8>, crate::snapshot::io::backing::BackingError> {
        panic!("resident-only peer reads must never recover or fetch missing DATA")
    }
}

#[tokio::test]
async fn partial_native_reclaim_is_a_range_miss_and_does_not_hide_resident_ancestors() {
    let parent = base("raw-parent", &[1, 1, 1, 1]);
    let parent_weak = Arc::downgrade(&parent);
    let snapshot = full(
        "raw-child",
        SnapshotLineage::default().with_frozen(Arc::clone(&parent)),
        &[0, 1, 2, 0],
    );
    let backing = parent.backing_handle(LayerKind::Block).unwrap();
    backing
        .backing
        .register_consumer("still-live", &[(65536, 3 * 65536)])
        .unwrap();
    backing.backing.install_recovery(Arc::new(MustNotRestore));
    assert_eq!(
        backing.backing.reclaim_unused().await.unwrap(),
        vec![(0, 65536)]
    );
    drop(parent);
    let registry = P2PSnapshotServeRegistry::new();
    let metadata = registry.register_native(&snapshot).await.unwrap();
    let missing = read_request(&metadata, LayerKind::Block, 0, 0, 65536);
    assert!(matches!(
        registry
            .read_native_range(&snapshot.snapshot_id, &missing)
            .await,
        Err(P2PServeError::NotFound)
    ));
    let resident = read_request(&metadata, LayerKind::Block, 0, 65536, 65536);
    assert_eq!(
        registry
            .read_native_range(&snapshot.snapshot_id, &resident)
            .await
            .unwrap(),
        vec![0x61; 65536]
    );
    let child = read_request(&metadata, LayerKind::Block, 1, 65536, 65536);
    assert_eq!(
        registry
            .read_native_range(&snapshot.snapshot_id, &child)
            .await
            .unwrap(),
        vec![0x72; 65536]
    );
    let availability = registry
        .native_availability(&snapshot.snapshot_id, &metadata.generation_id, false)
        .await;
    assert!(availability.available && !availability.complete);
    assert_eq!(
        availability.servable_layers, 3,
        "one missing native unit cannot hide the ancestor's other DATA ranges"
    );
    assert!(
        parent_weak.upgrade().is_some(),
        "the child pins immutable parent metadata and resources"
    );
    drop(snapshot);
    assert!(
        parent_weak.upgrade().is_none(),
        "peer registry must not retain snapshot/parent ownership"
    );
    assert!(matches!(
        registry.native_metadata("raw-child", None).await,
        Err(P2PServeError::NotFound)
    ));
}

#[tokio::test]
async fn remote_ancestor_uses_existing_raw_residency_and_never_reads_a_sparse_miss() {
    let unit = 65536;
    let size = 4 * unit;
    let bytes = vec![0x6c; unit as usize];
    let mut map = LayerRangeMap {
        registry_objects: None,
        format_version: 1,
        kind: LayerKind::Block,
        content_sha256: String::new(),
        logical_bytes: size,
        unit_size_bytes: 512,
        ranges: (0..4)
            .map(|index| {
                let kind = match index {
                    0 | 2 => RangeKind::Data,
                    1 => RangeKind::Zero,
                    _ => RangeKind::Inherit,
                };
                LayerRange {
                    kind,
                    logical_offset: index * unit,
                    length: unit,
                    source: (kind == RangeKind::Data).then(|| DataSource {
                        object_key: "origin-must-not-be-read".into(),
                        object_sha256: sha256_identity(b"unavailable-origin"),
                        object_version: None,
                        pack_offset: index * unit,
                        encoded_length: unit,
                        decoded_length: unit,
                        codec: DataCodec::Raw,
                        checksum_sha256: sha256_identity(&bytes),
                    }),
                }
            })
            .collect(),
        zfile: None,
        registry: None,
    };
    map.content_sha256 = compute_layer_content_sha256(&map).unwrap();
    let map = Arc::new(map);
    let directory = tempfile::tempdir().unwrap();
    let raw =
        crate::snapshot::io::backing::LocalBackingRegistry::new(directory.path(), "peer").unwrap();
    let handle = raw
        .acquire(crate::snapshot::io::backing::LayerBackingIdentity {
            kind: crate::snapshot::io::backing::LayerKind::Block,
            format_version: 1,
            logical_size: size,
            unit_size: 512,
            content_sha256: map.content_sha256.trim_start_matches("sha256:").to_owned(),
        })
        .await
        .unwrap();
    handle
        .backing
        .try_clone_file()
        .unwrap()
        .write_all_at(&bytes, 0)
        .unwrap();
    let loaded =
        crate::snapshot::io::backing::DataRangeKey::new(&handle.backing.backing_id, 0, unit)
            .unwrap();
    handle
        .backing
        .downloads
        .ensure_loaded(loaded, || async { Ok::<_, io::Error>(()) })
        .await
        .unwrap();
    let missing =
        crate::snapshot::io::backing::DataRangeKey::new(&handle.backing.backing_id, 2 * unit, unit)
            .unwrap();
    let source = RemoteLayerSource {
        ordinal: 0,
        layer_id: "remote-raw".into(),
        range_map: Arc::clone(&map),
        backing: Arc::new(handle.backing.try_clone_readonly_file().unwrap().into()),
        shared_mapping: None,
        residency: SourceResidency::OnDemand,
        dynamic_residency: Some(SourceResidencyHandle {
            backing_id: handle.backing.backing_id.clone(),
            registry: Arc::clone(&handle.backing.downloads),
        }),
    };
    let index = LayerChainIndex {
        format_version: 1,
        kind: LayerKind::Block,
        logical_bytes: size,
        unit_size_bytes: 512,
        layers: vec![LayerRef {
            ordinal: 0,
            layer_id: source.layer_id.clone(),
            format_version: 1,
            content_sha256: map.content_sha256.clone(),
            range_map: LayerRangeMapRef {
                object_key: format!(
                    "layers/block/1/{}/overlaybd.json",
                    map.content_sha256.trim_start_matches("sha256:")
                ),
                sha256: sha256_identity(b"unread-descriptor"),
                object_version: None,
                encoding: Some(crate::snapshot::metadata::RangeMapEncoding::OverlaybdLsmtV1),
                source_ref: None,
            },
        }],
    };
    let lineage = SnapshotLineage::from_remote(
        "remote-base",
        &HashMap::from([(LayerKind::Block, index)]),
        &[source],
    );
    let snapshot = full("remote-child", lineage, &[0, 0, 0, 0]);
    snapshot
        .retain_layer_resources(vec![
            crate::snapshot::io::layer_resources::LayerResources::new_native(handle.clone(), None),
        ])
        .unwrap();
    let registry = P2PSnapshotServeRegistry::new();
    let metadata = registry.register_native(&snapshot).await.unwrap();
    assert_eq!(
        metadata
            .layers
            .iter()
            .find(|layer| layer.kind == LayerKind::Block && layer.ordinal == 0)
            .unwrap()
            .identity_kind,
        NativeLayerIdentityKind::CanonicalContent
    );
    assert_eq!(
        metadata
            .layers
            .iter()
            .find(|layer| layer.kind == LayerKind::Block && layer.ordinal == 0)
            .unwrap()
            .coverage_unit_size,
        512
    );
    assert_eq!(
        metadata
            .layers
            .iter()
            .find(|layer| layer.kind == LayerKind::Block && layer.ordinal == 1)
            .unwrap()
            .coverage_unit_size,
        65536
    );
    assert_eq!(
        registry
            .read_native_range(
                &snapshot.snapshot_id,
                &read_request(&metadata, LayerKind::Block, 0, 0, unit)
            )
            .await
            .unwrap(),
        bytes
    );
    assert!(matches!(
        registry
            .read_native_range(
                &snapshot.snapshot_id,
                &read_request(&metadata, LayerKind::Block, 0, 2 * unit, unit)
            )
            .await,
        Err(P2PServeError::NotFound)
    ));
    assert_eq!(
        handle
            .backing
            .downloads
            .status(&missing)
            .await
            .attempt_count,
        0,
        "native misses cannot start origin or format loads"
    );
    drop(handle);
    assert_eq!(
        registry
            .read_native_range(
                &snapshot.snapshot_id,
                &read_request(&metadata, LayerKind::Block, 0, 0, unit)
            )
            .await
            .unwrap(),
        vec![0x6c; unit as usize]
    );
    assert!(
        !registry
            .native_availability(&snapshot.snapshot_id, &metadata.generation_id, false)
            .await
            .complete
    );
    let canonical = snapshot.canonical_snapshot().unwrap();
    assert_eq!(
        canonical.chain_indexes[&LayerKind::Block]
            .index
            .unit_size_bytes,
        512
    );
    assert_eq!(
        canonical.layers[&LayerKind::Block]
            .range_map
            .unit_size_bytes,
        512
    );
    assert_eq!(
        snapshot
            .backing_handle(LayerKind::Block)
            .unwrap()
            .backing
            .unit_size(),
        65536,
        "publication load geometry must not alter native COW ownership"
    );
    assert_eq!(
        registry
            .native_metadata(&snapshot.snapshot_id, Some(&metadata.generation_id))
            .await
            .unwrap(),
        metadata,
        "native generation capabilities remain stable after canonical encoding"
    );
}

#[tokio::test]
async fn metadata_only_missing_ancestor_keeps_own_native_data_servable() {
    let content = sha256_identity(b"metadata-only-parent");
    let index = LayerChainIndex {
        format_version: 1,
        kind: LayerKind::Block,
        logical_bytes: 4 * 65536,
        unit_size_bytes: 512,
        layers: vec![LayerRef {
            ordinal: 0,
            layer_id: "metadata-only-parent".into(),
            format_version: 1,
            content_sha256: content.clone(),
            range_map: LayerRangeMapRef {
                object_key: format!(
                    "layers/block/1/{}/overlaybd.json",
                    content.trim_start_matches("sha256:")
                ),
                sha256: sha256_identity(b"metadata-only-descriptor"),
                object_version: None,
                encoding: Some(crate::snapshot::metadata::RangeMapEncoding::OverlaybdLsmtV1),
                source_ref: None,
            },
        }],
    };
    let lineage = SnapshotLineage::from_remote(
        "metadata-only-parent",
        &HashMap::from([(LayerKind::Block, index)]),
        &[],
    );
    let snapshot = full("metadata-only-child", lineage, &[1, 2, 0, 1]);
    let registry = P2PSnapshotServeRegistry::new();
    let metadata = registry.register_native(&snapshot).await.unwrap();
    let missing = read_request(&metadata, LayerKind::Block, 0, 0, 512);
    assert!(matches!(
        registry
            .read_native_range(&snapshot.snapshot_id, &missing)
            .await,
        Err(P2PServeError::NotFound)
    ));
    assert!(matches!(
        registry
            .native_coverage(
                &snapshot.snapshot_id,
                &NativeCoverageRequest {
                    generation_id: missing.generation_id.clone(),
                    kind: missing.kind,
                    ordinal: missing.ordinal,
                    layer_id: missing.layer_id,
                    layer_generation_id: missing.layer_generation_id,
                    offset: 0,
                    length: 512,
                }
            )
            .await,
        Err(P2PServeError::NotFound)
    ));
    assert_eq!(
        registry
            .read_native_range(
                &snapshot.snapshot_id,
                &read_request(&metadata, LayerKind::Block, 1, 0, 65536)
            )
            .await
            .unwrap(),
        vec![0x72; 65536]
    );
    assert_eq!(
        registry
            .read_native_range(
                &snapshot.snapshot_id,
                &read_request(&metadata, LayerKind::Memory, 0, 0, 4096)
            )
            .await
            .unwrap(),
        vec![0x71; 4096]
    );
    let availability = registry
        .native_availability(&snapshot.snapshot_id, &metadata.generation_id, false)
        .await;
    assert!(availability.available && !availability.complete);
    assert!(
        snapshot.spec().is_none(),
        "missing ancestor must not trigger origin metadata or encoding work"
    );
}

#[tokio::test]
async fn native_coverage_is_paged_and_invalidation_rejects_stale_incarnations() {
    let mut states = vec![2; 8192];
    states[0] = 1;
    let snapshot = base("large-native", &states);
    let registry = P2PSnapshotServeRegistry::new();
    let metadata = registry.register_native(&snapshot).await.unwrap();
    assert!(
        serde_json::to_vec(&metadata).unwrap().len() < 2048,
        "bootstrap carries descriptors rather than packed coverage"
    );
    let request = read_request(&metadata, LayerKind::Block, 0, 0, 65536);
    let mut page = NativeCoverageRequest {
        generation_id: metadata.generation_id.clone(),
        kind: request.kind,
        ordinal: request.ordinal,
        layer_id: request.layer_id.clone(),
        layer_generation_id: request.layer_generation_id.clone(),
        offset: 0,
        length: 4096 * 65536,
    };
    assert_eq!(
        registry
            .native_coverage(&snapshot.snapshot_id, &page)
            .await
            .unwrap()
            .packed_states
            .len(),
        1024
    );
    page.length += 65536;
    assert!(matches!(
        registry.native_coverage(&snapshot.snapshot_id, &page).await,
        Err(P2PServeError::Invalid(_))
    ));
    registry.invalidate(&snapshot.snapshot_id).await;
    assert!(matches!(
        registry
            .read_native_range(&snapshot.snapshot_id, &request)
            .await,
        Err(P2PServeError::NotFound)
    ));
    let replacement = base("large-native", &[1]);
    registry.register_native(&replacement).await.unwrap();
    assert!(matches!(
        registry
            .read_native_range(&replacement.snapshot_id, &request)
            .await,
        Err(P2PServeError::IdentityConflict)
    ));
}
