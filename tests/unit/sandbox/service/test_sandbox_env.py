from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.admin.proto.request import SandboxCommand, SandboxCreateBashSessionRequest
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService


@pytest.fixture
def service():
    result = SandboxProxyService.__new__(SandboxProxyService)
    result.metrics_monitor = None
    result._meta_store = AsyncMock()
    result._meta_store.get.return_value = {
        "sandbox_id": "sandbox-123",
        "host_ip": "10.0.0.1",
        "env": {"WORKSPACE": "/workspace", "SHARED": "sandbox"},
    }
    result._update_expire_time = AsyncMock()
    result._send_request = AsyncMock()
    return result


@pytest.mark.asyncio
async def test_execute_merges_sandbox_environment_with_request_override(service):
    service._send_request.return_value = {"stdout": "ok", "exit_code": 0}
    command = SandboxCommand(
        sandbox_id="sandbox-123",
        command="pwd",
        env={"SHARED": "request", "COMMAND_ONLY": "value"},
    )

    await service.execute(command)

    payload = service._send_request.await_args.args[4]
    assert payload["env"] == {
        "WORKSPACE": "/workspace",
        "SHARED": "request",
        "COMMAND_ONLY": "value",
    }


@pytest.mark.asyncio
async def test_create_session_merges_sandbox_environment_with_request_override(service):
    service._send_request.return_value = {"output": "ready"}
    request = SandboxCreateBashSessionRequest(
        sandbox_id="sandbox-123",
        session="session-1",
        env={"SHARED": "request", "SESSION_ONLY": "value"},
    )

    await service.create_session(request)

    payload = service._send_request.await_args.args[4]
    assert payload["env"] == {
        "WORKSPACE": "/workspace",
        "SHARED": "request",
        "SESSION_ONLY": "value",
    }


@pytest.mark.asyncio
async def test_send_request_redacts_environment_in_logs(service):
    service._api_url = MagicMock(return_value="http://10.0.0.1:8080")
    service._headers = MagicMock(return_value={})
    service._rpc_client = AsyncMock()
    service._rpc_client.request.return_value = MagicMock(status_code=200)
    service._rpc_client.request.return_value.json.return_value = {"stdout": "ok", "exit_code": 0}
    payload = {"command": "pwd", "env": {"TOKEN": "secret"}}

    with patch("rock.sandbox.service.sandbox_proxy_service.logger.info") as log_info:
        await SandboxProxyService._send_request(
            service,
            "sandbox-123",
            {"host_ip": "10.0.0.1"},
            "execute",
            None,
            payload,
            None,
            "POST",
        )

    log_info.assert_any_call("json_data: %s", {"command": "pwd", "env": "<redacted>"})
