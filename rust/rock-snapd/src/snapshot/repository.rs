//! Snapshot objects, frozen-source ownership and the node-local repository.
//!
//! Publication and upload progress belong to each concrete snapshot instance. Background
//! services own execution and cancellation, never a second state table.
//! Range residency belongs to backing management and never changes publication.

pub mod frozen_layer;
pub(crate) mod lineage;

pub use crate::snapshot::instance::{
    AdoptionFailure, FrozenLocalLayer, LocalSnapshotError, Snapshot,
};

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex};

use crate::snapshot::format::overlaybd_oci::OverlaybdOciSnapshot;
use crate::snapshot::format::UploadedSnapshot;
use crate::snapshot::metadata::{
    LayerKind, SnapshotChainMetadata, StorageProvider, VmmCompatibility,
};
use crate::snapshot::{SnapshotInstance, SnapshotKind};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PublicationState {
    Creating,
    LocalReady,
    RemoteReady,
    Failed,
}

/// Physical format is part of the immutable identity. A frozen provisional
/// layer and its sealed publication remain separate identities, never an in-place
/// mutation of an already published layer.
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct LayerIdentity {
    pub kind: LayerKind,
    pub content_identity: String,
    pub format: String,
    pub logical_bytes: u64,
    pub unit_size_bytes: u64,
}

/// Registration provenance, independent of a layer's serialized format. This
/// is a transient view of the authenticated source, not a second state table.
#[derive(Clone, Copy)]
enum LayerIndexSource<'a> {
    Local {
        published_ancestors: &'a HashSet<LayerIdentity>,
    },
    Prepared,
    RemoteCommitted,
}

#[derive(Clone, Debug)]
pub struct LayerRecord {
    pub identity: LayerIdentity,
    pub state: PublicationState,
    pub last_usable: Option<PublicationState>,
    /// These owners preserve re-readable raw sources independently of tasks.
    pub local_snapshot_ids: HashSet<String>,
    pub referenced_by: HashSet<String>,
    /// Prepared upload artifacts are not the published snapshot's authoritative chain.
    pub prepared_by: HashSet<String>,
}

#[derive(Clone, Debug)]
pub struct SnapshotFailure {
    pub stage: &'static str,
    pub message: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(in crate::snapshot) struct SnapshotUploadTargetIdentity {
    pub(in crate::snapshot) provider: StorageProvider,
    pub(in crate::snapshot) endpoint_profile: String,
    pub(in crate::snapshot) backend_namespace: String,
    pub(in crate::snapshot) compatibility: VmmCompatibility,
}

#[derive(Clone, Debug)]
pub(in crate::snapshot) struct SnapshotUploadFailure {
    pub(in crate::snapshot) code: String,
    pub(in crate::snapshot) message: String,
    pub(in crate::snapshot) retryable: bool,
}

#[derive(Clone, Debug)]
pub struct SnapshotState {
    pub(in crate::snapshot) state: PublicationState,
    pub(in crate::snapshot) last_usable: Option<PublicationState>,
    pub(in crate::snapshot) remote_root_committed: bool,
    pub(in crate::snapshot) published_root_identity: Option<String>,
    pub(in crate::snapshot) local_released: bool,
    pub(in crate::snapshot) prepared_chains: Vec<SnapshotChainMetadata>,
    pub(in crate::snapshot) chains: Vec<SnapshotChainMetadata>,
    pub(in crate::snapshot) ancestor_snapshot_ids: Vec<String>,
    pub(in crate::snapshot) failure: Option<SnapshotFailure>,
    // Source health is independent of publication failure. This is sticky:
    // retrying transport must never make an observed inconsistent cut usable.
    pub(in crate::snapshot) source_integrity_failure:
        Option<crate::snapshot::transport::DataIntegrityFailure>,
    pub(in crate::snapshot) upload_identity: Option<String>,
    pub(in crate::snapshot) upload_target: Option<SnapshotUploadTargetIdentity>,
    pub(in crate::snapshot) remote_publishing: bool,
    pub(in crate::snapshot) attempt_count: u64,
    pub(in crate::snapshot) retry_at_unix_ms: Option<u64>,
    pub(in crate::snapshot) retry_scheduled: bool,
    pub(in crate::snapshot) upload_failure: Option<SnapshotUploadFailure>,
    pub(in crate::snapshot) published: Option<UploadedSnapshot>,
    pub(in crate::snapshot) snapshot_spec: Option<serde_json::Value>,
    pub(in crate::snapshot) spec_checksum: Option<String>,
    pub(in crate::snapshot) admin_finalized: bool,
}

impl SnapshotState {
    pub fn creating() -> Self {
        Self {
            state: PublicationState::Creating,
            last_usable: None,
            remote_root_committed: false,
            published_root_identity: None,
            local_released: false,
            prepared_chains: Vec::new(),
            chains: Vec::new(),
            ancestor_snapshot_ids: Vec::new(),
            failure: None,
            source_integrity_failure: None,
            upload_identity: None,
            upload_target: None,
            remote_publishing: false,
            attempt_count: 0,
            retry_at_unix_ms: None,
            retry_scheduled: false,
            upload_failure: None,
            published: None,
            snapshot_spec: None,
            spec_checksum: None,
            admin_finalized: false,
        }
    }

