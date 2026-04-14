"""JobExecutor — orchestrates the full execution of Trials produced by an Operator.

Flow:
    submit(operator, config)  — apply operator to get TrialList, start all sandboxes
                                in parallel, return JobClient (list of TrialClient)
    wait(job_client)          — wait for all trials, collect results, return list[TrialResult]
    run(operator, config)     — submit + wait
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rock.actions import CreateBashSessionRequest
from rock.logger import init_logger
from rock.sdk.job.operator import Operator
from rock.sdk.job.result import TrialResult
from rock.sdk.sandbox.client import Sandbox

if TYPE_CHECKING:
    from rock.sdk.job.config import JobConfig
    from rock.sdk.job.trial.abstract import AbstractTrial

logger = init_logger(__name__)


@dataclass
class TrialClient:
    """Handle for a single running trial."""

    sandbox: Sandbox
    session: str
    pid: int
    trial: AbstractTrial


@dataclass
class JobClient:
    """Handle returned by JobExecutor.submit(). Holds multiple TrialClients."""

    trials: list[TrialClient]


class JobExecutor:
    """Execution engine: drives Operator to generate trials, runs in parallel, collects results."""

    async def run(self, operator: Operator, config: JobConfig) -> list[TrialResult]:
        """Full lifecycle: submit + wait."""
        job_client = await self.submit(operator, config)
        return await self.wait(job_client)

    async def submit(self, operator: Operator, config: JobConfig) -> JobClient:
        """Operator generates TrialList, start all sandboxes in parallel."""
        trial_list = operator.apply(config)
        if not trial_list:
            return JobClient(trials=[])
        trial_clients = await asyncio.gather(*[self._do_submit(t) for t in trial_list])
        return JobClient(trials=list(trial_clients))

    async def wait(self, job_client: JobClient) -> list[TrialResult]:
        """Wait for all trials, collect results in parallel."""
        if not job_client.trials:
            return []
        return list(await asyncio.gather(*[self._do_wait(tc) for tc in job_client.trials]))

    # ── Internal: per-trial submit/wait ──

    @staticmethod
    def _job_tmp_prefix(config: JobConfig) -> str:
        """Prefix for per-job temp files on sandbox, e.g. /tmp/rock_job_my-job."""
        return f"/tmp/rock_job_{config.job_name or 'default'}"

    async def _do_submit(self, trial: AbstractTrial) -> TrialClient:
        """Start sandbox + execute script for a single trial."""
        config = trial._config
        sandbox = Sandbox(config.environment)
        await sandbox.start()
        logger.info(f"Sandbox started: sandbox_id={sandbox.sandbox_id}, job_name={config.job_name}")

        session = f"rock-job-{config.job_name or 'default'}"
        env = self._build_session_env(config)
        await sandbox.create_session(CreateBashSessionRequest(session=session, env_enable=True, env=env))

        await trial.setup(sandbox)
        script_content = trial.build()

        prefix = self._job_tmp_prefix(config)
        script_path = f"{prefix}.sh"
        await sandbox.write_file_by_path(script_content, script_path)

        tmp_file = f"{prefix}.out"
        pid, error = await sandbox.start_nohup_process(
            cmd=f"bash {script_path}",
            tmp_file=tmp_file,
            session=session,
        )
        if error is not None:
            raise RuntimeError(f"Failed to start trial: {error.output}")

        logger.info(f"Trial started: pid={pid}, job_name={config.job_name}")
        return TrialClient(sandbox=sandbox, session=session, pid=pid, trial=trial)

    async def _do_wait(self, client: TrialClient) -> TrialResult:
        """Wait for a single trial to finish, call trial.collect()."""
        from rock.sdk.job.result import ExceptionInfo

        config = client.trial._config
        try:
            success, message = await client.sandbox.wait_for_process_completion(
                pid=client.pid,
                session=client.session,
                wait_timeout=config.timeout,
                wait_interval=30,
            )
            obs = await client.sandbox.handle_nohup_output(
                tmp_file=f"{self._job_tmp_prefix(config)}.out",
                session=client.session,
                success=success,
                message=message,
                ignore_output=False,
                response_limited_bytes_in_nohup=None,
            )
            exit_code = obs.exit_code if obs.exit_code is not None else 1
            if obs.output:
                logger.info(f"Trial output (job={config.job_name}):\n{obs.output}")
            result = await client.trial.collect(client.sandbox, obs.output or "", exit_code)
            if not success and result.exception_info is None:
                result.exception_info = ExceptionInfo(
                    exception_type="ProcessTimeout",
                    exception_message=message or "process did not complete successfully",
                )
            return result
        finally:
            if config.auto_stop:
                await client.sandbox.close()

    @staticmethod
    def _build_session_env(config: JobConfig) -> dict[str, str] | None:
        """Merge OSS_* env vars from the process with config.env (config wins)."""
        oss_env = {k: v for k, v in os.environ.items() if k.startswith("OSS")}
        merged = {**oss_env, **config.env}
        return merged or None
