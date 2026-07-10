"""JobExecutor drives Trials produced by an Operator."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rock.actions import CreateBashSessionRequest
from rock.logger import init_logger
from rock.sdk.job.operator import Operator
from rock.sdk.job.result import ExceptionInfo, TrialResult
from rock.sdk.sandbox.client import Sandbox

if TYPE_CHECKING:
    from rock.sdk.job.meta import JobMeta
    from rock.sdk.job.planner import PlannedJob
    from rock.sdk.job.config import JobConfig
    from rock.sdk.job.trial.abstract import AbstractTrial

logger = init_logger(__name__)


@dataclass
class TrialClient:
    sandbox: Sandbox
    session: str
    pid: int
    trial: AbstractTrial


@dataclass
class JobClient:
    trials: list[TrialClient]


TrialDoneCallback = Callable[[TrialClient, TrialResult | list[TrialResult], int], None]
TrialStartedCallback = Callable[[TrialClient, int], None]


class JobExecutor:
    """Execution engine for job trials.

    ``submit``/``wait`` preserve the historical single-job behavior.
    ``run_job`` is the atomic primitive used by CLI run orchestration.
    """

    def __init__(self, max_concurrent: int | None = None):
        if max_concurrent is not None and max_concurrent <= 0:
            raise ValueError("max_concurrent must be positive or None")
        self._max_concurrent = max_concurrent

    async def run(self, operator: Operator, config: JobConfig) -> list[TrialResult | list[TrialResult]]:
        job_client = await self.submit(operator, config)
        return await self.wait(job_client)

    async def run_job(
        self,
        job: PlannedJob,
        *,
        callbacks=None,
    ) -> TrialResult | list[TrialResult]:
        """Run a single planned job as an SDK atomic capability."""
        client = None
        try:
            client = await self._do_submit(job.trial)
            if callbacks and hasattr(callbacks, "on_started"):
                callbacks.on_started(client)
            result = await self._do_wait(client)
            if callbacks and hasattr(callbacks, "on_done"):
                callbacks.on_done(client, result)
            return result
        except Exception as exc:
            logger.error("Job lifecycle failed: %s", exc, exc_info=True)
            result = self._failure_result_for_trial(job.trial, exc)
            if client is not None and callbacks and hasattr(callbacks, "on_done"):
                callbacks.on_done(client, result)
            return result

    async def wait_existing_job(
        self,
        planned_job: PlannedJob,
        job_meta: JobMeta,
    ) -> TrialResult | list[TrialResult]:
        """Reconnect to an existing sandbox/process and collect the result.

        The CLI decides whether a job is recoverable; this method only performs
        the atomic wait/collect operation from persisted metadata.
        """
        if not job_meta.sandbox_id or not job_meta.session or job_meta.pid is None:
            raise ValueError("job_meta is missing sandbox_id/session/pid")
        sandbox = Sandbox(planned_job.config.environment)
        sandbox._sandbox_id = job_meta.sandbox_id
        client = TrialClient(
            sandbox=sandbox,
            session=job_meta.session,
            pid=job_meta.pid,
            trial=planned_job.trial,
        )
        return await self._do_wait(client)

    def prepare(self, operator: Operator, config: JobConfig) -> list[AbstractTrial]:
        return operator.apply(config)

    async def run_trials(
        self,
        trials: Sequence[AbstractTrial],
        on_trial_started: TrialStartedCallback | None = None,
        on_trial_done: TrialDoneCallback | None = None,
    ) -> list[TrialResult | list[TrialResult]]:
        """Run caller-provided trials with optional lifecycle concurrency control."""
        if not trials:
            return []

        semaphore = asyncio.Semaphore(self._max_concurrent) if self._max_concurrent is not None else None

        async def run_one(index: int, trial: AbstractTrial):
            if semaphore is None:
                return await self._run_trial_lifecycle(trial, index, on_trial_started, on_trial_done)
            async with semaphore:
                return await self._run_trial_lifecycle(trial, index, on_trial_started, on_trial_done)

        return list(await asyncio.gather(*[run_one(i, trial) for i, trial in enumerate(trials)]))

    async def submit(self, operator: Operator, config: JobConfig) -> JobClient:
        trial_list = self.prepare(operator, config)
        if not trial_list:
            return JobClient(trials=[])
        trial_clients = await asyncio.gather(*[self._do_submit(t) for t in trial_list])
        return JobClient(trials=list(trial_clients))

    async def wait(
        self,
        job_client: JobClient,
        on_trial_done: TrialDoneCallback | None = None,
    ) -> list[TrialResult | list[TrialResult]]:
        if not job_client.trials:
            return []
        if on_trial_done is None:
            return list(await asyncio.gather(*[self._do_wait(tc) for tc in job_client.trials]))

        async def wait_one(index: int, trial_client: TrialClient):
            result = await self._do_wait(trial_client)
            on_trial_done(trial_client, result, index)
            return result

        return list(await asyncio.gather(*[wait_one(i, tc) for i, tc in enumerate(job_client.trials)]))

    async def _run_trial_lifecycle(
        self,
        trial: AbstractTrial,
        index: int,
        on_trial_started: TrialStartedCallback | None,
        on_trial_done: TrialDoneCallback | None,
    ) -> TrialResult | list[TrialResult]:
        try:
            client = await self._do_submit(trial)
            if on_trial_started:
                on_trial_started(client, index)
            result = await self._do_wait(client)
            if on_trial_done:
                on_trial_done(client, result, index)
            return result
        except Exception as exc:
            logger.error("Trial lifecycle failed: %s", exc, exc_info=True)
            return self._failure_result_for_trial(trial, exc)

    @staticmethod
    def _failure_result_for_trial(trial: AbstractTrial, exc: Exception) -> TrialResult:
        config = getattr(trial, "_config", None)
        labels = getattr(config, "labels", {}) or {}
        task_name = labels.get("rock_task_id") or getattr(config, "job_name", "") or ""
        return TrialResult(
            task_name=task_name,
            exception_info=ExceptionInfo(
                exception_type=type(exc).__name__,
                exception_message=str(exc),
            ),
        )

    @staticmethod
    def _job_tmp_prefix(config: JobConfig) -> str:
        from rock.sdk.bench.constants import USER_DEFINED_LOGS

        return f"{USER_DEFINED_LOGS}/rock_job_{config.job_name or 'default'}"

    async def _do_submit(self, trial: AbstractTrial) -> TrialClient:
        config = trial._config
        sandbox = Sandbox(config.environment)
        await sandbox.start()
        logger.info("Sandbox started: sandbox_id=%s, job_name=%s", sandbox.sandbox_id, config.job_name)

        await trial.on_sandbox_ready(sandbox)
        await trial.setup(sandbox)

        session = f"rock-job-{config.job_name or 'default'}"
        env = self._build_session_env(config)
        await sandbox.create_session(CreateBashSessionRequest(session=session, env_enable=True, env=env))

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

        logger.info("Trial started: pid=%s, job_name=%s", pid, config.job_name)
        return TrialClient(sandbox=sandbox, session=session, pid=pid, trial=trial)

    async def _do_wait(self, client: TrialClient) -> TrialResult | list[TrialResult]:
        config = client.trial._config
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
            logger.info("Trial output (job=%s):\n%s", config.job_name, obs.output)
        result = await client.trial.collect(client.sandbox, obs.output or "", exit_code)
        iter_results = result if isinstance(result, list) else [result]
        for trial_result in iter_results:
            if not trial_result.raw_output:
                trial_result.raw_output = obs.output or ""
            if trial_result.exit_code == 0 and exit_code != 0:
                trial_result.exit_code = exit_code
        if not success:
            fail_info = ExceptionInfo(
                exception_type="ProcessTimeout",
                exception_message=message or "process did not complete successfully",
            )
            for trial_result in iter_results:
                if trial_result.exception_info is None:
                    trial_result.exception_info = fail_info
        return result

    @staticmethod
    def _build_session_env(config: JobConfig) -> dict[str, str] | None:
        oss_env = {k: v for k, v in os.environ.items() if k.startswith("OSS")}
        merged = {**oss_env, **config.environment.env}
        return merged or None
