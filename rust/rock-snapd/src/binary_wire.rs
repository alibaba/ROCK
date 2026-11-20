//! Bounded binary primitives shared by data-plane protocols.
use std::io;

pub(crate) fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

#[derive(Default)]
pub(crate) struct Encoder(pub Vec<u8>);

impl Encoder {
    pub fn byte(&mut self, value: u8) {
        self.0.push(value);
    }
    pub fn u64(&mut self, value: u64) {
        self.0.extend_from_slice(&value.to_le_bytes());
    }
    pub fn boolean(&mut self, value: bool) {
        self.byte(u8::from(value));
    }
    pub fn text(&mut self, value: &str) -> io::Result<()> {
        if value.len() > 4096 {
            return Err(invalid("binary string exceeds 4096 bytes"));
        }
        self.0
            .extend_from_slice(&(value.len() as u32).to_le_bytes());
        self.0.extend_from_slice(value.as_bytes());
        Ok(())
    }
}

pub(crate) struct Decoder<'a>(pub &'a [u8]);

impl<'a> Decoder<'a> {
    pub fn take(&mut self, length: usize) -> io::Result<&'a [u8]> {
        if length > self.0.len() {
            return Err(invalid("truncated binary message"));
        }
        let (value, rest) = self.0.split_at(length);
        self.0 = rest;
        Ok(value)
    }
    pub fn byte(&mut self) -> io::Result<u8> {
        Ok(self.take(1)?[0])
    }
    pub fn u64(&mut self) -> io::Result<u64> {
        Ok(u64::from_le_bytes(self.take(8)?.try_into().unwrap()))
    }
    pub fn boolean(&mut self) -> io::Result<bool> {
        match self.byte()? {
            0 => Ok(false),
            1 => Ok(true),
            _ => Err(invalid("invalid binary bool")),
        }
    }
    pub fn text(&mut self) -> io::Result<String> {
        let length = u32::from_le_bytes(self.take(4)?.try_into().unwrap()) as usize;
        if length > 4096 {
            return Err(invalid("binary string exceeds 4096 bytes"));
        }
        String::from_utf8(self.take(length)?.to_vec()).map_err(|_| invalid("invalid binary UTF-8"))
    }
    pub fn finish(self) -> io::Result<()> {
        if self.0.is_empty() {
            Ok(())
        } else {
            Err(invalid("trailing binary message bytes"))
        }
    }
}
