"""Repository wrapper for job-level metadata."""

from __future__ import annotations

from rock.sdk.job.meta import JobMeta
from rock.sdk.job.viewer import JobViewer


class JobMetaRepository:
    """Stable SDK atomic interface for job metadata operations."""

    def __init__(self, viewer: JobViewer):
        self._viewer = viewer

    def write(self, job_meta: JobMeta) -> None:
        self._viewer.write_job_meta(job_meta)

    def get(self, job_name: str) -> JobMeta | None:
        return self._viewer.get_job_meta(job_name)

    def update_status(self, job_name: str, status: str, **fields) -> None:
        meta = self.get(job_name) or JobMeta(job_name=job_name)
        meta.status = status
        for key, value in fields.items():
            if hasattr(meta, key):
                setattr(meta, key, value)
        self.write(meta)

    def get_by_run_task(self, run_id: str, task_id: str) -> JobMeta | None:
        for job_name in self._viewer.list_jobs():
            meta = self.get(job_name)
            if meta is not None and meta.run_id == run_id and meta.task_id == task_id:
                return meta
        return None
