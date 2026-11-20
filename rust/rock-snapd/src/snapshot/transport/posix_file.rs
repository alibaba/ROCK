//! POSIX file transport and node-wide cache policy.
//!
//! Physical allocation and range reads come first. The inline cache modules
//! manage rebuildable physical bytes, sealed objects, pins and disk quota.
//! Native backing ownership, dependencies and residency remain in LocalBacking;
//! active raw backings are not admitted to the evictable cache.

use std::ffi::CStr;
use std::fs::{self, File, OpenOptions};
use std::io;
use std::os::fd::{FromRawFd, RawFd};
use std::os::unix::fs::{FileExt, OpenOptionsExt, PermissionsExt};
use std::path::Path;

pub(crate) fn private_directory(path: &Path) -> io::Result<()> {
    fs::create_dir_all(path)?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))
}

pub(crate) fn sparse_file(path: &Path, length: u64) -> io::Result<File> {
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_CLOEXEC)
        .open(path)?;
    file.set_len(length)?;
    Ok(file)
}

pub(crate) fn memory_file(name: &CStr, length: u64) -> io::Result<File> {
    // SAFETY: name is live and NUL-terminated. A successful call returns a new
    // uniquely owned FD, immediately transferred to File.
    let fd =
        unsafe { libc::memfd_create(name.as_ptr(), libc::MFD_CLOEXEC | libc::MFD_ALLOW_SEALING) };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: memfd_create returned a fresh owned descriptor.
    let file = unsafe { File::from_raw_fd(fd) };
    file.set_len(length)?;
    Ok(file)
}

pub(crate) fn reopen_readonly(fd: RawFd) -> io::Result<File> {
    File::open(format!("/proc/self/fd/{fd}"))
}

pub(crate) fn reopen_readwrite(fd: RawFd) -> io::Result<File> {
    OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(libc::O_CLOEXEC)
        .open(format!("/proc/self/fd/{fd}"))
}

/// Read physical bytes from a caller-owned file. The caller supplies its
/// backing/read lease and validates DATA coverage and residency. This primitive
/// does not admit the file to LocalByteCache, reserve LRU quota, infer holes, or
/// fetch an origin; native backing and cache policy stay separate.
pub(crate) fn read_file_range(file: &File, offset: u64, length: usize) -> io::Result<Vec<u8>> {
    let length_u64 = u64::try_from(length)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "file range is too large"))?;
    offset
        .checked_add(length_u64)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "file range overflows"))?;
    let mut bytes = vec![0; length];
    file.read_exact_at(&mut bytes, offset)?;
    Ok(bytes)
}

pub(crate) use bytes::disk::DiskCacheStats;
#[cfg(test)]
pub(crate) use bytes::{hex_digest, metadata_name};
pub(crate) use bytes::{CacheError, LocalByteCache, DEFAULT_MAX_BYTES};
pub(crate) use cache::temporary_staged_object as create_temporary_staged_object;
pub(crate) use cache::LocalFileCache;
#[cfg(test)]
pub(crate) use cache::SealValidator;

mod bytes {
    //! Format-independent local byte cache transport.
    //!
    //! Owns the private directories, atomic byte publication, file pins and disk LRU.
    //! Format adapters authenticate and decode their own layouts above this layer.

    use std::ffi::CString;
    use std::fs::File;
    use std::io;
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::{FileExt, PermissionsExt};
    use std::path::{Component, Path, PathBuf};
    use std::sync::{Arc, Mutex, MutexGuard};

    use crate::snapshot::transport::{ObjectStoreError, PublishFailureKind};
    use sha2::{Digest, Sha256};
    use thiserror::Error;

    pub(crate) mod disk {
        //! Process-coordinated, file-system backed LRU for disposable immutable bytes.

        use std::collections::HashMap;
        use std::ffi::{CStr, CString};
        use std::fs::File;
        use std::io;
        use std::os::fd::{AsRawFd, FromRawFd, IntoRawFd, RawFd};
        use std::os::unix::fs::MetadataExt;
        use std::sync::atomic::{AtomicBool, Ordering};
        use std::sync::{Arc, Mutex, OnceLock, Weak};
        use std::time::{SystemTime, UNIX_EPOCH};

        use super::{open_file, CacheError};

        pub(crate) const DEFAULT_MAX_BYTES: u64 = 100 * 1024 * 1024 * 1024;

        #[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
        pub(crate) struct DiskCacheStats {
            pub max_bytes: u64,
            pub used_bytes: u64,
            pub pinned_bytes: u64,
            pub staging_bytes: u64,
            pub entries: u64,
            pub evictions: u64,
            pub bypasses: u64,
        }

        #[derive(Clone, Debug, Eq, Hash, PartialEq)]
        struct Key {
            directory: (u64, u64),
            name: String,
        }

        impl Key {
            fn new(directory: &File, name: &str) -> io::Result<Self> {
                let metadata = directory.metadata()?;
                Ok(Self {
                    directory: (metadata.dev(), metadata.ino()),
                    name: name.into(),
                })
            }
        }

        #[derive(Debug)]
        struct Entry {
            directory: Arc<File>,
            bytes: u64,
            touched: i128,
        }

        #[derive(Debug)]
        struct Pending {
            key: Key,
            file: Option<Weak<File>>,
            orphaned: bool,
            bytes: u64,
        }

        #[derive(Debug)]
        struct Staging {
            fd: RawFd,
            identity: (u64, u64),
        }

        #[derive(Debug, Default)]
        struct State {
            entries: HashMap<Key, Entry>,
            used: u64,
            reserved: u64,
            protected: HashMap<Key, usize>,
            held_protected: HashMap<Key, Arc<File>>,
            pins: Vec<Weak<File>>,
            pending: HashMap<u64, Pending>,
            next_reservation: u64,
            staging: HashMap<RawFd, Staging>,
            evictions: u64,
            bypasses: u64,
        }

        #[derive(Debug)]
        pub(crate) struct DiskLru {
            // The exclusive root lock prevents a second process from independently
            // admitting bytes against this budget. Same-process users share this Arc.
            _owner: File,
            max_bytes: u64,
            allocation_unit: u64,
            state: Mutex<State>,
        }

        type Coordinators = Mutex<HashMap<(u64, u64), Weak<DiskLru>>>;
        static COORDINATORS: OnceLock<Coordinators> = OnceLock::new();

        impl DiskLru {
            pub(crate) fn new(root: Arc<File>, max_bytes: u64) -> Result<Arc<Self>, CacheError> {
                let metadata = root.metadata()?;
                let identity = (metadata.dev(), metadata.ino());
                let mut coordinators = super::lock(COORDINATORS.get_or_init(Default::default))?;
                coordinators.retain(|_, coordinator| coordinator.strong_count() != 0);
                if let Some(existing) = coordinators.get(&identity).and_then(Weak::upgrade) {
                    if existing.max_bytes != max_bytes {
                        return Err(CacheError::Invalid(
                            "one cache root cannot have conflicting byte limits".into(),
                        ));
                    }
                    return Ok(existing);
                }
                let owner = open_file(&root, ".lru.lock", libc::O_RDWR | libc::O_CREAT)?;
                if !try_lock(&owner, libc::LOCK_EX)? {
                    return Err(CacheError::Capacity(
                        "cache root is already owned by another daemon".into(),
                    ));
                }
                let mut state = State::default();
                scan_root(&root, &mut state)?;
                let cache = Arc::new(Self {
                    _owner: owner,
                    max_bytes,
                    allocation_unit: allocation_unit(&root)?,
                    state: Mutex::new(state),
                });
                {
                    let mut state = super::lock(&cache.state)?;
                    // A smaller limit after restart may leave pinned files above the
                    // limit. Keep them, deny new writes, and reclaim when they close.
                    cache.make_room(&mut state, 0)?;
                }
                coordinators.insert(identity, Arc::downgrade(&cache));
                Ok(cache)
            }

            pub(crate) fn bytes_for_regions(
                &self,
                regions: &[(u64, u64)],
            ) -> Result<u64, CacheError> {
                let mut pages = Vec::with_capacity(regions.len());
                for &(offset, length) in regions {
                    if length == 0 {
                        continue;
                    }
                    let end = offset.checked_add(length).ok_or_else(|| {
                        CacheError::Invalid("cache write region overflows".into())
                    })?;
                    let first = offset / self.allocation_unit;
                    let last = end.div_ceil(self.allocation_unit);
                    pages.push((first, last));
                }
                pages.sort_unstable();
                let mut count = 0_u64;
                let mut covered = 0_u64;
                for (first, last) in pages {
                    count = count.saturating_add(last.saturating_sub(first.max(covered)));
                    covered = covered.max(last);
                }
                count.checked_mul(self.allocation_unit).ok_or_else(|| {
                    CacheError::Invalid("cache allocation reservation overflows".into())
                })
            }

            pub(crate) fn reserve(
                self: &Arc<Self>,
                directory: &Arc<File>,
                name: &str,
                growth: u64,
                replace: bool,
            ) -> Result<Reservation, CacheError> {
                let key = Key::new(directory, name)?;
                let mut state = super::lock(&self.state)?;
                reap_orphans(&mut state);
                if state.pending.values().any(|pending| pending.key == key) {
                    state.bypasses = state.bypasses.saturating_add(1);
                    return capacity("cache object already has an in-flight writer");
                }
                refresh_entry(&mut state, directory, &key)?;
                // The state lock excludes concurrent eviction while acquiring a
                // replacement lock. Register protection only after fallible opens
                // and locking succeed, so an I/O error cannot leave a stale pin.
                let exclusive = if replace {
                    match open_file(directory, name, libc::O_RDONLY) {
                        Ok(file) if try_lock(&file, libc::LOCK_EX)? => Some(file),
                        Ok(_) => {
                            state.bypasses = state.bypasses.saturating_add(1);
                            return capacity("cache replacement is pinned");
                        }
                        Err(error) if error.kind() == io::ErrorKind::NotFound => None,
                        Err(error) => return Err(error.into()),
                    }
                } else {
                    None
                };
                // Protect before choosing victims, including an existing target.
                *state.protected.entry(key.clone()).or_default() += 1;
                let room = match self.make_room(&mut state, growth) {
                    Ok(room) => room,
                    Err(error) => {
                        unprotect(&mut state, &key);
                        return Err(error);
                    }
                };
                if !room {
                    unprotect(&mut state, &key);
                    state.bypasses = state.bypasses.saturating_add(1);
                    return capacity("local cache byte budget is full or pinned");
                }
                state.reserved = state.reserved.saturating_add(growth);
                state.next_reservation = state.next_reservation.wrapping_add(1);
                let id = state.next_reservation;
                state.pending.insert(
                    id,
                    Pending {
                        key: key.clone(),
                        file: None,
                        orphaned: false,
                        bytes: growth,
                    },
                );
                Ok(Reservation {
                    lease: Arc::new(ReservationLease {
                        cache: Arc::clone(self),
                        key,
                        directory: Arc::clone(directory),
                        bytes: growth,
                        id,
                        exclusive: Mutex::new(exclusive),
                        finished: AtomicBool::new(false),
                    }),
                })
            }

            pub(crate) fn open_pinned(
                &self,
                directory: &Arc<File>,
                name: &str,
                flags: libc::c_int,
            ) -> Result<Arc<File>, CacheError> {
                self.open_pinned_with_metadata(directory, name, flags)
                    .map(|(file, _)| file)
            }

            /// Observe the inode before the LRU touch changes its timestamps. Cache
            /// integrity memoization may distinguish this touch from an external edit.
            pub(crate) fn open_pinned_with_metadata(
                &self,
                directory: &Arc<File>,
                name: &str,
                flags: libc::c_int,
            ) -> Result<(Arc<File>, std::fs::Metadata), CacheError> {
                let mut state = super::lock(&self.state)?;
                let file = Arc::new(open_file(directory, name, flags)?);
                if !try_lock(&file, libc::LOCK_SH)? {
                    return capacity("cache file is being replaced or evicted");
                }
                let observed = file.metadata()?;
                touch(&file);
                let key = Key::new(directory, name)?;
                refresh_entry(&mut state, directory, &key)?;
                state.pins.retain(|pin| pin.strong_count() != 0);
                state.pins.push(Arc::downgrade(&file));
                Ok((file, observed))
            }

            pub(crate) fn protect(
                self: &Arc<Self>,
                entries: &[(Arc<File>, String)],
            ) -> Result<ProtectedEntries, CacheError> {
                let mut state = super::lock(&self.state)?;
                let mut keys = Vec::with_capacity(entries.len());
                let acquired = (|| {
                    for (directory, name) in entries {
                        let key = Key::new(directory, name)?;
                        if keys.contains(&key) {
                            continue;
                        }
                        let held = match open_file(directory, name, libc::O_RDONLY) {
                            Ok(file) => {
                                if !try_lock(&file, libc::LOCK_SH)? {
                                    state.bypasses = state.bypasses.saturating_add(1);
                                    return capacity("cache graph metadata is being replaced");
                                }
                                Some(Arc::new(file))
                            }
                            Err(error) if error.kind() == io::ErrorKind::NotFound => None,
                            Err(error) => return Err(error.into()),
                        };
                        *state.protected.entry(key.clone()).or_default() += 1;
                        if let Some(file) = held {
                            state.held_protected.insert(key.clone(), file);
                        }
                        keys.push(key);
                    }
                    Ok::<_, CacheError>(())
                })();
                if let Err(error) = acquired {
                    for key in &keys {
                        unprotect(&mut state, key);
                    }
                    return Err(error);
                }
                Ok(ProtectedEntries {
                    cache: Arc::clone(self),
                    keys,
                })
            }

            #[cfg(test)]
            pub(crate) fn record_staging(&self, file: &File) -> Result<(), CacheError> {
                let metadata = file.metadata()?;
                super::lock(&self.state)?.staging.insert(
                    file.as_raw_fd(),
                    Staging {
                        fd: file.as_raw_fd(),
                        identity: (metadata.dev(), metadata.ino()),
                    },
                );
                Ok(())
            }

            pub(crate) fn remove(
                &self,
                directory: &Arc<File>,
                name: &str,
            ) -> Result<(), CacheError> {
                let mut state = super::lock(&self.state)?;
                let key = Key::new(directory, name)?;
                if state.protected.contains_key(&key) {
                    return Ok(());
                }
                if let Some(entry) = state.entries.get(&key) {
                    let file = open_file(directory, name, libc::O_RDONLY)?;
                    if !try_lock(&file, libc::LOCK_EX)? {
                        return Ok(());
                    }
                    unlink_checked(directory, name, &file)?;
                    let bytes = entry.bytes;
                    state.entries.remove(&key);
                    state.used = state.used.saturating_sub(bytes);
                }
                Ok(())
            }

            pub(crate) fn entry_names(
                &self,
                directory: &File,
                suffix: &str,
            ) -> Result<Vec<String>, CacheError> {
                let metadata = directory.metadata()?;
                let identity = (metadata.dev(), metadata.ino());
                Ok(super::lock(&self.state)?
                    .entries
                    .keys()
                    .filter(|key| key.directory == identity && key.name.ends_with(suffix))
                    .map(|key| key.name.clone())
                    .collect())
            }

