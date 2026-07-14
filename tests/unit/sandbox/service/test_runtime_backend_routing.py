from unittest.mock import AsyncMock

import pytest

from rock.actions import CommandResponse
from rock.actions.sandbox.response import State
from rock.admin.proto.request import SandboxCommand, SandboxCreateBashSessionRequest
from rock.sandbox.service.opensandbox_proxy_service import OPENSANDBOX_BACKEND, OpenSandboxProxyService
from rock.sdk.common.exceptions import BadRequestRockError


@pytest.fixture
def opensandbox_proxy_service(sandbox_proxy_service):
    backend = AsyncMock()
    backend.execute.return_value = CommandResponse(stdout="opensandbox", exit_code=0)
    service = OpenSandboxProxyService(
        sandbox_proxy_service._rock_config,
        sandbox_proxy_service._meta_store,
        backend=backend,
    )
    return service, backend


def _info(*, backend=None, state=State.RUNNING, opensandbox_id=None):
    extended_params = {}
    if backend is not None:
        extended_params["backend"] = backend
    if opensandbox_id is not None:
        extended_params["opensandbox_id"] = opensandbox_id
    return {"sandbox_id": "sbx-1", "state": state, "extended_params": extended_params}


@pytest.mark.asyncio
async def test_explicit_opensandbox_routes_without_host_ip(opensandbox_proxy_service):
    service, backend = opensandbox_proxy_service
    service._meta_store.get = AsyncMock(return_value=_info(backend=OPENSANDBOX_BACKEND, opensandbox_id="osb-1"))

    result = await service.execute(SandboxCommand(command="pwd", sandbox_id="sbx-1"))

    assert result.stdout == "opensandbox"
    backend.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_opensandbox_missing_backend_fails_closed(opensandbox_proxy_service):
    service, backend = opensandbox_proxy_service
    service._meta_store.get = AsyncMock(return_value=_info(opensandbox_id="osb-1"))

    with pytest.raises(BadRequestRockError, match="backend"):
        await service.execute(SandboxCommand(command="pwd", sandbox_id="sbx-1"))

    backend.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_metadata_operator_conflict_fails_closed(opensandbox_proxy_service):
    service, backend = opensandbox_proxy_service
    service._meta_store.get = AsyncMock(return_value=_info(backend="rocklet"))

    with pytest.raises(BadRequestRockError, match="conflicts"):
        await service.execute(SandboxCommand(command="pwd", sandbox_id="sbx-1"))

    backend.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_backend_fails_closed(opensandbox_proxy_service):
    service, backend = opensandbox_proxy_service
    service._meta_store.get = AsyncMock(return_value=_info(backend="unknown"))

    with pytest.raises(BadRequestRockError, match="conflicts"):
        await service.execute(SandboxCommand(command="pwd", sandbox_id="sbx-1"))

    backend.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [State.PENDING, State.STOPPED, State.DELETED])
async def test_runtime_operations_require_running(opensandbox_proxy_service, state):
    service, backend = opensandbox_proxy_service
    service._meta_store.get = AsyncMock(
        return_value=_info(backend=OPENSANDBOX_BACKEND, state=state, opensandbox_id="osb-1")
    )

    with pytest.raises(BadRequestRockError, match="not running"):
        await service.execute(SandboxCommand(command="pwd", sandbox_id="sbx-1"))

    backend.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_opensandbox_get_status_does_not_probe_rocklet(opensandbox_proxy_service, monkeypatch):
    service, backend = opensandbox_proxy_service
    backend.get_state.return_value = State.RUNNING
    service._meta_store.get = AsyncMock(return_value=_info(backend=OPENSANDBOX_BACKEND, opensandbox_id="osb-1"))
    rocklet_probe = AsyncMock()
    monkeypatch.setattr("rock.sandbox.service.sandbox_proxy_service.get_remote_status", rocklet_probe)

    result = await service.get_status("sbx-1")

    assert result.is_alive is True
    assert result.state == State.RUNNING
    assert result.host_ip is None
    assert result.port_mapping is None
    assert result.swe_rex_version is None
    backend.get_state.assert_awaited_once()
    rocklet_probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_opensandbox_is_alive_uses_backend_state(opensandbox_proxy_service):
    service, backend = opensandbox_proxy_service
    backend.get_state.return_value = State.PENDING
    service._meta_store.get = AsyncMock(
        return_value=_info(backend=OPENSANDBOX_BACKEND, state=State.PENDING, opensandbox_id="osb-1")
    )

    result = await service.is_alive("sbx-1")

    assert result.is_alive is False
    backend.get_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_opensandbox_session_is_rejected(opensandbox_proxy_service):
    service, backend = opensandbox_proxy_service
    service._meta_store.get = AsyncMock(return_value=_info(backend=OPENSANDBOX_BACKEND, opensandbox_id="osb-1"))

    with pytest.raises(BadRequestRockError, match="does not support sessions"):
        await service.create_session(SandboxCreateBashSessionRequest(session="test", sandbox_id="sbx-1"))

    backend.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_opensandbox_portforward_is_rejected(opensandbox_proxy_service):
    service, backend = opensandbox_proxy_service
    service._meta_store.get = AsyncMock(return_value=_info(backend=OPENSANDBOX_BACKEND, opensandbox_id="osb-1"))

    with pytest.raises(BadRequestRockError, match="does not support portforward"):
        await service.websocket_to_tcp_proxy(None, "sbx-1", 8000)

    backend.execute.assert_not_awaited()
