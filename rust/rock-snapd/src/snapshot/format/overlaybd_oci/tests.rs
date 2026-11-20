use super::models::*;
use super::read::read_snapshot_metadata;
use super::*;
use std::collections::BTreeMap;
use std::convert::Infallible;
use std::os::unix::fs::FileExt;

use hyper::service::{make_service_fn, service_fn};
use hyper::{Body, Method, Request, Response, StatusCode};

use crate::snapshot::format::LayerEncodeError;
use crate::snapshot::io::range_io::{
    RangeReadRequest, RangeReadResult, RangeReader, RangeRepresentation,
};
use crate::snapshot::io::snapshot_io::{SnapshotIo, SnapshotReader};
use crate::snapshot::metadata::*;
use crate::snapshot::transport::registry::RegistryClient;
use crate::snapshot::transport::{BlobRef, RangeReadError};
use crate::snapshot::{LazyLoadContext, SnapshotReadRange};
use crate::RuntimeIdentity;

struct Graph {
    metadata: RegistrySnapshotMetadata,
    documents: RegistrySnapshotDocuments,
    objects: BTreeMap<String, Vec<u8>>,
}

fn graph(id: &str) -> Graph {
    graph_with_head(id, false)
}

fn graph_with_head(id: &str, zero_only: bool) -> Graph {
    let mut objects = BTreeMap::new();
    let mut physical = HashMap::<LayerKind, Vec<BlobRef>>::new();
    let mut chains = HashMap::new();
    for kind in [LayerKind::Block, LayerKind::Memory] {
        let unit = if kind == LayerKind::Block {
            65536
        } else {
            4096
        };
        let mut layers = Vec::new();
        let mut blobs = Vec::new();
        for generation in 0..2 {
            let raw =
                crate::snapshot::transport::posix_file::memory_file(c"oci-read-fixture", 4 * unit)
                    .unwrap();
            let mut ranges = Vec::new();
            for ordinal in 0..4 {
                let state = if generation == 0 || ordinal == 0 {
                    RangeKind::Data
                } else if ordinal == 1 {
                    RangeKind::Zero
                } else {
                    RangeKind::Inherit
                };
                let bytes = vec![
                    if generation == 0 {
                        0x30 + ordinal as u8
                    } else {
                        0x91
                    };
                    unit as usize
                ];
                raw.write_all_at(&bytes, ordinal * unit).unwrap();
                let checksum = sha256_identity(&bytes);
                ranges.push(LayerRange {
                    kind: state,
                    logical_offset: ordinal * unit,
                    length: unit,
                    source: (state == RangeKind::Data).then(|| DataSource {
                        object_key: "pending".into(),
                        object_sha256: checksum.clone(),
                        object_version: None,
                        pack_offset: 0,
                        encoded_length: unit,
                        decoded_length: unit,
                        codec: DataCodec::Raw,
                        checksum_sha256: checksum,
                    }),
                });
            }
            if kind == LayerKind::Block && generation == 0 {
                let mut bytes = vec![0; (4 * unit) as usize];
                raw.read_exact_at(&mut bytes, 0).unwrap();
                let checksum = sha256_identity(&bytes);
                ranges = vec![LayerRange {
                    kind: RangeKind::Data,
                    logical_offset: 0,
                    length: 4 * unit,
                    source: Some(DataSource {
                        object_key: "pending".into(),
                        object_sha256: checksum.clone(),
                        object_version: None,
                        pack_offset: 0,
                        encoded_length: 4 * unit,
                        decoded_length: 4 * unit,
                        codec: DataCodec::Raw,
                        checksum_sha256: checksum,
                    }),
                }];
            }
            // Adjacent ZERO/INHERIT runs use the canonical coalesced form.
            if generation != 0 {
                ranges[2].length += unit;
                ranges.pop();
            }
            if generation != 0 && zero_only {
                ranges = vec![LayerRange {
                    kind: RangeKind::Zero,
                    logical_offset: 0,
                    length: 4 * unit,
                    source: None,
                }];
            }
            let mut map = LayerRangeMap {
                format_version: 1,
                kind,
                content_sha256: String::new(),
                logical_bytes: 4 * unit,
                unit_size_bytes: unit,
                ranges,
                zfile: None,
                registry: None,
                registry_objects: None,
            };
            map.content_sha256 = compute_layer_content_sha256(&map).unwrap();
            let encoded =
                crate::snapshot::format::encode_layer::<LayerEncodeError>(map, &raw, &|bound| {
                    crate::snapshot::transport::temporary_staged_object(bound)
                        .map_err(|error| LayerEncodeError::Io(error.to_string()))
                })
                .unwrap();
            let blob = BlobRef {
                digest: encoded.blob.sha256.clone(),
                size: encoded.blob_length,
            };
            let mut bytes = vec![0; blob.size as usize];
            encoded
                .sealed_object
                .file()
                .read_exact_at(&mut bytes, 0)
                .unwrap();
            objects.insert(encoded.blob.object_key.clone(), bytes);
            blobs.push(blob);
            let encoding = if encoded.range_map.zfile.is_some() {
                RangeMapEncoding::OverlaybdZfileLz4V1
            } else {
                RangeMapEncoding::OverlaybdLsmtV1
            };
            let prefix = format!(
                "layers/{}/1/{}/",
                kind.object_key_component(),
                &encoded.range_map.content_sha256[7..]
            );
            let descriptor_key = format!("{prefix}{}", encoding.descriptor_name());
            objects.insert(descriptor_key.clone(), encoded.descriptor.clone());
            objects.insert(
                format!("{prefix}{}", encoding.committed_name()),
                canonical_json_bytes(&json!({
                    "format_version": 1, "kind": kind,
                    "content_sha256": encoded.range_map.content_sha256,
                    "range_map_sha256": encoded.descriptor_sha256,
                }))
                .unwrap(),
            );
            layers.push(LayerRef {
                ordinal: generation,
                layer_id: format!("{}-{generation}", kind.object_key_component()),
                format_version: 1,
                content_sha256: encoded.range_map.content_sha256,
                range_map: LayerRangeMapRef {
                    object_key: descriptor_key,
                    sha256: encoded.descriptor_sha256,
                    object_version: None,
                    encoding: Some(encoding),
                    source_ref: None,
                },
            });
        }
        let chain = LayerChainIndex {
            format_version: 1,
            kind,
            logical_bytes: 4 * unit,
            unit_size_bytes: unit,
            layers,
        };
        chain.validate().unwrap();
        let bytes = canonical_json_bytes(&chain).unwrap();
        let key = format!(
            "snapshots/{id}/{}.index",
            if kind == LayerKind::Block {
                "rootfs"
            } else {
                "memory"
            }
        );
        let reference = ChainIndexRef {
            logical_bytes: 4 * unit,
            index_object_key: key.clone(),
            index_sha256: sha256_identity(&bytes),
            object_version: None,
            source_ref: None,
        };
        objects.insert(key, bytes);
        chains.insert(kind, reference);
        physical.insert(kind, blobs);
    }
    let state = canonical_json_bytes(&json!({
        "magic": "ROCK_VM_STATE", "format_version": 1, "vm_config": {},
        "vm_state": {"snapshots": {"memory-manager": {"snapshot_data": {
            "state": serde_json::to_string(&json!({"guest_ram_mappings": [{
                "slot": 0, "gpa": 0, "size": 4 * 4096, "file_offset": 0,
                "zone_id": null, "virtio_mem": false,
            }]})).unwrap(),
        }}}}
    }))
    .unwrap();
    let state_ref = VmStateRef {
        object_key: format!("snapshots/{id}/vm-state.pack"),
        sha256: sha256_identity(&state),
        object_version: None,
        stored_bytes: state.len() as u64,
    };
    objects.insert(state_ref.object_key.clone(), state);
    let rootfs = chains.remove(&LayerKind::Block).unwrap();
    let memory = chains.remove(&LayerKind::Memory).unwrap();
    let manifest_chain = |reference: &ChainIndexRef| ManifestChainRef {
        object_key: reference.index_object_key.clone(),
        sha256: reference.index_sha256.clone(),
        logical_bytes: reference.logical_bytes,
        object_version: None,
    };
    let manifest = SnapshotManifest {
        format_version: 1,
        snapshot_id: id.into(),
        snapshot_kind: SnapshotKind::Full,
        vm_state: Some(state_ref.clone()),
        memory: Some(manifest_chain(&memory)),
        rootfs: manifest_chain(&rootfs),
    };
    let manifest_bytes = canonical_json_bytes(&manifest).unwrap();
    let manifest_ref = ObjectRef {
        object_key: format!("snapshots/{id}/manifest.json"),
        sha256: sha256_identity(&manifest_bytes),
        object_version: None,
    };
    objects.insert(manifest_ref.object_key.clone(), manifest_bytes);
    let spec = SnapshotSpec {
        format_version: 1,
        registry_snapshot: None,
        provenance: None,
        compatibility: VmmCompatibility {
            architecture: "x86_64".into(),
            vmm_type: "cloud-hypervisor".into(),
            vmm_state_format_version: Some(1),
            rocklet_artifact: Some(RockletArtifactCompatibility {
                identity: sha256_identity(b"rocklet"),
                version: "test-v1".into(),
                raw_sha256: sha256_identity(b"rocklet-raw"),
                logical_bytes: 512,
                guest_port: 12233,
            }),
        },
        storage: StorageDescriptor {
            provider: StorageProvider::Registry,
            endpoint_profile: "test-registry".into(),
            credential_ref: "test-pull".into(),
        },
        manifest: manifest_ref,
        vm_state: Some(state_ref),
        memory: Some(memory),
        rootfs,
    };
    let bindings = objects
        .iter()
        .map(|(key, bytes)| RegistryObjectBinding {
            object_key: key.clone(),
            blob: BlobRef {
                digest: sha256_identity(bytes),
                size: bytes.len() as u64,
            },
            object_version: None,
        })
        .collect();
    let state = objects[&spec.vm_state.as_ref().unwrap().object_key].clone();
    let metadata = RegistrySnapshotMetadata::new(spec, bindings, Vec::new(), Some(&state)).unwrap();
    let documents = build_snapshot_documents(
        &metadata,
        &physical[&LayerKind::Block],
        &physical[&LayerKind::Memory],
    )
    .unwrap();
    Graph {
        metadata,
        documents,
        objects,
    }
}

