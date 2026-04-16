"""BashTrial — execute a bash script inside a sandbox."""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

from rock import env_vars
from rock.logger import init_logger

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox
from rock.sdk.job.config import BashJobConfig
from rock.sdk.job.result import ExceptionInfo, TrialResult
from rock.sdk.job.trial.abstract import AbstractTrial
from rock.sdk.job.trial.registry import register_trial

logger = init_logger(__name__)


class BashTrial(AbstractTrial):
    """Bash script execution trial."""

    _config: BashJobConfig

    def __init__(self, config: BashJobConfig):
        super().__init__(config)
        self._ossutil_ready: bool = False
        self._oss_credentials: dict | None = None
        self._artifact_dir: str | None = None

    async def setup(self, sandbox: Sandbox) -> None:
        await self._upload_files(sandbox)
        if self._config.script_path:
            self._config.script = Path(self._config.script_path).read_text()

        oss_mirror = self._config.environment.oss_mirror
        if oss_mirror is not None and oss_mirror.enabled:
            await self._setup_oss_mirror(sandbox, oss_mirror)

    async def _setup_oss_mirror(self, sandbox: Sandbox, oss_mirror) -> None:
        if not self._config.namespace:
            raise ValueError("oss_mirror: namespace is not set (sandbox did not return one)")
        if not self._config.experiment_id:
            raise ValueError("oss_mirror: experiment_id is not set (sandbox did not return one)")

        self._artifact_dir = env_vars.ROCK_BASH_JOB_ARTIFACT_DIR

        # Resolve credentials (config field → env var fallback)
        bucket = oss_mirror.oss_bucket or os.environ.get("OSS_BUCKET")
        if not bucket:
            raise ValueError("oss_mirror.enabled=True but oss_bucket is not set (config or OSS_BUCKET env)")

        self._oss_credentials = {
            "oss_bucket": bucket,
            "access_key_id": oss_mirror.oss_access_key_id or os.environ.get("OSS_ACCESS_KEY_ID", ""),
            "access_key_secret": oss_mirror.oss_access_key_secret or os.environ.get("OSS_ACCESS_KEY_SECRET", ""),
            "endpoint": oss_mirror.oss_endpoint or os.environ.get("OSS_ENDPOINT", ""),
            "region": oss_mirror.oss_region or os.environ.get("OSS_REGION", ""),
        }

        # Create artifact dir in sandbox
        from rock.actions.sandbox.request import Command

        await sandbox.execute(Command(command=["mkdir", "-p", self._artifact_dir]))

        # Install ossutil
        self._ossutil_ready = await sandbox.fs.ensure_ossutil()
        if not self._ossutil_ready:
            logger.warning("ossutil install failed, OSS mirror upload will be skipped")
            return

        # Initial upload to ensure OSS path exists before script runs
        await self._upload_artifacts(sandbox)

    def _build_oss_prefix(self) -> str:
        return f"artifacts/{self._config.namespace}/{self._config.experiment_id}/{self._config.job_name}"

    def build(self) -> str:
        lines = ["#!/bin/bash", "set -e", ""]
        if self._config.script:
            lines.append(self._config.script)
        return "\n".join(lines)

    async def collect(self, sandbox: Sandbox, output: str, exit_code: int) -> TrialResult:
        exception_info = None
        if exit_code != 0:
            exception_info = ExceptionInfo(
                exception_type="BashExitCode",
                exception_message=f"Bash script exited with code {exit_code}",
            )

        oss_mirror = self._config.environment.oss_mirror
        if oss_mirror is not None and oss_mirror.enabled and self._ossutil_ready and self._oss_credentials:
            await self._upload_artifacts(sandbox)

        return TrialResult(
            task_name=self._config.job_name or "",
            exception_info=exception_info,
            raw_output=output,
            exit_code=exit_code,
        )

    @staticmethod
    def _build_ossutil_cmd(ossutil_args: str, creds: dict) -> str:
        inner = (
            f"ossutil {ossutil_args}"
            f" --access-key-id {shlex.quote(creds['access_key_id'])}"
            f" --access-key-secret {shlex.quote(creds['access_key_secret'])}"
            f" --endpoint {shlex.quote(creds['endpoint'])}"
            f" --region {shlex.quote(creds['region'])}"
        )
        return f"bash -c {shlex.quote(inner)}"

    async def _upload_artifacts(self, sandbox: Sandbox) -> None:
        from rock.sdk.sandbox.client import RunMode

        try:
            oss_url = f"oss://{self._oss_credentials['oss_bucket']}/{self._build_oss_prefix()}/"
            src = self._artifact_dir.rstrip("/") + "/"
            cmd = self._build_ossutil_cmd(
                f"cp {shlex.quote(src)} {shlex.quote(oss_url)} --recursive",
                self._oss_credentials,
            )
            result = await sandbox.arun(cmd=cmd, mode=RunMode.NOHUP, wait_timeout=600)
            if result.exit_code != 0:
                logger.warning(f"OSS mirror upload failed: {result.output}")
            else:
                logger.info(f"OSS mirror upload completed: {self._artifact_dir} -> {oss_url}")
        except Exception as e:
            logger.warning(f"OSS mirror upload error: {e}")


# Auto-register on import
register_trial(BashJobConfig, BashTrial)
