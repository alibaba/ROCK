//! Validated, immutable plans for snapshot upload.

use std::collections::{HashMap, HashSet};

use serde_json::json;
use sha2::{Digest, Sha256};

use crate::snapshot::format::overlaybd::{decode_descriptor_with_blob, plan_layer};
use crate::snapshot::format::overlaybd_s3::data::{DataPackSliceIdentity, DataPackUploadRequest};
use crate::snapshot::metadata::{
    canonical_json_bytes, parse_canonical_model, sha256_identity, LayerKind, LayerRangeMap,
    SnapshotKind, SnapshotSpec,
};

use crate::snapshot::format::overlaybd_s3::publication::validate_pack_path;
use crate::snapshot::io::publication::SnapshotUploadError;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ImmutableObject {
    pub object_key: String,
    pub payload: Vec<u8>,
    pub sha256: String,
}

impl ImmutableObject {
    pub fn new(
        object_key: impl Into<String>,
        payload: Vec<u8>,
    ) -> Result<Self, SnapshotUploadError> {
        let object_key = object_key.into();
        let sha256 = sha256_identity(&payload);
        let object = Self {
            object_key,
            payload,
            sha256,
        };
        object.validate()?;
        Ok(object)
    }

    pub(crate) fn validate(&self) -> Result<(), SnapshotUploadError> {
        if self.object_key.is_empty() || self.object_key.len() > 4096 {
            return Err(SnapshotUploadError::InvalidPlan(
                "object key length must be 1..=4096".into(),
            ));
        }
        if sha256_identity(&self.payload) != self.sha256 {
            return Err(SnapshotUploadError::InvalidPlan(format!(
                "local object checksum mismatch for {:?}",
                self.object_key
            )));
        }
        Ok(())
    }
}

#[derive(Debug, Clone)]
pub struct LayerUploadPlan {
    pub kind: LayerKind,
    pub format_version: u64,
    pub content_sha256: String,
    data_requests: Vec<DataPackUploadRequest>,
    pub range_map: ImmutableObject,
    pub committed: ImmutableObject,
}

impl LayerUploadPlan {
    pub(crate) fn from_requests(
        kind: LayerKind,
        format_version: u64,
        content_sha256: String,
        data_requests: Vec<DataPackUploadRequest>,
        range_map: ImmutableObject,
        committed: ImmutableObject,
    ) -> Result<Self, SnapshotUploadError> {
        let plan = Self {
            kind,
            format_version,
            content_sha256,
            data_requests,
            range_map,
            committed,
        };
        plan.validate()?;
        Ok(plan)
    }

    pub(crate) fn data_requests(&self) -> &[DataPackUploadRequest] {
        &self.data_requests
    }

    pub(crate) fn identity(&self, namespace: &str) -> String {
        format!(
            "{namespace}:{}:{}:{}:{}",
            self.kind.object_key_component(),
            self.format_version,
            self.content_sha256,
            self.range_map.sha256
        )
    }

