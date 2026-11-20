//! Fixed-unit native dependencies, recoverable residency and guarded access.

use super::*;
use std::collections::BTreeMap;
use std::os::unix::fs::FileExt;

use crate::snapshot::repository::frozen_layer::{
    FrozenLayerMetadata, FrozenLayerPin, PackedOwnershipMap,
};

#[derive(Clone, Debug)]
pub struct NativeBackingMetadata {
    pub descriptor: FrozenLayerMetadata,
    pub ownership: PackedOwnershipMap,
}

/// The installed capability owns the complete sealed object and authenticated
/// format metadata. A native page may never be punched without this owner.
#[async_trait::async_trait]
pub trait BackingRecovery: std::fmt::Debug + Send + Sync {
    async fn read_data(&self, offset: u64, length: u64) -> Result<Vec<u8>, BackingError>;
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct DependencyCounts {
    pub shared: u64,
    pub cow: u64,
    pub exited: u64,
}

impl DependencyCounts {
    pub fn remaining(self) -> u64 {
        self.shared - self.cow - self.exited
    }
}

/// Unit ranges are half-open. Intervals avoid a bitmap or counter allocation
/// proportional to a sparse multi-TiB logical block device.
#[derive(Clone, Debug, Default)]
struct Ranges(BTreeMap<u64, u64>);

impl Ranges {
    /// Append ranges that are already ordered, avoiding repeated sorting when
    /// walking a packed ownership map or an existing range set.
    fn push_sorted(&mut self, start: u64, end: u64) {
        if start == end {
            return;
        }
        if let Some(mut last) = self.0.last_entry() {
            if start <= *last.get() {
                *last.get_mut() = (*last.get()).max(end);
                return;
            }
        }
        self.0.insert(start, end);
    }

    fn insert(&mut self, mut start: u64, mut end: u64) {
        if start == end {
            return;
        }
        if let Some((&left, &right)) = self.0.range(..=start).next_back() {
            if right >= start {
                start = left;
                end = end.max(right);
            }
        }
        let merged = self
            .0
            .range(start..=end)
            .map(|(&left, &right)| (left, right))
            .collect::<Vec<_>>();
        for (left, right) in merged {
            self.0.remove(&left);
            end = end.max(right);
        }
        self.0.insert(start, end);
    }

    fn remove(&mut self, start: u64, end: u64) -> Vec<(u64, u64)> {
        let overlaps = self.overlapping(start, end).collect::<Vec<_>>();
        let mut removed = Vec::new();
        for (left, right) in overlaps {
            self.0.remove(&left);
            removed.push((left.max(start), right.min(end)));
            if left < start {
                self.0.insert(left, start);
            }
            if right > end {
                self.0.insert(end, right);
            }
        }
        removed
    }

    fn overlapping(&self, start: u64, end: u64) -> impl Iterator<Item = (u64, u64)> + '_ {
        let first = self
            .0
            .range(..=start)
            .next_back()
            .map_or(start, |(&left, _)| left);
        self.0
            .range(first..end)
            .filter_map(move |(&left, &right)| (right > start).then_some((left, right)))
    }

    fn difference(&self, start: u64, end: u64) -> Vec<(u64, u64)> {
        let mut result = Vec::new();
        let mut cursor = start;
        if let Some((_, &right)) = self.0.range(..=start).next_back() {
            cursor = cursor.max(right).min(end);
        }
        for (&left, &right) in self.0.range(start..end) {
            if left > cursor {
                result.push((cursor, left));
            }
            cursor = cursor.max(right).min(end);
        }
        if cursor < end {
            result.push((cursor, end));
        }
        result
    }

    fn overlaps(&self, start: u64, end: u64) -> bool {
        self.0
            .range(..end)
            .next_back()
            .is_some_and(|(_, &right)| start < right)
    }
}

#[derive(Debug, Default)]
struct Consumer {
    registered: Ranges,
    remaining: Ranges,
}

#[derive(Debug)]
pub(super) struct NativeState {
    consumers: HashMap<String, Consumer>,
    retired: HashSet<String>,
    counts: BTreeMap<u64, DependencyCounts>,
    reclaimed: Ranges,
    pending_reclaim: Ranges,
    discovered_native_data: bool,
    readers: usize,
    recovery: Option<Arc<dyn BackingRecovery>>,
}

impl Default for NativeState {
    fn default() -> Self {
        Self {
            consumers: HashMap::new(),
            retired: HashSet::new(),
            counts: BTreeMap::from([(0, DependencyCounts::default())]),
            reclaimed: Ranges::default(),
            pending_reclaim: Ranges::default(),
            discovered_native_data: false,
            readers: 0,
            recovery: None,
        }
    }
}

