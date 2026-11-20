//! A digest-pinned OCI root and its scoped logical-key to physical-blob map.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use async_trait::async_trait;
use serde_json::Value;

use super::models::*;
use crate::snapshot::format::overlaybd::composite;
use crate::snapshot::io::range_io::{
    P2PSource, RangeIO, RangeReadRequest, RangeReadResult, RangeReader, RangeRepresentation,
};
use crate::snapshot::io::snapshot_io::{
    source_load_error, SnapshotLoadError, SnapshotMetadataReadContext,
};
use crate::snapshot::metadata::{sha256_identity, RegistrySnapshotRef, SnapshotSpec, SourceRef};
use crate::snapshot::transport::registry::{
    layer_ref, RegistryClient, RegistryError, RegistryReference,
};
use crate::snapshot::transport::{
    BlobRef, BoundedGetError, ImmutableObjectStore, NamespaceMutationFence, ObjectStoreError,
    PhysicalRangeCache, PhysicalRangeRequest, PublishFailureKind, RangeReadError,
    RangeReadFailureClass, RemoteObject, RemoteObjectMetadata,
};
use crate::snapshot::{ResolvedSource, SourceError, SourceResolver, ValidateCanonicalModel};
use crate::RuntimeIdentity;

fn invalid(message: impl Into<String>) -> SourceError {
    SourceError::Invalid(message.into())
}

fn integrity(message: impl Into<String>) -> SourceError {
    SourceError::Integrity(message.into())
}

fn check_annotations(value: &Value, id: &str, digest: &str) -> Result<(), SourceError> {
    let annotations = &value["annotations"];
    if annotations[SNAPSHOT_FORMAT] != "1"
        || annotations[SNAPSHOT_ID] != id
        || annotations[METADATA_DIGEST] != digest
    {
        return Err(integrity(
            "OCI snapshot generation or metadata binding differs",
        ));
    }
    Ok(())
}

/// Bootstrap a complete snapshot from a user-supplied OCI reference. Resolve a
/// mutable tag once, then authenticate both platform entries against that exact
/// index digest. A disk view must never be accepted as a memory checkpoint.
pub(crate) async fn resolve_full_snapshot_metadata(
    resolver: &dyn SourceResolver,
    oci_ref: &str,
    max_metadata_bytes: usize,
) -> Result<(String, SnapshotSpec), SourceError> {
    let parsed = RegistryReference::parse(oci_ref)?;
    let client = resolver.registry_client(&parsed.registry).await?;
    let index = client
        .manifest(&parsed.repository, &parsed.reference)
        .await?;
    let reference = RegistrySnapshotRef {
        registry: parsed.registry,
        repository: parsed.repository,
        index_digest: index.digest,
    };
    let envelope = read_snapshot_metadata_with_size(&client, &reference).await?;
    if envelope.metadata_bytes > max_metadata_bytes {
        return Err(invalid(
            "OCI snapshot bootstrap exceeds the metadata byte budget",
        ));
    }
    let metadata = envelope.metadata;
    if metadata.spec.memory.is_none()
        || metadata.spec.vm_state.is_none()
        || metadata.memory_layout.is_none()
    {
        return Err(invalid(
            "OCI resume requires a FULL memory and VMM-state snapshot",
        ));
    }
    let spec = metadata.bound_spec(&reference).map_err(invalid)?;
    Ok((metadata.snapshot_id, spec))
}

/// Resolve exactly an immutable snapd index. Normal disk-only OCI/DADI import
/// keeps its independent selected-manifest path in the parent module.
#[cfg(test)]
pub(crate) async fn read_snapshot_metadata(
    client: &Arc<RegistryClient>,
    reference: &RegistrySnapshotRef,
) -> Result<RegistrySnapshotMetadata, SourceError> {
    read_snapshot_metadata_with_size(client, reference)
        .await
        .map(|envelope| envelope.metadata)
}

struct RegistrySnapshotEnvelope {
    metadata: RegistrySnapshotMetadata,
    metadata_bytes: usize,
    native_layers: [Vec<BlobRef>; 2],
}

