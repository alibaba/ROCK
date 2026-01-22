from pydantic import BaseModel, Field


class RuntimeEnvConfig(BaseModel):
    """Base configuration for runtime environments."""

    type: str = Field()
    """Runtime type discriminator."""

    version: str = Field(default="default")
    """Runtime version. Use 'default' for the default version of each runtime."""

    env: dict[str, str] = Field(default_factory=dict)
    """Environment variables for the runtime session."""

    install_timeout: int = Field(default=600)
    """Timeout in seconds for installation commands."""

    custom_install_cmd: str | None = Field(default=None)
    """Custom install command to run after init. Supports && or ; for multi-step commands."""
