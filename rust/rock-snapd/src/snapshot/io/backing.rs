//! Node-local sparse backing identity and lifecycle with RAII ownership.

use std::collections::{HashMap, HashSet};
use std::fs::{self, File};
use std::future::Future;
use std::io;
use std::os::fd::AsRawFd;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex as StdMutex, OnceLock, Weak};

use serde::{Deserialize, Serialize};
use thiserror::Error;
use tokio::sync::{Mutex, Notify};
use uuid::Uuid;

use crate::observability::metrics::{LeaseKind, SnapdMetrics};
use crate::snapshot::transport::posix_file;

mod native;
pub use native::{BackingRecovery, DependencyCounts, NativeBackingMetadata, NativeReadLease};

// Download residency state is owned by the backing whose bytes it describes.

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum DownloadState {
    NotResident,
    Loading,
    Resident,
    LoadFailed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum RangeLoadOutcome {
    CacheHit,
    Loaded,
    Joined,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DataRangeKey {
    pub target_id: String,
    pub offset: u64,
    pub length: u64,
}

impl DataRangeKey {
    pub fn new(target_id: impl Into<String>, offset: u64, length: u64) -> Result<Self, RangeError> {
        let key = Self {
            target_id: target_id.into(),
            offset,
            length,
        };
        if key.target_id.is_empty() {
            return Err(RangeError::InvalidKey("target_id must not be empty".into()));
        }
        if key.length == 0 || key.offset.checked_add(key.length).is_none() {
            return Err(RangeError::InvalidKey(
                "range length must be positive and must not overflow".into(),
            ));
        }
        Ok(key)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DataRangeStatus {
    pub state: DownloadState,
    pub attempt_count: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_error: Option<String>,
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum RangeError {
    #[error("invalid DATA range: {0}")]
    InvalidKey(String),
    #[error("range load failed: {0}")]
    LoadFailed(String),
}

#[derive(Debug)]
struct Attempt {
    notify: Notify,
    result: StdMutex<Option<Result<(), String>>>,
}

impl Attempt {
    fn new() -> Self {
        Self {
            notify: Notify::new(),
            result: StdMutex::new(None),
        }
    }

    fn finish(&self, result: Result<(), String>) {
        let mut terminal = self.result.lock().expect("attempt result lock poisoned");
        if terminal.is_none() {
            *terminal = Some(result);
            self.notify.notify_waiters();
        }
    }

    async fn wait(&self) -> Result<(), String> {
        loop {
            let notified = self.notify.notified();
            if let Some(result) = self
                .result
                .lock()
                .expect("attempt result lock poisoned")
                .clone()
            {
                return result;
            }
            notified.await;
        }
    }
}

#[derive(Debug)]
struct Entry {
    state: DownloadState,
    attempt_count: u64,
    last_error: Option<String>,
    load: Option<Arc<Attempt>>,
}

impl Default for Entry {
    fn default() -> Self {
        Self {
            state: DownloadState::NotResident,
            attempt_count: 0,
            last_error: None,
            load: None,
        }
    }
}

#[derive(Debug, Default)]
pub struct DataRangeResidencyRegistry {
    entries: Mutex<HashMap<DataRangeKey, Entry>>,
    backing: OnceLock<Weak<LocalBacking>>,
}

impl DataRangeResidencyRegistry {
    pub fn new() -> Arc<Self> {
        Arc::new(Self::default())
    }

    fn native_load_completed(&self, key: &DataRangeKey) {
        let Some(backing) = self.backing.get().and_then(Weak::upgrade) else {
            return;
        };
        if !backing.queue_materialized_range(key.offset, key.length) {
            return;
        }
        if let Ok(runtime) = tokio::runtime::Handle::try_current() {
            runtime.spawn(async move {
                let _ = backing.reclaim_unused().await;
            });
        }
    }

    pub async fn status(&self, key: &DataRangeKey) -> DataRangeStatus {
        let entries = self.entries.lock().await;
        entries.get(key).map_or(
            DataRangeStatus {
                state: DownloadState::NotResident,
                attempt_count: 0,
                last_error: None,
            },
            |entry| DataRangeStatus {
                state: entry.state,
                attempt_count: entry.attempt_count,
                last_error: entry.last_error.clone(),
            },
        )
    }

    pub async fn ensure_loaded<F, Fut, E>(
        self: &Arc<Self>,
        key: DataRangeKey,
        load: F,
    ) -> Result<RangeLoadOutcome, RangeError>
    where
        F: FnOnce() -> Fut + Send + 'static,
        Fut: Future<Output = Result<(), E>> + Send + 'static,
        E: std::fmt::Display + Send + 'static,
    {
        enum Decision {
            WaitLoad(Arc<Attempt>, bool),
            CacheHit,
        }

        let decision = {
            let mut entries = self.entries.lock().await;
            let entry = entries.entry(key.clone()).or_default();
            if entry.state == DownloadState::Resident {
                Decision::CacheHit
            } else if entry.state == DownloadState::Loading {
                Decision::WaitLoad(
                    Arc::clone(
                        entry
                            .load
                            .as_ref()
                            .expect("LOADING range must own an attempt"),
                    ),
                    false,
                )
            } else {
                let attempt = Arc::new(Attempt::new());
                entry.state = DownloadState::Loading;
                entry.attempt_count += 1;
                entry.last_error = None;
                entry.load = Some(Arc::clone(&attempt));
                Decision::WaitLoad(attempt, true)
            }
        };

        match decision {
            Decision::CacheHit => Ok(RangeLoadOutcome::CacheHit),
            Decision::WaitLoad(attempt, owner) => {
                if owner {
                    let registry = Arc::clone(self);
                    let key_for_task = key;
                    let attempt_for_task = Arc::clone(&attempt);
                    let worker =
                        tokio::spawn(
                            async move { load().await.map_err(|error| error.to_string()) },
                        );
                    tokio::spawn(async move {
                        let result = worker.await.unwrap_or_else(|error| {
                            Err(format!("load worker terminated before completion: {error}"))
                        });
                        let applied = registry
                            .finish_load(&key_for_task, &attempt_for_task, result.clone())
                            .await;
                        if applied {
                            attempt_for_task.finish(result);
                            registry.native_load_completed(&key_for_task);
                        }
                    });
                }
                attempt.wait().await.map_err(RangeError::LoadFailed)?;
                Ok(if owner {
                    RangeLoadOutcome::Loaded
                } else {
                    RangeLoadOutcome::Joined
                })
            }
        }
    }

    /// Load one immutable contiguous group and publish every exact member as
    /// resident in the same registry critical section. This is used when one
    /// provider read covers multiple independently addressable DATA grants.
    pub async fn ensure_group_loaded<F, Fut, E>(
        self: &Arc<Self>,
        group_key: DataRangeKey,
        member_keys: Vec<DataRangeKey>,
        load: F,
    ) -> Result<RangeLoadOutcome, RangeError>
    where
        F: FnOnce() -> Fut + Send + 'static,
        Fut: Future<Output = Result<(), E>> + Send + 'static,
        E: std::fmt::Display + Send + 'static,
    {
        validate_group_keys(&group_key, &member_keys)?;

        enum Decision {
            WaitLoad(Arc<Attempt>, bool, Vec<Arc<Attempt>>),
            CacheHit,
        }

        let decision = {
            let mut entries = self.entries.lock().await;
            let entry = entries.entry(group_key.clone()).or_default();
            if entry.state == DownloadState::Resident {
                Decision::CacheHit
            } else if entry.state == DownloadState::Loading {
                Decision::WaitLoad(
                    Arc::clone(
                        entry
                            .load
                            .as_ref()
                            .expect("LOADING range must own an attempt"),
                    ),
                    false,
                    Vec::new(),
                )
            } else {
                let attempt = Arc::new(Attempt::new());
                entry.state = DownloadState::Loading;
                entry.attempt_count += 1;
                entry.last_error = None;
                entry.load = Some(Arc::clone(&attempt));
                let mut superseded = Vec::new();
                for member_key in &member_keys {
                    if member_key == &group_key {
                        continue;
                    }
                    let member = entries.entry(member_key.clone()).or_default();
                    if member.state == DownloadState::Resident {
                        continue;
                    }
                    if member.state == DownloadState::Loading {
                        if let Some(old_attempt) = member.load.take() {
                            if !superseded
                                .iter()
                                .any(|existing| Arc::ptr_eq(existing, &old_attempt))
                            {
                                superseded.push(old_attempt);
                            }
                        }
                    } else {
                        member.attempt_count += 1;
                    }
                    member.state = DownloadState::Loading;
                    member.last_error = None;
                    member.load = Some(Arc::clone(&attempt));
                }
                Decision::WaitLoad(attempt, true, superseded)
            }
        };

        match decision {
            Decision::CacheHit => Ok(RangeLoadOutcome::CacheHit),
            Decision::WaitLoad(attempt, owner, superseded) => {
                if owner {
                    let registry = Arc::clone(self);
                    let group_key_for_task = group_key;
                    let attempt_for_task = Arc::clone(&attempt);
                    let worker =
                        tokio::spawn(
                            async move { load().await.map_err(|error| error.to_string()) },
                        );
                    tokio::spawn(async move {
                        let result = worker.await.unwrap_or_else(|error| {
                            Err(format!("load worker terminated before completion: {error}"))
                        });
                        registry
                            .finish_group_load(
                                &group_key_for_task,
                                &member_keys,
                                &superseded,
                                &attempt_for_task,
                                result.clone(),
                            )
                            .await;
                        attempt_for_task.finish(result);
                        registry.native_load_completed(&group_key_for_task);
                    });
                }
                attempt.wait().await.map_err(RangeError::LoadFailed)?;
                Ok(if owner {
                    RangeLoadOutcome::Loaded
                } else {
                    RangeLoadOutcome::Joined
                })
            }
        }
    }

    pub async fn resident_ranges(&self) -> HashSet<DataRangeKey> {
        self.entries
            .lock()
            .await
            .iter()
            .filter(|(_, entry)| entry.state == DownloadState::Resident)
            .map(|(key, _)| key.clone())
            .collect()
    }

    pub async fn resident_members(&self, keys: &[DataRangeKey]) -> Vec<bool> {
        let entries = self.entries.lock().await;
        keys.iter()
            .map(|key| {
                entries
                    .get(key)
                    .is_some_and(|entry| entry.state == DownloadState::Resident)
            })
            .collect()
    }

    async fn finish_load(
        &self,
        key: &DataRangeKey,
        attempt: &Arc<Attempt>,
        result: Result<(), String>,
    ) -> bool {
        let mut entries = self.entries.lock().await;
        let Some(entry) = entries.get_mut(key) else {
            return false;
        };
        if !entry
            .load
            .as_ref()
            .is_some_and(|active| Arc::ptr_eq(active, attempt))
        {
            return false;
        }
        entry.load = None;
        match result {
            Ok(()) => {
                entry.state = DownloadState::Resident;
                entry.last_error = None;
            }
            Err(error) => {
                entry.state = DownloadState::LoadFailed;
                entry.last_error = Some(error);
            }
        }
        true
    }

    async fn finish_group_load(
        &self,
        group_key: &DataRangeKey,
        member_keys: &[DataRangeKey],
        superseded: &[Arc<Attempt>],
        attempt: &Arc<Attempt>,
        result: Result<(), String>,
    ) {
        let mut entries = self.entries.lock().await;
        let Some(group_entry) = entries.get_mut(group_key) else {
            return;
        };
        if !group_entry
            .load
            .as_ref()
            .is_some_and(|active| Arc::ptr_eq(active, attempt))
        {
            return;
        }
        group_entry.load = None;
        let failure = result.as_ref().err().cloned();
        group_entry.state = if failure.is_some() {
            DownloadState::LoadFailed
        } else {
            DownloadState::Resident
        };
        group_entry.last_error = failure.clone();
        for key in member_keys {
            let entry = entries.entry(key.clone()).or_default();
            if entry.state == DownloadState::Resident && failure.is_some() {
                continue;
            }
            if !entry
                .load
                .as_ref()
                .is_some_and(|active| Arc::ptr_eq(active, attempt))
            {
                continue;
            }
            entry.load = None;
            entry.state = if failure.is_some() {
                DownloadState::LoadFailed
            } else {
                DownloadState::Resident
            };
            entry.last_error = failure.clone();
        }
        // Attempt completion is synchronous. Publishing terminal results while
        // the registry lock is held prevents superseded exact workers from
        // racing a stale result ahead of the group result.
        for member_attempt in superseded {
            member_attempt.finish(result.clone());
        }
    }
}

fn validate_group_keys(
    group_key: &DataRangeKey,
    member_keys: &[DataRangeKey],
) -> Result<(), RangeError> {
    if member_keys.is_empty() {
        return Err(RangeError::InvalidKey(
            "resident group must contain at least one exact member".into(),
        ));
    }
    let group_end = group_key
        .offset
        .checked_add(group_key.length)
        .ok_or_else(|| RangeError::InvalidKey("resident group range overflows".into()))?;
    let mut cursor = group_key.offset;
    for member in member_keys {
        if member.target_id != group_key.target_id || member.offset != cursor {
            return Err(RangeError::InvalidKey(
                "resident group members must exactly and contiguously cover the group".into(),
            ));
        }
        cursor = cursor
            .checked_add(member.length)
            .ok_or_else(|| RangeError::InvalidKey("resident group member overflows".into()))?;
    }
    if cursor != group_end {
        return Err(RangeError::InvalidKey(
            "resident group members must exactly and contiguously cover the group".into(),
        ));
    }
    Ok(())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum LayerKind {
    Memory,
    Block,
}

impl LayerKind {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Memory => "memory",
            Self::Block => "block",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LayerBackingIdentity {
    pub kind: LayerKind,
    pub format_version: u64,
    pub logical_size: u64,
    pub unit_size: u64,
    pub content_sha256: String,
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum LayerBackingIdentityError {
    #[error("format version must be positive")]
    InvalidFormatVersion,
    #[error("logical geometry must be nonzero and unit aligned")]
    InvalidGeometry,
    #[error("content_sha256 must contain 64 lowercase hex characters")]
    InvalidContentHash,
}

impl LayerBackingIdentity {
    pub fn validate(&self) -> Result<(), LayerBackingIdentityError> {
        if self.format_version == 0 {
            return Err(LayerBackingIdentityError::InvalidFormatVersion);
        }
        if self.logical_size == 0
            || self.unit_size == 0
            || !self.logical_size.is_multiple_of(self.unit_size)
        {
            return Err(LayerBackingIdentityError::InvalidGeometry);
        }
        if self.content_sha256.len() != 64
            || !self
                .content_sha256
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        {
            return Err(LayerBackingIdentityError::InvalidContentHash);
        }
        Ok(())
    }

    pub fn backing_id(&self) -> String {
        format!(
            "{}:{}:{}:{}:{}",
            self.kind.as_str(),
            self.format_version,
            self.logical_size,
            self.unit_size,
            self.content_sha256
        )
    }
}

#[derive(Debug, Error)]
pub enum BackingError {
    #[error(transparent)]
    InvalidLayer(#[from] LayerBackingIdentityError),
    #[error("backing registry is closed")]
    RegistryClosed,
    #[error("local backing is closed")]
    BackingClosed,
    #[error("adopted backing must be a read-only regular file with exact geometry")]
    InvalidAdoptedBacking,
    #[error("adopted backing conflicts with an active shared backing")]
    IdentityConflict,
    #[error("native backing range or consumer is invalid")]
    InvalidNativeRange,
    #[error("native backing has no recoverable sealed source")]
    RecoveryUnavailable,
    #[error("backing I/O failed: {0}")]
    Io(#[from] io::Error),
}

#[derive(Debug)]
pub struct LocalBacking {
    pub key: Option<LayerBackingIdentity>,
    pub backing_id: String,
    kind: LayerKind,
    logical_size: u64,
    unit_size: u64,
    native: OnceLock<NativeBackingMetadata>,
    native_state: StdMutex<native::NativeState>,
    native_io: Mutex<()>,
    registries: StdMutex<Vec<Weak<LocalBackingRegistry>>>,
    path: Option<PathBuf>,
    file: StdMutex<Option<File>>,
    closed: AtomicBool,
    owners: AtomicUsize,
    pub downloads: Arc<DataRangeResidencyRegistry>,
}

fn inode_backings() -> &'static StdMutex<HashMap<(u64, u64), Weak<LocalBacking>>> {
    static INODES: OnceLock<StdMutex<HashMap<(u64, u64), Weak<LocalBacking>>>> = OnceLock::new();
    INODES.get_or_init(Default::default)
}

impl LocalBacking {
    fn create(
        key: Option<LayerBackingIdentity>,
        backing_id: String,
        kind: LayerKind,
        logical_size: u64,
        _unit_size: u64,
        path: Option<PathBuf>,
        file: File,
    ) -> Result<Arc<Self>, BackingError> {
        let metadata = file.metadata()?;
        let unit_size = match kind {
            LayerKind::Memory => 4096,
            LayerKind::Block => 65536,
        };
        let mut inodes = inode_backings()
            .lock()
            .expect("backing inode index poisoned");
        inodes.retain(|_, backing| backing.strong_count() != 0);
        if let Some(existing) = inodes
            .get(&(metadata.dev(), metadata.ino()))
            .and_then(Weak::upgrade)
            .filter(|existing| !existing.is_closed())
        {
            if existing.kind != kind
                || existing.logical_size != logical_size
                || existing.unit_size != unit_size
            {
                return Err(BackingError::IdentityConflict);
            }
            return Ok(existing);
        }
        let backing = Arc::new(Self {
            key,
            backing_id,
            kind,
            logical_size,
            unit_size,
            path,
            native: OnceLock::new(),
            native_state: StdMutex::new(native::NativeState::default()),
            native_io: Mutex::new(()),
            registries: StdMutex::new(Vec::new()),
            file: StdMutex::new(Some(file)),
            closed: AtomicBool::new(false),
            owners: AtomicUsize::new(0),
            downloads: DataRangeResidencyRegistry::new(),
        });
        backing
            .downloads
            .backing
            .set(Arc::downgrade(&backing))
            .expect("new residency registry has no backing");
        inodes.insert((metadata.dev(), metadata.ino()), Arc::downgrade(&backing));
        Ok(backing)
    }
    pub fn logical_size(&self) -> u64 {
        self.logical_size
    }

    /// Retain physical backing ownership independently of a snapshot or of a
    /// canonical registry alias, for example for a live runtime dependency.
    pub(crate) fn lease(self: &Arc<Self>) -> LocalBackingHandle {
        LocalBackingHandle::new(Arc::clone(self), Weak::new(), None)
    }

    /// Find the already managed physical source of an immutable lineage FD.
    /// This neither creates a second backing nor revives a closed generation.
    pub(crate) fn lookup_physical(device: u64, inode: u64) -> Option<Arc<Self>> {
        inode_backings()
            .lock()
            .expect("backing inode index poisoned")
            .get(&(device, inode))
            .and_then(Weak::upgrade)
            .filter(|backing| !backing.is_closed())
    }

    pub fn kind(&self) -> LayerKind {
        self.kind
    }

    pub fn unit_size(&self) -> u64 {
        self.unit_size
    }

    pub fn physical_identity(&self) -> Result<(u64, u64), BackingError> {
        let metadata = self.try_clone_file()?.metadata()?;
        Ok((metadata.dev(), metadata.ino()))
    }

    pub fn path(&self) -> Option<&Path> {
        self.path.as_deref()
    }

    pub fn is_closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    pub fn try_clone_file(&self) -> Result<File, BackingError> {
        let file = self.file.lock().expect("backing file lock poisoned");
        file.as_ref()
            .ok_or(BackingError::BackingClosed)?
            .try_clone()
            .map_err(BackingError::Io)
    }

    /// Clone the controlled writable description used only to establish a
    /// shared UFFD-WP memory mapping in the VMM.
    ///
    /// RemoteLoad remains the sole data writer. The protocol carries this as
    /// a distinct capability beside the immutable read-only descriptor, and
    /// the CLH core never exposes an owned `File` or write/resize operation to
    /// its memory runtime.
    pub fn try_clone_memory_mapping_file(&self) -> Result<File, BackingError> {
        if self.kind != LayerKind::Memory {
            return Err(BackingError::IdentityConflict);
        }
        self.try_clone_file()
    }

    /// Reopen the live sparse backing through a read-only file description.
    /// A duplicated descriptor would retain the original `O_RDWR` access mode
    /// and must never be handed to the VMM as an immutable layer.
    pub fn try_clone_readonly_file(&self) -> Result<File, BackingError> {
        let writable = self.try_clone_file()?;
        let expected = writable.metadata()?;
        let readonly = posix_file::reopen_readonly(writable.as_raw_fd())?;
        let actual = readonly.metadata()?;
        if expected.dev() != actual.dev()
            || expected.ino() != actual.ino()
            || expected.len() != actual.len()
        {
            return Err(BackingError::IdentityConflict);
        }
        Ok(readonly)
    }

    fn identity(&self) -> Result<FileIdentity, BackingError> {
        let metadata = self.try_clone_file()?.metadata()?;
        Ok(FileIdentity::from_metadata(&metadata))
    }

    fn close(&self) {
        if self.closed.swap(true, Ordering::AcqRel) {
            return;
        }
        self.file.lock().expect("backing file lock poisoned").take();
        if let Some(path) = &self.path {
            let _ = fs::remove_file(path);
        }
    }
}

impl Drop for LocalBacking {
    fn drop(&mut self) {
        self.close();
    }
}

#[derive(Debug)]
pub struct LocalBackingHandle {
    pub backing: Arc<LocalBacking>,
    registry: Weak<LocalBackingRegistry>,
    metrics: Option<Arc<SnapdMetrics>>,
}

impl LocalBackingHandle {
    fn new(
        backing: Arc<LocalBacking>,
        registry: Weak<LocalBackingRegistry>,
        metrics: Option<Arc<SnapdMetrics>>,
    ) -> Self {
        backing.owners.fetch_add(1, Ordering::AcqRel);
        if registry.strong_count() != 0 {
            let mut registries = backing
                .registries
                .lock()
                .expect("backing registries poisoned");
            if !registries
                .iter()
                .any(|prior| Weak::ptr_eq(prior, &registry))
            {
                registries.push(registry.clone());
            }
        }
        if let Some(metrics) = &metrics {
            metrics.add_lease(LeaseKind::Backing, 1);
        }
        Self {
            backing,
            registry,
            metrics,
        }
    }
}

impl Clone for LocalBackingHandle {
    fn clone(&self) -> Self {
        Self::new(
            Arc::clone(&self.backing),
            self.registry.clone(),
            self.metrics.clone(),
        )
    }
}

impl Drop for LocalBackingHandle {
    fn drop(&mut self) {
        let previous = self.backing.owners.fetch_sub(1, Ordering::AcqRel);
        debug_assert!(previous > 0, "backing owner count underflow");
        if let Some(metrics) = &self.metrics {
            metrics.add_lease(LeaseKind::Backing, -1);
        }
        if previous == 1 {
            let registries = self
                .backing
                .registries
                .lock()
                .expect("backing registries poisoned")
                .clone();
            for registry in registries
                .into_iter()
                .filter_map(|registry| registry.upgrade())
            {
                registry.release_if_unowned(&self.backing);
            }
        }
    }
}

#[derive(Debug, Default)]
struct RegistryState {
    backings: HashMap<LayerBackingIdentity, Arc<LocalBacking>>,
    closed: bool,
}

#[derive(Debug)]
pub struct LocalBackingRegistry {
    cache_root: PathBuf,
    daemon_epoch: String,
    state: StdMutex<RegistryState>,
    metrics: Option<Arc<SnapdMetrics>>,
}

/// Disk blocks of shared immutable block-layer backings owned by this registry.
///
/// Memory memfds and the VMM's private writable layers are outside this scope.
/// The latter are separate, unlinked files owned by CLH, not cache entries.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct SharedReadonlyBackingDiskStats {
    pub allocated_bytes: u64,
    pub files: u64,
}

impl LocalBackingRegistry {
    pub fn new(
        cache_root: impl Into<PathBuf>,
        daemon_epoch: impl Into<String>,
    ) -> Result<Arc<Self>, BackingError> {
        Self::new_with_metrics(cache_root, daemon_epoch, None)
    }

    pub fn new_with_metrics(
        cache_root: impl Into<PathBuf>,
        daemon_epoch: impl Into<String>,
        metrics: Option<Arc<SnapdMetrics>>,
    ) -> Result<Arc<Self>, BackingError> {
        let daemon_epoch = daemon_epoch.into();
        if !valid_path_component(&daemon_epoch) {
            return Err(BackingError::Io(io::Error::new(
                io::ErrorKind::InvalidInput,
                "daemon_epoch must be a safe path component",
            )));
        }
        Ok(Arc::new(Self {
            cache_root: cache_root.into(),
            daemon_epoch,
            state: StdMutex::new(RegistryState::default()),
            metrics,
        }))
    }

    pub fn daemon_epoch(&self) -> &str {
        &self.daemon_epoch
    }

    pub(crate) fn cache_root(&self) -> &Path {
        &self.cache_root
    }

    /// Inspect live file descriptions, including unlinked/adopted backings.
    /// Different identities or runtime leases can refer to the same inode;
    /// count its actual allocated 512-byte blocks only once.
    pub(crate) fn stats(&self) -> Result<SharedReadonlyBackingDiskStats, BackingError> {
        let state = self.state.lock().expect("backing registry lock poisoned");
        let mut stats = SharedReadonlyBackingDiskStats::default();
        let mut seen = HashSet::new();
        for backing in state.backings.values() {
            if backing.kind != LayerKind::Block {
                continue;
            }
            let metadata = backing.try_clone_file()?.metadata()?;
            if !seen.insert((metadata.dev(), metadata.ino())) {
                continue;
            }
            let allocated = metadata.blocks().checked_mul(512).ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "allocated backing bytes overflow",
                )
            })?;
            stats.allocated_bytes =
                stats
                    .allocated_bytes
                    .checked_add(allocated)
                    .ok_or_else(|| {
                        io::Error::new(io::ErrorKind::InvalidData, "total backing bytes overflow")
                    })?;
            stats.files += 1;
        }
        Ok(stats)
    }

    pub async fn cleanup_old_block_epochs(&self) -> Result<Vec<PathBuf>, BackingError> {
        let epochs = self.cache_root.join("block/epochs");
        let entries = match fs::read_dir(&epochs) {
            Ok(entries) => entries,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(error) => return Err(error.into()),
        };
        let mut removed = Vec::new();
        for entry in entries {
            let path = entry?.path();
            if path.file_name().and_then(|name| name.to_str()) == Some(&self.daemon_epoch) {
                continue;
            }
            let metadata = fs::symlink_metadata(&path)?;
            if metadata.file_type().is_symlink() || metadata.is_file() {
                fs::remove_file(&path)?;
                removed.push(path);
            } else if metadata.is_dir() {
                fs::remove_dir_all(&path)?;
                removed.push(path);
            }
        }
        removed.sort();
        Ok(removed)
    }

    pub async fn acquire(
        self: &Arc<Self>,
        key: LayerBackingIdentity,
    ) -> Result<LocalBackingHandle, BackingError> {
        key.validate()?;
        let mut state = self.state.lock().expect("backing registry lock poisoned");
        if state.closed {
            return Err(BackingError::RegistryClosed);
        }
        let backing = match state.backings.get(&key) {
            Some(backing) => Arc::clone(backing),
            None => {
                let backing = self.create_backing(key.clone())?;
                state.backings.insert(key, Arc::clone(&backing));
                backing
            }
        };
        Ok(LocalBackingHandle::new(
            backing,
            Arc::downgrade(self),
            self.metrics.clone(),
        ))
    }

    pub async fn adopt_readonly(
        self: &Arc<Self>,
        key: LayerBackingIdentity,
        readonly: &File,
    ) -> Result<LocalBackingHandle, BackingError> {
        key.validate()?;
        let reopened = reopen_adopted_backing(readonly, &key)?;
        let candidate = FileIdentity::from_metadata(&reopened.metadata()?);
        let mut state = self.state.lock().expect("backing registry lock poisoned");
        if state.closed {
            return Err(BackingError::RegistryClosed);
        }
        let backing = match state.backings.get(&key) {
            Some(existing) => {
                if existing.identity()? != candidate {
                    return Err(BackingError::IdentityConflict);
                }
                Arc::clone(existing)
            }
            None => {
                let backing = LocalBacking::create(
                    Some(key.clone()),
                    key.backing_id(),
                    key.kind,
                    key.logical_size,
                    key.unit_size,
                    None,
                    reopened,
                )?;
                state.backings.insert(key, Arc::clone(&backing));
                backing
            }
        };
        Ok(LocalBackingHandle::new(
            backing,
            Arc::downgrade(self),
            self.metrics.clone(),
        ))
    }

    /// Seed a content-addressed promotion backing from a private frozen file,
    /// or reuse the already-active shared backing for the same immutable key.
    ///
    /// Captured native resources preserve their exact inode and dependency
    /// ledger because the VMM can still map that inode. Non-native sources may
    /// reuse an already-active canonical backing. Reattachment remains strict.
    pub async fn seed_readonly_or_acquire_existing<F: AsRawFd + ?Sized>(
        self: &Arc<Self>,
        key: LayerBackingIdentity,
        readonly: &F,
    ) -> Result<LocalBackingHandle, BackingError> {
        key.validate()?;
        let reopened = reopen_adopted_backing(readonly, &key)?;
        // A captured inode may still be mapped by the source VM. Promoting
        // metadata cannot migrate those PTEs to another same-content inode or
        // discard its dependency ledger. The global inode coordinator returns
        // that exact native resource when one already exists.
        let source = LocalBacking::create(
            Some(key.clone()),
            key.backing_id(),
            key.kind,
            key.logical_size,
            key.unit_size,
            None,
            reopened,
        )?;
        let mut state = self.state.lock().expect("backing registry lock poisoned");
        if state.closed {
            return Err(BackingError::RegistryClosed);
        }
        let backing = match state.backings.get(&key) {
            Some(_) if source.native_metadata().is_some() => Arc::clone(&source),
            Some(existing) => Arc::clone(existing),
            None => {
                let backing = source;
                state.backings.insert(key, Arc::clone(&backing));
                backing
            }
        };
        Ok(LocalBackingHandle::new(
            backing,
            Arc::downgrade(self),
            self.metrics.clone(),
        ))
    }

    pub async fn close(&self) {
        let backings = {
            let mut state = self.state.lock().expect("backing registry lock poisoned");
            if state.closed {
                return;
            }
            state.closed = true;
            state
                .backings
                .drain()
                .map(|(_, backing)| backing)
                .collect::<Vec<_>>()
        };
        for backing in backings {
            backing.close();
        }
    }

    fn release_if_unowned(&self, backing: &Arc<LocalBacking>) {
        let removed = {
            let mut state = self.state.lock().expect("backing registry lock poisoned");
            if backing.owners.load(Ordering::Acquire) != 0 {
                return;
            }
            let identities = state
                .backings
                .iter()
                .filter(|(_, value)| Arc::ptr_eq(value, backing))
                .map(|(key, _)| key.clone())
                .collect::<Vec<_>>();
            let mut removed = None;
            for identity in identities {
                removed = state.backings.remove(&identity);
            }
            removed
        };
        if let Some(backing) = removed {
            backing.close();
        }
    }

    fn create_backing(&self, key: LayerBackingIdentity) -> Result<Arc<LocalBacking>, BackingError> {
        let backing_id = key.backing_id();
        let (file, path) = match key.kind {
            LayerKind::Memory => (create_memory_file(&key)?, None),
            LayerKind::Block => {
                let directory = self
                    .cache_root
                    .join("block/epochs")
                    .join(&self.daemon_epoch)
                    .join(key.format_version.to_string());
                posix_file::private_directory(&directory)?;
                let path = directory.join(format!(
                    "{}.{}.raw",
                    key.content_sha256,
                    Uuid::new_v4().simple()
                ));
                let file = posix_file::sparse_file(&path, key.logical_size)?;
                (file, Some(path))
            }
        };
        LocalBacking::create(
            Some(key.clone()),
            backing_id,
            key.kind,
            key.logical_size,
            key.unit_size,
            path,
            file,
        )
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct FileIdentity {
    device: u64,
    inode: u64,
    size: u64,
}

impl FileIdentity {
    fn from_metadata(metadata: &fs::Metadata) -> Self {
        Self {
            device: metadata.dev(),
            inode: metadata.ino(),
            size: metadata.len(),
        }
    }
}

fn valid_path_component(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

fn create_memory_file(key: &LayerBackingIdentity) -> Result<File, BackingError> {
    let name = std::ffi::CString::new(format!("rock-snapd-{}", &key.content_sha256[..16]))
        .expect("hash-derived memfd name cannot contain NUL");
    posix_file::memory_file(&name, key.logical_size).map_err(Into::into)
}

fn reopen_adopted_backing<F: AsRawFd + ?Sized>(
    readonly: &F,
    key: &LayerBackingIdentity,
) -> Result<File, BackingError> {
    let fd = readonly.as_raw_fd();
    // SAFETY: F_GETFL only inspects the valid borrowed descriptor.
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || flags & libc::O_ACCMODE != libc::O_RDONLY {
        return Err(BackingError::InvalidAdoptedBacking);
    }
    let path = format!("/proc/self/fd/{fd}");
    let original = fs::metadata(&path)?;
    if !original.is_file() || original.len() != key.logical_size {
        return Err(BackingError::InvalidAdoptedBacking);
    }
    let reopened =
        posix_file::reopen_readwrite(fd).map_err(|_| BackingError::InvalidAdoptedBacking)?;
    if FileIdentity::from_metadata(&reopened.metadata()?) != FileIdentity::from_metadata(&original)
    {
        return Err(BackingError::InvalidAdoptedBacking);
    }
    Ok(reopened)
}

#[cfg(test)]
mod disk_stats_tests {
    use super::*;
    use std::os::unix::fs::FileExt;

    #[tokio::test]
    async fn shared_block_stats_count_unique_open_inodes_and_exclude_memory() {
        let root = std::env::temp_dir().join(format!("rock-backing-stats-{}", Uuid::new_v4()));
        let registry = LocalBackingRegistry::new(&root, "stats-epoch").unwrap();
        let key = LayerBackingIdentity {
            kind: LayerKind::Block,
            format_version: 1,
            logical_size: 64 * 1024 * 1024,
            unit_size: 65536,
            content_sha256: "a".repeat(64),
        };
        let block = registry.acquire(key.clone()).await.unwrap();
        let duplicate_lease = registry.acquire(key.clone()).await.unwrap();
        let file = block.backing.try_clone_file().unwrap();
        file.write_all_at(&[0x5a; 4096], 0).unwrap();
        file.sync_data().unwrap();
        let allocated = file.metadata().unwrap().blocks() * 512;
        assert!(allocated > 0 && allocated < key.logical_size);

        let memory = registry
            .acquire(LayerBackingIdentity {
                kind: LayerKind::Memory,
                unit_size: 4096,
                ..key.clone()
            })
            .await
            .unwrap();
        memory
            .backing
            .try_clone_file()
            .unwrap()
            .write_all_at(&[0x7b; 4096], 0)
            .unwrap();

        // A second logical identity can adopt this same immutable inode.
        let readonly = block.backing.try_clone_readonly_file().unwrap();
        let adopted = registry
            .adopt_readonly(
                LayerBackingIdentity {
                    content_sha256: "b".repeat(64),
                    ..key
                },
                &readonly,
            )
            .await
            .unwrap();
        // There is no directory entry now; only live FDs reveal the space.
        fs::remove_file(block.backing.path().unwrap()).unwrap();
        assert_eq!(
            registry.stats().unwrap(),
            SharedReadonlyBackingDiskStats {
                allocated_bytes: allocated,
                files: 1,
            }
        );
        drop((block, duplicate_lease, adopted, readonly, file));
        assert_eq!(
            registry.stats().unwrap(),
            SharedReadonlyBackingDiskStats::default()
        );
        drop(memory);
        registry.close().await;
        let _ = fs::remove_dir_all(root);
    }
}
