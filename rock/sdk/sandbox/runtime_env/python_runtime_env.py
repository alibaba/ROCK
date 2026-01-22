from __future__ import annotations

import os
import shlex
from typing import TYPE_CHECKING

from typing_extensions import override

from rock import env_vars
from rock.logger import init_logger
from rock.sdk.sandbox.runtime_env.base import RuntimeEnv
from rock.sdk.sandbox.runtime_env.config import PythonRuntimeEnvConfig

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox

logger = init_logger(__name__)


class PythonRuntimeEnv(RuntimeEnv):
    """Python runtime env.

    Each PythonRuntimeEnv is identified by (type, version) and is managed by Sandbox.runtime_envs.
    workdir is auto-generated as: /rock-runtime-envs/python/{version}/

    Usage:
        env = PythonRuntimeEnv(sandbox, version="3.11", pip=["langchain"])
        await env.init()  # Installs Python and pip packages
        await env.run("python --version")
    """

    runtime_env_type: str = "python"

    def __init__(
        self,
        sandbox: Sandbox,
        runtime_env_config: PythonRuntimeEnvConfig,
    ) -> None:
        # Validate version early
        version = runtime_env_config.version
        if version not in ("3.11", "3.12", "default"):
            raise ValueError(f"Unsupported Python version: {version}. Supported versions: 3.11, 3.12, default")

        # Create base config with resolved version (extra="ignore" handles 'pip' and 'pip_index_url' fields)
        super().__init__(sandbox=sandbox, runtime_env_config=runtime_env_config)

        self._pip = runtime_env_config.pip
        self._pip_index_url = runtime_env_config.pip_index_url

    def _get_install_cmd(self) -> str:
        if self._version in ("3.11", "default"):
            return env_vars.ROCK_RTENV_PYTHON_V31114_INSTALL_CMD
        return env_vars.ROCK_RTENV_PYTHON_V31212_INSTALL_CMD

    @override
    async def _post_init(self) -> None:
        """Additional initialization after runtime installation.

        This method:
        1. Validates Python exists
        2. Configures pip index URL (if specified)
        3. Installs pip packages (if specified)
        """
        # Step 1: validate python exists
        await self._validate_python()

        # Step 2: configure pip index url if specified
        if self._pip_index_url:
            await self._configure_pip()

        # Step 3: install pip packages if specified
        if self._pip:
            await self._install_pip()

    async def _validate_python(self) -> None:
        """Validate Python executable exists."""
        return await self.run("test -x python")

    async def _configure_pip(self) -> None:
        """Configure pip index URL."""
        return await self.run(f"pip config set global.index-url {shlex.quote(self._pip_index_url)}")

    async def _install_pip(self) -> None:
        """Install pip packages."""
        if not self._pip:
            return

        if isinstance(self._pip, str):
            # Treat as requirements.txt path - upload it first
            if os.path.exists(self._pip):
                # Upload requirements.txt to sandbox, keep original filename
                original_filename = os.path.basename(self._pip)
                target_path = f"{self._workdir}/{original_filename}"
                await self._sandbox.upload_by_path(
                    source_path=os.path.abspath(self._pip),
                    target_path=target_path,
                )
                return await self.run(f"pip install -r {shlex.quote(target_path)}")
            else:
                raise FileNotFoundError(f"Requirements file not found: {self._pip}")
        else:
            # Treat as list of packages
            packages = " ".join([shlex.quote(pkg) for pkg in self._pip])
            return await self.run(f"pip install {packages}")
