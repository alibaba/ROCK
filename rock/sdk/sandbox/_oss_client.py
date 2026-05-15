"""OssClient — encapsulates all OSS interactions for a Sandbox.

Holds OSS state (bucket, token expiration, async persistence tasks) and
exposes upload / download / persistence operations. Composed by Sandbox.
"""

from __future__ import annotations

import asyncio
import hashlib
import shlex
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import oss2

from rock import env_vars
from rock.actions.sandbox.request import BashAction, Command, CreateBashSessionRequest
from rock.actions.sandbox.response import CommandResponse, DownloadFileResponse, Observation, UploadResponse
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

    async def upload_via_oss(self, file_path: str, target_path: str) -> UploadResponse:
        """Upload a local file to sandbox via OSS as intermediary (large file path).

        Steps:
            1. Compute deterministic OSS object name
            2. Resumable upload local file to OSS
            3. Sign a temporary GET URL
            4. Inside sandbox: mkdir -p parent + wget the signed URL
            5. Verify target file exists in sandbox
        """
        from rock.sdk.sandbox.client import RunMode  # late import to avoid circular

        if self._bucket is None:
            return UploadResponse(success=False, message="OSS bucket not set up")

        file_name = Path(file_path).name
        oss_object_name = self._compute_object_name(
            sandbox_id=self._sandbox.sandbox_id,
            local_path=file_path,
            sandbox_path=target_path,
        )

        try:
            oss2.resumable_upload(self._bucket, oss_object_name, file_path)
            url = self._bucket.sign_url("GET", oss_object_name, 600, slash_safe=True)

            # mkdir -p target parent (wget -O does not auto-create dirs in NOHUP mode)
            parent_dir = str(Path(target_path).parent)
            await self._sandbox.arun(
                cmd=f"mkdir -p {shlex.quote(parent_dir)}",
                wait_timeout=10,
                mode=RunMode.NORMAL,
            )

            # wget the signed URL
            download_cmd = f"wget -c -O {target_path} '{url}'"
            await self._sandbox.arun(cmd=download_cmd, wait_timeout=600, mode=RunMode.NOHUP)

            # Verify target exists in sandbox
            check_session = f"oss-verify-{oss_object_name}-{time.time_ns()}"
            await self._sandbox.create_session(CreateBashSessionRequest(session=check_session))
            check: Observation = await self._sandbox._run_in_session(
                action=BashAction(command=f"test -f {target_path}", session=check_session)
            )
            if check.exit_code != 0:
                return UploadResponse(
                    success=False,
                    message=f"Failed to upload file {file_name}, sandbox download phase failed",
                )
            return UploadResponse(
                success=True,
                message=f"Successfully uploaded file {file_name} to {target_path}",
            )
        except Exception as e:
            logger.warning("upload_via_oss failed: %s", e)
            return UploadResponse(
                success=False,
                message=f"Failed to upload file {file_name} to {target_path}: {e}",
            )

    async def download_via_oss(self, remote_path: str, local_path: Path) -> DownloadFileResponse:
        """Download file from sandbox to local via OSS as intermediary.

        Note: ensure_setup must succeed before calling. Caller is LinuxFileSystem,
        which holds an `ensure_ossutil` helper for installing ossutil in the sandbox.
        """
        from rock.sdk.sandbox.client import RunMode  # late import

        if self._bucket is None or self._client_config is None:
            return DownloadFileResponse(success=False, message="OSS is not available")

        # Verify source file exists in sandbox (must be regular file)
        check: CommandResponse = await self._sandbox.execute(Command(command=["test", "-f", remote_path]))
        if check.exit_code != 0:
            return DownloadFileResponse(
                success=False,
                message=(
                    f"Source file not found or is not a regular file in sandbox: {remote_path}. "
                    "Note: Only regular files are supported. For directories, create a tar archive first."
                ),
            )

        # Caller (LinuxFileSystem) is responsible for ensure_ossutil before calling here.

        # Refresh STS creds for ossutil (use existing helper, will refresh if expired)
        if self._is_token_expired():
            await self._setup()
        sts_response = await self._get_sts_credentials()
        access_key_id = sts_response["AccessKeyId"]
        access_key_secret = sts_response["AccessKeySecret"]
        security_token = sts_response["SecurityToken"]

        # Upload sandbox file to OSS via ossutil
        oss_object_name = self._compute_object_name(
            sandbox_id=self._sandbox.sandbox_id,
            local_path=str(local_path),
            sandbox_path=remote_path,
        )
        oss_url = f"oss://{self._client_config.bucket}/{oss_object_name}"

        ossutil_inner = (
            f"ossutil cp {shlex.quote(remote_path)} {shlex.quote(oss_url)}"
            f" --access-key-id {shlex.quote(access_key_id)}"
            f" --access-key-secret {shlex.quote(access_key_secret)}"
            f" --sts-token {shlex.quote(security_token)}"
            f" --endpoint {shlex.quote(self._client_config.endpoint)}"
            f" --region {shlex.quote(self._client_config.region)}"
        )
        upload_cmd = f"bash -c {shlex.quote(ossutil_inner)}"
        upload_resp = await self._sandbox.arun(cmd=upload_cmd, mode=RunMode.NOHUP)
        if upload_resp.exit_code != 0:
            return DownloadFileResponse(
                success=False,
                message=f"Failed to upload file to OSS (exit_code={upload_resp.exit_code}): {upload_resp.output}",
            )

        # Download from OSS to local via oss2
        auth = oss2.StsAuth(access_key_id, access_key_secret, security_token)
        bucket = oss2.Bucket(
            auth, self._client_config.endpoint, self._client_config.bucket, region=self._client_config.region
        )
        local = Path(local_path).expanduser().resolve()
        local.parent.mkdir(parents=True, exist_ok=True)
        try:
            bucket.get_object_to_file(oss_object_name, str(local))
        except Exception as e:
            return DownloadFileResponse(success=False, message=f"Failed to download from OSS: {e}")

        if not local.exists():
            return DownloadFileResponse(success=False, message=f"Downloaded file not found at: {local}")

        return DownloadFileResponse(success=True, message=f"Successfully downloaded {remote_path} to {local}")

    async def close(self) -> None:
        """Wait for any pending OSS tasks (placeholder; full impl in async-persistence task)."""
        # Phase D 会替换为：await asyncio.wait_for(asyncio.gather(*self._pending_persistence_tasks, ...), 5.0)
        pass
