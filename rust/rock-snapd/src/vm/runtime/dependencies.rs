//! One runtime incarnation's dependency capabilities; counters live in LocalBacking.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use tokio::sync::Notify;

use crate::endpoints::lazy_load::protocol::DependencyRelease;
use crate::snapshot::io::backing::{LayerKind, LocalBacking, LocalBackingHandle};
use crate::vm::runtime::protocol::LayerKind as ProtocolKind;

pub(crate) struct DependencyLayer {
    pub backing: Arc<LocalBacking>,
    /// Half-open byte intervals. Layers are supplied oldest to newest.
    pub data: Vec<(u64, u64)>,
    pub zero: Vec<(u64, u64)>,
}

#[derive(Debug)]
struct Registration {
    lease: LocalBackingHandle,
    ranges: Vec<(u64, u64)>,
}

#[derive(Debug, Default)]
struct State {
    registrations: HashMap<(LayerKind, u64, u64), Registration>,
    captures: usize,
    closed: bool,
}

#[derive(Debug)]
pub(crate) struct RuntimeDependencies {
    consumer: String,
    state: Mutex<State>,
    changed: Notify,
}

impl RuntimeDependencies {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            consumer: uuid::Uuid::new_v4().to_string(),
            state: Mutex::new(State::default()),
            changed: Notify::new(),
        })
    }

    /// Register only the winning DATA contributors, including each distinct
    /// ancestor contributing a 512-byte portion of a 64-KiB block COW unit.
    pub async fn prepare(&self, layers: Vec<DependencyLayer>) -> Result<(), String> {
        let mut covered: HashMap<LayerKind, Vec<(u64, u64)>> = HashMap::new();
        let mut restore = Vec::new();
        for layer in layers.into_iter().rev() {
            let higher = covered.entry(layer.backing.kind()).or_default();
            let mut units = Vec::new();
            let unit = layer.backing.unit_size();
            for &(start, end) in &layer.data {
                for (left, right) in subtract(start, end, higher) {
                    let left = left / unit * unit;
                    let right = right
                        .div_ceil(unit)
                        .saturating_mul(unit)
                        .min(layer.backing.logical_size());
                    insert(&mut units, left, right);
                }
            }
            higher.extend(layer.data.into_iter().chain(layer.zero));
            normalize(higher);
            let ranges: Vec<_> = units.into_iter().map(|(s, e)| (s, e - s)).collect();
            self.register(Arc::clone(&layer.backing), &ranges)?;
            restore.push((layer.backing, ranges));
        }
        // A registered consumer prevents a concurrent reclaim. Restore holes
        // before exporting any LocalComplete mapping descriptor to the VMM.
        for (backing, ranges) in restore {
            backing
                .restore_ranges(&ranges)
                .await
                .map_err(|error| error.to_string())?;
        }
        Ok(())
    }

    pub fn register(
        &self,
        backing: Arc<LocalBacking>,
        ranges: &[(u64, u64)],
    ) -> Result<(), String> {
        let (device, inode) = backing
            .physical_identity()
            .map_err(|error| error.to_string())?;
        let mut state = self
            .state
            .lock()
            .expect("runtime dependency state poisoned");
        if state.closed {
            return Err("runtime dependency capability is closed".into());
        }
        backing
            .register_consumer(&self.consumer, ranges)
            .map_err(|error| error.to_string())?;
        let registration = state
            .registrations
            .entry((backing.kind(), device, inode))
            .or_insert_with(|| Registration {
                lease: backing.lease(),
                ranges: Vec::new(),
            });
        for &(offset, length) in ranges {
            insert(&mut registration.ranges, offset, offset + length);
        }
        drop(state);
        self.changed.notify_waiters();
        Ok(())
    }

    pub fn begin_capture(self: &Arc<Self>) -> Result<CaptureGuard, String> {
        let mut state = self
            .state
            .lock()
            .expect("runtime dependency state poisoned");
        if state.closed {
            return Err("runtime dependency capability is closed".into());
        }
        state.captures += 1;
        Ok(CaptureGuard(Arc::clone(self)))
    }

    pub async fn release(&self, release: &DependencyRelease) -> Result<(), String> {
        release.validate()?;
        let kind = match release.kind {
            ProtocolKind::Memory => LayerKind::Memory,
            ProtocolKind::Block => LayerKind::Block,
        };
        let key = (kind, release.device, release.inode);
        let backing = loop {
            let changed = self.changed.notified();
            tokio::pin!(changed);
            changed.as_mut().enable();
            {
                let state = self
                    .state
                    .lock()
                    .expect("runtime dependency state poisoned");
                if state.closed {
                    return Err("runtime dependency capability is closed".into());
                }
                if let Some(registration) = state.registrations.get(&key) {
                    if !subtract(
                        release.offset,
                        release.offset + release.length,
                        &registration.ranges,
                    )
                    .is_empty()
                    {
                        return Err(
                            "dependency release exceeds this runtime's registered coverage".into(),
                        );
                    }
                    break Arc::clone(&registration.lease.backing);
                }
                // CLH can resume after freeze before snapd adopts the frozen
                // inode. Only that bounded capture window permits waiting.
                if state.captures == 0 {
                    return Err("unknown runtime backing dependency".into());
                }
            }
            changed.await;
        };
        backing
            .release_consumer_ranges(&self.consumer, &[(release.offset, release.length)])
            .map_err(|error| error.to_string())?;
        if let Err(error) = backing.reclaim_unused().await {
            // COW has already committed in the VMM. Reclamation is optional;
            // keep the recoverable backing and pending retry instead of failing its data
            // channel after a valid ownership notification.
            eprintln!(
                "Native backing reclaim failed after committed COW for {}: {error}",
                backing.backing_id
            );
        }
        Ok(())
    }

    pub async fn close(&self) {
        let registrations = self.retire();
        for registration in registrations {
            let _ = registration.lease.backing.reclaim_unused().await;
        }
    }

    fn retire(&self) -> Vec<Registration> {
        let mut state = self
            .state
            .lock()
            .expect("runtime dependency state poisoned");
        state.closed = true;
        let registrations = state
            .registrations
            .drain()
            .map(|(_, r)| r)
            .collect::<Vec<_>>();
        for registration in &registrations {
            registration.lease.backing.release_consumer(&self.consumer);
        }
        drop(state);
        self.changed.notify_waiters();
        registrations
    }
}