    pub(crate) fn validate(&self) -> Result<LayerRangeMap, SnapshotUploadError> {
        for object in [&self.range_map, &self.committed] {
            object.validate()?;
        }
        if self
            .range_map
            .object_key
            .ends_with("/registry-overlaybd.json")
        {
            let invalid = SnapshotUploadError::InvalidPlan;
            if !self.data_requests.is_empty() {
                return Err(invalid(
                    "Registry base must not upload external DATA".into(),
                ));
            }
            let native = crate::snapshot::format::overlaybd::composite::decode_descriptor(
                &self.range_map.payload,
            )
            .map_err(invalid)?;
            let index = native.build_map().map_err(invalid)?;
            native.validate_map(&index).map_err(invalid)?;
            let content = index.content_sha256.trim_start_matches("sha256:");
            let prefix = format!("layers/block/1/{content}/");
            let expected = canonical_json_bytes(&json!({
                "content_sha256": index.content_sha256,
                "format_version": 1,
                "range_map_sha256": self.range_map.sha256,
                "kind": LayerKind::Block,
            }))
            .map_err(|error| invalid(error.to_string()))?;
            if self.kind != LayerKind::Block
                || self.format_version != 1
                || self.content_sha256 != index.content_sha256
                || self.range_map.object_key != format!("{prefix}registry-overlaybd.json")
                || self.committed.object_key != format!("{prefix}COMMITTED.registry-overlaybd")
                || self.committed.payload != expected
            {
                return Err(invalid(
                    "Registry base layer identity or commit fence differs".into(),
                ));
            }
            return Ok(index);
        }
        for request in &self.data_requests {
            request
                .identity
                .validate()
                .map_err(|error| SnapshotUploadError::InvalidPlan(error.to_string()))?;
        }
        if !self.range_map.object_key.ends_with("/overlaybd.json")
            && !self
                .range_map
                .object_key
                .ends_with("/overlaybd-zfile-lz4.json")
        {
            return Err(SnapshotUploadError::InvalidPlan(
                "layer descriptor must use an explicit OverlayBD encoding".into(),
            ));
        }
        let (index, blob, length) = decode_descriptor_with_blob(&self.range_map.payload)
            .map_err(SnapshotUploadError::InvalidPlan)?;
        let range_map_name = if index.zfile.is_some() {
            "overlaybd-zfile-lz4.json"
        } else {
            "overlaybd.json"
        };
        if index.kind != self.kind
            || index.format_version != self.format_version
            || index.content_sha256 != self.content_sha256
        {
            return Err(SnapshotUploadError::InvalidPlan(
                "layer upload identity does not match its index".into(),
            ));
        }
        let content = self
            .content_sha256
            .strip_prefix("sha256:")
            .ok_or_else(|| SnapshotUploadError::InvalidPlan("invalid layer content hash".into()))?;
        let prefix = format!(
            "layers/{}/{}/{content}/",
            self.kind.object_key_component(),
            self.format_version
        );
        let marker = if index.zfile.is_some() {
            "COMMITTED.overlaybd-zfile-lz4"
        } else {
            "COMMITTED.overlaybd"
        };
        if self.range_map.object_key != format!("{prefix}{range_map_name}")
            || self.committed.object_key != format!("{prefix}{marker}")
        {
            return Err(SnapshotUploadError::InvalidPlan(
                "layer metadata is outside its content-addressed prefix".into(),
            ));
        }
        let expected_committed = canonical_json_bytes(&json!({
            "content_sha256": self.content_sha256,
            "format_version": self.format_version,
            "range_map_sha256": self.range_map.sha256,
            "kind": self.kind,
        }))
        .map_err(|error| SnapshotUploadError::InvalidPlan(error.to_string()))?;
        if self.committed.payload != expected_committed {
            return Err(SnapshotUploadError::InvalidPlan(
                "COMMITTED marker does not bind its layer index".into(),
            ));
        }
        let data = self
            .data_requests
            .iter()
            .map(|request| {
                (
                    request.identity.object_key.as_str(),
                    (&request.identity.expected_sha256, request.identity.length),
                )
            })
            .collect::<HashMap<_, _>>();
        if data.len() != self.data_requests.len() {
            return Err(SnapshotUploadError::InvalidPlan(
                "layer plan contains duplicate DATA upload requests".into(),
            ));
        }
        if self
            .data_requests
            .iter()
            .any(|request| request.zfile != index.zfile)
        {
            return Err(SnapshotUploadError::InvalidPlan(
                "DATA source ZFile encoding conflicts with its descriptor".into(),
            ));
        }
        let hash = blob.sha256.strip_prefix("sha256:").ok_or_else(|| {
            SnapshotUploadError::InvalidPlan("invalid OverlayBD blob hash".into())
        })?;
        if blob.object_key != format!("{prefix}data/{hash}.commit")
            || self.data_requests.len() != 1
            || !data
                .get(blob.object_key.as_str())
                .is_some_and(|(sha256, actual_length)| {
                    *sha256 == &blob.sha256 && *actual_length == length
                })
        {
            return Err(SnapshotUploadError::InvalidPlan(
                "OverlayBD descriptor must bind exactly one complete blob upload".into(),
            ));
        }
        // An all-ZERO or all-INHERIT layer still has a standard LSMT
        // header/index/trailer object, even though it has no DATA slices.
        let mut referenced = HashSet::from([self.data_requests[0].identity.object_key.as_str()]);
        let mut expected_slices = HashMap::<&str, Vec<DataPackSliceIdentity>>::new();
        for range in &index.ranges {
            if let Some(source) = &range.source {
                validate_pack_path(source, &prefix)?;
                if !source.object_key.ends_with(".commit")
                    || source.object_key != blob.object_key
                    || source.object_sha256 != blob.sha256
                    || source.object_version != blob.object_version
                {
                    return Err(SnapshotUploadError::InvalidPlan(
                        "DATA source encoding conflicts with its layer descriptor".into(),
                    ));
                }
                referenced.insert(source.object_key.as_str());
                expected_slices
                    .entry(source.object_key.as_str())
                    .or_default()
                    .push(DataPackSliceIdentity {
                        offset: source.pack_offset,
                        length: source.encoded_length,
                        expected_sha256: source.checksum_sha256.clone(),
                    });
                let pack_end = source
                    .pack_offset
                    .checked_add(source.encoded_length)
                    .ok_or_else(|| {
                        SnapshotUploadError::InvalidPlan("DATA pack range overflows".into())
                    })?;
                if !data
                    .get(source.object_key.as_str())
                    .is_some_and(|(sha256, length)| {
                        *sha256 == &source.object_sha256
                            && pack_end
                                <= index
                                    .zfile
                                    .as_ref()
                                    .map_or(*length, |zfile| zfile.raw_bytes)
                    })
                {
                    return Err(SnapshotUploadError::InvalidPlan(format!(
                        "DATA pack {:?} is absent or conflicts with the layer plan",
                        source.object_key
                    )));
                }
            }
        }
        if data.keys().copied().collect::<HashSet<_>>() != referenced {
            return Err(SnapshotUploadError::InvalidPlan(
                "layer plan contains an unreferenced DATA pack".into(),
            ));
        }
        {
            let plan = plan_layer(&index).map_err(SnapshotUploadError::InvalidPlan)?;
            let slices = expected_slices
                .entry(self.data_requests[0].identity.object_key.as_str())
                .or_default();
            // DATA-unit hashes alone do not authenticate the actual standard
            // metadata stream. Bind the bytes sent to the writer to the
            // descriptor's deterministic header, padded index and trailer too.
            for (offset, payload) in [
                (0, plan.header.as_slice()),
                (plan.index_offset, plan.index.as_slice()),
                (
                    plan.stored_bytes - plan.trailer.len() as u64,
                    plan.trailer.as_slice(),
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
        }
        for request in &self.data_requests {
            let mut expected = expected_slices
                .remove(request.identity.object_key.as_str())
                .unwrap_or_default();
            expected.sort_by_key(|slice| slice.offset);
            let mut actual = request.slices.clone();
            actual.sort_by_key(|slice| slice.offset);
            if actual != expected {
                return Err(SnapshotUploadError::InvalidPlan(format!(
                    "DATA slice identities conflict for {:?}",
                    request.identity.object_key.as_str()
                )));
            }
        }
        Ok(index)
    }
}

#[derive(Debug, Clone)]
pub struct SnapshotUploadPlan {
    pub snapshot_id: String,
    pub snapshot_kind: SnapshotKind,
    pub spec: SnapshotSpec,
    pub layers: Vec<LayerUploadPlan>,
    pub chain_indexes: Vec<ImmutableObject>,
    pub vm_state: Option<ImmutableObject>,
    pub manifest: ImmutableObject,
    pub(crate) registry_sources:
        Vec<std::sync::Arc<crate::snapshot::format::overlaybd_oci::RegistryObjectMap>>,
}

impl SnapshotUploadPlan {
    /// Project prepared, authenticated chain objects for core's layer catalog.
    /// No DATA encoding, source reads or remote writes are performed here.
    pub fn metadata_chains(
        &self,
    ) -> Result<Vec<crate::snapshot::metadata::SnapshotChainMetadata>, SnapshotUploadError> {
        let mut chains = Vec::new();
        for object in &self.chain_indexes {
            let index: crate::snapshot::metadata::LayerChainIndex =
                parse_canonical_model(&object.payload, "prepared layer chain")
                    .map_err(|e| SnapshotUploadError::InvalidPlan(e.to_string()))?;
            chains.push(crate::snapshot::metadata::SnapshotChainMetadata {
                kind: index.kind,
                logical_bytes: index.logical_bytes,
                unit_size_bytes: index.unit_size_bytes,
                layers: index
                    .layers
                    .iter()
                    .map(|layer| {
                        Ok(crate::snapshot::metadata::SnapshotLayerMetadata {
                            ordinal: layer.ordinal,
                            layer_id: layer.layer_id.clone(),
                            content_identity: layer.content_sha256.clone(),
                            format: crate::snapshot::metadata::encoding_name(
                                layer.range_map.encoding,
                            )
                            .map_err(SnapshotUploadError::InvalidPlan)?
                            .into(),
                            logical_bytes: index.logical_bytes,
                            unit_size_bytes: index.unit_size_bytes,
                        })
                    })
                    .collect::<Result<Vec<_>, SnapshotUploadError>>()?,
            });
        }
        Ok(chains)
    }

    pub(crate) fn identity(&self) -> Result<String, SnapshotUploadError> {
        let mut objects = self.all_metadata_objects();
        objects.sort_by(|left, right| left.object_key.cmp(&right.object_key));
        let mut data = self
            .layers
            .iter()
            .flat_map(|layer| layer.data_requests())
            .map(|request| &request.identity)
            .collect::<Vec<_>>();
        data.sort_by(|left, right| left.object_key.as_str().cmp(right.object_key.as_str()));
        // Credential versions are authorization inputs, not immutable content
        // identity.  A retry may resolve a rotated credential while provider,
        // endpoint, namespace, and every published byte remain identical.
        let identity = json!({
            "snapshot_id": self.snapshot_id,
            "snapshot_kind": self.snapshot_kind,
            "spec": {
                "compatibility": self.spec.compatibility,
                "format_version": self.spec.format_version,
                "manifest": self.spec.manifest,
                "memory": self.spec.memory,
                "provenance": self.spec.provenance,
                "rootfs": self.spec.rootfs,
                "storage": {
                    "endpoint_profile": self.spec.storage.endpoint_profile,
                    "provider": self.spec.storage.provider,
                },
                "vm_state": self.spec.vm_state,
            },
        });
        // Snapshot-level spec JSON is bounded metadata. Per-object / per-pack
        // identities are a binary stream, not a JSON array/DOM proportional
        // to the number of packs. This identity is local retry state only.
        let metadata = canonical_json_bytes(&identity)
            .map_err(|error| SnapshotUploadError::InvalidPlan(error.to_string()))?;
        let mut hash = Sha256::new();
        hash.update(b"ROCK-UPLOAD-PLAN\0\x02");
        fn bytes(hash: &mut Sha256, value: &[u8]) {
            hash.update((value.len() as u64).to_le_bytes());
            hash.update(value);
        }
        bytes(&mut hash, &metadata);
        hash.update((objects.len() as u64).to_le_bytes());
        for object in objects {
            bytes(&mut hash, object.object_key.as_bytes());
            bytes(&mut hash, object.sha256.as_bytes());
            hash.update((object.payload.len() as u64).to_le_bytes());
        }
        hash.update((data.len() as u64).to_le_bytes());
        for identity in data {
            bytes(&mut hash, identity.object_key.as_str().as_bytes());
            bytes(&mut hash, identity.expected_sha256.as_bytes());
            hash.update(identity.length.to_le_bytes());
        }
        Ok(format!("sha256:{:x}", hash.finalize()))
    }

    pub(crate) fn all_metadata_objects(&self) -> Vec<&ImmutableObject> {
        self.layers
            .iter()
            .flat_map(|layer| [&layer.range_map, &layer.committed])
            .chain(self.chain_indexes.iter())
            .chain(self.vm_state.iter())
            .chain([&self.manifest])
            .collect()
    }

    pub(crate) fn validate_shallow(&self) -> Result<(), SnapshotUploadError> {
        self.spec
            .validate_for_preparation()
            .map_err(SnapshotUploadError::InvalidPlan)?;
        let mut keys = HashSet::<String>::new();
        for object in self.all_metadata_objects() {
            object.validate()?;
            if !keys.insert(object.object_key.clone()) {
                return Err(SnapshotUploadError::InvalidPlan(format!(
                    "duplicate object key {:?} in upload plan",
                    object.object_key
                )));
            }
        }
        for request in self.layers.iter().flat_map(|layer| layer.data_requests()) {
            request
                .identity
                .validate()
                .map_err(|error| SnapshotUploadError::InvalidPlan(error.to_string()))?;
            if !keys.insert(request.identity.object_key.as_str().to_owned()) {
                return Err(SnapshotUploadError::InvalidPlan(format!(
                    "duplicate object key {:?} in upload plan",
                    request.identity.object_key.as_str()
                )));
            }
        }
        for layer in &self.layers {
            layer.validate()?;
        }
        let actual_layer_order = self
            .layers
            .iter()
            .map(|layer| layer.kind)
            .collect::<Vec<_>>();
        let expected_layer_order: &[LayerKind] = match self.snapshot_kind {
            SnapshotKind::BaseImage => &[LayerKind::Block],
            SnapshotKind::Full => &[LayerKind::Memory, LayerKind::Block],
        };
        if actual_layer_order != expected_layer_order {
            return Err(SnapshotUploadError::InvalidPlan(
                "Snapshot layers must use the canonical Memory-then-Block order".into(),
            ));
        }
        if self.manifest.object_key != self.spec.manifest.object_key
            || self.manifest.sha256 != self.spec.manifest.sha256
        {
            return Err(SnapshotUploadError::InvalidPlan(
                "manifest object does not match snapshot spec".into(),
            ));
        }
        let expected_chains = self
            .spec
            .memory
            .iter()
            .map(|memory| memory.index_object_key.as_str())
            .chain([self.spec.rootfs.index_object_key.as_str()])
            .collect::<HashSet<_>>();
        let actual_chains = self
            .chain_indexes
            .iter()
            .map(|object| object.object_key.as_str())
            .collect::<HashSet<_>>();
        if actual_chains != expected_chains {
            return Err(SnapshotUploadError::InvalidPlan(
                "upload plan must contain exactly the referenced chain indexes".into(),
            ));
        }
        if self
            .spec
            .vm_state
            .as_ref()
            .map(|state| state.object_key.as_str())
            != self
                .vm_state
                .as_ref()
                .map(|state| state.object_key.as_str())
        {
            return Err(SnapshotUploadError::InvalidPlan(
                "upload plan VM state does not match snapshot spec".into(),
            ));
        }
        Ok(())
    }
}

#[cfg(test)]
mod overlaybd_tests {
    use super::*;
    use std::fs::File;
    use std::future::Future;
    use std::pin::Pin;
    use std::sync::Arc;

    use crate::snapshot::format::overlaybd::{
        encode_descriptor, encode_zfile_descriptor, plan_layer,
    };
    use crate::snapshot::format::overlaybd_s3::data::{DataPackIdentity, DataPackObjectKey};
    use crate::snapshot::metadata::{
        compute_layer_content_sha256, LayerRange, ObjectRef, RangeKind,
    };
    use crate::snapshot::transport::{DataPackSource, DataPackSourceError};

    struct UnreadSource(u64);

    impl DataPackSource for UnreadSource {
        fn length(&self) -> u64 {
            self.0
        }

        fn read_at<'a>(
            &'a self,
            _offset: u64,
            _max_bytes: usize,
        ) -> Pin<Box<dyn Future<Output = Result<Vec<u8>, DataPackSourceError>> + Send + 'a>>
        {
            Box::pin(async { panic!("upload-plan validation must not read the DATA source") })
        }
    }

    #[test]
    fn zero_data_descriptor_requires_one_exact_standard_blob_upload() {
        let mut layer = LayerRangeMap {
            registry_objects: None,
            format_version: 1,
            kind: LayerKind::Memory,
            content_sha256: format!("sha256:{}", "0".repeat(64)),
            logical_bytes: 4096,
            unit_size_bytes: 4096,
            registry: None,
            zfile: None,
            ranges: vec![LayerRange {
                kind: RangeKind::Zero,
                logical_offset: 0,
                length: 4096,
                source: None,
            }],
        };
        layer.content_sha256 = compute_layer_content_sha256(&layer).unwrap();
        let blob_plan = plan_layer(&layer).unwrap();
        let payload = blob_plan
            .read_at(&File::open("/dev/null").unwrap(), 0, 1024 * 1024)
            .unwrap();
        let blob_hash = sha256_identity(&payload);
        let prefix = format!("layers/memory/1/{}/", &layer.content_sha256[7..]);
        let blob = ObjectRef {
            object_key: format!("{prefix}data/{}.commit", &blob_hash[7..]),
            sha256: blob_hash,
            object_version: None,
        };
        let range_map = ImmutableObject::new(
            format!("{prefix}overlaybd.json"),
            encode_descriptor(&layer, &blob_plan, &blob).unwrap(),
        )
        .unwrap();
        let committed = ImmutableObject::new(
            format!("{prefix}COMMITTED.overlaybd"),
            canonical_json_bytes(&json!({
                "content_sha256": layer.content_sha256,
                "format_version": 1,
                "range_map_sha256": range_map.sha256,
                "kind": layer.kind,
            }))
            .unwrap(),
        )
        .unwrap();
        let make_plan = |requests| {
            LayerUploadPlan::from_requests(
                layer.kind,
                1,
                layer.content_sha256.clone(),
                requests,
                range_map.clone(),
                committed.clone(),
            )
        };
        assert!(matches!(
            make_plan(Vec::new()),
            Err(SnapshotUploadError::InvalidPlan(_))
        ));
        let mut request = DataPackUploadRequest {
            zfile: None,
            identity: DataPackIdentity {
                object_key: DataPackObjectKey::new(blob.object_key).unwrap(),
                expected_sha256: blob.sha256,
                length: blob_plan.stored_bytes,
            },
            source: Arc::new(UnreadSource(blob_plan.stored_bytes)),
            slices: [
                (0, blob_plan.header.as_slice()),
                (blob_plan.index_offset, blob_plan.index.as_slice()),
                (
                    blob_plan.stored_bytes - blob_plan.trailer.len() as u64,
                    blob_plan.trailer.as_slice(),
                ),
            ]
            .into_iter()
            .filter(|(_, payload)| !payload.is_empty())
            .map(|(offset, payload)| DataPackSliceIdentity {
                offset,
                length: payload.len() as u64,
                expected_sha256: sha256_identity(payload),
            })
            .collect(),
        };
        let overlaybd_plan = make_plan(vec![request.clone()]).unwrap();
        let zfile_path =
            std::env::temp_dir().join(format!("snapd-plan-zfile-{}", uuid::Uuid::new_v4(),));
        let zfile_output = File::options()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&zfile_path)
            .unwrap();
        std::fs::remove_file(zfile_path).unwrap();
        let encoded = crate::snapshot::format::overlaybd::zfile::encode_with_reader(
            blob_plan.stored_bytes,
            &zfile_output,
            |offset, count| blob_plan.read_at(&File::open("/dev/null").unwrap(), offset, count),
        )
        .unwrap();
        let zfile = Arc::new(encoded.index);
        let zfile_blob = ObjectRef {
            object_key: format!("{prefix}data/{}.commit", &encoded.sha256[7..]),
            sha256: encoded.sha256,
            object_version: None,
        };
        let zfile_index = ImmutableObject::new(
            format!("{prefix}overlaybd-zfile-lz4.json"),
            encode_zfile_descriptor(&layer, &blob_plan, &zfile_blob, &zfile).unwrap(),
        )
        .unwrap();
        let zfile_committed = ImmutableObject::new(
            format!("{prefix}COMMITTED.overlaybd-zfile-lz4"),
            canonical_json_bytes(&json!({
                "content_sha256": layer.content_sha256,
                "format_version": 1,
                "range_map_sha256": zfile_index.sha256,
                "kind": layer.kind,
            }))
            .unwrap(),
        )
        .unwrap();
        let zfile_plan = LayerUploadPlan::from_requests(
            layer.kind,
            1,
            layer.content_sha256.clone(),
            vec![DataPackUploadRequest {
                identity: DataPackIdentity {
                    object_key: DataPackObjectKey::new(zfile_blob.object_key).unwrap(),
                    expected_sha256: zfile_blob.sha256,
                    length: zfile.encoded_bytes,
                },
                source: Arc::new(UnreadSource(zfile.encoded_bytes)),
                slices: request.slices.clone(),
                zfile: Some(zfile),
            }],
            zfile_index,
            zfile_committed,
        )
        .unwrap();
        assert_eq!(overlaybd_plan.content_sha256, zfile_plan.content_sha256);
        assert_ne!(
            overlaybd_plan.committed.object_key,
            zfile_plan.committed.object_key
        );
        assert_ne!(overlaybd_plan.committed.sha256, zfile_plan.committed.sha256);
        assert_ne!(
            overlaybd_plan.identity("fixture"),
            zfile_plan.identity("fixture"),
            "same logical bytes in different encodings must not share a layer upload flight"
        );
        for name in ["range-map.json", "range-map.bin"] {
            let mut unsupported = overlaybd_plan.clone();
            unsupported.range_map.object_key = format!("{prefix}{name}");
            assert!(matches!(unsupported.validate(),
                Err(SnapshotUploadError::InvalidPlan(message))
                if message == "layer descriptor must use an explicit OverlayBD encoding"
            ));
        }
        request.identity.length += 512;
        request.source = Arc::new(UnreadSource(request.identity.length));
        assert!(matches!(
            make_plan(vec![request]),
            Err(SnapshotUploadError::InvalidPlan(_))
        ));
    }
}
