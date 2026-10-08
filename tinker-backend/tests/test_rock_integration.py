"""Real integration test for rock SDK — no mocks.

Exercises template loading and sandbox execution through RockAdapter.

Scenarios:
  1. load_job_config() with real bench template
  2. RockAdapter._build_job_config() — real rock pipeline
  3. _list_tasks_from_catalog() — real public dataset listing
  4. task_filter regex on real task list
  5. Real sandbox + Harbor job submit (--with-sandbox)

Usage:
  # Scenarios 1-4 (no sandbox, ~10s):
  uv run python tests/test_rock_integration.py

  # Include real sandbox (slow, costs resources):
  uv run python tests/test_rock_integration.py --with-sandbox
"""

from __future__ import annotations

__test__ = False  # Manual credentialed integration runner; not part of the unit suite.

import argparse
import asyncio
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("test_rock_integration")

REQUIRED_ROCK_ENV_KEYS = (
    "ROCK_KEY",
    "OSS_ACCESS_KEY_ID",
    "OSS_ACCESS_KEY_SECRET",
    "OSS_REGION",
    "OSS_ENDPOINT",
    "OSS_BUCKET",
    "OSS_DATASET_PATH",
)


def _missing_env_keys() -> list[str]:
    return [key for key in REQUIRED_ROCK_ENV_KEYS if not os.environ.get(key)]

ADAPTER_CONFIG: dict[str, Any] = {
    "rock": {
        "bench_name": "SWE-bench",
        "model_service_port": 28080,
        "job_wait_timeout_sec": 900,
        "llm": {
            "model_name": "Qwen2.5-72B-Instruct",
            "temperature": 0.8,
            "max_tokens": 4096,
        },
        "agent": {
            "name": "swe-agent-internal",
            "scaffold_config": "anthropic",
            "max_iterations": 100,
            "num_retries": 1,
        },
        "runtime": {
            "task_timeout_sec": 1800,
        },
    }
}

KNOWN_SWE_BENCH_PREFIXES = {"astropy__", "django__", "sympy__", "pytest-dev__", "pylint-dev__"}


def _set_env() -> dict[str, str | None]:
    missing = _missing_env_keys()
    if missing:
        raise RuntimeError(f"Missing required ROCK integration env vars: {', '.join(missing)}")
    return {}


def _restore_env(old: dict[str, str | None]) -> None:
    del old


def _write_config(config: dict) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    yaml.dump(config, f)
    f.close()
    return f.name


def _make_adapter(config: dict | None = None) -> "RockAdapter":
    from tinker_backend.adapters.rock_adapter import RockAdapter

    config_path = _write_config(config or ADAPTER_CONFIG)
    adapter = RockAdapter(
        backend_url="http://localhost:9000",
        runtime_id="rt_integration_test",
        config_path=config_path,
    )
    adapter._tmp_config_path = config_path
    return adapter


def _cleanup_adapter(adapter: "RockAdapter") -> None:
    path = getattr(adapter, "_tmp_config_path", None)
    if path:
        os.unlink(path)


# ── Scenario 1: load_job_config with real rock template ──


