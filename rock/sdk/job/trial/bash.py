"""BashTrial — execute a bash script inside a sandbox."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from rock import env_vars
from rock.logger import init_logger
from rock.sdk.job.config import BashJobConfig
from rock.sdk.job.result import ExceptionInfo, TrialResult
from rock.sdk.job.trial.abstract import AbstractTrial
from rock.sdk.job.trial.registry import register_trial
from rock.sdk.sandbox.client import Sandbox

logger = init_logger(__name__)

_OSS_CREDENTIAL_FIELDS = (
    "oss_access_key_id",
    "oss_access_key_secret",
    "oss_endpoint",
    "oss_region",
    "oss_bucket",
)


class BashTrial(AbstractTrial):
    """Bash script execution trial."""

    _config: BashJobConfig

    def __init__(self, config: BashJobConfig):
        super().__init__(config)
        self._ossutil_ready: bool = False

    def _oss_mirror_enabled(self) -> bool:
        mirror = self._config.environment.oss_mirror
        return mirror is not None and mirror.enabled

    async def on_sandbox_ready(self, sandbox: Sandbox) -> None:
        """Backfill namespace/experiment_id (via super) then prepare OSS session env.

        All BashJob-specific session env preparation (credential resolution,
        validation, derived ROCK_* keys) lives here so that the shared
        ``JobExecutor._build_session_env`` stays trial-agnostic. Because this
        hook runs strictly before ``_build_session_env``, any keys we write
        into ``config.environment.env`` here will propagate into the bash
        session env.
        """
        await super().on_sandbox_ready(sandbox)
        if self._oss_mirror_enabled():
            self._prepare_oss_session_env()

    def _prepare_oss_session_env(self) -> None:
        """Resolve OSS credentials, validate, and inject derived ROCK_* keys.

        Resolution order per key (first non-empty wins):
          1. ``OssMirrorConfig`` field (highest priority)
          2. ``environment.env`` (if the user already put it there)
          3. Host process ``os.environ`` (lowest priority)

        The resolved credentials are written into ``environment.env`` so that
        ``JobExecutor._build_session_env`` picks them up without needing to
        know anything about OSS. Also writes ``ROCK_ARTIFACT_DIR`` and
        ``ROCK_OSS_PREFIX`` for the wrapper script to consume.
        """
        mirror = self._config.environment.oss_mirror
        env = self._config.environment.env

        for field_name in _OSS_CREDENTIAL_FIELDS:
            env_key = field_name.upper()
            v = getattr(mirror, field_name, None) or env.get(env_key) or os.environ.get(env_key)
            if v:
                env[env_key] = v

        if not self._config.namespace:
            raise ValueError("oss_mirror: namespace is not set (sandbox did not return one)")
        if not self._config.experiment_id:
            raise ValueError("oss_mirror: experiment_id is not set (sandbox did not return one)")
        for env_key in ("OSS_BUCKET", "OSS_ENDPOINT", "OSS_REGION"):
            if not env.get(env_key):
                raise ValueError(f"oss_mirror.enabled=True but {env_key} is not resolvable")

        env["ROCK_ARTIFACT_DIR"] = env_vars.ROCK_BASH_JOB_ARTIFACT_DIR
        env[
            "ROCK_OSS_PREFIX"
        ] = f"artifacts/{self._config.namespace}/{self._config.experiment_id}/{self._config.job_name}"

    @staticmethod
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

    async def setup(self, sandbox: Sandbox) -> None:
        await self._upload_files(sandbox)
        if self._config.script_path:
            self._config.script = Path(self._config.script_path).read_text()

        if self._oss_mirror_enabled():
            self._ossutil_ready = await sandbox.fs.ensure_ossutil()
            if not self._ossutil_ready:
                logger.warning("ossutil install failed, OSS mirror upload will be skipped")

    def build(self) -> str:
        script = self._config.script or ""
        if not self._oss_mirror_enabled():
            return script
        if not self._ossutil_ready:
            logger.warning("ossutil unavailable, falling back to raw script (OSS mirror upload disabled for this run)")
            return script
        return self._render_wrapper(script)

    async def collect(self, sandbox: Sandbox, output: str, exit_code: int) -> TrialResult:
        exception_info = None
        if exit_code != 0:
            exception_info = ExceptionInfo(
                exception_type="BashExitCode",
                exception_message=f"Bash script exited with code {exit_code}",
            )

        return TrialResult(
            task_name=self._config.job_name or "",
            exception_info=exception_info,
            raw_output=output,
            exit_code=exit_code,
        )


# Auto-register on import
register_trial(BashJobConfig, BashTrial)