impl Drop for RuntimeDependencies {
    fn drop(&mut self) {
        let registrations = self.retire();
        if let Ok(runtime) = tokio::runtime::Handle::try_current() {
            runtime.spawn(async move {
                for registration in registrations {
                    let _ = registration.lease.backing.reclaim_unused().await;
                }
            });
        }
    }
}

pub(crate) struct CaptureGuard(Arc<RuntimeDependencies>);

impl Drop for CaptureGuard {
    fn drop(&mut self) {
        let mut state = self
            .0
            .state
            .lock()
            .expect("runtime dependency state poisoned");
        state.captures = state
            .captures
            .checked_sub(1)
            .expect("capture guard underflow");
        drop(state);
        self.0.changed.notify_waiters();
    }
}

fn insert(ranges: &mut Vec<(u64, u64)>, start: u64, end: u64) {
    if start >= end {
        return;
    }
    if let Some(last) = ranges.last_mut().filter(|last| start >= last.0) {
        if start <= last.1 {
            last.1 = last.1.max(end);
        } else {
            ranges.push((start, end));
        }
        return;
    }
    ranges.push((start, end));
    normalize(ranges);
}

fn normalize(ranges: &mut Vec<(u64, u64)>) {
    ranges.sort_unstable();
    let mut merged: Vec<(u64, u64)> = Vec::with_capacity(ranges.len());
    for (left, right) in ranges.drain(..) {
        if let Some(last) = merged.last_mut().filter(|last| left <= last.1) {
            last.1 = last.1.max(right);
        } else {
            merged.push((left, right));
        }
    }
    *ranges = merged;
}

