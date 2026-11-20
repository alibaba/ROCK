//! Format-owned logical coverage, authenticated read planning and decoding.

use crate::snapshot::format::{LayerMetadataInterface, LayerViewInterface};
#[cfg(test)]
use crate::snapshot::io::range_io::RangeReader;
use crate::snapshot::io::range_io::RangeRepresentation;
use crate::snapshot::io::range_io::{RangeDataKind, RangeReadRequest, RangeReadTransport};
use crate::snapshot::metadata::*;
use crate::snapshot::transport::{PhysicalRangeRequest, RangeReadError};
use crate::snapshot::LazyLoadInterface;
use async_trait::async_trait;
use sha2::{Digest, Sha256};
use std::sync::Arc;

fn same_identity(left: &str, right: &str) -> bool {
    left.trim_start_matches("sha256:") == right.trim_start_matches("sha256:")
}

#[derive(Debug)]
pub(crate) struct MappedLayerReader {
    pub(crate) map: Arc<LayerRangeMap>,
    pub(crate) format_name: &'static str,
}

/// Entry point for a verified common layer; selection stays in the format
/// module while callers retain an opaque lazy-load capability.
pub struct LayerReadCapability;

impl LayerReadCapability {
    /// Read a verified common mapping without inventing its serialized codec.
    pub(crate) fn new(map: Arc<LayerRangeMap>) -> Arc<dyn LayerViewInterface> {
        Arc::new(MappedLayerReader {
            map,
            format_name: "mapped",
        })
    }

    pub(crate) fn for_encoding(
        map: Arc<LayerRangeMap>,
        encoding: RangeMapEncoding,
    ) -> Arc<dyn LayerViewInterface> {
        crate::snapshot::format::format_for_encoding(encoding).open_reader(map)
    }
}

impl LayerMetadataInterface for MappedLayerReader {
    fn format_name(&self) -> &'static str {
        self.format_name
    }
    fn layer_kind(&self) -> LayerKind {
        self.map.kind
    }
    fn content_identity(&self) -> &str {
        &self.map.content_sha256
    }
    fn logical_bytes(&self) -> u64 {
        self.map.logical_bytes
    }
    fn unit_size_bytes(&self) -> u64 {
        self.map.unit_size_bytes
    }
    fn allows_p2p(&self) -> bool {
        self.map.registry.is_none() && self.map.registry_objects.is_none()
    }
    fn allows_background_prefetch(&self) -> bool {
        self.map.registry.is_none()
    }
}

