//! Native peer identity authorization; raw logical semantics belong to format/raw.

use super::*;
use crate::snapshot::format::raw::{
    resident_layer_views as layer_views, ResidentLayerView as LayerView,
};
use crate::snapshot::metadata::LazyLoadError;

const MAX_NATIVE_METADATA_BYTES: usize = 16 * 1024 * 1024;

#[derive(Debug)]
pub(super) struct NativeSnapshotMapping {
    snapshot: Weak<Snapshot>,
    metadata: Arc<NativeSnapshotMetadata>,
}

impl LayerView<'_> {
    fn descriptor(
        self,
        kind: LayerKind,
        ordinal: u32,
    ) -> Result<NativeLayerMetadata, P2PServeError> {
        Ok(match self {
            Self::Frozen { owner, layer } => {
                let generation = owner
                    .native_generation_id()
                    .ok_or(P2PServeError::NotFound)?
                    .to_owned();
                NativeLayerMetadata {
                    kind,
                    ordinal,
                    layer_id: layer.layer_id.clone(),
                    layer_generation_id: generation.clone(),
                    identity_kind: NativeLayerIdentityKind::NativeGeneration,
                    content_identity: format!("native-generation:{generation}"),
                    logical_bytes: layer.logical_bytes,
                    coverage_unit_size: layer.unit_size_bytes,
                }
            }
            Self::Remote(layer) => NativeLayerMetadata {
                kind,
                ordinal,
                layer_id: layer.layer_id.clone(),
                layer_generation_id: layer.range_map.content_sha256.clone(),
                identity_kind: NativeLayerIdentityKind::CanonicalContent,
                content_identity: layer.range_map.content_sha256.clone(),
                logical_bytes: layer.range_map.logical_bytes,
                coverage_unit_size: layer.range_map.unit_size_bytes,
            },
            Self::MissingRemote {
                layer,
                logical_bytes,
                unit_size,
                ..
            } => NativeLayerMetadata {
                kind,
                ordinal,
                layer_id: layer.layer_id.clone(),
                layer_generation_id: layer.content_sha256.clone(),
                identity_kind: NativeLayerIdentityKind::CanonicalContent,
                content_identity: layer.content_sha256.clone(),
                logical_bytes,
                coverage_unit_size: unit_size,
            },
        })
    }
}

impl P2PSnapshotServeRegistry {
    pub async fn register_native(
        &self,
        snapshot: &Arc<Snapshot>,
    ) -> Result<NativeSnapshotMetadata, P2PServeError> {
        snapshot
            .ensure_source_integrity()
            .map_err(|error| P2PServeError::Invalid(error.to_string()))?;
        let generation = snapshot
            .native_generation_id()
            .ok_or(P2PServeError::NotFound)?;
        let mut layers = Vec::new();
        for kind in [LayerKind::Memory, LayerKind::Block] {
            let views = layer_views(snapshot, kind);
            let declared = snapshot.layer_chain(kind).map_err(P2PServeError::Invalid)?;
            let descriptions = declared
                .as_ref()
                .map(|chain| chain.layers.as_slice())
                .unwrap_or(&[]);
            if descriptions.len() != views.len() {
                return Err(P2PServeError::IdentityConflict);
            }
            for (ordinal, (view, description)) in views.into_iter().zip(descriptions).enumerate() {
                let descriptor = view.descriptor(
                    kind,
                    u32::try_from(ordinal).map_err(|_| P2PServeError::IdentityConflict)?,
                )?;
                let source_identity = match view {
                    LayerView::MissingRemote { layer, .. } => layer.content_sha256.as_str(),
                    LayerView::Frozen { layer, .. } => layer.provisional_content_id.as_str(),
                    LayerView::Remote(layer) if layer.range_map.kind == kind => {
                        layer.range_map.content_sha256.as_str()
                    }
                    _ => return Err(P2PServeError::IdentityConflict),
                };
                if description.ordinal != ordinal as u64
                    || description.layer_id != descriptor.layer_id
                    || description.logical_bytes != descriptor.logical_bytes
                    || description.unit_size_bytes != descriptor.coverage_unit_size
                    || description.content_identity.trim_start_matches("sha256:")
                        != source_identity.trim_start_matches("sha256:")
                {
                    return Err(P2PServeError::IdentityConflict);
                }
                layers.push(descriptor);
            }
        }
        let mut metadata = NativeSnapshotMetadata {
            metadata_version: NATIVE_P2P_METADATA_VERSION,
            snapshot_id: snapshot.snapshot_id.clone(),
            snapshot_kind: snapshot.snapshot_kind,
            generation_id: generation.to_owned(),
            metadata_sha256: String::new(),
            layers,
        };
        metadata.metadata_sha256 = metadata
            .descriptor_checksum()
            .map_err(P2PServeError::Invalid)?;
        metadata.validate().map_err(P2PServeError::Invalid)?;
        if serde_json::to_vec(&metadata)
            .map_err(|error| P2PServeError::Invalid(error.to_string()))?
            .len()
            > MAX_NATIVE_METADATA_BYTES
        {
            return Err(P2PServeError::Invalid(
                "native descriptor metadata exceeds its bound".into(),
            ));
        }
        let mut native = self.native.write().await;
        snapshot
            .ensure_source_integrity()
            .map_err(|error| P2PServeError::Invalid(error.to_string()))?;
        native.retain(|_, entry| entry.snapshot.strong_count() != 0);
        if let Some(existing) = native.get(&snapshot.snapshot_id) {
            if existing.metadata.as_ref() != &metadata {
                return Err(P2PServeError::IdentityConflict);
            }
        }
        native.insert(
            snapshot.snapshot_id.clone(),
            NativeSnapshotMapping {
                snapshot: Arc::downgrade(snapshot),
                metadata: Arc::new(metadata.clone()),
            },
        );
        Ok(metadata)
    }

