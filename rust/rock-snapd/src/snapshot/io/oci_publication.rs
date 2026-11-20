//! Bounded execution of complete OCI snapshot publication. Format code builds
//! the graph and OCI documents; transport publishes their physical bytes.

use std::sync::Arc;
use std::time::Instant;

use tokio::sync::{watch, Semaphore};

use super::range_io::RangeWriter;
use super::registry_store::{RegistryCopyBudget, RegistryGraphStore};
use super::snapshot_io::{data_error, SnapshotDataUploadConfig, SnapshotPublisher};
use crate::snapshot::format::overlaybd_oci::{RegistryObjectBinding, RegistryObjectMap};
use crate::snapshot::format::{
    SnapshotPublicationContext, SnapshotUploadError, SnapshotUploadPlan, UploadedSnapshot,
};
use crate::snapshot::metadata::{RegistrySnapshotRef, SnapshotProvenance};
use crate::snapshot::transport::registry::{
    RegistryClient, RegistryReference, RegistryWriteTarget,
};
use crate::snapshot::transport::{
    BlobRef, DataPackWriteTarget, ImmutableObjectStore, ImmutableStreamWriter, IoCancellation,
    PhysicalRangeCache, PhysicalRangeReader, SnapshotMetadataWriteTarget, UploadTransactionLease,
};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RegistrySnapshotTagMode {
    #[default]
    SourceSnapshot,
    SnapshotId,
}

#[derive(Clone)]
pub(crate) struct RegistrySnapshotPublication {
    client: Arc<RegistryClient>,
    #[cfg(test)]
    writer: Option<Arc<RegistryWriteTarget>>,
    fallback_store: Option<Arc<dyn ImmutableObjectStore>>,
    fallback_reader: Option<Arc<dyn PhysicalRangeReader>>,
    cache: Option<Arc<dyn PhysicalRangeCache>>,
    tag_prefix: String,
    tag_mode: RegistrySnapshotTagMode,
    limits: SnapshotDataUploadConfig,
}

impl std::fmt::Debug for RegistrySnapshotPublication {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("RegistrySnapshotPublication")
            .field("client", &self.client)
            .field("tag_prefix", &self.tag_prefix)
            .field("tag_mode", &self.tag_mode)
            .finish_non_exhaustive()
    }
}

impl RegistrySnapshotPublication {
    #[cfg(test)]
    pub(crate) fn new(
        writer: Arc<RegistryWriteTarget>,
        fallback_store: Option<Arc<dyn ImmutableObjectStore>>,
        fallback_reader: Option<Arc<dyn PhysicalRangeReader>>,
        cache: Option<Arc<dyn PhysicalRangeCache>>,
        tag_prefix: String,
        limits: SnapshotDataUploadConfig,
    ) -> Arc<Self> {
        Arc::new(Self {
            client: Arc::clone(writer.client()),
            writer: Some(writer),
            fallback_store,
            fallback_reader,
            cache,
            tag_prefix,
            tag_mode: RegistrySnapshotTagMode::default(),
            limits,
        })
    }

    pub(crate) fn new_configured(
        client: Arc<RegistryClient>,
        fallback_store: Option<Arc<dyn ImmutableObjectStore>>,
        fallback_reader: Option<Arc<dyn PhysicalRangeReader>>,
        cache: Option<Arc<dyn PhysicalRangeCache>>,
        tag_prefix: String,
        limits: SnapshotDataUploadConfig,
    ) -> Arc<Self> {
        Arc::new(Self {
            client,
            #[cfg(test)]
            writer: None,
            fallback_store,
            fallback_reader,
            cache,
            tag_prefix,
            tag_mode: RegistrySnapshotTagMode::default(),
            limits,
        })
    }

    pub(crate) fn with_tag_mode(mut self: Arc<Self>, mode: RegistrySnapshotTagMode) -> Arc<Self> {
        Arc::make_mut(&mut self).tag_mode = mode;
        self
    }

    /// The canonical write-domain registry needs an empty capability before
    /// captures exist. Every active publication gets its own scoped object map.
    #[cfg(test)]
    pub(crate) fn metadata_store(
        &self,
    ) -> Result<Arc<dyn ImmutableObjectStore>, SnapshotUploadError> {
        let writer = self.writer.clone().ok_or_else(|| {
            SnapshotUploadError::InvalidPlan(
                "OCI publication requires a resolved repository".into(),
            )
        })?;
        self.metadata_store_for(writer)
    }

