//! Snapd-published snapshot envelopes with ordered S3-compatible object metadata.
//!
//! The profile owns snapshot paths, manifest/chain binding and strict DATA
//! validation policy. Layer codecs remain shared; byte delivery and budgets
//! are supplied by the caller's metadata read context.

use std::collections::HashMap;
use std::sync::Arc;

use serde_json::json;

use crate::snapshot::format::overlaybd::MAX_BLOB_BYTES;
use crate::snapshot::format::{
    decode_layer, validate_snapshot_id, validate_snapshot_paths, DecodedSnapshotMetadata,
    LayerFormatError, SnapshotMetadataFormat,
};
use crate::snapshot::io::snapshot_io::{
    SnapshotLoadError, SnapshotMetadataReadContext, VerifiedMetadata,
};
use crate::snapshot::metadata::{
    canonical_json_bytes, parse_canonical_model, sha256_identity, ChainIndexRef, DataSource,
    LayerChainIndex, LayerKind, LayerRangeMap, LayerRef, ManifestChainRef, ObjectRef, RangeKind,
    SnapshotChainView, SnapshotKind, SnapshotLayerView, SnapshotManifest, SnapshotSpec,
    StorageDescriptor, StorageProvider, MAX_CHAIN_DEPTH, MAX_RANGES_PER_LAYER,
};
use crate::snapshot::transport::{BlobRef, ObjectStoreError};
use crate::snapshot::SourceError;

#[derive(Debug, Clone, Copy, Default)]
pub struct OverlaybdS3Metadata;

/// Physical blobs in authenticated root-to-leaf order. External native disk
/// bases expand their original layers; zero-only layers still have a blob.
#[derive(Debug, Default)]
pub(crate) struct SnapshotBlobChains {
    pub rootfs: Vec<BlobRef>,
    pub memory: Vec<BlobRef>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct DataObjectIdentity {
    sha256: String,
    version: Option<String>,
    required_end: u64,
    pub(crate) exact_size: Option<u64>,
    max_bytes: u64,
}

#[async_trait::async_trait]
impl SnapshotMetadataFormat for OverlaybdS3Metadata {
    fn validate_spec_layout(
        &self,
        spec: &SnapshotSpec,
        snapshot_id: &str,
        expected_storage: Option<&StorageDescriptor>,
    ) -> Result<(), SnapshotLoadError> {
        validate_snapshot_id(snapshot_id)?;
        if spec.storage.provider != StorageProvider::Registry
            && expected_storage.is_some_and(|expected| &spec.storage != expected)
        {
            return Err(SnapshotLoadError::IdentityConflict(
                "snapshot storage credential or endpoint identity conflicts with this node".into(),
            ));
        }
        validate_snapshot_paths(spec, snapshot_id)
    }

