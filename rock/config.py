import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from rock import env_vars
from rock.logger import init_logger
from rock.utils.database import is_absolute_db_path
from rock.utils.providers import NacosConfigProvider

logger = init_logger(__name__)


@dataclass
class RayConfig:
    address: str | None = None
    runtime_env: dict = field(default_factory=dict)
    namespace: str = "xrl-sandbox"
    resources: dict | None = None
    ray_reconnect_enabled: bool = field(default=False)
    ray_reconnect_interval_seconds: int = field(default=60 * 60 * 12)
    ray_reconnect_request_threshold: int = field(default=10 * 1024 * 1024)
    ray_reconnect_check_interval_seconds: int = field(default=60 * 10)
    ray_reconnect_wait_timeout_seconds: int = field(default=30)


@dataclass
class WarmupConfig:
    images: list[str] | None = None


@dataclass
class NacosConfig:
    server_addresses: str = ""
    endpoint: str = ""
    group: str = ""
    data_id: str = ""


@dataclass
class RedisConfig:
    host: str = ""
    port: int = 0
    password: str = ""


@dataclass
class SandboxConfig:
    actor_resource: str = ""
    actor_resource_num: float = 0.0
    gateway_num: int = 1


@dataclass
class OssConfig:
    endpoint: str = ""
    bucket: str = ""
    access_key_id: str = ""
    access_key_secret: str = ""
    role_arn: str = ""


@dataclass
class ProxyServiceConfig:
    timeout: float = 180.0
    max_connections: int = 500
    max_keepalive_connections: int = 100
    batch_get_status_max_count: int = 2000
    aes_encrypt_key: str | None = None


@dataclass
class DatabaseConfig:
    url: str = ""


@dataclass
class StandardSpec:
    memory: str = "8g"
    cpus: int = 2


@dataclass
class TaskConfig:
    """Configuration for a single scheduled task."""

    task_class: str = ""  # Fully qualified class path of the task
    enabled: bool = True  # Whether the task is enabled
    interval_seconds: int = 3600  # Execution interval in seconds
    params: dict = field(default_factory=dict)  # Task-specific parameters


@dataclass
class SchedulerConfig:
    """Scheduler configuration."""

    enabled: bool = False
    worker_cache_ttl: int = 3600
    tasks: list[TaskConfig] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Convert list of dicts to list of TaskConfig objects
        if self.tasks and isinstance(self.tasks[0], dict):
            self.tasks = [TaskConfig(**task) for task in self.tasks]


@dataclass
class RuntimeConfig:
    enable_auto_clear: bool = False
    project_root: str = field(default_factory=lambda: env_vars.ROCK_PROJECT_ROOT)
    python_env_path: str = field(default_factory=lambda: env_vars.ROCK_PYTHON_ENV_PATH)
    envhub_db_url: str = field(default_factory=lambda: env_vars.ROCK_ENVHUB_DB_URL)
    operator_type: str = "ray"
    standard_spec: StandardSpec = field(default_factory=StandardSpec)
    max_allowed_spec: StandardSpec = field(default_factory=lambda: StandardSpec(cpus=16, memory="64g"))
    metrics_endpoint: str = ""

    def __post_init__(self) -> None:
        # Convert dict to StandardSpec if needed
        if isinstance(self.standard_spec, dict):
            self.standard_spec = StandardSpec(**self.standard_spec)
        if isinstance(self.max_allowed_spec, dict):
            self.max_allowed_spec = StandardSpec(**self.max_allowed_spec)

        if not self.python_env_path:
            raise Exception(
                "ROCK_PYTHON_ENV_PATH is not set, please specify the actual Python environment path "
                "(e.g., conda or system Python) that uv depends on"
            )

        if not self.envhub_db_url:
            raise Exception("ROCK_ENVHUB_DB_URL is not set, please specify the actual EnvHub database URL")

        # Considering Ray's distributed nature, the passed-in envhub_db_url may itself be a relative path, so it should not be pass as a relative path

        if not is_absolute_db_path(self.envhub_db_url):
            raise Exception("ROCK_ENVHUB_DB_URL is not an absolute path")


@dataclass
class RockConfig:
    ray: RayConfig = field(default_factory=RayConfig)
    warmup: WarmupConfig = field(default_factory=WarmupConfig)
    nacos: NacosConfig = field(default_factory=NacosConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    sandbox_config: SandboxConfig = field(default_factory=SandboxConfig)
    oss: OssConfig = field(default_factory=OssConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    proxy_service: ProxyServiceConfig = field(default_factory=ProxyServiceConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    nacos_provider: NacosConfigProvider | None = None

    @classmethod
    def from_env(cls, config_path: str | None = None):
        if not config_path:
            config_path = env_vars.ROCK_CONFIG

        if not config_path:
            return cls()

        config_file = Path(config_path)

        if not config_file.exists():
            raise Exception(f"config file {config_file} not found")

        config: dict
        with open(config_file) as f:
            config = yaml.safe_load(f)

        # Convert nested dictionaries to dataclass objects
        kwargs = {}
        if "ray" in config:
            kwargs["ray"] = RayConfig(**config["ray"])
        if "warmup" in config:
            kwargs["warmup"] = WarmupConfig(**config["warmup"])
        if "nacos" in config:
            kwargs["nacos"] = NacosConfig(**config["nacos"])
        if "redis" in config:
            kwargs["redis"] = RedisConfig(**config["redis"])
        if "sandbox_config" in config:
            kwargs["sandbox_config"] = SandboxConfig(**config["sandbox_config"])
        if "oss" in config:
            kwargs["oss"] = OssConfig(**config["oss"])
        if "runtime" in config:
            kwargs["runtime"] = RuntimeConfig(**config["runtime"])
        if "proxy_service" in config:
            kwargs["proxy_service"] = ProxyServiceConfig(**config["proxy_service"])
        if "scheduler" in config:
            kwargs["scheduler"] = SchedulerConfig(**config["scheduler"])

        return cls(**kwargs)

    def __post_init__(self) -> None:
        logger.info(f"init RockConfig: {self}")

        if self.nacos.endpoint or self.nacos.server_addresses:
            self.nacos_provider = NacosConfigProvider(
                server_addresses=self.nacos.server_addresses,
                endpoint=self.nacos.endpoint,
                namespace="",
                data_id=self.nacos.data_id,
                group=self.nacos.group,
            )
            self.nacos_provider.add_listener()
            logging.getLogger("nacos.client").setLevel(logging.WARNING)
            logging.getLogger("do-pulling").setLevel(logging.WARNING)
            logging.getLogger("process-polling-result").setLevel(logging.WARNING)

    async def update(self):
        if self.nacos_provider is None:
            return

        nacos_result = await self.nacos_provider.get_config()
        if not nacos_result:
            return

        # Map config keys to their corresponding dataclass types
        config_map = {
            "sandbox_config": (SandboxConfig, "sandbox_config"),
            "proxy_service": (ProxyServiceConfig, "proxy_service"),
        }

        # Update configs that are present in nacos_result
        for key, (config_class, attr_name) in config_map.items():
            if key in nacos_result:
                setattr(self, attr_name, config_class(**nacos_result[key]))

        logger.info(
            f"Updated config from Nacos: sandbox_config={self.sandbox_config}, proxy_service={self.proxy_service}"
        )