def test_load_job_config() -> None:
    print("\n" + "=" * 50)
    print("SCENARIO 1: load_job_config() with real SWE-bench template")
    print("=" * 50)

    from tinker_backend.rock_config import ROCKJobDefaults
    from tinker_backend.rock_config import load_job_config

    defaults = ROCKJobDefaults(
        rock_key=os.environ["ROCK_KEY"],
        openai_api_key="test-key",
        openai_base_url="http://127.0.0.1:28080/v1",
        openai_model="Qwen/Qwen3-4B-Instruct-2507",
    )
    cfg = ROCKJobDefaults.from_env(defaults=defaults)

    config = load_job_config(
        "SWE-bench",
        default_config=cfg,
        task_names=["sympy__sympy-19637"],
        model_name="openai/Qwen2.5-72B-Instruct",
        env={
            "OPENAI_BASE_URL": "http://127.0.0.1:28080/v1",
            "OPENAI_API_KEY": "test-env-id",
        },
        **{
            "environment.env.TASK_ID": "sympy__sympy-19637",
            "environment.env.DATASET": "princeton-nlp/SWE-bench_Verified",
            "environment.env.SPLIT": "test",
        },
    )

    assert config is not None, "load_job_config returned None"
    assert config.datasets, "No datasets in config"
    assert config.datasets[0].task_names == ["sympy__sympy-19637"]
    assert config.agents, "No agents in config"
    assert config.agents[0].name, "agent name should be set by rock template"
    assert config.environment is not None, "No environment in config"
    assert config.environment.xrl_authorization == os.environ["ROCK_KEY"], (
        f"xrl_authorization mismatch: {config.environment.xrl_authorization}"
    )

    env_dict = config.environment.env or {}
    assert env_dict.get("TASK_ID") == "sympy__sympy-19637"
    assert env_dict.get("DATASET") == "princeton-nlp/SWE-bench_Verified"
    assert env_dict.get("SPLIT") == "test"
    assert env_dict.get("OPENAI_API_KEY") == "test-env-id"
    assert "OSS_ACCESS_KEY_ID" in env_dict, "OSS credentials should be injected"

    print(f"  [JobConfig]      : agents={config.agents[0].name}, task={config.datasets[0].task_names[0]}")
    print(f"  [Environment]    : image={getattr(config.environment, 'image', '?')}")
    print(f"  [Cluster]        : {getattr(config.environment, 'cluster', '?')}")
    print(f"  [xrl_auth]       : {config.environment.xrl_authorization[:10]}...")
    print(f"  [Env keys]       : {sorted(env_dict.keys())}")
    print("  ✓ SCENARIO 1 PASSED")


# ── Scenario 2: RockAdapter._build_job_config (rock, no agent post-processing) ──


def test_adapter_build_config_rock() -> None:
    print("\n" + "=" * 50)
    print("SCENARIO 2: RockAdapter._build_job_config() — rock path, template passthrough")
    print("=" * 50)

    adapter = _make_adapter()
    try:
        job_config = adapter._build_job_config(
            task_id="sympy__sympy-19637",
            dataset="princeton-nlp/SWE-bench_Verified",
            split="test",
            env_id="env_test_001",
            job_id="tinker_integration_test_001",
        )

        assert job_config is not None
        assert job_config.job_name == "tinker_integration_test_001"
        assert job_config.experiment_id == "rt_integration_test"
        assert job_config.agents, "No agents — template should define them"
        assert job_config.agents[0].name, "agent name should come from rock template"
        assert job_config.agents[0].model_name == "openai/Qwen2.5-72B-Instruct"
        assert job_config.datasets
        assert job_config.datasets[0].task_names == ["sympy__sympy-19637"]
        assert job_config.environment is not None

        env_dict = job_config.environment.env or {}
        assert env_dict["TASK_ID"] == "sympy__sympy-19637"
        assert env_dict["DATASET"] == "princeton-nlp/SWE-bench_Verified"
        assert env_dict["SPLIT"] == "test"
        assert env_dict["OPENAI_API_KEY"] == "env_test_001"

        print(f"  [JobConfig]      : agents={job_config.agents[0].name}, model={job_config.agents[0].model_name}")
        print(f"  [Environment]    : image={getattr(job_config.environment, 'image', '?')}")
        print(f"  [Env keys]       : {sorted(env_dict.keys())}")
        print("  ✓ SCENARIO 2 PASSED")
    finally:
        _cleanup_adapter(adapter)


# ── Scenario 6: Legacy path — _build_job_config_legacy ──


