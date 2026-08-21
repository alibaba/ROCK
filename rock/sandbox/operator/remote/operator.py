"""RemoteOperator — manages sandboxes on a remote platform via a Provider.

Delegates lifecycle calls to a RemoteProvider (SandboxNextProvider by default)
and handles Redis metadata merging, template API graceful fallback, and
extended_params bookkeeping.

See docs/proposals/remote-operator.md for the full design.
"""

from __future__ import annotations

from typing import Any

from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.common.constants import StopReason
from rock.config import RemoteOperatorConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.logger import init_logger
from rock.sandbox.operator.abstract import AbstractOperator
from rock.sandbox.operator.remote.constants import EXT_REMOTE_ID
from rock.sandbox.operator.remote.providers.sandbox_next_provider import SandboxNextProvider
from rock.sdk.common.exceptions import BadRequestRockError

logger = init_logger(__name__)


class RemoteOperator(AbstractOperator):
    """Operator that manages sandboxes on a remote platform via HTTP REST API."""

    supports_running_delete = True

    def __init__(self, remote_config: RemoteOperatorConfig):
        self._config = remote_config
        self._provider = self._create_provider(remote_config)
        logger.info("Initialized RemoteOperator (provider=%s, endpoint=%s)", remote_config.provider, remote_config.endpoint)

    @staticmethod
    def _create_provider(config: RemoteOperatorConfig):
        """Factory method — the single extension point for new providers."""
        if config.provider == "sandbox_next":
            return SandboxNextProvider(config)
        raise ValueError(f"Unsupported remote provider: {config.provider}. Supported: sandbox_next")

    async def _resolve_remote_id(self, sandbox_id: str) -> str | None:
        """Read the platform-assigned ID from Redis extended_params."""
        info = await self.get_sandbox_info_from_redis(sandbox_id)
        if not info:
            return None
        return (info.get("extended_params") or {}).get(EXT_REMOTE_ID)

    async def submit(self, config: DockerDeploymentConfig, user_info: dict = {}) -> SandboxInfo:
        return await self._provider.submit(config, user_info)

    async def restart(self, config: DockerDeploymentConfig, host_ip: str | None = None) -> SandboxInfo:
        raise BadRequestRockError("RemoteOperator does not support container-reuse restart")

    async def get_status(self, sandbox_id: str) -> SandboxInfo | None:
        redis_info = await self.get_sandbox_info_from_redis(sandbox_id)
        if not redis_info:
            return None
        remote_id = (redis_info.get("extended_params") or {}).get(EXT_REMOTE_ID)
        if not remote_id:
            logger.warning("[%s] no remote_sandbox_id in cached info", sandbox_id)
            return None
        provider_info = await self._provider.get_status(remote_id)
        if provider_info is None:
            # Sandbox no longer exists on the remote platform
            redis_info["state"] = "deleted"
            return redis_info
        # Merge: provider real-time status overrides Redis base fields;
        # Redis user metadata is preserved (deep merge extended_params).
        merged = dict(redis_info)
        merged.update(provider_info)
        merged_extended = dict(redis_info.get("extended_params") or {})
        merged_extended.update(provider_info.get("extended_params") or {})
        merged["extended_params"] = merged_extended
        return merged

    async def stop(self, sandbox_id: str, reason: StopReason = StopReason.MANUAL) -> bool:
        remote_id = await self._resolve_remote_id(sandbox_id)
        if not remote_id:
            raise BadRequestRockError(f"cannot resolve remote_sandbox_id for sandbox {sandbox_id}")
        logger.info("[%s] remote stop -> pause (reason=%s)", sandbox_id, reason.value)
        return await self._provider.stop(remote_id)

    async def delete(self, config: DockerDeploymentConfig, host_ip: str | None = None) -> bool:
        sandbox_id = config.container_name
        remote_id = (config.extended_params or {}).get(EXT_REMOTE_ID) or await self._resolve_remote_id(sandbox_id)
        if not remote_id:
            raise BadRequestRockError(f"cannot resolve remote_sandbox_id for sandbox {sandbox_id}")
        logger.info("[%s] remote delete", sandbox_id)
        return await self._provider.delete(remote_id)

    # ========================================================================
    # Template API — delegate to provider with graceful fallback
    # ========================================================================

    async def create_template(self, spec: Any) -> dict:
        try:
            return await self._provider.create_template(spec)
        except NotImplementedError:
            raise BadRequestRockError(f"template not supported on {type(self).__name__}")

    async def get_template_status(self, template_id: str) -> dict | None:
        try:
            return await self._provider.get_template_status(template_id)
        except NotImplementedError:
            raise BadRequestRockError(f"template not supported on {type(self).__name__}")

    async def delete_template(self, template_id: str) -> bool:
        try:
            return await self._provider.delete_template(template_id)
        except NotImplementedError:
            raise BadRequestRockError(f"template not supported on {type(self).__name__}")
