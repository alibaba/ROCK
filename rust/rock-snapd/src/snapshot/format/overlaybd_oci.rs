//! OverlayBD snapshots described by an immutable OCI/DADI source.
//!
//! Registry references retain their digest-pinned authority and reconstruct a
//! common snapshot graph without publishing S3 metadata or copying blob DATA.
//! All Registry I/O goes through RangeIO; LSMT/ZFile parsing remains OverlayBD.
pub(crate) mod publication;

mod models;
mod read;
pub(crate) use models::{
    build_snapshot_documents, RegistryEmbeddedSource, RegistryObjectBinding,
    RegistrySnapshotDocuments, RegistrySnapshotMetadata, MAX_SNAPSHOT_OBJECTS,
};
pub(crate) use read::resolve_full_snapshot_metadata;
pub use read::RegistryObjectMap;
#[cfg(test)]
mod tests;

use crate::snapshot::format::{
    validate_snapshot_paths, DecodedSnapshotMetadata, ImmutableObject, LayerUploadPlan,
    SnapshotMetadataFormat, SnapshotUploadError, SnapshotUploadPlan,
};

use std::collections::HashMap;
use std::sync::Arc;

use async_trait::async_trait;
use serde_json::{json, Value};
use tokio::sync::{Mutex, Semaphore};

use crate::snapshot::format::overlaybd::composite::{
    self, RegistryRootfs, RegistryRootfsReadError,
};
use crate::snapshot::io::range_io::RangeIO;
use crate::snapshot::io::snapshot_io::{
    source_load_error, SnapshotLoadError, SnapshotMetadataReadContext,
};
use crate::snapshot::metadata::{
    canonical_json_bytes, sha256_identity, ChainIndexRef, LayerChainIndex, LayerKind,
    LayerRangeMapRef, LayerRef, ManifestChainRef, ObjectRef, RangeMapEncoding, SnapshotChainView,
    SnapshotKind, SnapshotLayerView, SnapshotManifest, SnapshotProvenance, SnapshotSpec, SourceRef,
    StorageDescriptor, StorageProvider, VmmCompatibility,
};
use crate::snapshot::transport::posix_file::LocalFileCache;
use crate::snapshot::transport::registry::{
    layer_ref, RegistryError, RegistryLayerRef, RegistryReference,
};
use crate::snapshot::transport::{BlobRangeError, BlobRangeErrorKind};
#[cfg(test)]
use crate::snapshot::SnapshotInstance;
use crate::snapshot::ValidateCanonicalModel;

const MAX_REGISTRY_LAYERS: usize = 64;
const MAX_RETAINED_SOURCES: usize = 64;
const MAX_CONCURRENT_IMPORTS: usize = 4;

use crate::snapshot::{ResolvedSource, SourceError, SourceResolver};

/// Format builder for a digest-pinned OCI graph on the common snapshot.
#[derive(Debug)]
pub struct OverlaybdOciSnapshot;

impl OverlaybdOciSnapshot {
    pub fn creating(snapshot_id: &str) -> Arc<crate::snapshot::Snapshot> {
        crate::snapshot::Snapshot::creating(snapshot_id, SnapshotKind::BaseImage)
    }

    fn new(
        snapshot_id: String,
        spec: SnapshotSpec,
        manifest: SnapshotManifest,
        rootfs: SnapshotChainView,
    ) -> Result<Arc<crate::snapshot::Snapshot>, String> {
        crate::snapshot::Snapshot::from_persistent(
            snapshot_id,
            crate::snapshot::instance::PersistentSnapshot {
                spec,
                manifest,
                memory: None,
                rootfs,
                vm_state: None,
                ancestor_snapshot_ids: Vec::new(),
            },
        )
    }

    pub(crate) fn from_registry(
        rootfs: Arc<RegistryRootfs>,
        published: &crate::snapshot::format::UploadedSnapshot,
    ) -> Result<Arc<crate::snapshot::Snapshot>, String> {
        let resolved = ResolvedSource::from_registry(rootfs).map_err(|error| error.to_string())?;
        let snapshot_id = &published.manifest.snapshot_id;
        let mut graph =
            build_registry_base_graph(snapshot_id, &resolved, published.spec.compatibility.clone())
                .map_err(|error| error.to_string())?;
        bind_registry_base_provenance(&mut graph.spec, published.spec.provenance.clone())
            .map_err(|error| error.to_string())?;
        if graph.spec != published.spec || graph.manifest != published.manifest {
            return Err("OCI source differs from the published catalog root".into());
        }
        let layer_ref = graph.chain.layers[0].clone();
        Self::new(
            snapshot_id.clone(),
            graph.spec,
            graph.manifest,
            SnapshotChainView {
                index: graph.chain,
                layers: vec![SnapshotLayerView {
                    layer_ref,
                    range_map: Arc::new(resolved.range_map.clone()),
                }],
            },
        )
    }
}

/// Reconstruct the common snapshot envelope from a digest-pinned OCI source.
#[derive(Debug)]
pub(crate) struct OverlaybdOciMetadata;

#[async_trait]
impl SnapshotMetadataFormat for OverlaybdOciMetadata {
    fn validate_spec_layout(
        &self,
        spec: &SnapshotSpec,
        snapshot_id: &str,
        _expected_storage: Option<&StorageDescriptor>,
    ) -> Result<(), SnapshotLoadError> {
        // Legacy image roots pin a selected source manifest. Published full
        // snapshots pin an immutable index. Neither inherits node S3 authority.
        spec.validate()
            .map_err(SnapshotLoadError::InvalidSnapshotMetadata)?;
        validate_snapshot_paths(spec, snapshot_id)
    }

    async fn load(
        &self,
        io: &SnapshotMetadataReadContext<'_>,
        spec: SnapshotSpec,
        snapshot_id: &str,
        verify_data_objects: bool,
    ) -> Result<DecodedSnapshotMetadata, SnapshotLoadError> {
        if spec.registry_snapshot.is_some() {
            return read::load_snapshot(io, spec, snapshot_id, verify_data_objects).await;
        }
        let source_ref = spec.rootfs.source_ref.as_ref().ok_or_else(|| {
            SnapshotLoadError::InvalidSnapshotMetadata(
                "Registry base has no source reference".into(),
            )
        })?;
        let resolved = io
            .source_resolver
            .resolve_source(source_ref)
            .await
            .map_err(source_load_error)?;
        let mut graph =
            build_registry_base_graph(snapshot_id, &resolved, spec.compatibility.clone())
                .map_err(source_load_error)?;
        bind_registry_base_provenance(&mut graph.spec, spec.provenance.clone())
            .map_err(source_load_error)?;
        // No S3 object is authoritative for a Registry base. Reconstruct every
        // synthetic reference from the pinned source and compare the complete
        // catalog spec before making any bytes available to the runtime.
        if graph.spec != spec {
            return Err(SnapshotLoadError::IdentityConflict(
                "Registry base metadata differs from its catalog spec".into(),
            ));
        }
        let metadata_bytes = graph
            .descriptor
            .payload
            .len()
            .saturating_add(graph.committed.payload.len())
            .saturating_add(graph.chain_index.payload.len())
            .saturating_add(graph.manifest_object.payload.len());
        if graph.descriptor.payload.len() > io.limits.max_range_map_bytes
            || graph.chain_index.payload.len() > io.limits.max_chain_index_bytes
            || graph.manifest_object.payload.len() > io.limits.max_manifest_bytes
            || metadata_bytes > io.limits.max_total_metadata_bytes
        {
            return Err(SnapshotLoadError::BoundExceeded(
                "Registry base metadata".into(),
            ));
        }
        let layer_ref = graph.chain.layers[0].clone();
        Ok(DecodedSnapshotMetadata {
            metadata: OverlaybdOciSnapshot::new(
                snapshot_id.to_owned(),
                spec,
                graph.manifest,
                SnapshotChainView {
                    index: graph.chain,
                    layers: vec![SnapshotLayerView {
                        layer_ref,
                        range_map: Arc::new(resolved.range_map.clone()),
                    }],
                },
            )
            .map_err(SnapshotLoadError::InvalidSnapshotMetadata)?,
            metadata_bytes,
        })
    }
}

impl From<RegistryError> for SourceError {
    fn from(error: RegistryError) -> Self {
        Self::Transport(error.into())
    }
}

impl From<RegistryRootfsReadError> for SourceError {
    fn from(error: RegistryRootfsReadError) -> Self {
        match error {
            RegistryRootfsReadError::Source(error) => Self::Transport(error),
            RegistryRootfsReadError::Invalid(message) => Self::Invalid(message),
            RegistryRootfsReadError::Integrity(message) => Self::Integrity(message),
        }
    }
}

impl ResolvedSource {
    pub(crate) fn from_registry(rootfs: Arc<RegistryRootfs>) -> Result<Arc<Self>, SourceError> {
        let descriptor = rootfs.encode_descriptor().map_err(SourceError::Invalid)?;
        let descriptor_sha256 = sha256_identity(&descriptor);
        let source_ref = SourceRef::Registry {
            registry: rootfs.registry().into(),
            repository: rootfs.repository().into(),
            manifest_digest: rootfs.manifest_digest().into(),
            platform: rootfs.platform().into(),
            descriptor_sha256: descriptor_sha256.clone(),
        };
        source_ref.validate().map_err(SourceError::Invalid)?;
        let range_map = rootfs.build_map().map_err(SourceError::Invalid)?;
        Ok(Arc::new(Self {
            source_ref,
            range_map,
            descriptor,
            descriptor_sha256,
        }))
    }

