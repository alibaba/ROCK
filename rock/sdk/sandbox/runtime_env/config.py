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

    extra_symlink_dir: str | None = Field(default=None)
    """Directory to create symlinks of executables. If None, no symlinks are created."""

    extra_symlink_executables: list[str] = Field(default_factory=list)
    """List of executable names to symlink. Empty list means no symlinks."""
