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
        self._lifecycle_service = None

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
