//! Standard OverlayBD ZFile, raw LZ4 blocks with CRC-32C integrity.
//!
//! Layout: containerd/overlaybd v1.0.18 (cee2186), zfile/format_spec.md and
//! zfile.cpp. This is a bounded codec, not a block-device or Photon runtime.
//! CRC uses upstream's raw CRC-32C state, including its 100007 block salt.

use std::fmt;
use std::fs::File;
use std::io;
use std::os::unix::fs::FileExt;
#[cfg(test)]
use std::os::unix::fs::MetadataExt;

use sha2::{Digest, Sha256};

use crate::observability::read_path::{ReadPathStage, ReadPathTimer};

pub const MAX_JUMP_TABLE_BYTES: usize = 64 * 1024 * 1024;
pub const MAX_RAW_RANGE_BYTES: u64 = 64 * 1024 * 1024;
pub const MAX_ENCODED_SPAN_BYTES: u64 = 66 * 1024 * 1024;
const MAX_FILE_BYTES: u64 = 2 * 1024 * 1024 * 1024 * 1024;
const SPACE: usize = 512;
const WRITE_BLOCK: usize = 4096;
const READ_BATCH: usize = 1024 * 1024;
const PREFIX_BLOCKS: usize = 256;
const MAGIC0: &[u8; 8] = b"ZFile\0\x01\0";
// The UUID-like magic is raw bytes, not a textual UUID's byte order.
const MAGIC_BYTES: [u8; 16] = [
    0x74, 0x75, 0x6a, 0x69, 0x2e, 0x79, 0x79, 0x66, 0x40, 0x41, 0x6c, 0x69, 0x62, 0x61, 0x62, 0x61,
];
const HEADER: u64 = 1;
const DATA: u64 = 2;
const SEALED: u64 = 4;
const INFO_VALID: u64 = 8;
const DIGEST: u64 = 16;

#[derive(Debug)]
pub enum ZfileError {
    Invalid(String),
    Bound(String),
    Io(io::Error),
    Source(String),
}

impl fmt::Display for ZfileError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Invalid(message) => write!(formatter, "invalid ZFile: {message}"),
            Self::Bound(message) => write!(formatter, "ZFile bound exceeded: {message}"),
            Self::Io(error) => write!(formatter, "ZFile I/O: {error}"),
            Self::Source(message) => write!(formatter, "ZFile source: {message}"),
        }
    }
}

impl std::error::Error for ZfileError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io(error) => Some(error),
            _ => None,
        }
    }
}

impl From<io::Error> for ZfileError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ZfileIndex {
    pub header: Vec<u8>,
    pub trailer: Vec<u8>,
    pub jump_table: Vec<u8>,
    pub raw_bytes: u64,
    pub encoded_bytes: u64,
    pub index_offset: u64,
    pub block_size: u32,
    pub verify: bool,
    // One absolute prefix per 256 blocks; no second u64 table per block.
    prefixes: Vec<u64>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct ReadBlock {
    encoded_bytes: u32,
    raw_bytes: u32,
}

/// Owned plan for just one bounded request. It does not clone the full index.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ZfileSpan {
    pub offset: u64,
    pub length: u64,
    blocks: Vec<ReadBlock>,
    skip: usize,
    raw_length: usize,
    verify: bool,
}

#[derive(Debug)]
pub struct EncodedZfile {
    pub index: ZfileIndex,
    pub sha256: String,
}

#[derive(Clone, Copy)]
struct Metadata {
    flags: u64,
    index_offset: u64,
    blocks: u64,
    raw_bytes: u64,
    index_crc: u32,
    block_size: u32,
    verify: bool,
}

fn invalid<T>(message: &str) -> Result<T, ZfileError> {
    Err(ZfileError::Invalid(message.into()))
}

fn bounded<T>(message: &str) -> Result<T, ZfileError> {
    Err(ZfileError::Bound(message.into()))
}

fn add(left: u64, right: u64) -> Result<u64, ZfileError> {
    left.checked_add(right)
        .ok_or_else(|| ZfileError::Bound("offset overflow".into()))
}

fn u32_at(bytes: &[u8], offset: usize) -> Result<u32, ZfileError> {
    Ok(u32::from_le_bytes(
        bytes
            .get(offset..offset + 4)
            .ok_or_else(|| ZfileError::Invalid("truncated u32".into()))?
            .try_into()
            .map_err(|_| ZfileError::Invalid("invalid u32 width".into()))?,
    ))
}

fn u64_at(bytes: &[u8], offset: usize) -> Result<u64, ZfileError> {
    Ok(u64::from_le_bytes(
        bytes
            .get(offset..offset + 8)
            .ok_or_else(|| ZfileError::Invalid("truncated u64".into()))?
            .try_into()
            .map_err(|_| ZfileError::Invalid("invalid u64 width".into()))?,
    ))
}

