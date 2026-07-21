"""OpenSandbox lifecycle operator (Approach B, Phase 1).

Delegates sandbox lifecycle to an OpenSandbox deployment via its Python SDK.
Command/file execution is handled separately by the proxy-layer backend
(Phase 2). Lifecycle semantics:

  submit  -> Sandbox.create
  stop    -> unsupported by default; OpenSandbox pause requires persistence
  restart -> unsupported by default; OpenSandbox resume requires persistence
  delete  -> sandbox.kill()       (Terminated -> Rock DELETED, irreversible)

See docs/plans/opensandbox-operator-plan.md and opensandbox-sdk-contract.md.
"""

from __future__ import annotations

from rock.actions.sandbox.response import State
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.common.constants import StopReason
from rock.config import OpenSandboxConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.logger import init_logger
from rock.sandbox.operator.abstract import AbstractOperator
from rock.sandbox.operator.opensandbox.client import OpenSandboxClient
from rock.sandbox.operator.opensandbox.session_registry import OpenSandboxSessionRegistry
from rock.sdk.common.exceptions import BadRequestRockError

logger = init_logger(__name__)

BACKEND_NAME = "opensandbox"
EXT_OPENSANDBOX_ID = "opensandbox_id"
EXT_BACKEND = "backend"

# OpenSandbox SandboxState -> Rock State. Rock's State enum has only
# pending/running/stopped/deleted, so Paused/Failed collapse to stopped.
_STATE_MAP = {
    "Pending": State.PENDING,
    "Running": State.RUNNING,
    "Pausing": State.STOPPED,
    "Paused": State.STOPPED,
    "Stopping": State.STOPPED,
    "Terminated": State.DELETED,
    "Failed": State.STOPPED,
    "Unknown": State.PENDING,
}


def _map_state(os_state: str | None) -> State:
    return _STATE_MAP.get(os_state, State.PENDING)


def _format_cpu(cpus: float) -> str:
    """Render a cpu count as a k8s-friendly string (``4`` not ``4.0``)."""
    return str(int(cpus)) if float(cpus).is_integer() else str(cpus)


def _docker_mem_to_k8s(mem: str) -> str:
    """Convert docker-style memory (``8g``/``4096m``) to k8s style (``8Gi``/``4096Mi``).

    Values already in k8s style (``Gi``/``Mi``/``Ki``) pass through unchanged.
    """
    m = mem.strip()
    if m.endswith(("Gi", "Mi", "Ki")):
        return m
    if m[-1:] in ("g", "G"):
        return f"{m[:-1]}Gi"
    if m[-1:] in ("m", "M"):
        return f"{m[:-1]}Mi"
    return m


def _qualify_image_registry(image: str, prefix: str | None) -> str:
    """Prefix an image unless its first path component identifies a registry."""
    prefix = (prefix or "").strip().rstrip("/")
    if not prefix:
        return image

    image = image.strip()
    if not image:
        return image

    if "/" in image:
        first_component = image.split("/", 1)[0]
        if "." in first_component or ":" in first_component or first_component == "localhost":
            return image

    return f"{prefix}/{image}"


class OpenSandboxOperator(AbstractOperator):
    """Operator that manages sandboxes on an OpenSandbox backend."""

    supports_running_delete = True

    def __init__(self, os_config: OpenSandboxConfig, redis_provider=None, *, client: OpenSandboxClient | None = None):
        self._os_config = os_config
        self._redis_provider = redis_provider
        self._client = client or OpenSandboxClient(os_config)
        logger.info("Initialized OpenSandboxOperator (endpoint=%s)", os_config.endpoint)

    async def _resolve_os_id_from_redis(self, sandbox_id: str) -> str | None:
        info = await self.get_sandbox_info_from_redis(sandbox_id)
        if not info:
            return None
        return (info.get("extended_params") or {}).get(EXT_OPENSANDBOX_ID)

    async def submit(self, config: DockerDeploymentConfig, user_info: dict = {}) -> SandboxInfo:
        sandbox_id = config.container_name
        image = _qualify_image_registry(config.image, self._os_config.image_registry_prefix)
        cpu = _format_cpu(config.limit_cpus or config.cpus)
        memory = _docker_mem_to_k8s(config.memory)
        user_id = user_info.get("user_id", "default")
        experiment_id = user_info.get("experiment_id", "default")
        namespace = user_info.get("namespace", "default")
        metadata = {
            "rock_sandbox_id": sandbox_id or "",
            "user_id": user_id,
            "experiment_id": experiment_id,
            "namespace": namespace,
        }
        opensandbox_id = await self._client.create(
            image=image,
            cpu=cpu,
            memory=memory,
            metadata=metadata,
            timeout=config.startup_timeout,
        )
        logger.info("[%s] opensandbox submitted, opensandbox_id=%s", sandbox_id, opensandbox_id)
        info: SandboxInfo = {
            "sandbox_id": sandbox_id,
            "image": image,
            "cpus": config.cpus,
            "memory": config.memory,
            "user_id": user_id,
            "experiment_id": experiment_id,
            "namespace": namespace,
            "state": State.PENDING,
            "extended_params": {EXT_BACKEND: BACKEND_NAME, EXT_OPENSANDBOX_ID: opensandbox_id},
        }
        return info

    async def get_status(self, sandbox_id: str) -> SandboxInfo | None:
        redis_info = await self.get_sandbox_info_from_redis(sandbox_id)
        if not redis_info:
            return None
        opensandbox_id = (redis_info.get("extended_params") or {}).get(EXT_OPENSANDBOX_ID)
        if not opensandbox_id:
            logger.warning("[%s] no opensandbox_id in cached info", sandbox_id)
            return None
        os_state = await self._client.get_state(opensandbox_id)
        if os_state is None:
            return None
        redis_info["state"] = _map_state(os_state)
        return redis_info

    async def stop(self, sandbox_id: str, reason: StopReason = StopReason.MANUAL) -> bool:
        raise BadRequestRockError(
            "OpenSandbox backend does not support stop by default. "
            "OpenSandbox pause requires creating the sandbox with persistence enabled; use delete instead."
        )

    async def restart(self, config: DockerDeploymentConfig, host_ip: str | None = None) -> SandboxInfo:
        raise BadRequestRockError(
            "OpenSandbox backend does not support restart by default. "
            "OpenSandbox resume requires creating the sandbox with persistence enabled; create a new sandbox instead."
        )

    async def delete(self, config: DockerDeploymentConfig, host_ip: str | None = None) -> bool:
        sandbox_id = config.container_name
        opensandbox_id = (config.extended_params or {}).get(EXT_OPENSANDBOX_ID) or await self._resolve_os_id_from_redis(
            sandbox_id
        )
        if not opensandbox_id:
            raise BadRequestRockError(f"cannot resolve opensandbox_id for sandbox {sandbox_id}")
        logger.info("[%s] opensandbox delete -> kill", sandbox_id)
        await self._client.kill(opensandbox_id)
        if self._redis_provider is not None:
            try:
                await OpenSandboxSessionRegistry(self._redis_provider).clear(sandbox_id)
            except Exception as error:
                # The remote sandbox is already irreversibly deleted. Do not turn
                # best-effort local metadata cleanup into a false delete failure.
                logger.warning("[%s] failed to clear OpenSandbox session mappings: %s", sandbox_id, error)
        return True
