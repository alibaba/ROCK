//! S3/MinIO immutable object store and bounded range provider.

use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex as StdMutex};
use std::task::{Context, Poll};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use aws_sdk_s3::error::{ProvideErrorMetadata, SdkError};
use aws_sdk_s3::primitives::{ByteStream, SdkBody};
use aws_sdk_s3::types::{CompletedMultipartUpload, CompletedPart};
use hyper::body::{Bytes, HttpBody, SizeHint};
use sha2::{Digest, Sha256};
use tokio::io::AsyncReadExt;

use crate::snapshot::transport::{
    BoundedGetError, ImmutableObjectIdentity, ImmutableObjectStore, ImmutableStreamWriter,
    IoCancellation, NamespaceMutationFence, ObjectStoreError, PublishFailureKind, RemoteObject,
    RemoteObjectMetadata, ReplayableByteSource, UploadTransactionLease, VerifiedWriteReceipt,
};
use crate::snapshot::transport::{MetadataObjectStore, MAX_METADATA_OBJECT_BYTES};
use crate::snapshot::transport::{PhysicalRangeReader, PhysicalRangeRequest};
use crate::snapshot::transport::{RangeReadError, RangeReadFailureClass};

const S3_MINIMUM_PART_BYTES: usize = 5 * 1024 * 1024;
const MAXIMUM_PART_BYTES: usize = 64 * 1024 * 1024;
const MAXIMUM_STREAM_CHUNK_BYTES: usize = 16 * 1024 * 1024;
const S3_MAXIMUM_PART_BYTES: u64 = 5 * 1024 * 1024 * 1024;
const S3_MAXIMUM_PARTS: u64 = 10_000;
const OSS_FORBID_OVERWRITE_HEADER: &str = "x-oss-forbid-overwrite";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct MultipartLayout {
    part_bytes: u64,
    part_count: u64,
}

fn multipart_layout(
    length: u64,
    configured_part_bytes: usize,
) -> Result<MultipartLayout, ObjectStoreError> {
    if length == 0 {
        return Err(ObjectStoreError::new(
            PublishFailureKind::Fatal,
            "multipart object is empty",
        ));
    }
    let part_bytes = (configured_part_bytes as u64)
        .max(S3_MINIMUM_PART_BYTES as u64)
        .max(length.div_ceil(S3_MAXIMUM_PARTS));
    let part_count = length.div_ceil(part_bytes);
    if part_bytes > S3_MAXIMUM_PART_BYTES || part_count > S3_MAXIMUM_PARTS {
        return Err(ObjectStoreError::new(
            PublishFailureKind::Fatal,
            "object exceeds multipart part size or count limits",
        ));
    }
    Ok(MultipartLayout {
        part_bytes,
        part_count,
    })
}

struct StreamProgress {
    next_offset: u64,
    digest: Sha256,
    failure: Option<ObjectStoreError>,
}

type PendingChunk = Pin<Box<dyn Future<Output = Result<Vec<u8>, ObjectStoreError>> + Send>>;

/// An HTTP part can be much larger than its source-read window. Keep only a
/// single pending window here; Hyper applies network backpressure to its Bytes.
/// SdkBody is intentionally non-retryable: a failed stream is aborted and the
/// existing immutable upload coordinator retries the whole replayable object.
struct StreamingPartBody {
    // Smithy requires a Sync body. The pending source future is Send, so a
    // mutex owns it; HttpBody polling itself still has exclusive mutable access.
    state: StdMutex<StreamingPartState>,
}

struct StreamingPartState {
    source: Arc<dyn ReplayableByteSource>,
    cursor: u64,
    end: u64,
    chunk_bytes: usize,
    deadline: Instant,
    cancellation: IoCancellation,
    progress: Arc<StdMutex<StreamProgress>>,
    pending: Option<(usize, PendingChunk)>,
    failed: bool,
}

impl StreamingPartBody {
    #[allow(clippy::too_many_arguments)]
    fn new(
        source: Arc<dyn ReplayableByteSource>,
        offset: u64,
        length: u64,
        chunk_bytes: usize,
        deadline: Instant,
        cancellation: IoCancellation,
        progress: Arc<StdMutex<StreamProgress>>,
    ) -> Result<Self, ObjectStoreError> {
        let end = offset.checked_add(length).ok_or_else(|| {
            ObjectStoreError::new(
                PublishFailureKind::Fatal,
                "multipart stream offset overflow",
            )
        })?;
        if length == 0
            || end > source.length()
            || !(1..=MAXIMUM_STREAM_CHUNK_BYTES).contains(&chunk_bytes)
        {
            return Err(ObjectStoreError::new(
                PublishFailureKind::Fatal,
                "invalid multipart stream geometry",
            ));
        }
        Ok(Self {
            state: StdMutex::new(StreamingPartState {
                source,
                cursor: offset,
                end,
                chunk_bytes,
                deadline,
                cancellation,
                progress,
                pending: None,
                failed: false,
            }),
        })
    }
}

async fn read_active_chunk(
    source: Arc<dyn ReplayableByteSource>,
    offset: u64,
    requested: usize,
    deadline: Instant,
    cancellation: IoCancellation,
) -> Result<Vec<u8>, ObjectStoreError> {
    ensure_active(deadline, &cancellation)?;
    let read = source.read_at(offset, requested);
    tokio::pin!(read);
    loop {
        tokio::select! {
            result = &mut read => {
                ensure_active(deadline, &cancellation)?;
                return result;
            }
            _ = tokio::time::sleep(Duration::from_millis(50)) => {
                ensure_active(deadline, &cancellation)?;
            }
        }
    }
}

impl HttpBody for StreamingPartBody {
    type Data = Bytes;
    type Error = ObjectStoreError;

