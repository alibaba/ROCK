"""Unit tests for SandboxManager.delete.

Avoids ray / docker dependencies by patching out the BaseManager scheduler
setup and stubbing the meta_store / operator.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.actions.sandbox.response import State
from rock.common.constants import DeleteReason
from rock.config import RockConfig, SandboxConfig
from rock.sandbox.sandbox_manager import SandboxManager
from rock.sdk.common.exceptions import BadRequestRockError


@pytest.fixture
def rock_config_min():
    cfg = RockConfig()
    cfg.sandbox_config = SandboxConfig()
    return cfg


@pytest.fixture
def manager(rock_config_min):
    operator = AsyncMock()
    meta_store = AsyncMock()
    meta_store.get = AsyncMock(return_value=None)
    # Patch BaseManager scheduler setup so tests don't spawn APScheduler.
    with patch("rock.sandbox.base_manager.BaseManager._setup_scheduler"):
        m = SandboxManager(
            rock_config=rock_config_min,
            meta_store=meta_store,
            ray_namespace="test",
            ray_service=MagicMock(),
            enable_runtime_auto_clear=False,
            operator=operator,
        )
    return m


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_unknown_sandbox_is_noop(self, manager):
        manager._meta_store.get = AsyncMock(return_value=None)
        await manager.delete("sb-unknown")
        manager._meta_store.archive.assert_not_called()
        manager._operator.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_from_pending_raises_400(self, manager):
        manager._meta_store.get = AsyncMock(
            return_value={"sandbox_id": "sb-1", "state": State.PENDING, "host_ip": "1.2.3.4"}
        )
        with pytest.raises(BadRequestRockError):
            await manager.delete("sb-1")
        manager._operator.delete.assert_not_called()
        manager._meta_store.archive.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_from_running_raises_400(self, manager):
        manager._meta_store.get = AsyncMock(
            return_value={"sandbox_id": "sb-1", "state": State.RUNNING, "host_ip": "1.2.3.4"}
        )
        with pytest.raises(BadRequestRockError):
            await manager.delete("sb-1")
        manager._operator.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_from_stopped_archives_with_deleted_state(self, manager):
        manager._meta_store.get = AsyncMock(
            return_value={
                "sandbox_id": "sb-1",
                "state": State.STOPPED,
                "host_ip": "1.2.3.4",
                "spec": {"container_name": "sb-1", "image": "python:3.11", "memory": "2g", "cpus": 1},
            }
        )
        await manager.delete("sb-1")
        manager._operator.delete.assert_awaited_once()
        args, kwargs = manager._operator.delete.call_args
        assert args[0].container_name == "sb-1"
        assert kwargs.get("host_ip") == "1.2.3.4"
        manager._meta_store.archive.assert_awaited_once()
        info = manager._meta_store.archive.call_args[0][1]
        assert info["state"] == State.DELETED
        assert info["delete_time"]

    @pytest.mark.asyncio
    async def test_delete_already_deleted_is_noop(self, manager):
        manager._meta_store.get = AsyncMock(
            return_value={"sandbox_id": "sb-1", "state": State.DELETED, "host_ip": "1.2.3.4"}
        )
        await manager.delete("sb-1")
        manager._operator.delete.assert_not_called()
        manager._meta_store.archive.assert_not_called()

    @pytest.mark.asyncio
    async def test_operator_delete_failure_still_archives(self, manager):
        manager._meta_store.get = AsyncMock(
            return_value={
                "sandbox_id": "sb-1",
                "state": State.STOPPED,
                "host_ip": "1.2.3.4",
                "spec": {"container_name": "sb-1", "image": "python:3.11", "memory": "2g", "cpus": 1},
            }
        )
        manager._operator.delete = AsyncMock(side_effect=RuntimeError("worker unreachable"))
        await manager.delete("sb-1")
        manager._meta_store.archive.assert_awaited_once()
        info = manager._meta_store.archive.call_args[0][1]
        assert info["state"] == State.DELETED

    @pytest.mark.asyncio
    async def test_delete_propagates_reason(self, manager):
        manager._meta_store.get = AsyncMock(
            return_value={
                "sandbox_id": "sb-1",
                "state": State.STOPPED,
                "host_ip": "1.2.3.4",
                "spec": {"container_name": "sb-1", "image": "python:3.11", "memory": "2g", "cpus": 1},
            }
        )
        await manager.delete("sb-1", reason=DeleteReason.EXPIRED)
        # No public assertion target — reason is logged. Just ensure it doesn't raise
        # and archive happened.
        manager._meta_store.archive.assert_awaited_once()


class TestStopCascadeDelete:
    """`docker run --rm` sandboxes collapse STOPPED → DELETED in one stop call,
    so users don't observe a STOPPED row that auto-delete would reap later."""

    @pytest.mark.asyncio
    async def test_stop_running_with_remove_container_cascades_to_deleted(self, manager):
        redis_info = {
            "sandbox_id": "sb-1",
            "state": State.RUNNING,
            "host_ip": "1.2.3.4",
            "start_time": "2026-05-28T00:00:00+00:00",
        }
        db_info = {
            **redis_info,
            "state": State.STOPPED,
            "spec": {
                "container_name": "sb-1",
                "image": "python:3.11",
                "memory": "2g",
                "cpus": 1,
                "remove_container": True,
            },
        }
        # Call order: (1) @monitor decorator reads user_info, (2) _get_current_statemachine
        # reads Redis (no spec), (3) cascade reads DB fallback (has spec).
        manager._meta_store.get = AsyncMock(side_effect=[redis_info, redis_info, db_info])
        await manager.stop("sb-1")
        manager._operator.stop.assert_awaited_once()
        manager._operator.delete.assert_not_called()
        # on_stop archives with STOPPED, on_delete archives with DELETED (but
        # skips operator.delete because IMMEDIATE means --rm already removed it).
        assert manager._meta_store.archive.await_count == 2
        last_info = manager._meta_store.archive.call_args[0][1]
        assert last_info["state"] == State.DELETED
        assert last_info["delete_time"]

    @pytest.mark.asyncio
    async def test_stop_running_without_remove_container_stays_stopped(self, manager):
        redis_info = {
            "sandbox_id": "sb-1",
            "state": State.RUNNING,
            "host_ip": "1.2.3.4",
            "start_time": "2026-05-28T00:00:00+00:00",
        }
        db_info = {
            **redis_info,
            "state": State.STOPPED,
            "spec": {
                "container_name": "sb-1",
                "image": "python:3.11",
                "memory": "2g",
                "cpus": 1,
                "remove_container": False,
            },
        }
        manager._meta_store.get = AsyncMock(side_effect=[redis_info, redis_info, db_info])
        await manager.stop("sb-1")
        manager._operator.stop.assert_awaited_once()
        manager._operator.delete.assert_not_called()
        manager._meta_store.archive.assert_awaited_once()
        info = manager._meta_store.archive.call_args[0][1]
        assert info["state"] == State.STOPPED

    @pytest.mark.asyncio
    async def test_stop_noop_on_already_stopped_does_not_cascade(self, manager):
        # A redundant stop on an already-STOPPED sandbox must stay idempotent
        # even if remove_container=True — auto-delete still owns this row's
        # eventual STOPPED → DELETED transition.
        manager._meta_store.get = AsyncMock(
            return_value={
                "sandbox_id": "sb-1",
                "state": State.STOPPED,
                "host_ip": "1.2.3.4",
                "spec": {"container_name": "sb-1", "remove_container": True},
            }
        )
        await manager.stop("sb-1")
        manager._operator.stop.assert_not_called()
        manager._operator.delete.assert_not_called()
