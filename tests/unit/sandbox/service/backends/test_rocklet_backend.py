from unittest.mock import AsyncMock

import httpx
import pytest

from rock.admin.proto.request import SandboxCommand, SandboxReadFileRequest, SandboxWriteFileRequest
from rock.deployments.constants import Port
from rock.sandbox.service.backends.rocklet import RockletBackend
from rock.utils import EAGLE_EYE_TRACE_ID, trace_id_ctx_var


def _response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(status_code, json=payload, request=httpx.Request("POST", "http://rocklet"))


def _status() -> dict:
    return {"host_ip": "10.0.0.8", "port_mapping": {Port.PROXY.value: 30123}}


@pytest.fixture
def rpc_client() -> AsyncMock:
    return AsyncMock()


@pytest.mark.asyncio
async def test_execute_preserves_rocklet_request_contract(rpc_client):
    rpc_client.request.return_value = _response(200, {"stdout": "ok\n", "stderr": "", "exit_code": 0})
    backend = RockletBackend(rpc_client)
    token = trace_id_ctx_var.set("trace-1")
    try:
        result = await backend.execute("sbx-1", _status(), SandboxCommand(command=["echo", "ok"], sandbox_id="sbx-1"))
    finally:
        trace_id_ctx_var.reset(token)

    assert result.exit_code == 0
    rpc_client.request.assert_awaited_once_with(
        method="POST",
        url="http://10.0.0.8:30123/execute",
        headers={"sandbox_id": "sbx-1", EAGLE_EYE_TRACE_ID: "trace-1"},
        json={
            "session_type": "bash",
            "command": ["echo", "ok"],
            "timeout": 1200,
            "env": None,
            "cwd": None,
            "shell": False,
            "check": False,
            "error_msg": "",
            "sandbox_id": "sbx-1",
        },
        data=None,
        files=None,
    )


@pytest.mark.asyncio
async def test_read_and_write_preserve_json_contract(rpc_client):
    rpc_client.request.side_effect = [
        _response(200, {"content": "hello"}),
        _response(200, {"success": True, "message": ""}),
    ]
    backend = RockletBackend(rpc_client)

    read = await backend.read_file("sbx-1", _status(), SandboxReadFileRequest(path="/tmp/a", sandbox_id="sbx-1"))
    written = await backend.write_file(
        "sbx-1",
        _status(),
        SandboxWriteFileRequest(path="/tmp/a", content="hello", sandbox_id="sbx-1"),
    )

    assert read.content == "hello"
    assert written.success is True
    assert rpc_client.request.await_args_list[0].kwargs["url"].endswith("/read_file")
    assert rpc_client.request.await_args_list[1].kwargs["url"].endswith("/write_file")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "payload", "expected"),
    [
        (511, {"rockletexception": {"message": "boom"}}, {"exit_code": -1, "failure_reason": "boom"}),
        (504, {"detail": "timeout"}, {"exit_code": -1, "failure_reason": "timeout"}),
    ],
)
async def test_special_error_responses_are_preserved(rpc_client, status_code, payload, expected):
    rpc_client.request.return_value = _response(status_code, payload)
    backend = RockletBackend(rpc_client)

    result = await backend.request("sbx-1", _status(), "execute", json_data={}, method="POST")

    assert result == expected


@pytest.mark.asyncio
async def test_network_error_is_translated_to_existing_message(rpc_client):
    request = httpx.Request("POST", "http://10.0.0.8:30123/execute")
    rpc_client.request.side_effect = httpx.ConnectError("offline", request=request)
    backend = RockletBackend(rpc_client)

    with pytest.raises(Exception, match="Service unavailable: Upstream server is not reachable"):
        await backend.request("sbx-1", _status(), "execute", json_data={}, method="POST")