def _make_legacy_job_config_yaml() -> str:
    """Create a Harbor job_config.yaml from the rock SWE-bench template.

    Used as the base YAML for _build_job_config_legacy.  The legacy code
    replaces agents/datasets/env so the template's agent name doesn't matter.
    """
    import json as _json
    from tinker_backend.rock_config import ROCKJobDefaults
    from tinker_backend.rock_config import load_job_config

    defaults = ROCKJobDefaults(
        rock_key=os.environ.get("ROCK_KEY", ""),
        openai_api_key="placeholder",
        openai_base_url="http://127.0.0.1:28080/v1",
        openai_model="Qwen2.5-72B-Instruct",
    )
    cfg = ROCKJobDefaults.from_env(defaults=defaults)
    config = load_job_config(
        "SWE-bench",
        default_config=cfg,
        task_names=["placeholder"],
        model_name="openai/Qwen2.5-72B-Instruct",
    )
    d = _json.loads(config.model_dump_json())
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    yaml.dump(d, f)
    f.close()
    return f.name


LEGACY_ADAPTER_CONFIG: dict[str, Any] = {
    "rock": {
        "model_service_port": 28080,
        "job_wait_timeout_sec": 900,
        "llm": {
            "model_name": "Qwen2.5-72B-Instruct",
            "temperature": 0.8,
            "max_tokens": 4096,
        },
        "agent": {
            "name": "swe-agent-internal",
            "scaffold_config": "anthropic",
            "max_iterations": 100,
            "num_retries": 1,
        },
        "runtime": {
            "task_timeout_sec": 1800,
        },
    }
}


def test_adapter_build_config_legacy() -> None:
    print("\n" + "=" * 50)
    print("SCENARIO 6: RockAdapter._build_job_config() — legacy path (job_config_path YAML)")
    print("=" * 50)

    job_config_yaml = _make_legacy_job_config_yaml()
    legacy_config = {**LEGACY_ADAPTER_CONFIG}
    legacy_config["rock"] = {**legacy_config["rock"], "job_config_path": job_config_yaml}

    adapter = _make_adapter(legacy_config)
    try:
        assert adapter.bench_name is None, "bench_name should be None for legacy path"

        job_config = adapter._build_job_config(
            task_id="sympy__sympy-19637",
            dataset="princeton-nlp/SWE-bench_Verified",
            split="test",
            env_id="env_legacy_001",
            job_id="tinker_legacy_test_001",
        )

        assert job_config is not None
        assert job_config.job_name == "tinker_legacy_test_001"
        assert job_config.agents, "No agents"
        assert job_config.agents[0].model_name == "openai/Qwen2.5-72B-Instruct"
        assert job_config.agents[0].kwargs["api_key"] == "env_legacy_001"
        assert job_config.agents[0].kwargs["api_base"] == ""
        assert job_config.agents[0].kwargs["max_iterations"] == 100
        assert job_config.agents[0].kwargs["temperature"] == 0.8
        assert job_config.agents[0].kwargs["max_tokens"] == 4096
        assert job_config.agents[0].max_timeout_sec == 1800

        env_dict = job_config.environment.env or {}
        assert env_dict["TASK_ID"] == "sympy__sympy-19637"
        assert env_dict["DATASET"] == "princeton-nlp/SWE-bench_Verified"
        assert env_dict["SPLIT"] == "test"

        print(f"  [JobConfig]      : agents={job_config.agents[0].name}, model={job_config.agents[0].model_name}")
        print(f"  [Environment]    : image={getattr(job_config.environment, 'image', '?')}")
        print(f"  [Agent kwargs]   : api_key={job_config.agents[0].kwargs['api_key']}, max_iter={job_config.agents[0].kwargs['max_iterations']}")
        print(f"  [Env keys]       : {sorted(env_dict.keys())}")
        print("  ✓ SCENARIO 6 PASSED")
    finally:
        os.unlink(job_config_yaml)
        _cleanup_adapter(adapter)


# ── Scenario 7: Legacy path — real sandbox ──


