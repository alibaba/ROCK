from __future__ import annotations  # Postpone annotation evaluation to avoid circular imports.

import asyncio
import os
import shlex
import time
import uuid
from typing import TYPE_CHECKING

from httpx import ReadTimeout
from pydantic import Field

from rock import env_vars
from rock.actions import CreateBashSessionRequest, Observation
from rock.logger import init_logger
from rock.sdk.sandbox.agent.base import Agent
from rock.sdk.sandbox.agent.config import AgentBashCommand, AgentConfig
from rock.sdk.sandbox.model_service.base import ModelService, ModelServiceConfig
from rock.sdk.sandbox.runtime_env.base import RuntimeEnv
from rock.sdk.sandbox.runtime_env.config import PythonRuntimeEnvConfig, RuntimeEnvConfig
from rock.sdk.sandbox.utils import with_time_logging

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox


logger = init_logger(__name__)


class RockAgentConfig(AgentConfig):
    """Configuration for RockAgent, inheriting from AgentConfig.

    Provides unified runtime identifiers, session management,
    startup/shutdown commands, and environment configurations.
    """

    agent_installed_dir: str = Field(default="/installed_agent")
    """Directory where the agent is installed in the sandbox."""

    instance_id: str = Field(default=f"instance-id-{uuid.uuid4().hex}")
    """Unique identifier for this agent instance."""

    project_path: str = Field(default="/testbed")
    """Working directory path in the sandbox. If not set, defaults to /testbed. If not exists, it will be created."""

    agent_session: str = Field(default=f"agent-session-{uuid.uuid4().hex}")
    """Session identifier for bash operations."""

    session_envs: dict[str, str] = Field(default_factory=dict)
    """Environment variables for the agent session."""

    pre_init_cmds: list[AgentBashCommand] = Field(
        default=[AgentBashCommand(**agent_bash_cmd) for agent_bash_cmd in env_vars.ROCK_AGENT_PRE_INIT_BASH_CMD_LIST]
    )
    """Commands to execute before agent initialization."""

    post_init_cmds: list[AgentBashCommand] = Field(default=[])
    """Commands to execute after agent initialization."""

    agent_install_timeout: int = Field(default=600, gt=0)
    """Maximum time in seconds for agent installation."""

    agent_run_timeout: int = Field(default=1800, gt=0)
    """Maximum time in seconds for agent execution."""

    agent_run_check_interval: int = Field(default=30, gt=0)
    """Interval in seconds for checking agent run status."""

    working_dir: str | None = Field(default=None)
    """Local directory to upload to sandbox. If None, no upload occurs."""

    run_cmd: str | None = Field(default=None)
    """Command to execute agent. 必须留一个{prompt}的位置"""

    rt_env_config: RuntimeEnvConfig | None = Field(default_factory=PythonRuntimeEnvConfig)
    """Runtime environment configuration for the agent."""

    model_service_config: ModelServiceConfig | None = Field(default=None)
    """Optional ModelService configuration for LLM integration."""


