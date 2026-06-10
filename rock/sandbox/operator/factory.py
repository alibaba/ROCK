"""Operator factory for creating operator instances based on configuration."""

from dataclasses import dataclass, field
from typing import Any

from rock.admin.core.ray_service import RayService
from rock.config import K8sConfig, RemoteConfig, RuntimeConfig
from rock.logger import init_logger
from rock.sandbox.operator.abstract import AbstractOperator
from rock.sandbox.operator.k8s.operator import K8sOperator
from rock.sandbox.operator.ray import RayOperator
from rock.utils.providers.nacos_provider import NacosConfigProvider
from rock.utils.providers.redis_provider import RedisProvider

logger = init_logger(__name__)


@dataclass
class OperatorContext:
    """Context object containing all dependencies needed for operator creation.

    This design pattern solves the parameter explosion problem by encapsulating
    all dependencies in a single context object. New operator types can add their
    dependencies to this context without changing the factory method signature.
    """

    runtime_config: RuntimeConfig
    ray_service: RayService | None = None
    redis_provider: RedisProvider | None = None
    # K8s operator dependencies
    k8s_config: K8sConfig | None = None
    nacos_provider: NacosConfigProvider | None = None
    # Remote operator dependencies
    remote_config: RemoteConfig | None = None
    # Future operator dependencies can be added here without breaking existing code
    extra_params: dict[str, Any] = field(default_factory=dict)


class OperatorFactory:
    """Factory class for creating operator instances.

    Uses the Context Object pattern to avoid parameter explosion as new
    operator types are added. ``build`` constructs a single operator by name,
    while ``create_operator`` is kept for backward compatibility (loads the
    single operator selected by ``runtime_config.operator_type``).
    """

    @staticmethod
    def build(name: str, context: OperatorContext) -> AbstractOperator:
        """Construct one operator instance by config-key name.

        ``name`` matches the top-level YAML key that triggered loading
        (one of ``OPERATOR_CONFIG_KEYS``: ``ray``/``k8s``/``remote``).
        """
        key = name.lower()

        if key == "ray":
            if context.ray_service is None:
                raise ValueError("RayService is required for RayOperator")
            logger.info("Creating RayOperator")
            ray_operator = RayOperator(ray_service=context.ray_service, runtime_config=context.runtime_config)
            if context.redis_provider is not None:
                ray_operator.set_redis_provider(context.redis_provider)
            if context.nacos_provider is not None:
                ray_operator.set_nacos_provider(context.nacos_provider)
            return ray_operator
        elif key == "k8s":
            if context.k8s_config is None:
                raise ValueError("K8sConfig is required for K8sOperator")
            logger.info("Creating K8sOperator")
            k8s_operator = K8sOperator(k8s_config=context.k8s_config)
            if context.redis_provider is not None:
                k8s_operator.set_redis_provider(context.redis_provider)
            if context.nacos_provider is not None:
                k8s_operator.set_nacos_provider(context.nacos_provider)
            return k8s_operator
        elif key == "remote":
            if context.remote_config is None:
                raise ValueError("RemoteConfig is required for RemoteOperator")
            # Lazy import to avoid pulling httpx into ray/k8s-only deployments.
            from rock.sandbox.operator.remote.operator import RemoteOperator

            logger.info("Creating RemoteOperator endpoint=%s", context.remote_config.api_endpoint)
            remote_operator = RemoteOperator(remote_config=context.remote_config)
            if context.redis_provider is not None:
                remote_operator.set_redis_provider(context.redis_provider)
            return remote_operator
        else:
            raise ValueError(f"Unsupported operator name: {name!r}. Supported: ray, k8s, remote")

    @staticmethod
    def create_operator(context: OperatorContext) -> AbstractOperator:
        """Backward-compatible single-operator creation by ``operator_type``."""
        return OperatorFactory.build(context.runtime_config.operator_type, context)
