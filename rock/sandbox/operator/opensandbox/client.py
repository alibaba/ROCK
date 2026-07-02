"""Thin async wrapper around the OpenSandbox Python SDK.

Isolates all SDK imports and exception translation so the operator/backend
layers work against a stable, mockable surface. The SDK is an optional
dependency (``opensandbox`` extra); it is imported lazily so this module can be
imported (and injected with fakes in tests) without the package installed.

See docs/plans/opensandbox-sdk-contract.md for the full mapping.
"""

from __future__ import annotations

from datetime import timedelta

from rock.config import OpenSandboxConfig
from rock.logger import init_logger
from rock.sdk.common.exceptions import InternalServerRockError

logger = init_logger(__name__)


class OpenSandboxClient:
    """Async facade over ``opensandbox.Sandbox``.

    ``sandbox_cls`` / ``connection_config_cls`` are injectable for testing; when
    omitted they are lazily imported from the ``opensandbox`` package.
    """

    def __init__(self, config: OpenSandboxConfig, *, sandbox_cls=None, connection_config_cls=None):
        self._config = config
        self._sandbox_cls = sandbox_cls
        self._connection_config_cls = connection_config_cls
        self._conn = None

    def _load_sdk(self) -> None:
        if self._sandbox_cls is not None and self._connection_config_cls is not None:
            return
        try:
            from opensandbox import Sandbox
            from opensandbox.config import ConnectionConfig
        except ImportError as e:  # pragma: no cover - exercised only without the extra
            raise InternalServerRockError(
                "opensandbox SDK is not installed; install the 'opensandbox' optional dependency "
                "to use operator_type=opensandbox"
            ) from e
        self._sandbox_cls = self._sandbox_cls or Sandbox
        self._connection_config_cls = self._connection_config_cls or ConnectionConfig

    def _connection_config(self):
        self._load_sdk()
        if self._conn is None:
            self._conn = self._connection_config_cls(
                api_key=self._config.api_key,
                domain=self._config.endpoint or None,
                protocol=self._config.protocol,
                use_server_proxy=self._config.use_server_proxy,
            )
        return self._conn

    async def create(self, *, image, cpu, memory, env=None, metadata=None, timeout=None) -> str:
        """Create a sandbox and return its OpenSandbox id."""
        self._load_sdk()
        try:
            sandbox = await self._sandbox_cls.create(
                image,
                resource={"cpu": cpu, "memory": memory},
                env=env,
                metadata=metadata,
                timeout=timedelta(seconds=timeout) if timeout else None,
                connection_config=self._connection_config(),
            )
        except Exception as e:
            raise InternalServerRockError(f"opensandbox create failed: {e}") from e
        return sandbox.id

    async def _connect(self, opensandbox_id: str):
        # skip_health_check: we only need a handle to read info / pause / kill.
        # A paused (or otherwise not-ready) sandbox fails the health check, which
        # would otherwise block for ready_timeout and make get_state read as gone.
        self._load_sdk()
        return await self._sandbox_cls.connect(
            opensandbox_id, connection_config=self._connection_config(), skip_health_check=True
        )

    async def get_state(self, opensandbox_id: str) -> str | None:
        """Return the OpenSandbox lifecycle state string, or None if not found."""
        try:
            sandbox = await self._connect(opensandbox_id)
            info = await sandbox.get_info()
        except Exception as e:
            logger.warning("opensandbox get_state failed for %s: %s", opensandbox_id, e)
            return None
        return info.status.state

    async def pause(self, opensandbox_id: str) -> None:
        try:
            sandbox = await self._connect(opensandbox_id)
            await sandbox.pause()
        except Exception as e:
            raise InternalServerRockError(f"opensandbox pause failed for {opensandbox_id}: {e}") from e

    async def resume(self, opensandbox_id: str) -> None:
        self._load_sdk()
        try:
            await self._sandbox_cls.resume(opensandbox_id, connection_config=self._connection_config())
        except Exception as e:
            raise InternalServerRockError(f"opensandbox resume failed for {opensandbox_id}: {e}") from e

    async def kill(self, opensandbox_id: str) -> None:
        try:
            sandbox = await self._connect(opensandbox_id)
            await sandbox.kill()
        except Exception as e:
            raise InternalServerRockError(f"opensandbox kill failed for {opensandbox_id}: {e}") from e
