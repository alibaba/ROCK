//! Optional snapshot image hints, distinct from immutable publication authority.

pub mod reader;
pub mod uploader;

use std::sync::Arc;

use thiserror::Error;

use crate::snapshot::format::image_metadata::CanonicalImageMetadataV1;
use crate::snapshot::format::{ImageMetadataCodecError, SnapshotImageMetadataCodec};
use crate::snapshot::io::range_io::RangeIO;
use crate::snapshot::transport::MetadataObjectStore;
use crate::snapshot::transport::{BoundedGetError, ObjectStoreError, PublishFailureKind};

pub use reader::SnapshotImageMetadataReader;
pub use uploader::SnapshotImageMetadataUploader;

#[derive(Debug, Error)]
pub enum ImageMetadataError {
    #[error("invalid snapshot image metadata response: {0}")]
    Invalid(String),
    #[error(transparent)]
    Codec(#[from] ImageMetadataCodecError),
    #[error(transparent)]
    Store(#[from] ObjectStoreError),
    #[error(transparent)]
    Read(#[from] BoundedGetError),
}

impl ImageMetadataError {
    /// Malformed optional metadata may be repaired by a later valid profile.
    /// Transport failures are not evidence that the existing object is invalid.
    pub(super) fn is_invalid_hint(&self) -> bool {
        match self {
            Self::Invalid(_)
            | Self::Codec(_)
            | Self::Read(BoundedGetError::BoundExceeded { .. }) => true,
            Self::Read(BoundedGetError::Store(error)) => {
                error.kind == PublishFailureKind::IdentityConflict
            }
            _ => false,
        }
    }
}

#[derive(Debug)]
pub struct ObjectStoreImageMetadata {
    pub(super) range_io: Arc<RangeIO>,
    pub(super) store: Arc<dyn MetadataObjectStore>,
    pub(super) codec: Arc<dyn SnapshotImageMetadataCodec>,
}

impl ObjectStoreImageMetadata {
    pub fn new(store: Arc<dyn MetadataObjectStore>) -> Arc<Self> {
        Self::with_codec(store, Arc::new(CanonicalImageMetadataV1))
    }

    pub fn with_codec(
        store: Arc<dyn MetadataObjectStore>,
        codec: Arc<dyn SnapshotImageMetadataCodec>,
    ) -> Arc<Self> {
        Arc::new(Self {
            store,
            codec,
            range_io: RangeIO::for_fixed_targets(),
        })
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;
    use std::sync::{Arc, Mutex};

    use super::*;
    use crate::snapshot::format::image_metadata::CanonicalImageMetadataV1;
    use crate::snapshot::format::SnapshotImageMetadataCodec;
    use crate::snapshot::metadata::{
        canonical_json_bytes, sha256_identity, snapshot_spec_checksum, ChainIndexRef,
        ManifestChainRef, ObjectRef, SnapshotHotRange, SnapshotHotset, SnapshotImageMetadata,
        SnapshotKind, SnapshotManifest, SnapshotMetadataSubject, SnapshotSpec, StorageDescriptor,
        StorageProvider, VmmCompatibility, MAX_SNAPSHOT_IMAGE_METADATA_BYTES,
    };
    use crate::snapshot::transport::MetadataObjectStore;
    use crate::snapshot::transport::{
        BoundedGetError, ObjectStoreError, PublishFailureKind, RemoteObject, RemoteObjectMetadata,
    };

    #[derive(Debug, Default)]
    struct FakeStore {
        objects: Mutex<HashMap<String, RemoteObject>>,
        reads: Mutex<Vec<String>>,
        writes: Mutex<Vec<String>>,
        read_failure: Mutex<Option<ObjectStoreError>>,
        write_failure: Mutex<Option<ObjectStoreError>>,
    }

    impl FakeStore {
        fn seed(&self, key: &str, payload: Vec<u8>) {
            let metadata = RemoteObjectMetadata {
                size: payload.len() as u64,
                sha256: sha256_identity(&payload),
                version: None,
                last_modified_unix: None,
                etag: None,
            };
            self.objects
                .lock()
                .unwrap()
                .insert(key.into(), RemoteObject { payload, metadata });
        }
    }

    #[async_trait::async_trait]
    impl MetadataObjectStore for FakeStore {
        fn namespace(&self) -> &str {
            "shared-hints"
        }

        async fn get_bounded(
            &self,
            key: &str,
            max: u64,
        ) -> Result<Option<RemoteObject>, BoundedGetError> {
            self.reads.lock().unwrap().push(key.into());
            if let Some(error) = self.read_failure.lock().unwrap().clone() {
                return Err(error.into());
            }
            let object = self.objects.lock().unwrap().get(key).cloned();
            if let Some(value) = &object {
                if value.payload.len() as u64 > max {
                    return Err(BoundedGetError::BoundExceeded {
                        object_key: key.into(),
                        actual_bytes: value.payload.len() as u64,
                        max_bytes: max,
                    });
                }
            }
            Ok(object)
        }

        async fn replace(
            &self,
            key: &str,
            payload: &[u8],
            expected: &str,
        ) -> Result<(), ObjectStoreError> {
            if let Some(error) = self.write_failure.lock().unwrap().clone() {
                return Err(error);
            }
            assert_eq!(sha256_identity(payload), expected);
            assert!(payload.len() as u64 <= MAX_SNAPSHOT_IMAGE_METADATA_BYTES);
            self.seed(key, payload.to_vec());
            self.writes.lock().unwrap().push(key.into());
            Ok(())
        }
    }

    fn subject() -> SnapshotMetadataSubject {
        SnapshotMetadataSubject::new("shared-image", format!("sha256:{}", "a".repeat(64))).unwrap()
    }

    fn profile(subject: &SnapshotMetadataSubject, offset: u64) -> SnapshotImageMetadata {
        SnapshotImageMetadata {
            format_version: 1,
            snapshot_id: subject.snapshot_id.clone(),
            spec_checksum: subject.spec_checksum.clone(),
            hotset: Some(SnapshotHotset {
                ranges: vec![SnapshotHotRange {
                    layer_content_sha256: format!("sha256:{}", "b".repeat(64)),
                    target_offset: offset,
                    length: 4096,
                }],
            }),
        }
    }

    #[tokio::test]
    async fn independent_nodes_share_and_update_only_the_optional_metadata_object() {
        let store = Arc::new(FakeStore::default());
        let node_a = ObjectStoreImageMetadata::new(store.clone());
        let node_b = ObjectStoreImageMetadata::new(store.clone());
        assert!(!Arc::ptr_eq(&node_a, &node_b));
        assert!(node_b.read(&subject()).await.unwrap().is_none());
        let first = profile(&subject(), 0);
        node_a.upload(&first).await.unwrap();
        assert_eq!(node_b.read(&subject()).await.unwrap(), Some(first.clone()));
        let second = profile(&subject(), 4096);
        node_b.upload(&second).await.unwrap();
        let learned = node_a.read(&subject()).await.unwrap().unwrap();
        assert_eq!(
            learned.hotset.unwrap().ranges,
            vec![
                first.hotset.unwrap().ranges[0].clone(),
                second.hotset.unwrap().ranges[0].clone()
            ]
        );
        // Empty/other metadata updates do not discard an already learned hotset.
        node_a
            .upload(&SnapshotImageMetadata {
                hotset: None,
                ..second
            })
            .await
            .unwrap();
        assert_eq!(
            node_b
                .read(&subject())
                .await
                .unwrap()
                .unwrap()
                .hotset
                .unwrap()
                .ranges
                .len(),
            2
        );
        let key = CanonicalImageMetadataV1.object_key(&subject()).unwrap();
        assert!(store.reads.lock().unwrap().iter().all(|seen| seen == &key));
        assert!(store.writes.lock().unwrap().iter().all(|seen| seen == &key));
        assert_eq!(store.objects.lock().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn reader_rejects_corrupt_mismatched_oversized_and_incoherent_hints() {
        let store = Arc::new(FakeStore::default());
        let client = ObjectStoreImageMetadata::new(store.clone());
        let key = CanonicalImageMetadataV1.object_key(&subject()).unwrap();
        store.seed(&key, b"invalid json".to_vec());
        assert!(client.read(&subject()).await.is_err());
        let another = SnapshotMetadataSubject::new("other-image", subject().spec_checksum).unwrap();
        store.seed(
            &key,
            CanonicalImageMetadataV1
                .encode(&profile(&another, 0))
                .unwrap(),
        );
        assert!(client.read(&subject()).await.is_err());
        store.seed(
            &key,
            vec![b' '; MAX_SNAPSHOT_IMAGE_METADATA_BYTES as usize + 1],
        );
        assert!(client.read(&subject()).await.is_err());
        store.seed(
            &key,
            CanonicalImageMetadataV1
                .encode(&profile(&subject(), 0))
                .unwrap(),
        );
        store
            .objects
            .lock()
            .unwrap()
            .get_mut(&key)
            .unwrap()
            .metadata
            .sha256 = sha256_identity(b"wrong body");
        assert!(client.read(&subject()).await.is_err());
        store.seed(
            &key,
            CanonicalImageMetadataV1
                .encode(&profile(&subject(), 0))
                .unwrap(),
        );
        store
            .objects
            .lock()
            .unwrap()
            .get_mut(&key)
            .unwrap()
            .metadata
            .size += 1;
        assert!(client.read(&subject()).await.is_err());
    }

    #[tokio::test]
    async fn uploader_repairs_bad_hints_but_preserves_remote_data_on_transport_failure() {
        let store = Arc::new(FakeStore::default());
        let client = ObjectStoreImageMetadata::new(store.clone());
        let key = CanonicalImageMetadataV1.object_key(&subject()).unwrap();
        store.seed(&key, b"bad old hint".to_vec());
        client.upload(&profile(&subject(), 0)).await.unwrap();
        assert!(client.read(&subject()).await.unwrap().is_some());
        store.seed(
            &key,
            vec![b' '; MAX_SNAPSHOT_IMAGE_METADATA_BYTES as usize + 1],
        );
        client.upload(&profile(&subject(), 0)).await.unwrap();
        *store.read_failure.lock().unwrap() = Some(ObjectStoreError::new(
            PublishFailureKind::IdentityConflict,
            "missing hint integrity header",
        ));
        client.upload(&profile(&subject(), 0)).await.unwrap();
        *store.read_failure.lock().unwrap() = None;
        let previous = store
            .objects
            .lock()
            .unwrap()
            .get(&key)
            .unwrap()
            .payload
            .clone();
        let writes = store.writes.lock().unwrap().len();
        *store.read_failure.lock().unwrap() = Some(ObjectStoreError::new(
            PublishFailureKind::Retryable,
            "read timeout",
        ));
        assert!(client.upload(&profile(&subject(), 4096)).await.is_err());
        assert_eq!(store.writes.lock().unwrap().len(), writes);
        assert_eq!(
            store.objects.lock().unwrap().get(&key).unwrap().payload,
            previous
        );
        *store.read_failure.lock().unwrap() = Some(ObjectStoreError::new(
            PublishFailureKind::Fatal,
            "read permission denied",
        ));
        assert!(client.upload(&profile(&subject(), 4096)).await.is_err());
        assert_eq!(store.writes.lock().unwrap().len(), writes);
        *store.read_failure.lock().unwrap() = None;
        *store.write_failure.lock().unwrap() = Some(ObjectStoreError::new(
            PublishFailureKind::Fatal,
            "permission denied",
        ));
        assert!(client.upload(&profile(&subject(), 4096)).await.is_err());
        assert_eq!(
            store.objects.lock().unwrap().get(&key).unwrap().payload,
            previous
        );
    }

    #[tokio::test]
    async fn learning_image_metadata_keeps_core_snapshot_and_manifest_hashes_unchanged() {
        let chain_sha = format!("sha256:{}", "b".repeat(64));
        let manifest = SnapshotManifest {
            format_version: 1,
            snapshot_id: "shared-image".into(),
            snapshot_kind: SnapshotKind::BaseImage,
            vm_state: None,
            memory: None,
            rootfs: ManifestChainRef {
                object_key: "snapshots/shared-image/rootfs.index".into(),
                sha256: chain_sha.clone(),
                logical_bytes: 65536,
                object_version: None,
            },
        };
        let manifest_bytes = canonical_json_bytes(&manifest).unwrap();
        let spec = SnapshotSpec {
            registry_snapshot: None,
            format_version: 1,
            compatibility: VmmCompatibility {
                architecture: "x86_64".into(),
                vmm_type: "any".into(),
                vmm_state_format_version: None,
                rocklet_artifact: None,
            },
            provenance: None,
            storage: StorageDescriptor {
                provider: StorageProvider::S3,
                endpoint_profile: "test".into(),
                credential_ref: "test-creds".into(),
            },
            manifest: ObjectRef {
                object_key: "snapshots/shared-image/manifest.json".into(),
                sha256: sha256_identity(&manifest_bytes),
                object_version: None,
            },
            vm_state: None,
            memory: None,
            rootfs: ChainIndexRef {
                logical_bytes: 65536,
                index_object_key: "snapshots/shared-image/rootfs.index".into(),
                index_sha256: chain_sha,
                object_version: None,
                source_ref: None,
            },
        };
        let spec_value = serde_json::to_value(&spec).unwrap();
        let checksum = snapshot_spec_checksum(&spec_value).unwrap();
        let subject = SnapshotMetadataSubject::new("shared-image", checksum.clone()).unwrap();
        let store = Arc::new(FakeStore::default());
        let node_a = ObjectStoreImageMetadata::new(store.clone());
        let node_b = ObjectStoreImageMetadata::new(store.clone());
        node_a.upload(&profile(&subject, 0)).await.unwrap();
        node_b.upload(&profile(&subject, 4096)).await.unwrap();
        assert_eq!(
            snapshot_spec_checksum(&serde_json::to_value(&spec).unwrap()).unwrap(),
            checksum
        );
        assert_eq!(canonical_json_bytes(&manifest).unwrap(), manifest_bytes);
        assert!(!serde_json::to_value(&spec)
            .unwrap()
            .as_object()
            .unwrap()
            .contains_key("hotset"));
        assert!(!serde_json::to_value(&manifest)
            .unwrap()
            .as_object()
            .unwrap()
            .contains_key("hotset"));
        assert!(store
            .writes
            .lock()
            .unwrap()
            .iter()
            .all(|key| key.ends_with("/image-metadata-v1.json")));
    }
}
