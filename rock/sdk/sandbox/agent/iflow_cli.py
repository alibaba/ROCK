from __future__ import annotations  # Postpone annotation evaluation to avoid circular imports.

import json
import os
import re
import shlex
import tempfile
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from rock import env_vars
from rock.actions import Observation, UploadRequest
from rock.logger import init_logger
from rock.sdk.sandbox.agent.base import DefaultAgent
from rock.sdk.sandbox.agent.config import DefaultAgentConfig
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.utils import arun_with_retry

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox

logger = init_logger(__name__)


# Default IFlow settings
DEFAULT_IFLOW_SETTINGS: dict[str, Any] = {
    "selectedAuthType": "openai-compatible",
    "apiKey": "",
    "baseUrl": "",
    "modelName": "",
    "searchApiKey": "88888888",
    "disableAutoUpdate": True,
    "shellTimeout": 360000,
    "tokensLimit": 128000,
    "coreTools": [
        "Edit",
        "exit_plan_mode",
        "glob",
        "list_directory",
        "multi_edit",
        "plan",
        "read plan",
        "read_file",
        "read_many_files",
        "save_memory",
        "Search",
        "Shell",
        "task",
        "web_fetch",
        "web_search",
        "write_file",
        "xml_escape",
    ],
}


class IFlowCliConfig(DefaultAgentConfig):
    """IFlow CLI Agent Configuration.

    Inherits common agent configuration and adds IFlow-specific settings.
    """

    agent_type: str = "iflow-cli"

    agent_session: str = "iflow-cli-session"

    pre_startup_bash_cmd_list: list[str] = env_vars.ROCK_AGENT_PRE_STARTUP_BASH_CMD_LIST

    npm_install_cmd: str = env_vars.ROCK_AGENT_NPM_INSTALL_CMD

    npm_install_timeout: int = 300

    iflow_cli_install_cmd: str = env_vars.ROCK_AGENT_IFLOW_CLI_INSTALL_CMD

    iflow_settings: dict[str, Any] = DEFAULT_IFLOW_SETTINGS

    iflow_run_cmd: str = "iflow -r {session_id} -p {problem_statement} --yolo > {iflow_log_file} 2>&1"

    iflow_log_file: str = "~/.iflow/session_info.log"

    session_envs: dict[str, str] = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


