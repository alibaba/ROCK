"""Tests for OpsJobTable CRUD (DB-backed ops job persistence)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.admin.core.ops_job_table import OpsJobTable


def _make_table_with_mock_session():
    """Return (table, mock_session_cm, mock_session) for assertions."""
    table = OpsJobTable.__new__(OpsJobTable)
    table._db = MagicMock()

    mock_session = AsyncMock()
    # AsyncSession is used as an async context manager
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=mock_session)
    cm.__aexit__ = AsyncMock(return_value=None)

    return table, cm, mock_session


@pytest.mark.asyncio
async def test_insert_calls_add_and_commit():
    table, cm, session = _make_table_with_mock_session()

    with patch("rock.admin.core.ops_job_table.AsyncSession", return_value=cm):
        await table.insert(
            {
                "job_id": "abc",
                "submitted_by": "1.2.3.4",
                "tasks": ["image_cleanup"],
                "worker_ips": ["10.0.0.1"],
                "status": "accepted",
                "submitted_at": time.time(),
                "pod_id": "pod-x",
            }
        )

    session.add.assert_called_once()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_returns_dict_when_found():
    table, cm, session = _make_table_with_mock_session()

    class Row:
        job_id = "abc"
        status = "accepted"

    session.get = AsyncMock(return_value=Row())

    with (
        patch("rock.admin.core.ops_job_table.AsyncSession", return_value=cm),
        patch(
            "rock.admin.core.ops_job_table._row_to_dict",
            return_value={"job_id": "abc", "status": "accepted"},
        ),
    ):
        result = await table.get("abc")

    assert result == {"job_id": "abc", "status": "accepted"}


@pytest.mark.asyncio
async def test_get_returns_none_when_not_found():
    table, cm, session = _make_table_with_mock_session()
    session.get = AsyncMock(return_value=None)

    with patch("rock.admin.core.ops_job_table.AsyncSession", return_value=cm):
        result = await table.get("does-not-exist")

    assert result is None


@pytest.mark.asyncio
async def test_update_status_completed_sets_completed_at():
    table, cm, session = _make_table_with_mock_session()

    class Row:
        status = "running"
        results = None
        error = None
        completed_at = None

    row = Row()
    session.get = AsyncMock(return_value=row)

    with patch("rock.admin.core.ops_job_table.AsyncSession", return_value=cm):
        ok = await table.update_status("abc", "completed", results={"x": 1})

    assert ok is True
    assert row.status == "completed"
    assert row.results == {"x": 1}
    assert row.completed_at is not None
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_status_returns_false_when_not_found():
    table, cm, session = _make_table_with_mock_session()
    session.get = AsyncMock(return_value=None)

    with patch("rock.admin.core.ops_job_table.AsyncSession", return_value=cm):
        ok = await table.update_status("does-not-exist", "completed")

    assert ok is False
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_recent_filters_by_task_type_intersection():
    """Only rows whose `tasks` intersects requested types are returned."""
    table, cm, session = _make_table_with_mock_session()

    class Row:
        def __init__(self, tasks):
            self.tasks = tasks

    rows = [Row(["image_cleanup"]), Row(["build_cache_cleanup"]), Row(["other"])]
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = rows
    session.execute = AsyncMock(return_value=result_mock)

    def fake_row_to_dict(r):
        return {"tasks": r.tasks}

    with (
        patch("rock.admin.core.ops_job_table.AsyncSession", return_value=cm),
        patch("rock.admin.core.ops_job_table._row_to_dict", side_effect=fake_row_to_dict),
    ):
        result = await table.list_recent_by_tasks(["image_cleanup", "build_cache_cleanup"], 0.0)

    # 2 rows match (image_cleanup, build_cache_cleanup); "other" filtered out
    assert len(result) == 2
    assert {tuple(r["tasks"]) for r in result} == {("image_cleanup",), ("build_cache_cleanup",)}


@pytest.mark.asyncio
async def test_list_recent_empty_task_types_returns_empty():
    table, _, _ = _make_table_with_mock_session()
    result = await table.list_recent_by_tasks([], 0.0)
    assert result == []
