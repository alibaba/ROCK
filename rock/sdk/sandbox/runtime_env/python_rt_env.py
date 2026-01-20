from __future__ import annotations

import os
import shlex
from typing import TYPE_CHECKING

from rock import env_vars
from rock.logger import init_logger
from rock.sdk.sandbox.runtime_env.base import RuntimeEnv
from rock.sdk.sandbox.runtime_env.config import PythonRuntimeEnvConfig
from rock.sdk.sandbox.utils import arun_with_retry

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

    Each PythonRuntimeEnv is identified by (type, version) and is managed by Sandbox.rt_envs.
    workdir is auto-generated as: /rock-rt-envs/python/{version}/

    Usage:
        env = PythonRuntimeEnv(sandbox, version="3.11", pip=["langchain"])
        await env.init()  # Installs Python and pip packages
        await env.run("python --version")
    """

    rt_env_type: str = "python"

    def __init__(
        self,
        sandbox: Sandbox,
        rt_env_config: PythonRuntimeEnvConfig,
    ) -> None:
        # Create base config with resolved version (extra="ignore" handles 'pip' and 'pip_index_url' fields)
        super().__init__(sandbox=sandbox, rt_env_config=rt_env_config)

        self.pip = rt_env_config.pip
        self.pip_index_url = rt_env_config.pip_index_url

        # Get install command based on version
        version = rt_env_config.version
        if version not in _PYTHON_VERSION_MAP:
            supported = list(_PYTHON_VERSION_MAP.keys())
            raise ValueError(f"Unsupported Python version: {version}. Supported versions: {supported}")
        self.python_install_cmd = _PYTHON_VERSION_MAP[version]

    async def _do_init(self) -> None:
        """Initialize and install Python runtime environment.

        This method:
        1. Creates workdir
        2. Installs Python runtime
        3. Validates Python exists
        4. Configures pip index URL (if specified)
        5. Installs pip packages (if specified)
        6. Adds to sys path (if specified)
        """
        await self.ensure_session()

        sandbox_id = self._sandbox.sandbox_id
        logger.info(f"[{sandbox_id}] Initializing Python {self.version} in {self.workdir}")

        from rock.sdk.sandbox.client import RunMode

        # 1) ensure workdir exists
        result = await self._sandbox.arun(
            cmd=f"mkdir -p {self.workdir}",
            session=self.session,
        )
        if result.exit_code != 0:
            raise RuntimeError(f"Failed to create workdir: {self.workdir}, exit_code: {result.exit_code}")

        # 2) install Python runtime
        install_cmd = f"cd {shlex.quote(self.workdir)} && {self.python_install_cmd}"
        await arun_with_retry(
            sandbox=self._sandbox,
            cmd=f"bash -c {shlex.quote(install_cmd)}",
            session=self.session,
            mode=RunMode.NOHUP,
            wait_timeout=self.install_timeout,
            error_msg="Python runtime installation failed",
        )
        logger.info(f"[{sandbox_id}] Python {self.version} installed")

        # 3) validate python exists
        check_cmd = f"test -x {self.bin_dir}/python"
        result = await self._sandbox.arun(
            cmd=check_cmd,
            session=self.session,
        )
        if result.exit_code != 0:
            raise RuntimeError(
                "PythonRuntimeEnv.init() failed: "
                f"{self.bin_dir}/python not found or not executable. "
                "Ensure python_install_cmd installs into ./python under workdir."
            )

        # 4) configure pip index url if specified
        if self.pip_index_url:
            logger.info(f"[{sandbox_id}] Configuring pip index URL: {self.pip_index_url}")
            result = await self._sandbox.arun(
                cmd=f"{self.bin_dir}/pip config set global.index-url {shlex.quote(self.pip_index_url)}",
                session=self.session,
            )
            if result.exit_code != 0:
                raise RuntimeError(
                    f"Failed to configure pip index URL: {self.pip_index_url}, exit_code: {result.exit_code}"
                )

        # 5) install pip packages if specified
        if self.pip:
            await self._install_pip()

        # 6) add to sys path if specified
        if self.add_to_sys_path:
            await self._add_to_sys_path(executables=["python", "pip", "python3", "pip3"])

        logger.info(f"[{sandbox_id}] Python {self.version} runtime env initialized")

    async def _install_pip(self) -> None:
        """Install pip packages."""
        if not self.pip:
            return

        sandbox_id = self._sandbox.sandbox_id
        logger.info(f"[{sandbox_id}] Installing pip packages")

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
                pip_cmd = f"{self.bin_dir}/pip install -r {shlex.quote(target_path)}"
            else:
                raise FileNotFoundError(f"Requirements file not found: {self.pip}")
        else:
            # Treat as list of packages
            packages = " ".join([shlex.quote(pkg) for pkg in self.pip])
            pip_cmd = f"{self.bin_dir}/pip install {packages}"

        from rock.sdk.sandbox.client import RunMode

        await arun_with_retry(
            sandbox=self._sandbox,
            cmd=f"bash -c {shlex.quote(pip_cmd)}",
            session=self.session,
            mode=RunMode.NOHUP,
            wait_timeout=self.install_timeout,
            error_msg="Pip packages installation failed",
        )

        logger.info(f"[{sandbox_id}] Pip packages installed successfully")