fn crc(bytes: &[u8], seed: u32) -> u32 {
    !crc32c::crc32c_append(!seed, bytes)
}

fn compress_bound(raw_bytes: usize) -> usize {
    raw_bytes + raw_bytes / 255 + 16
}

fn parse_metadata(bytes: &[u8], is_header: bool) -> Result<Metadata, ZfileError> {
    if bytes.len() != SPACE
        || &bytes[..8] != MAGIC0
        || bytes[8..24] != MAGIC_BYTES
        || u32_at(bytes, 24)? != 96
    {
        return invalid("header/trailer magic or structure size differs");
    }
    let flags = u64_at(bytes, 32)?;
    if (flags & HEADER != 0) != is_header
        || flags & DATA == 0
        || (!is_header && flags & SEALED == 0)
        || flags & !(HEADER | DATA | SEALED | INFO_VALID | DIGEST) != 0
    {
        return invalid("unsupported metadata flags or compressed index");
    }
    let mut checked = [0; SPACE];
    checked.copy_from_slice(bytes);
    checked[28..32].fill(0);
    if flags & DIGEST != 0 && crc(&checked, 0) != u32_at(bytes, 28)? {
        return invalid("header/trailer CRC-32C mismatch");
    }
    if bytes[68..72]
        .iter()
        .chain(&bytes[79..84])
        .chain(&bytes[89..])
        .any(|byte| *byte != 0)
        || bytes[76] != 1
        || bytes[78] != 0
        || u32_at(bytes, 84)? != 0
        || bytes[88] > 1
    {
        return invalid("only dictionary-free LZ4 with verify 0/1 is supported");
    }
    let block_size = u32_at(bytes, 72)?;
    if !(4096..=65536).contains(&block_size) || !block_size.is_power_of_two() {
        return invalid("block size must be a power of two from 4 to 64KiB");
    }
    Ok(Metadata {
        flags,
        index_offset: u64_at(bytes, 40)?,
        blocks: u64_at(bytes, 48)?,
        raw_bytes: u64_at(bytes, 56)?,
        index_crc: u32_at(bytes, 64)?,
        block_size,
        verify: bytes[88] != 0,
    })
}

fn copy_mirror(bytes: &[u8]) -> Result<Vec<u8>, ZfileError> {
    let mut result = Vec::new();
    result
        .try_reserve_exact(bytes.len())
        .map_err(|error| ZfileError::Bound(error.to_string()))?;
    result.extend_from_slice(bytes);
    Ok(result)
}

impl ZfileIndex {
    pub fn from_mirrors(
        header: &[u8],
        trailer: &[u8],
        jump_table: &[u8],
    ) -> Result<Self, ZfileError> {
        if header.len() != SPACE || trailer.len() != SPACE {
            return invalid("metadata mirrors must be exactly 512 bytes");
        }
        if jump_table.len() > MAX_JUMP_TABLE_BYTES {
            return bounded("jump table exceeds 64MiB");
        }
        Self::from_owned_mirrors(
            copy_mirror(header)?,
            copy_mirror(trailer)?,
            copy_mirror(jump_table)?,
        )
    }

    pub fn from_owned_mirrors(
        header: Vec<u8>,
        trailer: Vec<u8>,
        jump_table: Vec<u8>,
    ) -> Result<Self, ZfileError> {
        if jump_table.len() > MAX_JUMP_TABLE_BYTES {
            return bounded("jump table exceeds 64MiB");
        }
        let first = parse_metadata(&header, true)?;
        let last = parse_metadata(&trailer, false)?;
        if header[72..96] != trailer[72..96] {
            return invalid("header/trailer compression options differ");
        }
        if first.flags & INFO_VALID != 0 && header[40..72] != trailer[40..72] {
            return invalid("overwritten header metadata differs from trailer");
        }
        if last.raw_bytes == 0
            || last.raw_bytes > MAX_FILE_BYTES
            || last.blocks != last.raw_bytes.div_ceil(u64::from(last.block_size))
            || last.blocks.checked_mul(4) != Some(jump_table.len() as u64)
        {
            return invalid("jump table count does not match raw length/block geometry");
        }
        if last.flags & DIGEST != 0 && crc(&jump_table, 0) != last.index_crc {
            return invalid("jump table CRC-32C mismatch");
        }
        let encoded_bytes = add(
            add(last.index_offset, jump_table.len() as u64)?,
            SPACE as u64,
        )?;
        if encoded_bytes > MAX_FILE_BYTES {
            return bounded("encoded file exceeds 2TiB");
        }
        let count = jump_table.len() / 4;
        let mut prefixes = Vec::new();
        prefixes
            .try_reserve_exact(count.div_ceil(PREFIX_BLOCKS))
            .map_err(|error| ZfileError::Bound(error.to_string()))?;
        let mut cursor = SPACE as u64;
        for block in 0..count {
            if block % PREFIX_BLOCKS == 0 {
                prefixes.push(cursor);
            }
            let raw_length = (last.raw_bytes - block as u64 * u64::from(last.block_size))
                .min(u64::from(last.block_size)) as usize;
            let length = u32_at(&jump_table, block * 4)? as usize;
            let checksum = if last.verify { 4 } else { 0 };
            if length <= checksum || length > compress_bound(raw_length) + checksum {
                return invalid("encoded block length exceeds LZ4 bounds or contains no payload");
            }
            cursor = add(cursor, length as u64)?;
            if cursor > last.index_offset {
                return invalid("encoded block extends into jump table");
            }
        }
        if cursor != last.index_offset {
            return invalid("encoded blocks do not end at jump table");
        }
        Ok(Self {
            header,
            trailer,
            jump_table,
            raw_bytes: last.raw_bytes,
            encoded_bytes,
            index_offset: last.index_offset,
            block_size: last.block_size,
            verify: last.verify,
            prefixes,
        })
    }

