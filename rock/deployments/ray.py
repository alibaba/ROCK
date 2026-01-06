from typing_extensions import Self

from rock import BadRequestRockError
from rock.config import RuntimeConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.deployments.docker import DockerDeployment
from rock.logger import init_logger
from rock.sandbox.sandbox_actor import SandboxActor
from rock.utils.format import parse_memory_size

logger = init_logger(__name__)


class RayDeployment(DockerDeployment):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    @classmethod
    def from_config(cls, config: DockerDeploymentConfig) -> Self:
        return cls(**config.model_dump())

    async def creator_actor(self, actor_name: str, runtime_config: RuntimeConfig):
        return await self._create_sandbox_actor(actor_name, runtime_config)

    async def _create_sandbox_actor(self, actor_name: str, runtime_config: RuntimeConfig):
        """创建沙箱Actor实例"""
        # 只有 DockerDeploymentConfig 才指定 cpu 和内存参数
        actor_options = self._generate_actor_options(actor_name, self._config, runtime_config)
        sandbox_actor = SandboxActor.options(**actor_options).remote(self._config, self)
        return sandbox_actor

    def _generate_actor_options(self, actor_name: str, runtime_config: RuntimeConfig) -> dict:
        actor_options = {"name": actor_name, "lifetime": "detached"}
        if not isinstance(self._config, DockerDeploymentConfig):
            return actor_options
        try:
            memory = parse_memory_size(self._config.memory)
            max_memory = parse_memory_size(runtime_config.max_allowed_spec.memory)
            if self._config.cpus > runtime_config.max_allowed_spec.cpus:
                raise BadRequestRockError(
                    f"Requested CPUs {self._config.cpus} exceed the maximum allowed {runtime_config.max_allowed_spec.cpus}"
                )
            if memory > max_memory:
                raise BadRequestRockError(
                    f"Requested memory {self._config.memory} exceed the maximum allowed {runtime_config.max_allowed_spec.memory}"
                )
            actor_options["num_cpus"] = self._config.cpus
            actor_options["memory"] = memory
            return actor_options
        except ValueError as e:
            logger.warning(f"Invalid memory size: {self._config.memory}", exc_info=e)
            raise BadRequestRockError(f"Invalid memory size: {self._config.memory}")
