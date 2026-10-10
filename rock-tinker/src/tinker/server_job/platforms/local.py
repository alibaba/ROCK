"""Local subprocess-backed Tinker backend server job provider."""

from __future__ import annotations

import json
import os
import random
import re
import shlex
import signal
import socket
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from tinker.server_job.config_types import LocalServerJobConfig
from tinker.server_job.platforms.base import ServerJobHandle
from tinker.server_job.response_types import ServerJobLogsResponse, ServerJobStatusResponse, ServerJobStopResponse

_LOCAL_PROCESSES: dict[str, subprocess.Popen[Any]] = {}
_RUNTIME_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_job_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"local_{stamp}_{random.randrange(16**8):08x}"


def _job_root() -> Path:
    override = os.environ.get("TINKER_JOBS_ROOT")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".rock" / "tinker" / "jobs"


def _strip_trailing_slash(url: str) -> str:
    return url.rstrip("/")


class LocalServerJobProvider:
    def __init__(self, config: LocalServerJobConfig):
        self.config = config

    async def submit(self) -> ServerJobHandle:
        base_url = self._base_url()
        self._raise_if_backend_already_running(base_url)
        job_id = _make_job_id()
        log_dir = _job_root() / job_id
        log_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(log_dir / "job.json", {"job_id": job_id, "platform": "local", "config": self.config.model_dump(mode="json"), "base_url": base_url})
        self._write_status(log_dir, job_id=job_id, status="submitted", base_url=base_url)

        log_file = (log_dir / "tinker_backend.log").open("ab")
        try:
            process = subprocess.Popen(
                self._command(),
                cwd=str(self._repo_path()) if self._repo_path() else None,
                env=self._env(base_url, job_id, log_dir),
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
            )
        finally:
            log_file.close()
        _LOCAL_PROCESSES[job_id] = process
        handle = ServerJobHandle(
            job_id=job_id,
            platform="local",
            base_url=base_url,
            log_dir=str(log_dir),
            process_pid=process.pid,
        )
        self._write_status(log_dir, job_id=job_id, status="pending", base_url=base_url, process_pid=process.pid)
        try:
            self._wait_until_ready(base_url, process)
        except Exception as exc:
            _LOCAL_PROCESSES.pop(job_id, None)
            self._terminate_process_group(process.pid, process)
            self._write_status(
                log_dir,
                job_id=job_id,
                status="failed",
                base_url=base_url,
                process_pid=process.pid,
                message=str(exc),
                finished_at=_utcnow(),
            )
            raise
        self._write_status(log_dir, job_id=job_id, status="running", base_url=base_url, process_pid=process.pid)
        return handle

    async def attach(self, job_id: str) -> ServerJobHandle:
        log_dir = _job_root() / job_id
        if not log_dir.exists():
            raise FileNotFoundError(f"local ServerJob {job_id!r} not found under {_job_root()}")
        status = self._read_json(log_dir / "status.json")
        return ServerJobHandle(
            job_id=job_id,
            platform="local",
            base_url=status.get("base_url"),
            log_dir=str(log_dir),
            process_pid=status.get("process_pid"),
        )

    async def status(self, handle: ServerJobHandle) -> ServerJobStatusResponse:
        log_dir = Path(handle.log_dir) if handle.log_dir else _job_root() / handle.job_id
        data = self._read_json(log_dir / "status.json") if (log_dir / "status.json").exists() else {}
        process = _LOCAL_PROCESSES.get(handle.job_id)
        status = data.get("status") or "pending"
        message = data.get("message")
        exit_code = data.get("exit_code")
        process_pid = handle.process_pid or data.get("process_pid")
        finished_at = data.get("finished_at")

        if status in {"submitted", "pending", "running"}:
            if process is not None:
                exit_code = process.poll()
                if exit_code is not None:
                    status = "succeeded" if exit_code == 0 else "failed"
                    finished_at = _utcnow()
                    self._write_status(
                        log_dir,
                        job_id=handle.job_id,
                        status=status,
                        base_url=handle.base_url or data.get("base_url"),
                        process_pid=process_pid,
                        exit_code=exit_code,
                        message=message,
                        finished_at=finished_at,
                    )
            elif process_pid and not self._process_exists(int(process_pid)):
                status = "failed"
                message = f"local tinker-backend process {process_pid} is not running"
                finished_at = _utcnow()
                self._write_status(
                    log_dir,
                    job_id=handle.job_id,
                    status=status,
                    base_url=handle.base_url or data.get("base_url"),
                    process_pid=process_pid,
                    exit_code=exit_code,
                    message=message,
                    finished_at=finished_at,
                )

        return ServerJobStatusResponse(
            job_id=handle.job_id,
            platform="local",
            status=status,
            message=message,
            started_at=data.get("started_at"),
            finished_at=finished_at,
            exit_code=exit_code,
            base_url=handle.base_url or data.get("base_url"),
            log_dir=str(log_dir),
        )

    async def stop(self, handle: ServerJobHandle, *, force_after_seconds: float | None = None) -> ServerJobStopResponse:
        process = _LOCAL_PROCESSES.pop(handle.job_id, None)
        process_pid = handle.process_pid or (process.pid if process is not None else None)
        force_after = 30.0 if force_after_seconds is None else max(0.0, float(force_after_seconds))
        message = None
        if handle.base_url:
            try:
                self._request_backend_stop(handle.base_url, force_after_seconds=force_after)
                message = "backend graceful stop endpoint completed"
            except Exception as exc:  # noqa: BLE001 - process cleanup remains the final local fallback.
                message = f"backend graceful stop endpoint failed; process group cleanup used: {exc}"
        if process_pid and (process is None or process.poll() is None):
            self._terminate_process_group(int(process_pid), process)
        log_dir = Path(handle.log_dir) if handle.log_dir else _job_root() / handle.job_id
        self._write_status(
            log_dir,
            job_id=handle.job_id,
            status="stopped",
            base_url=handle.base_url,
            process_pid=process_pid,
            message=message,
            finished_at=_utcnow(),
        )
        return ServerJobStopResponse(job_id=handle.job_id, status="stopped", message=message)

    def _request_backend_stop(self, base_url: str, *, force_after_seconds: float) -> dict[str, Any]:
        timeout = max(5.0, force_after_seconds + 5.0)
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{_strip_trailing_slash(base_url)}/api/v1/server_job/stop",
                json={"force_after_seconds": force_after_seconds},
            )
            response.raise_for_status()
            return response.json()

    async def resolve_log_path(self, handle: ServerJobHandle, *, kind: str = "cookbook", runtime_id: str | None = None) -> Path:
        log_dir = Path(handle.log_dir) if handle.log_dir else _job_root() / handle.job_id
        if kind == "cookbook":
            cookbook_log = log_dir / "cookbook.log"
            backend_log = log_dir / "tinker_backend.log"
            return backend_log if not cookbook_log.exists() and backend_log.exists() else cookbook_log
        if kind == "backend":
            return log_dir / "tinker_backend.log"
        if kind == "runtime":
            if runtime_id is not None:
                if not _RUNTIME_ID_PATTERN.match(runtime_id):
                    raise ValueError(f"Invalid runtime id: {runtime_id}")
                return log_dir / f"runtime_{runtime_id}.log"
            runtime_logs = sorted(log_dir.glob("runtime_*.log"))
            if len(runtime_logs) == 1:
                return runtime_logs[0]
            if not runtime_logs:
                raise FileNotFoundError(f"No runtime logs found for {handle.job_id}")
            raise RuntimeError(f"Multiple runtime logs found for {handle.job_id}; pass --runtime-id")
        raise ValueError(f"Unknown log kind: {kind}")

    @staticmethod
    def _process_exists(process_pid: int) -> bool:
        try:
            os.kill(process_pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def _terminate_process_group(process_pid: int, process: subprocess.Popen[Any] | None = None) -> None:
        try:
            os.killpg(process_pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError:
            return
        if process is None:
            return
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)

    async def download_logs(self, handle: ServerJobHandle, output_path: Path | None) -> ServerJobLogsResponse:
        log_dir = Path(handle.log_dir) if handle.log_dir else _job_root() / handle.job_id
        if output_path is None:
            return ServerJobLogsResponse(job_id=handle.job_id, log_path=str(log_dir))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(output_path, "w:gz") as tar:
            tar.add(log_dir, arcname=handle.job_id)
        return ServerJobLogsResponse(job_id=handle.job_id, log_path=str(output_path), log_size_bytes=output_path.stat().st_size)

    def _base_url(self) -> str:
        return f"http://{self.config.client_host}:{self.config.port}"

    def _raise_if_backend_already_running(self, base_url: str) -> None:
        if self._port_has_listener() or self._healthz_responds(base_url):
            raise RuntimeError(
                f"port {self.config.port} is already occupied by another tinker-backend job; "
                "wait for it to finish or choose another port"
            )

    def _port_has_listener(self) -> bool:
        try:
            with socket.create_connection((self.config.client_host, self.config.port), timeout=1):
                return True
        except OSError:
            return False

    @staticmethod
    def _healthz_responds(base_url: str) -> bool:
        try:
            with httpx.Client(timeout=3) as client:
                client.get(f"{base_url}/healthz")
        except httpx.HTTPError:
            return False
        return True

    def _repo_path(self) -> Path | None:
        if not self.config.repo_path:
            return None
        path = Path(self.config.repo_path).expanduser()
        return path if path.exists() else None

    def _command(self) -> list[str]:
        repo_path = self._repo_path()
        if repo_path:
            executable = repo_path / ".venv" / "bin" / "tinker-backend"
            if executable.exists():
                return [str(executable)]
            python = repo_path / ".venv" / "bin" / "python"
            if python.exists():
                return [str(python), "-m", "tinker_backend.main"]
            return ["uv", "run", "tinker-backend"]
        return [sys.executable, "-m", "tinker_backend.main"]

    def _env(self, base_url: str, job_id: str, log_dir: Path) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "HOST": self.config.host,
                "PORT": str(self.config.port),
                "PUBLIC_BASE_URL": _strip_trailing_slash(base_url),
                "TINKER_PLATFORM": "local",
                "TINKER_JOB_ID": job_id,
                "TINKER_LOG_DIR": str(log_dir),
            }
        )
        return env

    def _wait_until_ready(self, base_url: str, process: subprocess.Popen[Any]) -> None:
        deadline = time.monotonic() + self.config.startup_timeout
        last_error: Exception | None = None
        while time.monotonic() <= deadline:
            if process.poll() is not None:
                raise RuntimeError(f"local tinker-backend exited during startup with code {process.poll()}")
            try:
                if self._is_ready(base_url):
                    return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
            time.sleep(self.config.poll_interval)
        if last_error:
            raise TimeoutError(f"local tinker-backend did not become ready at {base_url}: {last_error}")
        raise TimeoutError(f"local tinker-backend did not become ready at {base_url}")

    def _is_ready(self, base_url: str) -> bool:
        with httpx.Client(timeout=3) as client:
            health = client.get(f"{base_url}/healthz")
            health.raise_for_status()
            ready = client.get(f"{base_url}/readyz")
            ready.raise_for_status()
            payload = ready.json()
            return bool(payload.get("ready", True))

    def _write_status(self, log_dir: Path, **payload: Any) -> None:
        payload.setdefault("updated_at", _utcnow())
        if payload.get("status") == "pending":
            payload.setdefault("started_at", _utcnow())
        self._write_json(log_dir / "status.json", payload)

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))
