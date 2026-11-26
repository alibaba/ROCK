//! Complete OCI snapshots reuse standard OverlayBD graph preparation. The I/O
//! publisher owns bounded uploads; this profile selects the OCI root envelope.

use std::sync::Arc;

use async_trait::async_trait;

use crate::snapshot::format::overlaybd_s3::publication::OverlaybdS3Publication;
use crate::snapshot::format::{
    ImmutableObject, SnapshotPublicationContext, SnapshotUploadError, SnapshotUploadInterface,
    SnapshotUploadPlan, SnapshotUploadSource, UploadedSnapshot,
};
use crate::snapshot::metadata::{
    SnapshotManifest, StorageDescriptor, StorageProvider, VmmCompatibility,
};
use crate::snapshot::transport::{DataPackUploadReceipt, StagedObjectWriter};

/// Publication retains descriptor custody from its authenticated parent map;
/// an evictable resolver cache is never the authority for an OCI ancestor.
#[derive(Debug)]
pub(crate) struct RegistryPublicationResolver<'a> {
    pub fallback: &'a dyn crate::snapshot::SourceResolver,
    pub store: &'a crate::snapshot::io::registry_store::RegistryGraphStore,
    pub sources: &'a [Arc<super::RegistryObjectMap>],
}

#[async_trait]
impl crate::snapshot::SourceResolver for RegistryPublicationResolver<'_> {
    async fn resolve_source(
        &self,
        source: &crate::snapshot::metadata::SourceRef,
    ) -> Result<Arc<crate::snapshot::ResolvedSource>, crate::snapshot::SourceError> {
        use crate::snapshot::transport::ImmutableObjectStore;
        use crate::snapshot::{ResolvedSource, SourceError, ValidateCanonicalModel};
        source.validate().map_err(SourceError::Invalid)?;
        let crate::snapshot::metadata::SourceRef::Registry {
            descriptor_sha256, ..
        } = source;
        let key = format!(
            "layers/block/1/{}/registry-overlaybd.json",
            descriptor_sha256.trim_start_matches("sha256:")
        );
        if !self
            .sources
            .iter()
            .any(|parent| parent.bindings().contains_key(&key))
        {
            return self.fallback.resolve_source(source).await;
        }
        let object = self.store.get_with_metadata(&key, None, crate::snapshot::metadata::MAX_RANGE_MAP_BYTES as u64)
            .await.map_err(|error| {
                let retryable = matches!(&error, crate::snapshot::transport::BoundedGetError::Store(error) if error.retryable());
                SourceError::Transport(crate::snapshot::transport::BlobRangeError::new(
                    if retryable { crate::snapshot::transport::BlobRangeErrorKind::Transient }
                    else { crate::snapshot::transport::BlobRangeErrorKind::Integrity }, error.to_string()))
            })?.ok_or_else(|| SourceError::Integrity("Pinned OCI ancestor descriptor is absent".into()))?;
        if object.metadata.sha256 != *descriptor_sha256
            || crate::snapshot::metadata::sha256_identity(&object.payload) != *descriptor_sha256
        {
            return Err(SourceError::Integrity(
                "Pinned OCI ancestor descriptor differs".into(),
            ));
        }
        let rootfs =
            crate::snapshot::format::overlaybd::composite::decode_descriptor(&object.payload)
                .map_err(SourceError::Integrity)?;
        let resolved = ResolvedSource::from_registry(rootfs)?;
        if resolved.source_ref != *source {
            return Err(SourceError::Integrity(
                "Pinned OCI ancestor source differs".into(),
            ));
        }
        Ok(resolved)
    }

    async fn registry_client(
        &self,
        registry: &str,
    ) -> Result<
        Arc<crate::snapshot::transport::registry::RegistryClient>,
        crate::snapshot::SourceError,
    > {
        self.fallback.registry_client(registry).await
    }
}

pub(crate) async fn prepare_registry_documents(
    plan: &SnapshotUploadPlan,
    store: &crate::snapshot::io::registry_store::RegistryGraphStore,
    resolver: &dyn crate::snapshot::SourceResolver,
) -> Result<
    (
        super::RegistrySnapshotDocuments,
        super::RegistrySnapshotMetadata,
    ),
    SnapshotUploadError,