struct RegistryFixture {
    client: Arc<RegistryClient>,
    server: tokio::task::JoinHandle<()>,
}

impl Drop for RegistryFixture {
    fn drop(&mut self) {
        self.server.abort();
    }
}

fn serve(
    graphs: &[&Graph],
    index: Option<&RegistryDocument>,
    missing: Option<&str>,
) -> RegistryFixture {
    let mut blobs = HashMap::new();
    let mut manifests = HashMap::new();
    for graph in graphs {
        for (key, bytes) in &graph.objects {
            if missing != Some(key.as_str()) {
                blobs.insert(sha256_identity(bytes), bytes.clone());
            }
        }
        for document in [
            &graph.documents.disk_config,
            graph.documents.memory_config.as_ref().unwrap(),
        ] {
            blobs.insert(document.digest.clone(), document.payload.clone());
        }
        for document in [
            &graph.documents.disk_manifest,
            graph.documents.memory_manifest.as_ref().unwrap(),
            &graph.documents.index,
        ] {
            manifests.insert(document.digest.clone(), document.payload.clone());
        }
    }
    if let Some(index) = index {
        manifests.insert(index.digest.clone(), index.payload.clone());
    }
    serve_objects(blobs, manifests)
}

fn serve_objects(
    blobs: HashMap<String, Vec<u8>>,
    manifests: HashMap<String, Vec<u8>>,
) -> RegistryFixture {
    let blobs = Arc::new(blobs);
    let manifests = Arc::new(manifests);
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let address = listener.local_addr().unwrap();
    let client = RegistryClient::test_http_client(&format!("http://{address}"));
    let service = make_service_fn(move |_| {
        let blobs = Arc::clone(&blobs);
        let manifests = Arc::clone(&manifests);
        async move {
            Ok::<_, Infallible>(service_fn(move |request: Request<Body>| {
                let digest = request.uri().path().rsplit('/').next().unwrap().to_owned();
                let is_manifest = request.uri().path().contains("/manifests/");
                let bytes = if is_manifest {
                    manifests.get(&digest)
                } else {
                    blobs.get(&digest)
                };
                let response = match bytes {
                    None => Response::builder().status(404).body(Body::empty()).unwrap(),
                    Some(bytes) if request.method() == Method::HEAD => Response::builder()
                        .status(200)
                        .header("content-length", bytes.len())
                        .header("docker-content-digest", &digest)
                        .body(Body::empty())
                        .unwrap(),
                    Some(bytes) => {
                        let range = request
                            .headers()
                            .get("range")
                            .and_then(|header| header.to_str().ok())
                            .and_then(|value| value.strip_prefix("bytes="))
                            .and_then(|value| value.split_once('-'))
                            .map(|(start, end)| {
                                (
                                    start.parse::<usize>().unwrap(),
                                    end.parse::<usize>().unwrap(),
                                )
                            });
                        if let Some((start, end)) = range {
                            Response::builder()
                                .status(StatusCode::PARTIAL_CONTENT)
                                .header(
                                    "content-range",
                                    format!("bytes {start}-{end}/{}", bytes.len()),
                                )
                                .header("docker-content-digest", &digest)
                                .body(Body::from(bytes[start..=end].to_vec()))
                                .unwrap()
                        } else {
                            Response::builder()
                                .status(200)
                                .header("docker-content-digest", &digest)
                                .body(Body::from(bytes.clone()))
                                .unwrap()
                        }
                    }
                };
                async move { Ok::<_, Infallible>(response) }
            }))
        }
    });
    let server = tokio::spawn(async move {
        hyper::Server::from_tcp(listener)
            .unwrap()
            .serve(service)
            .await
            .unwrap();
    });
    RegistryFixture { client, server }
}

