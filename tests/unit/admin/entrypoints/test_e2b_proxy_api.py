from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from rock.actions.sandbox.response import State
from rock.admin.entrypoints.e2b_proxy_api import e2b_proxy_router, set_e2b_proxy_service
from rock.admin.service.e2b_proxy_service import E2BProxyService
from rock.sandbox.sandbox_meta_store import SandboxMetaStore
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService
from rock.sdk.common.exceptions import SandboxNotFoundRockError


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
    set_e2b_proxy_service(E2BProxyService(sandbox_service=proxy_service, meta_store=meta_store))
    monkeypatch.setattr("rock.sandbox.utils.timeout.time.time", lambda: 1767222000)

    app = FastAPI()
    app.include_router(e2b_proxy_router)
    return app, meta_store


@pytest.fixture
def e2b_list_app():
    meta_store = MagicMock()
    meta_store.list_by_metadata = AsyncMock(
        return_value=[
            {
                "sandbox_id": "sandbox-running",
                "state": State.RUNNING,
                "host_ip": "10.0.1.23",
                "labels": {
                    "ap-job-id": "job-123",
                    "team": "red",
                    "extra": "preserved",
                    "e2b.agents.kruise.io/sandbox-ip": "192.0.2.1",
                },
            },
            {
                "sandbox_id": "sandbox-archived",
                "state": State.ARCHIVED,
                "host_ip": "10.0.1.24",
                "labels": {"ap-job-id": "job-123", "team": "red"},
            },
            {
                "sandbox_id": "sandbox-stopped",
                "state": State.STOPPED,
                "labels": {"ap-job-id": "job-123", "team": "red"},
            },
            {
                "sandbox_id": "sandbox-deleted",
                "state": State.DELETED,
                "labels": {"ap-job-id": "job-123", "team": "red"},
            },
            {
                "sandbox_id": "sandbox-pending",
                "state": State.PENDING,
                "labels": {"ap-job-id": "job-123", "team": "red"},
            },
        ]
    )
    set_e2b_proxy_service(E2BProxyService(sandbox_service=MagicMock(), meta_store=meta_store))

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
        "clientID": "ap-sandbox",
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


@pytest.mark.asyncio
async def test_get_sandbox_maps_typed_not_found_to_404():
    proxy_service = MagicMock()
    proxy_service.get_status = AsyncMock(side_effect=SandboxNotFoundRockError("provider-specific message"))
    set_e2b_proxy_service(E2BProxyService(sandbox_service=proxy_service, meta_store=MagicMock()))

    app = FastAPI()
    app.include_router(e2b_proxy_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/sandboxes/missing")

    assert response.status_code == 404
    assert response.json() == {"code": 404, "message": "provider-specific message"}


@pytest.mark.asyncio
async def test_list_sandboxes_returns_only_e2b_states_without_headers(e2b_list_app):
    app, meta_store = e2b_list_app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/v2/sandboxes",
            params={"metadata": "ap-job-id=job-123&team=red"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == [
        {
            "sandboxID": "sandbox-running",
            "metadata": {
                "ap-job-id": "job-123",
                "team": "red",
                "extra": "preserved",
                "e2b.agents.kruise.io/sandbox-ip": "10.0.1.23",
            },
            "state": "running",
        },
        {
            "sandboxID": "sandbox-archived",
            "metadata": {
                "ap-job-id": "job-123",
                "team": "red",
                "e2b.agents.kruise.io/sandbox-ip": "10.0.1.24",
            },
            "state": "paused",
        },
    ]
    meta_store.list_by_metadata.assert_awaited_once_with({"ap-job-id": "job-123", "team": "red"})


@pytest.mark.asyncio
async def test_list_sandboxes_accepts_upstream_colon_metadata_filter(e2b_list_app):
    app, meta_store = e2b_list_app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v2/sandboxes", params={"metadata": "ap-job-id:job-123"})

    assert response.status_code == 200
    meta_store.list_by_metadata.assert_awaited_once_with({"ap-job-id": "job-123"})


@pytest.mark.asyncio
async def test_list_sandboxes_preserves_equals_sign_in_upstream_metadata_value(e2b_list_app):
    app, meta_store = e2b_list_app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v2/sandboxes", params={"metadata": "ap-job-id:job=123"})

    assert response.status_code == 200
    meta_store.list_by_metadata.assert_awaited_once_with({"ap-job-id": "job=123"})


@pytest.mark.asyncio
async def test_list_sandboxes_accepts_upstream_comma_separated_metadata_filters(e2b_list_app):
    app, meta_store = e2b_list_app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/v2/sandboxes",
            params={"metadata": "ap-job-id:job-123,team:red"},
        )

    assert response.status_code == 200
    meta_store.list_by_metadata.assert_awaited_once_with({"ap-job-id": "job-123", "team": "red"})


@pytest.mark.asyncio
async def test_list_sandboxes_decodes_url_encoded_upstream_metadata(e2b_list_app):
    app, meta_store = e2b_list_app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/v2/sandboxes",
            params={"metadata": "ap%2Fjob-id:job%3A123,team:red%20blue"},
        )

    assert response.status_code == 200
    meta_store.list_by_metadata.assert_awaited_once_with({"ap/job-id": "job:123", "team": "red blue"})


@pytest.mark.asyncio
async def test_list_sandboxes_returns_200_with_empty_array_when_nothing_matches(e2b_list_app):
    app, meta_store = e2b_list_app
    meta_store.list_by_metadata.return_value = []

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v2/sandboxes", params={"metadata": "ap-job-id=missing"})

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_list_sandboxes_decodes_url_encoded_metadata_keys_and_values(e2b_list_app):
    app, meta_store = e2b_list_app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/v2/sandboxes",
            params={"metadata": "ap%2Fjob-id=job%3A123&team=red%20blue"},
        )

    assert response.status_code == 200
    meta_store.list_by_metadata.assert_awaited_once_with({"ap/job-id": "job:123", "team": "red blue"})


@pytest.mark.parametrize(
    "metadata",
    [
        "ap-job-id",
        "ap-job-id=",
        "=job-123",
        "ap-job-id=job-123&",
        "ap-job-id=job-123&ap-job-id=job-456",
        "ap-job-id:",
        ":job-123",
        "ap-job-id:job-123,",
        "ap-job-id:job-123,ap-job-id:job-456",
        "a:b,c=d",
        "key=%ZZ",
        "key=%2",
        "key=%FF",
        "key:%FF",
    ],
)
@pytest.mark.asyncio
async def test_list_sandboxes_returns_400_for_invalid_metadata(e2b_list_app, metadata):
    app, meta_store = e2b_list_app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v2/sandboxes", params={"metadata": metadata})

    assert response.status_code == 400
    assert response.json()["code"] == 400
    meta_store.list_by_metadata.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_sandboxes_returns_400_when_metadata_is_missing(e2b_list_app):
    app, meta_store = e2b_list_app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v2/sandboxes")

    assert response.status_code == 400
    assert response.json()["code"] == 400
    meta_store.list_by_metadata.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_sandboxes_hides_internal_errors(e2b_list_app):
    app, meta_store = e2b_list_app
    meta_store.list_by_metadata.side_effect = RuntimeError("database password leaked")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v2/sandboxes", params={"metadata": "ap-job-id=job-123"})

    assert response.status_code == 500
    assert response.json() == {"code": 500, "message": "Internal server error"}