    fn poll_data(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Bytes, ObjectStoreError>>> {
        let state = match self.get_mut().state.get_mut() {
            Ok(state) => state,
            Err(_) => {
                return Poll::Ready(Some(Err(ObjectStoreError::new(
                    PublishFailureKind::Fatal,
                    "multipart body state lock poisoned",
                ))))
            }
        };
        if state.failed || state.cursor == state.end {
            return Poll::Ready(None);
        }
        if state.pending.is_none() {
            let requested = (state.end - state.cursor).min(state.chunk_bytes as u64) as usize;
            state.pending = Some((
                requested,
                Box::pin(read_active_chunk(
                    Arc::clone(&state.source),
                    state.cursor,
                    requested,
                    state.deadline,
                    state.cancellation.clone(),
                )),
            ));
        }
        let (requested, result) = {
            let (requested, pending) = state
                .pending
                .as_mut()
                .expect("pending read was initialized");
            match pending.as_mut().poll(cx) {
                Poll::Pending => return Poll::Pending,
                Poll::Ready(result) => (*requested, result),
            }
        };
        state.pending = None;
        let result = result.and_then(|chunk| {
            if chunk.len() != requested {
                return Err(ObjectStoreError::new(
                    PublishFailureKind::Fatal,
                    "replayable source returned a short or oversized non-EOF chunk",
                ));
            }
            let mut progress = state.progress.lock().map_err(|_| {
                ObjectStoreError::new(PublishFailureKind::Fatal, "multipart digest lock poisoned")
            })?;
            if progress.next_offset != state.cursor {
                return Err(ObjectStoreError::new(
                    PublishFailureKind::Fatal,
                    "multipart stream was polled out of order",
                ));
            }
            progress.digest.update(&chunk);
            state.cursor += chunk.len() as u64;
            progress.next_offset = state.cursor;
            Ok(Bytes::from(chunk))
        });
        if let Err(error) = &result {
            state.failed = true;
            if let Ok(mut progress) = state.progress.lock() {
                progress.failure = Some(error.clone());
            }
        }
        Poll::Ready(Some(result))
    }

    fn poll_trailers(
        self: Pin<&mut Self>,
        _cx: &mut Context<'_>,
    ) -> Poll<Result<Option<hyper::HeaderMap>, ObjectStoreError>> {
        Poll::Ready(Ok(None))
    }

    fn is_end_stream(&self) -> bool {
        self.state
            .lock()
            .is_ok_and(|state| state.failed || state.cursor == state.end)
    }

    fn size_hint(&self) -> SizeHint {
        self.state.lock().map_or_else(
            |_| SizeHint::default(),
            |state| SizeHint::with_exact(state.end - state.cursor),
        )
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ConditionalWriteDialect {
    S3IfNoneMatch,
    AliyunOss,
}

impl ConditionalWriteDialect {
    pub fn for_endpoint(endpoint_url: &str) -> Self {
        let authority = endpoint_url
            .split_once("://")
            .map_or(endpoint_url, |(_, value)| value)
            .split(['/', '?', '#'])
            .next()
            .unwrap_or_default()
            .rsplit('@')
            .next()
            .unwrap_or_default();
        let host = authority
            .strip_prefix('[')
            .and_then(|value| value.split_once(']').map(|(host, _)| host))
            .unwrap_or_else(|| authority.split(':').next().unwrap_or_default())
            .to_ascii_lowercase();
        if host.ends_with(".aliyuncs.com")
            && (host == "oss.aliyuncs.com" || host.starts_with("oss-") || host.contains(".oss-"))
        {
            Self::AliyunOss
        } else {
            Self::S3IfNoneMatch
        }
    }
}

#[derive(Debug)]
pub struct S3ImmutableObjectStore {
    bucket: String,
    client: aws_sdk_s3::Client,
    upload_client: aws_sdk_s3::Client,
    fence: Arc<NamespaceMutationFence>,
    multipart_threshold: usize,
    multipart_part_size: usize,
    write_domain_id: String,
    physical_source_id: String,
    conditional_write_dialect: ConditionalWriteDialect,
}

impl S3ImmutableObjectStore {
    pub const SHA_METADATA_KEY: &'static str = "rock-sha256";

    pub fn new(
        bucket: impl Into<String>,
        client: aws_sdk_s3::Client,
        multipart_threshold: usize,
        multipart_part_size: usize,
        fence: Arc<NamespaceMutationFence>,
    ) -> Result<Arc<Self>, ObjectStoreError> {
        Self::new_with_upload_client(
            bucket,
            client.clone(),
            client,
            multipart_threshold,
            multipart_part_size,
            fence,
            ConditionalWriteDialect::S3IfNoneMatch,
        )
    }

    pub fn new_with_upload_client(
        bucket: impl Into<String>,
        client: aws_sdk_s3::Client,
        upload_client: aws_sdk_s3::Client,
        multipart_threshold: usize,
        multipart_part_size: usize,
        fence: Arc<NamespaceMutationFence>,
        conditional_write_dialect: ConditionalWriteDialect,
    ) -> Result<Arc<Self>, ObjectStoreError> {
        let bucket = bucket.into();
        if bucket.is_empty()
            || multipart_threshold < S3_MINIMUM_PART_BYTES
            || !(S3_MINIMUM_PART_BYTES..=MAXIMUM_PART_BYTES).contains(&multipart_part_size)
            || fence.namespace() != bucket
        {
            return Err(ObjectStoreError::new(
                PublishFailureKind::Fatal,
                "invalid S3 immutable store configuration",
            ));
        }
        // An opaque SDK client exposes no stable endpoint identity. Without an
        // explicit source binding, isolate cache entries to this live client.
        let physical_source_id = format!("s3:unbound:{}", uuid::Uuid::new_v4().simple());
        Ok(Arc::new(Self {
            bucket,
            client,
            upload_client,
            fence,
            multipart_threshold,
            multipart_part_size,
            write_domain_id: uuid::Uuid::new_v4().simple().to_string(),
            physical_source_id,
            conditional_write_dialect,
        }))
    }

    /// Bind persistent byte reuse to the daemon-selected physical S3 source.
    /// The live write-domain fence remains independent and per-client.
    pub fn with_physical_source(
        self: Arc<Self>,
        endpoint: &str,
        region: &str,
    ) -> Result<Arc<Self>, ObjectStoreError> {
        let mut store = Arc::try_unwrap(self).map_err(|_| {
            ObjectStoreError::new(
                PublishFailureKind::Fatal,
                "S3 source must be bound before the client is shared",
            )
        })?;
        if endpoint.is_empty() || region.is_empty() {
            return Err(ObjectStoreError::new(
                PublishFailureKind::Fatal,
                "S3 physical endpoint and region must not be empty",
            ));
        }
        store.physical_source_id = format!(
            "s3:{:x}",
            Sha256::digest(
                serde_json::to_vec(&(endpoint, region, &store.bucket))
                    .expect("S3 source identity is serializable")
            )
        );
        Ok(Arc::new(store))
    }

    async fn head_with_client(
        &self,
        client: &aws_sdk_s3::Client,
        object_key: &str,
    ) -> Result<Option<RemoteObjectMetadata>, ObjectStoreError> {
        match client
            .head_object()
            .bucket(&self.bucket)
            .key(object_key)
            .send()
            .await
        {
            Ok(output) => {
                let checksum = output
                    .metadata()
                    .and_then(|metadata| metadata.get(Self::SHA_METADATA_KEY))
                    .cloned()
                    .ok_or_else(|| {
                        ObjectStoreError::new(
                            PublishFailureKind::IdentityConflict,
                            format!("existing object {object_key:?} has no immutable checksum"),
                        )
                    })?;
                let content_length = output.content_length().ok_or_else(|| {
                    ObjectStoreError::new(
                        PublishFailureKind::IdentityConflict,
                        "S3 object has no content length",
                    )
                })?;
                let size = u64::try_from(content_length).map_err(|_| {
                    ObjectStoreError::new(
                        PublishFailureKind::IdentityConflict,
                        "S3 object has a negative content length",
                    )
                })?;
                Ok(Some(RemoteObjectMetadata {
                    size,
                    sha256: checksum,
                    version: output.version_id().map(str::to_owned),
                    last_modified_unix: output.last_modified().map(|value| value.as_secs_f64()),
                    etag: output.e_tag().map(str::to_owned),
                }))
            }
            Err(error) if is_not_found(error.as_service_error().and_then(|value| value.code())) => {
                Ok(None)
            }
            Err(error) => Err(classify_s3_error(
                "head_object",
                object_key,
                error.as_service_error().and_then(|value| value.code()),
            )),
        }
    }

    async fn single_put(
        &self,
        object_key: &str,
        payload: &[u8],
        sha256: &str,
    ) -> Result<bool, ObjectStoreError> {
        let length = i64::try_from(payload.len()).map_err(|_| {
            ObjectStoreError::new(PublishFailureKind::Fatal, "object exceeds S3 length bound")
        })?;
        let request = self
            .client
            .put_object()
            .bucket(&self.bucket)
            .key(object_key)
            .body(ByteStream::from(payload.to_vec()))
            .content_length(length)
            .metadata(Self::SHA_METADATA_KEY, sha256);
        let result = match self.conditional_write_dialect {
            ConditionalWriteDialect::S3IfNoneMatch => request.if_none_match("*").send().await,
            ConditionalWriteDialect::AliyunOss => {
                request
                    .customize()
                    .mutate_request(|request| {
                        request
                            .headers_mut()
                            .insert(OSS_FORBID_OVERWRITE_HEADER, "true");
                    })
                    .send()
                    .await
            }
        };
        match result {
            Ok(_) => Ok(true),
            Err(error)
                if is_precondition(error.as_service_error().and_then(|value| value.code())) =>
            {
                Ok(false)
            }
            Err(error) => Err(classify_s3_error(
                "put_object",
                object_key,
                error.as_service_error().and_then(|value| value.code()),
            )),
        }
    }

    async fn multipart_put(
        &self,
        object_key: &str,
        payload: &[u8],
        sha256: &str,
    ) -> Result<bool, ObjectStoreError> {
        let create_request = self
            .upload_client
            .create_multipart_upload()
            .bucket(&self.bucket)
            .key(object_key)
            .metadata(Self::SHA_METADATA_KEY, sha256);
        let create_result = match self.conditional_write_dialect {
            ConditionalWriteDialect::S3IfNoneMatch => create_request.send().await,
            ConditionalWriteDialect::AliyunOss => {
                create_request
                    .customize()
                    .mutate_request(|request| {
                        request
                            .headers_mut()
                            .insert(OSS_FORBID_OVERWRITE_HEADER, "true");
                    })
                    .send()
                    .await
            }
        };
        let created = create_result.map_err(|error| {
            classify_s3_error(
                "create_multipart_upload",
                object_key,
                error.as_service_error().and_then(|value| value.code()),
            )
        })?;
        let upload_id = created.upload_id().ok_or_else(|| {
            ObjectStoreError::new(
                PublishFailureKind::Retryable,
                "S3 create_multipart_upload returned no upload ID",
            )
        })?;
        let result = self
            .upload_and_complete(object_key, upload_id, payload)
            .await;
        if result.is_err() || result == Ok(false) {
            let _ = self
                .upload_client
                .abort_multipart_upload()
                .bucket(&self.bucket)
                .key(object_key)
                .upload_id(upload_id)
                .send()
                .await;
        }
        if result? {
            let metadata = self.head(object_key).await?.ok_or_else(|| {
                ObjectStoreError::new(
                    PublishFailureKind::Retryable,
                    "completed multipart object is not visible",
                )
            })?;
            if metadata.size != payload.len() as u64 || metadata.sha256 != sha256 {
                return Err(ObjectStoreError::new(
                    PublishFailureKind::IdentityConflict,
                    "completed multipart metadata failed verification",
                ));
            }
            let remote = self.get(object_key).await?;
            if remote != payload {
                return Err(ObjectStoreError::new(
                    PublishFailureKind::IdentityConflict,
                    "completed multipart payload failed verification",
                ));
            }
            Ok(true)
        } else {
            Ok(false)
        }
    }

    async fn upload_and_complete(
        &self,
        object_key: &str,
        upload_id: &str,
        payload: &[u8],
    ) -> Result<bool, ObjectStoreError> {
        let mut parts = Vec::new();
        for (index, part) in payload.chunks(self.multipart_part_size).enumerate() {
            let part_number = i32::try_from(index + 1).map_err(|_| {
                ObjectStoreError::new(PublishFailureKind::Fatal, "too many multipart parts")
            })?;
            let length = i64::try_from(part.len()).map_err(|_| {
                ObjectStoreError::new(PublishFailureKind::Fatal, "multipart part is too large")
            })?;
            let uploaded = self
                .upload_client
                .upload_part()
                .bucket(&self.bucket)
                .key(object_key)
                .upload_id(upload_id)
                .part_number(part_number)
                .content_length(length)
                .body(ByteStream::from(part.to_vec()))
                .send()
                .await
                .map_err(|error| {
                    classify_s3_error(
                        "upload_part",
                        object_key,
                        error.as_service_error().and_then(|value| value.code()),
                    )
                })?;
            let etag = uploaded.e_tag().ok_or_else(|| {
                ObjectStoreError::new(
                    PublishFailureKind::Retryable,
                    "S3 upload_part returned no ETag",
                )
            })?;
            parts.push(
                CompletedPart::builder()
                    .part_number(part_number)
                    .e_tag(etag)
                    .build(),
            );
        }
        let completed = CompletedMultipartUpload::builder()
            .set_parts(Some(parts))
            .build();
        let complete_request = self
            .upload_client
            .complete_multipart_upload()
            .bucket(&self.bucket)
            .key(object_key)
            .upload_id(upload_id)
            .multipart_upload(completed);
        let complete_result = match self.conditional_write_dialect {
            ConditionalWriteDialect::S3IfNoneMatch => {
                complete_request.if_none_match("*").send().await
            }
            ConditionalWriteDialect::AliyunOss => {
                complete_request
                    .customize()
                    .mutate_request(|request| {
                        request
                            .headers_mut()
                            .insert(OSS_FORBID_OVERWRITE_HEADER, "true");
                    })
                    .send()
                    .await
            }
        };
        match complete_result {
            Ok(_) => Ok(true),
            Err(error)
                if is_precondition(error.as_service_error().and_then(|value| value.code())) =>
            {
                Ok(false)
            }
            Err(error) => Err(classify_s3_error(
                "complete_multipart_upload",
                object_key,
                error.as_service_error().and_then(|value| value.code()),
            )),
        }
    }
}

#[async_trait]
impl ImmutableObjectStore for S3ImmutableObjectStore {
    fn namespace(&self) -> &str {
        &self.bucket
    }

    fn fence(&self) -> Arc<NamespaceMutationFence> {
        Arc::clone(&self.fence)
    }

    async fn head(
        &self,
        object_key: &str,
    ) -> Result<Option<RemoteObjectMetadata>, ObjectStoreError> {
        self.head_with_client(&self.client, object_key).await
    }

    async fn put_if_absent(
        &self,
        object_key: &str,
        payload: &[u8],
        sha256: &str,
    ) -> Result<bool, ObjectStoreError> {
        if payload.len() >= self.multipart_threshold {
            self.multipart_put(object_key, payload, sha256).await
        } else {
            self.single_put(object_key, payload, sha256).await
        }
    }

    async fn get_with_metadata(
        &self,
        object_key: &str,
        object_version: Option<&str>,
        max_bytes: u64,
    ) -> Result<Option<RemoteObject>, BoundedGetError> {
        let output = match self
            .client
            .get_object()
            .bucket(&self.bucket)
            .key(object_key)
            .set_version_id(object_version.map(str::to_owned))
            .send()
            .await
        {
            Ok(output) => output,
            Err(error) if is_not_found(error.as_service_error().and_then(|value| value.code())) => {
                return Ok(None);
            }
            Err(error) => {
                return Err(classify_s3_error(
                    "get_object",
                    object_key,
                    error.as_service_error().and_then(|value| value.code()),
                )
                .into());
            }
        };
        let checksum = output
            .metadata()
            .and_then(|metadata| metadata.get(Self::SHA_METADATA_KEY))
            .cloned()
            .ok_or_else(|| {
                ObjectStoreError::new(
                    PublishFailureKind::IdentityConflict,
                    format!("existing object {object_key:?} has no immutable checksum"),
                )
            })?;
        let content_length = output.content_length().ok_or_else(|| {
            ObjectStoreError::new(
                PublishFailureKind::IdentityConflict,
                "S3 object has no content length",
            )
        })?;
        let size = u64::try_from(content_length).map_err(|_| {
            ObjectStoreError::new(
                PublishFailureKind::IdentityConflict,
                "S3 object has a negative content length",
            )
        })?;
        if size > max_bytes {
            return Err(BoundedGetError::BoundExceeded {
                object_key: object_key.to_owned(),
                actual_bytes: size,
                max_bytes,
            });
        }
        let metadata = RemoteObjectMetadata {
            size,
            sha256: checksum,
            version: output.version_id().map(str::to_owned),
            last_modified_unix: output.last_modified().map(|value| value.as_secs_f64()),
            etag: output.e_tag().map(str::to_owned),
        };
        // Do not trust Content-Length as the only memory guard. Reading at
        // most max+1 bytes detects a provider/proxy that sends a larger body
        // without ever buffering the unbounded response.
        let read_limit = max_bytes.checked_add(1).unwrap_or(max_bytes);
        let mut body = output.body.into_async_read().take(read_limit);
        let mut payload = Vec::new();
        body.read_to_end(&mut payload).await.map_err(|error| {
            ObjectStoreError::new(
                PublishFailureKind::Retryable,
                format!("S3 get_object body failed for {object_key:?}: {error}"),
            )
        })?;
        if payload.len() as u64 > max_bytes {
            return Err(BoundedGetError::BoundExceeded {
                object_key: object_key.to_owned(),
                actual_bytes: payload.len() as u64,
                max_bytes,
            });
        }
        Ok(Some(RemoteObject { payload, metadata }))
    }

    async fn get(&self, object_key: &str) -> Result<Vec<u8>, ObjectStoreError> {
        let output = self
            .client
            .get_object()
            .bucket(&self.bucket)
            .key(object_key)
            .send()
            .await
            .map_err(|error| {
                classify_s3_error(
                    "get_object",
                    object_key,
                    error.as_service_error().and_then(|value| value.code()),
                )
            })?;
        output
            .body
            .collect()
            .await
            .map(|payload| payload.into_bytes().to_vec())
            .map_err(|error| {
                ObjectStoreError::new(
                    PublishFailureKind::Retryable,
                    format!("S3 get_object body failed for {object_key:?}: {error}"),
                )
            })
    }
}

fn validate_metadata_key(object_key: &str) -> Result<(), ObjectStoreError> {
    if object_key.is_empty() || object_key.len() > 4096 || object_key.contains('\0') {
        return Err(ObjectStoreError::new(
            PublishFailureKind::Fatal,
            "metadata object key must contain 1..=4096 bytes without NUL",
        ));
    }
    Ok(())
}

#[async_trait]
impl MetadataObjectStore for S3ImmutableObjectStore {
    fn namespace(&self) -> &str {
        &self.bucket
    }

    async fn get_bounded(
        &self,
        object_key: &str,
        max_bytes: u64,
    ) -> Result<Option<RemoteObject>, BoundedGetError> {
        validate_metadata_key(object_key)?;
        // The existing operation returns bytes and identity from one response,
        // handles missing objects, and enforces the bound while streaming.
        <Self as ImmutableObjectStore>::get_with_metadata(
            self,
            object_key,
            None,
            max_bytes.min(MAX_METADATA_OBJECT_BYTES),
        )
        .await
    }

    async fn replace(
        &self,
        object_key: &str,
        payload: &[u8],
        expected_sha256: &str,
    ) -> Result<(), ObjectStoreError> {
        validate_metadata_key(object_key)?;
        if payload.len() as u64 > MAX_METADATA_OBJECT_BYTES {
            return Err(ObjectStoreError::new(
                PublishFailureKind::Fatal,
                "metadata object exceeds the transport byte bound",
            ));
        }
        let actual = format!("sha256:{:x}", Sha256::digest(payload));
        if actual != expected_sha256 {
            return Err(ObjectStoreError::new(
                PublishFailureKind::Fatal,
                "metadata payload checksum does not match expected SHA-256",
            ));
        }
        // This is deliberately an ordinary whole-object PUT for both S3 and
        // OSS: no If-None-Match or forbid-overwrite protection, and no claimed
        // CAS. The configured client and bucket remain the write authority.
        self.client
            .put_object()
            .bucket(&self.bucket)
            .key(object_key)
            .body(ByteStream::from(payload.to_vec()))
            .content_length(payload.len() as i64)
            .metadata(Self::SHA_METADATA_KEY, expected_sha256)
            .customize()
            .config_override(
                aws_sdk_s3::config::Builder::new().request_checksum_calculation(
                    aws_sdk_s3::config::RequestChecksumCalculation::WhenRequired,
                ),
            )
            .send()
            .await
            .map(|_| ())
            .map_err(|error| {
                classify_s3_error(
                    "put_metadata",
                    object_key,
                    error.as_service_error().and_then(|value| value.code()),
                )
            })
    }
}

#[async_trait]
impl ImmutableStreamWriter for S3ImmutableObjectStore {
    fn namespace(&self) -> &str {
        &self.bucket
    }

    fn fence(&self) -> Arc<NamespaceMutationFence> {
        Arc::clone(&self.fence)
    }

    fn write_domain_id(&self) -> &str {
        &self.write_domain_id
    }

    async fn ensure_stream_verified(
        &self,
        identity: &ImmutableObjectIdentity,
        source: Arc<dyn ReplayableByteSource>,
        lease: UploadTransactionLease,
        deadline: Instant,
        cancellation: IoCancellation,
        chunk_bytes: usize,
    ) -> Result<VerifiedWriteReceipt, ObjectStoreError> {
        if !lease.validates(&self.fence) {
            return Err(ObjectStoreError::new(
                PublishFailureKind::Fatal,
                "S3 stream writer lease belongs to another mutation fence",
            ));
        }
        if source.length() != identity.length
            || !(S3_MINIMUM_PART_BYTES..=MAXIMUM_STREAM_CHUNK_BYTES).contains(&chunk_bytes)
        {
            return Err(ObjectStoreError::new(
                PublishFailureKind::Fatal,
                "S3 replayable source or multipart chunk bound is invalid",
            ));
        }
        // Part size protects the provider's 10,000-part bound; source chunks
        // protect the caller's memory budget. They are independent sizes.
        let layout = multipart_layout(identity.length, self.multipart_part_size)?;
        if let Some(existing) = self
            .head_with_client(&self.upload_client, &identity.object_key)
            .await?
        {
            return self
                .verify_remote_stream(identity, &existing, deadline, &cancellation, chunk_bytes)
                .await;
        }
        let create_request = self
            .upload_client
            .create_multipart_upload()
            .bucket(&self.bucket)
            .key(&identity.object_key)
            .metadata(Self::SHA_METADATA_KEY, &identity.sha256);
        let create_result = match self.conditional_write_dialect {
            ConditionalWriteDialect::S3IfNoneMatch => create_request.send().await,
            ConditionalWriteDialect::AliyunOss => {
                create_request
                    .customize()
                    .mutate_request(|request| {
                        request
                            .headers_mut()
                            .insert(OSS_FORBID_OVERWRITE_HEADER, "true");
                    })
                    .send()
                    .await
            }
        };
        let created = create_result.map_err(|error| {
            classify_s3_error(
                "create_multipart_upload",
                &identity.object_key,
                error.as_service_error().and_then(|value| value.code()),
            )
        })?;
        let upload_id = created
            .upload_id()
            .ok_or_else(|| {
                ObjectStoreError::new(
                    PublishFailureKind::Retryable,
                    "S3 create_multipart_upload returned no upload ID",
                )
            })?
            .to_owned();
        let transfer = self
            .upload_stream_parts(
                identity,
                source,
                &upload_id,
                deadline,
                &cancellation,
                chunk_bytes,
                layout,
            )
            .await;
        let result = match transfer {
            Ok(parts) => {
                let completed = CompletedMultipartUpload::builder()
                    .set_parts(Some(parts))
                    .build();
                let complete_request = self
                    .upload_client
                    .complete_multipart_upload()
                    .bucket(&self.bucket)
                    .key(&identity.object_key)
                    .upload_id(&upload_id)
                    .multipart_upload(completed);
                let completion = match self.conditional_write_dialect {
                    ConditionalWriteDialect::S3IfNoneMatch => {
                        complete_request.if_none_match("*").send().await
                    }
                    ConditionalWriteDialect::AliyunOss => {
                        complete_request
                            .customize()
                            .mutate_request(|request| {
                                request
                                    .headers_mut()
                                    .insert(OSS_FORBID_OVERWRITE_HEADER, "true");
                            })
                            .send()
                            .await
                    }
                };
                match completion {
                    Ok(_) => {
                        self.verify_visible_stream(identity, deadline, &cancellation, chunk_bytes)
                            .await
                    }
                    Err(error) => {
                        let code = error.as_service_error().and_then(|value| value.code());
                        if is_precondition(code) {
                            self.verify_visible_stream(
                                identity,
                                deadline,
                                &cancellation,
                                chunk_bytes,
                            )
                            .await
                        } else {
                            // Complete can be ambiguous.  Resolve it through a
                            // cryptographic read-back before classifying the attempt.
                            match self
                                .verify_visible_stream(
                                    identity,
                                    deadline,
                                    &cancellation,
                                    chunk_bytes,
                                )
                                .await
                            {
                                Ok(receipt) => Ok(receipt),
                                Err(error)
                                    if error.kind == PublishFailureKind::IdentityConflict =>
                                {
                                    Err(error)
                                }
                                Err(_) => Err(classify_s3_error(
                                    "complete_multipart_upload",
                                    &identity.object_key,
                                    code,
                                )),
                            }
                        }
                    }
                }
            }
            Err(error) => Err(error),
        };
        if result.is_err() {
            let abort = self
                .upload_client
                .abort_multipart_upload()
                .bucket(&self.bucket)
                .key(&identity.object_key)
                .upload_id(&upload_id)
                .send();
            let abort_confirmed = matches!(
                tokio::time::timeout(Duration::from_secs(30), abort).await,
                Ok(Ok(_))
            );
            if !abort_confirmed {
                return result.map_err(ObjectStoreError::with_abort_cleanup_unconfirmed);
            }
        }
        result
    }
}

impl S3ImmutableObjectStore {
    #[allow(clippy::too_many_arguments)]
    async fn upload_stream_parts(
        &self,
        identity: &ImmutableObjectIdentity,
        source: Arc<dyn ReplayableByteSource>,
        upload_id: &str,
        deadline: Instant,
        cancellation: &IoCancellation,
        chunk_bytes: usize,
        layout: MultipartLayout,
    ) -> Result<Vec<CompletedPart>, ObjectStoreError> {
        let mut offset = 0_u64;
        let progress = Arc::new(StdMutex::new(StreamProgress {
            next_offset: 0,
            digest: Sha256::new(),
            failure: None,
        }));
        let mut parts = Vec::with_capacity(layout.part_count as usize);
        while offset < identity.length {
            ensure_active(deadline, cancellation)?;
            let length = (identity.length - offset).min(layout.part_bytes);
            let part_number = parts.len() + 1;
            if part_number as u64 > S3_MAXIMUM_PARTS {
                return Err(ObjectStoreError::new(
                    PublishFailureKind::Fatal,
                    "too many multipart parts",
                ));
            }
            let body = StreamingPartBody::new(
                Arc::clone(&source),
                offset,
                length,
                chunk_bytes,
                deadline,
                cancellation.clone(),
                Arc::clone(&progress),
            )?;
            let upload = self
                .upload_client
                .upload_part()
                .bucket(&self.bucket)
                .key(&identity.object_key)
                .upload_id(upload_id)
                .part_number(part_number as i32)
                .content_length(length as i64)
                .body(ByteStream::new(SdkBody::from_body_0_4(body)));
            let upload = if self.conditional_write_dialect == ConditionalWriteDialect::AliyunOss {
                // The SDK otherwise changes a streaming request into AWS's
                // aws-chunked checksum-trailer encoding. OSS compatibility
                // needs the ordinary fixed-length part bytes. Immutable SHA
                // scanning and cryptographic remote read-back still apply.
                upload.customize().config_override(
                    aws_sdk_s3::config::Builder::new().request_checksum_calculation(
                        aws_sdk_s3::config::RequestChecksumCalculation::WhenRequired,
                    ),
                )
            } else {
                upload.customize()
            };
            let uploaded = upload.send().await.map_err(|error| {
                // Preserve typed source/integrity failures instead of
                // flattening a body error into a generic transport retry.
                progress
                    .lock()
                    .ok()
                    .and_then(|state| state.failure.clone())
                    .unwrap_or_else(|| {
                        classify_s3_error(
                            "upload_part",
                            &identity.object_key,
                            error.as_service_error().and_then(|value| value.code()),
                        )
                    })
            })?;
            let end = offset.checked_add(length).ok_or_else(|| {
                ObjectStoreError::new(PublishFailureKind::Fatal, "stream offset overflow")
            })?;
            if progress
                .lock()
                .map_err(|_| {
                    ObjectStoreError::new(
                        PublishFailureKind::Fatal,
                        "multipart digest lock poisoned",
                    )
                })?
                .next_offset
                != end
            {
                return Err(ObjectStoreError::new(
                    PublishFailureKind::Fatal,
                    "multipart HTTP body was not fully consumed",
                ));
            }
            let etag = uploaded.e_tag().ok_or_else(|| {
                ObjectStoreError::new(
                    PublishFailureKind::Retryable,
                    "S3 upload_part returned no ETag",
                )
            })?;
            parts.push(
                CompletedPart::builder()
                    .part_number(part_number as i32)
                    .e_tag(etag)
                    .build(),
            );
            offset = end;
        }
        if !read_active_chunk(
            Arc::clone(&source),
            identity.length,
            1,
            deadline,
            cancellation.clone(),
        )
        .await?
        .is_empty()
        {
            return Err(ObjectStoreError::new(
                PublishFailureKind::Fatal,
                "replayable source exceeds declared length",
            ));
        }
        let actual = {
            let state = progress.lock().map_err(|_| {
                ObjectStoreError::new(PublishFailureKind::Fatal, "multipart digest lock poisoned")
            })?;
            format!("sha256:{:x}", state.digest.clone().finalize())
        };
        if actual != identity.sha256 {
            return Err(ObjectStoreError::new(
                PublishFailureKind::Fatal,
                "replayable source failed full-length checksum validation",
            ));
        }
        Ok(parts)
    }

    async fn verify_visible_stream(
        &self,
        identity: &ImmutableObjectIdentity,
        deadline: Instant,
        cancellation: &IoCancellation,
        chunk_bytes: usize,
    ) -> Result<VerifiedWriteReceipt, ObjectStoreError> {
        let metadata = self
            .head_with_client(&self.upload_client, &identity.object_key)
            .await?
            .ok_or_else(|| {
                ObjectStoreError::new(
                    PublishFailureKind::Retryable,
                    "completed multipart object is not visible",
                )
            })?;
        self.verify_remote_stream(identity, &metadata, deadline, cancellation, chunk_bytes)
            .await
    }

    async fn verify_remote_stream(
        &self,
        identity: &ImmutableObjectIdentity,
        metadata: &RemoteObjectMetadata,
        deadline: Instant,
        cancellation: &IoCancellation,
        chunk_bytes: usize,
    ) -> Result<VerifiedWriteReceipt, ObjectStoreError> {
        if metadata.size != identity.length || metadata.sha256 != identity.sha256 {
            return Err(ObjectStoreError::new(
                PublishFailureKind::IdentityConflict,
                format!(
                    "immutable object identity conflict for {:?}",
                    identity.object_key
                ),
            ));
        }
        ensure_active(deadline, cancellation)?;
        let output = self
            .upload_client
            .get_object()
            .bucket(&self.bucket)
            .key(&identity.object_key)
            .set_version_id(metadata.version.clone())
            .send()
            .await
            .map_err(|error| {
                classify_s3_error(
                    "get_object",
                    &identity.object_key,
                    error.as_service_error().and_then(|value| value.code()),
                )
            })?;
        let mut reader = output.body.into_async_read();
        let mut buffer = vec![0_u8; chunk_bytes];
        let mut length = 0_u64;
        let mut digest = Sha256::new();
        loop {
            ensure_active(deadline, cancellation)?;
            let read = reader.read(&mut buffer).await.map_err(|error| {
                ObjectStoreError::new(
                    PublishFailureKind::Retryable,
                    format!("S3 streaming verification failed: {error}"),
                )
            })?;
            if read == 0 {
                break;
            }
            length = length.checked_add(read as u64).ok_or_else(|| {
                ObjectStoreError::new(
                    PublishFailureKind::IdentityConflict,
                    "remote length overflow",
                )
            })?;
            if length > identity.length {
                return Err(ObjectStoreError::new(
                    PublishFailureKind::IdentityConflict,
                    "remote object exceeds its immutable length",
                ));
            }
            digest.update(&buffer[..read]);
        }
        if length != identity.length || format!("sha256:{:x}", digest.finalize()) != identity.sha256
        {
            return Err(ObjectStoreError::new(
                PublishFailureKind::IdentityConflict,
                format!(
                    "immutable object checksum conflict for {:?}",
                    identity.object_key
                ),
            ));
        }
        Ok(VerifiedWriteReceipt {
            object_key: identity.object_key.clone(),
            sha256: identity.sha256.clone(),
            length: identity.length,
            object_version: metadata.version.clone(),
        })
    }
}

fn ensure_active(deadline: Instant, cancellation: &IoCancellation) -> Result<(), ObjectStoreError> {
    if cancellation.is_cancelled() || Instant::now() >= deadline {
        Err(ObjectStoreError::new(
            PublishFailureKind::Retryable,
            "stream upload was cancelled or timed out",
        ))
    } else {
        Ok(())
    }
}

#[async_trait]
impl PhysicalRangeReader for S3ImmutableObjectStore {
    fn source_id(&self) -> &str {
        &self.physical_source_id
    }

    async fn read_physical_range(
        &self,
        request: PhysicalRangeRequest<'_>,
    ) -> Result<Vec<u8>, RangeReadError> {
        request.validate()?;
        if request
            .namespace
            .is_some_and(|namespace| namespace != self.bucket)
        {
            return Err(RangeReadError::failure(
                RangeReadFailureClass::Integrity,
                "physical range namespace differs from the bound S3 bucket",
            ));
        }
        let end = request
            .offset
            .checked_add(request.length)
            .and_then(|value| value.checked_sub(1))
            .ok_or(RangeReadError::InvalidRange)?;
        let output = self
            .client
            .get_object()
            .bucket(&self.bucket)
            .key(request.object_key)
            .range(format!("bytes={}-{end}", request.offset))
            .set_version_id(request.object_version.map(str::to_owned))
            .send()
            .await
            .map_err(|error| {
                let code = error.as_service_error().and_then(|value| value.code());
                let status = error
                    .raw_response()
                    .map(|response| response.status().as_u16());
                if is_not_found(code) || status == Some(404) {
                    RangeReadError::NotFound
                } else if matches!(code, Some("InvalidRange" | "416")) || status == Some(416) {
                    RangeReadError::InvalidRange
                } else {
                    let class = match &error {
                        SdkError::TimeoutError(_) | SdkError::DispatchFailure(_) => {
                            RangeReadFailureClass::Transient
                        }
                        SdkError::ServiceError(_) => classify_range_read_failure(code, status),
                        SdkError::ResponseError(_) => classify_range_read_failure(None, status),
                        SdkError::ConstructionFailure(_) => RangeReadFailureClass::Permanent,
                        _ => RangeReadFailureClass::Permanent,
                    };
                    RangeReadError::failure(
                        class,
                        format!(
                            "S3 ranged GET failed for {:?}: {}",
                            request.object_key,
                            code.unwrap_or("transport")
                        ),
                    )
                }
            })?;
        let content_length = output
            .content_length()
            .and_then(|value| u64::try_from(value).ok())
            .ok_or(RangeReadError::InvalidRange)?;
        if content_length != request.length {
            return Err(RangeReadError::InvalidRange);
        }
        let (actual_start, actual_end, total_size) = output
            .content_range()
            .and_then(parse_content_range)
            .ok_or(RangeReadError::InvalidRange)?;
        if actual_start != request.offset || actual_end != end || total_size <= end {
            return Err(RangeReadError::InvalidRange);
        }
        if request.object_size.is_some_and(|size| size != total_size) {
            return Err(RangeReadError::failure(
                RangeReadFailureClass::Integrity,
                "S3 response size conflicts with immutable object identity",
            ));
        }
        if output
            .metadata()
            .and_then(|metadata| metadata.get(Self::SHA_METADATA_KEY))
            .is_some_and(|sha| {
                sha.strip_prefix("sha256:").unwrap_or(sha)
                    != request
                        .object_sha256
                        .strip_prefix("sha256:")
                        .unwrap_or(request.object_sha256)
            })
        {
            return Err(RangeReadError::failure(
                RangeReadFailureClass::Integrity,
                "S3 response checksum conflicts with immutable object identity",
            ));
        }
        if request
            .object_version
            .is_some_and(|version| output.version_id() != Some(version))
        {
            return Err(RangeReadError::failure(
                RangeReadFailureClass::Integrity,
                "S3 response version conflicts with immutable range grant",
            ));
        }
        let payload = output
            .body
            .collect()
            .await
            .map_err(|error| {
                RangeReadError::failure(
                    RangeReadFailureClass::Transient,
                    format!("S3 range body failed: {error}"),
                )
            })?
            .into_bytes()
            .to_vec();
        if payload.len() as u64 != request.length {
            return Err(RangeReadError::InvalidRange);
        }
        Ok(payload)
    }
}

fn parse_content_range(header: &str) -> Option<(u64, u64, u64)> {
    let range = header.strip_prefix("bytes ")?;
    let (bounds, total) = range.split_once('/')?;
    let (start, end) = bounds.split_once('-')?;
    if start.is_empty()
        || end.is_empty()
        || total.is_empty()
        || start.contains(|character: char| !character.is_ascii_digit())
        || end.contains(|character: char| !character.is_ascii_digit())
        || total.contains(|character: char| !character.is_ascii_digit())
    {
        return None;
    }
    Some((start.parse().ok()?, end.parse().ok()?, total.parse().ok()?))
}

fn is_precondition(code: Option<&str>) -> bool {
    matches!(
        code,
        Some(
            "409"
                | "412"
                | "ConditionalRequestConflict"
                | "FileAlreadyExists"
                | "PreconditionFailed"
        )
    )
}

fn is_not_found(code: Option<&str>) -> bool {
    matches!(
        code,
        Some("404" | "NoSuchKey" | "NoSuchVersion" | "NotFound")
    )
}

fn classify_range_read_failure(code: Option<&str>, status: Option<u16>) -> RangeReadFailureClass {
    match (code, status) {
        (_, Some(401 | 403)) => RangeReadFailureClass::Authorization,
        (_, Some(408 | 429 | 500 | 502 | 503 | 504)) => RangeReadFailureClass::Transient,
        (
            Some(
                "AccessDenied"
                | "InvalidAccessKeyId"
                | "SignatureDoesNotMatch"
                | "ExpiredToken"
                | "Unauthorized"
                | "Forbidden"
                | "401"
                | "403",
            ),
            _,
        ) => RangeReadFailureClass::Authorization,
        (
            Some(
                "SlowDown" | "RequestTimeout" | "InternalError" | "ServiceUnavailable"
                | "Throttling" | "429" | "500" | "502" | "503" | "504",
            ),
            _,
        ) => RangeReadFailureClass::Transient,
        _ => RangeReadFailureClass::Permanent,
    }
}

fn classify_s3_error(operation: &str, key: &str, code: Option<&str>) -> ObjectStoreError {
    let kind = match code {
        Some("AccessDenied" | "InvalidAccessKeyId" | "SignatureDoesNotMatch") => {
            PublishFailureKind::Fatal
        }
        Some(
            "SlowDown" | "RequestTimeout" | "InternalError" | "ServiceUnavailable" | "500" | "503",
        )
        | None => PublishFailureKind::Retryable,
        _ => PublishFailureKind::Retryable,
    };
    ObjectStoreError::new(
        kind,
        format!(
            "S3 {operation} failed for {key:?}: {}",
            code.unwrap_or("transport")
        ),
    )
}

#[cfg(test)]
mod streaming_tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use tokio::sync::watch;

    #[tokio::test]
    async fn physical_cache_source_is_stable_and_separate_from_live_write_fencing() {
        use aws_sdk_s3::config::{BehaviorVersion, Credentials, Region};
        fn store(endpoint: &str, region: &str, bucket: &str) -> Arc<S3ImmutableObjectStore> {
            let client = aws_sdk_s3::Client::from_conf(
                aws_sdk_s3::Config::builder()
                    .behavior_version(BehaviorVersion::latest())
                    .endpoint_url(endpoint)
                    .region(Region::new(region.to_owned()))
                    .credentials_provider(Credentials::new("test", "test", None, None, "fixture"))
                    .force_path_style(true)
                    .build(),
            );
            S3ImmutableObjectStore::new(
                bucket,
                client,
                8 * 1024 * 1024,
                64 * 1024 * 1024,
                NamespaceMutationFence::new(bucket).unwrap(),
            )
            .unwrap()
            .with_physical_source(endpoint, region)
            .unwrap()
        }
        let first = store("http://cache-fixture.invalid", "region-a", "bucket-a");
        let reopened = store("http://cache-fixture.invalid", "region-a", "bucket-a");
        assert_eq!(first.source_id(), reopened.source_id());
        assert_ne!(first.write_domain_id(), reopened.write_domain_id());
        for other in [
            store("http://another-fixture.invalid", "region-a", "bucket-a"),
            store("http://cache-fixture.invalid", "region-b", "bucket-a"),
            store("http://cache-fixture.invalid", "region-a", "bucket-b"),
        ] {
            assert_ne!(first.source_id(), other.source_id());
        }
    }

    #[tokio::test]
    async fn physical_reader_keeps_bucket_digest_size_and_version_binding() {
        use aws_sdk_s3::config::{BehaviorVersion, Credentials, Region};
        use hyper::service::{make_service_fn, service_fn};
        use std::convert::Infallible;

        let calls = Arc::new(AtomicUsize::new(0));
        let observed = Arc::clone(&calls);
        let socket = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        socket.set_nonblocking(true).unwrap();
        let address = socket.local_addr().unwrap();
        let service = make_service_fn(move |_| {
            let observed = Arc::clone(&observed);
            async move {
                Ok::<_, Infallible>(service_fn(move |request: hyper::Request<hyper::Body>| {
                    observed.fetch_add(1, Ordering::Relaxed);
                    assert_eq!(request.uri().path(), "/test-bucket/raw.pack");
                    assert_eq!(request.headers().get("range").unwrap(), "bytes=2-4");
                    async move {
                        Ok::<_, Infallible>(
                            hyper::Response::builder()
                                .status(206)
                                .header("content-range", "bytes 2-4/8")
                                .header("x-amz-version-id", "version-1")
                                .header(
                                    "x-amz-meta-rock-sha256",
                                    format!("sha256:{}", "a".repeat(64)),
                                )
                                .body(hyper::Body::from("234"))
                                .unwrap(),
                        )
                    }
                }))
            }
        });
        let server = tokio::spawn(hyper::Server::from_tcp(socket).unwrap().serve(service));
        let config = aws_sdk_s3::Config::builder()
            .behavior_version(BehaviorVersion::latest())
            .endpoint_url(format!("http://{address}"))
            .region(Region::new("us-east-1"))
            .credentials_provider(Credentials::new(
                "test-key",
                "test-secret",
                None,
                None,
                "test",
            ))
            .force_path_style(true)
            .build();
        let client = aws_sdk_s3::Client::from_conf(config);
        let store = S3ImmutableObjectStore::new(
            "test-bucket",
            client,
            8 * 1024 * 1024,
            64 * 1024 * 1024,
            NamespaceMutationFence::new("test-bucket").unwrap(),
        )
        .unwrap();
        let digest = format!("sha256:{}", "a".repeat(64));
        let other_digest = format!("sha256:{}", "b".repeat(64));
        let request = PhysicalRangeRequest {
            namespace: Some("test-bucket"),
            object_key: "raw.pack",
            object_sha256: &digest,
            object_size: Some(8),
            object_version: Some("version-1"),
            offset: 2,
            length: 3,
        };
        let reader: &dyn PhysicalRangeReader = store.as_ref();
        assert!(!reader.source_id().is_empty());
        assert_eq!(reader.read_physical_range(request).await.unwrap(), b"234");
        assert!(reader
            .read_physical_range(PhysicalRangeRequest {
                namespace: Some("different-bucket"),
                ..request
            })
            .await
            .is_err());
        assert_eq!(calls.load(Ordering::Relaxed), 1);
        for invalid in [
            PhysicalRangeRequest {
                object_size: Some(9),
                ..request
            },
            PhysicalRangeRequest {
                object_version: Some("version-2"),
                ..request
            },
            PhysicalRangeRequest {
                object_sha256: &other_digest,
                ..request
            },
        ] {
            assert!(matches!(
                reader.read_physical_range(invalid).await,
                Err(RangeReadError::Failure {
                    class: RangeReadFailureClass::Integrity,
                    ..
                })
            ));
        }
        // Older authenticated snapshot metadata binds the concrete client and
        // bucket but does not carry the complete object size.
        assert_eq!(
            reader
                .read_physical_range(PhysicalRangeRequest {
                    namespace: None,
                    object_size: None,
                    ..request
                })
                .await
                .unwrap(),
            b"234"
        );
        // The actual S3 HTTP adapter and direct physical path use the same
        // cache coordinator as OCI; a restarted cache retains the raw bytes.
        use crate::snapshot::io::range_io::RangeReader;
        let cache_source = store.source_id().to_owned();
        let cache_root =
            std::env::temp_dir().join(format!("s3-physical-cache-{}", uuid::Uuid::new_v4()));
        let cache = crate::snapshot::transport::posix_file::LocalFileCache::new(
            cache_root.clone(),
            "s3-fixture",
        )
        .unwrap();
        let io = crate::snapshot::io::range_io::RangeIO::new_with_cache(
            store.clone(),
            None,
            Some(cache),
        );
        let before = calls.load(Ordering::Relaxed);
        assert_eq!(
            io.read_object_range(&cache_source, request).await.unwrap(),
            b"234"
        );
        assert_eq!(
            io.read_object_range(&cache_source, request).await.unwrap(),
            b"234"
        );
        assert_eq!(calls.load(Ordering::Relaxed), before + 1);
        drop(io);
        let cache = crate::snapshot::transport::posix_file::LocalFileCache::new(
            cache_root.clone(),
            "s3-fixture",
        )
        .unwrap();
        let io = crate::snapshot::io::range_io::RangeIO::new_with_cache(store, None, Some(cache));
        assert_eq!(
            io.read_object_range(&cache_source, request).await.unwrap(),
            b"234"
        );
        assert_eq!(calls.load(Ordering::Relaxed), before + 1);
        for _ in 0..2 {
            assert!(
                io.read_object_range(
                    &cache_source,
                    PhysicalRangeRequest {
                        object_version: Some("version-2"),
                        ..request
                    }
                )
                .await
                .is_err(),
                "a different object version cannot hit version-1 bytes"
            );
        }
        assert_eq!(calls.load(Ordering::Relaxed), before + 3);
        drop(io);
        std::fs::remove_dir_all(cache_root).unwrap();
        server.abort();
    }

    #[derive(Clone, Copy)]
    enum Fault {
        None,
        Short,
        Read,
        Pending,
    }

    struct Source {
        bytes: Vec<u8>,
        largest_read: AtomicUsize,
        fault: Fault,
    }

    impl ReplayableByteSource for Source {
        fn length(&self) -> u64 {
            self.bytes.len() as u64
        }

        fn read_at<'a>(
            &'a self,
            offset: u64,
            max_bytes: usize,
        ) -> Pin<Box<dyn Future<Output = Result<Vec<u8>, ObjectStoreError>> + Send + 'a>> {
            Box::pin(async move {
                self.largest_read.fetch_max(max_bytes, Ordering::SeqCst);
                match self.fault {
                    Fault::Read => {
                        return Err(ObjectStoreError::new(
                            PublishFailureKind::Retryable,
                            "source read failed",
                        ))
                    }
                    Fault::Pending => return std::future::pending().await,
                    Fault::None | Fault::Short => {}
                }
                let start = offset as usize;
                let end = start.saturating_add(max_bytes).min(self.bytes.len());
                let end = if matches!(self.fault, Fault::Short) && end > start {
                    end - 1
                } else {
                    end
                };
                Ok(self.bytes.get(start..end).unwrap_or_default().to_vec())
            })
        }
    }