fn reference(fixture: &RegistryFixture, index: &RegistryDocument) -> RegistrySnapshotRef {
    RegistrySnapshotRef {
        registry: fixture.client.registry().into(),
        repository: "team/snapshot".into(),
        index_digest: index.digest.clone(),
    }
}

#[tokio::test]
async fn import_full_oci_preserves_publication_identity_and_caches_verified_start_metadata() {
    let graph = graph("oci-import-full");
    let fixture = serve(&[&graph], None, None);
    let resolver = Arc::new(OverlaybdOciSourceResolver::new_with_registry_clients(
        None,
        vec![Arc::clone(&fixture.client)],
    ));
    let io = SnapshotIo::data_only().with_source_resolver(resolver);
    let reference = reference(&fixture, &graph.documents.index);
    let image = format!(
        "{}/{}@{}",
        reference.registry, reference.repository, reference.index_digest
    );
    let loaded = Arc::clone(&io).import_oci_snapshot(&image).await.unwrap();
    assert_eq!(loaded.snapshot_kind(), SnapshotKind::Full);
    assert_eq!(loaded.snapshot_id(), "oci-import-full");
    assert_eq!(
        loaded.spec().unwrap().registry_snapshot.as_ref(),
        Some(&reference)
    );
    assert!(loaded.vm_state().is_some());
    assert!(loaded.chain_view(LayerKind::Memory).is_some());
    let value = serde_json::to_value(loaded.spec().unwrap()).unwrap();
    let checksum = snapshot_spec_checksum(&value).unwrap();
    let opened = io
        .load_for_start_shared(&value, &checksum, loaded.snapshot_id())
        .await
        .unwrap();
    assert!(Arc::ptr_eq(&loaded, &opened));
}

