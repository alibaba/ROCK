from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RuntimeEnvConfig(BaseModel):
    """Base configuration for runtime environments.

    This is an abstract base class. Use PythonRuntimeEnvConfig or NodeRuntimeEnvConfig
    for concrete configurations.
    """

    type: str = Field()
    """Runtime type discriminator. Must be 'python' or 'node'."""

    version: str = Field(default="default")
    """Runtime version. Use 'default' for the default version of each runtime."""

    env: dict[str, str] | None = Field(default=None)
    """Environment variables for the runtime session."""

    install_timeout: int = Field(default=600)
    """Timeout in seconds for installation commands."""

    custom_install_cmd: str | None = Field(default=None)
    """Custom install command to run after _do_init(). Supports && or ; for multi-step commands."""


class PythonRuntimeEnvConfig(RuntimeEnvConfig):
    """Configuration for Python runtime environment.

    Example:
        runtime_env_config=PythonRuntimeEnvConfig(
            version="default",  # defaults to 3.11
            pip=["langchain", "langchain-openai"],
            pip_index_url="https://mirrors.aliyun.com/pypi/simple/",
        )
    """

    type: Literal["python"] = Field(default="python")
    """Runtime type discriminator. Must be 'python'."""

    version: Literal["3.11", "3.12", "default"] = Field(default="default")
    """Python version. Use "default" for 3.11."""

    pip: list[str] | str | None = Field(default=None)
    """Pip packages to install.

    Can be:
    - list[str]: List of package names to install
    - str: Path to requirements.txt file
    - None: No packages to install
    """

    pip_index_url: str | None = Field(default="https://mirrors.aliyun.com/pypi/simple/")
    """Pip index URL for package installation. If set, will use this mirror."""


class NodeRuntimeEnvConfig(RuntimeEnvConfig):
    """Configuration for Node.js runtime environment.

    Example:
        runtime_env_config=NodeRuntimeEnvConfig(
            version="default",  # defaults to 22.18.0
            npm_registry="https://registry.npmmirror.com",
        )
    """

    type: Literal["node"] = Field(default="node")
    """Runtime type discriminator. Must be 'node'."""

    version: Literal["22.18.0", "default"] = Field(default="default")
    """Node.js version. Use "default" for 22.18.0."""

    npm_registry: str | None = Field(default=None)
    """NPM registry URL. If set, will run 'npm config set registry <url>' during init."""
