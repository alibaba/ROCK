from __future__ import annotations

import json
import os
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from respx import MockRouter

import tinker
from tinker import types
from tinker_cookbook.tinker_backend_cookbook.env import RemoteSandboxEnv

base_url = os.environ.get("TEST_API_BASE_URL", "http://127.0.0.1:4010")


def _mock_session(respx_mock: MockRouter) -> None:
    respx_mock.post("/api/v1/create_session").mock(
        return_value=httpx.Response(200, json={"session_id": "session-public-task"})
    )


def test_init_task_env_request_uses_task_descriptor_names() -> None:
    request = types.InitTaskEnvRequest(
        task_id="sympy__sympy-19637",
        dataset="SWE-Env/SWE-Env",
        split="v2_2023pr",
        metadata={"bench_name": "SWE-bench"},
    )

    assert request.model_dump(exclude_unset=False, exclude_none=True, mode="json") == {
        "task_id": "sympy__sympy-19637",
        "dataset": "SWE-Env/SWE-Env",
        "split": "v2_2023pr",
        "metadata": {"bench_name": "SWE-bench"},
    }
    with pytest.raises(Exception):
        types.InitTaskEnvRequest(
            instance_id="sympy__sympy-19637",
            dataset_name="SWE-Env/SWE-Env",
            dataset_type="v2_2023pr",
        )


@pytest.mark.respx(base_url=base_url)
async def test_tinker_client_list_tasks_returns_task_descriptors(respx_mock: MockRouter) -> None:
    _mock_session(respx_mock)
    list_route = respx_mock.post("/api/v1/list_tasks").mock(
        return_value=httpx.Response(
            200,
            json={
                "tasks": [
                    {
                        "task_id": "sympy__sympy-19637",
                        "dataset": "SWE-Env/SWE-Env",
                        "split": "v2_2023pr",
                        "bench_name": "SWE-bench",
                        "metadata": {"source": "public-task"},
                    }
                ],
                "total": 1,
            },
        )
    )

    tinker_client = tinker.TinkerClient(base_url=base_url, api_key="tml-dummy")
    try:
        tasks = await tinker_client.list_tasks(
            dataset="SWE-Env/SWE-Env",
            split="v2_2023pr",
            bench_name="SWE-bench",
            task_filter="^sympy__sympy-19637$",
        )
    finally:
        tinker_client.close()

    assert tasks == [
        types.TaskDescriptor(
            task_id="sympy__sympy-19637",
            dataset="SWE-Env/SWE-Env",
            split="v2_2023pr",
            bench_name="SWE-bench",
            metadata={"source": "public-task"},
        )
    ]
    sent_payload = json.loads(list_route.calls[0].request.content.decode())
    assert sent_payload == {
        "datasets": [
            {
                "dataset": "SWE-Env/SWE-Env",
                "split": "v2_2023pr",
                "bench_name": "SWE-bench",
                "task_filter": "^sympy__sympy-19637$",
            }
        ]
    }


@pytest.mark.respx(base_url=base_url)
async def test_runtime_init_task_env_accepts_task_descriptor(respx_mock: MockRouter) -> None:
    _mock_session(respx_mock)
    init_route = respx_mock.post("/api/v1/sdk/rt_public-task/init_task_env").mock(
        return_value=httpx.Response(
            200,
            json={
                "type": "init_task_env",
                "request_id": "future-init",
                "future_id": "future-init",
                "runtime_id": "rt_public-task",
                "status": "pending",
            },
        )
    )
    respx_mock.post("/api/v1/retrieve_future").mock(
        return_value=httpx.Response(200, json={"type": "init_task_env", "env_id": "env_public-task"})
    )

    tinker_client = tinker.TinkerClient(base_url=base_url, api_key="tml-dummy")
    try:
        runtime = tinker.RuntimeClient(
            tinker_client.holder,
            types.CreateRuntimeResponse(
                runtime_id="rt_public-task",
                runtime_type="roll",
                status="ready",
                ready=True,
                config_type="yaml",
            ),
        )
        env_id = await runtime.init_task_env(
            types.TaskDescriptor(
                task_id="sympy__sympy-19637",
                dataset="SWE-Env/SWE-Env",
                split="v2_2023pr",
                bench_name="SWE-bench",
                metadata={"source": "public-task"},
            )
        )
    finally:
        tinker_client.close()

    assert env_id == "env_public-task"
    sent_payload = json.loads(init_route.calls[0].request.content.decode())
    assert sent_payload == {
        "task_id": "sympy__sympy-19637",
        "dataset": "SWE-Env/SWE-Env",
        "split": "v2_2023pr",
        "metadata": {"source": "public-task", "bench_name": "SWE-bench"},
    }


@pytest.mark.asyncio
async def test_remote_sandbox_env_sends_task_descriptor_to_runtime() -> None:
    task = types.TaskDescriptor(
        task_id="sympy__sympy-19637",
        dataset="SWE-Env/SWE-Env",
        split="v2_2023pr",
        bench_name="SWE-bench",
    )

    class RuntimeStub:
        runtime_id = "rt_stub"

        def __init__(self) -> None:
            self.task = None

        async def init_task_env(self, task):
            self.task = task
            return "env_stub"

    runtime = RuntimeStub()
    env = RemoteSandboxEnv(task=task, runtime=runtime)
    env_id = await env._init_sandbox()

    assert env_id == "env_stub"
    assert runtime.task == task


@pytest.mark.asyncio
async def test_remote_sandbox_env_returns_human_readable_backend_prompt() -> None:
    request = {
        "messages": [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": '{"command":"pwd"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "content": [{"type": "text", "text": "/workspace"}],
                "tool_call_id": "call_1",
            },
        ],
        "tools": [],
    }
    backend_prompt = tinker.OpenAIChatPrompt(request=request)

    class RuntimeStub:
        async def get_step(self, *, env_id: str, step_id: int):
            assert env_id == "env_stub"
            assert step_id == 0
            return SimpleNamespace(
                prompt=backend_prompt,
                step_id=0,
                reward=None,
                finish_reason=None,
            )

    env = RemoteSandboxEnv(
        task=tinker.TaskDescriptor(
            task_id="task",
            dataset="dataset",
            split="test",
            bench_name="bench",
        ),
        runtime=RuntimeStub(),
    )
    env._env_id = "env_stub"

    step_id, observation, reward, finish_reason = await env._get_step(step_id=0)

    assert step_id == 0
    assert observation is not None
    assert isinstance(observation, tinker.OpenAIChatPrompt)
    assert observation.request == request
    assert reward is None
    assert finish_reason is None


@pytest.mark.asyncio
async def test_remote_sandbox_env_rejects_token_prompt_from_backend() -> None:
    class RuntimeStub:
        async def get_step(self, *, env_id: str, step_id: int):
            return SimpleNamespace(
                prompt=tinker.ModelInput.from_ints([1, 2, 3]),
                step_id=step_id,
                reward=None,
                finish_reason=None,
            )

    env = RemoteSandboxEnv(
        task=tinker.TaskDescriptor(
            task_id="task",
            dataset="dataset",
            split="test",
            bench_name="bench",
        ),
        runtime=RuntimeStub(),
    )
    env._env_id = "env_stub"

    with pytest.raises(ValidationError):
        await env._get_step(step_id=0)