async fn read_snapshot_metadata_with_size(
    client: &Arc<RegistryClient>,
    reference: &RegistrySnapshotRef,
) -> Result<RegistrySnapshotEnvelope, SourceError> {
    reference.validate().map_err(invalid)?;
    if client.registry() != reference.registry {
        return Err(integrity("Registry snapshot client authority differs"));
    }
    let index = client
        .manifest(&reference.repository, &reference.index_digest)
        .await?;
    let mut metadata_bytes = index.payload.len();
    if index.digest != reference.index_digest
        || index.value["mediaType"] != OCI_INDEX
        || index.value["annotations"][SNAPSHOT_FORMAT] != "1"
    {
        return Err(invalid(
            "Registry snapshot reference is not a supported snapd index",
        ));
    }
    let entries = index.value["manifests"]
        .as_array()
        .filter(|entries| entries.len() == 2)
        .ok_or_else(|| invalid("Snapd index requires one disk and one private entry"))?;
    let mut disk = None;
    let mut memory = None;
    for entry in entries {
        if entry["mediaType"] != OCI_MANIFEST
            || entry["platform"]["os"] != "linux"
            || entry["platform"].get("variant").is_some()
        {
            return Err(invalid("Snapd index platform descriptor is invalid"));
        }
        let target = match entry["platform"]["architecture"].as_str() {
            Some("amd64") => &mut disk,
            Some(MEMORY_ARCHITECTURE) => &mut memory,
            _ => return Err(invalid("Snapd index has an unknown platform role")),
        };
        if target.replace(layer_ref(entry)?).is_some() {
            return Err(invalid("Snapd index repeats a platform role"));
        }
    }
    let disk = disk.ok_or_else(|| invalid("Snapd index has no disk manifest"))?;
    let memory = memory.ok_or_else(|| invalid("Snapd index has no private manifest"))?;
    let mut metadata = None;
    let mut reachable = HashMap::<String, u64>::new();
    let mut native_counts = [0_usize; 2];
    let mut native_layers = [Vec::new(), Vec::new()];
    for (ordinal, descriptor, architecture, role) in [
        (0, disk, "amd64", "disk"),
        (1, memory, MEMORY_ARCHITECTURE, "memory"),
    ] {
        let child = client
            .manifest(&reference.repository, &descriptor.digest)
            .await?;
        metadata_bytes += child.payload.len();
        if child.payload.len() as u64 != descriptor.size
            || child.value["mediaType"] != OCI_MANIFEST
            || child.value["artifactType"] != NATIVE_ARTIFACT
        {
            return Err(integrity("OCI child manifest identity or type differs"));
        }
        let config = layer_ref(&child.value["config"])?;
        if child.value["config"]["mediaType"] != OCI_CONFIG {
            return Err(invalid("OCI snapshot config media type differs"));
        }
        let bytes = client
            .read_blob_metadata(&reference.repository, &config)
            .await?;
        metadata_bytes += bytes.len();
        let config: Value = serde_json::from_slice(&bytes)
            .map_err(|_| invalid("OCI snapshot config is not JSON"))?;
        if config["os"] != "linux"
            || config["architecture"] != architecture
            || config["snapd_role"] != role
        {
            return Err(integrity("OCI config platform or snapshot role differs"));
        }
        let candidate: RegistrySnapshotMetadata =
            serde_json::from_value(config["snapd"].clone())
                .map_err(|error| invalid(format!("Invalid OCI snapshot metadata: {error}")))?;
        candidate.validate().map_err(invalid)?;
        let digest = candidate.digest().map_err(invalid)?;
        if config["snapd_metadata_digest"] != digest {
            return Err(integrity("OCI snapshot config metadata digest differs"));
        }
        check_annotations(&index.value, &candidate.snapshot_id, &digest)?;
        check_annotations(&child.value, &candidate.snapshot_id, &digest)?;
        if metadata
            .as_ref()
            .is_some_and(|previous| previous != &candidate)
        {
            return Err(integrity(
                "Disk and memory manifests bind different snapshots",
            ));
        }
        metadata = Some(candidate);
        let layers = child.value["layers"]
            .as_array()
            .filter(|layers| !layers.is_empty() && layers.len() <= MAX_SNAPSHOT_OBJECTS)
            .ok_or_else(|| invalid("OCI snapshot layer list is outside its bound"))?;
        let diff_ids = layers
            .iter()
            .map(|layer| layer["digest"].clone())
            .collect::<Vec<_>>();
        if config["rootfs"]["type"] != "layers"
            || config["rootfs"]["diff_ids"].as_array() != Some(&diff_ids)
        {
            return Err(integrity(
                "OCI config diff IDs differ from its ordered child layers",
            ));
        }
        for layer in layers {
            let blob = layer_ref(layer)?;
            let media = layer["mediaType"].as_str();
            let layer_role = layer["annotations"][LAYER_ROLE].as_str();
            match (ordinal, media, layer_role) {
                (0, Some(NATIVE_LAYER), Some("disk")) | (1, Some(NATIVE_LAYER), Some("memory")) => {
                    native_counts[ordinal] += 1;
                    native_layers[ordinal].push(blob.clone());
                    if layer["annotations"]["containerd.io/snapshot/overlaybd/blob-digest"]
                        != blob.digest
                        || layer["annotations"]["containerd.io/snapshot/overlaybd/blob-size"]
                            != blob.size.to_string()
                        || layer["annotations"]["containerd.io/snapshot/overlaybd/version"]
                            != "0.1.0"
                    {
                        return Err(integrity("Native OverlayBD annotation differs"));
                    }
                }
                (1, Some(AUXILIARY_LAYER), Some("metadata"))
                | (1, Some(VM_STATE_LAYER), Some("vm-state")) => {}
                _ => return Err(invalid("OCI snapshot layer has a conflicting role or type")),
            }
            if reachable
                .insert(blob.digest.clone(), blob.size)
                .is_some_and(|size| size != blob.size)
            {
                return Err(integrity(
                    "OCI snapshot has conflicting physical blob sizes",
                ));
            }
        }
    }
    let metadata = metadata.ok_or_else(|| invalid("OCI snapshot has no restore metadata"))?;
    if native_counts[0] == 0
        || native_counts[0] > 64
        || native_counts[1] > 64
        || metadata.spec.memory.is_some() != (native_counts[1] != 0)
    {
        return Err(invalid("OCI memory DATA or disk layer binding differs"));
    }
    let mut bound = HashSet::new();
    for object in &metadata.objects {
        if reachable.get(&object.blob.digest) != Some(&object.blob.size) {
            return Err(integrity(
                "OCI snapshot object is not reachable from its manifests",
            ));
        }
        bound.insert(object.blob.digest.as_str());
    }
    if reachable
        .keys()
        .any(|digest| !bound.contains(digest.as_str()))
    {
        return Err(integrity(
            "OCI manifest references an unbound physical object",
        ));
    }
    Ok(RegistrySnapshotEnvelope {
        metadata,
        metadata_bytes,
        native_layers,
    })
}

