"""Composite operator that delegates to multiple sub-operators based on operator type.

This module implements the Composite pattern: CompositeOperator inherits from
AbstractOperator and holds multiple concrete operators internally. It routes
each request to the appropriate sub-operator based on the operator_type field
in the deployment config (for submit) or in the Redis sandbox info (for
get_status/stop).

SandboxManager sees only a single AbstractOperator and requires no changes.
"""

from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.admin.core.redis_key import alive_sandbox_key
from rock.deployments.config import DeploymentConfig, DockerDeploymentConfig
from rock.logger import init_logger
from rock.sandbox.operator.abstract import AbstractOperator
from rock.utils.providers.redis_provider import RedisProvider

logger = init_logger(__name__)


class CompositeOperator(AbstractOperator):
    """Operator that holds multiple sub-operators and routes by operator_type.

    When a sandbox is created via submit(), the operator_type from the
    DeploymentConfig determines which sub-operator handles the request.
    The chosen operator_type is recorded in the returned SandboxInfo so that
    SandboxManager persists it to Redis.

    For get_status() and stop(), the operator_type is looked up from Redis
    to route to the correct sub-operator.
    """

    def __init__(
        self,
        operators: dict[str, AbstractOperator],
        default_operator_type: str,
    ):
        """Initialize CompositeOperator.

        Args:
            operators: Mapping from operator type name (e.g., "ray", "k8s")
                       to the corresponding AbstractOperator instance.
            default_operator_type: The operator type to use when the request
                                   does not specify one explicitly.
        """
        if not operators:
            raise ValueError("At least one operator must be provided")

        normalized_default = default_operator_type.lower()
        if normalized_default not in operators:
            raise ValueError(
                f"Default operator type '{default_operator_type}' not found "
                f"in provided operators: {list(operators.keys())}"
            )

        self._operators = operators
        self._default_operator_type = normalized_default
        logger.info(
            f"CompositeOperator initialized with operators: {list(operators.keys())}, "
            f"default: '{normalized_default}'"
        )

    def set_redis_provider(self, redis_provider: RedisProvider):
        """Propagate the redis provider to all sub-operators."""
        self._redis_provider = redis_provider
        for operator in self._operators.values():
            operator.set_redis_provider(redis_provider)

    def _resolve_operator_for_config(self, config: DeploymentConfig) -> tuple[str, AbstractOperator]:
        """Resolve the sub-operator for a submit request based on config.

        Returns:
            A tuple of (resolved_operator_type, operator_instance).
        """
        requested_type = None
        if isinstance(config, DockerDeploymentConfig) and config.operator_type:
            requested_type = config.operator_type.lower()

        resolved_type = requested_type or self._default_operator_type
        operator = self._operators.get(resolved_type)
        if operator is None:
            available = list(self._operators.keys())
            raise ValueError(f"Unsupported operator type: '{resolved_type}'. Available types: {available}")
        return resolved_type, operator

    async def _resolve_operator_for_sandbox(self, sandbox_id: str) -> tuple[str, AbstractOperator]:
        """Resolve the sub-operator for an existing sandbox by looking up Redis.

        Falls back to the default operator if Redis is unavailable or the
        sandbox has no operator_type recorded.

        Returns:
            A tuple of (resolved_operator_type, operator_instance).
        """
        resolved_type = self._default_operator_type

        if self._redis_provider:
            sandbox_status = await self._redis_provider.json_get(alive_sandbox_key(sandbox_id), "$")
            if sandbox_status and len(sandbox_status) > 0:
                stored_type = sandbox_status[0].get("operator_type")
                if stored_type:
                    resolved_type = stored_type.lower()

        operator = self._operators.get(resolved_type)
        if operator is None:
            logger.warning(
                f"Operator type '{resolved_type}' for sandbox '{sandbox_id}' not found, "
                f"falling back to default '{self._default_operator_type}'"
            )
            resolved_type = self._default_operator_type
            operator = self._operators[resolved_type]

        return resolved_type, operator

    async def submit(self, config: DeploymentConfig, user_info: dict = {}) -> SandboxInfo:
        """Submit a sandbox creation request to the appropriate sub-operator.

        The operator_type is determined from config.operator_type (if set) or
        falls back to the default. The resolved operator_type is written into
        the returned SandboxInfo so that SandboxManager persists it to Redis.
        """
        resolved_type, operator = self._resolve_operator_for_config(config)
        logger.info(
            f"Routing submit for sandbox '{getattr(config, 'container_name', 'unknown')}' "
            f"to operator '{resolved_type}'"
        )

        sandbox_info = await operator.submit(config, user_info)
        sandbox_info["operator_type"] = resolved_type
        return sandbox_info

    async def get_status(self, sandbox_id: str) -> SandboxInfo:
        """Get sandbox status by routing to the operator that created it.

        Ensures the returned SandboxInfo always contains operator_type so that
        SandboxManager.get_status() does not lose it when writing back to Redis.
        """
        resolved_type, operator = await self._resolve_operator_for_sandbox(sandbox_id)
        logger.debug(f"Routing get_status for sandbox '{sandbox_id}' to operator '{resolved_type}'")
        sandbox_info = await operator.get_status(sandbox_id)
        sandbox_info["operator_type"] = resolved_type
        return sandbox_info

    async def stop(self, sandbox_id: str) -> bool:
        """Stop a sandbox by routing to the operator that created it."""
        resolved_type, operator = await self._resolve_operator_for_sandbox(sandbox_id)
        logger.info(f"Routing stop for sandbox '{sandbox_id}' to operator '{resolved_type}'")
        return await operator.stop(sandbox_id)
