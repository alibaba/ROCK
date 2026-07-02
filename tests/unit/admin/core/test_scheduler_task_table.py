"""Tests for SchedulerTaskTable CRUD (single-table: scheduler_task).

Runs against a real in-memory SQLite DatabaseProvider (sync engine + DB thread
pool), so it exercises the actual sync execution path rather than mocking it.
"""

from __future__ import annotations

import time

import pytest

from rock.admin.core.scheduler_task_table import Phase, SchedulerTaskTable
from rock.admin.core.schema import SchedulerTaskRecord


@pytest.fixture
async def scheduler_table(db_provider):
    return SchedulerTaskTable(db_provider)


def _record(*, task_id: str = "b" * 32, taskset_id: str = "a" * 32, phase: Phase = Phase.PENDING, **overrides):
    fields = {
        "task_id": task_id,
        "taskset_id": taskset_id,
        "task_type": "image_cleanup",
        "target_workers": ["10.0.0.1"],
        "creation_timestamp": time.time(),
        "phase": phase,
        "assigned_pod": "pod-x",
    }
    fields.update(overrides)
    return SchedulerTaskRecord(**fields)


async def test_insert_tasks(scheduler_table):
    await scheduler_table.insert_tasks([_record()])

    rows = await scheduler_table.get_tasks_by_group("a" * 32)
    assert len(rows) == 1
    assert rows[0].task_id == "b" * 32


async def test_get_tasks_by_group(scheduler_table):
    await scheduler_table.insert_tasks([_record(phase=Phase.RUNNING)])

    results = await scheduler_table.get_tasks_by_group("a" * 32)
    assert len(results) == 1
    assert isinstance(results[0], SchedulerTaskRecord)
    assert results[0].taskset_id == "a" * 32


async def test_update_task(scheduler_table):
    await scheduler_table.insert_tasks([_record()])

    ok = await scheduler_table.update_task("b" * 32, phase=Phase.RUNNING, start_time=123.0)
    assert ok is True

    rows = await scheduler_table.get_tasks_by_group("a" * 32)
    assert rows[0].phase == Phase.RUNNING
    assert rows[0].start_time == 123.0


async def test_update_task_not_found(scheduler_table):
    ok = await scheduler_table.update_task("nonexistent", phase=Phase.RUNNING)
    assert ok is False


async def test_has_recent_task_true(scheduler_table):
    await scheduler_table.insert_tasks([_record(creation_timestamp=time.time())])

    found = await scheduler_table.has_recent_task("image_cleanup", time.time() - 60)
    assert found is True


async def test_has_recent_task_false(scheduler_table):
    found = await scheduler_table.has_recent_task("image_cleanup", time.time() - 60)
    assert found is False
