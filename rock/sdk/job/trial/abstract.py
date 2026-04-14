"""Trial abstract base class — three-phase interface (setup / build / collect).

Trial 对象不管理 sandbox 生命周期；生命周期由 JobExecutor 负责。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rock.sdk.job.config import JobConfig
    from rock.sdk.job.result import TrialResult
    from rock.sdk.sandbox.client import Sandbox


class AbstractTrial(ABC):
    """Trial base: three-phase interface (setup/build/collect).

    Trial 不管理 sandbox 生命周期 (由 JobExecutor 负责)。
    """

    def __init__(self, config: JobConfig):
        self._config = config

    @abstractmethod
    async def setup(self, sandbox: Sandbox) -> None:
        """Pre-execution: prepare sandbox environment (upload files, write configs)."""

    @abstractmethod
    def build(self) -> str:
        """Build: generate bash script to execute."""

    @abstractmethod
    async def collect(self, sandbox: Sandbox, output: str, exit_code: int) -> TrialResult:
        """Post-execution: collect and parse results."""

    async def _upload_files(self, sandbox: Sandbox) -> None:
        """Shared helper: upload all entries in ``config.file_uploads``."""
        for local_path, sandbox_path in self._config.file_uploads:
            await sandbox.fs.upload_dir(local_path, sandbox_path)
