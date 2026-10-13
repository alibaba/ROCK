"""HTTP content-coding helpers kept below the SDK business API."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import zstandard

ZSTD_ENCODING = "zstd"
ZSTD_MIN_SIZE_BYTES = 64 * 1024
ZSTD_LEVEL = 3
ACCEPT_ENCODING = "zstd, deflate"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EncodedJSONBody:
    content: bytes
    content_encoding: str | None
    original_size: int


def serialize_json(value: Any) -> bytes:
    """Match HTTPX's compact UTF-8 JSON representation."""

    if isinstance(value, bytes):
        return value
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def encode_json_body(
    value: Any,
    *,
    minimum_size: int = ZSTD_MIN_SIZE_BYTES,
    compression_level: int = ZSTD_LEVEL,
) -> EncodedJSONBody:
    """Compress JSON wire bytes only when their original size exceeds the threshold."""

    raw = serialize_json(value)
    if len(raw) <= minimum_size:
        return EncodedJSONBody(
            content=raw,
            content_encoding=None,
            original_size=len(raw),
        )

    compressed = zstandard.ZstdCompressor(
        level=compression_level,
        write_checksum=True,
    ).compress(raw)
    logger.debug(
        "zstd request original_bytes=%d compressed_bytes=%d",
        len(raw),
        len(compressed),
    )
    return EncodedJSONBody(
        content=compressed,
        content_encoding=ZSTD_ENCODING,
        original_size=len(raw),
    )
