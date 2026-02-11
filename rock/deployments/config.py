"""
Deployment configuration classes for different runtime environments.

This module defines configuration classes for various deployment types including
local, Docker, Ray, remote, and dummy deployments. Each configuration class
provides settings specific to its deployment environment.
"""

from abc import abstractmethod
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rock.admin.proto.request import SandboxStartRequest
from rock.config import RuntimeConfig
from rock.deployments.abstract import AbstractDeployment
from rock.utils import REQUEST_TIMEOUT_SECONDS


class DeploymentConfig(BaseModel):
    """Base configuration class for all deployment types."""

    model_config = ConfigDict(extra="forbid")

    role: str = Field(default="test", description="Role identifier for the deployment.")
    """TODO: Remove this field in future versions."""

    env: str = Field(default="dev", description="Environment identifier for the deployment.")
    """TODO: Remove this field in future versions."""

    @abstractmethod
    def get_deployment(self) -> AbstractDeployment:
        """Create and return the deployment instance.

        Returns:
            AbstractDeployment: The configured deployment instance.
        """


class LocalDeploymentConfig(DeploymentConfig):
    """Configuration for local deployment without containerization."""

    type: Literal["local"] = "local"
    """Deployment type discriminator for serialization/deserialization and CLI parsing. Should not be modified."""

    def get_deployment(self) -> AbstractDeployment:
        from rock.deployments.local import LocalDeployment

        return LocalDeployment.from_config(self)


class DockerDeploymentConfig(DeploymentConfig):
    """Configuration for Docker-based deployment with containerization."""

    image: str = "python:3.11"
    """Docker image name to use for the container."""

    port: int | None = None
    """Port number for container communication. If None, an available port will be automatically assigned."""

    docker_args: list[str] = []
    """Additional arguments to pass to the docker run command. Platform arguments will be moved to the platform field."""

    startup_timeout: float = REQUEST_TIMEOUT_SECONDS
    """Maximum time in seconds to wait for the runtime to start up."""

    pull: Literal["never", "always", "missing"] = "missing"
    """Docker image pull policy: 'never', 'always', or 'missing'."""

    remove_images: bool = False
    """Whether to remove the Docker image after the container stops."""

    python_standalone_dir: str | None = None
    """Directory path for Python standalone installation within the container."""

    platform: str | None = None
    """Target platform for the Docker image (e.g., 'linux/amd64', 'linux/arm64')."""

    remove_container: bool = True
    """Whether to remove the container after it stops running."""

    auto_clear_time_minutes: int = 30
    """Automatic container cleanup time in minutes."""

    memory: str = "8g"
    """Memory allocation for the container (e.g., '8g', '4096m')."""

    cpus: float = 2
    """Number of CPU cores to allocate for the container."""

    container_name: str | None = None
    """Custom name for the container. If None, a random name will be generated."""

    type: Literal["docker"] = "docker"
    """Deployment type discriminator for serialization/deserialization and CLI parsing. Should not be modified."""

    enable_auto_clear: bool = False
    """Enable automatic container cleanup based on auto_clear_time."""

    use_kata_runtime: bool = False
    """Whether to use kata container runtime (io.containerd.kata.v2) instead of --privileged mode."""

    # TODO: Refine these fields in future versions
    actor_resource: str | None = None
    """Resource type for actor allocation (to be refined)."""

    actor_resource_num: float = 1
    """Number of actor resources to allocate (to be refined)."""

    runtime_config: RuntimeConfig = Field(default_factory=RuntimeConfig)
    """Runtime configuration settings."""

    @model_validator(mode="before")
    def validate_platform_args(cls, data: dict) -> dict:
        """Validate and extract platform arguments from docker_args.

        This validator ensures that platform specification is consistent between
        the platform field and --platform arguments in docker_args.
        """
        if not isinstance(data, dict):
            return data

        docker_args = data.get("docker_args", [])
        platform = data.get("platform")

        platform_arg_idx = next((i for i, arg in enumerate(docker_args) if arg.startswith("--platform")), -1)

        if platform_arg_idx != -1:
            if platform is not None:
                msg = "Cannot specify platform both via 'platform' field and '--platform' in docker_args"
                raise ValueError(msg)
            # Extract platform value from --platform argument
            if "=" in docker_args[platform_arg_idx]:
                # Handle case where platform is specified as --platform=value
                data["platform"] = docker_args[platform_arg_idx].split("=", 1)[1]
                data["docker_args"] = docker_args[:platform_arg_idx] + docker_args[platform_arg_idx + 1 :]
            elif platform_arg_idx + 1 < len(docker_args):
                data["platform"] = docker_args[platform_arg_idx + 1]
                # Remove the --platform and its value from docker_args
                data["docker_args"] = docker_args[:platform_arg_idx] + docker_args[platform_arg_idx + 2 :]
            else:
                msg = "--platform argument must be followed by a value"
                raise ValueError(msg)

        return data

    def get_deployment(self) -> AbstractDeployment:
        from rock.deployments.docker import DockerDeployment

        return DockerDeployment.from_config(self)

    @property
    def auto_clear_time(self) -> int:
        return self.auto_clear_time_minutes

    @classmethod
    def from_request(cls, request: SandboxStartRequest) -> DeploymentConfig:
        """Create DockerDeploymentConfig from SandboxStartRequest"""
        return cls(**request.model_dump(exclude={"sandbox_id"}), container_name=request.sandbox_id)


class RayDeploymentConfig(DockerDeploymentConfig):
    """Configuration for Ray-based distributed deployment."""

    # TODO: Refine these fields in future versions
    actor_resource: str | None = None
    """Resource type for Ray actor allocation (to be refined)."""

    actor_resource_num: int = 1
    """Number of Ray actor resources to allocate (to be refined)."""

    def get_deployment(self) -> AbstractDeployment:
        from rock.deployments.ray import RayDeployment

        return RayDeployment.from_config(self)


class RemoteDeploymentConfig(DeploymentConfig):
    """Configuration for remote deployment connecting to an existing rocklet server.

    This deployment type acts as a wrapper around RemoteRuntime and can be used
    to connect to any running rocklet server instance.
    """

    host: str = "http://127.0.0.1"
    """Remote server host URL or IP address."""

    port: int | None = None
    """Remote server port number. If None, uses default port."""

    timeout: float = 0.15
    """Connection timeout in seconds for remote server communication."""

    type: Literal["remote"] = "remote"
    """Deployment type discriminator for serialization/deserialization and CLI parsing. Should not be modified."""

    def get_deployment(self) -> AbstractDeployment:
        from rock.deployments.remote import RemoteDeployment

        return RemoteDeployment.from_config(self)


def get_deployment(config: DeploymentConfig) -> AbstractDeployment:
    """Create a deployment instance from the given configuration.

    Args:
        config: Deployment configuration instance.

    Returns:
        AbstractDeployment: The configured deployment instance.
    """
    return config.get_deployment()
