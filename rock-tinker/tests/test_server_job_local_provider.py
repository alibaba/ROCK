from __future__ import annotations

import asyncio
import signal
from typing import Any

import httpx
import pytest

from tinker.server_job.config_types import LocalServerJobConfig
from tinker.server_job.platforms.local import LocalServerJobProvider, _job_root


def test_local_job_root_honors_tinker_jobs_root(monkeypatch, tmp_path) -> None:
    override = tmp_path / "custom-jobs"
    monkeypatch.setenv("TINKER_JOBS_ROOT", str(override))

    assert _job_root() == override


def test_local_server_job_provider_starts_owned_backend_and_records_job(monkeypatch, tmp_path) -> None:
    repo = tmp_path / "tinker-backend"
    executable = repo / ".venv" / "bin" / "tinker-backend"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    popen_calls: list[dict[str, Any]] = []
    backend_started = False

    class FakeProcess:
        pid = 23456

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            self.killed = True

    process = FakeProcess()

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        nonlocal backend_started
        backend_started = True
        popen_calls.append({"command": command, **kwargs})
        return process

    class FakeHttpClient:
        readyz_calls = 0

        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "FakeHttpClient":
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            return None

        def get(self, url: str) -> httpx.Response:
            request = httpx.Request("GET", url)
            if not backend_started:
                raise httpx.ConnectError("backend not started", request=request)
            if url.endswith("/readyz"):
                self.__class__.readyz_calls += 1
                return httpx.Response(
                    200,
                    json={"ready": self.__class__.readyz_calls > 1},
                    request=request,
                )
            return httpx.Response(200, json={"status": "ok"}, request=request)

    monkeypatch.setattr("tinker.server_job.platforms.local.subprocess.Popen", fake_popen)
    monkeypatch.setattr("tinker.server_job.platforms.local.httpx.Client", FakeHttpClient)

    provider = LocalServerJobProvider(
        LocalServerJobConfig(
            repo_path=str(repo),
            port=49154,
            host="0.0.0.0",
            client_host="127.0.0.1",
            startup_timeout=1,
            poll_interval=0,
        )
    )
    handle = asyncio.run(provider.submit())

    assert handle.job_id.startswith("local_")
    assert handle.base_url == "http://127.0.0.1:49154"
    assert handle.process_pid == 23456
    assert handle.log_dir == str(home / ".rock" / "tinker" / "jobs" / handle.job_id)
    assert (home / ".rock" / "tinker" / "jobs" / handle.job_id / "job.json").exists()
    assert (home / ".rock" / "tinker" / "jobs" / handle.job_id / "status.json").exists()
    assert popen_calls
    launch = popen_calls[0]
    assert launch["command"] == [str(executable)]
    assert launch["cwd"] == str(repo)
    assert launch["stdout"].name.endswith("tinker_backend.log")
    assert launch["stderr"] is launch["stdout"]
    assert launch["start_new_session"] is True
    env = launch["env"]
    assert env["TINKER_PLATFORM"] == "local"
    assert env["TINKER_JOB_ID"] == handle.job_id
    assert env["TINKER_LOG_DIR"] == handle.log_dir
    assert env["HOST"] == "0.0.0.0"
    assert env["PORT"] == "49154"
    assert env["PUBLIC_BASE_URL"] == "http://127.0.0.1:49154"


