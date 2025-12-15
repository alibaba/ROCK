import os
import shlex
from pathlib import Path
from typing import Literal

from rock import env_vars
from rock.actions.sandbox.base import AbstractSandbox
from rock.actions.sandbox.request import CreateBashSessionRequest, UploadRequest
from rock.logger import init_logger
from rock.sdk.sandbox.agent.base import Agent
from rock.sdk.sandbox.agent.config import AgentConfig
from rock.sdk.sandbox.client import Sandbox

logger = init_logger(__name__)


class SweAgentConfig(AgentConfig):
    """
    Configuration class for SWE-agent.

    Defines the setup parameters including session name, bash commands,
    working directory, and installation commands for the SWE-agent environment.
    """

    agent_type: Literal["swe-agent"] = "swe-agent"
    swe_session: str = "swe-agent-session"

    # Bash commands to execute before agent startup
    pre_startup_bash_cmd_list: list[str] = env_vars.ROCK_AGENT_PRE_STARTUP_BASH_CMD_LIST

    # Bash commands to execute after agent startup
    post_startup_bash_cmd_list: list[str] = []

    # Working directory for SWE-agent installation and execution
    swe_agent_workdir: str = "/tmp_sweagent"

    # Command to download and extract Python installation
    python_install_cmd: str = env_vars.ROCK_AGENT_PYTHON_INSTALL_CMD

    # Command to clone and install SWE-agent repository
    swe_agent_install_cmd: str = "git clone https://github.com/SWE-agent/SWE-agent.git && cd SWE-agent && {pip_path} install -e . -i https://mirrors.aliyun.com/pypi/simple/"

    # Maximum time (in seconds) to wait for agent execution
    agent_run_timeout: int = 1800

    # Interval (in seconds) between status checks during agent execution
    agent_run_check_interval: int = 30


class SweAgent(Agent):
    """
    SWE-agent implementation for automated software engineering tasks.

    Currently supports LocalDeployment and RunSingleConfig modes only.
    Manages the complete lifecycle of SWE-agent including initialization,
    environment setup, and execution.
    """

    def __init__(self, sandbox: AbstractSandbox, config: SweAgentConfig):
        """
        Initialize SWE-agent with sandbox and configuration.

        Args:
            sandbox: Sandbox environment for agent execution
            config: Configuration parameters for SWE-agent
        """
        super().__init__(sandbox)
        self.config = config
        self.swe_session = self.config.swe_session

        assert isinstance(self._sandbox, Sandbox), "Sandbox must be an instance of Sandbox class"
        logger.info(f" SWE-agent initialized with session: {self.swe_session}")

    async def init(self):
        """
        Initialize the SWE-agent environment.

        Performs the following steps:
        1. Creates a bash session for agent execution
        2. Executes pre-startup commands
        3. Creates working directory
        4. Installs Python
        5. Installs SWE-agent from repository
        """
        sandbox_id = self._sandbox.sandbox_id

        logger.info(f"[{sandbox_id}] Starting SWE-agent initialization")

        # Create dedicated bash session for SWE-agent
        logger.info(f" Creating bash session: {self.swe_session}")
        await self._sandbox.create_session(
            CreateBashSessionRequest(
                session=self.swe_session,
                env_enable=True,
            )
        )

        # Execute pre-startup commands (e.g., bashrc configuration, hosts setup)
        logger.info(f"[{sandbox_id}] Executing {len(self.config.pre_startup_bash_cmd_list)} pre-startup commands")
        for idx, cmd in enumerate(self.config.pre_startup_bash_cmd_list, 1):
            logger.debug(f" Pre-startup command {idx}/{len(self.config.pre_startup_bash_cmd_list)}: {cmd[:50]}...")
            await self._sandbox.arun(
                cmd=cmd,
                session=self.swe_session,
            )

        # Create working directory for SWE-agent
        logger.info(f"[{sandbox_id}] Creating working directory: {self.config.swe_agent_workdir}")
        await self._sandbox.arun(
            cmd=f"mkdir -p {self.config.swe_agent_workdir}",
            session=self.swe_session,
        )

        # Install Python environment
        logger.info(f"[{sandbox_id}]s Installing Python environment")
        python_install_cmd = f"cd {self.config.swe_agent_workdir} && {self.config.python_install_cmd}"
        await self._sandbox.arun(
            cmd=f"bash -c {shlex.quote(python_install_cmd)}",
            session=self.swe_session,
            mode="nohup",
            wait_timeout=300,
        )
        logger.info(f"[{sandbox_id}] Python installation completed")

        # Install SWE-agent from GitHub repository
        logger.info(f"[{sandbox_id}] Installing SWE-agent from repository")
        swe_agent_install_cmd = f"cd {self.config.swe_agent_workdir} && {self.config.swe_agent_install_cmd.format(pip_path=f'{self.config.swe_agent_workdir}/python/bin/pip')}"
        await self._sandbox.arun(
            cmd=f"bash -c {shlex.quote(swe_agent_install_cmd)}",
            session=self.swe_session,
            mode="nohup",
            wait_timeout=600,
        )
        logger.info(f"[{sandbox_id}] SWE-agent installation completed successfully")

    async def run(self, swe_agent_config_path: str | Path):
        """
        Execute SWE-agent with the provided configuration file.

        Args:
            swe_agent_config_path: Path to the SWE-agent configuration file
                                   (local path that will be uploaded to sandbox)

        Steps:
        1. Uploads configuration file to sandbox
        2. Executes SWE-agent with the configuration
        3. Waits for completion with configured timeout and interval
        """
        assert isinstance(self._sandbox, Sandbox), "Sandbox must be an instance of Sandbox class"

        logger.info(f" Starting SWE-agent execution with config: {swe_agent_config_path}")

        config_filename = Path(swe_agent_config_path).name

        # Upload configuration file to sandbox
        logger.info(f" Uploading configuration file: {config_filename}")
        await self._sandbox.upload(
            UploadRequest(
                source_path=os.path.abspath(swe_agent_config_path),
                target_path=f"{self.config.swe_agent_workdir}/{config_filename}",
            )
        )
        logger.debug(f" Configuration file uploaded to: {self.config.swe_agent_workdir}/{config_filename}")

        # Construct and execute SWE-agent run command
        swe_agent_run_cmd = f"cd {self.config.swe_agent_workdir} && {self.config.swe_agent_workdir}/python/bin/sweagent run --config {config_filename}"
        logger.info(
            f" Executing SWE-agent (timeout: {self.config.agent_run_timeout}s, check interval: {self.config.agent_run_check_interval}s)"
        )

        result = await self._sandbox.arun(
            cmd=f"bash -c {shlex.quote(swe_agent_run_cmd)}",
            session=self.swe_session,
            mode="nohup",
            wait_timeout=self.config.agent_run_timeout,
            wait_interval=self.config.agent_run_check_interval,
        )

        # Log execution result
        if result.exit_code == 0:
            logger.info(f" SWE-agent completed successfully (exit_code: {result.exit_code})")
        else:
            logger.error(f" SWE-agent failed with exit_code: {result.exit_code}")

        return result