async def test_real_sandbox_legacy(task_id: str) -> None:
    """Legacy path: same as Scenario 5 but using job_config_path YAML instead of bench_name."""
    print("\n" + "=" * 50)
    print(f"SCENARIO 7: Real ROCK sandbox — Legacy path (instance={task_id})")
    print("=" * 50)

    from rock.sdk.job import Job
    from tinker_backend.adapters.rock_adapter import ModelServiceOperator

    LLM_BASE_URL = "https://offline-whale-wave.alibaba-inc.com/api/v2/services/aigc/text-generation/v1/chat/completions"
    LLM_API_KEY = "ISQD5CV063"

    job_config_yaml = _make_legacy_job_config_yaml()
    sandbox_legacy_config: dict[str, Any] = {
        "rock": {
            **LEGACY_ADAPTER_CONFIG["rock"],
            "job_config_path": job_config_yaml,
            "agent": {
                **LEGACY_ADAPTER_CONFIG["rock"]["agent"],
                "name": "swe-agent",
            },
        }
    }
    adapter = _make_adapter(sandbox_legacy_config)

    try:
        assert adapter.bench_name is None

        job_config = adapter._build_job_config(
            task_id=task_id,
            dataset="princeton-nlp/SWE-bench_Verified",
            split="test",
            env_id=LLM_API_KEY,
            job_id="tinker_legacy_sandbox_001",
        )

        model_service_url = f"http://127.0.0.1:{adapter.model_service_port}/v1"
        job_config.agents[0].kwargs = {
            "api_key": LLM_API_KEY,
            "api_base": model_service_url,
        }
        env_dict = job_config.environment.env or {}
        env_dict["OPENAI_BASE_URL"] = model_service_url
        env_dict["OPENAI_API_KEY"] = LLM_API_KEY
        job_config.environment.env = env_dict

        print(f"  [JobConfig]      : agents={job_config.agents[0].name}, model={job_config.agents[0].model_name}")
        print(f"  [Environment]    : image={getattr(job_config.environment, 'image', '?')}")
        print(f"  [Cluster]        : {getattr(job_config.environment, 'cluster', '?')}")
        print(f"  [LLM endpoint]   : {LLM_BASE_URL}")
        print(f"  [Task]           : {[task_id]}")

        operator = ModelServiceOperator(
            model_service_port=adapter.model_service_port,
            model_service_install_cmd=(
                "pip install 'rl-rock' fastapi uvicorn psutil 'openai>=1.50.0' httpx"
                " -i https://mirrors.aliyun.com/pypi/simple/"
                " --no-extra-index-url"
                " --trusted-host mirrors.aliyun.com --timeout 600"
            ),
            model_service_install_timeout=900,
        )

        job = Job(config=job_config, operator=operator)
        print("  Submitting job (creates a real ROCK sandbox)...")
        started = time.monotonic()

        try:
            await job.submit()
            submit_elapsed = time.monotonic() - started

            trial_client = job._job_client.trials[0]
            trial = trial_client.trial
            sandbox = getattr(trial_client, "sandbox", None)
            sandbox_id = getattr(sandbox, "sandbox_id", "unknown") if sandbox else "unknown"
            model_service = trial.model_service
            harbor_pid = str(trial_client.pid)

            print(f"  ✓ Job submitted in {submit_elapsed:.1f}s")
            print(f"  [sandbox_id]     : {sandbox_id}")
            print(f"  [harbor_pid]     : {harbor_pid}")

            assert model_service is not None, "ModelService not initialised"

            print("  Starting watch_agent and inference loop...")
            asyncio.ensure_future(model_service.watch_agent(pid=harbor_pid))

            num_turns = await _inference_loop(
                model_service, LLM_BASE_URL, LLM_API_KEY,
                llm_model="Qwen2.5-72B-Instruct-Chatflow",
            )

            print(f"  ✓ Inference loop done: {num_turns} LLM turn(s)")

            print("  Waiting for Harbor result...")
            result = await job.wait()
            total_elapsed = time.monotonic() - started

            print(f"  ✓ Agent completed in {total_elapsed:.1f}s")
            print(f"  [result type]    : {type(result).__name__}")

            assert result.trial_results, "No trial results"
            tr = result.trial_results[0]
            print(f"  [exit_code]      : {tr.exit_code}")
            print(f"  [reward]         : {tr.verifier_result.rewards if tr.verifier_result else '?'}")
            assert tr.exit_code == 0, f"Trial exit_code={tr.exit_code}"
        except Exception as exc:
            elapsed = time.monotonic() - started
            logger.error("  Job failed after %.1fs: %s", elapsed, exc)
            raise
        finally:
            try:
                await job.cancel()
            except Exception:
                pass

        print("  ✓ SCENARIO 7 PASSED")
    finally:
        os.unlink(job_config_yaml)
        _cleanup_adapter(adapter)