class RockAgent(Agent):
    """RockAgent: Agent implementation for sandbox environments.

    Responsibilities:
    - Manage RuntimeEnv installation and initialization (Python environments)
    - Upload and provision working directory from local to sandbox
    - Execute pre/post initialization commands
    - Provide unified agent run entry with bash wrapper
    - Support optional ModelService integration for LLM support

    Initialization flow:
    1. Execute pre-init commands
    2. Setup bash session with environment variables
    3. Provision working directory (upload local dir to sandbox)
    4. Parallel: RuntimeEnv init + ModelService install (if configured)
    5. Execute post-init commands
    """

    def __init__(self, sandbox: Sandbox, config: RockAgentConfig):
        self._sandbox = sandbox
        self.model_service: ModelService | None = None
        self.rt_env: RuntimeEnv | None = None

        self.config = config
        self.agent_session = self.config.agent_session
        self._working_dir_in_sandbox = None

    async def init(self):
        """Initialize the agent environment.

        Flow:
        1. Execute pre-init commands
        2. Setup bash session with environment variables
        3. Provision working directory (upload local dir to sandbox)
        4. Parallel: RuntimeEnv init + ModelService install (if configured)
        5. Execute post-init commands
        """
        sandbox_id = self._sandbox.sandbox_id
        start_time = time.time()

        logger.info(f"[{sandbox_id}] Starting agent initialization")

        try:
            # Sequential steps that must happen first
            await self._execute_pre_init()

            await self._setup_session()

            await self._provision_working_dir()

            # Parallel tasks: agent-specific install + ModelService init
            tasks = [self.install()]

            if self.config.model_service_config:
                tasks.append(self._init_model_service())

            await asyncio.gather(*tasks)

            await self._execute_post_init()

            elapsed = time.time() - start_time
            logger.info(f"[{sandbox_id}] Agent initialization completed (elapsed: {elapsed:.2f}s)")

        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(
                f"[{sandbox_id}] Agent initialization failed - {str(e)} (elapsed: {elapsed:.2f}s)",
                exc_info=True,
            )
            raise

    async def _provision_working_dir(self) -> None:
        """If config.working_dir is set, upload it into sandbox /tmp/<random>.

        Assumes sandbox.upload_dir(source_dir, target_dir) exists.
        """
        working_dir = self.config.working_dir
        if not working_dir:
            return

        sandbox_id = self._sandbox.sandbox_id
        local_abs = os.path.abspath(working_dir)

        if not os.path.exists(local_abs):
            raise FileNotFoundError(f"config.working_dir not found: {local_abs}")
        if not os.path.isdir(local_abs):
            raise ValueError(f"config.working_dir must be a directory: {local_abs}")

        target_dir = f"/tmp/rock_workdir_{uuid.uuid4().hex}"

        logger.info(f"[{sandbox_id}] Provisioning working_dir: {local_abs} -> {target_dir}")

        # Clean & create
        result = await self._sandbox.arun(
            cmd=f"rm -rf {shlex.quote(target_dir)} && mkdir -p {shlex.quote(target_dir)}",
            session=self.agent_session,
        )
        if result.exit_code != 0:
            raise RuntimeError(f"Failed to create target directory {target_dir}: {result.output}")

        upload_result = await self._sandbox.fs.upload_dir(source_dir=local_abs, target_dir=target_dir)
        if upload_result.exit_code != 0:
            raise RuntimeError(f"Failed to upload directory: {upload_result.failure_reason}")

        self._working_dir_in_sandbox = target_dir
        logger.info(f"[{sandbox_id}] working_dir_in_sandbox ready: {target_dir}")

    @property
    def working_dir_in_sandbox(self) -> str | None:
        """Return sandbox workdir provisioned from config.working_dir.

        Raises if working_dir was not configured or provisioning not completed.
        """
        return self._working_dir_in_sandbox

    async def run(
        self,
        prompt: str,
    ) -> Observation:
        """Unified agent run entry.

        Notes:
        - RockAgent only wraps the command with: bash -c <quoted>.
        - Subclass is responsible for composing the full command content
          (including `cd ... && ...` if needed).
        - RockAgent does NOT start ModelService; upper layer should call `start_model_service()` if needed.
        """
        cmd = await self.create_agent_run_cmd(prompt)
        return await self._agent_run(
            cmd=cmd,
            session=self.agent_session,
        )

    async def install(self):
        """Initialize the runtime environment.

        Uses rt_env_config from the agent configuration.
        This method is idempotent: calling multiple times only initializes once.

        If rt_env already exists and is initialized, this method returns early
        without creating a new RuntimeEnv instance.
        """
        # Check if already initialized to avoid creating duplicate RuntimeEnv
        if self.rt_env is not None and self.rt_env.initialized:
            sandbox_id = self._sandbox.sandbox_id
            logger.info(f"[{sandbox_id}] RuntimeEnv already initialized, skipping install")
            return

        rt_config = self.config.rt_env_config

        # Create a new RuntimeEnv instance for this agent (uses UUID internally)
        self.rt_env = RuntimeEnv.from_config(self._sandbox, rt_config)

        # Initialize the runtime (includes installation)
        if not self.rt_env.initialized:
            await self.rt_env.init()

    async def _setup_session(self):
        """Create and configure the bash session for agent operations."""
        sandbox_id = self._sandbox.sandbox_id

        try:
            logger.info(f"[{sandbox_id}] Creating bash session: {self.agent_session}")

            await self._sandbox.create_session(
                CreateBashSessionRequest(
                    session=self.agent_session,
                    env_enable=True,
                    env=self.config.session_envs,
                )
            )

            logger.info(
                f"[{sandbox_id}] Setup Session completed: Bash session '{self.agent_session}' created successfully"
            )
        except Exception as e:
            logger.error(
                f"[{sandbox_id}] Failed to setup session: {str(e)}",
                exc_info=True,
            )
            raise

    @with_time_logging("Agent pre-init cmds")
    async def _execute_pre_init(self):
        await self._execute_init_commands(
            cmd_list=self.config.pre_init_cmds,
            step_name="pre-init",
        )

    @with_time_logging("Agent post-init cmds")
    async def _execute_post_init(self):
        await self._execute_init_commands(
            cmd_list=self.config.post_init_cmds,
            step_name="post-init",
        )

    async def _execute_init_commands(self, cmd_list: list[AgentBashCommand], step_name: str):
        """Execute init-stage commands using nohup."""
        sandbox_id = self._sandbox.sandbox_id

        if not cmd_list:
            return

        try:
            logger.info(f"[{sandbox_id}] {step_name.capitalize()} started: Executing {len(cmd_list)} commands")

            for idx, cmd_config in enumerate(cmd_list, 1):
                command = cmd_config.command
                timeout = cmd_config.timeout_seconds

                logger.debug(
                    f"[{sandbox_id}] Executing {step_name} command {idx}/{len(cmd_list)}: "
                    f"{command[:100]}... (timeout: {timeout}s)"
                )

                from rock.sdk.sandbox.client import RunMode

                result = await self._sandbox.arun(
                    cmd=f"bash -c {shlex.quote(command)}",
                    session=None,
                    wait_timeout=timeout,
                    mode=RunMode.NOHUP,
                )

                if result.exit_code != 0:
                    raise RuntimeError(
                        f"[{sandbox_id}] {step_name} command {idx} failed with exit code "
                        f"{result.exit_code}: {result.output[:200]}"
                    )
                logger.debug(f"[{sandbox_id}] {step_name} command {idx} completed successfully")

            logger.info(f"[{sandbox_id}] {step_name.capitalize()} completed: Completed {len(cmd_list)} commands")

        except Exception as e:
            logger.error(f"[{sandbox_id}] {step_name} execution failed: {str(e)}", exc_info=True)
            raise

    async def _init_model_service(self):
        """Initialize ModelService (install only, not start).

        If the sandbox already has a ModelService, reuse it instead of creating
        a new one. Otherwise, creates a ModelService instance and executes installation.
        The service will be started later in run() if needed.
        """
        sandbox_id = self._sandbox.sandbox_id

        try:
            # Check if sandbox already has a ModelService
            if self._sandbox.model_service is not None:
                logger.info(f"[{sandbox_id}] Reusing existing ModelService from sandbox")
                self.model_service = self._sandbox.model_service
                # Ensure it's installed if not already
                if not self.model_service.is_installed:
                    await self.model_service.install()
                logger.info(f"[{sandbox_id}] ModelService reused successfully")
                return

            logger.info(f"[{sandbox_id}] Initializing ModelService")

            self.model_service = ModelService(
                sandbox=self._sandbox,
                config=self.config.model_service_config,
            )

            await self.model_service.install()

            self._sandbox.model_service = self.model_service  # Ensure one sandbox has just one model service

            logger.info(f"[{sandbox_id}] ModelService initialized successfully")

        except Exception as e:
            logger.error(f"[{sandbox_id}] ModelService initialization failed: {str(e)}", exc_info=True)
            raise

    async def create_agent_run_cmd(self, prompt: str) -> str:
        project_path = shlex.quote(str(self.config.project_path))
        run_cmd = self.config.run_cmd.format(prompt=prompt)
        wrapped_cmd = self.rt_env.wrapped_cmd(run_cmd, prepend=False)

        parts = [
            f"mkdir -p {project_path}",
            f"cd {project_path}",
            wrapped_cmd,
        ]
        return " && ".join(parts)

    @with_time_logging("Agent run")
    async def _agent_run(self, cmd: str, session: str) -> Observation:
        """Execute agent command in nohup mode with optional ModelService watch.

        Args:
            cmd: Command to execute
            session: Bash session name

        Returns:
            Observation: Execution result with exit code and output

        Note:
            wait_timeout and wait_interval are read from self.config.
        """
        sandbox_id = self._sandbox.sandbox_id

        try:
            timestamp = str(time.time_ns())
            tmp_file = f"/tmp/tmp_{timestamp}.out"

            # Start nohup process and get PID
            pid, error_response = await self._sandbox.start_nohup_process(cmd=cmd, tmp_file=tmp_file, session=session)

            if error_response is not None:
                return error_response

            if pid is None:
                msg = "Failed to submit command, nohup failed to extract PID"
                return Observation(output=msg, exit_code=1, failure_reason=msg)

            logger.info(f"[{sandbox_id}] Agent process started with PID: {pid}")

            # If ModelService is configured, monitor the process
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
                pid=pid,
                session=session,
                wait_timeout=self.config.agent_run_timeout,
                wait_interval=self.config.agent_run_check_interval,
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
            error_msg = (
                f"Command execution failed due to timeout: '{cmd}'. "
                "This may be caused by an interactive command that requires user input."
            )
            return Observation(output=error_msg, exit_code=1, failure_reason=error_msg)
        except Exception as e:
            error_msg = f"Failed to execute nohup command '{cmd}': {str(e)}"
            logger.error(f"[{sandbox_id}] {error_msg}", exc_info=True)
            return Observation(output=error_msg, exit_code=1, failure_reason=error_msg)

    async def start_model_service(self):
        """Start the ModelService if it was initialized.

        Raises:
            RuntimeError: If ModelService is not initialized
        """
        sandbox_id = self._sandbox.sandbox_id

        if not self.model_service:
            raise RuntimeError(f"ModelService is not initialized in {self.config.agent_type}!")

        logger.info(f"[{sandbox_id}] Starting ModelService")
        await self.model_service.start()

    async def anti_call_llm(
        self,
        index: int,
        response_payload: str | None = None,
        call_timeout: int = 600,
        check_interval: int = 3,
    ) -> str:
        """Execute anti-call LLM command.

        Delegates to ModelService.anti_call_llm for execution.

        Raises:
            RuntimeError: If ModelService is not initialized
        """
        if not self.model_service:
            raise RuntimeError(f"ModelService is not initialized in {self.config.agent_type}!")

        return await self.model_service.anti_call_llm(
            index=index,
            response_payload=response_payload,
            call_timeout=call_timeout,
            check_interval=check_interval,
        )