    async fn load(
        &self,
        io: &SnapshotMetadataReadContext<'_>,
        spec: SnapshotSpec,
        snapshot_id: &str,
        verify_data_objects: bool,
    ) -> Result<DecodedSnapshotMetadata, SnapshotLoadError> {
        let (metadata, metadata_bytes, _) = self
            .load_snapshot(io, spec, snapshot_id, verify_data_objects)
            .await?;
        Ok(DecodedSnapshotMetadata {
            metadata,
            metadata_bytes,
        })
    }
}

impl OverlaybdS3Metadata {
    pub(crate) async fn load_snapshot(
        &self,
        io: &SnapshotMetadataReadContext<'_>,
        spec: SnapshotSpec,
        snapshot_id: &str,
        verify_data_objects: bool,
    ) -> Result<(Arc<crate::snapshot::Snapshot>, usize, SnapshotBlobChains), SnapshotLoadError>
    {
        let mut cache = HashMap::new();
        let mut metadata_bytes = 0_usize;
        let manifest_payload = io
            .read_verified(
                &spec.manifest.object_key,
                &spec.manifest.sha256,
                spec.manifest.object_version.as_deref(),
                io.limits.max_manifest_bytes,
                &mut metadata_bytes,
                &mut cache,
            )
            .await?;
        let manifest: SnapshotManifest =
            parse_canonical_model(&manifest_payload, "snapshot manifest")
                .map_err(|error| SnapshotLoadError::InvalidSnapshotMetadata(error.to_string()))?;
        self.validate_manifest_binding(&spec, &manifest, snapshot_id)?;

        let mut data_objects = HashMap::new();
        let (rootfs, disk_blobs) = self
            .load_chain_with_blobs(
                io,
                &spec.rootfs,
                LayerKind::Block,
                &mut metadata_bytes,
                &mut cache,
                &mut data_objects,
            )
            .await?;
        let (memory, memory_blobs) = if let Some(reference) = &spec.memory {
            let (chain, blobs) = self
                .load_chain_with_blobs(
                    io,
                    reference,
                    LayerKind::Memory,
                    &mut metadata_bytes,
                    &mut cache,
                    &mut data_objects,
                )
                .await?;
            (Some(chain), blobs)
        } else {
            (None, Vec::new())
        };
        let vm_state = if let Some(reference) = &spec.vm_state {
            if reference.stored_bytes > io.limits.max_vm_state_bytes as u64 {
                return Err(SnapshotLoadError::BoundExceeded("VM state".into()));
            }
            let payload = io
                .read_verified(
                    &reference.object_key,
                    &reference.sha256,
                    reference.object_version.as_deref(),
                    io.limits.max_vm_state_bytes,
                    &mut metadata_bytes,
                    &mut cache,
                )
                .await?;
            if payload.len() as u64 != reference.stored_bytes {
                return Err(SnapshotLoadError::IdentityConflict(
                    "VM state size conflicts with snapshot spec".into(),
                ));
            }
            Some(payload)
        } else {
            None
        };
        if verify_data_objects {
            self.verify_data_objects(io, &data_objects).await?;
        }

        let metadata = super::OverlaybdS3Snapshot::new(
            snapshot_id.to_owned(),
            spec,
            manifest,
            memory,
            rootfs,
            vm_state,
        )
        .map_err(SnapshotLoadError::InvalidSnapshotMetadata)?;
        Ok((
            metadata,
            metadata_bytes,
            SnapshotBlobChains {
                rootfs: disk_blobs,
                memory: memory_blobs,
            },
        ))
    }
}

impl OverlaybdS3Metadata {
    #[allow(clippy::too_many_arguments)]
    #[cfg(test)]
    pub(crate) async fn load_chain(
        &self,
        io: &SnapshotMetadataReadContext<'_>,
        reference: &ChainIndexRef,
        expected_kind: LayerKind,
        metadata_bytes: &mut usize,
        cache: &mut HashMap<String, VerifiedMetadata>,
        data_objects: &mut HashMap<String, DataObjectIdentity>,
    ) -> Result<SnapshotChainView, SnapshotLoadError> {
        self.load_chain_with_blobs(
            io,
            reference,
            expected_kind,
            metadata_bytes,
            cache,
            data_objects,
        )
        .await
        .map(|(chain, _)| chain)
    }