/// Runtime-only read authority retained by authenticated maps and lineage.
/// Neither clients nor routing data participate in serialized layer checksums.
pub struct RegistryObjectMap {
    registry: String,
    repository: String,
    objects: HashMap<String, BlobRef>,
    versions: HashMap<String, Option<String>>,
    client: Arc<RegistryClient>,
    ranges: Arc<RangeIO>,
    namespace: String,
    fence: Arc<NamespaceMutationFence>,
}

impl std::fmt::Debug for RegistryObjectMap {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("RegistryObjectMap")
            .field("registry", &self.registry)
            .field("repository", &self.repository)
            .field("objects", &self.objects.len())
            .finish_non_exhaustive()
    }
}

impl PartialEq for RegistryObjectMap {
    fn eq(&self, other: &Self) -> bool {
        self.registry == other.registry
            && self.repository == other.repository
            && self.objects == other.objects
            && self.versions == other.versions
    }
}

impl Eq for RegistryObjectMap {}

impl RegistryObjectMap {
    pub(crate) fn new(
        client: Arc<RegistryClient>,
        repository: String,
        bindings: &[RegistryObjectBinding],
        cache: Option<Arc<dyn PhysicalRangeCache>>,
    ) -> Result<Arc<Self>, String> {
        if bindings.is_empty() || bindings.len() > MAX_SNAPSHOT_OBJECTS {
            return Err("Registry object map count exceeds its bound".into());
        }
        RegistrySnapshotRef {
            registry: client.registry().into(),
            repository: repository.clone(),
            index_digest: format!("sha256:{}", "0".repeat(64)),
        }
        .validate()?;
        let mut objects = HashMap::new();
        let mut versions = HashMap::new();
        for object in bindings {
            validate_blob(&object.blob)?;
            if objects
                .insert(object.object_key.clone(), object.blob.clone())
                .is_some()
            {
                return Err("Registry object map repeats a logical key".into());
            }
            versions.insert(object.object_key.clone(), object.object_version.clone());
        }
        let registry = client.registry().to_owned();
        let namespace = format!("registry://{registry}/{repository}");
        Ok(Arc::new(Self {
            ranges: RangeIO::for_registry_client_with_cache(Arc::clone(&client), cache),
            fence: NamespaceMutationFence::new(&namespace).map_err(|error| error.to_string())?,
            registry,
            repository,
            objects,
            versions,
            client,
            namespace,
        }))
    }