#[tokio::test]
async fn import_oci_rejects_disk_manifest_and_base_index_instead_of_cold_booting() {
    let mut base = graph("oci-import-base");
    base.metadata.spec.memory = None;
    base.metadata.spec.vm_state = None;
    base.metadata.memory_layout = None;
    base.metadata.spec.compatibility.vmm_type = "any".into();
    base.metadata.spec.compatibility.vmm_state_format_version = None;
    base.metadata.spec.compatibility.rocklet_artifact = None;
    let manifest_key = base.metadata.spec.manifest.object_key.clone();
    let mut manifest: SnapshotManifest =
        serde_json::from_slice(&base.objects[&manifest_key]).unwrap();
    manifest.snapshot_kind = SnapshotKind::BaseImage;
    manifest.memory = None;
    manifest.vm_state = None;
    let payload = canonical_json_bytes(&manifest).unwrap();
    base.metadata.spec.manifest.sha256 = sha256_identity(&payload);
    let binding = base
        .metadata
        .objects
        .iter_mut()
        .find(|object| object.object_key == manifest_key)
        .unwrap();
    binding.blob = BlobRef {
        digest: sha256_identity(&payload),
        size: payload.len() as u64,
    };
    base.objects.insert(manifest_key, payload);
    let disk: Value = serde_json::from_slice(&base.documents.disk_manifest.payload).unwrap();
    let layers = disk["layers"]
        .as_array()
        .unwrap()
        .iter()
        .map(|layer| BlobRef {
            digest: layer["digest"].as_str().unwrap().into(),
            size: layer["size"].as_u64().unwrap(),
        })
        .collect::<Vec<_>>();
    base.documents = build_snapshot_documents(&base.metadata, &layers, &[]).unwrap();
    let fixture = serve(&[&base], None, None);
    for digest in [
        &base.documents.index.digest,
        &base.documents.disk_manifest.digest,
    ] {
        let resolver = Arc::new(OverlaybdOciSourceResolver::new_with_registry_clients(
            None,
            vec![Arc::clone(&fixture.client)],
        ));
        let io = SnapshotIo::data_only().with_source_resolver(resolver);
        let image = format!("{}/team/snapshot@{digest}", fixture.client.registry());
        assert!(io.import_oci_snapshot(&image).await.is_err());
    }
}

#[test]
fn private_platform_is_two_components_and_metadata_blobs_are_reachable() {
    let graph = graph("oci-platform");
    let index: Value = serde_json::from_slice(&graph.documents.index.payload).unwrap();
    assert_eq!(index["manifests"].as_array().unwrap().len(), 2);
    assert_eq!(index["manifests"][0]["platform"]["architecture"], "amd64");
    assert_eq!(
        index["manifests"][1]["platform"]["architecture"],
        "snapd-memory-amd64"
    );
    let disk: Value = serde_json::from_slice(&graph.documents.disk_manifest.payload).unwrap();
    assert_eq!(
        disk["layers"][0]["mediaType"],
        "application/vnd.oci.image.layer.v1.tar"
    );
    assert_eq!(
        disk["layers"][0]["annotations"]["containerd.io/snapshot/overlaybd/version"],
        "0.1.0"
    );
    let private: Value =
        serde_json::from_slice(&graph.documents.memory_manifest.as_ref().unwrap().payload).unwrap();
    assert!(disk["layers"]
        .as_array()
        .unwrap()
        .iter()
        .all(|layer| layer["annotations"][LAYER_ROLE] == "disk"));
    assert!(private["layers"]
        .as_array()
        .unwrap()
        .iter()
        .any(|layer| layer["mediaType"] == VM_STATE_LAYER));
    let mut bad = graph.metadata.clone();
    bad.memory_layout.as_mut().unwrap().unit_size_bytes = 65536;
    assert!(bad.validate().is_err());
    let mut bad = graph.metadata.clone();
    bad.spec.compatibility.architecture = MEMORY_ARCHITECTURE.into();
    assert!(bad.validate().is_err());
}

#[test]
fn memory_regions_bind_the_vmm_pack_and_reject_invalid_or_missing_layouts() {
    let graph = graph("oci-regions");
    let spec = &graph.metadata.spec;
    let bytes = &graph.objects[&spec.vm_state.as_ref().unwrap().object_key];
    let layout = memory_layout_from_state(spec, bytes).unwrap();
    assert_eq!(layout.regions.len(), 1);
    assert_eq!(layout.regions[0].guest_phys_addr, 0);
    assert_eq!(layout.regions[0].logical_offset, 0);
    assert_eq!(layout.regions[0].length, 16384);
    for failure in 0..5 {
        let mut state: Value = serde_json::from_slice(bytes).unwrap();
        let mut manager: Value = serde_json::from_str(
            state["vm_state"]["snapshots"]["memory-manager"]["snapshot_data"]["state"]
                .as_str()
                .unwrap(),
        )
        .unwrap();
        match failure {
            0 => {
                let duplicate = manager["guest_ram_mappings"][0].clone();
                manager["guest_ram_mappings"]
                    .as_array_mut()
                    .unwrap()
                    .push(duplicate);
            }
            1 => {
                manager["guest_ram_mappings"][0]["size"] = json!(32768);
            }
            2 => {
                manager["guest_ram_mappings"][0]["gpa"] = json!(1);
            }
            3 => {
                manager["guest_ram_mappings"] = json!([]);
            }
            _ => {
                state["format_version"] = json!(2);
            }
        }
        state["vm_state"]["snapshots"]["memory-manager"]["snapshot_data"]["state"] =
            json!(serde_json::to_string(&manager).unwrap());
        let bytes = canonical_json_bytes(&state).unwrap();
        let mut spec = spec.clone();
        let reference = spec.vm_state.as_mut().unwrap();
        reference.sha256 = sha256_identity(&bytes);
        reference.stored_bytes = bytes.len() as u64;
        assert!(memory_layout_from_state(&spec, &bytes).is_err());
    }
    let mut corrupt = bytes.to_vec();
    corrupt.push(b' ');
    assert!(memory_layout_from_state(spec, &corrupt).is_err());
}

