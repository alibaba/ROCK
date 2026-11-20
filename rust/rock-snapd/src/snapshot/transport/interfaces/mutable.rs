//! Random writes into a local staged byte object, distinct from publication.
//!
//! The caller must own the staging object and its disk reservation. Completing
//! a range does not authenticate, publish or make an immutable object visible.
//! Remote immutable stores implement `ImmutableStreamWriter` instead.

pub use crate::snapshot::transport::MutableRangeWriter;
