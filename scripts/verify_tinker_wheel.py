"""Run with the interpreter into which the unified wheel was installed."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.metadata
import os
import socket
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile


def inspect_wheel(wheel: Path) -> None:
    with ZipFile(wheel) as archive:
        names = archive.namelist()
        runtime_configs = [name for name in names if name.startswith("tinker_cookbook/config/") and name.endswith(".yaml")]
        assert all(Path(name).name.startswith(("roll_", "dummy_")) for name in runtime_configs), "Unexpected runtime configs; build from clean sources"
        required = [
            "rock/actions/envs/base.py",
            "tinker/__init__.py",
            "tinker/py.typed",
            "tinker/proto/tinker_public_pb2.pyi",
            "tinker/server_job/platforms/local.py",
            "tinker_backend/rock_config.py",
            "tinker_backend/templates/SWE-bench/job_config.yaml",
            "tinker_cookbook/stores/training_store.py",
            "tinker_cookbook/config/roll_runtime.yaml",
        ]
        required += [f"tinker_cookbook/config/{name}.yaml" for name in (
            "dummy_runtime", "roll_train_runtime",
        )]
        assert not any(name.startswith("tinker_cookbook/config/tinker_backend_cookbook/") for name in names)
        assert not any(name.startswith(("tinker_cookbook/data/", "tinker_cookbook/rock_harbor_bench/")) or name in {"tinker_cookbook/train.py", "tinker_cookbook/eval.py", "tinker_cookbook/tinker_backend_cookbook/test_checkpoint_endpoints.py"} for name in names), "Removed legacy examples present; build from clean sources"
        assert all(name in names for name in required), [name for name in required if name not in names]
        assert not any(Path(name).name == ".env" or name.startswith("tests/") for name in names)
        metadata = archive.read(next(name for name in names if name.endswith(".dist-info/METADATA"))).decode()
        dependencies = [
            line.removeprefix("Requires-Dist: ") for line in metadata.splitlines() if line.startswith("Requires-Dist:")
        ]
        assert not any(dep.lower().startswith("rl-rock==") for dep in dependencies)
        for name in names:
            if name.startswith(("tinker/", "tinker_backend/", "tinker_cookbook/")) and name.endswith((".py", ".yaml", ".jsonl")):
                payload = archive.read(name)
                assert not any(retired in payload for retired in (b"alibaba-inc.com", b"code-agi-sg-docker-registry-vpc", b"rock-instances-registry-vpc")), name
            if name.endswith(".py"):
                compile(archive.read(name), name, "exec")
        print(f"Wheel: {len(names)} entries, all Python sources compile, required resources present")


async def check_installed_backend() -> None:
    import httpx
    from tinker.server_job.config_types import LocalServerJobConfig
    from tinker.server_job.platforms.local import LocalServerJobProvider

    with tempfile.TemporaryDirectory(prefix="rl-rock-wheel-") as scratch:
        os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + str(Path(scratch) / "backend.sqlite3")
        os.environ["TINKER_JOBS_ROOT"] = str(Path(scratch) / "jobs")
        os.environ["XDG_STATE_HOME"] = str(Path(scratch) / "state")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        provider = LocalServerJobProvider(
            LocalServerJobConfig(host="127.0.0.1", port=port, startup_timeout=30, poll_interval=0.1)
        )
        assert provider._repo_path() is None
        assert provider._command() == [sys.executable, "-m", "tinker_backend.main"]
        handle = await provider.submit()
        try:
            async with httpx.AsyncClient() as client:
                for path in ("/healthz", "/readyz", "/openapi.json"):
                    response = await client.get(handle.base_url + path)
                    response.raise_for_status()
                response = await client.get(handle.base_url + "/openapi.json")
                assert "/api/v1/datasets/tasks" in response.json()["paths"] or any(
                    "tasks" in path for path in response.json()["paths"]
                )
            assert Path(scratch, "backend.sqlite3").is_file()
            print("Installed backend: subprocess startup, health, readiness, OpenAPI and SQLite passed")
        finally:
            await provider.stop(handle, force_after_seconds=2)
        assert not provider._process_exists(handle.process_pid)
        print("Installed backend: graceful stop passed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    inspect_wheel(args.wheel)
    for name in ("rock", "tinker", "tinker_backend", "tinker_cookbook"):
        module = importlib.import_module(name)
        locations = [module.__file__] if module.__file__ else list(module.__path__)
        assert all(Path(location).resolve().is_relative_to(Path(sys.prefix).resolve()) for location in locations), locations
    from tinker.proto import tinker_public_pb2
    from tinker_backend.rock_config import load_job_config
    from tinker_cookbook.utils.ml_log import JsonLogger

    message = tinker_public_pb2.SampleResponse()
    assert tinker_public_pb2.SampleResponse.FromString(message.SerializeToString()) == message
    config = load_job_config(
        "SWE-bench",
        model_name="openai/test",
        task_names=["test-task"],
        env={"OPENAI_API_KEY": "test-env", "OPENAI_BASE_URL": ""},
    )
    assert config.agents[0].kwargs["api_key"] == "test-env"
    assert config.datasets[0].task_names == ["test-task"]
    with tempfile.TemporaryDirectory() as scratch:
        logger = JsonLogger(scratch)
        logger.log_metrics({"loss": 1.0}, step=1)
        assert Path(scratch, "metrics.jsonl").is_file()
    print("Installed SDK: protobuf, template configuration and cookbook logging passed")
    asyncio.run(check_installed_backend())


if __name__ == "__main__":
    main()
