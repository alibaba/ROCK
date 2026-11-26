//! One concrete snapshot identity across capture, publication and remote restore.

use crate::snapshot::format::overlaybd_s3::capture::{
    backing_recovery, build_chain_index, encode_frozen_layer, validate_canonical_parts,
    CanonicalLocalSnapshot, FormatCaptureError, FrozenLayerInput, LocalChainIndex, LocalVmState,
};
use crate::snapshot::format::SnapshotUploadPlan;
use crate::snapshot::format::{SnapshotUploadError, SnapshotUploadInterface};
use crate::snapshot::io::backing::{BackingError, LocalBackingHandle};
use crate::snapshot::metadata::{
    LayerChainIndex, LayerKind, LayerRef, LazyLoadError, RockletArtifactCompatibility,
    SnapshotKind, SnapshotLayerMetadata, SnapshotProvenance, StorageDescriptor, VmStateRef,
    VmmCompatibility,
};
use crate::snapshot::repository::frozen_layer;
use crate::snapshot::repository::frozen_layer::{
    create_temporary_staged_object, CapturedSnapshotPin, FrozenLayerError, FrozenLayerPin,
};
use crate::snapshot::repository::lineage::{SnapshotLineage, SnapshotLineageEntry};
use crate::snapshot::repository::SnapshotState;
use crate::snapshot::transport::{DataIntegrityFailure, StagedObjectWriter};
use crate::snapshot::{LazyLoadInterface, SnapshotInstance};
use serde::ser::SerializeStruct;
use serde::{Serialize, Serializer};
use std::collections::{HashMap, HashSet};
#[cfg(test)]
use std::fs::File;
use std::sync::{Arc, Mutex, OnceLock};
use thiserror::Error;

#[derive(Clone, Debug)]
pub struct FrozenLocalLayer {
    pub kind: LayerKind,
    pub layer_id: String,
    pub provisional_content_id: String,
    pub logical_bytes: u64,
    pub unit_size_bytes: u64,
    backing: LocalBackingHandle,
}

impl FrozenLocalLayer {
    fn from_backing(backing: LocalBackingHandle) -> Result<Self, LocalSnapshotError> {
        let native = backing.backing.native_metadata().ok_or_else(|| {
            LocalSnapshotError::Invalid("frozen layer has no native ownership".into())
        })?;
        Ok(Self {
            kind: native.descriptor.kind,
            layer_id: native.descriptor.layer_id.clone(),
            provisional_content_id: native.descriptor.provisional_content_id.clone(),
            logical_bytes: native.descriptor.logical_size,
            unit_size_bytes: native.ownership.granularity,
            backing,
        })
    }

    pub fn ownership(&self) -> &frozen_layer::PackedOwnershipMap {
        &self
            .backing
            .backing
            .native_metadata()
            .expect("native layer metadata")
            .ownership
    }

    pub fn data_units(&self) -> u64 {
        (0..self.logical_bytes / self.unit_size_bytes)
            .filter(|unit| {
                packed_ownership_state(&self.ownership().packed_states, *unit) == Some(1)
            })
            .count() as u64
    }
}

impl PartialEq for FrozenLocalLayer {
    fn eq(&self, other: &Self) -> bool {
        self.kind == other.kind
            && self.layer_id == other.layer_id
            && self.provisional_content_id == other.provisional_content_id
            && self.logical_bytes == other.logical_bytes
            && self.unit_size_bytes == other.unit_size_bytes
            && self.ownership() == other.ownership()
    }
}
impl Eq for FrozenLocalLayer {}
impl Serialize for FrozenLocalLayer {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut value = serializer.serialize_struct("FrozenLocalLayer", 6)?;
        value.serialize_field("kind", &self.kind)?;
        value.serialize_field("layer_id", &self.layer_id)?;
        value.serialize_field("provisional_content_id", &self.provisional_content_id)?;
        value.serialize_field("logical_bytes", &self.logical_bytes)?;
        value.serialize_field("unit_size_bytes", &self.unit_size_bytes)?;
        value.serialize_field("ownership", self.ownership())?;
        value.end()
    }
}

impl From<BackingError> for LocalSnapshotError {
    fn from(error: BackingError) -> Self {
        Self::Io(error.to_string())
    }
}

#[derive(Debug, Error)]
pub enum LocalSnapshotError {
    #[error(transparent)]
    SourceIntegrity(#[from] DataIntegrityFailure),
    #[error("Invalid local snapshot: {0}")]
    Invalid(String),
    #[error("Captured snapshot resources are unavailable: {0}")]
    Frozen(String),
    #[error("Local snapshot I/O failed: {0}")]
    Io(String),
    #[error("Upload plan is invalid: {0}")]
    Upload(String),
    #[error("Local snapshot lock is poisoned")]
    LockPoisoned,
}

impl From<FormatCaptureError> for LocalSnapshotError {
    fn from(error: FormatCaptureError) -> Self {
        match error {
            FormatCaptureError::SourceIntegrity(failure) => Self::SourceIntegrity(failure),
            FormatCaptureError::Io(message) => Self::Io(message),
            error => Self::Invalid(error.to_string()),
        }
    }
}

impl From<FrozenLayerError> for LocalSnapshotError {
    fn from(error: FrozenLayerError) -> Self {
        match error {
            FrozenLayerError::SourceIntegrity(failure) => Self::SourceIntegrity(failure),
            FrozenLayerError::Io(message) => Self::Io(message),
            error => Self::Frozen(error.to_string()),
        }
    }
}

impl From<SnapshotUploadError> for LocalSnapshotError {
    fn from(error: SnapshotUploadError) -> Self {
        match error {
            SnapshotUploadError::SourceIntegrity(failure) => Self::SourceIntegrity(failure),
            error => Self::Upload(error.to_string()),
        }
    }
}

/// One stable repository identity, including while capture is still Creating.
#[derive(Debug)]
pub struct Snapshot {
    pub snapshot_id: String,
    pub snapshot_kind: SnapshotKind,
    lifecycle: Mutex<SnapshotState>,
    resources: OnceLock<Arc<SnapshotResources>>,
    persistent: OnceLock<PersistentSnapshot>,
}

/// Authenticated format metadata for the same identity. Native layer identities
/// remain stable while publication adds canonical content identities.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct PersistentSnapshot {
    pub spec: crate::snapshot::metadata::SnapshotSpec,
    pub manifest: crate::snapshot::metadata::SnapshotManifest,
    pub memory: Option<crate::snapshot::metadata::SnapshotChainView>,
    pub rootfs: crate::snapshot::metadata::SnapshotChainView,
    pub vm_state: Option<Vec<u8>>,
    pub ancestor_snapshot_ids: Vec<String>,
}

/// Native resources installed once after all capture validation succeeds.
/// This is private representation, not another snapshot instance or state table.
#[doc(hidden)]
#[derive(Debug)]
pub struct SnapshotResources {
    native_generation_id: String,
    ancestor_snapshot_ids: Vec<String>,
    chains: HashMap<LayerKind, crate::snapshot::metadata::SnapshotChainMetadata>,
    vm_state_reference: Option<VmStateRef>,
    // Logical closure removes the authoritative lease, while an encoder may
    // retain an Arc until it finishes reading the exact captured FDs.
    pin: Mutex<Option<Arc<()>>>,
    pub frozen_layers: HashMap<LayerKind, FrozenLocalLayer>,
    canonicalize: Mutex<()>,
    canonical: Mutex<Option<Arc<CanonicalLocalSnapshot>>>,
    lineage: SnapshotLineage,
    pub vm_state: Option<LocalVmState>,
    pub rocklet_artifact: Option<RockletArtifactCompatibility>,
    pub provenance: Option<SnapshotProvenance>,
    // Snapshot-scoped remote ancestry survives source-VM exit and upload-pin
    // closure. It is released only with the actual snapshot resource owners.
    retained_layer_resources: Mutex<Vec<Arc<crate::snapshot::io::layer_resources::LayerResources>>>,
}

impl std::ops::Deref for Snapshot {
    type Target = SnapshotResources;

    fn deref(&self) -> &Self::Target {
        self.resources
            .get()
            .expect("native resources are accessed only after LOCAL_READY")
    }
}

impl Snapshot {
    pub fn creating(snapshot_id: impl Into<String>, kind: SnapshotKind) -> Arc<Self> {
        Arc::new(Self {
            snapshot_id: snapshot_id.into(),
            snapshot_kind: kind,
            lifecycle: Mutex::new(SnapshotState::creating()),
            resources: OnceLock::new(),
            persistent: OnceLock::new(),
        })
    }

    pub fn is_initialized(&self) -> bool {
        self.has_native() || self.persistent.get().is_some()
    }

    pub(crate) fn persistent_owned(&self) -> Option<PersistentSnapshot> {
        self.persistent.get().cloned()
    }

    pub fn has_native(&self) -> bool {
        self.resources.get().is_some()
    }

    /// Transport failure cannot revoke a usable native generation; an observed
    /// source inconsistency revokes new exports of the entire frozen lineage.
    pub(crate) fn native_is_locally_ready(&self) -> bool {
        use crate::snapshot::repository::PublicationState;
        self.has_native()
            && self.ensure_source_integrity().is_ok()
            && self
                .lifecycle
                .lock()
                .map(|state| {
                    state.source_integrity_failure.is_none()
                        && (matches!(
                            state.state,
                            PublicationState::LocalReady | PublicationState::RemoteReady
                        ) || (state.state == PublicationState::Failed
                            && matches!(
                                state.last_usable,
                                Some(PublicationState::LocalReady | PublicationState::RemoteReady)
                            )))
                })
                .unwrap_or(false)
    }

    /// Check the authoritative source health without borrowing publication
    /// locks across I/O. Kept separate from upload-pin closure: successful
    /// finalization also closes that pin, but leaves native exports usable.
    pub(crate) fn ensure_source_integrity(&self) -> Result<(), LocalSnapshotError> {
        fn check(
            snapshot: &Snapshot,
            visited: &mut HashSet<*const Snapshot>,
        ) -> Result<(), LocalSnapshotError> {
            if !visited.insert(std::ptr::from_ref(snapshot)) {
                return Ok(());
            }
            let failure = snapshot
                .lifecycle
                .lock()
                .map_err(|_| LocalSnapshotError::LockPoisoned)?
                .source_integrity_failure
                .clone();
            if let Some(failure) = failure {
                return Err(LocalSnapshotError::SourceIntegrity(failure));
            }
            if let Some(resources) = snapshot.resources.get() {
                for parent in resources.lineage.frozen_snapshots() {
                    check(parent, visited)?;
                }
            }
            Ok(())
        }
        check(self, &mut HashSet::new())
    }

    fn quarantine_source_integrity(
        &self,
        failure: DataIntegrityFailure,
    ) -> Result<(), LocalSnapshotError> {
        {
            let mut state = self
                .lifecycle
                .lock()
                .map_err(|_| LocalSnapshotError::LockPoisoned)?;
            if state.source_integrity_failure.is_none() {
                state.fail("source_integrity", failure.to_string());
                state.source_integrity_failure = Some(failure);
            }
            state.retry_scheduled = false;
            state.retry_at_unix_ms = None;
        }
        // Never take resource locks while holding the lifecycle lock. Closing
        // publication does not remove the catalog identity or native backing.
        self.close_upload_pin()
    }

    /// Opaque capture incarnation, stable across publication and unrelated to
    /// a canonical content SHA or to the daemon's physical inode numbers.
    pub(crate) fn native_generation_id(&self) -> Option<&str> {
        self.resources
            .get()
            .map(|resources| resources.native_generation_id.as_str())
    }

    #[cfg(test)]
    pub(crate) fn persistent_mut(&mut self) -> &mut PersistentSnapshot {
        self.persistent
            .get_mut()
            .expect("persistent snapshot fixture")
    }

