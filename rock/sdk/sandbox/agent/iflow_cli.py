import shlex
from pathlib import Path
from typing import Literal

from rock import env_vars
from rock.actions import CreateBashSessionRequest, UploadRequest
from rock.actions.sandbox.base import AbstractSandbox
from rock.logger import init_logger
from rock.sdk.sandbox.agent.base import Agent
from rock.sdk.sandbox.agent.config import AgentConfig
from rock.sdk.sandbox.client import Sandbox
from rock.utils.retry import retry_async

logger = init_logger(__name__)


class IFlowCliConfig(AgentConfig):
    """IFlow CLI Agent Configuration Class

    Used to define and configure various parameters for the IFlow CLI sandbox agent, including session settings,
    installation scripts, timeout configurations, etc.
    """

    # Agent type identifier, fixed to "iflow-cli"
    agent_type: Literal["iflow-cli"] = "iflow-cli"

    # Bash session name for agent operations
    agent_session: str = "iflow-cli-session"

    # Commands to execute before agent initialization (e.g., bashrc setup, hosts config)
    pre_startup_bash_cmd_list: list[str] = env_vars.ROCK_AGENT_PRE_STARTUP_BASH_CMD_LIST

    # NPM installation command: download Node.js binary from OSS and extract to /opt/nodejs
    npm_install_cmd: str = env_vars.ROCK_AGENT_NPM_INSTALL_CMD

    # Create symbolic links for NPM related commands to system paths
    npm_ln_cmd: str = "ln -sf /opt/nodejs/bin/node /usr/local/bin/node && ln -sf /opt/nodejs/bin/npm /usr/local/bin/npm && ln -sf /opt/nodejs/bin/npx /usr/local/bin/npx && ln -sf /opt/nodejs/bin/corepack /usr/local/bin/corepack"

    # NPM installation command timeout (seconds)
    npm_install_timeout: int = 300

    # IFlow CLI installation command: download .tgz package from specified address and install globally
    iflow_cli_install_cmd: str = env_vars.ROCK_AGENT_IFLOW_CLI_INSTALL_CMD

    # Create symbolic link for IFlow CLI executable to system path
    iflow_cli_ln_cmd: str = "ln -s /opt/nodejs/bin/iflow /usr/local/bin/iflow"

    # Local path for IFlow configuration file
    iflow_settings_path: str

    # Command template for running IFlow CLI
    iflow_run_cmd: str = "iflow -p {problem_statement}"

    # Agent execution timeout (seconds), defaults to 30 minutes
    agent_run_timeout: int = 1800

    # Interval for checking progress during agent execution (seconds)
    agent_run_check_interval: int = 30


