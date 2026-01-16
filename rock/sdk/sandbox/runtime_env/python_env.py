from __future__ import annotations

import os
import shlex
import uuid
from typing import TYPE_CHECKING, Literal

from rock import env_vars
from rock.logger import init_logger
from rock.sdk.sandbox.runtime_env.base import RuntimeEnv
from rock.sdk.sandbox.utils import arun_with_retry

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox

logger = init_logger(__name__)


class PythonRuntimeEnv(RuntimeEnv):
    """Python runtime env.

    Contract:
    - If workdir is None, it will be auto-generated as /rock_runtime_env_python_{version}
    - workdir/python is the runtime root directory.
    - workdir/python/bin contains python/pip and any console_scripts (e.g. sweagent).
    - Supports pip packages installation via pip parameter.
    - Supports add_to_sys_path to link bin to /usr/local/bin.
    """

    def __init__(
        self,
        sandbox: Sandbox,
        workdir: str | None = None,
        session: str | None = None,
        version: Literal["3.11", "3.12"] = "3.11",
        pip: list[str] | str | None = None,
        add_to_sys_path: bool = False,
        init_timeout: int = 300,
        session_envs: dict[str, str] | None = None,
    ) -> None:
        # Auto-generate workdir if not provided
        if workdir is None:
            workdir = f"/rock_runtime_env_python_{version}"

        # Auto-generate session if not provided
        if session is None:
            session = f"python-runtime-env-{uuid.uuid4().hex}"

        super().__init__(
            sandbox=sandbox,
            workdir=workdir,
            session=session,
            version=version,
            add_to_sys_path=add_to_sys_path,
            init_timeout=init_timeout,
            session_envs=session_envs,
        )
        self.pip = pip

        # Get install command based on version
        if version == "3.11":
            self.python_install_cmd = env_vars.ROCK_AGENT_PYTHON_INSTALL_CMD
        elif version == "3.12":
            self.python_install_cmd = env_vars.ROCK_AGENT_PYTHON_v12_INSTALL_CMD
        else:
            raise ValueError(f"Unsupported Python version: {version}. Only 3.11 and 3.12 are supported.")

    @property
    def bin_dir(self) -> str:
        return f"{self.workdir}/python/bin"

    def wrap(self, cmd: str) -> str:
        wrapped = f"export PATH={shlex.quote(self.bin_dir)}:$PATH && {cmd}"
        return f"bash -c {shlex.quote(wrapped)}"

    async def init(self) -> None:
        """Initialize the Python runtime environment."""
        await self.ensure_session()

        sandbox_id = self._sandbox.sandbox_id
        logger.info(f"[{sandbox_id}] Initializing Python {self.version} runtime env in {self.workdir}")

        # 1) ensure workdir exists
        await self._sandbox.arun(
            cmd=f"mkdir -p {shlex.quote(self.workdir)}",
            session=self.session,
        )

        from rock.sdk.sandbox.client import RunMode

        # 2) run install cmd in workdir (must create ./python)
        install_cmd = f"cd {shlex.quote(self.workdir)} && {self.python_install_cmd}"
        await arun_with_retry(
            sandbox=self._sandbox,
            cmd=f"bash -c {shlex.quote(install_cmd)}",
            session=self.session,
            mode=RunMode.NOHUP,
            wait_timeout=self.init_timeout,
            error_msg="Python runtime installation failed",
        )

        # 3) validate python exists
        check_cmd = f"test -x {shlex.quote(self.bin_dir)}/python"
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

        # 4) install pip packages if specified
        if self.pip:
            await self._install_pip_packages()

        # 5) add to sys path if specified
        if self.add_to_sys_path:
            await self._add_to_sys_path()

        self._prepared = True
        logger.info(f"[{sandbox_id}] Python {self.version} runtime env initialized")

    async def _install_pip_packages(self) -> None:
        """Install pip packages specified in pip parameter."""
        sandbox_id = self._sandbox.sandbox_id
        logger.info(f"[{sandbox_id}] Installing pip packages")

        if isinstance(self.pip, str):
            # Treat as requirements.txt path - upload it first
            if os.path.exists(self.pip):
                # Upload requirements.txt to sandbox, keep original filename
                original_filename = os.path.basename(self.pip)
                target_path = f"{self.workdir}/{original_filename}"
                await self._sandbox.upload(
                    source_path=os.path.abspath(self.pip),
                    target_path=target_path,
                )
                pip_cmd = f"{self.bin_dir}/pip install -r {shlex.quote(target_path)} -i {env_vars.ROCK_PIP_INDEX_URL}"
            else:
                raise FileNotFoundError(f"Requirements file not found: {self.pip}")
        else:
            # Treat as list of packages
            packages = " ".join([shlex.quote(pkg) for pkg in self.pip])
            pip_cmd = f"{self.bin_dir}/pip install {packages} -i {env_vars.ROCK_PIP_INDEX_URL}"

        from rock.sdk.sandbox.client import RunMode

        await arun_with_retry(
            sandbox=self._sandbox,
            cmd=f"bash -c {shlex.quote(pip_cmd)}",
            session=self.session,
            mode=RunMode.NOHUP,
            wait_timeout=self.init_timeout,
            error_msg="Pip packages installation failed",
        )

        logger.info(f"[{sandbox_id}] Pip packages installed successfully")

    async def _add_to_sys_path(self) -> None:
        """Link python bin directory to /usr/local/bin for system-wide access."""
        sandbox_id = self._sandbox.sandbox_id
        logger.info(f"[{sandbox_id}] Adding Python bin to system path")

        # Create symlinks for common executables
        executables = ["python", "pip", "python3", "pip3"]
        for exe in executables:
            src = f"{self.bin_dir}/{exe}"
            dst = f"/usr/local/bin/{exe}"
            cmd = f"ln -sf {shlex.quote(src)} {shlex.quote(dst)}"
            result = await self._sandbox.arun(
                cmd=cmd,
                session=self.session,
            )
            if result.exit_code != 0:
                logger.warning(f"[{sandbox_id}] Failed to create symlink for {exe}: {result.output}")

        logger.info(f"[{sandbox_id}] Python bin added to system path")
