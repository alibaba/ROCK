from __future__ import annotations

import os
import shlex
from typing import TYPE_CHECKING

from rock import env_vars
from rock.logger import init_logger
from rock.sdk.sandbox.runtime_env.base import RuntimeEnv
from rock.sdk.sandbox.runtime_env.config import PythonRuntimeEnvConfig
from rock.sdk.sandbox.utils import arun_with_retry, with_time_logging

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox

logger = init_logger(__name__)

# Version to install command mapping
_PYTHON_VERSION_MAP: dict[str, str] = {
    "3.11": env_vars.ROCK_RTENV_PYTHON_V31114_INSTALL_CMD,
    "default": env_vars.ROCK_RTENV_PYTHON_V31114_INSTALL_CMD,
    "3.12": env_vars.ROCK_RTENV_PYTHON_V31212_INSTALL_CMD,
}


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
        # Create base config with resolved version (extra="ignore" handles 'pip' and 'pip_index_url' fields)
        super().__init__(sandbox=sandbox, runtime_env_config=runtime_env_config)

        self.pip = runtime_env_config.pip
        self.pip_index_url = runtime_env_config.pip_index_url

        # Get install command based on version
        version = runtime_env_config.version
        if version not in _PYTHON_VERSION_MAP:
            supported = list(_PYTHON_VERSION_MAP.keys())
            raise ValueError(f"Unsupported Python version: {version}. Supported versions: {supported}")
        self.python_install_cmd = _PYTHON_VERSION_MAP[version]

    async def _do_init(self) -> None:
        """Initialize and install Python runtime environment.

        This method:
        1. Installs Python runtime
        2. Validates Python exists
        3. Configures pip index URL (if specified)
        4. Installs pip packages (if specified)
        """
        # Step 1: install Python runtime
        await self._install_python_runtime()

        # Step 2: validate python exists
        await self._validate_python()

        # Step 3: configure pip index url if specified
        if self.pip_index_url:
            await self._configure_pip()

        # Step 4: install pip packages if specified
        if self.pip:
            await self._install_pip()

    @with_time_logging("Installing Python runtime")
    async def _install_python_runtime(self) -> None:
        """Install Python runtime."""
        from rock.sdk.sandbox.client import RunMode

        install_cmd = f"cd {shlex.quote(self.workdir)} && {self.python_install_cmd}"
        await arun_with_retry(
            sandbox=self._sandbox,
            cmd=f"bash -c {shlex.quote(install_cmd)}",
            session=self.session,
            mode=RunMode.NOHUP,
            wait_timeout=self.install_timeout,
            error_msg="Python runtime installation failed",
        )

    async def _validate_python(self) -> None:
        """Validate Python executable exists."""
        return await self.run("test -x python")

    async def _configure_pip(self) -> None:
        """Configure pip index URL."""
        return await self.run(f"pip config set global.index-url {shlex.quote(self.pip_index_url)}")

    @with_time_logging("Installing pip packages")
    async def _install_pip(self) -> None:
        """Install pip packages."""
        if not self.pip:
            return

        if isinstance(self.pip, str):
            # Treat as requirements.txt path - upload it first
            if os.path.exists(self.pip):
                # Upload requirements.txt to sandbox, keep original filename
                original_filename = os.path.basename(self.pip)
                target_path = f"{self.workdir}/{original_filename}"
                await self._sandbox.upload_by_path(
                    source_path=os.path.abspath(self.pip),
                    target_path=target_path,
                )
                return await self.run(f"pip install -r {shlex.quote(target_path)}")
            else:
                raise FileNotFoundError(f"Requirements file not found: {self.pip}")
        else:
            # Treat as list of packages
            packages = " ".join([shlex.quote(pkg) for pkg in self.pip])
            return await self.run(f"pip install {packages}")
