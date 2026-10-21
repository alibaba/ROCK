"""Unit tests for E2BService template resolution (raw-mode decision)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.actions.sandbox.response import State
from rock.admin.proto.response import SandboxStatusResponse
from rock.admin.service.e2b_service import E2BService
from rock.admin.service.snapshot_manager import SnapshotConflict
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.operator.remote.constants import EXT_USE_RAW, EXT_USE_RAW_ENABLED
from rock.sdk.common.exceptions import BadRequestRockError, E2BSandboxNotFoundError


def _make_service(ready_template):
    template_table = AsyncMock()
    template_table.get_ready_template.return_value = ready_template
    sandbox_manager = AsyncMock()
    return E2BService(sandbox_manager, template_table), sandbox_manager


class TestE2BServiceStart:
    @pytest.mark.asyncio
    async def test_no_ready_template_switches_to_raw(self):
        service, manager = _make_service(None)
        config = DockerDeploymentConfig(image="my-img:1", template_id="my-img:1")

        await service.start(config)

        passed = manager.start_from_template.call_args[0][0]
        assert passed.extended_params.get(EXT_USE_RAW) == EXT_USE_RAW_ENABLED
        assert passed.template_id is None
        assert passed.image == "my-img:1"

    @pytest.mark.asyncio
    async def test_ready_template_overrides_resources(self):
        ready = {"image": "reg/img:1", "cpu_count": 4, "memory_mb": 8192, "disk_size_mb": 51200}
        service, manager = _make_service(ready)
        config = DockerDeploymentConfig(image="pool-x", template_id="pool-x")

        await service.start(config)

        passed = manager.start_from_template.call_args[0][0]
        assert EXT_USE_RAW not in passed.extended_params
        assert passed.template_id == "pool-x"
        assert passed.image == "reg/img:1"
        assert passed.cpus == 4
        assert passed.memory == "8g"
        assert passed.disk == "50g"

    @pytest.mark.asyncio
    async def test_full_snapshot_defers_geometry_to_canonical_manager(self):
        ready = {
            "snapshot_id": "full-snapshot",
            "kind": "FULL",
            "cpu_count": None,
            "memory_mb": None,
            "disk_size_mb": None,
        }
        service, manager = _make_service(None)
        service.snapshot_service = AsyncMock()
        service.snapshot_service.get_start_snapshot.return_value = ready
        config = DockerDeploymentConfig(image="full-snapshot", template_id="full-snapshot")

        await service.start(config)

        passed = manager.start_from_template.call_args[0][0]
        assert passed.source_snapshot_id == "full-snapshot"
        assert passed.image == "full-snapshot"
        assert passed.cpus == config.cpus
        assert passed.memory == config.memory


class LifecycleManager:
    snapshot_runtime_configured = True

    def __init__(self, state: State):
        self.state = state
        self.calls = []

    async def get_status(self, sandbox_id, *, include_all_states, refresh_timeout):
        self.calls.append(("status", sandbox_id, include_all_states, refresh_timeout))
        return SandboxStatusResponse(sandbox_id=sandbox_id, state=self.state)

    async def stop(self, sandbox_id):
        self.calls.append(("stop", sandbox_id))

    async def restart_async(self, sandbox_id, *, timeout_seconds):
        self.calls.append(("restart", sandbox_id, timeout_seconds))


async def test_pause_delegates_to_standard_stop_without_a_new_manager_operation():
    manager = LifecycleManager(State.RUNNING)
    service = E2BService(manager, MagicMock())
    assert await service.pause("sandbox-a") is True
    assert manager.calls == [
        ("status", "sandbox-a", True, False),
        ("stop", "sandbox-a"),
    ]


@pytest.mark.parametrize("state,resumed", [(State.RUNNING, False), (State.STOPPED, True), (State.PENDING, True)])
async def test_connect_delegates_to_standard_restart_with_requested_timeout(state, resumed):
    manager = LifecycleManager(state)
    service = E2BService(manager, MagicMock())
    assert await service.connect("sandbox-a", timeout=37) is resumed
    assert manager.calls == [
        ("status", "sandbox-a", True, False),
        ("restart", "sandbox-a", 37),
    ]


@pytest.mark.parametrize("action", ["pause", "connect"])
async def test_lifecycle_preserves_missing_sandbox_mapping(action):
    manager = LifecycleManager(State.RUNNING)
    manager.get_status = AsyncMock(side_effect=BadRequestRockError("Sandbox missing not found"))
    service = E2BService(manager, MagicMock())
    with pytest.raises(E2BSandboxNotFoundError):
        await getattr(service, action)("missing")
    assert manager.calls == []


@pytest.mark.parametrize("state", [State.STOPPED, State.ARCHIVED])
async def test_generic_stopped_sandbox_connect_does_not_enable_new_runtime_capability(state):
    manager = LifecycleManager(state)
    manager.snapshot_runtime_configured = False
    service = E2BService(manager, MagicMock())
    with pytest.raises(SnapshotConflict, match="snapshot-backed"):
        await service.connect("sandbox-a")
    assert manager.calls == [("status", "sandbox-a", True, False)]
