"""OssClient — encapsulates all OSS interactions for a Sandbox.

Holds OSS state (bucket, token expiration, async persistence tasks) and
exposes upload / download / persistence operations. Composed by Sandbox.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rock.logger import init_logger

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox

logger = init_logger(__name__)


@dataclass
class OssClientConfig:
    """Resolved OSS configuration (Layer 1 env or Layer 2 server)."""

    endpoint: str
    bucket: str
    region: str
    enabled_via_env: bool  # True = Layer 1 (受 ROCK_OSS_ENABLE 控制); False = Layer 2


class OssClient:
    """OSS operations for a single Sandbox instance."""

    def __init__(self, sandbox: Sandbox):
        self._sandbox = sandbox
        self._bucket = None
        self._token_expire_time: str | None = None
        self._client_config: OssClientConfig | None = None
        self._pending_persistence_tasks: set[asyncio.Task] = set()

    @staticmethod
    def _compute_object_name(sandbox_id: str, local_path: str, sandbox_path: str) -> str:
        payload = f"{sandbox_id}|{local_path}|{sandbox_path}"
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        filename = Path(local_path).name or Path(sandbox_path).name
        return f"{digest}-{filename}"
