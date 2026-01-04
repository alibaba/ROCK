from __future__ import annotations  # Postpone annotation evaluation to avoid circular imports.

import asyncio
import json
import os
import re
import shlex
import tempfile
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from httpx import ReadTimeout

from rock import env_vars
from rock.actions import CreateBashSessionRequest, Observation, UploadRequest
from rock.logger import init_logger
from rock.sdk.sandbox.agent.base import Agent
from rock.sdk.sandbox.agent.config import AgentConfig
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.model_service.base import ModelService, ModelServiceConfig
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


class IFlowCliConfig(AgentConfig):
    """IFlow CLI Agent Configuration Class.

    Used to define and configure various parameters for the IFlow CLI sandbox agent,
    including session settings, installation scripts, timeout configurations, etc.

    Attributes:
        agent_type: Agent type identifier, fixed to "iflow-cli".
        agent_session: Bash session name for agent operations.
        pre_startup_bash_cmd_list: Commands to execute before agent initialization
            (e.g., bashrc setup, hosts config).
        npm_install_cmd: NPM installation command that downloads Node.js binary from
            OSS and extracts to /opt/nodejs.
        npm_install_timeout: NPM installation command timeout in seconds.
        iflow_cli_install_cmd: IFlow CLI installation command that downloads .tgz
            package and installs globally.
        iflow_settings: Default IFlow configuration settings dict.
        iflow_run_cmd: Command template for running IFlow CLI. Supports {session_id}
            and {problem_statement} placeholders. When session_id is empty string,
            it generates: iflow -r "" -p {problem_statement}
        iflow_log_file: IFlow log file path in sandbox.
        session_envs: Environment variables for sessions
        model_service_config: Optional ModelService configuration for LLM integration
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

    model_service_config: ModelServiceConfig | None = None


class IFlowCli(Agent):
    """IFlow CLI Agent Class.

    Manages the lifecycle of the IFlow CLI, including initialization, installation,
    and execution phases. Supports session resumption for continuing previous work.
    Optionally integrates with ModelService for LLM handling if model_service_config is provided.
    """

    def __init__(self, sandbox: Sandbox, config: IFlowCliConfig):
        """Initialize IFlow CLI agent.

        Args:
            sandbox: Sandbox instance used to execute commands and file operations.
            config: Configuration object for IFlow CLI.
        """
        super().__init__(sandbox)
        self._sandbox = sandbox
        self.config = config

        # ModelService instance (created during init if configured)
        self.model_service: ModelService | None = None

    @contextmanager
    def _temp_iflow_settings_file(self):
        """Context manager for creating temporary iflow settings file.

        Creates a temporary JSON file with the configured IFlow settings
        and ensures cleanup after use.

        Yields:
            str: Path to the temporary settings file
        """
        # Create the settings json content using config settings
        settings_content = json.dumps(self.config.iflow_settings, indent=2)

        # Create a temporary file to hold the settings
        with tempfile.NamedTemporaryFile(mode="w", suffix="_iflow_settings.json", delete=False) as temp_file:
            temp_file.write(settings_content)
            temp_settings_path = temp_file.name

        try:
            yield temp_settings_path
        finally:
            # Clean up the temporary file
            os.unlink(temp_settings_path)

    async def init(self):
        """Initialize IFlow CLI agent.

        Sets up all the environment required for agent execution, including:
        1. Creating dedicated bash session and executing pre-startup commands
        2. Installing NPM and Node.js
        3. Installing IFlow CLI tool
        4. Generating and uploading configuration files from default config dict
        5. Initializing ModelService if configured (parallel with iflow CLI setup steps)

        Raises:
            Exception: If any critical initialization step fails (e.g., creating
                directories, generating and uploading config files).
        """

        sandbox_id = self._sandbox.sandbox_id
        start_time = time.time()

        logger.info(f"[{sandbox_id}] Starting IFlow CLI-agent initialization")

        # Prepare tasks to run in parallel
        tasks = [self._install_iflow_cli()]

        # Initialize ModelService if configured
        if self.config.model_service_config:
            tasks.append(self._init_model_service())

        # Run tasks in parallel
        await asyncio.gather(*tasks)

        elapsed = time.time() - start_time
        logger.info(f"[{sandbox_id}] IFlow CLI-agent initialization completed (elapsed: {elapsed:.2f}s)")

    async def _install_iflow_cli(self):
        """Install iflow-cli and configure the environment."""
        sandbox_id = self._sandbox.sandbox_id
        start_time = time.time()

        logger.info(f"[{sandbox_id}] Step 1 started: IFlow CLI installation")

        try:
            # Step 1: Create dedicated bash session for agent operations
            step_start = time.time()
            logger.info(f"[{sandbox_id}] Creating bash session: {self.config.agent_session}")
            await self._sandbox.create_session(
                CreateBashSessionRequest(
                    session=self.config.agent_session,
                    env_enable=True,
                    env=self.config.session_envs,
                )
            )
            logger.debug(f"[{sandbox_id}] Bash session '{self.config.agent_session}' created successfully")
            elapsed_step = time.time() - step_start
            logger.info(f"[{sandbox_id}] Step 1 completed: Bash session created (elapsed: {elapsed_step:.2f}s)")

            # Step 2: Execute pre-startup configuration commands
            step_start = time.time()
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
            elapsed_step = time.time() - step_start
            logger.info(
                f"[{sandbox_id}] Step 2 completed: Pre-startup commands executed (elapsed: {elapsed_step:.2f}s)"
            )

            # Step 3: Install npm with retry
            step_start = time.time()
            logger.info(f"[{sandbox_id}] Installing npm")
            logger.debug(f"[{sandbox_id}] NPM install command: {self.config.npm_install_cmd[:100]}...")

            await arun_with_retry(
                sandbox=self._sandbox,
                cmd=f"bash -c {shlex.quote(self.config.npm_install_cmd)}",
                session=self.config.agent_session,
                mode="nohup",
                wait_timeout=self.config.npm_install_timeout,
                error_msg="npm installation failed",
            )
            elapsed_step = time.time() - step_start
            logger.info(f"[{sandbox_id}] Step 3 completed: NPM installation finished (elapsed: {elapsed_step:.2f}s)")

            # Step 4: Configure npm to use mirror registry for faster downloads
            step_start = time.time()
            logger.info(f"[{sandbox_id}] Configuring npm registry")
            result = await self._sandbox.arun(
                cmd="npm config set registry https://registry.npmmirror.com",
                session=self.config.agent_session,
            )
            if result.exit_code != 0:
                logger.warning(f"[{sandbox_id}] Failed to set npm registry: {result.output}")
            else:
                logger.debug(f"[{sandbox_id}] Npm registry configured successfully")
            elapsed_step = time.time() - step_start
            logger.info(f"[{sandbox_id}] Step 4 completed: NPM registry configured (elapsed: {elapsed_step:.2f}s)")

            # Step 5: Install iflow-cli with retry
            step_start = time.time()
            logger.info(f"[{sandbox_id}] Installing iflow-cli")
            logger.debug(f"[{sandbox_id}] IFlow CLI install command: {self.config.iflow_cli_install_cmd[:100]}...")

            await arun_with_retry(
                sandbox=self._sandbox,
                cmd=f"bash -c {shlex.quote(self.config.iflow_cli_install_cmd)}",
                session=self.config.agent_session,
                mode="nohup",
                wait_timeout=self.config.npm_install_timeout,
                error_msg="iflow-cli installation failed",
            )
            elapsed_step = time.time() - step_start
            logger.info(
                f"[{sandbox_id}] Step 5 completed: IFlow CLI installation finished (elapsed: {elapsed_step:.2f}s)"
            )

            # Step 6: Create iflow config directories
            step_start = time.time()
            logger.info(f"[{sandbox_id}] Creating iflow settings directories")
            result = await self._sandbox.arun(
                cmd="mkdir -p /root/.iflow && mkdir -p ~/.iflow",
                session=self.config.agent_session,
            )
            if result.exit_code != 0:
                logger.error(f"[{sandbox_id}] Failed to create iflow directories: {result.output}")
                raise Exception(f"Failed to create iflow directories: {result.output}")
            logger.debug(f"[{sandbox_id}] IFlow settings directories created")
            elapsed_step = time.time() - step_start
            logger.info(
                f"[{sandbox_id}] Step 6 completed: IFlow configuration directories created (elapsed: {elapsed_step:.2f}s)"
            )

            # Step 7: Generate and upload iflow-settings.json configuration file from config dict
            step_start = time.time()
            logger.info(f"[{sandbox_id}] Generating and uploading iflow settings from config dict")

            # Use context manager to create and clean up temporary settings file
            with self._temp_iflow_settings_file() as temp_settings_path:
                await self._sandbox.upload(
                    UploadRequest(
                        source_path=temp_settings_path,
                        target_path="/root/.iflow/settings.json",
                    )
                )
                logger.debug(f"[{sandbox_id}] Settings uploaded to /root/.iflow/settings.json")
            elapsed_step = time.time() - step_start
            logger.info(f"[{sandbox_id}] Step 7 completed: IFlow settings configuration (elapsed: {elapsed_step:.2f}s)")

        except Exception as e:
            elapsed_total = time.time() - start_time
            logger.error(
                f"[{sandbox_id}] Operation failed: IFlow CLI installation failed - {str(e)} "
                f"(elapsed: {elapsed_total:.2f}s)",
                exc_info=True,
            )
            raise

    async def _init_model_service(self):
        """Initialize ModelService (install only, not start).

        Creates a ModelService instance and executes the installation steps.
        The service will be started later in run() method if needed.

        Raises:
            Exception: If ModelService initialization fails
        """
        sandbox_id = self._sandbox.sandbox_id

        try:
            logger.info(f"[{sandbox_id}] Initializing ModelService")

            # Create ModelService instance with specified configuration
            self.model_service = ModelService(
                sandbox=self._sandbox,
                config=self.config.model_service_config,
            )

            # Execute install (this prepares the environment but doesn't start the service)
            await self.model_service.install()

            logger.info(f"[{sandbox_id}] ModelService initialized successfully")

        except Exception as e:
            logger.error(f"[{sandbox_id}] ModelService initialization failed: {str(e)}", exc_info=True)
            raise

    def _extract_session_id_from_log(self, log_content: str) -> str:
        """Extract session ID from IFlow log file content.

        Parses the log content to find <Execution Info> JSON block and extracts
        the session-id field.

        Args:
            log_content: Content from the log file (ideally last 1000 lines).

        Returns:
            Session ID string if found, empty string otherwise.
        """

        sandbox_id = self._sandbox.sandbox_id
        logger.debug(f"[{sandbox_id}] Attempting to extract session-id from log content")

        try:
            # Extract JSON content between <Execution Info> tags
            json_match = re.search(r"<Execution Info>\s*(.*?)\s*</Execution Info>", log_content, re.DOTALL)

            if not json_match:
                logger.debug(f"[{sandbox_id}] No <Execution Info> block found in log")
                return ""

            json_str = json_match.group(1).strip()
            logger.debug(f"[{sandbox_id}] Found Execution Info block, parsing JSON")

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

    async def _get_session_id_from_sandbox(self) -> str:
        """Retrieve session ID from IFlow log file in sandbox.

        Fetches the last 1000 lines of the log file and extracts the session ID.
        Returns empty string if log file is empty, not found, or parsing fails.

        The command uses 'tail -1000 ... 2>/dev/null || echo ""' to ensure:
        - Errors (file not found, permission denied) are suppressed via 2>/dev/null
        - If tail fails, echo returns empty string via || operator
        - Exit code is always 0, making error checking unnecessary

        Returns:
            Session ID string if found, empty string otherwise.
        """

        sandbox_id = self._sandbox.sandbox_id
        logger.info(f"[{sandbox_id}] Retrieving session ID from sandbox log file")

        try:
            log_file_path = self.config.iflow_log_file
            logger.debug(f"[{sandbox_id}] Reading log file: {log_file_path}")

            result = await self._sandbox.arun(
                cmd=f"tail -1000 {log_file_path} 2>/dev/null || echo ''",
                session=self.config.agent_session,
            )

            log_content = result.output.strip()

            if not log_content:
                logger.debug(f"[{sandbox_id}] Log file is empty or not found")
                return ""

            logger.debug(f"[{sandbox_id}] Retrieved log content ({len(log_content)} bytes)")
            session_id = self._extract_session_id_from_log(log_content)
            return session_id

        except Exception as e:
            logger.error(f"[{sandbox_id}] Error retrieving session ID from sandbox: {str(e)}")
            return ""

    async def run(
        self,
        problem_statement: str,
        project_path: str,
        agent_run_timeout: int = 1800,
        agent_run_check_interval: int = 30,
    ):
        """Run IFlow CLI to solve a specified problem.

        Automatically attempts to retrieve the previous session ID from the log file.
        If a session ID is found, it will be used to resume the previous execution.
        If not found, a fresh execution is started with an empty session ID.

        If ModelService is configured, it will be started and used to monitor the agent process.

        Args:
            problem_statement: Problem statement that IFlow CLI will attempt to solve.
            project_path: Project path, can be a string or Path object.
            agent_run_timeout: Agent execution timeout in seconds. Defaults to 1800 (30 minutes).
            agent_run_check_interval: Interval for checking progress during agent execution in seconds. Defaults to 30.

        Returns:
            Object containing command execution results, including exit code and output.
        """

        sandbox_id = self._sandbox.sandbox_id
        start_time = time.time()

        logger.info(f"[{sandbox_id}] Starting IFlow CLI run operation")
        logger.debug(f"[{sandbox_id}] Project path: {project_path}, Problem statement: {problem_statement[:100]}...")

        try:
            # Step 1: Change to project directory
            logger.info(f"[{sandbox_id}] Changing working directory to: {project_path}")
            result = await self._sandbox.arun(
                cmd=f"cd {project_path}",
                session=self.config.agent_session,
            )

            if result.exit_code != 0:
                logger.error(f"[{sandbox_id}] Failed to change directory to {project_path}: {result.output}")
                return result
            logger.debug(f"[{sandbox_id}] Successfully changed working directory")

            # Step 2: Attempt to retrieve session ID from previous execution
            logger.info(f"[{sandbox_id}] Attempting to retrieve session ID from previous execution")
            session_id = await self._get_session_id_from_sandbox()
            if session_id:
                logger.info(f"[{sandbox_id}] Using existing session ID: {session_id}")
            else:
                logger.info(f"[{sandbox_id}] No previous session found, will start fresh execution")

            # Step 3: Prepare and execute IFlow CLI command in nohup mode
            logger.info(
                f"[{sandbox_id}] Preparing to run IFlow CLI with timeout {agent_run_timeout}s "
                f"and check interval {agent_run_check_interval}s"
            )
            # Format the command with session ID (empty string if not found) and problem statement
            # Example command: iflow -r "session-id" -p "problem statement" --yolo > ~/.iflow/session_info.log 2>&1
            iflow_run_cmd = self.config.iflow_run_cmd.format(
                session_id=f'"{session_id}"',
                problem_statement=shlex.quote(problem_statement),
                iflow_log_file=self.config.iflow_log_file,
            )
            logger.debug(f"[{sandbox_id}] IFlow command template: {self.config.iflow_run_cmd}")
            logger.debug(f"[{sandbox_id}] Formatted IFlow command: {iflow_run_cmd}")

            # Use _agent_run method to execute command and handle ModelService integration
            result = await self._agent_run(
                cmd=f"bash -c {shlex.quote(iflow_run_cmd)}",
                session=self.config.agent_session,
                wait_timeout=agent_run_timeout,
                wait_interval=agent_run_check_interval,
            )

            # Step 4: Log execution outcome with detailed information
            logger.info(f"[{sandbox_id}] IFlow CLI execution completed")

            # Read the last 1000 lines of log file since output was redirected
            log_file_path = self.config.iflow_log_file
            result_log = await self._sandbox.arun(
                cmd=f"tail -1000 {log_file_path} 2>/dev/null || echo ''",
                session=self.config.agent_session,
            )
            log_content = result_log.output

            if result.exit_code == 0:
                logger.info(f"[{sandbox_id}] ✓ IFlow-Cli completed successfully (exit_code: {result.exit_code})")
                logger.debug(f"[{sandbox_id}] Command output: {log_content}")
            else:
                logger.error(f"[{sandbox_id}] ✗ IFlow-Cli failed with exit_code: {result.exit_code}")
                logger.error(f"[{sandbox_id}] Error output: {log_content}")

            elapsed_total = time.time() - start_time

            if result and result.exit_code == 0:
                logger.info(
                    f"[{sandbox_id}] Agent Run completed: IFlow CLI execution succeeded (elapsed: {elapsed_total:.2f}s)"
                )
            else:
                error_msg = result.failure_reason if result else "No result returned"
                logger.error(
                    f"[{sandbox_id}] Operation failed: IFlow CLI execution failed - {error_msg} "
                    f"(elapsed: {elapsed_total:.2f}s)"
                )

            return result

        except Exception as e:
            elapsed_total = time.time() - start_time
            logger.error(
                f"[{sandbox_id}] Operation failed: IFlow CLI execution failed - {str(e)} "
                f"(elapsed: {elapsed_total:.2f}s)",
                exc_info=True,
            )
            raise

    async def _agent_run(
        self,
        cmd: str,
        session: str,
        wait_timeout: int,
        wait_interval: int,
    ):
        """Execute agent command in nohup mode with optional ModelService watch.

        Starts the agent process and if ModelService is configured, calls watch_agent
        to monitor the process during execution.

        Args:
            cmd: Command to execute
            session: Bash session name
            wait_timeout: Timeout for process completion
            wait_interval: Interval for checking process status

        Returns:
            Observation: Execution result

        Raises:
            Exception: If process execution fails
        """

        sandbox_id = self._sandbox.sandbox_id

        try:
            timestamp = str(time.time_ns())
            tmp_file = f"/tmp/tmp_{timestamp}.out"

            # Start nohup process and get PID
            pid, error_response = await self._sandbox.start_nohup_process(cmd=cmd, tmp_file=tmp_file, session=session)

            if error_response is not None:
                return error_response

            # If failed to extract PID
            if pid is None:
                msg = "Failed to submit command, nohup failed to extract PID"
                return Observation(output=msg, exit_code=1, failure_reason=msg)

            logger.info(f"[{sandbox_id}] Agent process started with PID: {pid}")

            # If ModelService is configured, call watch_agent to monitor the process
            if self.model_service:
                try:
                    logger.info(f"[{sandbox_id}] Starting ModelService watch-agent for pid {pid}")
                    await self.model_service.watch_agent(pid=str(pid))
                    logger.info(f"[{sandbox_id}] ModelService watch-agent started successfully")
                except Exception as e:
                    logger.error(f"[{sandbox_id}] Failed to start watch-agent: {str(e)}", exc_info=True)
                    raise

            # Wait for agent process to complete
            logger.debug(f"[{sandbox_id}] Waiting for agent process completion (pid={pid})")
            success, message = await self._sandbox.wait_for_process_completion(
                pid=pid, session=session, wait_timeout=wait_timeout, wait_interval=wait_interval
            )

            # Handle nohup output and return result
            result = await self._sandbox.handle_nohup_output(
                tmp_file=tmp_file,
                session=session,
                success=success,
                message=message,
                ignore_output=False,
                response_limited_bytes_in_nohup=None,
            )

            return result

        except ReadTimeout:
            error_msg = f"Command execution failed due to timeout: '{cmd}'. This may be caused by an interactive command that requires user input."
            return Observation(output=error_msg, exit_code=1, failure_reason=error_msg)
        except Exception as e:
            error_msg = f"Failed to execute nohup command '{cmd}': {str(e)}"
            return Observation(output=error_msg, exit_code=1, failure_reason=error_msg)

    async def start_model_service(self):
        if not self.model_service:
            raise RuntimeError(f"ModelService is not initialized in {self.config.agent_type}!")

        await self.model_service.start()