    fn progress() -> Arc<StdMutex<StreamProgress>> {
        Arc::new(StdMutex::new(StreamProgress {
            next_offset: 0,
            digest: Sha256::new(),
            failure: None,
        }))
    }

    #[test]
    fn multipart_layout_handles_two_tib_without_exceeding_provider_limits() {
        let length = 2 * 1024 * 1024 * 1024 * 1024_u64;
        let layout = multipart_layout(length, 8 * 1024 * 1024).unwrap();
        assert!(layout.part_bytes > MAXIMUM_STREAM_CHUNK_BYTES as u64);
        assert!(layout.part_bytes <= S3_MAXIMUM_PART_BYTES);
        assert!(layout.part_count <= S3_MAXIMUM_PARTS);
        assert_eq!(layout.part_count, length.div_ceil(layout.part_bytes));
        let at_limit =
            multipart_layout(S3_MAXIMUM_PART_BYTES * S3_MAXIMUM_PARTS, 8 * 1024 * 1024).unwrap();
        assert_eq!(at_limit.part_bytes, S3_MAXIMUM_PART_BYTES);
        assert_eq!(at_limit.part_count, S3_MAXIMUM_PARTS);
        assert!(multipart_layout(
            S3_MAXIMUM_PART_BYTES * S3_MAXIMUM_PARTS + 1,
            8 * 1024 * 1024
        )
        .is_err());
        assert!(multipart_layout(0, 8 * 1024 * 1024).is_err());
        assert!(multipart_layout(u64::MAX, 8 * 1024 * 1024).is_err());
    }

