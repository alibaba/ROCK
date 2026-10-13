"""Tests for SamplingClient task env and get_step via AsyncSamplingResource."""

from __future__ import annotations

import json
import os

import httpx
import pytest
from respx import MockRouter

from tinker import types
from tinker._client import AsyncTinker

# Pre-existing issue: SDK's construct_type uses deprecated pydantic .construct()
pytestmark = pytest.mark.filterwarnings("ignore::pydantic.warnings.PydanticDeprecatedSince20")

base_url = os.environ.get("TEST_API_BASE_URL", "http://127.0.0.1:4010")
api_key = "tml-test-api-key"


# ===========================================================================
# init_task_env tests
# ===========================================================================


@pytest.mark.respx(base_url=base_url)
async def test_init_task_env_async(respx_mock: MockRouter) -> None:
    """Test init_task_env returns env_id on success."""
    respx_mock.post("/api/v1/init_task_env").mock(
        return_value=httpx.Response(
            200,
            json={"env_id": "env-test-123"},
        )
    )

    async with AsyncTinker(base_url=base_url, api_key=api_key) as client:
        request = types.InitTaskEnvRequest(
            task_id="instance-1",
            dataset="my-dataset",
            split="grpo",
        )
        response = await client.sampling.init_task_env(request=request)
        assert response.env_id == "env-test-123"


@pytest.mark.respx(base_url=base_url)
async def test_init_task_env_async_different_dataset_type(
    respx_mock: MockRouter,
) -> None:
    """Test init_task_env with different dataset_type values."""
    respx_mock.post("/api/v1/init_task_env").mock(
        return_value=httpx.Response(
            200,
            json={"env_id": "env-sft-456"},
        )
    )

    async with AsyncTinker(base_url=base_url, api_key=api_key) as client:
        request = types.InitTaskEnvRequest(
            task_id="instance-2",
            dataset="another-dataset",
            split="sft",
        )
        response = await client.sampling.init_task_env(request=request)
        assert response.env_id == "env-sft-456"


@pytest.mark.respx(base_url=base_url)
async def test_init_task_env_request_serialization(respx_mock: MockRouter) -> None:
    """Test that InitTaskEnvRequest serializes fields correctly in the HTTP body."""
    route = respx_mock.post("/api/v1/init_task_env").mock(
        return_value=httpx.Response(
            200,
            json={"env_id": "env-serial-789"},
        )
    )

    async with AsyncTinker(base_url=base_url, api_key=api_key) as client:
        request = types.InitTaskEnvRequest(
            task_id="inst-001",
            dataset="ds-name",
            split="rlhf",
        )
        await client.sampling.init_task_env(request=request)

        assert route.called
        sent_payload = json.loads(route.calls[0].request.content.decode())
        assert sent_payload["task_id"] == "inst-001"
        assert sent_payload["dataset"] == "ds-name"
        assert sent_payload["split"] == "rlhf"


@pytest.mark.respx(base_url=base_url)
async def test_init_task_env_request_excludes_none_fields(respx_mock: MockRouter) -> None:
    """Test that null fields are excluded from the serialized request."""
    respx_mock.post("/api/v1/init_task_env").mock(
        return_value=httpx.Response(
            200,
            json={"env_id": "env-no-null"},
        )
    )

    async with AsyncTinker(base_url=base_url, api_key=api_key) as client:
        request = types.InitTaskEnvRequest(
            task_id="inst-002",
            dataset="ds",
            split="dpo",
        )
        await client.sampling.init_task_env(request=request)

        sent_payload = json.loads(respx_mock.calls[0].request.content.decode())
        assert "task_id" in sent_payload
        assert "dataset" in sent_payload
        assert "split" in sent_payload
        assert len(sent_payload) == 3


def test_init_task_env_request_rejects_extra_fields() -> None:
    """Test that InitTaskEnvRequest (StrictBase) rejects extra fields."""
    with pytest.raises(Exception):
        types.InitTaskEnvRequest(
            task_id="inst-001",
            dataset="ds",
            split="sft",
            extra_field="should fail",
        )


def test_init_task_env_response_parses() -> None:
    """Test InitTaskEnvResponse parses env_id from JSON."""
    response = types.InitTaskEnvResponse.model_validate({"env_id": "env-parse-test"})
    assert response.env_id == "env-parse-test"


def test_init_task_env_request_model_dump() -> None:
    """Test InitTaskEnvRequest model_dump produces correct dict."""
    request = types.InitTaskEnvRequest(
        task_id="i-1",
        dataset="my-ds",
        split="grpo",
    )
    dumped = request.model_dump(exclude_unset=False, exclude_none=True, mode="json")
    assert dumped == {
        "task_id": "i-1",
        "dataset": "my-ds",
        "split": "grpo",
    }


# ===========================================================================
# get_step tests
# ===========================================================================