    pub(crate) fn repository(&self) -> &str {
        &self.repository
    }
    pub(crate) fn client(&self) -> Arc<RegistryClient> {
        Arc::clone(&self.client)
    }
    pub(crate) fn bindings(&self) -> &HashMap<String, BlobRef> {
        &self.objects
    }
    pub(crate) fn object_version(&self, key: &str) -> Option<&str> {
        self.versions.get(key).and_then(Option::as_deref)
    }

    fn object_metadata(&self, key: &str, blob: &BlobRef) -> RemoteObjectMetadata {
        RemoteObjectMetadata {
            size: blob.size,
            sha256: blob.digest.clone(),
            version: self.object_version(key).map(str::to_owned),
            last_modified_unix: None,
            etag: None,
        }
    }

    fn remap<'a>(
        &'a self,
        request: RangeReadRequest<'a>,
    ) -> Result<RangeReadRequest<'a>, RangeReadError> {
        if request.authority.is_some() {
            let physical = request.physical.ok_or(RangeReadError::InvalidRange)?;
            if request.object_key != physical.object_key
                || request.object_sha256 != physical.object_sha256
                || request.object_version.is_some()
                || request.preferred_p2p_source.is_some()
            {
                return Err(RangeReadError::failure(
                    RangeReadFailureClass::Integrity,
                    "External Registry range conflicts with its immutable physical plan",
                ));
            }
            let physical = self.remap_object(physical)?;
            physical.validate()?;
            return Ok(RangeReadRequest {
                object_key: physical.object_key,
                object_sha256: physical.object_sha256,
                authority: Some(&self.registry),
                physical: Some(physical),
                ..request
            });
        }
        let blob = self
            .objects
            .get(request.object_key)
            .ok_or(RangeReadError::NotFound)?;
        if request.object_sha256.trim_start_matches("sha256:")
            != blob.digest.trim_start_matches("sha256:")
            || request
                .object_version
                .is_some_and(|version| self.object_version(request.object_key) != Some(version))
            || request.authority.is_some()
        {
            return Err(RangeReadError::failure(
                RangeReadFailureClass::Integrity,
                "Logical range conflicts with its pinned Registry object",
            ));
        }
        let physical = request.physical.unwrap_or(PhysicalRangeRequest {
            namespace: None,
            object_key: request.object_key,
            object_sha256: request.object_sha256,
            object_size: None,
            object_version: request.object_version,
            offset: request.offset,
            length: request.length,
        });
        if physical.object_key != request.object_key
            || physical.object_sha256 != request.object_sha256
            || physical.object_version != request.object_version
        {
            return Err(RangeReadError::failure(
                RangeReadFailureClass::Integrity,
                "Physical range conflicts with its Registry logical plan",
            ));
        }
        let physical = PhysicalRangeRequest {
            namespace: Some(&self.repository),
            object_key: &blob.digest,
            object_sha256: &blob.digest,
            object_size: Some(blob.size),
            object_version: None,
            offset: physical.offset,
            length: physical.length,
        };
        physical.validate()?;
        Ok(RangeReadRequest {
            object_key: &blob.digest,
            object_sha256: &blob.digest,
            authority: Some(&self.registry),
            physical: Some(physical),
            preferred_p2p_source: None,
            ..request
        })
    }

    fn remap_object<'a>(
        &'a self,
        request: PhysicalRangeRequest<'_>,
    ) -> Result<PhysicalRangeRequest<'a>, RangeReadError> {
        let blob = self
            .objects
            .values()
            .find(|blob| {
                blob.digest == request.object_key
                    && blob.digest == request.object_sha256
                    && request.object_size == Some(blob.size)
            })
            .ok_or(RangeReadError::NotFound)?;
        if request.object_version.is_some() {
            return Err(RangeReadError::InvalidRange);
        }
        Ok(PhysicalRangeRequest {
            namespace: Some(&self.repository),
            object_key: &blob.digest,
            object_sha256: &blob.digest,
            object_size: Some(blob.size),
            object_version: None,
            offset: request.offset,
            length: request.length,
        })
    }
}

