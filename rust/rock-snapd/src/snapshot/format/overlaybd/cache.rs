//! OverlayBD validation for generated immutable physical objects.
//!
//! Files, pins, atomic publication and LRU belong to the generic LocalFileCache.
//! A generated object must authenticate both standard metadata and every DATA
//! slice before it can become an immutable publication or recovery source.

use std::fs::File;
use std::os::unix::fs::FileExt;
use std::sync::Arc;

use sha2::{Digest, Sha256};

use crate::snapshot::format::overlaybd::zfile::ZfileIndex;
use crate::snapshot::format::overlaybd::{
    decode_descriptor_with_blob, plan_layer, OverlaybdBlobPlan, MAX_BLOB_BYTES,
};
#[cfg(test)]
use crate::snapshot::transport::posix_file::CacheError;
#[cfg(test)]
use crate::snapshot::transport::posix_file::SealValidator;
use crate::snapshot::transport::ImmutableObjectIdentity;
use crate::snapshot::transport::{DataIntegrityFailure, ObjectStoreError, PublishFailureKind};

const HASH_WINDOW: usize = 1024 * 1024;
const DATA_HASH_WINDOW: u64 = 64 * 1024;
const MAX_METADATA: usize = 128 * 1024 * 1024;

#[derive(Debug)]
struct DataSlice {
    logical_offset: u64,
    offset: u64,
    length: u64,
    checksum: [u8; 32],
}

#[derive(Debug)]
pub(crate) struct OverlaybdObjectValidator {
    identity: ImmutableObjectIdentity,
    plan: OverlaybdBlobPlan,
    zfile: Option<Arc<ZfileIndex>>,
    data: Vec<DataSlice>,
}

impl OverlaybdObjectValidator {
    pub(crate) fn from_descriptor(payload: &[u8]) -> Result<Arc<Self>, ObjectStoreError> {
        if payload.len() > MAX_METADATA {
            return invalid("descriptor exceeds metadata bound");
        }
        let (layer, blob, length) = decode_descriptor_with_blob(payload).map_err(invalid_error)?;
        let mut plan = plan_layer(&layer).map_err(invalid_error)?;
        let zfile = layer.zfile.clone();
        if zfile
            .as_ref()
            .map_or(plan.stored_bytes, |index| index.encoded_bytes)
            != length
            || zfile
                .as_ref()
                .is_some_and(|index| index.raw_bytes != plan.stored_bytes)
            || length > MAX_BLOB_BYTES
        {
            return invalid("descriptor blob length is inconsistent");
        }
        let data = layer
            .ranges
            .iter()
            .filter_map(|range| {
                range
                    .source
                    .as_ref()
                    .map(|source| (range.logical_offset, source))
            })
            .map(|(logical_offset, source)| {
                Ok(DataSlice {
                    logical_offset,
                    offset: source.pack_offset,
                    length: source.decoded_length,
                    checksum: decode_checksum(&source.checksum_sha256)?,
                })
            })
            .collect::<Result<Vec<_>, ObjectStoreError>>()?;
        // Keep compact DATA identities rather than the source-reader extents.
        plan.data.clear();
        Ok(Arc::new(Self {
            identity: ImmutableObjectIdentity {
                object_key: blob.object_key,
                sha256: blob.sha256,
                length,
            },
            plan,
            zfile,
            data,
        }))
    }
}

impl OverlaybdObjectValidator {
    #[cfg(test)]
    fn validate_range(&self, offset: u64, payload: &[u8]) -> Result<(), ObjectStoreError> {
        if payload.is_empty()
            || offset
                .checked_add(payload.len() as u64)
                .is_none_or(|end| end > self.identity.length)
        {
            return invalid("seal source physical range exceeds its descriptor");
        }
        let regions = self.zfile.as_ref().map_or_else(
            || metadata_regions(&self.plan),
            |index| index.metadata_regions(),
        );
        if !overlap_matches(regions, offset, payload) {
            return invalid("seal source standard metadata differs from descriptor");
        }
        Ok(())
    }