    pub(crate) fn from_persistent(
        snapshot_id: String,
        persistent: PersistentSnapshot,
    ) -> Result<Arc<Self>, String> {
        let mut chains = Vec::new();
        if let Some(memory) = &persistent.memory {
            chains.push(memory.metadata()?);
        }
        chains.push(persistent.rootfs.metadata()?);
        let identity = crate::snapshot::metadata::snapshot_spec_checksum(
            &serde_json::to_value(&persistent.spec).map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
        let snapshot = Self::creating(snapshot_id, persistent.manifest.snapshot_kind);
        let mut state = SnapshotState::remote_ready(chains, identity);
        state.ancestor_snapshot_ids = persistent.ancestor_snapshot_ids.clone();
        *snapshot
            .lifecycle
            .lock()
            .map_err(|_| "snapshot state poisoned")? = state;
        snapshot.install_persistent(persistent)?;
        Ok(snapshot)
    }

    pub(crate) fn install_persistent(&self, source: PersistentSnapshot) -> Result<(), String> {
        if source.manifest.snapshot_id != self.snapshot_id
            || source.manifest.snapshot_kind != self.snapshot_kind
        {
            return Err("persistent metadata belongs to another snapshot".into());
        }
        if let Some(existing) = self.persistent.get() {
            return if existing == &source {
                Ok(())
            } else {
                Err("snapshot already owns a different persistent graph".into())
            };
        }
        match self.persistent.set(source) {
            Ok(()) => Ok(()),
            Err(candidate) if self.persistent.get() == Some(&candidate) => Ok(()),
            Err(_) => Err("snapshot persistent graph changed concurrently".into()),
        }
    }

    /// Publish immutable native resources into an existing Creating identity.
    /// Arc cloning preserves original inodes, pins and ancestor owners; no DATA
    /// is copied or encoded here. Conflicting resource sets cannot replace it.
    pub fn initialize_from(&self, source: &Snapshot) -> Result<(), String> {
        // Finalization can call this with self.lifecycle already locked. Only
        // the incoming native source needs a separate health check here.
        if source.has_native() && !std::ptr::eq(self, source) {
            source
                .ensure_source_integrity()
                .map_err(|error| error.to_string())?;
        }
        if self.snapshot_id != source.snapshot_id || self.snapshot_kind != source.snapshot_kind {
            return Err(
                "raw snapshot resources do not match the reserved snapshot identity".into(),
            );
        }
        if let Some(persistent) = source.persistent.get() {
            self.install_persistent(persistent.clone())?;
            if !source.has_native() {
                return Ok(());
            }
        }
        let resources = source
            .resources
            .get()
            .ok_or("raw source resources are not ready")?;
        if let Some(existing) = self.resources.get() {
            return if Arc::ptr_eq(existing, resources) {
                Ok(())
            } else {
                Err("raw snapshot already owns a different captured resource set".into())
            };
        }
        if self.resources.set(Arc::clone(resources)).is_ok()
            || self
                .resources
                .get()
                .is_some_and(|existing| Arc::ptr_eq(existing, resources))
        {
            Ok(())
        } else {
            Err("raw snapshot already owns a different captured resource set".into())
        }
    }

    fn initialized(
        snapshot_id: String,
        kind: SnapshotKind,
        resources: SnapshotResources,
    ) -> Arc<Self> {
        let instance = Self::creating(snapshot_id, kind);
        instance
            .resources
            .set(Arc::new(resources))
            .expect("new raw identity");
        instance
    }
}

impl SnapshotInstance for Snapshot {
    fn snapshot_id(&self) -> &str {
        &self.snapshot_id
    }
    fn snapshot_kind(&self) -> SnapshotKind {
        self.snapshot_kind
    }
    fn vm_state_reference(&self) -> Option<VmStateRef> {
        if let Some(persistent) = self.persistent.get() {
            return persistent.spec.vm_state.clone();
        }
        self.resources
            .get()
            .and_then(|resources| resources.vm_state_reference.clone())
    }
    fn ancestor_snapshot_ids(&self) -> &[String] {
        if let Some(persistent) = self.persistent.get() {
            return &persistent.ancestor_snapshot_ids;
        }
        self.resources
            .get()
            .map_or(&[], |resources| resources.ancestor_snapshot_ids.as_slice())
    }
    fn layer_chain(
        &self,
        kind: LayerKind,
    ) -> Result<Option<crate::snapshot::metadata::SnapshotChainMetadata>, String> {
        if !self.is_initialized() {
            return Err("snapshot resources are not ready".into());
        }
        if let Some(resources) = self.resources.get() {
            return Ok(resources.chains.get(&kind).cloned());
        }
        self.chain_view(kind)
            .map(crate::snapshot::metadata::SnapshotChainView::metadata)
            .transpose()
    }
    fn lifecycle(&self) -> &Mutex<SnapshotState> {
        &self.lifecycle
    }
    fn layer_reader(
        &self,
        kind: LayerKind,
        content_identity: &str,
    ) -> Result<Arc<dyn LazyLoadInterface>, LazyLoadError> {
        if let Some(view) = self.chain_view(kind) {
            match view.layer_reader(content_identity) {
                Err(LazyLoadError::IdentityConflict) => {}
                result => return result,
            }
        }
        if !self.has_native() {
            return Err(LazyLoadError::LocalIo(
                "raw snapshot resources are not ready".into(),
            ));
        }
        let chain = SnapshotInstance::layer_chain(self, kind)
            .map_err(|_| LazyLoadError::IdentityConflict)?
            .ok_or(LazyLoadError::IdentityConflict)?;
        let metadata = chain
            .layers
            .into_iter()
            .find(|layer| same_layer_identity(&layer.content_identity, content_identity))
            .ok_or(LazyLoadError::IdentityConflict)?;
        for entry in self.lineage.entries() {
            match entry {
                SnapshotLineageEntry::Remote {
                    kind: entry_kind,
                    chain_index,
                    layers,
                } if *entry_kind == kind => {
                    if let Some(layer) = layers.iter().find(|layer| {
                        layer.layer_id == metadata.layer_id
                            && layer.matches_content_identity(&metadata.content_identity)
                    }) {
                        let declared = chain_index
                            .layers
                            .get(layer.ordinal as usize)
                            .filter(|declared| {
                                declared.ordinal == u64::from(layer.ordinal)
                                    && declared.layer_id == metadata.layer_id
                                    && same_layer_identity(
                                        &declared.content_sha256,
                                        &metadata.content_identity,
                                    )
                            })
                            .ok_or(LazyLoadError::IdentityConflict)?;
                        return layer.format_reader(declared.range_map.encoding);
                    }
                }
                SnapshotLineageEntry::FrozenLocal(ancestor) => {
                    if let Some(layer) = ancestor.frozen_layers.get(&kind).filter(|layer| {
                        layer.layer_id == metadata.layer_id
                            && same_layer_identity(
                                &layer.provisional_content_id,
                                &metadata.content_identity,
                            )
                    }) {
                        return ancestor.raw_layer_reader(kind, layer, metadata);
                    }
                }
                _ => {}
            }
        }
        if let Some(layer) = self.frozen_layers.get(&kind).filter(|layer| {
            layer.layer_id == metadata.layer_id
                && same_layer_identity(&layer.provisional_content_id, &metadata.content_identity)
        }) {
            return self.raw_layer_reader(kind, layer, metadata);
        }
        Err(LazyLoadError::IdentityConflict)
    }

    fn spec(&self) -> Option<&crate::snapshot::metadata::SnapshotSpec> {
        self.persistent.get().map(|source| &source.spec)
    }
    fn provenance(&self) -> Option<&SnapshotProvenance> {
        self.resources
            .get()
            .and_then(|resources| resources.provenance.as_ref())
            .or_else(|| self.spec().and_then(|spec| spec.provenance.as_ref()))
    }
    fn manifest(&self) -> Option<&crate::snapshot::metadata::SnapshotManifest> {
        self.persistent.get().map(|source| &source.manifest)
    }
    fn vm_state(&self) -> Option<&[u8]> {
        self.persistent
            .get()
            .and_then(|source| source.vm_state.as_deref())
            .or_else(|| {
                self.resources.get().and_then(|resources| {
                    resources
                        .vm_state
                        .as_ref()
                        .map(|state| state.payload.as_slice())
                })
            })
    }
    fn chain_view(&self, kind: LayerKind) -> Option<&crate::snapshot::metadata::SnapshotChainView> {
        let source = self.persistent.get()?;
        match kind {
            LayerKind::Memory => source.memory.as_ref(),
            LayerKind::Block => Some(&source.rootfs),
        }
    }
}

fn same_layer_identity(left: &str, right: &str) -> bool {
    left.trim_start_matches("sha256:") == right.trim_start_matches("sha256:")
}

impl Snapshot {
    fn raw_layer_reader(
        &self,
        kind: LayerKind,
        layer: &FrozenLocalLayer,
        metadata: SnapshotLayerMetadata,
    ) -> Result<Arc<dyn LazyLoadInterface>, LazyLoadError> {
        if layer.unit_size_bytes == 0
            || !layer.logical_bytes.is_multiple_of(layer.unit_size_bytes)
            || layer.ownership().logical_size != layer.logical_bytes
            || layer.ownership().granularity != layer.unit_size_bytes
        {
            return Err(LazyLoadError::IdentityConflict);
        }
        let mut data_ranges: Vec<(u64, u64)> = Vec::new();
        for unit in 0..layer.logical_bytes / layer.unit_size_bytes {
            match packed_ownership_state(&layer.ownership().packed_states, unit) {
                Some(1) => {
                    let offset = unit * layer.unit_size_bytes;
                    if let Some(last) = data_ranges.last_mut() {
                        if last.0 + last.1 == offset {
                            last.1 += layer.unit_size_bytes;
                            continue;
                        }
                    }
                    data_ranges.push((offset, layer.unit_size_bytes));
                }
                Some(0 | 2) => {}
                _ => return Err(LazyLoadError::IdentityConflict),
            }
        }
        Ok(crate::snapshot::format::raw::RawLayerReader::new(
            metadata,
            kind,
            data_ranges,
            layer.backing.clone(),
        )?)
    }

    pub fn from_base_layer(
        snapshot_id: String,
        layer: FrozenLayerPin,
        provenance: SnapshotProvenance,
    ) -> Result<Arc<Self>, LocalSnapshotError> {
        if snapshot_id.is_empty() || snapshot_id.len() > 4096 {
            return Err(LocalSnapshotError::Invalid(
                "BASE_IMAGE snapshot identity length must be 1..=4096".into(),
            ));
        }
        if layer.descriptor.kind != LayerKind::Block {
            return Err(LocalSnapshotError::Invalid(
                "BASE_IMAGE contains a non-block frozen layer".into(),
            ));
        }
        layer.validate()?;
        if layer.ownership.snapshot_id != snapshot_id {
            return Err(LocalSnapshotError::Invalid(
                "BASE_IMAGE ownership snapshot ID does not match the snapshot".into(),
            ));
        }
        let backing = LocalBackingHandle::from_native(layer)?;
        let frozen_layers =
            HashMap::from([(LayerKind::Block, FrozenLocalLayer::from_backing(backing)?)]);
        Self::validate_frozen_parts(&frozen_layers, None, SnapshotKind::BaseImage)?;
        let (ancestor_snapshot_ids, chains, vm_state_reference) = Self::build_snapshot_metadata(
            &snapshot_id,
            SnapshotKind::BaseImage,
            &SnapshotLineage::default(),
            &frozen_layers,
            None,
        )?;
        Ok(Self::initialized(
            snapshot_id,
            SnapshotKind::BaseImage,
            SnapshotResources {
                native_generation_id: uuid::Uuid::new_v4().to_string(),
                ancestor_snapshot_ids,
                chains,
                vm_state_reference,
                pin: Mutex::new(Some(Arc::new(()))),
                frozen_layers,
                canonicalize: Mutex::new(()),
                canonical: Mutex::new(None),
                lineage: SnapshotLineage::default(),
                vm_state: None,
                rocklet_artifact: None,
                provenance: Some(provenance),
                retained_layer_resources: Mutex::new(Vec::new()),
            },
        ))
    }

    /// Complete a previously reserved BASE_IMAGE instance in place.
    pub fn adopt_base_layer(
        self: &Arc<Self>,
        layer: FrozenLayerPin,
        provenance: SnapshotProvenance,
    ) -> Result<Arc<Self>, LocalSnapshotError> {
        if self.snapshot_kind != SnapshotKind::BaseImage || self.is_initialized() {
            return Err(LocalSnapshotError::Invalid(
                "BASE_IMAGE reservation is occupied or has the wrong kind".into(),
            ));
        }
        let local = Self::from_base_layer(self.snapshot_id.clone(), layer, provenance)?;
        self.initialize_from(local.as_ref())
            .map_err(LocalSnapshotError::Invalid)?;
        Ok(Arc::clone(self))
    }

    /// Validate immutable ancestry and append this snapshot without a public
    /// draft/frozen metadata object or any DATA encoding/transport work.
    fn build_snapshot_metadata(
        snapshot_id: &str,
        snapshot_kind: SnapshotKind,
        lineage: &SnapshotLineage,
        frozen_layers: &HashMap<LayerKind, FrozenLocalLayer>,
        vm_state: Option<&LocalVmState>,
    ) -> Result<
        (
            Vec<String>,
            HashMap<LayerKind, crate::snapshot::metadata::SnapshotChainMetadata>,
            Option<VmStateRef>,
        ),
        LocalSnapshotError,
    > {
        use crate::snapshot::metadata::{SnapshotChainMetadata, MAX_CHAIN_DEPTH};
        let invalid = |message: &str| LocalSnapshotError::Invalid(message.into());
        if snapshot_id.is_empty() || snapshot_id.len() > 4096 {
            return Err(invalid(
                "snapshot ID is empty or exceeds the metadata bound",
            ));
        }
        let mut ancestors = Vec::new();
        let mut seen_ancestors = HashSet::new();
        let mut chains = HashMap::new();
        if let Some(parent) = lineage.frozen_snapshots().last() {
            for ancestor in parent
                .ancestor_snapshot_ids()
                .iter()
                .chain(std::iter::once(&parent.snapshot_id))
            {
                if ancestor.is_empty() || ancestor == snapshot_id {
                    return Err(invalid("snapshot ancestry is empty or self-referential"));
                }
                if seen_ancestors.insert(ancestor.clone()) {
                    ancestors.push(ancestor.clone());
                }
            }
            for kind in [LayerKind::Memory, LayerKind::Block] {
                if let Some(chain) = SnapshotInstance::layer_chain(parent.as_ref(), kind)
                    .map_err(LocalSnapshotError::Invalid)?
                {
                    chains.insert(kind, chain);
                }
            }
        } else {
            for entry in lineage.entries() {
                if let SnapshotLineageEntry::Remote {
                    kind, chain_index, ..
                } = entry
                {
                    let chain = SnapshotChainMetadata {
                        kind: *kind,
                        logical_bytes: chain_index.logical_bytes,
                        unit_size_bytes: chain_index.unit_size_bytes,
                        layers: chain_index
                            .layers
                            .iter()
                            .map(|layer| {
                                Ok(SnapshotLayerMetadata {
                                    ordinal: layer.ordinal,
                                    layer_id: layer.layer_id.clone(),
                                    content_identity: layer.content_sha256.clone(),
                                    format: crate::snapshot::metadata::encoding_name(
                                        layer.range_map.encoding,
                                    )
                                    .map_err(LocalSnapshotError::Invalid)?
                                    .into(),
                                    logical_bytes: chain_index.logical_bytes,
                                    unit_size_bytes: chain_index.unit_size_bytes,
                                })
                            })
                            .collect::<Result<Vec<_>, LocalSnapshotError>>()?,
                    };
                    if chains.insert(*kind, chain).is_some() {
                        return Err(invalid("duplicate immutable ancestor chain"));
                    }
                }
            }
        }
        for ancestor in lineage.ancestor_snapshot_ids() {
            if ancestor.is_empty() || ancestor == snapshot_id {
                return Err(invalid("snapshot ancestry is empty or self-referential"));
            }
            if seen_ancestors.insert(ancestor.clone()) {
                ancestors.push(ancestor);
            }
        }
        for kind in [LayerKind::Memory, LayerKind::Block] {
            if let Some(layer) = frozen_layers.get(&kind) {
                let chain = chains.entry(kind).or_insert_with(|| SnapshotChainMetadata {
                    kind,
                    logical_bytes: layer.logical_bytes,
                    unit_size_bytes: layer.unit_size_bytes,
                    layers: Vec::new(),
                });
                chain.layers.push(SnapshotLayerMetadata {
                    ordinal: chain.layers.len() as u64,
                    layer_id: layer.layer_id.clone(),
                    content_identity: layer.provisional_content_id.clone(),
                    format: "raw".into(),
                    logical_bytes: layer.logical_bytes,
                    unit_size_bytes: layer.unit_size_bytes,
                });
            }
        }
        for chain in chains.values() {
            if chain.layers.is_empty()
                || chain.layers.len() > MAX_CHAIN_DEPTH
                || chain.logical_bytes == 0
                || chain.unit_size_bytes == 0
                || !chain.logical_bytes.is_multiple_of(chain.unit_size_bytes)
            {
                return Err(invalid("snapshot chain geometry or depth is invalid"));
            }
            let mut layer_ids = HashSet::new();
            for (ordinal, layer) in chain.layers.iter().enumerate() {
                if layer.ordinal != ordinal as u64
                    || layer.layer_id.is_empty()
                    || layer.content_identity.is_empty()
                    || !layer_ids.insert(&layer.layer_id)
                    || layer.logical_bytes != chain.logical_bytes
                    || layer.unit_size_bytes == 0
                    || !layer.logical_bytes.is_multiple_of(layer.unit_size_bytes)
                    || match chain.kind {
                        LayerKind::Memory => {
                            layer.unit_size_bytes != 4096 || chain.unit_size_bytes != 4096
                        }
                        LayerKind::Block => {
                            !layer.unit_size_bytes.is_multiple_of(chain.unit_size_bytes)
                                && !chain.unit_size_bytes.is_multiple_of(layer.unit_size_bytes)
                        }
                    }
                {
                    return Err(invalid("layer metadata conflicts with immutable ancestry"));
                }
            }
        }
        let vm_state_reference = vm_state.map(|state| VmStateRef {
            object_key: state.object_key.clone(),
            sha256: state.sha256.clone(),
            object_version: None,
            stored_bytes: state.payload.len() as u64,
        });
        if let Some(state) = &vm_state_reference {
            if state.object_key.is_empty() || state.stored_bytes == 0 {
                return Err(invalid("VM state metadata is empty"));
            }
            crate::snapshot::metadata::validate_sha256(&state.sha256)
                .map_err(LocalSnapshotError::Invalid)?;
        }
        if !chains.contains_key(&LayerKind::Block) {
            return Err(invalid("snapshot metadata must contain a block chain"));
        }
        match snapshot_kind {
            SnapshotKind::Full
                if !chains.contains_key(&LayerKind::Memory) || vm_state_reference.is_none() =>
            {
                return Err(invalid(
                    "FULL snapshot requires memory and VM state metadata",
                ));
            }
            SnapshotKind::BaseImage
                if chains.contains_key(&LayerKind::Memory) || vm_state_reference.is_some() =>
            {
                return Err(invalid(
                    "BASE_IMAGE snapshot cannot contain memory or VM state metadata",
                ));
            }
            _ => {}
        }
        Ok((ancestors, chains, vm_state_reference))
    }