#[derive(Debug)]
struct NoProvider;
#[async_trait]
impl RangeReader for NoProvider {
    async fn read_range(&self, _: RangeReadRequest<'_>) -> Result<RangeReadResult, RangeReadError> {
        panic!("Registry read escaped its authenticated object map")
    }
}

#[tokio::test]
async fn fresh_registry_loader_restores_both_incremental_chains_and_native_disk_input() {
    let graph = graph("oci-fresh");
    let fixture = serve(&[&graph], None, None);
    let reference = reference(&fixture, &graph.documents.index);
    let read = read_snapshot_metadata(&fixture.client, &reference)
        .await
        .unwrap();
    assert_eq!(read, graph.metadata);
    let selected = fixture
        .client
        .resolve_manifest(
            &reference.repository,
            &reference.index_digest,
            "linux/amd64",
        )
        .await
        .unwrap();
    assert_eq!(selected.digest, graph.documents.disk_manifest.digest);
    let resolver = Arc::new(OverlaybdOciSourceResolver::new_with_registry_clients(
        None,
        vec![Arc::clone(&fixture.client)],
    ));
    let legacy = resolver
        .import_registry(
            &reference.registry,
            &reference.repository,
            &selected.digest,
            "linux/amd64",
        )
        .await
        .unwrap();
    assert!(legacy.range_map.registry.is_some());
    let spec = read.bound_spec(&reference).unwrap();
    let value = serde_json::to_value(&spec).unwrap();
    let checksum = snapshot_spec_checksum(&value).unwrap();
    let loaded = SnapshotIo::data_only()
        .with_source_resolver(resolver)
        .load(&value, &checksum, &read.snapshot_id)
        .await
        .unwrap();
    assert_eq!(
        loaded.vm_state().unwrap(),
        graph.objects[&spec.vm_state.as_ref().unwrap().object_key]
    );
    for kind in [LayerKind::Memory, LayerKind::Block] {
        let chain = loaded.chain_view(kind).unwrap();
        assert_eq!(chain.layers.len(), 2);
        let unit = chain.index.unit_size_bytes;
        assert_eq!(chain.layers[1].range_map.ranges[1].kind, RangeKind::Zero);
        assert_eq!(chain.layers[1].range_map.ranges[2].kind, RangeKind::Inherit);
        assert!(chain
            .layers
            .iter()
            .all(|layer| layer.range_map.registry_objects.is_some()));
        let context = LazyLoadContext::new(
            Arc::new(NoProvider),
            RuntimeIdentity::new("oci-reader"),
            None,
        );
        for (ordinal, offset, expected) in [(1, 0, 0x91), (0, 2 * unit, 0x32)] {
            let layer = &chain.layers[ordinal];
            // Reopening from a retained map models fresh fork/lineage use.
            let reader = crate::snapshot::format::reader::LayerReadCapability::for_encoding(
                Arc::clone(&layer.range_map),
                layer.layer_ref.range_map.encoding.unwrap(),
            );
            let bytes = reader
                .read_data_range(
                    &context,
                    SnapshotReadRange {
                        kind,
                        layer_content_sha256: &layer.layer_ref.content_sha256,
                        logical_offset: offset,
                        length: unit,
                    },
                )
                .await
                .unwrap()
                .payload;
            assert_eq!(bytes, vec![expected; unit as usize]);
        }
    }
}

#[tokio::test]
async fn malformed_or_cross_generation_private_entries_fail_closed() {
    let first = graph("oci-first");
    let second = graph("oci-second");
    for failure in 0..3 {
        let mut value: Value = serde_json::from_slice(&first.documents.index.payload).unwrap();
        match failure {
            0 => {
                value["manifests"].as_array_mut().unwrap().pop();
            }
            1 => {
                value["manifests"][1] = value["manifests"][0].clone();
            }
            _ => {
                let replacement: Value =
                    serde_json::from_slice(&second.documents.index.payload).unwrap();
                value["manifests"][1] = replacement["manifests"][1].clone();
            }
        }
        let index = RegistryDocument::new(OCI_INDEX, value).unwrap();
        let fixture = serve(&[&first, &second], Some(&index), None);
        assert!(
            read_snapshot_metadata(&fixture.client, &reference(&fixture, &index))
                .await
                .is_err()
        );
    }
}

