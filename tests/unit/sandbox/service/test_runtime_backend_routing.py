from unittest.mock import AsyncMock

import pytest

from rock.actions import CommandResponse
from rock.actions.sandbox.response import State
from rock.admin.proto.request import SandboxCommand
from rock.sandbox.service.backends import OPENSANDBOX_BACKEND, ROCKLET_BACKEND
from rock.sdk.common.exceptions import BadRequestRockError


@pytest.fixture
def backends(sandbox_proxy_service):
    rocklet = AsyncMock()
    opensandbox = AsyncMock()
    rocklet.execute.return_value = CommandResponse(stdout="rocklet", exit_code=0)
    opensandbox.execute.return_value = CommandResponse(stdout="opensandbox", exit_code=0)
    sandbox_proxy_service._backends = {ROCKLET_BACKEND: rocklet, OPENSANDBOX_BACKEND: opensandbox}
    return rocklet, opensandbox


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