    #[allow(clippy::too_many_arguments)]
    async fn load_chain_with_blobs(
        &self,
        io: &SnapshotMetadataReadContext<'_>,
        reference: &ChainIndexRef,
        expected_kind: LayerKind,
        metadata_bytes: &mut usize,
        cache: &mut HashMap<String, VerifiedMetadata>,
        data_objects: &mut HashMap<String, DataObjectIdentity>,
    ) -> Result<(SnapshotChainView, Vec<BlobRef>), SnapshotLoadError> {
        let payload = io
            .read_verified(
                &reference.index_object_key,
                &reference.index_sha256,
                reference.object_version.as_deref(),
                io.limits.max_chain_index_bytes,
                metadata_bytes,
                cache,
            )
            .await?;
        let chain: LayerChainIndex = parse_canonical_model(&payload, "chain index")
            .map_err(|error| SnapshotLoadError::InvalidSnapshotMetadata(error.to_string()))?;
        if chain.kind != expected_kind || chain.logical_bytes != reference.logical_bytes {
            return Err(SnapshotLoadError::IdentityConflict(
                "chain role or geometry conflicts with snapshot spec".into(),
            ));
        }
        if chain.layers.len() > MAX_CHAIN_DEPTH {
            return Err(SnapshotLoadError::BoundExceeded("chain depth".into()));
        }

        let mut layers = Vec::with_capacity(chain.layers.len());
        let mut native_blobs = Vec::new();
        for (ordinal, layer_ref) in chain.layers.iter().enumerate() {
            let external_source = layer_ref.range_map.source_ref.as_ref();
            let payload = if let Some(source_ref) = external_source {
                if expected_kind != LayerKind::Block || ordinal != 0 {
                    return Err(SnapshotLoadError::InvalidSnapshotMetadata(
                        "Registry sources are supported only as a rootfs base".into(),
                    ));
                }
                let resolved = io
                    .source_resolver
                    .resolve_source(source_ref)
                    .await
                    .map_err(source_load_error)?;
                let payload = resolved.descriptor.clone();
                if sha256_identity(&payload) != layer_ref.range_map.sha256 {
                    return Err(SnapshotLoadError::IdentityConflict(
                        "Registry descriptor differs from its layer reference".into(),
                    ));
                }
                *metadata_bytes = metadata_bytes.saturating_add(payload.len());
                if payload.len() > io.limits.max_range_map_bytes
                    || *metadata_bytes > io.limits.max_total_metadata_bytes
                {
                    return Err(SnapshotLoadError::BoundExceeded(
                        "Registry layer metadata".into(),
                    ));
                }
                payload
            } else {
                io.read_verified(
                    &layer_ref.range_map.object_key,
                    &layer_ref.range_map.sha256,
                    layer_ref.range_map.object_version.as_deref(),
                    io.limits.max_range_map_bytes,
                    metadata_bytes,
                    cache,
                )
                .await?
            };
            let decoded = decode_layer(layer_ref.range_map.encoding, &payload).map_err(
                |error| match error {
                    LayerFormatError::EncodingMismatch => {
                        SnapshotLoadError::IdentityConflict(error.to_string())
                    }
                    LayerFormatError::Invalid(message) => {
                        SnapshotLoadError::InvalidSnapshotMetadata(message)
                    }
                },
            )?;
            let layer = decoded.range_map;
            let overlaybd_blob = decoded.blob;
            if let Some((blob, size)) = &overlaybd_blob {
                native_blobs.push(BlobRef {
                    digest: blob.sha256.clone(),
                    size: *size,
                });
            } else if let Some(rootfs) = &layer.registry {
                native_blobs.extend(rootfs.layers().iter().map(|layer| BlobRef {
                    digest: layer.digest.clone(),
                    size: layer.size,
                }));
            }
            if layer.kind != chain.kind
                || layer.format_version != layer_ref.format_version
                || layer.logical_bytes != chain.logical_bytes
                || layer.unit_size_bytes != chain.unit_size_bytes
                || layer.content_sha256 != layer_ref.content_sha256
            {
                return Err(SnapshotLoadError::IdentityConflict(
                    "layer identity or geometry conflicts with chain".into(),
                ));
            }
            if layer.ranges.len() > MAX_RANGES_PER_LAYER {
                return Err(SnapshotLoadError::BoundExceeded("layer range count".into()));
            }
            if ordinal == 0
                && layer
                    .ranges
                    .iter()
                    .any(|range| range.kind == RangeKind::Inherit)
            {
                return Err(SnapshotLoadError::InvalidSnapshotMetadata(
                    "root layer cannot contain INHERIT ranges".into(),
                ));
            }
            if external_source.is_none() {
                self.verify_committed(io, layer_ref, &layer, metadata_bytes, cache)
                    .await?;
            } else {
                let expected = format!(
                    "layers/block/1/{}/registry-overlaybd.json",
                    &layer.content_sha256[7..]
                );
                if layer.registry.is_none() || layer_ref.range_map.object_key != expected {
                    return Err(SnapshotLoadError::IdentityConflict(
                        "Registry source conflicts with its canonical layer identity".into(),
                    ));
                }
            }
            if overlaybd_blob.is_some() {
                let content = layer
                    .content_sha256
                    .strip_prefix("sha256:")
                    .expect("validated layer identity");
                let expected = format!(
                    "layers/{}/{}/{content}/{}",
                    layer.kind.object_key_component(),
                    layer.format_version,
                    layer_ref
                        .range_map
                        .encoding
                        .expect("OverlayBD encoding")
                        .descriptor_name()
                );
                if layer_ref.range_map.object_key != expected {
                    return Err(SnapshotLoadError::InvalidSnapshotMetadata(
                        "OverlayBD descriptor is outside its canonical layer prefix".into(),
                    ));
                }
            }
            // Registry descriptors bind immutable OCI digests and native
            // physical indexes. Their logical source is not an S3 object and
            // must never be probed as a snapd pack or put into the SHA cache.
            if layer.registry.is_none() {
                let blob = overlaybd_blob.as_ref().ok_or_else(|| {
                    SnapshotLoadError::InvalidSnapshotMetadata(
                        "persistent layer requires an OverlayBD blob descriptor".into(),
                    )
                })?;
                self.collect_data_objects(&layer, blob, data_objects)?;
            }
            layers.push(SnapshotLayerView {
                layer_ref: layer_ref.clone(),
                range_map: Arc::new(layer),
            });
        }
        Ok((
            SnapshotChainView {
                index: chain,
                layers,
            },
            native_blobs,
        ))
    }