fn store_error(error: RegistryError) -> ObjectStoreError {
    ObjectStoreError::new(
        if error.retryable() {
            PublishFailureKind::Retryable
        } else if error.failure_kind() == crate::snapshot::transport::BlobRangeErrorKind::Integrity
        {
            PublishFailureKind::IdentityConflict
        } else {
            PublishFailureKind::Fatal
        },
        error.to_string(),
    )
}

#[async_trait]
impl ImmutableObjectStore for RegistryObjectMap {
    fn namespace(&self) -> &str {
        &self.namespace
    }
    fn fence(&self) -> Arc<NamespaceMutationFence> {
        Arc::clone(&self.fence)
    }
    async fn head(&self, key: &str) -> Result<Option<RemoteObjectMetadata>, ObjectStoreError> {
        let Some(blob) = self.objects.get(key) else {
            return Ok(None);
        };
        if !self
            .client
            .head_blob(&self.repository, blob)
            .await
            .map_err(store_error)?
        {
            return Ok(None);
        }
        Ok(Some(self.object_metadata(key, blob)))
    }
    async fn put_if_absent(&self, _: &str, _: &[u8], _: &str) -> Result<bool, ObjectStoreError> {
        Err(ObjectStoreError::new(
            PublishFailureKind::Fatal,
            "Registry snapshot read map is immutable",
        ))
    }
    async fn get_with_metadata(
        &self,
        key: &str,
        version: Option<&str>,
        max: u64,
    ) -> Result<Option<RemoteObject>, BoundedGetError> {
        if version.is_some_and(|version| self.object_version(key) != Some(version)) {
            return Err(ObjectStoreError::new(
                PublishFailureKind::IdentityConflict,
                "Registry snapshot object logical version differs",
            )
            .into());
        }
        let Some(blob) = self.objects.get(key) else {
            return Ok(None);
        };
        if blob.size > max || usize::try_from(blob.size).is_err() {
            return Err(BoundedGetError::BoundExceeded {
                object_key: key.into(),
                actual_bytes: blob.size,
                max_bytes: max,
            });
        }
        let mut payload = Vec::with_capacity(blob.size as usize);
        while (payload.len() as u64) < blob.size {
            let offset = payload.len() as u64;
            let length = (blob.size - offset).min(16 * 1024 * 1024);
            let bytes = self
                .ranges
                .read_object_range(
                    &self.registry,
                    PhysicalRangeRequest {
                        namespace: Some(&self.repository),
                        object_key: &blob.digest,
                        object_sha256: &blob.digest,
                        object_size: Some(blob.size),
                        object_version: None,
                        offset,
                        length,
                    },
                )
                .await
                .map_err(|error| {
                    ObjectStoreError::new(
                        match &error {
                            RangeReadError::Failure {
                                class: RangeReadFailureClass::Transient,
                                ..
                            } => PublishFailureKind::Retryable,
                            RangeReadError::Failure {
                                class: RangeReadFailureClass::Integrity,
                                ..
                            } => PublishFailureKind::IdentityConflict,
                            _ => PublishFailureKind::Fatal,
                        },
                        error.to_string(),
                    )
                })?;
            if bytes.len() as u64 != length {
                return Err(ObjectStoreError::new(
                    PublishFailureKind::IdentityConflict,
                    "Registry metadata read was short",
                )
                .into());
            }
            payload.extend_from_slice(&bytes);
        }
        if sha256_identity(&payload) != blob.digest {
            return Err(ObjectStoreError::new(
                PublishFailureKind::IdentityConflict,
                "Registry metadata bytes differ from their pinned SHA",
            )
            .into());
        }
        Ok(Some(RemoteObject {
            payload,
            metadata: self.object_metadata(key, blob),
        }))
    }
    async fn get(&self, key: &str) -> Result<Vec<u8>, ObjectStoreError> {
        self.get_with_metadata(key, None, MAX_OCI_METADATA_BYTES as u64)
            .await
            .map_err(|error| match error {
                BoundedGetError::Store(error) => error,
                other => ObjectStoreError::new(PublishFailureKind::Fatal, other.to_string()),
            })?
            .map(|object| object.payload)
            .ok_or_else(|| {
                ObjectStoreError::new(
                    PublishFailureKind::Fatal,
                    "Registry object map has no such key",
                )
            })
    }
}

