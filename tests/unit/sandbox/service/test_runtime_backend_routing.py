from unittest.mock import AsyncMock

import pytest

from rock.actions import CommandResponse
from rock.actions.sandbox.response import State
from rock.admin.proto.request import SandboxCommand, SandboxCreateBashSessionRequest
from rock.sandbox.service.backends import OPENSANDBOX_BACKEND, ROCKLET_BACKEND
from rock.sdk.common.exceptions import BadRequestRockError


@pytest.fixture
def backends(sandbox_proxy_service):
    original_operator_type = sandbox_proxy_service._rock_config.runtime.operator_type
    rocklet = AsyncMock()
    opensandbox = AsyncMock()
    rocklet.execute.return_value = CommandResponse(stdout="rocklet", exit_code=0)
    opensandbox.execute.return_value = CommandResponse(stdout="opensandbox", exit_code=0)
    sandbox_proxy_service._backends = {ROCKLET_BACKEND: rocklet, OPENSANDBOX_BACKEND: opensandbox}
    yield rocklet, opensandbox
    sandbox_proxy_service._rock_config.runtime.operator_type = original_operator_type


def _info(*, backend=None, state=State.RUNNING, opensandbox_id=None):
    extended_params = {}
    if backend is not None:
        extended_params["backend"] = backend
    if opensandbox_id is not None:
        extended_params["opensandbox_id"] = opensandbox_id
    return {"sandbox_id": "sbx-1", "state": state, "extended_params": extended_params}


@pytest.mark.asyncio
async def test_explicit_opensandbox_routes_without_host_ip(sandbox_proxy_service, backends):
    rocklet, opensandbox = backends
    sandbox_proxy_service._rock_config.runtime.operator_type = "opensandbox"
    sandbox_proxy_service._meta_store.get = AsyncMock(
        return_value=_info(backend=OPENSANDBOX_BACKEND, opensandbox_id="osb-1")
    )

    result = await sandbox_proxy_service.execute(SandboxCommand(command="pwd", sandbox_id="sbx-1"))

    assert result.stdout == "opensandbox"
    opensandbox.execute.assert_awaited_once()
    rocklet.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_opensandbox_missing_backend_fails_without_rocklet_call(sandbox_proxy_service, backends):
    rocklet, opensandbox = backends
    sandbox_proxy_service._rock_config.runtime.operator_type = "opensandbox"
    sandbox_proxy_service._meta_store.get = AsyncMock(return_value=_info(opensandbox_id="osb-1"))

    with pytest.raises(BadRequestRockError, match="backend"):
        await sandbox_proxy_service.execute(SandboxCommand(command="pwd", sandbox_id="sbx-1"))

    rocklet.execute.assert_not_awaited()
    opensandbox.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_metadata_operator_conflict_fails_closed(sandbox_proxy_service, backends):
    rocklet, opensandbox = backends
    sandbox_proxy_service._rock_config.runtime.operator_type = "ray"
    sandbox_proxy_service._meta_store.get = AsyncMock(
        return_value=_info(backend=OPENSANDBOX_BACKEND, opensandbox_id="osb-1")
    )

    with pytest.raises(BadRequestRockError, match="conflicts"):
        await sandbox_proxy_service.execute(SandboxCommand(command="pwd", sandbox_id="sbx-1"))

    rocklet.execute.assert_not_awaited()
    opensandbox.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_backend_fails_closed(sandbox_proxy_service, backends):
    rocklet, opensandbox = backends
    sandbox_proxy_service._rock_config.runtime.operator_type = "ray"
    sandbox_proxy_service._meta_store.get = AsyncMock(return_value=_info(backend="unknown"))

    with pytest.raises(BadRequestRockError, match="Unknown"):
        await sandbox_proxy_service.execute(SandboxCommand(command="pwd", sandbox_id="sbx-1"))

    rocklet.execute.assert_not_awaited()
    opensandbox.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_missing_backend_routes_to_rocklet(sandbox_proxy_service, backends):
    rocklet, opensandbox = backends
    sandbox_proxy_service._rock_config.runtime.operator_type = "ray"
    sandbox_proxy_service._meta_store.get = AsyncMock(return_value=_info())

    result = await sandbox_proxy_service.execute(SandboxCommand(command=["pwd"], sandbox_id="sbx-1"))

    assert result.stdout == "rocklet"
    rocklet.execute.assert_awaited_once()
    opensandbox.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [State.PENDING, State.STOPPED, State.DELETED])
