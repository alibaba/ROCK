import uuid

from rock import env_vars
from rock.config import RockConfig
from rock.deployments.config import AbstractDeployment, DeploymentConfig, DockerDeploymentConfig, RayDeploymentConfig
from rock.logger import init_logger
from rock.utils import sandbox_id_ctx_var

logger = init_logger(__name__)


class DeploymentManager:
    rock_config: RockConfig | None = None

    def __init__(self, rock_config: RockConfig, enable_runtime_auto_clear: bool = False):
        self._enable_runtime_auto_clear = enable_runtime_auto_clear
        self.rock_config = rock_config

    async def init_config(self, config: DeploymentConfig) -> DockerDeploymentConfig:
        _role = env_vars.ROCK_ADMIN_ROLE
        _env = env_vars.ROCK_ADMIN_ENV
        sandbox_id = uuid.uuid4().hex
        sandbox_id_ctx_var.set(sandbox_id)

        # TODO: get ray from config
        docker_deployment_config = RayDeploymentConfig(**config.model_dump())
        docker_deployment_config.role = _role
        docker_deployment_config.env = _env
        docker_deployment_config.container_name = sandbox_id
        docker_deployment_config.enable_auto_clear = self._enable_runtime_auto_clear
        docker_deployment_config.runtime_config = self.rock_config.runtime

        await self.rock_config.update()
        docker_deployment_config.actor_resource = self.rock_config.sandbox_config.actor_resource
        docker_deployment_config.actor_resource_num = self.rock_config.sandbox_config.actor_resource_num
        return docker_deployment_config

    def get_deployment(self, config: DeploymentConfig) -> AbstractDeployment:
        assert isinstance(config, RayDeploymentConfig)
        return config.get_deployment()

    def get_actor_name(self, sandbox_id):
        return f"sandbox-{sandbox_id}"
