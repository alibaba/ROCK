"""Public local operator configuration, without starting sandboxes or inference."""
from __future__ import annotations

import sys
from types import ModuleType

import pytest
import yaml

from rock.sdk.job.operator import Operator
from rock.sdk.bench.models.job.config import LocalDatasetConfig
from tinker_backend.adapters.rock_adapter import ModelServiceOperator, RockAdapter
from tinker_backend.rock_config import ROCKJobDefaults


def adapter(rock=None):
    value = RockAdapter.__new__(RockAdapter)
    value.adapter_config = {"rock": rock or {}}
    value.model_service_port = 28080
    value.model_service_install_cmd = "configured-install"
    value.model_service_install_timeout = 42
    return value


class PublicTestOperator(Operator):
    def __init__(self, port):
        self.port = port

    def apply(self, config):
        return []


@pytest.fixture
def factory_module(monkeypatch):
    module = ModuleType("public_local_factory_fixture")
    module.PublicOperator = PublicTestOperator
    module.NotOperator = dict
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module.__name__


def test_default_operator_preserves_original_configuration():
    result = adapter()._make_operator()
    assert isinstance(result, ModelServiceOperator)
    assert result._model_service_port == 28080
    assert result._model_service_install_cmd == "configured-install"
    assert result._model_service_install_timeout == 42


def test_explicit_factory_passes_only_configured_kwargs(factory_module):
    result = adapter({"operator_factory": factory_module + ":PublicOperator",
                      "operator_kwargs": {"port": 28765}})._make_operator()
    assert isinstance(result, Operator)
    assert isinstance(result, PublicTestOperator)
    assert result.port == 28765


@pytest.mark.parametrize("factory", ["missing_separator", ":PublicOperator", "module:", "module:Class:extra"])
def test_malformed_factory_rejects(factory):
    with pytest.raises(ValueError):
        adapter({"operator_factory": factory})._make_operator()


def test_factory_result_must_be_operator(factory_module):
    with pytest.raises(TypeError):
        adapter({"operator_factory": factory_module + ":NotOperator",
                 "operator_kwargs": {}})._make_operator()


def test_nonmapping_factory_kwargs_reject(factory_module):
    with pytest.raises((ValueError, TypeError)):
        adapter({"operator_factory": factory_module + ":PublicOperator",
                 "operator_kwargs": ["invalid"]})._make_operator()


def test_unknown_factory_module_surfaces_import_error():
    with pytest.raises(ImportError):
        adapter({"operator_factory": "no_such_public_operator_module:Class"})._make_operator()


def test_local_task_template_retains_path_and_public_agent(tmp_path):
    job_path = tmp_path / "public-job.yaml"
    job_path.write_text(yaml.safe_dump({
        "experiment_id": "public-local", "environment": {
            "base_url": "http://127.0.0.1:18080", "cluster": "local",
            "image": "public-fixture:local", "env": {}},
        "datasets": [{"path": "/opt/public/harbor-tasks", "task_names": ["old-task"]}],
        "agents": [{"name": None, "import_path": "public_swe_agent:PreinstalledSweAgent",
                    "model_name": "openai/public-model", "kwargs": {}}],
    }))
    value = adapter({"bench_name": "SWE-bench", "job_config_path": str(job_path),
                     "llm": {"model_name": "public-model"}})
    value.bench_name = "SWE-bench"
    value.job_config_path = str(job_path)
    value._job_config_overrides = {}
    value._agent_max_iterations = None
    value._job_defaults = ROCKJobDefaults()
    value.runtime_id = "rt-public-fixture"
    result = value._build_job_config("public-task", "public-dataset", "test", "env-fixture", "job-fixture")
    assert isinstance(result.datasets[0], LocalDatasetConfig)
    assert str(result.datasets[0].path) == "/opt/public/harbor-tasks"
    assert result.datasets[0].task_names == ["public-task"]
    assert result.agents[0].import_path == "public_swe_agent:PreinstalledSweAgent"
    assert result.agents[0].kwargs["api_key"] == "env-fixture"


def test_finished_env_cleanup_success_removes_registration():
    import asyncio
    from unittest.mock import AsyncMock
    value = adapter()
    live = object()
    value.live_envs = {"env-fixture": live}
    value._close_live_env = AsyncMock(return_value=[])
    asyncio.run(value._close_finished_env("env-fixture", live))
    value._close_live_env.assert_awaited_once_with("env-fixture", live, cancel_job=False)
    assert "env-fixture" not in value.live_envs


def test_finished_env_cleanup_errors_retain_registration_for_retry():
    import asyncio
    from unittest.mock import AsyncMock
    value = adapter()
    live = object()
    value.live_envs = {"env-fixture": live}
    value._close_live_env = AsyncMock(return_value=["owned sandbox stop failed"])
    asyncio.run(value._close_finished_env("env-fixture", live))
    assert value.live_envs["env-fixture"] is live


