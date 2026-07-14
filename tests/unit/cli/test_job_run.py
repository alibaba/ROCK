from __future__ import annotations

import json
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from rock.sdk.bench.models.job.config import HarborJobConfig, OssRegistryInfo, RegistryDatasetConfig
from rock.sdk.envhub.config import OssMirrorConfig
from rock.sdk.envhub.datasets.models import DatasetSpec
from rock.sdk.job.config import BashJobConfig
from rock.sdk.job.meta import RunScoreSummary
from rock.sdk.job.result import ExceptionInfo, TrialResult


def test_resolve_task_ids_filters_full_dataset_with_limit(monkeypatch):
    from rock.cli.job_run import resolve_task_ids

    client = MagicMock()
    client.list_dataset_tasks.return_value = DatasetSpec(id="alibaba/bench", split="test", task_ids=["t1", "t2", "t3"])
    monkeypatch.setattr("rock.cli.job_run.DatasetClient", lambda registry: client)
    config = HarborJobConfig(
        experiment_id="exp",
        datasets=[RegistryDatasetConfig(name="alibaba/bench", registry=OssRegistryInfo(split="test"))],
    )

    mode, ref, tasks = resolve_task_ids(
        config,
        task=None,
        tasks=None,
        all_tasks=True,
        org=None,
        dataset=None,
        split=None,
        limit=2,
    )

    assert mode == "full"
    assert ref.full_name == "alibaba/bench"
    assert tasks == ["t1", "t2"]


def test_resolve_task_ids_supports_bash_task_from_environment():
    from rock.cli.job_run import resolve_task_ids

    config = BashJobConfig(script="echo hi", environment={"env": {"TASK": "task-1"}})

    mode, _ref, tasks = resolve_task_ids(
        config,
        task=None,
        tasks=None,
        all_tasks=False,
        org=None,
        dataset=None,
        split=None,
        limit=None,
    )

    assert mode == "single"
    assert tasks == ["task-1"]


def test_sync_namespace_uses_oss_mirror_namespace_when_cli_namespace_is_omitted():
    from rock.cli.job_run import sync_namespace

    config = BashJobConfig(
        script="echo hi",
        environment={"oss_mirror": OssMirrorConfig(enabled=True, namespace="ns", oss_bucket="b", oss_endpoint="e")},
    )

    sync_namespace(config, None)

    assert config.namespace == "ns"
    assert config.environment.oss_mirror.namespace == "ns"


def test_build_run_summary_from_trial_results():
    from rock.cli.job_run import build_run_summary

    summary = build_run_summary(
        task_ids=["t1", "t2"],
        trial_results=[
            TrialResult(task_name="t1"),
            TrialResult(task_name="t2", exception_info=ExceptionInfo(exception_type="Error")),
        ],
    )

    assert summary.completed == 1
    assert summary.failed == 1
    assert summary.pass_rate == pytest.approx(0.5)


def test_jsonl_progress_reporter_emits_json_lines():
    from rock.cli.job_run import JsonlProgressReporter

    stream = StringIO()
    reporter = JsonlProgressReporter(stream)

    reporter.emit({"type": "run_started", "run_id": "run-1"})
    reporter.emit({"type": "summary", "summary": RunScoreSummary(completed=1, failed=0, skipped=0, avg_score=1.0, total_score=1.0, pass_rate=1.0).model_dump()})

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert lines[0] == {"type": "run_started", "run_id": "run-1"}
    assert lines[1]["type"] == "summary"


