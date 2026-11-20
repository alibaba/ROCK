//! Format-owned verification of finite uploaded artifact graphs before root
//! publication. Core controls admission/cancellation/retries; transport performs
//! physical I/O. No worker, scheduler or lifecycle state is started here.
use crate::snapshot::format::decode_layer;
use crate::snapshot::metadata::{
    canonical_json_bytes, parse_canonical_model, sha256_identity, ChainIndexRef, LayerChainIndex,
    LayerKind, ManifestChainRef, RangeKind, SnapshotManifest, MAX_CHAIN_DEPTH,
    MAX_RANGES_PER_LAYER, MAX_RANGE_MAP_BYTES,
};
use crate::snapshot::{SourceError, SourceResolver};

use crate::snapshot::format::overlaybd_s3::publication::validate_pack_path;
use crate::snapshot::format::{ImmutableObject, SnapshotUploadPlan};
use crate::snapshot::io::publication::SnapshotUploadError;
use crate::snapshot::transport::DataPackUploadReceipt;
use crate::snapshot::transport::{
    ObjectStoreError, RemoteObjectMetadata, SnapshotMetadataWriteTarget, UploadTransactionLease,
};
use serde_json::json;
use std::collections::{HashMap, HashSet};
const MAX_MANIFEST_BYTES: usize = 1024 * 1024;
const MAX_CHAIN_INDEX_BYTES: usize = 8 * 1024 * 1024;
const MAX_VM_STATE_BYTES: usize = 64 * 1024 * 1024;
#[derive(Debug, Clone)]
struct VerifiedObject {
    payload: Vec<u8>,
    sha256: String,
    version: Option<String>,
}

struct OfflineValidationContext<'a, 'b> {
    target: &'a SnapshotMetadataWriteTarget,
    lease: &'a UploadTransactionLease,
    overlay: &'a HashMap<String, ImmutableObject>,
    cache: &'a mut HashMap<String, VerifiedObject>,
    data_receipts: &'a HashMap<String, &'b DataPackUploadReceipt>,
    current_layer_indexes: &'a HashSet<String>,
    inherited_data: &'a mut HashMap<String, RemoteObjectMetadata>,
}

