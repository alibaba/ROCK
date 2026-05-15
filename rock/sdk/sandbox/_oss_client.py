"""OssClient — encapsulates all OSS interactions for a Sandbox.

Holds OSS state (bucket, token expiration, async persistence tasks) and
exposes upload / download / persistence operations. Composed by Sandbox.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from rock import env_vars
from rock.logger import init_logger
from rock.utils.http import HttpUtils

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

    @staticmethod
    def _resolve_config(sts_response: dict) -> OssClientConfig | None:
        # Layer 1: env var (highest priority)
        env_endpoint = env_vars.ROCK_OSS_BUCKET_ENDPOINT
        env_bucket = env_vars.ROCK_OSS_BUCKET_NAME
        env_region = env_vars.ROCK_OSS_BUCKET_REGION
        if env_endpoint and env_bucket and env_region:
            return OssClientConfig(
                endpoint=env_endpoint,
                bucket=env_bucket,
                region=env_region,
                enabled_via_env=True,
            )

        # Layer 2: server response (fallback default)
        resp_endpoint = sts_response.get("Endpoint")
        resp_bucket = sts_response.get("Bucket")
        resp_region = sts_response.get("Region")
        if resp_endpoint and resp_bucket and resp_region:
            return OssClientConfig(
                endpoint=resp_endpoint,
                bucket=resp_bucket,
                region=resp_region,
                enabled_via_env=False,
            )

        # Layer 3: OSS unavailable
        return None

    async def _get_sts_credentials(self) -> dict:
        """Fetch STS credentials and OSS config from /get_token endpoint.

        Returns the entire response result dict, which may include:
        - STS creds: AccessKeyId, AccessKeySecret, SecurityToken, Expiration
        - OSS config (if server is new + configured): Endpoint, Bucket, Region

        Side effect: caches Expiration in self._token_expire_time.
        """
        url = f"{self._sandbox._url}/get_token"
        headers = self._sandbox._build_headers()
        response = await HttpUtils.get(url, headers)
        if response["status"] != "Success":
            raise Exception(f"Failed to get OSS STS token: {response.get('message', 'Unknown error')}")
        credentials = response["result"]
        self._token_expire_time = credentials["Expiration"]
        return credentials

    def _is_token_expired(self) -> bool:
        """Whether cached token is missing or expired (per Expiration field)."""
        if not self._token_expire_time:
            return True
        try:
            # Aliyun STS Expiration format: "2026-12-31T00:00:00Z"
            exp = datetime.strptime(self._token_expire_time, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return True
        return datetime.now(timezone.utc) >= exp