    async fn verify_committed(
        &self,
        io: &SnapshotMetadataReadContext<'_>,
        layer_ref: &LayerRef,
        layer: &LayerRangeMap,
        metadata_bytes: &mut usize,
        cache: &mut HashMap<String, VerifiedMetadata>,
    ) -> Result<(), SnapshotLoadError> {
        let content = layer
            .content_sha256
            .strip_prefix("sha256:")
            .expect("validated layer content identity");
        let prefix = format!(
            "layers/{}/{}/{content}/",
            layer.kind.object_key_component(),
            layer.format_version
        );
        let payload = canonical_json_bytes(&json!({
            "content_sha256": layer.content_sha256,
            "format_version": layer.format_version,
            "range_map_sha256": layer_ref.range_map.sha256,
            "kind": layer.kind,
        }))
        .map_err(|error| SnapshotLoadError::InvalidSnapshotMetadata(error.to_string()))?;
        let marker =
            crate::snapshot::metadata::required_layer_encoding(layer_ref.range_map.encoding)
                .map_err(SnapshotLoadError::InvalidSnapshotMetadata)?
                .committed_name();
        let actual = io
            .read_verified(
                &format!("{prefix}{marker}"),
                &sha256_identity(&payload),
                None,
                io.limits.max_manifest_bytes,
                metadata_bytes,
                cache,
            )
            .await?;
        if actual != payload {
            return Err(SnapshotLoadError::IdentityConflict(
                "COMMITTED marker conflicts with layer identity".into(),
            ));
        }
        Ok(())
    }

    fn collect_data_objects(
        &self,
        layer: &LayerRangeMap,
        overlaybd_blob: &(ObjectRef, u64),
        objects: &mut HashMap<String, DataObjectIdentity>,
    ) -> Result<(), SnapshotLoadError> {
        let content = layer
            .content_sha256
            .strip_prefix("sha256:")
            .expect("validated layer content identity");
        let prefix = format!(
            "layers/{}/{}/{content}/data/",
            layer.kind.object_key_component(),
            layer.format_version
        );
        let (blob, length) = overlaybd_blob;
        {
            let hash = blob.sha256.strip_prefix("sha256:").ok_or_else(|| {
                SnapshotLoadError::InvalidSnapshotMetadata("invalid OverlayBD blob hash".into())
            })?;
            if blob.object_key != format!("{prefix}{hash}.commit") {
                return Err(SnapshotLoadError::InvalidSnapshotMetadata(
                    "OverlayBD blob is outside its canonical layer prefix".into(),
                ));
            }
            if *length == 0 || *length > MAX_BLOB_BYTES {
                return Err(SnapshotLoadError::BoundExceeded(
                    "OverlayBD blob size".into(),
                ));
            }
            // Keep the object in strict/audit validation even when the standard
            // layer contains no DATA units (all ZERO / INHERIT).
            let identity = DataObjectIdentity {
                sha256: blob.sha256.clone(),
                version: blob.object_version.clone(),
                required_end: 0,
                exact_size: Some(*length),
                max_bytes: MAX_BLOB_BYTES,
            };
            Self::insert_data_object(objects, &blob.object_key, identity)?;
        }
        for source in layer
            .ranges
            .iter()
            .filter_map(|range| range.source.as_ref())
        {
            self.validate_data_path(source, &prefix)?;
            let required_end = source
                .pack_offset
                .checked_add(source.encoded_length)
                .ok_or_else(|| {
                    SnapshotLoadError::InvalidSnapshotMetadata(
                        "DATA pack range overflows u64".into(),
                    )
                })?;
            let max_bytes = MAX_BLOB_BYTES;
            if required_end > max_bytes {
                return Err(SnapshotLoadError::BoundExceeded(format!(
                    "DATA object {:?}",
                    source.object_key
                )));
            }
            if source.object_key != blob.object_key
                || source.object_sha256 != blob.sha256
                || source.object_version != blob.object_version
                || required_end
                    > layer
                        .zfile
                        .as_ref()
                        .map_or(*length, |index| index.raw_bytes)
            {
                return Err(SnapshotLoadError::IdentityConflict(
                    "DATA range conflicts with its OverlayBD blob descriptor".into(),
                ));
            }
            let identity = DataObjectIdentity {
                sha256: source.object_sha256.clone(),
                version: source.object_version.clone(),
                // DATA addresses the decoded virtual LSMT, whereas HEAD size
                // and immutable object identity address physical ZFile bytes.
                required_end: if layer.zfile.is_some() {
                    0
                } else {
                    required_end
                },
                exact_size: Some(*length),
                max_bytes,
            };
            Self::insert_data_object(objects, &source.object_key, identity)?;
        }
        Ok(())
    }