    pub fn metadata_regions(&self) -> [(u64, &[u8]); 3] {
        [
            (0, &self.header),
            (self.index_offset, &self.jump_table),
            (self.encoded_bytes - SPACE as u64, &self.trailer),
        ]
    }

    pub fn retained_bytes(&self) -> usize {
        std::mem::size_of::<Self>()
            + self.header.capacity()
            + self.trailer.capacity()
            + self.jump_table.capacity()
            + self.prefixes.capacity() * std::mem::size_of::<u64>()
    }

    fn block_offset(&self, block: usize) -> Result<u64, ZfileError> {
        let count = self.jump_table.len() / 4;
        if block > count {
            return invalid("block offset exceeds jump table");
        }
        if block == count {
            return Ok(self.index_offset);
        }
        let group = block / PREFIX_BLOCKS;
        let mut offset = *self
            .prefixes
            .get(group)
            .ok_or_else(|| ZfileError::Invalid("missing block prefix".into()))?;
        for previous in group * PREFIX_BLOCKS..block {
            offset = add(offset, u64::from(u32_at(&self.jump_table, previous * 4)?))?;
        }
        Ok(offset)
    }

    pub fn compressed_span(
        &self,
        raw_offset: u64,
        raw_length: u64,
    ) -> Result<ZfileSpan, ZfileError> {
        if raw_length > MAX_RAW_RANGE_BYTES {
            return bounded("raw request exceeds 64MiB");
        }
        let raw_end = add(raw_offset, raw_length)?;
        if raw_end > self.raw_bytes {
            return invalid("raw request exceeds original file");
        }
        if raw_length == 0 {
            return Ok(ZfileSpan {
                offset: SPACE as u64,
                length: 0,
                blocks: Vec::new(),
                skip: 0,
                raw_length: 0,
                verify: self.verify,
            });
        }
        let block_size = u64::from(self.block_size);
        let first = (raw_offset / block_size) as usize;
        let end = raw_end.div_ceil(block_size) as usize;
        let offset = self.block_offset(first)?;
        let length = self.block_offset(end)? - offset;
        if length > MAX_ENCODED_SPAN_BYTES {
            return bounded("physical request exceeds 66MiB");
        }
        let mut blocks = Vec::new();
        blocks
            .try_reserve_exact(end - first)
            .map_err(|error| ZfileError::Bound(error.to_string()))?;
        for block in first..end {
            blocks.push(ReadBlock {
                encoded_bytes: u32_at(&self.jump_table, block * 4)?,
                raw_bytes: (self.raw_bytes - block as u64 * block_size).min(block_size) as u32,
            });
        }
        Ok(ZfileSpan {
            offset,
            length,
            blocks,
            skip: (raw_offset % block_size) as usize,
            raw_length: raw_length as usize,
            verify: self.verify,
        })
    }

    pub fn decode_range(
        &self,
        raw_offset: u64,
        raw_length: u64,
        span_offset: u64,
        span: &[u8],
    ) -> Result<Vec<u8>, ZfileError> {
        if span.len() as u64 > MAX_ENCODED_SPAN_BYTES {
            return bounded("supplied physical span exceeds 66MiB");
        }
        let plan = self.compressed_span(raw_offset, raw_length)?;
        if plan.length == 0 {
            return plan.decode(&[]);
        }
        let start = plan.offset.checked_sub(span_offset).ok_or_else(|| {
            ZfileError::Invalid("physical span begins after required block".into())
        })?;
        let end = add(start, plan.length)?;
        let bytes = span
            .get(
                start as usize
                    ..usize::try_from(end)
                        .map_err(|_| ZfileError::Bound("span end exceeds platform".into()))?,
            )
            .ok_or_else(|| ZfileError::Invalid("physical span is truncated".into()))?;
        plan.decode(bytes)
    }
}

