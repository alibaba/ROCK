import os
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.datastructures import Headers

from rock.deployments.constants import Port


@pytest.mark.asyncio
async def test_service_has_two_distinct_httpx_clients(sandbox_proxy_service):
    svc = sandbox_proxy_service
    assert isinstance(svc._rpc_client, httpx.AsyncClient)
    assert isinstance(svc._proxy_client, httpx.AsyncClient)
    assert svc._rpc_client is not svc._proxy_client


@pytest.mark.asyncio
async def test_metrics_monitor_has_worker_pid_tag(sandbox_proxy_service):
    tags = sandbox_proxy_service.metrics_monitor.user_defined_tags
    assert tags.get("worker_pid") == str(os.getpid())


@pytest.mark.asyncio
async def test_aclose_closes_both_clients(sandbox_proxy_service):
    svc = sandbox_proxy_service
    await svc.aclose()
    assert svc._rpc_client.is_closed
    assert svc._proxy_client.is_closed


def _fake_status():
    return {"host_ip": "1.2.3.4", "port_mapping": {Port.SERVER.value: 18080}}


@pytest.mark.asyncio
async def test_http_proxy_reuses_proxy_client_without_closing(sandbox_proxy_service, monkeypatch):
    svc = sandbox_proxy_service

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    svc._proxy_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(svc, "_update_expire_time", AsyncMock())
    monkeypatch.setattr(svc, "get_service_status", AsyncMock(return_value=[_fake_status()]))

    resp = await svc.http_proxy(
        sandbox_id="sb1",
        target_path="hello",
        body=None,
        headers=Headers({}),
        method="GET",
    )

    assert resp.status_code == 200
    # shared client must remain open for reuse
    assert not svc._proxy_client.is_closed


@pytest.mark.asyncio
async def test_host_proxy_reuses_proxy_client_without_closing(sandbox_proxy_service):
    svc = sandbox_proxy_service

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"pong": True})

    svc._proxy_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    resp = await svc.host_proxy(
        host_ip="10.0.0.1",
        target_path="ping",
        body={"a": 1},
        headers=Headers({}),
    )

    assert resp.status_code == 200
    assert not svc._proxy_client.is_closed
