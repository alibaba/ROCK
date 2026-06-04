"""K8s Operator implementation for managing sandboxes via Kubernetes."""

from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.common.constants import StopReason
from rock.config import K8sConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.logger import init_logger
from rock.sandbox.operator.abstract import AbstractOperator
from rock.sandbox.operator.k8s.constants import K8sConstants
from rock.sandbox.operator.k8s.provider import BatchSandboxProvider

logger = init_logger(__name__)


def _merge_sandbox_info(redis_info: dict, sandbox_info: SandboxInfo) -> SandboxInfo:
    """Merge Redis cached info with Provider real-time status.

    Merge rules:
    1. Compare resourceVersion in extended_params, use newer data if available
    2. Base fields: sandbox_info overrides redis_info (real-time status takes priority)
    3. extended_params: deep merge, values from sandbox_info take priority

    Args:
        redis_info: Cached info from Redis (contains user_id, etc.)
        sandbox_info: Real-time status from Provider (IP, port_mapping, is_alive, etc.)

    Returns:
        Merged SandboxInfo
    """
    redis_extended = redis_info.get("extended_params", {}) or {}
    sandbox_extended = sandbox_info.get("extended_params", {}) or {}

    # Check resourceVersion: return redis_info if it has newer version
    redis_rv = redis_extended.get(K8sConstants.EXT_RESOURCE_VERSION)
    sandbox_rv = sandbox_extended.get(K8sConstants.EXT_RESOURCE_VERSION)
    if redis_rv is not None and sandbox_rv is not None:
        try:
            if int(redis_rv) > int(sandbox_rv):
                return redis_info
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid resourceVersion format: redis_rv={redis_rv}, sandbox_rv={sandbox_rv}") from e

    # Deep merge extended_params
    merged_extended = dict(redis_extended)
    merged_extended.update(sandbox_extended)

    # Merge base fields (sandbox_info takes priority)
    merged = dict(redis_info)
    merged.update(sandbox_info)

    # Set the merged extended_params
    if merged_extended:
        merged["extended_params"] = merged_extended

    return merged


class K8sOperator(AbstractOperator):
    """Operator for managing sandboxes via Kubernetes BatchSandbox CRD."""

    def __init__(self, k8s_config: K8sConfig, redis_provider=None):
        """Initialize K8s operator.

        Args:
            k8s_config: K8sConfig object containing kubeconfig and templates
            redis_provider: Optional Redis provider for caching sandbox info
        """
        self._provider = BatchSandboxProvider(k8s_config=k8s_config)
        self._redis_provider = redis_provider
        logger.info("Initialized K8sOperator")

    def set_nacos_provider(self, nacos_provider):
        """Set Nacos config provider for dynamic pool configuration.

        Args:
            nacos_provider: NacosConfigProvider instance
        """
        super().set_nacos_provider(nacos_provider)
        self._provider.set_nacos_provider(nacos_provider)

    async def submit(self, config: DockerDeploymentConfig, user_info: dict = {}) -> SandboxInfo:
        """Submit a new sandbox deployment to Kubernetes.

        Args:
            config: Docker deployment configuration
            user_info: User metadata (user_id, experiment_id, namespace, rock_authorization)

        Returns:
            SandboxInfo with sandbox metadata
        """
        return await self._provider.submit(config, user_info)

    async def restart(self, config: DockerDeploymentConfig, host_ip: str | None = None) -> SandboxInfo:
        raise NotImplementedError("K8sOperator does not support container-reuse restart")

    async def get_status(self, sandbox_id: str) -> SandboxInfo | None:
        """Get sandbox status with user info from Redis.

        This method first gets status from provider (IP, port_mapping, is_alive),
        then merges it with user info from Redis if available.

        Args:
            sandbox_id: Sandbox identifier

        Returns:
            SandboxInfo with current status and user info, or None if K8s resource not found
        """
        try:
            sandbox_info = await self._provider.get_status(sandbox_id)
        except Exception as e:
            if hasattr(e, "status") and e.status == 404:
                logger.debug(f"K8s resource for sandbox {sandbox_id} not found, returning None")
                return None
            if "is being deleted" in str(e):
                logger.debug(f"K8s resource for sandbox {sandbox_id} is being deleted, returning None")
                return None
            logger.warning(f"Failed to get status from K8s for sandbox {sandbox_id}: {e}")
            return None

        # Get user info from redis if available
        if self._redis_provider:
            redis_info = await self.get_sandbox_info_from_redis(sandbox_id)
            if redis_info:
                return _merge_sandbox_info(redis_info, sandbox_info)
            else:
                return None
        return sandbox_info

    async def stop(self, sandbox_id: str, reason: StopReason = StopReason.MANUAL) -> bool:
        """Stop and delete a sandbox.

        Args:
            sandbox_id: Sandbox identifier
            reason: Why the stop was triggered. Logged here for traceability; K8s
                path has no actor-side lifecycle summary to attach it to.

        Returns:
            True if successful, False otherwise
        """
        logger.info(f"[{sandbox_id}] k8s stop (reason={reason.value})")
        return await self._provider.stop(sandbox_id)

    async def delete(self, config: DockerDeploymentConfig, host_ip: str | None = None) -> bool:
        raise NotImplementedError("delete is not yet implemented for K8sOperator")