    pub(crate) fn validate(&self) -> Result<(), SourceError> {
        self.source_ref.validate().map_err(SourceError::Invalid)?;
        let SourceRef::Registry {
            descriptor_sha256, ..
        } = &self.source_ref;
        if &self.descriptor_sha256 != descriptor_sha256
            || sha256_identity(&self.descriptor) != *descriptor_sha256
            || self.range_map.content_sha256 != *descriptor_sha256
        {
            return Err(SourceError::Integrity(
                "source descriptor identity differs".into(),
            ));
        }
        let rootfs = self.range_map.registry.as_ref().ok_or_else(|| {
            SourceError::Integrity("Registry source has no authenticated format capability".into())
        })?;
        let expected = Self::from_registry(Arc::clone(rootfs))?;
        if expected.source_ref != self.source_ref
            || expected.descriptor != self.descriptor
            || expected.range_map != self.range_map
        {
            return Err(SourceError::Integrity(
                "source differs from its pinned format map".into(),
            ));
        }
        Ok(())
    }
}

#[derive(Debug)]
pub(crate) struct OverlaybdOciSourceResolver {
    local_cache: Option<Arc<LocalFileCache>>,
    configured_clients: HashMap<String, Arc<crate::snapshot::transport::registry::RegistryClient>>,
    remembered: Mutex<HashMap<SourceRef, Arc<ResolvedSource>>>,
    ranges: Mutex<HashMap<String, Arc<RangeIO>>>,
    discovered: Mutex<HashMap<String, SourceRef>>,
    imports: Semaphore,
    #[cfg(test)]
    fixture_importer: Option<Arc<dyn SourceResolver>>,
}

impl Default for OverlaybdOciSourceResolver {
    fn default() -> Self {
        Self::new(None)
    }
}

impl OverlaybdOciSourceResolver {
    pub(crate) fn new(local_cache: Option<Arc<LocalFileCache>>) -> Self {
        Self::new_with_registry_clients(local_cache, Vec::new())
    }

    pub(crate) fn new_with_registry_clients(
        local_cache: Option<Arc<LocalFileCache>>,
        clients: Vec<Arc<crate::snapshot::transport::registry::RegistryClient>>,
    ) -> Self {
        Self {
            local_cache,
            remembered: Mutex::new(HashMap::new()),
            configured_clients: clients
                .into_iter()
                .map(|client| (client.registry().to_owned(), client))
                .collect(),
            ranges: Mutex::new(HashMap::new()),
            discovered: Mutex::new(HashMap::new()),
            imports: Semaphore::new(MAX_CONCURRENT_IMPORTS),
            #[cfg(test)]
            fixture_importer: None,
        }
    }

    async fn ranges(&self, registry: &str) -> Result<Arc<RangeIO>, SourceError> {
        let mut sources = self.ranges.lock().await;
        if let Some(ranges) = sources.get(registry) {
            return Ok(Arc::clone(ranges));
        }
        let client = self.registry_client(registry).await?;
        let ranges = RangeIO::for_registry_client_with_cache(client, self.local_cache.as_ref().map(|cache| {
            Arc::clone(cache) as Arc<dyn crate::snapshot::transport::interfaces::physical_cache::PhysicalRangeCache>
        }));
        if sources.len() >= MAX_RETAINED_SOURCES {
            sources.clear();
        }
        sources.insert(registry.into(), Arc::clone(&ranges));
        Ok(ranges)
    }

    async fn read_cached(&self, source: &SourceRef) -> Option<Arc<ResolvedSource>> {
        let cache = Arc::clone(self.local_cache.as_ref()?);
        let source = source.clone();
        tokio::task::spawn_blocking(move || {
            let SourceRef::Registry {
                descriptor_sha256, ..
            } = &source;
            let key = metadata_cache_key(&source).ok()?;
            let payload = cache.read_metadata(&key, descriptor_sha256).ok()??;
            let rootfs = composite::decode_descriptor(&payload).ok()?;
            let resolved = ResolvedSource::from_registry(rootfs).ok()?;
            (resolved.source_ref == source && resolved.descriptor == payload).then_some(resolved)
        })
        .await
        .ok()
        .flatten()
    }

    async fn retain(&self, resolved: &Arc<ResolvedSource>) {
        let SourceRef::Registry {
            registry,
            repository,
            manifest_digest,
            platform,
            ..
        } = &resolved.source_ref;
        let key = registry_pin_key(registry, repository, manifest_digest, platform);
        {
            let mut known = self.discovered.lock().await;
            if known.len() >= MAX_RETAINED_SOURCES {
                known.clear();
            }
            known.insert(key, resolved.source_ref.clone());
        }
        if let Some(cache) = &self.local_cache {
            let cache = Arc::clone(cache);
            let resolved = Arc::clone(resolved);
            let _ = tokio::task::spawn_blocking(move || {
                let key =
                    metadata_cache_key(&resolved.source_ref).map_err(|error| error.to_string())?;
                cache
                    .persist_metadata(&key, &resolved.descriptor, &resolved.descriptor_sha256)
                    .map_err(|error| error.to_string())
            })
            .await;
        }
    }

    async fn import_pinned(
        &self,
        registry: &str,
        repository: &str,
        manifest_digest: &str,
        platform: &str,
    ) -> Result<Arc<ResolvedSource>, SourceError> {
        // Validate every location component before constructing a network URL.
        SourceRef::Registry {
            registry: registry.into(),
            repository: repository.into(),
            manifest_digest: manifest_digest.into(),
            platform: platform.into(),
            descriptor_sha256: format!("sha256:{}", "0".repeat(64)),
        }
        .validate()
        .map_err(SourceError::Invalid)?;
        let _permit = self.imports.acquire().await.map_err(|_| {
            SourceError::Transport(BlobRangeError::new(
                BlobRangeErrorKind::Transient,
                "source import admission closed",
            ))
        })?;
        #[cfg(test)]
        if let Some(importer) = &self.fixture_importer {
            return importer
                .import_registry(registry, repository, manifest_digest, platform)
                .await;
        }
        let ranges = self.ranges(registry).await?;
        let manifest = ranges
            .registry_manifest(registry, repository, manifest_digest)
            .await?;
        if manifest.digest != manifest_digest || manifest.value.get("manifests").is_some() {
            return Err(SourceError::Integrity(
                "Registry source must pin a selected image manifest".into(),
            ));
        }
        validate_platform(
            ranges.as_ref(),
            registry,
            repository,
            &manifest.value,
            platform,
        )
        .await?;
        let layers = accelerated_layers(&manifest.value)?;
        let rootfs = RegistryRootfs::import_typed(
            ranges.as_ref(),
            registry,
            repository,
            manifest_digest,
            platform,
            &layers,
        )
        .await?;
        ResolvedSource::from_registry(rootfs)
    }
}

#[async_trait]
impl SourceResolver for OverlaybdOciSourceResolver {
    async fn remember_registry_source(
        &self,
        source: &SourceRef,
        descriptor: &[u8],
    ) -> Result<(), SourceError> {
        let rootfs = composite::decode_descriptor(descriptor).map_err(SourceError::Invalid)?;
        let resolved = ResolvedSource::from_registry(rootfs)?;
        if resolved.source_ref != *source || resolved.descriptor != descriptor {
            return Err(SourceError::Integrity(
                "Embedded source differs from its pinned descriptor".into(),
            ));
        }
        {
            let mut remembered = self.remembered.lock().await;
            if remembered.len() >= MAX_RETAINED_SOURCES && !remembered.contains_key(source) {
                remembered.clear();
            }
            remembered.insert(source.clone(), Arc::clone(&resolved));
        }
        self.retain(&resolved).await;
        Ok(())
    }
    async fn registry_client(
        &self,
        registry: &str,
    ) -> Result<Arc<crate::snapshot::transport::registry::RegistryClient>, SourceError> {
        self.configured_clients
            .get(registry)
            .cloned()
            .map(Ok)
            .unwrap_or_else(|| {
                crate::snapshot::transport::registry::RegistryClient::new(registry)
                    .map_err(Into::into)
            })
    }

    fn registry_physical_cache(
        &self,
    ) -> Option<Arc<dyn crate::snapshot::transport::PhysicalRangeCache>> {
        self.local_cache.as_ref().map(|cache| {
            Arc::clone(cache) as Arc<dyn crate::snapshot::transport::PhysicalRangeCache>
        })
    }
    async fn resolve_source(&self, source: &SourceRef) -> Result<Arc<ResolvedSource>, SourceError> {
        source.validate().map_err(SourceError::Invalid)?;
        if let Some(remembered) = self.remembered.lock().await.get(source).cloned() {
            return Ok(remembered);
        }
        if let Some(cached) = self.read_cached(source).await {
            return Ok(cached);
        }
        let SourceRef::Registry {
            registry,
            repository,
            manifest_digest,
            platform,
            ..
        } = source;
        let resolved = self
            .import_pinned(registry, repository, manifest_digest, platform)
            .await?;
        if &resolved.source_ref != source {
            return Err(SourceError::Integrity(
                "rebuilt descriptor differs from the fixed source identity".into(),
            ));
        }
        self.retain(&resolved).await;
        Ok(resolved)
    }

    async fn import_registry(
        &self,
        registry: &str,
        repository: &str,
        manifest_digest: &str,
        platform: &str,
    ) -> Result<Arc<ResolvedSource>, SourceError> {
        let key = registry_pin_key(registry, repository, manifest_digest, platform);
        let known = self.discovered.lock().await.get(&key).cloned();
        let resolved = match known {
            Some(source) => self.resolve_source(&source).await?,
            None => {
                self.import_pinned(registry, repository, manifest_digest, platform)
                    .await?
            }
        };
        self.retain(&resolved).await;
        Ok(resolved)
    }
}

