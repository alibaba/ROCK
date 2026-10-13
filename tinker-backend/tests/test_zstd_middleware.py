from __future__ import annotations

import asyncio
import json
import unittest

import httpx
import zstandard
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.responses import StreamingResponse

from tinker_backend.server.zstd_middleware import (
    ZSTD_MIN_SIZE_BYTES,
    ZstdMiddleware,
)


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _make_app(*, max_decompressed_request_bytes: int = 40 * 1024 * 1024) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        ZstdMiddleware,
        max_decompressed_request_bytes=max_decompressed_request_bytes,
    )

    @app.post("/echo")
    async def echo(request: Request) -> object:
        return await request.json()

    @app.get("/json/{size}")
    async def json_body(size: int) -> Response:
        if size < 2:
            raise ValueError("size must leave room for JSON quotes")
        return Response(
            content=b'"' + (b"x" * (size - 2)) + b'"',
            media_type="application/json",
        )

    @app.get("/text/{size}")
    async def text_body(size: int) -> Response:
        return Response(content=b"x" * size, media_type="text/plain")

    @app.get("/error/{size}")
    async def error_body(size: int) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": "x" * size},
        )

    @app.get("/stream")
    async def stream_body() -> StreamingResponse:
        async def chunks():
            yield b'"'
            yield b"x" * ZSTD_MIN_SIZE_BYTES
            yield b'"'

        return StreamingResponse(chunks(), media_type="application/json")

    return app


async def _request(
    app: FastAPI,
    method: str,
    path: str,
    **kwargs: object,
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.request(method, path, **kwargs)


class ZstdMiddlewareTest(unittest.TestCase):
    def test_decodes_zstd_request_before_fastapi_parsing(self) -> None:
        app = _make_app()
        payload = {"value": "hello" * 20_000}
        raw = _json_bytes(payload)
        compressed = zstandard.ZstdCompressor(level=3).compress(raw)

        response = asyncio.run(
            _request(
                app,
                "POST",
                "/echo",
                content=compressed,
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "zstd",
                    "Accept-Encoding": "identity",
                },
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)
        self.assertNotIn("content-encoding", response.headers)

    def test_compresses_only_above_64_kib(self) -> None:
        app = _make_app()

        boundary = asyncio.run(
            _request(
                app,
                "GET",
                f"/json/{ZSTD_MIN_SIZE_BYTES}",
                headers={"Accept-Encoding": "zstd"},
            )
        )
        above = asyncio.run(
            _request(
                app,
                "GET",
                f"/json/{ZSTD_MIN_SIZE_BYTES + 1}",
                headers={"Accept-Encoding": "zstd"},
            )
        )

        self.assertNotIn("content-encoding", boundary.headers)
        self.assertEqual(len(boundary.content), ZSTD_MIN_SIZE_BYTES)
        self.assertEqual(above.headers["content-encoding"], "zstd")
        self.assertEqual(above.headers["vary"], "Accept-Encoding")
        self.assertEqual(len(above.content), ZSTD_MIN_SIZE_BYTES + 1)
        self.assertEqual(above.json(), "x" * (ZSTD_MIN_SIZE_BYTES - 1))

    def test_respects_accept_encoding_and_json_content_type(self) -> None:
        app = _make_app()
        size = ZSTD_MIN_SIZE_BYTES + 1

        declined = asyncio.run(
            _request(
                app,
                "GET",
                f"/json/{size}",
                headers={"Accept-Encoding": "zstd;q=0, identity"},
            )
        )
        text = asyncio.run(
            _request(
                app,
                "GET",
                f"/text/{size}",
                headers={"Accept-Encoding": "zstd"},
            )
        )

        self.assertNotIn("content-encoding", declined.headers)
        self.assertNotIn("content-encoding", text.headers)

    def test_preserves_large_error_response_semantics(self) -> None:
        app = _make_app()
        response = asyncio.run(
            _request(
                app,
                "GET",
                f"/error/{ZSTD_MIN_SIZE_BYTES + 1}",
                headers={"Accept-Encoding": "zstd"},
            )
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.headers["content-encoding"], "zstd")
        self.assertEqual(
            response.json(),
            {"detail": "x" * (ZSTD_MIN_SIZE_BYTES + 1)},
        )

    def test_compresses_streamed_json_after_crossing_threshold(self) -> None:
        app = _make_app()
        response = asyncio.run(
            _request(
                app,
                "GET",
                "/stream",
                headers={"Accept-Encoding": "zstd"},
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-encoding"], "zstd")
        self.assertEqual(response.json(), "x" * ZSTD_MIN_SIZE_BYTES)

    def test_raw_asgi_response_is_a_valid_zstd_frame(self) -> None:
        raw = b'"' + (b"x" * ZSTD_MIN_SIZE_BYTES) + b'"'

        async def inner(_scope, _receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(raw)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": raw, "more_body": False})

        async def run():
            sent = []

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                sent.append(message)

            scope = {
                "type": "http",
                "method": "GET",
                "path": "/wire",
                "headers": [(b"accept-encoding", b"zstd")],
            }
            await ZstdMiddleware(inner)(scope, receive, send)
            return sent

        sent = asyncio.run(run())
        start = sent[0]
        headers = {key.lower(): value for key, value in start["headers"]}
        wire_body = b"".join(message.get("body", b"") for message in sent[1:])
        decoded = zstandard.ZstdDecompressor().decompress(
            wire_body,
            max_output_size=len(raw),
        )

        self.assertEqual(headers[b"content-encoding"], b"zstd")
        self.assertNotIn(b"content-length", headers)
        self.assertEqual(decoded, raw)

    def test_rejects_multiple_content_encoding_values(self) -> None:
        app = _make_app()
        response = asyncio.run(
            _request(
                app,
                "POST",
                "/echo",
                content=b"anything",
                headers=[
                    ("Content-Type", "application/json"),
                    ("Content-Encoding", "zstd"),
                    ("Content-Encoding", "identity"),
                ],
            )
        )

        self.assertEqual(response.status_code, 415)

    def test_rejects_invalid_or_unsupported_content_encoding(self) -> None:
        app = _make_app()

        invalid = asyncio.run(
            _request(
                app,
                "POST",
                "/echo",
                content=b"not-zstd",
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "zstd",
                },
            )
        )
        unsupported = asyncio.run(
            _request(
                app,
                "POST",
                "/echo",
                content=b"anything",
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                },
            )
        )

        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(unsupported.status_code, 415)

    def test_rejects_decompressed_request_over_limit(self) -> None:
        app = _make_app(max_decompressed_request_bytes=1024)
        raw = _json_bytes({"value": "x" * 2048})
        compressed = zstandard.ZstdCompressor(level=3).compress(raw)

        response = asyncio.run(
            _request(
                app,
                "POST",
                "/echo",
                content=compressed,
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "zstd",
                },
            )
        )

        self.assertEqual(response.status_code, 413)


if __name__ == "__main__":
    unittest.main()
