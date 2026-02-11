"""K8s Operator implementation for managing sandboxes via Kubernetes."""

from rock.config import K8sConfig
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.deployments.config import DockerDeploymentConfig
from rock.logger import init_logger
from rock.sandbox.operator.abstract import AbstractOperator
from rock.sandbox.operator.k8s.provider import BatchSandboxProvider

logger = init_logger(__name__)


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
    
    async def submit(self, config: DockerDeploymentConfig, user_info: dict = {}) -> SandboxInfo:
        """Submit a new sandbox deployment to Kubernetes.
        
        Args:
            config: Docker deployment configuration
            user_info: User metadata (user_id, experiment_id, namespace, rock_authorization)
            
        Returns:
            SandboxInfo with sandbox metadata
        """
        return await self._provider.submit(config, user_info)
    
    async def get_status(self, sandbox_id: str) -> SandboxInfo:
        """Get sandbox status with user info from Redis.
        
        This method first gets status from provider (IP, port_mapping, is_alive),
        then merges it with user info from Redis if available.
        
        Args:
            sandbox_id: Sandbox identifier
            
        Returns:
            SandboxInfo with current status and user info
        """
        # Get sandbox info from provider (includes is_alive check)
        sandbox_info = await self._provider.get_status(sandbox_id)
        
        # Get user info from redis if available
        if self._redis_provider:
            redis_info = await self._get_sandbox_info_from_redis(sandbox_id)
            if redis_info:
                redis_info.update(sandbox_info)
                return redis_info
        
        return sandbox_info
    
    async def _get_sandbox_info_from_redis(self, sandbox_id: str) -> dict | None:
        """Get sandbox user info from Redis.
        
        Args:
            sandbox_id: Sandbox identifier
            
        Returns:
            Sandbox info dict from Redis or None if not found
        """
        from rock.admin.core.redis_key import alive_sandbox_key
        try:
            sandbox_status = await self._redis_provider.json_get(alive_sandbox_key(sandbox_id), "$")
            if sandbox_status and len(sandbox_status) > 0:
                return sandbox_status[0]
        except Exception as e:
            logger.debug(f"Failed to get sandbox info from redis for {sandbox_id}: {e}")
        return None
    
    async def stop(self, sandbox_id: str) -> bool:
        """Stop and delete a sandbox.
        
        Args:
            sandbox_id: Sandbox identifier
            
        Returns:
            True if successful, False otherwise
        """
        return await self._provider.stop(sandbox_id)