fn registry_pin_key(registry: &str, repository: &str, digest: &str, platform: &str) -> String {
    format!("{registry}/{repository}@{digest}#{platform}")
}

fn metadata_cache_key(source: &SourceRef) -> Result<String, SourceError> {
    let bytes =
        canonical_json_bytes(source).map_err(|error| SourceError::Invalid(error.to_string()))?;
    Ok(format!(
        "sources/registry/{}/descriptor.json",
        &sha256_identity(&bytes)[7..]
    ))
}

async fn validate_platform(
    ranges: &RangeIO,
    registry: &str,
    repository: &str,
    manifest: &Value,
    platform: &str,
) -> Result<(), SourceError> {
    let config = layer_ref(
        manifest
            .get("config")
            .ok_or_else(|| SourceError::Invalid("Registry manifest has no config".into()))?,
    )?;
    let bytes = ranges
        .registry_blob_metadata(registry, repository, &config)
        .await?;
    let config: Value = serde_json::from_slice(&bytes)
        .map_err(|_| SourceError::Invalid("Registry image config is not JSON".into()))?;
    let parts = platform.split('/').collect::<Vec<_>>();
    if config.get("os").and_then(Value::as_str) != Some(parts[0])
        || config.get("architecture").and_then(Value::as_str) != Some(parts[1])
        || parts
            .get(2)
            .is_some_and(|variant| config.get("variant").and_then(Value::as_str) != Some(*variant))
    {
        return Err(SourceError::Integrity(
            "Registry config differs from the pinned platform".into(),
        ));
    }
    Ok(())
}

#[derive(Debug, Clone)]
pub(crate) struct SourceMetadata {
    pub object_key: String,
    pub sha256: String,
    pub payload: Vec<u8>,
}

impl SourceMetadata {
    fn new(object_key: String, payload: Vec<u8>) -> Self {
        Self {
            object_key,
            sha256: sha256_identity(&payload),
            payload,
        }
    }
}

pub(crate) struct RegistryBaseGraph {
    pub spec: SnapshotSpec,
    pub manifest: SnapshotManifest,
    pub chain: LayerChainIndex,
    pub descriptor: SourceMetadata,
    pub committed: SourceMetadata,
    pub chain_index: SourceMetadata,
    pub manifest_object: SourceMetadata,
}

/// Pure graph construction shared by initial import and cold restore. The
/// object paths are virtual names with SHA bindings, not a demand to write S3.
pub(crate) fn build_registry_base_graph(
    snapshot_id: &str,
    resolved: &ResolvedSource,
    compatibility: VmmCompatibility,
) -> Result<RegistryBaseGraph, SourceError> {
    resolved.validate()?;
    let map = &resolved.range_map;
    let SourceRef::Registry {
        registry,
        repository,
        manifest_digest,
        ..
    } = &resolved.source_ref;
    let encoding = RangeMapEncoding::RegistryOverlaybdV1;
    let content = map.content_sha256.trim_start_matches("sha256:");
    let prefix = format!("layers/block/1/{content}/");
    let descriptor = SourceMetadata::new(
        format!("{prefix}{}", encoding.descriptor_name()),
        resolved.descriptor.clone(),
    );
    let encode = |value: &Value| {
        canonical_json_bytes(value).map_err(|error| SourceError::Invalid(error.to_string()))
    };
    let committed = SourceMetadata::new(
        format!("{prefix}{}", encoding.committed_name()),
        encode(&json!({
            "content_sha256": map.content_sha256,
            "format_version": 1,
            "range_map_sha256": descriptor.sha256,
            "kind": LayerKind::Block,
        }))?,
    );
    let chain = LayerChainIndex {
        format_version: 1,
        kind: LayerKind::Block,
        logical_bytes: map.logical_bytes,
        unit_size_bytes: map.unit_size_bytes,
        layers: vec![LayerRef {
            ordinal: 0,
            layer_id: format!("registry-{content}"),
            format_version: 1,
            content_sha256: map.content_sha256.clone(),
            range_map: LayerRangeMapRef {
                object_key: descriptor.object_key.clone(),
                sha256: descriptor.sha256.clone(),
                object_version: None,
                encoding: Some(encoding),
                source_ref: Some(resolved.source_ref.clone()),
            },
        }],
    };
    chain.validate().map_err(SourceError::Invalid)?;
    let chain_index = SourceMetadata::new(
        format!("snapshots/{snapshot_id}/rootfs.index"),
        canonical_json_bytes(&chain).map_err(|error| SourceError::Invalid(error.to_string()))?,
    );
    let manifest = SnapshotManifest {
        format_version: 1,
        snapshot_id: snapshot_id.into(),
        snapshot_kind: SnapshotKind::BaseImage,
        vm_state: None,
        memory: None,
        rootfs: ManifestChainRef {
            object_key: chain_index.object_key.clone(),
            sha256: chain_index.sha256.clone(),
            logical_bytes: map.logical_bytes,
            object_version: None,
        },
    };
    manifest.validate().map_err(SourceError::Invalid)?;
    let manifest_object = SourceMetadata::new(
        format!("snapshots/{snapshot_id}/manifest.json"),
        canonical_json_bytes(&manifest).map_err(|error| SourceError::Invalid(error.to_string()))?,
    );
    let spec = SnapshotSpec {
        registry_snapshot: None,
        format_version: 1,
        compatibility,
        provenance: Some(SnapshotProvenance {
            oci_ref: Some(format!("{registry}/{repository}@{manifest_digest}")),
            oci_digest: Some(manifest_digest.clone()),
        }),
        storage: StorageDescriptor {
            provider: StorageProvider::Registry,
            endpoint_profile: "registry".into(),
            credential_ref: "anonymous-pull:v1".into(),
        },
        manifest: ObjectRef {
            object_key: manifest_object.object_key.clone(),
            sha256: manifest_object.sha256.clone(),
            object_version: None,
        },
        vm_state: None,
        memory: None,
        rootfs: ChainIndexRef {
            logical_bytes: map.logical_bytes,
            index_object_key: chain_index.object_key.clone(),
            index_sha256: chain_index.sha256.clone(),
            object_version: None,
            source_ref: Some(resolved.source_ref.clone()),
        },
    };
    spec.validate().map_err(SourceError::Invalid)?;
    Ok(RegistryBaseGraph {
        spec,
        manifest,
        chain,
        descriptor,
        committed,
        chain_index,
        manifest_object,
    })
}

pub(crate) fn accelerated_layers(manifest: &Value) -> Result<Vec<RegistryLayerRef>, SourceError> {
    let layers = manifest
        .get("layers")
        .and_then(Value::as_array)
        .filter(|layers| !layers.is_empty() && layers.len() <= MAX_REGISTRY_LAYERS)
        .ok_or_else(|| SourceError::Invalid("accelerated layer count is outside bounds".into()))?;
    let native = manifest.get("artifactType").and_then(Value::as_str)
        == Some("application/vnd.containerd.overlaybd.native.v1+json");
    let mut output = Vec::with_capacity(layers.len());
    for layer in layers {
        let declared = layer_ref(layer).map_err(SourceError::from)?;
        let annotations = layer.get("annotations");
        let dali_digest = annotations.and_then(|value| value.get("dadi.image.layer.v1.digest"));
        let dali_size = annotations.and_then(|value| value.get("dadi.image.layer.v1.size"));
        if dali_digest.is_some() || dali_size.is_some() {
            let digest = dali_digest.and_then(Value::as_str).ok_or_else(|| {
                SourceError::Invalid("DADI locator has no native blob digest".into())
            })?;
            let size = dali_size
                .and_then(Value::as_str)
                .and_then(|value| value.parse::<u64>().ok())
                .ok_or_else(|| {
                    SourceError::Invalid("DADI locator has no native blob size".into())
                })?;
            output.push(
                layer_ref(&serde_json::json!({"digest": digest, "size": size}))
                    .map_err(SourceError::from)?,
            );
        } else if native {
            // Native annotations, when present, must agree with the actual
            // manifest descriptor rather than overriding its immutable identity.
            if let Some(digest) = annotations
                .and_then(|value| value.get("containerd.io/snapshot/overlaybd/blob-digest"))
            {
                if digest.as_str() != Some(declared.digest.as_str()) {
                    return Err(SourceError::Invalid(
                        "native blob annotation conflicts with descriptor digest".into(),
                    ));
                }
            }
            if let Some(size) = annotations
                .and_then(|value| value.get("containerd.io/snapshot/overlaybd/blob-size"))
            {
                if size.as_str().and_then(|value| value.parse::<u64>().ok()) != Some(declared.size)
                {
                    return Err(SourceError::Invalid(
                        "native blob annotation conflicts with descriptor size".into(),
                    ));
                }
            }
            output.push(declared);
        } else {
            return Err(SourceError::Invalid(
                "accelerated reference is neither a DADI locator nor native OverlayBD manifest"
                    .into(),
            ));
        }
    }
    Ok(output)
}

/// Construct the catalog-owned Registry base graph without publishing any S3
/// object. The same source-layer helper is used by a fresh restore loader, so
/// these references remain reconstructible across daemon and worker changes.
pub(crate) fn registry_base_plan(
    snapshot_id: &str,
    rootfs: &Arc<RegistryRootfs>,
    compatibility: VmmCompatibility,
) -> Result<SnapshotUploadPlan, SnapshotUploadError> {
    let resolved = ResolvedSource::from_registry(Arc::clone(rootfs))
        .map_err(|error| SnapshotUploadError::InvalidPlan(error.to_string()))?;
    let graph = build_registry_base_graph(snapshot_id, &resolved, compatibility)
        .map_err(|error| SnapshotUploadError::InvalidPlan(error.to_string()))?;
    let object = |value: SourceMetadata| ImmutableObject {
        object_key: value.object_key,
        payload: value.payload,
        sha256: value.sha256,
    };
    let layer = LayerUploadPlan::from_requests(
        LayerKind::Block,
        1,
        resolved.range_map.content_sha256.clone(),
        Vec::new(),
        object(graph.descriptor),
        object(graph.committed),
    )?;
    let plan = SnapshotUploadPlan {
        registry_sources: Vec::new(),
        snapshot_id: snapshot_id.into(),
        snapshot_kind: SnapshotKind::BaseImage,
        spec: graph.spec,
        layers: vec![layer],
        chain_indexes: vec![object(graph.chain_index)],
        vm_state: None,
        manifest: object(graph.manifest_object),
    };
    plan.validate_shallow()?;
    Ok(plan)
}

