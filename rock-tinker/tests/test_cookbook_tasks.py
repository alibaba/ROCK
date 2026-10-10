from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

import tinker
from tinker_cookbook.tinker_backend_cookbook.env import RemoteSandboxEnv
from tinker_cookbook.tinker_backend_cookbook.tasks import (
    load_tasks,
    write_task_manifest,
)


class RuntimeStub:
    runtime_id = "rt_stub"

    def __init__(self) -> None:
        self.task = None

    async def init_task_env(self, task: tinker.TaskDescriptor) -> str:
        self.task = task
        return "env_stub"


@pytest.mark.asyncio
async def test_explicit_task_id_builds_descriptor_without_local_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_constructed(**kwargs):
        raise AssertionError(f"PublicTaskCatalog should not be constructed: {kwargs}")

    monkeypatch.setattr(tinker, "PublicTaskCatalog", fail_if_constructed)
    args = argparse.Namespace(
        dataset="princeton-nlp/SWE-bench_Verified",
        split="test",
        bench_name="SWE-bench",
        task_id="sympy__sympy-19637",
        task_filter=None,
    )

    tasks = await load_tasks(args)

    assert tasks == [
        tinker.TaskDescriptor(
            task_id="sympy__sympy-19637",
            dataset="princeton-nlp/SWE-bench_Verified",
            split="test",
            bench_name="SWE-bench",
        )
    ]


@pytest.mark.asyncio
async def test_task_filter_uses_local_catalog_before_server_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    selected = tinker.TaskDescriptor(
        task_id="sympy__sympy-19637",
        dataset="princeton-nlp/SWE-bench_Verified",
        split="test",
        bench_name="SWE-bench",
    )

    async def select_tasks(self, **kwargs):
        assert kwargs["task_filter"] == "^sympy__"
        assert kwargs["seed"] == 7
        return [selected]

    monkeypatch.setattr(tinker.PublicTaskCatalog, "select_tasks", select_tasks)
    args = argparse.Namespace(
        dataset="princeton-nlp/SWE-bench_Verified",
        split="test",
        bench_name="SWE-bench",
        task_id="sympy__default",
        task_filter="^sympy__",
        task_seed=7,
        config_path=tmp_path / "unused.yaml",
    )

    assert await load_tasks(args) == [selected]


def test_write_task_manifest_contains_only_selection_and_descriptors(tmp_path: Path) -> None:
    task = tinker.TaskDescriptor(
        task_id="sympy__sympy-19637",
        dataset="princeton-nlp/SWE-bench_Verified",
        split="test",
        bench_name="SWE-bench",
    )
    args = argparse.Namespace(
        dataset=task.dataset,
        split=task.split,
        bench_name=task.bench_name,
        task_id=task.task_id,
        task_filter=None,
        task_seed=None,
    )

    path = write_task_manifest(tmp_path, [task], args)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["version"] == 1
    assert payload["selection"] == {
        "task_id": task.task_id,
        "task_filter": None,
        "seed": None,
    }
    assert payload["tasks"] == [task.model_dump(mode="json")]


@pytest.mark.asyncio
async def test_remote_sandbox_env_accepts_task_descriptor_directly() -> None:
    task = tinker.TaskDescriptor(
        task_id="sympy__sympy-19637",
        dataset="SWE-Env/SWE-Env",
        split="v2_2023pr",
        bench_name="SWE-bench",
    )
    runtime = RuntimeStub()
    env = RemoteSandboxEnv(task=task, runtime=runtime)

    env_id = await env._init_sandbox()

    assert env_id == "env_stub"
    assert runtime.task == task


def test_tinker_backend_cookbook_entrypoints_do_not_load_jsonl_tasks() -> None:
    root = Path(__file__).resolve().parents[1]
    entrypoints = [
        root / "tinker_cookbook" / "tinker_backend_cookbook" / "eval_swe_bench.py",
        root / "tinker_cookbook" / "tinker_backend_cookbook" / "train_swe_bench.py",
        root / "tinker_cookbook" / "tinker_backend_cookbook" / "train_swe_bench_kl.py",
        root / "tinker_cookbook" / "tinker_backend_cookbook" / "train_swe_bench_grpo.py",
    ]
    for entrypoint in entrypoints:
        text = entrypoint.read_text(encoding="utf-8")
        assert "load_harbor_tasks" not in text, entrypoint
        assert "data_path" not in text, entrypoint
        assert "jsonl" not in text.lower(), entrypoint
        assert text.index("await load_one_task(args)") < text.index(
            "tinker.ServerJob.open("
        ), entrypoint
