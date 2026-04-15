"""BashTrial — execute a bash script inside a sandbox."""

from __future__ import annotations

from pathlib import Path

from rock.sdk.job.config import BashJobConfig
from rock.sdk.job.result import ExceptionInfo, TrialResult
from rock.sdk.job.trial.abstract import AbstractTrial
from rock.sdk.job.trial.registry import register_trial


class BashTrial(AbstractTrial):
    """Bash script execution trial."""

    _config: BashJobConfig

    async def setup(self, sandbox) -> None:
        await self._upload_files(sandbox)
        # If script_path is set, read content into self._config.script
        if self._config.script_path:
            self._config.script = Path(self._config.script_path).read_text()

    def build(self) -> str:
        lines = ["#!/bin/bash", "set -e", ""]
        if self._config.script:
            lines.append(self._config.script)
        return "\n".join(lines)

    async def collect(self, sandbox, output: str, exit_code: int) -> TrialResult:
        exception_info = None
        if exit_code != 0:
            exception_info = ExceptionInfo(
                exception_type="BashExitCode",
                exception_message=f"Bash script exited with code {exit_code}",
            )
        return TrialResult(
            task_name=self._config.job_name or "",
            exception_info=exception_info,
        )


# Auto-register on import
register_trial(BashJobConfig, BashTrial)