    fn insert_data_object(
        objects: &mut HashMap<String, DataObjectIdentity>,
        key: &str,
        identity: DataObjectIdentity,
    ) -> Result<(), SnapshotLoadError> {
        match objects.get_mut(key) {
            Some(existing)
                if existing.sha256 == identity.sha256
                    && existing.version == identity.version
                    && existing.exact_size == identity.exact_size
                    && existing.max_bytes == identity.max_bytes =>
            {
                existing.required_end = existing.required_end.max(identity.required_end);
            }
            Some(_) => {
                return Err(SnapshotLoadError::IdentityConflict(format!(
                    "DATA object {key:?} has conflicting references"
                )));
            }
            None => {
                objects.insert(key.to_owned(), identity);
            }
        }
        Ok(())
    }

    fn validate_data_path(
        &self,
        source: &DataSource,
        prefix: &str,
    ) -> Result<(), SnapshotLoadError> {
        let hash = source
            .object_sha256
            .strip_prefix("sha256:")
            .expect("validated DATA object identity");
        let expected = format!("{prefix}{hash}.commit");
        if source.object_key != expected {
            return Err(SnapshotLoadError::InvalidSnapshotMetadata(format!(
                "DATA object key must be {expected:?}"
            )));
        }
        Ok(())
    }

    pub(crate) async fn verify_data_objects(
        &self,
        io: &SnapshotMetadataReadContext<'_>,
        objects: &HashMap<String, DataObjectIdentity>,
    ) -> Result<(), SnapshotLoadError> {
        for (key, identity) in objects {
            let metadata = io.head_required(key).await?;
            io.verify_metadata_identity(
                key,
                &metadata,
                &identity.sha256,
                identity.version.as_deref(),
            )?;
            if metadata.size > identity.max_bytes {
                return Err(SnapshotLoadError::BoundExceeded(format!(
                    "DATA object {key:?}"
                )));
            }
            if identity.required_end > metadata.size
                || identity
                    .exact_size
                    .is_some_and(|length| length != metadata.size)
            {
                return Err(SnapshotLoadError::IdentityConflict(format!(
                    "DATA range exceeds immutable object {key:?}"
                )));
            }
        }
        Ok(())
    }

