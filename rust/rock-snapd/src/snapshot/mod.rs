//! Snapshot objects, image sources, storage and data loading capabilities.
//!
//! Snapshot-wide metadata and logical read contracts are defined here. Formats
//! own layer codecs and write preparation; transports own physical byte I/O.
pub mod format;
pub mod image;
pub mod instance;
pub mod io;
pub mod metadata;
pub mod repository;
pub mod services;
pub mod transport;

use std::fmt::Debug;
use std::sync::Arc;

use async_trait::async_trait;

pub use self::instance::Snapshot;
use self::metadata::{LayerRangeMap, SourceRef};
use self::transport::{BlobRangeError, BlobRangeErrorKind};

pub use self::image::metadata::ImageMetadataError;
pub use self::metadata::{
    LayerKind, LazyLoadContext, LazyLoadError, LazyReadAccounting, LazyReadResult,
    SnapshotChainMetadata, SnapshotChainView, SnapshotImageMetadata, SnapshotKind,
    SnapshotLayerMetadata, SnapshotManifest, SnapshotMetadataSubject, SnapshotReadRange,
    SnapshotSpec, VmStateRef,
};

// Snapshot-wide contracts. Implementations remain with their object or I/O owner.
pub trait ValidateCanonicalModel {
    fn validate(&self) -> Result<(), String>;
}

/// One snapshot identity, its logical layers and its authoritative lifecycle.
/// Native capture and persistent format builders share the concrete Snapshot.
/// DATA is read through the selected layer reader, never a second snapshot read API.
pub trait SnapshotInstance: std::any::Any + Debug + Send + Sync {
    fn snapshot_id(&self) -> &str;
    fn snapshot_kind(&self) -> SnapshotKind;
    fn lifecycle(&self) -> &std::sync::Mutex<repository::SnapshotState>;
    fn ancestor_snapshot_ids(&self) -> &[String] {
        &[]
    }
    fn vm_state_reference(&self) -> Option<VmStateRef> {
        None
    }
    fn layer_chain(&self, kind: LayerKind) -> Result<Option<SnapshotChainMetadata>, String>;
    fn layer_reader(
        &self,
        kind: LayerKind,
        content_identity: &str,
    ) -> Result<Arc<dyn LazyLoadInterface>, LazyLoadError>;

    /// Persistent-source capabilities are absent for an unpublished raw snapshot.
    fn spec(&self) -> Option<&SnapshotSpec> {
        None
    }
    /// Original image identity survives capture, restore and fork. A legacy
    /// snapshot may have no provenance and remains readable in that case.
    fn provenance(&self) -> Option<&metadata::SnapshotProvenance> {
        self.spec().and_then(|spec| spec.provenance.as_ref())
    }
    fn manifest(&self) -> Option<&SnapshotManifest> {
        None
    }
    fn vm_state(&self) -> Option<&[u8]> {
        None
    }
    fn chain_view(&self, _kind: LayerKind) -> Option<&SnapshotChainView> {
        None
    }

    fn state(&self) -> repository::PublicationState {
        self.lifecycle()
            .lock()
            .expect("snapshot state poisoned")
            .state
    }
    fn last_usable(&self) -> Option<repository::PublicationState> {
        self.lifecycle()
            .lock()
            .expect("snapshot state poisoned")
            .last_usable
    }
    fn chains(&self) -> Vec<SnapshotChainMetadata> {
        self.lifecycle()
            .lock()
            .expect("snapshot state poisoned")
            .chains
            .clone()
    }
    fn failure(&self) -> Option<repository::SnapshotFailure> {
        self.lifecycle()
            .lock()
            .expect("snapshot state poisoned")
            .failure
            .clone()
    }
    fn resume(&self) {
        self.lifecycle()
            .lock()
            .expect("snapshot state poisoned")
            .resume();
    }
    fn fail(&self, stage: &'static str, message: String) {
        self.lifecycle()
            .lock()
            .expect("snapshot state poisoned")
            .fail(stage, message);
    }
    /// A raw snapshot may already have a committed remote representation.
    fn publication(&self) -> Option<format::UploadedSnapshot> {
        let state = self.lifecycle().lock().expect("snapshot state poisoned");
        state.published.clone().or_else(|| {
            Some(format::UploadedSnapshot {
                registry_objects: None,
                spec: self.spec()?.clone(),
                manifest: self.manifest()?.clone(),
            })
        })
    }
}

impl dyn SnapshotInstance {
    /// Optional concrete capabilities do not add format-specific methods to
    /// the common snapshot contract. Arc upcasting preserves object identity.
    pub fn downcast<T: SnapshotInstance>(self: Arc<Self>) -> Option<Arc<T>> {
        let erased: Arc<dyn std::any::Any + Send + Sync> = self;
        erased.downcast::<T>().ok()
    }
}

