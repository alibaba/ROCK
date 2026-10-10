"""Tests for TinkerClient protocol helpers."""

from __future__ import annotations

import json
import os

import httpx
import pytest
from respx import MockRouter

import tinker
from tinker import types

base_url = os.environ.get("TEST_API_BASE_URL", "http://127.0.0.1:4010")


def mock_create_session(respx_mock: MockRouter, session_id: str = "test-session-id") -> None:
    respx_mock.post("/api/v1/create_session").mock(
        return_value=httpx.Response(200, json={"session_id": session_id})
    )


def mock_training_client_from_state_futures(respx_mock: MockRouter, model_id: str = "new-model-id") -> None:
    respx_mock.post("/api/v1/create_model").mock(
        return_value=httpx.Response(
            200,
            json={
                "type": "create_model",
                "request_id": "future-create-model",
                "future_id": "future-create-model",
                "model_id": model_id,
                "status": "pending",
            },
        )
    )
    respx_mock.post("/api/v1/load_weights").mock(
        return_value=httpx.Response(
            200,
            json={
                "type": "load_weights",
                "request_id": "future-load-weights",
                "future_id": "future-load-weights",
                "model_id": model_id,
                "status": "pending",
            },
        )
    )
    retrieve_responses = iter(
        [
            httpx.Response(200, json={"type": "create_model", "model_id": model_id}),
            httpx.Response(200, json={"type": "load_weights", "path": "tinker://loaded"}),
        ]
    )
    respx_mock.post("/api/v1/retrieve_future").mock(side_effect=lambda request: next(retrieve_responses))



@pytest.mark.respx(base_url=base_url)
def test_tinker_client_passes_project_id_on_session_create(respx_mock: MockRouter) -> None:
    create_session_route = respx_mock.post("/api/v1/create_session").mock(
        return_value=httpx.Response(200, json={"session_id": "test-session-id"})
    )

    tinker_client = tinker.TinkerClient(base_url=base_url, project_id="project-123")
    tinker_client.holder.close()

    assert create_session_route.called
    sent_payload = json.loads(create_session_route.calls[0].request.content.decode())
    assert sent_payload["project_id"] == "project-123"


@pytest.mark.respx(base_url=base_url)
async def test_create_training_client_from_state_async(respx_mock: MockRouter) -> None:
    """Test create_training_client_from_state_async uses public endpoint."""
    mock_create_session(respx_mock)
    tinker_path = "tinker://test-model-123/weights/checkpoint-001"
    weights_info_response = types.WeightsInfoResponse(
        base_model="meta-llama/Llama-3.2-1B", is_lora=True, lora_rank=32
    )

    # Mock the get_weights_info endpoint call
    respx_mock.post("/api/v1/weights_info").mock(
        return_value=httpx.Response(200, json=weights_info_response.model_dump())
    )

    mock_training_client_from_state_futures(respx_mock)
    tinker_client = tinker.TinkerClient(base_url=base_url)
    training_client = await tinker_client.create_training_client_from_state_async(tinker_path)

    assert training_client is not None
    assert training_client.model_id == "new-model-id"


@pytest.mark.respx(base_url=base_url)
async def test_create_training_client_from_state_async_with_user_metadata(
    respx_mock: MockRouter,
) -> None:
    """Test create_training_client_from_state_async preserves user metadata."""
    mock_create_session(respx_mock)
    tinker_path = "tinker://test-model-123/weights/checkpoint-001"
    user_metadata = {"key1": "value1", "key2": "value2"}
    weights_info_response = types.WeightsInfoResponse(
        base_model="meta-llama/Llama-3.2-1B", is_lora=True, lora_rank=32
    )

    # Mock the get_weights_info endpoint call
    respx_mock.post("/api/v1/weights_info").mock(
        return_value=httpx.Response(200, json=weights_info_response.model_dump())
    )

    mock_training_client_from_state_futures(respx_mock)
    tinker_client = tinker.TinkerClient(base_url=base_url)
    training_client = await tinker_client.create_training_client_from_state_async(
        tinker_path, user_metadata=user_metadata
    )

    assert training_client is not None
    # Verify user_metadata was passed through (we can't directly check it, but the call succeeded)


@pytest.mark.respx(base_url=base_url)
async def test_create_training_client_from_state_async_not_lora(respx_mock: MockRouter) -> None:
    """Test create_training_client_from_state_async raises assertion for non-LoRA model."""
    mock_create_session(respx_mock)
    tinker_path = "tinker://test-model-123/weights/checkpoint-001"

    # Mock WeightsInfo response with is_lora=False
    weights_info_response = types.WeightsInfoResponse(
        base_model="meta-llama/Llama-3.2-1B", is_lora=False, lora_rank=None
    )

    # Mock the get_weights_info endpoint call
    respx_mock.post("/api/v1/weights_info").mock(
        return_value=httpx.Response(200, json=weights_info_response.model_dump())
    )

    tinker_client = tinker.TinkerClient(base_url=base_url)

    # Should raise AssertionError because is_lora=False or lora_rank=None
    with pytest.raises(AssertionError):
        await tinker_client.create_training_client_from_state_async(tinker_path)


