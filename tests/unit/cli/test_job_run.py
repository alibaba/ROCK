from __future__ import annotations

import json
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from rock.sdk.bench.models.job.config import HarborJobConfig, OssRegistryInfo, RegistryDatasetConfig
from rock.sdk.envhub.config import OssMirrorConfig
from rock.sdk.envhub.datasets.models import DatasetSpec
from rock.sdk.job.config import BashJobConfig
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
    from rock.cli.job_run import JsonlProgressReporter, RunScoreSummary

    stream = StringIO()
    reporter = JsonlProgressReporter(stream)

    reporter.emit({"type": "job_started", "job_id": "12345678-1234-5678-1234-567812345678"})
    reporter.emit(
        {
            "type": "summary",
            "summary": RunScoreSummary(
                completed=1, failed=0, skipped=0, avg_score=1.0, total_score=1.0, pass_rate=1.0
            ).model_dump(),
        }
    )

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert lines[0] == {
        "type": "job_started",
        "job_id": "12345678-1234-5678-1234-567812345678",
    }
    assert lines[1]["type"] == "summary"


async def test_unified_handler_runs_each_task_without_metadata_repositories():
    from rock.cli.job_run import DatasetRef, UnifiedJobRunHandler

    seen_jobs = []
    events = []

    class CaptureProgressReporter:
        def emit(self, payload):
            events.append(payload)

    class FakeExecutor:
        _max_concurrent = 1

        async def run_job(self, job, callbacks=None):
            seen_jobs.append((job.task_id, job.job_id))
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
        executor=FakeExecutor(),
        progress=CaptureProgressReporter(),
    ).run(config)

    assert result.failed == 0
    assert result.total == 2
    assert [task_id for task_id, _job_id in seen_jobs] == ["t1", "t2"]
    assert len({job_id for _task_id, job_id in seen_jobs}) == 2
    assert all(isinstance(job_id, UUID) for _task_id, job_id in seen_jobs)
    assert all("run_id" not in event for event in events)
    job_events = [event for event in events if event["type"] in {"job_started", "job_done"}]
    assert len(job_events) == 4
    assert all(UUID(event["job_id"]) for event in job_events)


async def test_unified_handler_uses_supplied_job_id_in_job_and_events():
    from rock.cli.job_run import DatasetRef, UnifiedJobRunHandler

    supplied_job_id = UUID("12345678-1234-5678-1234-567812345678")
    seen_job_ids = []
    events = []

    class CaptureProgressReporter:
        def emit(self, payload):
            events.append(payload)

    class FakeExecutor:
        _max_concurrent = 1

        async def run_job(self, job, callbacks=None):
            seen_job_ids.append(job.job_id)
            client = SimpleNamespace(sandbox=SimpleNamespace(sandbox_id="sb"), session="s", pid=1)
            if callbacks:
                callbacks.on_started(client)
            result = TrialResult(task_name=job.task_id)
            if callbacks:
                callbacks.on_done(client, result)
            return result

    await UnifiedJobRunHandler(
        mode="single",
        task_ids=["t1"],
        dataset_ref=DatasetRef(org=None, dataset=None, split=None),
        job_id=supplied_job_id,
        executor=FakeExecutor(),
        progress=CaptureProgressReporter(),
    ).run(BashJobConfig(job_name="job", script="echo hi"))

    assert seen_job_ids == [supplied_job_id]
    job_events = [event for event in events if event["type"] in {"job_started", "job_done"}]
    assert [event["job_id"] for event in job_events] == [str(supplied_job_id), str(supplied_job_id)]


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
        executor=FakeExecutor(),
        progress=NullProgressReporter(),
    ).run(BashJobConfig(job_name="yaml-job", script="echo hi"))

    assert result.failed == 0
    assert seen_job_names == ["yaml-job"]


async def test_unified_handler_resumes_one_explicit_handle_without_starting_new_sandbox():
    from rock.cli.job_run import DatasetRef, NullProgressReporter, UnifiedJobRunHandler
    from rock.sdk.job.executor import ExistingJobHandle

    handle = ExistingJobHandle(
        sandbox_id="sb-1",
        session="session-1",
        pid=42,
    )

    class FakeExecutor:
        _max_concurrent = 1

        async def run_job(self, job, callbacks=None):
            raise AssertionError("resume must not start a new sandbox")

        async def wait_existing_job(self, job, received_handle):
            assert job.job_name == "old-job"
            assert received_handle is handle
            return TrialResult(task_name="t1")

    result = await UnifiedJobRunHandler(
        mode="single",
        task_ids=["t1"],
        dataset_ref=DatasetRef(org=None, dataset=None, split=None),
        executor=FakeExecutor(),
        progress=NullProgressReporter(),
        resume_handle=handle,
    ).run(BashJobConfig(job_name="old-job", script="echo hi"))

    assert result.failed == 0


async def test_unified_handler_does_not_fall_back_when_resume_wait_fails():
    from rock.cli.job_run import DatasetRef, NullProgressReporter, UnifiedJobRunHandler
    from rock.sdk.job.executor import ExistingJobHandle

    handle = ExistingJobHandle(
        sandbox_id="sb-1",
        session="session-1",
        pid=42,
    )

    class FakeExecutor:
        _max_concurrent = 1

        async def run_job(self, job, callbacks=None):
            raise AssertionError("resume failure must not start a new sandbox")

        async def wait_existing_job(self, job, received_handle):
            assert received_handle is handle
            raise RuntimeError("existing process not found")

    handler = UnifiedJobRunHandler(
        mode="single",
        task_ids=["t1"],
        dataset_ref=DatasetRef(org=None, dataset=None, split=None),
        executor=FakeExecutor(),
        progress=NullProgressReporter(),
        resume_handle=handle,
    )

    with pytest.raises(RuntimeError, match="existing process not found"):
        await handler.run(BashJobConfig(job_name="old-job", script="echo hi"))
