"""Tests for archive-related state machine transitions."""

import datetime
from unittest.mock import AsyncMock

import pytest
from statemachine.exceptions import TransitionNotAllowed

from rock.actions.sandbox.response import State
from rock.config import AutoTransitionConfig
from rock.sandbox.sandbox_statemachine import SandboxLifecycleHelper, SandboxStateMachine


@pytest.fixture
def meta_store():
    m = AsyncMock()
    m.update = AsyncMock()
    return m


class TestArchiveTransitions:
    async def test_stopped_to_archiving(self, meta_store):
        sm = await SandboxStateMachine.from_state_value(State.STOPPED, {})
        await sm.send("archive", sandbox_id="sbx-1", meta_store=meta_store)
        assert sm.current_state.value == State.ARCHIVING

    async def test_archiving_to_archived(self, meta_store):
        sm = await SandboxStateMachine.from_state_value(State.ARCHIVING, {"archive_time": "t1"})
        await sm.send("archive_done", sandbox_id="sbx-1", meta_store=meta_store)
        assert sm.current_state.value == State.ARCHIVED

    async def test_archiving_to_stopped_on_failure(self, meta_store):
        sm = await SandboxStateMachine.from_state_value(State.ARCHIVING, {"archive_time": "t1"})
        await sm.send("archive_failed", sandbox_id="sbx-1", meta_store=meta_store, reason="timeout")
        assert sm.current_state.value == State.STOPPED

    async def test_archived_to_pending_on_restore(self, meta_store):
        sm = await SandboxStateMachine.from_state_value(State.ARCHIVED, {"archive_time": "t1"})
        await sm.send("restore", sandbox_id="sbx-1", meta_store=meta_store)
        assert sm.current_state.value == State.PENDING

    async def test_pending_to_running_on_alive_after_restore(self, meta_store):
        sm = await SandboxStateMachine.from_state_value(State.PENDING, {})
        await sm.send("alive", sandbox_id="sbx-1", meta_store=meta_store, sandbox_info={"host_ip": "10.0.0.1"})
        assert sm.current_state.value == State.RUNNING

    async def test_pending_to_archived_on_restore_failed(self, meta_store):
        sm = await SandboxStateMachine.from_state_value(State.PENDING, {"archive_time": "t1"})
        await sm.send("restore_failed", sandbox_id="sbx-1", meta_store=meta_store, reason="timeout")
        assert sm.current_state.value == State.ARCHIVED

    async def test_archived_to_deleted(self, meta_store):
        sm = await SandboxStateMachine.from_state_value(State.ARCHIVED, {"archive_time": "t1"})
        await sm.send(
            "delete",
            sandbox_id="sbx-1",
            operator=AsyncMock(),
            meta_store=meta_store,
            reason=AsyncMock(),
        )
        assert sm.current_state.value == State.DELETED


class TestArchiveTransitionsRejected:
    async def test_running_cannot_archive(self, meta_store):
        sm = await SandboxStateMachine.from_state_value(State.RUNNING, {})
        with pytest.raises(TransitionNotAllowed):
            await sm.send("archive", sandbox_id="sbx-1", meta_store=meta_store)

    async def test_archiving_cannot_archive(self, meta_store):
        sm = await SandboxStateMachine.from_state_value(State.ARCHIVING, {})
        with pytest.raises(TransitionNotAllowed):
            await sm.send("archive", sandbox_id="sbx-1", meta_store=meta_store)

    async def test_archived_cannot_restart_directly(self, meta_store):
        sm = await SandboxStateMachine.from_state_value(State.ARCHIVED, {})
        with pytest.raises(TransitionNotAllowed):
            await sm.send("restart", sandbox_id="sbx-1", operator=AsyncMock(), meta_store=meta_store)

    async def test_stopped_cannot_restore(self, meta_store):
        sm = await SandboxStateMachine.from_state_value(State.STOPPED, {})
        with pytest.raises(TransitionNotAllowed):
            await sm.send("restore", sandbox_id="sbx-1", meta_store=meta_store)

    async def test_archiving_cannot_stop(self, meta_store):
        sm = await SandboxStateMachine.from_state_value(State.ARCHIVING, {})
        with pytest.raises(TransitionNotAllowed):
            await sm.send("stop", sandbox_id="sbx-1", operator=AsyncMock(), meta_store=meta_store)

    async def test_archiving_cannot_delete(self, meta_store):
        sm = await SandboxStateMachine.from_state_value(State.ARCHIVING, {})
        with pytest.raises(TransitionNotAllowed):
            await sm.send(
                "delete",
                sandbox_id="sbx-1",
                operator=AsyncMock(),
                meta_store=meta_store,
                reason=AsyncMock(),
            )