/// Preserve the caller's original tag while the native source stays pinned to
/// its resolved immutable manifest. Cold imports without it retain digest-only
/// provenance from the graph.
pub(crate) fn registry_base_plan_with_provenance(
    snapshot_id: &str,
    rootfs: &Arc<RegistryRootfs>,
    compatibility: VmmCompatibility,
    provenance: Option<SnapshotProvenance>,
) -> Result<SnapshotUploadPlan, SnapshotUploadError> {
    let mut plan = registry_base_plan(snapshot_id, rootfs, compatibility)?;
    if let Some(provenance) = provenance {
        bind_registry_base_provenance(&mut plan.spec, Some(provenance))
            .map_err(|error| SnapshotUploadError::InvalidPlan(error.to_string()))?;
    }
    plan.validate_shallow()?;
    Ok(plan)
}

/// A tag is descriptive provenance, while the native source reference remains
/// the authority for repository and immutable bytes. Authenticate their binding
/// without resolving a mutable tag during import or restore.
fn bind_registry_base_provenance(
    spec: &mut SnapshotSpec,
    provenance: Option<SnapshotProvenance>,
) -> Result<(), SourceError> {
    let Some(SourceRef::Registry {
        registry,
        repository,
        manifest_digest,
        ..
    }) = spec.rootfs.source_ref.as_ref()
    else {
        return Err(SourceError::Invalid(
            "Registry base has no pinned source reference".into(),
        ));
    };
    if let Some(provenance) = &provenance {
        if provenance
            .oci_digest
            .as_ref()
            .is_some_and(|digest| digest != manifest_digest)
        {
            return Err(SourceError::Integrity(
                "Registry base provenance digest differs from its pinned source".into(),
            ));
        }
        if let Some(reference) = &provenance.oci_ref {
            let source = RegistryReference::parse(reference)
                .map_err(|error| SourceError::Invalid(error.to_string()))?;
            if source.registry != *registry
                || source.repository != *repository
                || (source.reference.starts_with("sha256:") && source.reference != *manifest_digest)
            {
                return Err(SourceError::Integrity(
                    "Registry base provenance differs from its pinned source".into(),
                ));
            }
        }
    }
    spec.provenance = provenance;
    Ok(())
}

#[cfg(test)]
fn referenced_base_plan(
    snapshot_id: &str,
    rootfs: &Arc<RegistryRootfs>,
    storage: StorageDescriptor,
    compatibility: VmmCompatibility,
) -> Result<SnapshotUploadPlan, SnapshotUploadError> {
    let invalid = SnapshotUploadError::InvalidPlan;
    let map = rootfs.build_map().map_err(invalid)?;
    map.validate().map_err(invalid)?;
    let encoding = RangeMapEncoding::RegistryOverlaybdV1;
    let content = map.content_sha256.trim_start_matches("sha256:");
    let prefix = format!("layers/block/1/{content}/");
    let descriptor = ImmutableObject::new(
        format!("{prefix}{}", encoding.descriptor_name()),
        rootfs.encode_descriptor().map_err(invalid)?,
    )?;
    let committed = ImmutableObject::new(
        format!("{prefix}{}", encoding.committed_name()),
        canonical_json_bytes(&json!({
            "content_sha256": map.content_sha256,
            "format_version": 1,
            "range_map_sha256": descriptor.sha256,
            "kind": LayerKind::Block,
        }))
        .map_err(|error| invalid(error.to_string()))?,
    )?;
    let chain = LayerChainIndex {
        format_version: 1,
        kind: LayerKind::Block,
        logical_bytes: map.logical_bytes,
        unit_size_bytes: map.unit_size_bytes,
        layers: vec![LayerRef {
            ordinal: 0,
            layer_id: format!("registry-{content}"),
            format_version: 1,
            content_sha256: map.content_sha256.clone(),
            range_map: LayerRangeMapRef {
                object_key: descriptor.object_key.clone(),
                sha256: descriptor.sha256.clone(),
                source_ref: None,
                object_version: None,
                encoding: Some(encoding),
            },
        }],
    };
    chain.validate().map_err(invalid)?;
    let chain_object = ImmutableObject::new(
        format!("snapshots/{snapshot_id}/rootfs.index"),
        canonical_json_bytes(&chain).map_err(|error| invalid(error.to_string()))?,
    )?;
    let manifest = SnapshotManifest {
        format_version: 1,
        snapshot_id: snapshot_id.into(),
        snapshot_kind: SnapshotKind::BaseImage,
        vm_state: None,
        memory: None,
        rootfs: ManifestChainRef {
            object_key: chain_object.object_key.clone(),
            sha256: chain_object.sha256.clone(),
            logical_bytes: map.logical_bytes,
            object_version: None,
        },
    };
    manifest.validate().map_err(invalid)?;
    let manifest_object = ImmutableObject::new(
        format!("snapshots/{snapshot_id}/manifest.json"),
        canonical_json_bytes(&manifest).map_err(|error| invalid(error.to_string()))?,
    )?;
    let spec = SnapshotSpec {
        registry_snapshot: None,
        format_version: 1,
        compatibility,
        provenance: Some(SnapshotProvenance {
            oci_ref: Some(format!(
                "{}/{}@{}",
                rootfs.registry(),
                rootfs.repository(),
                rootfs.manifest_digest()
            )),
            oci_digest: Some(rootfs.manifest_digest().into()),
        }),
        storage,
        manifest: ObjectRef {
            object_key: manifest_object.object_key.clone(),
            sha256: manifest_object.sha256.clone(),
            object_version: None,
        },
        vm_state: None,
        memory: None,
        rootfs: ChainIndexRef {
            logical_bytes: map.logical_bytes,
            index_object_key: chain_object.object_key.clone(),
            index_sha256: chain_object.sha256.clone(),
            source_ref: None,
            object_version: None,
        },
    };
    spec.validate().map_err(invalid)?;
    let layer = LayerUploadPlan::from_requests(
        LayerKind::Block,
        1,
        map.content_sha256.clone(),
        Vec::new(),
        descriptor,
        committed,
    )?;
    Ok(SnapshotUploadPlan {
        registry_sources: Vec::new(),
        snapshot_id: snapshot_id.into(),
        snapshot_kind: SnapshotKind::BaseImage,
        spec,
        // The immutable metadata is published and read back by the ordinary
        // manifest-last transaction. External DATA has no local upload owner.
        layers: vec![layer],
        chain_indexes: vec![chain_object],
        vm_state: None,
        manifest: manifest_object,
    })
}

