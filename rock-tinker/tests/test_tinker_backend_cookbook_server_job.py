from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from tinker_cookbook.tinker_backend_cookbook.server_job_marker import emit_server_job_marker


def test_tinker_backend_cookbook_entrypoint_help_has_no_backend_connection_flags() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    entrypoints = [
        "eval_swe_bench.py",
        "train_swe_bench.py",
        "train_swe_bench_kl.py",
        "train_swe_bench_grpo.py",
    ]

    for entrypoint in entrypoints:
        result = subprocess.run(
            [sys.executable, f"tinker_cookbook/tinker_backend_cookbook/{entrypoint}", "--help"],
            cwd=repo_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert "--base-url" not in result.stdout
        assert "--platform" not in result.stdout
        assert "--backend-repo" not in result.stdout


def test_tinker_backend_cookbook_uses_server_job_not_service_client() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    cookbook_dir = repo_root / "tinker_cookbook" / "tinker_backend_cookbook"
    runtime_scripts = [
        "eval_swe_bench.py",
        "train_swe_bench.py",
        "train_swe_bench_kl.py",
        "train_swe_bench_grpo.py",
    ]

    for script in runtime_scripts:
        text = (cookbook_dir / script).read_text(encoding="utf-8")
        assert "ServerJob.open(config_path=args.config_path" in text
        assert "ServiceClient.create" not in text
        assert "service_client" not in text


def test_server_job_marker_exposes_provider_metadata(capsys) -> None:
    emit_server_job_marker(
        SimpleNamespace(
            job_id="local_20260720_deadbeef",
            platform="local",
            provider_job_id="0123456789abcdef0123456789abcdef",
            base_url="https://gateway.example/apis/tinker/v1/job-tinker-api/9000",
            log_dir="/root/.rock/tinker/jobs/local_20260720_deadbeef",
        )
    )

    output = capsys.readouterr().out
    assert '"job_id": "local_20260720_deadbeef"' in output
    assert '"platform": "local"' in output
    assert '"provider_job_id": "0123456789abcdef0123456789abcdef"' in output