    pub(crate) fn metadata_store_for(
        &self,
        writer: Arc<RegistryWriteTarget>,
    ) -> Result<Arc<dyn ImmutableObjectStore>, SnapshotUploadError> {
        if !Arc::ptr_eq(writer.client(), &self.client) {
            return Err(SnapshotUploadError::IdentityConflict(
                "OCI repository target uses another configured Registry client".into(),
            ));
        }
        let (_sender, receiver) = watch::channel(false);
        let budget = RegistryCopyBudget {
            transfer_permits: Arc::new(Semaphore::new(self.limits.max_concurrent_uploads)),
            buffer_permits: Arc::new(Semaphore::new(self.limits.max_bytes_in_flight as usize)),
            chunk_bytes: self.limits.stream_chunk_bytes(),
            attempt_timeout: self.limits.attempt_timeout,
            cancellation: IoCancellation::new(receiver),
        };
        Ok(RegistryGraphStore::new(
            writer,
            self.fallback_store.clone(),
            self.fallback_reader.clone(),
            Vec::new(),
            Vec::new(),
            None,
            budget,
        )?)
    }

    pub(super) async fn publish(
        &self,
        publisher: &Arc<SnapshotPublisher>,
        plan: SnapshotUploadPlan,
        original_target: SnapshotMetadataWriteTarget,
        data_target: DataPackWriteTarget,
    ) -> Result<UploadedSnapshot, SnapshotUploadError> {
        let tag = snapshot_tag_for_mode(
            self.tag_mode,
            &self.tag_prefix,
            &plan.snapshot_id,
            plan.spec.provenance.as_ref(),
        )?;
        let writer = self.writer_for_targets(&original_target, &data_target)?;
        let lease = UploadTransactionLease::acquire(writer.fence()).await;
        let data_requests = plan
            .layers
            .iter()
            .flat_map(|layer| layer.data_requests().iter().cloned())
            .collect::<Vec<_>>();
        let mut planned = plan
            .all_metadata_objects()
            .into_iter()
            .map(|object| RegistryObjectBinding {
                object_key: object.object_key.clone(),
                blob: BlobRef {
                    digest: object.sha256.clone(),
                    size: object.payload.len() as u64,
                },
                object_version: None,
            })
            .collect::<Vec<_>>();
        planned.extend(data_requests.iter().map(|request| RegistryObjectBinding {
            object_key: request.identity.object_key.as_str().to_owned(),
            blob: BlobRef {
                digest: request.identity.expected_sha256.clone(),
                size: request.identity.length,
            },
            object_version: None,
        }));
        let budget = RegistryCopyBudget {
            transfer_permits: Arc::clone(&publisher.data_service.transfer_permits),
            buffer_permits: Arc::clone(&publisher.data_service.buffer_permits),
            chunk_bytes: publisher.data_service.config.stream_chunk_bytes(),
            attempt_timeout: publisher.data_service.config.attempt_timeout,
            cancellation: IoCancellation::new(publisher.data_service.shutdown.subscribe()),
        };
        let store = RegistryGraphStore::new(
            Arc::clone(&writer),
            self.fallback_store.clone(),
            self.fallback_reader.clone(),
            plan.registry_sources.clone(),
            planned,
            Some(lease.clone()),
            budget.clone(),
        )?;
        let target = SnapshotMetadataWriteTarget::new(
            store.clone(),
            data_target.write_target_identity().to_owned(),
            writer.fence(),
        )?;
        publisher
            .data_service
            .scan_packs(&data_requests)
            .await
            .map_err(data_error)?;
        let mut receipts = Vec::new();
        for layer in &plan.layers {
            receipts.extend(
                publisher
                    .publish_layer(
                        layer.clone(),
                        target.clone(),
                        data_target.clone(),
                        lease.clone(),
                    )
                    .await?,
            );
        }
        for object in &plan.chain_indexes {
            publisher.ensure(&target, object, &lease).await?;
        }
        if let Some(state) = &plan.vm_state {
            publisher.ensure(&target, state, &lease).await?;
        }
        let resolver =
            crate::snapshot::format::overlaybd_oci::publication::RegistryPublicationResolver {
                fallback: publisher.source_resolver.as_ref(),
                store: store.as_ref(),
                sources: &plan.registry_sources,
            };
        // Validate every ancestor and committed marker before making a root
        // visible. The store imports missing S3/Registry ancestors by digest.
        let manifest = publisher
            .format
            .validate_uploaded_artifacts(
                &plan,
                &receipts,
                &SnapshotPublicationContext::new(
                    &target,
                    &lease,
                    &resolver,
                    publisher.range_writer.as_ref(),
                ),
            )
            .await?;
        publisher.ensure(&target, &plan.manifest, &lease).await?;
        let (documents, metadata) =
            crate::snapshot::format::overlaybd_oci::publication::prepare_registry_documents(
                &plan,
                store.as_ref(),
                &resolver,
            )
            .await?;
        // Configs, children, immutable index and finally the tag are distinct
        // publication boundaries. Every referenced blob already exists here.
        for config in std::iter::once(&documents.disk_config).chain(documents.memory_config.iter())
        {
            ensure_registry_config(
                publisher.range_writer.as_ref(),
                &target,
                store.as_ref(),
                &format!(
                    "oci/configs/{}",
                    config.digest.trim_start_matches("sha256:")
                ),
                &config.payload,
                &config.digest,
                &lease,
            )
            .await?;
        }
        for child in
            std::iter::once(&documents.disk_manifest).chain(documents.memory_manifest.iter())
        {
            writer
                .put_manifest(
                    &child.digest,
                    child.media_type,
                    &child.payload,
                    &lease,
                    Instant::now() + budget.attempt_timeout,
                    &budget.cancellation,
                )
                .await?;
        }
        writer
            .put_manifest(
                &documents.index.digest,
                documents.index.media_type,
                &documents.index.payload,
                &lease,
                Instant::now() + budget.attempt_timeout,
                &budget.cancellation,
            )
            .await?;
        writer
            .put_manifest(
                &tag,
                documents.index.media_type,
                &documents.index.payload,
                &lease,
                Instant::now() + budget.attempt_timeout,
                &budget.cancellation,
            )
            .await?;
        let reference = RegistrySnapshotRef {
            registry: writer.client().registry().to_owned(),
            repository: writer.repository().to_owned(),
            index_digest: documents.index.digest,
        };
        let spec = metadata
            .bound_spec(&reference)
            .map_err(SnapshotUploadError::IdentityConflict)?;
        let registry_objects = RegistryObjectMap::new(
            Arc::clone(writer.client()),
            writer.repository().to_owned(),
            &metadata.objects,
            self.cache.clone(),
        )
        .map_err(SnapshotUploadError::IdentityConflict)?;
        let published = UploadedSnapshot {
            spec,
            manifest,
            registry_objects: Some(registry_objects),
        };
        publisher.format.validate_published(&published)?;
        for request in data_requests {
            request.source.mark_remote_committed();
        }
        Ok(published)
    }