#[cfg(test)]
mod source_tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicUsize, Ordering};

    #[derive(Debug)]
    struct FixtureImporter {
        resolved: Arc<ResolvedSource>,
        calls: AtomicUsize,
        unavailable: bool,
    }

    #[async_trait]
    impl SourceResolver for FixtureImporter {
        async fn resolve_source(&self, _: &SourceRef) -> Result<Arc<ResolvedSource>, SourceError> {
            unreachable!("fixture only supplies authoritative metadata import")
        }

        async fn import_registry(
            &self,
            _: &str,
            _: &str,
            _: &str,
            _: &str,
        ) -> Result<Arc<ResolvedSource>, SourceError> {
            self.calls.fetch_add(1, Ordering::Relaxed);
            if self.unavailable {
                return Err(SourceError::Transport(BlobRangeError::new(
                    BlobRangeErrorKind::Transient,
                    "fixture Registry is offline",
                )));
            }
            Ok(Arc::clone(&self.resolved))
        }
    }

    struct Root(PathBuf);
    impl Root {
        fn new() -> Self {
            Self(std::env::temp_dir().join(format!("snapd-source-{}", uuid::Uuid::new_v4())))
        }
        fn cache(&self, max: u64) -> Arc<LocalFileCache> {
            LocalFileCache::new_with_limit(self.0.clone(), "source-test", max).unwrap()
        }
    }
    impl Drop for Root {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn fixture() -> Arc<ResolvedSource> {
        ResolvedSource::from_registry(composite::test_registry_rootfs()).unwrap()
    }

    fn importer(resolved: &Arc<ResolvedSource>, unavailable: bool) -> Arc<FixtureImporter> {
        Arc::new(FixtureImporter {
            resolved: Arc::clone(resolved),
            calls: AtomicUsize::new(0),
            unavailable,
        })
    }

    fn resolver(
        cache: Arc<LocalFileCache>,
        fixture: Arc<FixtureImporter>,
    ) -> OverlaybdOciSourceResolver {
        let mut resolver = OverlaybdOciSourceResolver::new(Some(cache));
        resolver.fixture_importer = Some(fixture);
        resolver
    }

    #[test]
    fn registry_graph_is_compact_pinned_and_deterministic_without_object_store() {
        let resolved = fixture();
        let compatibility = VmmCompatibility {
            architecture: "x86_64".into(),
            vmm_type: "any".into(),
            vmm_state_format_version: None,
            rocklet_artifact: None,
        };
        let graph =
            build_registry_base_graph("registry-test", &resolved, compatibility.clone()).unwrap();
        let replay = build_registry_base_graph("registry-test", &resolved, compatibility).unwrap();
        assert_eq!(graph.spec, replay.spec);
        assert_eq!(graph.spec.storage.provider, StorageProvider::Registry);
        assert_eq!(
            graph.spec.rootfs.source_ref,
            Some(resolved.source_ref.clone())
        );
        assert_eq!(
            graph.chain.layers[0].range_map.source_ref,
            graph.spec.rootfs.source_ref
        );
        assert_eq!(graph.descriptor.sha256, resolved.descriptor_sha256);
        let bytes = canonical_json_bytes(&graph.spec).unwrap();
        assert!(
            bytes.len() < 4096,
            "catalog stores a compact pin, not format metadata"
        );
        let mut wrong = graph.chain.clone();
        wrong.layers[0].range_map.sha256 = format!("sha256:{}", "1".repeat(64));
        assert!(wrong.validate().is_err());
        let mut legacy = graph.spec.clone();
        legacy.storage.provider = StorageProvider::S3;
        legacy.rootfs.source_ref = None;
        let value = serde_json::to_value(&legacy).unwrap();
        assert!(value["rootfs"].get("source_ref").is_none());
        legacy.validate().unwrap();
    }

    #[tokio::test]
    async fn fresh_resolver_uses_quota_accounted_metadata_when_registry_is_unavailable() {
        let root = Root::new();
        let resolved = fixture();
        {
            let cache = root.cache(4 * 1024 * 1024);
            let writer = OverlaybdOciSourceResolver::new(Some(Arc::clone(&cache)));
            writer.retain(&resolved).await;
            assert!(cache.stats().unwrap().used_bytes > 0);
        }
        let cache = root.cache(4 * 1024 * 1024);
        let offline = importer(&resolved, true);
        let reader = resolver(Arc::clone(&cache), Arc::clone(&offline));
        let loaded = reader.resolve_source(&resolved.source_ref).await.unwrap();
        assert_eq!(loaded.descriptor, resolved.descriptor);
        assert_eq!(loaded.range_map, resolved.range_map);
        assert_eq!(offline.calls.load(Ordering::Relaxed), 0);
        let stats = cache.stats().unwrap();
        assert!(stats.used_bytes > 0 && stats.used_bytes <= stats.max_bytes);
    }

    #[tokio::test]
    async fn corrupted_metadata_is_a_miss_and_is_rebuilt_from_the_fixed_source() {
        let root = Root::new();
        let resolved = fixture();
        let cache = root.cache(4 * 1024 * 1024);
        let authoritative = importer(&resolved, false);
        let reader = resolver(Arc::clone(&cache), Arc::clone(&authoritative));
        reader.retain(&resolved).await;
        let key = metadata_cache_key(&resolved.source_ref).unwrap();
        let name = crate::snapshot::transport::posix_file::metadata_name(
            &key,
            &resolved.descriptor_sha256,
        )
        .unwrap();
        let domain = crate::snapshot::transport::posix_file::hex_digest(b"source-test");
        let path = root.0.join(domain).join("metadata").join(name);
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        std::fs::write(path, b"corrupt").unwrap();
        assert!(reader.read_cached(&resolved.source_ref).await.is_none());
        let loaded = reader.resolve_source(&resolved.source_ref).await.unwrap();
        assert_eq!(loaded.descriptor, resolved.descriptor);
        assert_eq!(authoritative.calls.load(Ordering::Relaxed), 1);
        assert!(reader.read_cached(&resolved.source_ref).await.is_some());
    }

    #[tokio::test]
    async fn cached_descriptor_cannot_change_authority_or_bypass_format_validation() {
        let root = Root::new();
        let resolved = fixture();
        let cache = root.cache(4 * 1024 * 1024);
        let authoritative = importer(&resolved, false);
        let reader = resolver(Arc::clone(&cache), Arc::clone(&authoritative));
        let mut other = resolved.source_ref.clone();
        let SourceRef::Registry { registry, .. } = &mut other;
        *registry = "other.invalid".into();
        cache
            .persist_metadata(
                &metadata_cache_key(&other).unwrap(),
                &resolved.descriptor,
                &resolved.descriptor_sha256,
            )
            .unwrap();
        assert!(reader.read_cached(&other).await.is_none());
        assert!(reader
            .resolve_source(&other)
            .await
            .unwrap_err()
            .is_integrity());
        let invalid = b"{}\n";
        let mut malformed = resolved.source_ref.clone();
        let SourceRef::Registry {
            descriptor_sha256, ..
        } = &mut malformed;
        *descriptor_sha256 = sha256_identity(invalid);
        cache
            .persist_metadata(
                &metadata_cache_key(&malformed).unwrap(),
                invalid,
                &sha256_identity(invalid),
            )
            .unwrap();
        assert!(reader.read_cached(&malformed).await.is_none());
        assert!(reader
            .resolve_source(&malformed)
            .await
            .unwrap_err()
            .is_integrity());
        assert_eq!(authoritative.calls.load(Ordering::Relaxed), 2);
    }

    #[tokio::test]
    async fn cache_admission_failure_and_lru_eviction_preserve_authoritative_fallback() {
        let root = Root::new();
        let resolved = fixture();
        {
            let cache = root.cache(4 * 1024 * 1024);
            OverlaybdOciSourceResolver::new(Some(cache))
                .retain(&resolved)
                .await;
        }
        let cache = root.cache(0);
        assert_eq!(cache.stats().unwrap().used_bytes, 0);
        assert!(cache.stats().unwrap().evictions > 0);
        let authoritative = importer(&resolved, false);
        let reader = resolver(Arc::clone(&cache), Arc::clone(&authoritative));
        assert!(reader.read_cached(&resolved.source_ref).await.is_none());
        let loaded = reader.resolve_source(&resolved.source_ref).await.unwrap();
        assert_eq!(loaded.source_ref, resolved.source_ref);
        assert_eq!(authoritative.calls.load(Ordering::Relaxed), 1);
        assert_eq!(cache.stats().unwrap().used_bytes, 0);
        assert!(cache.stats().unwrap().bypasses > 0);
    }
}

#[cfg(test)]
mod publication_tests {
    use super::*;
    use crate::snapshot::io::snapshot_io::{SnapshotDataUploadConfig, SnapshotPublisher};
    use crate::snapshot::metadata::StorageProvider;
    use crate::snapshot::transport::DataPackWriteTarget;
    use crate::snapshot::transport::{
        BoundedGetError, ImmutableObjectIdentity, ImmutableObjectStore, ImmutableStreamWriter,
        IoCancellation, NamespaceMutationFence, ObjectStoreError, RemoteObject,
        RemoteObjectMetadata, ReplayableByteSource, SnapshotMetadataWriteTarget,
        UploadTransactionLease, VerifiedWriteReceipt,
    };
    use std::collections::BTreeMap;
    use std::sync::Mutex;
    use std::time::Instant;

    #[derive(Debug)]
    struct MetadataOnlyStore {
        fence: Arc<NamespaceMutationFence>,
        objects: Mutex<BTreeMap<String, (Vec<u8>, String)>>,
        writes: Mutex<Vec<String>>,
    }

    #[async_trait::async_trait]
    impl ImmutableObjectStore for MetadataOnlyStore {
        fn namespace(&self) -> &str {
            "registry-test"
        }
        fn fence(&self) -> Arc<NamespaceMutationFence> {
            Arc::clone(&self.fence)
        }
        async fn head(&self, key: &str) -> Result<Option<RemoteObjectMetadata>, ObjectStoreError> {
            assert!(
                !key.starts_with("registry-rootfs/"),
                "external DATA must not be audited in S3"
            );
            Ok(self
                .objects
                .lock()
                .unwrap()
                .get(key)
                .map(|(data, sha)| RemoteObjectMetadata {
                    size: data.len() as u64,
                    sha256: sha.clone(),
                    version: None,
                    etag: None,
                    last_modified_unix: None,
                }))
        }
        async fn get(&self, key: &str) -> Result<Vec<u8>, ObjectStoreError> {
            Ok(self
                .objects
                .lock()
                .unwrap()
                .get(key)
                .expect("published metadata exists")
                .0
                .clone())
        }
        async fn get_with_metadata(
            &self,
            key: &str,
            object_version: Option<&str>,
            max_bytes: u64,
        ) -> Result<Option<RemoteObject>, BoundedGetError> {
            assert!(
                !key.starts_with("registry-rootfs/"),
                "external DATA must not be loaded from S3"
            );
            // The fixture has no versioned objects. Hold one lock while
            // checking the bound and copying both bytes and identity so this
            // models an atomic bounded provider response, not HEAD -> GET.
            if object_version.is_some() {
                return Ok(None);
            }
            let objects = self.objects.lock().unwrap();
            let Some((payload, sha256)) = objects.get(key) else {
                return Ok(None);
            };
            let size = payload.len() as u64;
            if size > max_bytes {
                return Err(BoundedGetError::BoundExceeded {
                    object_key: key.into(),
                    actual_bytes: size,
                    max_bytes,
                });
            }
            Ok(Some(RemoteObject {
                payload: payload.clone(),
                metadata: RemoteObjectMetadata {
                    size,
                    sha256: sha256.clone(),
                    version: None,
                    etag: None,
                    last_modified_unix: None,
                },
            }))
        }
        async fn put_if_absent(
            &self,
            key: &str,
            data: &[u8],
            sha: &str,
        ) -> Result<bool, ObjectStoreError> {
            let mut objects = self.objects.lock().unwrap();
            if objects.contains_key(key) {
                return Ok(false);
            }
            objects.insert(key.into(), (data.into(), sha.into()));
            self.writes.lock().unwrap().push(key.into());
            Ok(true)
        }
    }

