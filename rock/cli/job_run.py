"""CLI orchestration for unified ``rock job run``."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import TextIO

from pydantic import BaseModel, Field

from rock.sdk.bench.models.job.config import HarborJobConfig, OssRegistryInfo
from rock.sdk.envhub.datasets import DatasetClient
from rock.sdk.job.config import BashJobConfig, JobConfig
from rock.sdk.job.executor import ExistingJobHandle, JobExecutor, TrialClient
from rock.sdk.job.planner import PlannedJob, ResolvedTask, SingleTaskPlanner
from rock.sdk.job.result import TrialResult


@dataclass(frozen=True)
class DatasetRef:
    org: str | None
    dataset: str | None
    split: str | None

    @property
    def full_name(self) -> str | None:
        if not self.dataset:
            return None
        if "/" in self.dataset or not self.org:
            return self.dataset
        return f"{self.org}/{self.dataset}"


@dataclass(frozen=True)
class RunResult:
    run_id: str
    total: int
    failed: int
    summary: RunScoreSummary


class RunScoreSummary(BaseModel):
    """Process-local score summary emitted by one CLI invocation."""

    completed: int
    failed: int
    skipped: int
    avg_score: float
    total_score: float
    pass_rate: float
    scores: dict[str, float] = Field(default_factory=dict)


class NullProgressReporter:
    def emit(self, payload: dict) -> None:
        return None


class JsonlProgressReporter:
    def __init__(self, stream: TextIO | None = None):
        self._stream = stream or sys.stdout

    def emit(self, payload: dict) -> None:
        self._stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._stream.flush()


def generate_run_id() -> str:
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def resolve_dataset_ref(
    config: JobConfig,
    *,
    org: str | None,
    dataset: str | None,
    split: str | None,
) -> DatasetRef:
    config_org = None
    config_dataset = None
    config_split = None

    if isinstance(config, HarborJobConfig) and config.datasets:
        dataset_config = config.datasets[0]
        config_dataset = getattr(dataset_config, "name", None)
        if config_dataset and "/" in config_dataset:
            config_org, config_dataset = config_dataset.split("/", 1)
        registry = getattr(dataset_config, "registry", None)
        if isinstance(registry, OssRegistryInfo):
            config_split = registry.split
        config_split = config_split or getattr(dataset_config, "version", None)
    elif isinstance(config, BashJobConfig):
        env = config.environment.env
        config_dataset = env.get("ROCK_DATASET")
        config_split = env.get("ROCK_SPLIT")
        if config_dataset and "/" in config_dataset:
            config_org, config_dataset = config_dataset.split("/", 1)

    selected_dataset = dataset or config_dataset
    selected_org = org or config_org
    if selected_dataset and "/" in selected_dataset:
        embedded_org, embedded_dataset = selected_dataset.split("/", 1)
        selected_org = org or embedded_org
        selected_dataset = embedded_dataset

    return DatasetRef(org=selected_org, dataset=selected_dataset, split=split or config_split)


def resolve_oss_registry_info(config: JobConfig) -> OssRegistryInfo:
    if isinstance(config, HarborJobConfig) and config.datasets:
        registry = getattr(config.datasets[0], "registry", None)
        if isinstance(registry, OssRegistryInfo):
            return registry

    mirror = config.environment.oss_mirror
    if mirror and mirror.enabled:
        return OssRegistryInfo(
            oss_bucket=mirror.oss_bucket,
            oss_endpoint=mirror.oss_endpoint,
            oss_access_key_id=mirror.oss_access_key_id,
            oss_access_key_secret=mirror.oss_access_key_secret,
            oss_region=mirror.oss_region,
        )

    raise ValueError("dataset task enumeration requires a dataset registry or environment.oss_mirror")


def get_config_task_ids(config: JobConfig) -> list[str]:
    if isinstance(config, HarborJobConfig) and config.datasets:
        return list(getattr(config.datasets[0], "task_names", None) or [])
    if isinstance(config, BashJobConfig):
        task = config.environment.env.get("TASK")
        return [task] if task else []
    return []


def resolve_task_ids(
    config: JobConfig,
    *,
    task: str | None,
    tasks: str | None,
    all_tasks: bool,
    org: str | None,
    dataset: str | None,
    split: str | None,
    limit: int | None,
) -> tuple[str, DatasetRef, list[str]]:
    explicit_count = sum(bool(v) for v in (task, tasks, all_tasks))
    if explicit_count > 1:
        raise ValueError("--task, --tasks and --all are mutually exclusive")

    dataset_ref = resolve_dataset_ref(config, org=org, dataset=dataset, split=split)
    if task:
        return "single", dataset_ref, [task]
    if tasks:
        parsed = [item.strip() for item in tasks.split(",") if item.strip()]
        if not parsed:
            raise ValueError("--tasks must contain at least one task id")
        return ("single" if len(parsed) == 1 else "multi"), dataset_ref, parsed[:limit] if limit else parsed
    if all_tasks:
        if not dataset_ref.org or not dataset_ref.dataset:
            raise ValueError("--all requires dataset org/name from config or CLI args")
        selected_split = dataset_ref.split or "test"
        client = DatasetClient(resolve_oss_registry_info(config))
        spec = client.list_dataset_tasks(dataset_ref.org, dataset_ref.dataset, selected_split)
        if spec is None:
            raise ValueError(f"Dataset split not found: {dataset_ref.org}/{dataset_ref.dataset}@{selected_split}")
        task_ids = list(spec.task_ids)
        if limit is not None:
            task_ids = task_ids[:limit]
        return "full", DatasetRef(dataset_ref.org, dataset_ref.dataset, selected_split), task_ids

    config_tasks = get_config_task_ids(config)
    if len(config_tasks) == 1:
        return "single", dataset_ref, config_tasks
    if len(config_tasks) > 1:
        return "multi", dataset_ref, config_tasks[:limit] if limit else config_tasks
    raise ValueError("fresh run requires an explicit task: use --task, --tasks, --all, or a config with one task")


def flatten_trial_results(results: Sequence[TrialResult | list[TrialResult]]) -> list[TrialResult]:
    flat: list[TrialResult] = []
    for result in results:
        if isinstance(result, list):
            flat.extend(result)
        else:
            flat.append(result)
    return flat


def build_run_summary(
    *, task_ids: Sequence[str], trial_results: Sequence[TrialResult | list[TrialResult]]
) -> RunScoreSummary:
    by_task: dict[str, TrialResult] = {}
    for result in flatten_trial_results(trial_results):
        if result.task_name:
            by_task[result.task_name] = result

    completed = 0
    failed = 0
    scores: dict[str, float] = {}
    for task_id in task_ids:
        result = by_task.get(task_id)
        if result is None:
            failed += 1
            continue
        score = result.score if result.score is not None else 0.0
        scores[task_id] = score
        if result.status == "completed":
            completed += 1
        else:
            failed += 1

    total = len(task_ids)
    total_score = sum(scores.values())
    return RunScoreSummary(
        completed=completed,
        failed=failed,
        skipped=0,
        avg_score=total_score / total if total else 0.0,
        total_score=total_score,
        pass_rate=completed / total if total else 0.0,
        scores=scores,
    )


def sync_namespace(config: JobConfig, namespace: str | None) -> None:
    mirror = config.environment.oss_mirror
    selected_namespace = namespace or config.namespace or (mirror.namespace if mirror else None)
    if not selected_namespace:
        return
    config.namespace = selected_namespace
    if hasattr(config.environment, "namespace"):
        config.environment.namespace = selected_namespace
    if mirror:
        mirror.namespace = selected_namespace


class _Callbacks:
    def __init__(self, handler: UnifiedJobRunHandler, planned_job: PlannedJob, index: int):
        self._handler = handler
        self._planned_job = planned_job
        self._index = index

    def on_started(self, client: TrialClient) -> None:
        self._handler.on_job_started(self._planned_job, client, self._index)

    def on_done(self, client: TrialClient, result: TrialResult | list[TrialResult]) -> None:
        self._handler.on_job_done(self._planned_job, client, result, self._index)


class UnifiedJobRunHandler:
    """CLI-level orchestration for single, multi, and full runs."""

    def __init__(
        self,
        *,
        mode: str,
        task_ids: Sequence[str],
        dataset_ref: DatasetRef,
        run_id: str,
        executor: JobExecutor,
        progress: JsonlProgressReporter | NullProgressReporter,
        resume_handle: ExistingJobHandle | None = None,
    ):
        if resume_handle is not None and (mode != "single" or len(task_ids) != 1):
            raise ValueError("resume_handle requires exactly one task in single mode")
        self.mode = mode
        self.task_ids = list(task_ids)
        self.dataset_ref = dataset_ref
        self.run_id = run_id
        self.executor = executor
        self.progress = progress
        self.resume_handle = resume_handle
        self._completed = 0
        self._passed = 0
        self._failed = 0

    async def run(self, config: JobConfig) -> RunResult:
        started = datetime.now(timezone.utc)
        planner = SingleTaskPlanner(run_id=self.run_id, preserve_job_name=self.mode == "single")
        planned_jobs = []
        for task_id in self.task_ids:
            planned = planner.plan(
                config,
                task=ResolvedTask(
                    task_id=task_id,
                    org=self.dataset_ref.org,
                    dataset=self.dataset_ref.full_name,
                    split=self.dataset_ref.split,
                ),
            )
            planned_jobs.append(planned)

        self.progress.emit(
            {
                "type": "run_started",
                "run_id": self.run_id,
                "mode": self.mode,
                "total": len(self.task_ids),
                "pending": len(self.task_ids),
                "resumed": self.resume_handle is not None,
            }
        )

        semaphore = asyncio.Semaphore(self.executor._max_concurrent or len(planned_jobs) or 1)

        async def run_one(index: int, job: PlannedJob):
            async with semaphore:
                if self.resume_handle is not None:
                    self.progress.emit(
                        {
                            "type": "job_recovered",
                            "run_id": self.run_id,
                            "task_id": job.task_id,
                            "job_name": job.job_name,
                            "sandbox_id": self.resume_handle.sandbox_id,
                        }
                    )
                    result = await self.executor.wait_existing_job(job, self.resume_handle)
                    client = SimpleNamespace(
                        sandbox=SimpleNamespace(sandbox_id=self.resume_handle.sandbox_id),
                        session=self.resume_handle.session,
                        pid=self.resume_handle.pid,
                    )
                    self.on_job_done(job, client, result, index)
                    return result
                return await self.executor.run_job(job, callbacks=_Callbacks(self, job, index))

        results = list(await asyncio.gather(*[run_one(i, job) for i, job in enumerate(planned_jobs)]))
        summary = build_run_summary(task_ids=self.task_ids, trial_results=results)
        status = "completed" if summary.failed == 0 else "failed" if summary.completed == 0 else "partial"
        duration_s = int((datetime.now(timezone.utc) - started).total_seconds())
        self.progress.emit(
            {
                "type": "summary",
                "run_id": self.run_id,
                "status": status,
                "total": len(self.task_ids),
                "passed": summary.completed,
                "failed": summary.failed,
                "pass_rate": summary.pass_rate,
                "avg_score": summary.avg_score,
                "duration_s": duration_s,
            }
        )
        return RunResult(run_id=self.run_id, total=len(self.task_ids), failed=summary.failed, summary=summary)

    def on_job_started(self, job: PlannedJob, client: TrialClient, index: int) -> None:
        self.progress.emit(
            {
                "type": "job_started",
                "run_id": self.run_id,
                "task_id": job.task_id,
                "job_name": job.job_name,
                "index": index,
                "total": len(self.task_ids),
                "sandbox_id": getattr(client.sandbox, "sandbox_id", None),
            }
        )

    def on_job_done(
        self,
        job: PlannedJob,
        client: TrialClient,
        result: TrialResult | list[TrialResult],
        index: int,
    ) -> None:
        primary = flatten_trial_results([result])[0]
        if primary.status == "completed":
            self._passed += 1
        else:
            self._failed += 1
        self._completed += 1
        error = None
        if primary.exception_info:
            error = primary.exception_info.exception_message or primary.exception_info.exception_type
        self.progress.emit(
            {
                "type": "job_done",
                "run_id": self.run_id,
                "task_id": job.task_id,
                "job_name": job.job_name,
                "status": primary.status,
                "score": primary.score,
                "index": index,
                "total": len(self.task_ids),
                "sandbox_id": getattr(client.sandbox, "sandbox_id", None),
                "error": error,
            }
        )
        self.progress.emit(
            {
                "type": "progress",
                "run_id": self.run_id,
                "completed": self._completed,
                "passed": self._passed,
                "failed": self._failed,
                "total": len(self.task_ids),
            }
        )
