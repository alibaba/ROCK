//! Owning captured snapshot descriptors and canonical local layer conversion.

use std::fs::{File, OpenOptions};
use std::os::fd::{AsRawFd, OwnedFd, RawFd};
use std::os::unix::fs::MetadataExt;
#[cfg(test)]
use std::os::unix::fs::OpenOptionsExt;

use thiserror::Error;

use crate::snapshot::format::overlaybd_s3::capture::{CanonicalLocalLayer, FormatCaptureError};
use crate::snapshot::format::LayerEncodeError;
#[cfg(test)]
use crate::snapshot::metadata::RangeKind;
#[cfg(test)]
use crate::snapshot::metadata::MAX_DATA_RANGE_BYTES;
use crate::snapshot::metadata::{sha256_identity, LayerKind as SnapshotLayerKind};
use crate::snapshot::transport::DataIntegrityFailure;

/// Snapshot-owned native source metadata, independent of any VMM wire message.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct FrozenLayerMetadata {
    pub kind: SnapshotLayerKind,
    pub layer_id: String,
    pub provisional_content_id: String,
    pub logical_size: u64,
    pub alignment: u64,
}

#[derive(Clone, Debug, Eq, PartialEq, serde::Serialize)]
pub struct PackedOwnershipMap {
    #[serde(serialize_with = "serialize_ownership_kind")]
    pub kind: SnapshotLayerKind,
    pub snapshot_id: String,
    pub format_version: u64,
    pub logical_size: u64,
    pub granularity: u64,
    pub packed_states: Vec<u8>,
}