fn subtract(start: u64, end: u64, covered: &[(u64, u64)]) -> Vec<(u64, u64)> {
    let mut result = Vec::new();
    let mut cursor = start;
    let first = covered.partition_point(|&(_, right)| right <= start);
    for &(left, right) in &covered[first..] {
        if right <= cursor {
            continue;
        }
        if left >= end {
            break;
        }
        if left > cursor {
            result.push((cursor, left.min(end)));
        }
        cursor = cursor.max(right);
        if cursor >= end {
            break;
        }
    }
    if cursor < end {
        result.push((cursor, end));
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::snapshot::metadata::LayerKind as SnapshotKind;
    use crate::snapshot::repository::frozen_layer::{
        FrozenLayerMetadata, FrozenLayerPin, PackedOwnershipMap,
    };

    fn native(kind: SnapshotKind, size: u64) -> LocalBackingHandle {
        let file = match kind {
            SnapshotKind::Memory => crate::snapshot::transport::posix_file::memory_file(
                c"runtime-dependency-test",
                size,
            )
            .unwrap(),
            SnapshotKind::Block => tempfile::tempfile().unwrap(),
        };
        file.set_len(size).unwrap();
        let unit = match kind {
            SnapshotKind::Memory => 4096,
            SnapshotKind::Block => 65536,
        };
        let count = size / unit;
        let mut packed_states = vec![0; (count.div_ceil(4)) as usize];
        for index in 0..count {
            packed_states[index as usize / 4] |= 1 << ((index % 4) * 2);
        }
        LocalBackingHandle::from_native(FrozenLayerPin::new(
            FrozenLayerMetadata {
                kind,
                layer_id: "dependency-test".into(),
                provisional_content_id: "native-test".into(),
                logical_size: size,
                alignment: unit,
            },
            PackedOwnershipMap {
                kind,
                snapshot_id: "dependency-test".into(),
                format_version: 1,
                logical_size: size,
                granularity: unit,
                packed_states,
            },
            file.into(),
            false,
        ))
        .unwrap()
    }

    fn release(backing: &LocalBacking, offset: u64, length: u64) -> DependencyRelease {
        let (device, inode) = backing.physical_identity().unwrap();
        DependencyRelease {
            kind: match backing.kind() {
                LayerKind::Memory => ProtocolKind::Memory,
                LayerKind::Block => ProtocolKind::Block,
            },
            device,
            inode,
            offset,
            length,
        }
    }

    #[tokio::test]
    async fn committed_cow_is_acknowledged_even_when_hole_punch_fails() {
        use crate::snapshot::io::backing::{BackingError, BackingRecovery};
        use std::os::fd::AsRawFd;
        use std::os::unix::fs::FileExt;

        #[derive(Debug)]
        struct Recovery;
        #[async_trait::async_trait]
        impl BackingRecovery for Recovery {
            async fn read_data(&self, _offset: u64, length: u64) -> Result<Vec<u8>, BackingError> {
                Ok(vec![0x4d; length as usize])
            }
        }
        let native = native(SnapshotKind::Memory, 4096);
        let file = native.backing.try_clone_file().unwrap();
        file.write_all_at(&[0x4d; 4096], 0).unwrap();
        // A real kernel write seal forces fallocate(PUNCH_HOLE) to fail while
        // leaving the already captured DATA readable and unchanged.
        assert_eq!(
            unsafe { libc::fcntl(file.as_raw_fd(), libc::F_ADD_SEALS, libc::F_SEAL_WRITE) },
            0
        );
        native.backing.install_recovery(Arc::new(Recovery));
        let vm = RuntimeDependencies::new();
        vm.register(native.backing.clone(), &[(0, 4096)]).unwrap();
        vm.release(&release(&native.backing, 0, 4096))
            .await
            .unwrap();
        assert_eq!(native.backing.dependency_counts(0).unwrap().remaining(), 0);
        // A failed punch is conservatively marked for recovery because a
        // filesystem error could have happened after a partial change.
        assert!(native.backing.has_reclaimed_data());
        assert!(native.backing.pin_native_reader().is_err());
        assert!(native.backing.read_data(0, 4096).await.is_err());
        let mut bytes = [0; 4096];
        file.read_exact_at(&mut bytes, 0).unwrap();
        assert_eq!(bytes, [0x4d; 4096]);
    }

    #[tokio::test]
    async fn block_cow_unit_tracks_each_visible_sector_ancestor() {
        let a = native(SnapshotKind::Block, 65536);
        let b = native(SnapshotKind::Block, 65536);
        let vm = RuntimeDependencies::new();
        vm.prepare(vec![
            DependencyLayer {
                backing: a.backing.clone(),
                data: vec![(0, 65536)],
                zero: vec![],
            },
            DependencyLayer {
                backing: b.backing.clone(),
                data: vec![(0, 512)],
                zero: vec![(512, 1024)],
            },
        ])
        .await
        .unwrap();
        assert_eq!(a.backing.dependency_counts(0).unwrap().remaining(), 1);
        assert_eq!(b.backing.dependency_counts(0).unwrap().remaining(), 1);
        vm.release(&release(&a.backing, 0, 65536)).await.unwrap();
        vm.release(&release(&b.backing, 0, 65536)).await.unwrap();
        assert_eq!(a.backing.dependency_counts(0).unwrap().remaining(), 0);
        assert_eq!(b.backing.dependency_counts(0).unwrap().remaining(), 0);
    }

    #[tokio::test]
    async fn zero_and_data_mask_older_memory_dependencies() {
        let old = native(SnapshotKind::Memory, 12288);
        let new = native(SnapshotKind::Memory, 12288);
        let vm = RuntimeDependencies::new();
        vm.prepare(vec![
            DependencyLayer {
                backing: old.backing.clone(),
                data: vec![(0, 12288)],
                zero: vec![],
            },
            DependencyLayer {
                backing: new.backing.clone(),
                data: vec![(4096, 8192)],
                zero: vec![(0, 4096)],
            },
        ])
        .await
        .unwrap();
        assert_eq!(old.backing.dependency_counts(0).unwrap().remaining(), 0);
        assert_eq!(old.backing.dependency_counts(4096).unwrap().remaining(), 0);
        assert_eq!(old.backing.dependency_counts(8192).unwrap().remaining(), 1);
        assert_eq!(new.backing.dependency_counts(4096).unwrap().remaining(), 1);
        assert!(vm.release(&release(&old.backing, 0, 4096)).await.is_err());
    }

    #[tokio::test]
    async fn duplicate_and_old_incarnation_notifications_cannot_release_new_vm() {
        let native = native(SnapshotKind::Memory, 4096);
        let old = RuntimeDependencies::new();
        let new = RuntimeDependencies::new();
        old.register(native.backing.clone(), &[(0, 4096)]).unwrap();
        new.register(native.backing.clone(), &[(0, 4096)]).unwrap();
        let message = release(&native.backing, 0, 4096);
        old.release(&message).await.unwrap();
        old.release(&message).await.unwrap();
        assert_eq!(native.backing.dependency_counts(0).unwrap().remaining(), 1);
        old.close().await;
        assert!(old.release(&message).await.is_err());
        assert_eq!(native.backing.dependency_counts(0).unwrap().remaining(), 1);
        new.close().await;
        assert_eq!(native.backing.dependency_counts(0).unwrap().remaining(), 0);
    }

    #[tokio::test]
    async fn notification_waits_for_capture_adoption_and_keeps_real_lease() {
        let native = native(SnapshotKind::Memory, 4096);
        let backing = native.backing.clone();
        let message = release(&backing, 0, 4096);
        let vm = RuntimeDependencies::new();
        let guard = vm.begin_capture().unwrap();
        let pending = {
            let vm = vm.clone();
            tokio::spawn(async move { vm.release(&message).await })
        };
        tokio::task::yield_now().await;
        assert!(!pending.is_finished());
        vm.register(backing.clone(), &[(0, 4096)]).unwrap();
        drop(native);
        drop(guard);
        pending.await.unwrap().unwrap();
        assert!(!backing.is_closed());
        assert_eq!(backing.dependency_counts(0).unwrap().remaining(), 0);
        vm.close().await;
        assert_eq!(backing.dependency_counts(0).unwrap().remaining(), 0);
    }
}