#[async_trait]
pub trait LazyLoadInterface: Debug + Send + Sync {
    /// Read DATA only. ZERO and INHERIT are kept distinct in metadata; neither
    /// may be silently fetched or turned into zero bytes by this operation.
    async fn read_data_range(
        &self,
        context: &LazyLoadContext,
        request: SnapshotReadRange<'_>,
    ) -> Result<LazyReadResult, LazyLoadError>;
}

#[async_trait::async_trait]
pub trait SnapshotImageMetadataReader: std::fmt::Debug + Send + Sync {
    async fn read(
        &self,
        subject: &SnapshotMetadataSubject,
    ) -> Result<Option<SnapshotImageMetadata>, ImageMetadataError>;
}

#[async_trait::async_trait]
pub trait SnapshotImageMetadataUploader: std::fmt::Debug + Send + Sync {
    async fn upload(&self, metadata: &SnapshotImageMetadata) -> Result<(), ImageMetadataError>;
}

// Digest-pinned external metadata contracts; concrete source assembly is I/O.
#[derive(Debug, Clone, thiserror::Error)]
pub(crate) enum SourceError {
    #[error("invalid snapshot source: {0}")]
    Invalid(String),
    #[error("snapshot source identity conflict: {0}")]
    Integrity(String),
    #[error(transparent)]
    Transport(#[from] BlobRangeError),
}

impl SourceError {
    pub(crate) fn retryable(&self) -> bool {
        matches!(self, Self::Transport(error) if error.retryable())
    }

    pub(crate) fn is_integrity(&self) -> bool {
        matches!(self, Self::Integrity(_))
            || matches!(self, Self::Transport(error) if error.kind == BlobRangeErrorKind::Integrity)
    }
}

#[derive(Debug)]
pub(crate) struct ResolvedSource {
    pub source_ref: SourceRef,
    pub range_map: LayerRangeMap,
    pub descriptor: Vec<u8>,
    pub descriptor_sha256: String,
}

#[async_trait]
pub(crate) trait SourceResolver: std::fmt::Debug + Send + Sync {
    async fn resolve_source(&self, source: &SourceRef) -> Result<Arc<ResolvedSource>, SourceError>;

    async fn registry_client(
        &self,
        registry: &str,
    ) -> Result<Arc<crate::snapshot::transport::registry::RegistryClient>, SourceError> {
        crate::snapshot::transport::registry::RegistryClient::new(registry).map_err(Into::into)
    }

    fn registry_physical_cache(
        &self,
    ) -> Option<Arc<dyn crate::snapshot::transport::PhysicalRangeCache>> {
        None
    }

    async fn remember_registry_source(
        &self,
        _source: &SourceRef,
        _descriptor: &[u8],
    ) -> Result<(), SourceError> {
        Ok(())
    }

    /// Initial import precedes discovery of the descriptor hash. Implementations
    /// must resolve only this selected manifest digest, never a mutable tag.
    async fn import_registry(
        &self,
        _registry: &str,
        _repository: &str,
        _manifest_digest: &str,
        _platform: &str,
    ) -> Result<Arc<ResolvedSource>, SourceError> {
        Err(SourceError::Invalid(
            "resolver cannot import Registry sources".into(),
        ))
    }
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    fn visit_rust_sources(directory: &Path, visit: &mut impl FnMut(&Path, &str)) {
        for entry in std::fs::read_dir(directory).expect("snapshot source directory") {
            let path = entry.expect("snapshot source entry").path();
            if path.is_dir() {
                visit_rust_sources(&path, visit);
            } else if path.extension().and_then(|value| value.to_str()) == Some("rs") {
                let source = std::fs::read_to_string(&path).expect("snapshot Rust source");
                visit(&path, &source);
            }
        }
    }

    #[test]
    fn snapshot_sources_do_not_depend_on_vm_or_business_or_endpoints() {
        let directory = Path::new(env!("CARGO_MANIFEST_DIR")).join("src/snapshot");
        let forbidden =
            ["vm", "core", "endpoints"].map(|module| format!("{}::{module}::", "crate"));
        let mut violations = Vec::new();
        visit_rust_sources(&directory, &mut |path, source| {
            for (index, line) in source.lines().enumerate() {
                for prefix in &forbidden {
                    if line.contains(prefix) {
                        violations.push(format!("{}:{}: {prefix}", path.display(), index + 1));
                    }
                }
            }
        });
        assert!(violations.is_empty(), "{}", violations.join("\n"));
    }
}