    async fn native_source(
        &self,
        snapshot_id: &str,
        generation: Option<&str>,
    ) -> Result<(Arc<Snapshot>, Arc<NativeSnapshotMetadata>), P2PServeError> {
        let native = self.native.read().await;
        let entry = native.get(snapshot_id).ok_or(P2PServeError::NotFound)?;
        if generation.is_some_and(|generation| generation != entry.metadata.generation_id) {
            return Err(P2PServeError::IdentityConflict);
        }
        let snapshot = entry.snapshot.upgrade().ok_or(P2PServeError::NotFound)?;
        if snapshot.native_generation_id() != Some(entry.metadata.generation_id.as_str()) {
            return Err(P2PServeError::IdentityConflict);
        }
        // A previously issued generation token is not a health exemption.
        snapshot
            .ensure_source_integrity()
            .map_err(|error| P2PServeError::Invalid(error.to_string()))?;
        Ok((snapshot, Arc::clone(&entry.metadata)))
    }

    pub async fn native_metadata(
        &self,
        snapshot_id: &str,
        generation: Option<&str>,
    ) -> Result<NativeSnapshotMetadata, P2PServeError> {
        let (snapshot, metadata) = self.native_source(snapshot_id, generation).await?;
        snapshot
            .ensure_source_integrity()
            .map_err(|error| P2PServeError::Invalid(error.to_string()))?;
        Ok(metadata.as_ref().clone())
    }

    pub async fn native_coverage(
        &self,
        snapshot_id: &str,
        request: &NativeCoverageRequest,
    ) -> Result<NativeCoverageResult, P2PServeError> {
        let (snapshot, metadata) = self
            .native_source(snapshot_id, Some(&request.generation_id))
            .await?;
        let descriptor = authorized_layer(
            &metadata,
            request.kind,
            request.ordinal,
            &request.layer_id,
            &request.layer_generation_id,
        )?;
        checked_range(descriptor, request.offset, request.length)?;
        let view = layer_views(&snapshot, request.kind)
            .get(request.ordinal as usize)
            .copied()
            .ok_or(P2PServeError::NotFound)?;
        let page = view
            .coverage(request.offset, request.length, MAX_NATIVE_COVERAGE_UNITS)
            .map_err(raw_error)?
            .ok_or(P2PServeError::NotFound)?;
        snapshot
            .ensure_source_integrity()
            .map_err(|error| P2PServeError::Invalid(error.to_string()))?;
        Ok(NativeCoverageResult {
            offset: page.offset,
            length: page.length,
            unit_size: page.unit_size,
            packed_states: page.packed_states,
        })
    }