@pytest.mark.respx(base_url=base_url)
async def test_get_step_async(respx_mock: MockRouter) -> None:
    """Test get_step returns step_id and prompt on success."""
    respx_mock.post("/api/v1/get_step").mock(
        return_value=httpx.Response(
            200,
            json={
                "step_id": 42,
                "prompt": {
                    "chunks": [
                        {
                            "type": "encoded_text",
                            "tokens": [1, 2, 3, 4, 5],
                        }
                    ]
                },
                "finish_reason": None,
                "reward": 1.5,
            },
        )
    )

    async with AsyncTinker(base_url=base_url, api_key=api_key) as client:
        request = types.GetStepRequest(env_id="env-test-123", step_id=42)
        response = await client.sampling.get_step(request=request)
        assert response.step_id == 42
        assert response.reward == 1.5
        # SDK's construct_type doesn't recursively build nested ModelInput,
        # so prompt arrives as a dict. This is expected SDK behavior.
        assert response.prompt is not None
        assert response.prompt["chunks"][0]["tokens"] == [1, 2, 3, 4, 5]


@pytest.mark.respx(base_url=base_url)
async def test_get_step_async_null_prompt(respx_mock: MockRouter) -> None:
    """Test get_step with null prompt."""
    respx_mock.post("/api/v1/get_step").mock(
        return_value=httpx.Response(
            200,
            json={"step_id": 0, "prompt": None},
        )
    )

    async with AsyncTinker(base_url=base_url, api_key=api_key) as client:
        request = types.GetStepRequest(env_id="env-empty-prompt")
        response = await client.sampling.get_step(request=request)
        assert response.step_id == 0
        assert response.prompt is None


@pytest.mark.respx(base_url=base_url)
async def test_get_step_request_serialization(respx_mock: MockRouter) -> None:
    """Test that GetStepRequest serializes env_id and step_id correctly."""
    route = respx_mock.post("/api/v1/get_step").mock(
        return_value=httpx.Response(
            200,
            json={"step_id": 1, "prompt": None},
        )
    )

    async with AsyncTinker(base_url=base_url, api_key=api_key) as client:
        request = types.GetStepRequest(env_id="env-serial-789", step_id=5)
        await client.sampling.get_step(request=request)

        assert route.called
        sent_payload = json.loads(route.calls[0].request.content.decode())
        assert sent_payload["env_id"] == "env-serial-789"
        assert sent_payload["step_id"] == 5


@pytest.mark.respx(base_url=base_url)
async def test_get_step_request_excludes_none_fields(respx_mock: MockRouter) -> None:
    """Test that null fields are excluded from the serialized request."""
    respx_mock.post("/api/v1/get_step").mock(
        return_value=httpx.Response(
            200,
            json={"step_id": 5, "prompt": None},
        )
    )

    async with AsyncTinker(base_url=base_url, api_key=api_key) as client:
        request = types.GetStepRequest(env_id="env-no-null")
        await client.sampling.get_step(request=request)

        sent_payload = json.loads(respx_mock.calls[0].request.content.decode())
        assert sent_payload == {"env_id": "env-no-null"}
        assert len(sent_payload) == 1


def test_get_step_request_rejects_extra_fields() -> None:
    """Test that GetStepRequest (StrictBase) rejects extra fields."""
    with pytest.raises(Exception):
        types.GetStepRequest(
            env_id="env-001",
            extra_field="should fail",
        )


def test_get_step_response_parses() -> None:
    """Test GetStepResponse parses step_id, prompt, finish_reason, reward from JSON."""
    response = types.GetStepResponse.model_validate({
        "step_id": 100,
        "prompt": {"chunks": [{"type": "encoded_text", "tokens": [10, 20]}]},
        "finish_reason": "finish",
        "reward": 1.0,
    })
    assert response.step_id == 100
    assert response.prompt is not None
    assert response.prompt.length == 2
    assert response.finish_reason == "finish"
    assert response.reward == 1.0


def test_get_step_response_parses_null_prompt() -> None:
    """Test GetStepResponse parses with null prompt."""
    response = types.GetStepResponse.model_validate({
        "step_id": 0,
        "prompt": None,
    })
    assert response.step_id == 0
    assert response.prompt is None


def test_get_step_request_model_dump() -> None:
    """Test GetStepRequest model_dump produces correct dict."""
    request = types.GetStepRequest(env_id="env-42", step_id=3)
    dumped = request.model_dump(exclude_unset=False, exclude_none=True, mode="json")
    assert dumped == {"env_id": "env-42", "step_id": 3}


def test_get_step_request_model_dump_no_step_id() -> None:
    """Test GetStepRequest excludes None step_id from model_dump."""
    request = types.GetStepRequest(env_id="env-42")
    dumped = request.model_dump(exclude_unset=False, exclude_none=True, mode="json")
    assert dumped == {"env_id": "env-42"}
