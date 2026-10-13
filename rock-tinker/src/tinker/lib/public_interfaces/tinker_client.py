# Derived from https://github.com/thinking-machines-lab/tinker (Apache-2.0).
# Modified in this Tinker fork; see rock-tinker/UPSTREAM.md and NOTICE.
"""TinkerClient for the Tinker backend protocol."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tinker import types
from tinker.lib.client_connection_pool_type import ClientConnectionPoolType
from tinker.lib.public_interfaces.api_future import AwaitableConcurrentFuture
from tinker.lib.telemetry import Telemetry, capture_exceptions
from tinker.lib.telemetry_provider import TelemetryProvider

from ..api_future_impl import _APIFuture
from ..internal_client_holder import InternalClientHolder
from ..queue_state_logger import QueueStateLogger
from ..retry_handler import RetryConfig
from ..sync_only import sync_only

if TYPE_CHECKING:
    from .rest_client import RestClient
    from .runtime_client import RuntimeClient
    from .sampling_client import SamplingClient
    from .training_client import TrainingClient

# pyright: reportPrivateImportUsage=false

logger = logging.getLogger(__name__)


class TinkerClient(TelemetryProvider):
    """Protocol client for an already-running Tinker backend.

    `ServerJob` owns backend lifecycle. In normal cookbook/runtime flows, get a
    `TinkerClient` from `await job.get_client()` instead of constructing one
    directly. Direct construction is reserved for advanced attach or test flows
    where the caller already knows the backend `base_url`.

    The client provides methods to:
    - Query server capabilities and health status
    - Generate TrainingClient instances for model training workflows
    - Generate SamplingClient instances for text generation and inference
    - Generate RestClient instances for REST API operations like listing weights

    Args:
        user_metadata: Optional metadata attached to the created session.
        project_id: Optional project ID to attach to the created session.
        **kwargs: advanced options passed to the underlying HTTP client,
                 including API keys, headers, base URLs, and connection settings.

    Example:
    ```python
    async with tinker.ServerJob.open(config_path="runtime.yaml") as job:
        client = await job.get_client()
        capabilities = await client.get_server_capabilities_async()
    ```
    """

    def __init__(
        self,
        user_metadata: dict[str, str] | None = None,
        project_id: str | None = None,
        **kwargs: Any,
    ):
        default_headers = _get_default_headers() | kwargs.pop("default_headers", {})
        self.holder = InternalClientHolder(
            user_metadata=user_metadata,
            project_id=project_id,
            **kwargs,
            default_headers=default_headers,
            _strict_response_validation=True,
        )
        logger.info(f"TinkerClient initialized for session {self.holder._session_id}")

    @property
    def backend_base_url(self) -> str:
        base_url = self.holder._constructor_kwargs.get("base_url")
        return str(base_url) if base_url is not None else ""

    def close(self) -> None:
        self.holder.close()

    def __enter__(self) -> "TinkerClient":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    async def __aenter__(self) -> "TinkerClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _get_server_capabilities_submit(
        self,
    ) -> AwaitableConcurrentFuture[types.GetServerCapabilitiesResponse]:
        async def _get_server_capabilities_async():
            async def _send_request():
                with self.holder.aclient(ClientConnectionPoolType.TRAIN) as client:
                    return await client.service.get_server_capabilities()

            return await self.holder.execute_with_retries(_send_request)

        return self.holder.run_coroutine_threadsafe(_get_server_capabilities_async())

    @sync_only
    @capture_exceptions(fatal=True)
    def get_server_capabilities(self) -> types.GetServerCapabilitiesResponse:
        """Query the server's supported features and capabilities.

        Returns:
        - `GetServerCapabilitiesResponse` with available models, features, and limits

        Example:
        ```python
        capabilities = tinker_client.get_server_capabilities()
        print(f"Supported models: {capabilities.supported_models}")
        print(f"Max batch size: {capabilities.max_batch_size}")
        ```
        """
        return self._get_server_capabilities_submit().result()

    @capture_exceptions(fatal=True)
    async def get_server_capabilities_async(self) -> types.GetServerCapabilitiesResponse:
        """Async version of get_server_capabilities."""
        return await self._get_server_capabilities_submit()

    def _create_runtime_submit(
        self,
        *,
        runtime_type: str,
        config_path: str | os.PathLike[str] | None = None,
        config_content: str | None = None,
        config_type: str = "yaml",
        user_metadata: dict[str, Any] | None = None,
    ) -> AwaitableConcurrentFuture[RuntimeClient]:
        normalized_runtime_type = runtime_type.strip().lower()
        normalized_config_type = config_type.strip().lower()
        resolved_config_content = _resolve_runtime_config_content(
            config_path=config_path,
            config_content=config_content,
        )
        session_id = self.holder.get_session_id()
        config_source = str(config_path) if config_path is not None else "<inline config_content>"

        async def _create_runtime_async() -> RuntimeClient:
            start_time = time.time()
            logger.info(
                "create_runtime: submit runtime_type=%s config_type=%s config_source=%s session_id=%s",
                normalized_runtime_type,
                normalized_config_type,
                config_source,
                session_id,
            )
            with self.holder.aclient(ClientConnectionPoolType.SESSION) as client:
                request = types.CreateRuntimeRequest(
                    runtime_type=normalized_runtime_type,
                    config_type=normalized_config_type,
                    config_content=resolved_config_content,
                    session_id=session_id,
                    user_metadata=user_metadata,
                )
                future = await client.service.create_runtime(request=request)
            logger.info(
                "create_runtime: waiting for ready future request_id=%s runtime_id=%s status=%s",
                future.request_id,
                future.runtime_id,
                future.status,
            )
            runtime_info = await _APIFuture(
                types.CreateRuntimeResponse,
                self.holder,
                future,
                request_start_time=start_time,
                request_type="CreateRuntime",
            ).result_async()
            from .runtime_client import RuntimeClient

            logger.info(
                "create_runtime: ready runtime_id=%s runtime_type=%s status=%s ready=%s elapsed=%.1fs",
                runtime_info.runtime_id,
                runtime_info.runtime_type,
                runtime_info.status,
                runtime_info.ready,
                time.time() - start_time,
            )

            return RuntimeClient(self.holder, runtime_info)

        return self.holder.run_coroutine_threadsafe(_create_runtime_async())

    @capture_exceptions(fatal=True)
    def create_runtime(
        self,
        *,
        runtime_type: str,
        config_path: str | os.PathLike[str] | None = None,
        config_content: str | None = None,
        config_type: str = "yaml",
        user_metadata: dict[str, Any] | None = None,
    ) -> AwaitableConcurrentFuture[RuntimeClient]:
        """Create a backend runtime and return an awaitable ready future.

        Exactly one of `config_path` or `config_content` must be provided.
        `config_path` is read locally by the SDK; the backend receives opaque
        `config_type` and `config_content` fields.

        Use `runtime = await tinker_client.create_runtime(...)` in async code,
        or `tinker_client.create_runtime(...).result()` in sync code.
        """
        return self._create_runtime_submit(
            runtime_type=runtime_type,
            config_path=config_path,
            config_content=config_content,
            config_type=config_type,
            user_metadata=user_metadata,
        )

    @sync_only
    @capture_exceptions(fatal=True)
    def create_runtime_blocking(
        self,
        *,
        runtime_type: str,
        config_path: str | os.PathLike[str] | None = None,
        config_content: str | None = None,
        config_type: str = "yaml",
        user_metadata: dict[str, Any] | None = None,
    ) -> RuntimeClient:
        """Blocking helper for code that cannot await `create_runtime`."""
        return self._create_runtime_submit(
            runtime_type=runtime_type,
            config_path=config_path,
            config_content=config_content,
            config_type=config_type,
            user_metadata=user_metadata,
        ).result()

    def _list_tasks_submit(
        self,
        *,
        dataset: str | None = None,
        split: str | None = None,
        bench_name: str | None = None,
        task_filter: str | None = None,
        datasets: list[types.DatasetSpec] | None = None,
    ) -> AwaitableConcurrentFuture[list[types.TaskDescriptor]]:
        if datasets is None:
            if dataset is None or split is None or bench_name is None:
                raise ValueError("dataset, split, and bench_name are required when datasets is not provided")
            datasets = [
                types.DatasetSpec(
                    dataset=dataset,
                    split=split,
                    bench_name=bench_name,
                    task_filter=task_filter,
                )
            ]
        elif any(value is not None for value in (dataset, split, bench_name, task_filter)):
            raise ValueError("pass either datasets or dataset/split/bench_name/task_filter, not both")

        request = types.ListTasksRequest(datasets=datasets)

        async def _list_tasks_async() -> list[types.TaskDescriptor]:
            with self.holder.aclient(ClientConnectionPoolType.SESSION) as client:
                response = await client.service.list_tasks(request=request)
            return list(response.tasks)

        return self.holder.run_coroutine_threadsafe(_list_tasks_async())

    @capture_exceptions(fatal=True)
    async def list_tasks(
        self,
        *,
        dataset: str | None = None,
        split: str | None = None,
        bench_name: str | None = None,
        task_filter: str | None = None,
        datasets: list[types.DatasetSpec] | None = None,
    ) -> list[types.TaskDescriptor]:
        """List backend task descriptors from dataset catalogs."""
        return await self._list_tasks_submit(
            dataset=dataset,
            split=split,
            bench_name=bench_name,
            task_filter=task_filter,
            datasets=datasets,
        )

    @sync_only
    @capture_exceptions(fatal=True)
    def list_tasks_blocking(
        self,
        *,
        dataset: str | None = None,
        split: str | None = None,
        bench_name: str | None = None,
        task_filter: str | None = None,
        datasets: list[types.DatasetSpec] | None = None,
    ) -> list[types.TaskDescriptor]:
        """Blocking helper for `list_tasks`."""
        return self._list_tasks_submit(
            dataset=dataset,
            split=split,
            bench_name=bench_name,
            task_filter=task_filter,
            datasets=datasets,
        ).result()

    def _create_lora_training_client_submit(
        self,
        base_model: str,
        rank: int,
        seed: int | None,
        train_mlp: bool,
        train_attn: bool,
        train_unembed: bool,
        user_metadata: dict[str, str] | None,
    ) -> AwaitableConcurrentFuture[TrainingClient]:
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

        async def _create_lora_training_client_async():
            start_time = time.time()
            with self.holder.aclient(ClientConnectionPoolType.TRAIN) as client:
                request = types.CreateModelRequest(
                    session_id=session_id,
                    model_seq_id=model_seq_id,
                    base_model=base_model,
                    lora_config=lora_config,
                    user_metadata=user_metadata,
                )
                future = await client.models.create(request=request)
            create_model_response = await _APIFuture(
                types.CreateModelResponse,
                self.holder,
                future,
                request_start_time=start_time,
                request_type="CreateModel",
                queue_state_observer=QueueStateLogger(base_model, "Model creation"),
            ).result_async()
            model_id = create_model_response.model_id
            from .training_client import TrainingClient

            training_client = TrainingClient(
                self.holder, model_seq_id=model_seq_id, model_id=model_id
            )
            logger.info(f"TrainingClient initialized for model {model_id}")
            return training_client

        return self.holder.run_coroutine_threadsafe(_create_lora_training_client_async())

    @sync_only
    @capture_exceptions(fatal=True)
    def create_lora_training_client(
        self,
        base_model: str,
        rank: int = 32,
        seed: int | None = None,
        train_mlp: bool = True,
        train_attn: bool = True,
        train_unembed: bool = True,
        user_metadata: dict[str, str] | None = None,
    ) -> TrainingClient:
        """Create a TrainingClient for LoRA fine-tuning.

        Args:
        - `base_model`: Name of the base model to fine-tune (e.g., "Qwen/Qwen3-8B")
        - `rank`: LoRA rank controlling the size of adaptation matrices (default 32)
        - `seed`: Random seed for initialization. None means random seed.
        - `train_mlp`: Whether to train MLP layers (default True)
        - `train_attn`: Whether to train attention layers (default True)
        - `train_unembed`: Whether to train unembedding layers (default True)
        - `user_metadata`: Optional metadata to attach to the training run

        Returns:
        - `TrainingClient` configured for LoRA training

        Example:
        ```python
        training_client = tinker_client.create_lora_training_client(
            base_model="Qwen/Qwen3-8B",
            rank=16,
            train_mlp=True,
            train_attn=True
        )
        # Now use training_client.forward_backward() to train
        ```
        """
        return self._create_lora_training_client_submit(
            base_model,
            rank,
            seed,
            train_mlp,
            train_attn,
            train_unembed,
            user_metadata,
        ).result()

    @capture_exceptions(fatal=True)
    async def create_lora_training_client_async(
        self,
        base_model: str,
        rank: int = 32,
        seed: int | None = None,
        train_mlp: bool = True,
        train_attn: bool = True,
        train_unembed: bool = True,
        user_metadata: dict[str, str] | None = None,
    ) -> TrainingClient:
        """Async version of create_lora_training_client."""
        return await self._create_lora_training_client_submit(
            base_model,
            rank,
            seed,
            train_mlp,
            train_attn,
            train_unembed,
            user_metadata,
        ).result_async()

    def _get_rest_client_for_weights(self, weights_access_token: str | None = None) -> RestClient:
        """Get a rest client for weights info lookups.

        If weights_access_token is provided, creates a separate TinkerClient
        authenticated with that token.
        """
        if weights_access_token is not None:
            token_client = TinkerClient(
                api_key=weights_access_token, **self.holder._constructor_kwargs
            )
            return token_client.create_rest_client()
        return self.create_rest_client()

    @sync_only
    @capture_exceptions(fatal=True)
    def create_training_client_from_state(
        self,
        path: str,
        user_metadata: dict[str, str] | None = None,
        weights_access_token: str | None = None,
    ) -> TrainingClient:
        """Create a TrainingClient from saved model weights.

        This loads only the model weights, not optimizer state. To also restore
        optimizer state (e.g., Adam momentum), use create_training_client_from_state_with_optimizer.

        Args:
        - `path`: Tinker path to saved weights (e.g., "tinker://run-id/weights/checkpoint-001")
        - `user_metadata`: Optional metadata to attach to the new training run
        - `weights_access_token`: Optional access token for loading checkpoints under a different account.

        Returns:
        - `TrainingClient` loaded with the specified weights

        Example:
        ```python
        # Resume training from a checkpoint (weights only, optimizer resets)
        training_client = tinker_client.create_training_client_from_state(
            "tinker://run-id/weights/checkpoint-001"
        )
        # Continue training from the loaded state
        ```
        """
        rest_client = self._get_rest_client_for_weights(weights_access_token)
        # Use weights info endpoint which allows access to models with public checkpoints
        weights_info = rest_client.get_weights_info_by_tinker_path(path).result()

        training_client = self.create_lora_training_client(
            base_model=weights_info.base_model,
            rank=weights_info.lora_rank,
            train_unembed=weights_info.train_unembed
            if weights_info.train_unembed is not None
            else True,
            train_mlp=weights_info.train_mlp if weights_info.train_mlp is not None else True,
            train_attn=weights_info.train_attn if weights_info.train_attn is not None else True,
            user_metadata=user_metadata,
        )

        training_client.load_state(path, weights_access_token=weights_access_token).result()
        return training_client

    @capture_exceptions(fatal=True)
    async def create_training_client_from_state_async(
        self,
        path: str,
        user_metadata: dict[str, str] | None = None,
        weights_access_token: str | None = None,
    ) -> TrainingClient:
        """Async version of create_training_client_from_state."""
        rest_client = self._get_rest_client_for_weights(weights_access_token)
        # Use weights info endpoint which allows access to models with public checkpoints
        weights_info = await rest_client.get_weights_info_by_tinker_path(path)

        # Right now all training runs are LoRa runs.
        assert weights_info.is_lora and weights_info.lora_rank is not None

        training_client = await self.create_lora_training_client_async(
            base_model=weights_info.base_model,
            rank=weights_info.lora_rank,
            train_unembed=weights_info.train_unembed
            if weights_info.train_unembed is not None
            else True,
            train_mlp=weights_info.train_mlp if weights_info.train_mlp is not None else True,
            train_attn=weights_info.train_attn if weights_info.train_attn is not None else True,
            user_metadata=user_metadata,
        )

        load_future = await training_client.load_state_async(
            path, weights_access_token=weights_access_token
        )
        await load_future.result_async()
        return training_client

    @sync_only
    @capture_exceptions(fatal=True)
    def create_training_client_from_state_with_optimizer(
        self,
        path: str,
        user_metadata: dict[str, str] | None = None,
        weights_access_token: str | None = None,
    ) -> TrainingClient:
        """Create a TrainingClient from saved model weights and optimizer state.

        This is similar to create_training_client_from_state but also restores
        optimizer state (e.g., Adam momentum), which is useful for resuming
        training exactly where it left off.

        Args:
        - `path`: Tinker path to saved weights (e.g., "tinker://run-id/weights/checkpoint-001")
        - `user_metadata`: Optional metadata to attach to the new training run
        - `weights_access_token`: Optional access token for loading checkpoints under a different account.

        Returns:
        - `TrainingClient` loaded with the specified weights and optimizer state

        Example:
        ```python
        # Resume training from a checkpoint with optimizer state
        training_client = tinker_client.create_training_client_from_state_with_optimizer(
            "tinker://run-id/weights/checkpoint-001"
        )
        # Continue training with restored optimizer momentum
        ```
        """
        rest_client = self._get_rest_client_for_weights(weights_access_token)
        # Use weights info endpoint which allows access to models with public checkpoints
        weights_info = rest_client.get_weights_info_by_tinker_path(path).result()

        training_client = self.create_lora_training_client(
            base_model=weights_info.base_model,
            rank=weights_info.lora_rank,
            train_unembed=weights_info.train_unembed
            if weights_info.train_unembed is not None
            else True,
            train_mlp=weights_info.train_mlp if weights_info.train_mlp is not None else True,
            train_attn=weights_info.train_attn if weights_info.train_attn is not None else True,
            user_metadata=user_metadata,
        )

        training_client.load_state_with_optimizer(
            path, weights_access_token=weights_access_token
        ).result()
        return training_client

    @capture_exceptions(fatal=True)
    async def create_training_client_from_state_with_optimizer_async(
        self,
        path: str,
        user_metadata: dict[str, str] | None = None,
        weights_access_token: str | None = None,
    ) -> TrainingClient:
        """Async version of create_training_client_from_state_with_optimizer."""
        rest_client = self._get_rest_client_for_weights(weights_access_token)
        # Use weights info endpoint which allows access to models with public checkpoints
        weights_info = await rest_client.get_weights_info_by_tinker_path(path)

        # Right now all training runs are LoRa runs.
        assert weights_info.is_lora and weights_info.lora_rank is not None

        training_client = await self.create_lora_training_client_async(
            base_model=weights_info.base_model,
            rank=weights_info.lora_rank,
            train_unembed=weights_info.train_unembed
            if weights_info.train_unembed is not None
            else True,
            train_mlp=weights_info.train_mlp if weights_info.train_mlp is not None else True,
            train_attn=weights_info.train_attn if weights_info.train_attn is not None else True,
            user_metadata=user_metadata,
        )

        load_future = await training_client.load_state_with_optimizer_async(
            path, weights_access_token=weights_access_token
        )
        await load_future.result_async()
        return training_client

    @capture_exceptions(fatal=True)
    def create_sampling_client(
        self,
        model_path: str | None = None,
        base_model: str | None = None,
        retry_config: RetryConfig | None = None,
    ) -> SamplingClient:
        """Create a SamplingClient for text generation.

        Args:
        - `model_path`: Path to saved model weights (e.g., "tinker://run-id/weights/checkpoint-001")
        - `base_model`: Name of base model to use (e.g., "Qwen/Qwen3-8B")
        - `retry_config`: Optional configuration for retrying failed requests

        Returns:
        - `SamplingClient` configured for text generation

        Raises:
            ValueError: If neither model_path nor base_model is provided

        Example:
        ```python
        # Use a base model
        sampling_client = tinker_client.create_sampling_client(
            base_model="Qwen/Qwen3-8B"
        )

        # Or use saved weights
        sampling_client = tinker_client.create_sampling_client(
            model_path="tinker://run-id/weights/checkpoint-001"
        )
        ```
        """
        from .sampling_client import SamplingClient

        if model_path is None and base_model is None:
            raise ValueError("Either model_path or base_model must be provided")
        return SamplingClient.create(
            self.holder,
            model_path=model_path,
            base_model=base_model,
            retry_config=retry_config,
        ).result()

    @capture_exceptions(fatal=True)
    async def create_sampling_client_async(
        self,
        model_path: str | None = None,
        base_model: str | None = None,
        retry_config: RetryConfig | None = None,
    ) -> SamplingClient:
        """Async version of create_sampling_client."""
        from .sampling_client import SamplingClient

        if model_path is None and base_model is None:
            raise ValueError("Either model_path or base_model must be provided")
        return await SamplingClient.create(
            self.holder,
            model_path=model_path,
            base_model=base_model,
            retry_config=retry_config,
        )

    @capture_exceptions(fatal=True)
    def create_rest_client(self) -> RestClient:
        """Create a RestClient for REST API operations.

        The RestClient provides access to various REST endpoints for querying
        model information, checkpoints, sessions, and managing checkpoint visibility.

        Returns:
        - `RestClient` for accessing REST API endpoints

        Example:
        ```python
        rest_client = tinker_client.create_rest_client()

        # List checkpoints for a training run
        checkpoints = rest_client.list_checkpoints("run-id").result()

        # Get training run info
        training_run = rest_client.get_training_run("run-id").result()

        # Publish a checkpoint
        rest_client.publish_checkpoint_from_tinker_path(
            "tinker://run-id/weights/checkpoint-001"
        ).result()
        ```
        """
        from .rest_client import RestClient

        return RestClient(self.holder)

    def get_telemetry(self) -> Telemetry | None:
        return self.holder.get_telemetry()


def _get_default_headers() -> dict[str, str]:
    headers = {}

    if (api_key := os.environ.get("TINKER_API_KEY", "")) and "X-API-Key" not in headers:
        headers["X-API-Key"] = api_key

    if (
        client_id := os.environ.get("CLOUDFLARE_ACCESS_CLIENT_ID")
    ) and "CF-Access-Client-Id" not in headers:
        headers["CF-Access-Client-Id"] = client_id
    if (
        client_secret := os.environ.get("CLOUDFLARE_ACCESS_CLIENT_SECRET")
    ) and "CF-Access-Client-Secret" not in headers:
        headers["CF-Access-Client-Secret"] = client_secret
    return headers

def _resolve_runtime_config_content(
    *,
    config_path: str | os.PathLike[str] | None,
    config_content: str | None,
) -> str:
    if config_path is not None and config_content is not None:
        raise ValueError("config_path and config_content are mutually exclusive")
    if config_path is None and config_content is None:
        raise ValueError("config_path or config_content is required")
    if config_path is not None:
        content = Path(config_path).expanduser().read_text(encoding="utf-8")
    else:
        assert config_content is not None
        content = config_content
    if not content.strip():
        raise ValueError("runtime config content must not be empty")
    return content