            pub(crate) fn stats(&self) -> Result<DiskCacheStats, CacheError> {
                let mut state = super::lock(&self.state)?;
                reap_orphans(&mut state);
                // Include anonymous pending seal files by inode, without double
                // counting a file after its atomic rename into the named cache.
                let mut files = HashMap::<(u64, u64), (u64, bool)>::new();
                let mut existing = 0_u64;
                for (key, entry) in &state.entries {
                    let Ok(file) = open_file(&entry.directory, &key.name, libc::O_RDONLY) else {
                        continue;
                    };
                    let metadata = file.metadata()?;
                    let pinned =
                        state.protected.contains_key(key) || !try_lock(&file, libc::LOCK_EX)?;
                    files.insert(
                        (metadata.dev(), metadata.ino()),
                        (allocated(&metadata), pinned),
                    );
                    existing += 1;
                }
                state.pins.retain(|pin| pin.strong_count() != 0);
                for file in state.pins.iter().filter_map(Weak::upgrade).chain(
                    state
                        .pending
                        .values()
                        .filter_map(|pending| pending.file.as_ref()?.upgrade()),
                ) {
                    let metadata = file.metadata()?;
                    files.insert(
                        (metadata.dev(), metadata.ino()),
                        (allocated(&metadata), true),
                    );
                }
                let mut staging_bytes = 0_u64;
                state.staging.retain(|_, staging| {
                    let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
                    // SAFETY: fstat only reads the FD table and writes the output. An
                    // FD reused for another inode is rejected by the identity check.
                    if unsafe { libc::fstat(staging.fd, stat.as_mut_ptr()) } != 0 {
                        return false;
                    }
                    // SAFETY: successful fstat initialized the value.
                    let stat = unsafe { stat.assume_init() };
                    if stat_identity(&stat) != staging.identity {
                        return false;
                    }
                    staging_bytes =
                        staging_bytes.saturating_add((stat.st_blocks as u64).saturating_mul(512));
                    true
                });
                Ok(DiskCacheStats {
                    max_bytes: self.max_bytes,
                    used_bytes: files.values().map(|(bytes, _)| bytes).sum(),
                    pinned_bytes: files
                        .values()
                        .filter(|(_, pinned)| *pinned)
                        .map(|(bytes, _)| bytes)
                        .sum(),
                    staging_bytes,
                    entries: existing,
                    evictions: state.evictions,
                    bypasses: state.bypasses,
                })
            }

            fn make_room(&self, state: &mut State, growth: u64) -> Result<bool, CacheError> {
                reap_orphans(state);
                if growth > self.max_bytes {
                    return Ok(false);
                }
                if state
                    .used
                    .saturating_add(state.reserved)
                    .saturating_add(growth)
                    <= self.max_bytes
                {
                    return Ok(true);
                }
                let mut candidates = state
                    .entries
                    .iter()
                    .filter(|(key, _)| !state.protected.contains_key(*key))
                    .map(|(key, entry)| (key.clone(), entry.touched))
                    .collect::<Vec<_>>();
                candidates.sort_unstable_by_key(|(_, touched)| *touched);
                for (key, _) in candidates {
                    if state
                        .used
                        .saturating_add(state.reserved)
                        .saturating_add(growth)
                        <= self.max_bytes
                    {
                        return Ok(true);
                    }
                    let Some(entry) = state.entries.get(&key) else {
                        continue;
                    };
                    let file = match open_file(&entry.directory, &key.name, libc::O_RDONLY) {
                        Ok(file) => file,
                        Err(error) if error.kind() == io::ErrorKind::NotFound => {
                            let removed = state.entries.remove(&key).expect("entry exists");
                            state.used = state.used.saturating_sub(removed.bytes);
                            continue;
                        }
                        Err(_) => continue,
                    };
                    if !try_lock(&file, libc::LOCK_EX)? {
                        continue;
                    }
                    unlink_checked(&entry.directory, &key.name, &file)?;
                    let removed = state.entries.remove(&key).expect("entry exists");
                    state.used = state.used.saturating_sub(removed.bytes);
                    state.evictions = state.evictions.saturating_add(1);
                }
                Ok(state
                    .used
                    .saturating_add(state.reserved)
                    .saturating_add(growth)
                    <= self.max_bytes)
            }
        }

        #[derive(Debug)]
        pub(crate) struct Reservation {
            lease: Arc<ReservationLease>,
        }

        #[derive(Debug)]
        struct ReservationLease {
            cache: Arc<DiskLru>,
            key: Key,
            directory: Arc<File>,
            bytes: u64,
            id: u64,
            exclusive: Mutex<Option<File>>,
            finished: AtomicBool,
        }

        // The file is dropped before the lease: cancelled spawn_blocking writes keep
        // their complete reservation until their last writable FD has closed.
        #[cfg(test)]
        pub(crate) struct PendingWrite {
            file: Arc<File>,
            _lease: Arc<ReservationLease>,
        }

        #[cfg(test)]
        impl crate::snapshot::transport::MutableRangeWriter for PendingWrite {
            fn write_all_at(&self, payload: &[u8], offset: u64) -> io::Result<()> {
                std::os::unix::fs::FileExt::write_all_at(&*self.file, payload, offset)
            }
        }

        impl Reservation {
            pub(crate) fn published(&self) -> bool {
                self.lease.finished.load(Ordering::Acquire)
            }

            /// Publish a generated object without copying its reserved temporary inode.
            /// Hold the accounting lock across rename and pin acquisition so eviction
            /// can never observe an unaccounted or unpinned published object.
            pub(crate) fn publish_as(
                &self,
                source: &str,
                name: &str,
            ) -> Result<Arc<File>, CacheError> {
                let lease = &self.lease;
                let mut state = super::lock(&lease.cache.state)?;
                if lease.finished.load(Ordering::Acquire) {
                    return Err(CacheError::Invalid(
                        "staged reservation is already published".into(),
                    ));
                }
                let target = Key::new(&lease.directory, name)?;
                if state
                    .pending
                    .iter()
                    .any(|(id, pending)| *id != lease.id && pending.key == target)
                {
                    return capacity("generated object has an in-flight cache writer");
                }
                let replacement = match open_file(&lease.directory, name, libc::O_RDONLY) {
                    Ok(file) => {
                        if !try_lock(&file, libc::LOCK_EX)? {
                            return capacity("generated object target is pinned");
                        }
                        Some(file)
                    }
                    Err(error) if error.kind() == io::ErrorKind::NotFound => None,
                    Err(error) => return Err(error.into()),
                };
                let readonly = Arc::new(open_file(&lease.directory, source, libc::O_RDONLY)?);
                if !try_lock(&readonly, libc::LOCK_SH)? {
                    return capacity("generated object cannot be pinned");
                }
                let metadata = readonly.metadata()?;
                let original = state
                    .pending
                    .get(&lease.id)
                    .and_then(|pending| pending.file.as_ref())
                    .and_then(Weak::upgrade)
                    .ok_or_else(|| {
                        CacheError::Invalid("generated reservation lost its source".into())
                    })?;
                let original_metadata = original.metadata()?;
                if (metadata.dev(), metadata.ino())
                    != (original_metadata.dev(), original_metadata.ino())
                {
                    return Err(CacheError::IdentityConflict(
                        "generated temporary inode changed".into(),
                    ));
                }
                let bytes = allocated(&metadata);
                super::rename_file(&lease.directory, source, name)?;
                // From this point the inode belongs to the cache even if a subsequent
                // fsync fails. Keep it charged and allow a later attempt to validate it.
                if let Some(temporary) = state.entries.remove(&lease.key) {
                    state.used = state.used.saturating_sub(temporary.bytes);
                }
                let previous = state.entries.insert(
                    target,
                    Entry {
                        directory: Arc::clone(&lease.directory),
                        bytes,
                        touched: modified(&metadata),
                    },
                );
                state.used = state
                    .used
                    .saturating_sub(previous.map_or(0, |entry| entry.bytes))
                    .saturating_add(bytes);
                state.reserved = state.reserved.saturating_sub(lease.bytes);
                state.pending.remove(&lease.id);
                unprotect(&mut state, &lease.key);
                lease.finished.store(true, Ordering::Release);
                state.pins.retain(|pin| pin.strong_count() != 0);
                state.pins.push(Arc::downgrade(&readonly));
                drop(replacement);
                if state.used.saturating_add(state.reserved) > lease.cache.max_bytes {
                    return capacity("generated object exceeded its allocation reservation");
                }
                drop(state);
                sync_directory(&lease.directory)?;
                Ok(readonly)
            }

            pub(crate) fn attach(&self, file: &Arc<File>) -> Result<(), CacheError> {
                if let Some(pending) = super::lock(&self.lease.cache.state)?
                    .pending
                    .get_mut(&self.lease.id)
                {
                    pending.file = Some(Arc::downgrade(file));
                }
                Ok(())
            }

            #[cfg(test)]
            pub(crate) fn pin_write(&self, file: Arc<File>) -> PendingWrite {
                PendingWrite {
                    file,
                    _lease: Arc::clone(&self.lease),
                }
            }

            pub(crate) fn finish(&self) -> Result<(), CacheError> {
                let lease = &self.lease;
                super::lock(&lease.exclusive)?.take();
                let mut state = super::lock(&lease.cache.state)?;
                refresh_entry(&mut state, &lease.directory, &lease.key)?;
                let reserved_after = state.reserved.saturating_sub(lease.bytes);
                if state.used.saturating_add(reserved_after) > lease.cache.max_bytes {
                    // Keep unexpected allocation visible and charged. A reader may
                    // hold this inode, so never unlink it and pretend it was freed.
                    // Subsequent admission can reclaim it only with the exclusive lock.
                    state.bypasses = state.bypasses.saturating_add(1);
                    return capacity("filesystem allocation exceeded cache reservation");
                }
                state.reserved = reserved_after;
                state.pending.remove(&lease.id);
                unprotect(&mut state, &lease.key);
                lease.finished.store(true, Ordering::Release);
                if state.protected.contains_key(&lease.key) {
                    let file = Arc::new(open_file(
                        &lease.directory,
                        &lease.key.name,
                        libc::O_RDONLY,
                    )?);
                    if !try_lock(&file, libc::LOCK_SH)? {
                        return capacity("published graph metadata could not be pinned");
                    }
                    state.held_protected.insert(lease.key.clone(), file);
                }
                Ok(())
            }
        }

        pub(crate) fn sync_directory(directory: &File) -> io::Result<()> {
            #[cfg(test)]
            if FAIL_DIRECTORY_SYNC.with(|fail| fail.replace(false)) {
                return Err(io::Error::from_raw_os_error(libc::EIO));
            }
            directory.sync_all()
        }

        #[cfg(test)]
        thread_local! {
            static FAIL_DIRECTORY_SYNC: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
        }

        #[cfg(test)]
        pub(crate) fn fail_next_directory_sync() {
            FAIL_DIRECTORY_SYNC.with(|fail| fail.set(true));
        }

        impl Drop for ReservationLease {
            fn drop(&mut self) {
                if !self.finished.load(Ordering::Acquire) {
                    if let Ok(mut state) = self.cache.state.lock() {
                        // An interrupted in-place partial write may have allocated
                        // blocks. Reconcile it before releasing its growth budget.
                        let _ = refresh_entry(&mut state, &self.directory, &self.key);
                        let keep = state.pending.get(&self.id).is_some_and(|pending| {
                            pending
                                .file
                                .as_ref()
                                .is_some_and(|file| file.strong_count() != 0)
                        });
                        if keep {
                            // TemporaryFile and detached blocking I/O can outlive the
                            // cancelled future. Retain its full reservation until the
                            // last tracked FD closes, including after unlink.
                            state
                                .pending
                                .get_mut(&self.id)
                                .expect("pending exists")
                                .orphaned = true;
                        } else {
                            state.reserved = state.reserved.saturating_sub(self.bytes);
                            state.pending.remove(&self.id);
                        }
                        unprotect(&mut state, &self.key);
                    }
                }
            }
        }

        fn reap_orphans(state: &mut State) {
            state.pending.retain(|_, pending| {
                if pending.orphaned
                    && pending
                        .file
                        .as_ref()
                        .is_none_or(|file| file.strong_count() == 0)
                {
                    state.reserved = state.reserved.saturating_sub(pending.bytes);
                    false
                } else {
                    true
                }
            });
        }

        pub(crate) struct ProtectedEntries {
            cache: Arc<DiskLru>,
            keys: Vec<Key>,
        }

        impl Drop for ProtectedEntries {
            fn drop(&mut self) {
                if let Ok(mut state) = self.cache.state.lock() {
                    for key in &self.keys {
                        unprotect(&mut state, key);
                    }
                }
            }
        }

        fn unprotect(state: &mut State, key: &Key) {
            if let Some(count) = state.protected.get_mut(key) {
                *count -= 1;
                if *count == 0 {
                    state.protected.remove(key);
                    state.held_protected.remove(key);
                }
            }
        }

        fn refresh_entry(
            state: &mut State,
            directory: &Arc<File>,
            key: &Key,
        ) -> Result<(), CacheError> {
            let file = match open_file(directory, &key.name, libc::O_RDONLY) {
                Ok(file) => file,
                Err(error) if error.kind() == io::ErrorKind::NotFound => {
                    if let Some(removed) = state.entries.remove(key) {
                        state.used = state.used.saturating_sub(removed.bytes);
                    }
                    return Ok(());
                }
                Err(error) => return Err(error.into()),
            };
            let metadata = file.metadata()?;
            let bytes = allocated(&metadata);
            let previous = state.entries.insert(
                key.clone(),
                Entry {
                    directory: Arc::clone(directory),
                    bytes,
                    touched: modified(&metadata),
                },
            );
            state.used = state
                .used
                .saturating_sub(previous.map_or(0, |entry| entry.bytes))
                .saturating_add(bytes);
            Ok(())
        }

        fn scan_root(root: &File, state: &mut State) -> Result<(), CacheError> {
            for domain in names(root)? {
                if !hex_name(&domain, 64) {
                    continue;
                }
                let Ok(domain) = open_directory(root, &domain) else {
                    continue;
                };
                for child in ["objects", "metadata"] {
                    let Ok(directory) = open_directory(&domain, child) else {
                        continue;
                    };
                    let directory = Arc::new(directory);
                    for name in names(&directory)? {
                        let Some((stem, suffix)) = name.rsplit_once('.') else {
                            continue;
                        };
                        let persistent = hex_name(stem, 64)
                            && matches!(suffix, "commit" | "partial" | "descriptor" | "metadata");
                        let temporary = hex_name(stem, 32) && matches!(suffix, "tmp" | "scratch");
                        if !persistent && !temporary {
                            continue;
                        }
                        let Ok(file) = open_file(&directory, &name, libc::O_RDONLY) else {
                            continue;
                        };
                        if temporary && try_lock(&file, libc::LOCK_EX)? {
                            unlink_checked(&directory, &name, &file)?;
                            continue;
                        }
                        let key = Key::new(&directory, &name)?;
                        refresh_entry(state, &directory, &key)?;
                    }
                }
            }
            Ok(())
        }

        fn hex_name(value: &str, length: usize) -> bool {
            value.len() == length
                && value
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        }

        fn open_directory(parent: &File, name: &str) -> io::Result<File> {
            let name = CString::new(name)?;
            // SAFETY: one checked directory entry, NOFOLLOW refuses symlink traversal.
            let fd = unsafe {
                libc::openat(
                    parent.as_raw_fd(),
                    name.as_ptr(),
                    libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                )
            };
            if fd < 0 {
                Err(io::Error::last_os_error())
            } else {
                Ok(unsafe { File::from_raw_fd(fd) })
            }
        }

