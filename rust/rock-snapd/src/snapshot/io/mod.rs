//! Shared snapshot graph I/O, neutral range coordination and byte residency.
//! Scenario services call these capabilities without reverse dependencies.

pub mod backing;
pub mod layer_resources;
pub(crate) mod oci_publication;
pub mod publication;
pub mod range_io;
pub(crate) mod registry_store;
pub mod snapshot_io;