    #[test]
    fn multipart_layout_can_raise_five_mib_parts_for_dense_block_layers() {
        let length = 64 * 1024 * 1024 * 1024_u64 + 8192;
        let layout = multipart_layout(length, S3_MINIMUM_PART_BYTES).unwrap();
        assert!(layout.part_bytes > S3_MINIMUM_PART_BYTES as u64);
        assert!(layout.part_count <= S3_MAXIMUM_PARTS);
        assert_eq!(multipart_layout(1, 64 * 1024 * 1024).unwrap().part_count, 1);
    }

    #[tokio::test]
    async fn streaming_parts_preserve_full_digest_with_small_source_windows() {
        let bytes = (0_u8..39).collect::<Vec<_>>();
        let source = Arc::new(Source {
            bytes: bytes.clone(),
            largest_read: AtomicUsize::new(0),
            fault: Fault::None,
        });
        let state = progress();
        let (_sender, receiver) = watch::channel(false);
        let mut actual = Vec::new();
        for offset in [0, 13, 26] {
            let body = StreamingPartBody::new(
                source.clone(),
                offset,
                13,
                5,
                Instant::now() + Duration::from_secs(1),
                IoCancellation::new(receiver.clone()),
                Arc::clone(&state),
            )
            .unwrap();
            // Constructing Smithy's actual body here also verifies its Send +
            // Sync requirements without a live S3 server or credentials.
            let payload = ByteStream::new(SdkBody::from_body_0_4(body))
                .collect()
                .await
                .unwrap()
                .into_bytes();
            actual.extend_from_slice(&payload);
        }
        assert_eq!(actual, bytes);
        assert!(source.largest_read.load(Ordering::SeqCst) <= 5);
        let state = state.lock().unwrap();
        assert_eq!(state.next_offset, 39);
        assert_eq!(
            format!("sha256:{:x}", state.digest.clone().finalize()),
            format!("sha256:{:x}", Sha256::digest(&bytes))
        );
        assert!(state.failure.is_none());
    }

