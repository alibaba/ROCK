"""OssClient — encapsulates all OSS interactions for a Sandbox.

Holds OSS state (bucket, token expiration, async persistence tasks) and
exposes upload / download / persistence operations. Composed by Sandbox.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import oss2

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
        """Whether cached token is missing, malformed, or within 5min of expiration."""
        try:
            expire_time = datetime.fromisoformat(self._token_expire_time.replace("Z", "+00:00"))
            current_time = datetime.now(timezone.utc)
            effective_expire_time = expire_time - timedelta(minutes=5)
            return current_time >= effective_expire_time
        except (ValueError, AttributeError):
            return True

    @property
    def is_available(self) -> bool:
        """OSS 是否可用：bucket 已成功初始化。"""
        return self._bucket is not None

    async def ensure_setup(self) -> bool:
        """Ensure OSS bucket is set up and token is fresh. Idempotent.

        Returns True if OSS is available, False otherwise.
        """
        if self._bucket is not None and not self._is_token_expired():
            return True
        return await self._setup()

    async def _setup(self) -> bool:
        try:
            sts_response = await self._get_sts_credentials()
        except Exception as e:
            logger.warning("Failed to get STS credentials: %s", e)
            return False

        config = self._resolve_config(sts_response)
        if config is None:
            return False

        # Layer 1 还要看 ROCK_OSS_ENABLE
        if config.enabled_via_env and not env_vars.ROCK_OSS_ENABLE:
            return False

        try:
            auth = oss2.StsAuth(
                sts_response["AccessKeyId"],
                sts_response["AccessKeySecret"],
                sts_response["SecurityToken"],
            )
            self._bucket = oss2.Bucket(
                auth=auth,
                endpoint=config.endpoint,
                bucket_name=config.bucket,
                region=config.region,
            )
            self._client_config = config
            return True
        except Exception as e:
            logger.warning("Failed to initialize OSS bucket: %s", e)
            self._bucket = None
            return False