    fn writer_for_targets(
        &self,
        metadata_target: &SnapshotMetadataWriteTarget,
        data_target: &DataPackWriteTarget,
    ) -> Result<Arc<RegistryWriteTarget>, SnapshotUploadError> {
        if metadata_target.namespace() != data_target.namespace()
            || metadata_target.write_target_identity() != data_target.write_target_identity()
            || !Arc::ptr_eq(&metadata_target.fence(), &data_target.fence())
        {
            return Err(SnapshotUploadError::IdentityConflict(
                "OCI publication targets do not share one Registry domain".into(),
            ));
        }
        let prefix = format!("{}/", self.client.registry());
        let repository = metadata_target
            .namespace()
            .strip_prefix(&prefix)
            .ok_or_else(|| {
                SnapshotUploadError::IdentityConflict(
                    "OCI publication target differs from its configured Registry authority".into(),
                )
            })?;
        RegistryWriteTarget::new(
            Arc::clone(&self.client),
            repository.to_owned(),
            metadata_target.write_target_identity().to_owned(),
            metadata_target.fence(),
        )
        .map_err(|error| SnapshotUploadError::IdentityConflict(error.to_string()))
    }
}

fn snapshot_tag_for_mode(
    mode: RegistrySnapshotTagMode,
    tag_prefix: &str,
    snapshot_id: &str,
    provenance: Option<&SnapshotProvenance>,
) -> Result<String, SnapshotUploadError> {
    if mode == RegistrySnapshotTagMode::SourceSnapshot {
        return snapshot_tag(tag_prefix, snapshot_id, provenance);
    }
    if snapshot_id.is_empty()
        || snapshot_id.len() > 128
        || !snapshot_id.bytes().enumerate().all(|(index, byte)| {
            byte.is_ascii_alphanumeric() || byte == b'_' || (index > 0 && b".-".contains(&byte))
        })
    {
        return Err(SnapshotUploadError::InvalidPlan(
            "snapshot_id tag mode requires an unchanged valid Registry tag of 1..=128 ASCII bytes"
                .into(),
        ));
    }
    Ok(snapshot_id.to_owned())
}

