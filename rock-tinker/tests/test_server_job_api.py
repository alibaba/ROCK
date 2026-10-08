from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import tinker
from tinker.server_job import server_job as server_job_module
from tinker.server_job.platforms.base import ServerJobHandle
from tinker.server_job.response_types import (
    ServerJobLogsResponse,
    ServerJobStatusResponse,
    ServerJobStopResponse,
)


def test_server_job_open_get_client_caches_and_context_exit_stops(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text("tinker_backend:\n  platform: local\n  backend_config: {}\n", encoding="utf-8")
    calls: list[str] = []

    class FakeProvider:
        async def submit(self) -> ServerJobHandle:
            calls.append("submit")
            return ServerJobHandle(
                job_id="local_20260701T000000_deadbeef",
                platform="local",
                base_url="http://127.0.0.1:49000",
                log_dir=str(tmp_path / "job"),
            )

        async def attach(self, job_id: str) -> ServerJobHandle:
            raise AssertionError("not used")

        async def status(self, handle: ServerJobHandle) -> ServerJobStatusResponse:
            calls.append("status")
            return ServerJobStatusResponse(
                job_id=handle.job_id,
                platform=handle.platform,
                status="running",
                base_url=handle.base_url,
                log_dir=handle.log_dir,
            )

        async def stop(self, handle: ServerJobHandle, *, force_after_seconds: float | None = None) -> ServerJobStopResponse:
            calls.append("stop")
            return ServerJobStopResponse(job_id=handle.job_id, status="stopped")

        async def download_logs(self, handle: ServerJobHandle, output_path: Path | None) -> ServerJobLogsResponse:
            return ServerJobLogsResponse(job_id=handle.job_id, log_path=handle.log_dir)

    class FakeClient:
        def __init__(self, *, base_url: str, api_key: str | None = None) -> None:
            self.base_url = base_url
            self.api_key = api_key

        async def get_server_capabilities_async(self):
            calls.append("ready")
            return object()

        def close(self) -> None:
            calls.append("client_close")

    provider = FakeProvider()
    monkeypatch.setattr(server_job_module, "create_server_job_provider", lambda config: provider)
    monkeypatch.setattr(server_job_module, "TinkerClient", FakeClient)

    async def run() -> None:
        async with tinker.ServerJob.open(config_path=config_path, api_key="tml-dummy") as job:
            assert job.job_id == "local_20260701T000000_deadbeef"
            client1 = await job.get_client()
            client2 = await job.get_client()
            assert client1 is client2
            assert client1.base_url == "http://127.0.0.1:49000"
            assert client1.api_key == "tml-dummy"

    asyncio.run(run())

    assert calls == ["submit", "ready", "stop", "client_close"]


def test_server_job_attach_rejects_metadata_without_platform(monkeypatch, tmp_path) -> None:
    jobs_root = tmp_path / "jobs"
    job_id = "missing_platform"
    job_dir = jobs_root / job_id
    job_dir.mkdir(parents=True)
    job_dir.joinpath("job.json").write_text(
        '{"job_id": "missing_platform", "config": {}}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("TINKER_JOBS_ROOT", str(jobs_root))

    with pytest.raises(ValueError, match="does not declare a platform"):
        asyncio.run(tinker.ServerJob.attach(job_id))