impl ZfileSpan {
    pub fn decode(&self, encoded: &[u8]) -> Result<Vec<u8>, ZfileError> {
        // Profile only synchronous decode work, after physical bytes have
        // arrived. This includes CRC, allocation and output copying, not the
        // provider read or any spawn_blocking admission wait.
        let timer = ReadPathTimer::start(ReadPathStage::ZfileDecode);
        let result = self.decode_inner(encoded);
        timer.finish(
            result.as_ref().map_or(0, |payload| payload.len() as u64),
            result.is_ok(),
        );
        result
    }

    fn decode_inner(&self, encoded: &[u8]) -> Result<Vec<u8>, ZfileError> {
        if encoded.len() as u64 != self.length {
            return invalid("physical span has the wrong length");
        }
        let mut result = vec![0; self.raw_length];
        let maximum_block = self
            .blocks
            .iter()
            .map(|block| block.raw_bytes)
            .max()
            .unwrap_or(0) as usize;
        let mut raw = vec![0; maximum_block];
        let mut input = 0_usize;
        let mut output = 0_usize;
        for (number, block) in self.blocks.iter().enumerate() {
            let end = input
                .checked_add(block.encoded_bytes as usize)
                .ok_or_else(|| ZfileError::Bound("compressed block end overflow".into()))?;
            let bytes = encoded
                .get(input..end)
                .ok_or_else(|| ZfileError::Invalid("truncated compressed block".into()))?;
            let compressed = if self.verify {
                let size = bytes
                    .len()
                    .checked_sub(4)
                    .ok_or_else(|| ZfileError::Invalid("missing block CRC".into()))?;
                if crc(&bytes[..size], 100_007) != u32_at(bytes, size)? {
                    return invalid("compressed block CRC-32C mismatch");
                }
                &bytes[..size]
            } else {
                bytes
            };
            // SAFETY: the validated index bounds each input to LZ4_compressBound
            // (<=64KiB+overhead); output is a live buffer of raw_bytes capacity.
            // LZ4_decompress_safe validates the encoded stream and never writes
            // beyond that capacity. All sizes therefore fit C int.
            let count = unsafe {
                lz4_sys::LZ4_decompress_safe(
                    compressed.as_ptr().cast(),
                    raw.as_mut_ptr().cast(),
                    compressed.len() as i32,
                    block.raw_bytes as i32,
                )
            };
            if count != block.raw_bytes as i32 {
                return invalid("LZ4 decode length differs from expected block/tail");
            }
            let start = if number == 0 { self.skip } else { 0 };
            let take = (self.raw_length - output).min(block.raw_bytes as usize - start);
            result[output..output + take].copy_from_slice(&raw[start..start + take]);
            output += take;
            input = end;
        }
        if output != self.raw_length || input != encoded.len() {
            return invalid("decoded span coverage is incomplete");
        }
        Ok(result)
    }
}

fn metadata(
    is_header: bool,
    block_size: u32,
    verify: bool,
    index_offset: u64,
    count: u64,
    raw_bytes: u64,
    index_crc: u32,
) -> Vec<u8> {
    let mut bytes = vec![0; SPACE];
    bytes[..8].copy_from_slice(MAGIC0);
    bytes[8..24].copy_from_slice(&MAGIC_BYTES);
    bytes[24..28].copy_from_slice(&96_u32.to_le_bytes());
    bytes[32..40]
        .copy_from_slice(&(DATA | DIGEST | if is_header { HEADER } else { SEALED }).to_le_bytes());
    for (offset, value) in [(40, index_offset), (48, count), (56, raw_bytes)] {
        bytes[offset..offset + 8].copy_from_slice(&value.to_le_bytes());
    }
    bytes[64..68].copy_from_slice(&index_crc.to_le_bytes());
    bytes[72..76].copy_from_slice(&block_size.to_le_bytes());
    bytes[76] = 1;
    bytes[88] = u8::from(verify);
    let digest = crc(&bytes, 0);
    bytes[28..32].copy_from_slice(&digest.to_le_bytes());
    bytes
}

#[cfg(test)]
fn encode_file(
    source: &File,
    raw_length: u64,
    destination: &File,
) -> Result<EncodedZfile, ZfileError> {
    let source_metadata = source.metadata()?;
    let destination_metadata = destination.metadata()?;
    if !source_metadata.is_file()
        || source_metadata.len() != raw_length
        || (source_metadata.dev(), source_metadata.ino())
            == (destination_metadata.dev(), destination_metadata.ino())
    {
        return invalid("file source size differs or source aliases destination");
    }
    encode_with_reader(raw_length, destination, |offset, count| {
        let mut bytes = vec![0; count];
        source
            .read_exact_at(&mut bytes, offset)
            .map_err(|error| error.to_string())?;
        Ok(bytes)
    })
}