#[async_trait]
impl LazyLoadInterface for MappedLayerReader {
    async fn read_data_range(
        &self,
        context: &LazyLoadContext,
        request: SnapshotReadRange<'_>,
    ) -> Result<LazyReadResult, LazyLoadError> {
        let scoped;
        let context = if let Some(objects) = &self.map.registry_objects {
            scoped = LazyLoadContext {
                provider: Arc::clone(objects)
                    as Arc<dyn crate::snapshot::io::range_io::RangeReader>,
                runtime: context.runtime.clone(),
                p2p_source: None,
                provider_timeout: context.provider_timeout,
            };
            &scoped
        } else {
            context
        };
        if request.kind != self.map.kind
            || !same_identity(request.layer_content_sha256, &self.map.content_sha256)
        {
            return Err(LazyLoadError::IdentityConflict);
        }
        let requested_bytes =
            usize::try_from(request.length).map_err(|_| LazyLoadError::InvalidRange)?;
        let end = request
            .logical_offset
            .checked_add(request.length)
            .filter(|end| {
                request.length > 0
                    && request.length <= MAX_DATA_RANGE_BYTES
                    && *end <= self.map.logical_bytes
            })
            .ok_or(LazyLoadError::InvalidRange)?;
        let first = self.map.ranges.partition_point(|r| {
            r.logical_offset.saturating_add(r.length) <= request.logical_offset
        });
        let mut cursor = request.logical_offset;
        let mut selected = Vec::new();
        for range in &self.map.ranges[first..] {
            if cursor >= end {
                break;
            }
            if range.logical_offset > cursor || range.kind != RangeKind::Data {
                return Err(LazyLoadError::NonDataRange);
            }
            let source = range
                .source
                .as_ref()
                .ok_or(LazyLoadError::IdentityConflict)?;
            if source.decoded_length != range.length || source.encoded_length != range.length {
                return Err(LazyLoadError::UnsupportedCodec);
            }
            selected.push(range);
            cursor = range
                .logical_offset
                .checked_add(range.length)
                .ok_or(LazyLoadError::InvalidRange)?;
        }
        if cursor < end || selected.is_empty() {
            return Err(LazyLoadError::InvalidRange);
        }
        let provider_only = match &context.p2p_source {
            None => true,
            Some(peer) => {
                context
                    .provider
                    .allows_provider_coalescing(&context.runtime, peer)
                    .await
            }
        };
        let mut result = LazyReadResult {
            payload: Vec::new(),
            accounting: LazyReadAccounting::default(),
        };
        let mut at = 0;
        while at < selected.len() {
            let mut stop = at + 1;
            if provider_only {
                while stop < selected.len() {
                    let left = selected[stop - 1].source.as_ref().unwrap();
                    let right = selected[stop].source.as_ref().unwrap();
                    if left.object_key != right.object_key
                        || left.object_sha256 != right.object_sha256
                        || left.object_version != right.object_version
                        || left.codec != right.codec
                        || left.pack_offset.checked_add(left.encoded_length)
                            != Some(right.pack_offset)
                        || selected[stop - 1].logical_offset + selected[stop - 1].length
                            != selected[stop].logical_offset
                        || selected[stop]
                            .logical_offset
                            .checked_add(selected[stop].length)
                            .and_then(|end| end.checked_sub(selected[at].logical_offset))
                            .is_none_or(|length| length > MAX_DATA_RANGE_BYTES)
                    {
                        break;
                    }
                    stop += 1;
                }
            }
            let ranges = &selected[at..stop];
            let (payload, accounting) = self.read_group(context, ranges).await?;
            let begin = request.logical_offset.max(ranges[0].logical_offset);
            let finish =
                end.min(ranges.last().unwrap().logical_offset + ranges.last().unwrap().length);
            let offset = (begin - ranges[0].logical_offset) as usize;
            let count = (finish - begin) as usize;
            if at == 0 && stop == selected.len() && offset == 0 && count == payload.len() {
                // The common complete contiguous read returns the provider/cache
                // allocation itself, including after a cache write-through.
                return Ok(LazyReadResult {
                    payload,
                    accounting,
                });
            }
            if result.payload.is_empty() {
                result.payload.reserve_exact(requested_bytes);
            }
            result.payload.extend_from_slice(
                payload
                    .get(offset..offset + count)
                    .ok_or(LazyLoadError::ShortRead)?,
            );
            result.accounting.cache_bytes += accounting.cache_bytes;
            result.accounting.provider_bytes += accounting.provider_bytes;
            result.accounting.p2p_bytes += accounting.p2p_bytes;
            result.accounting.registry_logical_bytes += accounting.registry_logical_bytes;
            at = stop;
        }
        Ok(result)
    }
}