    fn validate_file(&self, file: &File) -> Result<(), ObjectStoreError> {
        let metadata = file.metadata().map_err(io_error)?;
        if !metadata.is_file() || metadata.len() != self.identity.length {
            return invalid("sealed file length differs from descriptor");
        }
        let physical = self.zfile.as_ref().map_or_else(
            || metadata_regions(&self.plan),
            |index| index.metadata_regions(),
        );
        for (offset, expected) in physical {
            for (part, chunk) in expected.chunks(HASH_WINDOW).enumerate() {
                let mut bytes = vec![0; chunk.len()];
                file.read_exact_at(&mut bytes, offset + (part * HASH_WINDOW) as u64)
                    .map_err(io_error)?;
                if bytes != chunk {
                    return invalid("sealed outer metadata differs from descriptor");
                }
            }
        }
        if let Some(index) = &self.zfile {
            for (offset, expected) in metadata_regions(&self.plan) {
                for (part, chunk) in expected.chunks(HASH_WINDOW).enumerate() {
                    let span = index
                        .compressed_span(offset + (part * HASH_WINDOW) as u64, chunk.len() as u64)
                        .map_err(|error| invalid_error(error.to_string()))?;
                    let mut encoded = vec![0; span.length as usize];
                    file.read_exact_at(&mut encoded, span.offset)
                        .map_err(io_error)?;
                    let decoded = span
                        .decode(&encoded)
                        .map_err(|error| invalid_error(error.to_string()))?;
                    if decoded != chunk {
                        return invalid("sealed inner LSMT metadata differs from descriptor");
                    }
                }
            }
        }
        for slice in &self.data {
            let mut digest = Sha256::new();
            let mut consumed = 0;
            while consumed < slice.length {
                let offset = slice.offset + consumed;
                let mut length = (slice.length - consumed).min(DATA_HASH_WINDOW);
                let bytes = if let Some(index) = &self.zfile {
                    // Decode at most one bounded ZFile block, even when a
                    // DATA range is larger or begins inside a block.
                    let block_size = u64::from(index.block_size);
                    length = length.min(block_size - offset % block_size);
                    let span = index
                        .compressed_span(offset, length)
                        .map_err(|error| invalid_error(error.to_string()))?;
                    let mut encoded = vec![0; span.length as usize];
                    file.read_exact_at(&mut encoded, span.offset)
                        .map_err(io_error)?;
                    span.decode(&encoded)
                        .map_err(|error| invalid_error(error.to_string()))?
                } else {
                    let mut bytes = vec![0; length as usize];
                    file.read_exact_at(&mut bytes, offset).map_err(io_error)?;
                    bytes
                };
                digest.update(&bytes);
                consumed += length;
            }
            let actual: [u8; 32] = digest.finalize().into();
            if actual != slice.checksum {
                let expected = slice
                    .checksum
                    .iter()
                    .map(|byte| format!("{byte:02x}"))
                    .collect::<String>();
                let actual = actual
                    .iter()
                    .map(|byte| format!("{byte:02x}"))
                    .collect::<String>();
                return Err(ObjectStoreError::source_integrity(DataIntegrityFailure {
                    object_key: self.identity.object_key.clone(),
                    logical_offset: slice.logical_offset,
                    object_offset: slice.offset,
                    length: slice.length,
                    expected_sha256: format!("sha256:{expected}"),
                    actual_sha256: format!("sha256:{actual}"),
                }));
            }
        }
        Ok(())
    }
}

impl crate::snapshot::transport::StagedObjectValidator for OverlaybdObjectValidator {
    fn validate_file(&self, file: &File) -> Result<(), ObjectStoreError> {
        self.validate_file(file)
    }
}

#[cfg(test)]
impl SealValidator for OverlaybdObjectValidator {
    fn validate_range(&self, offset: u64, payload: &[u8]) -> Result<(), CacheError> {
        self.validate_range(offset, payload)
            .map_err(|error| CacheError::Invalid(error.to_string()))
    }

    fn validate_file(&self, file: &File) -> Result<(), CacheError> {
        self.validate_file(file)
            .map_err(|error| CacheError::Invalid(error.to_string()))
    }
}

fn decode_checksum(identity: &str) -> Result<[u8; 32], ObjectStoreError> {
    let encoded = identity.strip_prefix("sha256:").unwrap_or(identity);
    if encoded.len() != 64 {
        return invalid("DATA checksum is not SHA-256");
    }
    let mut checksum = [0; 32];
    for (byte, pair) in checksum.iter_mut().zip(encoded.as_bytes().chunks_exact(2)) {
        let pair = std::str::from_utf8(pair).map_err(|_| invalid_error("invalid DATA checksum"))?;
        *byte = u8::from_str_radix(pair, 16).map_err(|_| invalid_error("invalid DATA checksum"))?;
    }
    Ok(checksum)
}

fn invalid_error(message: impl Into<String>) -> ObjectStoreError {
    ObjectStoreError::new(PublishFailureKind::Fatal, message)
}

fn invalid<T>(message: &str) -> Result<T, ObjectStoreError> {
    Err(invalid_error(message))
}

fn io_error(error: std::io::Error) -> ObjectStoreError {
    ObjectStoreError::new(PublishFailureKind::Retryable, error.to_string())
}

fn metadata_regions(plan: &OverlaybdBlobPlan) -> [(u64, &[u8]); 3] {
    [
        (0, &plan.header),
        (plan.index_offset, &plan.index),
        (plan.stored_bytes - plan.trailer.len() as u64, &plan.trailer),
    ]
}