> {
    use crate::snapshot::metadata::{
        canonical_json_bytes, parse_canonical_model, sha256_identity, LayerChainIndex, LayerKind,
        MAX_RANGE_MAP_BYTES,
    };
    use crate::snapshot::transport::{BlobRef, BoundedGetError, ImmutableObjectStore};

    async fn read(
        store: &dyn ImmutableObjectStore,
        key: &str,
        expected: &str,
        max_bytes: usize,
    ) -> Result<Vec<u8>, SnapshotUploadError> {
        let object = store
            .get_with_metadata(key, None, max_bytes as u64)
            .await
            .map_err(|error| match error {
                BoundedGetError::Store(error) => SnapshotUploadError::Store(error),
                error => SnapshotUploadError::InvalidPlan(error.to_string()),
            })?
            .ok_or_else(|| {
                SnapshotUploadError::InvalidPlan(format!("Registry graph object {key:?} is absent"))
            })?;
        if object.metadata.sha256 != expected
            || object.payload.len() as u64 != object.metadata.size
            || sha256_identity(&object.payload) != expected
        {
            return Err(SnapshotUploadError::IdentityConflict(format!(
                "Registry graph object {key:?} differs"
            )));
        }
        Ok(object.payload)
    }

    let mut disk_layers = Vec::new();
    let mut memory_layers = Vec::new();
    let mut external_sources = Vec::new();
    for (kind, reference) in std::iter::once((LayerKind::Block, &plan.spec.rootfs)).chain(
        plan.spec
            .memory
            .as_ref()
            .map(|memory| (LayerKind::Memory, memory)),
    ) {
        let payload = read(
            store,
            &reference.index_object_key,
            &reference.index_sha256,
            8 * 1024 * 1024,
        )
        .await?;
        let chain: LayerChainIndex = parse_canonical_model(&payload, "OCI layer chain")
            .map_err(|error| SnapshotUploadError::InvalidPlan(error.to_string()))?;
        if chain.kind != kind || chain.logical_bytes != reference.logical_bytes {
            return Err(SnapshotUploadError::IdentityConflict(
                "OCI layer chain geometry differs".into(),
            ));
        }
        for layer in &chain.layers {
            if let Some(source) = &layer.range_map.source_ref {
                // A captured OCI parent already authenticates this descriptor.
                // Its old image registry and the resolver's evictable cache are
                // not dependencies of later forks.
                let resolved = if plan
                    .registry_sources
                    .iter()
                    .any(|parent| parent.bindings().contains_key(&layer.range_map.object_key))
                {
                    let descriptor = read(
                        store,
                        &layer.range_map.object_key,
                        &layer.range_map.sha256,
                        MAX_RANGE_MAP_BYTES,
                    )
                    .await?;
                    let rootfs = crate::snapshot::format::overlaybd::composite::decode_descriptor(
                        &descriptor,
                    )
                    .map_err(SnapshotUploadError::InvalidPlan)?;
                    let resolved = crate::snapshot::ResolvedSource::from_registry(rootfs).map_err(
                        |error| SnapshotUploadError::IdentityConflict(error.to_string()),
                    )?;
                    if resolved.source_ref != *source {
                        return Err(SnapshotUploadError::IdentityConflict(
                            "Embedded OCI ancestor source differs".into(),
                        ));
                    }
                    resolved
                } else {
                    resolver.resolve_source(source).await.map_err(|error| {
                        SnapshotUploadError::Data {
                            retryable: error.retryable(),
                            message: error.to_string(),
                        }
                    })?
                };
                let rootfs = resolved.range_map.registry.as_ref().ok_or_else(|| {
                    SnapshotUploadError::InvalidPlan(
                        "Registry ancestor has no native descriptor".into(),
                    )
                })?;
                let embedded =
                    super::RegistryEmbeddedSource::new(source.clone(), resolved.descriptor.clone())
                        .map_err(SnapshotUploadError::InvalidPlan)?;
                if !external_sources
                    .iter()
                    .any(|other: &super::RegistryEmbeddedSource| other.source_ref == *source)
                {
                    external_sources.push(embedded);
                }
                if kind != LayerKind::Block || resolved.descriptor_sha256 != layer.range_map.sha256
                {
                    return Err(SnapshotUploadError::IdentityConflict(
                        "External OCI layer binding differs".into(),
                    ));
                }
                store
                    .put_if_absent(
                        &layer.range_map.object_key,
                        &resolved.descriptor,
                        &resolved.descriptor_sha256,
                    )
                    .await?;
                let prefix = layer
                    .range_map
                    .object_key
                    .strip_suffix("registry-overlaybd.json")
                    .ok_or_else(|| {
                        SnapshotUploadError::InvalidPlan(
                            "External OCI descriptor path differs".into(),
                        )
                    })?;
                let marker = canonical_json_bytes(&serde_json::json!({
                    "content_sha256": layer.content_sha256, "format_version": 1,
                    "range_map_sha256": layer.range_map.sha256, "kind": LayerKind::Block,
                }))
                .map_err(|error| SnapshotUploadError::InvalidPlan(error.to_string()))?;
                store
                    .put_if_absent(
                        &format!("{prefix}COMMITTED.registry-overlaybd"),
                        &marker,
                        &sha256_identity(&marker),
                    )
                    .await?;
                for native in rootfs.layers() {
                    let blob = BlobRef {
                        digest: native.digest.clone(),
                        size: native.size,
                    };
                    let key = format!(
                        "registry/blobs/{}",
                        blob.digest.trim_start_matches("sha256:")
                    );
                    match store.head(&key).await? {
                        Some(found) if found.sha256 == blob.digest && found.size == blob.size => {}
                        Some(_) => {
                            return Err(SnapshotUploadError::IdentityConflict(
                                "Native OCI ancestor blob differs".into(),
                            ))
                        }
                        None => {
                            let client = resolver
                                .registry_client(rootfs.registry())
                                .await
                                .map_err(|error| SnapshotUploadError::Data {
                                    retryable: error.retryable(),
                                    message: error.to_string(),
                                })?;
                            store
                                .copy_registry_blob(client, rootfs.repository(), &blob, &key)
                                .await?;
                        }
                    }
                    disk_layers.push(blob);
                }
            } else {
                let descriptor = read(
                    store,
                    &layer.range_map.object_key,
                    &layer.range_map.sha256,
                    MAX_RANGE_MAP_BYTES,
                )
                .await?;
                let (map, blob, length) =
                    crate::snapshot::format::overlaybd::decode_descriptor_with_blob(&descriptor)
                        .map_err(SnapshotUploadError::InvalidPlan)?;
                if map.kind != kind
                    || map.content_sha256 != layer.content_sha256
                    || map.logical_bytes != chain.logical_bytes
                    || map.unit_size_bytes != chain.unit_size_bytes
                {
                    return Err(SnapshotUploadError::IdentityConflict(
                        "OCI standard layer descriptor differs".into(),
                    ));
                }
                let found = store.head(&blob.object_key).await?.ok_or_else(|| {
                    SnapshotUploadError::InvalidPlan("OCI ancestor DATA is absent".into())
                })?;
                if found.sha256 != blob.sha256 || found.size != length {
                    return Err(SnapshotUploadError::IdentityConflict(
                        "OCI ancestor DATA identity differs".into(),
                    ));
                }
                let physical = BlobRef {
                    digest: blob.sha256,
                    size: length,
                };
                match kind {
                    LayerKind::Block => disk_layers.push(physical),
                    LayerKind::Memory => memory_layers.push(physical),
                }
            }
        }
    }
    let metadata = super::RegistrySnapshotMetadata::new(
        plan.spec.clone(),
        store.bindings()?,
        external_sources,
        plan.vm_state.as_ref().map(|state| state.payload.as_slice()),
    )
    .map_err(SnapshotUploadError::InvalidPlan)?;
    let documents = super::build_snapshot_documents(&metadata, &disk_layers, &memory_layers)
        .map_err(SnapshotUploadError::InvalidPlan)?;
    Ok((documents, metadata))
}

