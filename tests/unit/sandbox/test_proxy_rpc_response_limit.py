from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService
from rock.sdk.common.exceptions import BadRequestRockError


def _service(response: httpx.Response, max_bytes: int) -> SandboxProxyService:
    service = SandboxProxyService.__new__(SandboxProxyService)
    service._rpc_client = AsyncMock()
    service._rpc_client.request.return_value = response
    service._max_rpc_response_bytes = max_bytes
    service._api_url = MagicMock(return_value="http://rocklet:8080")
    service._headers = MagicMock(return_value={})
    return service


@pytest.mark.asyncio
async def test_rpc_response_at_limit_is_parsed():
    response = httpx.Response(200, content=b'{"ok":true}')
    service = _service(response, max_bytes=len(response.content))

    result = await service._send_request("sandbox-1", {}, "execute", None, {}, None, "POST")

    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_rpc_response_limit_falls_back_when_init_is_bypassed(monkeypatch):
    monkeypatch.setenv("ROCK_PROXY_MAX_RPC_RESPONSE_BYTES", "1024")
    response = httpx.Response(200, content=b'{"ok":true}')
    service = _service(response, max_bytes=1024)
    del service._max_rpc_response_bytes

    result = await service._send_request("sandbox-1", {}, "execute", None, {}, None, "POST")

    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_rpc_response_over_limit_fails_before_json_decode(monkeypatch):
    response = httpx.Response(200, content=b'{"stdout":"large output"}')
    json_spy = MagicMock(side_effect=AssertionError("response.json() must not be called"))
    monkeypatch.setattr(response, "json", json_spy)
    service = _service(response, max_bytes=len(response.content) - 1)

    with pytest.raises(BadRequestRockError, match=r"RPC response too large:.*response_bytes=.*max_bytes="):
        await service._send_request("sandbox-1", {}, "execute", None, {}, None, "POST")

    json_spy.assert_not_called()


def test_rpc_response_limit_defaults_to_128_mib(monkeypatch):
    from rock import env_vars

    monkeypatch.delenv("ROCK_PROXY_MAX_RPC_RESPONSE_BYTES", raising=False)

    assert env_vars.ROCK_PROXY_MAX_RPC_RESPONSE_BYTES == 128 * 1024 * 1024


def test_rpc_response_limit_uses_environment(monkeypatch):
    from rock import env_vars

    monkeypatch.setenv("ROCK_PROXY_MAX_RPC_RESPONSE_BYTES", "1024")

    assert env_vars.ROCK_PROXY_MAX_RPC_RESPONSE_BYTES == 1024
