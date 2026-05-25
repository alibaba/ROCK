"""Tests for admin ops API (DB-backed, multi-pod safe)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from rock.admin.core.ops_job_table import (
    JOB_STATUS_ACCEPTED,
    JOB_STATUS_NOT_FOUND,
    JOB_STATUS_RATE_LIMITED,
    JOB_STATUS_REJECTED,
)
from rock.admin.entrypoints.admin_ops_api import (
    admin_ops_router,
    set_alive_workers_provider,
    set_ops_job_table,
    set_task_registry_provider,
)


def _fake_task(type_: str):
    """Minimal BaseTask stand-in."""
    t = MagicMock()
    t.type = type_
    t.run = AsyncMock(return_value=None)
    return t


@pytest.fixture
def app_with_router():
    app = FastAPI()
    app.include_router(admin_ops_router, prefix="/apis/v1")
    return app


@pytest.fixture
def fake_table():
    """In-memory fake OpsJobTable behaving like a shared DB across pods."""
    store: dict[str, dict] = {}

    class FakeTable:
        async def insert(self, job):
            store[job["job_id"]] = dict(job)

        async def get(self, job_id):
            return dict(store[job_id]) if job_id in store else None

        async def update_status(self, job_id, status, results=None, error=None):
            if job_id not in store:
                return False
            store[job_id]["status"] = status
            if results is not None:
                store[job_id]["results"] = results
            if error is not None:
                store[job_id]["error"] = error
            return True

        async def list_recent_by_tasks(self, task_types, since_epoch):
            out = []
            for r in store.values():
                if r["submitted_at"] >= since_epoch and (set(r.get("tasks") or []) & set(task_types)):
                    out.append(dict(r))
            return out

    table = FakeTable()
    table._store = store  # exposed for assertions
    return table


@pytest.fixture(autouse=True)
def setup_module(fake_table):
    set_ops_job_table(fake_table)
    registry = {
        "image_cleanup": _fake_task("image_cleanup"),
        "build_cache_cleanup": _fake_task("build_cache_cleanup"),
        "ray_log_cleanup": _fake_task("ray_log_cleanup"),
    }
    set_task_registry_provider(lambda: registry)
    set_alive_workers_provider(lambda: ["10.0.0.1", "10.0.0.2"])
    yield
    set_ops_job_table(None)
    set_task_registry_provider(None)
    set_alive_workers_provider(None)


@pytest.fixture
async def client(app_with_router):
    transport = ASGITransport(app=app_with_router)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestSubmitOpsJob:
    @pytest.mark.asyncio
    async def test_accepted_default_tasks_default_workers(self, client, fake_table):
        r = await client.post("/apis/v1/admin/ops/jobs", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "Success"
        assert body["result"]["status"] == JOB_STATUS_ACCEPTED
        assert body["result"]["job_id"] is not None
        assert set(body["result"]["tasks"]) == {"image_cleanup", "build_cache_cleanup", "ray_log_cleanup"}
        assert body["result"]["worker_count"] == 2

    @pytest.mark.asyncio
    async def test_accepted_specific_tasks(self, client):
        r = await client.post("/apis/v1/admin/ops/jobs", json={"tasks": ["image_cleanup"]})
        body = r.json()
        assert body["status"] == "Success"
        assert body["result"]["status"] == JOB_STATUS_ACCEPTED
        assert body["result"]["tasks"] == ["image_cleanup"]

    @pytest.mark.asyncio
    async def test_rejected_non_whitelisted_task(self, client):
        """Tasks not ending in _cleanup / _prune are rejected. SUCCESS + result.rejected."""
        r = await client.post("/apis/v1/admin/ops/jobs", json={"tasks": ["image_pull"]})
        body = r.json()
        assert body["status"] == "Success"
        assert body["result"]["status"] == JOB_STATUS_REJECTED
        assert body["result"]["rejected_tasks"] == ["image_pull"]
        assert body["result"]["job_id"] is None

    @pytest.mark.asyncio
    async def test_rejected_unknown_whitelisted_task(self, client):
        """Whitelisted suffix but not in registry → also rejected."""
        r = await client.post("/apis/v1/admin/ops/jobs", json={"tasks": ["nonexistent_cleanup"]})
        body = r.json()
        assert body["status"] == "Success"
        assert body["result"]["status"] == JOB_STATUS_REJECTED
        assert "nonexistent_cleanup" in body["result"]["rejected_tasks"]

    @pytest.mark.asyncio
    async def test_rate_limited(self, client):
        """Second POST of same task within cooldown → SUCCESS + result.rate_limited."""
        # 1st: accepted
        await client.post("/apis/v1/admin/ops/jobs", json={"tasks": ["image_cleanup"]})
        # 2nd: blocked
        r = await client.post("/apis/v1/admin/ops/jobs", json={"tasks": ["image_cleanup"]})
        body = r.json()
        assert body["status"] == "Success"
        assert body["result"]["status"] == JOB_STATUS_RATE_LIMITED
        assert body["result"]["rate_limited_tasks"] == ["image_cleanup"]
        assert body["result"]["cooldown_seconds"] == 60

    @pytest.mark.asyncio
    async def test_partial_rate_limit_runs_remainder(self, client, fake_table):
        """One task rate-limited, the other still runs."""
        await client.post("/apis/v1/admin/ops/jobs", json={"tasks": ["image_cleanup"]})
        r = await client.post(
            "/apis/v1/admin/ops/jobs",
            json={"tasks": ["image_cleanup", "build_cache_cleanup"]},
        )
        body = r.json()
        assert body["status"] == "Success"
        assert body["result"]["status"] == JOB_STATUS_ACCEPTED
        assert body["result"]["tasks"] == ["build_cache_cleanup"]
        assert body["result"]["rate_limited_tasks"] == ["image_cleanup"]

    @pytest.mark.asyncio
    async def test_job_persisted_to_db(self, client, fake_table):
        r = await client.post("/apis/v1/admin/ops/jobs", json={"tasks": ["image_cleanup"]})
        job_id = r.json()["result"]["job_id"]
        assert job_id in fake_table._store
        assert fake_table._store[job_id]["status"] in ("accepted", "running", "completed")
        assert fake_table._store[job_id]["pod_id"]


class TestGetOpsJob:
    @pytest.mark.asyncio
    async def test_get_existing_job(self, client):
        post = await client.post("/apis/v1/admin/ops/jobs", json={"tasks": ["image_cleanup"]})
        job_id = post.json()["result"]["job_id"]

        get = await client.get(f"/apis/v1/admin/ops/jobs/{job_id}")
        body = get.json()
        assert body["status"] == "Success"
        assert body["result"]["job_id"] == job_id
        assert body["result"]["status"] in ("accepted", "running", "completed")

    @pytest.mark.asyncio
    async def test_get_nonexistent_job_returns_success_not_found(self, client):
        r = await client.get("/apis/v1/admin/ops/jobs/doesnotexist")
        body = r.json()
        assert body["status"] == "Success"
        assert body["result"]["job_id"] == "doesnotexist"
        assert body["result"]["status"] == JOB_STATUS_NOT_FOUND


class TestMultiPod:
    @pytest.mark.asyncio
    async def test_post_pod_a_get_pod_b_shares_state(self, fake_table):
        """POST on pod A's app instance, GET from pod B's app — same DB,
        both see the job. Simulates the multi-pod scenario where the old
        process-local _async_jobs dict failed.
        """
        # Two FastAPI apps mounted with the same router & same fake_table —
        # simulating two admin pods sharing the DB.
        app_a = FastAPI()
        app_a.include_router(admin_ops_router, prefix="/apis/v1")
        app_b = FastAPI()
        app_b.include_router(admin_ops_router, prefix="/apis/v1")

        async with AsyncClient(transport=ASGITransport(app=app_a), base_url="http://a") as ca:
            post = await ca.post("/apis/v1/admin/ops/jobs", json={"tasks": ["image_cleanup"]})
            job_id = post.json()["result"]["job_id"]
            assert job_id

        async with AsyncClient(transport=ASGITransport(app=app_b), base_url="http://b") as cb:
            get = await cb.get(f"/apis/v1/admin/ops/jobs/{job_id}")
            body = get.json()

        assert body["status"] == "Success"
        assert body["result"]["job_id"] == job_id
        assert body["result"]["status"] != JOB_STATUS_NOT_FOUND


class TestMisconfiguration:
    @pytest.mark.asyncio
    async def test_post_returns_failed_when_table_unset(self, app_with_router):
        set_ops_job_table(None)
        transport = ASGITransport(app=app_with_router)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/apis/v1/admin/ops/jobs", json={})
        assert r.json()["status"] == "Failed"

    @pytest.mark.asyncio
    async def test_get_returns_failed_when_table_unset(self, app_with_router):
        set_ops_job_table(None)
        transport = ASGITransport(app=app_with_router)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/apis/v1/admin/ops/jobs/x")
        assert r.json()["status"] == "Failed"