impl MappedLayerReader {
    async fn read_group(
        &self,
        context: &LazyLoadContext,
        ranges: &[&crate::snapshot::metadata::LayerRange],
    ) -> Result<(Vec<u8>, LazyReadAccounting), LazyLoadError> {
        let first = ranges[0]
            .source
            .as_ref()
            .ok_or(LazyLoadError::IdentityConflict)?;
        let length = ranges
            .iter()
            .try_fold(0_u64, |sum, range| sum.checked_add(range.length))
            .filter(|length| *length > 0 && *length <= MAX_DATA_RANGE_BYTES)
            .ok_or(LazyLoadError::InvalidRange)?;
        usize::try_from(length).map_err(|_| LazyLoadError::InvalidRange)?;
        let external = self.map.registry.is_some();
        if (external
            && (self.map.kind != LayerKind::Block
                || self.map.zfile.is_some()
                || context.p2p_source.is_some()
                || ranges.iter().any(|range| {
                    range.source.as_ref().unwrap().codec != DataCodec::RegistryRootfs
                })))
            || (!external
                && ranges
                    .iter()
                    .any(|range| range.source.as_ref().unwrap().codec != DataCodec::Raw))
        {
            return Err(LazyLoadError::UnsupportedCodec);
        }
        if let Some(rootfs) = &self.map.registry {
            let invalid = |message| {
                LazyLoadError::Provider(RangeReadError::failure(
                    crate::snapshot::transport::RangeReadFailureClass::Integrity,
                    message,
                ))
            };
            let key = rootfs.logical_object_key().map_err(&invalid)?;
            let identity = rootfs.content_sha256().map_err(&invalid)?;
            if ranges.iter().any(|range| {
                let source = range.source.as_ref().unwrap();
                source.object_key != key
                    || !same_identity(&source.object_sha256, &identity)
                    || source.object_version.is_some()
                    || !source.checksum_sha256.is_empty()
            }) {
                return Err(invalid(
                    "Registry DATA request conflicts with its source capability".into(),
                ));
            }
        }
        if let Some(rootfs) = &self.map.registry {
            let payload = rootfs
                .read_logical_ranges(
                    context.provider.as_ref(),
                    &context.runtime,
                    first.pack_offset,
                    length,
                    context.provider_timeout,
                )
                .await
                .map_err(registry_read_error)?;
            if payload.len() as u64 != length {
                return Err(LazyLoadError::ShortRead);
            }
            return Ok((
                payload,
                LazyReadAccounting {
                    registry_logical_bytes: length,
                    ..Default::default()
                },
            ));
        }
        let span = self
            .map
            .zfile
            .as_ref()
            .map(|index| index.compressed_span(first.pack_offset, length))
            .transpose()
            .map_err(|_| LazyLoadError::InvalidRange)?;
        let request = RangeReadRequest {
            runtime: &context.runtime,
            kind: match self.map.kind {
                LayerKind::Memory => RangeDataKind::Memory,
                LayerKind::Block => RangeDataKind::Block,
            },
            object_key: &first.object_key,
            object_sha256: &first.object_sha256,
            expected_range_sha256: if ranges.len() == 1 {
                first.checksum_sha256.trim_start_matches("sha256:")
            } else {
                ""
            },
            offset: first.pack_offset,
            length,
            object_version: first.object_version.as_deref(),
            preferred_p2p_source: context.p2p_source.as_ref(),
            physical: span.as_ref().map(|span| PhysicalRangeRequest {
                namespace: None,
                object_size: None,
                object_key: &first.object_key,
                object_sha256: &first.object_sha256,
                object_version: first.object_version.as_deref(),
                offset: span.offset,
                length: span.length,
            }),
            authority: None,
        };
        for retry in 0..=1 {
            let read = if retry == 0 {
                context
                    .provider
                    .read_range_with_timeout(request, context.provider_timeout)
                    .await
            } else {
                context
                    .provider
                    .read_range_uncached_with_timeout(request, context.provider_timeout)
                    .await
            }?;
            let peer = read.transport == RangeReadTransport::P2P;
            let cache_hit = read.physical_cache_hit;
            let physical_bytes = read.payload.len() as u64;
            let checked = async {
                let payload = match (read.transport, read.representation, span.clone()) {
                    (RangeReadTransport::P2P, RangeRepresentation::DecodedData, _) => read.payload,
                    (
                        RangeReadTransport::Provider,
                        RangeRepresentation::EncodedObject,
                        Some(span),
                    ) => {
                        if read.payload.len() as u64 != span.length {
                            return Err(LazyLoadError::ShortRead);
                        }
                        tokio::task::spawn_blocking(move || span.decode(&read.payload))
                            .await
                            .map_err(|error| {
                                LazyLoadError::Provider(RangeReadError::failure(
                                    crate::snapshot::transport::RangeReadFailureClass::Transient,
                                    format!("ZFile decompression worker failed: {error}"),
                                ))
                            })?
                            .map_err(|error| {
                                LazyLoadError::Provider(RangeReadError::failure(
                                    crate::snapshot::transport::RangeReadFailureClass::Integrity,
                                    format!("ZFile range failed CRC or decompression: {error}"),
                                ))
                            })?
                    }
                    (RangeReadTransport::Provider, RangeRepresentation::DecodedData, None) => {
                        read.payload
                    }
                    _ => return Err(LazyLoadError::Provider(RangeReadError::failure(
                        crate::snapshot::transport::RangeReadFailureClass::Integrity,
                        "source returned a representation that conflicts with the format read plan",
                    ))),
                };
                if payload.len() as u64 != length {
                    return Err(LazyLoadError::ShortRead);
                }
                let mut cursor = 0;
                for range in ranges {
                    let source = range.source.as_ref().unwrap();
                    let stop = cursor + range.length as usize;
                    if format!("{:x}", Sha256::digest(&payload[cursor..stop]))
                        != source
                            .checksum_sha256
                            .trim_start_matches("sha256:")
                            .to_ascii_lowercase()
                    {
                        return Err(LazyLoadError::ChecksumMismatch);
                    }
                    cursor = stop;
                }
                Ok(payload)
            }
            .await;
            match checked {
                Ok(payload) => {
                    return Ok((
                        payload,
                        LazyReadAccounting {
                            cache_bytes: if cache_hit { length } else { 0 },
                            provider_bytes: if !peer && !cache_hit {
                                physical_bytes
                            } else {
                                0
                            },
                            p2p_bytes: if peer { length } else { 0 },
                            ..Default::default()
                        },
                    ));
                }
                Err(error) => {
                    // Decoded peer bytes never enter or invalidate a physical
                    // provider cache. A poisoned physical span is removed even
                    // when the single uncached repair attempt also fails.
                    let integrity_error = matches!(
                        &error,
                        LazyLoadError::ChecksumMismatch | LazyLoadError::ShortRead
                    ) || matches!(
                        &error,
                        LazyLoadError::Provider(RangeReadError::Failure {
                            class: crate::snapshot::transport::RangeReadFailureClass::Integrity,
                            ..
                        })
                    );
                    let repair = !peer
                        && integrity_error
                        && context
                            .provider
                            .invalidate_range(request)
                            .await
                            .unwrap_or(false);
                    if retry == 0 && repair {
                        continue;
                    }
                    return Err(error);
                }
            }
        }
        unreachable!("physical cache repair is bounded to one retry")
    }
}

