"""Tests for SchedulerTaskTable CRUD (single-table: scheduler_task)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.admin.core.schema import SchedulerTaskRecord
from rock.admin.core.scheduler_task_table import Phase, SchedulerTaskTable


def _make_table_with_mock_session():
    table = SchedulerTaskTable.__new__(SchedulerTaskTable)
    table._db = MagicMock()

    mock_session = AsyncMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=mock_session)
    cm.__aexit__ = AsyncMock(return_value=None)

    return table, cm, mock_session


@pytest.mark.asyncio
async def test_insert_tasks():
    table, cm, session = _make_table_with_mock_session()

    record = SchedulerTaskRecord(
        task_id="b" * 32,
        taskset_id="a" * 32,
        task_type="image_cleanup",
        target_workers=["10.0.0.1"],
        creation_timestamp=time.time(),
        phase=Phase.PENDING,
        assigned_pod="pod-x",
    )

    with patch("rock.admin.core.scheduler_task_table.AsyncSession", return_value=cm):
        await table.insert_tasks([record])

    session.add.assert_called_once_with(record)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_tasks_by_group():
    table, cm, session = _make_table_with_mock_session()

    mock_record = SchedulerTaskRecord(
        task_id="b" * 32,
        taskset_id="a" * 32,
        task_type="image_cleanup",
        target_workers=["10.0.0.1"],
        creation_timestamp=time.time(),
        phase=Phase.RUNNING,
        assigned_pod="pod-x",
    )

    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [mock_record]
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    session.execute = AsyncMock(return_value=result_mock)

    with patch("rock.admin.core.scheduler_task_table.AsyncSession", return_value=cm):
        results = await table.get_tasks_by_group("a" * 32)

    assert len(results) == 1
    assert isinstance(results[0], SchedulerTaskRecord)
    assert results[0].taskset_id == "a" * 32


@pytest.mark.asyncio
async def test_update_task():
    table, cm, session = _make_table_with_mock_session()

    row = SchedulerTaskRecord(
        task_id="b" * 32,
        taskset_id="a" * 32,
        task_type="image_cleanup",
        target_workers=["10.0.0.1"],
        creation_timestamp=time.time(),
        phase=Phase.PENDING,
        assigned_pod="pod-x",
    )
    session.get = AsyncMock(return_value=row)

    with patch("rock.admin.core.scheduler_task_table.AsyncSession", return_value=cm):
        ok = await table.update_task("b" * 32, phase=Phase.RUNNING, start_time=123.0)

    assert ok is True
    assert row.phase == Phase.RUNNING
    assert row.start_time == 123.0


@pytest.mark.asyncio
async def test_update_task_not_found():
    table, cm, session = _make_table_with_mock_session()
    session.get = AsyncMock(return_value=None)

    with patch("rock.admin.core.scheduler_task_table.AsyncSession", return_value=cm):
        ok = await table.update_task("nonexistent", phase=Phase.RUNNING)

    assert ok is False


@pytest.mark.asyncio
async def test_has_recent_task_true():
    table, cm, session = _make_table_with_mock_session()

    result_mock = MagicMock()
    result_mock.first.return_value = ("some_id",)
    session.execute = AsyncMock(return_value=result_mock)

    with patch("rock.admin.core.scheduler_task_table.AsyncSession", return_value=cm):
        found = await table.has_recent_task("image_cleanup", time.time() - 60)

    assert found is True


@pytest.mark.asyncio
async def test_has_recent_task_false():
    table, cm, session = _make_table_with_mock_session()

    result_mock = MagicMock()
    result_mock.first.return_value = None
    session.execute = AsyncMock(return_value=result_mock)

    with patch("rock.admin.core.scheduler_task_table.AsyncSession", return_value=cm):
        found = await table.has_recent_task("image_cleanup", time.time() - 60)

    assert found is False