        fn names(directory: &File) -> io::Result<Vec<String>> {
            // A fresh open description avoids sharing directory offsets with callers.
            let fd = open_directory(directory, ".")?.into_raw_fd();
            let stream = unsafe { libc::fdopendir(fd) };
            if stream.is_null() {
                unsafe {
                    libc::close(fd);
                }
                return Err(io::Error::last_os_error());
            }
            let mut result = Vec::new();
            loop {
                let entry = unsafe { libc::readdir(stream) };
                if entry.is_null() {
                    break;
                }
                let name = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) };
                if let Ok(name) = name.to_str() {
                    result.push(name.into());
                }
            }
            unsafe {
                libc::closedir(stream);
            }
            Ok(result)
        }

        #[cfg(test)]
        thread_local! {
            static LOCK_EIO_AFTER: std::cell::Cell<Option<usize>> = const { std::cell::Cell::new(None) };
        }

        fn try_lock(file: &File, operation: libc::c_int) -> io::Result<bool> {
            #[cfg(test)]
            if LOCK_EIO_AFTER.with(|remaining| match remaining.get() {
                Some(0) => {
                    remaining.set(None);
                    true
                }
                Some(calls) => {
                    remaining.set(Some(calls - 1));
                    false
                }
                None => false,
            }) {
                return Err(io::Error::from_raw_os_error(libc::EIO));
            }
            if unsafe { libc::flock(file.as_raw_fd(), operation | libc::LOCK_NB) } == 0 {
                return Ok(true);
            }
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::WouldBlock {
                Ok(false)
            } else {
                Err(error)
            }
        }

        pub(crate) fn pin_temporary(file: &File) -> io::Result<()> {
            if try_lock(file, libc::LOCK_SH)? {
                Ok(())
            } else {
                Err(io::Error::new(
                    io::ErrorKind::WouldBlock,
                    "temporary cache file is locked",
                ))
            }
        }

        fn unlink_checked(directory: &File, name: &str, file: &File) -> io::Result<()> {
            let name = CString::new(name)?;
            let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
            if unsafe {
                libc::fstatat(
                    directory.as_raw_fd(),
                    name.as_ptr(),
                    stat.as_mut_ptr(),
                    libc::AT_SYMLINK_NOFOLLOW,
                )
            } != 0
            {
                return Err(io::Error::last_os_error());
            }
            let stat = unsafe { stat.assume_init() };
            let opened = file.metadata()?;
            if stat_identity(&stat) != (opened.dev(), opened.ino()) {
                return Err(io::Error::other("cache inode changed before eviction"));
            }
            if unsafe { libc::unlinkat(directory.as_raw_fd(), name.as_ptr(), 0) } != 0 {
                Err(io::Error::last_os_error())
            } else {
                Ok(())
            }
        }

        fn allocated(metadata: &std::fs::Metadata) -> u64 {
            metadata.blocks().saturating_mul(512)
        }
        fn modified(metadata: &std::fs::Metadata) -> i128 {
            i128::from(metadata.mtime()) * 1_000_000_000 + i128::from(metadata.mtime_nsec())
        }
        fn touch(file: &File) {
            let _ = file.set_modified(SystemTime::now().max(UNIX_EPOCH));
        }
        #[allow(clippy::unnecessary_cast)] // libc statvfs fields differ across supported Unix hosts.
        fn allocation_unit(root: &File) -> io::Result<u64> {
            let mut stat = std::mem::MaybeUninit::<libc::statvfs>::uninit();
            if unsafe { libc::fstatvfs(root.as_raw_fd(), stat.as_mut_ptr()) } != 0 {
                return Err(io::Error::last_os_error());
            }
            let stat = unsafe { stat.assume_init() };
            // f_bsize may describe a much larger preferred I/O size (not allocation
            // granularity), notably on APFS. f_frsize is the allocation unit.
            Ok(if stat.f_frsize != 0 {
                stat.f_frsize as u64
            } else {
                stat.f_bsize as u64
            }
            .max(4096))
        }
        #[allow(clippy::unnecessary_cast)] // dev_t/ino_t widths differ on Linux and macOS.
        fn stat_identity(stat: &libc::stat) -> (u64, u64) {
            (stat.st_dev as u64, stat.st_ino as u64)
        }
        fn capacity<T>(message: &str) -> Result<T, CacheError> {
            Err(CacheError::Capacity(message.into()))
        }

        #[cfg(test)]
        mod tests {
            use super::*;
            use std::os::unix::fs::FileExt;
            use std::path::PathBuf;
            use std::sync::Barrier;
            use std::time::Duration;

            struct LockEio;
            impl LockEio {
                fn after(calls: usize) -> Self {
                    LOCK_EIO_AFTER.with(|remaining| {
                        assert!(remaining.get().is_none());
                        remaining.set(Some(calls));
                    });
                    Self
                }
            }
            impl Drop for LockEio {
                fn drop(&mut self) {
                    LOCK_EIO_AFTER.with(|remaining| remaining.set(None));
                }
            }

            fn assert_eio(error: CacheError) {
                match error {
                    CacheError::Io(error) => assert_eq!(error.raw_os_error(), Some(libc::EIO)),
                    error => panic!("expected injected I/O failure, got {error}"),
                }
            }

            struct Root(PathBuf);
            impl Root {
                fn new() -> Self {
                    static NEXT: std::sync::atomic::AtomicU64 =
                        std::sync::atomic::AtomicU64::new(0);
                    let path = std::env::temp_dir().join(format!(
                        "rock-disk-lru-{}-{}",
                        std::process::id(),
                        NEXT.fetch_add(1, Ordering::Relaxed),
                    ));
                    std::fs::create_dir(&path).unwrap();
                    Self(path)
                }
                fn open(&self) -> Arc<File> {
                    Arc::new(File::open(&self.0).unwrap())
                }
                fn directory(&self, domain: u64) -> Arc<File> {
                    let path = self.0.join(format!("{domain:064x}")).join("objects");
                    std::fs::create_dir_all(&path).unwrap();
                    Arc::new(File::open(path).unwrap())
                }
            }
            impl Drop for Root {
                fn drop(&mut self) {
                    let _ = std::fs::remove_dir_all(&self.0);
                }
            }
            fn name(index: u64) -> String {
                format!("{index:064x}.commit")
            }
            fn write_entry(cache: &Arc<DiskLru>, directory: &Arc<File>, index: u64) {
                let name = name(index);
                let reservation = cache
                    .reserve(directory, &name, cache.allocation_unit, false)
                    .unwrap();
                let file = cache
                    .open_pinned(directory, &name, libc::O_RDWR | libc::O_CREAT)
                    .unwrap();
                file.write_all_at(&vec![index as u8; 4096], 0).unwrap();
                file.sync_all().unwrap();
                reservation.finish().unwrap();
            }

            #[test]
            fn full_cache_keeps_live_fd_and_evicts_cold_entry_after_close() {
                let root = Root::new();
                let directory = root.directory(1);
                let cache = DiskLru::new(root.open(), 4096).unwrap();
                write_entry(&cache, &directory, 1);
                let live = cache
                    .open_pinned(&directory, &name(1), libc::O_RDONLY)
                    .unwrap();
                assert!(cache
                    .reserve(&directory, &name(2), 4096, false)
                    .unwrap_err()
                    .is_capacity());
                let stats = cache.stats().unwrap();
                assert_eq!((stats.used_bytes, stats.pinned_bytes), (4096, 4096));
                let mut data = [0; 16];
                live.read_exact_at(&mut data, 0).unwrap();
                assert_eq!(data, [1; 16]);
                drop(live);
                write_entry(&cache, &directory, 2);
                assert!(open_file(&directory, &name(1), libc::O_RDONLY).is_err());
                let stats = cache.stats().unwrap();
                assert_eq!(stats.used_bytes, 4096);
                assert_eq!(stats.evictions, 1);
                assert_eq!(stats.bypasses, 1);
            }

            #[test]
            fn restart_reclaims_all_domains_by_persisted_recency_and_counts_sparse_blocks() {
                let root = Root::new();
                let first = root.directory(1);
                let second = root.directory(2);
                for (directory, index, seconds, offset) in
                    [(&first, 1, 1, 0), (&second, 2, 2, 1 << 30)]
                {
                    let file =
                        open_file(directory, &name(index), libc::O_RDWR | libc::O_CREAT).unwrap();
                    file.write_all_at(&[index as u8; 4096], offset).unwrap();
                    file.sync_all().unwrap();
                    file.set_modified(UNIX_EPOCH + Duration::from_secs(seconds))
                        .unwrap();
                }
                let cache = DiskLru::new(root.open(), 4096).unwrap();
                let stats = cache.stats().unwrap();
                assert_eq!(
                    (stats.used_bytes, stats.entries, stats.evictions),
                    (4096, 1, 1)
                );
                assert!(open_file(&first, &name(1), libc::O_RDONLY).is_err());
                assert!(
                    open_file(&second, &name(2), libc::O_RDONLY)
                        .unwrap()
                        .metadata()
                        .unwrap()
                        .len()
                        > 1 << 30
                );
            }

            #[test]
            fn oversized_reservation_is_rejected_before_creating_file() {
                let root = Root::new();
                let directory = root.directory(1);
                let cache = DiskLru::new(root.open(), 1).unwrap();
                assert!(cache
                    .reserve(&directory, &name(1), 4096, false)
                    .unwrap_err()
                    .is_capacity());
                assert!(open_file(&directory, &name(1), libc::O_RDONLY).is_err());
                assert_eq!(cache.stats().unwrap().used_bytes, 0);
            }

            #[test]
            fn replacement_lock_error_leaves_no_protection_or_reservation() {
                let root = Root::new();
                let directory = root.directory(1);
                let cache = DiskLru::new(root.open(), 4096).unwrap();
                write_entry(&cache, &directory, 1);
                {
                    let _failure = LockEio::after(0);
                    assert_eio(cache.reserve(&directory, &name(1), 4096, true).unwrap_err());
                }
                {
                    let state = cache.state.lock().unwrap();
                    assert!(state.protected.is_empty());
                    assert!(state.held_protected.is_empty());
                    assert!(state.pending.is_empty());
                    assert_eq!(state.reserved, 0);
                }
                write_entry(&cache, &directory, 2);
                assert!(open_file(&directory, &name(1), libc::O_RDONLY).is_err());
                assert_eq!(cache.stats().unwrap().used_bytes, 4096);
            }

            #[test]
            fn graph_lock_error_rolls_back_prior_protection_and_shared_fds() {
                let root = Root::new();
                let directory = root.directory(1);
                let cache = DiskLru::new(root.open(), 8192).unwrap();
                write_entry(&cache, &directory, 1);
                write_entry(&cache, &directory, 2);
                {
                    let _failure = LockEio::after(1);
                    let result = cache.protect(&[
                        (Arc::clone(&directory), name(1)),
                        (Arc::clone(&directory), name(2)),
                    ]);
                    assert_eio(result.err().expect("the second graph lock must fail"));
                }
                {
                    let state = cache.state.lock().unwrap();
                    assert!(state.protected.is_empty());
                    assert!(state.held_protected.is_empty());
                }
                // Require reclamation of both original entries, independently of
                // filesystem timestamp ties and the LRU order of later new entries.
                let reservation = cache.reserve(&directory, &name(3), 8192, false).unwrap();
                for index in [1, 2] {
                    assert!(open_file(&directory, &name(index), libc::O_RDONLY).is_err());
                }
                drop(reservation);
                write_entry(&cache, &directory, 3);
                write_entry(&cache, &directory, 4);
                assert_eq!(cache.stats().unwrap().used_bytes, 8192);
            }

            #[test]
            fn failed_overlapping_graph_guard_keeps_only_the_original_pin() {
                let root = Root::new();
                let directory = root.directory(1);
                let cache = DiskLru::new(root.open(), 8192).unwrap();
                write_entry(&cache, &directory, 1);
                write_entry(&cache, &directory, 2);
                let original = cache.protect(&[(Arc::clone(&directory), name(1))]).unwrap();
                {
                    let _failure = LockEio::after(1);
                    let result = cache.protect(&[
                        (Arc::clone(&directory), name(1)),
                        (Arc::clone(&directory), name(2)),
                    ]);
                    assert_eio(result.err().expect("the second graph lock must fail"));
                }
                {
                    let state = cache.state.lock().unwrap();
                    assert_eq!(state.protected.len(), 1);
                    assert_eq!(state.protected[&Key::new(&directory, &name(1)).unwrap()], 1);
                    assert_eq!(state.held_protected.len(), 1);
                }
                write_entry(&cache, &directory, 3);
                assert!(open_file(&directory, &name(1), libc::O_RDONLY).is_ok());
                assert!(open_file(&directory, &name(2), libc::O_RDONLY).is_err());
                drop(original);
                write_entry(&cache, &directory, 4);
                assert!(open_file(&directory, &name(1), libc::O_RDONLY).is_err());
                assert_eq!(cache.stats().unwrap().used_bytes, 8192);
            }

            #[test]
            fn failed_partial_write_is_charged_before_its_reservation_is_released() {
                let root = Root::new();
                let directory = root.directory(1);
                let cache = DiskLru::new(root.open(), 4096).unwrap();
                let reservation = cache.reserve(&directory, &name(1), 4096, false).unwrap();
                let file = cache
                    .open_pinned(&directory, &name(1), libc::O_RDWR | libc::O_CREAT)
                    .unwrap();
                file.write_all_at(&[0x5a; 4096], 0).unwrap();
                file.sync_all().unwrap();
                // Model an I/O error after one successful allocation, before finish.
                drop(reservation);
                assert_eq!(cache.stats().unwrap().used_bytes, 4096);
                assert!(cache
                    .reserve(&directory, &name(2), 4096, false)
                    .unwrap_err()
                    .is_capacity());
                drop(file);
                write_entry(&cache, &directory, 2);
                assert_eq!(cache.stats().unwrap().used_bytes, 4096);
            }

            #[test]
            fn metadata_bundle_protects_files_created_after_guard() {
                let root = Root::new();
                let directory = root.directory(1);
                let cache = DiskLru::new(root.open(), 4096).unwrap();
                let guard = cache.protect(&[(Arc::clone(&directory), name(1))]).unwrap();
                write_entry(&cache, &directory, 1);
                assert!(cache
                    .reserve(&directory, &name(2), 4096, false)
                    .unwrap_err()
                    .is_capacity());
                drop(guard);
                write_entry(&cache, &directory, 2);
                assert_eq!(cache.stats().unwrap().used_bytes, 4096);
            }

            #[test]
            fn concurrent_admission_and_same_root_instances_share_one_budget() {
                let root = Root::new();
                let directory = root.directory(1);
                let cache = DiskLru::new(root.open(), 8192).unwrap();
                let second = DiskLru::new(root.open(), 8192).unwrap();
                assert!(Arc::ptr_eq(&cache, &second));
                let barrier = Arc::new(Barrier::new(9));
                let (sender, receiver) = std::sync::mpsc::channel();
                let jobs = (1..=8)
                    .map(|index| {
                        let cache = Arc::clone(&cache);
                        let directory = Arc::clone(&directory);
                        let barrier = Arc::clone(&barrier);
                        let sender = sender.clone();
                        std::thread::spawn(move || {
                            let reservation = cache.reserve(&directory, &name(index), 4096, false);
                            sender.send(reservation.is_ok()).unwrap();
                            barrier.wait();
                            drop(reservation);
                        })
                    })
                    .collect::<Vec<_>>();
                assert_eq!((0..8).filter(|_| receiver.recv().unwrap()).count(), 2);
                barrier.wait();
                for job in jobs {
                    job.join().unwrap();
                }
                assert_eq!(cache.stats().unwrap().used_bytes, 0);
            }

            #[test]
            fn cancelled_write_keeps_reservation_until_last_writable_fd_closes() {
                let root = Root::new();
                let directory = root.directory(1);
                let cache = DiskLru::new(root.open(), 4096).unwrap();
                let reservation = cache.reserve(&directory, &name(1), 4096, false).unwrap();
                let file = Arc::new(
                    open_file(&directory, "anonymous.tmp", libc::O_RDWR | libc::O_CREAT).unwrap(),
                );
                let filename = CString::new("anonymous.tmp").unwrap();
                assert_eq!(
                    unsafe { libc::unlinkat(directory.as_raw_fd(), filename.as_ptr(), 0) },
                    0
                );
                reservation.attach(&file).unwrap();
                let pending = reservation.pin_write(Arc::clone(&file));
                drop(file);
                drop(reservation);
                assert!(cache
                    .reserve(&directory, &name(2), 4096, false)
                    .unwrap_err()
                    .is_capacity());
                let writer: &dyn crate::snapshot::transport::MutableRangeWriter = &pending;
                writer.write_all_at(&[0x7f; 4096], 0).unwrap();
                let mut bytes = [0; 4];
                pending.file.read_exact_at(&mut bytes, 100).unwrap();
                assert_eq!(bytes, [0x7f; 4]);
                pending.file.sync_all().unwrap();
                assert_eq!(cache.stats().unwrap().used_bytes, 4096);
                drop(pending);
                // Reuse the same key without a preceding metrics scrape: admission
                // itself must reap the now-closed orphan reservation.
                assert!(cache.reserve(&directory, &name(1), 4096, false).is_ok());
                assert!(cache.reserve(&directory, &name(2), 4096, false).is_ok());
                assert_eq!(cache.stats().unwrap().used_bytes, 0);
            }

            #[test]
            fn staging_is_separate_and_closed_fds_are_pruned() {
                let root = Root::new();
                let directory = root.directory(1);
                let cache = DiskLru::new(root.open(), 4096).unwrap();
                let file = open_file(
                    &directory,
                    "anonymous.scratch",
                    libc::O_RDWR | libc::O_CREAT,
                )
                .unwrap();
                let filename = CString::new("anonymous.scratch").unwrap();
                assert_eq!(
                    unsafe { libc::unlinkat(directory.as_raw_fd(), filename.as_ptr(), 0) },
                    0
                );
                cache.record_staging(&file).unwrap();
                file.write_all_at(&[0; 4096], 1 << 30).unwrap();
                file.sync_all().unwrap();
                let stats = cache.stats().unwrap();
                assert_eq!((stats.used_bytes, stats.staging_bytes), (0, 4096));
                drop(file);
                assert_eq!(cache.stats().unwrap().staging_bytes, 0);
            }
        }
    }
    use super::DiskCacheStats;
    pub(crate) use disk::DEFAULT_MAX_BYTES;
    const HASH_WINDOW: usize = 1024 * 1024;
    const MAX_METADATA: usize = 128 * 1024 * 1024;

    #[derive(Debug, Error)]
    pub(crate) enum CacheError {
        #[error("invalid local byte cache request: {0}")]
        Invalid(String),
        #[error("local byte cache identity conflict: {0}")]
        IdentityConflict(String),
        #[error("local byte cache I/O: {0}")]
        Io(#[from] io::Error),
        #[error("local byte source: {0}")]
        Source(#[from] ObjectStoreError),
        #[error("local byte cache capacity: {0}")]
        Capacity(String),
    }

    impl CacheError {
        pub(crate) fn retryable(&self) -> bool {
            match self {
                Self::Io(_) | Self::Capacity(_) => true,
                Self::Source(error) => error.retryable(),
                Self::Invalid(_) | Self::IdentityConflict(_) => false,
            }
        }

        pub(crate) fn is_identity(&self) -> bool {
            match self {
                Self::Invalid(_) | Self::IdentityConflict(_) => true,
                Self::Source(error) => error.kind == PublishFailureKind::IdentityConflict,
                Self::Io(_) | Self::Capacity(_) => false,
            }
        }

        pub(crate) fn is_capacity(&self) -> bool {
            matches!(self, Self::Capacity(_))
        }
    }

    pub(crate) struct CacheMetadataPin {
        _guard: disk::ProtectedEntries,
    }

    #[derive(Debug)]
    pub(crate) struct LocalByteCache {
        pub(crate) objects_dir: Arc<File>,
        pub(crate) metadata_dir: Arc<File>,
        pub(crate) disk: Arc<disk::DiskLru>,
    }

    impl LocalByteCache {
        pub(crate) fn new(
            root: PathBuf,
            domain_identity: &str,
            max_bytes: u64,
        ) -> Result<Self, CacheError> {
            if domain_identity.is_empty() || domain_identity.len() > 64 * 1024 {
                return invalid("storage domain identity length is outside 1..=65536");
            }
            let root = Arc::new(open_private_directory(&root)?);
            let disk = disk::DiskLru::new(Arc::clone(&root), max_bytes)?;
            let domain = open_child_directory(&root, &hex_digest(domain_identity.as_bytes()))?;
            Ok(Self {
                objects_dir: Arc::new(open_child_directory(&domain, "objects")?),
                metadata_dir: Arc::new(open_child_directory(&domain, "metadata")?),
                disk,
            })
        }
        pub(crate) fn stats(&self) -> Result<DiskCacheStats, CacheError> {
            self.disk.stats()
        }

        pub(crate) fn pin_metadata(
            &self,
            objects: &[(&str, &str)],
        ) -> Result<CacheMetadataPin, CacheError> {
            let entries = objects
                .iter()
                .map(|(key, sha)| Ok((Arc::clone(&self.metadata_dir), metadata_name(key, sha)?)))
                .collect::<Result<Vec<_>, CacheError>>()?;
            self.disk
                .protect(&entries)
                .map(|guard| CacheMetadataPin { _guard: guard })
        }

        /// Legacy test fixture. Generated production objects use admitted named
        /// staging and same-inode publication through LocalFileCache instead.
        #[cfg(test)]
        pub(crate) fn create_scratch_file(&self) -> Result<File, CacheError> {
            let name = format!("{}.scratch", uuid::Uuid::new_v4().simple());
            let file = open_file(
                &self.objects_dir,
                &name,
                libc::O_RDWR | libc::O_CREAT | libc::O_EXCL,
            )?;
            require_regular(&file)?;
            let name = CString::new(name).expect("generated basename has no NUL");
            // SAFETY: the directory FD is live and the name is a generated basename.
            if unsafe { libc::unlinkat(self.objects_dir.as_raw_fd(), name.as_ptr(), 0) } != 0 {
                return Err(io::Error::last_os_error().into());
            }
            self.disk.record_staging(&file)?;
            Ok(file)
        }

        pub(crate) fn persist_metadata(
            &self,
            object_key: &str,
            payload: &[u8],
            expected_sha256: &str,
        ) -> Result<(), CacheError> {
            let name = metadata_name(object_key, expected_sha256)?;
            if payload.len() > MAX_METADATA {
                return invalid("metadata payload exceeds byte bound");
            }
            let expected = normalized_sha(expected_sha256)?;
            if format!("sha256:{}", hex_digest(payload)) != expected {
                return invalid("metadata payload SHA-256 differs from expected identity");
            }
            persist_bytes(
                &self.disk,
                &self.metadata_dir,
                &name,
                payload,
                &expected[7..],
            )
        }

        pub(crate) fn read_metadata(
            &self,
            object_key: &str,
            expected_sha256: &str,
        ) -> Result<Option<Vec<u8>>, CacheError> {
            let name = metadata_name(object_key, expected_sha256)?;
            let Ok(file) = self
                .disk
                .open_pinned(&self.metadata_dir, &name, libc::O_RDONLY)
            else {
                return Ok(None);
            };
            let length = file.metadata()?.len();
            if length > MAX_METADATA as u64 || require_regular(&file).is_err() {
                return Ok(None);
            }
            let mut bytes = vec![0; length as usize];
            if file.read_exact_at(&mut bytes, 0).is_err()
                || format!("sha256:{}", hex_digest(&bytes)) != normalized_sha(expected_sha256)?
            {
                return Ok(None);
            }
            Ok(Some(bytes))
        }
    }

    pub(crate) fn invalid<T>(message: &str) -> Result<T, CacheError> {
        Err(CacheError::Invalid(message.into()))
    }

    pub(crate) fn lock<T>(mutex: &Mutex<T>) -> Result<MutexGuard<'_, T>, CacheError> {
        mutex
            .lock()
            .map_err(|_| io::Error::other("local byte cache lock is poisoned").into())
    }

    pub(crate) async fn blocking<T: Send + 'static>(
        operation: impl FnOnce() -> Result<T, CacheError> + Send + 'static,
    ) -> Result<T, CacheError> {
        tokio::task::spawn_blocking(operation)
            .await
            .map_err(|error| CacheError::Io(io::Error::other(error.to_string())))?
    }

    pub(crate) fn normalized_sha(value: &str) -> Result<String, CacheError> {
        let hex = value.strip_prefix("sha256:").unwrap_or(value);
        if hex.len() != 64
            || !hex
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        {
            return invalid("SHA-256 must contain 64 lowercase hexadecimal digits");
        }
        Ok(format!("sha256:{hex}"))
    }

    pub(crate) fn hex_digest(bytes: &[u8]) -> String {
        format!("{:x}", Sha256::digest(bytes))
    }

    pub(crate) fn validate_key(key: &str) -> Result<(), CacheError> {
        if key.is_empty()
            || key.len() > 4096
            || key.contains(['\\', '\0'])
            || key
                .split('/')
                .any(|part| part.is_empty() || part == "." || part == "..")
        {
            return invalid("object key contains an unsafe or empty path component");
        }
        Ok(())
    }

    pub(crate) fn metadata_name(key: &str, sha: &str) -> Result<String, CacheError> {
        validate_key(key)?;
        let sha = normalized_sha(sha)?;
        let identity = serde_json::to_vec(&(key, sha))
            .map_err(|error| CacheError::Invalid(error.to_string()))?;
        Ok(format!("{}.metadata", hex_digest(&identity)))
    }

    pub(crate) fn persist_bytes(
        disk: &Arc<disk::DiskLru>,
        directory: &Arc<File>,
        name: &str,
        payload: &[u8],
        sha: &str,
    ) -> Result<(), CacheError> {
        match disk.open_pinned(directory, name, libc::O_RDONLY) {
            Ok(file) => {
                require_regular(&file)?;
                if file.metadata()?.len() == payload.len() as u64 {
                    let mut digest = Sha256::new();
                    let mut bytes = vec![0; HASH_WINDOW];
                    let mut offset = 0_usize;
                    while offset < payload.len() {
                        let count = (payload.len() - offset).min(bytes.len());
                        if file
                            .read_exact_at(&mut bytes[..count], offset as u64)
                            .is_err()
                        {
                            break;
                        }
                        digest.update(&bytes[..count]);
                        offset += count;
                    }
                    if offset == payload.len() && format!("{:x}", digest.finalize()) == sha {
                        return Ok(());
                    }
                }
            }
            Err(CacheError::Io(error)) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
        let reservation = disk.reserve(
            directory,
            name,
            disk.bytes_for_regions(&[(0, payload.len() as u64)])?,
            true,
        )?;
        let temporary = TemporaryFile::create(Arc::clone(directory))?;
        reservation.attach(&temporary.file)?;
        for (part, bytes) in payload.chunks(HASH_WINDOW).enumerate() {
            temporary
                .file
                .write_all_at(bytes, (part * HASH_WINDOW) as u64)?;
        }
        temporary
            .file
            .set_permissions(std::fs::Permissions::from_mode(0o400))?;
        temporary.file.sync_all()?;
        rename_file(directory, &temporary.name, name)?;
        directory.sync_all()?;
        reservation.finish()?;
        Ok(())
    }

    pub(crate) struct TemporaryFile {
        pub(crate) directory: Arc<File>,
        pub(crate) name: String,
        pub(crate) file: Arc<File>,
    }

    impl TemporaryFile {
        pub(crate) fn create(directory: Arc<File>) -> Result<Self, CacheError> {
            let name = format!("{}.tmp", uuid::Uuid::new_v4().simple());
            let file = open_file(
                &directory,
                &name,
                libc::O_RDWR | libc::O_CREAT | libc::O_EXCL,
            )?;
            disk::pin_temporary(&file)?;
            Ok(Self {
                directory,
                name,
                file: Arc::new(file),
            })
        }
    }

    impl Drop for TemporaryFile {
        fn drop(&mut self) {
            if let Ok(name) = CString::new(self.name.as_bytes()) {
                // SAFETY: directory is a live FD and name is a local basename.
                unsafe { libc::unlinkat(self.directory.as_raw_fd(), name.as_ptr(), 0) };
            }
        }
    }

    pub(crate) fn require_regular(file: &File) -> io::Result<()> {
        if file.metadata()?.is_file() {
            Ok(())
        } else {
            Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "cache entry is not a regular file",
            ))
        }
    }

    fn basename(name: &str) -> io::Result<CString> {
        if name.is_empty() || name.contains('/') || matches!(name, "." | "..") {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "unsafe cache basename",
            ));
        }
        CString::new(name)
            .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "NUL in cache basename"))
    }

    pub(crate) fn open_file(directory: &File, name: &str, flags: libc::c_int) -> io::Result<File> {
        let name = basename(name)?;
        // SAFETY: directory is owned/live, name is a checked basename; NOFOLLOW
        // prevents an attacker-controlled cache entry redirecting an open.
        let fd = unsafe {
            libc::openat(
                directory.as_raw_fd(),
                name.as_ptr(),
                flags | libc::O_CLOEXEC | libc::O_NOFOLLOW | libc::O_NONBLOCK,
                0o600,
            )
        };
        if fd < 0 {
            Err(io::Error::last_os_error())
        } else {
            // SAFETY: openat returned a fresh owned FD.
            let file = unsafe { File::from_raw_fd(fd) };
            require_regular(&file)?;
            Ok(file)
        }
    }

    pub(crate) fn rename_file(directory: &File, source: &str, destination: &str) -> io::Result<()> {
        let source = basename(source)?;
        let destination = basename(destination)?;
        // SAFETY: both names are checked basenames relative to the same owned FD.
        let result = unsafe {
            libc::renameat(
                directory.as_raw_fd(),
                source.as_ptr(),
                directory.as_raw_fd(),
                destination.as_ptr(),
            )
        };
        if result < 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(())
        }
    }

    fn open_private_directory(path: &Path) -> io::Result<File> {
        if !path.is_absolute() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "cache root must be absolute",
            ));
        }
        let mut current = File::open("/")?;
        for component in path.components() {
            match component {
                Component::RootDir => {}
                Component::Normal(name) => {
                    let name = CString::new(name.as_bytes()).map_err(|_| {
                        io::Error::new(io::ErrorKind::InvalidInput, "NUL in cache root")
                    })?;
                    current = open_directory_component(&current, &name)?;
                }
                _ => {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidInput,
                        "unsafe cache root component",
                    ));
                }
            }
        }
        Ok(current)
    }

    fn open_child_directory(parent: &File, name: &str) -> io::Result<File> {
        open_directory_component(parent, &basename(name)?)
    }

    fn open_directory_component(parent: &File, name: &CString) -> io::Result<File> {
        // SAFETY: parent is owned/live and name is one NUL-terminated component.
        let result = unsafe { libc::mkdirat(parent.as_raw_fd(), name.as_ptr(), 0o700) };
        if result < 0 && io::Error::last_os_error().kind() != io::ErrorKind::AlreadyExists {
            return Err(io::Error::last_os_error());
        }
        if result == 0 {
            // A later fsync of an object directory cannot make its own newly
            // created name durable in the parent. Persist each mkdir as well.
            parent.sync_all()?;
        }
        // SAFETY: NOFOLLOW applies to this component; traversing one FD at a time
        // prevents symlink races in ancestors as well as the final cache root.
        let fd = unsafe {
            libc::openat(
                parent.as_raw_fd(),
                name.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC | libc::O_NOFOLLOW,
            )
        };
        if fd < 0 {
            Err(io::Error::last_os_error())
        } else {
            // SAFETY: openat returned a fresh owned directory FD.
            Ok(unsafe { File::from_raw_fd(fd) })
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn arbitrary_immutable_bytes_need_no_format_descriptor_and_reauthenticate_after_restart() {
            let parent = std::fs::canonicalize(std::env::temp_dir()).unwrap();
            let root = parent.join(format!("rock-byte-cache-{}", uuid::Uuid::new_v4()));
            let payload = b"future-format: opaque immutable metadata";
            let sha = format!("sha256:{}", hex_digest(payload));
            let cache =
                LocalByteCache::new(root.clone(), "test-transport-domain", DEFAULT_MAX_BYTES)
                    .unwrap();
            cache
                .persist_metadata("opaque/index", payload, &sha)
                .unwrap();
            assert_eq!(
                cache.read_metadata("opaque/index", &sha).unwrap().unwrap(),
                payload
            );
            let name = metadata_name("opaque/index", &sha).unwrap();
            drop(cache);
            let cache =
                LocalByteCache::new(root.clone(), "test-transport-domain", DEFAULT_MAX_BYTES)
                    .unwrap();
            assert_eq!(
                cache.read_metadata("opaque/index", &sha).unwrap().unwrap(),
                payload
            );
            open_file(&cache.metadata_dir, &name, libc::O_RDONLY)
                .unwrap()
                .set_permissions(std::fs::Permissions::from_mode(0o600))
                .unwrap();
            let file = open_file(&cache.metadata_dir, &name, libc::O_RDWR).unwrap();
            file.write_all_at(b"!", 0).unwrap();
            assert!(cache.read_metadata("opaque/index", &sha).unwrap().is_none());
            drop(cache);
            std::fs::remove_dir_all(root).unwrap();
        }
    }
}