fn registry_read_error(
    error: crate::snapshot::format::overlaybd::composite::RegistryRootfsReadError,
) -> RangeReadError {
    use crate::snapshot::format::overlaybd::composite::RegistryRootfsReadError;
    use crate::snapshot::transport::{BlobRangeErrorKind, RangeReadFailureClass};
    match error {
        RegistryRootfsReadError::Source(error) => {
            let class = match error.kind {
                BlobRangeErrorKind::Authorization => RangeReadFailureClass::Authorization,
                BlobRangeErrorKind::Integrity => RangeReadFailureClass::Integrity,
                BlobRangeErrorKind::Transient => RangeReadFailureClass::Transient,
                BlobRangeErrorKind::Invalid | BlobRangeErrorKind::Permanent => {
                    RangeReadFailureClass::Permanent
                }
            };
            RangeReadError::failure(class, error.to_string())
        }
        RegistryRootfsReadError::Invalid(message) => {
            RangeReadError::failure(RangeReadFailureClass::Permanent, message)
        }
        RegistryRootfsReadError::Integrity(message) => {
            RangeReadError::failure(RangeReadFailureClass::Integrity, message)
        }
    }
}

#[cfg(test)]
mod registry_format_tests {
    use super::*;
    use crate::snapshot::format::overlaybd::composite::RegistryRootfsReadError;
    use crate::snapshot::transport::registry::RegistryClient;
    use crate::snapshot::transport::{BlobRangeError, BlobRangeErrorKind, RangeReadFailureClass};
    fn source_error(error: BlobRangeError) -> RangeReadError {
        registry_read_error(RegistryRootfsReadError::Source(error))
    }
    use std::sync::atomic::Ordering;