    pub fn remote_ready(chains: Vec<SnapshotChainMetadata>, root_identity: String) -> Self {
        Self {
            state: PublicationState::RemoteReady,
            last_usable: Some(PublicationState::RemoteReady),
            remote_root_committed: true,
            published_root_identity: Some(root_identity),
            chains,
            ..Self::creating()
        }
    }

    pub(in crate::snapshot) fn resume(&mut self) {
        if self.source_integrity_failure.is_some() {
            return;
        }
        if self.state == PublicationState::Failed {
            self.state = self.last_usable.unwrap_or(PublicationState::Creating);
        }
        self.failure = None;
    }

    pub(crate) fn fail(&mut self, stage: &'static str, message: impl Into<String>) {
        // A later task failure cannot unpublish a committed immutable root.
        if !self.remote_root_committed {
            self.state = PublicationState::Failed;
        }
        self.failure = Some(SnapshotFailure {
            stage,
            message: message.into(),
        });
        // In particular, do not drop local, chains, ancestors or last_usable.
    }
}

/// Instances own their state and resources. The repository owns identity and
/// the shared layer-reference index; it never wraps a concrete snapshot.
#[derive(Debug, Default)]
pub struct SnapshotRepository {
    snapshots: Mutex<HashMap<String, Arc<dyn SnapshotInstance>>>,
    layers: Mutex<HashMap<LayerIdentity, LayerRecord>>,
}

impl SnapshotRepository {
    pub fn new() -> Arc<Self> {
        Arc::new(Self::default())
    }

    pub fn reserve_raw(
        &self,
        snapshot_id: &str,
        kind: SnapshotKind,
    ) -> Result<Arc<Snapshot>, String> {
        let instance = Snapshot::creating(snapshot_id.to_owned(), kind);
        self.insert_vacant(Arc::clone(&instance) as Arc<dyn SnapshotInstance>)?;
        Ok(instance)
    }

    pub fn reserve_oci(&self, snapshot_id: &str) -> Result<Arc<Snapshot>, String> {
        let instance = OverlaybdOciSnapshot::creating(snapshot_id);
        self.insert_vacant(Arc::clone(&instance) as Arc<dyn SnapshotInstance>)?;
        Ok(instance)
    }

    fn insert_vacant(&self, instance: Arc<dyn SnapshotInstance>) -> Result<(), String> {
        let mut snapshots = self.snapshots.lock().expect("snapshot index poisoned");
        if snapshots.contains_key(instance.snapshot_id()) {
            return Err("snapshot ID already belongs to another instance".into());
        }
        snapshots.insert(instance.snapshot_id().to_owned(), instance);
        Ok(())
    }

    pub fn get(&self, snapshot_id: &str) -> Option<Arc<dyn SnapshotInstance>> {
        self.snapshots
            .lock()
            .expect("snapshot index poisoned")
            .get(snapshot_id)
            .cloned()
    }