    #[async_trait::async_trait]
    impl ImmutableStreamWriter for MetadataOnlyStore {
        fn namespace(&self) -> &str {
            "registry-test"
        }
        fn fence(&self) -> Arc<NamespaceMutationFence> {
            Arc::clone(&self.fence)
        }
        fn write_domain_id(&self) -> &str {
            "registry-test"
        }
        async fn ensure_stream_verified(
            &self,
            identity: &ImmutableObjectIdentity,
            source: Arc<dyn ReplayableByteSource>,
            _: UploadTransactionLease,
            _: Instant,
            _: IoCancellation,
            chunk_bytes: usize,
        ) -> Result<VerifiedWriteReceipt, ObjectStoreError> {
            assert!(
                identity.object_key.starts_with("layers/")
                    && identity.object_key.contains("/data/")
                    && identity.object_key.ends_with(".commit"),
                "only captured standard blobs may be uploaded, never native Registry DATA"
            );
            assert_eq!(source.length(), identity.length);
            let mut payload = Vec::new();
            while (payload.len() as u64) < identity.length {
                let bytes = source.read_at(payload.len() as u64, chunk_bytes).await?;
                assert!(!bytes.is_empty(), "standard blob source must make progress");
                payload.extend_from_slice(&bytes);
            }
            assert_eq!(payload.len() as u64, identity.length);
            assert_eq!(sha256_identity(&payload), identity.sha256);
            self.put_if_absent(&identity.object_key, &payload, &identity.sha256)
                .await?;
            Ok(VerifiedWriteReceipt {
                object_key: identity.object_key.clone(),
                sha256: identity.sha256.clone(),
                length: identity.length,
                object_version: None,
            })
        }
    }

    fn plan() -> SnapshotUploadPlan {
        referenced_base_plan(
            "registry-base",
            &crate::snapshot::format::overlaybd::composite::test_registry_rootfs(),
            StorageDescriptor {
                provider: StorageProvider::S3,
                endpoint_profile: "registry-test".into(),
                credential_ref: "test".into(),
            },
            VmmCompatibility {
                architecture: "x86_64".into(),
                vmm_type: "any".into(),
                vmm_state_format_version: None,
                rocklet_artifact: None,
            },
        )
        .unwrap()
    }

    #[tokio::test]
    async fn referenced_base_publishes_metadata_manifest_last_without_data_and_replays() {
        let store = Arc::new(MetadataOnlyStore {
            fence: NamespaceMutationFence::new("registry-test").unwrap(),
            objects: Mutex::new(BTreeMap::new()),
            writes: Mutex::new(Vec::new()),
        });
        let metadata = SnapshotMetadataWriteTarget::new(
            store.clone(),
            "registry-test".into(),
            store.fence.clone(),
        )
        .unwrap();
        let data =
            DataPackWriteTarget::new(store.clone(), "registry-test".into(), store.fence.clone())
                .unwrap();
        let publisher = SnapshotPublisher::new(SnapshotDataUploadConfig::default(), None);
        let plan = plan();
        plan.validate_shallow().unwrap();
        assert!(plan.layers[0].data_requests().is_empty());
        let first = publisher
            .publish(plan.clone(), metadata.clone(), data.clone())
            .await
            .unwrap();
        let replay = publisher.publish(plan, metadata, data).await.unwrap();
        assert_eq!(first, replay);
        let writes = store.writes.lock().unwrap();
        assert_eq!(writes.len(), 4);
        assert_eq!(
            writes.last().unwrap(),
            "snapshots/registry-base/manifest.json"
        );
        assert!(first.spec.memory.is_none() && first.spec.vm_state.is_none());
    }

    #[test]
    fn referenced_base_rejects_a_validly_hashed_but_unbound_commit_marker() {
        let mut plan = plan();
        let key = plan.layers[0].committed.object_key.clone();
        plan.layers[0].committed = ImmutableObject::new(key, b"{}\n".to_vec()).unwrap();
        assert!(plan.validate_shallow().is_err());
    }

    #[derive(Debug)]
    struct FixtureSource;

    #[async_trait::async_trait]
    impl crate::snapshot::SourceResolver for FixtureSource {
        async fn resolve_source(
            &self,
            source: &crate::snapshot::metadata::SourceRef,
        ) -> Result<Arc<crate::snapshot::ResolvedSource>, crate::snapshot::SourceError> {
            // A fresh import on every call deliberately keeps no process-local
            // descriptor registry or original daemon capability alive.
            let resolved = crate::snapshot::ResolvedSource::from_registry(
                crate::snapshot::format::overlaybd::composite::test_registry_rootfs(),
            )?;
            if &resolved.source_ref != source {
                return Err(crate::snapshot::SourceError::Integrity(
                    "fixture pin differs".into(),
                ));
            }
            Ok(resolved)
        }
    }

    #[derive(Debug)]
    struct NoObjectStore;

    #[async_trait::async_trait]
    impl ImmutableObjectStore for NoObjectStore {
        fn namespace(&self) -> &str {
            "unavailable-s3"
        }
        fn fence(&self) -> Arc<NamespaceMutationFence> {
            NamespaceMutationFence::new("unavailable-s3").unwrap()
        }
        async fn head(&self, _: &str) -> Result<Option<RemoteObjectMetadata>, ObjectStoreError> {
            panic!("Registry base must never HEAD S3")
        }
        async fn get(&self, _: &str) -> Result<Vec<u8>, ObjectStoreError> {
            panic!("Registry base must never GET S3")
        }
        async fn put_if_absent(
            &self,
            _: &str,
            _: &[u8],
            _: &str,
        ) -> Result<bool, ObjectStoreError> {
            panic!("Registry base must never PUT S3")
        }
    }

    fn base_compatibility() -> VmmCompatibility {
        VmmCompatibility {
            architecture: "x86_64".into(),
            vmm_type: "any".into(),
            vmm_state_format_version: None,
            rocklet_artifact: None,
        }
    }

    #[test]
    fn registry_base_keeps_original_tag_bound_to_its_pinned_source() {
        let rootfs = crate::snapshot::format::overlaybd::composite::test_registry_rootfs();
        let provenance = SnapshotProvenance {
            oci_ref: Some(format!(
                "{}/{}:original_accelerated",
                rootfs.registry(),
                rootfs.repository()
            )),
            oci_digest: Some(rootfs.manifest_digest().into()),
        };
        let plan = registry_base_plan_with_provenance(
            "tagged-native-base",
            &rootfs,
            base_compatibility(),
            Some(provenance.clone()),
        )
        .unwrap();
        assert_eq!(plan.spec.provenance.as_ref(), Some(&provenance));
        let published = crate::snapshot::format::UploadedSnapshot {
            registry_objects: None,
            spec: plan.spec,
            manifest: serde_json::from_slice(&plan.manifest.payload).unwrap(),
        };
        let snapshot =
            OverlaybdOciSnapshot::from_registry(Arc::clone(&rootfs), &published).unwrap();
        assert_eq!(snapshot.provenance(), Some(&provenance));
        let mut wrong = published.clone();
        wrong.spec.provenance.as_mut().unwrap().oci_ref =
            Some("other.example/unrelated/repository:original_accelerated".into());
        assert!(OverlaybdOciSnapshot::from_registry(Arc::clone(&rootfs), &wrong).is_err());
        let mut legacy = published;
        legacy.spec.provenance = None;
        let snapshot = OverlaybdOciSnapshot::from_registry(rootfs, &legacy).unwrap();
        assert_eq!(snapshot.provenance(), None);
    }

    #[test]
    fn pending_oci_instance_installs_one_source_without_replacing_its_state() {
        let rootfs = crate::snapshot::format::overlaybd::composite::test_registry_rootfs();
        let build = |compatibility| {
            let plan = registry_base_plan("one-oci-instance", &rootfs, compatibility).unwrap();
            let manifest = serde_json::from_slice(&plan.manifest.payload).unwrap();
            OverlaybdOciSnapshot::from_registry(
                Arc::clone(&rootfs),
                &crate::snapshot::format::UploadedSnapshot {
                    registry_objects: None,
                    spec: plan.spec,
                    manifest,
                },
            )
            .unwrap()
        };
        let pending = OverlaybdOciSnapshot::creating("one-oci-instance");
        assert!(pending.spec().is_none());
        assert!(pending.chain_view(LayerKind::Block).is_none());
        let ready = build(base_compatibility());
        pending.initialize_from(&ready).unwrap();
        pending.initialize_from(&ready).unwrap();
        assert_eq!(pending.spec(), ready.spec());
        // Initialization only installs source metadata. Repository publication
        // commits the existing lifecycle while holding its identity fence.
        assert_eq!(
            pending.state(),
            crate::snapshot::repository::PublicationState::Creating
        );
        let other_id = OverlaybdOciSnapshot::creating("another-instance");
        assert!(other_id.initialize_from(&ready).is_err());
        let mut compatibility = base_compatibility();
        compatibility.vmm_type = "cloud-hypervisor".into();
        let conflicting = build(compatibility);
        assert!(pending.initialize_from(&conflicting).is_err());
        assert_eq!(pending.spec(), ready.spec());
    }

    #[test]
    fn registry_instance_rejects_a_catalog_root_that_does_not_match_its_source() {
        let rootfs = crate::snapshot::format::overlaybd::composite::test_registry_rootfs();
        let plan = registry_base_plan("mismatched-oci", &rootfs, base_compatibility()).unwrap();
        let mut published = crate::snapshot::format::UploadedSnapshot {
            registry_objects: None,
            spec: plan.spec,
            manifest: serde_json::from_slice(&plan.manifest.payload).unwrap(),
        };
        published.spec.rootfs.logical_bytes += 65536;
        assert!(OverlaybdOciSnapshot::from_registry(rootfs, &published).is_err());
    }