    use crate::snapshot::format::overlaybd::composite::{test_registry_blob, RegistryRootfs};
    use crate::snapshot::metadata::sha256_identity;

    #[tokio::test]
    async fn real_http_import_reads_metadata_then_crc_checked_logical_ranges() {
        use crate::snapshot::io::range_io::RangeIO;
        use crate::snapshot::transport::posix_file::LocalFileCache;
        use hyper::service::{make_service_fn, service_fn};
        use hyper::{Body, Response, Server};
        use std::convert::Infallible;
        use std::sync::atomic::{AtomicU8, AtomicUsize};

        struct CacheRoot(std::path::PathBuf);
        impl Drop for CacheRoot {
            fn drop(&mut self) {
                let _ = std::fs::remove_dir_all(&self.0);
            }
        }
        let cache_root = CacheRoot(
            std::fs::canonicalize(std::env::temp_dir())
                .unwrap()
                .join(format!("registry-http-cache-{}", uuid::Uuid::new_v4())),
        );
        let cache = LocalFileCache::new(cache_root.0.clone(), "registry-http").unwrap();

        let (entry, blob) = test_registry_blob();
        let blob = Arc::new(blob);
        let transferred = Arc::new(AtomicUsize::new(0));
        let mode = Arc::new(AtomicU8::new(0));
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let origin = format!("http://{}", listener.local_addr().unwrap());
        let client = RegistryClient::test_http_client(&origin);
        let authority = client.registry().to_owned();
        let expected_path = format!("/v2/images/rootfs/blobs/{}", entry.digest);
        let make_service = make_service_fn({
            let blob = Arc::clone(&blob);
            let transferred = Arc::clone(&transferred);
            let mode = Arc::clone(&mode);
            move |_| {
                let blob = Arc::clone(&blob);
                let transferred = Arc::clone(&transferred);
                let mode = Arc::clone(&mode);
                let expected_path = expected_path.clone();
                async move {
                    Ok::<_, Infallible>(service_fn(move |request: hyper::Request<Body>| {
                        let blob = Arc::clone(&blob);
                        let transferred = Arc::clone(&transferred);
                        let mode = Arc::clone(&mode);
                        let expected_path = expected_path.clone();
                        async move {
                            let response = if request.uri().path() != expected_path {
                                Response::builder().status(404).body(Body::empty()).unwrap()
                            } else if mode.load(Ordering::Relaxed) == 2 {
                                Response::builder().status(403).body(Body::empty()).unwrap()
                            } else {
                                let range =
                                    request.headers().get("range").unwrap().to_str().unwrap();
                                let (begin, end) = range
                                    .strip_prefix("bytes=")
                                    .unwrap()
                                    .split_once('-')
                                    .unwrap();
                                let begin: usize = begin.parse().unwrap();
                                let end: usize = end.parse().unwrap();
                                let mut payload = blob[begin..=end].to_vec();
                                if mode.load(Ordering::Relaxed) == 1 {
                                    payload[0] ^= 1;
                                }
                                transferred.fetch_add(payload.len(), Ordering::Relaxed);
                                Response::builder()
                                    .status(206)
                                    .header(
                                        "content-range",
                                        format!("bytes {begin}-{end}/{}", blob.len()),
                                    )
                                    .header("content-length", payload.len())
                                    .body(Body::from(payload))
                                    .unwrap()
                            };
                            Ok::<_, Infallible>(response)
                        }
                    }))
                }
            }
        });
        let server = tokio::spawn(Server::from_tcp(listener).unwrap().serve(make_service));
        let ranges = RangeIO::new_with_cache(client.clone(), None, Some(cache.clone()));
        let runtime = crate::RuntimeIdentity::new("registry-http-plan");
        let manifest = sha256_identity(b"http manifest");
        let rootfs = RegistryRootfs::import_typed(
            ranges.as_ref(),
            &authority,
            "images/rootfs",
            &manifest,
            "linux/amd64",
            std::slice::from_ref(&entry),
        )
        .await
        .unwrap();
        assert!(
            transferred.load(Ordering::Relaxed) < blob.len() / 2,
            "metadata import must not scan all blob DATA"
        );
        let before_data = transferred.load(Ordering::SeqCst);
        let bytes = rootfs
            .read_logical_ranges(
                ranges.as_ref(),
                &runtime,
                4 * 1024 * 1024 - 512,
                1024,
                std::time::Duration::from_secs(30),
            )
            .await
            .unwrap();
        assert_eq!(&bytes[..512], &[0x61; 512]);
        assert_eq!(&bytes[512..], &[0x62; 512]);
        let after_cold = transferred.load(Ordering::SeqCst);
        assert!(
            after_cold > before_data,
            "cold DATA must issue HTTP Range requests"
        );
        let warm = rootfs
            .read_logical_ranges(
                ranges.as_ref(),
                &runtime,
                4 * 1024 * 1024 - 512,
                1024,
                std::time::Duration::from_secs(30),
            )
            .await
            .unwrap();
        assert_eq!(warm, bytes);
        assert_eq!(
            transferred.load(Ordering::SeqCst),
            after_cold,
            "the second native Registry read must hit the common physical cache"
        );

        drop(ranges);
        drop(cache);
        let cache = LocalFileCache::new(cache_root.0.clone(), "registry-http").unwrap();
        let ranges = RangeIO::new_with_cache(client.clone(), None, Some(cache));
        let reopened = RegistryRootfs::import_typed(
            ranges.as_ref(),
            &authority,
            "images/rootfs",
            &manifest,
            "linux/amd64",
            std::slice::from_ref(&entry),
        )
        .await
        .unwrap();
        assert_eq!(
            reopened.content_sha256().unwrap(),
            rootfs.content_sha256().unwrap()
        );
        let restarted = reopened
            .read_logical_ranges(
                ranges.as_ref(),
                &runtime,
                4 * 1024 * 1024 - 512,
                1024,
                std::time::Duration::from_secs(30),
            )
            .await
            .unwrap();
        assert_eq!(restarted, bytes);
        assert_eq!(
            transferred.load(Ordering::SeqCst),
            after_cold,
            "native metadata and DATA must reuse physical entries after restart"
        );

        // Keep the original authoritative CRC and HTTP authorization checks
        // independent of the warm cache above.
        let ranges = RangeIO::new(client, None);
        mode.store(1, Ordering::Relaxed);
        assert!(matches!(
            rootfs
                .read_logical_ranges(
                    ranges.as_ref(),
                    &runtime,
                    0,
                    512,
                    std::time::Duration::from_secs(30)
                )
                .await,
            Err(RegistryRootfsReadError::Integrity(_))
        ));
        mode.store(2, Ordering::Relaxed);
        let error = rootfs
            .read_logical_ranges(
                ranges.as_ref(),
                &runtime,
                0,
                512,
                std::time::Duration::from_secs(30),
            )
            .await
            .unwrap_err();
        assert!(matches!(error, RegistryRootfsReadError::Source(error)
            if error.kind == BlobRangeErrorKind::Authorization));
        server.abort();
        let _ = server.await;
    }