/// Stream <=1MiB source batches into 4KiB LZ4+CRC blocks. The destination must
/// be an empty, exclusively owned regular file. Publication/fsync is the
/// caller's transaction; a failed encode leaves only an incomplete temp file.
pub fn encode_with_reader(
    raw_length: u64,
    destination: &File,
    reader: impl FnMut(u64, usize) -> Result<Vec<u8>, String>,
) -> Result<EncodedZfile, ZfileError> {
    if !destination.metadata()?.is_file() {
        return invalid("destination must be an empty regular file");
    }
    encode_to(
        raw_length,
        || destination.metadata().map(|metadata| metadata.len()),
        |payload, offset| destination.write_all_at(payload, offset),
        reader,
    )
}

/// Encode physical bytes directly into a transport-owned staged object.
pub(crate) fn encode_with_writer(
    raw_length: u64,
    destination: &dyn crate::snapshot::transport::StagedObjectWriter,
    reader: impl FnMut(u64, usize) -> Result<Vec<u8>, String>,
) -> Result<EncodedZfile, ZfileError> {
    encode_to(
        raw_length,
        || destination.length(),
        |payload, offset| destination.write_all_at(payload, offset),
        reader,
    )
}

pub(crate) fn encoded_size_bound(raw_length: u64) -> Result<u64, ZfileError> {
    raw_length
        .div_ceil(WRITE_BLOCK as u64)
        .checked_mul((compress_bound(WRITE_BLOCK) + 8) as u64)
        .and_then(|bytes| bytes.checked_add(2 * SPACE as u64))
        .ok_or_else(|| ZfileError::Bound("encoded size bound overflows".into()))
}

