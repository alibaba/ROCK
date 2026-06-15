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
    temp_dir: str | None = None
    ray_reconnect_enabled: bool = field(default=False)
    ray_reconnect_interval_seconds: int = field(default=60 * 60 * 12)
    ray_reconnect_request_threshold: int = field(default=10 * 1024 * 1024)
    ray_reconnect_check_interval_seconds: int = field(default=60 * 10)
    ray_reconnect_wait_timeout_seconds: int = field(default=30)
    ray_reconnect_max_attempts: int = field(default=2)
    ray_reconnect_retry_backoff_seconds: float = field(default=5.0)

    def __post_init__(self):
        if self.temp_dir:
            self.temp_dir = str(Path(self.temp_dir).resolve())


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
class SandboxLogConfig:
    """Policy for archiving stopped-sandbox log directories to OSS.

    Lives under SandboxConfig.log: the fields are domain knobs of "what to do
    with stopped sandbox logs" — when to archive, how many retries, what OSS
    key prefix to use — colocated with other sandbox lifecycle / cleanup
    policy (remove_container_enabled). OSS endpoint /
    bucket / credentials still belong to OssConfig.primary.
    """

    archive_prefix: str = "rock-archives/"
    """OSS object key prefix under OssConfig.primary.bucket for sandbox-log
    archives. The canonical default ``rock-archives/`` ensures all clusters
    write to a unified namespace. Override only for migration scenarios."""

    keep_days_before_archive: int = 3
    """Days to wait after sandbox stop before archiving. Gives operators
    a short investigation window without bloating disk."""

    archive_max_attempts: int = 3
    """Max retry attempts before giving up archival and degrading to KEEP
    (FileCleanupTask is the eventual janitor)."""


@dataclass
class SandboxFileTransferConfig:
    """Policy for sandbox <-> host file transfer via OSS as intermediary.

    Lives under SandboxConfig.file_transfer: the prefix governs where the
    SDK puts ephemeral transfer objects under OssConfig.primary.bucket,
    a sandbox-side concern not OSS connectivity.
    """

    prefix: str = ""
    """Prefix under OssConfig.primary.bucket for ephemeral host↔container
    file transfers ({timestamp}-{filename} objects). The legacy bucket keeps
    its pre-existing flat layout (no prefix) for backward compatibility —
    xrl-sandbox has a 3-day lifecycle rule at bucket root (configured in
    the Aliyun OSS console, not in repo) that we do not disturb.

    Note: this field lives in admin-side RockConfig and is NOT what the SDK
    reads. The SDK reads ROCK_OSS_TRANSFER_PREFIX directly from the process
    env. xrl package is no longer maintained, so internal users must export
    this env var themselves when upgrading to SDK >= 1.8."""


@dataclass
class SandboxConfig:
    actor_resource: str = ""
    actor_resource_num: float = 0.0
    gateway_num: int = 1
    remove_container_enabled: bool = True
    log: SandboxLogConfig = field(default_factory=SandboxLogConfig)
    file_transfer: SandboxFileTransferConfig = field(default_factory=SandboxFileTransferConfig)

    def __post_init__(self):
        # Allow YAML to pass dicts for nested dataclasses (yaml.safe_load
        # returns dicts, not nested dataclass instances).
        if isinstance(self.log, dict):
            self.log = SandboxLogConfig(**self.log)
        if isinstance(self.file_transfer, dict):
            self.file_transfer = SandboxFileTransferConfig(**self.file_transfer)


@dataclass
class OssAccountConfig:
    endpoint: str = ""
    bucket: str = ""
    access_key_id: str = ""
    access_key_secret: str = ""
    role_arn: str = ""
    region: str = ""
    """Region used to construct the STS AcsClient for this account. Falls back
    to env_vars.ROCK_OSS_BUCKET_REGION when empty, to preserve legacy behavior."""


@dataclass
class OssConfig:
    endpoint: str = ""
    bucket: str = ""
    access_key_id: str = ""
    access_key_secret: str = ""
    role_arn: str = ""
    region: str = ""
    """Region for the legacy STS AcsClient. Empty falls back to
    env_vars.ROCK_OSS_BUCKET_REGION, YAML-level values always win over env."""

    primary: OssAccountConfig = field(default_factory=OssAccountConfig)
    """Primary account used by SDK >= 1.8 (`/get_token?account=primary`) and by
    host-side archival. An empty `primary.bucket` disables v2 STS and archival,
    leaving legacy path fully operational."""

    def __post_init__(self):
        # Allow YAML to pass a dict for `primary` (dataclass deserialization
        # from yaml.safe_load returns dicts, not nested dataclasses).
        if isinstance(self.primary, dict):
            self.primary = OssAccountConfig(**self.primary)


