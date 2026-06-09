"""RemoteOperator implementation.

Bridges ROCK's :class:`AbstractOperator` contract to an Infra-style external
sandbox API. ROCK keeps owning the local ``sandbox_id`` (== ``container_name``)
as the meta-store primary key; the remote API has its own ``sandboxID``, which
is carried side-band inside :class:`SandboxInfo` as ``host_name`` (and again
under ``remote.sandbox_id`` in ``extended_params`` for forward compat). The
operator resolves the remote id on every operate call via the redis-backed
sandbox_info, so dispatch stays stateless across process restarts.
"""

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
    """Operator that delegates sandbox lifecycle to a remote Infra-style API."""

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
        payload = to_new_sandbox_payload(
            config,
            default_template_id=self._remote_config.default_template_id,
        )
        logger.info(
            f"[{local_id}] remote submit -> POST /sandboxes (template={payload.get('templateID') or payload.get('fromImage')})"
        )
        body = await self._client.create_sandbox(payload)
        remote_id = body.get("sandboxID") or ""
        if not remote_id:
            raise RemoteApiError(0, f"create_sandbox returned no sandboxID: {body!r}")

        sandbox_info: SandboxInfo = from_sandbox_response(body, config=config)
        # Local ROCK id stays as the meta-store primary key; the remote id is
        # piggy-backed on host_name so subsequent operate calls can retrieve
        # it via redis without touching the meta store directly.
        sandbox_info["sandbox_id"] = local_id
        sandbox_info["host_name"] = remote_id

        # Inject identity bookkeeping into extended_params for downstream
        # debuggability (the redis alive key picks this up via SandboxInfo).
        ext = dict(sandbox_info.get("extended_params") or {})
        ext["remote.sandbox_id"] = remote_id
        ext["remote.template_id"] = body.get("templateID") or ""
        sandbox_info["extended_params"] = ext

        if user_info:
            for key in ("user_id", "experiment_id", "namespace", "rock_authorization"):
                value = user_info.get(key)
                if value is not None:
                    sandbox_info[key] = value  # type: ignore[literal-required]

        logger.info(
            f"[{local_id}] remote submit OK: remote_id={remote_id} domain={sandbox_info.get('sandbox_domain')}"
        )
        return sandbox_info

    async def restart(
        self, config: DeploymentConfig, host_ip: str | None = None
    ) -> SandboxInfo:
        """Remote API has no stop→start lifecycle: keep-alive ping + refetch.

        The ROCK state machine only invokes ``restart`` from the STOPPED state,
        but the remote treats kill as terminal — once stopped the sandbox
        is gone. We attempt a refresh (keep-alive); if the remote returns 404
        we surface the state as STOPPED so the caller can decide to recreate.
        """
        if not isinstance(config, DockerDeploymentConfig):
            raise TypeError("RemoteOperator.restart requires DockerDeploymentConfig")
        local_id = config.container_name
        remote_id = await self._resolve_remote_id(local_id)
        if not remote_id:
            raise RemoteApiError(
                0, f"restart: no remote sandbox id recorded for {local_id}"
            )
        try:
            await self._client.refresh_sandbox(remote_id)
        except RemoteApiError as exc:
            if exc.status_code == 404:
                logger.warning(
                    f"[{local_id}] remote restart: sandbox {remote_id} not found (already gone)"
                )
                return {"sandbox_id": local_id, "host_name": remote_id, "state": State.STOPPED}
            raise

        body = await self._client.get_sandbox(remote_id)
        if body is None:
            return {"sandbox_id": local_id, "host_name": remote_id, "state": State.STOPPED}
        info = from_sandbox_detail(body)
        info["sandbox_id"] = local_id
        info["host_name"] = remote_id
        return info

    async def get_status(self, sandbox_id: str) -> SandboxInfo | None:
        remote_id = await self._resolve_remote_id(sandbox_id)
        if not remote_id:
            logger.debug(f"[{sandbox_id}] remote get_status: no remote id, returning None")
            return None
        body = await self._client.get_sandbox(remote_id)
        if body is None:
            return None
        info = from_sandbox_detail(body)
        # Preserve the ROCK-side primary key on the way back up.
        info["sandbox_id"] = sandbox_id
        info["host_name"] = remote_id
        return info

    async def stop(self, sandbox_id: str, reason: StopReason = StopReason.MANUAL) -> bool:
        remote_id = await self._resolve_remote_id(sandbox_id)
        if not remote_id:
            logger.warning(f"[{sandbox_id}] remote stop: no remote id, treating as already gone")
            return True
        logger.info(f"[{sandbox_id}] remote stop -> DELETE /sandboxes/{remote_id} (reason={reason})")
        return await self._client.kill_sandbox(remote_id)

    async def delete(self, config: DeploymentConfig, host_ip: str | None = None) -> bool:
        # Remote has no separate "delete after stop" — kill_sandbox already removes
        # the sandbox. delete() is therefore best-effort: if the remote id is
        # still around, kill it; otherwise no-op.
        if not isinstance(config, DockerDeploymentConfig):
            return True
        local_id = config.container_name
        remote_id = await self._resolve_remote_id(local_id)
        if not remote_id:
            logger.info(f"[{local_id}] remote delete: no remote id, no-op")
            return True
        try:
            return await self._client.kill_sandbox(remote_id)
        except RemoteApiError as exc:
            if exc.status_code == 404:
                return True
            raise

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _resolve_remote_id(self, sandbox_id: str) -> str:
        """Look up the remote sandboxID for a ROCK ``sandbox_id``.

        Reads the redis-backed alive key (populated at submit time). Returns
        an empty string when the mapping cannot be resolved — callers decide
        whether that's fatal or a soft no-op.
        """
        info: dict[str, Any] | None = None
        try:
            info = await self.get_sandbox_info_from_redis(sandbox_id)
        except RuntimeError:
            # Redis provider not configured — operate path will treat this as
            # no remote id and return None / no-op accordingly.
            return ""
        if not info:
            return ""
        # ``host_name`` carries the remote sandboxID; fall back to extended_params.
        remote_id = info.get("host_name") or ""
        if remote_id:
            return remote_id
        ext = info.get("extended_params") or {}
        return ext.get("remote.sandbox_id") or ""
