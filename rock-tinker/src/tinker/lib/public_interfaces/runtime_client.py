"""RuntimeClient for Tinker backend runtime-scoped operations."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, TypeVar, cast

from tinker import types
from tinker._base_client import make_request_options
from tinker._compat import model_dump
from tinker._models import BaseModel
from tinker.lib.client_connection_pool_type import ClientConnectionPoolType
from tinker.lib.public_interfaces.api_future import AwaitableConcurrentFuture
from tinker.lib.telemetry import Telemetry
from tinker.lib.telemetry_provider import TelemetryProvider

from ..api_future_impl import _APIFuture
from ..retry_handler import RetryConfig

if TYPE_CHECKING:
    from ..internal_client_holder import InternalClientHolder
    from .sampling_client import SamplingClient
    from .training_client import TrainingClient

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class RuntimeClient(TelemetryProvider):
    """Client bound to one Tinker backend runtime.

    Runtime-scoped calls use /api/v1/sdk/{runtime_id}/... paths so the SDK does
    not depend on custom runtime headers.
    """

    def __init__(self, holder: InternalClientHolder, runtime_info: types.CreateRuntimeResponse):
        self.holder = holder
        self.runtime_info = runtime_info
        self.runtime_id = runtime_info.runtime_id
        self.runtime_type = runtime_info.runtime_type
        self._sample_seq_id = 0
        self._closed = False

    async def __aenter__(self) -> "RuntimeClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()

    @property
    def ready(self) -> bool:
        return self.runtime_info.ready

    @property
    def status(self) -> str:
        return self.runtime_info.status

    def _runtime_path(self, endpoint: str) -> str:
        return f"/api/v1/sdk/{self.runtime_id}/{endpoint.lstrip('/')}"

    def create_lora_training(
        self,
        *,
        base_model: str,
        rank: int = 32,
        seed: int | None = None,
        train_mlp: bool = True,
        train_attn: bool = True,
        train_unembed: bool = True,
        user_metadata: dict[str, str] | None = None,
    ) -> AwaitableConcurrentFuture[TrainingClient]:
        """Create a runtime-scoped LoRA training handle."""
        assert any([train_mlp, train_attn, train_unembed]), (
            "At least one of train_mlp, train_attn, or train_unembed must be True"
        )
        session_id = self.holder.get_session_id()
        model_seq_id = self.holder.get_training_client_id()
        lora_config = types.LoraConfig(
            rank=rank,
            seed=seed,
            train_mlp=train_mlp,
            train_attn=train_attn,
            train_unembed=train_unembed,
        )

        async def _create_lora_training_async():
            start_time = time.time()
            request = types.CreateModelRequest(
                session_id=session_id,
                model_seq_id=model_seq_id,
                base_model=base_model,
                lora_config=lora_config,
                user_metadata=user_metadata,
            )
            with self.holder.aclient(ClientConnectionPoolType.TRAIN) as client:
                payload = await client.post(
                    self._runtime_path("create_model"),
                    body=model_dump(request, exclude_unset=False, exclude_none=True, mode="json"),
                    options=make_request_options(),
                    cast_to=cast(Any, dict),
                )
            future = types.UntypedAPIFuture.model_validate(payload)
            create_model_response = await _APIFuture(
                types.CreateModelResponse,
                self.holder,
                future,
                request_start_time=start_time,
                request_type="CreateModel",
            ).result_async()

            from .training_client import TrainingClient

            return TrainingClient(
                self.holder,
                model_seq_id=model_seq_id,
                model_id=create_model_response.model_id,
                runtime_id=self.runtime_id,
            )

        return self.holder.run_coroutine_threadsafe(_create_lora_training_async())

    def create_sampling_client(
        self,
        *,
        model_path: str | None = None,
        base_model: str | None = None,
        sampling_session_id: str | None = None,
        retry_config: RetryConfig | None = None,
    ) -> AwaitableConcurrentFuture[SamplingClient]:
        """Create a runtime-scoped sampler, for policy or reference sampling."""
        from .sampling_client import SamplingClient

        return SamplingClient.create(
            self.holder,
            model_path=model_path,
            base_model=base_model,
            sampling_session_id=sampling_session_id,
            retry_config=retry_config,
            runtime_id=self.runtime_id,
        )

    async def close(
        self,
        *,
        force_after_seconds: float | None = 30.0,
        timeout: float | None = 650,
        extra_headers: dict[str, str] | None = None,
    ) -> types.CloseRuntimeResponse:
        """Close this runtime and wait until backend cleanup completes."""

        if self._closed:
            return types.CloseRuntimeResponse(
                runtime_id=self.runtime_id,
                status="stopped",
                graceful=True,
                killed_pids=[],
                message="runtime client already closed",
            )
        response = await self.holder.run_coroutine_threadsafe(
            self._close_impl(
                force_after_seconds=force_after_seconds,
                timeout=timeout,
                extra_headers=extra_headers,
            )
        )
        self._closed = True
        self.runtime_info = types.CreateRuntimeResponse(
            runtime_id=self.runtime_id,
            runtime_type=self.runtime_type,
            status=response.status,
            ready=False,
            config_type=self.runtime_info.config_type,
            config_path=self.runtime_info.config_path,
            adapter_base_url=self.runtime_info.adapter_base_url,
            error_message=self.runtime_info.error_message,
        )
        return response

    async def _close_impl(
        self,
        *,
        force_after_seconds: float | None,
        timeout: float | None,
        extra_headers: dict[str, str] | None,
    ) -> types.CloseRuntimeResponse:
        request = types.CloseRuntimeRequest(force_after_seconds=force_after_seconds)
        logger.info(
            "CloseRuntime: submit runtime_id=%s force_after_seconds=%s timeout=%s",
            self.runtime_id,
            force_after_seconds,
            timeout,
        )
        response = await self._post_runtime_endpoint(
            path=self._runtime_path("close"),
            request=request,
            model_cls=types.CloseRuntimeResponse,
            request_type="CloseRuntime",
            timeout=timeout,
            extra_headers=extra_headers,
        )
        logger.info(
            "CloseRuntime: completed runtime_id=%s graceful=%s killed_pids=%s message=%s",
            self.runtime_id,
            response.graceful,
            response.killed_pids,
            response.message,
        )
        return response

    async def init_task_env(
        self,
        task: types.TaskDescriptor,
        *,
        timeout: float | None = 2000,
        extra_headers: dict[str, str] | None = None,
    ) -> str:
        """Create an environment under this runtime and return its env id."""

        return await self.holder.run_coroutine_threadsafe(
            self._init_task_env_impl(
                task=task,
                timeout=timeout,
                extra_headers=extra_headers,
            )
        )

    async def _init_task_env_impl(
        self,
        *,
        task: types.TaskDescriptor,
        timeout: float | None,
        extra_headers: dict[str, str] | None,
    ) -> str:
        metadata = dict(task.metadata or {})
        metadata.setdefault("bench_name", task.bench_name)
        request = types.InitTaskEnvRequest(
            task_id=task.task_id,
            dataset=task.dataset,
            split=task.split,
            metadata=metadata,
        )
        logger.info(
            "InitTaskEnv: submit runtime_id=%s task_id=%s dataset=%s/%s timeout=%s",
            self.runtime_id,
            task.task_id,
            task.dataset,
            task.split,
            timeout,
        )
        response = await self._post_runtime_endpoint(
            path=self._runtime_path("init_task_env"),
            request=request,
            model_cls=types.InitTaskEnvResponse,
            request_type="InitTaskEnv",
            timeout=timeout,
            extra_headers=extra_headers,
        )
        logger.info(
            "InitTaskEnv: ready runtime_id=%s env_id=%s task_id=%s",
            self.runtime_id,
            response.env_id,
            task.task_id,
        )
        return response.env_id

    async def get_step(
        self,
        *,
        env_id: str,
        step_id: int | None = None,
        timeout: float | None = 650,
        extra_headers: dict[str, str] | None = None,
    ) -> types.RuntimeGetStepResponse:
        """Return one model step for an env under this runtime."""

        return await self.holder.run_coroutine_threadsafe(
            self._get_step_impl(
                env_id=env_id,
                step_id=step_id,
                timeout=timeout,
                extra_headers=extra_headers,
            )
        )

    async def _get_step_impl(
        self,
        *,
        env_id: str,
        step_id: int | None,
        timeout: float | None,
        extra_headers: dict[str, str] | None,
    ) -> types.RuntimeGetStepResponse:
        request = types.GetStepRequest(env_id=env_id, step_id=step_id)
        return await self._post_runtime_endpoint(
            path=self._runtime_path("get_step"),
            request=request,
            model_cls=types.RuntimeGetStepResponse,
            request_type="GetStep",
            timeout=timeout,
            extra_headers=extra_headers,
        )

    async def sample(
        self,
        *,
        prompt: types.OpenAIChatPrompt | types.ModelInput,
        sampling_params: types.SamplingParams,
        num_samples: int = 1,
        include_prompt_logprobs: bool = False,
        topk_prompt_logprobs: int = 0,
        env_id: str | None = None,
        timeout: float | None = 650,
        extra_headers: dict[str, str] | None = None,
    ) -> types.SampleResponse:
        """Sample from this runtime."""

        return await self.holder.run_coroutine_threadsafe(
            self._sample_impl(
                prompt=prompt,
                sampling_params=sampling_params,
                num_samples=num_samples,
                include_prompt_logprobs=include_prompt_logprobs,
                topk_prompt_logprobs=topk_prompt_logprobs,
                env_id=env_id,
                timeout=timeout,
                extra_headers=extra_headers,
            )
        )

    async def _sample_impl(
        self,
        *,
        prompt: types.OpenAIChatPrompt | types.ModelInput,
        sampling_params: types.SamplingParams,
        num_samples: int,
        include_prompt_logprobs: bool,
        topk_prompt_logprobs: int,
        env_id: str | None,
        timeout: float | None,
        extra_headers: dict[str, str] | None,
    ) -> types.SampleResponse:
        seq_id = self._sample_seq_id
        self._sample_seq_id += 1
        request = types.RuntimeSampleRequest(
            sampling_session_id=self.runtime_id,
            seq_id=seq_id,
            num_samples=num_samples,
            prompt=prompt if isinstance(prompt, types.OpenAIChatPrompt) else None,
            model_input=prompt if isinstance(prompt, types.ModelInput) else None,
            sampling_params=sampling_params,
            prompt_logprobs=include_prompt_logprobs,
            topk_prompt_logprobs=topk_prompt_logprobs,
            env_id=env_id,
        )
        return await self._post_runtime_endpoint(
            path=self._runtime_path("asample"),
            request=request,
            model_cls=types.SampleResponse,
            request_type="RuntimeSample",
            timeout=timeout,
            extra_headers=extra_headers,
        )

    async def _post_runtime_endpoint(
        self,
        *,
        path: str,
        request: Any,
        model_cls: type[T],
        request_type: str,
        timeout: float | None,
        extra_headers: dict[str, str] | None,
    ) -> T:
        start_time = time.time()
        body = model_dump(request, exclude_unset=False, exclude_none=True, mode="json")
        logger.info(
            "%s: post runtime_id=%s path=%s timeout=%s",
            request_type,
            self.runtime_id,
            path,
            timeout,
        )

        with self.holder.aclient(ClientConnectionPoolType.SAMPLE) as client:
            payload = await client.post(
                path,
                body=body,
                options=make_request_options(
                    extra_headers=extra_headers,
                    timeout=timeout,
                ),
                cast_to=cast(Any, dict),
            )

        if not isinstance(payload, dict):
            raise TypeError(f"{request_type} returned non-dict payload: {type(payload)!r}")

        if _is_untyped_future_payload(payload):
            future = types.UntypedAPIFuture.model_validate(payload)
            logger.info(
                "%s: waiting for future request_id=%s runtime_id=%s elapsed=%.1fs",
                request_type,
                future.request_id,
                self.runtime_id,
                time.time() - start_time,
            )
            result = await _APIFuture(
                model_cls,
                self.holder,
                future,
                request_start_time=start_time,
                request_type=request_type,
            ).result_async()
            logger.info(
                "%s: completed future runtime_id=%s elapsed=%.1fs",
                request_type,
                self.runtime_id,
                time.time() - start_time,
            )
            return result

        logger.info(
            "%s: completed directly runtime_id=%s elapsed=%.1fs",
            request_type,
            self.runtime_id,
            time.time() - start_time,
        )
        return model_cls.model_validate(payload)

    def get_telemetry(self) -> Telemetry | None:
        return self.holder.get_telemetry()


def _is_untyped_future_payload(payload: dict[str, Any]) -> bool:
    if "env_id" in payload or "step_id" in payload or "sequences" in payload:
        return False
    return "request_id" in payload or "future_id" in payload