impl NativeState {
    fn adjust(&mut self, start: u64, end: u64, reason: u8) {
        for point in [start, end] {
            let value = *self
                .counts
                .range(..=point)
                .next_back()
                .expect("zero boundary")
                .1;
            self.counts.entry(point).or_insert(value);
        }
        for (_, count) in self.counts.range_mut(start..end) {
            match reason {
                0 => {
                    count.shared = count
                        .shared
                        .checked_add(1)
                        .expect("consumer count overflow")
                }
                1 => count.cow += 1,
                2 => count.exited += 1,
                _ => unreachable!(),
            }
            debug_assert!(count.shared >= count.cow + count.exited);
        }
        // A uniform increment preserves differences inside the range. Only
        // the two edited boundaries can now equal their predecessor.
        for point in [start, end] {
            if point != 0
                && self
                    .counts
                    .range(..point)
                    .next_back()
                    .map(|(_, count)| *count)
                    == self.counts.get(&point).copied()
            {
                self.counts.remove(&point);
            }
        }
    }
}

#[derive(Debug)]
pub struct NativeReadLease {
    owner: LocalBackingHandle,
    file: File,
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;

    #[derive(Debug)]
    struct Recovery {
        bytes: Vec<u8>,
        reads: Arc<AtomicUsize>,
    }

    #[async_trait::async_trait]
    impl BackingRecovery for Recovery {
        async fn read_data(&self, offset: u64, length: u64) -> Result<Vec<u8>, BackingError> {
            self.reads.fetch_add(1, Ordering::Relaxed);
            Ok(self.bytes[offset as usize..(offset + length) as usize].to_vec())
        }
    }

    fn captured(kind: LayerKind, units: u64) -> (LocalBackingHandle, Vec<u8>, Arc<AtomicUsize>) {
        captured_states(kind, &vec![1; units as usize])
    }

    fn captured_states(
        kind: LayerKind,
        ownership: &[u8],
    ) -> (LocalBackingHandle, Vec<u8>, Arc<AtomicUsize>) {
        let units = ownership.len() as u64;
        let unit = match kind {
            LayerKind::Memory => 4096,
            LayerKind::Block => 65536,
        };
        let bytes = (0..units * unit)
            .map(|offset| {
                if ownership[(offset / unit) as usize] == 1 {
                    ((offset / unit) % 251 + 1) as u8
                } else {
                    0
                }
            })
            .collect::<Vec<_>>();
        let file = posix_file::memory_file(c"native-dependency-test", units * unit).unwrap();
        file.write_all_at(&bytes, 0).unwrap();
        let kind = match kind {
            LayerKind::Memory => crate::snapshot::metadata::LayerKind::Memory,
            LayerKind::Block => crate::snapshot::metadata::LayerKind::Block,
        };
        let mut states = vec![0_u8; units.div_ceil(4) as usize];
        for index in 0..units {
            states[(index / 4) as usize] |= ownership[index as usize] << ((index % 4) * 2);
        }
        let pin = FrozenLayerPin::new(
            FrozenLayerMetadata {
                kind,
                layer_id: "native-layer".into(),
                provisional_content_id: "capture-stable-native".into(),
                logical_size: units * unit,
                alignment: unit,
            },
            PackedOwnershipMap {
                kind,
                snapshot_id: Uuid::new_v4().to_string(),
                format_version: 1,
                logical_size: units * unit,
                granularity: unit,
                packed_states: states,
            },
            file.into(),
            false,
        );
        (
            LocalBackingHandle::from_native(pin).unwrap(),
            bytes,
            Arc::new(AtomicUsize::new(0)),
        )
    }

    fn recoverable(backing: &Arc<LocalBacking>, bytes: Vec<u8>, reads: Arc<AtomicUsize>) {
        backing.install_recovery(Arc::new(Recovery { bytes, reads }));
    }

    #[test]
    fn fragmented_ranges_match_independent_unit_coverage() {
        let mut ranges = Ranges::default();
        let mut covered = [false; 257];
        let mut random = 0x87a9_b354_d02f_6781_u64;
        for operation in 0..1024 {
            random = random.wrapping_mul(6364136223846793005).wrapping_add(1);
            let start = (random >> 32) as usize % covered.len();
            let end = (start + 1 + (random as usize % 31)).min(covered.len());
            if operation % 3 == 0 {
                ranges.remove(start as u64, end as u64);
                covered[start..end].fill(false);
            } else {
                ranges.insert(start as u64, end as u64);
                covered[start..end].fill(true);
            }
            let missing = ranges.difference(0, covered.len() as u64);
            for (unit, &expected) in covered.iter().enumerate() {
                assert_eq!(ranges.overlaps(unit as u64, unit as u64 + 1), expected);
                assert_eq!(
                    missing
                        .iter()
                        .any(|&(left, right)| left <= unit as u64 && (unit as u64) < right),
                    !expected
                );
            }
        }
    }