    #[tokio::test]
    async fn streaming_body_reports_short_reads_and_source_errors() {
        for (fault, expected) in [
            (Fault::Short, PublishFailureKind::Fatal),
            (Fault::Read, PublishFailureKind::Retryable),
        ] {
            let source = Arc::new(Source {
                bytes: vec![1; 13],
                largest_read: AtomicUsize::new(0),
                fault,
            });
            let state = progress();
            let (_sender, receiver) = watch::channel(false);
            let body = StreamingPartBody::new(
                source,
                0,
                13,
                5,
                Instant::now() + Duration::from_secs(1),
                IoCancellation::new(receiver),
                Arc::clone(&state),
            )
            .unwrap();
            assert!(ByteStream::new(SdkBody::from_body_0_4(body))
                .collect()
                .await
                .is_err());
            assert_eq!(
                state.lock().unwrap().failure.as_ref().unwrap().kind,
                expected
            );
            assert_eq!(state.lock().unwrap().next_offset, 0);
        }
    }

    #[tokio::test]
    async fn streaming_body_interrupts_pending_source_on_cancel_or_deadline() {
        for cancel in [false, true] {
            let source = Arc::new(Source {
                bytes: vec![1; 13],
                largest_read: AtomicUsize::new(0),
                fault: Fault::Pending,
            });
            let state = progress();
            let (sender, receiver) = watch::channel(false);
            let deadline = Instant::now()
                + if cancel {
                    Duration::from_secs(5)
                } else {
                    Duration::from_millis(1)
                };
            let body = StreamingPartBody::new(
                source,
                0,
                13,
                5,
                deadline,
                IoCancellation::new(receiver),
                Arc::clone(&state),
            )
            .unwrap();
            let reader = tokio::spawn(async move {
                ByteStream::new(SdkBody::from_body_0_4(body))
                    .collect()
                    .await
            });
            if cancel {
                tokio::task::yield_now().await;
                sender.send(true).unwrap();
            }
            assert!(tokio::time::timeout(Duration::from_secs(1), reader)
                .await
                .unwrap()
                .unwrap()
                .is_err());
            assert_eq!(
                state.lock().unwrap().failure.as_ref().unwrap().kind,
                PublishFailureKind::Retryable
            );
        }
    }

