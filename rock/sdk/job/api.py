"""Job — thin user-facing facade over JobExecutor + Operator.

Only 2 params (config + operator). Delegates everything to JobExecutor.

Usage:
    result = await Job(config).run()
    # or
    job = Job(config, operator=ScatterOperator(size=8))
    await job.submit()
    result = await job.wait()
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rock.sdk.job.executor import JobExecutor
from rock.sdk.job.operator import ScatterOperator
from rock.sdk.job.result import JobResult, JobStatus

if TYPE_CHECKING:
    from rock.sdk.job.config import JobConfig
    from rock.sdk.job.executor import JobClient
    from rock.sdk.job.operator import Operator
    from rock.sdk.job.result import TrialResult


class Job:
    """Job Facade — the thin user-facing entry point.

    Usage:
        result = await Job(config).run()
        # or
        job = Job(config, operator=ScatterOperator(size=8))
        await job.submit()
        result = await job.wait()
    """

    def __init__(self, config: JobConfig, operator: Operator | None = None):
        self._config = config
        self._executor = JobExecutor()
        self._operator = operator or ScatterOperator()
        self._job_client: JobClient | None = None

    async def run(self) -> JobResult:
        """Full lifecycle: submit + wait."""
        await self.submit()
        return await self.wait()

    async def submit(self) -> None:
        """Non-blocking submit: operator generates trials, executor starts them."""
        self._job_client = await self._executor.submit(self._operator, self._config)

    async def wait(self) -> JobResult:
        """Wait for completion, build JobResult."""
        if not self._job_client:
            raise RuntimeError("No submitted job. Call submit() first.")
        raw = await self._executor.wait(self._job_client)
        return self._build_result(raw)

    async def cancel(self) -> None:
        """Kill all running trials."""
        if self._job_client:
            for tc in self._job_client.trials:
                await tc.sandbox.arun(cmd=f"kill {tc.pid}", session=tc.session)

    def _build_result(self, raw_results: list[TrialResult | list[TrialResult]]) -> JobResult:
        """Flatten list-returning collect() outputs into JobResult.trial_results.

        Each element of ``raw_results`` is whatever one Trial's ``collect()``
        returned — either a single TrialResult or a list. HarborTrial returns
        a list (one entry per sub-trial); BashTrial returns a single result.
        """
        flat: list[TrialResult] = []
        for r in raw_results:
            if isinstance(r, list):
                flat.extend(r)
            else:
                flat.append(r)
        all_success = all(t.exception_info is None for t in flat)
        # G5: surface first non-empty output / non-zero exit code from sub-trials
        raw_output = next((t.raw_output for t in flat if t.raw_output), "")
        exit_code = next((t.exit_code for t in flat if t.exit_code != 0), 0)
        result = JobResult(
            job_id=self._config.job_name or "",
            status=JobStatus.COMPLETED if all_success else JobStatus.FAILED,
            labels=self._config.labels,
            trial_results=flat,
            raw_output=raw_output,
            exit_code=exit_code,
        )

        # Report tracking if adapter is available (never raises)
        self._report_tracking(result)

        return result

    def _report_tracking(self, result: JobResult) -> None:
        """Resolve all adapters and report job metrics. Never raises."""
        from rock.logger import init_logger
        from rock.sdk.job.adapter import resolve_tracking_adapters

        adapters = resolve_tracking_adapters()
        if not adapters:
            return

        config = self._config
        namespace = config.namespace or "rock-namespace"
        experiment_id = config.experiment_id or "rock-experiment"
        job_name = config.job_name or "default"

        for adapter in adapters:
            try:
                adapter.init(namespace=namespace, experiment_id=experiment_id, job_id=job_name, config=config)

                # per-trial metrics
                for i, trial in enumerate(result.trial_results):
                    adapter.report(
                        {
                            "task_name": trial.task_name,
                            "trial_index": i,
                            "status": trial.status,
                            "score": trial.score,
                            "exit_code": trial.exit_code,
                            "duration_sec": trial.duration_sec,
                        }
                    )

                # job-level summary
                adapter.report(
                    {
                        "job_status": result.status.value,
                        "job_score": result.score,
                        "n_completed": result.n_completed,
                        "n_failed": result.n_failed,
                        "total_trials": len(result.trial_results),
                    }
                )
            except Exception:  # noqa: BLE001 — tracking failure must not affect result return
                init_logger(__name__).warning(
                    f"Tracking adapter {type(adapter).__name__} failed to report", exc_info=True
                )
            finally:
                try:
                    adapter.close()
                except Exception:  # noqa: BLE001 — close() must never propagate
                    pass