    pub(in crate::snapshot) fn instances(&self) -> Vec<Arc<dyn SnapshotInstance>> {
        self.snapshots
            .lock()
            .expect("snapshot index poisoned")
            .values()
            .cloned()
            .collect()
    }

    /// Hold identity fencing through both state and layer-index mutation. A
    /// task for an invalidated instance cannot update a same-ID replacement.
    /// Callbacks must not await, do blocking I/O or re-lock this state/index.
    pub(crate) fn update_instance<T>(
        &self,
        expected: &Arc<dyn SnapshotInstance>,
        update: impl FnOnce(&mut SnapshotState) -> T,
    ) -> Option<T> {
        let snapshots = self.snapshots.lock().expect("snapshot index poisoned");
        let current = snapshots.get(expected.snapshot_id())?;
        if !Arc::ptr_eq(current, expected) {
            return None;
        }
        let mut state = current.lifecycle().lock().expect("snapshot state poisoned");
        Some(update(&mut state))
    }

    pub(crate) fn initialize_raw(
        &self,
        expected: &Arc<dyn SnapshotInstance>,
        ready: Arc<Snapshot>,
    ) -> Result<Arc<Snapshot>, String> {
        let snapshots = self.snapshots.lock().expect("snapshot index poisoned");
        let current = snapshots
            .get(expected.snapshot_id())
            .ok_or("snapshot was invalidated")?;
        if !Arc::ptr_eq(current, expected) {
            return Err("snapshot instance was replaced".into());
        }
        Arc::clone(current)
            .downcast::<Snapshot>()
            .ok_or("snapshot is not a raw instance")?
            .initialize_from(&ready)?;
        Arc::clone(current)
            .downcast::<Snapshot>()
            .ok_or("raw snapshot resources are not initialized".into())
    }

    pub(in crate::snapshot) fn initialize_oci(
        &self,
        expected: &Arc<dyn SnapshotInstance>,
        ready: Arc<Snapshot>,
    ) -> Result<(), String> {
        let snapshots = self.snapshots.lock().expect("snapshot index poisoned");
        let current = snapshots
            .get(expected.snapshot_id())
            .ok_or("snapshot was invalidated")?;
        if !Arc::ptr_eq(current, expected) {
            return Err("snapshot instance was replaced".into());
        }
        let instance = Arc::clone(current)
            .downcast::<Snapshot>()
            .ok_or("snapshot is not an OCI instance")?;
        instance.initialize_from(&ready)
    }

    pub fn layer(&self, identity: &LayerIdentity) -> Option<LayerRecord> {
        self.layers
            .lock()
            .expect("layer catalog index poisoned")
            .get(identity)
            .cloned()
    }

    /// This is metadata-only: LOCAL_READY is valid for memfd and file-backed
    /// snapshots, and registering a source never seals or compresses it.
    pub fn register_raw(&self, local: Arc<Snapshot>) -> Result<Arc<Snapshot>, String> {
        local
            .ensure_source_integrity()
            .map_err(|error| error.to_string())?;
        let chains = metadata_chains(local.as_ref())?;
        let ancestors = local.ancestor_snapshot_ids().to_vec();
        let published_ancestors = published_ancestor_layers(&local)?;
        let mut snapshots = self.snapshots.lock().expect("snapshot index poisoned");
        let instance: Arc<dyn SnapshotInstance> = match snapshots.get(&local.snapshot_id) {
            Some(existing) => {
                let same = Arc::clone(existing)
                    .downcast::<Snapshot>()
                    .is_some_and(|value| Arc::ptr_eq(&value, &local));
                if !same {
                    return Err("snapshot already belongs to another raw instance".into());
                }
                Arc::clone(existing)
            }
            None => {
                let instance: Arc<dyn SnapshotInstance> = local;
                snapshots.insert(instance.snapshot_id().to_owned(), Arc::clone(&instance));
                instance
            }
        };
        let mut data = instance
            .lifecycle()
            .lock()
            .expect("snapshot state poisoned");
        if let Some(failure) = &data.source_integrity_failure {
            return Err(failure.to_string());
        }
        if !data.remote_root_committed {
            self.index_layers(
                instance.snapshot_id(),
                &chains,
                LayerIndexSource::Local {
                    published_ancestors: &published_ancestors,
                },
            );
            data.ancestor_snapshot_ids = ancestors;
            data.state = PublicationState::LocalReady;
            data.last_usable = Some(PublicationState::LocalReady);
            data.chains = chains;
            data.failure = None;
        }
        drop(data);
        Arc::clone(&instance)
            .downcast::<Snapshot>()
            .ok_or("raw snapshot resources are not initialized".into())
    }

