from __future__ import annotations

import json
import os

import httpx
import pytest
from respx import MockRouter

import tinker
from tinker import types

base_url = os.environ.get("TEST_API_BASE_URL", "http://127.0.0.1:4010")


def test_tinker_exports_tinker_client_and_not_service_client() -> None:
    assert hasattr(tinker, "TinkerClient")
    assert not hasattr(tinker, "ServiceClient")


@pytest.mark.respx(base_url=base_url)
async def test_tinker_client_list_tasks_keeps_protocol_behavior(respx_mock: MockRouter) -> None:
    respx_mock.post("/api/v1/create_session").mock(
        return_value=httpx.Response(200, json={"session_id": "session-tinker-client"})
    )
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
                    }
                ],
                "total": 1,
            },
        )
    )

    client = tinker.TinkerClient(base_url=base_url, api_key="tml-dummy")
    try:
        tasks = await client.list_tasks(
            dataset="SWE-Env/SWE-Env",
            split="v2_2023pr",
            bench_name="SWE-bench",
            task_filter="sympy",
        )
    finally:
        client.close()

    assert tasks == [
        types.TaskDescriptor(
            task_id="sympy__sympy-19637",
            dataset="SWE-Env/SWE-Env",
            split="v2_2023pr",
            bench_name="SWE-bench",
        )
    ]
    assert json.loads(list_route.calls[0].request.content.decode()) == {
        "datasets": [
            {
                "dataset": "SWE-Env/SWE-Env",
                "split": "v2_2023pr",
                "bench_name": "SWE-bench",
                "task_filter": "sympy",
            }
        ]
    }