    #[test]
    fn physical_source_error_classes_reach_the_logical_provider_unchanged() {
        for (kind, expected) in [
            (
                BlobRangeErrorKind::Invalid,
                RangeReadFailureClass::Permanent,
            ),
            (
                BlobRangeErrorKind::Authorization,
                RangeReadFailureClass::Authorization,
            ),
            (
                BlobRangeErrorKind::Integrity,
                RangeReadFailureClass::Integrity,
            ),
            (
                BlobRangeErrorKind::Transient,
                RangeReadFailureClass::Transient,
            ),
            (
                BlobRangeErrorKind::Permanent,
                RangeReadFailureClass::Permanent,
            ),
        ] {
            assert!(matches!(
                source_error(BlobRangeError::new(kind, "physical source failure")),
                RangeReadError::Failure { class, .. } if class == expected
            ));
        }
    }
}

#[cfg(test)]
mod zfile_format_tests {
    use super::*;
    use crate::snapshot::io::range_io::{P2PSource, RangeReadResult};
    use crate::RuntimeIdentity;
    use std::fs::File;
    use std::os::unix::fs::FileExt;

    struct PhysicalProvider {
        bytes: Vec<u8>,
        offset: u64,
        length: u64,
    }
    #[async_trait]
    impl RangeReader for PhysicalProvider {
        async fn read_range(
            &self,
            request: RangeReadRequest<'_>,
        ) -> Result<RangeReadResult, RangeReadError> {
            let physical = request
                .physical
                .expect("format must compile an encoded physical plan");
            assert_eq!(request.object_version, Some("physical-version"));
            assert_eq!(
                (physical.offset, physical.length),
                (self.offset, self.length)
            );
            assert_ne!(request.offset, physical.offset);
            Ok(RangeReadResult::encoded(
                self.bytes[physical.offset as usize..(physical.offset + physical.length) as usize]
                    .to_vec(),
            ))
        }
    }

