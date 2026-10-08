from __future__ import annotations

import asyncio
import json
import unittest

import httpx
import zstandard

from tinker._base_client import AsyncAPIClient
from tinker._client import AsyncTinker
from tinker._content_encoding import (
    ACCEPT_ENCODING,
    ZSTD_MIN_SIZE_BYTES,
    serialize_json,
)
from tinker._models import FinalRequestOptions


def _build_post_request(client: AsyncTinker, payload: object) -> httpx.Request:
    options = FinalRequestOptions.construct(
        method="post",
        url="/transport-test",
        json_data=payload,
    )
    return AsyncAPIClient._build_request(client, options)


class ZstdTransportTest(unittest.TestCase):
    def test_accept_encoding_excludes_gzip(self) -> None:
        self.assertEqual(ACCEPT_ENCODING, "zstd, deflate")

    def test_compresses_only_above_64_kib(self) -> None:
        client = AsyncTinker(api_key="tml-test", base_url="http://test")
        try:
            boundary = _build_post_request(client, b"x" * ZSTD_MIN_SIZE_BYTES)
            above = _build_post_request(client, b"x" * (ZSTD_MIN_SIZE_BYTES + 1))

            self.assertNotIn("content-encoding", boundary.headers)
            self.assertEqual(boundary.content, b"x" * ZSTD_MIN_SIZE_BYTES)
            self.assertEqual(above.headers["content-encoding"], "zstd")
            self.assertEqual(
                zstandard.ZstdDecompressor().decompress(above.content),
                b"x" * (ZSTD_MIN_SIZE_BYTES + 1),
            )
            self.assertEqual(
                int(above.headers["content-length"]),
                len(above.content),
            )
        finally:
            asyncio.run(client.close())

    def test_large_json_round_trips_without_changing_values(self) -> None:
        client = AsyncTinker(api_key="tml-test", base_url="http://test")
        payload = {
            "messages": [{"role": "user", "content": "你好" * 40_000}],
            "temperature": 0.8,
        }
        try:
            request = _build_post_request(client, payload)

            self.assertEqual(request.headers["content-encoding"], "zstd")
            raw = zstandard.ZstdDecompressor().decompress(request.content)
            self.assertEqual(json.loads(raw), payload)
            self.assertEqual(request.headers["accept-encoding"], ACCEPT_ENCODING)
        finally:
            asyncio.run(client.close())

    def test_small_json_uses_identity_with_httpx_compatible_encoding(self) -> None:
        client = AsyncTinker(api_key="tml-test", base_url="http://test")
        payload = {"message": "你好", "values": [1, 2, 3]}
        try:
            request = _build_post_request(client, payload)

            self.assertNotIn("content-encoding", request.headers)
            self.assertEqual(request.content, serialize_json(payload))
        finally:
            asyncio.run(client.close())

    def test_httpx_transparently_decodes_zstd_response(self) -> None:
        payload = {"result": "x" * 100_000, "tokens": list(range(1000))}
        raw = serialize_json(payload)
        compressed = zstandard.ZstdCompressor(level=3).compress(raw)

        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["accept-encoding"], ACCEPT_ENCODING)
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "zstd",
                },
                content=compressed,
            )

        async def run() -> dict[str, object]:
            http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            client = AsyncTinker(
                api_key="tml-test",
                base_url="http://test",
                http_client=http_client,
            )
            try:
                return await client.post(
                    "/transport-test",
                    cast_to=dict[str, object],
                    body={"ping": "pong"},
                )
            finally:
                await client.close()

        self.assertEqual(asyncio.run(run()), payload)

    def test_retries_build_the_same_compressed_json(self) -> None:
        client = AsyncTinker(api_key="tml-test", base_url="http://test")
        payload = {"tokens": list(range(50_000))}
        options = FinalRequestOptions.construct(
            method="post",
            url="/transport-test",
            json_data=payload,
        )
        try:
            first = AsyncAPIClient._build_request(client, options, retries_taken=0)
            retry = AsyncAPIClient._build_request(client, options, retries_taken=1)

            self.assertEqual(first.content, retry.content)
            self.assertEqual(first.headers["content-encoding"], "zstd")
            self.assertEqual(retry.headers["content-encoding"], "zstd")
        finally:
            asyncio.run(client.close())


if __name__ == "__main__":
    unittest.main()