pub(crate) struct PublicationValidator<'a> {
    pub source_resolver: &'a dyn SourceResolver,
    pub range_io: &'a dyn crate::snapshot::io::range_io::RangeWriter,
}
impl PublicationValidator<'_> {
    pub(crate) async fn validate_uploaded_artifacts(
        &self,
        plan: &SnapshotUploadPlan,
        data_receipts: &[DataPackUploadReceipt],
        target: &SnapshotMetadataWriteTarget,
        lease: &UploadTransactionLease,
    ) -> Result<SnapshotManifest, SnapshotUploadError> {
        // Every object except the manifest must be read back from the store.
        // This detects conditional-write collisions and ensures a joined layer
        // single-flight cannot validate against its caller's unpublished bytes.
        let overlay = HashMap::from([(plan.manifest.object_key.clone(), plan.manifest.clone())]);
        let manifest: SnapshotManifest =
            parse_canonical_model(&plan.manifest.payload, "snapshot manifest")
                .map_err(|error| SnapshotUploadError::InvalidPlan(error.to_string()))?;
        validate_top_level(plan, &manifest)?;
        let receipts = validate_data_receipts(plan, data_receipts)?;
        let mut cache = HashMap::new();
        let current_layer_indexes = plan
            .layers
            .iter()
            .map(|layer| layer.range_map.object_key.clone())
            .collect::<HashSet<_>>();
        let mut inherited_data = HashMap::new();
        let mut validation = OfflineValidationContext {
            target,
            lease,
            overlay: &overlay,
            cache: &mut cache,
            data_receipts: &receipts,
            current_layer_indexes: &current_layer_indexes,
            inherited_data: &mut inherited_data,
        };
        let mut referenced_layers = self
            .validate_chain(&plan.spec.rootfs, LayerKind::Block, &mut validation)
            .await?;
        if let Some(memory) = &plan.spec.memory {
            referenced_layers.extend(
                self.validate_chain(memory, LayerKind::Memory, &mut validation)
                    .await?,
            );
        }
        if plan
            .layers
            .iter()
            .any(|layer| !referenced_layers.contains(layer.range_map.object_key.as_str()))
        {
            return Err(SnapshotUploadError::InvalidPlan(
                "upload plan contains a layer absent from snapshot chains".into(),
            ));
        }
        if let Some(vm_state) = &plan.spec.vm_state {
            let payload = self
                .read_verified(
                    &vm_state.object_key,
                    &vm_state.sha256,
                    vm_state.object_version.as_deref(),
                    MAX_VM_STATE_BYTES,
                    validation.target,
                    validation.lease,
                    validation.overlay,
                    validation.cache,
                )
                .await?;
            if payload.len() as u64 != vm_state.stored_bytes {
                return Err(SnapshotUploadError::InvalidPlan(
                    "VM state size does not match snapshot spec".into(),
                ));
            }
        }
        Ok(manifest)
    }

    async fn validate_chain(
        &self,
        reference: &ChainIndexRef,
        expected_kind: LayerKind,
        validation: &mut OfflineValidationContext<'_, '_>,
    ) -> Result<HashSet<String>, SnapshotUploadError> {
        let payload = self
            .read_verified(
                &reference.index_object_key,
                &reference.index_sha256,
                reference.object_version.as_deref(),
                MAX_CHAIN_INDEX_BYTES,
                validation.target,
                validation.lease,
                validation.overlay,
                validation.cache,
            )
            .await?;
        let chain: LayerChainIndex = parse_canonical_model(&payload, "chain index")
            .map_err(|error| SnapshotUploadError::InvalidPlan(error.to_string()))?;
        if chain.kind != expected_kind
            || chain.logical_bytes != reference.logical_bytes
            || chain.layers.len() > MAX_CHAIN_DEPTH
        {
            return Err(SnapshotUploadError::InvalidPlan(
                "chain kind, size, or depth conflicts with snapshot spec".into(),
            ));
        }
        let mut referenced_layers = HashSet::new();
        for (ordinal, layer_ref) in chain.layers.iter().enumerate() {
            referenced_layers.insert(layer_ref.range_map.object_key.clone());
            let is_current_layer = validation
                .current_layer_indexes
                .contains(&layer_ref.range_map.object_key);
            let external_source = layer_ref.range_map.source_ref.as_ref();
            let payload = if let Some(source_ref) = external_source {
                if expected_kind != LayerKind::Block || ordinal != 0 || is_current_layer {
                    return Err(SnapshotUploadError::InvalidPlan(
                        "Registry references are supported only as an inherited rootfs base".into(),
                    ));
                }
                let resolved = self
                    .source_resolver
                    .resolve_source(source_ref)
                    .await
                    .map_err(source_upload_error)?;
                if resolved.descriptor.len() > MAX_RANGE_MAP_BYTES {
                    return Err(SnapshotUploadError::InvalidPlan(
                        "Registry descriptor exceeds metadata bound".into(),
                    ));
                }
                if resolved.descriptor_sha256 != layer_ref.range_map.sha256 {
                    return Err(SnapshotUploadError::IdentityConflict(
                        "Registry descriptor differs from its inherited layer reference".into(),
                    ));
                }
                resolved.descriptor.clone()
            } else {
                self.read_verified(
                    &layer_ref.range_map.object_key,
                    &layer_ref.range_map.sha256,
                    layer_ref.range_map.object_version.as_deref(),
                    MAX_RANGE_MAP_BYTES,
                    validation.target,
                    validation.lease,
                    validation.overlay,
                    validation.cache,
                )
                .await?
            };
            let decoded = decode_layer(layer_ref.range_map.encoding, &payload)
                .map_err(|error| SnapshotUploadError::InvalidPlan(error.to_string()))?;
            let layer = decoded.range_map;
            let overlaybd_blob_info = decoded.blob;
            if layer.kind != chain.kind
                || layer.format_version != layer_ref.format_version
                || layer.logical_bytes != chain.logical_bytes
                || layer.unit_size_bytes != chain.unit_size_bytes
                || layer.content_sha256 != layer_ref.content_sha256
                || layer.ranges.len() > MAX_RANGES_PER_LAYER
            {
                return Err(SnapshotUploadError::InvalidPlan(
                    "layer identity or geometry conflicts with its chain".into(),
                ));
            }
            if ordinal == 0
                && layer
                    .ranges
                    .iter()
                    .any(|range| range.kind == RangeKind::Inherit)
            {
                return Err(SnapshotUploadError::InvalidPlan(
                    "root layer cannot contain INHERIT ranges".into(),
                ));
            }
            let content = layer
                .content_sha256
                .strip_prefix("sha256:")
                .expect("validated content hash");
            let prefix = format!(
                "layers/{}/{}/{content}/",
                layer.kind.object_key_component(),
                layer.format_version
            );
            let overlaybd_blob = if let Some((blob, length)) = overlaybd_blob_info {
                let encoding = layer_ref
                    .range_map
                    .encoding
                    .as_ref()
                    .expect("OverlayBD encoding");
                if layer_ref.range_map.object_key
                    != format!("{prefix}{}", encoding.descriptor_name())
                {
                    return Err(SnapshotUploadError::InvalidPlan(
                        "OverlayBD descriptor is outside its canonical layer prefix".into(),
                    ));
                }
                let hash = blob.sha256.strip_prefix("sha256:").ok_or_else(|| {
                    SnapshotUploadError::InvalidPlan("invalid OverlayBD blob hash".into())
                })?;
                if blob.object_key != format!("{prefix}data/{hash}.commit") {
                    return Err(SnapshotUploadError::InvalidPlan(
                        "OverlayBD blob is outside its canonical layer prefix".into(),
                    ));
                }
                if is_current_layer {
                    if !validation
                        .data_receipts
                        .get(&blob.object_key)
                        .is_some_and(|receipt| {
                            receipt.sha256 == blob.sha256
                                && receipt.length == length
                                && blob.object_version.as_ref().is_none_or(|version| {
                                    receipt.object_version.as_ref() == Some(version)
                                })
                        })
                    {
                        return Err(SnapshotUploadError::InvalidPlan(
                            "OverlayBD blob is not bound to its complete verified receipt".into(),
                        ));
                    }
                } else {
                    let metadata = self
                        .inherited_data_metadata(&blob.object_key, validation)
                        .await?;
                    if metadata.sha256 != blob.sha256
                        || metadata.size != length
                        || blob
                            .object_version
                            .as_ref()
                            .is_some_and(|version| metadata.version.as_ref() != Some(version))
                    {
                        return Err(SnapshotUploadError::IdentityConflict(
                            "inherited OverlayBD blob conflicts with its descriptor".into(),
                        ));
                    }
                }
                Some((blob, length))
            } else {
                None
            };
            let committed_payload = canonical_json_bytes(&json!({
                "content_sha256": layer.content_sha256,
                "format_version": layer.format_version,
                "range_map_sha256": layer_ref.range_map.sha256,
                "kind": layer.kind,
            }))
            .map_err(|error| SnapshotUploadError::InvalidPlan(error.to_string()))?;
            let marker =
                crate::snapshot::metadata::required_layer_encoding(layer_ref.range_map.encoding)
                    .map_err(SnapshotUploadError::InvalidPlan)?
                    .committed_name();
            let committed_key = format!("{prefix}{marker}");
            if external_source.is_none() {
                self.read_verified(
                    &committed_key,
                    &sha256_identity(&committed_payload),
                    None,
                    MAX_MANIFEST_BYTES,
                    validation.target,
                    validation.lease,
                    validation.overlay,
                    validation.cache,
                )
                .await?;
            } else if layer_ref.range_map.object_key != format!("{prefix}registry-overlaybd.json") {
                return Err(SnapshotUploadError::IdentityConflict(
                    "Registry source is outside its canonical descriptor prefix".into(),
                ));
            }
            if let Some(registry) = &layer.registry {
                if expected_kind != LayerKind::Block || ordinal != 0 {
                    return Err(SnapshotUploadError::InvalidPlan(
                        "Registry sources are supported only as a rootfs base".into(),
                    ));
                }
                registry
                    .validate_map(&layer)
                    .map_err(SnapshotUploadError::InvalidPlan)?;
                // The source was rebuilt from its immutable pin, or its
                // stored descriptor and commit fence were read back above.
                // Registry DATA has no S3 upload receipt or HEAD.
                continue;
            }
            if overlaybd_blob.is_none() {
                return Err(SnapshotUploadError::InvalidPlan(
                    "persistent layer requires an OverlayBD blob descriptor".into(),
                ));
            }
            for range in &layer.ranges {
                if let Some(source) = &range.source {
                    validate_pack_path(source, &prefix)?;
                    if !source.object_key.ends_with(".commit")
                        || overlaybd_blob.as_ref().is_some_and(|(blob, _)| {
                            source.object_key != blob.object_key
                                || source.object_sha256 != blob.sha256
                                || source.object_version != blob.object_version
                        })
                    {
                        return Err(SnapshotUploadError::InvalidPlan(
                            "DATA source encoding conflicts with its layer descriptor".into(),
                        ));
                    }
                    let pack_end = source
                        .pack_offset
                        .checked_add(source.encoded_length)
                        .ok_or_else(|| {
                            SnapshotUploadError::InvalidPlan("DATA pack range overflows".into())
                        })?;
                    if overlaybd_blob.as_ref().is_some_and(|(_, length)| {
                        pack_end
                            > layer
                                .zfile
                                .as_ref()
                                .map_or(*length, |index| index.raw_bytes)
                    }) {
                        return Err(SnapshotUploadError::InvalidPlan(
                            "DATA range exceeds its OverlayBD blob".into(),
                        ));
                    }
                    if is_current_layer {
                        if !validation
                            .data_receipts
                            .get(&source.object_key)
                            .is_some_and(|receipt| {
                                receipt.sha256 == source.object_sha256
                                    && pack_end
                                        <= layer
                                            .zfile
                                            .as_ref()
                                            .map_or(receipt.length, |index| index.raw_bytes)
                                    && source.object_version.as_ref().is_none_or(|version| {
                                        receipt.object_version.as_ref() == Some(version)
                                    })
                            })
                        {
                            return Err(SnapshotUploadError::InvalidPlan(
                                "current DATA range is not bound to a verified data-plane receipt"
                                    .into(),
                            ));
                        }
                    } else {
                        let metadata = self
                            .inherited_data_metadata(&source.object_key, validation)
                            .await?;
                        if metadata.sha256 != source.object_sha256
                            || pack_end
                                > layer
                                    .zfile
                                    .as_ref()
                                    .map_or(metadata.size, |index| index.raw_bytes)
                            || source
                                .object_version
                                .as_ref()
                                .is_some_and(|version| metadata.version.as_ref() != Some(version))
                        {
                            return Err(SnapshotUploadError::IdentityConflict(
                                "inherited DATA object conflicts with its layer reference".into(),
                            ));
                        }
                    }
                }
            }
        }
        Ok(referenced_layers)
    }

    async fn inherited_data_metadata(
        &self,
        object_key: &str,
        validation: &mut OfflineValidationContext<'_, '_>,
    ) -> Result<RemoteObjectMetadata, SnapshotUploadError> {
        if let Some(metadata) = validation.inherited_data.get(object_key) {
            return Ok(metadata.clone());
        }
        let metadata = self
            .range_io
            .head_write_target(validation.target, object_key, validation.lease)
            .await?
            .ok_or_else(|| {
                SnapshotUploadError::InvalidPlan("inherited DATA object is absent".into())
            })?;
        validation
            .inherited_data
            .insert(object_key.to_owned(), metadata.clone());
        Ok(metadata)
    }

    #[allow(clippy::too_many_arguments)]
    async fn read_verified(
        &self,
        object_key: &str,
        expected_sha256: &str,
        expected_version: Option<&str>,
        max_bytes: usize,
        target: &SnapshotMetadataWriteTarget,
        lease: &UploadTransactionLease,
        overlay: &HashMap<String, ImmutableObject>,
        cache: &mut HashMap<String, VerifiedObject>,
    ) -> Result<Vec<u8>, SnapshotUploadError> {
        if let Some(verified) = cache.get(object_key) {
            if verified.sha256 != expected_sha256
                || expected_version
                    .is_some_and(|version| verified.version.as_deref() != Some(version))
            {
                return Err(SnapshotUploadError::IdentityConflict(format!(
                    "object was referenced with conflicting identity for {object_key:?}"
                )));
            }
            return Ok(verified.payload.clone());
        }
        let (payload, version) = if let Some(object) = overlay.get(object_key) {
            if object.sha256 != expected_sha256 {
                return Err(SnapshotUploadError::InvalidPlan(format!(
                    "object checksum reference conflicts for {object_key:?}"
                )));
            }
            if expected_version.is_some() {
                return Err(SnapshotUploadError::InvalidPlan(format!(
                    "unpublished overlay object cannot satisfy versioned reference for {object_key:?}"
                )));
            }
            (object.payload.clone(), None)
        } else {
            let metadata = self
                .range_io
                .head_write_target(target, object_key, lease)
                .await?
                .ok_or_else(|| {
                    SnapshotUploadError::InvalidPlan(format!(
                        "required object {object_key:?} is absent"
                    ))
                })?;
            if metadata.sha256 != expected_sha256
                || expected_version
                    .is_some_and(|version| metadata.version.as_deref() != Some(version))
            {
                return Err(SnapshotUploadError::IdentityConflict(format!(
                    "remote object identity conflicts for {object_key:?}"
                )));
            }
            if metadata.size > max_bytes as u64 {
                return Err(SnapshotUploadError::InvalidPlan(format!(
                    "object {object_key:?} exceeds configured bound"
                )));
            }
            let payload = self
                .range_io
                .read_write_target(target, object_key, lease)
                .await?;
            if payload.len() as u64 != metadata.size || sha256_identity(&payload) != expected_sha256
            {
                return Err(SnapshotUploadError::IdentityConflict(format!(
                    "remote object payload conflicts for {object_key:?}"
                )));
            }
            (payload, metadata.version)
        };
        if payload.len() > max_bytes {
            return Err(SnapshotUploadError::InvalidPlan(format!(
                "object {object_key:?} exceeds configured bound"
            )));
        }
        cache.insert(
            object_key.to_owned(),
            VerifiedObject {
                payload: payload.clone(),
                sha256: expected_sha256.to_owned(),
                version,
            },
        );
        Ok(payload)
    }
}
fn validate_data_receipts<'a>(
    plan: &SnapshotUploadPlan,
    receipts: &'a [DataPackUploadReceipt],
) -> Result<HashMap<String, &'a DataPackUploadReceipt>, SnapshotUploadError> {
    let expected = plan
        .layers
        .iter()
        .flat_map(|layer| layer.data_requests())
        .map(|request| {
            (
                request.identity.object_key.as_str().to_owned(),
                (
                    request.identity.expected_sha256.as_str(),
                    request.identity.length,
                ),
            )
        })
        .collect::<HashMap<_, _>>();
    let mut verified = HashMap::with_capacity(receipts.len());
    for receipt in receipts {
        let Some((sha256, length)) = expected.get(&receipt.object_key) else {
            return Err(SnapshotUploadError::InvalidPlan(
                "data plane returned an unrequested DATA receipt".into(),
            ));
        };
        if receipt.sha256 != *sha256
            || receipt.length != *length
            || verified
                .insert(receipt.object_key.clone(), receipt)
                .is_some()
        {
            return Err(SnapshotUploadError::IdentityConflict(
                "data-plane receipt identity conflicts with the upload plan".into(),
            ));
        }
    }
    if verified.len() != expected.len() {
        return Err(SnapshotUploadError::InvalidPlan(
            "data-plane receipt set is incomplete".into(),
        ));
    }
    Ok(verified)
}

