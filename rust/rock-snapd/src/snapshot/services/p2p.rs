//! Source-side snapshot DATA range authorization and distribution.

use std::collections::{HashMap, HashSet};
use std::io;
use std::mem::size_of;
use std::ops::Deref;
use std::sync::{Arc, Weak};

use serde::Serialize;
use sha2::{Digest, Sha256};
use thiserror::Error;
use tokio::sync::{OnceCell, RwLock};

use crate::snapshot::format::overlaybd::OverlaybdBlobPlan;
use crate::snapshot::format::UploadedSnapshot;
use crate::snapshot::metadata::{
    canonical_json_bytes, sha256_identity, LayerChainIndex, LayerKind, LayerRangeMap, RangeKind,
    SnapshotChainView, SnapshotManifest, SnapshotSpec, MAX_DATA_RANGE_BYTES, MAX_RANGE_MAP_BYTES,
};
use crate::snapshot::repository::Snapshot;
use crate::snapshot::services::ondemand_loader::{RangeLoadExecutor, ResidentObjectRange};
use crate::snapshot::SnapshotInstance;
use crate::RuntimeIdentity;

mod native;
pub use crate::snapshot::transport::p2p::native::*;

// Versioned separately from the snapshot API. Old peers fail the handshake
// and the restore source selector can use verified OSS; never guess a codec.
pub use crate::snapshot::transport::p2p::protocol::{
    P2PAvailabilityResult, P2PBinaryFrame, P2PReadRangeRequest, P2PTcpErrorCode, P2PTcpRequest,
    P2PTcpResponse, P2P_TCP_MAGIC, P2P_TCP_MAX_FRAME_BYTES,
};
const MAX_P2P_CANONICAL_GRAPH_BYTES: usize = MAX_RANGE_MAP_BYTES;

pub use crate::snapshot::transport::p2p::tcp::{read_tcp_frame, write_tcp_frame};