class TestArchiveCleanupInCallbacks:
    async def test_on_delete_cleans_archive_artifacts(self, meta_store):
        dir_storage = AsyncMock()
        image_storage = AsyncMock()
        image_storage.registry_url = "localhost:5000"
        operator = AsyncMock()
        sm = await SandboxStateMachine.from_state_value(
            State.ARCHIVED,
            {
                "sandbox_id": "sbx-1",
                "archive_time": "2026-01-01T000000Z",
                "spec": {"container_name": "sbx-1", "image": "img:latest"},
            },
        )
        await sm.send(
            "delete",
            sandbox_id="sbx-1",
            operator=operator,
            meta_store=meta_store,
            dir_storage=dir_storage,
            image_storage=image_storage,
        )
        assert sm.current_state.value == State.DELETED
        dir_storage.delete.assert_called_once()
        assert "sbx-1" in dir_storage.delete.call_args[0][0]
        image_storage.delete.assert_called_once()
        assert "sbx-1" in image_storage.delete.call_args[0][0]

    async def test_on_delete_no_archive_time_skips_cleanup(self, meta_store):
        dir_storage = AsyncMock()
        image_storage = AsyncMock()
        image_storage.registry_url = "localhost:5000"
        operator = AsyncMock()
        sm = await SandboxStateMachine.from_state_value(
            State.STOPPED,
            {"sandbox_id": "sbx-1", "spec": {"container_name": "sbx-1", "image": "img:latest"}},
        )
        await sm.send(
            "delete",
            sandbox_id="sbx-1",
            operator=operator,
            meta_store=meta_store,
            dir_storage=dir_storage,
            image_storage=image_storage,
        )
        assert sm.current_state.value == State.DELETED
        dir_storage.delete.assert_not_called()
        image_storage.delete.assert_not_called()

    async def test_on_delete_without_storage_skips_cleanup(self, meta_store):
        operator = AsyncMock()
        sm = await SandboxStateMachine.from_state_value(
            State.ARCHIVED,
            {
                "sandbox_id": "sbx-1",
                "archive_time": "2026-01-01T000000Z",
                "spec": {"container_name": "sbx-1", "image": "img:latest"},
            },
        )
        await sm.send(
            "delete",
            sandbox_id="sbx-1",
            operator=operator,
            meta_store=meta_store,
        )
        assert sm.current_state.value == State.DELETED


