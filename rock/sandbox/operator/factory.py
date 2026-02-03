"""Operator factory for creating operator instances based on configuration."""

from rock.admin.core.ray_service import RayService
from rock.config import RuntimeConfig
from rock.logger import init_logger
from rock.sandbox.operator.abstract import AbstractOperator
from rock.sandbox.operator.ray import RayOperator

logger = init_logger(__name__)


class OperatorFactory:
    """Factory class for creating operator instances."""

    @staticmethod
    def create_operator(runtime_config: RuntimeConfig, ray_service: RayService | None = None) -> AbstractOperator:
        """Create an operator instance based on the runtime configuration.

        Args:
            runtime_config: Runtime configuration containing operator_type
            ray_service: Ray service instance (required for RayOperator)

        Returns:
            AbstractOperator: The created operator instance

        Raises:
            ValueError: If operator_type is not supported or required dependencies are missing
        """
        operator_type = runtime_config.operator_type.lower()

        if operator_type == "ray":
            if ray_service is None:
                raise ValueError("RayService is required for RayOperator")
            logger.info("Creating RayOperator")
            return RayOperator(ray_service=ray_service)
        else:
            raise ValueError(f"Unsupported operator type: {operator_type}. " f"Supported types: ray")
