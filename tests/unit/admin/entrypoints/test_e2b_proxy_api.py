from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from rock.actions.sandbox.response import State
from rock.admin.entrypoints.e2b_proxy_api import e2b_proxy_router, set_e2b_proxy_service
from rock.sandbox.sandbox_meta_store import SandboxMetaStore
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService


@pytest.fixture
async def e2b_proxy_app(redis_provider, _memory_sandbox_table, rock_config, monkeypatch):
    meta_store = SandboxMetaStore(
        redis_provider=redis_provider,
        sandbox_table=_memory_sandbox_table,
        rock_config=rock_config,
    )
    proxy_service = SandboxProxyService(rock_config=rock_config, meta_store=meta_store)
    proxy_service._rpc_client = AsyncMock()
    proxy_service._rpc_client.post.side_effect = RuntimeError("rocklet unavailable")
    proxy_service._rpc_client.get.side_effect = RuntimeError("rocklet unavailable")
    set_e2b_proxy_service(proxy_service)
    monkeypatch.setattr("rock.sandbox.utils.timeout.time.time", lambda: 1767222000)

    app = FastAPI()
    app.include_router(e2b_proxy_router)
    return app, meta_store


async def _seed_sandbox(meta_store: SandboxMetaStore) -> None:
    await meta_store.create(
        "sandbox-123",
        {
            "sandbox_id": "sandbox-123",
            "host_ip": "10.0.1.23",
            "image": "linux-dind",
            "metadata": {
                "ap-job-id": "job-123",
                "ap-template": "swe-bench",
                "e2b.agents.kruise.io/return-sandbox-ip": "true",
            },
            "state": State.RUNNING,
            "cpus": 4,
            "memory": "8g",
            "disk": "20g",
            "create_time": "2026-01-01T06:59:00+08:00",
            "start_time": "2026-01-01T07:00:00+08:00",
        },
        timeout_info={"auto_clear_time": "60", "expire_time": "1767225600"},
    )


@pytest.mark.asyncio
async def test_get_sandbox_returns_e2b_detail_without_requiring_headers(e2b_proxy_app):
    app, meta_store = e2b_proxy_app
    await _seed_sandbox(meta_store)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/sandboxes/sandbox-123")

    assert response.status_code == 200
    assert response.json() == {
        "sandboxID": "sandbox-123",
        "metadata": {
            "ap-job-id": "job-123",
            "ap-template": "swe-bench",
            "e2b.agents.kruise.io/return-sandbox-ip": "true",
            "e2b.agents.kruise.io/sandbox-ip": "10.0.1.23",
        },
        "state": "running",
        "clientID": "rock",
        "templateID": "linux-dind",
        "envdVersion": "0.1.0",
        "cpuCount": 4,
        "memoryMB": 8192,
        "diskSizeMB": 20480,
        "startedAt": "2026-01-01T07:00:00+08:00",
        "endAt": "2026-01-01T08:00:00+08:00",
    }


@pytest.mark.asyncio
async def test_get_sandbox_maps_archived_to_paused(e2b_proxy_app):
    app, meta_store = e2b_proxy_app
    await _seed_sandbox(meta_store)
    await meta_store.archive(
        "sandbox-123",
        {
            "state": State.ARCHIVED,
            "archive_time": "2026-01-01T09:00:00+08:00",
        },
    )

    assert await meta_store.get_timeout("sandbox-123") is None

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/sandboxes/sandbox-123")

    assert response.status_code == 200
    assert response.json()["state"] == "paused"
    assert response.json()["endAt"] == "2026-01-01T09:00:00+08:00"