fn snapshot_tag(
    tag_prefix: &str,
    snapshot_id: &str,
    provenance: Option<&SnapshotProvenance>,
) -> Result<String, SnapshotUploadError> {
    let source = provenance.and_then(|provenance| provenance.oci_ref.as_deref());
    let mut tag = if let Some(source) = source {
        let parsed = RegistryReference::parse(source)
            .map_err(|error| SnapshotUploadError::InvalidPlan(error.to_string()))?;
        let source = source.strip_prefix("docker://").unwrap_or(source);
        let (_, image) = source
            .split_once('/')
            .expect("validated Registry reference");
        let source_tag = if let Some((name, digest)) = image.rsplit_once('@') {
            name.rsplit_once(':')
                .map(|(_, tag)| tag.to_owned())
                .unwrap_or_else(|| digest.replace(':', "-"))
        } else {
            parsed.reference
        };
        let tag = format!("{source_tag}-{tag_prefix}{snapshot_id}");
        if !tag.bytes().enumerate().all(|(index, byte)| {
            byte.is_ascii_alphanumeric() || byte == b'_' || (index > 0 && b".-".contains(&byte))
        }) {
            return Err(SnapshotUploadError::InvalidPlan(
                "OCI snapshot tag is not canonical".into(),
            ));
        }
        if tag.len() > 128 {
            let identity = crate::snapshot::metadata::sha256_identity(tag.as_bytes());
            format!("{}-{}", &tag[..63], identity.trim_start_matches("sha256:"))
        } else {
            tag
        }
    } else {
        format!("{tag_prefix}{snapshot_id}")
    };
    if tag.len() > 128 {
        let identity = crate::snapshot::metadata::sha256_identity(snapshot_id.as_bytes());
        tag = format!("{tag_prefix}_{}", identity.trim_start_matches("sha256:"));
    }
    if tag.is_empty()
        || tag.len() > 128
        || !tag.bytes().enumerate().all(|(index, byte)| {
            byte.is_ascii_alphanumeric() || byte == b'_' || (index > 0 && b".-".contains(&byte))
        })
    {
        return Err(SnapshotUploadError::InvalidPlan(
            "OCI snapshot tag is not canonical".into(),
        ));
    }
    Ok(tag)
}

