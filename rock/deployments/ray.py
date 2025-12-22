from typing_extensions import Self

from rock.deployments.config import DockerDeploymentConfig
from rock.deployments.docker import DockerDeployment
from rock.logger import init_logger
from rock.sandbox.sandbox_actor import SandboxActor
from rock.sdk.common.exceptions import InvalidParameterRockException
from rock.utils.format import parse_memory_size

logger = init_logger(__name__)


class RayDeployment(DockerDeployment):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    @classmethod
    def from_config(cls, config: DockerDeploymentConfig) -> Self:
        return cls(**config.model_dump())

    async def creator_actor(self, actor_name: str):
        return await self._create_sandbox_actor(actor_name)

    async def _create_sandbox_actor(self, actor_name: str):
        """Create sandbox actor instance"""
        actor_options = {"name": actor_name, "lifetime": "detached"}
        try:
            # TODO: check upper limit from runtime_config
            actor_options["num_cpus"] = self._config.cpus
            actor_options["memory"] = parse_memory_size(self._config.memory)
            sandbox_actor = SandboxActor.options(**actor_options).remote(self.config)
            return sandbox_actor
        except ValueError as e:
            logger.warning(f"Invalid memory size: {self._config.memory}", exc_info=e)
            raise InvalidParameterRockException(f"Invalid memory size: {self._config.memory}")