    #[tokio::test]
    async fn fragmented_cow_exit_and_new_consumer_preserve_counter_boundaries() {
        const UNITS: u64 = 1024;
        const UNIT: u64 = 4096;
        let (handle, bytes, reads) = captured(LayerKind::Memory, UNITS);
        let backing = &handle.backing;
        for consumer in ["a", "b"] {
            backing
                .register_consumer(consumer, &[(0, UNITS * UNIT)])
                .unwrap();
        }
        for step in (0..UNITS).step_by(2) {
            let offset = (step * 73 % UNITS) * UNIT;
            assert_eq!(
                backing
                    .release_consumer_ranges("a", &[(offset, UNIT)])
                    .unwrap(),
                1
            );
            assert_eq!(
                backing
                    .release_consumer_ranges("a", &[(offset, UNIT)])
                    .unwrap(),
                0
            );
        }
        // Reattaching the same incarnation does not reacquire units that have
        // already COWed into that runtime's private generation.
        backing
            .register_consumer("a", &[(0, UNITS * UNIT)])
            .unwrap();
        assert_eq!(backing.release_consumer("b"), UNITS);
        for unit in 0..UNITS {
            let count = backing.dependency_counts(unit * UNIT).unwrap();
            assert_eq!(
                (count.shared, count.cow, count.exited, count.remaining()),
                (2, u64::from(unit % 2 == 0), 1, u64::from(unit % 2 != 0))
            );
        }
        backing
            .register_consumer("c", &[(0, UNITS * UNIT)])
            .unwrap();
        assert_eq!(backing.release_consumer("a"), UNITS / 2);
        for step in 0..UNITS {
            let offset = (step * 73 % UNITS) * UNIT;
            assert_eq!(
                backing
                    .release_consumer_ranges("c", &[(offset, UNIT)])
                    .unwrap(),
                1
            );
        }
        for unit in 0..UNITS {
            let count = backing.dependency_counts(unit * UNIT).unwrap();
            assert_eq!(
                (count.shared, count.cow, count.exited, count.remaining()),
                (
                    3,
                    1 + u64::from(unit % 2 == 0),
                    1 + u64::from(unit % 2 != 0),
                    0
                )
            );
        }
        recoverable(backing, bytes.clone(), reads);
        assert_eq!(
            backing.reclaim_unused().await.unwrap(),
            vec![(0, UNITS * UNIT)]
        );
        let pin = backing.restore_and_pin_native_reader().await.unwrap();
        let mut restored = vec![0; bytes.len()];
        pin.file().read_exact_at(&mut restored, 0).unwrap();
        assert_eq!(restored, bytes);
    }

    #[tokio::test]
    async fn only_last_runtime_cow_releases_a_unit_and_duplicate_notices_are_noops() {
        let (handle, bytes, reads) = captured(LayerKind::Memory, 4);
        let backing = &handle.backing;
        for consumer in ["runtime-a", "runtime-b"] {
            backing.register_consumer(consumer, &[(0, 16384)]).unwrap();
        }
        assert_eq!(
            backing
                .release_consumer_ranges("runtime-a", &[(4096, 8192)])
                .unwrap(),
            2
        );
        assert_eq!(
            backing
                .release_consumer_ranges("runtime-a", &[(4096, 8192)])
                .unwrap(),
            0
        );
        recoverable(backing, bytes.clone(), reads.clone());
        assert!(backing.reclaim_unused().await.unwrap().is_empty());
        assert_eq!(backing.dependency_counts(4096).unwrap().remaining(), 1);
        assert_eq!(
            backing
                .release_consumer_ranges("runtime-b", &[(4096, 8192)])
                .unwrap(),
            2
        );
        assert_eq!(backing.reclaim_unused().await.unwrap(), vec![(4096, 8192)]);
        assert_eq!(
            backing.read_data(4096, 8192).await.unwrap(),
            bytes[4096..12288]
        );
        assert_eq!(reads.load(Ordering::Relaxed), 1);
        let counts = backing.dependency_counts(4096).unwrap();
        assert_eq!(
            (counts.shared, counts.cow, counts.exited, counts.remaining()),
            (2, 2, 0, 0)
        );
    }

    #[tokio::test]
    async fn exit_and_rollback_release_each_remaining_unit_once() {
        let (handle, bytes, reads) = captured(LayerKind::Memory, 2);
        let backing = &handle.backing;
        backing.register_consumer("runtime", &[(0, 8192)]).unwrap();
        backing
            .release_consumer_ranges("runtime", &[(0, 4096)])
            .unwrap();
        assert_eq!(backing.release_consumer("runtime"), 1);
        assert_eq!(backing.release_consumer("runtime"), 0);
        assert!(backing.register_consumer("runtime", &[(0, 8192)]).is_err());
        recoverable(backing, bytes, reads);
        assert_eq!(backing.reclaim_unused().await.unwrap(), vec![(0, 8192)]);
        assert_eq!(backing.dependency_counts(0).unwrap().cow, 1);
        assert_eq!(backing.dependency_counts(4096).unwrap().exited, 1);
    }

    #[tokio::test]
    async fn no_recovery_or_inflight_reader_means_no_hole_punch() {
        let (handle, bytes, reads) = captured(LayerKind::Memory, 1);
        let backing = &handle.backing;
        assert!(backing.reclaim_unused().await.unwrap().is_empty());
        let pin = backing.pin_native_reader().unwrap();
        recoverable(backing, bytes.clone(), reads);
        assert!(backing.reclaim_unused().await.unwrap().is_empty());
        let mut original = vec![0; 4096];
        pin.file().read_exact_at(&mut original, 0).unwrap();
        assert_eq!(original, bytes);
        drop(pin);
        backing.reclaim_unused().await.unwrap();
        assert!(backing.has_reclaimed_data());
    }

