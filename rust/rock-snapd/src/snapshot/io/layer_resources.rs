//! Runtime-neutral ownership of one immutable layer reader and its local lease.
//!
//! Attachments and snapshots own these resources. Lookup indexes only hold
//! weak references; an authorized in-flight load holds the same resource Arc.

use std::fs::File;
use std::sync::atomic::{AtomicBool, AtomicUsize};
use std::sync::{Arc, Mutex};

use serde::{Deserialize, Serialize};

use super::backing::{BackingError, LayerBackingIdentity, LocalBacking, LocalBackingHandle};
use super::range_io::P2PSource;
use crate::snapshot::metadata::LayerRangeMap;
use crate::snapshot::LazyLoadInterface;
use crate::RuntimeIdentity;

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CredentialIdentity {
    pub endpoint_profile: String,
    pub credential_ref: String,
    pub client_identity: String,
}

/// Authorization belongs to one attachment, independently of shared content.
#[derive(Debug)]
pub(in crate::snapshot) struct RemoteLayerIdentity {
    pub runtime: RuntimeIdentity,
    pub target_id: String,
    pub backing_key: LayerBackingIdentity,
    pub provider: String,
    pub authorization: Option<CredentialIdentity>,
}

#[derive(Debug)]
pub struct LayerResources {
    remote: Option<RemoteLayerIdentity>,
    pub(in crate::snapshot) range_map: Mutex<Option<Arc<LayerRangeMap>>>,
    pub(in crate::snapshot) lazy_reader: Mutex<Option<Arc<dyn LazyLoadInterface>>>,
    pub(in crate::snapshot) p2p_source: Mutex<Option<P2PSource>>,
    lease: Mutex<Option<LocalBackingHandle>>,
    pub(in crate::snapshot) active_loads: AtomicUsize,
    pub(in crate::snapshot) revoked: AtomicBool,
    pub(in crate::snapshot) released: AtomicBool,
}

impl LayerResources {
    pub(in crate::snapshot) fn new_remote(
        identity: RemoteLayerIdentity,
        backing: LocalBackingHandle,
    ) -> Arc<Self> {
        Arc::new(Self {
            remote: Some(identity),
            range_map: Mutex::new(None),
            lazy_reader: Mutex::new(None),
            p2p_source: Mutex::new(None),
            lease: Mutex::new(Some(backing)),
            active_loads: AtomicUsize::new(0),
            revoked: AtomicBool::new(false),
            released: AtomicBool::new(false),
        })
    }

    /// Native and persistent attachments retain the same backing lease. A
    /// native capture's stable inode identity does not fabricate a content SHA.
    pub(crate) fn new_native(
        backing: LocalBackingHandle,
        reader: Option<Arc<dyn LazyLoadInterface>>,
    ) -> Arc<Self> {
        Arc::new(Self {
            remote: None,
            range_map: Mutex::new(None),
            lazy_reader: Mutex::new(reader),
            p2p_source: Mutex::new(None),
            lease: Mutex::new(Some(backing)),
            active_loads: AtomicUsize::new(0),
            revoked: AtomicBool::new(false),
            released: AtomicBool::new(false),
        })
    }

    /// Only remote resources are admitted into the remote target index.
    pub(in crate::snapshot) fn remote_identity(&self) -> &RemoteLayerIdentity {
        self.remote
            .as_ref()
            .expect("remote target index must contain remote layer resources")
    }

    pub(crate) fn backing(&self) -> Option<Arc<LocalBacking>> {
        let lease = self.lease.lock().expect("layer lease lock poisoned");
        Some(Arc::clone(&lease.as_ref()?.backing))
    }

    pub(crate) fn reader(&self) -> Option<Arc<dyn LazyLoadInterface>> {
        self.lazy_reader
            .lock()
            .expect("layer reader lock poisoned")
            .clone()
    }

    pub(crate) fn readonly_file(&self) -> Result<File, BackingError> {
        let lease = self.lease.lock().expect("layer lease lock poisoned");
        lease
            .as_ref()
            .ok_or(BackingError::BackingClosed)?
            .backing
            .try_clone_readonly_file()
    }

    pub(crate) fn memory_mapping_file(&self) -> Result<Option<File>, BackingError> {
        let lease = self.lease.lock().expect("layer lease lock poisoned");
        let backing = &lease.as_ref().ok_or(BackingError::BackingClosed)?.backing;
        if backing.kind() == super::backing::LayerKind::Memory {
            Ok(Some(backing.try_clone_memory_mapping_file()?))
        } else {
            Ok(None)
        }
    }

    pub(in crate::snapshot) fn backing_handle(&self) -> Option<LocalBackingHandle> {
        let lease = self.lease.lock().expect("layer lease lock poisoned");
        lease.as_ref().cloned()
    }

    pub(in crate::snapshot) fn release_lease(&self) {
        self.lease.lock().expect("layer lease lock poisoned").take();
    }
}