@dataclass
class ArchiveDirStorageConfig:
    type: str = "oss"
    endpoint: str = ""
    bucket: str = ""
    access_key_id: str = ""
    access_key_secret: str = ""
    region: str = ""


@dataclass
class ArchiveAcrConfig:
    registry_url: str = ""
    username: str = ""
    password: str = ""
    namespace: str = "sandbox_archive"


@dataclass
class ArchiveConfig:
    dir_storage: ArchiveDirStorageConfig = field(default_factory=ArchiveDirStorageConfig)
    acr: ArchiveAcrConfig = field(default_factory=ArchiveAcrConfig)
    scan_interval_sec: int = 30
    timeout_sec: int = 1800
    max_retries: int = 3
    prefix: str = "rock-archives/"

    def __post_init__(self):
        if isinstance(self.dir_storage, dict):
            self.dir_storage = ArchiveDirStorageConfig(**self.dir_storage)
        if isinstance(self.acr, dict):
            self.acr = ArchiveAcrConfig(**self.acr)


@dataclass
class SandboxLifecycleConfig:
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)

    default_startup_timeout_seconds: float = 600
    """Default startup_timeout when SDK does not supply one. YAML: lifecycle.default_startup_timeout_seconds."""

    min_startup_timeout_seconds: float = 600
    """Floor for startup_timeout — SDK values below this are raised. YAML: lifecycle.min_startup_timeout_seconds."""

    max_startup_timeout_seconds: float = 1800
    """Ceiling for startup_timeout — values above this are capped. YAML: lifecycle.max_startup_timeout_seconds."""

    def __post_init__(self):
        if isinstance(self.archive, dict):
            self.archive = ArchiveConfig(**self.archive)


@dataclass
class ProxyServiceConfig:
    timeout: float = 180.0
    max_connections: int = 500
    max_keepalive_connections: int = 100
    batch_get_status_max_count: int = 2000
    aes_encrypt_key: str | None = None


@dataclass
class DatabaseConfig:
    # Supported URL formats:
    #   SQLite:     sqlite:///relative/path.db  or  sqlite:////absolute/path.db
    #   PostgreSQL: postgresql://user:password@host:port/dbname
    url: str = ""


@dataclass
class StandardSpec:
    memory: str = "8g"
    cpus: int = 2
    disk: str | None = None
    """Maximum disk quota a user can request. None means no upper limit."""


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
class ImageRegistryMirror:
    """A single internal registry that may host a mirrored copy of the sandbox image."""

    registry: str = ""
    namespace: str = ""
    username: str | None = None
    password: str | None = None


@dataclass
class PoolConfig:
    """Pool configuration with resource and port settings."""

    image: str
    cpus: float
    memory: str
    disk: str = ""
    ports: dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        # Ensure ports has default values
        default_ports = {"proxy": 8000, "server": 8080, "ssh": 22}
        for key, value in default_ports.items():
            if key not in self.ports:
                self.ports[key] = value


@dataclass
class K8sConfig:
    """Kubernetes configuration for K8s operator."""

    kubeconfig_path: str | None = None
    namespace: str = "rock"
    templates: dict[str, dict] = field(default_factory=dict)

    # Paths (relative to env yaml dir, or absolute) of YAML files holding shared
    # template definitions. Each file's top-level keys are template names. The
    # resolver in RockConfig.from_env loads these in order (later wins on key
    # conflict) then lets the inline `templates` block override. After loading
    # the field is consumed (cleared) so K8sConfig only carries `templates`.
    template_includes: list[str] = field(default_factory=list)

    # Template mapping: image_os -> template_name, e.g., {"windows": "windows_template", "linux": "default"}
    template_map: dict[str, str] = field(default_factory=dict)

    # API client rate limiting
    api_qps: float = 20.0  # Queries per second

    # Watch configuration
    resync_period: int = 60  # How often (seconds) to perform a full re-list

    # ============================================================================
    # DEPRECATED: The following fields are deprecated and will be removed in a
    # future version. Do NOT use them in new code.
    # ============================================================================
    watch_timeout_seconds: int = 60  # DEPRECATED: Use resync_period instead
    watch_reconnect_delay_seconds: int = 5  # DEPRECATED: No longer used