mod cache {
    //! Generic physical-byte cache backed by the existing node-wide disk LRU.

    mod staged {
        //! Bounded generated objects move from Writing to Sealed without copying bytes.

        use super::super::bytes::{
            disk::{self, Reservation},
            normalized_sha,
        };
        use super::*;
        use crate::snapshot::transport::{
            ObjectStoreError, PublishFailureKind, SealedObject, StagedObjectValidator,
            StagedObjectWriter,
        };
        use std::sync::atomic::{AtomicBool, Ordering};

        pub(super) struct TemporaryRoot(PathBuf);

        impl Drop for TemporaryRoot {
            fn drop(&mut self) {
                let _ = std::fs::remove_dir_all(&self.0);
            }
        }

        struct Writing {
            temporary: TemporaryFile,
            reservation: Reservation,
        }

        enum State {
            Writing(Writing),
            // Rename transferred the inode into the cache. Even if directory fsync or
            // subsequent bookkeeping fails, no writable descriptor may survive here.
            PublishedPending(ImmutableObjectIdentity, Arc<File>),
            Sealed(ImmutableObjectIdentity, Arc<dyn SealedObject>),
        }

        pub(super) struct ManagedStagedObject {
            cache: Arc<LocalFileCache>,
            max_bytes: u64,
            state: Mutex<State>,
            cleanup: Option<Arc<TemporaryRoot>>,
        }