class IFlowCli(Agent):
    """IFlow CLI Agent Class

    Used to manage the lifecycle of the IFlow CLI, including initialization, installation, and execution phases.
    """

    def __init__(self, sandbox: AbstractSandbox, config: IFlowCliConfig):
        """Initialize IFlow CLI agent

        Args:
            sandbox: Sandbox instance used to execute commands and file operations
            config: Configuration object for IFlow CLI
        """
        super().__init__(sandbox)
        self.config = config

    async def init(self):
        """Initialize IFlow CLI agent

        This method sets up all the environment required for agent execution, including:
        1. Creating dedicated bash session
        2. Executing pre-startup configuration commands
        3. Installing NPM and Node.js
        4. Installing IFlow CLI tool
        5. Uploading configuration files
        """
        assert isinstance(self._sandbox, Sandbox), "Sandbox must be an instance of Sandbox class"

        sandbox_id = self._sandbox.sandbox_id

        logger.info(f"[{sandbox_id}] Starting IFlow CLI-agent initialization")

        # Step 1: Create dedicated bash session for agent operations
        logger.info(f"[{sandbox_id}] Creating bash session: {self.config.agent_session}")
        await self._sandbox.create_session(
            CreateBashSessionRequest(
                session=self.config.agent_session,
                env_enable=True,
            )
        )
        logger.debug(f"[{sandbox_id}] Bash session '{self.config.agent_session}' created successfully")

        # Step 2: Execute pre-startup configuration commands
        logger.info(f"[{sandbox_id}] Executing {len(self.config.pre_startup_bash_cmd_list)} pre-startup commands")
        for idx, cmd in enumerate(self.config.pre_startup_bash_cmd_list, 1):
            logger.debug(
                f"[{sandbox_id}] Executing pre-startup command {idx}/{len(self.config.pre_startup_bash_cmd_list)}: {cmd[:100]}..."
            )
            result = await self._sandbox.arun(
                cmd=cmd,
                session=self.config.agent_session,
            )
            if result.exit_code != 0:
                logger.warning(
                    f"[{sandbox_id}] Pre-startup command {idx} failed with exit code {result.exit_code}: {result.output[:200]}..."
                )
            else:
                logger.debug(f"[{sandbox_id}] Pre-startup command {idx} completed successfully")
        logger.info(f"[{sandbox_id}] Completed {len(self.config.pre_startup_bash_cmd_list)} pre-startup commands")

        # Step 3: Install npm with retry (added for clarity, was labeled as Step 4 originally)
        logger.info(f"[{sandbox_id}] Installing npm")
        logger.debug(f"[{sandbox_id}] NPM install command: {self.config.npm_install_cmd[:100]}...")

        await self._arun_with_retry(
            cmd=f"bash -c {shlex.quote(self.config.npm_install_cmd)}",
            session=self.config.agent_session,
            mode="nohup",
            wait_timeout=self.config.npm_install_timeout,
            error_msg="npm installation failed",
        )
        logger.info(f"[{sandbox_id}] npm archive downloaded and extracted")

        logger.info(f"[{sandbox_id}] Creating symbolic links for npm binaries")
        await self._sandbox.arun(
            cmd=self.config.npm_ln_cmd,
            session=self.config.agent_session,
        )
        logger.debug(f"[{sandbox_id}] npm symbolic links created successfully")

        logger.info(f"[{sandbox_id}] npm installation completed")

        # Configure npm to use mirror registry for faster downloads (originally described as for China)
        logger.info(f"[{sandbox_id}] Configuring npm registry")
        result = await self._sandbox.arun(
            cmd="npm config set registry https://registry.npmmirror.com",
            session=self.config.agent_session,
        )
        if result.exit_code != 0:
            logger.warning(f"[{sandbox_id}] Failed to set npm registry: {result.output}")
        else:
            logger.debug(f"[{sandbox_id}] Npm registry configured successfully")

        # Install iflow-cli with retry
        logger.info(f"[{sandbox_id}] Installing iflow-cli")
        logger.debug(f"[{sandbox_id}] IFlow CLI install command: {self.config.iflow_cli_install_cmd[:100]}...")

        await self._arun_with_retry(
            cmd=f"bash -c {shlex.quote(self.config.iflow_cli_install_cmd)}",
            session=self.config.agent_session,
            mode="nohup",
            wait_timeout=self.config.npm_install_timeout,
            error_msg="iflow-cli installation failed",
        )
        logger.info(f"[{sandbox_id}] iflow-cli installed from package")

        logger.info(f"[{sandbox_id}] Creating symbolic link for iflow binary")
        await self._sandbox.arun(
            cmd=self.config.iflow_cli_ln_cmd,
            session=self.config.agent_session,
        )
        logger.debug(f"[{sandbox_id}] iflow symbolic link created successfully")

        logger.info(f"[{sandbox_id}] iflow-cli installation completed successfully")

        # Create iflow config directories: mkdir /root/.iflow, ~/.iflow
        logger.info(f"[{sandbox_id}] Creating iflow settings directories")
        result = await self._sandbox.arun(
            cmd="mkdir -p /root/.iflow && mkdir -p ~/.iflow",
            session=self.config.agent_session,
        )
        if result.exit_code != 0:
            logger.error(f"[{sandbox_id}] Failed to create iflow directories: {result.output}")
            raise Exception(f"Failed to create iflow directories: {result.output}")
        logger.debug(f"[{sandbox_id}] IFlow settings directories created")

        # Upload iflow-settings.json configuration file
        logger.info(f"[{sandbox_id}] Uploading iflow settings from {self.config.iflow_settings_path}")

        # Upload to user's home directory
        await self._sandbox.upload(
            UploadRequest(
                source_path=self.config.iflow_settings_path,
                target_path="~/.iflow/settings.json",
            )
        )
        logger.debug(f"[{sandbox_id}] Settings uploaded to ~/.iflow/settings.json")

        # Upload to root's home directory
        await self._sandbox.upload(
            UploadRequest(
                source_path=self.config.iflow_settings_path,
                target_path="/root/.iflow/settings.json",
            )
        )
        logger.debug(f"[{sandbox_id}] Settings uploaded to /root/.iflow/settings.json")

        logger.info(f"[{sandbox_id}] IFlow settings configuration completed successfully")

    @retry_async(max_attempts=3, delay_seconds=5.0, backoff=2.0)
    async def _arun_with_retry(
        self,
        cmd: str,
        session: str,
        mode: str = "nohup",
        wait_timeout: int = 300,
        wait_interval: int = 10,
        error_msg: str = "Command failed",
    ):
        """
        Execute command with retry logic

        This method executes a command, and automatically retries up to 3 times when the command fails
        (non-zero exit code). Implements exponential backoff strategy, with delay between retries
        that increases progressively.

        Args:
            cmd: Command to be executed
            session: Session name where the command will be executed
            mode: Execution mode (normal, nohup, etc.)
            wait_timeout: Timeout for command execution (in seconds)
            wait_interval: Check interval for nohup commands
            error_msg: Error message to use when exception occurs

        Returns:
            Command result object upon successful execution

        Raises:
            Exception: Raises exception when command execution fails (non-zero exit code) to trigger retry
        """
        sandbox_id = self._sandbox.sandbox_id
        logger.debug(f"[{sandbox_id}] Executing command with retry: {cmd[:100]}...")
        logger.debug(
            f"[{sandbox_id}] Command execution parameters: mode={mode}, timeout={wait_timeout}, interval={wait_interval}"
        )

        result = await self._sandbox.arun(
            cmd=cmd, session=session, mode=mode, wait_timeout=wait_timeout, wait_interval=wait_interval
        )

        logger.debug(f"[{sandbox_id}] Command execution result: exit_code={result.exit_code}")

        # If exit_code is not 0, raise an exception to trigger retry
        if result.exit_code != 0:
            logger.warning(f"[{sandbox_id}] Command attempt failed: {error_msg}, exit code: {result.exit_code}")
            logger.debug(f"[{sandbox_id}] Command output: {result.output[:500]}...")
            raise Exception(f"{error_msg} with exit code: {result.exit_code}, output: {result.output}")

        logger.debug(f"[{sandbox_id}] Command executed successfully with retry mechanism")
        return result

    async def run(self, project_path: str | Path, problem_statement: str):
        """Run IFlow CLI to solve a specified problem

        This method switches to the specified project directory and executes the IFlow CLI command
        to handle the problem statement.

        Args:
            project_path: Project path, can be a string or Path object
            problem_statement: Problem statement that IFlow CLI will attempt to solve

        Returns:
            Object containing command execution results, including exit code and output
        """
        assert isinstance(self._sandbox, Sandbox), "Sandbox must be an instance of Sandbox class"

        sandbox_id = self._sandbox.sandbox_id
        logger.info(f"[{sandbox_id}] Starting IFlow CLI run operation")
        logger.debug(f"[{sandbox_id}] Project path: {project_path}, Problem statement: {problem_statement[:100]}...")

        # Change directory to the project path
        if isinstance(project_path, Path):
            logger.debug(f"[{sandbox_id}] Converting Path object to string: {project_path}")
            project_path = str(project_path)

        logger.info(f"[{sandbox_id}] Changing working directory to: {project_path}")
        result = await self._sandbox.arun(
            cmd=f"cd {project_path}",
            session=self.config.agent_session,
        )

        if result.exit_code != 0:
            logger.error(f"[{sandbox_id}] Failed to change directory to {project_path}: {result.output}")
            return result
        logger.debug(f"[{sandbox_id}] Successfully changed working directory")

        # Prepare and execute IFlow CLI command
        logger.info(
            f"[{sandbox_id}] Preparing to run IFlow CLI with timeout {self.config.agent_run_timeout}s and check interval {self.config.agent_run_check_interval}s"
        )
        iflow_run_cmd = self.config.iflow_run_cmd.format(problem_statement=shlex.quote(problem_statement))
        logger.debug(f"[{sandbox_id}] IFlow run command template: {self.config.iflow_run_cmd}")
        logger.debug(f"[{sandbox_id}] Formatted IFlow command: {iflow_run_cmd}")

        # Wrap in `bash -c` and quote the entire command to prevent shell parsing issues
        safe_iflow_run_cmd = f"bash -c {shlex.quote(iflow_run_cmd)}"
        logger.info(f"[{sandbox_id}] Executing IFlow CLI command with safety wrapping")

        result = await self._sandbox.arun(
            cmd=safe_iflow_run_cmd,
            session=self.config.agent_session,
            mode="nohup",
            wait_timeout=self.config.agent_run_timeout,
            wait_interval=self.config.agent_run_check_interval,
        )

        # Log execution outcome with detailed information
        logger.info(f"[{sandbox_id}] IFlow CLI execution completed")
        if result.exit_code == 0:
            logger.info(f"[{sandbox_id}] ✓ IFlow-Cli completed successfully (exit_code: {result.exit_code})")
            logger.debug(f"[{sandbox_id}] Command output (first 500 chars): {result.output[:500]}...")
        else:
            logger.error(f"[{sandbox_id}] ✗ IFlow-Cli failed with exit_code: {result.exit_code}")
            logger.error(f"[{sandbox_id}] Error output: {result.output}")

        logger.info(f"[{sandbox_id}] IFlow CLI run operation finished")
        return result