    /// Apply prepared metadata to an already locked record. Background tasks
    /// call this inside `update_record` to keep the record identity fenced.
    pub(in crate::snapshot) fn stage_upload_data(
        &self,
        snapshot_id: &str,
        data: &mut SnapshotState,
        chains: Vec<SnapshotChainMetadata>,
    ) -> Result<(), String> {
        validate_chains(&chains)?;
        if data.remote_root_committed {
            return if data.chains == chains {
                Ok(())
            } else {
                Err("published snapshot catalog metadata is immutable".into())
            };
        }
        data.prepared_chains = chains.clone();
        self.index_layers(snapshot_id, &chains, LayerIndexSource::Prepared);
        Ok(())
    }

    /// Finalize the persistent graph on the same identity and release only the
    /// upload capability. Native recovery and live VM backing leases survive.
    pub(crate) fn release_local_source(
        &self,
        expected: &Arc<dyn SnapshotInstance>,
    ) -> Result<(), String> {
        // Build only a metadata view from the already sealed publication. No
        // DATA reads, re-encoding or remote I/O are permitted at this boundary.
        let replacement: Option<Arc<dyn SnapshotInstance>> = if let Some(raw) = Arc::clone(expected)
            .downcast::<Snapshot>()
            .filter(|snapshot| snapshot.has_native())
        {
            let published = {
                let state = expected
                    .lifecycle()
                    .lock()
                    .expect("snapshot state poisoned");
                if !state.remote_root_committed || state.remote_publishing {
                    return Err("snapshot publication has not finished finalization".into());
                }
                state
                    .published
                    .clone()
                    .ok_or("finalized raw snapshot has no publication")?
            };
            Some(raw.published_snapshot(&published)?)
        } else {
            None
        };
        let snapshots = self.snapshots.lock().expect("snapshot index poisoned");
        let current = snapshots
            .get(expected.snapshot_id())
            .ok_or("snapshot was invalidated")?;
        if !Arc::ptr_eq(current, expected) {
            return Err("snapshot instance was replaced".into());
        }
        let mut state = current.lifecycle().lock().expect("snapshot state poisoned");
        if !state.remote_root_committed || state.remote_publishing {
            return Err("snapshot publication has not finished finalization".into());
        }
        if let Some(replacement) = &replacement {
            let spec = replacement
                .spec()
                .ok_or("replacement has no persistent source")?;
            let identity = crate::snapshot::metadata::snapshot_spec_checksum(
                &serde_json::to_value(spec).map_err(|error| error.to_string())?,
            )
            .map_err(|error| error.to_string())?;
            if replacement.snapshot_id() != expected.snapshot_id()
                || state.published_root_identity.as_deref() != Some(&identity)
            {
                return Err("finalized publication identity changed".into());
            }
            let prepared = Arc::clone(replacement)
                .downcast::<Snapshot>()
                .ok_or("persistent metadata is not a common snapshot instance")?;
            Arc::clone(current)
                .downcast::<Snapshot>()
                .ok_or("registered snapshot is not a common snapshot instance")?
                .initialize_from(prepared.as_ref())?;
            state.admin_finalized = true;
            state.local_released = true;
        } else {
            state.admin_finalized = true;
            state.local_released = true;
        }
        drop(state);
        let mut layers = self.layers.lock().expect("layer catalog index poisoned");
        for layer in layers.values_mut() {
            layer.local_snapshot_ids.remove(expected.snapshot_id());
        }
        layers.retain(|_, layer| {
            !layer.referenced_by.is_empty()
                || !layer.prepared_by.is_empty()
                || !layer.local_snapshot_ids.is_empty()
        });
        Ok(())
    }

