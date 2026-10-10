from __future__ import annotations

import json
from pathlib import Path

from tinker.server_job import cli_helper
from tinker.server_job.response_types import ServerJobLogsResponse, ServerJobStatusResponse, ServerJobStopResponse


class FakeJob:
    def __init__(self) -> None:
        self.job_id = "local_helper"
        self.log_dir = "/tmp/local_helper"
        self.stop_force_after: float | None = None

    async def status(self) -> ServerJobStatusResponse:
        return ServerJobStatusResponse(
            job_id=self.job_id,
            platform="local",
            status="running",
            base_url="http://127.0.0.1:9000",
            log_dir=self.log_dir,
        )

    async def stop(self, *, force_after_seconds: float | None = None) -> ServerJobStopResponse:
        self.stop_force_after = force_after_seconds
        return ServerJobStopResponse(job_id=self.job_id, status="stopped")

    async def download_logs(self, output_path: str | Path | None = None) -> ServerJobLogsResponse:
        return ServerJobLogsResponse(job_id=self.job_id, log_path=str(output_path or self.log_dir), log_size_bytes=10)

    async def resolve_log_path(self, *, kind: str = "cookbook", runtime_id: str | None = None) -> Path:
        suffix = "cookbook.log" if kind == "cookbook" else f"runtime_{runtime_id}.log"
        return Path(self.log_dir) / suffix


def test_cli_helper_status_stop_resolve_log_and_download(monkeypatch, capsys, tmp_path) -> None:
    fake = FakeJob()

    async def fake_attach(job_id: str):
        assert job_id == "local_helper"
        return fake

    monkeypatch.setattr(cli_helper.ServerJob, "attach", fake_attach)

    assert cli_helper.main(["status", "--job-id", "local_helper"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "running"

    assert cli_helper.main(["resolve-log", "--job-id", "local_helper", "--kind", "runtime", "--runtime-id", "rt_1"]) == 0
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["log_path"].endswith("runtime_rt_1.log")

    out = tmp_path / "logs.tar.gz"
    assert cli_helper.main(["download-logs", "--job-id", "local_helper", "--output", str(out)]) == 0
    downloaded = json.loads(capsys.readouterr().out)
    assert downloaded["log_path"] == str(out)

    assert cli_helper.main(["stop", "--job-id", "local_helper", "--force-after", "4.5"]) == 0
    stopped = json.loads(capsys.readouterr().out)
    assert stopped["status"] == "stopped"
    assert fake.stop_force_after == 4.5