impl P2PReadRangeRequest {
    pub fn validate(&self) -> Result<(), P2PServeError> {
        if crate::snapshot::metadata::validate_sha256(&self.spec_checksum).is_err()
            || crate::snapshot::metadata::validate_sha256(&self.object_sha256).is_err()
            || self.object_key.is_empty()
            || self.object_key.len() > 4096
            || self
                .object_version
                .as_ref()
                .is_some_and(|version| version.is_empty() || version.len() > 1024)
            || self.length == 0
            || self.length > MAX_DATA_RANGE_BYTES
            || self.offset.checked_add(self.length).is_none()
        {
            return Err(P2PServeError::Invalid(
                "P2P range identity or geometry is invalid".into(),
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Error)]
pub enum P2PServeError {
    #[error("invalid P2P range request: {0}")]
    Invalid(String),
    #[error("P2P snapshot DATA is not locally available")]
    NotFound,
    #[error("P2P snapshot or object identity conflicts")]
    IdentityConflict,
    #[error("P2P local DATA read failed: {0}")]
    Io(#[from] io::Error),
}

#[derive(Clone, Debug)]
struct ObjectMapping {
    kind: LayerKind,
    object_sha256: String,
    object_version: Option<String>,
    plan: Arc<OverlaybdBlobPlan>,
    length: u64,
}

#[derive(Clone, Debug)]
struct ObjectDataExtent {
    object_offset: u64,
    source_offset: u64,
    length: u64,
}

/// Resident-only access to an immutable layer owned by an external consumer.
///
/// Implementations must return `None` after the source lease is gone and must
/// never fetch a missing range. Reads must verify the complete immutable layer
/// identity and reject ranges outside its logical size. A source may use a weak
/// lease so registering a
/// peer does not extend the lifetime of an active consumer's backing files.
pub(crate) trait ResidentRangeSource: std::fmt::Debug + Send + Sync {
    fn has_local_complete_layer(
        &self,
        kind: LayerKind,
        ordinal: u32,
        layer_id: &str,
        content_sha256: &str,
    ) -> bool;

    fn read_local_complete_range(
        &self,
        kind: LayerKind,
        ordinal: u32,
        layer_id: &str,
        content_sha256: &str,
        offset: u64,
        length: u64,
    ) -> io::Result<Option<Vec<u8>>>;
}

#[derive(Clone, Debug)]
struct BindingObjectMapping {
    binding: Arc<dyn ResidentRangeSource>,
    kind: LayerKind,
    ordinal: u32,
    layer_id: String,
    layer_content_sha256: String,
    object_sha256: String,
    object_version: Option<String>,
    extents: Arc<Vec<ObjectDataExtent>>,
    length: u64,
}

#[derive(Debug)]
struct SnapshotMapping {
    spec_checksum: String,
    local: Option<Weak<Snapshot>>,
    objects: HashMap<String, ObjectMapping>,
    binding_objects: HashMap<String, BindingObjectMapping>,
    required_ranges: Arc<Vec<RemoteObjectRange>>,
    canonical_graph: Option<Arc<CanonicalGraphSlot>>,
    remote_runtimes: Vec<RemoteRuntimeMapping>,
}

#[derive(Debug)]
struct RemoteRuntimeMapping {
    runtime: RuntimeIdentity,
    service: Weak<RangeLoadExecutor>,
    objects: Arc<HashSet<RemoteObjectRange>>,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct RemoteObjectIdentity {
    object_key: String,
    object_sha256: String,
    object_version: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct RemoteObjectRange {
    object: Arc<RemoteObjectIdentity>,
    offset: u64,
    length: u64,
}

impl Deref for RemoteObjectRange {
    type Target = RemoteObjectIdentity;

    fn deref(&self) -> &Self::Target {
        &self.object
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
struct RemoteObjectIdentityRef<'a> {
    object_key: &'a str,
    object_sha256: &'a str,
    object_version: Option<&'a str>,
}

#[derive(Debug, Clone, Copy)]
struct RemoteObjectRangeRef<'a> {
    object: RemoteObjectIdentityRef<'a>,
    offset: u64,
    length: u64,
}

#[derive(Debug)]
struct CanonicalGraphSlot {
    identity: String,
    graph: OnceCell<Arc<CanonicalSnapshotGraph>>,
}

#[derive(Debug)]
struct CanonicalSnapshotGraph {
    required_ranges: Arc<Vec<RemoteObjectRange>>,
    authorized_ranges: Arc<HashSet<RemoteObjectRange>>,
    #[allow(dead_code)]
    retained_bytes: usize,
}

#[derive(Serialize)]
struct SnapshotGraphIdentity<'a> {
    snapshot_id: &'a str,
    spec_checksum: &'a str,
    spec: &'a SnapshotSpec,
    manifest: &'a SnapshotManifest,
    memory_chain: Option<&'a LayerChainIndex>,
    rootfs_chain: &'a LayerChainIndex,
}

#[derive(Debug, Default)]
pub struct P2PSnapshotServeRegistry {
    snapshots: RwLock<HashMap<String, SnapshotMapping>>,
    native: RwLock<HashMap<String, native::NativeSnapshotMapping>>,
}

impl P2PSnapshotServeRegistry {
    pub fn new() -> Arc<Self> {
        Arc::new(Self::default())
    }

    async fn canonical_graph_for_snapshot(
        &self,
        snapshot_id: &str,
        spec_checksum: &str,
        snapshot: &dyn SnapshotInstance,
    ) -> Result<(Arc<CanonicalGraphSlot>, Arc<CanonicalSnapshotGraph>), P2PServeError> {
        let identity = snapshot_graph_identity(snapshot, spec_checksum)?;
        let slot = {
            let mut snapshots = self.snapshots.write().await;
            let entry = snapshots
                .entry(snapshot_id.to_owned())
                .or_insert_with(|| empty_snapshot_mapping(spec_checksum));
            if entry.spec_checksum != spec_checksum {
                return Err(P2PServeError::IdentityConflict);
            }
            match &entry.canonical_graph {
                Some(slot) if slot.identity != identity => {
                    return Err(P2PServeError::IdentityConflict);
                }
                Some(slot) => Arc::clone(slot),
                None => {
                    let slot = Arc::new(CanonicalGraphSlot {
                        identity,
                        graph: OnceCell::new(),
                    });
                    entry.canonical_graph = Some(Arc::clone(&slot));
                    slot
                }
            }
        };
        let graph = match slot
            .graph
            .get_or_try_init(|| async { build_canonical_snapshot_graph(snapshot).map(Arc::new) })
            .await
        {
            Ok(graph) => Arc::clone(graph),
            Err(error) => {
                self.discard_failed_graph_slot(snapshot_id, &slot).await;
                return Err(error);
            }
        };
        let snapshots = self.snapshots.read().await;
        let Some(entry) = snapshots.get(snapshot_id) else {
            return Err(P2PServeError::NotFound);
        };
        if entry.spec_checksum != spec_checksum
            || entry
                .canonical_graph
                .as_ref()
                .is_none_or(|current| !Arc::ptr_eq(current, &slot))
        {
            return Err(P2PServeError::IdentityConflict);
        }
        Ok((slot, graph))
    }

    async fn discard_failed_graph_slot(&self, snapshot_id: &str, slot: &Arc<CanonicalGraphSlot>) {
        let mut snapshots = self.snapshots.write().await;
        let remove_entry = snapshots.get_mut(snapshot_id).is_some_and(|entry| {
            if entry
                .canonical_graph
                .as_ref()
                .is_some_and(|current| Arc::ptr_eq(current, slot))
            {
                entry.canonical_graph = None;
            }
            entry.local.as_ref().and_then(Weak::upgrade).is_none()
                && entry.objects.is_empty()
                && entry.binding_objects.is_empty()
                && entry.remote_runtimes.is_empty()
                && entry.required_ranges.is_empty()
                && entry.canonical_graph.is_none()
        });
        if remove_entry {
            snapshots.remove(snapshot_id);
        }
    }

    pub async fn register_local(
        &self,
        local: &Arc<Snapshot>,
        published: &UploadedSnapshot,
        spec_checksum: &str,
    ) -> Result<(), P2PServeError> {
        let objects = self.local_object_mappings(local, published, spec_checksum)?;
        let canonical = local
            .canonical_snapshot()
            .map_err(|_| P2PServeError::IdentityConflict)?;
        let graph = build_canonical_graph_from_maps(
            canonical
                .layers
                .values()
                .map(|layer| layer.range_map.as_ref()),
            MAX_P2P_CANONICAL_GRAPH_BYTES,
        )?;
        let mut snapshots = self.snapshots.write().await;
        if let Some(existing) = snapshots.get(&local.snapshot_id) {
            if existing.spec_checksum != spec_checksum {
                return Err(P2PServeError::IdentityConflict);
            }
        }
        let entry = snapshots
            .entry(local.snapshot_id.clone())
            .or_insert_with(|| empty_snapshot_mapping(spec_checksum));
        entry.local = Some(Arc::downgrade(local));
        entry.objects = objects;
        entry.binding_objects.clear();
        if entry.canonical_graph.is_none() {
            entry.required_ranges = graph.required_ranges;
        }
        Ok(())
    }

    fn local_object_mappings(
        &self,
        local: &Arc<Snapshot>,
        published: &UploadedSnapshot,
        spec_checksum: &str,
    ) -> Result<HashMap<String, ObjectMapping>, P2PServeError> {
        if local.snapshot_id.is_empty()
            || crate::snapshot::metadata::validate_sha256(spec_checksum).is_err()
            || published.manifest.snapshot_id != local.snapshot_id
        {
            return Err(P2PServeError::IdentityConflict);
        }
        let canonical = local
            .canonical_snapshot()
            .map_err(|_| P2PServeError::IdentityConflict)?;
        let mut objects = HashMap::new();
        for (kind, layer) in &canonical.layers {
            let blob = &layer.blob;
            let mut object_version = None::<Option<String>>;
            for range in &layer.range_map.ranges {
                let Some(source) = &range.source else {
                    continue;
                };
                let source_end = source
                    .pack_offset
                    .checked_add(source.encoded_length)
                    .ok_or(P2PServeError::IdentityConflict)?;
                if source.object_key != blob.object_key
                    || source.object_sha256 != blob.sha256
                    || source_end > blob.plan.stored_bytes
                    || object_version
                        .as_ref()
                        .is_some_and(|version| version != &source.object_version)
                {
                    return Err(P2PServeError::IdentityConflict);
                }
                object_version = Some(source.object_version.clone());
            }
            // All-zero/inherit layers have a standard blob but no DATA to
            // advertise. The object-store publication still includes them.
            let Some(object_version) = object_version else {
                continue;
            };
            let mapping = ObjectMapping {
                kind: *kind,
                object_sha256: blob.sha256.clone(),
                object_version,
                plan: Arc::clone(&blob.plan),
                // Peers serve decoded DATA from native backing, using inner
                // LSMT offsets even when the object SHA names a physical ZFile.
                length: blob.plan.stored_bytes,
            };
            if objects.insert(blob.object_key.clone(), mapping).is_some() {
                return Err(P2PServeError::IdentityConflict);
            }
        }
        if objects.is_empty() {
            return Err(P2PServeError::NotFound);
        }
        Ok(objects)
    }

    /// Register the complete immutable graph behind a captured snapshot. The
    /// current frozen layer is read through `Snapshot`; locally complete
    /// ancestors are read through a weak runtime binding. Remote-lazy
    /// ancestors, when present, are handled by `register_remote_runtime` and
    /// only count as available once every required range is resident.
    pub(crate) async fn register_local_graph(
        &self,
        local: &Arc<Snapshot>,
        published: &UploadedSnapshot,
        spec_checksum: &str,
        parent: Option<Arc<dyn ResidentRangeSource>>,
        snapshot: &dyn SnapshotInstance,
    ) -> Result<(), P2PServeError> {
        if snapshot.snapshot_id() != local.snapshot_id
            || snapshot.spec() != Some(&published.spec)
            || snapshot.manifest() != Some(&published.manifest)
        {
            return Err(P2PServeError::IdentityConflict);
        }
        // Validate the local and parent mappings without writing registry
        // state, so failures cannot leave an otherwise unused graph slot.
        let objects = self.local_object_mappings(local, published, spec_checksum)?;
        let binding_objects = parent
            .as_ref()
            .map(|parent| binding_object_mappings(parent, snapshot))
            .transpose()?
            .unwrap_or_default();
        // Build the bounded immutable graph before installing any local
        // mappings. Failed admission must not leave an unbounded fallback
        // index behind, and successful registration shares this same Vec.
        let (graph_slot, graph) = self
            .canonical_graph_for_snapshot(&local.snapshot_id, spec_checksum, snapshot)
            .await?;
        if graph.required_ranges.is_empty() {
            return Err(P2PServeError::NotFound);
        }
        let mut snapshots = self.snapshots.write().await;
        let entry = snapshots
            .get_mut(&local.snapshot_id)
            .ok_or(P2PServeError::NotFound)?;
        if entry.spec_checksum != spec_checksum {
            return Err(P2PServeError::IdentityConflict);
        }
        if entry
            .canonical_graph
            .as_ref()
            .is_none_or(|current| !Arc::ptr_eq(current, &graph_slot))
        {
            return Err(P2PServeError::IdentityConflict);
        }
        entry.local = Some(Arc::downgrade(local));
        entry.objects = objects;
        entry.binding_objects = binding_objects;
        entry.required_ranges = Arc::clone(&graph.required_ranges);
        Ok(())
    }

    pub async fn register_remote_runtime(
        &self,
        snapshot_id: &str,
        spec_checksum: &str,
        runtime: &RuntimeIdentity,
        service: &Arc<RangeLoadExecutor>,
        snapshot: &dyn SnapshotInstance,
    ) -> Result<(), P2PServeError> {
        if snapshot_id.is_empty()
            || crate::snapshot::metadata::validate_sha256(spec_checksum).is_err()
        {
            return Err(P2PServeError::Invalid(
                "P2P runtime snapshot identity is invalid".into(),
            ));
        }
        if snapshot.snapshot_id() != snapshot_id {
            return Err(P2PServeError::IdentityConflict);
        }
        let (graph_slot, graph) = self
            .canonical_graph_for_snapshot(snapshot_id, spec_checksum, snapshot)
            .await?;
        let mut snapshots = self.snapshots.write().await;
        let entry = snapshots
            .get_mut(snapshot_id)
            .ok_or(P2PServeError::NotFound)?;
        if entry.spec_checksum != spec_checksum {
            return Err(P2PServeError::IdentityConflict);
        }
        if entry
            .canonical_graph
            .as_ref()
            .is_none_or(|current| !Arc::ptr_eq(current, &graph_slot))
        {
            return Err(P2PServeError::IdentityConflict);
        }
        entry.required_ranges = Arc::clone(&graph.required_ranges);
        entry
            .remote_runtimes
            .retain(|mapping| mapping.service.upgrade().is_some());
        if let Some(existing) = entry
            .remote_runtimes
            .iter()
            .find(|mapping| mapping.runtime == *runtime)
        {
            if existing
                .service
                .upgrade()
                .is_some_and(|existing_service| Arc::ptr_eq(&existing_service, service))
                && Arc::ptr_eq(&existing.objects, &graph.authorized_ranges)
            {
                return Ok(());
            }
            return Err(P2PServeError::IdentityConflict);
        }
        entry.remote_runtimes.push(RemoteRuntimeMapping {
            runtime: runtime.clone(),
            service: Arc::downgrade(service),
            objects: Arc::clone(&graph.authorized_ranges),
        });
        Ok(())
    }

    pub async fn unregister_runtime(&self, runtime: &RuntimeIdentity) {
        let mut snapshots = self.snapshots.write().await;
        snapshots.retain(|_, entry| {
            entry.remote_runtimes.retain(|mapping| {
                mapping.runtime != *runtime && mapping.service.upgrade().is_some()
            });
            entry
                .local
                .as_ref()
                .is_some_and(|local| local.upgrade().is_some())
                || entry.binding_objects.values().any(|mapping| {
                    mapping.binding.has_local_complete_layer(
                        mapping.kind,
                        mapping.ordinal,
                        &mapping.layer_id,
                        &mapping.layer_content_sha256,
                    )
                })
                || !entry.remote_runtimes.is_empty()
        });
    }

    pub async fn invalidate(&self, snapshot_id: &str) {
        self.snapshots.write().await.remove(snapshot_id);
        self.native.write().await.remove(snapshot_id);
    }

    /// Selection needs proof of one resident range, not a full-graph census.
    /// Capture-owned DATA is immutable and local; every later read still
    /// validates identity and residency independently. Complete remains false
    /// (not established) on this conservative fast path.
    pub async fn availability_for_probe(
        &self,
        snapshot_id: &str,
        spec_checksum: &str,
        selection_only: bool,
    ) -> P2PAvailabilityResult {
        if selection_only {
            let snapshots = self.snapshots.read().await;
            let Some(entry) = snapshots
                .get(snapshot_id)
                .filter(|entry| entry.spec_checksum == spec_checksum)
            else {
                return unavailable(snapshot_id, spec_checksum);
            };
            let local = entry.local.as_ref().and_then(Weak::upgrade);
            let available = entry.objects.values().any(|mapping| {
                local
                    .as_ref()
                    .is_some_and(|local| local.pin_native_layer(mapping.kind).is_ok())
            }) || entry.binding_objects.values().any(|mapping| {
                mapping.binding.has_local_complete_layer(
                    mapping.kind,
                    mapping.ordinal,
                    &mapping.layer_id,
                    &mapping.layer_content_sha256,
                )
            });
            if available {
                return P2PAvailabilityResult {
                    snapshot_id: snapshot_id.to_owned(),
                    spec_checksum: spec_checksum.to_owned(),
                    available: true,
                    complete: false,
                    servable_objects: 1,
                };
            }
            // Do not turn a timed selection probe into a full range census.
            // No cheap residency proof means the peer is conservatively
            // unavailable; explicit diagnostic availability remains exact.
            return unavailable(snapshot_id, spec_checksum);
        }
        self.availability(snapshot_id, spec_checksum).await
    }

    pub async fn availability(
        &self,
        snapshot_id: &str,
        spec_checksum: &str,
    ) -> P2PAvailabilityResult {
        let (local, objects, binding_objects, required_ranges, remote_runtimes) = {
            let snapshots = self.snapshots.read().await;
            let Some(entry) = snapshots
                .get(snapshot_id)
                .filter(|entry| entry.spec_checksum == spec_checksum)
            else {
                return unavailable(snapshot_id, spec_checksum);
            };
            let remote_runtimes = entry
                .remote_runtimes
                .iter()
                .filter_map(|mapping| {
                    mapping.service.upgrade().map(|service| {
                        (
                            mapping.runtime.clone(),
                            service,
                            Arc::clone(&mapping.objects),
                        )
                    })
                })
                .collect::<Vec<_>>();
            (
                entry.local.as_ref().and_then(Weak::upgrade),
                entry.objects.clone(),
                entry.binding_objects.clone(),
                entry.required_ranges.clone(),
                remote_runtimes,
            )
        };
        let local_objects = objects
            .iter()
            .filter_map(|(key, mapping)| {
                local
                    .as_ref()
                    .is_some_and(|local| local.pin_native_layer(mapping.kind).is_ok())
                    .then_some(key.as_str())
            })
            .collect::<std::collections::HashSet<_>>();
        let binding_local_objects = binding_objects
            .iter()
            .filter_map(|(key, mapping)| {
                mapping
                    .binding
                    .has_local_complete_layer(
                        mapping.kind,
                        mapping.ordinal,
                        &mapping.layer_id,
                        &mapping.layer_content_sha256,
                    )
                    .then_some(key.as_str())
            })
            .collect::<std::collections::HashSet<_>>();
        let needs_remote = required_ranges.iter().any(|required| {
            !objects.get(&required.object_key).is_some_and(|mapping| {
                mapping.object_sha256 == required.object_sha256
                    && mapping.object_version == required.object_version
                    && range_is_within(required.offset, required.length, 0, mapping.length)
                    && local_objects.contains(required.object_key.as_str())
            }) && !binding_objects
                .get(&required.object_key)
                .is_some_and(|mapping| {
                    mapping.object_sha256 == required.object_sha256
                        && mapping.object_version == required.object_version
                        && range_is_within(required.offset, required.length, 0, mapping.length)
                        && binding_local_objects.contains(required.object_key.as_str())
                })
        });
        let mut remote_resident = Vec::new();
        if needs_remote {
            for (runtime, service, authorized) in remote_runtimes {
                remote_resident.push((authorized, service.resident_object_ranges(&runtime).await));
            }
        }
        let mut servable_object_keys = std::collections::HashSet::new();
        let mut complete = !required_ranges.is_empty();
        for required in required_ranges.iter() {
            let local_servable = objects.get(&required.object_key).is_some_and(|mapping| {
                mapping.object_sha256 == required.object_sha256
                    && mapping.object_version == required.object_version
                    && range_is_within(required.offset, required.length, 0, mapping.length)
                    && local_objects.contains(required.object_key.as_str())
            });
            let binding_servable =
                binding_objects
                    .get(&required.object_key)
                    .is_some_and(|mapping| {
                        mapping.object_sha256 == required.object_sha256
                            && mapping.object_version == required.object_version
                            && range_is_within(required.offset, required.length, 0, mapping.length)
                            && binding_local_objects.contains(required.object_key.as_str())
                    });
            let remote_identity = ResidentObjectRange {
                object_key: required.object_key.clone(),
                object_sha256: required.object_sha256.clone(),
                object_version: required.object_version.clone(),
                offset: required.offset,
                length: required.length,
            };
            let remote_servable = !local_servable
                && !binding_servable
                && remote_resident.iter().any(|(authorized, resident)| {
                    authorized.contains(required) && resident.contains(&remote_identity)
                });
            if local_servable || binding_servable || remote_servable {
                servable_object_keys.insert(required.object_key.clone());
            } else {
                complete = false;
            }
        }
        let servable_objects = servable_object_keys.len();
        P2PAvailabilityResult {
            snapshot_id: snapshot_id.to_owned(),
            spec_checksum: spec_checksum.to_owned(),
            available: servable_objects != 0,
            complete,
            servable_objects,
        }
    }

    pub async fn read_range(
        &self,
        snapshot_id: &str,
        request: &P2PReadRangeRequest,
    ) -> Result<Vec<u8>, P2PServeError> {
        request.validate()?;
        let (local, mapping, binding_mapping, remote_runtimes) = {
            let snapshots = self.snapshots.read().await;
            let entry = snapshots.get(snapshot_id).ok_or(P2PServeError::NotFound)?;
            if entry.spec_checksum != request.spec_checksum {
                return Err(P2PServeError::IdentityConflict);
            }
            let local = entry.local.as_ref().and_then(Weak::upgrade);
            let mapping = entry.objects.get(&request.object_key).cloned();
            let binding_mapping = entry.binding_objects.get(&request.object_key).cloned();
            let remote_runtimes = entry
                .remote_runtimes
                .iter()
                .filter_map(|mapping| {
                    mapping.service.upgrade().map(|service| {
                        (
                            mapping.runtime.clone(),
                            service,
                            Arc::clone(&mapping.objects),
                        )
                    })
                })
                .collect::<Vec<_>>();
            (local, mapping, binding_mapping, remote_runtimes)
        };
        if mapping.as_ref().is_some_and(|mapping| {
            mapping.object_sha256 != request.object_sha256
                || mapping.object_version != request.object_version
        }) {
            return Err(P2PServeError::IdentityConflict);
        }
        if let (Some(local), Some(mapping)) = (local, mapping) {
            return read_local_range(&local, &mapping, request);
        }
        if binding_mapping.as_ref().is_some_and(|mapping| {
            mapping.object_sha256 != request.object_sha256
                || mapping.object_version != request.object_version
        }) {
            return Err(P2PServeError::IdentityConflict);
        }
        if let Some(mapping) = binding_mapping {
            if let Some(payload) = read_binding_range(&mapping, request)? {
                return Ok(payload);
            }
        }
        let mut identity_conflict = false;
        for (runtime, service, objects) in remote_runtimes {
            let requested_end = request
                .offset
                .checked_add(request.length)
                .ok_or_else(|| P2PServeError::Invalid("range overflows".into()))?;
            // Demand reads normally match an exact authorized DATA range.
            // Keep containment support for sub-ranges without scanning the
            // entire graph on every 4 KiB exact page read.
            let exact = RemoteObjectRange {
                object: Arc::new(RemoteObjectIdentity {
                    object_key: request.object_key.clone(),
                    object_sha256: request.object_sha256.clone(),
                    object_version: request.object_version.clone(),
                }),
                offset: request.offset,
                length: request.length,
            };
            let authorized = objects.contains(&exact)
                || objects.iter().any(|object| {
                    object.object_key == request.object_key
                        && object.object_sha256 == request.object_sha256
                        && object.object_version == request.object_version
                        && request.offset >= object.offset
                        && object
                            .offset
                            .checked_add(object.length)
                            .is_some_and(|end| requested_end <= end)
                });
            if !authorized {
                identity_conflict |= objects.iter().any(|object| {
                    object.object_key == request.object_key
                        && (object.object_sha256 != request.object_sha256
                            || object.object_version != request.object_version)
                });
                continue;
            }
            match service
                .read_resident_range(
                    &runtime,
                    &request.object_key,
                    &request.object_sha256,
                    request.object_version.as_deref(),
                    request.offset,
                    request.length,
                )
                .await
            {
                Ok(Some(payload)) => return Ok(payload),
                Ok(None) => {}
                Err(error) => return Err(P2PServeError::Io(io::Error::other(error.to_string()))),
            }
        }
        Err(if identity_conflict {
            P2PServeError::IdentityConflict
        } else {
            P2PServeError::NotFound
        })
    }
}

fn empty_snapshot_mapping(spec_checksum: &str) -> SnapshotMapping {
    SnapshotMapping {
        spec_checksum: spec_checksum.to_owned(),
        local: None,
        objects: HashMap::new(),
        binding_objects: HashMap::new(),
        required_ranges: Arc::new(Vec::new()),
        canonical_graph: None,
        remote_runtimes: Vec::new(),
    }
}

fn snapshot_chains<'a>(
    snapshot: &'a dyn SnapshotInstance,
) -> Result<impl Iterator<Item = &'a SnapshotChainView> + 'a, P2PServeError> {
    let rootfs = snapshot
        .chain_view(LayerKind::Block)
        .ok_or(P2PServeError::IdentityConflict)?;
    Ok(snapshot
        .chain_view(LayerKind::Memory)
        .into_iter()
        .chain(std::iter::once(rootfs)))
}

fn snapshot_graph_identity(
    snapshot: &dyn SnapshotInstance,
    spec_checksum: &str,
) -> Result<String, P2PServeError> {
    let payload = canonical_json_bytes(&SnapshotGraphIdentity {
        snapshot_id: snapshot.snapshot_id(),
        spec_checksum,
        spec: snapshot.spec().ok_or(P2PServeError::IdentityConflict)?,
        manifest: snapshot.manifest().ok_or(P2PServeError::IdentityConflict)?,
        memory_chain: snapshot
            .chain_view(LayerKind::Memory)
            .map(|chain| &chain.index),
        rootfs_chain: &snapshot
            .chain_view(LayerKind::Block)
            .ok_or(P2PServeError::IdentityConflict)?
            .index,
    })
    .map_err(|error| P2PServeError::Invalid(error.to_string()))?;
    Ok(sha256_identity(&payload))
}

fn build_canonical_snapshot_graph(
    snapshot: &dyn SnapshotInstance,
) -> Result<CanonicalSnapshotGraph, P2PServeError> {
    build_canonical_snapshot_graph_with_limit(snapshot, MAX_P2P_CANONICAL_GRAPH_BYTES)
}

fn build_canonical_snapshot_graph_with_limit(
    snapshot: &dyn SnapshotInstance,
    max_retained_bytes: usize,
) -> Result<CanonicalSnapshotGraph, P2PServeError> {
    build_canonical_graph_from_maps(
        snapshot_chains(snapshot)?
            .into_iter()
            .flat_map(|chain| chain.layers.iter().map(|layer| layer.range_map.as_ref())),
        max_retained_bytes,
    )
}

fn build_canonical_graph_from_maps<'a>(
    maps: impl IntoIterator<Item = &'a LayerRangeMap>,
    max_retained_bytes: usize,
) -> Result<CanonicalSnapshotGraph, P2PServeError> {
    let mut builder = CanonicalGraphBuilder::new(max_retained_bytes)?;
    for map in maps {
        for range in object_ranges(map) {
            builder.insert(range?)?;
        }
    }
    builder.finish()
}

// Borrow the authenticated metadata for lookup; only the first occurrence of
// each immutable object copies its strings. Both final range collections share
// those identities, as do all runtimes registered against the graph.
struct CanonicalGraphBuilder<'a> {
    identities: HashMap<RemoteObjectIdentityRef<'a>, Arc<RemoteObjectIdentity>>,
    authorized: HashSet<RemoteObjectRange>,
    identity_bytes: usize,
    max_bytes: usize,
}

impl<'a> CanonicalGraphBuilder<'a> {
    fn new(max_bytes: usize) -> Result<Self, P2PServeError> {
        check_graph_budget(max_bytes, 0, 0, 0, 0)?;
        Ok(Self {
            identities: HashMap::new(),
            authorized: HashSet::new(),
            identity_bytes: 0,
            max_bytes,
        })
    }

    fn insert(&mut self, range: RemoteObjectRangeRef<'a>) -> Result<(), P2PServeError> {
        if range.length == 0 || range.offset.checked_add(range.length).is_none() {
            return Err(P2PServeError::IdentityConflict);
        }
        let existing = self.identities.get(&range.object).cloned();
        if let Some(object) = &existing {
            if self.authorized.contains(&RemoteObjectRange {
                object: Arc::clone(object),
                offset: range.offset,
                length: range.length,
            }) {
                return Ok(());
            }
        }
        let identity_bytes = if existing.is_none() {
            checked_graph_add(self.identity_bytes, range.object.allocation_bytes()?)?
        } else {
            self.identity_bytes
        };
        let range_count = checked_graph_add(self.authorized.len(), 1)?;
        // Admission covers old and new bucket arrays during rehash, the
        // borrowed-key interner, and the eventual Vec while the set is alive.
        // Do this before reserving either table or copying an object identity.
        let identity_table_bytes = if existing.is_none() {
            hash_table_reservation_bytes::<(RemoteObjectIdentityRef<'_>, Arc<RemoteObjectIdentity>)>(
                self.identities.capacity(),
                checked_graph_add(self.identities.len(), 1)?,
            )?
        } else {
            hash_table_bytes::<(RemoteObjectIdentityRef<'_>, Arc<RemoteObjectIdentity>)>(
                self.identities.capacity(),
            )?
        };
        check_graph_budget(
            self.max_bytes,
            identity_bytes,
            range_count,
            hash_table_reservation_bytes::<RemoteObjectRange>(
                self.authorized.capacity(),
                range_count,
            )?,
            identity_table_bytes,
        )?;
        if existing.is_none() {
            self.identities
                .try_reserve(1)
                .map_err(|_| graph_allocation_error())?;
        }
        self.authorized
            .try_reserve(1)
            .map_err(|_| graph_allocation_error())?;
        self.check_capacities(identity_bytes, range_count)?;
        let object = match existing {
            Some(object) => object,
            None => {
                let object = Arc::new(RemoteObjectIdentity {
                    object_key: graph_string(range.object.object_key)?,
                    object_sha256: graph_string(range.object.object_sha256)?,
                    object_version: range.object.object_version.map(graph_string).transpose()?,
                });
                // Account actual String capacities as well as the admitted
                // lengths (try_reserve_exact need not return an exact capacity).
                self.identity_bytes =
                    checked_graph_add(self.identity_bytes, object.allocation_bytes()?)?;
                self.check_capacities(self.identity_bytes, range_count)?;
                self.identities.insert(range.object, Arc::clone(&object));
                object
            }
        };
        self.authorized.insert(RemoteObjectRange {
            object,
            offset: range.offset,
            length: range.length,
        });
        Ok(())
    }

    fn check_capacities(
        &self,
        identity_bytes: usize,
        range_capacity: usize,
    ) -> Result<usize, P2PServeError> {
        check_graph_budget(
            self.max_bytes,
            identity_bytes,
            range_capacity,
            hash_table_bytes::<RemoteObjectRange>(self.authorized.capacity())?,
            hash_table_bytes::<(RemoteObjectIdentityRef<'_>, Arc<RemoteObjectIdentity>)>(
                self.identities.capacity(),
            )?,
        )
    }

    fn finish(self) -> Result<CanonicalSnapshotGraph, P2PServeError> {
        self.check_capacities(self.identity_bytes, self.authorized.len())?;
        // Release construction-only references before allocating the final Vec.
        drop(self.identities);
        let mut ranges = Vec::new();
        ranges
            .try_reserve_exact(self.authorized.len())
            .map_err(|_| graph_allocation_error())?;
        let retained_bytes = check_graph_budget(
            self.max_bytes,
            self.identity_bytes,
            ranges.capacity(),
            hash_table_bytes::<RemoteObjectRange>(self.authorized.capacity())?,
            0,
        )?;
        ranges.extend(self.authorized.iter().cloned());
        Ok(CanonicalSnapshotGraph {
            required_ranges: Arc::new(canonical_ranges(ranges)),
            authorized_ranges: Arc::new(self.authorized),
            retained_bytes,
        })
    }
}

impl RemoteObjectIdentityRef<'_> {
    fn allocation_bytes(&self) -> Result<usize, P2PServeError> {
        identity_allocation_bytes(
            self.object_key.len(),
            self.object_sha256.len(),
            self.object_version.map(str::len),
        )
    }
}

impl RemoteObjectIdentity {
    fn allocation_bytes(&self) -> Result<usize, P2PServeError> {
        identity_allocation_bytes(
            self.object_key.capacity(),
            self.object_sha256.capacity(),
            self.object_version.as_ref().map(String::capacity),
        )
    }
}

fn identity_allocation_bytes(
    key: usize,
    sha256: usize,
    version: Option<usize>,
) -> Result<usize, P2PServeError> {
    // Arc counters plus conservative allocation alignment/header allowance for
    // the identity and each separately allocated String payload.
    let mut bytes = checked_graph_add(size_of::<RemoteObjectIdentity>(), 4 * size_of::<usize>())?;
    for capacity in [Some(key), Some(sha256), version].into_iter().flatten() {
        bytes = checked_graph_add(bytes, checked_graph_add(capacity, 2 * size_of::<usize>())?)?;
    }
    Ok(bytes)
}

fn graph_string(value: &str) -> Result<String, P2PServeError> {
    let mut owned = String::new();
    owned
        .try_reserve_exact(value.len())
        .map_err(|_| graph_allocation_error())?;
    owned.push_str(value);
    Ok(owned)
}

fn checked_graph_add(left: usize, right: usize) -> Result<usize, P2PServeError> {
    left.checked_add(right).ok_or_else(graph_size_error)
}

fn hash_table_bytes<T>(capacity: usize) -> Result<usize, P2PServeError> {
    if capacity == 0 {
        return Ok(0);
    }
    // std HashMap/HashSet capacities exclude empty control buckets. Round up
    // to the full power-of-two bucket allocation and include control/alignment.
    let buckets = capacity
        .checked_add(1)
        .and_then(usize::checked_next_power_of_two)
        .ok_or_else(graph_size_error)?;
    buckets
        .checked_mul(checked_graph_add(size_of::<T>(), 1)?)
        .and_then(|bytes| bytes.checked_add(32 + std::mem::align_of::<T>()))
        .ok_or_else(graph_size_error)
}

fn hash_table_reservation_bytes<T>(
    capacity: usize,
    required: usize,
) -> Result<usize, P2PServeError> {
    let current = hash_table_bytes::<T>(capacity)?;
    if required <= capacity {
        return Ok(current);
    }
    // Allow an extra doubling beyond the requested growth, while retaining
    // the old allocation until rehash completes. Check actual capacities too.
    let next = capacity
        .checked_add(1)
        .and_then(|value| value.checked_mul(2))
        .ok_or_else(graph_size_error)?
        .max(required);
    checked_graph_add(current, hash_table_bytes::<T>(next)?)
}

fn check_graph_budget(
    max_bytes: usize,
    identity_bytes: usize,
    range_capacity: usize,
    authorized_bytes: usize,
    interner_bytes: usize,
) -> Result<usize, P2PServeError> {
    let range_bytes = range_capacity
        .checked_mul(size_of::<RemoteObjectRange>())
        .ok_or_else(graph_size_error)?;
    let mut bytes = size_of::<CanonicalSnapshotGraph>();
    // Graph Arc and the Arc allocations containing Vec and HashSet headers.
    for part in [
        8 * size_of::<usize>(),
        size_of::<Vec<RemoteObjectRange>>(),
        size_of::<HashSet<RemoteObjectRange>>(),
        identity_bytes,
        range_bytes,
        authorized_bytes,
        interner_bytes,
    ] {
        bytes = checked_graph_add(bytes, part)?;
    }
    if bytes > max_bytes {
        return Err(P2PServeError::Invalid(format!(
            "P2P canonical graph exceeds {max_bytes} bytes"
        )));
    }
    Ok(bytes)
}

fn graph_size_error() -> P2PServeError {
    P2PServeError::Invalid("P2P canonical graph retained byte count overflows".into())
}

fn graph_allocation_error() -> P2PServeError {
    P2PServeError::Invalid("P2P canonical graph allocation failed".into())
}

fn object_ranges(
    range_map: &LayerRangeMap,
) -> impl Iterator<Item = Result<RemoteObjectRangeRef<'_>, P2PServeError>> + '_ {
    range_map.ranges.iter().filter_map(|range| {
        if range.kind != RangeKind::Data {
            return None;
        }
        let result = range
            .source
            .as_ref()
            .ok_or(P2PServeError::IdentityConflict)
            .and_then(|source| {
                source
                    .pack_offset
                    .checked_add(source.encoded_length)
                    .ok_or(P2PServeError::IdentityConflict)?;
                Ok(RemoteObjectRangeRef {
                    object: RemoteObjectIdentityRef {
                        object_key: &source.object_key,
                        object_sha256: &source.object_sha256,
                        object_version: source.object_version.as_deref(),
                    },
                    offset: source.pack_offset,
                    length: source.encoded_length,
                })
            });
        Some(result)
    })
}

fn canonical_ranges(mut ranges: Vec<RemoteObjectRange>) -> Vec<RemoteObjectRange> {
    // Unstable sort is in-place: do not allocate a second range-sized scratch
    // buffer outside the admitted graph budget.
    ranges.sort_unstable_by(|left, right| {
        (
            &left.object_key,
            &left.object_sha256,
            &left.object_version,
            left.offset,
            left.length,
        )
            .cmp(&(
                &right.object_key,
                &right.object_sha256,
                &right.object_version,
                right.offset,
                right.length,
            ))
    });
    ranges.dedup();
    ranges
}

fn binding_object_mappings(
    binding: &Arc<dyn ResidentRangeSource>,
    snapshot: &dyn SnapshotInstance,
) -> Result<HashMap<String, BindingObjectMapping>, P2PServeError> {
    let mut mappings = HashMap::<String, BindingObjectMapping>::new();
    for chain in snapshot_chains(snapshot)? {
        add_binding_chain_mappings(binding, chain, &mut mappings)?;
    }
    for mapping in mappings.values_mut() {
        let extents = Arc::make_mut(&mut mapping.extents);
        extents.sort_by_key(|extent| (extent.object_offset, extent.length));
        extents.dedup_by_key(|extent| (extent.object_offset, extent.length));
        let mut merged: Vec<ObjectDataExtent> = Vec::with_capacity(extents.len());
        for extent in extents.drain(..) {
            if let Some(previous) = merged.last_mut() {
                if previous.object_offset.checked_add(previous.length) == Some(extent.object_offset)
                    && previous.source_offset.checked_add(previous.length)
                        == Some(extent.source_offset)
                {
                    previous.length = previous
                        .length
                        .checked_add(extent.length)
                        .ok_or(P2PServeError::IdentityConflict)?;
                    continue;
                }
            }
            merged.push(extent);
        }
        *extents = merged;
    }
    Ok(mappings)
}

fn add_binding_chain_mappings(
    binding: &Arc<dyn ResidentRangeSource>,
    chain: &SnapshotChainView,
    mappings: &mut HashMap<String, BindingObjectMapping>,
) -> Result<(), P2PServeError> {
    for layer in &chain.layers {
        let ordinal =
            u32::try_from(layer.layer_ref.ordinal).map_err(|_| P2PServeError::IdentityConflict)?;
        let kind = layer.range_map.kind;
        if !binding.has_local_complete_layer(
            kind,
            ordinal,
            &layer.layer_ref.layer_id,
            &layer.layer_ref.content_sha256,
        ) {
            continue;
        }
        for range in &layer.range_map.ranges {
            let Some(source) = &range.source else {
                continue;
            };
            let length = source
                .pack_offset
                .checked_add(source.encoded_length)
                .ok_or(P2PServeError::IdentityConflict)?;
            let mapping = mappings
                .entry(source.object_key.clone())
                .or_insert_with(|| BindingObjectMapping {
                    binding: binding.clone(),
                    kind,
                    ordinal,
                    layer_id: layer.layer_ref.layer_id.clone(),
                    layer_content_sha256: layer.layer_ref.content_sha256.clone(),
                    object_sha256: source.object_sha256.clone(),
                    object_version: source.object_version.clone(),
                    extents: Arc::new(Vec::new()),
                    length,
                });
            if mapping.object_sha256 != source.object_sha256
                || mapping.object_version != source.object_version
            {
                return Err(P2PServeError::IdentityConflict);
            }
            mapping.length = mapping.length.max(length);
            Arc::make_mut(&mut mapping.extents).push(ObjectDataExtent {
                object_offset: source.pack_offset,
                source_offset: range.logical_offset,
                length: source.encoded_length,
            });
        }
    }
    Ok(())
}

fn range_is_within(offset: u64, length: u64, outer_offset: u64, outer_length: u64) -> bool {
    let Some(end) = offset.checked_add(length) else {
        return false;
    };
    let Some(outer_end) = outer_offset.checked_add(outer_length) else {
        return false;
    };
    offset >= outer_offset && end <= outer_end
}

/// Translate physical DATA in an immutable blob back to native logical offsets.
/// A compact LSMT blob can join distant native ranges next to each other;
/// neither subtraction of one base offset nor reading through a native hole
/// preserves those bytes.
fn native_read_pieces(
    extents: &[ObjectDataExtent],
    offset: u64,
    length: u64,
) -> Result<Vec<(u64, u64)>, P2PServeError> {
    let end = offset
        .checked_add(length)
        .ok_or_else(|| P2PServeError::Invalid("range overflows".into()))?;
    if length == 0 {
        return Err(P2PServeError::Invalid("empty DATA range".into()));
    }
    let mut cursor = offset;
    let mut pieces = Vec::new();
    while cursor < end {
        let position = extents.partition_point(|extent| {
            extent
                .object_offset
                .checked_add(extent.length)
                .is_some_and(|extent_end| extent_end <= cursor)
        });
        let extent = extents
            .get(position)
            .filter(|extent| extent.object_offset <= cursor)
            .ok_or(P2PServeError::NotFound)?;
        let extent_end = extent
            .object_offset
            .checked_add(extent.length)
            .ok_or(P2PServeError::IdentityConflict)?;
        let length = (end - cursor).min(extent_end - cursor);
        let source_offset = extent
            .source_offset
            .checked_add(cursor - extent.object_offset)
            .ok_or(P2PServeError::IdentityConflict)?;
        pieces.push((source_offset, length));
        cursor += length;
    }
    Ok(pieces)
}

fn read_binding_range(
    mapping: &BindingObjectMapping,
    request: &P2PReadRangeRequest,
) -> Result<Option<Vec<u8>>, P2PServeError> {
    if !range_is_within(request.offset, request.length, 0, mapping.length) {
        return Err(P2PServeError::Invalid(
            "range exceeds immutable object".into(),
        ));
    }
    let pieces = native_read_pieces(&mapping.extents, request.offset, request.length)?;
    let mut payload = Vec::with_capacity(
        usize::try_from(request.length)
            .map_err(|_| P2PServeError::Invalid("range exceeds address space".into()))?,
    );
    for (source_offset, length) in pieces {
        let Some(bytes) = mapping
            .binding
            .read_local_complete_range(
                mapping.kind,
                mapping.ordinal,
                &mapping.layer_id,
                &mapping.layer_content_sha256,
                source_offset,
                length,
            )
            .map_err(P2PServeError::Io)?
        else {
            return Ok(None);
        };
        if bytes.len() as u64 != length {
            return Err(P2PServeError::IdentityConflict);
        }
        payload.extend_from_slice(&bytes);
    }
    Ok(Some(payload))
}

fn read_local_range(
    local: &Snapshot,
    mapping: &ObjectMapping,
    request: &P2PReadRangeRequest,
) -> Result<Vec<u8>, P2PServeError> {
    let end = request
        .offset
        .checked_add(request.length)
        .ok_or_else(|| P2PServeError::Invalid("range overflows".into()))?;
    if end > mapping.length {
        return Err(P2PServeError::Invalid(
            "range exceeds immutable object".into(),
        ));
    }
    let backing = local
        .backing_handle(mapping.kind)
        .map_err(|_| P2PServeError::NotFound)?;
    // Availability is only a hint. Pin DATA again for the entire native read;
    // a prior availability probe does not exclude concurrent reclamation.
    let reader = backing
        .backing
        .pin_native_reader()
        .map_err(|_| P2PServeError::NotFound)?;
    mapping
        .plan
        .read_at(
            reader.file(),
            request.offset,
            usize::try_from(request.length)
                .map_err(|_| P2PServeError::Invalid("range length exceeds address space".into()))?,
        )
        .map_err(|error| P2PServeError::Io(io::Error::other(error)))
}

fn unavailable(snapshot_id: &str, spec_checksum: &str) -> P2PAvailabilityResult {
    P2PAvailabilityResult {
        snapshot_id: snapshot_id.to_owned(),
        spec_checksum: spec_checksum.to_owned(),
        available: false,
        complete: false,
        servable_objects: 0,
    }
}

pub fn range_sha256(payload: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(payload))
}

