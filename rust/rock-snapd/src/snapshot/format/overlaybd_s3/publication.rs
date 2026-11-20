//! OverlayBD preparation and publication in the snapd S3 snapshot envelope.
//! Finite format hooks own canonical paths and validation; I/O owns scheduling.

use crate::snapshot::format::overlaybd_s3::capture::{
    chain_ref, layer_encoding, manifest_chain, FormatCaptureError,
};
use crate::snapshot::format::overlaybd_s3::data::{
    DataPackIdentity, DataPackObjectKey, DataPackSliceIdentity, DataPackUploadRequest,
};
use crate::snapshot::format::{ImmutableObject, LayerUploadPlan, SnapshotUploadPlan};
use crate::snapshot::format::{SnapshotUploadInterface, SnapshotUploadSource};
use crate::snapshot::io::publication::{
    SnapshotPublicationContext, SnapshotUploadError, UploadedSnapshot,
};
use crate::snapshot::metadata::{
    canonical_json_bytes, sha256_identity, DataSource, LayerKind, ObjectRef, SnapshotKind,
    SnapshotManifest, SnapshotSpec, StorageDescriptor, VmStateRef, VmmCompatibility,
    FORMAT_VERSION_V1,
};
use crate::snapshot::transport::{
    DataPackSource, DataPackSourceError, SealedObject, StagedObjectWriter,
};
use crate::snapshot::ValidateCanonicalModel;
use serde_json::json;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