fn encode_to(
    raw_length: u64,
    destination_length: impl Fn() -> std::io::Result<u64>,
    mut write: impl FnMut(&[u8], u64) -> std::io::Result<()>,
    mut reader: impl FnMut(u64, usize) -> Result<Vec<u8>, String>,
) -> Result<EncodedZfile, ZfileError> {
    let count = raw_length.div_ceil(WRITE_BLOCK as u64);
    let jump_bytes = count
        .checked_mul(4)
        .filter(|bytes| *bytes <= MAX_JUMP_TABLE_BYTES as u64)
        .ok_or_else(|| {
            ZfileError::Bound("4KiB jump table exceeds 64MiB; use raw sealed fallback".into())
        })?;
    if raw_length == 0 || raw_length > MAX_FILE_BYTES {
        return bounded("raw file is empty or exceeds 2TiB");
    }
    if destination_length()? != 0 {
        return invalid("destination must be an empty regular file");
    }
    let mut jump_table = Vec::new();
    jump_table
        .try_reserve_exact(jump_bytes as usize)
        .map_err(|error| ZfileError::Bound(error.to_string()))?;
    let header = metadata(true, WRITE_BLOCK as u32, true, 0, 0, 0, 0);
    write(&header, 0)?;
    let mut digest = Sha256::new();
    digest.update(&header);
    let mut raw_offset = 0_u64;
    let mut encoded_offset = SPACE as u64;
    let mut compressed = vec![0; compress_bound(WRITE_BLOCK) + 4];
    let mut batch = Vec::with_capacity(
        READ_BATCH
            + READ_BATCH / 255
            + 16 * (READ_BATCH / WRITE_BLOCK)
            + 4 * (READ_BATCH / WRITE_BLOCK),
    );
    while raw_offset < raw_length {
        let take = (raw_length - raw_offset).min(READ_BATCH as u64) as usize;
        let raw = reader(raw_offset, take).map_err(ZfileError::Source)?;
        if raw.len() != take {
            return invalid("reader returned a short or oversized source batch");
        }
        batch.clear();
        for block in raw.chunks(WRITE_BLOCK) {
            // SAFETY: input/output are live disjoint slices, all sizes fit C
            // int, and capacity includes the documented LZ4 compression bound.
            let length = unsafe {
                lz4_sys::LZ4_compress_default(
                    block.as_ptr().cast(),
                    compressed.as_mut_ptr().cast(),
                    block.len() as i32,
                    (compressed.len() - 4) as i32,
                )
            };
            if length <= 0 || length as usize > compressed.len() - 4 {
                return invalid("LZ4 compression failed");
            }
            let length = length as usize;
            let checksum = crc(&compressed[..length], 100_007);
            compressed[length..length + 4].copy_from_slice(&checksum.to_le_bytes());
            batch.extend_from_slice(&compressed[..length + 4]);
            jump_table.extend_from_slice(&((length + 4) as u32).to_le_bytes());
        }
        write(&batch, encoded_offset)?;
        digest.update(&batch);
        encoded_offset = add(encoded_offset, batch.len() as u64)?;
        raw_offset += take as u64;
    }
    let index_offset = encoded_offset;
    write(&jump_table, index_offset)?;
    digest.update(&jump_table);
    encoded_offset = add(encoded_offset, jump_table.len() as u64)?;
    let trailer = metadata(
        false,
        WRITE_BLOCK as u32,
        true,
        index_offset,
        count,
        raw_length,
        crc(&jump_table, 0),
    );
    write(&trailer, encoded_offset)?;
    digest.update(&trailer);
    let index = ZfileIndex::from_owned_mirrors(header, trailer, jump_table)?;
    if destination_length()? != index.encoded_bytes {
        return invalid("encoded destination length differs");
    }
    Ok(EncodedZfile {
        index,
        sha256: format!("sha256:{:x}", digest.finalize()),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    static SEQUENCE: AtomicU64 = AtomicU64::new(0);

    struct TestFile {
        file: File,
        path: PathBuf,
    }

    impl TestFile {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "rock-zfile-{}-{}",
                std::process::id(),
                SEQUENCE.fetch_add(1, Ordering::Relaxed)
            ));
            let file = File::options()
                .read(true)
                .write(true)
                .create_new(true)
                .open(&path)
                .unwrap();
            Self { file, path }
        }

        fn bytes(&self) -> Vec<u8> {
            let mut bytes = vec![0; self.file.metadata().unwrap().len() as usize];
            self.file.read_exact_at(&mut bytes, 0).unwrap();
            bytes
        }
    }

    impl Drop for TestFile {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.path);
        }
    }

    fn raw_bytes(length: usize) -> Vec<u8> {
        let mut state = 0x8f2a_47b1_u32;
        (0..length)
            .map(|position| {
                state ^= state << 13;
                state ^= state >> 17;
                state ^= state << 5;
                if position % 8192 < 1024 {
                    0
                } else {
                    state as u8
                }
            })
            .collect()
    }

    // Independently assemble supported producer variants that the production
    // writer intentionally does not emit (block sizes and optional digests).
    fn fixture(raw: &[u8], block_size: usize, verify: bool, digest: bool) -> (ZfileIndex, Vec<u8>) {
        let mut bytes = vec![0; SPACE];
        let mut table = Vec::new();
        let mut compressed = vec![0; compress_bound(block_size)];
        for block in raw.chunks(block_size) {
            // SAFETY: fixed <=64KiB inputs and LZ4_compressBound output.
            let count = unsafe {
                lz4_sys::LZ4_compress_default(
                    block.as_ptr().cast(),
                    compressed.as_mut_ptr().cast(),
                    block.len() as i32,
                    compressed.len() as i32,
                )
            };
            assert!(count > 0);
            let compressed = &compressed[..count as usize];
            bytes.extend_from_slice(compressed);
            if verify {
                bytes.extend_from_slice(&crc(compressed, 100_007).to_le_bytes());
            }
            table.extend_from_slice(&(count as u32 + if verify { 4 } else { 0 }).to_le_bytes());
        }
        let mut header = metadata(true, block_size as u32, verify, 0, 0, 0, 0);
        let mut trailer = metadata(
            false,
            block_size as u32,
            verify,
            bytes.len() as u64,
            table.len() as u64 / 4,
            raw.len() as u64,
            crc(&table, 0),
        );
        if !digest {
            header[28..32].fill(0);
            header[32..40].copy_from_slice(&(HEADER | DATA).to_le_bytes());
            trailer[28..32].fill(0);
            trailer[32..40].copy_from_slice(&(SEALED | DATA).to_le_bytes());
            trailer[64..68].fill(0);
        }
        bytes[..SPACE].copy_from_slice(&header);
        bytes.extend_from_slice(&table);
        bytes.extend_from_slice(&trailer);
        (
            ZfileIndex::from_owned_mirrors(header, trailer, table).unwrap(),
            bytes,
        )
    }

    fn fix_digest(metadata: &mut [u8]) {
        metadata[28..32].fill(0);
        let digest = crc(metadata, 0);
        metadata[28..32].copy_from_slice(&digest.to_le_bytes());
    }

    fn fix_index(index: &mut ZfileIndex) {
        index.trailer[64..68].copy_from_slice(&crc(&index.jump_table, 0).to_le_bytes());
        fix_digest(&mut index.trailer);
    }

    #[test]
    fn raw_crc32c_matches_upstream_known_vectors() {
        assert_eq!(crc(b"", 0), 0);
        assert_eq!(crc(b"123456789", 0), 0x58e3_fa20);
        assert_eq!(crc(b"123456789", 100_007), 0x3c9b_d4e0);
    }

    #[test]
    fn writer_streams_bounded_batches_and_emits_standard_layout_and_full_sha() {
        let raw = raw_bytes(READ_BATCH + 4096 + 37);
        let output = TestFile::new();
        let mut calls = Vec::new();
        let encoded = encode_with_reader(raw.len() as u64, &output.file, |offset, count| {
            assert!(count <= READ_BATCH);
            calls.push((offset, count));
            Ok(raw[offset as usize..offset as usize + count].to_vec())
        })
        .unwrap();
        assert_eq!(calls.len(), 2);
        assert_eq!(encoded.index.block_size, 4096);
        assert!(encoded.index.verify);
        assert_eq!(u64_at(&encoded.index.header, 32).unwrap(), 19);
        assert_eq!(u64_at(&encoded.index.trailer, 32).unwrap(), 22);
        assert_eq!(encoded.index.jump_table.len(), raw.len().div_ceil(4096) * 4);
        let bytes = output.bytes();
        assert_eq!(
            encoded.sha256,
            format!("sha256:{:x}", Sha256::digest(&bytes))
        );
        for (offset, mirror) in encoded.index.metadata_regions() {
            assert_eq!(
                &bytes[offset as usize..offset as usize + mirror.len()],
                mirror
            );
        }
        assert_eq!(
            encoded
                .index
                .decode_range(0, raw.len() as u64, 0, &bytes)
                .unwrap(),
            raw
        );
        let rebuilt = ZfileIndex::from_mirrors(
            &encoded.index.header,
            &encoded.index.trailer,
            &encoded.index.jump_table,
        )
        .unwrap();
        assert_eq!(encoded.index, rebuilt);
        assert!(rebuilt.retained_bytes() < rebuilt.jump_table.len() + 16 * 1024);
    }

    #[test]
    fn all_block_sizes_verify_modes_and_tail_ranges_decode_exactly() {
        for block_size in [4096, 8192, 16384, 32768, 65536] {
            let raw = raw_bytes(block_size * 3 + 17);
            for verify in [false, true] {
                let (index, bytes) = fixture(&raw, block_size, verify, true);
                for (offset, length) in [
                    (0, 1),
                    (block_size - 1, 2),
                    (block_size / 2, block_size * 2),
                    (raw.len() - 19, 19),
                    (0, raw.len()),
                ] {
                    let span = index.compressed_span(offset as u64, length as u64).unwrap();
                    let payload =
                        &bytes[span.offset as usize..(span.offset + span.length) as usize];
                    assert_eq!(
                        span.decode(payload).unwrap(),
                        raw[offset..offset + length],
                        "bs={block_size}, verify={verify}"
                    );
                    assert_eq!(
                        index
                            .decode_range(offset as u64, length as u64, span.offset, payload)
                            .unwrap(),
                        raw[offset..offset + length]
                    );
                }
            }
        }
    }

    #[test]
    fn acr_legacy_metadata_without_digests_still_verifies_every_data_block() {
        let raw = raw_bytes(4096 * 2 + 91);
        let (index, mut bytes) = fixture(&raw, 4096, true, false);
        assert_eq!(u64_at(&index.header, 32).unwrap(), 3);
        assert_eq!(u64_at(&index.trailer, 32).unwrap(), 6);
        assert_eq!(u32_at(&index.trailer, 64).unwrap(), 0);
        assert_eq!(
            index.decode_range(0, raw.len() as u64, 0, &bytes).unwrap(),
            raw
        );
        bytes[SPACE + 2] ^= 1;
        assert!(index.decode_range(0, 4096, 0, &bytes).is_err());
    }

    #[test]
    fn metadata_and_index_crc_corruption_are_rejected() {
        let (index, _) = fixture(&raw_bytes(5000), 4096, true, true);
        for region in 0..3 {
            let mut bad = index.clone();
            match region {
                0 => bad.header[56] ^= 1,
                1 => bad.trailer[56] ^= 1,
                _ => bad.jump_table[0] ^= 1,
            }
            assert!(
                ZfileIndex::from_owned_mirrors(bad.header, bad.trailer, bad.jump_table).is_err()
            );
        }
        for length in [0, 7, 511, 513] {
            let result = std::panic::catch_unwind(|| {
                ZfileIndex::from_owned_mirrors(
                    vec![0; length],
                    index.trailer.clone(),
                    index.jump_table.clone(),
                )
            });
            assert!(result.is_ok_and(|parsed| parsed.is_err()));
            assert!(
                ZfileIndex::from_mirrors(&index.header, &vec![0; length], &index.jump_table)
                    .is_err()
            );
        }
    }

    #[test]
    fn valid_crc_cannot_authorize_bad_flags_dictionary_or_jump_geometry() {
        let (index, _) = fixture(&raw_bytes(9000), 4096, true, true);
        for (offset, value) in [
            (32, 54_u8),
            (68, 1),
            (76, 2),
            (78, 1),
            (84, 1),
            (88, 2),
            (511, 1),
        ] {
            let mut bad = index.clone();
            bad.trailer[offset] = value;
            fix_digest(&mut bad.trailer);
            assert!(
                ZfileIndex::from_owned_mirrors(bad.header, bad.trailer, bad.jump_table).is_err(),
                "offset={offset}"
            );
        }
        for length in [0_u32, 4, u32::MAX] {
            let mut bad = index.clone();
            bad.jump_table[..4].copy_from_slice(&length.to_le_bytes());
            fix_index(&mut bad);
            assert!(
                ZfileIndex::from_owned_mirrors(bad.header, bad.trailer, bad.jump_table).is_err()
            );
        }
        let mut bad = index.clone();
        let offset = bad.index_offset + 1;
        bad.trailer[40..48].copy_from_slice(&offset.to_le_bytes());
        fix_digest(&mut bad.trailer);
        assert!(ZfileIndex::from_owned_mirrors(bad.header, bad.trailer, bad.jump_table).is_err());
    }

    #[test]
    fn overwritten_header_must_agree_with_trailer() {
        let (mut index, _) = fixture(&raw_bytes(8193), 4096, true, true);
        index.header[40..72].copy_from_slice(&index.trailer[40..72]);
        index.header[32] |= INFO_VALID as u8;
        fix_digest(&mut index.header);
        ZfileIndex::from_mirrors(&index.header, &index.trailer, &index.jump_table).unwrap();
        index.header[56] ^= 1;
        fix_digest(&mut index.header);
        assert!(
            ZfileIndex::from_owned_mirrors(index.header, index.trailer, index.jump_table).is_err()
        );
    }

    #[test]
    fn truncated_spans_bounds_and_wrong_tail_decode_length_fail() {
        let raw = raw_bytes(4096 + 17);
        let (mut index, bytes) = fixture(&raw, 4096, false, true);
        let span = index.compressed_span(4090, 23).unwrap();
        let payload = &bytes[span.offset as usize..(span.offset + span.length) as usize];
        assert!(span.decode(&payload[..payload.len() - 1]).is_err());
        assert!(index
            .decode_range(4090, 23, span.offset + 1, payload)
            .is_err());
        assert!(index.compressed_span(u64::MAX, 2).is_err());
        assert!(index.compressed_span(0, MAX_RAW_RANGE_BYTES + 1).is_err());
        assert!(index.compressed_span(raw.len() as u64 + 1, 0).is_err());
        assert!(index
            .compressed_span(raw.len() as u64, 0)
            .unwrap()
            .decode(&[])
            .unwrap()
            .is_empty());
        index.trailer[56..64].copy_from_slice(&(raw.len() as u64 + 1).to_le_bytes());
        fix_digest(&mut index.trailer);
        let bad =
            ZfileIndex::from_owned_mirrors(index.header, index.trailer, index.jump_table).unwrap();
        assert!(bad.decode_range(4096, 18, 0, &bytes).is_err());
    }

    #[test]
    fn writer_bounds_short_sources_and_existing_destination_fail_without_false_success() {
        let output = TestFile::new();
        let too_large = (MAX_JUMP_TABLE_BYTES as u64 / 4) * WRITE_BLOCK as u64 + 1;
        assert!(matches!(
            encode_with_reader(too_large, &output.file, |_, _| panic!(
                "must preflight before IO"
            )),
            Err(ZfileError::Bound(_))
        ));
        assert_eq!(output.file.metadata().unwrap().len(), 0);
        assert!(encode_with_reader(5000, &output.file, |_, count| Ok(vec![0; count - 1])).is_err());
        let output = TestFile::new();
        output.file.write_all_at(b"existing", 0).unwrap();
        assert!(encode_with_reader(4096, &output.file, |_, count| Ok(vec![0; count])).is_err());
        assert_eq!(output.bytes(), b"existing");
    }

    #[test]
    fn file_wrapper_matches_closure_writer_and_rejects_source_aliases() {
        let raw = raw_bytes(16384 + 7);
        let source = TestFile::new();
        source.file.write_all_at(&raw, 0).unwrap();
        let output = TestFile::new();
        let encoded = encode_file(&source.file, raw.len() as u64, &output.file).unwrap();
        assert_eq!(
            encoded
                .index
                .decode_range(0, raw.len() as u64, 0, &output.bytes())
                .unwrap(),
            raw
        );
        assert!(encode_file(&source.file, raw.len() as u64 - 1, &output.file).is_err());
        assert!(encode_file(&source.file, raw.len() as u64, &source.file).is_err());
    }
}
