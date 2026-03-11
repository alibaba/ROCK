"""Operator factory for creating operator instances based on configuration."""

from dataclasses import dataclass, field
from typing import Any

from rock.admin.core.ray_service import RayService
from rock.config import K8sConfig, RuntimeConfig
from rock.logger import init_logger
from rock.sandbox.operator.abstract import AbstractOperator
from rock.sandbox.operator.composite import CompositeOperator
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
    def _create_single_operator(operator_type: str, context: OperatorContext) -> AbstractOperator:
        """Create a single operator instance by type.

        Args:
            operator_type: The operator type string (e.g., "ray", "k8s")
            context: OperatorContext containing all necessary dependencies

        Returns:
            AbstractOperator: The created operator instance

        Raises:
            ValueError: If operator_type is not supported or required dependencies are missing
        """
        normalized_type = operator_type.lower()

        if normalized_type == "ray":
            if context.ray_service is None:
                raise ValueError("RayService is required for RayOperator")
            logger.info("Creating RayOperator")
            ray_operator = RayOperator(ray_service=context.ray_service, runtime_config=context.runtime_config)
            if context.nacos_provider is not None:
                ray_operator.set_nacos_provider(context.nacos_provider)
            return ray_operator
        elif normalized_type == "k8s":
            if context.k8s_config is None:
                raise ValueError("K8sConfig is required for K8sOperator")
            logger.info("Creating K8sOperator")
            return K8sOperator(k8s_config=context.k8s_config)
        else:
            raise ValueError(f"Unsupported operator type: {operator_type}. Supported types: ray, k8s")

    @staticmethod
    def create_operator(context: OperatorContext) -> AbstractOperator:
        """Create a single operator instance based on the default operator_type in runtime config.

        Args:
            context: OperatorContext containing all necessary dependencies

        Returns:
            AbstractOperator: The created operator instance
        """
        return OperatorFactory._create_single_operator(context.runtime_config.operator_type, context)

    @staticmethod
    def create_operators(context: OperatorContext) -> dict[str, AbstractOperator]:
        """Create multiple operator instances based on operator_types list in runtime config.

        Iterates over runtime_config.operator_types and creates an operator for each type.
        The returned dict is keyed by the normalized operator type string.

        Args:
            context: OperatorContext containing all necessary dependencies

        Returns:
            dict[str, AbstractOperator]: Mapping from operator type to operator instance

        Raises:
            ValueError: If operator_types is empty or any type is unsupported
        """
        operator_types = context.runtime_config.operator_types
        if not operator_types:
            raise ValueError("operator_types list is empty, at least one operator type must be configured")

        operators: dict[str, AbstractOperator] = {}
        for operator_type in operator_types:
            normalized_type = operator_type.lower()
            if normalized_type in operators:
                logger.warning(f"Duplicate operator type '{normalized_type}' in config, skipping")
                continue
            operator = OperatorFactory._create_single_operator(normalized_type, context)
            operators[normalized_type] = operator
            logger.info(f"Created operator for type '{normalized_type}'")

        logger.info(f"Initialized {len(operators)} operator(s): {list(operators.keys())}")
        return operators

    @staticmethod
    def create_composite_operator(context: OperatorContext) -> CompositeOperator:
        """Create a CompositeOperator that wraps multiple sub-operators.

        This is the recommended entry point for multi-operator setups. It reads
        operator_types from the runtime config, creates each sub-operator, and
        wraps them in a CompositeOperator that implements AbstractOperator.

        The returned CompositeOperator can be passed directly to SandboxManager
        as a single operator — no changes to SandboxManager are needed.

        Args:
            context: OperatorContext containing all necessary dependencies

        Returns:
            CompositeOperator: A composite operator wrapping all configured sub-operators
        """
        operators = OperatorFactory.create_operators(context)
        default_type = context.runtime_config.operator_type.lower()
        return CompositeOperator(operators=operators, default_operator_type=default_type)