# ── Scenario 3: _list_tasks_from_catalog with real OSS ──


def test_list_tasks_from_catalog() -> None:
    print("\n" + "=" * 50)
    print("SCENARIO 3: _list_tasks_from_catalog() — real public dataset listing")
    print("=" * 50)

    adapter = _make_adapter()
    try:
        started = time.monotonic()
        task_ids = adapter._list_tasks_from_catalog(
            "princeton-nlp/SWE-bench_Verified", "test"
        )
        elapsed = time.monotonic() - started

        assert isinstance(task_ids, list)
        assert len(task_ids) > 0, "No task IDs returned from OSS"
        assert all(isinstance(tid, str) and tid for tid in task_ids), "task IDs should be non-empty strings"

        # spot-check: SWE-bench_Verified has well-known instances (mirrors ROLL Scenario 5)
        has_known = any(any(t.startswith(p) for p in KNOWN_SWE_BENCH_PREFIXES) for t in task_ids)
        assert has_known, f"Expected well-known SWE-bench task prefixes in {task_ids[:5]}"

        assert "sympy__sympy-19637" in task_ids, "sympy__sympy-19637 should be in SWE-bench_Verified"

        print(f"  [_list_tasks_from_catalog] : {len(task_ids)} tasks, first 5: {task_ids[:5]}")
        print(f"  [Elapsed]             : {elapsed:.2f}s")
        print("  ✓ _list_tasks_from_catalog verified")
        print("  ✓ SCENARIO 3 PASSED")
    finally:
        _cleanup_adapter(adapter)


# ── Scenario 4: task_filter on real data ──


def test_task_filter_on_real_data() -> None:
    print("\n" + "=" * 50)
    print("SCENARIO 4: task_filter regex on real OSS task list")
    print("=" * 50)

    def _make_filter_adapter(task_filter: str) -> "RockAdapter":
        cfg = {
            "rock": {
                **ADAPTER_CONFIG["rock"],
                "datasets": [
                    {
                        "dataset": "princeton-nlp/SWE-bench_Verified",
                        "split": "test",
                        "bench_name": "SWE-bench",
                        "task_filter": task_filter,
                    },
                ],
            }
        }
        return _make_adapter(cfg)

    # 4a: prefix filter
    adapter = _make_filter_adapter("^sympy__")
    try:
        results = adapter.list_dataset_tasks()
        assert len(results) > 0, "task_filter='^sympy__' returned 0 results"
        assert all(r["task_id"].startswith("sympy__") for r in results)
        assert all(r["bench_name"] == "SWE-bench" for r in results)
        print(f"  [^sympy__]        : {len(results)} tasks")
    finally:
        _cleanup_adapter(adapter)

    # 4b: exact match
    adapter = _make_filter_adapter("^sympy__sympy-19637$")
    try:
        results = adapter.list_dataset_tasks()
        assert len(results) == 1
        assert results[0]["task_id"] == "sympy__sympy-19637"
        print(f"  [^sympy__sympy-19637$] : {len(results)} task (exact)")
    finally:
        _cleanup_adapter(adapter)

    # 4c: multi-project filter
    adapter = _make_filter_adapter("^django__|^sympy__")
    try:
        results = adapter.list_dataset_tasks()
        has_django = any(r["task_id"].startswith("django__") for r in results)
        has_sympy = any(r["task_id"].startswith("sympy__") for r in results)
        assert has_django, "Multi-project filter missing django tasks"
        assert has_sympy, "Multi-project filter missing sympy tasks"
        non_matching = [r["task_id"] for r in results
                        if not (r["task_id"].startswith("django__") or r["task_id"].startswith("sympy__"))]
        assert not non_matching, f"Filter leakage: {non_matching[:5]}"
        print(f"  [^django__|^sympy__] : {len(results)} tasks (multi-project)")
    finally:
        _cleanup_adapter(adapter)

    print("  ✓ SCENARIO 4 PASSED")


