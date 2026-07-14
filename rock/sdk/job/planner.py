"""Single-task planning primitives for CLI run orchestration."""

from __future__ import annotations

from dataclasses import dataclass

from rock.sdk.bench.models.job.config import HarborJobConfig, OssRegistryInfo, RegistryDatasetConfig
from rock.sdk.job.config import BashJobConfig, JobConfig
from rock.sdk.job.trial.abstract import AbstractTrial
from rock.sdk.job.trial.registry import _create_trial


@dataclass(frozen=True)
class ResolvedTask:
    task_id: str
    dataset: str | None = None
    split: str | None = None
    org: str | None = None


@dataclass(frozen=True)
class PlannedJob:
    task_id: str
    job_name: str
    config: JobConfig
    trial: AbstractTrial


class SingleTaskPlanner:
    """Clone a JobConfig into one task-bound job/trial."""

    def __init__(self, *, run_id: str, preserve_job_name: bool = False):
        self.run_id = run_id
        self.preserve_job_name = preserve_job_name

    def plan(self, config: JobConfig, *, task: ResolvedTask) -> PlannedJob:
        cloned = config.model_copy(deep=True)
        job_name = self._job_name(cloned, task)

        cloned.job_name = job_name
        cloned.labels["rock_run_id"] = self.run_id
        cloned.labels["rock_task_id"] = task.task_id

        if isinstance(cloned, HarborJobConfig):
            self._apply_harbor_task(cloned, task)
        elif isinstance(cloned, BashJobConfig):
            self._apply_bash_task(cloned, task, job_name)
        else:
            raise TypeError(f"Unsupported JobConfig type: {type(config).__name__}")

        trial = _create_trial(cloned)
        return PlannedJob(task_id=task.task_id, job_name=job_name, config=cloned, trial=trial)

    def _job_name(self, config: JobConfig, task: ResolvedTask) -> str:
        if self.preserve_job_name and config.job_name:
            return config.job_name
        dataset = task.dataset or getattr(config, "job_name", None) or "job"
        dataset_short = dataset.rsplit("/", 1)[-1]
        task_short = task.task_id.rsplit("/", 1)[-1]
        return f"{dataset_short}_{task_short}_{self.run_id}"

    @staticmethod
    def _apply_harbor_task(config: HarborJobConfig, task: ResolvedTask) -> None:
        if not config.datasets:
            return
        dataset_config = config.datasets[0]
        dataset_config.task_names = [task.task_id]
        if isinstance(dataset_config, RegistryDatasetConfig):
            if task.dataset:
                dataset_config.name = task.dataset if "/" in task.dataset or not task.org else f"{task.org}/{task.dataset}"
            if isinstance(dataset_config.registry, OssRegistryInfo) and task.split:
                dataset_config.registry.split = task.split
                dataset_config.version = task.split

    def _apply_bash_task(self, config: BashJobConfig, task: ResolvedTask, job_name: str) -> None:
        env = config.environment.env
        env["TASK"] = task.task_id
        env["ROCK_TASK_ID"] = task.task_id
        env["ROCK_RUN_ID"] = self.run_id
        env["ROCK_JOB_NAME"] = job_name
        if task.dataset:
            env["ROCK_DATASET"] = task.dataset
        if task.split:
            env["ROCK_SPLIT"] = task.split
