"""Operator factory for creating operator instances based on configuration."""

from dataclasses import dataclass, field
from typing import Any

from rock.admin.core.ray_service import RayService
from rock.config import RuntimeConfig, K8sConfig
from rock.logger import init_logger
from rock.sandbox.operator.abstract import AbstractOperator
from rock.sandbox.operator.k8s.operator import K8sOperator
from rock.sandbox.operator.ray import RayOperator
from rock.utils.providers.nacos_provider import NacosConfigProvider

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
    # K8s operator dependencies
    k8s_config: K8sConfig | None = None
    nacos_provider: NacosConfigProvider | None = None
    # Future operator dependencies can be added here without breaking existing code
    extra_params: dict[str, Any] = field(default_factory=dict)


class OperatorFactory:
    """Factory class for creating operator instances.

    Uses the Context Object pattern to avoid parameter explosion as new
    operator types are added.
    """

    @staticmethod
    def create_operator(context: OperatorContext) -> AbstractOperator:
        """Create an operator instance based on the runtime configuration.

        Args:
            context: OperatorContext containing all necessary dependencies

        Returns:
            AbstractOperator: The created operator instance

        Raises:
            ValueError: If operator_type is not supported or required dependencies are missing
        """
        operator_type = context.runtime_config.operator_type.lower()

        if operator_type == "ray":
            if context.ray_service is None:
                raise ValueError("RayService is required for RayOperator")
            logger.info("Creating RayOperator")
            ray_operator = RayOperator(ray_service=context.ray_service)
            if context.nacos_provider is not None:
                ray_operator.set_nacos_provider(context.nacos_provider)
            return ray_operator
        elif operator_type == "k8s":
            if context.k8s_config is None:
                raise ValueError("K8sConfig is required for K8sOperator")
            logger.info("Creating K8sOperator")
            return K8sOperator(k8s_config=context.k8s_config)
        else:
            raise ValueError(f"Unsupported operator type: {operator_type}. " f"Supported types: ray, kubernetes")