#[cfg(test)]
mod overlaybd_range_tests {
    use super::*;

    fn graph_range<'a>(
        key: &'a str,
        sha256: &'a str,
        version: Option<&'a str>,
        offset: u64,
    ) -> RemoteObjectRangeRef<'a> {
        RemoteObjectRangeRef {
            object: RemoteObjectIdentityRef {
                object_key: key,
                object_sha256: sha256,
                object_version: version,
            },
            offset,
            length: 4096,
        }
    }

    #[test]
    fn canonical_graph_interns_the_full_memory_snapshot_object_identity() {
        // Real pilot shape: 399445 memory pages plus 101273 rootfs ranges.
        // Previously just these key/SHA lengths charged over 512 MiB before
        // deduplication, even though there are only three object identities.
        let keys = ["m".repeat(157), "b".repeat(157), "d".repeat(157)];
        let sha256 = format!("sha256:{}", "a".repeat(64));
        let mut builder = CanonicalGraphBuilder::new(MAX_P2P_CANONICAL_GRAPH_BYTES).unwrap();
        for (key, count) in keys.iter().zip([399445, 101197, 76]) {
            for page in 0..count {
                builder
                    .insert(graph_range(key, &sha256, None, page * 4096))
                    .unwrap();
            }
        }
        assert_eq!(builder.identities.len(), 3);
        let graph = builder.finish().unwrap();
        assert_eq!(graph.required_ranges.len(), 500718);
        assert_eq!(graph.authorized_ranges.len(), 500718);
        assert!(graph.retained_bytes < 64 * 1024 * 1024);
        let memory = &graph.required_ranges[101273];
        let next = &graph.required_ranges[101274];
        assert_eq!(memory.object_key, keys[0]);
        assert!(Arc::ptr_eq(&memory.object, &next.object));
        let authorized = graph.authorized_ranges.get(memory).unwrap();
        assert!(Arc::ptr_eq(&memory.object, &authorized.object));
    }

    #[test]
    fn canonical_graph_duplicates_need_no_additional_budget_or_allocation() {
        let mut builder = CanonicalGraphBuilder::new(32 * 1024).unwrap();
        let mut accepted = 0;
        loop {
            let state = (
                builder.identities.capacity(),
                builder.authorized.capacity(),
                builder.identity_bytes,
                builder.authorized.len(),
            );
            let range = graph_range("object", "sha256:object", Some("version"), accepted * 4096);
            if let Err(error) = builder.insert(range) {
                assert!(
                    matches!(error, P2PServeError::Invalid(message) if message.contains("exceeds"))
                );
                // Budget rejection happens before reserve/copy/registry writes.
                assert_eq!(
                    state,
                    (
                        builder.identities.capacity(),
                        builder.authorized.capacity(),
                        builder.identity_bytes,
                        builder.authorized.len(),
                    )
                );
                break;
            }
            accepted += 1;
        }
        assert!(accepted > 1);
        let capacity = builder.authorized.capacity();
        for _ in 0..10000 {
            builder
                .insert(graph_range("object", "sha256:object", Some("version"), 0))
                .unwrap();
        }
        assert_eq!(builder.authorized.len(), accepted as usize);
        assert_eq!(builder.authorized.capacity(), capacity);
        assert!(builder.finish().unwrap().retained_bytes <= 32 * 1024);
    }

    #[test]
    fn canonical_graph_bounds_distinct_identity_payloads_and_preserves_versions() {
        let keys = (0..128)
            .map(|index| format!("{index:04}{}", "k".repeat(4092)))
            .collect::<Vec<_>>();
        let mut builder = CanonicalGraphBuilder::new(128 * 1024).unwrap();
        let mut rejected = false;
        for key in &keys {
            if builder
                .insert(graph_range(key, "sha256:object", None, 0))
                .is_err()
            {
                rejected = true;
                break;
            }
        }
        assert!(rejected);
        assert!(builder.finish().unwrap().retained_bytes <= 128 * 1024);

        let mut builder = CanonicalGraphBuilder::new(16 * 1024).unwrap();
        for (sha256, version) in [
            ("sha256:first", None),
            ("sha256:first", Some("v1")),
            ("sha256:first", Some("v2")),
            ("sha256:second", Some("v1")),
        ] {
            builder
                .insert(graph_range("same-key", sha256, version, 0))
                .unwrap();
        }
        assert_eq!(builder.identities.len(), 4);
        assert_eq!(builder.finish().unwrap().required_ranges.len(), 4);
    }

    #[test]
    fn canonical_graph_budget_rejects_overflow_and_invalid_geometry() {
        assert!(CanonicalGraphBuilder::new(0).is_err());
        assert!(hash_table_bytes::<RemoteObjectRange>(usize::MAX).is_err());
        assert!(
            hash_table_reservation_bytes::<RemoteObjectRange>(usize::MAX / 2, usize::MAX).is_err()
        );
        assert!(identity_allocation_bytes(usize::MAX, 71, None).is_err());
        assert!(check_graph_budget(usize::MAX, 0, usize::MAX, 0, 0).is_err());
        let mut builder = CanonicalGraphBuilder::new(16 * 1024).unwrap();
        assert!(matches!(
            builder.insert(graph_range("object", "sha256:object", None, u64::MAX)),
            Err(P2PServeError::IdentityConflict)
        ));
        assert!(builder.identities.is_empty());
        assert_eq!(builder.authorized.capacity(), 0);
    }

    fn compact_extents() -> Vec<ObjectDataExtent> {
        vec![
            ObjectDataExtent {
                object_offset: 4096,
                source_offset: 0,
                length: 4096,
            },
            ObjectDataExtent {
                object_offset: 8192,
                source_offset: 12288,
                length: 4096,
            },
        ]
    }

    #[test]
    fn compact_blob_ranges_skip_native_zero_and_inherit_holes() {
        // The request starts inside the first DATA unit and ends in a distant
        // second unit which immediately follows it in the physical LSMT blob.
        assert_eq!(
            native_read_pieces(&compact_extents(), 6144, 4096).unwrap(),
            vec![(2048, 2048), (12288, 2048)]
        );
        assert_eq!(
            native_read_pieces(&compact_extents(), 10000, 1000).unwrap(),
            vec![(14096, 1000)]
        );
    }

    #[test]
    fn native_binding_cannot_serve_blob_metadata_or_unmapped_bytes() {
        assert!(matches!(
            native_read_pieces(&compact_extents(), 0, 4096),
            Err(P2PServeError::NotFound)
        ));
        assert!(matches!(
            native_read_pieces(&compact_extents(), 12000, 500),
            Err(P2PServeError::NotFound)
        ));
        assert!(native_read_pieces(&compact_extents(), u64::MAX - 2, 8).is_err());
        assert!(native_read_pieces(&compact_extents(), 4096, 0).is_err());
    }

    #[derive(Debug)]
    struct WeakResidentSource(Weak<Vec<u8>>);

    impl ResidentRangeSource for WeakResidentSource {
        fn has_local_complete_layer(
            &self,
            kind: LayerKind,
            ordinal: u32,
            layer_id: &str,
            content_sha256: &str,
        ) -> bool {
            kind == LayerKind::Memory
                && ordinal == 0
                && layer_id == "memory-layer"
                && content_sha256 == "sha256:layer"
                && self.0.upgrade().is_some()
        }

        fn read_local_complete_range(
            &self,
            kind: LayerKind,
            ordinal: u32,
            layer_id: &str,
            content_sha256: &str,
            offset: u64,
            length: u64,
        ) -> io::Result<Option<Vec<u8>>> {
            if !self.has_local_complete_layer(kind, ordinal, layer_id, content_sha256) {
                return Ok(None);
            }
            let Some(bytes) = self.0.upgrade() else {
                return Ok(None);
            };
            let end = offset
                .checked_add(length)
                .ok_or_else(|| io::Error::other("overflow"))?;
            let range = bytes
                .get(offset as usize..end as usize)
                .ok_or_else(|| io::Error::other("out of bounds"))?;
            Ok(Some(range.to_vec()))
        }
    }

    #[test]
    fn resident_peer_source_does_not_pin_consumer_backing() {
        let bytes = Arc::new(vec![0x5a; 4096]);
        let mapping = BindingObjectMapping {
            binding: Arc::new(WeakResidentSource(Arc::downgrade(&bytes))),
            kind: LayerKind::Memory,
            ordinal: 0,
            layer_id: "memory-layer".into(),
            layer_content_sha256: "sha256:layer".into(),
            object_sha256: "sha256:object".into(),
            object_version: None,
            extents: Arc::new(vec![ObjectDataExtent {
                object_offset: 4096,
                source_offset: 0,
                length: 4096,
            }]),
            length: 8192,
        };
        let request = P2PReadRangeRequest {
            spec_checksum: "sha256:spec".into(),
            object_key: "object".into(),
            object_sha256: "sha256:object".into(),
            object_version: None,
            offset: 4096,
            length: 4096,
        };
        assert_eq!(
            read_binding_range(&mapping, &request).unwrap(),
            Some(vec![0x5a; 4096])
        );
        assert_eq!(Arc::strong_count(&bytes), 1);
        drop(bytes);
        assert!(!mapping.binding.has_local_complete_layer(
            LayerKind::Memory,
            0,
            "memory-layer",
            "sha256:layer",
        ));
        assert!(read_binding_range(&mapping, &request).unwrap().is_none());
    }
}
