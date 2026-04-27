"""BashTrial — execute a bash script inside a sandbox."""

from __future__ import annotations

import os
import secrets
import shlex
from pathlib import Path

from rock import env_vars
from rock.actions.sandbox.request import Command
from rock.logger import init_logger
from rock.sdk.job.config import BashJobConfig
from rock.sdk.job.result import ExceptionInfo, TrialResult
from rock.sdk.job.trial.abstract import AbstractTrial
from rock.sdk.job.trial.registry import register_trial
from rock.sdk.sandbox.client import RunMode, Sandbox

logger = init_logger(__name__)


def _render_wrapper(user_script: str, token: str | None = None) -> str:
    """Render the BashJob wrapper script.

    Structure: prologue (mkdir + touch + initial upload) → user script
    (isolated in a single-quoted heredoc) → epilogue (final upload) → exit
    with user's exit code.

    When ``token`` is ``None`` a random 8-char hex is generated. Collision
    probability is ~2^-32 and collisions also require the user script to
    contain the terminator on its own line, which is not actively guarded
    against — the risk is acceptable.
    """
    if token is None:
        token = secrets.token_hex(4)  # 8-char hex
    eof = f"__ROCK_USER_SCRIPT_EOF_{token}__"
    return (
        "#!/bin/bash\n"
        "# rock bash-job wrapper (generated, do not edit)\n"
        "# OSS credentials and paths come from session env; no secrets in this file.\n"
        "set +e\n"
        "\n"
        "# -- prologue: prepare artifact dir and do an initial placeholder upload --\n"
        'mkdir -p "$ROCK_ARTIFACT_DIR"\n'
        'touch "$ROCK_ARTIFACT_DIR/.placeholder"\n'
        'ossutil cp "$ROCK_ARTIFACT_DIR/" "oss://$OSS_BUCKET/$ROCK_OSS_PREFIX/" \\\n'
        "    --recursive -f >/dev/null 2>&1 || true\n"
        "\n"
        "# -- user script: heredoc isolates user's trap/exit from the wrapper --\n"
        f"bash <<'{eof}'\n"
        f"{user_script}\n"
        f"{eof}\n"
        "_rock_user_rc=$?\n"
        "\n"
        "# -- epilogue: final upload (failure is logged but does not change exit code) --\n"
        'ossutil cp "$ROCK_ARTIFACT_DIR/" "oss://$OSS_BUCKET/$ROCK_OSS_PREFIX/" \\\n'
        "    --recursive -f \\\n"
        '    || echo "[rock] oss upload failed (rc=$?), ignored" >&2\n'
        "\n"
        "exit $_rock_user_rc\n"
    )


class BashTrial(AbstractTrial):
    """Bash script execution trial."""

    _config: BashJobConfig

    def __init__(self, config: BashJobConfig):
        super().__init__(config)
        self._ossutil_ready: bool = False
        self._oss_credentials: dict | None = None
        self._artifact_dir: str | None = None

    @property
    def _oss_mirror(self):
        return self._config.environment.oss_mirror

    async def setup(self, sandbox: Sandbox) -> None:
        await self._upload_files(sandbox)
        if self._config.script_path:
            self._config.script = Path(self._config.script_path).read_text()

        if self._oss_mirror is not None and self._oss_mirror.enabled:
            await self._setup_oss_mirror(sandbox)

    async def _setup_oss_mirror(self, sandbox: Sandbox) -> None:
        if not self._config.namespace:
            raise ValueError("oss_mirror: namespace is not set (sandbox did not return one)")
        if not self._config.experiment_id:
            raise ValueError("oss_mirror: experiment_id is not set (sandbox did not return one)")

        self._artifact_dir = env_vars.ROCK_BASH_JOB_ARTIFACT_DIR

        bucket = self._oss_mirror.oss_bucket or os.environ.get("OSS_BUCKET")
        if not bucket:
            raise ValueError("oss_mirror.enabled=True but oss_bucket is not set (config or OSS_BUCKET env)")

        self._oss_credentials = {
            "oss_bucket": bucket,
            "access_key_id": self._oss_mirror.oss_access_key_id or os.environ.get("OSS_ACCESS_KEY_ID", ""),
            "access_key_secret": self._oss_mirror.oss_access_key_secret or os.environ.get("OSS_ACCESS_KEY_SECRET", ""),
            "endpoint": self._oss_mirror.oss_endpoint or os.environ.get("OSS_ENDPOINT", ""),
            "region": self._oss_mirror.oss_region or os.environ.get("OSS_REGION", ""),
        }

        await sandbox.execute(Command(command=["mkdir", "-p", self._artifact_dir]))
        # Touch a placeholder so ossutil cp has something to upload (OSS has no real dirs)
        await sandbox.execute(Command(command=["touch", f"{self._artifact_dir}/.placeholder"]))

        self._ossutil_ready = await sandbox.fs.ensure_ossutil()
        if not self._ossutil_ready:
            logger.warning("ossutil install failed, OSS mirror upload will be skipped")
            return

        await self._upload_artifacts(sandbox)

    def _build_oss_prefix(self) -> str:
        return f"artifacts/{self._config.namespace}/{self._config.experiment_id}/{self._config.job_name}"

    def build(self) -> str:
        return self._config.script or ""

    async def collect(self, sandbox: Sandbox, output: str, exit_code: int) -> TrialResult:
        exception_info = None
        if exit_code != 0:
            exception_info = ExceptionInfo(
                exception_type="BashExitCode",
                exception_message=f"Bash script exited with code {exit_code}",
            )

        if self._oss_mirror is not None and self._oss_mirror.enabled and self._ossutil_ready and self._oss_credentials:
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