async def test_unified_handler_writes_run_meta_and_runs_each_task():
    from rock.cli.job_run import DatasetRef, NullProgressReporter, UnifiedJobRunHandler
    from rock.sdk.job.planner import PlannedJob

    writes = []

    class FakeRunRepo:
        def write(self, meta):
            writes.append(meta.model_copy(deep=True))

    class FakeExecutor:
        _max_concurrent = 1

        async def run_job(self, job, callbacks=None):
            client = SimpleNamespace(sandbox=SimpleNamespace(sandbox_id="sb"), session="s", pid=1)
            if callbacks:
                callbacks.on_started(client)
            result = TrialResult(task_name=job.task_id)
            if callbacks:
                callbacks.on_done(client, result)
            return result

    config = BashJobConfig(job_name="job", script="echo hi")
    result = await UnifiedJobRunHandler(
        mode="multi",
        task_ids=["t1", "t2"],
        dataset_ref=DatasetRef(org=None, dataset=None, split=None),
        run_id="run-1",
        run_meta_repo=FakeRunRepo(),
        job_meta_repo=None,
        executor=FakeExecutor(),
        progress=NullProgressReporter(),
    ).run(config)

    assert result.failed == 0
    assert writes[0].status == "planning"
    assert writes[-1].status == "completed"
    assert writes[-1].task_job_map == {"t1": "job_t1_run-1", "t2": "job_t2_run-1"}


async def test_unified_handler_preserves_yaml_job_name_for_single_task():
    from rock.cli.job_run import DatasetRef, NullProgressReporter, UnifiedJobRunHandler

    seen_job_names = []

    class FakeExecutor:
        _max_concurrent = 1

        async def run_job(self, job, callbacks=None):
            seen_job_names.append(job.job_name)
            client = SimpleNamespace(sandbox=SimpleNamespace(sandbox_id="sb"), session="s", pid=1)
            if callbacks:
                callbacks.on_started(client)
            result = TrialResult(task_name=job.task_id)
            if callbacks:
                callbacks.on_done(client, result)
            return result

    result = await UnifiedJobRunHandler(
        mode="single",
        task_ids=["t1"],
        dataset_ref=DatasetRef(org=None, dataset=None, split=None),
        run_id="run-1",
        run_meta_repo=None,
        job_meta_repo=None,
        executor=FakeExecutor(),
        progress=NullProgressReporter(),
    ).run(BashJobConfig(job_name="yaml-job", script="echo hi"))

    assert result.failed == 0
    assert seen_job_names == ["yaml-job"]


async def test_unified_handler_resume_recovers_running_job_meta_before_new_sandbox():
    from rock.cli.job_run import DatasetRef, NullProgressReporter, UnifiedJobRunHandler
    from rock.sdk.job.meta import JobMeta, RunMeta

    existing = JobMeta(
        job_name="old-job",
        run_id="run-1",
        task_id="t1",
        status="running",
        sandbox_id="sb-1",
        session="session-1",
        pid=42,
    )

    class FakeRunRepo:
        def write(self, meta):
            pass

    class FakeJobRepo:
        def get(self, job_name):
            assert job_name == "old-job"
            return existing

        def write(self, meta):
            pass

        def update_status(self, *args, **kwargs):
            raise AssertionError("recoverable job should not be marked unrecoverable")

    class FakeExecutor:
        _max_concurrent = 1

        async def run_job(self, job, callbacks=None):
            raise AssertionError("recoverable job should not start a new sandbox")

        async def wait_existing_job(self, job, meta):
            assert job.job_name == "old-job"
            assert meta is existing
            return TrialResult(task_name="t1")

    result = await UnifiedJobRunHandler(
        mode="single",
        task_ids=["t1"],
        dataset_ref=DatasetRef(org=None, dataset=None, split=None),
        run_id="run-1",
        run_meta_repo=FakeRunRepo(),
        job_meta_repo=FakeJobRepo(),
        executor=FakeExecutor(),
        progress=NullProgressReporter(),
        resumed=True,
        base_run_meta=RunMeta(
            run_id="run-1",
            mode="single",
            status="running",
            total_tasks=1,
            pending_tasks=1,
            task_job_map={"t1": "old-job"},
        ),
    ).run(BashJobConfig(job_name="job", script="echo hi"))

    assert result.failed == 0
