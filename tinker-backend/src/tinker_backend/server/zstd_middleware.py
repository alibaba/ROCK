"""Transparent zstd content coding for Tinker HTTP JSON traffic."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterable
from typing import Any

import zstandard
from starlette.types import ASGIApp, Message, Receive, Scope, Send

ZSTD_ENCODING = "zstd"
ZSTD_MIN_SIZE_BYTES = 64 * 1024
ZSTD_LEVEL = 3
MAX_COMPRESSED_REQUEST_BYTES = 40 * 1024 * 1024
MAX_DECOMPRESSED_REQUEST_BYTES = 40 * 1024 * 1024

logger = logging.getLogger(__name__)


class _InvalidZstdBody(ValueError):
    pass


class _RequestBodyTooLarge(ValueError):
    pass


def _get_header(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> bytes | None:
    expected = name.lower()
    for key, value in headers:
        if key.lower() == expected:
            return value
    return None


def _set_header(
    headers: Iterable[tuple[bytes, bytes]],
    name: bytes,
    value: bytes,
) -> list[tuple[bytes, bytes]]:
    expected = name.lower()
    updated = [(key, item) for key, item in headers if key.lower() != expected]
    updated.append((name, value))
    return updated


def _remove_headers(
    headers: Iterable[tuple[bytes, bytes]],
    *names: bytes,
) -> list[tuple[bytes, bytes]]:
    expected = {name.lower() for name in names}
    return [(key, value) for key, value in headers if key.lower() not in expected]


def _append_vary_accept_encoding(
    headers: Iterable[tuple[bytes, bytes]],
) -> list[tuple[bytes, bytes]]:
    existing = _get_header(headers, b"vary")
    if existing is None:
        return [*headers, (b"vary", b"Accept-Encoding")]
    values = [item.strip() for item in existing.decode("latin-1").split(",")]
    if any(item.lower() == "accept-encoding" for item in values):
        return list(headers)
    return _set_header(headers, b"vary", existing + b", Accept-Encoding")


def _accepts_zstd(value: bytes | None) -> bool:
    if value is None:
        return False
    wildcard_quality: float | None = None
    for raw_item in value.decode("latin-1").split(","):
        parts = [part.strip() for part in raw_item.split(";")]
        coding = parts[0].lower()
        quality = 1.0
        for parameter in parts[1:]:
            key, separator, raw_value = parameter.partition("=")
            if separator and key.strip().lower() == "q":
                try:
                    quality = float(raw_value.strip())
                except ValueError:
                    quality = 0.0
        if coding == ZSTD_ENCODING:
            return quality > 0
        if coding == "*":
            wildcard_quality = quality
    return wildcard_quality is not None and wildcard_quality > 0


def _is_json_content_type(value: bytes | None) -> bool:
    if value is None:
        return False
    media_type = value.decode("latin-1").split(";", 1)[0].strip().lower()
    return media_type == "application/json" or media_type.endswith("+json")


async def _read_request_body(receive: Receive, *, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise _InvalidZstdBody(
                "client disconnected before sending the complete request"
            )
        if message["type"] != "http.request":
            continue
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > max_bytes:
            raise _RequestBodyTooLarge(
                "compressed request body exceeds the configured limit"
            )
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


def _decompress_request_body(payload: bytes, *, max_bytes: int) -> bytes:
    output = bytearray()
    try:
        with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(payload)) as reader:
            while True:
                chunk = reader.read(64 * 1024)
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > max_bytes:
                    raise _RequestBodyTooLarge(
                        "decompressed request body exceeds the configured limit"
                    )
    except _RequestBodyTooLarge:
        raise
    except zstandard.ZstdError as exc:
        raise _InvalidZstdBody("request body is not a valid zstd frame") from exc
    return bytes(output)


async def _send_error(send: Send, status_code: int, detail: str) -> None:
    body = json.dumps({"detail": detail}, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


class _ZstdResponseSender:
    def __init__(
        self,
        send: Send,
        *,
        accepts_zstd: bool,
        request_method: str,
        minimum_size: int,
        compression_level: int,
        request_path: str,
    ) -> None:
        self._send = send
        self._accepts_zstd = accepts_zstd
        self._request_method = request_method
        self._minimum_size = minimum_size
        self._compression_level = compression_level
        self._request_path = request_path
        self._start: Message | None = None
        self._buffer = bytearray()
        self._compressor: Any | None = None
        self._original_size = 0
        self._compressed_size = 0
        self._passthrough = False

    async def __call__(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self._start = message
            if not self._is_candidate(message):
                self._passthrough = True
                await self._send(message)
            return

        if message["type"] != "http.response.body" or self._passthrough:
            await self._send(message)
            return

        if self._start is None:
            raise RuntimeError("received response body before response start")

        body = message.get("body", b"")
        self._original_size += len(body)
        more_body = message.get("more_body", False)

        if self._compressor is not None:
            await self._send_compressed_body(body, more_body=more_body)
            return

        self._buffer.extend(body)
        if len(self._buffer) <= self._minimum_size:
            if more_body:
                return
            await self._send(self._start)
            await self._send(
                {
                    "type": "http.response.body",
                    "body": bytes(self._buffer),
                    "more_body": False,
                }
            )
            return

        headers = _remove_headers(self._start.get("headers", []), b"content-length")
        headers = _set_header(
            headers, b"content-encoding", ZSTD_ENCODING.encode("ascii")
        )
        headers = _append_vary_accept_encoding(headers)
        start = dict(self._start)
        start["headers"] = headers
        await self._send(start)
        self._compressor = zstandard.ZstdCompressor(
            level=self._compression_level,
            write_checksum=True,
        ).compressobj()
        buffered = bytes(self._buffer)
        self._buffer.clear()
        await self._send_compressed_body(buffered, more_body=more_body)

    def _is_candidate(self, start: Message) -> bool:
        status = int(start["status"])
        headers = start.get("headers", [])
        return (
            self._accepts_zstd
            and self._request_method != "HEAD"
            and status >= 200
            and status not in {204, 304}
            and _is_json_content_type(_get_header(headers, b"content-type"))
            and _get_header(headers, b"content-encoding") is None
            and _get_header(headers, b"content-range") is None
        )

    async def _send_compressed_body(self, body: bytes, *, more_body: bool) -> None:
        assert self._compressor is not None
        compressed = self._compressor.compress(body)
        if not more_body:
            compressed += self._compressor.flush()
        self._compressed_size += len(compressed)
        await self._send(
            {
                "type": "http.response.body",
                "body": compressed,
                "more_body": more_body,
            }
        )
        if not more_body:
            logger.debug(
                "zstd response path=%s original_bytes=%d compressed_bytes=%d",
                self._request_path,
                self._original_size,
                self._compressed_size,
            )


class ZstdMiddleware:
    """Decode zstd requests and encode large JSON responses."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        minimum_size: int = ZSTD_MIN_SIZE_BYTES,
        compression_level: int = ZSTD_LEVEL,
        max_compressed_request_bytes: int = MAX_COMPRESSED_REQUEST_BYTES,
        max_decompressed_request_bytes: int = MAX_DECOMPRESSED_REQUEST_BYTES,
    ) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.compression_level = compression_level
        self.max_compressed_request_bytes = max_compressed_request_bytes
        self.max_decompressed_request_bytes = max_decompressed_request_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers", [])
        content_encodings = [
            value.strip().lower()
            for key, value in headers
            if key.lower() == b"content-encoding"
        ]
        if len(content_encodings) > 1 or any(
            b"," in value for value in content_encodings
        ):
            await _send_error(
                send, 415, "multiple Content-Encoding values are not supported"
            )
            return
        normalized_encoding = content_encodings[0] if content_encodings else b""
        if normalized_encoding not in {b"", b"identity", b"zstd"}:
            await _send_error(send, 415, "unsupported Content-Encoding; expected zstd")
            return

        decoded_receive = receive
        if normalized_encoding == b"zstd":
            try:
                compressed = await _read_request_body(
                    receive,
                    max_bytes=self.max_compressed_request_bytes,
                )
                body = _decompress_request_body(
                    compressed,
                    max_bytes=self.max_decompressed_request_bytes,
                )
            except _RequestBodyTooLarge as exc:
                await _send_error(send, 413, str(exc))
                return
            except _InvalidZstdBody as exc:
                await _send_error(send, 400, str(exc))
                return

            updated_scope = dict(scope)
            updated_headers = _remove_headers(
                headers,
                b"content-encoding",
                b"content-length",
            )
            updated_headers.append((b"content-length", str(len(body)).encode("ascii")))
            updated_scope["headers"] = updated_headers
            scope = updated_scope
            delivered = False

            async def receive_decoded() -> Message:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            decoded_receive = receive_decoded
            logger.debug(
                "zstd request path=%s compressed_bytes=%d original_bytes=%d",
                scope.get("path", ""),
                len(compressed),
                len(body),
            )

        response_sender = _ZstdResponseSender(
            send,
            accepts_zstd=_accepts_zstd(_get_header(headers, b"accept-encoding")),
            request_method=scope.get("method", ""),
            minimum_size=self.minimum_size,
            compression_level=self.compression_level,
            request_path=scope.get("path", ""),
        )
        await self.app(scope, decoded_receive, response_sender)