#[tokio::test]
async fn native_manifest_roles_and_order_must_match_the_authenticated_chains() {
    use crate::snapshot::io::snapshot_io::SnapshotLoadError;
    let original = graph("oci-native-order");
    for failure in 0..6 {
        let mut forged = Graph {
            metadata: original.metadata.clone(),
            documents: original.documents.clone(),
            objects: original.objects.clone(),
        };
        let mut disk: Value =
            serde_json::from_slice(&forged.documents.disk_manifest.payload).unwrap();
        let mut memory: Value =
            serde_json::from_slice(&forged.documents.memory_manifest.as_ref().unwrap().payload)
                .unwrap();
        let mut disk_config: Value =
            serde_json::from_slice(&forged.documents.disk_config.payload).unwrap();
        let mut memory_config: Value =
            serde_json::from_slice(&forged.documents.memory_config.as_ref().unwrap().payload)
                .unwrap();
        match failure {
            0 => {
                disk["layers"].as_array_mut().unwrap().reverse();
            }
            1 => {
                let disk_layer = disk["layers"][0].clone();
                let memory_layer = memory["layers"][0].clone();
                disk["layers"][0] = memory_layer;
                disk["layers"][0]["annotations"][LAYER_ROLE] = json!("disk");
                memory["layers"][0] = disk_layer;
                memory["layers"][0]["annotations"][LAYER_ROLE] = json!("memory");
            }
            2 => {
                let repeated = disk["layers"][1].clone();
                disk["layers"].as_array_mut().unwrap().push(repeated);
            }
            3 => {
                let mut dropped = disk["layers"].as_array_mut().unwrap().remove(0);
                dropped["mediaType"] = json!(AUXILIARY_LAYER);
                dropped["annotations"][LAYER_ROLE] = json!("metadata");
                // Preserve all reachable object digests while removing one
                // required disk layer from the ordinary platform's chain.
                memory["layers"].as_array_mut().unwrap().push(dropped);
            }
            4 => {
                memory["layers"].as_array_mut().unwrap()[..2].reverse();
            }
            _ => {}
        }
        let digests = |manifest: &Value| {
            manifest["layers"]
                .as_array()
                .unwrap()
                .iter()
                .map(|layer| layer["digest"].clone())
                .collect::<Vec<_>>()
        };
        disk_config["rootfs"]["diff_ids"] = json!(digests(&disk));
        memory_config["rootfs"]["diff_ids"] = json!(digests(&memory));
        if failure == 5 {
            disk_config["rootfs"]["diff_ids"][0] = json!(format!("sha256:{}", "e".repeat(64)));
        }
        // Rehash every changed document. The snapd config and its generation
        // digest remain unchanged; hash pinning alone cannot establish order.
        forged.documents.disk_config = RegistryDocument::new(OCI_CONFIG, disk_config).unwrap();
        forged.documents.memory_config =
            Some(RegistryDocument::new(OCI_CONFIG, memory_config).unwrap());
        disk["config"] = forged.documents.disk_config.descriptor();
        memory["config"] = forged
            .documents
            .memory_config
            .as_ref()
            .unwrap()
            .descriptor();
        forged.documents.disk_manifest = RegistryDocument::new(OCI_MANIFEST, disk).unwrap();
        forged.documents.memory_manifest =
            Some(RegistryDocument::new(OCI_MANIFEST, memory).unwrap());
        let mut index: Value = serde_json::from_slice(&forged.documents.index.payload).unwrap();
        index["manifests"][0]["digest"] = json!(forged.documents.disk_manifest.digest);
        index["manifests"][0]["size"] = json!(forged.documents.disk_manifest.payload.len());
        index["manifests"][1]["digest"] =
            json!(forged.documents.memory_manifest.as_ref().unwrap().digest);
        index["manifests"][1]["size"] = json!(forged
            .documents
            .memory_manifest
            .as_ref()
            .unwrap()
            .payload
            .len());
        forged.documents.index = RegistryDocument::new(OCI_INDEX, index).unwrap();
        let fixture = serve(&[&forged], None, None);
        let reference = reference(&fixture, &forged.documents.index);
        let spec = forged.metadata.bound_spec(&reference).unwrap();
        let value = serde_json::to_value(spec).unwrap();
        let checksum = snapshot_spec_checksum(&value).unwrap();
        let resolver = Arc::new(OverlaybdOciSourceResolver::new_with_registry_clients(
            None,
            vec![fixture.client.clone()],
        ));
        let error = SnapshotIo::data_only()
            .with_source_resolver(resolver)
            .load(&value, &checksum, &forged.metadata.snapshot_id)
            .await
            .unwrap_err();
        let SnapshotLoadError::IdentityConflict(message) = error else {
            panic!("Malformed native chain returned the wrong failure: {error:?}");
        };
        assert!(
            message.contains(if failure == 5 {
                "diff IDs"
            } else {
                "native manifest order"
            }),
            "{message}"
        );
    }
}

#[tokio::test]
async fn zero_only_native_heads_still_participate_in_manifest_order_binding() {
    let graph = graph_with_head("oci-zero-heads", true);
    let fixture = serve(&[&graph], None, None);
    let reference = reference(&fixture, &graph.documents.index);
    let spec = graph.metadata.bound_spec(&reference).unwrap();
    let value = serde_json::to_value(spec).unwrap();
    let checksum = snapshot_spec_checksum(&value).unwrap();
    let resolver = Arc::new(OverlaybdOciSourceResolver::new_with_registry_clients(
        None,
        vec![fixture.client.clone()],
    ));
    let loaded = SnapshotIo::data_only()
        .with_source_resolver(resolver)
        .load(&value, &checksum, &graph.metadata.snapshot_id)
        .await
        .unwrap();
    for kind in [LayerKind::Memory, LayerKind::Block] {
        let chain = loaded.chain_view(kind).unwrap();
        assert_eq!(chain.layers.len(), 2);
        assert_eq!(chain.layers[1].range_map.ranges.len(), 1);
        assert_eq!(chain.layers[1].range_map.ranges[0].kind, RangeKind::Zero);
    }
}