@pytest.mark.respx(base_url=base_url)
async def test_create_training_client_from_state_async_uses_public_endpoint(
    respx_mock: MockRouter,
) -> None:
    """Test that create_training_client_from_state_async uses get_weights_info_by_tinker_path."""
    mock_create_session(respx_mock)
    tinker_path = "tinker://test-model-123/weights/checkpoint-001"

    # Mock WeightsInfo response
    weights_info_response = types.WeightsInfoResponse(
        base_model="meta-llama/Llama-3.2-1B", is_lora=True, lora_rank=32
    )

    # Mock the get_weights_info endpoint call (public endpoint)
    info_lite_route = respx_mock.post("/api/v1/weights_info").mock(
        return_value=httpx.Response(200, json=weights_info_response.model_dump())
    )

    mock_training_client_from_state_futures(respx_mock)
    tinker_client = tinker.TinkerClient(base_url=base_url)
    await tinker_client.create_training_client_from_state_async(tinker_path)

    # Verify it uses the public endpoint (info_lite), not the full training run endpoint
    assert info_lite_route.called


@pytest.mark.respx(base_url=base_url)
def test_create_training_client_from_state_sync(respx_mock: MockRouter) -> None:
    """Test create_training_client_from_state (sync) uses public endpoint."""
    mock_create_session(respx_mock)
    tinker_path = "tinker://test-model-123/weights/checkpoint-001"
    weights_info_response = types.WeightsInfoResponse(
        base_model="meta-llama/Llama-3.2-1B", is_lora=True, lora_rank=32
    )

    # Mock the get_weights_info endpoint call
    respx_mock.post("/api/v1/weights_info").mock(
        return_value=httpx.Response(200, json=weights_info_response.model_dump())
    )

    mock_training_client_from_state_futures(respx_mock)
    tinker_client = tinker.TinkerClient(base_url=base_url)
    training_client = tinker_client.create_training_client_from_state(tinker_path)

    assert training_client is not None
    assert training_client.model_id == "new-model-id"


@pytest.mark.respx(base_url=base_url)
def test_create_training_client_from_state_sync_uses_public_endpoint(
    respx_mock: MockRouter,
) -> None:
    """Test that create_training_client_from_state (sync) uses get_weights_info_by_tinker_path."""
    mock_create_session(respx_mock)
    tinker_path = "tinker://test-model-123/weights/checkpoint-001"

    # Mock WeightsInfo response
    weights_info_response = types.WeightsInfoResponse(
        base_model="meta-llama/Llama-3.2-1B", is_lora=True, lora_rank=32
    )

    # Mock the get_weights_info endpoint call (public endpoint)
    info_lite_route = respx_mock.post("/api/v1/weights_info").mock(
        return_value=httpx.Response(200, json=weights_info_response.model_dump())
    )

    mock_training_client_from_state_futures(respx_mock)
    tinker_client = tinker.TinkerClient(base_url=base_url)
    tinker_client.create_training_client_from_state(tinker_path)

    # Verify it uses the public endpoint (info_lite), not the full training run endpoint
    assert info_lite_route.called

@pytest.mark.respx(base_url=base_url)
async def test_create_runtime_reads_config_path_and_waits_for_ready_future(
    respx_mock: MockRouter,
    tmp_path,
) -> None:
    config_path = tmp_path / "runtime.yaml"
    config_content = "runtime:\n  launch_script: /root/tinker-dummy/run_dummy.py\n"
    config_path.write_text(config_content, encoding="utf-8")

    respx_mock.post("/api/v1/create_session").mock(
        return_value=httpx.Response(200, json={"session_id": "session-runtime"})
    )
    create_runtime_route = respx_mock.post("/api/v1/create_runtime").mock(
        return_value=httpx.Response(
            200,
            json={
                "type": "create_runtime",
                "request_id": "future-1",
                "future_id": "future-1",
                "runtime_id": "rt_1",
                "status": "pending",
            },
        )
    )
    retrieve_route = respx_mock.post("/api/v1/retrieve_future").mock(
        return_value=httpx.Response(
            200,
            json={
                "type": "create_runtime",
                "runtime_id": "rt_1",
                "runtime_type": "dummy",
                "status": "ready",
                "ready": True,
                "config_type": "yaml",
                "config_path": "/var/lib/tinker-backend/runtimes/rt_1/config.yaml",
            },
        )
    )

    tinker_client = tinker.TinkerClient(base_url=base_url, api_key="tml-dummy")
    try:
        result = await tinker_client.create_runtime(
            runtime_type="Dummy",
            config_path=config_path,
        )
    finally:
        tinker_client.holder.close()

    assert result.runtime_id == "rt_1"
    assert result.runtime_type == "dummy"
    assert result.ready is True

    sent_payload = json.loads(create_runtime_route.calls[0].request.content.decode())
    assert sent_payload["runtime_type"] == "dummy"
    assert sent_payload["config_type"] == "yaml"
    assert sent_payload["config_content"] == config_content
    assert sent_payload["session_id"] == "session-runtime"

    retrieve_payload = json.loads(retrieve_route.calls[0].request.content.decode())
    assert retrieve_payload["request_id"] == "future-1"
    assert retrieve_payload["allow_metadata_only"] is True


