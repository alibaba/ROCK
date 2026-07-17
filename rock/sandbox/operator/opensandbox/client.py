"""Thin async wrapper around the OpenSandbox Python SDK.

Isolates all SDK imports and exception translation so the operator/backend
layers work against a stable, mockable surface. The SDK is an optional
dependency (``opensandbox`` extra); it is imported lazily so this module can be
imported (and injected with fakes in tests) without the package installed.

See docs/plans/opensandbox-sdk-contract.md for the full mapping.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from typing import IO

import httpx

from rock.config import OpenSandboxConfig
from rock.logger import init_logger
from rock.sdk.common.exceptions import InternalServerRockError

logger = init_logger(__name__)


class OpenSandboxClient:
    """Async facade over ``opensandbox.Sandbox``.

    ``sandbox_cls`` / ``connection_config_cls`` are injectable for testing; when
    omitted they are lazily imported from the ``opensandbox`` package.
    """

    def __init__(
        self,
        config: OpenSandboxConfig,
        *,
        sandbox_cls=None,
        connection_config_cls=None,
        run_command_opts_cls=None,
        transport=None,
    ):
        if config.default_timeout <= 0:
            raise ValueError("OpenSandbox default_timeout must be positive")
        self._config = config
        self._sandbox_cls = sandbox_cls
        self._connection_config_cls = connection_config_cls
        self._run_command_opts_cls = run_command_opts_cls
        self._transport = transport or httpx.AsyncHTTPTransport()
        self._closed = False
        self._conn = None
        self._lifecycle_service = None

    def _load_sdk(self) -> None:
        if (
            self._sandbox_cls is not None
            and self._connection_config_cls is not None
            and self._run_command_opts_cls is not None
        ):
            return
        try:
            from opensandbox import Sandbox
            from opensandbox.config import ConnectionConfig
            from opensandbox.models.execd import RunCommandOpts
        except ImportError as e:  # pragma: no cover - exercised only without the extra
            raise InternalServerRockError(
                "opensandbox SDK is not installed; install the 'opensandbox' optional dependency "
                "to use operator_type=opensandbox"
            ) from e
        self._sandbox_cls = self._sandbox_cls or Sandbox
        self._connection_config_cls = self._connection_config_cls or ConnectionConfig
        self._run_command_opts_cls = self._run_command_opts_cls or RunCommandOpts

    def _connection_config(self):
        self._load_sdk()
        if self._conn is None:
            self._conn = self._connection_config_cls(
                api_key=self._config.api_key,
                domain=self._config.endpoint or None,
                protocol=self._config.protocol,
                use_server_proxy=self._config.use_server_proxy,
                request_timeout=timedelta(seconds=self._config.default_timeout),
                transport=self._transport,
            )
        return self._conn

    @asynccontextmanager
    async def _runtime_handle(self, opensandbox_id: str, operation: str):
        self._load_sdk()
        handle = None
        try:
            handle = await self._sandbox_cls.connect(
                opensandbox_id,
                connection_config=self._connection_config(),
                skip_health_check=True,
            )
            yield handle
        except Exception as e:
            raise InternalServerRockError(f"opensandbox {operation} failed for {opensandbox_id}: {e}") from e
        finally:
            if handle is not None:
                await handle.close()

    def _get_lifecycle_service(self):
        """Build the SDK lifecycle adapter without resolving sandbox endpoints."""
        if self._lifecycle_service is None:
            self._load_sdk()
            from opensandbox.adapters.factory import AdapterFactory

            self._lifecycle_service = AdapterFactory(self._connection_config()).create_sandbox_service()
        return self._lifecycle_service

    async def create(self, *, image, cpu, memory, env=None, metadata=None, timeout=None) -> str:
        """Create a sandbox and return its OpenSandbox id."""
        self._load_sdk()
        create_kwargs = {
            "resource": {"cpu": cpu, "memory": memory},
            "env": env,
            "metadata": metadata,
            "connection_config": self._connection_config(),
            # Return as soon as the sandbox id is assigned; do NOT block create()
            # on the SDK readiness health check. Rock's lifecycle is async —
            # submit() returns PENDING and get_status() polls until RUNNING — and
            # the health probe would otherwise time out (default 30s) on a cold
            # image pull, or when the caller cannot directly reach the sandbox.
            "skip_health_check": True,
        }
        # Only pass timeout when set. Passing timeout=None explicitly overrides
        # the SDK's default with a null duration, which strict servers reject
        # ("Provided duration string (nulls) is invalid"); omit it to keep the
        # SDK default (sandbox TTL) instead.
        if timeout:
            create_kwargs["timeout"] = timedelta(seconds=timeout)
        try:
            sandbox = await self._sandbox_cls.create(image, **create_kwargs)
        except Exception as e:
            raise InternalServerRockError(f"opensandbox create failed: {e}") from e
        return sandbox.id

    async def get_state(self, opensandbox_id: str) -> str | None:
        """Return the OpenSandbox lifecycle state string, or None if not found."""
        try:
            info = await self._get_lifecycle_service().get_sandbox_info(opensandbox_id)
        except Exception as e:
            logger.warning("opensandbox get_state failed for %s: %s", opensandbox_id, e)
            return None
        return info.status.state

    async def get_endpoint(self, opensandbox_id: str, port: int):
        """Resolve a sandbox service endpoint without connecting to execd."""
        try:
            return await self._get_lifecycle_service().get_sandbox_endpoint(
                opensandbox_id,
                port,
                self._config.use_server_proxy,
            )
        except Exception as e:
            raise InternalServerRockError(f"opensandbox get_endpoint failed for {opensandbox_id}: {e}") from e

    async def pause(self, opensandbox_id: str) -> None:
        try:
            await self._get_lifecycle_service().pause_sandbox(opensandbox_id)
        except Exception as e:
            raise InternalServerRockError(f"opensandbox pause failed for {opensandbox_id}: {e}") from e

    async def resume(self, opensandbox_id: str) -> None:
        try:
            await self._get_lifecycle_service().resume_sandbox(opensandbox_id)
        except Exception as e:
            raise InternalServerRockError(f"opensandbox resume failed for {opensandbox_id}: {e}") from e

    async def kill(self, opensandbox_id: str) -> None:
        try:
            await self._get_lifecycle_service().kill_sandbox(opensandbox_id)
        except Exception as e:
            raise InternalServerRockError(f"opensandbox kill failed for {opensandbox_id}: {e}") from e

    async def execute(
        self,
        opensandbox_id: str,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ):
        self._load_sdk()
        opts = self._run_command_opts_cls(
            timeout=timedelta(seconds=timeout) if timeout is not None else None,
            working_directory=cwd,
            envs=env,
        )
        async with self._runtime_handle(opensandbox_id, "execute") as handle:
            return await handle.commands.run(command, opts=opts)

    async def read_bytes(self, opensandbox_id: str, path: str) -> bytes:
        async with self._runtime_handle(opensandbox_id, "read_bytes") as handle:
            return await handle.files.read_bytes(path)

    async def get_file_info(self, opensandbox_id: str, path: str):
        try:
            async with self._runtime_handle(opensandbox_id, "get_file_info") as handle:
                return await handle.files.get_file_info([path])
        except InternalServerRockError as e:
            sdk_error = getattr(e.__cause__, "error", None)
            if getattr(sdk_error, "code", None) == "FILE_NOT_FOUND":
                return {}
            raise

    async def write_file(self, opensandbox_id: str, path: str, data: str | bytes | IO, *, mode: int) -> None:
        async with self._runtime_handle(opensandbox_id, "write_file") as handle:
            await handle.files.write_file(path, data, mode=mode)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._transport.aclose()
