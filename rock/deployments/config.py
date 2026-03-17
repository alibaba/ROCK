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

    image_os: str = "linux"

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
    """Number of CPU cores to allocate for the container. Used as --cpu-shares (cpus * 1024)."""

    limit_cpus: float | None = None
    """Hard limit on the number of CPU cores the container can use. Used as --cpus when CPU preemption is enabled via nacos switch."""

    container_name: str | None = None
    """Custom name for the container. If None, a random name will be generated."""

    auto_delete_seconds: int | None = None
    """If set, the container will be automatically deleted after container stopped."""

    type: Literal["docker"] = "docker"
    """Deployment type discriminator for serialization/deserialization and CLI parsing. Should not be modified."""

    enable_auto_clear: bool = False
    """Enable automatic container cleanup based on auto_clear_time."""

    use_kata_runtime: bool = False
    """Whether to use kata container runtime (io.containerd.kata.v2) instead of --privileged mode."""

    kata_disk_size: str = "50G"
    """Size of the sparse disk image for kata DinD. Can be overridden by nacos config 'kata_dind_disk_size'."""

    kata_disk_base_path: str = "/data/docker-disk"
    """Base directory on the host for storing kata disk image files."""

    # TODO: Refine these fields in future versions
    actor_resource: str | None = None
    """Resource type for actor allocation (to be refined)."""

    actor_resource_num: float = 1
    """Number of actor resources to allocate (to be refined)."""

    registry_username: str | None = None
    """Username for Docker registry authentication. When both username and password are provided, docker login will be performed before pulling the image."""

    registry_password: str | None = Field(default=None, repr=False, exclude=True)
    """Password for Docker registry authentication. When both username and password are provided, docker login will be performed before pulling the image."""

    runtime_config: RuntimeConfig = Field(default_factory=RuntimeConfig)
    """Runtime configuration settings."""

    extended_params: dict[str, str] = Field(default_factory=dict)
    """Generic extension field for storing custom string key-value pairs."""

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
        return cls(
            **request.model_dump(exclude={"sandbox_id"}),
            container_name=request.sandbox_id,
        )


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


class FCDeploymentConfig(DeploymentConfig):
    """Configuration for Alibaba Cloud Function Compute deployment.

    This deployment type enables serverless sandbox execution using FC
    with WebSocket session management for stateful operations.

    FC (Function Compute) is Alibaba Cloud's serverless compute service:
    https://www.alibabacloud.com/product/function-compute

    Configuration Hierarchy:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  FCConfig (Admin 服务级) - 本文件                                    │
    │  - Admin 启动时加载，提供默认值和凭证                                 │
    │  - 服务级设置 (region, account_id, credentials)                     │
    └─────────────────────────────────────────────────────────────────────┘
                              │
                              │ merge_with_fc_config()
                              ▼
    ┌─────────────────────────────────────────────────────────────────────┐
    │  FCDeploymentConfig (API 调用级)                                     │
    │  - 每个 sandbox API 请求创建                                         │
    │  - session_id 与 ROCK sandbox_id 1:1 映射                           │
    │  - 用于调用已部署的 FC 函数，不涉及函数部署                            │
    │  - sandbox_ttl/sandbox_idle_timeout: ROCK 内部沙箱生命周期管理        │
    └─────────────────────────────────────────────────────────────────────┘
                              │
                              │ 调用已部署的 FC 函数
                              ▼
    ┌─────────────────────────────────────────────────────────────────────┐
    │  s.yaml (FC 函数部署配置)                                            │
    │  - 定义 FC 函数资源规格，通过 `s deploy` 部署                         │
    │  - Session affinity, memory, CPU, timeout 等                        │
    │  - 位置: rock/deployments/fc_rocklet/{runtime,container,adapter}/    │
    │  - 函数部署是前置条件，Admin 调用时函数已存在                           │
    └─────────────────────────────────────────────────────────────────────┘

    The session_id serves as both the FC session identifier (for WebSocket
    stateful invocation routing) and the ROCK sandbox_id (for business logic).
    """

    type: Literal["fc"] = "fc"
    """Deployment type discriminator for JSON/YAML parsing."""

    session_id: str | None = None
    """FC session identifier for stateful invocation routing.

    This serves as both:
    - FC native session_id: Routes WebSocket requests to the same function instance
    - ROCK sandbox_id: Used for lifecycle management, billing, and state tracking

    If None, will be auto-generated as 'fc-{uuid}'.
    """

    # Connection settings (optional, use FCConfig defaults if not provided)
    function_name: str | None = None
    """FC function name. If None, uses FCConfig.function_name."""

    region: str | None = None
    """Alibaba Cloud region. If None, uses FCConfig.region."""

    account_id: str | None = None
    """Alibaba Cloud account ID. If None, uses FCConfig.account_id."""

    access_key_id: str | None = None
    """AccessKey ID. If None, uses FCConfig.access_key_id."""

    access_key_secret: str | None = Field(default=None, repr=False, exclude=True)
    """AccessKey Secret. If None, uses FCConfig.access_key_secret."""

    security_token: str | None = None
    """STS security token. If None, uses FCConfig.security_token."""

    # Resource settings (optional, use FCConfig defaults if not provided)
    memory: int | None = None
    """Memory in MB. If None, uses FCConfig.default_memory."""

    cpus: float | None = None
    """CPU cores. If None, uses FCConfig.default_cpus."""

    # Timeout settings (optional, use FCConfig defaults if not provided, all in seconds)
    sandbox_ttl: int | None = None
    """Sandbox time-to-live in seconds. If None, uses FCConfig.default_session_ttl."""

    sandbox_idle_timeout: int | None = None
    """Sandbox idle timeout in seconds. If None, uses FCConfig.default_session_idle_timeout."""

    timeout: float | None = None
    """Request timeout in seconds. If None, uses FCConfig.default_timeout."""

    def get_deployment(self) -> AbstractDeployment:
        from rock.deployments.fc import FCDeployment

        return FCDeployment.from_config(self)

    def merge_with_fc_config(self, fc_config: "FCConfig") -> "FCDeploymentConfig":
        """Merge this config with FCConfig defaults.

        Args:
            fc_config: Admin-level FC configuration with defaults.

        Returns:
            New FCDeploymentConfig with all fields populated.
        """
        from rock.config import FCConfig

        return FCDeploymentConfig(
            type=self.type,
            session_id=self.session_id,
            function_name=self.function_name or fc_config.function_name,
            region=self.region or fc_config.region,
            account_id=self.account_id or fc_config.account_id,
            access_key_id=self.access_key_id or fc_config.access_key_id,
            access_key_secret=self.access_key_secret or fc_config.access_key_secret,
            security_token=self.security_token or fc_config.security_token,
            memory=self.memory or fc_config.default_memory,
            cpus=self.cpus or fc_config.default_cpus,
            sandbox_ttl=self.sandbox_ttl or fc_config.default_session_ttl,
            sandbox_idle_timeout=self.sandbox_idle_timeout or fc_config.default_session_idle_timeout,
            timeout=self.timeout or fc_config.default_timeout,
        )


def get_deployment(config: DeploymentConfig) -> AbstractDeployment:
    """Create a deployment instance from the given configuration.

    Args:
        config: Deployment configuration instance.

    Returns:
        AbstractDeployment: The configured deployment instance.
    """
    return config.get_deployment()
