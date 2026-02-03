import warnings

from pydantic import BaseModel, Field, field_validator

from rock import env_vars


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
    auto_clear_seconds: int = 60 * 5
    route_key: str | None = None
    startup_timeout: float = env_vars.ROCK_SANDBOX_STARTUP_TIMEOUT_SECONDS
    memory: str = "8g"
    cpus: float = 2
    user_id: str | None = None
    experiment_id: str | None = None
    cluster: str = "zb"
    namespace: str | None = None


class SandboxGroupConfig(SandboxConfig):
    size: int = 2
    start_concurrency: int = 2
    start_retry_times: int = 3