# ── Scenario 5: Real sandbox + Harbor job ──


async def _inference_loop(
    model_service: Any,
    llm_base_url: str,
    llm_api_key: str,
    llm_model: str | None = None,
) -> int:
    """Drive the LLM inference loop via anti_call_llm (file-based protocol).

    Returns the number of LLM turns completed.
    """
    import httpx

    current_index = 0
    response_payload: str | None = None

    async with httpx.AsyncClient(timeout=600.0) as client:
        while True:
            raw_output = await model_service.anti_call_llm(
                index=current_index,
                response_payload=response_payload,
            )
            raw_stripped = raw_output.strip() if raw_output else ""

            if not raw_stripped:
                await asyncio.sleep(0.5)
                continue

            if "SESSION_END" in raw_stripped:
                print(f"    [inference] SESSION_END after {current_index} turn(s)")
                break

            import json as _json
            json_start = raw_stripped.find("{")
            if json_start < 0:
                print(f"    [inference] no JSON found at turn {current_index + 1}: {raw_stripped[:200]}")
                response_payload = _json.dumps({
                    "id": "error", "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "[no JSON in request]"}, "finish_reason": "stop"}],
                })
                current_index += 1
                continue
            json_body = raw_stripped[json_start:]
            if json_start > 0:
                print(f"    [inference] skipped {json_start} chars of non-JSON prefix")
            try:
                request_dict = _json.loads(json_body)
            except Exception as parse_err:
                print(f"    [inference] JSON parse error at turn {current_index + 1}: {parse_err}")
                print(f"    [inference] json_body[:300]: {json_body[:300]}")
                response_payload = _json.dumps({
                    "id": "error", "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "[parse error]"}, "finish_reason": "stop"}],
                })
                current_index += 1
                continue

            if llm_model:
                request_dict["model"] = llm_model
            model = request_dict.get("model", "?")
            messages = request_dict.get("messages", [])
            n_messages = len(messages)
            print(f"    [inference] turn {current_index + 1}: model={model}, messages={n_messages}")
            for i, msg in enumerate(messages):
                role = msg.get("role", "?")
                content = msg.get("content", "")
                if isinstance(content, list):
                    parts = [p.get("text", str(p)) if isinstance(p, dict) else str(p) for p in content]
                    content_str = " | ".join(parts)
                else:
                    content_str = str(content) if content else ""
                snippet = (content_str[:300] + "...") if len(content_str) > 300 else content_str
                snippet = snippet.replace("\n", "\\n")
                print(f"      msg[{i}] role={role} len={len(content_str)}: {snippet}")

            try:
                resp = await client.post(
                    llm_base_url,
                    json=request_dict,
                    headers={"Authorization": f"Bearer {llm_api_key}"},
                )
                resp.raise_for_status()
            except Exception as llm_err:
                resp_text = getattr(resp, "text", "") if "resp" in dir() else ""
                print(f"    [inference] LLM error: {llm_err}")
                print(f"    [inference] response body: {resp_text[:500]}")
                print(f"    [inference] request keys: {list(request_dict.keys())}")
                raise

            response_payload = resp.text
            current_index += 1
            print(f"    [inference] turn {current_index} done")

    return current_index