    #[tokio::test]
    async fn ownership_excludes_zero_and_inherit_and_export_pin_preserves_restored_data() {
        let (handle, bytes, reads) = captured_states(LayerKind::Memory, &[0, 1, 2, 1]);
        let backing = &handle.backing;
        recoverable(backing, bytes.clone(), reads.clone());
        assert_eq!(
            backing.reclaim_unused().await.unwrap(),
            vec![(4096, 4096), (12288, 4096)]
        );
        let pin = backing.restore_and_pin_native_reader().await.unwrap();
        assert!(!backing.has_reclaimed_data());
        assert!(backing.reclaim_unused().await.unwrap().is_empty());
        let mut restored = vec![0; bytes.len()];
        pin.file().read_exact_at(&mut restored, 0).unwrap();
        assert_eq!(restored, bytes);
        assert_eq!(reads.load(Ordering::Relaxed), 2);
    }

    #[tokio::test]
    async fn memfd_reclaim_works_with_an_existing_shared_mapping() {
        let (handle, bytes, reads) = captured(LayerKind::Memory, 1);
        let backing = &handle.backing;
        let file = backing.try_clone_memory_mapping_file().unwrap();
        // This mapping deliberately outlives the runtime's dependency. A live
        // dependency would prevent the punch, even when the reader is idle.
        let address = unsafe {
            libc::mmap(
                std::ptr::null_mut(),
                bytes.len(),
                libc::PROT_READ,
                libc::MAP_SHARED,
                file.as_raw_fd(),
                0,
            )
        };
        assert_ne!(address, libc::MAP_FAILED);
        struct Mapping(*mut libc::c_void, usize);
        impl Drop for Mapping {
            fn drop(&mut self) {
                assert_eq!(unsafe { libc::munmap(self.0, self.1) }, 0);
            }
        }
        let mapping = Mapping(address, bytes.len());
        assert_eq!(
            unsafe { std::ptr::read_volatile(mapping.0.cast::<u8>()) },
            1
        );
        recoverable(backing, bytes, reads);
        assert_eq!(backing.reclaim_unused().await.unwrap(), vec![(0, 4096)]);
        assert_eq!(file.metadata().unwrap().blocks(), 0);
        let _pin = backing.restore_and_pin_native_reader().await.unwrap();
        assert_eq!(
            unsafe { std::ptr::read_volatile(mapping.0.cast::<u8>()) },
            1
        );
    }