@dataclass
class RuntimeConfig:
    enable_auto_clear: bool = False
    project_root: str = field(default_factory=lambda: env_vars.ROCK_PROJECT_ROOT)
    python_env_path: str = field(default_factory=lambda: env_vars.ROCK_PYTHON_ENV_PATH)
    envhub_db_url: str = field(default_factory=lambda: env_vars.ROCK_ENVHUB_DB_URL)
    operator_type: str = "ray"
    standard_spec: StandardSpec = field(default_factory=StandardSpec)
    max_allowed_spec: StandardSpec = field(default_factory=lambda: StandardSpec(cpus=16, memory="64g", disk="256g"))
    use_standard_spec_only: bool = False
    metrics_endpoint: str = ""
    user_defined_tags: dict = field(default_factory=dict)
    sandbox_disk_limit_rootfs: str | None = None
    """Default rootfs quota per container. None means no limit. Can be overridden by nacos key 'default_disk_limit'."""

    sandbox_disk_overcommit_ratio: float | None = None
    """Disk overcommit ratio. E.g. 2.0 means user requests 20G but Docker storage-opt is 40G. None or <=1 means no overcommit."""

    instance_registry_mirrors: list[str] = field(default_factory=list)
    """Registry mirrors injected into each launched sandbox as
    INSTANCE_ROCK_REGISTRY=<comma-joined>. Each entry is a host/namespace
    string (e.g. "reg-a.aliyuncs.com/mirror-1"). When empty, the env var is
    not set and downstream tools skip mirror rewriting."""

    image_os_profiles: dict = field(default_factory=dict)
    """Profiles keyed by image_os value (e.g. "android"). Nacos image_os_profiles
    are merged on top at request time via _apply_image_os_profile().
    YAML: runtime.image_os_profiles.<image_os>: {runtime_env: {...}, startup_timeout, ...}"""

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


def _resolve_k8s_template_includes(k8s_dict: dict, base_dir: Path) -> None:
    """Resolve K8sConfig.template_includes in place.

    Reads each path under `template_includes` (relative paths are anchored at
    `base_dir`, typically the env yaml's parent directory), parses it as YAML
    whose top-level keys are template names, and merges the result into
    `templates`. Resolution rules:

    - Multiple includes: processed in declaration order; later wins per key.
    - Inline `templates`: takes precedence over any include.
    - Each template entry is replaced wholesale (no deep merge per template) so
      env-level overrides are explicit and unambiguous.

    The `template_includes` key is removed from the dict after resolution so
    that K8sConfig(**dict) sees an already-merged `templates` only.
    """
    includes = k8s_dict.pop("template_includes", None) or []
    if not includes:
        return

    inline = k8s_dict.get("templates") or {}
    merged: dict[str, dict] = {}
    for rel in includes:
        path = Path(rel)
        if not path.is_absolute():
            path = base_dir / path
        if not path.exists():
            raise FileNotFoundError(f"k8s.template_includes references missing file: {path}")
        with open(path) as f:
            loaded = yaml.safe_load(f) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"k8s template include {path} must be a mapping at top level")
        merged.update(loaded)
    merged.update(inline)
    k8s_dict["templates"] = merged