class IFlowCli(DefaultAgent):
    """IFlow CLI Agent implementation.

    Manages the lifecycle of IFlow CLI including installation, configuration,
    and execution. Supports session resumption for continuing previous work.
    """

    def __init__(self, sandbox: Sandbox, config: IFlowCliConfig):
        """Initialize IFlow CLI agent.

        Args:
            sandbox: Sandbox instance for executing commands
            config: IFlowCliConfig instance with agent settings
        """
        super().__init__(sandbox, config)

        self.config: IFlowCliConfig = config

    async def _install(self):
        """Install IFlow CLI and configure the environment.

        Steps:
        1. Install npm with retry
        2. Configure npm registry
        3. Install iflow-cli with retry
        4. Create iflow configuration directories
        5. Generate and upload settings configuration file
        """
        sandbox_id = self._sandbox.sandbox_id
        start_time = time.time()

        logger.info(f"[{sandbox_id}] Starting IFlow CLI installation")

        try:
            # Step 1: Install npm
            await self._install_npm()

            # Step 2: Configure npm registry
            await self._configure_npm_registry()

            # Step 3: Install iflow-cli
            await self._install_iflow_cli_package()

            # Step 4: Create configuration directories
            await self._create_iflow_directories()

            # Step 5: Upload settings configuration
            await self._upload_iflow_settings()

            elapsed = time.time() - start_time
            logger.info(f"[{sandbox_id}] IFlow CLI installation completed (elapsed: {elapsed:.2f}s)")

        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(
                f"[{sandbox_id}] IFlow CLI installation failed - {str(e)} (elapsed: {elapsed:.2f}s)",
                exc_info=True,
            )
            raise

    async def _install_npm(self):
        """Install npm with Node.js binary."""
        sandbox_id = self._sandbox.sandbox_id
        step_start = time.time()

        self._log_step("Installing npm")

        logger.debug(f"[{sandbox_id}] NPM install command: {self.config.npm_install_cmd[:100]}...")

        await arun_with_retry(
            sandbox=self._sandbox,
            cmd=f"bash -c {shlex.quote(self.config.npm_install_cmd)}",
            session=self.agent_session,
            mode="nohup",
            wait_timeout=self.config.npm_install_timeout,
            error_msg="npm installation failed",
        )

        elapsed_step = time.time() - step_start
        self._log_step("NPM installation finished", step_name="NPM Install", is_complete=True, elapsed=elapsed_step)

    async def _configure_npm_registry(self):
        """Configure npm to use mirror registry for faster downloads."""
        sandbox_id = self._sandbox.sandbox_id
        step_start = time.time()

        self._log_step("Configuring npm registry")

        result = await self._sandbox.arun(
            cmd="npm config set registry https://registry.npmmirror.com",
            session=self.agent_session,
        )

        if result.exit_code != 0:
            logger.warning(f"[{sandbox_id}] Failed to set npm registry: {result.output}")
        else:
            logger.debug(f"[{sandbox_id}] Npm registry configured successfully")

        elapsed_step = time.time() - step_start
        self._log_step("NPM registry configured", step_name="NPM Registry", is_complete=True, elapsed=elapsed_step)

    async def _install_iflow_cli_package(self):
        """Install iflow-cli package globally."""
        sandbox_id = self._sandbox.sandbox_id
        step_start = time.time()

        self._log_step("Installing iflow-cli")

        logger.debug(f"[{sandbox_id}] IFlow CLI install command: {self.config.iflow_cli_install_cmd[:100]}...")

        await arun_with_retry(
            sandbox=self._sandbox,
            cmd=f"bash -c {shlex.quote(self.config.iflow_cli_install_cmd)}",
            session=self.agent_session,
            mode="nohup",
            wait_timeout=self.config.npm_install_timeout,
            error_msg="iflow-cli installation failed",
        )

        elapsed_step = time.time() - step_start
        self._log_step(
            "IFlow CLI installation finished", step_name="IFlow Install", is_complete=True, elapsed=elapsed_step
        )

    async def _create_iflow_directories(self):
        """Create iflow configuration directories."""
        sandbox_id = self._sandbox.sandbox_id
        step_start = time.time()

        self._log_step("Creating iflow settings directories")

        result = await self._sandbox.arun(
            cmd="mkdir -p /root/.iflow && mkdir -p ~/.iflow",
            session=self.agent_session,
        )

        if result.exit_code != 0:
            error_msg = f"Failed to create iflow directories: {result.output}"
            logger.error(f"[{sandbox_id}] {error_msg}")
            raise Exception(error_msg)

        logger.debug(f"[{sandbox_id}] IFlow settings directories created")

        elapsed_step = time.time() - step_start
        self._log_step(
            "IFlow configuration directories created",
            step_name="Create Directories",
            is_complete=True,
            elapsed=elapsed_step,
        )

    async def _upload_iflow_settings(self):
        """Generate and upload iflow-settings.json configuration file."""
        sandbox_id = self._sandbox.sandbox_id
        step_start = time.time()

        self._log_step("Generating and uploading iflow settings")

        with self._temp_iflow_settings_file() as temp_settings_path:
            await self._sandbox.upload(
                UploadRequest(
                    source_path=temp_settings_path,
                    target_path="/root/.iflow/settings.json",
                )
            )
            logger.debug(f"[{sandbox_id}] Settings uploaded to /root/.iflow/settings.json")

        elapsed_step = time.time() - step_start
        self._log_step(
            "IFlow settings configuration uploaded", step_name="Upload Settings", is_complete=True, elapsed=elapsed_step
        )

    @contextmanager
    def _temp_iflow_settings_file(self):
        """Context manager for creating temporary iflow settings file.

        Creates a temporary JSON file with the configured IFlow settings
        and ensures cleanup after use.

        Yields:
            str: Path to the temporary settings file
        """
        settings_content = json.dumps(self.config.iflow_settings, indent=2)

        with tempfile.NamedTemporaryFile(mode="w", suffix="_iflow_settings.json", delete=False) as temp_file:
            temp_file.write(settings_content)
            temp_settings_path = temp_file.name

        try:
            yield temp_settings_path
        finally:
            os.unlink(temp_settings_path)

    async def _get_session_id_from_sandbox(self) -> str:
        """Retrieve session ID from IFlow log file in sandbox.

        Fetches the last 1000 lines of the log file and extracts the session ID.
        Returns empty string if log file is empty, not found, or parsing fails.

        Returns:
            Session ID string if found, empty string otherwise
        """
        sandbox_id = self._sandbox.sandbox_id
        logger.info(f"[{sandbox_id}] Retrieving session ID from sandbox log file")

        try:
            log_file_path = self.config.iflow_log_file
            logger.debug(f"[{sandbox_id}] Reading log file: {log_file_path}")

            result = await self._sandbox.arun(
                cmd=f"tail -1000 {log_file_path} 2>/dev/null || echo ''",
                session=self.agent_session,
            )

            log_content = result.output.strip()

            if not log_content:
                logger.debug(f"[{sandbox_id}] Log file is empty or not found")
                return ""

            logger.debug(f"[{sandbox_id}] Retrieved log content ({len(log_content)} bytes)")
            session_id = self._extract_session_id_from_log(log_content)
            return session_id

        except Exception as e:
            logger.error(f"[{sandbox_id}] Error retrieving session ID: {str(e)}")
            return ""

    def _extract_session_id_from_log(self, log_content: str) -> str:
        """Extract session ID from IFlow log file content.

        Args:
            log_content: Content from the log file

        Returns:
            Session ID string if found, empty string otherwise
        """
        sandbox_id = self._sandbox.sandbox_id
        logger.debug(f"[{sandbox_id}] Attempting to extract session-id from log content")

        try:
            json_match = re.search(r"<Execution Info>\s*(.*?)\s*</Execution Info>", log_content, re.DOTALL)

            if not json_match:
                logger.debug(f"[{sandbox_id}] No <Execution Info> block found in log")
                return ""

            json_str = json_match.group(1).strip()
            data = json.loads(json_str)
            session_id = data.get("session-id", "")

            if session_id:
                logger.info(f"[{sandbox_id}] Successfully extracted session-id: {session_id}")
                return session_id
            else:
                logger.debug(f"[{sandbox_id}] session-id field not found in Execution Info")
                return ""

        except json.JSONDecodeError as e:
            logger.warning(f"[{sandbox_id}] Failed to parse JSON in Execution Info: {str(e)}")
            return ""
        except Exception as e:
            logger.warning(f"[{sandbox_id}] Error extracting session-id: {str(e)}")
            return ""

    async def run(
        self,
        problem_statement: str,
        project_path: str,
        agent_run_timeout: int = 1800,
        agent_run_check_interval: int = 30,
    ) -> Observation:
        """Run IFlow CLI to solve a specified problem.

        Automatically attempts to retrieve the previous session ID from the log file.
        If a session ID is found, it will be used to resume the previous execution.

        Args:
            problem_statement: Problem statement that IFlow CLI will attempt to solve
            project_path: Project path to work on
            agent_run_timeout: Agent execution timeout in seconds (default: 1800)
            agent_run_check_interval: Interval for checking progress in seconds (default: 30)

        Returns:
            Observation: Execution result with exit code and output
        """
        sandbox_id = self._sandbox.sandbox_id
        start_time = time.time()

        logger.info(f"[{sandbox_id}] Starting IFlow CLI run operation")
        logger.debug(f"[{sandbox_id}] Project path: {project_path}, Problem statement: {problem_statement[:100]}...")

        try:
            # Step 1: Change to project directory
            self._log_step(f"Changing to project directory: {project_path}", step_name="CD Project")
            result = await self._sandbox.arun(
                cmd=f"cd {project_path}",
                session=self.agent_session,
            )

            if result.exit_code != 0:
                logger.error(f"[{sandbox_id}] Failed to change directory to {project_path}: {result.output}")
                return result
            logger.debug(f"[{sandbox_id}] Successfully changed working directory")

            # Step 2: Retrieve session ID from previous execution
            logger.info(f"[{sandbox_id}] Attempting to retrieve session ID from previous execution")
            session_id = await self._get_session_id_from_sandbox()
            if session_id:
                logger.info(f"[{sandbox_id}] Using existing session ID: {session_id}")
            else:
                logger.info(f"[{sandbox_id}] No previous session found, will start fresh execution")

            # Step 3: Execute IFlow CLI command
            self._log_step(
                f"Running IFlow CLI with timeout {agent_run_timeout}s",
                step_name="IFlow Execution",
            )

            iflow_run_cmd = self.config.iflow_run_cmd.format(
                session_id=f'"{session_id}"',
                problem_statement=shlex.quote(problem_statement),
                iflow_log_file=self.config.iflow_log_file,
            )
            logger.debug(f"[{sandbox_id}] Formatted IFlow command: {iflow_run_cmd}")

            result = await self._agent_run(
                cmd=f"bash -c {shlex.quote(iflow_run_cmd)}",
                session=self.agent_session,
                wait_timeout=agent_run_timeout,
                wait_interval=agent_run_check_interval,
            )

            # Step 4: Log execution outcome
            log_file_path = self.config.iflow_log_file
            result_log = await self._sandbox.arun(
                cmd=f"tail -1000 {log_file_path} 2>/dev/null || echo ''",
                session=self.agent_session,
            )
            log_content = result_log.output

            elapsed_total = time.time() - start_time

            if result and result.exit_code == 0:
                logger.info(
                    f"[{sandbox_id}] ✓ IFlow-Cli completed successfully "
                    f"(exit_code: {result.exit_code}, elapsed: {elapsed_total:.2f}s)"
                )
                logger.debug(f"[{sandbox_id}] Output: {log_content}")
            else:
                error_msg = result.failure_reason if result else "No result returned"
                logger.error(f"[{sandbox_id}] ✗ IFlow-Cli failed - {error_msg} (elapsed: {elapsed_total:.2f}s)")
                logger.error(f"[{sandbox_id}] Output: {log_content}")

            return result

        except Exception as e:
            elapsed_total = time.time() - start_time
            logger.error(
                f"[{sandbox_id}] IFlow CLI execution failed - {str(e)} (elapsed: {elapsed_total:.2f}s)",
                exc_info=True,
            )
            raise