    #[tokio::test]
    async fn cancelled_request_reclaims_after_background_load_publishes_residency() {
        let (handle, bytes, reads) = captured(LayerKind::Memory, 1);
        let backing = &handle.backing;
        backing.register_consumer("runtime", &[(0, 4096)]).unwrap();
        recoverable(backing, bytes.clone(), reads);
        let key = DataRangeKey::new(&backing.backing_id, 0, 4096).unwrap();
        let request_backing = Arc::clone(backing);
        let request_key = key.clone();
        let payload = bytes.clone();
        let (started, wait_started) = tokio::sync::oneshot::channel();
        let (finish, wait_finish) = tokio::sync::oneshot::channel();
        let request = tokio::spawn(async move {
            let writer = Arc::clone(&request_backing);
            request_backing
                .downloads
                .ensure_loaded(request_key, move || async move {
                    let _pin = writer.pin_native_io().unwrap();
                    started.send(()).unwrap();
                    wait_finish.await.unwrap();
                    writer.try_clone_file().unwrap().write_all_at(&payload, 0)
                })
                .await
        });
        wait_started.await.unwrap();
        backing.release_consumer("runtime");
        assert!(backing.reclaim_unused().await.unwrap().is_empty());
        request.abort();
        finish.send(()).unwrap();
        tokio::time::timeout(std::time::Duration::from_secs(2), async {
            loop {
                if backing.downloads.status(&key).await.state == DownloadState::NotResident
                    && backing
                        .try_clone_file()
                        .unwrap()
                        .metadata()
                        .unwrap()
                        .blocks()
                        == 0
                {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("completed worker must retry native reclamation after publication");
        assert_eq!(backing.read_data(0, 4096).await.unwrap(), bytes);
    }

    #[tokio::test]
    async fn partial_block_reclaim_invalidates_overlapping_sixteen_mib_download() {
        let (handle, bytes, reads) = captured(LayerKind::Block, 256);
        let backing = &handle.backing;
        backing
            .register_consumer("runtime", &[(0, 16 * 1024 * 1024)])
            .unwrap();
        let big = DataRangeKey::new(backing.backing_id.clone(), 0, 16 * 1024 * 1024).unwrap();
        backing
            .downloads
            .ensure_loaded(big.clone(), || async { Ok::<_, io::Error>(()) })
            .await
            .unwrap();
        backing
            .release_consumer_ranges("runtime", &[(65536, 65536)])
            .unwrap();
        recoverable(backing, bytes.clone(), reads);
        assert_eq!(
            backing.reclaim_unused().await.unwrap(),
            vec![(65536, 65536)]
        );
        assert_eq!(
            backing.downloads.status(&big).await.state,
            DownloadState::NotResident
        );
        backing
            .register_consumer("fork", &[(65536, 65536)])
            .unwrap();
        backing.restore_ranges(&[(65536, 65536)]).await.unwrap();
        assert_eq!(
            backing.read_data(65536, 65536).await.unwrap(),
            bytes[65536..131072]
        );
        assert_eq!(backing.dependency_counts(65536).unwrap().remaining(), 1);
    }

    #[tokio::test]
    async fn promotion_retains_native_inode_and_one_dependency_ledger() {
        let (handle, _, _) = captured(LayerKind::Memory, 1);
        handle
            .backing
            .register_consumer("runtime", &[(0, 4096)])
            .unwrap();
        let directory = tempfile::tempdir().unwrap();
        let registry = LocalBackingRegistry::new(directory.path(), "epoch").unwrap();
        let key = LayerBackingIdentity {
            kind: LayerKind::Memory,
            format_version: 1,
            logical_size: 4096,
            unit_size: 4096,
            content_sha256: "1".repeat(64),
        };
        let readonly = handle.backing.try_clone_readonly_file().unwrap();
        let promoted = registry
            .seed_readonly_or_acquire_existing(key.clone(), &readonly)
            .await
            .unwrap();
        assert!(Arc::ptr_eq(&handle.backing, &promoted.backing));
        assert_eq!(
            promoted.backing.dependency_counts(0).unwrap().remaining(),
            1
        );
        let acquired = registry.acquire(key).await.unwrap();
        assert!(Arc::ptr_eq(&handle.backing, &acquired.backing));
        let backing = Arc::clone(&handle.backing);
        let runtime = backing.lease();
        drop(acquired);
        drop(promoted);
        drop(handle);
        assert!(!backing.is_closed());
        drop(runtime);
        assert!(backing.is_closed());
    }

    #[tokio::test]
    async fn register_fork_and_reclaim_are_serialized_without_losing_history() {
        let (handle, bytes, reads) = captured(LayerKind::Memory, 2);
        recoverable(&handle.backing, bytes.clone(), reads);
        for generation in 0..16 {
            let consumer = format!("fork-{generation}");
            let other = Arc::clone(&handle.backing);
            let reclaim = tokio::spawn(async move { other.reclaim_unused().await.unwrap() });
            handle
                .backing
                .register_consumer(&consumer, &[(0, 8192)])
                .unwrap();
            handle.backing.restore_ranges(&[(0, 8192)]).await.unwrap();
            reclaim.await.unwrap();
            assert_eq!(handle.backing.read_data(0, 8192).await.unwrap(), bytes);
            handle.backing.release_consumer(&consumer);
        }
    }
}

impl NativeReadLease {
    pub fn file(&self) -> &File {
        &self.file
    }
}

impl Drop for NativeReadLease {
    fn drop(&mut self) {
        let mut state = self
            .owner
            .backing
            .native_state
            .lock()
            .expect("native state poisoned");
        state.readers = state
            .readers
            .checked_sub(1)
            .expect("native reader underflow");
        let schedule = state.readers == 0 && state.recovery.is_some();
        drop(state);
        if schedule {
            if let Ok(runtime) = tokio::runtime::Handle::try_current() {
                let backing = Arc::clone(&self.owner.backing);
                runtime.spawn(async move {
                    let _ = backing.reclaim_unused().await;
                });
            }
        }
    }
}

impl LocalBackingHandle {
    /// Adopt only metadata and existing FDs; neither encoding nor a content SHA
    /// is needed to make a captured generation available to a local fork.
    pub fn from_native(layer: FrozenLayerPin) -> Result<Self, BackingError> {
        layer
            .validate()
            .map_err(|error| BackingError::Io(io::Error::other(error.to_string())))?;
        let readonly = File::from(
            layer
                .open_readonly()
                .map_err(|error| BackingError::Io(io::Error::other(error.to_string())))?,
        );
        let metadata = readonly.metadata()?;
        let kind = match layer.descriptor.kind {
            crate::snapshot::metadata::LayerKind::Memory => LayerKind::Memory,
            crate::snapshot::metadata::LayerKind::Block => LayerKind::Block,
        };
        let file = if kind == LayerKind::Memory {
            File::from(
                layer
                    .open_shared_mapping()
                    .map_err(|error| BackingError::Io(io::Error::other(error.to_string())))?,
            )
        } else {
            posix_file::reopen_readwrite(readonly.as_raw_fd())?
        };
        let backing = LocalBacking::create(
            None,
            format!(
                "native:{}:{}:{}:{}",
                kind.as_str(),
                layer.ownership.snapshot_id,
                metadata.dev(),
                metadata.ino()
            ),
            kind,
            layer.descriptor.logical_size,
            layer.ownership.granularity,
            None,
            file,
        )?;
        let native = NativeBackingMetadata {
            descriptor: layer.descriptor.clone(),
            ownership: layer.ownership.clone(),
        };
        if let Some(existing) = backing.native.get() {
            if existing.descriptor != native.descriptor || existing.ownership != native.ownership {
                return Err(BackingError::IdentityConflict);
            }
        } else {
            if let Err(native) = backing.native.set(native) {
                let existing = backing.native.get().expect("native metadata installed");
                if existing.descriptor != native.descriptor
                    || existing.ownership != native.ownership
                {
                    return Err(BackingError::IdentityConflict);
                }
            }
        }
        Ok(Self::new(backing, Weak::new(), None))
    }
}

impl LocalBacking {
    pub fn native_metadata(&self) -> Option<&NativeBackingMetadata> {
        self.native.get()
    }

    fn unit_range(&self, offset: u64, length: u64) -> Result<(u64, u64), BackingError> {
        if length == 0
            || !offset.is_multiple_of(self.unit_size)
            || (!length.is_multiple_of(self.unit_size)
                && offset.checked_add(length) != Some(self.logical_size))
            || offset
                .checked_add(length)
                .is_none_or(|end| end > self.logical_size)
        {
            return Err(BackingError::InvalidNativeRange);
        }
        Ok((
            offset / self.unit_size,
            (offset + length).div_ceil(self.unit_size),
        ))
    }

    pub fn register_consumer(
        &self,
        consumer: &str,
        ranges: &[(u64, u64)],
    ) -> Result<(), BackingError> {
        if consumer.is_empty() {
            return Err(BackingError::InvalidNativeRange);
        }
        let units = ranges
            .iter()
            .map(|&(o, l)| self.unit_range(o, l))
            .collect::<Result<Vec<_>, _>>()?;
        let mut state = self.native_state.lock().expect("native state poisoned");
        if state.retired.contains(consumer) {
            return Err(BackingError::InvalidNativeRange);
        }
        for (start, end) in units {
            let fresh = state
                .consumers
                .entry(consumer.to_owned())
                .or_default()
                .registered
                .difference(start, end);
            for (left, right) in fresh {
                let owner = state.consumers.get_mut(consumer).expect("consumer exists");
                owner.registered.insert(left, right);
                owner.remaining.insert(left, right);
                state.adjust(left, right, 0);
                state.pending_reclaim.remove(left, right);
            }
        }
        Ok(())
    }

    pub fn release_consumer_ranges(
        &self,
        consumer: &str,
        ranges: &[(u64, u64)],
    ) -> Result<u64, BackingError> {
        let units = ranges
            .iter()
            .map(|&(o, l)| self.unit_range(o, l))
            .collect::<Result<Vec<_>, _>>()?;
        let mut state = self.native_state.lock().expect("native state poisoned");
        let mut released = 0;
        for (start, end) in units {
            let removed = state
                .consumers
                .get_mut(consumer)
                .map_or_else(Vec::new, |owner| owner.remaining.remove(start, end));
            for (left, right) in removed {
                state.adjust(left, right, 1);
                state.pending_reclaim.insert(left, right);
                released += right - left;
            }
        }
        Ok(released)
    }

    pub fn release_consumer(&self, consumer: &str) -> u64 {
        let mut state = self.native_state.lock().expect("native state poisoned");
        state.retired.insert(consumer.to_owned());
        let Some(owner) = state.consumers.remove(consumer) else {
            return 0;
        };
        let mut released = 0;
        for (left, right) in owner.remaining.0 {
            state.adjust(left, right, 2);
            state.pending_reclaim.insert(left, right);
            released += right - left;
        }
        released
    }

    pub fn dependency_counts(&self, offset: u64) -> Result<DependencyCounts, BackingError> {
        if offset >= self.logical_size {
            return Err(BackingError::InvalidNativeRange);
        }
        let state = self.native_state.lock().expect("native state poisoned");
        Ok(*state
            .counts
            .range(..=offset / self.unit_size)
            .next_back()
            .expect("zero boundary")
            .1)
    }

    pub fn install_recovery(&self, recovery: Arc<dyn BackingRecovery>) {
        self.native_state
            .lock()
            .expect("native state poisoned")
            .recovery = Some(recovery);
    }

    pub fn has_reclaimed_data(&self) -> bool {
        !self
            .native_state
            .lock()
            .expect("native state poisoned")
            .reclaimed
            .0
            .is_empty()
    }

    /// A remote worker can finish after its requesting runtime has exited.
    /// Queue its physical writes after residency publication as well as when
    /// its read pin drops. Partial writes do not establish full-unit recovery.
    pub(super) fn queue_materialized_range(&self, offset: u64, length: u64) -> bool {
        if self.native_metadata().is_none()
            || length == 0
            || offset
                .checked_add(length)
                .is_none_or(|end| end > self.logical_size)
        {
            return false;
        }
        let mut state = self.native_state.lock().expect("native state poisoned");
        state.pending_reclaim.insert(
            offset / self.unit_size,
            (offset + length).div_ceil(self.unit_size),
        );
        state.recovery.is_some()
    }

    fn reader_lease(self: &Arc<Self>, permit_holes: bool) -> Result<NativeReadLease, BackingError> {
        let mut state = self.native_state.lock().expect("native state poisoned");
        if !permit_holes && !state.reclaimed.0.is_empty() {
            return Err(BackingError::RecoveryUnavailable);
        }
        let file = self.try_clone_readonly_file()?;
        state.readers += 1;
        Ok(NativeReadLease {
            owner: LocalBackingHandle::new(Arc::clone(self), Weak::new(), None),
            file,
        })
    }

    pub fn pin_native_reader(self: &Arc<Self>) -> Result<NativeReadLease, BackingError> {
        self.reader_lease(false)
    }

    /// In-flight remote loading or a resident-range read pins the inode while
    /// inspecting its independently authenticated download residency.
    pub(crate) fn pin_native_io(self: &Arc<Self>) -> Result<NativeReadLease, BackingError> {
        self.reader_lease(true)
    }

    pub async fn restore_ranges(
        self: &Arc<Self>,
        ranges: &[(u64, u64)],
    ) -> Result<(), BackingError> {
        let _io = self.native_io.lock().await;
        self.restore_ranges_locked(ranges).await
    }

    /// Restore every previously reclaimed DATA unit and retain an inode reader
    /// pin before releasing the restoration gate. Descriptor export can hold
    /// this lease through the VMM acknowledgement without an intervening punch.
    pub async fn restore_and_pin_native_reader(
        self: &Arc<Self>,
    ) -> Result<NativeReadLease, BackingError> {
        let _io = self.native_io.lock().await;
        self.restore_ranges_locked(&[(0, self.logical_size)])
            .await?;
        self.reader_lease(false)
    }

    async fn restore_ranges_locked(
        self: &Arc<Self>,
        ranges: &[(u64, u64)],
    ) -> Result<(), BackingError> {
        let _pin = self.reader_lease(true)?;
        let units = ranges
            .iter()
            .map(|&(o, l)| self.unit_range(o, l))
            .collect::<Result<Vec<_>, _>>()?;
        let (missing, recovery) = {
            let state = self.native_state.lock().expect("native state poisoned");
            let mut missing = Ranges::default();
            for (start, end) in units {
                for (left, right) in state.reclaimed.overlapping(start, end) {
                    missing.insert(start.max(left), end.min(right));
                }
            }
            (missing, state.recovery.clone())
        };
        if missing.0.is_empty() {
            return Ok(());
        }
        let recovery = recovery.ok_or(BackingError::RecoveryUnavailable)?;
        let writable = self.try_clone_file()?;
        for (left, right) in missing.0 {
            // Keep reconstruction reads bounded even when a whole layer was reclaimed.
            let mut unit = left;
            while unit < right {
                let take = (right - unit).min((16 * 1024 * 1024 / self.unit_size).max(1));
                let offset = unit * self.unit_size;
                let length = (take * self.unit_size).min(self.logical_size - offset);
                let bytes = recovery.read_data(offset, length).await?;
                if bytes.len() as u64 != length {
                    return Err(BackingError::RecoveryUnavailable);
                }
                writable.write_all_at(&bytes, offset)?;
                let mut state = self.native_state.lock().expect("native state poisoned");
                state.reclaimed.remove(unit, unit + take);
                state.pending_reclaim.insert(unit, unit + take);
                unit += take;
            }
        }
        Ok(())
    }

    pub async fn read_data(
        self: &Arc<Self>,
        offset: u64,
        length: u64,
    ) -> Result<Vec<u8>, BackingError> {
        if length == 0
            || length > 16 * 1024 * 1024
            || offset
                .checked_add(length)
                .is_none_or(|end| end > self.logical_size)
        {
            return Err(BackingError::InvalidNativeRange);
        }
        let _io = self.native_io.lock().await;
        let start = offset / self.unit_size * self.unit_size;
        let end =
            ((offset + length).div_ceil(self.unit_size) * self.unit_size).min(self.logical_size);
        self.restore_ranges_locked(&[(start, end - start)]).await?;
        let pin = self.reader_lease(true)?;
        posix_file::read_file_range(
            &pin.file,
            offset,
            usize::try_from(length).map_err(|_| BackingError::InvalidNativeRange)?,
        )
        .map_err(BackingError::from)
    }

    /// Read only bytes whose current native residency is already established.
    /// A peer probe must never restore a punched page or start a provider read.
    pub(crate) async fn read_resident_data(
        self: &Arc<Self>,
        offset: u64,
        length: u64,
        known_complete: bool,
    ) -> Result<Option<Vec<u8>>, BackingError> {
        if length == 0
            || length > 16 * 1024 * 1024
            || offset
                .checked_add(length)
                .is_none_or(|end| end > self.logical_size)
        {
            return Err(BackingError::InvalidNativeRange);
        }
        let _io = self.native_io.lock().await;
        let pin = self.reader_lease(true)?;
        if !self.range_is_resident(offset, length, known_complete).await {
            return Ok(None);
        }
        Ok(Some(posix_file::read_file_range(
            &pin.file,
            offset,
            usize::try_from(length).map_err(|_| BackingError::InvalidNativeRange)?,
        )?))
    }

    pub(crate) async fn contains_resident_data(
        self: &Arc<Self>,
        offset: u64,
        length: u64,
        known_complete: bool,
    ) -> Result<bool, BackingError> {
        if length == 0
            || offset
                .checked_add(length)
                .is_none_or(|end| end > self.logical_size)
        {
            return Err(BackingError::InvalidNativeRange);
        }
        let _io = self.native_io.lock().await;
        let _pin = self.reader_lease(true)?;
        Ok(self.range_is_resident(offset, length, known_complete).await)
    }

    pub(crate) async fn contains_any_resident_data(
        self: &Arc<Self>,
        offset: u64,
        length: u64,
        known_complete: bool,
    ) -> Result<bool, BackingError> {
        if length == 0
            || offset
                .checked_add(length)
                .is_none_or(|end| end > self.logical_size)
        {
            return Err(BackingError::InvalidNativeRange);
        }
        let _io = self.native_io.lock().await;
        let _pin = self.reader_lease(true)?;
        if self.native_metadata().is_some() || known_complete {
            let state = self.native_state.lock().expect("native state poisoned");
            return Ok(!state
                .reclaimed
                .difference(
                    offset / self.unit_size,
                    (offset + length).div_ceil(self.unit_size),
                )
                .is_empty());
        }
        let entries = self.downloads.entries.lock().await;
        Ok(entries.iter().any(|(key, entry)| {
            entry.state == DownloadState::Resident
                && key.offset < offset + length
                && offset < key.offset + key.length
        }))
    }

    async fn range_is_resident(&self, offset: u64, length: u64, known_complete: bool) -> bool {
        {
            let state = self.native_state.lock().expect("native state poisoned");
            if state.reclaimed.overlaps(
                offset / self.unit_size,
                (offset + length).div_ceil(self.unit_size),
            ) {
                return false;
            }
        }
        if self.native_metadata().is_none() && !known_complete {
            let entries = self.downloads.entries.lock().await;
            if entries
                .get(&DataRangeKey {
                    target_id: self.backing_id.clone(),
                    offset,
                    length,
                })
                .is_some_and(|entry| entry.state == DownloadState::Resident)
            {
                return true;
            }
            let mut resident = Ranges::default();
            for (key, _) in entries
                .iter()
                .filter(|(_, entry)| entry.state == DownloadState::Resident)
            {
                resident.insert(key.offset, key.offset + key.length);
            }
            if !resident.difference(offset, offset + length).is_empty() {
                return false;
            }
        }
        true
    }

    /// Reclaim native DATA only. Ownership and immutable logical identity are
    /// unchanged; future attaches restore the ranges before exporting FDs.
    pub async fn reclaim_unused(self: &Arc<Self>) -> Result<Vec<(u64, u64)>, BackingError> {
        let _io = self.native_io.lock().await;
        let mut state = self.native_state.lock().expect("native state poisoned");
        if state.readers != 0 || state.recovery.is_none() {
            return Ok(Vec::new());
        }
        let Some(native) = self.native_metadata() else {
            return Ok(Vec::new());
        };
        let Ok(mut downloads) = self.downloads.entries.try_lock() else {
            return Ok(Vec::new());
        };
        let mut loading = Ranges::default();
        for (key, _) in downloads
            .iter()
            .filter(|(_, entry)| entry.state == DownloadState::Loading)
        {
            loading.insert(key.offset, key.offset + key.length);
        }
        let file = self.try_clone_file()?;
        if !state.discovered_native_data {
            // One initial pass also finds immutable DATA with no runtime
            // consumers. Later COW notifications enqueue only affected units.
            state
                .pending_reclaim
                .insert(0, self.logical_size / self.unit_size);
            state.discovered_native_data = true;
        }
        let pending = std::mem::take(&mut state.pending_reclaim);
        let mut candidates = Ranges::default();
        for unit in pending.0.into_iter().flat_map(|(left, right)| left..right) {
            let disposition =
                (native.ownership.packed_states[(unit / 4) as usize] >> ((unit % 4) * 2)) & 3;
            if disposition != 1
                || state
                    .counts
                    .range(..=unit)
                    .next_back()
                    .expect("zero boundary")
                    .1
                    .remaining()
                    != 0
            {
                continue;
            }
            let offset = unit * self.unit_size;
            if loading.overlaps(offset, offset + self.unit_size) {
                state.pending_reclaim.push_sorted(unit, unit + 1);
                continue;
            }
            candidates.push_sorted(unit, unit + 1);
        }
        let mut reclaimed = Vec::new();
        for (&left, &right) in &candidates.0 {
            let offset = left * self.unit_size;
            let length = (right - left) * self.unit_size;
            // The registry lock also excludes a new exact/group load becoming
            // Resident while the native inode is punched.
            for (key, entry) in downloads
                .iter_mut()
                .filter(|(key, _)| key.offset < offset + length && offset < key.offset + key.length)
            {
                let _ = key;
                entry.state = DownloadState::NotResident;
                entry.load = None;
                entry.last_error = None;
            }
            // A failed filesystem operation may already have changed part of
            // its range. Mark it recoverable before the syscall so exports
            // also fail closed after an I/O error or an interrupted punch.
            state.reclaimed.insert(left, right);
            // SAFETY: file is owned and writable; offset/length were validated
            // against fixed-unit logical geometry. No consumer or reader can
            // be admitted while this native-state critical section is held.
            let result = unsafe {
                libc::fallocate(
                    file.as_raw_fd(),
                    libc::FALLOC_FL_PUNCH_HOLE | libc::FALLOC_FL_KEEP_SIZE,
                    offset as libc::off_t,
                    length as libc::off_t,
                )
            };
            if result != 0 {
                for (&retry_left, &retry_right) in &candidates.0 {
                    if retry_right > left {
                        state
                            .pending_reclaim
                            .insert(retry_left.max(left), retry_right);
                    }
                }
                return Err(BackingError::Io(io::Error::last_os_error()));
            }
            reclaimed.push((offset, length));
        }
        Ok(reclaimed)
    }
}
