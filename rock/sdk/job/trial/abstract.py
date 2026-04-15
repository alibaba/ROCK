"""Trial abstract base class — three-phase interface (setup / build / collect).

Trial objects do not manage sandbox lifecycle; lifecycle is managed by JobExecutor.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rock.sdk.job.config import JobConfig
    from rock.sdk.job.result import TrialResult
    from rock.sdk.sandbox.client import Sandbox


class AbstractTrial(ABC):
    """Trial base: three-phase interface (setup/build/collect).

    Trial does not manage sandbox lifecycle (managed by JobExecutor).
    """

    def __init__(self, config: JobConfig):
        self._config = config

    async def on_sandbox_ready(self, sandbox: Sandbox) -> None:
        """G4 hook: called by JobExecutor once sandbox.start() succeeds, before setup().

        Default behavior backfills ``namespace`` and ``experiment_id`` from the
        sandbox into ``self._config`` (both are fields on ``JobConfig``), and
        raises ``ValueError`` if the sandbox reports a value that conflicts
        with one already set on the config. Matches legacy
        ``_autofill_sandbox_info``. Subclasses can override to extend.
        """
        sb_ns = getattr(sandbox, "_namespace", None)
        if sb_ns is not None:
            if self._config.namespace is not None and self._config.namespace != sb_ns:
                raise ValueError(
                    f"namespace mismatch: {type(self._config).__name__} has "
                    f"'{self._config.namespace}', but sandbox returned '{sb_ns}'"
                )
            self._config.namespace = sb_ns

        sb_exp = getattr(sandbox, "_experiment_id", None)
        if sb_exp is not None:
            if self._config.experiment_id is not None and self._config.experiment_id != sb_exp:
                raise ValueError(
                    f"experiment_id mismatch: {type(self._config).__name__} has "
                    f"'{self._config.experiment_id}', but sandbox returned '{sb_exp}'"
                )
            self._config.experiment_id = sb_exp

    @abstractmethod
    async def setup(self, sandbox: Sandbox) -> None:
        """Pre-execution: prepare sandbox environment (upload files, write configs)."""

    @abstractmethod
    def build(self) -> str:
        """Build: generate bash script to execute."""

    @abstractmethod
    async def collect(self, sandbox: Sandbox, output: str, exit_code: int) -> TrialResult | list[TrialResult]:
        """Post-execution: collect and parse results.

        Return a single ``TrialResult`` for one-shot tasks (e.g. BashTrial),
        or a ``list[TrialResult]`` when the underlying tool produces multiple
        sub-results per sandbox invocation (e.g. HarborTrial running a dataset
        over N tasks). The Job / JobExecutor layer flattens lists into the
        final ``JobResult.trial_results``.
        """

    async def _upload_files(self, sandbox: Sandbox) -> None:
        """Shared helper: upload all entries in ``config.uploads``.

        Automatically detects file vs directory and dispatches accordingly:
        - file  → ``sandbox.upload_by_path()``
        - dir   → ``sandbox.fs.upload_dir()``
        """
        for local_path, sandbox_path in self._config.environment.uploads:
            src = Path(local_path)
            if src.is_file():
                resp = await sandbox.upload_by_path(file_path=local_path, target_path=sandbox_path)
                if not resp.success:
                    raise RuntimeError(f"Failed to upload file {local_path} -> {sandbox_path}: {resp.message}")
            elif src.is_dir():
                obs = await sandbox.fs.upload_dir(source_dir=local_path, target_dir=sandbox_path)
                if obs.exit_code != 0:
                    raise RuntimeError(f"Failed to upload dir {local_path} -> {sandbox_path}: {obs.failure_reason}")
            else:
                raise RuntimeError(f"Upload source not found or unsupported: {local_path}")