    #[tokio::test]
    async fn boundary_expansion_reads_original_physical_bytes_then_crops_virtual_range() {
        let path = std::env::temp_dir().join(format!("snapd-zfile-plan-{}", uuid::Uuid::new_v4()));
        let output = File::options()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
            .unwrap();
        std::fs::remove_file(path).unwrap();
        let raw = (0..15_000)
            .map(|offset| (offset % 251) as u8)
            .collect::<Vec<_>>();
        let encoded = crate::snapshot::format::overlaybd::zfile::encode_with_reader(
            raw.len() as u64,
            &output,
            |offset, count| Ok(raw[offset as usize..offset as usize + count].to_vec()),
        )
        .unwrap();
        let mut physical = vec![0; encoded.index.encoded_bytes as usize];
        output.read_exact_at(&mut physical, 0).unwrap();
        let offset = 4096 + 123;
        let length = 5000;
        let span = encoded.index.compressed_span(offset, length).unwrap();
        let identity = sha256_identity(&raw);
        let map = Arc::new(LayerRangeMap {
            format_version: 1,
            kind: LayerKind::Memory,
            content_sha256: identity.clone(),
            logical_bytes: length,
            unit_size_bytes: 4096,
            ranges: vec![LayerRange {
                kind: RangeKind::Data,
                logical_offset: 0,
                length,
                source: Some(DataSource {
                    object_key: "encoded.commit".into(),
                    object_sha256: encoded.sha256.clone(),
                    object_version: Some("physical-version".into()),
                    pack_offset: offset,
                    encoded_length: length,
                    decoded_length: length,
                    codec: DataCodec::Raw,
                    checksum_sha256: sha256_identity(
                        &raw[offset as usize..(offset + length) as usize],
                    ),
                }),
            }],
            zfile: Some(Arc::new(encoded.index)),
            registry: None,
            registry_objects: None,
        });
        let provider = Arc::new(PhysicalProvider {
            bytes: physical,
            offset: span.offset,
            length: span.length,
        });
        let peer = P2PSource {
            snapshot_id: "test".into(),
            spec_checksum: "test".into(),
            worker_ip: "127.0.0.1".parse().unwrap(),
        };
        let context =
            LazyLoadContext::new(provider, RuntimeIdentity::new("zfile-plan"), Some(peer));
        let result = LayerReadCapability::new(map)
            .read_data_range(
                &context,
                SnapshotReadRange {
                    kind: LayerKind::Memory,
                    layer_content_sha256: &identity,
                    logical_offset: 0,
                    length,
                },
            )
            .await
            .unwrap();
        assert_eq!(
            result.payload,
            raw[offset as usize..(offset + length) as usize]
        );
        assert_eq!(result.accounting.provider_bytes, span.length);
    }
}