async def test_runtime_operations_require_running(sandbox_proxy_service, backends, state):
    rocklet, opensandbox = backends
    sandbox_proxy_service._rock_config.runtime.operator_type = "opensandbox"
    sandbox_proxy_service._meta_store.get = AsyncMock(
        return_value=_info(backend=OPENSANDBOX_BACKEND, state=state, opensandbox_id="osb-1")
    )

    with pytest.raises(BadRequestRockError, match="not running"):
        await sandbox_proxy_service.execute(SandboxCommand(command="pwd", sandbox_id="sbx-1"))

    rocklet.execute.assert_not_awaited()
    opensandbox.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_opensandbox_get_status_does_not_probe_rocklet(sandbox_proxy_service, backends, monkeypatch):
    rocklet, opensandbox = backends
    opensandbox.get_state.return_value = State.RUNNING
    sandbox_proxy_service._rock_config.runtime.operator_type = "opensandbox"
    sandbox_proxy_service._meta_store.get = AsyncMock(
        return_value=_info(backend=OPENSANDBOX_BACKEND, opensandbox_id="osb-1")
    )
    rocklet_probe = AsyncMock()
    monkeypatch.setattr("rock.sandbox.service.sandbox_proxy_service.get_remote_status", rocklet_probe)

    result = await sandbox_proxy_service.get_status("sbx-1")

    assert result.is_alive is True
    assert result.state == State.RUNNING
    assert result.host_ip is None
    assert result.port_mapping is None
    assert result.swe_rex_version is None
    opensandbox.get_state.assert_awaited_once()
    rocklet_probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_opensandbox_is_alive_uses_backend_state(sandbox_proxy_service, backends):
    _, opensandbox = backends
    opensandbox.get_state.return_value = State.PENDING
    sandbox_proxy_service._rock_config.runtime.operator_type = "opensandbox"
    sandbox_proxy_service._meta_store.get = AsyncMock(
        return_value=_info(backend=OPENSANDBOX_BACKEND, state=State.PENDING, opensandbox_id="osb-1")
    )

    result = await sandbox_proxy_service.is_alive("sbx-1")

    assert result.is_alive is False
    opensandbox.get_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_opensandbox_session_is_rejected_before_rocklet_call(sandbox_proxy_service, backends):
    rocklet, opensandbox = backends
    sandbox_proxy_service._rock_config.runtime.operator_type = "opensandbox"
    sandbox_proxy_service._meta_store.get = AsyncMock(
        return_value=_info(backend=OPENSANDBOX_BACKEND, opensandbox_id="osb-1")
    )

    with pytest.raises(BadRequestRockError, match="does not support sessions"):
        await sandbox_proxy_service.create_session(SandboxCreateBashSessionRequest(session="test", sandbox_id="sbx-1"))

    rocklet.assert_not_awaited()
    opensandbox.assert_not_awaited()


@pytest.mark.asyncio
async def test_opensandbox_portforward_is_rejected_before_rocklet_call(sandbox_proxy_service, backends):
    rocklet, opensandbox = backends
    sandbox_proxy_service._rock_config.runtime.operator_type = "opensandbox"
    sandbox_proxy_service._meta_store.get = AsyncMock(
        return_value=_info(backend=OPENSANDBOX_BACKEND, opensandbox_id="osb-1")
    )

    with pytest.raises(BadRequestRockError, match="does not support portforward"):
        await sandbox_proxy_service._require_capability_backend("sbx-1", "portforward")

    rocklet.assert_not_awaited()
    opensandbox.assert_not_awaited()
