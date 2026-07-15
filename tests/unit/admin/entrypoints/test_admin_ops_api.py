"""Tests for admin ops API (layered: api → OpsService → SchedulerTaskTable)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from rock.admin.core.scheduler_task_table import Phase
from rock.admin.core.schema import SchedulerTaskRecord
from rock.admin.entrypoints.admin_ops_api import admin_ops_router, set_ops_service
from rock.admin.scheduler.task_base import TaskRunReport, WorkerRunOutcome, WorkerRunResult
from rock.admin.service.ops_service import OpsService


def _fake_task(type_: str):
    t = MagicMock()
    t.type = type_

    async def run(worker_ips):
        return TaskRunReport(
            task_type=type_,
            timestamp="2026-07-15T12:00:00",
            duration_ms=1.0,
            worker_results=[
                WorkerRunResult(worker_ip=worker_ip, outcome=WorkerRunOutcome.SUCCESS)
                for worker_ip in sorted(worker_ips)
            ],
        )

    t.run = AsyncMock(side_effect=run)
    return t


class FakeTable:
    """In-memory fake SchedulerTaskTable using SchedulerTaskRecord objects."""

    def __init__(self):
        self._tasks: dict[str, SchedulerTaskRecord] = {}

    async def insert_tasks(self, records: list[SchedulerTaskRecord]):
        for r in records:
            self._tasks[r.task_id] = r

    async def get_tasks_by_group(self, taskset_id: str) -> list[SchedulerTaskRecord]:
        return [t for t in self._tasks.values() if t.taskset_id == taskset_id]

    async def update_task(self, task_id: str, **fields) -> bool:
        if task_id not in self._tasks:
            return False
        record = self._tasks[task_id]
        for k, v in fields.items():
            setattr(record, k, v)
        return True

    async def has_recent_task(self, task_type: str, since_epoch: float) -> bool:
        return any(t.task_type == task_type and t.creation_timestamp >= since_epoch for t in self._tasks.values())


@pytest.fixture
def app_with_router():
    app = FastAPI()
    app.include_router(admin_ops_router, prefix="/apis/envs/sandbox/v1/ops")
    return app


@pytest.fixture
def fake_table():
    return FakeTable()


@pytest.fixture(autouse=True)
def setup_module(fake_table):
    registry = {
        "image_cleanup": _fake_task("image_cleanup"),
        "build_cache_cleanup": _fake_task("build_cache_cleanup"),
        "ray_log_cleanup": _fake_task("ray_log_cleanup"),
    }
    service = OpsService(
        task_table=fake_table,
        task_registry=registry,
        alive_workers_provider=lambda: {"10.0.0.1", "10.0.0.2"},
    )
    set_ops_service(service)
    yield
    set_ops_service(None)


@pytest.fixture
async def client(app_with_router):
    transport = ASGITransport(app=app_with_router)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestCreateTaskSet:
    @pytest.mark.asyncio
    async def test_accepted_default_tasks_default_workers(self, client, fake_table):
        r = await client.post("/apis/envs/sandbox/v1/ops/tasksets", json={"spec": {}})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "Success"
        result = body["result"]
        assert result["status"]["phase"] == Phase.RUNNING
        assert result["metadata"]["tasksetId"] != ""
        assert len(result["metadata"]["tasksetId"]) == 32
        assert result["status"]["active"] == 3
        assert len(result["tasks"]) == 3
        task_types = {t["spec"]["taskType"] for t in result["tasks"]}
        assert task_types == {"image_cleanup", "build_cache_cleanup", "ray_log_cleanup"}

    @pytest.mark.asyncio
    async def test_accepted_specific_tasks(self, client):
        r = await client.post(
            "/apis/envs/sandbox/v1/ops/tasksets",
            json={"spec": {"taskTypes": ["image_cleanup"]}},
        )
        body = r.json()
        assert body["status"] == "Success"
        assert body["result"]["status"]["phase"] == Phase.RUNNING
        assert len(body["result"]["tasks"]) == 1
        assert body["result"]["tasks"][0]["spec"]["taskType"] == "image_cleanup"

    @pytest.mark.asyncio
    async def test_rejected_non_whitelisted_task(self, client):
        r = await client.post(
            "/apis/envs/sandbox/v1/ops/tasksets",
            json={"spec": {"taskTypes": ["image_pull"]}},
        )
        body = r.json()
        assert body["status"] == "Success"
        assert body["result"]["status"]["phase"] == Phase.REJECTED

    @pytest.mark.asyncio
    async def test_rejected_unknown_whitelisted_task(self, client):
        r = await client.post(
            "/apis/envs/sandbox/v1/ops/tasksets",
            json={"spec": {"taskTypes": ["nonexistent_cleanup"]}},
        )
        body = r.json()
        assert body["status"] == "Success"
        assert body["result"]["status"]["phase"] == Phase.REJECTED

    @pytest.mark.asyncio
    async def test_rate_limited(self, client):
        await client.post(
            "/apis/envs/sandbox/v1/ops/tasksets",
            json={"spec": {"taskTypes": ["image_cleanup"]}},
        )
        r = await client.post(
            "/apis/envs/sandbox/v1/ops/tasksets",
            json={"spec": {"taskTypes": ["image_cleanup"]}},
        )
        body = r.json()
        assert body["status"] == "Success"
        assert body["result"]["status"]["phase"] == Phase.RATE_LIMITED

    @pytest.mark.asyncio
    async def test_partial_rate_limit_runs_remainder(self, client):
        await client.post(
            "/apis/envs/sandbox/v1/ops/tasksets",
            json={"spec": {"taskTypes": ["image_cleanup"]}},
        )
        r = await client.post(
            "/apis/envs/sandbox/v1/ops/tasksets",
            json={"spec": {"taskTypes": ["image_cleanup", "build_cache_cleanup"]}},
        )
        body = r.json()
        assert body["status"] == "Success"
        assert body["result"]["status"]["phase"] == Phase.RUNNING
        assert len(body["result"]["tasks"]) == 1
        assert body["result"]["tasks"][0]["spec"]["taskType"] == "build_cache_cleanup"

    @pytest.mark.asyncio
    async def test_tasks_persisted(self, client, fake_table):
        r = await client.post(
            "/apis/envs/sandbox/v1/ops/tasksets",
            json={"spec": {"taskTypes": ["image_cleanup"]}},
        )
        taskset_id = r.json()["result"]["metadata"]["tasksetId"]
        child_tasks = [t for t in fake_table._tasks.values() if t.taskset_id == taskset_id]
        assert len(child_tasks) == 1
        assert child_tasks[0].task_type == "image_cleanup"

    @pytest.mark.asyncio
    async def test_taskset_id_is_128_bit_uuid(self, client):
        r = await client.post("/apis/envs/sandbox/v1/ops/tasksets", json={"spec": {}})
        taskset_id = r.json()["result"]["metadata"]["tasksetId"]
        assert len(taskset_id) == 32
        int(taskset_id, 16)

    @pytest.mark.asyncio
    async def test_child_task_has_taskset_reference(self, client):
        r = await client.post(
            "/apis/envs/sandbox/v1/ops/tasksets",
            json={"spec": {"taskTypes": ["image_cleanup"]}},
        )
        result = r.json()["result"]
        parent_id = result["metadata"]["tasksetId"]
        child = result["tasks"][0]
        assert child["metadata"]["tasksetId"] == parent_id


class TestGetTaskSet:
    @pytest.mark.asyncio
    async def test_get_existing_taskset_with_tasks(self, client):
        post = await client.post(
            "/apis/envs/sandbox/v1/ops/tasksets",
            json={"spec": {"taskTypes": ["image_cleanup"]}},
        )
        taskset_id = post.json()["result"]["metadata"]["tasksetId"]

        get = await client.get(f"/apis/envs/sandbox/v1/ops/tasksets/{taskset_id}")
        body = get.json()
        assert body["status"] == "Success"
        assert body["result"]["metadata"]["tasksetId"] == taskset_id
        assert body["result"]["status"]["phase"] in (Phase.RUNNING, Phase.SUCCEEDED)
        assert len(body["result"]["tasks"]) == 1

    @pytest.mark.asyncio
    async def test_get_nonexistent_taskset(self, client):
        r = await client.get("/apis/envs/sandbox/v1/ops/tasksets/doesnotexist")
        body = r.json()
        assert body["status"] == "Success"
        assert body["result"]["status"]["phase"] == Phase.NOT_FOUND


class TestTaskExecutionStatus:
    @staticmethod
    def _record(task_type: str = "image_cleanup") -> SchedulerTaskRecord:
        return SchedulerTaskRecord(
            task_id="b" * 32,
            taskset_id="a" * 32,
            task_type=task_type,
            target_workers=["10.0.0.1", "10.0.0.2", "10.0.0.3"],
            creation_timestamp=1.0,
            phase=Phase.PENDING,
            assigned_pod="admin-0",
        )

    @pytest.mark.asyncio
    async def test_partial_worker_failure_marks_task_failed_with_per_worker_status(self):
        table = FakeTable()
        record = self._record()
        await table.insert_tasks([record])
        report = TaskRunReport(
            task_type="image_cleanup",
            timestamp="2026-07-15T12:00:00",
            duration_ms=1.0,
            worker_results=[
                WorkerRunResult("10.0.0.1", WorkerRunOutcome.SUCCESS),
                WorkerRunResult("10.0.0.2", WorkerRunOutcome.FAILED, "RuntimeError", "boom"),
                WorkerRunResult("10.0.0.3", WorkerRunOutcome.TIMEOUT, "TimeoutError", "timed out"),
            ],
        )
        task = MagicMock(type="image_cleanup", run=AsyncMock(return_value=report))
        service = OpsService(table, {}, lambda: set())

        await service._run_tasks_async(
            record.taskset_id,
            [task],
            record.target_workers,
            [record],
        )

        assert record.phase == Phase.FAILED
        assert record.status == [
            {"worker": "10.0.0.1", "success": True},
            {"worker": "10.0.0.2", "success": False, "message": "boom"},
            {"worker": "10.0.0.3", "success": False, "message": "timed out"},
        ]
        assert record.conditions == [
            {
                "type": "PartialFailure",
                "reason": "partial",
                "message": "2/3 workers failed or timed out",
            }
        ]

    @pytest.mark.asyncio
    async def test_all_worker_failures_use_failed_condition(self):
        table = FakeTable()
        record = self._record()
        record.target_workers = ["10.0.0.1", "10.0.0.2"]
        await table.insert_tasks([record])
        report = TaskRunReport(
            task_type="image_cleanup",
            timestamp="2026-07-15T12:00:00",
            duration_ms=1.0,
            worker_results=[
                WorkerRunResult("10.0.0.1", WorkerRunOutcome.FAILED, "RuntimeError", "boom"),
                WorkerRunResult("10.0.0.2", WorkerRunOutcome.TIMEOUT, "TimeoutError", "timed out"),
            ],
        )
        task = MagicMock(type="image_cleanup", run=AsyncMock(return_value=report))
        service = OpsService(table, {}, lambda: set())

        await service._run_tasks_async(
            record.taskset_id,
            [task],
            record.target_workers,
            [record],
        )

        assert record.phase == Phase.FAILED
        assert record.conditions == [
            {
                "type": "Failed",
                "reason": "failed",
                "message": "2/2 workers failed or timed out",
            }
        ]


class TestMultiPod:
    @pytest.mark.asyncio
    async def test_post_pod_a_get_pod_b_shares_state(self, fake_table):
        app_a = FastAPI()
        app_a.include_router(admin_ops_router, prefix="/apis/envs/sandbox/v1/ops")
        app_b = FastAPI()
        app_b.include_router(admin_ops_router, prefix="/apis/envs/sandbox/v1/ops")

        async with AsyncClient(transport=ASGITransport(app=app_a), base_url="http://a") as ca:
            post = await ca.post(
                "/apis/envs/sandbox/v1/ops/tasksets",
                json={"spec": {"taskTypes": ["image_cleanup"]}},
            )
            taskset_id = post.json()["result"]["metadata"]["tasksetId"]

        async with AsyncClient(transport=ASGITransport(app=app_b), base_url="http://b") as cb:
            get = await cb.get(f"/apis/envs/sandbox/v1/ops/tasksets/{taskset_id}")
            body = get.json()

        assert body["status"] == "Success"
        assert body["result"]["metadata"]["tasksetId"] == taskset_id
        assert body["result"]["status"]["phase"] != Phase.NOT_FOUND


class TestMisconfiguration:
    @pytest.mark.asyncio
    async def test_post_returns_failed_when_service_unset(self, app_with_router):
        set_ops_service(None)
        transport = ASGITransport(app=app_with_router)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/apis/envs/sandbox/v1/ops/tasksets", json={"spec": {}})
        assert r.json()["status"] == "Failed"

    @pytest.mark.asyncio
    async def test_get_returns_failed_when_service_unset(self, app_with_router):
        set_ops_service(None)
        transport = ASGITransport(app=app_with_router)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/apis/envs/sandbox/v1/ops/tasksets/x")
        assert r.json()["status"] == "Failed"