def test_local_server_job_provider_refuses_occupied_backend_port(monkeypatch, tmp_path) -> None:
    repo = tmp_path / "tinker-backend"
    executable = repo / ".venv" / "bin" / "tinker-backend"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    def fake_popen(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("occupied backend port must be rejected before Popen")

    class FakeHttpClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "FakeHttpClient":
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            return None

        def get(self, url: str) -> httpx.Response:
            request = httpx.Request("GET", url)
            if url.endswith("/readyz"):
                return httpx.Response(200, json={"ready": False}, request=request)
            return httpx.Response(200, json={"status": "ok"}, request=request)

    monkeypatch.setattr("tinker.server_job.platforms.local.subprocess.Popen", fake_popen)
    monkeypatch.setattr("tinker.server_job.platforms.local.httpx.Client", FakeHttpClient)

    provider = LocalServerJobProvider(
        LocalServerJobConfig(repo_path=str(repo), port=49154, client_host="127.0.0.1")
    )
    with pytest.raises(RuntimeError, match="port 49154 is already occupied"):
        asyncio.run(provider.submit())
    assert not (home / ".rock" / "tinker" / "jobs").exists()


def test_local_status_marks_stale_running_pid_failed_cross_process(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    log_dir = home / ".rock" / "tinker" / "jobs" / "local_dead"
    log_dir.mkdir(parents=True)
    (log_dir / "status.json").write_text(
        '{'
        '"job_id":"local_dead",'
        '"platform":"local",'
        '"status":"running",'
        '"base_url":"http://127.0.0.1:49000",'
        '"process_pid":43210'
        '}',
        encoding="utf-8",
    )

    def fake_kill(pid: int, sig: int) -> None:
        assert (pid, sig) == (43210, 0)
        raise ProcessLookupError

    monkeypatch.setattr("tinker.server_job.platforms.local.os.kill", fake_kill)

    provider = LocalServerJobProvider(LocalServerJobConfig())
    handle = asyncio.run(provider.attach("local_dead"))
    status = asyncio.run(provider.status(handle))

    assert status.status == "failed"
    assert status.exit_code is None
    assert "process 43210 is not running" in (status.message or "")
    assert '\"status\": \"failed\"' in (log_dir / "status.json").read_text(encoding="utf-8")


def test_local_stop_terminates_owned_process_group(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    log_dir = home / ".rock" / "tinker" / "jobs" / "local_owned"
    log_dir.mkdir(parents=True)
    (log_dir / "status.json").write_text(
        '{'
        '"job_id":"local_owned",'
        '"platform":"local",'
        '"status":"running",'
        '"base_url":"http://127.0.0.1:49000",'
        '"process_pid":23456'
        '}',
        encoding="utf-8",
    )

    class FakeProcess:
        pid = 23456

        def __init__(self) -> None:
            self.wait_calls = 0

        def poll(self) -> int | None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls += 1
            return 0

    process = FakeProcess()
    import tinker.server_job.platforms.local as local_module

    local_module._LOCAL_PROCESSES["local_owned"] = process
    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr("tinker.server_job.platforms.local.os.killpg", lambda pid, sig: killpg_calls.append((pid, sig)))

    provider = LocalServerJobProvider(LocalServerJobConfig())
    handle = asyncio.run(provider.attach("local_owned"))
    result = asyncio.run(provider.stop(handle))

    assert result.status == "stopped"
    assert killpg_calls == [(23456, signal.SIGTERM)]
    assert process.wait_calls == 1
    assert '\"status\": \"stopped\"' in (log_dir / "status.json").read_text(encoding="utf-8")


def test_local_submit_startup_failure_records_failed_status(monkeypatch, tmp_path) -> None:
    repo = tmp_path / "tinker-backend"
    executable = repo / ".venv" / "bin" / "tinker-backend"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    backend_started = False

    class FakeProcess:
        pid = 34567

        def poll(self) -> int | None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

    def fake_popen(*args: Any, **kwargs: Any) -> FakeProcess:
        nonlocal backend_started
        backend_started = True
        return FakeProcess()

    def fake_create_connection(*args: Any, **kwargs: Any) -> None:
        raise OSError("port is free")

    monkeypatch.setattr("tinker.server_job.platforms.local.subprocess.Popen", fake_popen)
    monkeypatch.setattr("tinker.server_job.platforms.local.os.killpg", lambda pid, sig: None)
    monkeypatch.setattr("tinker.server_job.platforms.local.socket.create_connection", fake_create_connection)

    class FakeHttpClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "FakeHttpClient":
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            return None

        def get(self, url: str) -> httpx.Response:
            request = httpx.Request("GET", url)
            if not backend_started:
                raise httpx.ConnectError("backend not started", request=request)
            return httpx.Response(503, json={"ready": False}, request=request)

    monkeypatch.setattr("tinker.server_job.platforms.local.httpx.Client", FakeHttpClient)

    provider = LocalServerJobProvider(
        LocalServerJobConfig(repo_path=str(repo), port=49155, startup_timeout=0, poll_interval=0)
    )
    with pytest.raises(TimeoutError):
        asyncio.run(provider.submit())

    jobs = list((home / ".rock" / "tinker" / "jobs").iterdir())
    assert len(jobs) == 1
    status_text = (jobs[0] / "status.json").read_text(encoding="utf-8")
    assert '\"status\": \"failed\"' in status_text
    assert "did not become ready" in status_text
    assert '\"process_pid\": 34567' in status_text



def test_local_stop_requests_backend_graceful_shutdown_before_terminating(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    log_dir = home / ".rock" / "tinker" / "jobs" / "local_graceful"
    log_dir.mkdir(parents=True)
    (log_dir / "status.json").write_text(
        '{'
        '"job_id":"local_graceful",'
        '"platform":"local",'
        '"status":"running",'
        '"base_url":"http://127.0.0.1:49000",'
        '"process_pid":23456'
        '}',
        encoding="utf-8",
    )

    posts: list[tuple[str, dict]] = []

    class FakeHttpClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "FakeHttpClient":
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            return None

        def post(self, url: str, json: dict | None = None) -> httpx.Response:
            posts.append((url, json or {}))
            return httpx.Response(200, json={"status": "stopped", "runtime_count": 1}, request=httpx.Request("POST", url))

    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr("tinker.server_job.platforms.local.httpx.Client", FakeHttpClient)
    monkeypatch.setattr("tinker.server_job.platforms.local.os.killpg", lambda pid, sig: killpg_calls.append((pid, sig)))

    provider = LocalServerJobProvider(LocalServerJobConfig())
    handle = asyncio.run(provider.attach("local_graceful"))
    result = asyncio.run(provider.stop(handle, force_after_seconds=7.5))

    assert result.status == "stopped"
    assert posts == [("http://127.0.0.1:49000/api/v1/server_job/stop", {"force_after_seconds": 7.5})]
    assert killpg_calls == [(23456, signal.SIGTERM)]
    status_text = (log_dir / "status.json").read_text(encoding="utf-8")
    assert '"status": "stopped"' in status_text
