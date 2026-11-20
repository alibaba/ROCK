//! Snapshot scenario services used by VM and endpoint orchestration.
//!
//! Services organize tasks and call snapshot I/O and format capabilities;
//! physical byte delivery remains in RangeIO and transport implementations.

pub mod background_prefetcher;
pub mod background_uploader;
pub mod ondemand_loader;
pub mod p2p;