    #[tokio::test]
    async fn sdk_streams_parts_with_fixed_bytes_for_oss_and_checksum_trailers_for_s3() {
        use aws_sdk_s3::config::{BehaviorVersion, Credentials, Region};
        use hyper::service::{make_service_fn, service_fn};
        use std::convert::Infallible;

        for dialect in [
            ConditionalWriteDialect::AliyunOss,
            ConditionalWriteDialect::S3IfNoneMatch,
        ] {
            let received = Arc::new(StdMutex::new(None));
            let observed = Arc::clone(&received);
            let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
            listener.set_nonblocking(true).unwrap();
            let address = listener.local_addr().unwrap();
            let service = make_service_fn(move |_| {
                let observed = Arc::clone(&observed);
                async move {
                    Ok::<_, Infallible>(service_fn(move |request: hyper::Request<hyper::Body>| {
                        let observed = Arc::clone(&observed);
                        async move {
                            let headers = request.headers().clone();
                            let bytes = hyper::body::to_bytes(request.into_body()).await.unwrap();
                            *observed.lock().unwrap() = Some((headers, bytes));
                            Ok::<_, Infallible>(
                                hyper::Response::builder()
                                    .status(200)
                                    .header("etag", "\"test-part\"")
                                    .body(hyper::Body::empty())
                                    .unwrap(),
                            )
                        }
                    }))
                }
            });
            let server = tokio::spawn(hyper::Server::from_tcp(listener).unwrap().serve(service));
            let config = aws_sdk_s3::Config::builder()
                .behavior_version(BehaviorVersion::latest())
                .endpoint_url(format!("http://{address}"))
                .region(Region::new("us-east-1"))
                .credentials_provider(Credentials::new(
                    "test-key",
                    "test-secret",
                    None,
                    None,
                    "test",
                ))
                .force_path_style(true)
                .build();
            let client = aws_sdk_s3::Client::from_conf(config);
            let store = S3ImmutableObjectStore::new_with_upload_client(
                "test-bucket",
                client.clone(),
                client,
                8 * 1024 * 1024,
                64 * 1024 * 1024,
                NamespaceMutationFence::new("test-bucket").unwrap(),
                dialect,
            )
            .unwrap();
            let bytes = (0_u8..39).collect::<Vec<_>>();
            let source = Arc::new(Source {
                bytes: bytes.clone(),
                largest_read: AtomicUsize::new(0),
                fault: Fault::None,
            });
            let identity = ImmutableObjectIdentity {
                object_key: "test-object".into(),
                sha256: format!("sha256:{:x}", Sha256::digest(&bytes)),
                length: bytes.len() as u64,
            };
            let (_sender, receiver) = watch::channel(false);
            let parts = store
                .upload_stream_parts(
                    &identity,
                    source.clone(),
                    "test-upload",
                    Instant::now() + Duration::from_secs(5),
                    &IoCancellation::new(receiver),
                    5,
                    multipart_layout(identity.length, 64 * 1024 * 1024).unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(parts.len(), 1);
            assert!(source.largest_read.load(Ordering::SeqCst) <= 5);
            let (headers, actual) = received.lock().unwrap().take().unwrap();
            if dialect == ConditionalWriteDialect::AliyunOss {
                assert!(headers.get("content-encoding").is_none());
                assert!(headers.get("x-amz-trailer").is_none());
                assert_eq!(actual.as_ref(), bytes.as_slice());
                assert_eq!(headers["content-length"], identity.length.to_string());
            } else {
                assert_eq!(headers["content-encoding"], "aws-chunked");
                assert!(headers.get("x-amz-trailer").is_some());
            }
            server.abort();
        }
    }
}

#[cfg(test)]
mod metadata_tests {
    use super::*;
    use aws_sdk_s3::config::{BehaviorVersion, Credentials, Region};
    use hyper::service::{make_service_fn, service_fn};
    use std::collections::HashMap;
    use std::convert::Infallible;

