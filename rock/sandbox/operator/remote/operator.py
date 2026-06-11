"""RemoteOperator — delegates sandbox lifecycle to an external sandbox API."""

from __future__ import annotations

from typing import Any

from rock.actions.sandbox.response import State
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.common.constants import StopReason
from rock.config import RemoteConfig
from rock.deployments.config import DeploymentConfig, DockerDeploymentConfig
from rock.logger import init_logger
from rock.sandbox.operator.abstract import AbstractOperator
from rock.sandbox.operator.remote.client import RemoteApiError, RemoteClient
from rock.sandbox.operator.remote.mapping import (
    from_sandbox_detail,
    from_sandbox_response,
    to_new_sandbox_payload,
)

logger = init_logger(__name__)


class RemoteOperator(AbstractOperator):

    def __init__(self, remote_config: RemoteConfig) -> None:
        self._remote_config = remote_config
        self._client = RemoteClient(remote_config)

    @property
    def client(self) -> RemoteClient:
        return self._client

    async def close(self) -> None:
        await self._client.close()

    # ------------------------------------------------------------------
    # AbstractOperator contract
    # ------------------------------------------------------------------

    async def submit(self, config: DeploymentConfig, user_info: dict | None = None) -> SandboxInfo:
        if not isinstance(config, DockerDeploymentConfig):
            raise TypeError(
                f"RemoteOperator.submit only supports DockerDeploymentConfig, got {type(config).__name__}"
            )
        local_id = config.container_name
        payload = to_new_sandbox_payload(config)
        logger.info(
            f"[{local_id}] remote submit -> POST /sandboxes (image={payload.get('fromImage')})"
        )
        body = await self._client.create(payload)
        remote_id = body.get("sandboxID") or ""
        if not remote_id:
            raise RemoteApiError(0, f"create returned no sandboxID: {body!r}")

        sandbox_info: SandboxInfo = from_sandbox_response(body, config=config)
        sandbox_info["sandbox_id"] = local_id
        sandbox_info["host_name"] = remote_id
        # placeholder for state machine on_restart guard check
        sandbox_info["host_ip"] = self._remote_config.api_endpoint

        ext = dict(sandbox_info.get("extended_params") or {})
        ext["remote.sandbox_id"] = remote_id
        sandbox_info["extended_params"] = ext

        # persist into config so the DB spec column carries remote_id for restart/delete
        config.extended_params["remote.sandbox_id"] = remote_id

        if user_info:
            for key in ("user_id", "experiment_id", "namespace", "rock_authorization"):
                value = user_info.get(key)
                if value is not None:
                    sandbox_info[key] = value  # type: ignore[literal-required]

        logger.info(
            f"[{local_id}] remote submit OK: remote_id={remote_id} domain={ext.get('remote.sandbox_domain')}"
        )
        return sandbox_info

    async def restart(
        self, config: DeploymentConfig, host_ip: str | None = None
    ) -> SandboxInfo:
        """Resume a paused sandbox via POST /connect."""
        if not isinstance(config, DockerDeploymentConfig):
            raise TypeError("RemoteOperator.restart requires DockerDeploymentConfig")
        local_id = config.container_name
        remote_id = await self._resolve_remote_id(local_id)
        if not remote_id:
            remote_id = config.extended_params.get("remote.sandbox_id", "")
        if not remote_id:
            raise RemoteApiError(
                0, f"restart: no remote sandbox id recorded for {local_id}"
            )
        timeout_seconds = int((config.auto_clear_time_minutes or 0) * 60)
        body = await self._client.restart(remote_id, timeout_seconds=timeout_seconds)
        if body is None:
            return {"sandbox_id": local_id, "host_name": remote_id, "state": State.STOPPED}
        info = from_sandbox_response(body, config=config)
        info["sandbox_id"] = local_id
        info["host_name"] = remote_id
        info["state"] = State.RUNNING
        return info

    async def get_status(self, sandbox_id: str) -> SandboxInfo | None:
        remote_id = await self._resolve_remote_id(sandbox_id)
        if not remote_id:
            logger.debug(f"[{sandbox_id}] remote get_status: no remote id, returning None")
            return None
        body = await self._client.get(remote_id)
        if body is None:
            return None
        info = from_sandbox_detail(body)
        info["sandbox_id"] = sandbox_id
        info["host_name"] = remote_id
        info["host_ip"] = self._remote_config.api_endpoint
        return info

    async def stop(self, sandbox_id: str, reason: StopReason = StopReason.MANUAL) -> bool:
        """Pause the remote sandbox. Kill is reserved for delete."""
        remote_id = await self._resolve_remote_id(sandbox_id)
        if not remote_id:
            logger.warning(f"[{sandbox_id}] remote stop: no remote id, treating as already gone")
            return True
        logger.info(f"[{sandbox_id}] remote stop -> POST /sandboxes/{remote_id}/pause (reason={reason})")
        return await self._client.stop(remote_id)

    async def delete(self, config: DeploymentConfig, host_ip: str | None = None) -> bool:
        if not isinstance(config, DockerDeploymentConfig):
            return True
        local_id = config.container_name
        remote_id = await self._resolve_remote_id(local_id)
        if not remote_id:
            remote_id = config.extended_params.get("remote.sandbox_id", "")
        if not remote_id:
            logger.info(f"[{local_id}] remote delete: no remote id, no-op")
            return True
        try:
            return await self._client.delete(remote_id)
        except RemoteApiError as exc:
            if exc.status_code == 404:
                return True
            raise

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _resolve_remote_id(self, sandbox_id: str) -> str:
        """Look up the remote sandboxID from redis alive key."""
        info: dict[str, Any] | None = None
        try:
            info = await self.get_sandbox_info_from_redis(sandbox_id)
        except RuntimeError:
            return ""
        if not info:
            return ""
        remote_id = info.get("host_name") or ""
        if remote_id:
            return remote_id
        ext = info.get("extended_params") or {}
        return ext.get("remote.sandbox_id") or ""