    fn validate_frozen_parts(
        layers: &HashMap<LayerKind, FrozenLocalLayer>,
        vm_state: Option<&LocalVmState>,
        snapshot_kind: SnapshotKind,
    ) -> Result<(), LocalSnapshotError> {
        if layers.is_empty() {
            return Err(LocalSnapshotError::Invalid(
                "Frozen local snapshot must contain at least one layer".into(),
            ));
        }
        match snapshot_kind {
            SnapshotKind::BaseImage
                if layers.len() != 1
                    || !layers.contains_key(&LayerKind::Block)
                    || vm_state.is_some() =>
            {
                return Err(LocalSnapshotError::Invalid(
                    "BASE_IMAGE must contain cold-boot rootfs only".into(),
                ));
            }
            SnapshotKind::Full
                if !layers.contains_key(&LayerKind::Memory)
                    || !layers.contains_key(&LayerKind::Block)
                    || vm_state.is_none() =>
            {
                return Err(LocalSnapshotError::Invalid(
                    "FULL snapshot requires memory, rootfs and VM state".into(),
                ));
            }
            _ => {}
        }
        for (kind, layer) in layers {
            if layer.kind != *kind
                || layer.layer_id.is_empty()
                || layer.logical_bytes == 0
                || layer.unit_size_bytes == 0
                || !layer.logical_bytes.is_multiple_of(layer.unit_size_bytes)
            {
                return Err(LocalSnapshotError::Invalid(
                    "Frozen local layer identity or geometry is invalid".into(),
                ));
            }
        }
        Ok(())
    }

    /// Rehome a committed publication using only already-authenticated metadata.
    /// This must not call canonical_snapshot: finalization never reads, encodes
    /// or uploads DATA, even if an ancestor's upload lease has been closed.
    pub(crate) fn published_snapshot(
        &self,
        published: &crate::snapshot::format::UploadedSnapshot,
    ) -> Result<Arc<Snapshot>, String> {
        use crate::snapshot::format::decode_layer;
        use crate::snapshot::format::overlaybd_s3::{
            publication::OverlaybdS3Publication, OverlaybdS3Snapshot,
        };
        use crate::snapshot::metadata::{
            canonical_json_bytes, sha256_identity, ChainIndexRef, LayerRangeMap, ManifestChainRef,
            SnapshotChainView, SnapshotLayerView, StorageProvider, MAX_CHAIN_DEPTH,
        };
        use crate::snapshot::ValidateCanonicalModel;

        fn add_layer(
            layers: &mut Vec<SnapshotLayerView>,
            reference: &LayerRef,
            map: Arc<LayerRangeMap>,
            kind: LayerKind,
        ) -> Result<(), String> {
            map.validate()?;
            if map.kind != kind
                || map.content_sha256 != reference.content_sha256
                || map.format_version != reference.format_version
            {
                return Err("retained layer map conflicts with its authenticated reference".into());
            }
            if let Some(previous) = layers
                .iter()
                .find(|layer| layer.layer_ref == *reference && layer.range_map.kind == kind)
            {
                if previous.range_map != map {
                    return Err("retained layer reference has conflicting decoded metadata".into());
                }
            } else {
                layers.push(SnapshotLayerView {
                    layer_ref: reference.clone(),
                    range_map: map,
                });
            }
            Ok(())
        }

        fn collect_layers(
            snapshot: &Snapshot,
            visited: &mut HashSet<usize>,
            layers: &mut Vec<SnapshotLayerView>,
        ) -> Result<(), String> {
            if !visited.insert(snapshot as *const Snapshot as usize) {
                return Ok(());
            }
            if visited.len() > MAX_CHAIN_DEPTH {
                return Err("raw publication ancestry exceeds the chain bound".into());
            }
            let resources = snapshot
                .resources
                .get()
                .ok_or("raw snapshot resources are not ready")?;
            let canonical = resources
                .canonical
                .lock()
                .map_err(|_| "raw canonical cache lock poisoned")?
                .clone()
                .ok_or("published raw snapshot resources have no prepared canonical metadata")?;
            validate_canonical_parts(
                &snapshot.snapshot_id,
                &canonical.layers,
                &canonical.chain_indexes,
                resources.vm_state.as_ref(),
                snapshot.snapshot_kind,
            )
            .map_err(|error| error.to_string())?;
            for (kind, layer) in &canonical.layers {
                let head = canonical
                    .chain_indexes
                    .get(kind)
                    .and_then(|chain| chain.index.layers.last())
                    .ok_or("canonical layer is absent from its chain")?;
                if sha256_identity(&layer.range_map_bytes) != head.range_map.sha256 {
                    return Err(
                        "canonical descriptor hash conflicts with its layer reference".into(),
                    );
                }
                let decoded = decode_layer(head.range_map.encoding, &layer.range_map_bytes)
                    .map_err(|error| error.to_string())?;
                if decoded.range_map != *layer.range_map {
                    return Err("canonical descriptor differs from its retained map".into());
                }
                add_layer(layers, head, Arc::clone(&layer.range_map), *kind)?;
            }
            for entry in resources.lineage.entries() {
                match entry {
                    SnapshotLineageEntry::FrozenLocal(parent) => {
                        collect_layers(parent, visited, layers)?
                    }
                    SnapshotLineageEntry::Remote {
                        kind,
                        chain_index,
                        layers: sources,
                    } => {
                        chain_index.validate()?;
                        for source in sources {
                            // The original chain and this decoded map were authenticated
                            // together at load. Preserve the entire descriptor reference,
                            // including SHA, codec and external source pin, across rehome.
                            let declared = chain_index
                                .layers
                                .get(source.ordinal as usize)
                                .filter(|declared| {
                                    declared.layer_id == source.layer_id
                                        && declared.content_sha256
                                            == source.range_map.content_sha256
                                })
                                .ok_or(
                                    "retained remote layer is absent from its authenticated chain",
                                )?;
                            if source.range_map.logical_bytes != chain_index.logical_bytes
                                || source.range_map.unit_size_bytes != chain_index.unit_size_bytes
                            {
                                return Err(
                                    "retained remote layer geometry differs from its chain".into(),
                                );
                            }
                            add_layer(layers, declared, Arc::clone(&source.range_map), *kind)?;
                        }
                    }
                }
            }
            Ok(())
        }

        if !self.is_initialized() {
            return Err("raw snapshot resources are not ready".into());
        }
        OverlaybdS3Publication
            .validate_published(published)
            .map_err(|error| error.to_string())?;
        crate::snapshot::format::validate_snapshot_paths(&published.spec, &self.snapshot_id)
            .map_err(|error| error.to_string())?;
        let manifest_chain = |reference: &ChainIndexRef| ManifestChainRef {
            object_key: reference.index_object_key.clone(),
            sha256: reference.index_sha256.clone(),
            logical_bytes: reference.logical_bytes,
            object_version: reference.object_version.clone(),
        };
        if (published.spec.storage.provider == StorageProvider::Registry
            && published.spec.registry_snapshot.is_none())
            || published.manifest.snapshot_id != self.snapshot_id
            || published.manifest.snapshot_kind != self.snapshot_kind
            || published.manifest.format_version != published.spec.format_version
            || published.manifest.rootfs != manifest_chain(&published.spec.rootfs)
            || published.manifest.memory != published.spec.memory.as_ref().map(manifest_chain)
            || published.spec.compatibility.rocklet_artifact != self.rocklet_artifact
            || published.spec.provenance != self.provenance
            || published.spec.vm_state != self.vm_state_reference()
        {
            return Err("published metadata differs from its captured raw snapshot".into());
        }
        let canonical = self
            .resources
            .get()
            .ok_or("raw snapshot resources are not ready")?
            .canonical
            .lock()
            .map_err(|_| "raw canonical cache lock poisoned")?
            .clone()
            .ok_or("published raw snapshot resources have no prepared canonical metadata")?;
        let mut candidates = Vec::new();
        collect_layers(self, &mut HashSet::new(), &mut candidates)?;
        let chain_view = |reference: &ChainIndexRef,
                          kind: LayerKind|
         -> Result<SnapshotChainView, String> {
            let canonical = canonical
                .chain_indexes
                .get(&kind)
                .ok_or("published chain has no prepared metadata")?;
            canonical.index.validate()?;
            let payload =
                canonical_json_bytes(&canonical.index).map_err(|error| error.to_string())?;
            if canonical.index.kind != kind
                || canonical.payload != payload
                || canonical.sha256 != sha256_identity(&payload)
                || reference.index_object_key != canonical.object_key
                || reference.index_sha256 != canonical.sha256
                || reference.logical_bytes != canonical.index.logical_bytes
                || reference.object_version.is_some()
                || reference.source_ref.is_some()
            {
                return Err("published chain differs from its prepared canonical metadata".into());
            }
            let mut layers = Vec::with_capacity(canonical.index.layers.len());
            for declared in &canonical.index.layers {
                let layer = candidates
                    .iter()
                    .find(|layer| layer.layer_ref == *declared && layer.range_map.kind == kind)
                    .ok_or("published layer has no retained authenticated metadata")?;
                if layer.range_map.logical_bytes != canonical.index.logical_bytes
                    || layer.range_map.unit_size_bytes != canonical.index.unit_size_bytes
                {
                    return Err("published layer geometry conflicts with its chain".into());
                }
                let mut layer = layer.clone();
                if let Some(objects) = &published.registry_objects {
                    let mut map = layer.range_map.as_ref().clone();
                    map.registry_objects = Some(Arc::clone(objects));
                    layer.range_map = Arc::new(map);
                }
                layers.push(layer);
            }
            Ok(SnapshotChainView {
                index: canonical.index.clone(),
                layers,
            })
        };
        let rootfs = chain_view(&published.spec.rootfs, LayerKind::Block)?;
        let memory = published
            .spec
            .memory
            .as_ref()
            .map(|reference| chain_view(reference, LayerKind::Memory))
            .transpose()?;
        OverlaybdS3Snapshot::new_with_ancestors(
            self.snapshot_id.clone(),
            published.spec.clone(),
            published.manifest.clone(),
            memory,
            rootfs,
            self.vm_state.as_ref().map(|state| state.payload.clone()),
            self.ancestor_snapshot_ids().to_vec(),
        )
    }

    pub(crate) fn canonical_snapshot(
        &self,
    ) -> Result<Arc<CanonicalLocalSnapshot>, LocalSnapshotError> {
        self.canonical_snapshot_with_scratch(&create_temporary_staged_object)
    }

