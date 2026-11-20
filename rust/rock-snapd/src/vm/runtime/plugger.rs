//! Direct control of the independent guest plugger over its own VSOCK port.
//!
//! This channel does not call Rocklet or use its TCP proxy. Cancellation drops
//! the owned connection. This transport exposes configuration and health only.

use std::io;
use std::path::Path;
use std::time::Duration;

use hyper::body::HttpBody;
use hyper::{Body, Request};
use serde_json::Value;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;

pub const GUEST_PORT: u32 = 22_556;

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GuestReadinessMode {
    #[default]
    Rocklet,
    PluggerOnly,
}
const MAX_CONTROL_BYTES: usize = 64 * 1024;
const MAX_HANDSHAKE_BYTES: usize = 128;

#[derive(Clone, Debug, PartialEq)]
pub struct PluggerReply {
    pub status: u16,
    pub body: Value,
}

pub async fn control(
    socket_path: &Path,
    action: &str,
    payload: &Value,
    timeout: Duration,
) -> io::Result<PluggerReply> {
    if !matches!(action, "configure" | "state" | "health") {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "unknown plugger action",
        ));
    }
    if !payload.is_object() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "plugger payload must be an object",
        ));
    }
    let encoded = serde_json::to_vec(payload).map_err(io::Error::other)?;
    if encoded.len() > MAX_CONTROL_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "plugger payload exceeds 64 KiB",
        ));
    }
    tokio::time::timeout(timeout, async {
        let mut stream = UnixStream::connect(socket_path).await?;
        stream
            .write_all(format!("CONNECT {GUEST_PORT}\n").as_bytes())
            .await?;
        let mut handshake = Vec::new();
        loop {
            let byte = stream.read_u8().await?;
            handshake.push(byte);
            if byte == b'\n' {
                break;
            }
            if handshake.len() >= MAX_HANDSHAKE_BYTES {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "plugger VSOCK handshake is oversized",
                ));
            }
        }
        if !handshake.starts_with(b"OK ") {
            return Err(io::Error::new(
                io::ErrorKind::ConnectionRefused,
                "guest snapd-plugger is unavailable on VSOCK 22556",
            ));
        }
        let (mut sender, connection) = hyper::client::conn::handshake(stream)
            .await
            .map_err(io::Error::other)?;
        let request = Request::builder()
            .method(if matches!(action, "state" | "health") {
                "GET"
            } else {
                "POST"
            })
            .uri(format!("/{action}"))
            .header("host", "snapd-plugger")
            .header("content-type", "application/json")
            .header("connection", "close")
            .body(if matches!(action, "state" | "health") {
                Body::empty()
            } else {
                Body::from(encoded)
            })
            .map_err(io::Error::other)?;
        // Drive the connection and request in this scope, without a detached
        // task that could retain the control socket beyond cancellation.
        let (_, reply) = tokio::join!(async { connection.await }, async {
            let response = sender
                .send_request(request)
                .await
                .map_err(io::Error::other)?;
            let status = response.status().as_u16();
            let mut response = response.into_body();
            let mut bytes = Vec::new();
            while let Some(chunk) = response.data().await {
                let chunk = chunk.map_err(io::Error::other)?;
                if bytes.len() + chunk.len() > MAX_CONTROL_BYTES {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "plugger response exceeds 64 KiB",
                    ));
                }
                bytes.extend_from_slice(&chunk);
            }
            let body: Value = serde_json::from_slice(&bytes).map_err(io::Error::other)?;
            if !body.is_object() {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "plugger response must be an object",
                ));
            }
            Ok(PluggerReply { status, body })
        });
        // A connection's final shutdown can report ENOTCONN after a complete
        // response (notably on Unix sockets). The body reader still rejects
        // truncated transport and invalid JSON; its result determines success.
        reply
    })
    .await
    .map_err(|_| {
        io::Error::new(
            io::ErrorKind::TimedOut,
            "snapd-plugger control deadline expired; plug state may be unknown",
        )
    })?
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::net::UnixListener;

    #[test]
    fn readiness_mode_is_explicit_and_defaults_to_rocklet() {
        assert_eq!(GuestReadinessMode::default(), GuestReadinessMode::Rocklet);
        assert_eq!(
            serde_json::from_str::<GuestReadinessMode>("\"plugger_only\"").unwrap(),
            GuestReadinessMode::PluggerOnly
        );
        assert!(serde_json::from_str::<GuestReadinessMode>("\"fallback\"").is_err());
    }

    #[tokio::test]
    async fn independent_health_does_not_contact_the_rocklet_port() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("vsock.sock");
        let listener = UnixListener::bind(&path).unwrap();
        let peer = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut handshake = Vec::new();
            while handshake.last() != Some(&b'\n') {
                handshake.push(stream.read_u8().await.unwrap());
            }
            assert_eq!(handshake, b"CONNECT 22556\n");
            stream.write_all(b"OK 124\n").await.unwrap();
            let service = hyper::service::service_fn(|request: Request<Body>| async move {
                assert_eq!(request.method(), hyper::Method::GET);
                assert_eq!(request.uri().path(), "/health");
                Ok::<_, std::convert::Infallible>(hyper::Response::new(Body::from(
                    r#"{"closed":false,"configured":false}"#,
                )))
            });
            hyper::server::conn::Http::new()
                .serve_connection(stream, service)
                .await
                .unwrap();
        });
        let reply = control(
            &path,
            "health",
            &serde_json::json!({}),
            Duration::from_secs(1),
        )
        .await
        .unwrap();
        assert_eq!(reply.status, 200);
        assert_eq!(reply.body["closed"], false);
        peer.await.unwrap();
    }

    #[tokio::test]
    async fn independent_port_works_without_a_rocklet_server() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("vsock.sock");
        let listener = UnixListener::bind(&path).unwrap();
        let peer = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut handshake = Vec::new();
            while handshake.last() != Some(&b'\n') {
                handshake.push(stream.read_u8().await.unwrap());
            }
            assert_eq!(handshake, b"CONNECT 22556\n");
            stream.write_all(b"OK 123\n").await.unwrap();
            let service = hyper::service::service_fn(|request: Request<Body>| async move {
                assert_eq!(request.uri().path(), "/configure");
                let payload = hyper::body::to_bytes(request.into_body()).await.unwrap();
                assert_eq!(
                    serde_json::from_slice::<Value>(&payload).unwrap(),
                    serde_json::json!({"upstream_url":"http://relay/inference"})
                );
                Ok::<_, std::convert::Infallible>(hyper::Response::new(Body::from(
                    r#"{"configured":true,"inflight":0}"#,
                )))
            });
            hyper::server::conn::Http::new()
                .serve_connection(stream, service)
                .await
                .unwrap();
        });
        let result = control(
            &path,
            "configure",
            &serde_json::json!({"upstream_url":"http://relay/inference"}),
            Duration::from_secs(2),
        )
        .await
        .unwrap();
        assert_eq!(result.status, 200);
        assert_eq!(result.body["configured"], true);
        peer.await.unwrap();
    }

    #[tokio::test]
    async fn absent_plugger_is_not_retried_through_rocklet() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("vsock.sock");
        let listener = UnixListener::bind(&path).unwrap();
        let peer = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut handshake = Vec::new();
            while handshake.last() != Some(&b'\n') {
                handshake.push(stream.read_u8().await.unwrap());
            }
            assert_eq!(handshake, b"CONNECT 22556\n");
            stream.write_all(b"ERR unavailable\n").await.unwrap();
        });
        let error = control(
            &path,
            "state",
            &serde_json::json!({}),
            Duration::from_secs(1),
        )
        .await
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::ConnectionRefused);
        peer.await.unwrap();
    }

    #[tokio::test]
    async fn deadline_closes_the_independent_socket() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("vsock.sock");
        let listener = UnixListener::bind(&path).unwrap();
        let peer = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut handshake = Vec::new();
            while handshake.last() != Some(&b'\n') {
                handshake.push(stream.read_u8().await.unwrap());
            }
            let mut byte = [0];
            assert_eq!(stream.read(&mut byte).await.unwrap(), 0);
        });
        let error = control(
            &path,
            "configure",
            &serde_json::json!({"upstream_url":"http://relay/inference"}),
            Duration::from_millis(20),
        )
        .await
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
        peer.await.unwrap();
    }
}
