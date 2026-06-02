"""Tests for admin ops API (single-table scheduler_task, multi-pod safe)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from rock.admin.core.scheduler_task_table import (
    PHASE_NOT_FOUND,
    PHASE_RATE_LIMITED,
    PHASE_REJECTED,
    PHASE_RUNNING,
    PHASE_SUCCEEDED,
)
from rock.admin.entrypoints.admin_ops_api import (
    admin_ops_router,
    set_alive_workers_provider,
    set_scheduler_task_table,
    set_task_registry_provider,
)


def _fake_task(type_: str):
    t = MagicMock()
    t.type = type_
    t.run = AsyncMock(return_value=None)
    return t


@pytest.fixture
def app_with_router():
    app = FastAPI()
    app.include_router(admin_ops_router, prefix="/apis/envs/sandbox/v1/ops")
    return app


@pytest.fixture
def fake_table():
    """In-memory fake SchedulerTaskTable simulating shared DB."""
    tasks: dict[str, dict] = {}

    class FakeTable:
        async def insert_tasks(self, records):
            for r in records:
                tasks[r["task_id"]] = dict(r)

        async def get_tasks_by_group(self, taskset_id):
            return [dict(t) for t in tasks.values() if t["taskset_id"] == taskset_id]

        async def update_task(self, task_id, **fields):
            if task_id not in tasks:
                return False
            tasks[task_id].update(fields)
            return True

        async def has_recent_task(self, task_type, since_epoch):
            return any(t["task_type"] == task_type and t["creation_timestamp"] >= since_epoch for t in tasks.values())

    table = FakeTable()
    table._tasks = tasks
    return table


@pytest.fixture(autouse=True)
def setup_module(fake_table):
    set_scheduler_task_table(fake_table)
    registry = {
        "image_cleanup": _fake_task("image_cleanup"),
        "build_cache_cleanup": _fake_task("build_cache_cleanup"),
        "ray_log_cleanup": _fake_task("ray_log_cleanup"),
    }
    set_task_registry_provider(lambda: registry)
    set_alive_workers_provider(lambda: ["10.0.0.1", "10.0.0.2"])
    yield
    set_scheduler_task_table(None)
    set_task_registry_provider(None)
    set_alive_workers_provider(None)


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
        assert result["status"]["phase"] == PHASE_RUNNING
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
        assert body["result"]["status"]["phase"] == PHASE_RUNNING
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
        assert body["result"]["status"]["phase"] == PHASE_REJECTED

    @pytest.mark.asyncio
    async def test_rejected_unknown_whitelisted_task(self, client):
        r = await client.post(
            "/apis/envs/sandbox/v1/ops/tasksets",
            json={"spec": {"taskTypes": ["nonexistent_cleanup"]}},
        )
        body = r.json()
        assert body["status"] == "Success"
        assert body["result"]["status"]["phase"] == PHASE_REJECTED

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
        assert body["result"]["status"]["phase"] == PHASE_RATE_LIMITED

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
        assert body["result"]["status"]["phase"] == PHASE_RUNNING
        assert len(body["result"]["tasks"]) == 1
        assert body["result"]["tasks"][0]["spec"]["taskType"] == "build_cache_cleanup"

    @pytest.mark.asyncio
    async def test_tasks_persisted(self, client, fake_table):
        r = await client.post(
            "/apis/envs/sandbox/v1/ops/tasksets",
            json={"spec": {"taskTypes": ["image_cleanup"]}},
        )
        taskset_id = r.json()["result"]["metadata"]["tasksetId"]
        child_tasks = [t for t in fake_table._tasks.values() if t["taskset_id"] == taskset_id]
        assert len(child_tasks) == 1
        assert child_tasks[0]["task_type"] == "image_cleanup"

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
        assert body["result"]["status"]["phase"] in (PHASE_RUNNING, PHASE_SUCCEEDED)
        assert len(body["result"]["tasks"]) == 1

    @pytest.mark.asyncio
    async def test_get_nonexistent_taskset(self, client):
        r = await client.get("/apis/envs/sandbox/v1/ops/tasksets/doesnotexist")
        body = r.json()
        assert body["status"] == "Success"
        assert body["result"]["status"]["phase"] == PHASE_NOT_FOUND


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
        assert body["result"]["status"]["phase"] != PHASE_NOT_FOUND


class TestMisconfiguration:
    @pytest.mark.asyncio
    async def test_post_returns_failed_when_table_unset(self, app_with_router):
        set_scheduler_task_table(None)
        transport = ASGITransport(app=app_with_router)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/apis/envs/sandbox/v1/ops/tasksets", json={"spec": {}})
        assert r.json()["status"] == "Failed"

    @pytest.mark.asyncio
    async def test_get_returns_failed_when_table_unset(self, app_with_router):
        set_scheduler_task_table(None)
        transport = ASGITransport(app=app_with_router)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/apis/envs/sandbox/v1/ops/tasksets/x")
        assert r.json()["status"] == "Failed"