    fn canonical_snapshot_with_scratch(
        &self,
        create_scratch: &dyn Fn(u64) -> Result<Arc<dyn StagedObjectWriter>, FrozenLayerError>,
    ) -> Result<Arc<CanonicalLocalSnapshot>, LocalSnapshotError> {
        self.ensure_source_integrity()?;
        if !self.has_native() {
            return Err(LocalSnapshotError::Frozen(
                "Snapshot has no native capture".into(),
            ));
        }
        if let Some(canonical) = self
            .canonical
            .lock()
            .map_err(|_| LocalSnapshotError::LockPoisoned)?
            .as_ref()
        {
            return Ok(Arc::clone(canonical));
        }
        let _encoder = self
            .canonicalize
            .lock()
            .map_err(|_| LocalSnapshotError::LockPoisoned)?;
        if let Some(canonical) = self
            .canonical
            .lock()
            .map_err(|_| LocalSnapshotError::LockPoisoned)?
            .as_ref()
        {
            return Ok(Arc::clone(canonical));
        }
        // This logical lease fences cancellation. Each native reader below
        // separately excludes reclamation while the format consumes its DATA.
        let pin = self
            .pin
            .lock()
            .map_err(|_| LocalSnapshotError::LockPoisoned)?
            .as_ref()
            .cloned()
            .ok_or_else(|| {
                LocalSnapshotError::Frozen("LOCAL_READY upload source pin is closed".into())
            })?;
        let mut parent_chains = HashMap::new();
        for entry in self.lineage.entries() {
            match entry {
                SnapshotLineageEntry::Remote {
                    kind, chain_index, ..
                } => {
                    parent_chains.insert(*kind, chain_index.clone());
                }
                SnapshotLineageEntry::FrozenLocal(parent) => {
                    let canonical = parent.canonical_snapshot_with_scratch(create_scratch)?;
                    for (kind, chain) in &canonical.chain_indexes {
                        parent_chains.insert(*kind, chain.index.clone());
                    }
                }
            }
        }
        let mut layers = HashMap::new();
        let mut native_readers = Vec::new();
        for (&kind, layer) in &self.frozen_layers {
            let reader = layer.backing.backing.pin_native_reader()?;
            let native = layer
                .backing
                .backing
                .native_metadata()
                .expect("frozen local layers have native metadata");
            let canonical = encode_frozen_layer(
                FrozenLayerInput {
                    kind,
                    layer_id: &native.descriptor.layer_id,
                    logical_bytes: native.descriptor.logical_size,
                    unit_size_bytes: native.ownership.granularity,
                    packed_states: &native.ownership.packed_states,
                    file: reader.file(),
                },
                parent_chains.contains_key(&kind),
                parent_chains
                    .get(&kind)
                    .map(|parent| parent.unit_size_bytes),
                &|max_bytes| {
                    create_scratch(max_bytes).map_err(|error| match error {
                        FrozenLayerError::SourceIntegrity(failure) => {
                            FormatCaptureError::SourceIntegrity(failure)
                        }
                        FrozenLayerError::Io(message) => FormatCaptureError::Io(message),
                        error => FormatCaptureError::Invalid(error.to_string()),
                    })
                },
            );
            let canonical = match canonical {
                Ok(canonical) => canonical,
                Err(FormatCaptureError::SourceIntegrity(failure)) => {
                    // Preserve the typed integrity cause if pin cleanup finds
                    // a poisoned lock. The sticky state (or a poisoned state
                    // lock itself) still makes admission fail closed.
                    let _ = self.quarantine_source_integrity(failure.clone());
                    return Err(LocalSnapshotError::SourceIntegrity(failure));
                }
                Err(error) => return Err(error.into()),
            };
            native_readers.push(reader);
            if layers.insert(kind, canonical).is_some() {
                return Err(LocalSnapshotError::Invalid(
                    "Frozen snapshot contains a duplicate layer kind".into(),
                ));
            }
        }
        let chain_indexes = layers
            .iter()
            .map(|(kind, layer)| {
                build_chain_index(&self.snapshot_id, *kind, layer, parent_chains.get(kind))
                    .map(|chain| (*kind, chain))
            })
            .collect::<Result<HashMap<_, _>, _>>()?;
        validate_canonical_parts(
            &self.snapshot_id,
            &layers,
            &chain_indexes,
            self.vm_state.as_ref(),
            self.snapshot_kind,
        )?;
        let canonical = Arc::new(CanonicalLocalSnapshot {
            layers,
            chain_indexes,
        });
        self.ensure_source_integrity()?;
        // Install under the short authoritative-lease lock. If close won the
        // race, discard this work and its temporary files. If install won,
        // close will find the cache and release its temporary-file ownership.
        let authoritative = self
            .pin
            .lock()
            .map_err(|_| LocalSnapshotError::LockPoisoned)?;
        if !authoritative
            .as_ref()
            .is_some_and(|active| Arc::ptr_eq(active, &pin))
        {
            return Err(LocalSnapshotError::Frozen(
                "LOCAL_READY upload source pin closed during encoding".into(),
            ));
        }
        let mut slot = self
            .canonical
            .lock()
            .map_err(|_| LocalSnapshotError::LockPoisoned)?;
        for (kind, layer) in &canonical.layers {
            let native = &self.frozen_layers[kind];
            if native.data_units() != 0 {
                native
                    .backing
                    .backing
                    .install_recovery(backing_recovery(layer)?);
            }
        }
        *slot = Some(Arc::clone(&canonical));
        // Every recovery source is now installed before readers may trigger
        // reclamation of units without runtime dependencies.
        drop(native_readers);
        Ok(canonical)
    }

    pub fn canonical_chain_index(
        &self,
        kind: LayerKind,
    ) -> Result<LocalChainIndex, LocalSnapshotError> {
        self.canonical_snapshot()?
            .chain_indexes
            .get(&kind)
            .cloned()
            .ok_or_else(|| {
                LocalSnapshotError::Invalid(format!("Local snapshot has no {kind:?} chain"))
            })
    }

    pub fn head_layer_ref(&self, kind: LayerKind) -> Result<LayerRef, LocalSnapshotError> {
        self.canonical_chain_index(kind)?
            .index
            .layers
            .last()
            .cloned()
            .ok_or_else(|| LocalSnapshotError::Invalid("Local snapshot chain is empty".into()))
    }

    pub(crate) fn lineage(&self) -> &SnapshotLineage {
        &self.lineage
    }

    pub(crate) fn remote_parent_chains(&self) -> HashMap<LayerKind, LayerChainIndex> {
        self.lineage
            .entries()
            .iter()
            .filter_map(|entry| match entry {
                SnapshotLineageEntry::Remote {
                    kind, chain_index, ..
                } => Some((*kind, chain_index.clone())),
                SnapshotLineageEntry::FrozenLocal(_) => None,
            })
            .collect()
    }

    pub fn is_closed(&self) -> Result<bool, LocalSnapshotError> {
        if !self.has_native() {
            return Ok(true);
        }
        self.pin
            .lock()
            .map(|pin| pin.is_none())
            .map_err(|_| LocalSnapshotError::LockPoisoned)
    }

    pub fn close_upload_pin(&self) -> Result<(), LocalSnapshotError> {
        if !self.has_native() {
            return Ok(());
        }
        self.pin
            .lock()
            .map_err(|_| LocalSnapshotError::LockPoisoned)?
            .take();
        let canonical = self
            .canonical
            .lock()
            .map_err(|_| LocalSnapshotError::LockPoisoned)?
            .clone();
        if let Some(canonical) = canonical {
            for layer in canonical.layers.values() {
                layer.blob.release_sealed_object()?;
            }
        }
        Ok(())
    }

    /// Retain the same immutable layer resources behind snapshot-scoped target
    /// aliases. No VM object, attachment index or DATA copy enters Snapshot.
    pub(crate) fn retain_layer_resources(
        &self,
        resources: Vec<Arc<crate::snapshot::io::layer_resources::LayerResources>>,
    ) -> Result<(), LocalSnapshotError> {
        let native = self.resources.get().ok_or_else(|| {
            LocalSnapshotError::Frozen("raw snapshot resources are not ready".into())
        })?;
        let mut retained = native
            .retained_layer_resources
            .lock()
            .map_err(|_| LocalSnapshotError::LockPoisoned)?;
        for resource in resources {
            if !retained
                .iter()
                .any(|existing| Arc::ptr_eq(existing, &resource))
            {
                retained.push(resource);
            }
        }
        Ok(())
    }

    /// Pin native DATA while a direct reader or exported FD can observe it.
    /// Keep this lease alive until all duplicated descriptors have finished
    /// reading or the runtime dependency ledger has taken over ownership.
    /// Async exports of reclaimed DATA use LocalBacking::restore_and_pin_native_reader.
    pub fn pin_native_layer(
        &self,
        kind: LayerKind,
    ) -> Result<crate::snapshot::io::backing::NativeReadLease, LocalSnapshotError> {
        // Resource projections are also used by repository callbacks while the
        // lifecycle lock is held. Admission and export boundaries check source
        // integrity before calling these accessors.
        Ok(self.backing_handle(kind)?.backing.pin_native_reader()?)
    }

    pub fn backing_handle(
        &self,
        kind: LayerKind,
    ) -> Result<LocalBackingHandle, LocalSnapshotError> {
        self.resources
            .get()
            .and_then(|resources| resources.frozen_layers.get(&kind))
            .map(|layer| layer.backing.clone())
            .ok_or_else(|| {
                LocalSnapshotError::Frozen(format!("Snapshot has no native {kind:?} layer"))
            })
    }

    /// Restore DATA before exporting direct FD/mapping capabilities. Historical
    /// snapshots own recovery metadata, while their zero-dependent native pages
    /// may have been reclaimed independently of this snapshot identity.
    pub async fn ensure_native_resident(&self) -> Result<(), LocalSnapshotError> {
        self.ensure_source_integrity()?;
        let Some(resources) = self.resources.get() else {
            return Ok(());
        };
        for layer in resources.frozen_layers.values() {
            let mut ranges = Vec::new();
            for unit in 0..layer.logical_bytes / layer.unit_size_bytes {
                if packed_ownership_state(&layer.ownership().packed_states, unit) == Some(1) {
                    ranges.push((unit * layer.unit_size_bytes, layer.unit_size_bytes));
                }
            }
            layer.backing.backing.restore_ranges(&ranges).await?;
        }
        for parent in self.lineage.frozen_snapshots() {
            // Boxed recursion keeps the future bounded independently of depth.
            Box::pin(parent.ensure_native_resident()).await?;
        }
        self.ensure_source_integrity()?;
        Ok(())
    }

    pub fn upload_plan(
        &self,
        storage: StorageDescriptor,
        compatibility: VmmCompatibility,
    ) -> Result<SnapshotUploadPlan, LocalSnapshotError> {
        self.upload_plan_with_scratch(storage, compatibility, &create_temporary_staged_object)
    }

    pub(crate) fn upload_plan_with_scratch(
        &self,
        storage: StorageDescriptor,
        compatibility: VmmCompatibility,
        create_scratch: &dyn Fn(u64) -> Result<Arc<dyn StagedObjectWriter>, FrozenLayerError>,
    ) -> Result<SnapshotUploadPlan, LocalSnapshotError> {
        crate::snapshot::format::overlaybd_s3::publication::OverlaybdS3Publication
            .prepare_upload(self, storage, compatibility, &|max_bytes| {
                create_scratch(max_bytes).map_err(snapshot_upload_error_from_frozen)
            })
            .map_err(LocalSnapshotError::from)
    }
}

impl crate::snapshot::format::SnapshotUploadSource for Snapshot {
    fn registry_sources(
        &self,
    ) -> Vec<Arc<crate::snapshot::format::overlaybd_oci::RegistryObjectMap>> {
        fn add(
            result: &mut Vec<Arc<crate::snapshot::format::overlaybd_oci::RegistryObjectMap>>,
            map: &crate::snapshot::metadata::LayerRangeMap,
        ) {
            if let Some(source) = &map.registry_objects {
                if !result.iter().any(|other| Arc::ptr_eq(other, source)) {
                    result.push(Arc::clone(source));
                }
            }
        }
        fn collect(
            snapshot: &Snapshot,
            visited: &mut HashSet<*const Snapshot>,
            result: &mut Vec<Arc<crate::snapshot::format::overlaybd_oci::RegistryObjectMap>>,
        ) {
            if !visited.insert(std::ptr::from_ref(snapshot)) {
                return;
            }
            // Publication is authoritative before Admin finalization installs
            // the persistent graph; release its short lock before ancestry.
            let published_objects = {
                let state = snapshot.lifecycle.lock().expect("snapshot state poisoned");
                state
                    .published
                    .as_ref()
                    .and_then(|published| published.registry_objects.clone())
            };
            if let Some(objects) = published_objects {
                if !result.iter().any(|other| Arc::ptr_eq(other, &objects)) {
                    result.push(objects);
                }
            }
            if let Some(persistent) = snapshot.persistent.get() {
                for chain in std::iter::once(&persistent.rootfs).chain(persistent.memory.iter()) {
                    for layer in &chain.layers {
                        add(result, &layer.range_map);
                    }
                }
            }
            if let Some(resources) = snapshot.resources.get() {
                for entry in resources.lineage.entries() {
                    match entry {
                        SnapshotLineageEntry::Remote { layers, .. } => {
                            for layer in layers {
                                add(result, &layer.range_map);
                            }
                        }
                        SnapshotLineageEntry::FrozenLocal(parent) => {
                            collect(parent, visited, result);
                        }
                    }
                }
            }
        }
        let mut result = Vec::new();
        collect(self, &mut HashSet::new(), &mut result);
        result
    }