        impl std::fmt::Debug for ManagedStagedObject {
            fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.debug_struct("ManagedStagedObject")
                    .field("max_bytes", &self.max_bytes)
                    .finish_non_exhaustive()
            }
        }

        pub(super) fn create(
            cache: Arc<LocalFileCache>,
            max_bytes: u64,
            cleanup: Option<Arc<TemporaryRoot>>,
        ) -> Result<Arc<ManagedStagedObject>, CacheError> {
            if max_bytes == 0 {
                return invalid("generated object admission bound must be positive");
            }
            let temporary = TemporaryFile::create(Arc::clone(&cache.bytes.objects_dir))?;
            let reservation = cache.bytes.disk.reserve(
                &cache.bytes.objects_dir,
                &temporary.name,
                cache.bytes.disk.bytes_for_regions(&[(0, max_bytes)])?,
                false,
            )?;
            reservation.attach(&temporary.file)?;
            Ok(Arc::new(ManagedStagedObject {
                cache,
                max_bytes,
                state: Mutex::new(State::Writing(Writing {
                    temporary,
                    reservation,
                })),
                cleanup,
            }))
        }

        /// Standalone callers use the same admission, cleanup and atomic seal path.
        /// Production publication supplies its node cache rather than this private root.
        pub(crate) fn temporary_staged_object(
            max_bytes: u64,
        ) -> Result<Arc<dyn StagedObjectWriter>, ObjectStoreError> {
            let path = std::env::temp_dir().join(format!("rock-staged-{}", uuid::Uuid::new_v4()));
            let cleanup = Arc::new(TemporaryRoot(path.clone()));
            let cache = LocalFileCache::new_with_source_and_limit(
                path,
                "temporary-generated-object",
                "temporary-generated-object",
                max_bytes.max(4096).checked_add(64 * 1024).ok_or_else(|| {
                    ObjectStoreError::new(
                        PublishFailureKind::Fatal,
                        "staged object bound overflows",
                    )
                })?,
            )
            .map_err(store_error)?;
            create(cache, max_bytes, Some(cleanup))
                .map(|writer| writer as Arc<dyn StagedObjectWriter>)
                .map_err(store_error)
        }

