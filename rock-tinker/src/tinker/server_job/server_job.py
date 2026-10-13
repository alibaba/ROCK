"""Public ServerJob lifecycle wrapper for Tinker backend instances."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Coroutine, Generic, TypeVar

from tinker.lib.public_interfaces.tinker_client import TinkerClient
from tinker.server_job.config_types import (
    LocalServerJobConfig,
    load_server_job_config,
)
from tinker.server_job.platforms import create_server_job_provider
from tinker.server_job.platforms.base import ServerJobHandle, ServerJobProvider
from tinker.server_job.response_types import (
    ServerJobLogsResponse,
    ServerJobStatusResponse,
    ServerJobStopResponse,
)

T = TypeVar("T")


class _AwaitableContext(Generic[T]):
    def __init__(self, coroutine: Coroutine[Any, Any, T]):
        self._coroutine = coroutine
        self._result: T | None = None

    async def _get(self) -> T:
        if self._result is None:
            self._result = await self._coroutine
        return self._result

    def __await__(self):
        return self._get().__await__()

    async def __aenter__(self) -> T:
        result = await self._get()
        enter = getattr(result, "__aenter__", None)
        if enter is not None:
            return await enter()
        return result

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._result is None:
            return None
        exit_method = getattr(self._result, "__aexit__", None)
        if exit_method is not None:
            await exit_method(exc_type, exc, traceback)
        return None


class ServerJob:
    def __init__(
        self,
        *,
        handle: ServerJobHandle,
        provider: ServerJobProvider,
        api_key: str | None = None,
        owned: bool = True,
        client_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.handle = handle
        self.job_id = handle.job_id
        self.platform = handle.platform
        self.provider_job_id = handle.provider_job_id
        self.ingress_id = handle.ingress_id
        self.base_url = handle.base_url
        self.log_dir = handle.log_dir
        self.api_key = api_key
        self._provider = provider
        self._owned = owned
        self._client_kwargs = dict(client_kwargs or {})
        self._client: TinkerClient | None = None

    @classmethod
    def open(cls, *, config_path: str | Path, api_key: str | None = None, **kwargs: Any) -> _AwaitableContext["ServerJob"]:
        return _AwaitableContext(cls._open(config_path=config_path, api_key=api_key, **kwargs))

    @classmethod
    async def _open(cls, *, config_path: str | Path, api_key: str | None = None, **kwargs: Any) -> "ServerJob":
        config = load_server_job_config(config_path)
        provider = create_server_job_provider(config)
        handle = await provider.submit()
        return cls(handle=handle, provider=provider, api_key=api_key, owned=True, client_kwargs=kwargs)

    @classmethod
    async def attach(cls, job_id: str, *, api_key: str | None = None, **kwargs: Any) -> "ServerJob":
        jobs_root = Path(os.environ.get("TINKER_JOBS_ROOT", Path.home() / ".rock" / "tinker" / "jobs")).expanduser()
        job_path = jobs_root / job_id / "job.json"
        if job_path.exists():
            payload = json.loads(job_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError(f"ServerJob metadata must be a JSON object: {job_path}")
            stored_config = payload.get("config")
            if stored_config is None:
                raw_config: dict[str, Any] = {}
            elif isinstance(stored_config, dict):
                raw_config = dict(stored_config)
            else:
                raise ValueError(f"ServerJob metadata config must be an object: {job_path}")
            platform = raw_config.get("platform") or payload.get("platform")
            if not isinstance(platform, str) or not platform:
                raise ValueError(
                    f"ServerJob metadata does not declare a platform in config or job record: {job_path}"
                )
            raw_config.setdefault("platform", platform)
            if platform == "local":
                config = LocalServerJobConfig.model_validate(raw_config)
            else:
                raise NotImplementedError(f"ServerJob.attach does not support platform {platform!r}")
            provider = create_server_job_provider(config)
        elif job_id.startswith("local_"):
            provider = create_server_job_provider(LocalServerJobConfig())
        else:
            raise FileNotFoundError(f"ServerJob {job_id!r} not found under {jobs_root}")
        handle = await provider.attach(job_id)
        return cls(handle=handle, provider=provider, api_key=api_key, owned=False, client_kwargs=kwargs)

    async def __aenter__(self) -> "ServerJob":
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._owned:
            await self.stop()
        if self._client is not None:
            self._client.close()
            self._client = None

    async def get_client(self) -> TinkerClient:
        if self._client is not None:
            return self._client
        if not self.base_url:
            raise RuntimeError(f"ServerJob {self.job_id} does not have a backend base_url yet")
        client = TinkerClient(base_url=self.base_url, api_key=self.api_key, **self._client_kwargs)
        try:
            await client.get_server_capabilities_async()
        except Exception:
            client.close()
            raise
        self._client = client
        return client

    async def status(self) -> ServerJobStatusResponse:
        return await self._provider.status(self.handle)

    async def wait(self, timeout: float | None = None, poll_interval: float = 2.0) -> ServerJobStatusResponse:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            status = await self.status()
            if status.status in {"running", "succeeded", "failed", "stopped"}:
                return status
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for ServerJob {self.job_id}")
            await asyncio.sleep(poll_interval)

    async def stop(self, *, force_after_seconds: float | None = None) -> ServerJobStopResponse:
        return await self._provider.stop(self.handle, force_after_seconds=force_after_seconds)

    async def resolve_log_path(self, *, kind: str = "cookbook", runtime_id: str | None = None) -> Path:
        return await self._provider.resolve_log_path(self.handle, kind=kind, runtime_id=runtime_id)

    async def download_logs(self, output_path: str | Path | None = None) -> ServerJobLogsResponse:
        path = Path(output_path) if output_path is not None else None
        return await self._provider.download_logs(self.handle, path)