#[async_trait]
impl RangeReader for RegistryObjectMap {
    async fn read_object_range(
        &self,
        _authority: &str,
        request: PhysicalRangeRequest<'_>,
    ) -> Result<Vec<u8>, RangeReadError> {
        self.ranges
            .read_object_range(&self.registry, self.remap_object(request)?)
            .await
    }
    async fn read_object_range_uncached(
        &self,
        _authority: &str,
        request: PhysicalRangeRequest<'_>,
    ) -> Result<Vec<u8>, RangeReadError> {
        self.ranges
            .read_object_range_uncached(&self.registry, self.remap_object(request)?)
            .await
    }
    async fn invalidate_object_range(
        &self,
        _authority: &str,
        request: PhysicalRangeRequest<'_>,
    ) -> Result<bool, RangeReadError> {
        self.ranges
            .invalidate_object_range(&self.registry, self.remap_object(request)?)
            .await
    }
    async fn allows_provider_coalescing(&self, _: &RuntimeIdentity, _: &P2PSource) -> bool {
        true
    }
    async fn read_range(
        &self,
        request: RangeReadRequest<'_>,
    ) -> Result<RangeReadResult, RangeReadError> {
        let encoded = request.physical.is_some();
        let mut result = self.ranges.read_range(self.remap(request)?).await?;
        if !encoded {
            result.representation = RangeRepresentation::DecodedData;
        }
        Ok(result)
    }
    async fn read_range_uncached(
        &self,
        request: RangeReadRequest<'_>,
    ) -> Result<RangeReadResult, RangeReadError> {
        let encoded = request.physical.is_some();
        let mut result = self
            .ranges
            .read_range_uncached(self.remap(request)?)
            .await?;
        if !encoded {
            result.representation = RangeRepresentation::DecodedData;
        }
        Ok(result)
    }
    async fn invalidate_range(
        &self,
        request: RangeReadRequest<'_>,
    ) -> Result<bool, RangeReadError> {
        self.ranges.invalidate_range(self.remap(request)?).await
    }
}

#[derive(Debug)]
struct EmbeddedResolver<'a> {
    delegate: &'a dyn SourceResolver,
    sources: HashMap<SourceRef, Arc<ResolvedSource>>,
}

#[async_trait]
impl SourceResolver for EmbeddedResolver<'_> {
    async fn resolve_source(&self, source: &SourceRef) -> Result<Arc<ResolvedSource>, SourceError> {
        self.sources
            .get(source)
            .cloned()
            .ok_or_else(|| invalid("Registry snapshot is missing an embedded external source"))
    }
    async fn registry_client(&self, registry: &str) -> Result<Arc<RegistryClient>, SourceError> {
        self.delegate.registry_client(registry).await
    }
    fn registry_physical_cache(&self) -> Option<Arc<dyn PhysicalRangeCache>> {
        self.delegate.registry_physical_cache()
    }
}