    #[tokio::test]
    async fn registry_catalog_base_reconstructs_on_fresh_loaders_without_any_s3_io() {
        use crate::snapshot::io::snapshot_io::{SnapshotIo, SnapshotLoadLimits};
        use crate::snapshot::metadata::snapshot_spec_checksum;
        let rootfs = crate::snapshot::format::overlaybd::composite::test_registry_rootfs();
        let provenance = SnapshotProvenance {
            oci_ref: Some(format!(
                "{}/{}:original_accelerated",
                rootfs.registry(),
                rootfs.repository()
            )),
            oci_digest: Some(rootfs.manifest_digest().into()),
        };
        let plan = registry_base_plan_with_provenance(
            "catalog-registry-base",
            &rootfs,
            base_compatibility(),
            Some(provenance.clone()),
        )
        .unwrap();
        assert_eq!(plan.spec.storage.provider, StorageProvider::Registry);
        assert!(plan.spec.rootfs.source_ref.is_some());
        let value = serde_json::to_value(&plan.spec).unwrap();
        let checksum = snapshot_spec_checksum(&value).unwrap();
        for _ in 0..2 {
            // Even a loader configured for an unrelated S3 namespace must load
            // this Registry-only graph exclusively through its source resolver.
            let loader = SnapshotIo::new_with_storage_identity(
                Arc::new(NoObjectStore),
                StorageDescriptor {
                    provider: StorageProvider::S3,
                    endpoint_profile: "unavailable".into(),
                    credential_ref: "unavailable".into(),
                },
                SnapshotLoadLimits::default(),
            )
            .unwrap()
            .with_source_resolver(Arc::new(FixtureSource));
            let loaded = loader
                .load_for_start_shared(&value, &checksum, &plan.snapshot_id)
                .await
                .unwrap();
            let metadata: &dyn crate::snapshot::SnapshotInstance = loaded.as_ref();
            assert_eq!(metadata.snapshot_kind(), SnapshotKind::BaseImage);
            assert_eq!(
                metadata
                    .layer_chain(LayerKind::Block)
                    .unwrap()
                    .unwrap()
                    .layers
                    .len(),
                1
            );
            assert!(metadata.layer_chain(LayerKind::Memory).unwrap().is_none());
            let view: &dyn crate::snapshot::SnapshotInstance = loaded.as_ref();
            assert!(view
                .layer_reader(
                    LayerKind::Block,
                    &loaded.chain_view(LayerKind::Block).unwrap().index.layers[0].content_sha256
                )
                .is_ok());
            assert_eq!(loaded.spec(), Some(&plan.spec));
            assert_eq!(loaded.provenance(), Some(&provenance));
            assert!(loaded.chain_view(LayerKind::Block).unwrap().layers[0]
                .range_map
                .registry
                .is_some());
            assert!(loaded.chain_view(LayerKind::Memory).is_none() && loaded.vm_state().is_none());
        }
        let loader =
            SnapshotIo::new(Arc::new(NoObjectStore)).with_source_resolver(Arc::new(FixtureSource));
        let mut legacy = plan.spec.clone();
        legacy.provenance = None;
        let value = serde_json::to_value(&legacy).unwrap();
        let checksum = snapshot_spec_checksum(&value).unwrap();
        let loaded = loader
            .load_for_start(&value, &checksum, &plan.snapshot_id)
            .await
            .unwrap();
        assert_eq!(loaded.provenance(), None);
        for mutate in [0, 1] {
            let mut altered = plan.spec.clone();
            if mutate == 0 {
                altered.manifest.sha256 =
                    crate::snapshot::metadata::sha256_identity(b"wrong manifest");
            } else {
                altered.rootfs.logical_bytes += 65536;
            }
            let value = serde_json::to_value(&altered).unwrap();
            let checksum = snapshot_spec_checksum(&value).unwrap();
            assert!(matches!(
                loader
                    .load_for_start(&value, &checksum, &plan.snapshot_id)
                    .await,
                Err(crate::snapshot::io::snapshot_io::SnapshotLoadError::IdentityConflict(_))
            ));
        }
    }

    struct StandardBlobBytes(Vec<u8>);

    impl crate::snapshot::transport::DataPackSource for StandardBlobBytes {
        fn length(&self) -> u64 {
            self.0.len() as u64
        }