@dataclass
class RockConfig:
    ray: RayConfig = field(default_factory=RayConfig)
    k8s: K8sConfig = field(default_factory=K8sConfig)
    warmup: WarmupConfig = field(default_factory=WarmupConfig)
    nacos: NacosConfig = field(default_factory=NacosConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    sandbox_config: SandboxConfig = field(default_factory=SandboxConfig)
    oss: OssConfig = field(default_factory=OssConfig)
    lifecycle: SandboxLifecycleConfig = field(default_factory=SandboxLifecycleConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    proxy_service: ProxyServiceConfig = field(default_factory=ProxyServiceConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    image_registry_mirrors: list[ImageRegistryMirror] = field(default_factory=list)
    image_mirror_lookup_allowlist: list[str] = field(default_factory=list)
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

        # Handle _base config inheritance
        if "_base" in config:
            base_path = Path(config.pop("_base"))
            if not base_path.is_absolute():
                base_path = config_file.parent / base_path
            if not base_path.exists():
                raise Exception(f"base config file {base_path} not found")
            with open(base_path) as f:
                base_config = yaml.safe_load(f)
            config = cls._deep_merge(base_config, config)

        # Convert nested dictionaries to dataclass objects
        kwargs = {}
        if "ray" in config:
            kwargs["ray"] = RayConfig(**config["ray"])
        if "k8s" in config:
            _resolve_k8s_template_includes(config["k8s"], config_file.parent)
            kwargs["k8s"] = K8sConfig(**config["k8s"])
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
        if "lifecycle" in config:
            kwargs["lifecycle"] = SandboxLifecycleConfig(**config["lifecycle"])
        if "runtime" in config:
            kwargs["runtime"] = RuntimeConfig(**config["runtime"])
        if "proxy_service" in config:
            kwargs["proxy_service"] = ProxyServiceConfig(**config["proxy_service"])
        if "scheduler" in config:
            kwargs["scheduler"] = SchedulerConfig(**config["scheduler"])
        if "database" in config:
            kwargs["database"] = DatabaseConfig(**config["database"])
        if "image_registry_mirrors" in config:
            raw_mirrors = config["image_registry_mirrors"] or []
            kwargs["image_registry_mirrors"] = [ImageRegistryMirror(**m) for m in raw_mirrors]
        if "image_mirror_lookup_allowlist" in config:
            kwargs["image_mirror_lookup_allowlist"] = list(config["image_mirror_lookup_allowlist"] or [])

        return cls(**kwargs)

    # ============================================================================
    # Merging Rules:
    # 1. Dictionary elements within the list are matched based on their `task_class`.
    # 2. Matched elements: Regional configurations are deep-merged to override the base library (applied at the field level, not as a complete replacement).
    # 3. Unmatched base tasks: Retained as-is.
    # 4. Newly added regional tasks: Appended to the list.
    # 5. To "disable" a specific base task within a region: Set `enabled: false`.
    # ============================================================================

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> dict:
        """Deep merge override into base. Override values take precedence."""
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = RockConfig._deep_merge(result[key], value)
            elif key in result and isinstance(result[key], list) and isinstance(value, list):
                result[key] = RockConfig._merge_lists(result[key], value)
            else:
                result[key] = value
        return result

    @staticmethod
    def _merge_lists(base_list: list, override_list: list) -> list:
        """Merge two lists. For lists of dicts with 'task_class' key, merge by that key."""
        if not base_list or not override_list:
            return override_list if override_list else base_list

        # Check if both lists contain dicts with a common identity key
        merge_key = None
        for candidate in ("task_class", "name", "id"):
            if all(isinstance(item, dict) and candidate in item for item in base_list) and all(
                isinstance(item, dict) and candidate in item for item in override_list
            ):
                merge_key = candidate
                break

        if not merge_key:
            # No merge key found, override replaces base entirely
            return override_list

        # Merge by key: base items are kept/overridden, new override items appended
        override_map = {item[merge_key]: item for item in override_list}
        result = []
        seen = set()
        for item in base_list:
            key_val = item[merge_key]
            if key_val in override_map:
                # Deep merge the matching item
                result.append(RockConfig._deep_merge(item, override_map[key_val]))
            else:
                result.append(item)
            seen.add(key_val)
        # Append new items from override that aren't in base
        for item in override_list:
            if item[merge_key] not in seen:
                result.append(item)
        return result

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
            "lifecycle": (SandboxLifecycleConfig, "lifecycle"),
        }

        # Update configs that are present in nacos_result (field-level merge,
        # then re-run __post_init__ to coerce nested dicts → dataclasses)
        for key, (config_class, attr_name) in config_map.items():
            if key in nacos_result:
                existing = getattr(self, attr_name)
                for field_name, value in nacos_result[key].items():
                    if hasattr(existing, field_name):
                        setattr(existing, field_name, value)
                if hasattr(existing, "__post_init__"):
                    existing.__post_init__()

        if "image_registry_mirrors" in nacos_result:
            raw_mirrors = nacos_result["image_registry_mirrors"] or []
            self.image_registry_mirrors = [ImageRegistryMirror(**m) for m in raw_mirrors]
        if "image_mirror_lookup_allowlist" in nacos_result:
            self.image_mirror_lookup_allowlist = list(nacos_result["image_mirror_lookup_allowlist"] or [])

        if "runtime" in nacos_result and isinstance(nacos_result["runtime"], dict):
            runtime_overrides = nacos_result["runtime"]
            if "instance_registry_mirrors" in runtime_overrides:
                self.runtime.instance_registry_mirrors = list(runtime_overrides["instance_registry_mirrors"] or [])


        logger.info(
            f"Updated config from Nacos: sandbox_config={self.sandbox_config}, proxy_service={self.proxy_service}"
            f", image_registry_mirrors={self.image_registry_mirrors}"
            f", image_mirror_lookup_allowlist={self.image_mirror_lookup_allowlist}"
            f", instance_registry_mirrors={self.runtime.instance_registry_mirrors}"
        )