fn serialize_ownership_kind<S>(kind: &SnapshotLayerKind, serializer: S) -> Result<S::Ok, S::Error>
where
    S: serde::Serializer,
{
    // Preserve the public local-checkpoint metadata shape while owning the
    // neutral kind here rather than importing a CLH protocol enum.
    serializer.serialize_str(match kind {
        SnapshotLayerKind::Memory => "memory",
        SnapshotLayerKind::Block => "block",
    })
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct VmStateMetadata {
    pub format_version: u64,
    pub content_id: String,
    pub size: u64,
    pub alignment: u64,
}

#[derive(Debug)]
pub struct CapturedSnapshotMetadata {
    pub snapshot_id: String,
    pub vm_state: VmStateMetadata,
}

pub const MEMORY_OWNERSHIP_UNIT: u64 = 4 * 1024;
pub const BLOCK_OWNERSHIP_UNIT: u64 = 64 * 1024;
pub const MAX_OWNERSHIP_UNITS: u64 = 16 * 1024 * 1024;
pub const MAX_VM_STATE_BYTES: u64 = 64 * 1024 * 1024;

#[derive(Debug, Error)]
pub enum FrozenLayerError {
    #[error(transparent)]
    SourceIntegrity(DataIntegrityFailure),
    #[error("Invalid captured snapshot: {0}")]
    Invalid(String),
    #[error("Captured snapshot I/O failed: {0}")]
    Io(String),
    #[error("Canonical snapshot conversion failed: {0}")]
    CanonicalModel(String),
}

impl From<FormatCaptureError> for FrozenLayerError {
    fn from(error: FormatCaptureError) -> Self {
        match error {
            FormatCaptureError::SourceIntegrity(failure) => Self::SourceIntegrity(failure),
            FormatCaptureError::Io(message) => Self::Io(message),
            FormatCaptureError::Invalid(message) => Self::Invalid(message),
            FormatCaptureError::CanonicalModel(message) => Self::CanonicalModel(message),
        }
    }
}

impl From<LayerEncodeError> for FrozenLayerError {
    fn from(error: LayerEncodeError) -> Self {
        match error {
            LayerEncodeError::SourceIntegrity(failure) => Self::SourceIntegrity(failure),
            LayerEncodeError::Invalid(message) => Self::Invalid(message),
            LayerEncodeError::Io(message) => Self::Io(message),
            LayerEncodeError::CanonicalModel(message) => Self::CanonicalModel(message),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
enum OwnershipState {
    Inherit = 0,
    Data = 1,
    Zero = 2,
}

impl TryFrom<u8> for OwnershipState {
    type Error = FrozenLayerError;

    fn try_from(value: u8) -> Result<Self, Self::Error> {
        match value {
            0 => Ok(Self::Inherit),
            1 => Ok(Self::Data),
            2 => Ok(Self::Zero),
            _ => Err(FrozenLayerError::Invalid(
                "Packed ownership payload contains an unknown state".into(),
            )),
        }
    }
}

#[derive(Debug)]
pub struct FrozenLayerPin {
    pub descriptor: FrozenLayerMetadata,
    pub ownership: PackedOwnershipMap,
    backing: OwnedFd,
    backing_readonly: bool,
}

impl FrozenLayerPin {
    /// Duplicate a validated capture capability while preserving native inode
    /// and mapping access. Backing adoption consumes this owned capability.
    pub(crate) fn try_clone(&self) -> Result<Self, FrozenLayerError> {
        Ok(Self::new(
            self.descriptor.clone(),
            self.ownership.clone(),
            if self.descriptor.kind == SnapshotLayerKind::Memory && !self.backing_readonly {
                self.open_shared_mapping()?
            } else {
                self.open_readonly()?
            },
            self.descriptor.kind != SnapshotLayerKind::Memory || self.backing_readonly,
        ))
    }
    pub fn new(
        descriptor: FrozenLayerMetadata,
        ownership: PackedOwnershipMap,
        backing: OwnedFd,
        backing_readonly: bool,
    ) -> Self {
        Self {
            descriptor,
            ownership,
            backing,
            backing_readonly,
        }
    }

    pub fn validate(&self) -> Result<(), FrozenLayerError> {
        if self.descriptor.kind != self.ownership.kind {
            return invalid("Frozen layer and ownership kind differ");
        }
        if self.descriptor.logical_size != self.ownership.logical_size {
            return invalid("Frozen layer and ownership geometry differ");
        }
        let unit = ownership_unit(self.descriptor.kind);
        if self.ownership.granularity != unit {
            return invalid("Ownership granularity does not match the layer kind");
        }
        if self.descriptor.alignment != unit {
            return invalid("Frozen layer alignment does not match ownership granularity");
        }
        if self.descriptor.logical_size == 0 || !self.descriptor.logical_size.is_multiple_of(unit) {
            return invalid("Frozen logical size is not ownership-unit aligned");
        }
        validate_access(
            self.backing.as_raw_fd(),
            if self.backing_readonly {
                libc::O_RDONLY
            } else {
                libc::O_RDWR
            },
            "frozen backing",
        )?;
        let backing = fd_metadata(self.backing.as_raw_fd(), "frozen backing")?;
        if backing.st_size < 0 || backing.st_size as u64 != self.descriptor.logical_size {
            return invalid("Frozen backing size does not match descriptor");
        }
        let expected_units = self.descriptor.logical_size / unit;
        if expected_units > MAX_OWNERSHIP_UNITS {
            return invalid("Ownership map exceeds the protocol unit bound");
        }
        if self.ownership.format_version != 1 {
            return invalid("Ownership map encoding version is unsupported");
        }
        let expected_bytes = expected_units
            .checked_add(3)
            .and_then(|units| units.checked_div(4))
            .and_then(|bytes| usize::try_from(bytes).ok())
            .ok_or_else(|| FrozenLayerError::Invalid("Ownership map length overflows".into()))?;
        if self.ownership.packed_states.len() != expected_bytes {
            return invalid("Packed ownership payload length does not match geometry");
        }
        for unit in 0..expected_units {
            self.ownership_state(unit)?;
        }
        let used_bits = (expected_units % 4) * 2;
        if used_bits != 0
            && self
                .ownership
                .packed_states
                .last()
                .is_some_and(|byte| *byte >> used_bits != 0)
        {
            return invalid("Packed ownership payload has non-zero trailing bits");
        }
        Ok(())
    }

    fn ownership_state(&self, unit: u64) -> Result<OwnershipState, FrozenLayerError> {
        let byte = self
            .ownership
            .packed_states
            .get(usize::try_from(unit / 4).map_err(|_| {
                FrozenLayerError::Invalid("Ownership unit index exceeds usize".into())
            })?)
            .ok_or_else(|| FrozenLayerError::Invalid("Ownership unit is out of range".into()))?;
        OwnershipState::try_from((byte >> ((unit % 4) * 2)) & 0b11)
    }

    pub fn read_data(&self, offset: u64, length: u64) -> Result<Vec<u8>, FrozenLayerError> {
        let end = offset
            .checked_add(length)
            .ok_or_else(|| FrozenLayerError::Invalid("DATA range overflows u64".into()))?;
        if length == 0 || end > self.descriptor.logical_size {
            return invalid("DATA range is outside frozen backing");
        }
        read_exact_at(self.backing.as_raw_fd(), length, offset)
    }

    pub fn open_readonly(&self) -> Result<OwnedFd, FrozenLayerError> {
        let original = fd_metadata(self.backing.as_raw_fd(), "frozen backing")?;
        let candidates = [
            format!("/proc/self/fd/{}", self.backing.as_raw_fd()),
            format!("/dev/fd/{}", self.backing.as_raw_fd()),
        ];
        let mut last_error = None;
        for path in candidates {
            match File::open(path) {
                Ok(file) => {
                    let current = file.metadata().map_err(io_error)?;
                    let fd: OwnedFd = file.into();
                    if let Err(error) =
                        validate_access(fd.as_raw_fd(), libc::O_RDONLY, "reopened frozen backing")
                    {
                        last_error = Some(error.to_string());
                        continue;
                    }
                    if (current.dev(), current.ino(), current.size())
                        != (
                            original.st_dev as u64,
                            original.st_ino,
                            original.st_size as u64,
                        )
                    {
                        last_error = Some("Reopened frozen backing identity changed".into());
                        continue;
                    }
                    return Ok(fd);
                }
                Err(error) => last_error = Some(error.to_string()),
            }
        }
        Err(FrozenLayerError::Io(format!(
            "Cannot reopen frozen backing read-only: {}",
            last_error.unwrap_or_else(|| "no descriptor path is available".into())
        )))
    }

    pub fn open_shared_mapping(&self) -> Result<OwnedFd, FrozenLayerError> {
        if self.descriptor.kind != SnapshotLayerKind::Memory || self.backing_readonly {
            return invalid("Frozen memory has no writable shared-mapping capability");
        }
        let original = fd_metadata(self.backing.as_raw_fd(), "frozen memory backing")?;
        let path = format!("/proc/self/fd/{}", self.backing.as_raw_fd());
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .open(path)
            .map_err(io_error)?;
        let current = file.metadata().map_err(io_error)?;
        let fd: OwnedFd = file.into();
        validate_access(fd.as_raw_fd(), libc::O_RDWR, "frozen memory shared mapping")?;
        if (current.dev(), current.ino(), current.size())
            != (
                original.st_dev as u64,
                original.st_ino,
                original.st_size as u64,
            )
        {
            return invalid("Reopened frozen memory mapping identity changed");
        }
        Ok(fd)
    }
}

#[derive(Debug)]
pub struct VmStatePin {
    pub descriptor: VmStateMetadata,
    fd: OwnedFd,
}

impl VmStatePin {
    pub fn new(descriptor: VmStateMetadata, fd: OwnedFd) -> Self {
        Self { descriptor, fd }
    }

    pub fn read_verified(&self) -> Result<Vec<u8>, FrozenLayerError> {
        validate_access(self.fd.as_raw_fd(), libc::O_RDONLY, "VM_STATE")?;
        let metadata = fd_metadata(self.fd.as_raw_fd(), "VM_STATE")?;
        if metadata.st_size < 0 || metadata.st_size as u64 != self.descriptor.size {
            return invalid("VM_STATE size does not match descriptor");
        }
        if self.descriptor.size == 0 || self.descriptor.size > MAX_VM_STATE_BYTES {
            return invalid("VM_STATE exceeds the canonical snapshot bound");
        }
        let payload = read_exact_at(self.fd.as_raw_fd(), self.descriptor.size, 0)?;
        if normalize_sha256(&sha256_identity(&payload))
            != normalize_sha256(&self.descriptor.content_id)
        {
            return invalid("VM_STATE checksum does not match content identity");
        }
        Ok(payload)
    }
}

#[derive(Debug)]
pub struct CapturedSnapshotPin {
    pub metadata: CapturedSnapshotMetadata,
    pub layers: Vec<FrozenLayerPin>,
    pub vm_state: VmStatePin,
}

impl CapturedSnapshotPin {
    pub(crate) fn try_clone(&self) -> Result<Self, FrozenLayerError> {
        Ok(Self {
            metadata: CapturedSnapshotMetadata {
                snapshot_id: self.metadata.snapshot_id.clone(),
                vm_state: self.metadata.vm_state.clone(),
            },
            layers: self
                .layers
                .iter()
                .map(FrozenLayerPin::try_clone)
                .collect::<Result<_, _>>()?,
            vm_state: VmStatePin::new(
                self.vm_state.descriptor.clone(),
                self.vm_state.fd.try_clone().map_err(io_error)?,
            ),
        })
    }
    pub fn new(
        snapshot_id: String,
        layers: Vec<FrozenLayerPin>,
        vm_state: VmStatePin,
    ) -> Result<Self, FrozenLayerError> {
        let pin = Self {
            metadata: CapturedSnapshotMetadata {
                snapshot_id,
                vm_state: vm_state.descriptor.clone(),
            },
            layers,
            vm_state,
        };
        pin.validate()?;
        Ok(pin)
    }

    pub fn validate(&self) -> Result<(), FrozenLayerError> {
        for layer in &self.layers {
            layer.validate()?;
            if layer.ownership.snapshot_id != self.metadata.snapshot_id {
                return invalid("Ownership snapshot ID does not match captured snapshot");
            }
        }
        self.vm_state.read_verified()?;
        Ok(())
    }

    pub fn layer(&self, kind: SnapshotLayerKind) -> Result<&FrozenLayerPin, FrozenLayerError> {
        self.layers
            .iter()
            .find(|layer| layer.descriptor.kind == kind)
            .ok_or_else(|| {
                FrozenLayerError::Invalid(format!("Captured snapshot has no {kind:?} layer"))
            })
    }
}

pub fn canonicalize_frozen_layer(
    pin: &FrozenLayerPin,
    has_parent_layer: bool,
) -> Result<CanonicalLocalLayer, FrozenLayerError> {
    canonicalize_frozen_layer_with_scratch(pin, has_parent_layer, &create_temporary_staged_object)
}

pub(crate) fn create_temporary_staged_object(
    max_bytes: u64,
) -> Result<std::sync::Arc<dyn crate::snapshot::transport::StagedObjectWriter>, FrozenLayerError> {
    crate::snapshot::transport::posix_file::create_temporary_staged_object(max_bytes).map_err(
        |error| {
            if error.retryable() {
                FrozenLayerError::Io(error.to_string())
            } else {
                FrozenLayerError::CanonicalModel(error.to_string())
            }
        },
    )
}

#[cfg(test)]
pub(crate) fn create_temporary_zfile() -> Result<File, FrozenLayerError> {
    let path = std::env::temp_dir().join(format!("rock-zfile-{}", uuid::Uuid::new_v4()));
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)
        .map_err(io_error)?;
    std::fs::remove_file(&path).map_err(io_error)?;
    Ok(file)
}

pub(crate) fn canonicalize_frozen_layer_with_scratch(
    pin: &FrozenLayerPin,
    has_parent_layer: bool,
    create_scratch: &dyn Fn(
        u64,
    ) -> Result<
        std::sync::Arc<dyn crate::snapshot::transport::StagedObjectWriter>,
        FrozenLayerError,
    >,
) -> Result<CanonicalLocalLayer, FrozenLayerError> {
    pin.validate()?;
    let file = File::from(pin.open_readonly()?);
    crate::snapshot::format::overlaybd_s3::capture::encode_frozen_layer(
        crate::snapshot::format::overlaybd_s3::capture::FrozenLayerInput {
            kind: pin.descriptor.kind,
            layer_id: &pin.descriptor.layer_id,
            logical_bytes: pin.descriptor.logical_size,
            unit_size_bytes: pin.ownership.granularity,
            packed_states: &pin.ownership.packed_states,
            file: &file,
        },
        has_parent_layer,
        None,
        &|max_bytes| {
            create_scratch(max_bytes).map_err(|error| match error {
                FrozenLayerError::SourceIntegrity(failure) => {
                    FormatCaptureError::SourceIntegrity(failure)
                }
                FrozenLayerError::Io(message) => {
                    crate::snapshot::format::overlaybd_s3::capture::FormatCaptureError::Io(message)
                }
                error => {
                    crate::snapshot::format::overlaybd_s3::capture::FormatCaptureError::Invalid(
                        error.to_string(),
                    )
                }
            })
        },
    )
    .map_err(FrozenLayerError::from)
}

pub(crate) fn packed_data_ownership(
    logical_size: u64,
    granularity: u64,
) -> Result<Vec<u8>, FrozenLayerError> {
    if logical_size == 0 || granularity == 0 || !logical_size.is_multiple_of(granularity) {
        return invalid("DATA ownership geometry is invalid");
    }
    let units = logical_size / granularity;
    if units > MAX_OWNERSHIP_UNITS {
        return invalid("Ownership map exceeds the protocol unit bound");
    }
    let byte_count = units
        .checked_add(3)
        .and_then(|value| value.checked_div(4))
        .and_then(|value| usize::try_from(value).ok())
        .ok_or_else(|| FrozenLayerError::Invalid("Ownership map length overflows".into()))?;
    let mut packed = vec![0x55; byte_count];
    if !units.is_multiple_of(4) {
        let used_bits = (units % 4) * 2;
        let mask = u8::try_from((1_u16 << used_bits) - 1)
            .map_err(|_| FrozenLayerError::Invalid("Ownership tail mask overflows".into()))?;
        if let Some(last) = packed.last_mut() {
            *last &= mask;
        }
    }
    Ok(packed)
}

fn ownership_unit(kind: SnapshotLayerKind) -> u64 {
    match kind {
        SnapshotLayerKind::Memory => MEMORY_OWNERSHIP_UNIT,
        SnapshotLayerKind::Block => BLOCK_OWNERSHIP_UNIT,
    }
}

fn validate_access(fd: RawFd, expected: i32, label: &str) -> Result<(), FrozenLayerError> {
    // SAFETY: F_GETFL only reads flags from this live borrowed descriptor.
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 {
        return Err(io_error(std::io::Error::last_os_error()));
    }
    if flags & libc::O_ACCMODE != expected {
        return invalid(&format!("{label} has the wrong access mode"));
    }
    Ok(())
}

fn fd_metadata(fd: RawFd, label: &str) -> Result<libc::stat, FrozenLayerError> {
    // SAFETY: fstat initializes the stat structure on success.
    let mut metadata = unsafe { std::mem::zeroed::<libc::stat>() };
    // SAFETY: fd is live and metadata is writable for one stat structure.
    if unsafe { libc::fstat(fd, &mut metadata) } != 0 {
        return Err(FrozenLayerError::Io(format!(
            "Cannot inspect {label}: {}",
            std::io::Error::last_os_error()
        )));
    }
    if metadata.st_mode & libc::S_IFMT != libc::S_IFREG {
        return invalid(&format!("{label} is not a regular file or memfd"));
    }
    Ok(metadata)
}

fn read_exact_at(fd: RawFd, length: u64, offset: u64) -> Result<Vec<u8>, FrozenLayerError> {
    let length = usize::try_from(length)
        .map_err(|_| FrozenLayerError::Invalid("Read length exceeds address space".into()))?;
    let mut output = vec![0_u8; length];
    let mut completed = 0_usize;
    while completed < length {
        let current_offset = offset
            .checked_add(completed as u64)
            .and_then(|value| libc::off_t::try_from(value).ok())
            .ok_or_else(|| FrozenLayerError::Invalid("Read offset exceeds off_t".into()))?;
        // SAFETY: the output tail is valid for the requested length and fd is
        // borrowed for this positional read.
        let read = unsafe {
            libc::pread(
                fd,
                output[completed..].as_mut_ptr().cast(),
                length - completed,
                current_offset,
            )
        };
        if read == 0 {
            return invalid("Frozen FD returned a short read");
        }
        if read < 0 {
            let error = std::io::Error::last_os_error();
            if error.kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return Err(io_error(error));
        }
        completed += read as usize;
    }
    Ok(output)
}

fn normalize_sha256(value: &str) -> &str {
    value.strip_prefix("sha256:").unwrap_or(value)
}

fn invalid<T>(message: &str) -> Result<T, FrozenLayerError> {
    Err(FrozenLayerError::Invalid(message.into()))
}

fn io_error(error: std::io::Error) -> FrozenLayerError {
    FrozenLayerError::Io(error.to_string())
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    use crate::snapshot::format::overlaybd::decode_descriptor;
    use std::os::unix::fs::FileExt;

    fn frozen_layer(kind: SnapshotLayerKind, states: &[u8]) -> FrozenLayerPin {
        let unit = ownership_unit(kind);
        let logical_size = states.len() as u64 * unit;
        let path = std::env::temp_dir().join(format!("rock-overlaybd-{}", uuid::Uuid::new_v4()));
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
            .expect("create test backing");
        std::fs::remove_file(&path).expect("unlink test backing");
        file.set_len(logical_size).expect("size test backing");
        let mut packed_states = vec![0_u8; states.len().div_ceil(4)];
        for (index, state) in states.iter().copied().enumerate() {
            packed_states[index / 4] |= state << ((index % 4) * 2);
            if state == OwnershipState::Data as u8 {
                file.write_all_at(&vec![(index + 1) as u8; unit as usize], index as u64 * unit)
                    .expect("write test DATA");
            }
        }
        FrozenLayerPin::new(
            FrozenLayerMetadata {
                kind,
                layer_id: "test-layer".into(),
                provisional_content_id: format!("sha256:{}", "1".repeat(64)),
                logical_size,
                alignment: unit,
            },
            PackedOwnershipMap {
                kind,
                snapshot_id: "test-snapshot".into(),
                format_version: 1,
                logical_size,
                granularity: unit,
                packed_states,
            },
            file.into(),
            false,
        )
    }

    #[test]
    fn overlaybd_export_preserves_sparse_ownership_and_streamed_payloads() {
        let pin = frozen_layer(SnapshotLayerKind::Block, &[1, 0, 2, 1]);
        let layer = canonicalize_frozen_layer(&pin, true).expect("export block layer");
        assert!(layer.blob.object_key.ends_with(".commit"));
        assert_eq!(
            layer
                .range_map
                .ranges
                .iter()
                .map(|range| range.kind)
                .collect::<Vec<_>>(),
            [
                RangeKind::Data,
                RangeKind::Inherit,
                RangeKind::Zero,
                RangeKind::Data
            ]
        );
        let file = File::from(pin.open_readonly().expect("open source"));
        let encoded_file = layer.blob.encoded_file().unwrap();
        let mut blob = Vec::new();
        while blob.len() as u64 != layer.blob.length {
            let chunk = if let Some(encoded) = &encoded_file {
                let count = (layer.blob.length - blob.len() as u64).min(777) as usize;
                let mut chunk = vec![0; count];
                encoded
                    .read_exact_at(&mut chunk, blob.len() as u64)
                    .unwrap();
                chunk
            } else {
                layer
                    .blob
                    .plan
                    .read_at(&file, blob.len() as u64, 777)
                    .expect("stream across standard metadata/data boundaries")
            };
            assert!(!chunk.is_empty());
            assert!(chunk.len() <= 777);
            blob.extend_from_slice(&chunk);
        }
        assert_eq!(sha256_identity(&blob), layer.blob.sha256);
        let index = layer
            .range_map
            .zfile
            .as_ref()
            .expect("normal layers use ZFile-LZ4");
        let raw = index
            .decode_range(0, index.raw_bytes, 0, &blob)
            .expect("decode actual ZFile");
        assert!(layer.blob.length < layer.blob.plan.stored_bytes);
        let scratch = encoded_file.as_ref().unwrap().metadata().unwrap();
        assert_eq!(scratch.nlink(), 1);
        assert_eq!(scratch.mode() & 0o777, 0o400);
        for range in &layer.range_map.ranges {
            let Some(source) = &range.source else {
                continue;
            };
            assert_eq!(range.length, BLOCK_OWNERSHIP_UNIT);
            assert_eq!(source.object_key, layer.blob.object_key);
            let start = source.pack_offset as usize;
            let end = start + source.encoded_length as usize;
            assert_eq!(sha256_identity(&raw[start..end]), source.checksum_sha256);
            assert_eq!(
                raw[start],
                (range.logical_offset / BLOCK_OWNERSHIP_UNIT + 1) as u8
            );
        }
        assert_eq!(
            decode_descriptor(&layer.range_map_bytes).expect("restore descriptor"),
            *layer.range_map
        );
        assert_eq!(
            canonicalize_frozen_layer(&pin, true).expect("deterministic export"),
            layer
        );
    }

    #[test]
    fn overlaybd_export_publishes_metadata_blob_for_zero_and_inherited_layers() {
        for (states, has_parent, expected) in [
            (vec![0, 2, 0], false, RangeKind::Zero),
            (vec![0, 0, 0], true, RangeKind::Inherit),
        ] {
            let pin = frozen_layer(SnapshotLayerKind::Memory, &states);
            let layer =
                canonicalize_frozen_layer(&pin, has_parent).expect("export empty DATA layer");
            assert!(layer.blob.length > 0);
            assert!(layer.blob.plan.data.is_empty());
            assert_eq!(layer.range_map.ranges.len(), 1);
            assert_eq!(layer.range_map.ranges[0].kind, expected);
            assert_eq!(layer.range_map.ranges[0].length, 3 * MEMORY_OWNERSHIP_UNIT);
            let (blob, size) =
                crate::snapshot::format::overlaybd::descriptor_blob(&layer.range_map_bytes)
                    .expect("descriptor binds metadata-only standard blob");
            assert_eq!(blob.object_key, layer.blob.object_key);
            assert_eq!(blob.sha256, layer.blob.sha256);
            assert_eq!(size, layer.blob.length);
        }
    }

    #[test]
    fn format_encoding_preserves_scratch_capacity_io_error_for_capture_retry() {
        let pin = frozen_layer(SnapshotLayerKind::Block, &[1]);
        let error = canonicalize_frozen_layer_with_scratch(&pin, true, &|_| {
            Err(FrozenLayerError::Io(
                "local cache capacity is exhausted".into(),
            ))
        })
        .unwrap_err();
        assert!(matches!(
            error,
            FrozenLayerError::Io(message) if message == "local cache capacity is exhausted"
        ));
    }

    #[test]
    fn overlaybd_block_export_keeps_large_contiguous_ranges() {
        let units = MAX_DATA_RANGE_BYTES / BLOCK_OWNERSHIP_UNIT + 1;
        let pin = frozen_layer(SnapshotLayerKind::Block, &vec![1; units as usize]);
        let layer = canonicalize_frozen_layer(&pin, true).expect("export contiguous rootfs DATA");
        assert_eq!(layer.range_map.ranges.len(), 2);
        assert_eq!(layer.range_map.ranges[0].length, MAX_DATA_RANGE_BYTES);
        assert_eq!(layer.range_map.ranges[1].length, BLOCK_OWNERSHIP_UNIT);
        assert_eq!(layer.blob.plan.data.len(), 1);

        let memory = frozen_layer(SnapshotLayerKind::Memory, &[1, 1]);
        let memory =
            canonicalize_frozen_layer(&memory, true).expect("export adjacent memory pages");
        assert_eq!(memory.range_map.ranges.len(), 2);
        assert!(memory
            .range_map
            .ranges
            .iter()
            .all(|range| range.length == MEMORY_OWNERSHIP_UNIT));
    }

    fn mutable_vm_state(payload: &[u8]) -> (File, VmStatePin) {
        let path = std::env::temp_dir().join(format!("rock-vm-state-{}", uuid::Uuid::new_v4()));
        let writer = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
            .unwrap();
        writer.write_all_at(payload, 0).unwrap();
        let readonly = File::open(&path).unwrap();
        std::fs::remove_file(path).unwrap();
        let state = VmStatePin::new(
            VmStateMetadata {
                format_version: 1,
                content_id: sha256_identity(payload),
                size: payload.len() as u64,
                alignment: 1,
            },
            readonly.into(),
        );
        (writer, state)
    }

    #[test]
    fn vm_state_validation_rechecks_mutable_files_after_capture_pin_construction() {
        let payload = b"original";
        let (writer, vm_state) = mutable_vm_state(payload);
        let pin = CapturedSnapshotPin::new(
            "test-snapshot".into(),
            vec![
                frozen_layer(SnapshotLayerKind::Memory, &[1]),
                frozen_layer(SnapshotLayerKind::Block, &[1]),
            ],
            vm_state,
        )
        .unwrap();
        assert_eq!(pin.vm_state.read_verified().unwrap(), payload);
        pin.validate().unwrap();

        // A read-only descriptor does not make an unsealed inode immutable.
        // Keep its size unchanged: integrity must not rely on geometry alone.
        writer.write_all_at(b"tampered", 0).unwrap();
        assert_eq!(writer.metadata().unwrap().len(), payload.len() as u64);
        assert!(matches!(
            pin.vm_state.read_verified(),
            Err(FrozenLayerError::Invalid(message)) if message.contains("checksum")
        ));
        assert!(matches!(
            pin.validate(),
            Err(FrozenLayerError::Invalid(message)) if message.contains("checksum")
        ));
        assert!(matches!(
            CapturedSnapshotPin::new("test-snapshot".into(), pin.layers, pin.vm_state),
            Err(FrozenLayerError::Invalid(message)) if message.contains("checksum")
        ));
    }

    #[test]
    fn vm_state_public_verification_rejects_writable_and_wrong_sized_fds() {
        let (writer, readonly) = mutable_vm_state(b"valid-state");
        let writable = VmStatePin::new(
            readonly.descriptor.clone(),
            writer.try_clone().unwrap().into(),
        );
        assert!(matches!(
            writable.read_verified(),
            Err(FrozenLayerError::Invalid(message)) if message.contains("access mode")
        ));
        assert!(matches!(
            CapturedSnapshotPin::new("snapshot".into(), Vec::new(), writable),
            Err(FrozenLayerError::Invalid(message)) if message.contains("access mode")
        ));

        writer.set_len(readonly.descriptor.size + 1).unwrap();
        assert!(matches!(
            readonly.read_verified(),
            Err(FrozenLayerError::Invalid(message)) if message.contains("size")
        ));
        assert!(matches!(
            CapturedSnapshotPin::new("snapshot".into(), Vec::new(), readonly),
            Err(FrozenLayerError::Invalid(message)) if message.contains("size")
        ));
    }
}