#[tokio::test]
async fn fresh_full_registry_graph_registers_idempotently_and_preserves_catalog_immutability() {
    let original = graph("oci-full-catalog");
    let disk: Value = serde_json::from_slice(&original.documents.disk_manifest.payload).unwrap();
    let memory: Value =
        serde_json::from_slice(&original.documents.memory_manifest.as_ref().unwrap().payload)
            .unwrap();
    let disk_layers = accelerated_layers(&disk).unwrap();
    let memory_layers = memory["layers"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|layer| layer["annotations"][LAYER_ROLE] == "memory")
        .map(layer_ref)
        .collect::<Result<Vec<_>, _>>()
        .unwrap();
    let mut conflicting = Graph {
        metadata: original.metadata.clone(),
        documents: original.documents.clone(),
        objects: original.objects.clone(),
    };
    // This is another fully self-consistent OCI root for the same snapshot ID.
    // Changing its immutable spec must remain a real catalog conflict.
    conflicting.metadata.spec.storage.credential_ref = "another-pull-profile".into();
    conflicting.documents =
        build_snapshot_documents(&conflicting.metadata, &disk_layers, &memory_layers).unwrap();
    let fixture = serve(&[&original, &conflicting], None, None);
    let open = |graph: &Graph| {
        let reference = reference(&fixture, &graph.documents.index);
        let spec = graph.metadata.bound_spec(&reference).unwrap();
        let value = serde_json::to_value(spec).unwrap();
        let checksum = snapshot_spec_checksum(&value).unwrap();
        let resolver = Arc::new(OverlaybdOciSourceResolver::new_with_registry_clients(
            None,
            vec![fixture.client.clone()],
        ));
        (
            SnapshotIo::data_only().with_source_resolver(resolver),
            value,
            checksum,
        )
    };
    let catalog = crate::snapshot::repository::SnapshotRepository::new();
    let (loader, value, checksum) = open(&original);
    let loaded = loader
        .load(&value, &checksum, &original.metadata.snapshot_id)
        .await
        .unwrap();
    let canonical = catalog.register_opened(loaded.clone()).unwrap();
    assert!(Arc::ptr_eq(&canonical, &loaded));
    assert!(Arc::ptr_eq(
        &catalog.register_opened(loaded).unwrap(),
        &canonical
    ));
    let (fresh, value, checksum) = open(&original);
    let reopened = fresh
        .load(&value, &checksum, &original.metadata.snapshot_id)
        .await
        .unwrap();
    assert!(!Arc::ptr_eq(&reopened, &canonical));
    assert!(Arc::ptr_eq(
        &catalog.register_opened(reopened).unwrap(),
        &canonical
    ));
    let (fresh, value, checksum) = open(&conflicting);
    let other = fresh
        .load(&value, &checksum, &original.metadata.snapshot_id)
        .await
        .unwrap();
    assert!(catalog
        .register_opened(other)
        .unwrap_err()
        .contains("catalog metadata is immutable"));
    assert!(Arc::ptr_eq(
        &catalog.get(&original.metadata.snapshot_id).unwrap(),
        &canonical
    ));
}

#[tokio::test]
async fn missing_graph_metadata_is_an_error_on_a_fresh_data_only_loader() {
    let graph = graph("oci-missing");
    let missing = graph
        .objects
        .keys()
        .find(|key| key.contains("COMMITTED"))
        .unwrap();
    let fixture = serve(&[&graph], None, Some(missing));
    let reference = reference(&fixture, &graph.documents.index);
    let spec = graph.metadata.bound_spec(&reference).unwrap();
    let value = serde_json::to_value(&spec).unwrap();
    let checksum = snapshot_spec_checksum(&value).unwrap();
    let resolver = Arc::new(OverlaybdOciSourceResolver::new_with_registry_clients(
        None,
        vec![fixture.client.clone()],
    ));
    assert!(SnapshotIo::data_only()
        .with_source_resolver(resolver)
        .load_for_start(&value, &checksum, &graph.metadata.snapshot_id)
        .await
        .is_err());
}

#[tokio::test]
async fn object_map_preserves_logical_versions_and_raw_range_representation() {
    let graph = graph("oci-versions");
    let fixture = serve(&[&graph], None, None);
    let mut bindings = graph.metadata.objects.clone();
    let binding = bindings
        .iter_mut()
        .find(|binding| binding.object_key.contains("/data/"))
        .unwrap();
    binding.object_version = Some("old-s3-version".into());
    let selected = binding.clone();
    let map = RegistryObjectMap::new(
        fixture.client.clone(),
        "team/snapshot".into(),
        &bindings,
        None,
    )
    .unwrap();
    let runtime = RuntimeIdentity::new("raw-version");
    let request = RangeReadRequest {
        physical: None,
        authority: None,
        runtime: &runtime,
        kind: crate::snapshot::io::range_io::RangeDataKind::Block,
        object_key: &selected.object_key,
        object_sha256: &selected.blob.digest,
        expected_range_sha256: "",
        offset: 16,
        length: 8,
        object_version: Some("old-s3-version"),
        preferred_p2p_source: None,
    };
    let read = map.read_range(request).await.unwrap();
    assert_eq!(read.representation, RangeRepresentation::DecodedData);
    assert_eq!(read.payload, graph.objects[&selected.object_key][16..24]);
    assert!(map
        .read_range(RangeReadRequest {
            object_version: Some("wrong-version"),
            ..request
        })
        .await
        .is_err());
    assert_eq!(
        map.object_version(&selected.object_key),
        Some("old-s3-version")
    );
    // The source descriptor still names an old authority; only copied bytes
    // whose digest and size are in this authenticated map may be redirected.
    let bytes = map
        .read_object_range(
            "old-registry.invalid",
            crate::snapshot::transport::PhysicalRangeRequest {
                namespace: Some("old/repository"),
                object_key: &selected.blob.digest,
                object_sha256: &selected.blob.digest,
                object_size: Some(selected.blob.size),
                object_version: None,
                offset: 16,
                length: 8,
            },
        )
        .await
        .unwrap();
    assert_eq!(bytes, read.payload);
    let redirected = map
        .read_range(RangeReadRequest {
            authority: Some("old-registry.invalid"),
            object_key: &selected.blob.digest,
            object_sha256: &selected.blob.digest,
            object_version: None,
            physical: Some(crate::snapshot::transport::PhysicalRangeRequest {
                namespace: Some("old/repository"),
                object_key: &selected.blob.digest,
                object_sha256: &selected.blob.digest,
                object_size: Some(selected.blob.size),
                object_version: None,
                offset: 16,
                length: 8,
            }),
            ..request
        })
        .await
        .unwrap();
    assert_eq!(
        redirected.representation,
        RangeRepresentation::EncodedObject
    );
    assert_eq!(redirected.payload, read.payload);
}

