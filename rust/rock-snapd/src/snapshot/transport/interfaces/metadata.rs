//! Bounded, replaceable metadata objects, separate from immutable artifacts.
//!
//! A successful whole-object replacement is last-writer-wins. This interface
//! provides no compare-and-swap or lossless concurrent-merge guarantee.

/// Transport memory envelope for one small metadata object. Domain codecs may
/// impose tighter limits without changing the byte-transport contract.
pub const MAX_METADATA_OBJECT_BYTES: u64 = 2 * 1024 * 1024;

pub use crate::snapshot::transport::MetadataObjectStore;
