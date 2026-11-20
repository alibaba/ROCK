//! Explicit persistent-layer codecs. Selection consumes authenticated metadata;
//! invalid input is never retried through another implementation.

use crate::snapshot::metadata::{LayerRangeMap, ObjectRef, RangeMapEncoding};
use thiserror::Error;

#[derive(Debug)]
pub struct DecodedLayer {
    pub range_map: LayerRangeMap,
    /// Only owned physical objects participate in snapshot-store verification.
    pub blob: Option<(ObjectRef, u64)>,
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum LayerFormatError {
    #[error("{0}")]
    Invalid(String),
    #[error("OverlayBD descriptor codec conflicts with chain encoding")]
    EncodingMismatch,
}

pub use crate::snapshot::format::LayerFormatInterface;

use crate::snapshot::format::overlaybd::codec::RegistryOverlaybdFormat;
use crate::snapshot::format::overlaybd::codec::{OverlaybdLsmtFormat, OverlaybdZfileFormat};

/// The only built-in format selection point. Adding a codec implementation
/// does not add parsing or format branches to VM runtime code.
pub fn format_for_encoding(encoding: RangeMapEncoding) -> &'static dyn LayerFormatInterface {
    match encoding {
        RangeMapEncoding::OverlaybdLsmtV1 => &OverlaybdLsmtFormat,
        RangeMapEncoding::OverlaybdZfileLz4V1 => &OverlaybdZfileFormat,
        RangeMapEncoding::RegistryOverlaybdV1 => &RegistryOverlaybdFormat,
    }
}