pub(crate) async fn load_snapshot(
    io: &SnapshotMetadataReadContext<'_>,
    spec: SnapshotSpec,
    snapshot_id: &str,
    verify_data_objects: bool,
) -> Result<crate::snapshot::format::DecodedSnapshotMetadata, SnapshotLoadError> {
    let reference = spec.registry_snapshot.as_ref().ok_or_else(|| {
        SnapshotLoadError::InvalidSnapshotMetadata(
            "Registry snapshot has no immutable index".into(),
        )
    })?;
    let client = io
        .source_resolver
        .registry_client(&reference.registry)
        .await
        .map_err(source_load_error)?;
    let RegistrySnapshotEnvelope {
        metadata,
        metadata_bytes: envelope_bytes,
        native_layers,
    } = read_snapshot_metadata_with_size(&client, reference)
        .await
        .map_err(source_load_error)?;
    if envelope_bytes > io.limits.max_total_metadata_bytes {
        return Err(SnapshotLoadError::BoundExceeded(
            "Registry snapshot envelope".into(),
        ));
    }
    let expected = metadata
        .bound_spec(reference)
        .map_err(SnapshotLoadError::InvalidSnapshotMetadata)?;
    if expected != spec || metadata.snapshot_id != snapshot_id {
        return Err(SnapshotLoadError::IdentityConflict(
            "Registry index restore metadata differs from its catalog spec".into(),
        ));
    }
    let objects = RegistryObjectMap::new(
        client,
        reference.repository.clone(),
        &metadata.objects,
        io.source_resolver.registry_physical_cache(),
    )
    .map_err(SnapshotLoadError::InvalidSnapshotMetadata)?;
    let mut sources = HashMap::new();
    for source in &metadata.external_sources {
        let bytes = source
            .decode()
            .map_err(SnapshotLoadError::InvalidSnapshotMetadata)?;
        let rootfs = composite::decode_descriptor(&bytes)
            .map_err(SnapshotLoadError::InvalidSnapshotMetadata)?;
        let resolved = ResolvedSource::from_registry(rootfs).map_err(source_load_error)?;
        if resolved.source_ref != source.source_ref {
            return Err(SnapshotLoadError::IdentityConflict(
                "Embedded external source differs".into(),
            ));
        }
        io.source_resolver
            .remember_registry_source(&source.source_ref, &bytes)
            .await
            .map_err(source_load_error)?;
        sources.insert(source.source_ref.clone(), resolved);
    }
    let resolver = EmbeddedResolver {
        delegate: io.source_resolver,
        sources,
    };
    let scoped = io.with_store_and_reader(objects.as_ref(), objects.as_ref(), &resolver);
    let (snapshot, metadata_bytes, physical_chains) =
        crate::snapshot::format::overlaybd_s3::OverlaybdS3Metadata
            .load_snapshot(&scoped, spec, snapshot_id, verify_data_objects)
            .await?;
    if native_layers[0] != physical_chains.rootfs || native_layers[1] != physical_chains.memory {
        return Err(SnapshotLoadError::IdentityConflict(
            "OCI native manifest order differs from its authenticated snapshot chains".into(),
        ));
    }
    let metadata_bytes = metadata_bytes
        .checked_add(envelope_bytes)
        .filter(|bytes| *bytes <= io.limits.max_total_metadata_bytes)
        .ok_or_else(|| {
            SnapshotLoadError::BoundExceeded("Registry snapshot total metadata".into())
        })?;
    let mut persistent = snapshot.persistent_owned().ok_or_else(|| {
        SnapshotLoadError::InvalidSnapshotMetadata(
            "Loaded Registry graph has no persistent view".into(),
        )
    })?;
    let actual_layout = persistent
        .vm_state
        .as_deref()
        .map(|payload| memory_layout_from_state(&persistent.spec, payload))
        .transpose()
        .map_err(SnapshotLoadError::InvalidSnapshotMetadata)?;
    if actual_layout != metadata.memory_layout {
        return Err(SnapshotLoadError::IdentityConflict(
            "VMM state memory regions differ from the OCI snapshot layout".into(),
        ));
    }
    for chain in std::iter::once(&mut persistent.rootfs).chain(persistent.memory.iter_mut()) {
        for layer in &mut chain.layers {
            Arc::make_mut(&mut layer.range_map).registry_objects = Some(Arc::clone(&objects));
        }
    }
    let snapshot = crate::snapshot::Snapshot::from_persistent(snapshot_id.into(), persistent)
        .map_err(SnapshotLoadError::InvalidSnapshotMetadata)?;
    Ok(crate::snapshot::format::DecodedSnapshotMetadata {
        metadata: snapshot,
        metadata_bytes,
    })
}