@pytest.mark.respx(base_url=base_url)
async def test_create_runtime_retrieve_future_failed_detail_is_reported(respx_mock: MockRouter) -> None:
    error_detail = "sympy__sympy-19637: RuntimeError: docker build timeout"
    respx_mock.post("/api/v1/create_session").mock(
        return_value=httpx.Response(200, json={"session_id": "session-runtime"})
    )
    respx_mock.post("/api/v1/create_runtime").mock(
        return_value=httpx.Response(
            200,
            json={
                "type": "create_runtime",
                "request_id": "future-failed",
                "future_id": "future-failed",
                "runtime_id": "rt_failed",
                "status": "pending",
            },
        )
    )
    retrieve_route = respx_mock.post("/api/v1/retrieve_future").mock(
        return_value=httpx.Response(400, json={"detail": error_detail})
    )

    tinker_client = tinker.TinkerClient(base_url=base_url, api_key="tml-dummy")
    try:
        with pytest.raises(ValueError, match="docker build timeout"):
            await tinker_client.create_runtime(
                runtime_type="dummy",
                config_content="runtime:\n  launch_script: /root/tinker-dummy/run_dummy.py\n",
            )
    finally:
        tinker_client.holder.close()

    assert len(retrieve_route.calls) == 1


@pytest.mark.respx(base_url=base_url)
async def test_runtime_client_get_step_uses_runtime_route_param(respx_mock: MockRouter) -> None:
    respx_mock.post("/api/v1/create_session").mock(
        return_value=httpx.Response(200, json={"session_id": "session-runtime"})
    )
    get_step_route = respx_mock.post("/api/v1/sdk/rt_route/get_step").mock(
        return_value=httpx.Response(200, json={"step_id": 0})
    )

    tinker_client = tinker.TinkerClient(base_url=base_url, api_key="tml-dummy")
    try:
        runtime = tinker.RuntimeClient(
            tinker_client.holder,
            types.CreateRuntimeResponse(
                runtime_id="rt_route",
                runtime_type="dummy",
                status="ready",
                ready=True,
                config_type="yaml",
            ),
        )
        response = await runtime.get_step(env_id="env_1", step_id=0)
    finally:
        tinker_client.holder.close()

    assert response.step_id == 0
    assert get_step_route.calls[0].request.url.path == "/api/v1/sdk/rt_route/get_step"
    assert "X-Tinker-Backend-Runtime" not in get_step_route.calls[0].request.headers


@pytest.mark.respx(base_url=base_url)
async def test_create_runtime_retrieve_future_408_uses_bounded_exponential_backoff(
    respx_mock: MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tinker.lib.api_future_impl as api_future_impl

    sleep_delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_delays.append(delay)

    monkeypatch.setattr(api_future_impl, "_sleep_before_retry", fake_sleep)
    monkeypatch.setattr(api_future_impl, "RETRIEVE_FUTURE_408_MAX_RETRIES", 3)
    monkeypatch.setattr(api_future_impl, "RETRIEVE_FUTURE_408_INITIAL_RETRY_DELAY_SECONDS", 1)
    monkeypatch.setattr(api_future_impl, "RETRIEVE_FUTURE_408_MAX_RETRY_DELAY_SECONDS", 4)

    respx_mock.post("/api/v1/create_session").mock(
        return_value=httpx.Response(200, json={"session_id": "session-runtime"})
    )
    respx_mock.post("/api/v1/create_runtime").mock(
        return_value=httpx.Response(
            200,
            json={
                "type": "create_runtime",
                "request_id": "future-408",
                "future_id": "future-408",
                "runtime_id": "rt_408",
                "status": "pending",
            },
        )
    )
    retrieve_route = respx_mock.post("/api/v1/retrieve_future").mock(
        return_value=httpx.Response(
            408,
            json={"detail": "Timeout waiting for result"},
            headers={"X-Tinker-Queue-State": "active"},
        )
    )

    tinker_client = tinker.TinkerClient(base_url=base_url, api_key="tml-dummy")
    try:
        with pytest.raises(TimeoutError):
            await tinker_client.create_runtime(
                runtime_type="dummy",
                config_content="runtime:\n  launch_script: /root/tinker-dummy/run_dummy.py\n",
            )
    finally:
        tinker_client.holder.close()

    assert sleep_delays == [1, 2, 4]
    assert len(retrieve_route.calls) == 4