        fn read_at<'a>(
            &'a self,
            offset: u64,
            max_bytes: usize,
        ) -> std::pin::Pin<
            Box<
                dyn std::future::Future<
                        Output = Result<Vec<u8>, crate::snapshot::transport::DataPackSourceError>,
                    > + Send
                    + 'a,
            >,
        > {
            Box::pin(async move {
                let offset = usize::try_from(offset)
                    .ok()
                    .filter(|offset| *offset <= self.0.len())
                    .ok_or(crate::snapshot::transport::DataPackSourceError::InvalidRange)?;
                let end = offset.saturating_add(max_bytes).min(self.0.len());
                Ok(self.0[offset..end].to_vec())
            })
        }
    }

    fn zero_or_inherit_layer(
        kind: LayerKind,
        logical_bytes: u64,
        inherit: bool,
    ) -> (LayerUploadPlan, crate::snapshot::metadata::LayerRef) {
        use crate::snapshot::format::overlaybd::{encode_descriptor, plan_layer};
        use crate::snapshot::format::overlaybd_s3::data::{
            DataPackIdentity, DataPackObjectKey, DataPackSliceIdentity, DataPackUploadRequest,
        };
        use crate::snapshot::metadata::{
            compute_layer_content_sha256, LayerRange, LayerRangeMap, RangeKind,
        };
        let mut map = LayerRangeMap {
            format_version: 1,
            kind,
            content_sha256: format!("sha256:{}", "0".repeat(64)),
            logical_bytes,
            unit_size_bytes: if kind == LayerKind::Memory {
                4096
            } else {
                65536
            },
            registry: None,
            registry_objects: None,
            zfile: None,
            ranges: vec![LayerRange {
                kind: if inherit {
                    RangeKind::Inherit
                } else {
                    RangeKind::Zero
                },
                logical_offset: 0,
                length: logical_bytes,
                source: None,
            }],
        };
        map.content_sha256 = compute_layer_content_sha256(&map).unwrap();
        let prefix = format!(
            "layers/{}/1/{}/",
            kind.object_key_component(),
            &map.content_sha256[7..]
        );
        let standard = plan_layer(&map).unwrap();
        let payload = standard
            .read_at(
                &std::fs::File::open("/dev/null").unwrap(),
                0,
                standard.stored_bytes as usize,
            )
            .unwrap();
        assert_eq!(payload.len() as u64, standard.stored_bytes);
        let blob = ObjectRef {
            object_key: format!("{prefix}data/{}.commit", &sha256_identity(&payload)[7..]),
            sha256: sha256_identity(&payload),
            object_version: None,
        };
        let mut slices = Vec::new();
        for (offset, bytes) in [
            (0, standard.header.as_slice()),
            (standard.index_offset, standard.index.as_slice()),
            (
                standard.stored_bytes - standard.trailer.len() as u64,
                standard.trailer.as_slice(),
            ),
        ] {
            if !bytes.is_empty() {
                slices.push(DataPackSliceIdentity {
                    offset,
                    length: bytes.len() as u64,
                    expected_sha256: sha256_identity(bytes),
                });
            }
        }
        slices.sort_by_key(|slice| slice.offset);
        let data = DataPackUploadRequest {
            identity: DataPackIdentity {
                object_key: DataPackObjectKey::new(blob.object_key.clone()).unwrap(),
                expected_sha256: blob.sha256.clone(),
                length: payload.len() as u64,
            },
            source: Arc::new(StandardBlobBytes(payload)),
            slices,
            zfile: None,
        };
        let encoding = RangeMapEncoding::OverlaybdLsmtV1;
        let descriptor = ImmutableObject::new(
            format!("{prefix}{}", encoding.descriptor_name()),
            encode_descriptor(&map, &standard, &blob).unwrap(),
        )
        .unwrap();
        let marker = ImmutableObject::new(
            format!("{prefix}{}", encoding.committed_name()),
            canonical_json_bytes(&json!({
                "content_sha256": map.content_sha256, "format_version": 1,
                "range_map_sha256": descriptor.sha256, "kind": kind,
            }))
            .unwrap(),
        )
        .unwrap();
        let reference = LayerRef {
            ordinal: if inherit { 1 } else { 0 },
            layer_id: format!("fixture-{}", kind.object_key_component()),
            format_version: 1,
            content_sha256: map.content_sha256.clone(),
            range_map: LayerRangeMapRef {
                object_key: descriptor.object_key.clone(),
                sha256: descriptor.sha256.clone(),
                object_version: None,
                encoding: Some(encoding),
                source_ref: None,
            },
        };
        (
            LayerUploadPlan::from_requests(
                kind,
                1,
                map.content_sha256,
                vec![data],
                descriptor,
                marker,
            )
            .unwrap(),
            reference,
        )
    }

    fn inherited_full_plan() -> SnapshotUploadPlan {
        use crate::snapshot::metadata::{RockletArtifactCompatibility, VmStateRef};
        let id = "registry-inherited-full";
        let base = registry_base_plan(
            "catalog-base",
            &crate::snapshot::format::overlaybd::composite::test_registry_rootfs(),
            base_compatibility(),
        )
        .unwrap();
        let mut root_chain: LayerChainIndex =
            serde_json::from_slice(&base.chain_indexes[0].payload).unwrap();
        let (block, block_ref) =
            zero_or_inherit_layer(LayerKind::Block, root_chain.logical_bytes, true);
        root_chain.layers.push(block_ref);
        let (memory, memory_ref) = zero_or_inherit_layer(LayerKind::Memory, 4096, false);
        let memory_chain = LayerChainIndex {
            format_version: 1,
            kind: LayerKind::Memory,
            logical_bytes: 4096,
            unit_size_bytes: 4096,
            layers: vec![memory_ref],
        };
        let root_index = ImmutableObject::new(
            format!("snapshots/{id}/rootfs.index"),
            canonical_json_bytes(&root_chain).unwrap(),
        )
        .unwrap();
        let memory_index = ImmutableObject::new(
            format!("snapshots/{id}/memory.index"),
            canonical_json_bytes(&memory_chain).unwrap(),
        )
        .unwrap();
        let chain_ref = |object: &ImmutableObject, logical_bytes| ChainIndexRef {
            logical_bytes,
            index_object_key: object.object_key.clone(),
            index_sha256: object.sha256.clone(),
            object_version: None,
            source_ref: None,
        };
        let manifest_ref = |reference: &ChainIndexRef| ManifestChainRef {
            object_key: reference.index_object_key.clone(),
            sha256: reference.index_sha256.clone(),
            logical_bytes: reference.logical_bytes,
            object_version: None,
        };
        let rootfs = chain_ref(&root_index, root_chain.logical_bytes);
        let mem = chain_ref(&memory_index, 4096);
        let vm_object = ImmutableObject::new(
            format!("snapshots/{id}/vm-state.pack"),
            b"opaque frozen state".to_vec(),
        )
        .unwrap();
        let vm_ref = VmStateRef {
            object_key: vm_object.object_key.clone(),
            sha256: vm_object.sha256.clone(),
            object_version: None,
            stored_bytes: vm_object.payload.len() as u64,
        };
        let manifest = SnapshotManifest {
            format_version: 1,
            snapshot_id: id.into(),
            snapshot_kind: SnapshotKind::Full,
            vm_state: Some(vm_ref.clone()),
            memory: Some(manifest_ref(&mem)),
            rootfs: manifest_ref(&rootfs),
        };
        let manifest_object = ImmutableObject::new(
            format!("snapshots/{id}/manifest.json"),
            canonical_json_bytes(&manifest).unwrap(),
        )
        .unwrap();
        let identity = crate::snapshot::metadata::sha256_identity(b"fixture rocklet");
        let spec = SnapshotSpec {
            registry_snapshot: None,
            format_version: 1,
            compatibility: VmmCompatibility {
                architecture: "x86_64".into(),
                vmm_type: "cloud-hypervisor".into(),
                vmm_state_format_version: Some(1),
                rocklet_artifact: Some(RockletArtifactCompatibility {
                    identity: identity.clone(),
                    version: "fixture-v1".into(),
                    raw_sha256: identity,
                    logical_bytes: 512,
                    guest_port: 22555,
                }),
            },
            provenance: base.spec.provenance,
            storage: plan().spec.storage,
            manifest: ObjectRef {
                object_key: manifest_object.object_key.clone(),
                sha256: manifest_object.sha256.clone(),
                object_version: None,
            },
            vm_state: Some(vm_ref),
            memory: Some(mem),
            rootfs,
        };
        let full = SnapshotUploadPlan {
            registry_sources: Vec::new(),
            snapshot_id: id.into(),
            snapshot_kind: SnapshotKind::Full,
            spec,
            layers: vec![memory, block],
            chain_indexes: vec![memory_index, root_index],
            vm_state: Some(vm_object),
            manifest: manifest_object,
        };
        full.validate_shallow().unwrap();
        full
    }

    #[tokio::test]
    async fn full_publication_and_fresh_restore_inherit_registry_without_s3_base_metadata() {
        use crate::snapshot::io::snapshot_io::{SnapshotIo, SnapshotLoadError};
        use crate::snapshot::metadata::snapshot_spec_checksum;
        let store = Arc::new(MetadataOnlyStore {
            fence: NamespaceMutationFence::new("registry-test").unwrap(),
            objects: Mutex::new(BTreeMap::new()),
            writes: Mutex::new(vec![]),
        });
        let metadata = SnapshotMetadataWriteTarget::new(
            store.clone(),
            "registry-test".into(),
            store.fence.clone(),
        )
        .unwrap();
        let data =
            DataPackWriteTarget::new(store.clone(), "registry-test".into(), store.fence.clone())
                .unwrap();
        let full = inherited_full_plan();
        let publisher = SnapshotPublisher::new(SnapshotDataUploadConfig::default(), None)
            .with_source_resolver(Arc::new(FixtureSource));
        let published = publisher
            .publish(full.clone(), metadata, data)
            .await
            .unwrap();
        assert!(matches!(
            store
                .get_with_metadata(
                    &full.manifest.object_key,
                    None,
                    full.manifest.payload.len() as u64 - 1,
                )
                .await,
            Err(BoundedGetError::BoundExceeded { .. })
        ));
        assert_eq!(
            store
                .get_with_metadata(
                    &full.manifest.object_key,
                    None,
                    full.manifest.payload.len() as u64,
                )
                .await
                .unwrap()
                .unwrap()
                .payload,
            full.manifest.payload
        );
        let parent_ref: LayerChainIndex =
            serde_json::from_slice(&full.chain_indexes[1].payload).unwrap();
        let parent = &parent_ref.layers[0];
        assert!(parent.range_map.source_ref.is_some());
        let captured_blobs = full
            .layers
            .iter()
            .flat_map(|layer| layer.data_requests())
            .collect::<Vec<_>>();
        assert_eq!(captured_blobs.len(), 2);
        let objects = store.objects.lock().unwrap();
        for blob in captured_blobs {
            assert!(objects.contains_key(blob.identity.object_key.as_str()));
        }
        assert!(objects
            .keys()
            .all(|key| !key.starts_with("snapshots/catalog-base/")
                && !key.starts_with("registry-rootfs/")));
        drop(objects);
        assert!(!store
            .objects
            .lock()
            .unwrap()
            .contains_key(&parent.range_map.object_key));
        let value = serde_json::to_value(&published.spec).unwrap();
        let checksum = snapshot_spec_checksum(&value).unwrap();
        let loader = SnapshotIo::new(store.clone()).with_source_resolver(Arc::new(FixtureSource));
        let loaded = loader
            .load_for_start(&value, &checksum, &full.snapshot_id)
            .await
            .unwrap();
        let metadata: &dyn crate::snapshot::SnapshotInstance = loaded.as_ref();
        assert_eq!(metadata.snapshot_kind(), SnapshotKind::Full);
        assert_eq!(
            metadata
                .layer_chain(LayerKind::Block)
                .unwrap()
                .unwrap()
                .layers
                .len(),
            2
        );
        assert!(metadata.layer_chain(LayerKind::Memory).unwrap().is_some());
        assert_eq!(loaded.chain_view(LayerKind::Block).unwrap().layers.len(), 2);
        assert!(loaded.chain_view(LayerKind::Block).unwrap().layers[0]
            .range_map
            .registry
            .is_some());
        assert!(loaded.chain_view(LayerKind::Memory).is_some() && loaded.vm_state().is_some());

        // A catalog checksum alone cannot authorize a source descriptor that
        // conflicts with the inherited layer SHA. Rebind the containing graph
        // hashes so this reaches the leaf identity check on a fresh loader.
        let mut altered = full.spec.clone();
        let mut chain = parent_ref;
        let wrong = crate::snapshot::metadata::sha256_identity(b"wrong descriptor");
        chain.layers[0].range_map.sha256 = wrong.clone();
        chain.layers[0].content_sha256 = wrong.clone();
        chain.layers[0].layer_id = format!("registry-{}", &wrong[7..]);
        chain.layers[0].range_map.object_key =
            format!("layers/block/1/{}/registry-overlaybd.json", &wrong[7..]);
        let Some(crate::snapshot::metadata::SourceRef::Registry {
            descriptor_sha256, ..
        }) = &mut chain.layers[0].range_map.source_ref
        else {
            panic!("fixture parent has a Registry source pin")
        };
        *descriptor_sha256 = wrong;
        // This forged graph is structurally canonical. Rejection must come
        // from resolving its altered immutable source, not an old path/hash.
        chain.validate().unwrap();
        let bytes = canonical_json_bytes(&chain).unwrap();
        altered.rootfs.index_sha256 = crate::snapshot::metadata::sha256_identity(&bytes);
        store.objects.lock().unwrap().insert(
            altered.rootfs.index_object_key.clone(),
            (bytes, altered.rootfs.index_sha256.clone()),
        );
        let mut manifest: SnapshotManifest =
            serde_json::from_slice(&full.manifest.payload).unwrap();
        manifest.rootfs.sha256 = altered.rootfs.index_sha256.clone();
        let bytes = canonical_json_bytes(&manifest).unwrap();
        altered.manifest.sha256 = crate::snapshot::metadata::sha256_identity(&bytes);
        store.objects.lock().unwrap().insert(
            altered.manifest.object_key.clone(),
            (bytes, altered.manifest.sha256.clone()),
        );
        let value = serde_json::to_value(&altered).unwrap();
        let checksum = snapshot_spec_checksum(&value).unwrap();
        let loader = SnapshotIo::new(store).with_source_resolver(Arc::new(FixtureSource));
        let error = loader
            .load_for_start(&value, &checksum, &full.snapshot_id)
            .await
            .unwrap_err();
        assert!(
            matches!(&error, SnapshotLoadError::IdentityConflict(message)
                if message.contains("fixture pin differs")),
            "expected the resolver to reject the forged source pin, got {error:?}"
        );
    }
}