fn validate_top_level(
    plan: &SnapshotUploadPlan,
    manifest: &SnapshotManifest,
) -> Result<(), SnapshotUploadError> {
    if manifest.snapshot_id != plan.snapshot_id || manifest.snapshot_kind != plan.snapshot_kind {
        return Err(SnapshotUploadError::InvalidPlan(
            "manifest identity conflicts with upload request".into(),
        ));
    }
    let prefix = format!("snapshots/{}/", plan.snapshot_id);
    if plan.spec.manifest.object_key != format!("{prefix}manifest.json")
        || plan.spec.rootfs.index_object_key != format!("{prefix}rootfs.index")
        || plan
            .spec
            .memory
            .as_ref()
            .is_some_and(|memory| memory.index_object_key != format!("{prefix}memory.index"))
        || plan
            .spec
            .vm_state
            .as_ref()
            .is_some_and(|state| state.object_key != format!("{prefix}vm-state.pack"))
    {
        return Err(SnapshotUploadError::InvalidPlan(
            "snapshot spec contains a non-canonical top-level object path".into(),
        ));
    }
    if manifest.rootfs.object_key != plan.spec.rootfs.index_object_key
        || manifest.rootfs.sha256 != plan.spec.rootfs.index_sha256
        || manifest.rootfs.logical_bytes != plan.spec.rootfs.logical_bytes
        || !same_optional_chain(manifest.memory.as_ref(), plan.spec.memory.as_ref())
        || manifest.vm_state != plan.spec.vm_state
    {
        return Err(SnapshotUploadError::InvalidPlan(
            "manifest references conflict with snapshot spec".into(),
        ));
    }
    Ok(())
}

fn same_optional_chain(manifest: Option<&ManifestChainRef>, spec: Option<&ChainIndexRef>) -> bool {
    match (manifest, spec) {
        (None, None) => true,
        (Some(manifest), Some(spec)) => {
            manifest.object_key == spec.index_object_key
                && manifest.sha256 == spec.index_sha256
                && manifest.logical_bytes == spec.logical_bytes
                && manifest.object_version == spec.object_version
        }
        _ => false,
    }
}

fn source_upload_error(error: SourceError) -> SnapshotUploadError {
    if error.is_integrity() {
        SnapshotUploadError::IdentityConflict(error.to_string())
    } else if error.retryable() {
        SnapshotUploadError::Store(ObjectStoreError::new(
            crate::snapshot::transport::PublishFailureKind::Retryable,
            error.to_string(),
        ))
    } else {
        SnapshotUploadError::InvalidPlan(error.to_string())
    }
}
