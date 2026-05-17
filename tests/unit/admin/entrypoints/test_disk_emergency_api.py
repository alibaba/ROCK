"""Unit tests for disk_emergency_api.

Tests bypass FastAPI by calling the handler via __wrapped__ (handle_exceptions
uses functools.wraps so this is safe). Mocks RockConfig and TaskFactory so we
don't depend on a real scheduler.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.actions import ResponseStatus
from rock.admin.entrypoints import admin_ops_api as api
from rock.admin.proto.request import DiskEmergencyCleanupRequest

# ----------------------------------------------------------------------------- #
# Fixtures
# ----------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _reset_state():
    """Snapshot + restore rate limit, providers, and async jobs between tests."""
    saved_rate = dict(api._last_triggered_at)
    saved_workers = api._alive_workers_provider
    saved_config = api._rock_config_provider
    saved_jobs = dict(api._async_jobs)

    api._last_triggered_at = {}
    api._alive_workers_provider = None
    api._rock_config_provider = None
    api._async_jobs = {}

    yield

    api._last_triggered_at = saved_rate
    api._alive_workers_provider = saved_workers
    api._rock_config_provider = saved_config
    api._async_jobs = saved_jobs


def _make_task(task_type: str, run_impl=None) -> MagicMock:
    task = MagicMock()
    task.type = task_type
    task.run = AsyncMock(side_effect=run_impl) if run_impl else AsyncMock(return_value=None)
    return task


def _make_request(client_host="127.0.0.1") -> MagicMock:
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = client_host
    return req


def _wire_config_with_tasks(tasks: list):
    """Mock _rock_config_provider to return a RockConfig-shaped MagicMock whose
    scheduler.tasks is a list of fake TaskConfig entries, and patch TaskFactory
    so create_task returns the given BaseTask mocks in declaration order."""
    fake_task_configs = []
    for t in tasks:
        tc = MagicMock(spec=["task_class", "enabled"])
        tc.task_class = f"fake.module.{t.type.title().replace('_', '')}"
        tc.enabled = True
        fake_task_configs.append(tc)
    fake_cfg = MagicMock()
    fake_cfg.scheduler.tasks = fake_task_configs
    api._rock_config_provider = lambda: fake_cfg

    # Map task_class -> BaseTask mock for TaskFactory.create_task to look up
    by_class = {tc.task_class: t for tc, t in zip(fake_task_configs, tasks, strict=True)}

    def fake_create(task_config):
        return by_class[task_config.task_class]

    return patch("rock.admin.entrypoints.admin_ops_api.TaskFactory.create_task", side_effect=fake_create)


# ----------------------------------------------------------------------------- #
# Whitelist + selection
# ----------------------------------------------------------------------------- #


class TestSelectTasks:
    def test_default_returns_all_available(self):
        available = {
            "docker_image_prune": _make_task("docker_image_prune"),
            "file_cleanup": _make_task("file_cleanup"),
        }
        selected, errors = api._select_tasks(None, available)
        assert sorted(t.type for t in selected) == ["docker_image_prune", "file_cleanup"]
        assert errors == []

    def test_explicit_request_filters_unknown(self):
        available = {"file_cleanup": _make_task("file_cleanup")}
        selected, errors = api._select_tasks(["file_cleanup", "ghost_cleanup"], available)
        assert [t.type for t in selected] == ["file_cleanup"]
        assert any("ghost_cleanup" in e and "not in eligible task pool" in e for e in errors)

    def test_duplicate_in_request_reported_once(self):
        available = {"file_cleanup": _make_task("file_cleanup")}
        selected, errors = api._select_tasks(["file_cleanup", "file_cleanup"], available)
        assert [t.type for t in selected] == ["file_cleanup"]
        assert any("duplicate" in e for e in errors)


class TestWhitelist:
    def test_suffix_cleanup(self):
        assert api._is_task_whitelisted("file_cleanup") is True
        assert api._is_task_whitelisted("docker_image_prune") is True

    def test_suffix_archive(self):
        # `_archive` is whitelisted so emergency API can manually trigger
        # SandboxLogArchiveTask outside its 24h cadence (e.g. for SRE recovery).
        assert api._is_task_whitelisted("sandbox_log_archive") is True

    def test_non_whitelist(self):
        assert api._is_task_whitelisted("image_pull") is False
        assert api._is_task_whitelisted("warmup_check") is False


# ----------------------------------------------------------------------------- #
# Rate limit
# ----------------------------------------------------------------------------- #


class TestRateLimit:
    def test_first_call_allowed(self):
        assert api._check_and_mark_rate_limit("file_cleanup", now=1000.0) is True

    def test_within_window_blocked(self):
        api._check_and_mark_rate_limit("file_cleanup", now=1000.0)
        assert api._check_and_mark_rate_limit("file_cleanup", now=1030.0) is False

    def test_after_window_allowed(self):
        api._check_and_mark_rate_limit("file_cleanup", now=1000.0)
        future = 1000.0 + api._RATE_LIMIT_SECONDS + 1
        assert api._check_and_mark_rate_limit("file_cleanup", now=future) is True

    def test_independent_per_task(self):
        api._check_and_mark_rate_limit("file_cleanup", now=1000.0)
        # different task name, same instant -> allowed
        assert api._check_and_mark_rate_limit("docker_image_prune", now=1000.0) is True


# ----------------------------------------------------------------------------- #
# Handler full path — async mode (default)
# ----------------------------------------------------------------------------- #


class TestDiskEmergencyCleanupAsync:
    @pytest.mark.asyncio
    async def test_no_worker_returns_failed(self):
        api.set_alive_workers_provider(lambda: [])
        with _wire_config_with_tasks([_make_task("file_cleanup")]):
            resp = await api.disk_emergency_cleanup.__wrapped__(DiskEmergencyCleanupRequest(), _make_request())
        assert resp.status == ResponseStatus.FAILED
        assert "no worker available" in resp.message

    @pytest.mark.asyncio
    async def test_no_config_provider_returns_failed(self):
        api.set_alive_workers_provider(lambda: ["10.0.0.1"])
        resp = await api.disk_emergency_cleanup.__wrapped__(DiskEmergencyCleanupRequest(), _make_request())
        assert resp.status == ResponseStatus.FAILED
        assert "not initialized" in resp.message

    @pytest.mark.asyncio
    async def test_no_eligible_task_returns_failed(self):
        api.set_alive_workers_provider(lambda: ["10.0.0.1"])
        # image_pull is not whitelisted, gets filtered in _build_eligible_tasks
        with _wire_config_with_tasks([_make_task("image_pull")]):
            resp = await api.disk_emergency_cleanup.__wrapped__(DiskEmergencyCleanupRequest(), _make_request())
        assert resp.status == ResponseStatus.FAILED
        assert "no eligible task" in resp.message

    @pytest.mark.asyncio
    async def test_async_returns_job_id_immediately(self):
        t1 = _make_task("file_cleanup")
        api.set_alive_workers_provider(lambda: ["10.0.0.1", "10.0.0.2"])
        with _wire_config_with_tasks([t1]):
            resp = await api.disk_emergency_cleanup.__wrapped__(DiskEmergencyCleanupRequest(), _make_request())

        assert resp.status == ResponseStatus.SUCCESS
        assert resp.message == "accepted"
        assert "job_id" in resp.result
        assert resp.result["worker_count"] == 2
        assert resp.result["tasks"] == ["file_cleanup"]

        # Let background task complete
        await asyncio.sleep(0.05)
        job_id = resp.result["job_id"]
        assert api._async_jobs[job_id]["status"] == "completed"
        assert "file_cleanup" in api._async_jobs[job_id]["results"]
        t1.run.assert_awaited_once_with(["10.0.0.1", "10.0.0.2"])

    @pytest.mark.asyncio
    async def test_async_background_error_recorded_in_job(self):
        async def boom(_workers):
            raise RuntimeError("kaboom")

        t = _make_task("file_cleanup", run_impl=boom)
        api.set_alive_workers_provider(lambda: ["10.0.0.1"])
        with _wire_config_with_tasks([t]):
            resp = await api.disk_emergency_cleanup.__wrapped__(
                DiskEmergencyCleanupRequest(tasks=["file_cleanup"]), _make_request()
            )

        assert resp.status == ResponseStatus.SUCCESS
        assert resp.message == "accepted"

        # Let background task complete
        await asyncio.sleep(0.05)
        job_id = resp.result["job_id"]
        # Task error is recorded per-task inside results, job itself completes
        job = api._async_jobs[job_id]
        assert job["status"] == "completed"
        assert job["results"]["file_cleanup"]["status"] == "error"
        assert "kaboom" in job["results"]["file_cleanup"]["error"]

    @pytest.mark.asyncio
    async def test_rate_limit_blocks_second_call(self):
        t = _make_task("file_cleanup")
        api.set_alive_workers_provider(lambda: ["10.0.0.1"])
        with _wire_config_with_tasks([t]):
            first = await api.disk_emergency_cleanup.__wrapped__(
                DiskEmergencyCleanupRequest(tasks=["file_cleanup"]), _make_request()
            )
            assert first.status == ResponseStatus.SUCCESS

            second = await api.disk_emergency_cleanup.__wrapped__(
                DiskEmergencyCleanupRequest(tasks=["file_cleanup"]), _make_request()
            )
            assert second.status == ResponseStatus.FAILED
            assert "rate limited" in second.message
            assert "file_cleanup" in second.result["rate_limited"]

    @pytest.mark.asyncio
    async def test_explicit_worker_ips_overrides_provider(self):
        t = _make_task("file_cleanup")
        api.set_alive_workers_provider(lambda: ["from-provider"])
        with _wire_config_with_tasks([t]):
            resp = await api.disk_emergency_cleanup.__wrapped__(
                DiskEmergencyCleanupRequest(tasks=["file_cleanup"], worker_ips=["10.99.0.1"]),
                _make_request(),
            )
        assert resp.status == ResponseStatus.SUCCESS
        # Let background task complete
        await asyncio.sleep(0.05)
        t.run.assert_awaited_once_with(["10.99.0.1"])

    @pytest.mark.asyncio
    async def test_provider_exception_falls_back_to_failed(self):
        def boom():
            raise RuntimeError("ray not initialised")

        api.set_alive_workers_provider(boom)
        with _wire_config_with_tasks([_make_task("file_cleanup")]):
            resp = await api.disk_emergency_cleanup.__wrapped__(DiskEmergencyCleanupRequest(), _make_request())
        assert resp.status == ResponseStatus.FAILED
        assert "no worker available" in resp.message

    @pytest.mark.asyncio
    async def test_partial_rate_limit_runs_the_unblocked_subset(self):
        # Trigger file_cleanup first, then request both — only docker_image_prune runs.
        t1 = _make_task("file_cleanup")
        t2 = _make_task("docker_image_prune")
        api.set_alive_workers_provider(lambda: ["10.0.0.1"])
        with _wire_config_with_tasks([t1, t2]):
            await api.disk_emergency_cleanup.__wrapped__(
                DiskEmergencyCleanupRequest(tasks=["file_cleanup"]), _make_request()
            )
            resp = await api.disk_emergency_cleanup.__wrapped__(
                DiskEmergencyCleanupRequest(tasks=["file_cleanup", "docker_image_prune"]),
                _make_request(),
            )
        assert resp.status == ResponseStatus.SUCCESS
        assert "file_cleanup" in resp.result["rate_limited"]
        assert "docker_image_prune" in resp.result["tasks"]


# ----------------------------------------------------------------------------- #
# Status endpoint
# ----------------------------------------------------------------------------- #


class TestDiskEmergencyCleanupStatus:
    @pytest.mark.asyncio
    async def test_unknown_job_returns_failed(self):
        resp = await api.disk_emergency_cleanup_status.__wrapped__("nonexistent")
        assert resp.status == ResponseStatus.FAILED
        assert "not found" in resp.message

    @pytest.mark.asyncio
    async def test_existing_job_returns_status(self):
        api._async_jobs["test123"] = {"status": "running", "tasks": ["file_cleanup"]}
        resp = await api.disk_emergency_cleanup_status.__wrapped__("test123")
        assert resp.status == ResponseStatus.SUCCESS
        assert resp.result["status"] == "running"