#[derive(Debug)]
pub(crate) struct OverlaybdOciPublication;

#[async_trait]
impl SnapshotUploadInterface for OverlaybdOciPublication {
    fn prepare_upload(
        &self,
        source: &dyn SnapshotUploadSource,
        storage: StorageDescriptor,
        compatibility: VmmCompatibility,
        scratch: &dyn Fn(u64) -> Result<Arc<dyn StagedObjectWriter>, SnapshotUploadError>,
    ) -> Result<SnapshotUploadPlan, SnapshotUploadError> {
        if storage.provider != StorageProvider::Registry {
            return Err(SnapshotUploadError::InvalidPlan(
                "OCI publication requires Registry storage".into(),
            ));
        }
        OverlaybdS3Publication.prepare_upload(source, storage, compatibility, scratch)
    }

    fn validate_prepared(&self, plan: &SnapshotUploadPlan) -> Result<(), SnapshotUploadError> {
        if plan.spec.storage.provider != StorageProvider::Registry
            || plan.spec.registry_snapshot.is_some()
        {
            return Err(SnapshotUploadError::InvalidPlan(
                "OCI plan must contain an unpublished Registry graph".into(),
            ));
        }
        plan.validate_shallow()
    }

    async fn validate_uploaded_artifacts(
        &self,
        plan: &SnapshotUploadPlan,
        receipts: &[DataPackUploadReceipt],
        context: &SnapshotPublicationContext<'_>,
    ) -> Result<SnapshotManifest, SnapshotUploadError> {
        OverlaybdS3Publication
            .validate_uploaded_artifacts(plan, receipts, context)
            .await
    }

    fn root_metadata<'a>(
        &self,
        plan: &'a SnapshotUploadPlan,
    ) -> Result<&'a ImmutableObject, SnapshotUploadError> {
        Ok(&plan.manifest)
    }

    fn validate_published(&self, published: &UploadedSnapshot) -> Result<(), SnapshotUploadError> {
        if published.spec.registry_snapshot.is_none() || published.registry_objects.is_none() {
            return Err(SnapshotUploadError::IdentityConflict(
                "Published OCI root has no authenticated Registry binding".into(),
            ));
        }
        OverlaybdS3Publication.validate_published(published)
    }
}
