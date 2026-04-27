"""BashTrial — execute a bash script inside a sandbox."""

from __future__ import annotations

import secrets
from pathlib import Path

from rock.logger import init_logger
from rock.sdk.job.config import BashJobConfig
from rock.sdk.job.result import ExceptionInfo, TrialResult
from rock.sdk.job.trial.abstract import AbstractTrial
from rock.sdk.job.trial.registry import register_trial
from rock.sdk.sandbox.client import Sandbox

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

    def _oss_mirror_enabled(self) -> bool:
        mirror = self._config.environment.oss_mirror
        return mirror is not None and mirror.enabled

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
        return _render_wrapper(script)

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
