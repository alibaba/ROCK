"""Repository wrapper for run metadata."""

from __future__ import annotations

from rock.sdk.job.meta import RunJobRef, RunJobStatus, RunMeta, RunScoreSummary
from rock.sdk.job.viewer import JobViewer


class RunMetaRepository:
    """Stable SDK atomic interface for run metadata operations."""

    def __init__(self, viewer: JobViewer):
        self._viewer = viewer

    @classmethod
    def from_job_config(cls, config) -> RunMetaRepository:
        oss_mirror = getattr(config.environment, "oss_mirror", None)
        if oss_mirror is None or not oss_mirror.enabled:
            raise ValueError("run metadata requires environment.oss_mirror.enabled=True")
        if not oss_mirror.namespace and getattr(config, "namespace", None):
            oss_mirror.namespace = config.namespace
        if not oss_mirror.experiment_id and getattr(config, "experiment_id", None):
            oss_mirror.experiment_id = config.experiment_id
        return cls(JobViewer.from_oss_mirror(oss_mirror))

    def write(self, run_meta: RunMeta) -> None:
        self._viewer.write_run_meta(run_meta)

    def get(self, run_id: str) -> RunMeta | None:
        return self._viewer.get_run_meta(run_id)

    def list(self, experiment_id: str | None = None) -> list[RunMeta]:
        return self._viewer.list_runs()

    def list_run_jobs(self, run_id: str) -> list[RunJobRef]:
        run_meta = self.get(run_id)
        if run_meta is None:
            return []
        return [
            RunJobRef(task_id=task_id, job_name=job_name)
            for task_id, job_name in sorted(run_meta.task_job_map.items())
        ]

    def get_run_job_statuses(self, run_id: str) -> list[RunJobStatus]:
        statuses: list[RunJobStatus] = []
        for ref in self.list_run_jobs(run_id):
            meta = self._viewer.get_job_meta(ref.job_name)
            trial_results = self._viewer.get_trial_results(ref.job_name)
            score = None
            status = "unknown"
            error = None
            if meta is not None:
                status = meta.status or status
                score = meta.score
                error = meta.error
            if trial_results:
                first = next(iter(trial_results.values()))
                status = getattr(first, "status", None) or status
                score = getattr(first, "score", None)
                exc = getattr(first, "exception_info", None)
                if exc:
                    error = getattr(exc, "exception_message", None) or getattr(exc, "exception_type", None)
            statuses.append(
                RunJobStatus(
                    task_id=ref.task_id,
                    job_name=ref.job_name,
                    status=status,
                    sandbox_id=getattr(meta, "sandbox_id", None) if meta is not None else None,
                    score=score,
                    started_at=getattr(meta, "started_at", None) if meta is not None else None,
                    finished_at=getattr(meta, "finished_at", None) if meta is not None else None,
                    error=error,
                )
            )
        return statuses

    def update_summary(self, run_id: str, summary: RunScoreSummary, status: str) -> None:
        run_meta = self.get(run_id)
        if run_meta is None:
            raise ValueError(f"run_id not found: {run_id}")
        run_meta.summary = summary
        run_meta.status = status
        self.write(run_meta)

    def find_completed_tasks(self, run_id: str) -> set[str]:
        return self._viewer.find_completed_tasks_in_run(run_id)

    def resolve_run_id_for_resume(self) -> str | None:
        return self._viewer.resolve_run_id_for_resume()