    pub async fn read_native_range(
        &self,
        snapshot_id: &str,
        request: &NativeReadRangeRequest,
    ) -> Result<Vec<u8>, P2PServeError> {
        let (snapshot, metadata) = self
            .native_source(snapshot_id, Some(&request.generation_id))
            .await?;
        let descriptor = authorized_layer(
            &metadata,
            request.kind,
            request.ordinal,
            &request.layer_id,
            &request.layer_generation_id,
        )?;
        checked_range(descriptor, request.offset, request.length)?;
        let view = layer_views(&snapshot, request.kind)
            .get(request.ordinal as usize)
            .copied()
            .ok_or(P2PServeError::NotFound)?;
        let bytes = view
            .read_resident(request.ordinal, request.offset, request.length)
            .await
            .map_err(raw_error)?
            .ok_or(P2PServeError::NotFound)?;
        snapshot
            .ensure_source_integrity()
            .map_err(|error| P2PServeError::Invalid(error.to_string()))?;
        Ok(bytes)
    }

    pub async fn native_availability(
        &self,
        snapshot_id: &str,
        generation: &str,
        selection_only: bool,
    ) -> NativeAvailabilityResult {
        let unavailable = || NativeAvailabilityResult {
            snapshot_id: snapshot_id.to_owned(),
            generation_id: generation.to_owned(),
            available: false,
            complete: false,
            servable_layers: 0,
        };
        let Ok((snapshot, metadata)) = self.native_source(snapshot_id, Some(generation)).await
        else {
            return unavailable();
        };
        let mut servable_layers = 0;
        let mut complete = true;
        for descriptor in &metadata.layers {
            let Some(view) = layer_views(&snapshot, descriptor.kind)
                .get(descriptor.ordinal as usize)
                .copied()
            else {
                complete = false;
                continue;
            };
            let Ok(residency) = view.residency(selection_only).await else {
                complete = false;
                continue;
            };
            complete &= residency.complete;
            servable_layers += usize::from(residency.any_owned);
            if selection_only && residency.any_owned {
                if snapshot.ensure_source_integrity().is_err() {
                    return unavailable();
                }
                return NativeAvailabilityResult {
                    snapshot_id: snapshot_id.to_owned(),
                    generation_id: generation.to_owned(),
                    available: true,
                    complete: false,
                    servable_layers: 1,
                };
            }
        }
        if snapshot.ensure_source_integrity().is_err() {
            return unavailable();
        }
        NativeAvailabilityResult {
            snapshot_id: snapshot_id.to_owned(),
            generation_id: generation.to_owned(),
            available: servable_layers != 0,
            complete,
            servable_layers,
        }
    }
}

fn raw_error(error: LazyLoadError) -> P2PServeError {
    match error {
        LazyLoadError::InvalidRange => P2PServeError::Invalid(error.to_string()),
        LazyLoadError::IdentityConflict => P2PServeError::IdentityConflict,
        LazyLoadError::NonDataRange => P2PServeError::NotFound,
        error => P2PServeError::Io(io::Error::other(error.to_string())),
    }
}

fn authorized_layer<'a>(
    metadata: &'a NativeSnapshotMetadata,
    kind: LayerKind,
    ordinal: u32,
    layer_id: &str,
    generation: &str,
) -> Result<&'a NativeLayerMetadata, P2PServeError> {
    metadata
        .layers
        .iter()
        .find(|layer| layer.kind == kind && layer.ordinal == ordinal)
        .filter(|layer| layer.layer_id == layer_id && layer.layer_generation_id == generation)
        .ok_or(P2PServeError::IdentityConflict)
}

fn checked_range(
    layer: &NativeLayerMetadata,
    offset: u64,
    length: u64,
) -> Result<u64, P2PServeError> {
    offset
        .checked_add(length)
        .filter(|end| length != 0 && *end <= layer.logical_bytes)
        .ok_or_else(|| P2PServeError::Invalid("native logical range exceeds its layer".into()))
}

#[cfg(test)]
mod tests;