    /// Commit the remote root to an already locked record. Upload completion
    /// writes its immutable result under the same guard before status can read it.
    pub(in crate::snapshot) fn commit_remote_data(
        &self,
        snapshot_id: &str,
        data: &mut SnapshotState,
        chains: Vec<SnapshotChainMetadata>,
        root_identity: &str,
    ) -> Result<(), String> {
        validate_chains(&chains)?;
        crate::snapshot::metadata::validate_sha256(root_identity)?;
        if data.remote_root_committed
            && (data.chains != chains
                || data.published_root_identity.as_deref() != Some(root_identity))
        {
            return Err("published snapshot catalog metadata is immutable".into());
        }
        self.index_layers(snapshot_id, &chains, LayerIndexSource::RemoteCommitted);
        data.chains = chains;
        data.prepared_chains.clear();
        data.published_root_identity = Some(root_identity.to_owned());
        data.remote_root_committed = true;
        data.state = PublicationState::RemoteReady;
        data.last_usable = Some(PublicationState::RemoteReady);
        data.failure = None;
        // The raw source stays owned until Admin finalization or invalidation;
        // upload success does not release active VM/backing references.
        Ok(())
    }

    pub fn register_opened(
        &self,
        snapshot: Arc<dyn SnapshotInstance>,
    ) -> Result<Arc<dyn SnapshotInstance>, String> {
        let spec = snapshot.spec().ok_or("snapshot has no persistent source")?;
        let value = serde_json::to_value(spec).map_err(|error| error.to_string())?;
        let root_identity = crate::snapshot::metadata::snapshot_spec_checksum(&value)
            .map_err(|error| error.to_string())?;
        let chains = metadata_chains(snapshot.as_ref())?;
        let mut snapshots = self.snapshots.lock().expect("snapshot index poisoned");
        if let Some(current) = snapshots.get(snapshot.snapshot_id()) {
            let mut state = current.lifecycle().lock().expect("snapshot state poisoned");
            if !state.remote_root_committed {
                return Err(
                    "an opened source cannot take over an uncommitted snapshot identity".into(),
                );
            }
            self.commit_remote_data(current.snapshot_id(), &mut state, chains, &root_identity)?;
            return Ok(Arc::clone(current));
        }
        {
            let mut state = snapshot
                .lifecycle()
                .lock()
                .expect("snapshot state poisoned");
            self.commit_remote_data(snapshot.snapshot_id(), &mut state, chains, &root_identity)?;
        }
        snapshots.insert(snapshot.snapshot_id().to_owned(), Arc::clone(&snapshot));
        Ok(snapshot)
    }

    /// Called only after the existing Admin delete fence and runtime-reference
    /// checks. Dropping this alias is not remote object garbage collection.
    pub fn invalidate(&self, snapshot_id: &str) {
        if let Some(instance) = self.get(snapshot_id) {
            self.invalidate_instance(&instance);
        }
    }

    /// Stale cleanup must carry the original instance, never rediscover its ID.
    /// Keep its identity fenced until all layer aliases have been removed.
    pub(crate) fn invalidate_instance(&self, expected: &Arc<dyn SnapshotInstance>) -> bool {
        let mut snapshots = self.snapshots.lock().expect("snapshot index poisoned");
        if !snapshots
            .get(expected.snapshot_id())
            .is_some_and(|current| Arc::ptr_eq(current, expected))
        {
            return false;
        }
        snapshots.remove(expected.snapshot_id());
        let mut layers = self.layers.lock().expect("layer catalog index poisoned");
        for layer in layers.values_mut() {
            layer.referenced_by.remove(expected.snapshot_id());
            layer.prepared_by.remove(expected.snapshot_id());
            layer.local_snapshot_ids.remove(expected.snapshot_id());
        }
        layers.retain(|_, layer| {
            !layer.referenced_by.is_empty()
                || !layer.prepared_by.is_empty()
                || !layer.local_snapshot_ids.is_empty()
        });
        true
    }

