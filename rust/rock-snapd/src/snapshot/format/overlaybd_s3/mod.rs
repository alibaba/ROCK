//! Snapshot metadata and publication profile for S3-compatible object stores.

pub use reader::OverlaybdS3Metadata;
pub use snapshot::OverlaybdS3Snapshot;
pub(crate) mod reader;
pub mod snapshot;

pub mod capture;
pub(crate) mod data;
pub mod plan;
pub mod publication;
pub(crate) mod scan;
pub(crate) mod validation;