    #[derive(Default)]
    struct State {
        objects: HashMap<String, (Vec<u8>, String)>,
        puts: Vec<(String, hyper::HeaderMap, Vec<u8>)>,
        calls: usize,
    }

    fn digest(payload: &[u8]) -> String {
        format!("sha256:{:x}", Sha256::digest(payload))
    }

    fn error_response(status: u16, code: &str) -> hyper::Response<hyper::Body> {
        hyper::Response::builder()
            .status(status)
            .header("content-type", "application/xml")
            .body(hyper::Body::from(format!(
                "<Error><Code>{code}</Code><Message>test error</Message></Error>"
            )))
            .unwrap()
    }

    async fn store(
        dialect: ConditionalWriteDialect,
    ) -> (
        Arc<S3ImmutableObjectStore>,
        Arc<StdMutex<State>>,
        tokio::task::JoinHandle<Result<(), hyper::Error>>,
    ) {
        let state = Arc::new(StdMutex::new(State::default()));
        let observed = state.clone();
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let address = listener.local_addr().unwrap();
        let service = make_service_fn(move |_| {
            let observed = observed.clone();
            async move {
                Ok::<_, Infallible>(service_fn(move |request: hyper::Request<hyper::Body>| {
                    let observed = observed.clone();
                    async move {
                        let method = request.method().clone();
                        let key = request
                            .uri()
                            .path()
                            .strip_prefix("/test-bucket/")
                            .expect("configured bucket")
                            .to_owned();
                        let headers = request.headers().clone();
                        let body = hyper::body::to_bytes(request.into_body()).await.unwrap();
                        let mut state = observed.lock().unwrap();
                        state.calls += 1;
                        if method == hyper::Method::PUT {
                            state
                                .puts
                                .push((key.clone(), headers.clone(), body.to_vec()));
                            let create_only = headers.contains_key("if-none-match")
                                || headers.contains_key(OSS_FORBID_OVERWRITE_HEADER);
                            if create_only && state.objects.contains_key(&key) {
                                return Ok::<_, Infallible>(error_response(
                                    412,
                                    "PreconditionFailed",
                                ));
                            }
                            let sha = headers
                                .get("x-amz-meta-rock-sha256")
                                .expect("metadata identity")
                                .to_str()
                                .unwrap()
                                .to_owned();
                            state.objects.insert(key, (body.to_vec(), sha));
                            return Ok(hyper::Response::builder()
                                .status(200)
                                .header("etag", "\"metadata-etag\"")
                                .body(hyper::Body::empty())
                                .unwrap());
                        }
                        assert_eq!(method, hyper::Method::GET);
                        if key == "denied" {
                            return Ok(error_response(403, "AccessDenied"));
                        }
                        let Some((bytes, sha)) = state.objects.get(&key) else {
                            return Ok(error_response(404, "NoSuchKey"));
                        };
                        Ok(hyper::Response::builder()
                            .status(200)
                            .header("content-length", bytes.len())
                            .header("x-amz-meta-rock-sha256", sha.as_str())
                            .header("etag", "\"metadata-etag\"")
                            .header("x-amz-version-id", "metadata-version")
                            .body(hyper::Body::from(bytes.clone()))
                            .unwrap())
                    }
                }))
            }
        });
        let server = tokio::spawn(hyper::Server::from_tcp(listener).unwrap().serve(service));
        let client = aws_sdk_s3::Client::from_conf(
            aws_sdk_s3::Config::builder()
                .behavior_version(BehaviorVersion::latest())
                .endpoint_url(format!("http://{address}"))
                .region(Region::new("us-east-1"))
                .credentials_provider(Credentials::new(
                    "test-key",
                    "test-secret",
                    None,
                    None,
                    "test",
                ))
                .force_path_style(true)
                .build(),
        );
        let store = S3ImmutableObjectStore::new_with_upload_client(
            "test-bucket",
            client.clone(),
            client,
            8 * 1024 * 1024,
            64 * 1024 * 1024,
            NamespaceMutationFence::new("test-bucket").unwrap(),
            dialect,
        )
        .unwrap();
        (store, state, server)
    }