    fn index_layers(
        &self,
        snapshot_id: &str,
        chains: &[SnapshotChainMetadata],
        source: LayerIndexSource<'_>,
    ) {
        let mut layers = self.layers.lock().expect("layer catalog index poisoned");
        let preparing = matches!(source, LayerIndexSource::Prepared);
        let committed = matches!(source, LayerIndexSource::RemoteCommitted);
        // Replacing a view must replace references, not accumulate stale raw or
        // previous-attempt layers forever. Independent raw source pins survive.
        for layer in layers.values_mut() {
            if preparing {
                layer.prepared_by.remove(snapshot_id);
            } else {
                layer.referenced_by.remove(snapshot_id);
            }
            if committed {
                layer.prepared_by.remove(snapshot_id);
            }
        }
        for chain in chains {
            for layer in &chain.layers {
                let identity = LayerIdentity {
                    kind: chain.kind,
                    content_identity: layer.content_identity.clone(),
                    format: layer.format.clone(),
                    logical_bytes: layer.logical_bytes,
                    unit_size_bytes: layer.unit_size_bytes,
                };
                let remote = match source {
                    LayerIndexSource::Local {
                        published_ancestors,
                    } => published_ancestors.contains(&identity),
                    LayerIndexSource::Prepared => false,
                    LayerIndexSource::RemoteCommitted => true,
                };
                let state = if remote {
                    PublicationState::RemoteReady
                } else {
                    PublicationState::LocalReady
                };
                let entry = layers
                    .entry(identity.clone())
                    .or_insert_with(|| LayerRecord {
                        identity,
                        state,
                        last_usable: Some(state),
                        local_snapshot_ids: HashSet::new(),
                        referenced_by: HashSet::new(),
                        prepared_by: HashSet::new(),
                    });
                if preparing {
                    entry.prepared_by.insert(snapshot_id.to_owned());
                } else {
                    entry.referenced_by.insert(snapshot_id.to_owned());
                }
                if !remote && !preparing {
                    entry.local_snapshot_ids.insert(snapshot_id.to_owned());
                }
                if remote {
                    entry.state = PublicationState::RemoteReady;
                    entry.last_usable = Some(PublicationState::RemoteReady);
                }
            }
        }
        layers.retain(|_, layer| {
            !layer.referenced_by.is_empty()
                || !layer.prepared_by.is_empty()
                || !layer.local_snapshot_ids.is_empty()
        });
    }
}

/// Only Remote lineage entries were authenticated as already published. Frozen
/// local ancestors remain local regardless of whether their current codec is
/// raw, OverlayBD or a format added in the future.
fn published_ancestor_layers(local: &Snapshot) -> Result<HashSet<LayerIdentity>, String> {
    let mut published = HashSet::new();
    for (kind, chain_index) in local.remote_parent_chains() {
        for layer in &chain_index.layers {
            published.insert(LayerIdentity {
                kind,
                content_identity: layer.content_sha256.clone(),
                format: crate::snapshot::metadata::encoding_name(layer.range_map.encoding)?
                    .to_owned(),
                logical_bytes: chain_index.logical_bytes,
                unit_size_bytes: chain_index.unit_size_bytes,
            });
        }
    }
    Ok(published)
}

fn metadata_chains(snapshot: &dyn SnapshotInstance) -> Result<Vec<SnapshotChainMetadata>, String> {
    let mut chains = Vec::new();
    for kind in [LayerKind::Memory, LayerKind::Block] {
        if let Some(chain) = snapshot.layer_chain(kind)? {
            chains.push(chain);
        }
    }
    validate_chains(&chains)?;
    Ok(chains)
}

fn validate_chains(chains: &[SnapshotChainMetadata]) -> Result<(), String> {
    let mut kinds = HashSet::new();
    for chain in chains {
        if !kinds.insert(chain.kind)
            || chain.layers.is_empty()
            || chain.unit_size_bytes == 0
            || chain.logical_bytes == 0
        {
            return Err("snapshot catalog chain geometry is invalid".into());
        }
        for (ordinal, layer) in chain.layers.iter().enumerate() {
            if layer.ordinal != ordinal as u64
                || layer.content_identity.is_empty()
                || layer.logical_bytes != chain.logical_bytes
                || layer.unit_size_bytes != chain.unit_size_bytes
            {
                return Err("snapshot catalog layer identity or order is invalid".into());
            }
        }
    }
    if !kinds.contains(&LayerKind::Block) {
        return Err("snapshot catalog has no block chain".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn one_concurrent_reservation_owns_the_concrete_snapshot_identity() {
        let catalog = SnapshotRepository::new();
        let winners = std::thread::scope(|scope| {
            let tasks: Vec<_> = (0..8)
                .map(|_| {
                    let catalog = Arc::clone(&catalog);
                    scope.spawn(move || catalog.reserve_raw("same-id", SnapshotKind::Full))
                })
                .collect();
            tasks
                .into_iter()
                .filter_map(|task| task.join().unwrap().ok())
                .collect::<Vec<_>>()
        });
        assert_eq!(winners.len(), 1);
        let winner: Arc<dyn SnapshotInstance> = winners[0].clone();
        assert!(Arc::ptr_eq(&catalog.get("same-id").unwrap(), &winner));
        assert_eq!(winner.state(), PublicationState::Creating);
        assert!(winner.layer_chain(LayerKind::Block).is_err());
    }

    #[test]
    fn stale_state_and_cleanup_cannot_change_a_same_id_replacement_or_its_layers() {
        let catalog = SnapshotRepository::new();
        let old: Arc<dyn SnapshotInstance> =
            catalog.reserve_raw("same-id", SnapshotKind::Full).unwrap();
        assert!(catalog.invalidate_instance(&old));
        let current: Arc<dyn SnapshotInstance> =
            catalog.reserve_raw("same-id", SnapshotKind::Full).unwrap();
        let identity = LayerIdentity {
            kind: LayerKind::Block,
            content_identity: "replacement-layer".into(),
            format: "raw".into(),
            logical_bytes: 65536,
            unit_size_bytes: 65536,
        };
        catalog.index_layers(
            "same-id",
            &[SnapshotChainMetadata {
                kind: LayerKind::Block,
                logical_bytes: 65536,
                unit_size_bytes: 65536,
                layers: vec![crate::snapshot::SnapshotLayerMetadata {
                    ordinal: 0,
                    layer_id: "new-layer".into(),
                    content_identity: identity.content_identity.clone(),
                    format: "raw".into(),
                    logical_bytes: 65536,
                    unit_size_bytes: 65536,
                }],
            }],
            LayerIndexSource::Prepared,
        );
        assert!(catalog
            .update_instance(&old, |state| state.fail("late", "obsolete result"))
            .is_none());
        assert!(!catalog.invalidate_instance(&old));
        assert!(Arc::ptr_eq(&catalog.get("same-id").unwrap(), &current));
        assert_eq!(current.state(), PublicationState::Creating);
        assert!(current.failure().is_none());
        assert!(catalog
            .layer(&identity)
            .unwrap()
            .prepared_by
            .contains("same-id"));
    }

    #[test]
    fn a_verified_remote_source_cannot_take_over_a_creating_snapshot() {
        use crate::snapshot::format::overlaybd::composite::test_registry_rootfs;
        let rootfs = test_registry_rootfs();
        let plan = crate::snapshot::format::overlaybd_oci::registry_base_plan(
            "occupied",
            &rootfs,
            VmmCompatibility {
                architecture: "x86_64".into(),
                vmm_type: "any".into(),
                vmm_state_format_version: None,
                rocklet_artifact: None,
            },
        )
        .unwrap();
        let published = UploadedSnapshot {
            registry_objects: None,
            spec: plan.spec,
            manifest: serde_json::from_slice(&plan.manifest.payload).unwrap(),
        };
        let remote: Arc<dyn SnapshotInstance> =
            OverlaybdOciSnapshot::from_registry(rootfs, &published).unwrap();
        let catalog = SnapshotRepository::new();
        let owner: Arc<dyn SnapshotInstance> =
            catalog.reserve_raw("occupied", SnapshotKind::Full).unwrap();
        assert!(catalog.register_opened(remote).is_err());
        assert!(Arc::ptr_eq(&catalog.get("occupied").unwrap(), &owner));
        assert_eq!(owner.state(), PublicationState::Creating);
        assert!(owner.chains().is_empty());
    }
}
