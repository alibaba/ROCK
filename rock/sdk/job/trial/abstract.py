"""Trial abstract base class — three-phase interface (setup / build / collect).

Trial objects do not manage sandbox lifecycle; lifecycle is managed by JobExecutor.
"""

from __future__ import annotations

import shlex
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from rock import env_vars
from rock.logger import init_logger
from rock.sdk.sandbox.model_service.base import ModelService, ModelServiceConfig

if TYPE_CHECKING:
    from rock.sdk.job.config import JobConfig
    from rock.sdk.job.result import TrialResult
    from rock.sdk.sandbox.client import Sandbox

logger = init_logger(__name__)


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
            if self._config.experiment_id is None:
                self._config.experiment_id = sb_exp
            # If config already has experiment_id, it takes priority over sandbox's value.

    def _build_proxy_start_cmd(self) -> str:
        """Build the ``rock model-service start ...`` command line."""
        proxy = self._config.environment.proxy
        env = self._config.environment.env
        upstream = env["OPENAI_BASE_URL"]
        parts = [
            "rock model-service start --type proxy",
            f"--host {shlex.quote(proxy.host)}",
            f"--port {proxy.port}",
            f"--proxy-base-url {shlex.quote(upstream)}",
        ]
        if proxy.replay_file:
            parts.append(f"--replay-file {shlex.quote(env_vars.ROCK_JOB_PROXY_REPLAY_FILE)}")
        elif proxy.recording_file:
            parts.append(f"--recording-file {shlex.quote(proxy.recording_file)}")
        return " ".join(parts)

    async def _detect_and_rewrite_proxy_url(self, sandbox: Sandbox) -> None:
        """Detect the outer sandbox eth0 IP and rewrite env['OPENAI_BASE_URL']."""
        proxy = self._config.environment.proxy
        obs = await sandbox.arun("hostname -I 2>/dev/null | awk '{print $1}'")
        host_ip = obs.output.strip() or "127.0.0.1"
        proxy_url = f"http://{host_ip}:{proxy.port}/v1"
        self._config.environment.env["OPENAI_BASE_URL"] = proxy_url
        logger.info(f"Proxy ready at {proxy_url}")

    async def _setup_proxy(self, sandbox: Sandbox) -> None:
        """Bring up the in-sandbox model-service proxy when enabled, otherwise no-op.

        Called at the start of ``HarborTrial.setup()`` and ``BashTrial.setup()`` so the
        proxy is ready before any user/agent code runs.

        The OPENAI_BASE_URL existence check lives here (not in the
        EnvironmentConfig validator) to keep job-layer concerns out of the generic
        sandbox config.
        """
        proxy = self._config.environment.proxy
        if proxy is None or not proxy.enabled:
            return

        if not self._config.environment.env.get("OPENAI_BASE_URL"):
            raise ValueError(
                "proxy.enabled=True but env['OPENAI_BASE_URL'] is not set. "
                "Set environment.env.OPENAI_BASE_URL to the upstream OpenAI-compatible "
                "base URL (e.g. 'https://api.openai.com/v1') so the proxy knows where "
                "to forward."
            )

        if proxy.replay_file:
            resp = await sandbox.upload_by_path(
                file_path=proxy.replay_file,
                target_path=env_vars.ROCK_JOB_PROXY_REPLAY_FILE,
            )
            if not resp.success:
                raise RuntimeError(
                    f"Failed to upload proxy replay file {proxy.replay_file} -> "
                    f"{env_vars.ROCK_JOB_PROXY_REPLAY_FILE}: {resp.message}"
                )

        ms_config = ModelServiceConfig(
            enabled=True,
            type="proxy",
            install_cmd=f"pip install {shlex.quote(proxy.model_service_package)}",
            start_cmd=self._build_proxy_start_cmd(),
        )
        sandbox.model_service = ModelService(sandbox, ms_config)
        await sandbox.model_service.install()
        await sandbox.model_service.start()

        await self._detect_and_rewrite_proxy_url(sandbox)

    async def setup(self, sandbox: Sandbox) -> None:
        """Pre-execution: start proxy (if enabled) and upload files.

        Subclasses should call ``await super().setup(sandbox)`` first, then add
        their own setup logic.
        """
        await self._setup_proxy(sandbox)
        await self._upload_files(sandbox)

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
