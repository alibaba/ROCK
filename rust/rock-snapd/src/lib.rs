//! Rust implementation of the ROCK snapshot node daemon.
//!
//! Snapshot persistence and its DATA plane, object storage, VM runtime
//! control, and daemon hosting have explicit ownership boundaries.

#[cfg(not(target_os = "linux"))]
compile_error!("rock-snapd is supported only on Linux");

use serde::{Deserialize, Serialize};

/// Sandbox identity used by the node-local runtime and its backing.
#[derive(Clone, Debug, Deserialize, Eq, Hash, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeIdentity {
    pub sandbox_id: String,
}

impl RuntimeIdentity {
    pub fn new(sandbox_id: impl Into<String>) -> Self {
        Self {
            sandbox_id: sandbox_id.into(),
        }
    }
}

pub(crate) mod binary_wire;
pub mod daemon;
pub mod endpoints;
pub mod observability;
pub mod snapshot;
pub mod vm;