    #[tokio::test]
    async fn replaceable_metadata_uses_whole_put_and_keeps_immutable_create_protection() {
        for dialect in [
            ConditionalWriteDialect::S3IfNoneMatch,
            ConditionalWriteDialect::AliyunOss,
        ] {
            let (store, state, server) = store(dialect).await;
            let metadata: &dyn MetadataObjectStore = store.as_ref();
            assert_eq!(metadata.namespace(), "test-bucket");
            let key = "image-metadata-v1.json";
            let first = br#"{"hotset":"first"}"#;
            let second = br#"{"hotset":"second"}"#;
            metadata.replace(key, first, &digest(first)).await.unwrap();
            metadata
                .replace(key, second, &digest(second))
                .await
                .unwrap();
            let object = metadata.get_bounded(key, 4096).await.unwrap().unwrap();
            assert_eq!(object.payload, second);
            assert_eq!(object.metadata.size, second.len() as u64);
            assert_eq!(object.metadata.sha256, digest(second));
            assert_eq!(object.metadata.version.as_deref(), Some("metadata-version"));
            assert_eq!(object.metadata.etag.as_deref(), Some("\"metadata-etag\""));
            // The old immutable operation still sends its provider-specific
            // create protection and cannot replace the second metadata value.
            assert!(!ImmutableObjectStore::put_if_absent(
                store.as_ref(),
                key,
                first,
                &digest(first)
            )
            .await
            .unwrap());
            let state = state.lock().unwrap();
            assert_eq!(state.objects[key].0, second);
            assert_eq!(state.puts.len(), 3);
            for ((actual_key, headers, bytes), expected) in state.puts[..2]
                .iter()
                .zip([first.as_slice(), second.as_slice()])
            {
                assert_eq!(actual_key, key);
                assert_eq!(bytes, expected);
                assert_eq!(headers["content-length"], expected.len().to_string());
                assert_eq!(headers["x-amz-meta-rock-sha256"], digest(expected));
                for absent in [
                    "if-none-match",
                    "if-match",
                    OSS_FORBID_OVERWRITE_HEADER,
                    "content-encoding",
                    "x-amz-trailer",
                ] {
                    assert!(!headers.contains_key(absent), "{absent}");
                }
            }
            let headers = &state.puts[2].1;
            match dialect {
                ConditionalWriteDialect::S3IfNoneMatch => {
                    assert_eq!(headers["if-none-match"], "*");
                    assert!(!headers.contains_key(OSS_FORBID_OVERWRITE_HEADER));
                }
                ConditionalWriteDialect::AliyunOss => {
                    assert_eq!(headers[OSS_FORBID_OVERWRITE_HEADER], "true");
                    assert!(!headers.contains_key("if-none-match"));
                }
            }
            drop(state);
            server.abort();
        }
    }

    #[tokio::test]
    async fn metadata_get_returns_bounded_identity_missing_and_provider_errors() {
        let (store, state, server) = store(ConditionalWriteDialect::AliyunOss).await;
        {
            let mut state = state.lock().unwrap();
            let bytes = b"metadata bytes".to_vec();
            state
                .objects
                .insert("small".into(), (bytes.clone(), digest(&bytes)));
            let bytes = vec![0; MAX_METADATA_OBJECT_BYTES as usize + 1];
            state
                .objects
                .insert("large".into(), (bytes.clone(), digest(&bytes)));
        }
        let metadata: &dyn MetadataObjectStore = store.as_ref();
        assert!(metadata.get_bounded("missing", 32).await.unwrap().is_none());
        assert!(matches!(
            metadata.get_bounded("denied", 32).await,
            Err(BoundedGetError::Store(ObjectStoreError {
                kind: PublishFailureKind::Fatal,
                ..
            }))
        ));
        assert!(matches!(
            metadata.get_bounded("small", 4).await,
            Err(BoundedGetError::BoundExceeded {
                actual_bytes: 14,
                max_bytes: 4,
                ..
            })
        ));
        assert!(matches!(
            metadata.get_bounded("large", u64::MAX).await,
            Err(BoundedGetError::BoundExceeded {
                actual_bytes,
                max_bytes: MAX_METADATA_OBJECT_BYTES,
                ..
            }) if actual_bytes == MAX_METADATA_OBJECT_BYTES + 1
        ));
        assert_eq!(state.lock().unwrap().calls, 4);
        server.abort();
    }

    #[tokio::test]
    async fn metadata_invalid_key_size_or_digest_is_rejected_before_network_io() {
        let (store, state, server) = store(ConditionalWriteDialect::AliyunOss).await;
        let metadata: &dyn MetadataObjectStore = store.as_ref();
        let bytes = b"metadata";
        for key in [String::new(), "x".repeat(4097), "bad\0key".into()] {
            assert_eq!(
                metadata
                    .replace(&key, bytes, &digest(bytes))
                    .await
                    .unwrap_err()
                    .kind,
                PublishFailureKind::Fatal
            );
            assert!(matches!(
                metadata.get_bounded(&key, 32).await,
                Err(BoundedGetError::Store(ObjectStoreError {
                    kind: PublishFailureKind::Fatal,
                    ..
                }))
            ));
        }
        for expected in ["not-a-digest".to_owned(), digest(b"different")] {
            assert_eq!(
                metadata
                    .replace("metadata", bytes, &expected)
                    .await
                    .unwrap_err()
                    .kind,
                PublishFailureKind::Fatal
            );
        }
        let oversized = vec![0; MAX_METADATA_OBJECT_BYTES as usize + 1];
        assert_eq!(
            metadata
                .replace("metadata", &oversized, &digest(&oversized))
                .await
                .unwrap_err()
                .kind,
            PublishFailureKind::Fatal
        );
        assert_eq!(state.lock().unwrap().calls, 0);
        server.abort();
    }
}
