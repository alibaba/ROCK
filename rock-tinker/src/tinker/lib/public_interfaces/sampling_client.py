# Derived from https://github.com/thinking-machines-lab/tinker (Apache-2.0).
# Modified in this Tinker fork; see rock-tinker/UPSTREAM.md and NOTICE.
"""SamplingClient for Tinker API."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import time
import uuid
from concurrent.futures import Future as ConcurrentFuture
from functools import lru_cache
from typing import TYPE_CHECKING, Any, TypeVar, cast

import tinker
from tinker import types
from tinker._base_client import make_request_options
from tinker._compat import model_dump
from tinker.lib.client_connection_pool_type import ClientConnectionPoolType
from tinker.lib.public_interfaces.api_future import APIFuture, AwaitableConcurrentFuture
from tinker.lib.sidecar import SidecarHandle, SidecarRPC, create_sidecar_handle
from tinker.lib.telemetry import Telemetry, capture_exceptions
from tinker.lib.telemetry_provider import TelemetryProvider

from ..api_future_impl import QueueState, QueueStateObserver, _APIFuture
from ..retry_handler import RetryConfig, RetryHandler

if TYPE_CHECKING:
    from transformers.tokenization_utils import PreTrainedTokenizer

    from ..internal_client_holder import InternalClientHolder

# pyright: reportPrivateImportUsage=false

logger = logging.getLogger(__name__)

U = TypeVar("U")


# ---------------------------------------------------------------------------
# Pickle serialization state
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _SamplingClientPickleState:
    """Serialized state for pickling SamplingClient across processes."""

    session_id: str
    sampling_session_id: str
    constructor_kwargs: dict[str, Any]
    subprocess_sampling: bool
    runtime_id: str | None = None


# ---------------------------------------------------------------------------
# Typed RPCs for subprocess-isolated sampling
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _SampleRPC(SidecarRPC):
    """Typed RPC for SamplingClient.sample()."""

    prompt: types.ModelInput | types.OpenAIChatPrompt
    num_samples: int
    sampling_params: types.SamplingParams
    include_prompt_logprobs: bool
    topk_prompt_logprobs: int
    env_id: str | None = None

    async def execute(self, target: Any) -> Any:
        return target.sample(
            prompt=self.prompt,
            num_samples=self.num_samples,
            sampling_params=self.sampling_params,
            include_prompt_logprobs=self.include_prompt_logprobs,
            topk_prompt_logprobs=self.topk_prompt_logprobs,
            env_id=self.env_id,
        )


@dataclasses.dataclass
class _ComputeLogprobsRPC(SidecarRPC):
    """Typed RPC for SamplingClient.compute_logprobs()."""

    prompt: types.ModelInput

    async def execute(self, target: Any) -> Any:
        return target.compute_logprobs(prompt=self.prompt)


class SamplingClient(TelemetryProvider, QueueStateObserver):
    """Client for text generation and inference from trained or base models.

    The SamplingClient lets you generate text tokens from either a base model or from weights
    you've saved using a TrainingClient. You typically get one by calling
    `tinker_client.create_sampling_client()` or `training_client.save_weights_and_get_sampling_client()`.

    Key methods:
    - sample() - generate text completions with customizable parameters
    - compute_logprobs() - get log probabilities for prompt tokens

    Create method parameters:
    - `model_path`: Path to saved model weights (starts with 'tinker://')
    - `base_model`: Name of base model to use for inference (e.g., 'Qwen/Qwen3-8B')
    - `retry_config`: Configuration for retrying failed requests

    Example:
    ```python
    sampling_client = tinker_client.create_sampling_client(base_model="Qwen/Qwen3-8B")
    prompt = types.ModelInput.from_ints(tokenizer.encode("The weather today is"))
    params = types.SamplingParams(max_tokens=20, temperature=0.7)
    future = sampling_client.sample(prompt=prompt, sampling_params=params, num_samples=1)
    result = future.result()
    ```

    Multi-processing support:
    This class is picklable, so it can be passed to a separate process/worker to sample. It is also
    safe to pass the same instance of SamplingClient to multiple processes/workers.

    If you are using Tinker SDK with more than one process you should always create SamplingClient from
    the main process and then pass it to the other processes/workers.
    TinkerClient and TrainingClient should always be managed from the main process.

    Subprocess isolation:
    Set ``TINKER_SUBPROCESS_SAMPLING=1`` to run sample() and compute_logprobs() in a dedicated
    subprocess, preventing GIL contention from CPU-heavy user code (grading, environment
    interactions) from stalling networking IO and heartbeats. This is transparent — the same
    API works with or without it.
    """

    def __init__(
        self,
        holder: InternalClientHolder,
        *,
        sampling_session_id: str,
        shadow: bool = False,
        retry_config: RetryConfig | None = None,
        subprocess_sampling: bool | None = None,
        runtime_id: str | None = None,
    ):
        self.holder = holder

        # Create retry handler with the provided configuration
        self.retry_handler = _get_retry_handler(
            sampling_session_id, retry_config=retry_config, telemetry=holder.get_telemetry()
        )

        self.feature_gates = set(
            os.environ.get("TINKER_FEATURE_GATES", "async_sampling").split(",")
        )

        self._last_queue_state_logged: float = 0

        self._sampling_session_id: str = sampling_session_id
        self._runtime_id = runtime_id

        self._request_id_counter: int = 0
        if shadow:
            # Start request_id_counter at a random high value to avoid collisions
            # with the original client or other unpickled copies
            # We use 1B as the base and mod for uuid because the maximum int value is 2^63-1 and 1B*1B is less than 2^63-1.
            self._request_id_counter = 1_000_000_000 * (int(uuid.uuid4()) % 1_000_000_000 + 1)

        # Subprocess isolation: read env var if not explicitly set
        if subprocess_sampling is None:
            subprocess_sampling = os.environ.get("TINKER_SUBPROCESS_SAMPLING", "").lower() in (
                "1",
                "true",
                "yes",
            )
        self._sampling_client_sidecar_handle: SidecarHandle | None = None
        if subprocess_sampling:
            from tinker.lib.sidecar import _inside_sidecar

            if not _inside_sidecar:
                self._sampling_client_sidecar_handle = create_sidecar_handle(self)

    @staticmethod
    async def _create_impl(
        holder: InternalClientHolder,
        *,
        model_path: str | None,
        base_model: str | None,
        sampling_session_id: str | None,
        retry_config: RetryConfig | None,
        runtime_id: str | None = None,
    ) -> SamplingClient:
        if sampling_session_id is None:
            if runtime_id is not None:
                sampling_session_id = await _create_runtime_sampling_session(
                    holder,
                    runtime_id=runtime_id,
                    model_path=model_path,
                    base_model=base_model,
                )
            else:
                sampling_session_id = await holder._create_sampling_session(
                    model_path=model_path, base_model=base_model
                )
        return SamplingClient(
            holder,
            sampling_session_id=sampling_session_id,
            retry_config=retry_config,
            runtime_id=runtime_id,
        )

    @staticmethod
    def create(
        holder: InternalClientHolder,
        *,
        model_path: str | None = None,
        base_model: str | None = None,
        sampling_session_id: str | None = None,
        retry_config: RetryConfig | None = None,
        runtime_id: str | None = None,
    ) -> APIFuture[SamplingClient]:
        return holder.run_coroutine_threadsafe(
            SamplingClient._create_impl(
                holder,
                model_path=model_path,
                base_model=base_model,
                sampling_session_id=sampling_session_id,
                retry_config=retry_config,
                runtime_id=runtime_id,
            )
        )

    async def _send_asample_request(
        self,
        num_samples: int,
        prompt: types.ModelInput | types.OpenAIChatPrompt,
        sampling_params: types.SamplingParams,
        include_prompt_logprobs: bool,
        topk_prompt_logprobs: int,
        env_id: str | None = None,
    ):
        try:
            request_id = self._request_id_counter
            self._request_id_counter += 1
            with self.holder.aclient(ClientConnectionPoolType.SAMPLE) as client:
                if self._runtime_id is not None:
                    request = types.RuntimeSampleRequest(
                        sampling_session_id=self._sampling_session_id,
                        seq_id=request_id,
                        num_samples=num_samples,
                        prompt=prompt if isinstance(prompt, types.OpenAIChatPrompt) else None,
                        model_input=prompt if isinstance(prompt, types.ModelInput) else None,
                        sampling_params=sampling_params,
                        prompt_logprobs=include_prompt_logprobs,
                        topk_prompt_logprobs=topk_prompt_logprobs,
                        env_id=env_id,
                    )
                    payload = await client.post(
                        f"/api/v1/sdk/{self._runtime_id}/asample",
                        body=model_dump(request, exclude_unset=False, exclude_none=True, mode="json"),
                        options=make_request_options(
                            extra_headers={"X-Tinker-Sampling-Backpressure": "1"},
                        ),
                        cast_to=cast(Any, dict),
                    )
                    return types.UntypedAPIFuture.model_validate(payload)
                if not isinstance(prompt, types.ModelInput):
                    raise TypeError("OpenAIChatPrompt sampling requires a backend runtime")
                request = types.SampleRequest(
                    sampling_session_id=self._sampling_session_id,
                    seq_id=request_id,
                    num_samples=num_samples,
                    prompt=prompt,
                    sampling_params=sampling_params,
                    prompt_logprobs=include_prompt_logprobs,
                    topk_prompt_logprobs=topk_prompt_logprobs,
                    env_id=env_id,
                )
                return await client.sampling.asample(
                    request=request,
                    max_retries=0,
                    extra_headers={"X-Tinker-Sampling-Backpressure": "1"},
                )
        except tinker.APIStatusError as e:
            if e.status_code == 429:
                return None
            raise e

    async def _sample_async_impl(
        self,
        prompt: types.ModelInput | types.OpenAIChatPrompt,
        num_samples: int,
        sampling_params: types.SamplingParams,
        include_prompt_logprobs: bool,
        topk_prompt_logprobs: int = 0,
        env_id: str | None = None,
    ) -> types.SampleResponse:
        if isinstance(prompt, types.ModelInput):
            estimated_bytes_count = self.holder.estimate_bytes_count_in_model_input(prompt)
        else:
            estimated_bytes_count = len(
                json.dumps(model_dump(prompt, mode="json"), ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
        async with self.holder.sample_dispatch_rate_limit(estimated_bytes_count):
            while True:
                if (
                    self.holder._sample_backoff_until is not None
                    and time.monotonic() < self.holder._sample_backoff_until
                ):
                    await asyncio.sleep(1)
                    continue

                untyped_future = await self.holder.execute_with_retries(
                    self._send_asample_request,
                    num_samples,
                    prompt,
                    sampling_params,
                    include_prompt_logprobs,
                    topk_prompt_logprobs,
                    env_id,
                )
                if untyped_future is not None:
                    break
                # Handle backoff
                backoff_duration = 1 if estimated_bytes_count <= 128 * 1024 else 5
                self.holder._sample_backoff_until = time.monotonic() + backoff_duration
                continue

        return await _APIFuture(
            types.SampleResponse,
            self.holder,
            untyped_future,
            request_start_time=time.time(),
            request_type="Sample",
            queue_state_observer=self,
        ).result_async()

    @capture_exceptions(fatal=True)
    def sample(
        self,
        prompt: types.ModelInput | types.OpenAIChatPrompt,
        num_samples: int,
        sampling_params: types.SamplingParams,
        include_prompt_logprobs: bool = False,
        topk_prompt_logprobs: int = 0,
        env_id: str | None = None,
    ) -> ConcurrentFuture[types.SampleResponse]:
        """Generate text completions from the model.

        Args:
        - `prompt`: Tokenized ModelInput, or an OpenAIChatPrompt for a backend runtime
        - `num_samples`: Number of independent samples to generate
        - `sampling_params`: Parameters controlling generation (temperature, max_tokens, etc.)
        - `include_prompt_logprobs`: Whether to include log probabilities for prompt tokens
        - `topk_prompt_logprobs`: Number of top token log probabilities to return per position
        - `env_id`: Optional environment ID for task-based sampling

        Returns:
        - A `Future` containing the `SampleResponse` with generated text

        Example:
        ```python
        prompt = types.ModelInput.from_ints(tokenizer.encode("The weather today is"))
        params = types.SamplingParams(max_tokens=20, temperature=0.7)
        future = sampling_client.sample(prompt=prompt, sampling_params=params, num_samples=1)
        result = future.result()
        for sample in result.samples:
            print(tokenizer.decode(sample.tokens))
        ```
        """
        if self._sampling_client_sidecar_handle is not None:
            return self._sampling_client_sidecar_handle.submit_rpc(
                _SampleRPC(
                    prompt=prompt,
                    num_samples=num_samples,
                    sampling_params=sampling_params,
                    include_prompt_logprobs=include_prompt_logprobs,
                    topk_prompt_logprobs=topk_prompt_logprobs,
                    env_id=env_id,
                )
            )

        async def _sample_async():
            return await self._sample_async_impl(
                prompt,
                num_samples,
                sampling_params,
                include_prompt_logprobs,
                topk_prompt_logprobs,
                env_id,
            )

        @capture_exceptions(fatal=True)
        async def _sample_async_with_retries() -> types.SampleResponse:
            return await self.retry_handler.execute(_sample_async)

        # TODO make max_tokens a required field
        return self.holder.run_coroutine_threadsafe(_sample_async_with_retries()).future()

    async def sample_async(
        self,
        prompt: types.ModelInput | types.OpenAIChatPrompt,
        num_samples: int,
        sampling_params: types.SamplingParams,
        include_prompt_logprobs: bool = False,
        topk_prompt_logprobs: int = 0,
        env_id: str | None = None,
    ) -> types.SampleResponse:
        """Async version of sample."""
        return await AwaitableConcurrentFuture(
            self.sample(
                prompt,
                num_samples,
                sampling_params,
                include_prompt_logprobs,
                topk_prompt_logprobs,
                env_id,
            )
        )

    @capture_exceptions(fatal=True)
    def compute_logprobs(self, prompt: types.ModelInput) -> ConcurrentFuture[list[float | None]]:
        """Compute log probabilities for prompt tokens.

        Args:
        - `prompt`: The input tokens as ModelInput

        Returns:
        - A `Future` containing a list of log probabilities for each token in the prompt.
            None values indicate tokens where log probabilities couldn't be computed.

        Example:
        ```python
        prompt = types.ModelInput.from_ints(tokenizer.encode("Hello world"))
        future = sampling_client.compute_logprobs(prompt)
        logprobs = future.result()
        for i, logprob in enumerate(logprobs):
            if logprob is not None:
                print(f"Token {i}: logprob = {logprob:.4f}")
        ```
        """
        if self._sampling_client_sidecar_handle is not None:
            return self._sampling_client_sidecar_handle.submit_rpc(
                _ComputeLogprobsRPC(prompt=prompt)
            )

        async def _compute_logprobs_async() -> list[float | None]:
            sample_res = await self._sample_async_impl(
                prompt,
                num_samples=1,
                sampling_params=types.SamplingParams(max_tokens=1),
                include_prompt_logprobs=True,
            )
            return cast(list[float | None], sample_res.prompt_logprobs)

        @capture_exceptions(fatal=True)
        async def _compute_logprobs_async_with_retries() -> list[float | None]:
            return await self.retry_handler.execute(_compute_logprobs_async)

        return self.holder.run_coroutine_threadsafe(_compute_logprobs_async_with_retries()).future()

    async def compute_logprobs_async(self, prompt: types.ModelInput) -> list[float | None]:
        """Async version of compute_logprobs."""
        return await AwaitableConcurrentFuture(self.compute_logprobs(prompt))

    def _get_sampler_submit(self) -> AwaitableConcurrentFuture[types.GetSamplerResponse]:
        async def _get_sampler_async() -> types.GetSamplerResponse:
            async def _send_request() -> types.GetSamplerResponse:
                with self.holder.aclient(ClientConnectionPoolType.TRAIN) as client:
                    return await client.get(
                        f"/api/v1/samplers/{self._sampling_session_id}",
                        cast_to=types.GetSamplerResponse,
                    )

            return await self.holder.execute_with_retries(_send_request)

        return self.holder.run_coroutine_threadsafe(_get_sampler_async())

    @capture_exceptions(fatal=True)
    def get_tokenizer(self) -> PreTrainedTokenizer:
        """Get the tokenizer for the current model.

        Returns:
        - `PreTrainedTokenizer` compatible with the model
        """
        sampler_info = self._get_sampler_submit().result()
        return _load_tokenizer_from_model_info(sampler_info.base_model)

    @capture_exceptions(fatal=True)
    def get_base_model(self) -> str:
        """Get the base model name for the current sampling session."""
        return self._get_sampler_submit().result().base_model

    async def get_base_model_async(self) -> str:
        """Async version of get_base_model."""
        return (await self._get_sampler_submit()).base_model

    def get_telemetry(self) -> Telemetry | None:
        return self.holder.get_telemetry()

    def __reduce__(self) -> tuple[Any, tuple[_SamplingClientPickleState]]:
        """Enable pickling of SamplingClient for subprocess use.

        Serializes into a ``_SamplingClientPickleState`` dataclass. The
        ``_sampling_client_sidecar_handle`` handle is deliberately omitted — only a
        bool flag is stored. The unpickled copy creates its own handle via
        the per-process sidecar singleton. Do not add ``__getstate__``
        without preserving this behavior.
        """
        return (
            _unpickle_sampling_client,
            (
                _SamplingClientPickleState(
                    session_id=self.holder.get_session_id(),
                    sampling_session_id=self._sampling_session_id,
                    constructor_kwargs=self.holder.shadow_kwargs,
                    subprocess_sampling=self._sampling_client_sidecar_handle is not None,
                    runtime_id=self._runtime_id,
                ),
            ),
        )

    @capture_exceptions(fatal=True)
    def init_task_env(
        self,
        task: types.TaskDescriptor,
    ) -> str:
        """Initialize a task environment from a task descriptor.

        Returns:
        - `env_id`: The environment identifier string
        """

        async def _init_task_env_async() -> str:
            metadata = dict(task.metadata or {})
            metadata.setdefault("bench_name", task.bench_name)
            request = types.InitTaskEnvRequest(
                task_id=task.task_id,
                dataset=task.dataset,
                split=task.split,
                metadata=metadata,
            )
            with self.holder.aclient(ClientConnectionPoolType.SAMPLE) as client:
                response = await client.sampling.init_task_env(request=request, timeout=2000)
                return response.env_id

        async def _init_task_env_with_retries() -> str:
            return await self.retry_handler.execute(_init_task_env_async)

        return self.holder.run_coroutine_threadsafe(_init_task_env_with_retries()).result()

    @capture_exceptions(fatal=True)
    def get_step(
        self,
        env_id: str,
        step_id: int | None = None,
    ) -> types.GetStepResponse:
        """Get a step for a task environment.

        Args:
        - `env_id`: Environment identifier
        - `step_id`: Step number to retrieve (None means latest/current)

        Returns:
        - `GetStepResponse` containing step_id, prompt, finish_reason, reward
        """

        async def _get_step_async() -> types.GetStepResponse:
            request = types.GetStepRequest(env_id=env_id, step_id=step_id)
            with self.holder.aclient(ClientConnectionPoolType.SAMPLE) as client:
                try:
                    return await client.sampling.get_step(request=request, timeout=360)
                except tinker.APIStatusError as e:
                    if e.status_code == 408:
                        logger.warning(
                            "get_step(env_id=%s, step_id=%s): 暂时没返回结果，继续等待",
                            env_id,
                            step_id,
                        )
                    raise
                except tinker.APITimeoutError:
                    logger.warning(
                        "get_step(env_id=%s, step_id=%s): 暂时没返回结果，继续等待",
                        env_id,
                        step_id,
                    )
                    raise

        async def _get_step_with_retries() -> types.GetStepResponse:
            return await self.retry_handler.execute(_get_step_async)

        return self.holder.run_coroutine_threadsafe(_get_step_with_retries()).result()

    def on_queue_state_change(
        self, queue_state: QueueState, queue_state_reason: str | None
    ) -> None:
        QUEUE_STATE_LOG_INTERVAL = 60
        if queue_state == QueueState.ACTIVE:
            return
        if time.time() - self._last_queue_state_logged < QUEUE_STATE_LOG_INTERVAL:
            return
        if not queue_state_reason:
            if queue_state == QueueState.PAUSED_RATE_LIMIT:
                queue_state_reason = "concurrent sampler weights limit hit"
            elif queue_state == QueueState.PAUSED_CAPACITY:
                queue_state_reason = "Tinker backend is running short on capacity, please wait"
            else:
                queue_state_reason = "unknown"
        self._last_queue_state_logged = time.time()

        logger.warning(
            f"Sampling is paused for sampler {self._sampling_session_id}. Reason: {queue_state_reason}"
        )


def _unpickle_sampling_client(state: _SamplingClientPickleState) -> SamplingClient:
    """Reconstruct a SamplingClient from pickled state.

    Creates a shadow InternalClientHolder and builds a new SamplingClient.
    Subprocess enablement is handled by the constructor.
    """
    from ..internal_client_holder import InternalClientHolder

    holder = InternalClientHolder.get_shadow_holder(state.session_id, state.constructor_kwargs)
    return SamplingClient(
        holder,
        sampling_session_id=state.sampling_session_id,
        shadow=True,
        subprocess_sampling=state.subprocess_sampling,
        runtime_id=state.runtime_id,
    )


async def _create_runtime_sampling_session(
    holder: InternalClientHolder,
    *,
    runtime_id: str,
    model_path: str | None = None,
    base_model: str | None = None,
) -> str:
    if model_path and not model_path.startswith("tinker://"):
        raise ValueError("model_path must start with 'tinker://'")
    assert holder._sampling_client_counter is not None
    sampling_session_seq_id = holder._sampling_client_counter
    holder._sampling_client_counter += 1
    request = types.CreateSamplingSessionRequest(
        session_id=holder.get_session_id(),
        sampling_session_seq_id=sampling_session_seq_id,
        model_path=model_path,
        base_model=base_model,
    )
    with holder.aclient(ClientConnectionPoolType.SESSION) as client:
        payload = await client.post(
            f"/api/v1/sdk/{runtime_id}/create_sampling_session",
            body=model_dump(request, exclude_unset=False, exclude_none=True, mode="json"),
            options=make_request_options(),
            cast_to=types.CreateSamplingSessionResponse,
        )
    return payload.sampling_session_id


@lru_cache(maxsize=100)
def _get_retry_handler(
    name: str, retry_config: RetryConfig | None = None, telemetry: Telemetry | None = None
) -> RetryHandler:
    retry_config = retry_config or RetryConfig()
    return RetryHandler(config=retry_config, name=name, telemetry=telemetry)


@lru_cache(maxsize=32)
def _load_tokenizer_from_model_info(
    model_name: str, tokenizer_id: str | None = None
) -> PreTrainedTokenizer:
    """Load a tokenizer given a model name and optional tokenizer_id.

    This is a shared helper used by both TrainingClient and SamplingClient.

    Args:
        model_name: The model name (e.g., "Qwen/Qwen3-8B")
        tokenizer_id: Optional explicit tokenizer ID. If None, heuristics are applied.

    Returns:
        The loaded PreTrainedTokenizer
    """
    from transformers.models.auto.tokenization_auto import AutoTokenizer

    model_name = model_name.split(":")[0]

    # Use tokenizer_id if provided, otherwise fall back to heuristic logic
    kwargs = {}
    if tokenizer_id is None:
        # We generally adhere to the huggingface convention of "<org>/<model>" but
        # in some cases we'll deploy variants using the format
        # "<org>/<model>/<variant>". In that case, we want to load the tokenizer
        # using the huggingface convention.
        if model_name.startswith("meta-llama/Llama-3"):
            # Avoid gating of Llama 3 models:
            tokenizer_id = "thinkingmachineslabinc/meta-llama-3-instruct-tokenizer"
        elif model_name.count("/") == 2:
            org, model, _variant = model_name.split("/", 2)
            tokenizer_id = f"{org}/{model}"
        else:
            tokenizer_id = model_name

    if tokenizer_id.startswith("TML/"):
        from tml_tokenizers.tinker_tokenizers import get_tinker_tokenizer

        if (tokenizer := get_tinker_tokenizer(tokenizer_id)) is not None:
            return tokenizer

    if tokenizer_id == "moonshotai/Kimi-K2-Thinking":
        kwargs = {
            "trust_remote_code": True,
            "revision": "612681931a8c906ddb349f8ad0f582cb552189cd",
        }

    if tokenizer_id in ("moonshotai/Kimi-K2.5-Text-Only", "moonshotai/Kimi-K2.5"):
        tokenizer_id = "moonshotai/Kimi-K2.5"
        kwargs = {
            "trust_remote_code": True,
            "revision": "2426b45b6af0da48d0dcce71bbce6225e5c73adc",
        }

    return AutoTokenizer.from_pretrained(tokenizer_id, fast=True, **kwargs)
