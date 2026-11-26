//! Bounded async framing for the peer-to-peer process boundary.

use std::io;

use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};

use super::protocol::{P2PBinaryFrame, P2P_TCP_MAX_FRAME_BYTES};

pub async fn read_tcp_frame<R, T>(reader: &mut R) -> io::Result<T>
where
    R: AsyncRead + Unpin,
    T: P2PBinaryFrame,
{
    let length = reader.read_u32().await? as usize;
    if length == 0 || length > P2P_TCP_MAX_FRAME_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "P2P TCP frame length is invalid",
        ));
    }
    let mut payload = vec![0_u8; length];
    reader.read_exact(&mut payload).await?;
    T::from_binary(&payload)
}

pub async fn write_tcp_frame<W, T>(writer: &mut W, value: &T) -> io::Result<()>
where
    W: AsyncWrite + Unpin,
    T: P2PBinaryFrame,
{
    let payload = value.to_binary()?;
    if payload.is_empty() || payload.len() > P2P_TCP_MAX_FRAME_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "P2P TCP frame length is invalid",
        ));
    }
    writer.write_u32(payload.len() as u32).await?;
    writer.write_all(&payload).await
}