/// Register the new config before the ordinary immutable metadata contract
/// performs HEAD. This keeps optional ancestry providers out of its lookup;
/// read-back verification also covers retry reuse of an existing config blob.
async fn ensure_registry_config(
    range_writer: &dyn RangeWriter,
    target: &SnapshotMetadataWriteTarget,
    store: &RegistryGraphStore,
    key: &str,
    payload: &[u8],
    digest: &str,
    lease: &UploadTransactionLease,
) -> Result<(), SnapshotUploadError> {
    store.put_if_absent(key, payload, digest).await?;
    range_writer
        .ensure_metadata_verified(target, key, payload, digest, lease)
        .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::convert::Infallible;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::time::Duration;

    use hyper::service::{make_service_fn, service_fn};
    use hyper::{Body, Method, Response};

    use crate::snapshot::metadata::sha256_identity;
    use crate::snapshot::transport::registry::RegistryClient;
    use crate::snapshot::transport::NamespaceMutationFence;

    fn provenance(reference: &str) -> SnapshotProvenance {
        SnapshotProvenance {
            oci_ref: Some(reference.into()),
            oci_digest: Some(format!("sha256:{}", "a".repeat(64))),
        }
    }

    #[test]
    fn default_tag_mode_preserves_source_and_legacy_tag_selection() {
        let source = provenance("source.test/team/app:v1");
        for provenance in [None, Some(&source)] {
            assert_eq!(
                snapshot_tag_for_mode(
                    RegistrySnapshotTagMode::default(),
                    "saved-",
                    "capture1",
                    provenance,
                )
                .unwrap(),
                snapshot_tag("saved-", "capture1", provenance).unwrap()
            );
        }
    }

    #[test]
    fn snapshot_id_tags_preserve_ids_and_ignore_source_provenance_and_tag_prefix() {
        let invalid_source = provenance("not-a-registry-reference");
        for id in [
            "019a9d6d-6543-7f00-a012-0123456789ab".to_owned(),
            "_Replay.A-1".to_owned(),
            "harbor-base-0123456789abcdef0123456789abcdef".to_owned(),
            "relay-019a9d6d-6543-7f00-a012-0123456789ab-18446744073709551615".to_owned(),
            "s".repeat(128),
        ] {
            for provenance in [None, Some(&invalid_source)] {
                assert_eq!(
                    snapshot_tag_for_mode(
                        RegistrySnapshotTagMode::SnapshotId,
                        "ignored-",
                        &id,
                        provenance,
                    )
                    .unwrap(),
                    id
                );
            }
        }
    }

    #[test]
    fn snapshot_id_tags_reject_invalid_or_long_ids_without_rewriting() {
        for id in [
            "".to_owned(),
            "s".repeat(129),
            "-leading".to_owned(),
            ".leading".to_owned(),
            "a/b".to_owned(),
            "sha256:0123456789abcdef".to_owned(),
            "a@digest".to_owned(),
            "a b".to_owned(),
            "a\n".to_owned(),
            "快照".to_owned(),
        ] {
            assert!(matches!(
                snapshot_tag_for_mode(RegistrySnapshotTagMode::SnapshotId, "snapshot-", &id, None),
                Err(SnapshotUploadError::InvalidPlan(_))
            ));
        }
    }

    #[test]
    fn snapshot_tags_identify_the_source_tag_and_snapshot_generation() {
        for (reference, label) in [
            ("source.test/team/app:v1", "v1".to_owned()),
            ("docker://source.test/team/app", "latest".to_owned()),
            (
                &format!("source.test/team/app:v2@sha256:{}", "a".repeat(64)),
                "v2".to_owned(),
            ),
            (
                &format!("source.test/team/app@sha256:{}", "a".repeat(64)),
                format!("sha256-{}", "a".repeat(64)),
            ),
        ] {
            let source = provenance(reference);
            assert_eq!(
                snapshot_tag("snapshot-", "capture1", Some(&source)).unwrap(),
                format!("{label}-snapshot-capture1")
            );
            assert_eq!(
                snapshot_tag("saved-", "capture2", Some(&source)).unwrap(),
                format!("{label}-saved-capture2")
            );
        }
    }

    #[test]
    fn long_source_tags_remain_readable_and_bind_the_complete_identity() {
        let shared = "a".repeat(100);
        let first = provenance(&format!("source.test/team/app:{shared}1"));
        let second = provenance(&format!("source.test/team/app:{shared}2"));
        let id = "s".repeat(100);
        let first_tag = snapshot_tag("snapshot-", &id, Some(&first)).unwrap();
        let second_tag = snapshot_tag("snapshot-", &id, Some(&second)).unwrap();
        assert_eq!(first_tag.len(), 128);
        assert!(first_tag.starts_with(&"a".repeat(63)));
        assert_ne!(first_tag, second_tag);
        assert_ne!(
            first_tag,
            snapshot_tag("snapshot-", &format!("{id}2"), Some(&first)).unwrap()
        );
        assert_eq!(
            first_tag,
            snapshot_tag("snapshot-", &id, Some(&first)).unwrap()
        );
    }

    #[test]
    fn snapshots_without_source_provenance_keep_legacy_tags() {
        assert_eq!(
            snapshot_tag("snapshot-", "capture1", None).unwrap(),
            "snapshot-capture1"
        );
        let id = "s".repeat(200);
        assert_eq!(
            snapshot_tag("snapshot-", &id, None).unwrap(),
            format!(
                "snapshot-_{}",
                sha256_identity(id.as_bytes()).trim_start_matches("sha256:")
            )
        );
        let digest_only = SnapshotProvenance {
            oci_ref: None,
            oci_digest: Some(sha256_identity(b"old")),
        };
        assert_eq!(
            snapshot_tag("saved-", "old1", Some(&digest_only)).unwrap(),
            "saved-old1"
        );
    }

    #[test]
    fn source_tag_validation_rejects_invalid_references_and_unicode_without_panicking() {
        for reference in [
            "app:v1",
            "source.test/../app:v1",
            "source.test/team/app:bad/tag",
        ] {
            assert!(snapshot_tag("snapshot-", "capture1", Some(&provenance(reference))).is_err());
        }
        assert!(snapshot_tag(
            "snapshot-",
            &"快照".repeat(100),
            Some(&provenance("source.test/team/app:v1"))
        )
        .is_err());
        assert!(snapshot_tag(
            "snapshot-",
            &format!("{}/bad", "s".repeat(200)),
            Some(&provenance("source.test/team/app:v1"))
        )
        .is_err());
    }

    fn targets(
        publication: &RegistrySnapshotPublication,
        client: Arc<RegistryClient>,
        repository: &str,
    ) -> (SnapshotMetadataWriteTarget, DataPackWriteTarget) {
        let namespace = format!("{}/{repository}", client.registry());
        let fence = NamespaceMutationFence::new(&namespace).unwrap();
        let identity = format!("registry:{namespace}");
        let writer = RegistryWriteTarget::new(
            client,
            repository.into(),
            identity.clone(),
            Arc::clone(&fence),
        )
        .unwrap();
        let store = publication.metadata_store_for(Arc::clone(&writer)).unwrap();
        (
            SnapshotMetadataWriteTarget::new(store, identity.clone(), Arc::clone(&fence)).unwrap(),
            DataPackWriteTarget::new(writer, identity, fence).unwrap(),
        )
    }

    #[test]
    fn configured_publication_scopes_each_source_repository_to_its_own_domain() {
        let client = RegistryClient::new_configured("upload.test", false, None, None).unwrap();
        let publication = RegistrySnapshotPublication::new_configured(
            Arc::clone(&client),
            None,
            None,
            None,
            "snapshot-".into(),
            SnapshotDataUploadConfig::default(),
        );
        assert!(publication.metadata_store().is_err());
        let first = targets(&publication, Arc::clone(&client), "archive/team/app");
        let second = targets(&publication, Arc::clone(&client), "archive/other/app");
        let first_writer = publication.writer_for_targets(&first.0, &first.1).unwrap();
        let second_writer = publication
            .writer_for_targets(&second.0, &second.1)
            .unwrap();
        assert_eq!(first_writer.repository(), "archive/team/app");
        assert_eq!(second_writer.repository(), "archive/other/app");
        assert!(Arc::ptr_eq(&first_writer.fence(), &first.0.fence()));
        assert!(publication.writer_for_targets(&first.0, &second.1).is_err());
        let another_client =
            RegistryClient::new_configured("upload.test", false, None, None).unwrap();
        let another_fence = NamespaceMutationFence::new("upload.test/archive/team/app").unwrap();
        let another_writer = RegistryWriteTarget::new(
            another_client,
            "archive/team/app".into(),
            "another".into(),
            another_fence,
        )
        .unwrap();
        assert!(publication.metadata_store_for(another_writer).is_err());
    }

    #[test]
    fn configured_publication_rejects_targets_from_another_registry_authority() {
        let client = RegistryClient::new_configured("upload.test", false, None, None).unwrap();
        let publication = RegistrySnapshotPublication::new_configured(
            client,
            None,
            None,
            None,
            "snapshot-".into(),
            SnapshotDataUploadConfig::default(),
        );
        let foreign = RegistryClient::new_configured("foreign.test", false, None, None).unwrap();
        let foreign_publication = RegistrySnapshotPublication::new_configured(
            Arc::clone(&foreign),
            None,
            None,
            None,
            "snapshot-".into(),
            SnapshotDataUploadConfig::default(),
        );
        let foreign_targets = targets(&foreign_publication, foreign, "team/app");
        assert!(publication
            .writer_for_targets(&foreign_targets.0, &foreign_targets.1)
            .is_err());
    }

    #[tokio::test]
    async fn publication_puts_the_configured_snapshot_id_tag_and_keeps_an_immutable_root() {
        use std::collections::HashMap;
        use std::os::unix::fs::FileExt;

        use crate::snapshot::metadata::{
            LayerKind, StorageDescriptor, StorageProvider, VmmCompatibility,
        };
        use crate::snapshot::repository::frozen_layer::{
            create_temporary_zfile, FrozenLayerMetadata, FrozenLayerPin, PackedOwnershipMap,
        };
        use crate::snapshot::Snapshot;

        #[derive(Default)]
        struct Registry {
            blobs: HashMap<String, Vec<u8>>,
            uploads: HashMap<String, Vec<u8>>,
            next_upload: usize,
            manifests: HashMap<String, (Vec<u8>, String)>,
            manifest_puts: Vec<String>,
        }
        let state = Arc::new(std::sync::Mutex::new(Registry::default()));
        let observed = Arc::clone(&state);
        let socket = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        socket.set_nonblocking(true).unwrap();
        let authority = socket.local_addr().unwrap().to_string();
        let service = make_service_fn(move |_| {
            let state = Arc::clone(&state);
            async move {
                Ok::<_, Infallible>(service_fn(move |request: hyper::Request<Body>| {
                    let state = Arc::clone(&state);
                    async move {
                        let method = request.method().clone();
                        let path = request.uri().path().to_owned();
                        let query = request.uri().query().unwrap_or("").to_owned();
                        let range = request
                            .headers()
                            .get("range")
                            .map(|v| v.to_str().unwrap().to_owned());
                        let content_type = request
                            .headers()
                            .get("content-type")
                            .map(|v| v.to_str().unwrap().to_owned())
                            .unwrap_or_default();
                        let body = hyper::body::to_bytes(request.into_body()).await.unwrap();
                        let mut state = state.lock().unwrap();
                        let response = if method == Method::POST
                            && path == "/v2/test/blobs/uploads/"
                        {
                            let id = state.next_upload.to_string();
                            state.next_upload += 1;
                            state.uploads.insert(id.clone(), Vec::new());
                            Response::builder()
                                .status(202)
                                .header("location", format!("/v2/test/blobs/uploads/{id}"))
                                .body(Body::empty())
                                .unwrap()
                        } else if let Some(id) = path.strip_prefix("/v2/test/blobs/uploads/") {
                            state.uploads.get_mut(id).unwrap().extend_from_slice(&body);
                            if method == Method::PUT {
                                let digest = url::form_urlencoded::parse(query.as_bytes())
                                    .find(|(key, _)| key == "digest")
                                    .unwrap()
                                    .1
                                    .into_owned();
                                let payload = state.uploads.remove(id).unwrap();
                                assert_eq!(sha256_identity(&payload), digest);
                                state.blobs.insert(digest.clone(), payload);
                                Response::builder()
                                    .status(201)
                                    .header("docker-content-digest", digest)
                                    .body(Body::empty())
                                    .unwrap()
                            } else {
                                assert_eq!(method, Method::PATCH);
                                Response::builder()
                                    .status(202)
                                    .header("location", &path)
                                    .header("range", format!("0-{}", state.uploads[id].len() - 1))
                                    .body(Body::empty())
                                    .unwrap()
                            }
                        } else if let Some(digest) = path.strip_prefix("/v2/test/blobs/") {
                            match state.blobs.get(digest) {
                                None => {
                                    Response::builder().status(404).body(Body::empty()).unwrap()
                                }
                                Some(payload) if method == Method::HEAD => Response::builder()
                                    .status(200)
                                    .header("content-length", payload.len())
                                    .header("docker-content-digest", digest)
                                    .body(Body::empty())
                                    .unwrap(),
                                Some(payload) => {
                                    assert_eq!(method, Method::GET);
                                    let range = range.unwrap();
                                    let (start, end) = range
                                        .strip_prefix("bytes=")
                                        .unwrap()
                                        .split_once('-')
                                        .unwrap();
                                    let start: usize = start.parse().unwrap();
                                    let end: usize = end.parse().unwrap();
                                    Response::builder()
                                        .status(206)
                                        .header(
                                            "content-range",
                                            format!("bytes {start}-{end}/{}", payload.len()),
                                        )
                                        .header("content-length", end - start + 1)
                                        .header("docker-content-digest", digest)
                                        .body(Body::from(payload[start..=end].to_vec()))
                                        .unwrap()
                                }
                            }
                        } else if let Some(selector) = path.strip_prefix("/v2/test/manifests/") {
                            if method == Method::PUT {
                                state.manifest_puts.push(selector.to_owned());
                                state
                                    .manifests
                                    .insert(selector.to_owned(), (body.to_vec(), content_type));
                                Response::builder()
                                    .status(201)
                                    .header("docker-content-digest", sha256_identity(&body))
                                    .body(Body::empty())
                                    .unwrap()
                            } else {
                                match state.manifests.get(selector) {
                                    None => {
                                        Response::builder().status(404).body(Body::empty()).unwrap()
                                    }
                                    Some((payload, media_type)) => Response::builder()
                                        .status(200)
                                        .header("content-type", media_type)
                                        .header("content-length", payload.len())
                                        .header("docker-content-digest", sha256_identity(payload))
                                        .body(if method == Method::HEAD {
                                            Body::empty()
                                        } else {
                                            Body::from(payload.clone())
                                        })
                                        .unwrap(),
                                }
                            }
                        } else {
                            panic!("unexpected fixture request: {method} {path}");
                        };
                        Ok::<_, Infallible>(response)
                    }
                }))
            }
        });
        let server = tokio::spawn(async move {
            hyper::Server::from_tcp(socket)
                .unwrap()
                .serve(service)
                .await
                .unwrap();
        });
        let id = "019a9d6d-6543-7f00-a012-0123456789ab";
        let raw = create_temporary_zfile().unwrap();
        raw.set_len(65536).unwrap();
        raw.write_all_at(&vec![0x5a; 65536], 0).unwrap();
        let local = Snapshot::from_base_layer(
            id.into(),
            FrozenLayerPin::new(
                FrozenLayerMetadata {
                    kind: LayerKind::Block,
                    layer_id: "tag-mode-layer".into(),
                    provisional_content_id: sha256_identity(b"tag-mode-layer"),
                    logical_size: 65536,
                    alignment: 65536,
                },
                PackedOwnershipMap {
                    kind: LayerKind::Block,
                    snapshot_id: id.into(),
                    format_version: 1,
                    logical_size: 65536,
                    granularity: 65536,
                    packed_states: vec![1],
                },
                raw.into(),
                false,
            ),
            SnapshotProvenance {
                oci_ref: None,
                oci_digest: None,
            },
        )
        .unwrap();
        let plan = local
            .upload_plan(
                StorageDescriptor {
                    provider: StorageProvider::Registry,
                    endpoint_profile: "registry".into(),
                    credential_ref: "test:v1".into(),
                },
                VmmCompatibility {
                    architecture: "x86_64".into(),
                    vmm_type: "any".into(),
                    vmm_state_format_version: None,
                    rocklet_artifact: None,
                },
            )
            .unwrap();
        let client = RegistryClient::test_http_client(&format!("http://{authority}"));
        let original = RegistrySnapshotPublication::new_configured(
            Arc::clone(&client),
            None,
            None,
            None,
            "saved-".into(),
            SnapshotDataUploadConfig::default(),
        );
        for (mode, expected) in [
            (
                RegistrySnapshotTagMode::SourceSnapshot,
                format!("saved-{id}"),
            ),
            (RegistrySnapshotTagMode::SnapshotId, id.to_owned()),
        ] {
            let publication = Arc::clone(&original).with_tag_mode(mode);
            assert_eq!(original.tag_mode, RegistrySnapshotTagMode::SourceSnapshot);
            let (metadata_target, data_target) = targets(&publication, Arc::clone(&client), "test");
            let publisher = SnapshotPublisher::new(SnapshotDataUploadConfig::default(), None)
                .with_registry_publication(publication);
            let published = tokio::time::timeout(
                Duration::from_secs(10),
                publisher.publish(plan.clone(), metadata_target, data_target),
            )
            .await
            .expect("local Registry publication must be bounded")
            .unwrap();
            let root = published.spec.registry_snapshot.unwrap();
            assert_eq!(root.registry, authority);
            assert_eq!(root.repository, "test");
            let state = observed.lock().unwrap();
            assert_eq!(state.manifest_puts.last(), Some(&expected));
            let (tag_bytes, _) = &state.manifests[&expected];
            assert_eq!(sha256_identity(tag_bytes), root.index_digest);
            assert_eq!(state.manifests[&root.index_digest].0, *tag_bytes);
            drop(state);
            publisher.close().await;
        }
        server.abort();
    }

    #[tokio::test]
    async fn reused_configs_require_real_bytes_before_root_publication() {
        let payload = br#"{"architecture":"amd64","os":"linux"}"#;
        let digest = sha256_identity(payload);
        // A successful control ensures a HEAD hit itself is supported; the
        // other modes have identical trustworthy-looking HEAD responses.
        for mode in ["valid", "missing", "corrupt"] {
            let socket = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
            socket.set_nonblocking(true).unwrap();
            let authority = socket.local_addr().unwrap().to_string();
            let heads = Arc::new(AtomicUsize::new(0));
            let gets = Arc::new(AtomicUsize::new(0));
            let observed_heads = Arc::clone(&heads);
            let observed_gets = Arc::clone(&gets);
            let expected_digest = digest.clone();
            let service = make_service_fn(move |_| {
                let heads = Arc::clone(&observed_heads);
                let gets = Arc::clone(&observed_gets);
                let digest = expected_digest.clone();
                async move {
                    Ok::<_, Infallible>(service_fn(move |request: hyper::Request<Body>| {
                        assert!(request.uri().path().starts_with("/v2/test/blobs/sha256:"));
                        let response = if request.method() == Method::HEAD {
                            heads.fetch_add(1, Ordering::Relaxed);
                            Response::builder()
                                .status(200)
                                .header("content-length", payload.len())
                                .header("docker-content-digest", &digest)
                                .body(Body::empty())
                                .unwrap()
                        } else {
                            assert_eq!(request.method(), Method::GET);
                            gets.fetch_add(1, Ordering::Relaxed);
                            if mode == "missing" {
                                Response::builder().status(404).body(Body::empty()).unwrap()
                            } else {
                                let mut bytes = payload.to_vec();
                                if mode == "corrupt" {
                                    bytes[0] ^= 1;
                                }
                                Response::builder()
                                    .status(206)
                                    .header(
                                        "content-range",
                                        format!("bytes 0-{}/{}", payload.len() - 1, payload.len()),
                                    )
                                    .header("docker-content-digest", &digest)
                                    .body(Body::from(bytes))
                                    .unwrap()
                            }
                        };
                        async move { Ok::<_, Infallible>(response) }
                    }))
                }
            });
            let server = tokio::spawn(async move {
                hyper::Server::from_tcp(socket)
                    .unwrap()
                    .serve(service)
                    .await
                    .unwrap();
            });
            let client = RegistryClient::test_http_client(&format!("http://{authority}"));
            let fence = NamespaceMutationFence::new(format!("{authority}/test")).unwrap();
            let writer = RegistryWriteTarget::new(
                client,
                "test".into(),
                "config-test".into(),
                Arc::clone(&fence),
            )
            .unwrap();
            let lease = UploadTransactionLease::acquire(Arc::clone(&fence)).await;
            let (_shutdown, receiver) = watch::channel(false);
            let budget = RegistryCopyBudget {
                transfer_permits: Arc::new(Semaphore::new(1)),
                buffer_permits: Arc::new(Semaphore::new(1024)),
                chunk_bytes: 128,
                attempt_timeout: Duration::from_secs(2),
                cancellation: IoCancellation::new(receiver),
            };
            let store = RegistryGraphStore::new(
                writer,
                None,
                None,
                Vec::new(),
                Vec::new(),
                Some(lease.clone()),
                budget,
            )
            .unwrap();
            let target =
                SnapshotMetadataWriteTarget::new(store.clone(), "config-test".into(), fence)
                    .unwrap();
            let range_writer = super::super::range_io::RangeIO::for_fixed_targets();
            let result = ensure_registry_config(
                range_writer.as_ref(),
                &target,
                store.as_ref(),
                &format!("oci/configs/{}", digest.trim_start_matches("sha256:")),
                payload,
                &digest,
                &lease,
            )
            .await;
            assert_eq!(result.is_ok(), mode == "valid", "{mode}: {result:?}");
            assert!(heads.load(Ordering::Relaxed) > 0);
            assert_eq!(gets.load(Ordering::Relaxed), 1);
            server.abort();
        }
    }
}