impl From<FormatCaptureError> for SnapshotUploadError {
    fn from(error: FormatCaptureError) -> Self {
        match error {
            FormatCaptureError::SourceIntegrity(failure) => Self::SourceIntegrity(failure),
            FormatCaptureError::Io(message) => Self::Data {
                message,
                retryable: true,
            },
            error => Self::InvalidPlan(error.to_string()),
        }
    }
}
#[derive(Debug)]
pub struct OverlaybdS3Publication;
#[async_trait::async_trait]
impl SnapshotUploadInterface for OverlaybdS3Publication {
    fn prepare_upload(
        &self,
        source: &dyn SnapshotUploadSource,
        storage: StorageDescriptor,
        compatibility: VmmCompatibility,
        scratch: &dyn Fn(u64) -> Result<Arc<dyn StagedObjectWriter>, SnapshotUploadError>,
    ) -> Result<SnapshotUploadPlan, SnapshotUploadError> {
        let registry_sources = source.registry_sources();
        let source = source.prepare_upload_source(scratch)?;
        let mut layer_plans = Vec::new();
        for kind in [LayerKind::Memory, LayerKind::Block] {
            let Some(layer) = source.canonical.layers.get(&kind) else {
                continue;
            };
            let chain = source.canonical.chain_indexes.get(&kind).ok_or_else(|| {
                SnapshotUploadError::InvalidPlan("Canonical layer has no chain index".into())
            })?;
            let head = chain.index.layers.last().ok_or_else(|| {
                SnapshotUploadError::InvalidPlan("Canonical layer chain is empty".into())
            })?;
            // Every layer owns one complete standard blob, including layers
            // without DATA. Its standard metadata still needs publication.
            let object = layer.blob.sealed_object()?.ok_or_else(|| {
                SnapshotUploadError::InvalidPlan("Sealed upload source is closed".into())
            })?;
            let source: Arc<dyn DataPackSource> = Arc::new(OverlaybdBlobSource {
                object,
                length: layer.blob.length,
            });
            let object_key = DataPackObjectKey::new(layer.blob.object_key.clone())
                .map_err(|error| SnapshotUploadError::InvalidPlan(error.to_string()))?;
            let mut slices = layer
                .range_map
                .ranges
                .iter()
                .filter_map(|range| range.source.as_ref())
                .map(|source| DataPackSliceIdentity {
                    offset: source.pack_offset,
                    length: source.encoded_length,
                    expected_sha256: source.checksum_sha256.clone(),
                })
                .collect::<Vec<_>>();
            // Authenticate the actual standard metadata in the byte source,
            // not only the descriptor's mirror and DATA. Otherwise a caller
            // could bind a different malformed blob with matching DATA units.
            for (offset, payload) in [
                (0, &layer.blob.plan.header),
                (layer.blob.plan.index_offset, &layer.blob.plan.index),
                (
                    layer.blob.plan.stored_bytes - layer.blob.plan.trailer.len() as u64,
                    &layer.blob.plan.trailer,
                ),
            ] {
                if !payload.is_empty() {
                    slices.push(DataPackSliceIdentity {
                        offset,
                        length: payload.len() as u64,
                        expected_sha256: sha256_identity(payload),
                    });
                }
            }
            slices.sort_by_key(|slice| slice.offset);
            let data_requests = vec![DataPackUploadRequest {
                identity: DataPackIdentity {
                    object_key,
                    expected_sha256: layer.blob.sha256.clone(),
                    length: layer.blob.length,
                },
                source,
                slices,
                zfile: layer.range_map.zfile.clone(),
            }];
            let range_map = ImmutableObject::new(
                head.range_map.object_key.clone(),
                layer.range_map_bytes.clone(),
            )?;
            let prefix = head
                .range_map
                .object_key
                .strip_suffix(layer_encoding(layer).descriptor_name())
                .ok_or_else(|| {
                    SnapshotUploadError::InvalidPlan("Layer index path is not canonical".into())
                })?;
            let committed = ImmutableObject::new(
                format!("{prefix}{}", layer_encoding(layer).committed_name()),
                canonical_json_bytes(&json!({
                    "content_sha256": layer.range_map.content_sha256,
                    "format_version": layer.range_map.format_version,
                    "range_map_sha256": layer.range_map_sha256,
                    "kind": layer.range_map.kind,
                }))
                .map_err(|error| SnapshotUploadError::InvalidPlan(error.to_string()))?,
            )?;
            layer_plans.push(LayerUploadPlan::from_requests(
                kind,
                layer.range_map.format_version,
                layer.range_map.content_sha256.clone(),
                data_requests,
                range_map,
                committed,
            )?);
        }

        let mut chain_objects = Vec::new();
        for kind in [LayerKind::Memory, LayerKind::Block] {
            if source.canonical.layers.contains_key(&kind) {
                let chain = source.canonical.chain_indexes.get(&kind).ok_or_else(|| {
                    SnapshotUploadError::InvalidPlan("Canonical layer has no chain index".into())
                })?;
                chain_objects.push(ImmutableObject::new(
                    chain.object_key.clone(),
                    chain.payload.clone(),
                )?);
            }
        }
        let vm_object = source
            .vm_state
            .as_ref()
            .map(|state| ImmutableObject::new(state.object_key.clone(), state.payload.clone()))
            .transpose()?;
        let memory = source
            .canonical
            .layers
            .contains_key(&LayerKind::Memory)
            .then(|| source.canonical.chain_indexes.get(&LayerKind::Memory))
            .flatten();
        let rootfs = source
            .canonical
            .chain_indexes
            .get(&LayerKind::Block)
            .ok_or_else(|| {
                SnapshotUploadError::InvalidPlan("Canonical rootfs chain is absent".into())
            })?;
        let vm_ref = source.vm_state.as_ref().map(|state| VmStateRef {
            object_key: state.object_key.clone(),
            sha256: state.sha256.clone(),
            object_version: None,
            stored_bytes: state.payload.len() as u64,
        });
        let memory_manifest = memory.map(manifest_chain);
        let rootfs_manifest = manifest_chain(rootfs);
        let manifest = SnapshotManifest {
            format_version: FORMAT_VERSION_V1,
            snapshot_id: source.snapshot_id.clone(),
            snapshot_kind: source.snapshot_kind,
            vm_state: vm_ref.clone(),
            memory: memory_manifest,
            rootfs: rootfs_manifest,
        };
        manifest
            .validate()
            .map_err(SnapshotUploadError::InvalidPlan)?;
        let manifest_payload = canonical_json_bytes(&manifest)
            .map_err(|error| SnapshotUploadError::InvalidPlan(error.to_string()))?;
        let manifest_key = format!("snapshots/{}/manifest.json", source.snapshot_id);
        let manifest_object = ImmutableObject::new(manifest_key.clone(), manifest_payload)?;
        let mut compatibility = compatibility;
        compatibility.rocklet_artifact = match source.snapshot_kind {
            SnapshotKind::Full => Some(source.rocklet_artifact.clone().ok_or_else(|| {
                SnapshotUploadError::InvalidPlan(
                    "FULL snapshot is missing its captured Rocklet artifact pin".into(),
                )
            })?),
            SnapshotKind::BaseImage => {
                if source.rocklet_artifact.is_some() {
                    return Err(SnapshotUploadError::InvalidPlan(
                        "BASE_IMAGE snapshot cannot pin a runtime Rocklet artifact".into(),
                    ));
                }
                None
            }
        };
        let spec = SnapshotSpec {
            registry_snapshot: None,
            format_version: FORMAT_VERSION_V1,
            compatibility,
            provenance: source.provenance.clone(),
            storage,
            manifest: ObjectRef {
                object_key: manifest_key,
                sha256: manifest_object.sha256.clone(),
                object_version: None,
            },
            vm_state: vm_ref,
            memory: memory.map(chain_ref),
            rootfs: chain_ref(rootfs),
        };
        spec.validate_for_preparation()
            .map_err(SnapshotUploadError::InvalidPlan)?;
        Ok(SnapshotUploadPlan {
            registry_sources,
            snapshot_id: source.snapshot_id.clone(),
            snapshot_kind: source.snapshot_kind,
            spec,
            layers: layer_plans,
            chain_indexes: chain_objects,
            vm_state: vm_object,
            manifest: manifest_object,
        })
    }
    fn validate_prepared(&self, plan: &SnapshotUploadPlan) -> Result<(), SnapshotUploadError> {
        if plan.spec.storage.provider == crate::snapshot::metadata::StorageProvider::Registry {
            return Err(SnapshotUploadError::InvalidPlan(
                "Registry base metadata belongs in the catalog, not the object store".into(),
            ));
        }
        plan.validate_shallow()
    }
    async fn validate_uploaded_artifacts(
        &self,
        plan: &SnapshotUploadPlan,
        receipts: &[crate::snapshot::transport::DataPackUploadReceipt],
        context: &SnapshotPublicationContext<'_>,
    ) -> Result<SnapshotManifest, SnapshotUploadError> {
        crate::snapshot::format::overlaybd_s3::validation::PublicationValidator {
            source_resolver: context.resolver,
            range_io: context.range_io,
        }
        .validate_uploaded_artifacts(plan, receipts, context.target, context.lease)
        .await
    }