    fn validate_manifest_binding(
        &self,
        spec: &SnapshotSpec,
        manifest: &SnapshotManifest,
        snapshot_id: &str,
    ) -> Result<(), SnapshotLoadError> {
        if manifest.snapshot_id != snapshot_id
            || manifest.format_version != spec.format_version
            || manifest.rootfs != manifest_chain(&spec.rootfs)
            || manifest.memory != spec.memory.as_ref().map(manifest_chain)
            || manifest.vm_state != spec.vm_state
        {
            return Err(SnapshotLoadError::IdentityConflict(
                "snapshot manifest conflicts with catalog spec".into(),
            ));
        }
        match manifest.snapshot_kind {
            SnapshotKind::BaseImage if spec.memory.is_some() || spec.vm_state.is_some() => {
                return Err(SnapshotLoadError::IdentityConflict(
                    "BASE_IMAGE spec contains memory or VM state".into(),
                ));
            }
            SnapshotKind::Full if spec.memory.is_none() || spec.vm_state.is_none() => {
                return Err(SnapshotLoadError::IdentityConflict(
                    "FULL spec is missing memory or VM state".into(),
                ));
            }
            _ => {}
        }
        Ok(())
    }
}

fn manifest_chain(reference: &ChainIndexRef) -> ManifestChainRef {
    ManifestChainRef {
        object_key: reference.index_object_key.clone(),
        sha256: reference.index_sha256.clone(),
        logical_bytes: reference.logical_bytes,
        object_version: reference.object_version.clone(),
    }
}

fn source_load_error(error: SourceError) -> SnapshotLoadError {
    if error.is_integrity() {
        SnapshotLoadError::IdentityConflict(error.to_string())
    } else if error.retryable() {
        SnapshotLoadError::Store(ObjectStoreError::new(
            crate::snapshot::transport::PublishFailureKind::Retryable,
            error.to_string(),
        ))
    } else {
        SnapshotLoadError::InvalidSnapshotMetadata(error.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs::File;
    use std::sync::Mutex;

    use crate::snapshot::format::overlaybd::{encode_descriptor, plan_layer};
    use crate::snapshot::io::snapshot_io::{SnapshotIo, SnapshotLoadLimits};
    use crate::snapshot::metadata::{
        compute_layer_content_sha256, snapshot_spec_checksum, LayerRange, LayerRangeMapRef,
        RangeMapEncoding, RockletArtifactCompatibility, VmStateRef, VmmCompatibility,
    };
    use crate::snapshot::transport::{
        BoundedGetError, ImmutableObjectStore, NamespaceMutationFence, RemoteObject,
        RemoteObjectMetadata,
    };

    #[derive(Debug)]
    struct ProfileStore {
        fence: Arc<NamespaceMutationFence>,
        metadata: HashMap<String, Vec<u8>>,
        blobs: HashMap<String, RemoteObjectMetadata>,
        reads: Mutex<Vec<String>>,
        heads: Mutex<Vec<String>>,
    }

    fn object_metadata(payload: &[u8]) -> RemoteObjectMetadata {
        RemoteObjectMetadata {
            size: payload.len() as u64,
            sha256: sha256_identity(payload),
            version: None,
            last_modified_unix: None,
            etag: None,
        }
    }

    #[async_trait::async_trait]
    impl ImmutableObjectStore for ProfileStore {
        fn namespace(&self) -> &str {
            self.fence.namespace()
        }

        fn fence(&self) -> Arc<NamespaceMutationFence> {
            Arc::clone(&self.fence)
        }

        async fn head(&self, key: &str) -> Result<Option<RemoteObjectMetadata>, ObjectStoreError> {
            self.heads.lock().unwrap().push(key.to_owned());
            Ok(self.blobs.get(key).cloned())
        }

        async fn put_if_absent(
            &self,
            _key: &str,
            _payload: &[u8],
            _sha256: &str,
        ) -> Result<bool, ObjectStoreError> {
            panic!("opening snapshot metadata must not publish objects")
        }

        async fn get(&self, _key: &str) -> Result<Vec<u8>, ObjectStoreError> {
            panic!("snapshot profiles must use bounded authenticated metadata reads")
        }

        async fn get_with_metadata(
            &self,
            key: &str,
            _version: Option<&str>,
            max_bytes: u64,
        ) -> Result<Option<RemoteObject>, BoundedGetError> {
            assert!(
                !self.blobs.contains_key(key),
                "metadata opening must never GET a DATA blob"
            );
            self.reads.lock().unwrap().push(key.to_owned());
            let Some(payload) = self.metadata.get(key) else {
                return Ok(None);
            };
            if payload.len() as u64 > max_bytes {
                return Err(BoundedGetError::BoundExceeded {
                    object_key: key.to_owned(),
                    actual_bytes: payload.len() as u64,
                    max_bytes,
                });
            }
            Ok(Some(RemoteObject {
                payload: payload.clone(),
                metadata: object_metadata(payload),
            }))
        }
    }

    fn add_zero_chain(store: &mut ProfileStore, id: &str, kind: LayerKind) -> ChainIndexRef {
        let unit = match kind {
            LayerKind::Memory => 4096,
            LayerKind::Block => 65536,
        };
        let mut layer = LayerRangeMap {
            format_version: 1,
            kind,
            content_sha256: sha256_identity(b"provisional"),
            logical_bytes: unit,
            unit_size_bytes: unit,
            registry: None,
            registry_objects: None,
            zfile: None,
            ranges: vec![LayerRange {
                kind: RangeKind::Zero,
                logical_offset: 0,
                length: unit,
                source: None,
            }],
        };
        layer.content_sha256 = compute_layer_content_sha256(&layer).unwrap();
        let plan = plan_layer(&layer).unwrap();
        let payload = plan
            .read_at(&File::open("/dev/null").unwrap(), 0, 1024 * 1024)
            .unwrap();
        let prefix = format!(
            "layers/{}/1/{}/",
            kind.object_key_component(),
            &layer.content_sha256[7..]
        );
        let blob = ObjectRef {
            object_key: format!("{prefix}data/{}.commit", &sha256_identity(&payload)[7..]),
            sha256: sha256_identity(&payload),
            object_version: None,
        };
        store
            .blobs
            .insert(blob.object_key.clone(), object_metadata(&payload));
        let encoding = RangeMapEncoding::OverlaybdLsmtV1;
        let descriptor = encode_descriptor(&layer, &plan, &blob).unwrap();
        let layer_ref = LayerRef {
            ordinal: 0,
            layer_id: format!("{id}-{kind:?}"),
            format_version: 1,
            content_sha256: layer.content_sha256.clone(),
            range_map: LayerRangeMapRef {
                object_key: format!("{prefix}{}", encoding.descriptor_name()),
                sha256: sha256_identity(&descriptor),
                object_version: None,
                source_ref: None,
                encoding: Some(encoding),
            },
        };
        let committed = canonical_json_bytes(&json!({
            "content_sha256": layer.content_sha256,
            "format_version": 1,
            "range_map_sha256": layer_ref.range_map.sha256,
            "kind": kind,
        }))
        .unwrap();
        store
            .metadata
            .insert(layer_ref.range_map.object_key.clone(), descriptor);
        store
            .metadata
            .insert(format!("{prefix}{}", encoding.committed_name()), committed);
        let chain = LayerChainIndex {
            format_version: 1,
            kind,
            logical_bytes: unit,
            unit_size_bytes: unit,
            layers: vec![layer_ref],
        };
        let payload = canonical_json_bytes(&chain).unwrap();
        let name = match kind {
            LayerKind::Memory => "memory",
            LayerKind::Block => "rootfs",
        };
        let reference = ChainIndexRef {
            logical_bytes: unit,
            index_object_key: format!("snapshots/{id}/{name}.index"),
            index_sha256: sha256_identity(&payload),
            object_version: None,
            source_ref: None,
        };
        store
            .metadata
            .insert(reference.index_object_key.clone(), payload);
        reference
    }

    fn snapshot_fixture(full: bool) -> (ProfileStore, SnapshotSpec, SnapshotManifest) {
        let id = if full { "profile-full" } else { "profile-base" };
        let mut store = ProfileStore {
            fence: NamespaceMutationFence::new(id).unwrap(),
            metadata: HashMap::new(),
            blobs: HashMap::new(),
            reads: Mutex::new(Vec::new()),
            heads: Mutex::new(Vec::new()),
        };
        let rootfs = add_zero_chain(&mut store, id, LayerKind::Block);
        let memory = full.then(|| add_zero_chain(&mut store, id, LayerKind::Memory));
        let vm_state = full.then(|| {
            let payload = b"captured registers and device state".to_vec();
            let reference = VmStateRef {
                object_key: format!("snapshots/{id}/vm-state.pack"),
                sha256: sha256_identity(&payload),
                object_version: None,
                stored_bytes: payload.len() as u64,
            };
            store.metadata.insert(reference.object_key.clone(), payload);
            reference
        });
        let manifest = SnapshotManifest {
            format_version: 1,
            snapshot_id: id.into(),
            snapshot_kind: if full {
                SnapshotKind::Full
            } else {
                SnapshotKind::BaseImage
            },
            vm_state: vm_state.clone(),
            memory: memory.as_ref().map(manifest_chain),
            rootfs: manifest_chain(&rootfs),
        };
        let payload = canonical_json_bytes(&manifest).unwrap();
        let manifest_ref = ObjectRef {
            object_key: format!("snapshots/{id}/manifest.json"),
            sha256: sha256_identity(&payload),
            object_version: None,
        };
        store
            .metadata
            .insert(manifest_ref.object_key.clone(), payload);
        let spec = SnapshotSpec {
            registry_snapshot: None,
            format_version: 1,
            compatibility: VmmCompatibility {
                architecture: "x86_64".into(),
                vmm_type: "clh".into(),
                vmm_state_format_version: full.then_some(1),
                rocklet_artifact: full.then(|| RockletArtifactCompatibility {
                    identity: sha256_identity(b"rocklet-fixture"),
                    version: "v1".into(),
                    raw_sha256: sha256_identity(b"rocklet-disk"),
                    logical_bytes: 65536,
                    guest_port: 4400,
                }),
            },
            provenance: None,
            storage: StorageDescriptor {
                provider: StorageProvider::S3,
                endpoint_profile: "profile-test".into(),
                credential_ref: "profile-test-credential".into(),
            },
            manifest: manifest_ref,
            vm_state,
            memory,
            rootfs,
        };
        (store, spec, manifest)
    }

    #[tokio::test]
    async fn s3_base_and_full_open_complete_metadata_without_fetching_data() {
        for full in [false, true] {
            let (store, spec, manifest) = snapshot_fixture(full);
            let store = Arc::new(store);
            let reader = SnapshotIo::new_with_storage_identity(
                store.clone(),
                spec.storage.clone(),
                SnapshotLoadLimits::default(),
            )
            .unwrap();
            let value = serde_json::to_value(&spec).unwrap();
            let checksum = snapshot_spec_checksum(&value).unwrap();
            let loaded = reader
                .load_for_start(&value, &checksum, &manifest.snapshot_id)
                .await
                .unwrap();
            let metadata: &dyn crate::snapshot::SnapshotInstance = loaded.as_ref();
            assert_eq!(metadata.snapshot_kind(), manifest.snapshot_kind);
            assert_eq!(
                metadata
                    .layer_chain(LayerKind::Block)
                    .unwrap()
                    .unwrap()
                    .layers
                    .len(),
                1
            );
            assert_eq!(
                metadata.layer_chain(LayerKind::Memory).unwrap().is_some(),
                full
            );
            let view: &dyn crate::snapshot::SnapshotInstance = loaded.as_ref();
            assert!(view
                .layer_reader(
                    LayerKind::Block,
                    &loaded.chain_view(LayerKind::Block).unwrap().index.layers[0].content_sha256
                )
                .is_ok());
            assert_eq!(loaded.spec(), Some(&spec));
            assert_eq!(loaded.manifest(), Some(&manifest));
            assert_eq!(
                loaded.chain_view(LayerKind::Block).unwrap().index.kind,
                LayerKind::Block
            );
            assert_eq!(loaded.chain_view(LayerKind::Memory).is_some(), full);
            if full {
                assert_eq!(
                    loaded.chain_view(LayerKind::Memory).unwrap().index.kind,
                    LayerKind::Memory
                );
                assert_eq!(
                    loaded.vm_state(),
                    Some(b"captured registers and device state".as_slice())
                );
            } else {
                assert!(loaded.vm_state().is_none());
            }
            assert!(store.heads.lock().unwrap().is_empty());
            reader
                .load(&value, &checksum, &manifest.snapshot_id)
                .await
                .unwrap();
            let heads = store.heads.lock().unwrap();
            assert_eq!(heads.len(), store.blobs.len());
            assert!(heads.iter().all(|key| store.blobs.contains_key(key)));
        }
    }

    #[tokio::test]
    async fn s3_rejects_authenticated_manifest_that_conflicts_with_catalog_before_chain_reads() {
        for full in [false, true] {
            let (mut store, mut spec, mut manifest) = snapshot_fixture(full);
            manifest.rootfs.logical_bytes += 65536;
            let payload = canonical_json_bytes(&manifest).unwrap();
            spec.manifest.sha256 = sha256_identity(&payload);
            store
                .metadata
                .insert(spec.manifest.object_key.clone(), payload);
            let store = Arc::new(store);
            let reader = SnapshotIo::new(store.clone());
            let value = serde_json::to_value(&spec).unwrap();
            let checksum = snapshot_spec_checksum(&value).unwrap();
            let error = reader
                .load_for_start(&value, &checksum, &manifest.snapshot_id)
                .await
                .unwrap_err();
            assert!(matches!(
                error,
                SnapshotLoadError::IdentityConflict(message)
                    if message == "snapshot manifest conflicts with catalog spec"
            ));
            assert_eq!(*store.reads.lock().unwrap(), vec![spec.manifest.object_key]);
            assert!(store.heads.lock().unwrap().is_empty());
        }
    }
}
