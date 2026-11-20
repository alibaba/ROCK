//! OverlayBD descriptor profiles over the same LSMT/ZFile physical format.
//!
//! Encoding IDs and validation remain profile-specific for persisted compatibility.

use super::decode_descriptor_with_blob;
use super::reader::OverlaybdLayerReader;
use crate::snapshot::format::LayerViewInterface;
use crate::snapshot::format::{DecodedLayer, LayerFormatError, LayerFormatInterface};
use crate::snapshot::metadata::{LayerRangeMap, RangeMapEncoding};
use std::sync::Arc;

pub struct OverlaybdLsmtFormat;
pub struct OverlaybdZfileFormat;

fn decode_overlaybd(payload: &[u8], compressed: bool) -> Result<DecodedLayer, LayerFormatError> {
    let (range_map, blob, length) =
        decode_descriptor_with_blob(payload).map_err(LayerFormatError::Invalid)?;
    if range_map.zfile.is_some() != compressed {
        return Err(LayerFormatError::EncodingMismatch);
    }
    Ok(DecodedLayer {
        range_map,
        blob: Some((blob, length)),
    })
}

impl LayerFormatInterface for OverlaybdLsmtFormat {
    fn open_reader(&self, map: Arc<LayerRangeMap>) -> Arc<dyn LayerViewInterface> {
        Arc::new(OverlaybdLayerReader::new(map, self.name()))
    }
    fn name(&self) -> &'static str {
        "overlaybd-lsmt-v1"
    }
    fn encoding(&self) -> Option<RangeMapEncoding> {
        Some(RangeMapEncoding::OverlaybdLsmtV1)
    }
    fn decode(&self, payload: &[u8]) -> Result<DecodedLayer, LayerFormatError> {
        decode_overlaybd(payload, false)
    }
}
impl LayerFormatInterface for OverlaybdZfileFormat {
    fn open_reader(&self, map: Arc<LayerRangeMap>) -> Arc<dyn LayerViewInterface> {
        Arc::new(OverlaybdLayerReader::new(map, self.name()))
    }
    fn name(&self) -> &'static str {
        "overlaybd-zfile-lz4-v1"
    }
    fn encoding(&self) -> Option<RangeMapEncoding> {
        Some(RangeMapEncoding::OverlaybdZfileLz4V1)
    }
    fn decode(&self, payload: &[u8]) -> Result<DecodedLayer, LayerFormatError> {
        decode_overlaybd(payload, true)
    }
}

pub struct RegistryOverlaybdFormat;

impl LayerFormatInterface for RegistryOverlaybdFormat {
    fn open_reader(&self, map: Arc<LayerRangeMap>) -> Arc<dyn LayerViewInterface> {
        Arc::new(OverlaybdLayerReader::external(map, self.name()))
    }
    fn name(&self) -> &'static str {
        "registry-overlaybd-v1"
    }
    fn encoding(&self) -> Option<RangeMapEncoding> {
        Some(RangeMapEncoding::RegistryOverlaybdV1)
    }
    fn decode(&self, payload: &[u8]) -> Result<DecodedLayer, LayerFormatError> {
        let rootfs =
            super::composite::decode_descriptor(payload).map_err(LayerFormatError::Invalid)?;
        let range_map = rootfs.build_map().map_err(LayerFormatError::Invalid)?;
        rootfs
            .validate_map(&range_map)
            .map_err(LayerFormatError::Invalid)?;
        Ok(DecodedLayer {
            range_map,
            blob: None,
        })
    }
    fn allows_p2p(&self) -> bool {
        false
    }
    fn allows_background_prefetch(&self) -> bool {
        false
    }
}