#[tokio::test]
async fn full_registry_envelope_obeys_the_metadata_byte_budget() {
    use crate::snapshot::io::snapshot_io::{SnapshotLoadError, SnapshotLoadLimits};
    let graph = graph("oci-budget");
    let fixture = serve(&[&graph], None, None);
    let reference = reference(&fixture, &graph.documents.index);
    let spec = graph.metadata.bound_spec(&reference).unwrap();
    let value = serde_json::to_value(&spec).unwrap();
    let checksum = snapshot_spec_checksum(&value).unwrap();
    let resolver = Arc::new(OverlaybdOciSourceResolver::new_with_registry_clients(
        None,
        vec![fixture.client.clone()],
    ));
    let store = RegistryObjectMap::new(
        fixture.client.clone(),
        reference.repository.clone(),
        &graph.metadata.objects,
        None,
    )
    .unwrap();
    let io = SnapshotIo::new_with_limits(
        store,
        SnapshotLoadLimits {
            max_total_metadata_bytes: 1,
            ..SnapshotLoadLimits::default()
        },
    )
    .unwrap()
    .with_source_resolver(resolver);
    assert!(matches!(
        io.load_for_start(&value, &checksum, &graph.metadata.snapshot_id)
            .await,
        Err(SnapshotLoadError::BoundExceeded(_))
    ));
}

#[derive(Debug)]
struct DeadOriginalRegistry;

#[async_trait]
impl crate::snapshot::SourceResolver for DeadOriginalRegistry {
    async fn resolve_source(&self, _: &SourceRef) -> Result<Arc<ResolvedSource>, SourceError> {
        panic!("Publication revisited a dead original Registry or evictable cache")
    }
}

#[tokio::test]
async fn publication_reconstructs_dadi_source_from_parent_custody_after_origin_and_cache_are_gone()
{
    use crate::snapshot::io::registry_store::{RegistryCopyBudget, RegistryGraphStore};
    use crate::snapshot::transport::registry::RegistryWriteTarget;
    use crate::snapshot::transport::{
        IoCancellation, NamespaceMutationFence, UploadTransactionLease,
    };
    use std::time::Duration;
    use tokio::sync::Semaphore;
    let original = ResolvedSource::from_registry(
        crate::snapshot::format::overlaybd::composite::test_registry_rootfs(),
    )
    .unwrap();
    let key = format!(
        "layers/block/1/{}/registry-overlaybd.json",
        &original.descriptor_sha256[7..]
    );
    let descriptor = BlobRef {
        digest: original.descriptor_sha256.clone(),
        size: original.descriptor.len() as u64,
    };
    // No source-native DATA, original manifest or metadata cache is served.
    let fixture = serve_objects(
        HashMap::from([(descriptor.digest.clone(), original.descriptor.clone())]),
        HashMap::new(),
    );
    let SourceRef::Registry {
        registry: old_origin,
        ..
    } = &original.source_ref;
    assert_ne!(old_origin, fixture.client.registry());
    let parent = RegistryObjectMap::new(
        fixture.client.clone(),
        "team/snapshot".into(),
        &[RegistryObjectBinding {
            object_key: key,
            blob: descriptor,
            object_version: None,
        }],
        None,
    )
    .unwrap();
    let fence = NamespaceMutationFence::new(format!("{}/team/snapshot", fixture.client.registry()))
        .unwrap();
    let target = RegistryWriteTarget::new(
        fixture.client.clone(),
        "team/snapshot".into(),
        "descriptor-custody-test".into(),
        Arc::clone(&fence),
    )
    .unwrap();
    let lease = UploadTransactionLease::acquire(fence).await;
    let (_sender, receiver) = tokio::sync::watch::channel(false);
    let parents = vec![parent.clone()];
    let store = RegistryGraphStore::new(
        target,
        None,
        None,
        parents.clone(),
        Vec::new(),
        Some(lease),
        RegistryCopyBudget {
            transfer_permits: Arc::new(Semaphore::new(1)),
            buffer_permits: Arc::new(Semaphore::new(4096)),
            chunk_bytes: 2048,
            attempt_timeout: Duration::from_secs(5),
            cancellation: IoCancellation::new(receiver),
        },
    )
    .unwrap();
    let resolver = super::publication::RegistryPublicationResolver {
        fallback: &DeadOriginalRegistry,
        store: store.as_ref(),
        sources: &parents,
    };
    let loaded = resolver.resolve_source(&original.source_ref).await.unwrap();
    assert_eq!(loaded.source_ref, original.source_ref);
    assert_eq!(loaded.descriptor, original.descriptor);
    assert_eq!(loaded.range_map, original.range_map);
}