    fn root_metadata<'a>(
        &self,
        plan: &'a SnapshotUploadPlan,
    ) -> Result<&'a ImmutableObject, SnapshotUploadError> {
        plan.manifest.validate()?;
        Ok(&plan.manifest)
    }
    fn validate_published(&self, published: &UploadedSnapshot) -> Result<(), SnapshotUploadError> {
        published
            .spec
            .validate()
            .map_err(SnapshotUploadError::InvalidPlan)?;
        published
            .manifest
            .validate()
            .map_err(SnapshotUploadError::InvalidPlan)?;
        let payload = canonical_json_bytes(&published.manifest)
            .map_err(|e| SnapshotUploadError::InvalidPlan(e.to_string()))?;
        if published.spec.manifest.sha256 != sha256_identity(&payload)
            || published.spec.vm_state != published.manifest.vm_state
        {
            return Err(SnapshotUploadError::IdentityConflict(
                "Published root metadata differs from its authenticated spec".into(),
            ));
        }
        Ok(())
    }
}

/// Uploads replay transport-owned physical bytes without decoding or depending
/// on the native memory/block source, including the uncompressed LSMT format.
struct OverlaybdBlobSource {
    object: Arc<dyn SealedObject>,
    length: u64,
}

impl DataPackSource for OverlaybdBlobSource {
    fn length(&self) -> u64 {
        self.length
    }

    fn mark_remote_committed(&self) {
        self.object.mark_remote_committed();
    }

    fn read_at<'a>(
        &'a self,
        offset: u64,
        max_bytes: usize,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<u8>, DataPackSourceError>> + Send + 'a>> {
        Box::pin(async move {
            if offset > self.length || max_bytes > 64 * 1024 * 1024 {
                return Err(DataPackSourceError::InvalidRange);
            }
            self.object
                .read_at(offset, max_bytes)
                .await
                .map_err(|error| DataPackSourceError::Io(error.to_string()))
        })
    }
}

pub(crate) fn validate_pack_path(
    source: &DataSource,
    prefix: &str,
) -> Result<(), SnapshotUploadError> {
    let content = source
        .object_sha256
        .strip_prefix("sha256:")
        .ok_or_else(|| SnapshotUploadError::InvalidPlan("invalid DATA object hash".into()))?;
    if source.object_key != format!("{prefix}data/{content}.pack")
        && source.object_key != format!("{prefix}data/{content}.commit")
    {
        return Err(SnapshotUploadError::InvalidPlan(
            "DATA pack is outside its content-addressed layer prefix".into(),
        ));
    }
    Ok(())
}
