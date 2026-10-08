from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_runtime_cookbook_entrypoint_help_does_not_shadow_stdlib_platform() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "tinker_cookbook/tinker_backend_cookbook/eval_swe_bench.py",
            "--help",
        ],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--platform" not in result.stdout
    assert "--backend-repo" not in result.stdout


def test_tinker_backend_cookbook_uses_server_job_api() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    cookbook_dir = repo_root / "tinker_cookbook" / "tinker_backend_cookbook"
    scripts = [
        "eval_swe_bench.py",
        "train_swe_bench.py",
        "train_swe_bench_kl.py",
        "train_swe_bench_grpo.py",
    ]

    for script in scripts:
        text = (cookbook_dir / script).read_text(encoding="utf-8")
        assert "ServerJob.open(config_path=args.config_path" in text
        assert "ServiceClient.create" not in text
        assert "backend_client" not in text
        assert "create_service_client_from_config" not in text

    for script in scripts[1:]:
        text = (cookbook_dir / script).read_text(encoding="utf-8")
        assert "--tokenizer-path" not in text
        assert "get_tokenizer(" not in text