def test_finished_env_cleanup_timeout_retains_registration_without_wait(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    value = adapter()
    live = object()
    value.live_envs = {"env-fixture": live}
    value._close_live_env = AsyncMock(return_value=[])
    observed = []

    async def immediate_timeout(awaitable, timeout):
        observed.append(timeout)
        awaitable.close()
        raise TimeoutError("simulated owned sandbox timeout")

    monkeypatch.setattr("tinker_backend.adapters.rock_adapter.asyncio.wait_for", immediate_timeout)
    asyncio.run(value._close_finished_env("env-fixture", live))
    assert observed == [90]
    assert value.live_envs["env-fixture"] is live


def cleanup_fixture():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    sandbox = SimpleNamespace(sandbox_id="owned-fixture", close=AsyncMock())
    service = SimpleNamespace(stop=AsyncMock())
    job = SimpleNamespace(cancel=AsyncMock(),
        _job_client=SimpleNamespace(trials=[SimpleNamespace(sandbox=sandbox)]))
    live = SimpleNamespace(job=job, model_service=service)
    return live, job, service, sandbox


def test_completed_job_cleanup_skips_cancel_but_closes_service_and_sandbox():
    import asyncio
    value = adapter()
    live, job, service, sandbox = cleanup_fixture()
    value.live_envs = {"env-fixture": live}
    asyncio.run(value._close_finished_env("env-fixture", live))
    job.cancel.assert_not_awaited()
    service.stop.assert_awaited_once()
    sandbox.close.assert_awaited_once()
    assert "env-fixture" not in value.live_envs


def test_regular_shutdown_still_cancels_job_and_closes_owned_resources():
    import asyncio
    value = adapter()
    live, job, service, sandbox = cleanup_fixture()
    value.live_envs = {"env-fixture": live}
    value.pending_jobs = {}
    value._tasks = set()
    value._http = None
    result = asyncio.run(value.shutdown(close_http=False))
    job.cancel.assert_awaited_once()
    service.stop.assert_awaited_once()
    sandbox.close.assert_awaited_once()
    assert not value.live_envs
    assert result["closed_env_ids"] == ["env-fixture"]
    assert not result.get("errors")


@pytest.mark.parametrize("branch", ["deliver", "init"])
def test_terminal_publication_waits_for_completed_cleanup(branch, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    async def run():
        value = adapter()
        value.live_envs = {}
        value.pending_jobs = {}
        reached = asyncio.Event()
        release = asyncio.Event()
        events = []

        async def cleanup(env_id, live, *, cancel_job):
            assert cancel_job is False
            events.append("cleanup-start")
            reached.set()
            await release.wait()
            events.append("cleanup-complete")
            return []

        async def publish_step(*args, **kwargs):
            assert "env-fixture" not in value.live_envs
            events.append("step")

        async def publish_result(*args, **kwargs):
            assert "env-fixture" not in value.live_envs
            events.append("result")

        value._close_live_env = AsyncMock(side_effect=cleanup)
        value._finish_env = AsyncMock(return_value=("exit", 0.0, None))
        value._anti_call_llm_with_health_monitor = AsyncMock(return_value="SESSION_END")
        value._post_step = AsyncMock(side_effect=publish_step)
        value._post_result = AsyncMock(side_effect=publish_result)

        if branch == "deliver":
            live = SimpleNamespace(current_index=0)
            value.live_envs["env-fixture"] = live
            value._make_openai_response = MagicMock(return_value={"choices": []})
            operation = value._handle_deliver_sample(
                1, "env-fixture", {"sample_response": {"text": "public-fixture"}})
        else:
            service = SimpleNamespace(watch_agent=AsyncMock())
            trial = SimpleNamespace(model_service=service)
            job = SimpleNamespace(submit=AsyncMock(),
                _job_client=SimpleNamespace(trials=[SimpleNamespace(trial=trial, pid=123)]))
            config = SimpleNamespace(agents=[SimpleNamespace(kwargs={})],
                                     environment=SimpleNamespace(env={}))
            value._make_operator = MagicMock(return_value=object())
            value._build_job_config = MagicMock(return_value=config)
            monkeypatch.setattr("tinker_backend.adapters.rock_adapter.Job", lambda **kwargs: job)
            operation = value._handle_init_task_env(
                1, "env-fixture", {"task_id": "public-fixture", "dataset": "local", "split": "test"})

        task = asyncio.create_task(operation)
        try:
            await asyncio.wait_for(reached.wait(), 1)
            value._post_step.assert_not_awaited()
            value._post_result.assert_not_awaited()
            assert "env-fixture" in value.live_envs
            release.set()
            await asyncio.wait_for(task, 1)
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        assert events == ["cleanup-start", "cleanup-complete", "step", "result"]
        assert value._post_result.await_args.kwargs["status"] == (
            "failed" if branch == "init" else "completed")

    asyncio.run(run())