async def test_real_sandbox(task_id: str) -> None:
    """Pull mode: drive inference loop via anti_call_llm, proxy to real LLM.

    Mirrors ROLL PullModeRunner + RockAdapter._handle_init_task_env flow.
    """
    print("\n" + "=" * 50)
    print(f"SCENARIO 5: Real ROCK sandbox + Harbor job — Pull mode (instance={task_id})")
    print("=" * 50)

    from rock.sdk.job import Job
    from tinker_backend.adapters.rock_adapter import ModelServiceOperator

    LLM_BASE_URL = "https://offline-whale-wave.alibaba-inc.com/api/v2/services/aigc/text-generation/v1/chat/completions"
    LLM_API_KEY = "ISQD5CV063"

    adapter = _make_adapter()
    try:
        job_config = adapter._build_job_config(
            task_id=task_id,
            dataset="princeton-nlp/SWE-bench_Verified",
            split="test",
            env_id=LLM_API_KEY,
            job_id="tinker_sandbox_test_001",
        )

        env_dict = job_config.environment.env or {}
        env_dict["OPENAI_BASE_URL"] = LLM_BASE_URL
        env_dict["OPENAI_API_KEY"] = LLM_API_KEY
        job_config.environment.env = env_dict

        job_config.agents[0].kwargs["api_base"] = f"http://127.0.0.1:{adapter.model_service_port}/v1"
        job_config.agents[0].kwargs["api_key"] = LLM_API_KEY

        print(f"  [JobConfig]      : agents={job_config.agents[0].name}, model={job_config.agents[0].model_name}")
        print(f"  [Environment]    : image={getattr(job_config.environment, 'image', '?')}")
        print(f"  [Cluster]        : {getattr(job_config.environment, 'cluster', '?')}")
        print(f"  [LLM endpoint]   : {LLM_BASE_URL}")
        print(f"  [Task]           : {job_config.datasets[0].task_names}")

        operator = ModelServiceOperator(
            model_service_port=adapter.model_service_port,
            model_service_install_cmd=(
                "pip install 'rl-rock' fastapi uvicorn psutil 'openai>=1.50.0' httpx"
                " -i https://mirrors.aliyun.com/pypi/simple/"
                " --no-extra-index-url"
                " --trusted-host mirrors.aliyun.com --timeout 600"
            ),
            model_service_install_timeout=900,
        )

        job = Job(config=job_config, operator=operator)
        print("  Submitting job (creates a real ROCK sandbox)...")
        started = time.monotonic()

        try:
            await job.submit()
            submit_elapsed = time.monotonic() - started

            trial_client = job._job_client.trials[0]
            trial = trial_client.trial
            sandbox = getattr(trial_client, "sandbox", None)
            sandbox_id = getattr(sandbox, "sandbox_id", "unknown") if sandbox else "unknown"
            model_service = trial.model_service
            harbor_pid = str(trial_client.pid)

            print(f"  ✓ Job submitted in {submit_elapsed:.1f}s")
            print(f"  [sandbox_id]     : {sandbox_id}")
            print(f"  [harbor_pid]     : {harbor_pid}")

            assert model_service is not None, "ModelService not initialised"

            print("  Starting watch_agent and inference loop...")
            asyncio.ensure_future(model_service.watch_agent(pid=harbor_pid))

            num_turns = await _inference_loop(
                model_service, LLM_BASE_URL, LLM_API_KEY,
                llm_model="Qwen2.5-72B-Instruct-Chatflow",
            )

            print(f"  ✓ Inference loop done: {num_turns} LLM turn(s)")

            print("  Waiting for Harbor result...")
            result = await job.wait()
            total_elapsed = time.monotonic() - started

            print(f"  ✓ Agent completed in {total_elapsed:.1f}s")
            print(f"  [result type]    : {type(result).__name__}")
            print(f"  [result]         : {result}")

            # Read trajectory from sandbox before cleanup
            if result.trial_results:
                trial_name = result.trial_results[0].trial_name
                traj_path = f"/data/logs/user-defined/jobs/tinker_sandbox_test_001/{trial_name}/agent/trajectory.json"
                print(f"\n  --- Reading trajectory from sandbox: {traj_path} ---")
                try:
                    from rock.sdk.sandbox.client import ReadFileRequest
                    traj_resp = await sandbox.read_file(ReadFileRequest(path=traj_path))
                    traj_content = traj_resp.content if hasattr(traj_resp, 'content') else str(traj_resp)
                    import json as _json
                    try:
                        traj_data = _json.loads(traj_content)
                        print(f"  [trajectory keys]: {list(traj_data.keys()) if isinstance(traj_data, dict) else type(traj_data).__name__}")
                        # Print each step summary
                        steps = traj_data if isinstance(traj_data, list) else traj_data.get("steps", traj_data.get("history", []))
                        if isinstance(steps, list):
                            print(f"  [trajectory steps]: {len(steps)}")
                            for i, step in enumerate(steps):
                                if isinstance(step, dict):
                                    role = step.get("role", step.get("type", "?"))
                                    content = step.get("content", step.get("message", step.get("action", "")))
                                    if isinstance(content, str):
                                        snippet = content[:300].replace("\n", "\\n")
                                    else:
                                        snippet = str(content)[:300]
                                    print(f"    step[{i}] role={role}: {snippet}")
                                else:
                                    print(f"    step[{i}]: {str(step)[:200]}")
                        else:
                            # Just dump first 3000 chars
                            print(f"  [trajectory dump]: {_json.dumps(traj_data, indent=2, ensure_ascii=False)[:3000]}")
                    except _json.JSONDecodeError:
                        print(f"  [trajectory raw]: {traj_content[:3000]}")
                except Exception as traj_err:
                    print(f"  [trajectory read error]: {traj_err}")
        except Exception as exc:
            elapsed = time.monotonic() - started
            logger.error("  Job failed after %.1fs: %s", elapsed, exc)
            raise
        finally:
            try:
                await job.cancel()
            except Exception:
                pass

        print("  ✓ SCENARIO 5 PASSED")
    finally:
        _cleanup_adapter(adapter)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Real rock integration tests")
    parser.add_argument("--with-sandbox", action="store_true", help="Include real sandbox test (slow)")
    parser.add_argument("--task-id", default="sympy__sympy-19637")
    parser.add_argument("--skip-oss", action="store_true", help="Skip OSS tests (scenarios 3-4)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    old_env = _set_env()
    passed = 0
    failed = 0
    errors: list[str] = []

    tests: list[tuple[str, Any]] = [
        ("Scenario 1: load_job_config", test_load_job_config),
        ("Scenario 2: adapter build config (rock)", test_adapter_build_config_rock),
        ("Scenario 6: adapter build config (legacy)", test_adapter_build_config_legacy),
    ]
    if not args.skip_oss:
        tests.append(("Scenario 3: list tasks from OSS", test_list_tasks_from_catalog))
        tests.append(("Scenario 4: task_filter on real data", test_task_filter_on_real_data))

    try:
        for name, test_fn in tests:
            try:
                test_fn()
                passed += 1
            except Exception as exc:
                failed += 1
                errors.append(f"{name}: {exc}")
                logger.error("%s FAILED: %s", name, exc, exc_info=True)

        if args.with_sandbox:
            try:
                asyncio.run(test_real_sandbox(args.task_id))
                passed += 1
            except Exception as exc:
                failed += 1
                errors.append(f"Scenario 5 (sandbox rock): {exc}")
                logger.error("Scenario 5 (sandbox rock) FAILED: %s", exc, exc_info=True)

            try:
                asyncio.run(test_real_sandbox_legacy(args.task_id))
                passed += 1
            except Exception as exc:
                failed += 1
                errors.append(f"Scenario 7 (sandbox legacy): {exc}")
                logger.error("Scenario 7 (sandbox legacy) FAILED: %s", exc, exc_info=True)
    finally:
        _restore_env(old_env)

    print()
    print("=" * 50)
    print(f"RESULTS: {passed} passed, {failed} failed")
    if errors:
        for e in errors:
            print(f"  FAIL: {e}")
    print("=" * 50)

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