#[cfg(test)]
fn overlap_matches(regions: [(u64, &[u8]); 3], offset: u64, payload: &[u8]) -> bool {
    let Some(end) = offset.checked_add(payload.len() as u64) else {
        return false;
    };
    regions.into_iter().all(|(start, bytes)| {
        let from = offset.max(start);
        let to = end.min(start + bytes.len() as u64);
        from >= to
            || payload[(from - offset) as usize..(to - offset) as usize]
                == bytes[(from - start) as usize..(to - start) as usize]
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::snapshot::format::overlaybd::{encode_descriptor, encode_zfile_descriptor};
    use crate::snapshot::format::reader::LayerReadCapability;
    use crate::snapshot::io::range_io::RangeIO;
    use crate::snapshot::metadata::{
        compute_layer_content_sha256, sha256_identity, DataCodec, DataSource, LayerKind,
        LayerRange, LayerRangeMap, LazyLoadContext, ObjectRef, RangeKind, SnapshotReadRange,
    };
    use crate::snapshot::transport::interfaces::physical_cache::{
        PhysicalCacheKey, PhysicalRangeCache,
    };
    use crate::snapshot::transport::posix_file::LocalFileCache;
    use crate::snapshot::transport::{
        ObjectStoreError, PhysicalRangeReader, PhysicalRangeRequest, RangeReadError,
        ReplayableByteSource,
    };
    use crate::snapshot::ValidateCanonicalModel;
    use std::future::Future;
    use std::os::fd::AsRawFd;
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    use std::path::PathBuf;
    use std::pin::Pin;
    use std::sync::atomic::{AtomicUsize, Ordering};
    #[derive(Clone)]
    struct DataCheck {
        offset: u64,
        length: u64,
        sha256: String,
    }
    struct TestRoot(PathBuf);

    impl TestRoot {
        fn new() -> Self {
            // macOS's /var is a symlink; use its canonical parent so these
            // tests exercise the same strict component walk as Linux.
            let parent = std::fs::canonicalize(std::env::temp_dir()).unwrap();
            let path = parent.join(format!("rock-local-overlaybd-{}", uuid::Uuid::new_v4()));
            std::fs::create_dir(&path).unwrap();
            Self(path)
        }
    }

    impl Drop for TestRoot {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    struct Fixture {
        _root: TestRoot,
        layer: LayerRangeMap,
        plan: OverlaybdBlobPlan,
        blob: Vec<u8>,
        reference: ObjectRef,
        descriptor: Vec<u8>,
        checks: Vec<DataCheck>,
    }

    impl Fixture {
        fn new(seed: u8, version: Option<&str>) -> Self {
            let root = TestRoot::new();
            let mut raw = vec![0; 5 * 4096];
            raw[..4096].fill(seed);
            raw[4096..8192].fill(seed.wrapping_add(1));
            let ranges = [
                RangeKind::Data,
                RangeKind::Data,
                RangeKind::Data,
                RangeKind::Zero,
                RangeKind::Inherit,
            ]
            .into_iter()
            .enumerate()
            .map(|(page, kind)| {
                let checksum = sha256_identity(&raw[page * 4096..(page + 1) * 4096]);
                LayerRange {
                    kind,
                    logical_offset: page as u64 * 4096,
                    length: 4096,
                    source: (kind == RangeKind::Data).then_some(DataSource {
                        object_key: "pending".into(),
                        object_sha256: checksum.clone(),
                        object_version: None,
                        pack_offset: 0,
                        encoded_length: 4096,
                        decoded_length: 4096,
                        codec: DataCodec::Raw,
                        checksum_sha256: checksum,
                    }),
                }
            })
            .collect();
            let mut layer = LayerRangeMap {
                registry_objects: None,
                format_version: 1,
                kind: LayerKind::Memory,
                content_sha256: String::new(),
                logical_bytes: raw.len() as u64,
                unit_size_bytes: 4096,
                ranges,
                registry: None,
                zfile: None,
            };
            layer.content_sha256 = compute_layer_content_sha256(&layer).unwrap();
            layer.validate().unwrap();
            let plan = plan_layer(&layer).unwrap();
            let file = File::options()
                .read(true)
                .write(true)
                .create_new(true)
                .open(root.0.join("native"))
                .unwrap();
            file.write_all_at(&raw, 0).unwrap();
            let blob = plan.read_at(&file, 0, plan.stored_bytes as usize).unwrap();
            let mut fixture = Self {
                _root: root,
                layer,
                plan,
                blob,
                reference: ObjectRef {
                    object_key: String::new(),
                    sha256: String::new(),
                    object_version: None,
                },
                descriptor: Vec::new(),
                checks: Vec::new(),
            };
            fixture.rebind(version);
            fixture
        }

        fn rebind(&mut self, version: Option<&str>) {
            let sha256 = sha256_identity(&self.blob);
            self.reference = ObjectRef {
                object_key: format!(
                    "layers/memory/1/{}/data/{}.commit",
                    &self.layer.content_sha256[7..],
                    &sha256[7..]
                ),
                sha256,
                object_version: version.map(str::to_owned),
            };
            self.descriptor = encode_descriptor(&self.layer, &self.plan, &self.reference).unwrap();
            let (layer, _, _) = decode_descriptor_with_blob(&self.descriptor).unwrap();
            self.checks = layer
                .ranges
                .iter()
                .filter_map(|range| range.source.as_ref())
                .map(|source| DataCheck {
                    offset: source.pack_offset,
                    length: source.encoded_length,
                    sha256: source.checksum_sha256.clone(),
                })
                .collect();
        }

        fn compress(&mut self) {
            let output = File::options()
                .read(true)
                .write(true)
                .create_new(true)
                .open(self._root.0.join("encoded"))
                .unwrap();
            let encoded = crate::snapshot::format::overlaybd::zfile::encode_with_reader(
                self.blob.len() as u64,
                &output,
                |offset, count| Ok(self.blob[offset as usize..offset as usize + count].to_vec()),
            )
            .unwrap();
            self.blob = vec![0; encoded.index.encoded_bytes as usize];
            output.read_exact_at(&mut self.blob, 0).unwrap();
            self.reference.sha256 = encoded.sha256;
            self.reference.object_key = format!(
                "layers/memory/1/{}/data/{}.commit",
                &self.layer.content_sha256[7..],
                &self.reference.sha256[7..]
            );
            self.descriptor =
                encode_zfile_descriptor(&self.layer, &self.plan, &self.reference, &encoded.index)
                    .unwrap();
        }

        fn identity(&self) -> ImmutableObjectIdentity {
            ImmutableObjectIdentity {
                object_key: self.reference.object_key.clone(),
                sha256: self.reference.sha256.clone(),
                length: self.blob.len() as u64,
            }
        }
        fn validator(&self) -> Arc<OverlaybdObjectValidator> {
            OverlaybdObjectValidator::from_descriptor(&self.descriptor).unwrap()
        }
        fn key(&self, cache: &LocalFileCache, offset: u64, length: u64) -> PhysicalCacheKey {
            let mut key = cache
                .physical_key(&self.identity(), offset, length)
                .unwrap();
            key.object_size = None;
            key.object_version = self.reference.object_version.clone();
            key
        }
        fn span(&self, checks: &[DataCheck]) -> (u64, Vec<u8>) {
            let offset = checks[0].offset;
            let length = checks.iter().map(|v| v.length).sum();
            let (layer, _, _) = decode_descriptor_with_blob(&self.descriptor).unwrap();
            let (offset, length) = match layer.zfile {
                Some(index) => {
                    let span = index.compressed_span(offset, length).unwrap();
                    (span.offset, span.length)
                }
                None => (offset, length),
            };
            (
                offset,
                self.blob[offset as usize..(offset + length) as usize].to_vec(),
            )
        }
        async fn write(&self, cache: &LocalFileCache, checks: &[DataCheck]) {
            let (offset, payload) = self.span(checks);
            cache
                .write(
                    &self.key(cache, offset, payload.len() as u64),
                    Arc::new(payload),
                )
                .await
                .unwrap();
        }
        async fn read(&self, cache: Arc<LocalFileCache>, checks: &[DataCheck]) -> Option<Vec<u8>> {
            if checks.is_empty() {
                return None;
            }
            let (mut map, _, _) = decode_descriptor_with_blob(&self.descriptor).unwrap();
            let first = map.ranges.iter().position(|range| {
                range
                    .source
                    .as_ref()
                    .is_some_and(|source| source.pack_offset == checks[0].offset)
            })?;
            let logical_offset = map.ranges[first].logical_offset;
            let length = checks.iter().map(|v| v.length).sum();
            for (check, range) in checks.iter().zip(&mut map.ranges[first..]) {
                let source = range.source.as_mut()?;
                if source.pack_offset != check.offset || range.length != check.length {
                    return None;
                }
                source.checksum_sha256 = check.sha256.clone();
            }
            let provider = Arc::new(CacheOnly {
                source: cache.source_id().into(),
            });
            let io = RangeIO::new_with_cache(provider, None, Some(cache));
            let context = LazyLoadContext::new(
                io,
                crate::RuntimeIdentity::new("physical-format-cache"),
                None,
            );
            LayerReadCapability::new(Arc::new(map))
                .read_data_range(
                    &context,
                    SnapshotReadRange {
                        kind: LayerKind::Memory,
                        layer_content_sha256: &self.layer.content_sha256,
                        logical_offset,
                        length,
                    },
                )
                .await
                .ok()
                .map(|result| result.payload)
        }
    }
    struct CacheOnly {
        source: String,
    }
    #[async_trait::async_trait]
    impl PhysicalRangeReader for CacheOnly {
        fn source_id(&self) -> &str {
            &self.source
        }
        async fn read_physical_range(
            &self,
            _request: PhysicalRangeRequest<'_>,
        ) -> Result<Vec<u8>, RangeReadError> {
            Err(RangeReadError::InvalidRange)
        }
    }
    struct BytesSource {
        bytes: Vec<u8>,
        short_at: Option<u64>,
    }

    impl ReplayableByteSource for BytesSource {
        fn length(&self) -> u64 {
            self.bytes.len() as u64
        }
        fn read_at<'a>(
            &'a self,
            offset: u64,
            count: usize,
        ) -> Pin<Box<dyn Future<Output = Result<Vec<u8>, ObjectStoreError>> + Send + 'a>> {
            Box::pin(async move {
                if self.short_at.is_some_and(|at| offset >= at) {
                    return Ok(Vec::new());
                }
                let start = offset as usize;
                Ok(self.bytes[start..(start + count).min(self.bytes.len())].to_vec())
            })
        }
    }

    fn paths(cache: &LocalFileCache, suffix: &str) -> Vec<PathBuf> {
        let directory = cache.byte_cache().objects_dir.as_ref();
        #[cfg(target_os = "linux")]
        let path = PathBuf::from(format!("/proc/self/fd/{}", directory.as_raw_fd()));
        #[cfg(target_os = "macos")]
        let path = {
            use std::os::unix::ffi::OsStrExt;
            let mut bytes = [0_u8; libc::PATH_MAX as usize];
            assert_eq!(
                unsafe { libc::fcntl(directory.as_raw_fd(), libc::F_GETPATH, bytes.as_mut_ptr()) },
                0
            );
            let len = bytes.iter().position(|b| *b == 0).unwrap();
            PathBuf::from(std::ffi::OsStr::from_bytes(&bytes[..len]))
        };
        std::fs::read_dir(path)
            .unwrap()
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .filter(|path| {
                path.file_name()
                    .unwrap()
                    .to_string_lossy()
                    .ends_with(suffix)
            })
            .collect()
    }
    #[test]
    fn scratch_file_is_anonymous_private_disk_staging() {
        let root = TestRoot::new();
        let cache = LocalFileCache::new(root.0.clone(), "scratch").unwrap();
        let file = cache.create_scratch_file().unwrap();
        assert_eq!(file.metadata().unwrap().nlink(), 0);
        assert_eq!(file.metadata().unwrap().mode() & 0o777, 0o600);
        file.write_all_at(b"bounded disk staging", 1 << 30).unwrap();
        assert!(paths(&cache, ".scratch").is_empty());
    }

    #[tokio::test]
    async fn encoded_partial_restarts_and_reauthenticates_crc_and_every_raw_unit() {
        let root = TestRoot::new();
        let mut fixture = Fixture::new(73, Some("zfile-v1"));
        fixture.compress();
        let cache = LocalFileCache::new(root.0.clone(), "domain").unwrap();
        assert!(fixture
            .read(cache.clone(), &fixture.checks[..2])
            .await
            .is_none());
        fixture.write(&cache, &fixture.checks[..2]).await;
        assert_eq!(
            fixture.read(cache.clone(), &fixture.checks[..2]).await,
            Some([vec![73; 4096], vec![74; 4096]].concat())
        );
        let (offset, mut encoded) = fixture.span(&fixture.checks[..2]);
        encoded[0] ^= 0xff;
        cache
            .write(
                &fixture.key(&cache, offset, encoded.len() as u64),
                Arc::new(encoded),
            )
            .await
            .unwrap();
        assert!(fixture
            .read(cache.clone(), &fixture.checks[..2])
            .await
            .is_none());
        assert!(
            paths(&cache, ".partial").is_empty(),
            "bad CRC span must be explicitly invalidated"
        );
        fixture.write(&cache, &fixture.checks[..2]).await;
        let mut wrong = fixture.checks[..2].to_vec();
        wrong[1].sha256 = "0".repeat(64);
        assert!(
            fixture.read(cache.clone(), &wrong).await.is_none(),
            "valid CRC cannot replace each trusted DATA checksum"
        );
        fixture.write(&cache, &fixture.checks[..2]).await;
        drop(cache);
        let restarted = LocalFileCache::new(root.0.clone(), "domain").unwrap();
        assert!(fixture
            .read(restarted.clone(), &fixture.checks[..2])
            .await
            .is_some());
        assert!(
            fixture
                .read(restarted, &fixture.checks[2..])
                .await
                .is_none(),
            "an uncached zero DATA range is not a cache hit"
        );
    }

    #[tokio::test]
    async fn compressed_complete_seal_keeps_exact_physical_identity_after_restart() {
        let root = TestRoot::new();
        let mut fixture = Fixture::new(75, None);
        fixture.compress();
        let cache = LocalFileCache::new(root.0.clone(), "domain").unwrap();
        let file = cache
            .seal_blob(
                &fixture.identity(),
                Arc::new(BytesSource {
                    bytes: fixture.blob.clone(),
                    short_at: None,
                }),
                113,
                fixture.validator(),
            )
            .await
            .unwrap();
        let mut bytes = vec![0; fixture.blob.len()];
        file.read_exact_at(&mut bytes, 0).unwrap();
        assert_eq!(bytes, fixture.blob);
        assert_eq!(file.metadata().unwrap().mode() & 0o777, 0o400);
        drop(file);
        drop(cache);
        let restarted = LocalFileCache::new(root.0.clone(), "domain").unwrap();
        assert!(fixture
            .read(restarted.clone(), &fixture.checks[..2])
            .await
            .is_some());
        assert_eq!(restarted.stats().unwrap().entries, 1);
    }

    #[tokio::test]
    async fn complete_seal_rejects_changed_data_with_valid_blob_hash_metadata_and_crc() {
        for compressed in [false, true] {
            let root = TestRoot::new();
            let mut valid = Fixture::new(83, None);
            if compressed {
                valid.compress();
            }
            let cache = LocalFileCache::new(root.0.join("valid"), "domain").unwrap();
            cache
                .seal_blob(
                    &valid.identity(),
                    Arc::new(BytesSource {
                        bytes: valid.blob.clone(),
                        short_at: None,
                    }),
                    113,
                    valid.validator(),
                )
                .await
                .unwrap();

            let mut changed = Fixture::new(89, None);
            // Preserve the descriptor's DATA identity but bind its complete
            // physical hash to the changed bytes. Compression recomputes CRCs
            // too, so neither outer SHA nor metadata/CRC checks can detect it.
            let offset = changed.checks[1].offset as usize + 17;
            changed.blob[offset] ^= 1;
            changed.rebind(None);
            if compressed {
                changed.compress();
            }
            let encoded = tempfile::tempfile().unwrap();
            encoded.write_all_at(&changed.blob, 0).unwrap();
            let error = changed.validator().validate_file(&encoded).unwrap_err();
            assert_eq!(error.kind, PublishFailureKind::Fatal);
            let failure = error.data_integrity.unwrap();
            assert_eq!(failure.object_key, changed.reference.object_key);
            assert_eq!(failure.logical_offset, 4096);
            assert_eq!(failure.object_offset, changed.checks[1].offset);
            assert_eq!(failure.length, 4096);
            assert_eq!(failure.expected_sha256, changed.checks[1].sha256);
            assert_ne!(failure.actual_sha256, failure.expected_sha256);
            let cache = LocalFileCache::new(root.0.join("changed"), "domain").unwrap();
            let error = cache
                .seal_blob(
                    &changed.identity(),
                    Arc::new(BytesSource {
                        bytes: changed.blob.clone(),
                        short_at: None,
                    }),
                    113,
                    changed.validator(),
                )
                .await
                .unwrap_err();
            assert!(error.to_string().contains("sealed DATA checksum differs"));
            assert!(paths(&cache, ".commit").is_empty());
            assert!(paths(&cache, ".tmp").is_empty());
        }
    }

    #[tokio::test]
    async fn native_publication_retains_raw_data_when_seal_authentication_fails() {
        use crate::snapshot::metadata::{
            SnapshotProvenance, StorageDescriptor, StorageProvider, VmmCompatibility,
        };
        use crate::snapshot::repository::frozen_layer::{
            FrozenLayerMetadata, FrozenLayerPin, PackedOwnershipMap,
        };

        let root = TestRoot::new();
        let cache = LocalFileCache::new(root.0.clone(), "native-seal-failure").unwrap();
        let backing = cache.create_scratch_file().unwrap();
        backing.set_len(65536).unwrap();
        backing.write_all_at(&vec![0x5a; 65536], 0).unwrap();
        let snapshot = crate::snapshot::Snapshot::from_base_layer(
            "native-seal-failure".into(),
            FrozenLayerPin::new(
                FrozenLayerMetadata {
                    kind: LayerKind::Block,
                    layer_id: "native-seal-failure-block".into(),
                    provisional_content_id: sha256_identity(b"native-seal-failure"),
                    logical_size: 65536,
                    alignment: 65536,
                },
                PackedOwnershipMap {
                    kind: LayerKind::Block,
                    snapshot_id: "native-seal-failure".into(),
                    format_version: 1,
                    logical_size: 65536,
                    granularity: 65536,
                    packed_states: vec![1],
                },
                backing.try_clone().unwrap().into(),
                false,
            ),
            SnapshotProvenance {
                oci_ref: Some("fixture:latest".into()),
                oci_digest: Some(sha256_identity(b"fixture")),
            },
        )
        .unwrap();
        let result = snapshot.upload_plan_with_scratch(
            StorageDescriptor {
                provider: StorageProvider::S3,
                endpoint_profile: "fixture".into(),
                credential_ref: "fixture:v1".into(),
            },
            VmmCompatibility {
                architecture: "x86_64".into(),
                vmm_type: "any".into(),
                vmm_state_format_version: None,
                rocklet_artifact: None,
            },
            &|capacity| {
                // Scratch admission happens after the native checksum pass,
                // before the writer rereads DATA. Model a writable alias.
                backing.write_all_at(&[0x6b], 17).unwrap();
                cache.create_staged_object(capacity).map_err(|error| {
                    crate::snapshot::repository::frozen_layer::FrozenLayerError::Io(
                        error.to_string(),
                    )
                })
            },
        );
        assert!(matches!(
            result,
            Err(crate::snapshot::repository::LocalSnapshotError::SourceIntegrity(_))
        ));
        assert!(snapshot.is_closed().unwrap());
        assert!(!snapshot.native_is_locally_ready());
        let native = snapshot.backing_handle(LayerKind::Block).unwrap();
        assert!(native.backing.reclaim_unused().await.unwrap().is_empty());
        assert!(!native.backing.has_reclaimed_data());
        let mut expected = vec![0x5a; 65536];
        expected[17] = 0x6b;
        assert_eq!(native.backing.read_data(0, 65536).await.unwrap(), expected);
        assert!(paths(&cache, ".commit").is_empty());
    }

    #[tokio::test]
    async fn physical_sha_and_outer_mirrors_cannot_hide_invalid_inner_lsmt_metadata() {
        let root = TestRoot::new();
        let mut fixture = Fixture::new(79, None);
        fixture.blob[0] ^= 0xff;
        fixture.compress();
        let encoded = tempfile::tempfile().unwrap();
        encoded.write_all_at(&fixture.blob, 0).unwrap();
        assert!(fixture
            .validator()
            .validate_file(&encoded)
            .unwrap_err()
            .data_integrity
            .is_none());
        let cache = LocalFileCache::new(root.0.clone(), "domain").unwrap();
        let error = cache
            .seal_blob(
                &fixture.identity(),
                Arc::new(BytesSource {
                    bytes: fixture.blob.clone(),
                    short_at: None,
                }),
                113,
                fixture.validator(),
            )
            .await
            .unwrap_err();
        assert!(matches!(error, CacheError::Invalid(_)));
        assert!(paths(&cache, ".commit").is_empty());
        assert!(paths(&cache, ".tmp").is_empty());
    }

    #[tokio::test]
    async fn partial_restart_authenticates_all_units_and_missing_holes() {
        let root = TestRoot::new();
        let fixture = Fixture::new(31, None);
        let cache = LocalFileCache::new(root.0.clone(), "domain").unwrap();
        fixture.write(&cache, &fixture.checks[..2]).await;
        drop(cache);
        let restarted = LocalFileCache::new(root.0.clone(), "domain").unwrap();
        assert_eq!(
            fixture.read(restarted.clone(), &fixture.checks[..2]).await,
            Some(fixture.blob[4096..12288].to_vec())
        );
        assert!(fixture
            .read(restarted, &fixture.checks[2..])
            .await
            .is_none());
    }

    #[tokio::test]
    async fn corruption_short_files_and_incomplete_checks_miss_or_reject_then_repair() {
        let root = TestRoot::new();
        let fixture = Fixture::new(37, None);
        let cache = LocalFileCache::new(root.0.clone(), "domain").unwrap();
        fixture.write(&cache, &fixture.checks[..2]).await;
        let path = paths(&cache, ".partial").pop().unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        let file = File::options().read(true).write(true).open(&path).unwrap();
        file.set_len(12).unwrap();
        drop(file);
        assert!(fixture
            .read(cache.clone(), &fixture.checks[..2])
            .await
            .is_none());
        fixture.write(&cache, &fixture.checks[..2]).await;
        assert!(fixture.read(cache.clone(), &[]).await.is_none());
        let mut incomplete = fixture.checks[..2].to_vec();
        incomplete[1].offset += 1;
        assert!(fixture.read(cache.clone(), &incomplete).await.is_none());
        assert!(fixture.read(cache, &fixture.checks[..2]).await.is_some());
    }

    #[tokio::test]
    async fn complete_blob_is_exact_standard_bytes_readonly_and_reusable_after_restart() {
        let root = TestRoot::new();
        let fixture = Fixture::new(41, None);
        let cache = LocalFileCache::new(root.0.clone(), "domain").unwrap();
        let file = cache
            .seal_blob(
                &fixture.identity(),
                Arc::new(BytesSource {
                    bytes: fixture.blob.clone(),
                    short_at: None,
                }),
                113,
                fixture.validator(),
            )
            .await
            .unwrap();
        let mut payload = vec![0; fixture.blob.len()];
        file.read_exact_at(&mut payload, 0).unwrap();
        assert_eq!(payload, fixture.blob);
        assert_eq!(file.metadata().unwrap().mode() & 0o777, 0o400);
        assert!(file.write_all_at(b"x", 0).is_err());
        drop(file);
        drop(cache);
        let restarted = LocalFileCache::new(root.0.clone(), "domain").unwrap();
        assert!(fixture
            .read(restarted.clone(), &fixture.checks[..2])
            .await
            .is_some());
        assert_eq!(paths(&restarted, ".commit").len(), 1);
        assert!(paths(&restarted, ".partial").is_empty());
    }

    #[tokio::test]
    async fn short_stream_bad_sha_and_metadata_never_publish_complete_or_leave_temps() {
        let root = TestRoot::new();
        let mut fixture = Fixture::new(39, None);
        let cache = LocalFileCache::new(root.0.clone(), "domain").unwrap();
        assert!(cache
            .seal_blob(
                &fixture.identity(),
                Arc::new(BytesSource {
                    bytes: fixture.blob.clone(),
                    short_at: Some(4096)
                }),
                4096,
                fixture.validator()
            )
            .await
            .is_err());
        let mut bad_data = fixture.blob.clone();
        bad_data[4096] ^= 1;
        assert!(cache
            .seal_blob(
                &fixture.identity(),
                Arc::new(BytesSource {
                    bytes: bad_data,
                    short_at: None
                }),
                4096,
                fixture.validator()
            )
            .await
            .is_err());
        fixture.blob[0] ^= 1;
        fixture.rebind(None);
        assert!(cache
            .seal_blob(
                &fixture.identity(),
                Arc::new(BytesSource {
                    bytes: fixture.blob.clone(),
                    short_at: None
                }),
                4096,
                fixture.validator()
            )
            .await
            .is_err());
        assert!(paths(&cache, ".commit").is_empty());
        assert!(paths(&cache, ".tmp").is_empty());
    }

    #[tokio::test]
    async fn storage_domains_versions_and_metadata_identities_are_isolated() {
        let root = TestRoot::new();
        let mut fixture = Fixture::new(47, Some("v1"));
        let cache = LocalFileCache::new(root.0.clone(), "bucket-one").unwrap();
        fixture.write(&cache, &fixture.checks[..1]).await;
        fixture.rebind(Some("v2"));
        assert!(fixture
            .read(cache.clone(), &fixture.checks[..1])
            .await
            .is_none());
        fixture.rebind(Some("v1"));
        let other = LocalFileCache::new(root.0.clone(), "bucket-two").unwrap();
        assert!(fixture
            .read(other.clone(), &fixture.checks[..1])
            .await
            .is_none());
        let bytes = b"immutable metadata";
        let sha = sha256_identity(bytes);
        cache
            .persist_metadata("snapshots/id/manifest.json", bytes, &sha)
            .unwrap();
        assert_eq!(
            cache
                .read_metadata("snapshots/id/manifest.json", &sha)
                .unwrap(),
            Some(bytes.to_vec())
        );
        assert!(other
            .read_metadata("snapshots/id/manifest.json", &sha)
            .unwrap()
            .is_none());
        assert!(cache.persist_metadata("../escape", bytes, &sha).is_err());
        assert!(cache
            .persist_metadata("safe/key", bytes, &sha256_identity(b"other"))
            .is_err());
    }

    #[tokio::test]
    async fn symlinked_roots_and_entries_are_never_followed() {
        let root = TestRoot::new();
        let sentinel = root.0.join("sentinel");
        std::fs::write(&sentinel, b"unchanged").unwrap();
        let link = root.0.join("linked");
        std::os::unix::fs::symlink(&root.0, &link).unwrap();
        assert!(LocalFileCache::new(link.join("cache"), "domain").is_err());
        let fixture = Fixture::new(51, None);
        let cache = LocalFileCache::new(root.0.join("cache"), "domain").unwrap();
        fixture.write(&cache, &fixture.checks[..1]).await;
        let entry = paths(&cache, ".partial").pop().unwrap();
        std::fs::remove_file(&entry).unwrap();
        std::os::unix::fs::symlink(&sentinel, &entry).unwrap();
        let (offset, payload) = fixture.span(&fixture.checks[..1]);
        let key = fixture.key(&cache, offset, payload.len() as u64);
        assert!(cache.read(&key).await.is_err());
        assert!(cache.write(&key, Arc::new(payload)).await.is_err());
        assert_eq!(std::fs::read(&sentinel).unwrap(), b"unchanged");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn concurrent_partial_writers_and_readers_share_one_object_lock() {
        let root = TestRoot::new();
        let fixture = Arc::new(Fixture::new(57, None));
        let cache = LocalFileCache::new(root.0.clone(), "domain").unwrap();
        let mut jobs = Vec::new();
        for _ in 0..8 {
            let fixture = fixture.clone();
            let cache = cache.clone();
            jobs.push(tokio::spawn(async move {
                for _ in 0..8 {
                    fixture.write(&cache, &fixture.checks[..2]).await;
                    assert!(fixture
                        .read(cache.clone(), &fixture.checks[..2])
                        .await
                        .is_some());
                }
            }));
        }
        for job in jobs {
            job.await.unwrap();
        }
    }
    struct GatedSource {
        bytes: Vec<u8>,
        started: Arc<AtomicUsize>,
        permits: Arc<tokio::sync::Semaphore>,
    }

    impl ReplayableByteSource for GatedSource {
        fn length(&self) -> u64 {
            self.bytes.len() as u64
        }
        fn read_at<'a>(
            &'a self,
            offset: u64,
            count: usize,
        ) -> Pin<Box<dyn Future<Output = Result<Vec<u8>, ObjectStoreError>> + Send + 'a>> {
            Box::pin(async move {
                self.started.fetch_add(1, Ordering::SeqCst);
                self.permits.acquire().await.unwrap().forget();
                let offset = offset as usize;
                Ok(self.bytes[offset..offset + count].to_vec())
            })
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn global_seal_admission_bounds_distinct_objects_and_cancellation_cleans_temp() {
        let root = TestRoot::new();
        let cache = LocalFileCache::new(root.0.clone(), "domain").unwrap();
        let started = Arc::new(AtomicUsize::new(0));
        let permits = Arc::new(tokio::sync::Semaphore::new(0));
        let mut jobs = Vec::new();
        for seed in 60..66 {
            let fixture = Fixture::new(seed, None);
            let identity = fixture.identity();
            let validator = fixture.validator();
            let cache = cache.clone();
            let source = Arc::new(GatedSource {
                bytes: fixture.blob,
                started: started.clone(),
                permits: permits.clone(),
            });
            jobs.push(tokio::spawn(async move {
                cache.seal_blob(&identity, source, 65536, validator).await
            }));
        }
        tokio::time::timeout(std::time::Duration::from_secs(5), async {
            while started.load(Ordering::SeqCst) < 4 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        assert_eq!(started.load(Ordering::SeqCst), 4);
        for job in &jobs {
            job.abort();
        }
        for job in jobs {
            assert!(job.await.unwrap_err().is_cancelled());
        }
        tokio::time::timeout(std::time::Duration::from_secs(5), async {
            while !paths(&cache, ".tmp").is_empty() || cache.stats().unwrap().staging_bytes != 0 {
                tokio::time::sleep(std::time::Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();
        assert!(paths(&cache, ".commit").is_empty());
        assert_eq!(cache.stats().unwrap().used_bytes, 0);
    }

    #[tokio::test]
    async fn dropping_descriptor_validation_metadata_does_not_remove_persistent_objects() {
        let root = TestRoot::new();
        let fixture = Fixture::new(77, Some("v0"));
        let cache = LocalFileCache::new(root.0.clone(), "domain").unwrap();
        let validator = fixture.validator();
        let retained = Arc::downgrade(&validator);
        fixture.write(&cache, &fixture.checks[..1]).await;
        drop(validator);
        assert!(
            retained.upgrade().is_none(),
            "format validation has no persistent descriptor registry"
        );
        for _ in 0..2048 {
            drop(fixture.validator());
        }
        assert!(fixture.read(cache, &fixture.checks[..1]).await.is_some());
    }
}