class TestArchiveHookSideEffects:
    async def test_on_archive_sets_fields(self, meta_store):
        sm = await SandboxStateMachine.from_state_value(State.STOPPED, {})
        await sm.send("archive", sandbox_id="sbx-1", meta_store=meta_store)
        info = sm.sandbox_info
        assert "archive_time" not in info
        assert info["state"] == State.ARCHIVING
        # state_history records the transition with timestamp
        assert any(r["to_state"] == "archiving" for r in info.get("state_history", []))

    async def test_on_archive_done_sets_archive_time(self, meta_store):
        sm = await SandboxStateMachine.from_state_value(State.ARCHIVING, {})
        await sm.send("archive_done", sandbox_id="sbx-1", meta_store=meta_store)
        info = sm.sandbox_info
        assert info["state"] == State.ARCHIVED
        assert info.get("archive_time") is not None

    async def test_on_archive_done_sets_cluster_archived_delete_deadline(self, meta_store):
        sm = await SandboxStateMachine.from_state_value(
            State.ARCHIVING,
            {
                "spec": {"auto_delete_seconds": 600},
                "auto_transition_state": State.ARCHIVED,
                "auto_transition_time": "2026-01-01T00:00:00+00:00",
            },
        )
        await sm.send(
            "archive_done",
            sandbox_id="sbx-1",
            meta_store=meta_store,
            auto_transition=AutoTransitionConfig(auto_delete_seconds=3600, auto_delete_archived_seconds=7200),
        )
        info = sm.sandbox_info
        archive_time = SandboxLifecycleHelper.parse_iso8601_timestamp(info["archive_time"])
        archived_delete_time = SandboxLifecycleHelper.parse_iso8601_timestamp(info["auto_transition_time"])
        assert info["auto_transition_state"] == State.DELETED
        assert (archived_delete_time - archive_time).total_seconds() == 7200

    async def test_on_archive_done_sets_immediate_archived_delete_deadline_for_zero(self, meta_store):
        sm = await SandboxStateMachine.from_state_value(
            State.ARCHIVING,
            {
                "auto_delete_seconds": 0,
                "auto_transition_state": State.ARCHIVED,
                "auto_transition_time": "2026-01-01T00:00:00+00:00",
            },
        )
        await sm.send(
            "archive_done",
            sandbox_id="sbx-1",
            meta_store=meta_store,
            auto_transition=AutoTransitionConfig(auto_delete_seconds=3600, auto_delete_archived_seconds=0),
        )
        info = sm.sandbox_info
        archive_time = SandboxLifecycleHelper.parse_iso8601_timestamp(info["archive_time"])
        archived_delete_time = SandboxLifecycleHelper.parse_iso8601_timestamp(info["auto_transition_time"])
        assert info["auto_transition_state"] == State.DELETED
        assert archived_delete_time == archive_time

    async def test_on_archive_failed_clears_archive_time(self, meta_store):
        sm = await SandboxStateMachine.from_state_value(State.ARCHIVING, {"archive_time": "t1"})
        await sm.send("archive_failed", sandbox_id="sbx-1", meta_store=meta_store, reason="timeout")
        info = sm.sandbox_info
        assert info["state"] == State.STOPPED
        assert info["archive_time"] is None

    async def test_on_archive_failed_sets_auto_delete_time_from_failure_time(self, meta_store):
        sm = await SandboxStateMachine.from_state_value(
            State.ARCHIVING,
            {
                "stop_time": "2026-01-01T00:00:00+00:00",
                "auto_delete_seconds": 600,
                "auto_transition_state": None,
                "auto_transition_time": None,
            },
        )
        before_failure = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).replace(microsecond=0)
        await sm.send(
            "archive_failed",
            sandbox_id="sbx-1",
            meta_store=meta_store,
            reason="timeout",
            auto_transition=AutoTransitionConfig(auto_delete_seconds=3600),
        )
        after_failure = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).replace(microsecond=0)
        info = sm.sandbox_info
        auto_delete_time = SandboxLifecycleHelper.parse_iso8601_timestamp(info["auto_transition_time"])
        assert info["state"] == State.STOPPED
        assert info["auto_transition_state"] == State.DELETED
        assert auto_delete_time.tzinfo is not None
        assert before_failure + datetime.timedelta(seconds=3600) <= auto_delete_time
        assert auto_delete_time <= after_failure + datetime.timedelta(seconds=3600)

    async def test_on_restore_sets_pending_and_clears_archive_deadline(self, meta_store):
        deadline = "2026-01-02T08:00:00+08:00"
        sm = await SandboxStateMachine.from_state_value(
            State.ARCHIVED,
            {
                "archive_time": "t1",
                "auto_transition_state": State.DELETED,
                "auto_transition_time": deadline,
            },
        )
        await sm.send("restore", sandbox_id="sbx-1", meta_store=meta_store)
        info = sm.sandbox_info
        assert info["state"] == State.PENDING
        assert "archive_time" in info
        assert info["auto_transition_state"] is None
        assert info["auto_transition_time"] is None
        # state_history records the transition
        assert any(r["to_state"] == "pending" for r in info.get("state_history", []))

    async def test_on_restore_failed_resets_archived_delete_deadline_from_failure_time(self, meta_store):
        old_deadline = "2026-01-02T08:00:00+08:00"
        sm = await SandboxStateMachine.from_state_value(
            State.PENDING,
            {
                "archive_time": "t1",
                "auto_transition_state": State.DELETED,
                "auto_transition_time": old_deadline,
            },
        )
        before_failure = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).replace(microsecond=0)
        await sm.send(
            "restore_failed",
            sandbox_id="sbx-1",
            meta_store=meta_store,
            reason="timeout",
            auto_transition=AutoTransitionConfig(auto_delete_archived_seconds=7200),
        )
        after_failure = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).replace(microsecond=0)
        info = sm.sandbox_info
        auto_delete_time = SandboxLifecycleHelper.parse_iso8601_timestamp(info["auto_transition_time"])
        assert info["state"] == State.ARCHIVED
        assert info["auto_transition_state"] == State.DELETED
        assert info["auto_transition_time"] != old_deadline
        assert before_failure + datetime.timedelta(seconds=7200) <= auto_delete_time
        assert auto_delete_time <= after_failure + datetime.timedelta(seconds=7200)

    async def test_on_restore_failed_sets_immediate_archived_delete_deadline_for_zero(self, meta_store):
        sm = await SandboxStateMachine.from_state_value(
            State.PENDING,
            {
                "archive_time": "t1",
                "auto_transition_state": State.DELETED,
                "auto_transition_time": "2026-01-02T08:00:00+08:00",
            },
        )
        before_failure = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).replace(microsecond=0)
        await sm.send(
            "restore_failed",
            sandbox_id="sbx-1",
            meta_store=meta_store,
            reason="timeout",
            auto_transition=AutoTransitionConfig(auto_delete_archived_seconds=0),
        )
        after_failure = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).replace(microsecond=0)
        info = sm.sandbox_info
        auto_delete_time = SandboxLifecycleHelper.parse_iso8601_timestamp(info["auto_transition_time"])
        assert info["state"] == State.ARCHIVED
        assert info["auto_transition_state"] == State.DELETED
        assert before_failure <= auto_delete_time <= after_failure

    async def test_on_restore_failed_clears_deadline_when_archived_auto_delete_is_disabled(self, meta_store):
        sm = await SandboxStateMachine.from_state_value(
            State.PENDING,
            {
                "archive_time": "t1",
                "auto_transition_state": State.DELETED,
                "auto_transition_time": "2026-01-02T08:00:00+08:00",
            },
        )
        await sm.send(
            "restore_failed",
            sandbox_id="sbx-1",
            meta_store=meta_store,
            reason="timeout",
            auto_transition=AutoTransitionConfig(auto_delete_archived_seconds=None),
        )
        info = sm.sandbox_info
        assert info["state"] == State.ARCHIVED
        assert info["auto_transition_state"] is None
        assert info["auto_transition_time"] is None
