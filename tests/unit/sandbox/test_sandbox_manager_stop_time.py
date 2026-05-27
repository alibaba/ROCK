"""Regression tests for SandboxManager.stop() stop_time behavior.

Bug: prior to this fix, ``stop_time`` was written inside an
``if sandbox_info.get("start_time")`` guard, so sandboxes that failed
before sandbox_actor wrote ``start_time`` (e.g. image pull / docker run
failure) ended up with ``state=stopped`` but ``stop_time=NULL`` in
``sandbox_record``. SandboxLogArchiveTask then couldn't age them and
their log dirs were never cleaned up.

Fix: always write stop_time; keep billing gated on start_time.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.actions.sandbox.response import State
from rock.common.constants import StopReason
from rock.sandbox.sandbox_manager import SandboxManager


def _bare_manager() -> SandboxManager:
    """Construct a SandboxManager without going through __init__ — the real
    __init__ wires Ray / Redis / AES which aren't needed for stop() logic.
    """
    mgr = SandboxManager.__new__(SandboxManager)
    mgr._meta_store = MagicMock()
    mgr._meta_store.get = AsyncMock()
    mgr._meta_store.archive = AsyncMock()
    mgr._operator = MagicMock()
    mgr._operator.stop = AsyncMock()
    return mgr


@pytest.mark.asyncio
async def test_stop_time_written_when_start_time_present():
    mgr = _bare_manager()
    mgr._meta_store.get.return_value = {"start_time": "2026-05-27T10:00:00+00:00"}

    await mgr.stop("sb-1", reason=StopReason.MANUAL)

    archived = mgr._meta_store.archive.await_args.args[1]
    assert archived["state"] == State.STOPPED
    assert archived.get("stop_time"), "stop_time must be set when start_time present"


@pytest.mark.asyncio
async def test_stop_time_written_even_when_start_time_absent():
    """REGRESSION: sandboxes that never started (no start_time) must still
    get stop_time, otherwise SandboxLogArchiveTask can't age them."""
    mgr = _bare_manager()
    mgr._meta_store.get.return_value = {}  # no start_time — start failed early

    await mgr.stop("sb-start-failed", reason=StopReason.MANUAL)

    archived = mgr._meta_store.archive.await_args.args[1]
    assert archived["state"] == State.STOPPED
    assert archived.get("stop_time"), (
        "stop_time must be set even when start_time is absent "
        "(start-failed sandboxes still need archival ageing)"
    )


@pytest.mark.asyncio
async def test_stop_time_written_when_meta_store_returns_none():
    """meta_store.get may return None for sandboxes wiped from Redis;
    stop() should still archive a row with state=stopped + stop_time."""
    mgr = _bare_manager()
    mgr._meta_store.get.return_value = None

    await mgr.stop("sb-missing", reason=StopReason.MANUAL)

    archived = mgr._meta_store.archive.await_args.args[1]
    assert archived["state"] == State.STOPPED
    assert archived.get("stop_time")


@pytest.mark.asyncio
async def test_stop_time_written_when_operator_stop_raises():
    """Operator may raise ValueError when actor is gone (already cleaned up);
    the early-return path must still archive stop_time."""
    mgr = _bare_manager()
    mgr._meta_store.get.return_value = {}
    mgr._operator.stop.side_effect = ValueError("actor not found")

    await mgr.stop("sb-gone", reason=StopReason.MANUAL)

    archived = mgr._meta_store.archive.await_args.args[1]
    assert archived["state"] == State.STOPPED
    assert archived.get("stop_time")
