import warnings

from pydantic import BaseModel, Field, field_validator

from rock import env_vars
from rock.deployments.log_cleanup import LogCleanupPolicy


class BaseConfig(BaseModel):
    base_url: str = env_vars.ROCK_BASE_URL
    xrl_authorization: str | None = Field(
        default=None,
        deprecated=True,
        description="DEPRECATED: Use extra_headers instead. Will be removed in the future ",
    )
    extra_headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("xrl_authorization")
    @classmethod
    def validate_xrl_authorization(cls, v):
        if v is not None:
            warnings.warn(
                "RockConfig.xrl_authorization is deprecated and will be removed in the future. "
                "Please migrate to using extra_headers={'XRL-Authorization': 'Baerer your_token'}",
                DeprecationWarning,
                stacklevel=2,
            )
        return v


class SandboxConfig(BaseConfig):
    image: str = "python:3.11"
    image_os: str = "linux"
    auto_clear_seconds: int = 60 * 5
    route_key: str | None = None
    startup_timeout: float = env_vars.ROCK_SANDBOX_STARTUP_TIMEOUT_SECONDS
    memory: str = "8g"
    cpus: float = 2
    limit_cpus: float | None = None
    user_id: str | None = None
    experiment_id: str | None = None
    cluster: str = "zb"
    namespace: str | None = None
    registry_username: str | None = None
    registry_password: str | None = None
    use_kata_runtime: bool = False
    sandbox_id: str | None = None
    auto_delete_seconds: int | None = None
    sandbox_log_cleanup_policy: LogCleanupPolicy | None = None
    """Override cluster default for sandbox log dir cleanup on stop.
    None = follow cluster (sandbox_config.sandbox_log_cleanup_policy_default).
    """

    @field_validator("auto_delete_seconds")
    @classmethod
    def validate_auto_delete_seconds(cls, v):
        if v is not None and v < 0:
            raise ValueError("auto_delete_seconds must be >= 0")
        return v


class SandboxGroupConfig(SandboxConfig):
    size: int = 2
    start_concurrency: int = 2
    start_retry_times: int = 3
