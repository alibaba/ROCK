"""Unit tests for the missing-timeout self-heal path.

Covers ``SandboxManager._rebuild_missing_timeout`` disambiguation branches and
the ``_auto_stop_expired`` loop routing (expire / skip / reseed). Ray and docker
are avoided by patching the BaseManager scheduler and stubbing the meta_store.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock import env_vars
from rock.actions.sandbox.response import State
from rock.admin.metrics.constants import MetricsConstants
from rock.common.constants import StopReason
from rock.config import RockConfig, SandboxConfig
from rock.sandbox.sandbox_manager import SandboxManager

TEST_AES_ENCRYPT_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def _timeout_info(expire_time: int, auto_clear_minutes: int = 30) -> dict[str, str]:
    return {
        env_vars.ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY: str(auto_clear_minutes),
        env_vars.ROCK_SANDBOX_EXPIRE_TIME_KEY: str(expire_time),
    }


def _async_iter(items):
    """Return a callable that yields *items* as an async generator (for iter_* stubs)."""

    async def _gen(*_args, **_kwargs):
        for item in items:
            yield item

    return _gen


@pytest.fixture
def rock_config_min():
    cfg = RockConfig()
    cfg.sandbox_config = SandboxConfig()
    cfg.aes_encrypt_key = TEST_AES_ENCRYPT_KEY
    return cfg


@pytest.fixture
def manager(rock_config_min):
    operator = AsyncMock()
    meta_store = AsyncMock()
    with patch("rock.sandbox.base_manager.BaseManager._setup_scheduler"):
        m = SandboxManager(
            rock_config=rock_config_min,
            meta_store=meta_store,
            ray_namespace="test",
            ray_service=MagicMock(),
            enable_runtime_auto_clear=False,
            operator=operator,
        )
    # Assert metric emission independent of env skip logic.
    m.metrics_monitor = MagicMock()
    return m


class TestRebuildMissingTimeout:
    @pytest.mark.asyncio
    async def test_active_state_with_valid_spec_reseeds(self, manager):
        manager._meta_store.get = AsyncMock(
            return_value={"sandbox_id": "sb-1", "state": State.RUNNING}
        )
        info = {"sandbox_id": "sb-1", "spec": {"auto_clear_time_minutes": 45}}

        await manager._rebuild_missing_timeout(info)

        manager._meta_store.update_timeout.assert_awaited_once()
        called_id, timeout_info = manager._meta_store.update_timeout.await_args.args
        assert called_id == "sb-1"
        assert timeout_info[env_vars.ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY] == "45"
        manager.metrics_monitor.record_counter_by_name.assert_called_once_with(
            MetricsConstants.SANDBOX_TIMEOUT_KEY_MISSING, 1, {"action": "rebuilt"}
        )

    @pytest.mark.asyncio
    async def test_terminal_state_skips_rebuild(self, manager):
        # Concurrent stop already set a terminal DB state — must not resurrect.
        manager._meta_store.get = AsyncMock(
            return_value={"sandbox_id": "sb-1", "state": State.STOPPED}
        )
        info = {"sandbox_id": "sb-1", "spec": {"auto_clear_time_minutes": 30}}

        await manager._rebuild_missing_timeout(info)

        manager._meta_store.update_timeout.assert_not_awaited()
        manager.metrics_monitor.record_counter_by_name.assert_called_once_with(
            MetricsConstants.SANDBOX_TIMEOUT_KEY_MISSING, 1, {"action": "skipped_stopped"}
        )

    @pytest.mark.asyncio
    async def test_missing_record_skips_rebuild(self, manager):
        manager._meta_store.get = AsyncMock(return_value=None)
        info = {"sandbox_id": "sb-1", "spec": {"auto_clear_time_minutes": 30}}

        await manager._rebuild_missing_timeout(info)

        manager._meta_store.update_timeout.assert_not_awaited()
        manager.metrics_monitor.record_counter_by_name.assert_called_once_with(
            MetricsConstants.SANDBOX_TIMEOUT_KEY_MISSING, 1, {"action": "skipped_stopped"}
        )

    @pytest.mark.asyncio
    async def test_invalid_spec_skips_rebuild(self, manager):
        manager._meta_store.get = AsyncMock(
            return_value={"sandbox_id": "sb-1", "state": State.RUNNING}
        )
        info = {"sandbox_id": "sb-1", "spec": {}}  # no auto_clear_time_minutes

        await manager._rebuild_missing_timeout(info)

        manager._meta_store.update_timeout.assert_not_awaited()
        manager.metrics_monitor.record_counter_by_name.assert_called_once_with(
            MetricsConstants.SANDBOX_TIMEOUT_KEY_MISSING, 1, {"action": "skipped_no_spec"}
        )

    @pytest.mark.asyncio
    async def test_missing_sandbox_id_is_noop(self, manager):
        await manager._rebuild_missing_timeout({"spec": {"auto_clear_time_minutes": 30}})

        manager._meta_store.get.assert_not_awaited()
        manager._meta_store.update_timeout.assert_not_awaited()
        manager.metrics_monitor.record_counter_by_name.assert_not_called()


class TestAutoStopExpired:
    @pytest.mark.asyncio
    async def test_expired_triggers_stop_without_rebuild(self, manager):
        manager._meta_store.iter_alive_sandbox_info = _async_iter([{"sandbox_id": "sb-1"}])
        manager._meta_store.get_timeout = AsyncMock(return_value=_timeout_info(expire_time=0))
        manager._rebuild_missing_timeout = AsyncMock()
        manager.stop = AsyncMock()

        await manager._auto_stop_expired()
        await asyncio.sleep(0)  # let the create_task(stop) run

        manager.stop.assert_awaited_once_with("sb-1", reason=StopReason.EXPIRED)
        manager._rebuild_missing_timeout.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_not_expired_does_nothing(self, manager):
        far_future = 32503680000  # year ~3000
        manager._meta_store.iter_alive_sandbox_info = _async_iter([{"sandbox_id": "sb-1"}])
        manager._meta_store.get_timeout = AsyncMock(return_value=_timeout_info(expire_time=far_future))
        manager._rebuild_missing_timeout = AsyncMock()
        manager.stop = AsyncMock()

        await manager._auto_stop_expired()
        await asyncio.sleep(0)

        manager.stop.assert_not_awaited()
        manager._rebuild_missing_timeout.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_timeout_routes_to_rebuild_not_stop(self, manager):
        info = {"sandbox_id": "sb-1", "spec": {"auto_clear_time_minutes": 30}}
        manager._meta_store.iter_alive_sandbox_info = _async_iter([info])
        manager._meta_store.get_timeout = AsyncMock(return_value=None)
        manager._rebuild_missing_timeout = AsyncMock()
        manager.stop = AsyncMock()

        await manager._auto_stop_expired()
        await asyncio.sleep(0)

        manager._rebuild_missing_timeout.assert_awaited_once_with(info)
        manager.stop.assert_not_awaited()
