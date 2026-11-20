//! Metrics and structured events shared by node components.
//!
//! Daemon bootstrap owns configuration, construction and injection.
pub mod metrics;
pub(crate) mod read_path;
pub mod structured_log;
