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


@dataclass
class WarmupConfig:
    images: list[str] | None = None


@dataclass
class NacosConfig:
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


@dataclass
class DatabaseConfig:
    url: str = ""


@dataclass
class RuntimeConfig:
    enable_auto_clear: bool = False
    project_root: str = field(default_factory=lambda: env_vars.ROCK_PROJECT_ROOT)
    python_env_path: str = field(default_factory=lambda: env_vars.ROCK_PYTHON_ENV_PATH)
    envhub_db_url: str = field(default_factory=lambda: env_vars.ROCK_ENVHUB_DB_URL)

    def __post_init__(self) -> None:
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

        return cls(**kwargs)

    def __post_init__(self) -> None:
        logger.info(f"init RockConfig: {self}")

        if self.nacos.endpoint:
            self.nacos_provider = NacosConfigProvider(
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
        if nacos_result:
            sandbox_config = SandboxConfig(**nacos_result)
            self.sandbox_config = sandbox_config