        impl MutableRangeWriter for ManagedStagedObject {
            fn write_all_at(&self, payload: &[u8], offset: u64) -> io::Result<()> {
                if offset
                    .checked_add(payload.len() as u64)
                    .is_none_or(|end| end > self.max_bytes)
                {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidInput,
                        "generated write exceeds its admitted byte bound",
                    ));
                }
                let state = self
                    .state
                    .lock()
                    .map_err(|_| io::Error::other("staged object lock poisoned"))?;
                match &*state {
                    State::Writing(writing) => writing.temporary.file.write_all_at(payload, offset),
                    State::PublishedPending(..) | State::Sealed(..) => Err(io::Error::new(
                        io::ErrorKind::PermissionDenied,
                        "sealed generated object is immutable",
                    )),
                }
            }
        }

        impl StagedObjectWriter for ManagedStagedObject {
            fn length(&self) -> io::Result<u64> {
                let state = self
                    .state
                    .lock()
                    .map_err(|_| io::Error::other("staged object lock poisoned"))?;
                match &*state {
                    State::Writing(writing) => writing.temporary.file.metadata().map(|m| m.len()),
                    State::PublishedPending(identity, _) | State::Sealed(identity, _) => {
                        Ok(identity.length)
                    }
                }
            }

            fn seal(
                &self,
                identity: &ImmutableObjectIdentity,
                validator: &dyn StagedObjectValidator,
            ) -> Result<Arc<dyn SealedObject>, ObjectStoreError> {
                let mut state = lock(&self.state).map_err(store_error)?;
                if let State::Sealed(previous, handle) = &*state {
                    if previous != identity {
                        return Err(ObjectStoreError::new(
                            PublishFailureKind::IdentityConflict,
                            "staged object was sealed with a different identity",
                        ));
                    }
                    return Ok(Arc::clone(handle));
                }
                if let State::PublishedPending(previous, pin) = &*state {
                    if previous != identity {
                        return Err(ObjectStoreError::new(
                            PublishFailureKind::IdentityConflict,
                            "published object has a different identity",
                        ));
                    }
                    let key = self
                        .cache
                        .physical_key(identity, 0, identity.length)
                        .map_err(store_error)?;
                    let stem = object_stem(&key).map_err(store_error)?;
                    let io_lock = object_lock(&self.cache.io_locks, &stem).map_err(store_error)?;
                    let _io = io_lock.try_lock().map_err(|_| {
                        ObjectStoreError::new(
                            PublishFailureKind::Retryable,
                            "published object has in-flight cache I/O",
                        )
                    })?;
                    let readonly = open_complete(&self.cache.bytes, &self.cache.verified, &key)
                        .map_err(store_error)?
                        .ok_or_else(|| {
                            ObjectStoreError::new(
                                PublishFailureKind::IdentityConflict,
                                "published object failed revalidation",
                            )
                        })?;
                    let expected = pin.metadata().map_err(io_store_error)?;
                    let observed = readonly.metadata().map_err(io_store_error)?;
                    if (expected.dev(), expected.ino()) != (observed.dev(), observed.ino()) {
                        return Err(ObjectStoreError::new(
                            PublishFailureKind::IdentityConflict,
                            "published object inode changed",
                        ));
                    }
                    validator.validate_file(&readonly)?;
                    readonly.sync_all().map_err(io_store_error)?;
                    disk::sync_directory(&self.cache.bytes.objects_dir).map_err(io_store_error)?;
                    let handle = self.finish_seal(identity, readonly)?;
                    *state = State::Sealed(identity.clone(), Arc::clone(&handle));
                    return Ok(handle);
                }
                let State::Writing(writing) = &*state else {
                    unreachable!()
                };
                let file = &writing.temporary.file;
                let key = self
                    .cache
                    .physical_key(identity, 0, identity.length)
                    .map_err(store_error)?;
                if file.metadata().map_err(io_store_error)?.len() != identity.length {
                    return Err(ObjectStoreError::new(
                        PublishFailureKind::IdentityConflict,
                        "generated object length differs from its identity",
                    ));
                }
                let mut digest = Sha256::new();
                let mut buffer = vec![0; HASH_WINDOW];
                let mut offset = 0;
                while offset < identity.length {
                    let count = (identity.length - offset).min(HASH_WINDOW as u64) as usize;
                    file.read_exact_at(&mut buffer[..count], offset)
                        .map_err(io_store_error)?;
                    digest.update(&buffer[..count]);
                    offset += count as u64;
                }
                if format!("sha256:{:x}", digest.finalize())
                    != normalized_sha(&identity.sha256).map_err(store_error)?
                {
                    return Err(ObjectStoreError::new(
                        PublishFailureKind::IdentityConflict,
                        "generated object SHA-256 differs from its identity",
                    ));
                }
                validator.validate_file(file)?;
                let stem = object_stem(&key).map_err(store_error)?;
                // Serialize publication with downloaded ranges and the legacy seal
                // adapter. A busy object is retryable; never block a Tokio worker on
                // its asynchronous mutex or race replacement of a pinned source.
                let io_lock = object_lock(&self.cache.io_locks, &stem).map_err(store_error)?;
                let _io = io_lock.try_lock().map_err(|_| {
                    ObjectStoreError::new(
                        PublishFailureKind::Retryable,
                        "generated object has in-flight cache I/O",
                    )
                })?;
                // Reuse an already authenticated complete object rather than replacing
                // an inode another reader has pinned. The newly encoded temporary is
                // dropped with its reservation when the state changes below.
                let readonly = match open_complete(&self.cache.bytes, &self.cache.verified, &key)
                    .map_err(store_error)?
                {
                    Some(existing) => {
                        validator.validate_file(&existing)?;
                        existing.sync_all().map_err(io_store_error)?;
                        disk::sync_directory(&self.cache.bytes.objects_dir)
                            .map_err(io_store_error)?;
                        existing
                    }
                    None => {
                        file.set_permissions(std::fs::Permissions::from_mode(0o400))
                            .map_err(io_store_error)?;
                        file.sync_all().map_err(io_store_error)?;
                        // Keep a read-only pin before rename so a failed directory
                        // flush can close the writable description without losing the
                        // unpublished inode or allowing LRU to discard its only copy.
                        let pin = Arc::new(
                            open_file(
                                &self.cache.bytes.objects_dir,
                                &writing.temporary.name,
                                libc::O_RDONLY,
                            )
                            .map_err(io_store_error)?,
                        );
                        disk::pin_temporary(&pin).map_err(io_store_error)?;
                        match writing
                            .reservation
                            .publish_as(&writing.temporary.name, &format!("{stem}.commit"))
                        {
                            Ok(readonly) => readonly,
                            Err(error) => {
                                let published = writing.reservation.published();
                                if published {
                                    *state = State::PublishedPending(identity.clone(), pin);
                                }
                                return Err(store_error(error));
                            }
                        }
                    }
                };
                // Freeze the writer immediately after publication, before any fallible
                // integrity memo or cleanup work can return an error to the caller.
                *state = State::PublishedPending(identity.clone(), Arc::clone(&readonly));
                let handle = self.finish_seal(identity, readonly)?;
                *state = State::Sealed(identity.clone(), Arc::clone(&handle));
                Ok(handle)
            }
        }

        impl ManagedStagedObject {
            fn finish_seal(
                &self,
                identity: &ImmutableObjectIdentity,
                readonly: Arc<File>,
            ) -> Result<Arc<dyn SealedObject>, ObjectStoreError> {
                let key = self
                    .cache
                    .physical_key(identity, 0, identity.length)
                    .map_err(store_error)?;
                let stem = object_stem(&key).map_err(store_error)?;
                lock(&self.cache.verified).map_err(store_error)?.record(
                    stem.clone(),
                    FileSignature::from(&readonly.metadata().map_err(io_store_error)?),
                    true,
                );
                // Once a complete object exists, original-byte range copies are
                // redundant. Failure to trim disposable ranges cannot invalidate a
                // successfully synchronized, pinned generated object.
                let _ = discard_partial_ranges(&self.cache, &stem);
                let handle: Arc<dyn SealedObject> = Arc::new(LocalSealedObject {
                    file: readonly,
                    length: identity.length,
                    remote_committed: AtomicBool::new(false),
                    _cache: Arc::clone(&self.cache),
                    _cleanup: self.cleanup.clone(),
                });
                Ok(handle)
            }
        }

        struct LocalSealedObject {
            file: Arc<File>,
            length: u64,
            remote_committed: AtomicBool,
            _cache: Arc<LocalFileCache>,
            _cleanup: Option<Arc<TemporaryRoot>>,
        }

        impl std::fmt::Debug for LocalSealedObject {
            fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.debug_struct("LocalSealedObject")
                    .field("length", &self.length)
                    .field(
                        "remote_committed",
                        &self.remote_committed.load(Ordering::Acquire),
                    )
                    .finish_non_exhaustive()
            }
        }

        impl ReplayableByteSource for LocalSealedObject {
            fn length(&self) -> u64 {
                self.length
            }

            fn read_at<'a>(
                &'a self,
                offset: u64,
                max_bytes: usize,
            ) -> std::pin::Pin<
                Box<
                    dyn std::future::Future<Output = Result<Vec<u8>, ObjectStoreError>> + Send + 'a,
                >,
            > {
                Box::pin(async move {
                    if offset > self.length || max_bytes as u64 > MAX_RANGE_BYTES {
                        return Err(ObjectStoreError::new(
                            PublishFailureKind::Fatal,
                            "sealed read exceeds its bound",
                        ));
                    }
                    let file = Arc::clone(&self.file);
                    let cleanup = self._cleanup.clone();
                    let count = (self.length - offset).min(max_bytes as u64) as usize;
                    tokio::task::spawn_blocking(move || {
                        let _cleanup = cleanup;
                        let mut bytes = vec![0; count];
                        file.read_exact_at(&mut bytes, offset)
                            .map_err(io_store_error)?;
                        Ok(bytes)
                    })
                    .await
                    .map_err(|e| io_store_error(io::Error::other(e.to_string())))?
                })
            }
        }

        impl SealedObject for LocalSealedObject {
            fn file(&self) -> &File {
                &self.file
            }
            fn mark_remote_committed(&self) {
                self.remote_committed.store(true, Ordering::Release);
            }
        }

        fn io_store_error(error: io::Error) -> ObjectStoreError {
            ObjectStoreError::new(PublishFailureKind::Retryable, error.to_string())
        }

        fn store_error(error: CacheError) -> ObjectStoreError {
            let kind = if error.retryable() {
                PublishFailureKind::Retryable
            } else {
                PublishFailureKind::Fatal
            };
            ObjectStoreError::new(kind, error.to_string())
        }

        fn discard_partial_ranges(cache: &LocalFileCache, stem: &str) -> Result<(), CacheError> {
            for name in cache
                .bytes
                .disk
                .entry_names(&cache.bytes.objects_dir, ".partial")?
            {
                let file = match open_file(&cache.bytes.objects_dir, &name, libc::O_RDONLY) {
                    Ok(file) => file,
                    Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
                    Err(error) => return Err(error.into()),
                };
                let mut prefix = [0; 12];
                if file.read_exact_at(&mut prefix, 0).is_err() || &prefix[..8] != RANGE_MAGIC {
                    continue;
                }
                let size =
                    u32::from_le_bytes(prefix[8..].try_into().expect("four-byte header length"))
                        as usize;
                if size == 0 || size > MAX_HEADER_BYTES {
                    continue;
                }
                let mut header = vec![0; size];
                if file.read_exact_at(&mut header, 12).is_err() {
                    continue;
                }
                let Ok(header) = serde_json::from_slice::<RangeHeader>(&header) else {
                    continue;
                };
                if object_stem(&header.key)? == stem {
                    drop(file);
                    cache.bytes.disk.remove(&cache.bytes.objects_dir, &name)?;
                }
            }
            Ok(())
        }

        #[cfg(test)]
        mod tests {
            use super::*;

            struct Accept;
            impl StagedObjectValidator for Accept {
                fn validate_file(&self, _: &File) -> Result<(), ObjectStoreError> {
                    Ok(())
                }
            }

            fn identity(payload: &[u8]) -> ImmutableObjectIdentity {
                ImmutableObjectIdentity {
                    object_key: "layers/test/data.commit".into(),
                    sha256: format!("sha256:{}", hex_digest(payload)),
                    length: payload.len() as u64,
                }
            }

            fn cache(root: &std::path::Path, max_bytes: u64) -> Arc<LocalFileCache> {
                LocalFileCache::new_with_source_and_limit(
                    root.into(),
                    "generated-tests",
                    "generated-tests",
                    max_bytes,
                )
                .unwrap()
            }

            #[tokio::test]
            async fn same_inode_seal_is_readonly_replayable_and_cache_visible() {
                let root = tempfile::tempdir().unwrap();
                let cache = cache(root.path(), 1024 * 1024);
                let writer = create(Arc::clone(&cache), 16 * 1024, None).unwrap();
                writer.write_all_at(b"physical bytes", 0).unwrap();
                let inode = match &*writer.state.lock().unwrap() {
                    State::Writing(writing) => writing.temporary.file.metadata().unwrap().ino(),
                    _ => unreachable!(),
                };
                let identity = identity(b"physical bytes");
                let sealed = writer.seal(&identity, &Accept).unwrap();
                assert_eq!(sealed.file().metadata().unwrap().ino(), inode);
                assert!(sealed.file().write_all_at(b"bad", 0).is_err());
                assert!(writer.write_all_at(b"bad", 0).is_err());
                assert_eq!(sealed.read_at(0, 64).await.unwrap(), b"physical bytes");
                assert_eq!(sealed.read_at(0, 64).await.unwrap(), b"physical bytes");
                sealed.mark_remote_committed();
                let key = cache.physical_key(&identity, 0, identity.length).unwrap();
                assert_eq!(cache.read(&key).await.unwrap().unwrap(), b"physical bytes");
                let stats = cache.stats().unwrap();
                assert_eq!(stats.entries, 1);
                assert_eq!(stats.used_bytes, stats.pinned_bytes);
            }

            #[test]
            fn writing_admission_and_pin_prevent_unbounded_unpublished_storage() {
                let root = tempfile::tempdir().unwrap();
                let cache = cache(root.path(), 4096);
                let writer = create(Arc::clone(&cache), 4096, None).unwrap();
                assert!(matches!(
                    create(Arc::clone(&cache), 4096, None),
                    Err(CacheError::Capacity(_))
                ));
                assert!(writer.write_all_at(&[1], 4096).is_err());
                let data = vec![7; 4096];
                writer.write_all_at(&data, 0).unwrap();
                let sealed = writer.seal(&identity(&data), &Accept).unwrap();
                drop(writer);
                assert!(matches!(
                    create(Arc::clone(&cache), 4096, None),
                    Err(CacheError::Capacity(_))
                ));
                sealed.mark_remote_committed();
                assert!(matches!(
                    create(Arc::clone(&cache), 4096, None),
                    Err(CacheError::Capacity(_))
                ));
                drop(sealed);
                assert!(create(Arc::clone(&cache), 4096, None).is_ok());
            }

            #[test]
            fn failed_validation_and_cancelled_writer_leave_no_temporary_or_reservation() {
                let root = tempfile::tempdir().unwrap();
                let cache = cache(root.path(), 4096);
                let writer = create(Arc::clone(&cache), 4096, None).unwrap();
                writer.write_all_at(b"correct", 0).unwrap();
                assert!(writer.seal(&identity(b"corrupt"), &Accept).is_err());
                drop(writer);
                assert_eq!(cache.stats().unwrap().used_bytes, 0);
                let next = create(Arc::clone(&cache), 4096, None).unwrap();
                drop(next);
                assert_eq!(cache.stats().unwrap().entries, 0);
                // Named temporaries must not survive owner cancellation.
                let domain = root
                    .path()
                    .join(hex_digest(b"generated-tests"))
                    .join("objects");
                assert_eq!(std::fs::read_dir(domain).unwrap().count(), 0);
            }

            #[test]
            fn format_rejection_never_publishes_a_complete_object() {
                struct Reject;
                impl StagedObjectValidator for Reject {
                    fn validate_file(&self, _: &File) -> Result<(), ObjectStoreError> {
                        Err(ObjectStoreError::new(
                            PublishFailureKind::Fatal,
                            "invalid format",
                        ))
                    }
                }
                let root = tempfile::tempdir().unwrap();
                let cache = cache(root.path(), 4096);
                let writer = create(Arc::clone(&cache), 4096, None).unwrap();
                writer.write_all_at(b"bytes", 0).unwrap();
                assert!(writer.seal(&identity(b"bytes"), &Reject).is_err());
                assert!(cache
                    .bytes
                    .disk
                    .entry_names(&cache.bytes.objects_dir, ".commit")
                    .unwrap()
                    .is_empty());
                drop(writer);
                assert_eq!(cache.stats().unwrap().entries, 0);
                assert!(create(cache, 4096, None).is_ok());
            }

            #[tokio::test]
            async fn sealing_reuses_a_pinned_identical_object_and_discards_range_copies() {
                let root = tempfile::tempdir().unwrap();
                let cache = cache(root.path(), 1024 * 1024);
                let data = b"complete object bytes";
                let identity = identity(data);
                let key = cache.physical_key(&identity, 0, data.len() as u64).unwrap();
                cache.write(&key, Arc::new(data.to_vec())).await.unwrap();
                assert_eq!(cache.stats().unwrap().entries, 1);
                let first = create(Arc::clone(&cache), 4096, None).unwrap();
                first.write_all_at(data, 0).unwrap();
                let sealed = first.seal(&identity, &Accept).unwrap();
                assert_eq!(cache.stats().unwrap().entries, 1, "partial must be removed");
                let second = create(Arc::clone(&cache), 4096, None).unwrap();
                second.write_all_at(data, 0).unwrap();
                let reused = second.seal(&identity, &Accept).unwrap();
                assert_eq!(
                    sealed.file().metadata().unwrap().ino(),
                    reused.file().metadata().unwrap().ino()
                );
                assert_eq!(reused.read_at(0, 64).await.unwrap(), data);
                assert_eq!(cache.stats().unwrap().entries, 1);
            }

            #[test]
            fn active_cache_io_causes_retry_without_publishing_or_losing_source() {
                let root = tempfile::tempdir().unwrap();
                let cache = cache(root.path(), 1024 * 1024);
                let writer = create(Arc::clone(&cache), 4096, None).unwrap();
                writer.write_all_at(b"bytes", 0).unwrap();
                let identity = identity(b"bytes");
                let key = cache.physical_key(&identity, 0, identity.length).unwrap();
                let io = object_lock(&cache.io_locks, &object_stem(&key).unwrap()).unwrap();
                let guard = io.try_lock().unwrap();
                assert!(writer.seal(&identity, &Accept).unwrap_err().retryable());
                drop(guard);
                assert!(writer.seal(&identity, &Accept).is_ok());
            }

            #[test]
            fn pinned_corrupt_cache_inode_is_not_replaced_by_generated_publication() {
                let root = tempfile::tempdir().unwrap();
                let cache = cache(root.path(), 1024 * 1024);
                let first = create(Arc::clone(&cache), 4096, None).unwrap();
                first.write_all_at(b"bytes", 0).unwrap();
                let identity = identity(b"bytes");
                let sealed = first.seal(&identity, &Accept).unwrap();
                let inode = sealed.file().metadata().unwrap().ino();
                let key = cache.physical_key(&identity, 0, identity.length).unwrap();
                let name = format!("{}.commit", object_stem(&key).unwrap());
                sealed
                    .file()
                    .set_permissions(std::fs::Permissions::from_mode(0o600))
                    .unwrap();
                let external = open_file(&cache.bytes.objects_dir, &name, libc::O_RDWR).unwrap();
                external.write_all_at(b"wrong", 0).unwrap();
                let second = create(Arc::clone(&cache), 4096, None).unwrap();
                second.write_all_at(b"bytes", 0).unwrap();
                assert!(second.seal(&identity, &Accept).is_err());
                assert_eq!(
                    open_file(&cache.bytes.objects_dir, &name, libc::O_RDONLY)
                        .unwrap()
                        .metadata()
                        .unwrap()
                        .ino(),
                    inode
                );
            }

            #[test]
            fn restart_discards_unowned_named_temporaries() {
                let root = tempfile::tempdir().unwrap();
                let first = cache(root.path(), 4096);
                let name = format!("{}.tmp", uuid::Uuid::new_v4().simple());
                let orphan = open_file(
                    &first.bytes.objects_dir,
                    &name,
                    libc::O_RDWR | libc::O_CREAT | libc::O_EXCL,
                )
                .unwrap();
                orphan.write_all_at(b"unpublished", 0).unwrap();
                drop(orphan);
                drop(first);
                let reopened = cache(root.path(), 4096);
                assert_eq!(reopened.stats().unwrap().used_bytes, 0);
                assert!(open_file(&reopened.bytes.objects_dir, &name, libc::O_RDONLY).is_err());
            }

            #[tokio::test]
            async fn directory_sync_failure_freezes_published_inode_and_retry_seals_without_rewrite(
            ) {
                let root = tempfile::tempdir().unwrap();
                let cache = cache(root.path(), 4096);
                let writer = create(Arc::clone(&cache), 4096, None).unwrap();
                writer.write_all_at(b"bytes", 0).unwrap();
                let identity = identity(b"bytes");
                disk::fail_next_directory_sync();
                assert!(writer.seal(&identity, &Accept).unwrap_err().retryable());
                assert!(writer.write_all_at(b"wrong", 0).is_err());
                let inode = match &*writer.state.lock().unwrap() {
                    State::PublishedPending(_, pin) => {
                        assert!(pin.write_all_at(b"wrong", 0).is_err());
                        let mut bytes = [0; 5];
                        pin.read_exact_at(&mut bytes, 0).unwrap();
                        assert_eq!(&bytes, b"bytes");
                        pin.metadata().unwrap().ino()
                    }
                    _ => panic!("post-rename failure must retain only an immutable pin"),
                };
                let stats = cache.stats().unwrap();
                assert_eq!(stats.entries, 1);
                assert_eq!(stats.used_bytes, 4096);
                assert_eq!(stats.pinned_bytes, stats.used_bytes);
                assert!(matches!(
                    create(Arc::clone(&cache), 4096, None),
                    Err(CacheError::Capacity(_))
                ));
                let sealed = writer.seal(&identity, &Accept).unwrap();
                assert_eq!(sealed.file().metadata().unwrap().ino(), inode);
                assert_eq!(sealed.read_at(0, 5).await.unwrap(), b"bytes");
                assert!(writer.write_all_at(b"wrong", 0).is_err());
                drop(writer);
                drop(sealed);
                assert!(create(cache, 4096, None).is_ok());
            }
        }
    }

    pub(crate) use staged::temporary_staged_object;

    use std::collections::HashMap;
    use std::fs::{File, Metadata};
    use std::io;
    use std::os::unix::fs::{FileExt, MetadataExt, PermissionsExt};
    use std::path::PathBuf;
    use std::sync::{Arc, Mutex, Weak};

    use async_trait::async_trait;
    use serde::{Deserialize, Serialize};
    use sha2::{Digest, Sha256};

    use crate::observability::read_path::{ReadPathStage, ReadPathTimer};

    use super::bytes::{
        blocking, hex_digest, invalid, lock, open_file, rename_file, require_regular, CacheError,
        CacheMetadataPin, LocalByteCache, TemporaryFile, DEFAULT_MAX_BYTES,
    };
    use crate::snapshot::transport::interfaces::physical_cache::{
        PhysicalCacheKey, PhysicalRangeCache,
    };
    use crate::snapshot::transport::{
        ImmutableObjectIdentity, MutableRangeWriter, PhysicalRangeRequest, ReplayableByteSource,
    };

    #[cfg(test)]
    use super::DiskCacheStats;

    const MAX_RANGE_BYTES: u64 = 64 * 1024 * 1024;
    const MAX_HEADER_BYTES: usize = 512 * 1024;
    const HASH_WINDOW: usize = 1024 * 1024;
    const MAX_VERIFIED_OBJECTS: usize = 1024;
    const RANGE_MAGIC: &[u8; 8] = b"RCKPHYS1";

    /// Format-specific validation is supplied by the selected writer. The cache
    /// itself authenticates only complete physical length/SHA and stored ranges.
    #[cfg(test)]
    pub(crate) trait SealValidator: Send + Sync {
        fn validate_range(&self, offset: u64, payload: &[u8]) -> Result<(), CacheError>;
        fn validate_file(&self, file: &File) -> Result<(), CacheError>;
    }

    type ObjectLocks = Mutex<HashMap<String, Weak<tokio::sync::Mutex<()>>>>;

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    struct FileSignature {
        device: u64,
        inode: u64,
        length: u64,
        modified: (i64, i64),
        changed: (i64, i64),
    }

    impl From<&Metadata> for FileSignature {
        fn from(metadata: &Metadata) -> Self {
            Self {
                device: metadata.dev(),
                inode: metadata.ino(),
                length: metadata.len(),
                modified: (metadata.mtime(), metadata.mtime_nsec()),
                changed: (metadata.ctime(), metadata.ctime_nsec()),
            }
        }
    }

    #[derive(Debug, Default)]
    struct VerifiedObjects {
        objects: HashMap<String, (FileSignature, bool)>,
    }

    impl VerifiedObjects {
        fn record(&mut self, stem: String, signature: FileSignature, valid: bool) {
            if self.objects.len() >= MAX_VERIFIED_OBJECTS && !self.objects.contains_key(&stem) {
                self.objects.clear(); // Metadata only: dropping a memo never drops a disk pin.
            }
            self.objects.insert(stem, (signature, valid));
        }
    }

    #[derive(Debug, Serialize, Deserialize)]
    #[serde(deny_unknown_fields)]
    struct RangeHeader {
        key: PhysicalCacheKey,
        payload_sha256: String,
    }

    #[derive(Debug)]
    pub(crate) struct LocalFileCache {
        bytes: Arc<LocalByteCache>,
        source_id: String,
        #[cfg(test)]
        seals: tokio::sync::Semaphore,
        #[cfg(test)]
        seal_locks: ObjectLocks,
        io_locks: ObjectLocks,
        verified: Arc<Mutex<VerifiedObjects>>,
    }

    impl LocalFileCache {
        /// Admit a generated object before encoding. Capacity is a retryable
        /// failure rather than permission to retain an unbounded unpublished copy.
        pub(crate) fn create_staged_object(
            self: &Arc<Self>,
            max_bytes: u64,
        ) -> Result<Arc<dyn crate::snapshot::transport::StagedObjectWriter>, CacheError> {
            staged::create(Arc::clone(self), max_bytes, None)
                .map(|writer| writer as Arc<dyn crate::snapshot::transport::StagedObjectWriter>)
        }

        #[allow(dead_code)]
        pub(crate) fn new(root: PathBuf, domain_identity: &str) -> Result<Arc<Self>, CacheError> {
            Self::new_with_limit(root, domain_identity, DEFAULT_MAX_BYTES)
        }

        pub(crate) fn new_with_limit(
            root: PathBuf,
            domain_identity: &str,
            max_bytes: u64,
        ) -> Result<Arc<Self>, CacheError> {
            Self::new_with_source_and_limit(root, domain_identity, domain_identity, max_bytes)
        }

        pub(crate) fn new_with_source_and_limit(
            root: PathBuf,
            domain_identity: &str,
            source_id: &str,
            max_bytes: u64,
        ) -> Result<Arc<Self>, CacheError> {
            Self::from_byte_cache(
                Arc::new(LocalByteCache::new(root, domain_identity, max_bytes)?),
                source_id,
            )
        }

        pub(crate) fn from_byte_cache(
            bytes: Arc<LocalByteCache>,
            source_id: &str,
        ) -> Result<Arc<Self>, CacheError> {
            if source_id.is_empty() || source_id.len() > 65536 {
                return invalid("cache physical source identity is invalid");
            }
            Ok(Arc::new(Self {
                bytes,
                source_id: source_id.into(),
                #[cfg(test)]
                seals: tokio::sync::Semaphore::new(4),
                #[cfg(test)]
                seal_locks: Mutex::new(HashMap::new()),
                io_locks: Mutex::new(HashMap::new()),
                verified: Arc::new(Mutex::new(VerifiedObjects::default())),
            }))
        }

        pub(crate) fn byte_cache(&self) -> &Arc<LocalByteCache> {
            &self.bytes
        }
        #[cfg(test)]
        pub(crate) fn source_id(&self) -> &str {
            &self.source_id
        }
        #[cfg(test)]
        pub(crate) fn stats(&self) -> Result<DiskCacheStats, CacheError> {
            self.bytes.stats()
        }
        pub(crate) fn pin_metadata(
            &self,
            objects: &[(&str, &str)],
        ) -> Result<CacheMetadataPin, CacheError> {
            self.bytes.pin_metadata(objects)
        }
        #[cfg(test)]
        pub(crate) fn create_scratch_file(&self) -> Result<File, CacheError> {
            self.bytes.create_scratch_file()
        }
        pub(crate) fn persist_metadata(
            &self,
            key: &str,
            payload: &[u8],
            sha: &str,
        ) -> Result<(), CacheError> {
            self.bytes.persist_metadata(key, payload, sha)
        }
        pub(crate) fn read_metadata(
            &self,
            key: &str,
            sha: &str,
        ) -> Result<Option<Vec<u8>>, CacheError> {
            self.bytes.read_metadata(key, sha)
        }

        pub(crate) fn physical_key(
            &self,
            identity: &ImmutableObjectIdentity,
            offset: u64,
            length: u64,
        ) -> Result<PhysicalCacheKey, CacheError> {
            PhysicalCacheKey::from_request(
                &self.source_id,
                PhysicalRangeRequest {
                    namespace: None,
                    object_key: &identity.object_key,
                    object_sha256: &identity.sha256,
                    object_size: Some(identity.length),
                    object_version: None,
                    offset,
                    length,
                },
            )
            .map_err(|error| CacheError::Invalid(error.to_string()))
        }

        /// Replay the original physical bytes into one immutable FD. Partial range
        /// entries never auto-promote; the selected format validates this actual FD.
        #[cfg(test)]
        pub(crate) async fn seal_blob(
            &self,
            identity: &ImmutableObjectIdentity,
            source: Arc<dyn ReplayableByteSource>,
            chunk_bytes: usize,
            validator: Arc<dyn SealValidator>,
        ) -> Result<Arc<File>, CacheError> {
            let key = self.physical_key(identity, 0, identity.length)?;
            if source.length() != identity.length
                || chunk_bytes == 0
                || chunk_bytes as u64 > MAX_RANGE_BYTES
            {
                return invalid("seal source geometry or chunk bound is inconsistent");
            }
            let stem = object_stem(&key)?;
            let seal_lock = object_lock(&self.seal_locks, &stem)?;
            let _seal = seal_lock.lock().await;
            let _admission = self
                .seals
                .acquire()
                .await
                .map_err(|error| CacheError::Io(io::Error::other(error.to_string())))?;
            let io_lock = object_lock(&self.io_locks, &stem)?;
            {
                let _io = io_lock.lock().await;
                let bytes = Arc::clone(&self.bytes);
                let verified = Arc::clone(&self.verified);
                let key = key.clone();
                let validate = Arc::clone(&validator);
                if let Some(file) = blocking(move || {
                    let Some(file) = open_complete(&bytes, &verified, &key)? else {
                        return Ok(None);
                    };
                    validate.validate_file(&file)?;
                    Ok(Some(file))
                })
                .await?
                {
                    return Ok(file);
                }
            }
            let name = format!("{stem}.commit");
            let reservation = self.bytes.disk.reserve(
                &self.bytes.objects_dir,
                &name,
                self.bytes.disk.bytes_for_regions(&[(0, identity.length)])?,
                true,
            )?;
            let directory = Arc::clone(&self.bytes.objects_dir);
            let temporary = blocking(move || TemporaryFile::create(directory)).await?;
            reservation.attach(&temporary.file)?;
            let mut digest = Sha256::new();
            let mut offset = 0;
            while offset < identity.length {
                let count = (identity.length - offset).min(chunk_bytes as u64) as usize;
                let payload = source.read_at(offset, count).await?;
                if payload.len() != count {
                    return invalid("seal source returned a short or oversized chunk");
                }
                let destination = reservation.pin_write(Arc::clone(&temporary.file));
                let validate = Arc::clone(&validator);
                digest = blocking(move || {
                    validate.validate_range(offset, &payload)?;
                    digest.update(&payload);
                    destination.write_all_at(&payload, offset)?;
                    Ok(digest)
                })
                .await?;
                offset += count as u64;
            }
            if format!("{:x}", digest.finalize()) != key.object_sha256 {
                return invalid("seal source whole-object SHA-256 differs from its identity");
            }
            let _io = io_lock.lock().await;
            let disk = Arc::clone(&self.bytes.disk);
            let verified = Arc::clone(&self.verified);
            blocking(move || {
                if temporary.file.metadata()?.len() != key.length {
                    return invalid("sealed file length differs from its identity");
                }
                validator.validate_file(&temporary.file)?;
                temporary
                    .file
                    .set_permissions(std::fs::Permissions::from_mode(0o400))?;
                temporary.file.sync_all()?;
                match open_file(&temporary.directory, &name, libc::O_RDONLY) {
                    Ok(file) => require_regular(&file)?,
                    Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                    Err(error) => return Err(error.into()),
                }
                rename_file(&temporary.directory, &temporary.name, &name)?;
                temporary.directory.sync_all()?;
                let readonly = disk.open_pinned(&temporary.directory, &name, libc::O_RDONLY)?;
                if readonly.metadata()?.ino() != temporary.file.metadata()?.ino() {
                    return Err(CacheError::IdentityConflict(
                        "published local object inode changed".into(),
                    ));
                }
                reservation.finish()?;
                lock(&verified)?.record(stem, FileSignature::from(&readonly.metadata()?), true);
                Ok(readonly)
            })
            .await
        }
    }

    fn canonical_key(key: &PhysicalCacheKey) -> Result<PhysicalCacheKey, CacheError> {
        key.validate()
            .map_err(|error| CacheError::Invalid(error.to_string()))?;
        if key.length > MAX_RANGE_BYTES
            || key.object_key.len() > 65536
            || key.namespace.as_ref().is_some_and(|v| v.len() > 65536)
            || key.object_version.as_ref().is_some_and(|v| v.len() > 65536)
        {
            return invalid("physical cache range or identity exceeds its bound");
        }
        let mut key = key.clone();
        key.object_sha256 = key
            .object_sha256
            .strip_prefix("sha256:")
            .unwrap_or(&key.object_sha256)
            .into();
        Ok(key)
    }

    fn object_stem(key: &PhysicalCacheKey) -> Result<String, CacheError> {
        let encoded = serde_json::to_vec(&(
            "physical-object-v1",
            &key.source_id,
            &key.namespace,
            &key.object_key,
            key.object_sha256
                .strip_prefix("sha256:")
                .unwrap_or(&key.object_sha256),
            &key.object_version,
        ))
        .map_err(|error| CacheError::Invalid(error.to_string()))?;
        Ok(hex_digest(&encoded))
    }

    fn range_name(key: &PhysicalCacheKey) -> Result<String, CacheError> {
        let encoded = serde_json::to_vec(&("physical-range-v1", key))
            .map_err(|error| CacheError::Invalid(error.to_string()))?;
        Ok(format!("{}.partial", hex_digest(&encoded)))
    }

    fn object_lock(
        locks: &ObjectLocks,
        stem: &str,
    ) -> Result<Arc<tokio::sync::Mutex<()>>, CacheError> {
        let mut locks = lock(locks)?;
        locks.retain(|_, entry| entry.strong_count() != 0);
        if let Some(existing) = locks.get(stem).and_then(Weak::upgrade) {
            return Ok(existing);
        }
        let entry = Arc::new(tokio::sync::Mutex::new(()));
        locks.insert(stem.into(), Arc::downgrade(&entry));
        Ok(entry)
    }

    fn open_complete(
        bytes: &LocalByteCache,
        verified: &Mutex<VerifiedObjects>,
        key: &PhysicalCacheKey,
    ) -> Result<Option<Arc<File>>, CacheError> {
        let stem = object_stem(key)?;
        let name = format!("{stem}.commit");
        let (file, before) =
            match bytes
                .disk
                .open_pinned_with_metadata(&bytes.objects_dir, &name, libc::O_RDONLY)
            {
                Ok(value) => value,
                Err(CacheError::Io(error)) if error.kind() == io::ErrorKind::NotFound => {
                    return Ok(None)
                }
                Err(error) => return Err(error),
            };
        if key.object_size.is_some_and(|size| size != before.len())
            || key
                .offset
                .checked_add(key.length)
                .is_none_or(|end| end > before.len())
        {
            return Ok(None);
        }
        let prior = lock(verified)?.objects.get(&stem).copied();
        let touched = FileSignature::from(&file.metadata()?);
        let valid = if let Some((signature, valid)) =
            prior.filter(|(signature, _)| *signature == FileSignature::from(&before))
        {
            let _ = signature;
            valid
        } else {
            let mut digest = Sha256::new();
            let mut at = 0;
            let mut buffer = vec![0; HASH_WINDOW];
            while at < before.len() {
                let count = (before.len() - at).min(buffer.len() as u64) as usize;
                file.read_exact_at(&mut buffer[..count], at)?;
                digest.update(&buffer[..count]);
                at += count as u64;
            }
            FileSignature::from(&file.metadata()?) == touched
                && format!("{:x}", digest.finalize()) == key.object_sha256
        };
        lock(verified)?.record(stem, FileSignature::from(&file.metadata()?), valid);
        if valid {
            Ok(Some(file))
        } else {
            drop(file);
            // A live upload pin may prevent removal; the negative memo still
            // quarantines that exact inode until it can be reclaimed safely.
            bytes.disk.remove(&bytes.objects_dir, &name)?;
            Ok(None)
        }
    }

    fn decode_range(file: &File, key: &PhysicalCacheKey) -> Result<Option<Vec<u8>>, CacheError> {
        let mut prefix = [0; 12];
        if file.read_exact_at(&mut prefix, 0).is_err() || &prefix[..8] != RANGE_MAGIC {
            return Ok(None);
        }
        let header_length =
            u32::from_le_bytes(prefix[8..].try_into().expect("four-byte header length")) as usize;
        if header_length == 0
            || header_length > MAX_HEADER_BYTES
            || file.metadata()?.len() != 12 + header_length as u64 + key.length
        {
            return Ok(None);
        }
        let mut header = vec![0; header_length];
        file.read_exact_at(&mut header, 12)?;
        let Ok(header) = serde_json::from_slice::<RangeHeader>(&header) else {
            return Ok(None);
        };
        if header.key != *key {
            return Ok(None);
        }
        let mut payload = vec![0; key.length as usize];
        file.read_exact_at(&mut payload, 12 + header_length as u64)?;
        Ok((hex_digest(&payload) == header.payload_sha256).then_some(payload))
    }

    fn io_error(error: CacheError) -> io::Error {
        match error {
            CacheError::Io(error) => error,
            other => io::Error::other(other),
        }
    }

    #[async_trait]
    impl PhysicalRangeCache for LocalFileCache {
        async fn read(&self, key: &PhysicalCacheKey) -> io::Result<Option<Vec<u8>>> {
            // Inclusive physical-cache wall time: lookup/locking, blocking-pool
            // admission, filesystem reads and verification. It never includes
            // a provider fetch or format decode.
            let timer = ReadPathTimer::start(ReadPathStage::CacheRead);
            let key = canonical_key(key).map_err(io_error)?;
            let io_lock = object_lock(&self.io_locks, &object_stem(&key).map_err(io_error)?)
                .map_err(io_error)?;
            let _io = io_lock.lock().await;
            let bytes = Arc::clone(&self.bytes);
            let verified = Arc::clone(&self.verified);
            let result = blocking(move || {
                if let Some(file) = open_complete(&bytes, &verified, &key)? {
                    let mut payload = vec![0; key.length as usize];
                    file.read_exact_at(&mut payload, key.offset)?;
                    return Ok(Some(payload));
                }
                let name = range_name(&key)?;
                let file = match bytes
                    .disk
                    .open_pinned(&bytes.objects_dir, &name, libc::O_RDONLY)
                {
                    Ok(file) => file,
                    Err(CacheError::Io(error)) if error.kind() == io::ErrorKind::NotFound => {
                        return Ok(None)
                    }
                    Err(error) => return Err(error),
                };
                let result = decode_range(&file, &key)?;
                drop(file);
                if result.is_none() {
                    bytes.disk.remove(&bytes.objects_dir, &name)?;
                }
                Ok(result)
            })
            .await
            .map_err(io_error);
            timer.finish(
                result
                    .as_ref()
                    .ok()
                    .and_then(|payload| payload.as_ref())
                    .map_or(0, |payload| payload.len() as u64),
                result.is_ok(),
            );
            result
        }

        async fn write(&self, key: &PhysicalCacheKey, payload: Arc<Vec<u8>>) -> io::Result<()> {
            // Includes cache lock/pool waits, hashing, quota and atomic rename;
            // this is advisory-cache wall time, not a durability flush timer.
            let timer = ReadPathTimer::start(ReadPathStage::CacheWrite);
            let payload_bytes = payload.len() as u64;
            let key = canonical_key(key).map_err(io_error)?;
            if payload.len() as u64 != key.length {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "physical cache write must exactly cover its range",
                ));
            }
            let io_lock = object_lock(&self.io_locks, &object_stem(&key).map_err(io_error)?)
                .map_err(io_error)?;
            let _io = io_lock.lock().await;
            let bytes = Arc::clone(&self.bytes);
            let result = blocking(move || {
                let name = range_name(&key)?;
                let header = serde_json::to_vec(&RangeHeader {
                    key,
                    payload_sha256: hex_digest(&payload),
                })
                .map_err(|error| CacheError::Invalid(error.to_string()))?;
                if header.len() > MAX_HEADER_BYTES {
                    return invalid("physical cache header exceeds its bound");
                }
                let length = 12 + header.len() as u64 + payload.len() as u64;
                let reservation = bytes.disk.reserve(
                    &bytes.objects_dir,
                    &name,
                    bytes.disk.bytes_for_regions(&[(0, length)])?,
                    true,
                )?;
                let temporary = TemporaryFile::create(Arc::clone(&bytes.objects_dir))?;
                reservation.attach(&temporary.file)?;
                temporary.file.write_all_at(RANGE_MAGIC, 0)?;
                temporary
                    .file
                    .write_all_at(&(header.len() as u32).to_le_bytes(), 8)?;
                temporary.file.write_all_at(&header, 12)?;
                // The provider allocation is borrowed directly. Do not clone DATA
                // or concatenate it with the small cache envelope header.
                for (part, chunk) in payload.chunks(HASH_WINDOW).enumerate() {
                    temporary.file.write_all_at(
                        chunk,
                        12 + header.len() as u64 + (part * HASH_WINDOW) as u64,
                    )?;
                }
                temporary
                    .file
                    .set_permissions(std::fs::Permissions::from_mode(0o400))?;
                match open_file(&temporary.directory, &name, libc::O_RDONLY) {
                    Ok(file) => require_regular(&file)?,
                    Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                    Err(error) => return Err(error.into()),
                }
                // Disposable ranges are checked on every hit. As with the old
                // partial cache, avoid a durability flush for every guest fault.
                rename_file(&temporary.directory, &temporary.name, &name)?;
                reservation.finish()?;
                Ok(())
            })
            .await
            .map_err(io_error);
            timer.finish(
                if result.is_ok() { payload_bytes } else { 0 },
                result.is_ok(),
            );
            result
        }

        async fn invalidate(&self, key: &PhysicalCacheKey) -> io::Result<()> {
            let key = canonical_key(key).map_err(io_error)?;
            let stem = object_stem(&key).map_err(io_error)?;
            let io_lock = object_lock(&self.io_locks, &stem).map_err(io_error)?;
            let _io = io_lock.lock().await;
            let bytes = Arc::clone(&self.bytes);
            let verified = Arc::clone(&self.verified);
            blocking(move || {
                let name = format!("{stem}.commit");
                match bytes
                    .disk
                    .open_pinned(&bytes.objects_dir, &name, libc::O_RDONLY)
                {
                    Ok(file) => {
                        lock(&verified)?.record(stem, FileSignature::from(&file.metadata()?), false)
                    }
                    Err(CacheError::Io(error)) if error.kind() == io::ErrorKind::NotFound => {}
                    Err(error) => return Err(error),
                }
                bytes.disk.remove(&bytes.objects_dir, &name)?;
                bytes.disk.remove(&bytes.objects_dir, &range_name(&key)?)?;
                Ok(())
            })
            .await
            .map_err(io_error)
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use std::pin::Pin;
        use std::sync::atomic::{AtomicUsize, Ordering};

        use crate::snapshot::transport::ObjectStoreError;

        struct TestRoot(PathBuf);
        impl TestRoot {
            fn new() -> Self {
                Self(
                    std::fs::canonicalize(std::env::temp_dir())
                        .unwrap()
                        .join(format!("generic-byte-cache-{}", uuid::Uuid::new_v4())),
                )
            }
            fn cache(&self) -> Arc<LocalFileCache> {
                LocalFileCache::new_with_source_and_limit(
                    self.0.clone(),
                    "disk-domain",
                    "physical-source",
                    DEFAULT_MAX_BYTES,
                )
                .unwrap()
            }
        }
        impl Drop for TestRoot {
            fn drop(&mut self) {
                let _ = std::fs::remove_dir_all(&self.0);
            }
        }

        struct BytesSource {
            bytes: Vec<u8>,
            calls: AtomicUsize,
        }
        impl BytesSource {
            fn new(bytes: Vec<u8>) -> Arc<Self> {
                Arc::new(Self {
                    bytes,
                    calls: AtomicUsize::new(0),
                })
            }
        }
        impl ReplayableByteSource for BytesSource {
            fn length(&self) -> u64 {
                self.bytes.len() as u64
            }
            fn read_at<'a>(
                &'a self,
                offset: u64,
                count: usize,
            ) -> Pin<
                Box<
                    dyn std::future::Future<Output = Result<Vec<u8>, ObjectStoreError>> + Send + 'a,
                >,
            > {
                Box::pin(async move {
                    self.calls.fetch_add(1, Ordering::SeqCst);
                    Ok(self.bytes[offset as usize..offset as usize + count].to_vec())
                })
            }
        }

        struct OpaqueValidator;
        impl SealValidator for OpaqueValidator {
            fn validate_range(&self, _: u64, _: &[u8]) -> Result<(), CacheError> {
                Ok(())
            }
            fn validate_file(&self, _: &File) -> Result<(), CacheError> {
                Ok(())
            }
        }

        fn identity(bytes: &[u8]) -> ImmutableObjectIdentity {
            ImmutableObjectIdentity {
                object_key: "arbitrary/future-format.bin".into(),
                sha256: format!("sha256:{}", hex_digest(bytes)),
                length: bytes.len() as u64,
            }
        }

        #[tokio::test]
        async fn arbitrary_physical_ranges_survive_restart_and_keep_all_source_identity_fields() {
            let root = TestRoot::new();
            let cache = root.cache();
            let bytes = vec![0x5a; 8192];
            let object = identity(&bytes);
            let mut key = cache.physical_key(&object, 31, 1024).unwrap();
            key.namespace = Some("repository".into());
            key.object_version = Some("version-one".into());
            let payload = &bytes[31..1055];
            assert!(cache.read(&key).await.unwrap().is_none());
            cache.write(&key, Arc::new(payload.to_vec())).await.unwrap();
            assert_eq!(cache.read(&key).await.unwrap().unwrap(), payload);
            drop(cache);
            let restarted = root.cache();
            assert_eq!(restarted.read(&key).await.unwrap().unwrap(), payload);
            for changed in [
                PhysicalCacheKey {
                    source_id: "another-source".into(),
                    ..key.clone()
                },
                PhysicalCacheKey {
                    namespace: Some("another-repository".into()),
                    ..key.clone()
                },
                PhysicalCacheKey {
                    object_key: "another/key".into(),
                    ..key.clone()
                },
                PhysicalCacheKey {
                    object_sha256: "a".repeat(64),
                    ..key.clone()
                },
                PhysicalCacheKey {
                    object_version: Some("version-two".into()),
                    ..key.clone()
                },
                PhysicalCacheKey {
                    object_size: Some(16384),
                    ..key.clone()
                },
                PhysicalCacheKey {
                    offset: 32,
                    ..key.clone()
                },
            ] {
                assert!(restarted.read(&changed).await.unwrap().is_none());
            }
            assert!(restarted
                .read(&PhysicalCacheKey {
                    object_sha256: "bad".into(),
                    ..key
                })
                .await
                .is_err());
        }

        #[tokio::test]
        async fn physical_write_releases_the_provider_allocation_before_returning() {
            let root = TestRoot::new();
            let cache = root.cache();
            let payload = Arc::new(vec![0x53; 8192]);
            let pointer = payload.as_ptr();
            let key = cache
                .physical_key(&identity(&payload), 0, payload.len() as u64)
                .unwrap();
            cache.write(&key, Arc::clone(&payload)).await.unwrap();
            let recovered =
                Arc::try_unwrap(payload).expect("cache must not retain the provider buffer");
            assert_eq!(recovered.as_ptr(), pointer);
            assert_eq!(cache.read(&key).await.unwrap().unwrap(), recovered);
        }

        #[tokio::test]
        async fn corrupted_physical_range_is_a_miss_and_can_be_repaired_without_format_metadata() {
            let root = TestRoot::new();
            let cache = root.cache();
            let bytes = vec![0x7a; 4096];
            let key = cache.physical_key(&identity(&bytes), 0, 4096).unwrap();
            cache.write(&key, Arc::new(bytes.clone())).await.unwrap();
            let name = range_name(&key).unwrap();
            // Deliberate corruption must also work when tests run without root.
            let readonly = open_file(&cache.bytes.objects_dir, &name, libc::O_RDONLY).unwrap();
            readonly
                .set_permissions(std::fs::Permissions::from_mode(0o600))
                .unwrap();
            drop(readonly);
            let file = open_file(&cache.bytes.objects_dir, &name, libc::O_RDWR).unwrap();
            file.write_all_at(&[0], file.metadata().unwrap().len() - 1)
                .unwrap();
            drop(file);
            assert!(cache.read(&key).await.unwrap().is_none());
            cache.write(&key, Arc::new(bytes.clone())).await.unwrap();
            assert_eq!(cache.read(&key).await.unwrap().unwrap(), bytes);
            cache.invalidate(&key).await.unwrap();
            assert!(cache.read(&key).await.unwrap().is_none());
        }

        #[tokio::test]
        async fn sealed_physical_object_reuses_readonly_fd_and_serves_subranges_without_a_descriptor(
        ) {
            let root = TestRoot::new();
            let cache = root.cache();
            let bytes = (0..20000)
                .map(|index| (index % 251) as u8)
                .collect::<Vec<_>>();
            let object = identity(&bytes);
            let source = BytesSource::new(bytes.clone());
            let file = cache
                .seal_blob(&object, source.clone(), 4096, Arc::new(OpaqueValidator))
                .await
                .unwrap();
            assert_eq!(file.metadata().unwrap().mode() & 0o777, 0o400);
            assert!(file.write_at(b"x", 0).is_err());
            let before = source.calls.load(Ordering::SeqCst);
            let same = cache
                .seal_blob(&object, source.clone(), 4096, Arc::new(OpaqueValidator))
                .await
                .unwrap();
            assert_eq!(
                file.metadata().unwrap().ino(),
                same.metadata().unwrap().ino()
            );
            assert_eq!(source.calls.load(Ordering::SeqCst), before);
            let mut key = cache.physical_key(&object, 123, 1024).unwrap();
            key.object_size = None; // Object-store descriptors may omit total physical size.
            assert_eq!(cache.read(&key).await.unwrap().unwrap(), bytes[123..1147]);
            assert!(cache
                .read(&PhysicalCacheKey {
                    object_size: Some(20001),
                    ..key.clone()
                })
                .await
                .unwrap()
                .is_none());
            assert!(cache
                .read(&PhysicalCacheKey {
                    source_id: "other-source".into(),
                    ..key.clone()
                })
                .await
                .unwrap()
                .is_none());
            drop(same);
            drop(file);
            drop(cache);
            let restarted = root.cache();
            assert_eq!(
                restarted.read(&key).await.unwrap().unwrap(),
                bytes[123..1147]
            );
        }

        #[tokio::test]
        async fn invalidating_a_pinned_complete_object_quarantines_it_until_a_new_publication() {
            let root = TestRoot::new();
            let cache = root.cache();
            let bytes = vec![0x2d; 8192];
            let object = identity(&bytes);
            let file = cache
                .seal_blob(
                    &object,
                    BytesSource::new(bytes.clone()),
                    4096,
                    Arc::new(OpaqueValidator),
                )
                .await
                .unwrap();
            let key = cache.physical_key(&object, 0, 4096).unwrap();
            assert!(cache.read(&key).await.unwrap().is_some());
            cache.invalidate(&key).await.unwrap();
            assert!(file.metadata().unwrap().len() == object.length); // Upload's FD stays valid.
            assert!(cache.read(&key).await.unwrap().is_none());
            drop(file);
            cache
                .seal_blob(
                    &object,
                    BytesSource::new(bytes),
                    4096,
                    Arc::new(OpaqueValidator),
                )
                .await
                .unwrap();
            assert!(cache.read(&key).await.unwrap().is_some());
        }

        struct RejectFile;
        impl SealValidator for RejectFile {
            fn validate_range(&self, _: u64, _: &[u8]) -> Result<(), CacheError> {
                Ok(())
            }
            fn validate_file(&self, _: &File) -> Result<(), CacheError> {
                invalid("selected format rejected its inner metadata")
            }
        }

        #[tokio::test]
        async fn whole_sha_and_actual_format_validator_are_both_required_before_seal_publication() {
            let root = TestRoot::new();
            let cache = root.cache();
            let bytes = vec![0x38; 8192];
            let object = identity(&bytes);
            let key = cache.physical_key(&object, 0, 4096).unwrap();
            assert!(cache
                .seal_blob(
                    &object,
                    BytesSource::new(bytes.clone()),
                    4096,
                    Arc::new(RejectFile)
                )
                .await
                .is_err());
            assert!(cache.read(&key).await.unwrap().is_none());
            let wrong = ImmutableObjectIdentity {
                sha256: format!("sha256:{}", "a".repeat(64)),
                ..object.clone()
            };
            assert!(cache
                .seal_blob(
                    &wrong,
                    BytesSource::new(bytes.clone()),
                    4096,
                    Arc::new(OpaqueValidator)
                )
                .await
                .is_err());
            assert!(cache
                .seal_blob(
                    &object,
                    BytesSource::new(bytes[..4096].to_vec()),
                    4096,
                    Arc::new(OpaqueValidator)
                )
                .await
                .is_err());
            assert_eq!(cache.stats().unwrap().staging_bytes, 0);
        }

        #[tokio::test]
        async fn generic_cache_reuses_one_disk_lru_and_keeps_capacity_bypass_before_source_io() {
            let root = TestRoot::new();
            let bytes =
                Arc::new(LocalByteCache::new(root.0.clone(), "shared-domain", 4096).unwrap());
            let first = LocalFileCache::from_byte_cache(Arc::clone(&bytes), "first").unwrap();
            let second = LocalFileCache::from_byte_cache(Arc::clone(&bytes), "second").unwrap();
            assert!(Arc::ptr_eq(
                &first.byte_cache().disk,
                &second.byte_cache().disk
            ));
            let payload = vec![0x69; 8192];
            let source = BytesSource::new(payload.clone());
            let result = first
                .seal_blob(
                    &identity(&payload),
                    source.clone(),
                    4096,
                    Arc::new(OpaqueValidator),
                )
                .await;
            assert!(matches!(result, Err(CacheError::Capacity(_))));
            assert_eq!(source.calls.load(Ordering::SeqCst), 0);
            assert_eq!(second.stats().unwrap().max_bytes, 4096);
            assert!(second.stats().unwrap().bypasses > 0);
        }
    }
}