    fn prepare_upload_source(
        &self,
        scratch: &dyn Fn(u64) -> Result<Arc<dyn StagedObjectWriter>, SnapshotUploadError>,
    ) -> Result<crate::snapshot::format::PreparedSnapshotSource, SnapshotUploadError> {
        self.ensure_source_integrity()
            .map_err(snapshot_upload_error_from_local)?;
        if self.is_closed().map_err(snapshot_upload_error_from_local)? {
            return Err(SnapshotUploadError::InvalidPlan(
                "LOCAL_READY upload source pin is closed".into(),
            ));
        }
        let canonical = self
            .canonical_snapshot_with_scratch(&|max_bytes| {
                scratch(max_bytes).map_err(|error| {
                    if let SnapshotUploadError::SourceIntegrity(failure) = error {
                        FrozenLayerError::SourceIntegrity(failure)
                    } else if error.retryable() {
                        FrozenLayerError::Io(error.to_string())
                    } else {
                        FrozenLayerError::CanonicalModel(error.to_string())
                    }
                })
            })
            .map_err(snapshot_upload_error_from_local)?;
        Ok(crate::snapshot::format::PreparedSnapshotSource {
            snapshot_id: self.snapshot_id.clone(),
            snapshot_kind: self.snapshot_kind,
            canonical,
            vm_state: self.vm_state.clone(),
            rocklet_artifact: self.rocklet_artifact.clone(),
            provenance: self.provenance.clone(),
        })
    }
}
fn snapshot_upload_error_from_frozen(error: FrozenLayerError) -> SnapshotUploadError {
    match error {
        FrozenLayerError::SourceIntegrity(failure) => SnapshotUploadError::SourceIntegrity(failure),
        FrozenLayerError::Io(message) => SnapshotUploadError::Data {
            message,
            retryable: true,
        },
        error => SnapshotUploadError::InvalidPlan(error.to_string()),
    }
}
fn snapshot_upload_error_from_local(error: LocalSnapshotError) -> SnapshotUploadError {
    match error {
        LocalSnapshotError::SourceIntegrity(failure) => {
            SnapshotUploadError::SourceIntegrity(failure)
        }
        LocalSnapshotError::Io(message) => SnapshotUploadError::Data {
            message,
            retryable: true,
        },
        error => SnapshotUploadError::InvalidPlan(error.to_string()),
    }
}

/// A failed adoption returns the original frozen resources to the capture owner.
#[derive(Debug)]
pub struct AdoptionFailure {
    pub error: LocalSnapshotError,
    pin: Box<CapturedSnapshotPin>,
}

impl AdoptionFailure {
    pub fn into_pin(self) -> CapturedSnapshotPin {
        *self.pin
    }
}

impl Snapshot {
    /// Adopt the native resources of an already captured snapshot without encoding its DATA.
    /// All fallible work borrows the original pin; only a complete object
    /// takes ownership, so failure can return the exact captured resources.
    pub(crate) fn from_capture(
        pin: CapturedSnapshotPin,
        lineage: SnapshotLineage,
        rocklet_artifact: RockletArtifactCompatibility,
    ) -> Result<Arc<Self>, AdoptionFailure> {
        let mut retained_pin = Some(pin);
        let snapshot_id = retained_pin
            .as_ref()
            .expect("retained pin")
            .metadata
            .snapshot_id
            .clone();
        let adopted = (|| -> Result<SnapshotResources, LocalSnapshotError> {
            let pin = retained_pin
                .as_ref()
                .expect("frozen pin is retained until adoption");
            pin.validate()?;
            let mut frozen_layers = HashMap::new();
            for layer in &pin.layers {
                let kind = layer.descriptor.kind;
                if frozen_layers
                    .insert(
                        kind,
                        FrozenLocalLayer::from_backing(LocalBackingHandle::from_native(
                            layer.try_clone()?,
                        )?)?,
                    )
                    .is_some()
                {
                    return Err(LocalSnapshotError::Invalid(
                        "Captured snapshot contains a duplicate layer kind".into(),
                    ));
                }
            }
            let payload = pin.vm_state.read_verified()?;
            // The payload has already been authenticated against this identity.
            // Canonicalizing the identity does not hash or copy DATA again.
            let vm_state = LocalVmState {
                object_key: format!("snapshots/{}/vm-state.pack", pin.metadata.snapshot_id),
                sha256: format!(
                    "sha256:{}",
                    pin.vm_state
                        .descriptor
                        .content_id
                        .strip_prefix("sha256:")
                        .unwrap_or(&pin.vm_state.descriptor.content_id)
                ),
                format_version: pin.metadata.vm_state.format_version,
                payload,
            };
            Self::validate_frozen_parts(&frozen_layers, Some(&vm_state), SnapshotKind::Full)?;
            let snapshot_id = pin.metadata.snapshot_id.clone();
            let (ancestor_snapshot_ids, chains, vm_state_reference) =
                Self::build_snapshot_metadata(
                    &snapshot_id,
                    SnapshotKind::Full,
                    &lineage,
                    &frozen_layers,
                    Some(&vm_state),
                )?;
            let provenance = lineage.provenance().cloned();
            Ok(SnapshotResources {
                native_generation_id: uuid::Uuid::new_v4().to_string(),
                ancestor_snapshot_ids,
                chains,
                vm_state_reference,
                pin: Mutex::new(Some(Arc::new(()))),
                frozen_layers,
                canonicalize: Mutex::new(()),
                canonical: Mutex::new(None),
                lineage,
                vm_state: Some(vm_state),
                rocklet_artifact: Some(rocklet_artifact),
                provenance,
                retained_layer_resources: Mutex::new(Vec::new()),
            })
        })();
        adopted
            .map(|resources| {
                drop(retained_pin.take());
                Self::initialized(snapshot_id, SnapshotKind::Full, resources)
            })
            .map_err(|error| AdoptionFailure {
                error,
                pin: Box::new(
                    retained_pin.expect("failed adoption retains its original frozen pin"),
                ),
            })
    }
}

impl Snapshot {
    /// Complete the same repository instance reserved before capture. A failed
    /// installation still returns the exact captured snapshot pin to its capture owner.
    pub(crate) fn adopt_capture(
        self: &Arc<Self>,
        pin: CapturedSnapshotPin,
        lineage: SnapshotLineage,
        rocklet_artifact: RockletArtifactCompatibility,
    ) -> Result<Arc<Self>, AdoptionFailure> {
        if self.snapshot_kind != SnapshotKind::Full
            || self.is_initialized()
            || self.snapshot_id != pin.metadata.snapshot_id
        {
            return Err(AdoptionFailure {
                error: LocalSnapshotError::Invalid(
                    "FULL reservation is occupied or does not match the captured identity".into(),
                ),
                pin: Box::new(pin),
            });
        }
        let duplicate = match pin.try_clone() {
            Ok(duplicate) => duplicate,
            Err(error) => {
                return Err(AdoptionFailure {
                    error: error.into(),
                    pin: Box::new(pin),
                })
            }
        };
        let local = match Self::from_capture(duplicate, lineage, rocklet_artifact) {
            Ok(local) => local,
            Err(failure) => {
                return Err(AdoptionFailure {
                    error: failure.error,
                    pin: Box::new(pin),
                })
            }
        };
        if let Err(message) = self.initialize_from(local.as_ref()) {
            return Err(AdoptionFailure {
                error: LocalSnapshotError::Invalid(message),
                pin: Box::new(pin),
            });
        }
        drop(pin);
        Ok(Arc::clone(self))
    }
}

fn packed_ownership_state(payload: &[u8], unit: u64) -> Option<u8> {
    let byte = payload.get(usize::try_from(unit / 4).ok()?)?;
    Some((byte >> ((unit % 4) * 2)) & 0b11)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::snapshot::metadata::{
        sha256_identity, LazyLoadContext, SnapshotReadRange, StorageProvider,
    };
    use crate::snapshot::repository::frozen_layer::{FrozenLayerMetadata, PackedOwnershipMap};
    use crate::snapshot::repository::PublicationState;
    use crate::snapshot::services::background_uploader::persist_upload_plan;
    use crate::snapshot::transport::posix_file::LocalFileCache;
    use async_trait::async_trait;
    use std::os::unix::fs::FileExt;

    struct TestRoot(std::path::PathBuf);
    impl Drop for TestRoot {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    struct NoRemoteReads;

    #[async_trait]
    impl crate::snapshot::io::range_io::RangeReader for NoRemoteReads {
        async fn read_range(
            &self,
            _request: crate::snapshot::io::range_io::RangeReadRequest<'_>,
        ) -> Result<
            crate::snapshot::io::range_io::RangeReadResult,
            crate::snapshot::transport::RangeReadError,
        > {
            panic!("a frozen raw layer must not access a remote provider");
        }
    }

    fn raw_snapshot(id: &str, byte: u8, cache: &LocalFileCache) -> Arc<Snapshot> {
        let backing = cache.create_scratch_file().unwrap();
        backing.set_len(2 * 65536).unwrap();
        backing.write_all_at(&vec![byte; 65536], 0).unwrap();
        Snapshot::from_base_layer(
            id.into(),
            FrozenLayerPin::new(
                FrozenLayerMetadata {
                    kind: LayerKind::Block,
                    layer_id: format!("{id}-block"),
                    provisional_content_id: sha256_identity(id.as_bytes()),
                    logical_size: 2 * 65536,
                    alignment: 65536,
                },
                PackedOwnershipMap {
                    kind: LayerKind::Block,
                    snapshot_id: id.into(),
                    format_version: 1,
                    logical_size: 2 * 65536,
                    granularity: 65536,
                    // Unit 0 is DATA; unit 1 is ZERO. The raw file's holes
                    // cannot be returned as DATA by the snapshot interface.
                    packed_states: vec![0b1001],
                },
                backing.into(),
                false,
            ),
            SnapshotProvenance {
                oci_ref: Some("fixture:raw".into()),
                oci_digest: Some(sha256_identity(b"raw fixture")),
            },
        )
        .unwrap()
    }

    #[test]
    fn failed_upload_preserves_native_local_ready_capability() {
        use crate::snapshot::repository::PublicationState;
        let directory = tempfile::tempdir().unwrap();
        let cache =
            LocalFileCache::new(directory.path().to_owned(), "failed-upload-native").unwrap();
        let snapshot = raw_snapshot("failed-upload-native", 0x53, &cache);
        assert!(!snapshot.native_is_locally_ready());
        {
            let mut state = snapshot.lifecycle.lock().unwrap();
            state.state = PublicationState::LocalReady;
            state.last_usable = Some(PublicationState::LocalReady);
            state.fail("upload", "deliberate transport failure");
        }
        assert!(snapshot.native_is_locally_ready());
        let pin = snapshot.pin_native_layer(LayerKind::Block).unwrap();
        let mut bytes = [0; 4];
        pin.file().read_exact_at(&mut bytes, 0).unwrap();
        assert_eq!(bytes, [0x53; 4]);
    }

    #[test]
    fn creating_raw_instance_installs_original_resources_without_replacing_identity() {
        use std::os::unix::fs::MetadataExt;
        let root = TestRoot(
            std::env::temp_dir().join(format!("rock-raw-reservation-{}", uuid::Uuid::new_v4())),
        );
        let cache = LocalFileCache::new(root.0.clone(), "raw-reservation").unwrap();
        let reserved = Snapshot::creating("raw-reservation", SnapshotKind::BaseImage);
        let instance: Arc<dyn SnapshotInstance> = reserved.clone();
        assert!(!reserved.is_initialized());
        assert!(Arc::ptr_eq(
            &Arc::clone(&instance).downcast::<Snapshot>().unwrap(),
            &reserved
        ));
        assert!(instance.layer_chain(LayerKind::Block).is_err());
        assert!(instance
            .layer_reader(LayerKind::Block, "not-ready")
            .is_err());
        let local = raw_snapshot("raw-reservation", 0x47, &cache);
        let source_fd = local.pin_native_layer(LayerKind::Block).unwrap();
        let source_stat = File::from(source_fd.file().try_clone().unwrap())
            .metadata()
            .unwrap();
        reserved.initialize_from(&local).unwrap();
        // These queries also run from repository callbacks while state is locked.
        let _state = instance.lifecycle().lock().unwrap();
        assert!(reserved.is_initialized());
        let raw = Arc::clone(&instance).downcast::<Snapshot>().unwrap();
        assert!(Arc::ptr_eq(&raw, &reserved));
        let installed_fd = raw.pin_native_layer(LayerKind::Block).unwrap();
        assert!(Arc::ptr_eq(
            &local.backing_handle(LayerKind::Block).unwrap().backing,
            &raw.backing_handle(LayerKind::Block).unwrap().backing
        ));
        let installed_stat = File::from(installed_fd.file().try_clone().unwrap())
            .metadata()
            .unwrap();
        assert_eq!(
            (source_stat.dev(), source_stat.ino()),
            (installed_stat.dev(), installed_stat.ino())
        );
        assert!(raw.canonical.lock().unwrap().is_none());
    }

    #[test]
    fn raw_initialization_is_idempotent_for_one_resource_set_and_rejects_replacement() {
        let root = TestRoot(
            std::env::temp_dir().join(format!("rock-raw-initialize-{}", uuid::Uuid::new_v4())),
        );
        let cache = LocalFileCache::new(root.0.clone(), "raw-initialize").unwrap();
        let reserved = Snapshot::creating("raw-initialize", SnapshotKind::BaseImage);
        let first = raw_snapshot("raw-initialize", 0x35, &cache);
        let replacement = raw_snapshot("raw-initialize", 0x72, &cache);
        reserved.initialize_from(&first).unwrap();
        reserved.initialize_from(&first).unwrap();
        assert!(reserved.initialize_from(&replacement).is_err());
        let mut value = [0];
        File::from(
            reserved
                .pin_native_layer(LayerKind::Block)
                .unwrap()
                .file()
                .try_clone()
                .unwrap(),
        )
        .read_exact_at(&mut value, 0)
        .unwrap();
        assert_eq!(value, [0x35]);
        assert!(Snapshot::creating("other-id", SnapshotKind::BaseImage)
            .initialize_from(&first)
            .is_err());
        assert!(Snapshot::creating("raw-initialize", SnapshotKind::Full)
            .initialize_from(&first)
            .is_err());
    }

    #[test]
    fn raw_snapshot_metadata_validation_keeps_depth_geometry_and_full_base_requirements() {
        use crate::snapshot::metadata::MAX_CHAIN_DEPTH;
        let root = TestRoot(
            std::env::temp_dir().join(format!("rock-raw-validation-{}", uuid::Uuid::new_v4())),
        );
        let cache = LocalFileCache::new(root.0.clone(), "raw-validation").unwrap();
        let mut parent = raw_snapshot("raw-validation", 0x35, &cache);
        assert!(Snapshot::build_snapshot_metadata(
            "full-without-memory",
            SnapshotKind::Full,
            &SnapshotLineage::default(),
            &parent.frozen_layers,
            None
        )
        .is_err());
        let mut wrong_geometry = parent.frozen_layers.clone();
        wrong_geometry
            .get_mut(&LayerKind::Block)
            .unwrap()
            .unit_size_bytes = 65535;
        assert!(Snapshot::build_snapshot_metadata(
            "bad-geometry",
            SnapshotKind::BaseImage,
            &SnapshotLineage::default(),
            &wrong_geometry,
            None
        )
        .is_err());
        // Fill only immutable metadata to the chain limit; no synthetic reader
        // or DATA source is needed for metadata validation.
        {
            let resources = Arc::get_mut(
                Arc::get_mut(&mut parent)
                    .unwrap()
                    .resources
                    .get_mut()
                    .unwrap(),
            )
            .unwrap();
            let chain = resources.chains.get_mut(&LayerKind::Block).unwrap();
            let first = chain.layers[0].clone();
            chain.layers = (0..MAX_CHAIN_DEPTH)
                .map(|ordinal| {
                    let mut layer = first.clone();
                    layer.ordinal = ordinal as u64;
                    layer.layer_id = format!("ancestor-{ordinal}");
                    layer
                })
                .collect();
        }
        let lineage = SnapshotLineage::default().with_frozen(Arc::clone(&parent));
        assert!(Snapshot::build_snapshot_metadata(
            "too-deep",
            SnapshotKind::BaseImage,
            &lineage,
            &parent.frozen_layers,
            None
        )
        .is_err());
        assert!(Snapshot::build_snapshot_metadata(
            "raw-validation",
            SnapshotKind::BaseImage,
            &lineage,
            &HashMap::new(),
            None
        )
        .is_err());
    }

    fn publication(plan: &SnapshotUploadPlan) -> crate::snapshot::format::UploadedSnapshot {
        crate::snapshot::format::UploadedSnapshot {
            registry_objects: None,
            spec: plan.spec.clone(),
            manifest: serde_json::from_slice(&plan.manifest.payload).unwrap(),
        }
    }

    fn upload_storage() -> StorageDescriptor {
        StorageDescriptor {
            provider: StorageProvider::S3,
            endpoint_profile: "fixture".into(),
            credential_ref: "fixture:v1".into(),
        }
    }

    #[test]
    fn published_base_reuses_prepared_metadata_after_upload_pin_closes() {
        use crate::snapshot::metadata::canonical_json_bytes;
        let root = TestRoot(
            std::env::temp_dir().join(format!("rock-raw-published-base-{}", uuid::Uuid::new_v4())),
        );
        let cache = LocalFileCache::new(root.0.clone(), "published-base").unwrap();
        let raw = raw_snapshot("published-base", 0x45, &cache);
        let plan = raw
            .upload_plan(
                upload_storage(),
                VmmCompatibility {
                    architecture: "x86_64".into(),
                    vmm_type: "any".into(),
                    vmm_state_format_version: None,
                    rocklet_artifact: None,
                },
            )
            .unwrap();
        let published = publication(&plan);
        let canonical = raw.canonical.lock().unwrap().as_ref().unwrap().clone();
        raw.close_upload_pin().unwrap();
        let snapshot = raw.published_snapshot(&published).unwrap();
        assert_eq!(snapshot.snapshot_kind(), SnapshotKind::BaseImage);
        assert!(snapshot.chain_view(LayerKind::Memory).is_none());
        assert!(snapshot.vm_state().is_none());
        assert_eq!(
            snapshot.chain_view(LayerKind::Block).unwrap().index,
            canonical.chain_indexes[&LayerKind::Block].index
        );
        assert!(Arc::ptr_eq(
            &snapshot.chain_view(LayerKind::Block).unwrap().layers[0].range_map,
            &canonical.layers[&LayerKind::Block].range_map
        ));
        assert!(snapshot
            .layer_reader(
                LayerKind::Block,
                &snapshot.chain_view(LayerKind::Block).unwrap().layers[0]
                    .layer_ref
                    .content_sha256
            )
            .is_ok());
        let mut changed = published.clone();
        changed.spec.rootfs.index_sha256 = sha256_identity(b"different chain");
        changed.manifest.rootfs.sha256 = changed.spec.rootfs.index_sha256.clone();
        changed.spec.manifest.sha256 =
            sha256_identity(&canonical_json_bytes(&changed.manifest).unwrap());
        assert!(raw.published_snapshot(&changed).is_err());
        raw.canonical.lock().unwrap().take();
        assert!(raw.published_snapshot(&published).is_err());
        assert!(
            raw.canonical.lock().unwrap().is_none(),
            "rehome must not recreate a missing cache"
        );
    }

    #[test]
    fn published_full_preserves_both_layers_and_frozen_ancestor_maps() {
        let (first_pin, _first_writer) = captured_snapshot_pin("publication-first");
        let first =
            Snapshot::from_capture(first_pin, SnapshotLineage::default(), captured_rocklet())
                .unwrap();
        let (second_pin, _second_writer) = captured_snapshot_pin("publication-second");
        let second = Snapshot::from_capture(
            second_pin,
            SnapshotLineage::default().with_frozen(Arc::clone(&first)),
            captured_rocklet(),
        )
        .unwrap();
        let plan = second
            .upload_plan(
                upload_storage(),
                VmmCompatibility {
                    architecture: "x86_64".into(),
                    vmm_type: "cloud-hypervisor".into(),
                    vmm_state_format_version: Some(1),
                    rocklet_artifact: None,
                },
            )
            .unwrap();
        let published = publication(&plan);
        let first_canonical = first.canonical.lock().unwrap().as_ref().unwrap().clone();
        let second_canonical = second.canonical.lock().unwrap().as_ref().unwrap().clone();
        first.close_upload_pin().unwrap();
        second.close_upload_pin().unwrap();
        let snapshot = second.published_snapshot(&published).unwrap();
        assert_eq!(snapshot.snapshot_kind(), SnapshotKind::Full);
        assert_eq!(
            snapshot.ancestor_snapshot_ids(),
            &["publication-first".to_owned()]
        );
        assert_eq!(snapshot.vm_state(), Some(b"opaque VM state".as_slice()));
        for kind in [LayerKind::Memory, LayerKind::Block] {
            let chain = snapshot.chain_view(kind).unwrap();
            assert_eq!(chain.layers.len(), 2);
            assert_eq!(chain.index, second_canonical.chain_indexes[&kind].index);
            assert!(Arc::ptr_eq(
                &chain.layers[0].range_map,
                &first_canonical.layers[&kind].range_map
            ));
            assert!(Arc::ptr_eq(
                &chain.layers[1].range_map,
                &second_canonical.layers[&kind].range_map
            ));
            for layer in &chain.layers {
                assert!(snapshot
                    .layer_reader(kind, &layer.layer_ref.content_sha256)
                    .is_ok());
            }
        }
        let mut mismatched = published;
        mismatched.spec.vm_state.as_mut().unwrap().sha256 = sha256_identity(b"other state");
        assert!(second.published_snapshot(&mismatched).is_err());
    }

    #[test]
    fn deep_flat_lineage_checks_health_and_retains_every_registry_capability() {
        use crate::snapshot::format::overlaybd_oci::{RegistryObjectBinding, RegistryObjectMap};
        use crate::snapshot::format::SnapshotUploadSource;
        use crate::snapshot::metadata::RegistrySnapshotRef;
        use crate::snapshot::repository::SnapshotRepository;
        use crate::snapshot::transport::registry::RegistryClient;
        use crate::snapshot::transport::BlobRef;

        let (finished, completion) = std::sync::mpsc::sync_channel(1);
        let worker = std::thread::spawn(move || {
            let repository = SnapshotRepository::new();
            let client = RegistryClient::new("registry.test").unwrap();
            let mut lineage = SnapshotLineage::default();
            let mut snapshots = Vec::new();
            let mut capabilities = Vec::new();
            for generation in 0..=32 {
                let id = format!("deep-flat-{generation}");
                let (pin, _writer) = captured_snapshot_pin(&id);
                let snapshot =
                    Snapshot::from_capture(pin, lineage.clone(), captured_rocklet()).unwrap();
                repository.register_raw(Arc::clone(&snapshot)).unwrap();
                let plan = snapshot
                    .upload_plan(
                        upload_storage(),
                        VmmCompatibility {
                            architecture: "x86_64".into(),
                            vmm_type: "cloud-hypervisor".into(),
                            vmm_state_format_version: Some(1),
                            rocklet_artifact: None,
                        },
                    )
                    .unwrap();
                let mut bindings = plan
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
                for request in plan.layers.iter().flat_map(|layer| layer.data_requests()) {
                    bindings.push(RegistryObjectBinding {
                        object_key: request.identity.object_key.as_str().into(),
                        blob: BlobRef {
                            digest: request.identity.expected_sha256.clone(),
                            size: request.identity.length,
                        },
                        object_version: None,
                    });
                }
                let objects = RegistryObjectMap::new(
                    Arc::clone(&client),
                    "snapshots/deep-flat".into(),
                    &bindings,
                    None,
                )
                .unwrap();
                let mut published = publication(&plan);
                published.spec.storage.provider = StorageProvider::Registry;
                published.spec.storage.endpoint_profile = "registry.test".into();
                published.spec.registry_snapshot = Some(RegistrySnapshotRef {
                    registry: "registry.test".into(),
                    repository: "snapshots/deep-flat".into(),
                    index_digest: sha256_identity(id.as_bytes()),
                });
                published.registry_objects = Some(Arc::clone(&objects));
                let value = serde_json::to_value(&published.spec).unwrap();
                let checksum = crate::snapshot::metadata::snapshot_spec_checksum(&value).unwrap();
                {
                    let mut state = snapshot.lifecycle.lock().unwrap();
                    repository
                        .commit_remote_data(
                            &id,
                            &mut state,
                            plan.metadata_chains().unwrap(),
                            &checksum,
                        )
                        .unwrap();
                    state.published = Some(published);
                    state.snapshot_spec = Some(value);
                    state.spec_checksum = Some(checksum);
                }
                lineage = lineage.with_frozen(Arc::clone(&snapshot));
                snapshots.push(snapshot);
                capabilities.push(objects);
            }
            let head = snapshots.last().unwrap();
            assert_eq!(head.lineage.frozen_snapshots().count(), 32);
            head.ensure_source_integrity().unwrap();
            let sources = head.registry_sources();
            assert_eq!(sources.len(), capabilities.len());
            for capability in &capabilities {
                assert_eq!(
                    sources
                        .iter()
                        .filter(|source| Arc::ptr_eq(source, capability))
                        .count(),
                    1
                );
            }
            let failure = DataIntegrityFailure {
                object_key: "deep-flat-inconsistent-data".into(),
                logical_offset: 0,
                object_offset: 0,
                length: 4096,
                expected_sha256: sha256_identity(b"expected"),
                actual_sha256: sha256_identity(b"changed"),
            };
            snapshots[17]
                .quarantine_source_integrity(failure.clone())
                .unwrap();
            assert!(
                matches!(head.ensure_source_integrity(), Err(LocalSnapshotError::SourceIntegrity(actual)) if actual == failure)
            );
            assert!(!head.is_closed().unwrap());
            finished.send(()).unwrap();
        });
        completion
            .recv_timeout(std::time::Duration::from_secs(20))
            .expect("32 flat ancestors must not cause exponential source traversal");
        worker.join().unwrap();
    }

    #[test]
    fn frozen_child_retains_registry_custody_before_parent_admin_finalization() {
        use crate::snapshot::format::overlaybd_oci::{RegistryObjectBinding, RegistryObjectMap};
        use crate::snapshot::metadata::RegistrySnapshotRef;
        use crate::snapshot::repository::SnapshotRepository;
        use crate::snapshot::transport::registry::RegistryClient;
        use crate::snapshot::transport::BlobRef;

        let (parent_pin, _parent_writer) = captured_snapshot_pin("registry-published-parent");
        let parent =
            Snapshot::from_capture(parent_pin, SnapshotLineage::default(), captured_rocklet())
                .unwrap();
        let repository = SnapshotRepository::new();
        repository.register_raw(Arc::clone(&parent)).unwrap();
        let compatibility = VmmCompatibility {
            architecture: "x86_64".into(),
            vmm_type: "cloud-hypervisor".into(),
            vmm_state_format_version: Some(1),
            rocklet_artifact: None,
        };
        let plan = parent
            .upload_plan(upload_storage(), compatibility.clone())
            .unwrap();
        let mut bindings = plan
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
        for request in plan.layers.iter().flat_map(|layer| layer.data_requests()) {
            bindings.push(RegistryObjectBinding {
                object_key: request.identity.object_key.as_str().into(),
                blob: BlobRef {
                    digest: request.identity.expected_sha256.clone(),
                    size: request.identity.length,
                },
                object_version: None,
            });
        }
        let objects = RegistryObjectMap::new(
            RegistryClient::new("registry.test").unwrap(),
            "snapshots/full".into(),
            &bindings,
            None,
        )
        .unwrap();
        let mut published = publication(&plan);
        published.spec.storage.provider = StorageProvider::Registry;
        published.spec.storage.endpoint_profile = "registry.test".into();
        published.spec.registry_snapshot = Some(RegistrySnapshotRef {
            registry: "registry.test".into(),
            repository: "snapshots/full".into(),
            index_digest: sha256_identity(b"published parent index"),
        });
        published.registry_objects = Some(Arc::clone(&objects));
        let value = serde_json::to_value(&published.spec).unwrap();
        let checksum = crate::snapshot::metadata::snapshot_spec_checksum(&value).unwrap();
        {
            let mut state = parent.lifecycle.lock().unwrap();
            repository
                .commit_remote_data(
                    &parent.snapshot_id,
                    &mut state,
                    plan.metadata_chains().unwrap(),
                    &checksum,
                )
                .unwrap();
            state.published = Some(published);
            state.snapshot_spec = Some(value);
            state.spec_checksum = Some(checksum);
            assert!(!state.admin_finalized);
        }
        assert_eq!(parent.state(), PublicationState::RemoteReady);
        assert!(parent.persistent_owned().is_none());

        let (child_pin, _child_writer) = captured_snapshot_pin("registry-unfinalized-child");
        let child = Snapshot::from_capture(
            child_pin,
            SnapshotLineage::default().with_frozen(Arc::clone(&parent)),
            captured_rocklet(),
        )
        .unwrap();
        let child_plan = child.upload_plan(upload_storage(), compatibility).unwrap();
        assert_eq!(child_plan.registry_sources.len(), 1);
        assert!(Arc::ptr_eq(&child_plan.registry_sources[0], &objects));
        for binding in bindings {
            assert_eq!(
                child_plan.registry_sources[0]
                    .bindings()
                    .get(&binding.object_key),
                Some(&binding.blob),
                "the child lost parent object {}",
                binding.object_key
            );
        }
        // The finite upload plan owns custody independently of the raw parent
        // and the lifecycle/catalog aliases that made it discoverable.
        let retained = Arc::downgrade(&objects);
        drop(objects);
        repository.invalidate(&parent.snapshot_id);
        drop(child);
        drop(parent);
        assert!(retained.upgrade().is_some());
    }

    #[test]
    fn published_full_retains_native_registry_base_and_per_layer_policy() {
        use crate::snapshot::format::overlaybd::composite::test_registry_rootfs_with_units;
        use crate::snapshot::repository::lineage::{RemoteLayerSource, SourceResidency};
        let registry = test_registry_rootfs_with_units(2);
        let base = crate::snapshot::format::overlaybd_oci::registry_base_plan(
            "registry-base",
            &registry,
            VmmCompatibility {
                architecture: "x86_64".into(),
                vmm_type: "any".into(),
                vmm_state_format_version: None,
                rocklet_artifact: None,
            },
        )
        .unwrap();
        let chain: LayerChainIndex =
            serde_json::from_slice(&base.chain_indexes[0].payload).unwrap();
        let map = Arc::new(registry.build_map().unwrap());
        let source = RemoteLayerSource {
            ordinal: 0,
            layer_id: chain.layers[0].layer_id.clone(),
            range_map: Arc::clone(&map),
            backing: Arc::new(File::open("/dev/null").unwrap().into()),
            shared_mapping: None,
            residency: SourceResidency::OnDemand,
            dynamic_residency: None,
        };
        let lineage = SnapshotLineage::from_remote(
            "registry-base",
            &HashMap::from([(LayerKind::Block, chain.clone())]),
            &[source],
        );
        let (pin, _writer) = captured_snapshot_pin("registry-local");
        let raw = Snapshot::from_capture(pin, lineage, captured_rocklet()).unwrap();
        let plan = raw
            .upload_plan(
                upload_storage(),
                VmmCompatibility {
                    architecture: "x86_64".into(),
                    vmm_type: "cloud-hypervisor".into(),
                    vmm_state_format_version: Some(1),
                    rocklet_artifact: None,
                },
            )
            .unwrap();
        raw.close_upload_pin().unwrap();
        let snapshot = raw.published_snapshot(&publication(&plan)).unwrap();
        assert_eq!(
            snapshot.ancestor_snapshot_ids(),
            &["registry-base".to_owned()]
        );
        assert_eq!(
            snapshot.chain_view(LayerKind::Block).unwrap().layers.len(),
            2
        );
        let inherited = &snapshot.chain_view(LayerKind::Block).unwrap().layers[0];
        assert_eq!(inherited.layer_ref, chain.layers[0]);
        assert!(Arc::ptr_eq(&inherited.range_map, &map));
        assert!(Arc::ptr_eq(
            inherited.range_map.registry.as_ref().unwrap(),
            &registry
        ));
        assert!(!inherited.allows_p2p());
        assert!(!inherited.allows_background_prefetch());
        assert!(snapshot
            .layer_reader(LayerKind::Block, &inherited.layer_ref.content_sha256)
            .is_ok());
        assert!(snapshot.chain_view(LayerKind::Block).unwrap().layers[1].allows_p2p());
        assert!(
            snapshot.chain_view(LayerKind::Block).unwrap().layers[1].allows_background_prefetch()
        );
        assert_eq!(
            snapshot.chain_view(LayerKind::Memory).unwrap().layers.len(),
            1
        );
    }

    #[test]
    fn slow_encoding_does_not_block_snapshot_status_or_close() {
        use std::sync::mpsc;
        use std::time::Duration;

        let root = TestRoot(
            std::env::temp_dir().join(format!("rock-encoding-pin-lock-{}", uuid::Uuid::new_v4())),
        );
        let cache = LocalFileCache::new(root.0.clone(), "encoding-pin-lock").unwrap();
        let local = raw_snapshot("encoding-pin-lock", 0x5a, &cache);
        let (entered_tx, entered_rx) = mpsc::sync_channel(1);
        let (release_tx, release_rx) = mpsc::sync_channel(1);
        let encoder = {
            let local = Arc::clone(&local);
            std::thread::spawn(move || {
                local.canonical_snapshot_with_scratch(&|max_bytes| {
                    entered_tx.send(()).unwrap();
                    release_rx.recv_timeout(Duration::from_secs(10)).unwrap();
                    create_temporary_staged_object(max_bytes)
                })
            })
        };
        entered_rx.recv_timeout(Duration::from_secs(10)).unwrap();

        // The encoder has entered actual format preparation and is deliberately
        // stopped. Neither status nor logical source closure needs that work.
        let (lifecycle_tx, lifecycle_rx) = mpsc::sync_channel(1);
        let lifecycle = {
            let local = Arc::clone(&local);
            std::thread::spawn(move || {
                let before = local.is_closed();
                let close = local.close_upload_pin();
                let after = local.is_closed();
                lifecycle_tx.send((before, close, after)).unwrap();
            })
        };
        let fast_lifecycle = lifecycle_rx.recv_timeout(Duration::from_secs(2));
        // Always release and join before asserting, so the old implementation
        // produces a finite regression failure rather than a hanging test.
        release_tx.send(()).unwrap();
        let encoded = encoder.join().unwrap();
        lifecycle.join().unwrap();
        let (before, close, after) = fast_lifecycle
            .expect("snapshot status/close waited for the blocked background encoder");
        assert!(!before.unwrap());
        close.unwrap();
        assert!(after.unwrap());
        assert!(matches!(encoded, Err(LocalSnapshotError::Frozen(_))));
        assert!(local.canonical.lock().unwrap().is_none());
        // Independent immutable read capabilities remain valid after the
        // upload-source lease is logically closed.
        let file = File::from(
            local
                .pin_native_layer(LayerKind::Block)
                .unwrap()
                .file()
                .try_clone()
                .unwrap(),
        );
        let mut actual = [0; 32];
        file.read_exact_at(&mut actual, 0).unwrap();
        assert_eq!(actual, [0x5a; 32]);
    }

    #[test]
    fn close_preserves_inflight_frozen_lease_and_discards_cancelled_encoding() {
        use std::sync::mpsc;
        use std::time::Duration;

        let root = TestRoot(
            std::env::temp_dir().join(format!("rock-encoding-lease-{}", uuid::Uuid::new_v4())),
        );
        let cache = LocalFileCache::new(root.0.clone(), "encoding-lease").unwrap();
        let local = raw_snapshot("encoding-lease", 0x61, &cache);
        let lease = Arc::downgrade(local.pin.lock().unwrap().as_ref().unwrap());
        let (scratch_tx, scratch_rx) = mpsc::sync_channel(1);
        let (release_tx, release_rx) = mpsc::sync_channel(1);
        let encoder = {
            let local = Arc::clone(&local);
            std::thread::spawn(move || {
                local.canonical_snapshot_with_scratch(&|max_bytes| {
                    let scratch = create_temporary_staged_object(max_bytes)?;
                    scratch_tx.send(Arc::downgrade(&scratch)).unwrap();
                    release_rx.recv_timeout(Duration::from_secs(10)).unwrap();
                    Ok(scratch)
                })
            })
        };
        let scratch = scratch_rx.recv_timeout(Duration::from_secs(10)).unwrap();
        let (close_tx, close_rx) = mpsc::sync_channel(1);
        let closer = {
            let local = Arc::clone(&local);
            std::thread::spawn(move || {
                close_tx.send(local.close_upload_pin()).unwrap();
            })
        };
        let close = close_rx.recv_timeout(Duration::from_secs(2));
        let lease_alive_during_encoding = lease.upgrade().is_some();
        release_tx.send(()).unwrap();
        let encoded = encoder.join().unwrap();
        closer.join().unwrap();

        close
            .expect("logical closure waited for background encoding")
            .unwrap();
        assert!(lease_alive_during_encoding);
        assert!(lease.upgrade().is_none());
        assert!(matches!(encoded, Err(LocalSnapshotError::Frozen(_))));
        assert!(local.is_closed().unwrap());
        assert!(local.canonical.lock().unwrap().is_none());
        assert!(
            scratch.upgrade().is_none(),
            "cancelled encoding retained its staged writer"
        );
    }

    #[tokio::test]
    async fn local_snapshot_view_reads_selected_raw_ancestor_without_sealing() {
        let root =
            TestRoot(std::env::temp_dir().join(format!("rock-raw-view-{}", uuid::Uuid::new_v4())));
        let cache = LocalFileCache::new(root.0.clone(), "raw-view-fixture").unwrap();
        let parent = raw_snapshot("raw-parent", 0x35, &cache);
        let mut child = raw_snapshot("raw-child", 0x7a, &cache);
        // Complete the synthetic resources before exposing its immutable view.
        // Production capture seals metadata with its lineage in the converter;
        // changing only the ownership lineage after that cannot change metadata.
        {
            let child = Arc::get_mut(&mut child).unwrap();
            let resources = Arc::get_mut(child.resources.get_mut().unwrap()).unwrap();
            resources.lineage = SnapshotLineage::default().with_frozen(Arc::clone(&parent));
            let (ancestors, chains, vm_state_reference) = Snapshot::build_snapshot_metadata(
                &child.snapshot_id,
                child.snapshot_kind,
                &resources.lineage,
                &resources.frozen_layers,
                resources.vm_state.as_ref(),
            )
            .unwrap();
            resources.ancestor_snapshot_ids = ancestors;
            resources.chains = chains;
            resources.vm_state_reference = vm_state_reference;
        }
        let view: &dyn SnapshotInstance = child.as_ref();
        assert_eq!(view.ancestor_snapshot_ids(), &["raw-parent"]);
        let chain = view.layer_chain(LayerKind::Block).unwrap().unwrap();
        assert_eq!(chain.layers.len(), 2);
        assert_eq!(chain.layers[0].ordinal, 0);
        assert_eq!(chain.layers[1].ordinal, 1);
        let context = LazyLoadContext::new(
            Arc::new(NoRemoteReads),
            crate::RuntimeIdentity::new("raw-view-runtime"),
            None,
        );
        for (layer, expected) in chain.layers.iter().zip([0x35, 0x7a]) {
            let read = view
                .layer_reader(LayerKind::Block, &layer.content_identity)
                .unwrap()
                .read_data_range(
                    &context,
                    SnapshotReadRange {
                        kind: LayerKind::Block,
                        layer_content_sha256: &layer.content_identity,
                        logical_offset: 123,
                        length: 64,
                    },
                )
                .await
                .unwrap();
            assert_eq!(read.payload, vec![expected; 64]);
            assert_eq!(read.accounting.provider_bytes, 0);
        }
        assert!(parent.canonical.lock().unwrap().is_none());
        assert!(child.canonical.lock().unwrap().is_none());
        // Upload pin lifetime is separate from an immutable layer's read FD.
        parent.close_upload_pin().unwrap();
        child.close_upload_pin().unwrap();
        let read = view
            .layer_reader(LayerKind::Block, &chain.layers[0].content_identity)
            .unwrap()
            .read_data_range(
                &context,
                SnapshotReadRange {
                    kind: LayerKind::Block,
                    layer_content_sha256: &chain.layers[0].content_identity,
                    logical_offset: 0,
                    length: 32,
                },
            )
            .await
            .unwrap();
        assert_eq!(read.payload, vec![0x35; 32]);
    }

    #[tokio::test]
    async fn local_snapshot_view_preserves_zero_coverage_and_rejects_wrong_identity() {
        let root =
            TestRoot(std::env::temp_dir().join(format!("rock-raw-hole-{}", uuid::Uuid::new_v4())));
        let cache = LocalFileCache::new(root.0.clone(), "raw-hole-fixture").unwrap();
        let local = raw_snapshot("raw-hole", 0x19, &cache);
        let view: &dyn SnapshotInstance = local.as_ref();
        let context = LazyLoadContext::new(
            Arc::new(NoRemoteReads),
            crate::RuntimeIdentity::new("raw-hole-runtime"),
            None,
        );
        assert!(matches!(
            view.layer_reader(LayerKind::Block, &sha256_identity(b"raw-hole"))
                .unwrap()
                .read_data_range(
                    &context,
                    SnapshotReadRange {
                        kind: LayerKind::Block,
                        layer_content_sha256: &sha256_identity(b"raw-hole"),
                        logical_offset: 65536,
                        length: 1,
                    },
                )
                .await,
            Err(LazyLoadError::NonDataRange)
        ));
        assert!(matches!(
            view.layer_reader(LayerKind::Block, "another-snapshot"),
            Err(LazyLoadError::IdentityConflict)
        ));
        assert!(matches!(
            view.layer_reader(LayerKind::Memory, &sha256_identity(b"raw-hole")),
            Err(LazyLoadError::IdentityConflict)
        ));
        assert!(local.canonical.lock().unwrap().is_none());
    }

    #[tokio::test]
    async fn finalization_releases_temporary_zfile_but_preserves_existing_source_and_metadata() {
        let root = TestRoot(
            std::env::temp_dir().join(format!("rock-zfile-lifetime-{}", uuid::Uuid::new_v4())),
        );
        let cache = LocalFileCache::new(root.0.clone(), "zfile-lifetime-fixture").unwrap();
        let backing = cache.create_scratch_file().unwrap();
        backing.set_len(65536).unwrap();
        let snapshot_id = "zfile-lifetime-fixture".to_owned();
        let local = Snapshot::from_base_layer(
            snapshot_id.clone(),
            FrozenLayerPin::new(
                FrozenLayerMetadata {
                    kind: LayerKind::Block,
                    layer_id: "fixture-block".into(),
                    provisional_content_id: sha256_identity(b"fixture-block"),
                    logical_size: 65536,
                    alignment: 65536,
                },
                PackedOwnershipMap {
                    kind: LayerKind::Block,
                    snapshot_id,
                    format_version: 1,
                    logical_size: 65536,
                    granularity: 65536,
                    packed_states: vec![2],
                },
                backing.into(),
                false,
            ),
            SnapshotProvenance {
                oci_ref: Some("fixture:latest".into()),
                oci_digest: Some(sha256_identity(b"fixture")),
            },
        )
        .unwrap();
        let metadata: &dyn crate::snapshot::SnapshotInstance = local.as_ref();
        let chain = metadata.layer_chain(LayerKind::Block).unwrap().unwrap();
        assert_eq!(metadata.snapshot_id(), "zfile-lifetime-fixture");
        assert_eq!(metadata.snapshot_kind(), SnapshotKind::BaseImage);
        assert_eq!(chain.layers.len(), 1);
        assert_eq!(chain.layers[0].ordinal, 0);
        assert_eq!(chain.layers[0].layer_id, "fixture-block");
        assert_eq!(chain.layers[0].format, "raw");
        assert!(metadata.layer_chain(LayerKind::Memory).unwrap().is_none());
        // Metadata queries on a frozen raw snapshot must not initiate its
        // expensive persistent encoding or change the local-fork fast path.
        assert!(local.canonical.lock().unwrap().is_none());
        let plan = local
            .upload_plan_with_scratch(
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
                &|max_bytes| {
                    cache
                        .create_staged_object(max_bytes)
                        .map_err(|error| FrozenLayerError::Io(error.to_string()))
                },
            )
            .unwrap();
        let canonical = local.canonical_snapshot().unwrap();
        let layer = &canonical.layers[&LayerKind::Block];
        let descriptor = layer.range_map_bytes.clone();
        let temporary = Arc::downgrade(&layer.blob.sealed_object().unwrap().unwrap());
        let source = Arc::clone(&plan.layers[0].data_requests()[0].source);
        let hash = plan.layers[0].data_requests()[0]
            .identity
            .expected_sha256
            .clone();
        let persisted = persist_upload_plan(plan.clone(), Arc::clone(&cache))
            .await
            .unwrap();
        local.close_upload_pin().unwrap();
        assert!(local.is_closed().unwrap());
        assert!(layer.blob.encoded_file().unwrap().is_none());
        assert!(temporary.upgrade().is_some());
        assert_eq!(
            sha256_identity(&source.read_at(0, 1024 * 1024).await.unwrap()),
            hash
        );
        assert_eq!(
            sha256_identity(
                &persisted.layers[0].data_requests()[0]
                    .source
                    .read_at(0, 1024 * 1024)
                    .await
                    .unwrap()
            ),
            hash
        );
        assert_eq!(
            local.canonical_snapshot().unwrap().layers[&LayerKind::Block].range_map_bytes,
            descriptor
        );
        assert!(local.pin_native_layer(LayerKind::Block).is_ok());
        assert!(layer.blob.plan.stored_bytes > 0);
        drop(plan);
        assert!(temporary.upgrade().is_some());
        drop(source);
        drop(persisted);
        assert!(temporary.upgrade().is_none());
    }

    fn captured_snapshot_pin(id: &str) -> (CapturedSnapshotPin, File) {
        use crate::snapshot::repository::frozen_layer::{VmStateMetadata, VmStatePin};
        use crate::snapshot::transport::posix_file;
        use std::os::fd::AsRawFd;

        let mut layers = Vec::new();
        for (kind, unit, byte) in [
            (LayerKind::Memory, 4096, 0x61),
            (LayerKind::Block, 65536, 0x62),
        ] {
            let name = std::ffi::CString::new(format!("adoption-{id}-{kind:?}")).unwrap();
            let file = posix_file::memory_file(&name, 2 * unit).unwrap();
            file.write_all_at(&vec![byte; unit as usize], 0).unwrap();
            layers.push(FrozenLayerPin::new(
                FrozenLayerMetadata {
                    kind,
                    layer_id: format!("{id}-{kind:?}"),
                    provisional_content_id: sha256_identity(format!("{id}-{kind:?}").as_bytes()),
                    logical_size: 2 * unit,
                    alignment: unit,
                },
                PackedOwnershipMap {
                    kind,
                    snapshot_id: id.into(),
                    format_version: 1,
                    logical_size: 2 * unit,
                    granularity: unit,
                    packed_states: vec![0b1001],
                },
                file.into(),
                false,
            ));
        }
        let payload = b"opaque VM state";
        let name = std::ffi::CString::new("adoption-vm-state").unwrap();
        let writer = posix_file::memory_file(&name, payload.len() as u64).unwrap();
        writer.write_all_at(payload, 0).unwrap();
        let readonly = posix_file::reopen_readonly(writer.as_raw_fd()).unwrap();
        let pin = CapturedSnapshotPin::new(
            id.into(),
            layers,
            VmStatePin::new(
                VmStateMetadata {
                    format_version: 1,
                    content_id: sha256_identity(payload),
                    size: payload.len() as u64,
                    alignment: 1,
                },
                readonly.into(),
            ),
        )
        .unwrap();
        (pin, writer)
    }

    fn captured_rocklet() -> RockletArtifactCompatibility {
        RockletArtifactCompatibility {
            identity: sha256_identity(b"adoption-rocklet"),
            version: "v1".into(),
            raw_sha256: sha256_identity(b"adoption-rocklet"),
            logical_bytes: 65536,
            guest_port: 4400,
        }
    }

    #[test]
    fn image_provenance_survives_native_base_capture_and_child_capture() {
        let directory = tempfile::tempdir().unwrap();
        let cache = LocalFileCache::new(directory.path().to_owned(), "origin-lineage").unwrap();
        let base = raw_snapshot("origin-base", 0x53, &cache);
        let provenance = base.provenance().unwrap().clone();
        let (parent_pin, _parent_writer) = captured_snapshot_pin("origin-parent");
        let parent = Snapshot::from_capture(
            parent_pin,
            SnapshotLineage::default().with_frozen(Arc::clone(&base)),
            captured_rocklet(),
        )
        .unwrap();
        assert_eq!(parent.provenance(), Some(&provenance));
        let (child_pin, _child_writer) = captured_snapshot_pin("origin-child");
        let child = Snapshot::from_capture(
            child_pin,
            parent.lineage().with_frozen(Arc::clone(&parent)),
            captured_rocklet(),
        )
        .unwrap();
        assert_eq!(child.provenance(), Some(&provenance));
        let plan = child
            .upload_plan(
                upload_storage(),
                VmmCompatibility {
                    architecture: "x86_64".into(),
                    vmm_type: "cloud-hypervisor".into(),
                    vmm_state_format_version: Some(1),
                    rocklet_artifact: None,
                },
            )
            .unwrap();
        assert_eq!(plan.spec.provenance.as_ref(), Some(&provenance));
        assert_eq!(
            child
                .published_snapshot(&publication(&plan))
                .unwrap()
                .provenance(),
            Some(&provenance)
        );
    }

    #[tokio::test]
    async fn native_capture_stays_local_ready_without_encoding_then_recovers_from_sealed_data() {
        let (pin, _state_writer) = captured_snapshot_pin("recovery-lifecycle");
        let local =
            Snapshot::from_capture(pin, SnapshotLineage::default(), captured_rocklet()).unwrap();
        let repository = crate::snapshot::repository::SnapshotRepository::new();
        let registered = repository.register_raw(Arc::clone(&local)).unwrap();
        assert!(Arc::ptr_eq(&local, &registered));
        assert_eq!(
            registered.state(),
            crate::snapshot::repository::PublicationState::LocalReady
        );
        assert!(registered.canonical.lock().unwrap().is_none());
        assert_eq!(registered.vm_state(), Some(b"opaque VM state".as_slice()));

        let memory = registered.backing_handle(LayerKind::Memory).unwrap();
        let block = registered.backing_handle(LayerKind::Block).unwrap();
        memory
            .backing
            .register_consumer("test-runtime", &[(0, 4096)])
            .unwrap();
        block
            .backing
            .register_consumer("test-runtime", &[(0, 65536)])
            .unwrap();
        let reader = registered
            .layer_reader(
                LayerKind::Memory,
                &registered.frozen_layers[&LayerKind::Memory].provisional_content_id,
            )
            .unwrap();
        registered.canonical_snapshot().unwrap();
        registered.close_upload_pin().unwrap();
        memory.backing.release_consumer("test-runtime");
        block.backing.release_consumer("test-runtime");
        memory.backing.reclaim_unused().await.unwrap();
        block.backing.reclaim_unused().await.unwrap();
        assert!(memory.backing.has_reclaimed_data());
        assert!(block.backing.has_reclaimed_data());
        assert!(registered.pin_native_layer(LayerKind::Memory).is_err());
        // The preexisting logical reader survives upload-pin closure and must
        // authenticate sealed DATA instead of returning zeroes from the hole.
        let context = LazyLoadContext::new(
            crate::snapshot::io::range_io::RangeIO::for_fixed_targets(),
            crate::RuntimeIdentity::new("recovery-lifecycle"),
            None,
        );
        let bytes = reader
            .read_data_range(
                &context,
                SnapshotReadRange {
                    kind: LayerKind::Memory,
                    layer_content_sha256: &registered.frozen_layers[&LayerKind::Memory]
                        .provisional_content_id,
                    logical_offset: 0,
                    length: 4096,
                },
            )
            .await
            .unwrap()
            .payload;
        assert_eq!(bytes, vec![0x61; 4096]);
        assert_eq!(
            block.backing.read_data(0, 65536).await.unwrap(),
            vec![0x62; 65536]
        );
        assert!(registered.canonical.lock().unwrap().is_some());
        assert!(Arc::ptr_eq(
            &registered
                .backing_handle(LayerKind::Memory)
                .unwrap()
                .backing,
            &memory.backing
        ));
    }

    #[test]
    fn adopted_capture_keeps_fd_identity_permissions_and_ancestor_lifetime() {
        use std::os::fd::AsRawFd;
        use std::os::unix::fs::MetadataExt;

        let (pin, _state_writer) = captured_snapshot_pin("first");
        let original = File::from(
            pin.layer(LayerKind::Memory)
                .unwrap()
                .open_readonly()
                .unwrap(),
        );
        let original_stat = original.metadata().unwrap();
        let reserved = Snapshot::creating("first", SnapshotKind::Full);
        let first = reserved
            .adopt_capture(pin, SnapshotLineage::default(), captured_rocklet())
            .unwrap();
        assert!(Arc::ptr_eq(&first, &reserved));
        drop(reserved);
        let readonly = first.pin_native_layer(LayerKind::Memory).unwrap();
        let mapping = first
            .backing_handle(LayerKind::Memory)
            .unwrap()
            .backing
            .try_clone_memory_mapping_file()
            .unwrap();
        for fd in [readonly.file(), &mapping] {
            let stat = std::fs::File::from(fd.try_clone().unwrap())
                .metadata()
                .unwrap();
            assert_eq!(
                (stat.dev(), stat.ino(), stat.len()),
                (
                    original_stat.dev(),
                    original_stat.ino(),
                    original_stat.len()
                )
            );
        }
        // SAFETY: the Arc-owned descriptors remain live throughout each query.
        assert_eq!(
            unsafe { libc::fcntl(readonly.file().as_raw_fd(), libc::F_GETFL) } & libc::O_ACCMODE,
            libc::O_RDONLY
        );
        assert_eq!(
            unsafe { libc::fcntl(mapping.as_raw_fd(), libc::F_GETFL) } & libc::O_ACCMODE,
            libc::O_RDWR
        );
        assert_eq!(first.vm_state.as_ref().unwrap().payload, b"opaque VM state");
        assert_eq!(
            first.vm_state.as_ref().unwrap().sha256,
            sha256_identity(b"opaque VM state")
        );

        let retained = Arc::downgrade(&first);
        let lineage = SnapshotLineage::default().with_frozen(Arc::clone(&first));
        let (next_pin, _next_state_writer) = captured_snapshot_pin("second");
        let second = Snapshot::from_capture(next_pin, lineage, captured_rocklet()).unwrap();
        assert_eq!(second.ancestor_snapshot_ids(), &["first".to_owned()]);
        let chain = SnapshotInstance::layer_chain(second.as_ref(), LayerKind::Memory)
            .unwrap()
            .unwrap();
        assert!(first.canonical.lock().unwrap().is_none());
        assert!(second.canonical.lock().unwrap().is_none());
        assert_eq!(chain.layers.len(), 2);
        drop(first);
        assert!(
            retained.upgrade().is_some(),
            "child snapshot must retain its original ancestor"
        );
        drop(second);
        assert!(retained.upgrade().is_none());
        let reader = File::from(readonly.file().try_clone().unwrap());
        let mut bytes = [0; 4];
        reader.read_exact_at(&mut bytes, 0).unwrap();
        assert_eq!(
            bytes, [0x61; 4],
            "retained reader FD outlives snapshot owners"
        );
    }

    #[test]
    fn failed_adoption_returns_original_pin_and_rejects_changed_vm_state() {
        use std::os::unix::fs::MetadataExt;

        let (parent_pin, _parent_state) = captured_snapshot_pin("same");
        let parent =
            Snapshot::from_capture(parent_pin, SnapshotLineage::default(), captured_rocklet())
                .unwrap();
        let (pin, state_writer) = captured_snapshot_pin("same");
        let original = File::from(
            pin.layer(LayerKind::Memory)
                .unwrap()
                .open_readonly()
                .unwrap(),
        );
        let original_inode = original.metadata().unwrap().ino();
        // This fails in metadata construction after readonly/mapping FDs
        // have already been opened, not during the initial pin validation.
        let failure = Snapshot::from_capture(
            pin,
            SnapshotLineage::default().with_frozen(parent),
            captured_rocklet(),
        )
        .unwrap_err();
        assert!(matches!(&failure.error, LocalSnapshotError::Invalid(_)));
        let pin = failure.into_pin();
        let returned = File::from(
            pin.layer(LayerKind::Memory)
                .unwrap()
                .open_readonly()
                .unwrap(),
        );
        assert_eq!(returned.metadata().unwrap().ino(), original_inode);
        assert_eq!(
            pin.layer(LayerKind::Memory)
                .unwrap()
                .read_data(0, 4096)
                .unwrap(),
            vec![0x61; 4096]
        );
        assert_eq!(pin.vm_state.read_verified().unwrap(), b"opaque VM state");

        // Read-only access does not seal the inode against another writer.
        state_writer.write_all_at(b"X", 0).unwrap();
        let failure = Snapshot::from_capture(pin, SnapshotLineage::default(), captured_rocklet())
            .unwrap_err();
        assert!(matches!(&failure.error, LocalSnapshotError::Frozen(_)));
        let pin = failure.into_pin();
        assert!(pin.vm_state.read_verified().is_err());
        state_writer.write_all_at(b"opaque VM state", 0).unwrap();
        let recovered =
            Snapshot::from_capture(pin, SnapshotLineage::default(), captured_rocklet()).unwrap();
        let recovered = File::from(
            recovered
                .pin_native_layer(LayerKind::Memory)
                .unwrap()
                .file()
                .try_clone()
                .unwrap(),
        );
        assert_eq!(recovered.metadata().unwrap().ino(), original_inode);
    }
}
