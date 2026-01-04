from __future__ import annotations  # Postpone annotation evaluation to avoid circular imports.

import asyncio
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from httpx import ReadTimeout

from rock.actions import CreateBashSessionRequest, Observation
from rock.actions.sandbox.base import AbstractSandbox
from rock.logger import init_logger
from rock.sdk.sandbox.agent.config import DefaultAgentConfig
from rock.sdk.sandbox.model_service.base import ModelService

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox

logger = init_logger(__name__)


class Agent(ABC):
    def __init__(self, sandbox: AbstractSandbox):
        self._sandbox = sandbox
        self.model_service: ModelService | None = None

    @abstractmethod
    async def init(self):
        pass

    @abstractmethod
    async def run(self, **kwargs):
        pass


class DefaultAgent(Agent):
    """Base agent class with common initialization and execution logic.

    Provides shared functionality for:
    - Session management (create, setup)
    - Pre/post startup command execution
    - ModelService initialization
    - Common error handling and logging
    - Nohup process execution

    Subclasses must implement:
    - _install() - specific installation logic
    - run() - specific execution logic
    """

    def __init__(self, sandbox: Sandbox, config: DefaultAgentConfig):
        super().__init__(sandbox)

        self._sandbox = sandbox
        self.model_service: ModelService | None = None

        self.config = config
        self.agent_session = self.config.agent_session

    async def init(self):
        """Initialize the agent environment.

        Common flow:
        1. Setup bash session
        2. Execute pre-startup commands
        3. Install agent-specific dependencies (via _install)
        4. Execute post-startup commands
        5. Initialize ModelService if configured

        All installation and post-startup tasks run in parallel with ModelService init.
        """
        sandbox_id = self._sandbox.sandbox_id
        start_time = time.time()

        logger.info(f"[{sandbox_id}] Starting agent initialization")

        try:
            # Sequential steps that must happen first
            await self._setup_session()
            await self._execute_pre_startup()

            # Parallel tasks: agent-specific install + ModelService init
            tasks = [self._install()]

            if self.config.model_service_config:
                tasks.append(self._init_model_service())

            await asyncio.gather(*tasks)

            # Post-startup commands (sequential)
            await self._execute_post_startup()

            elapsed = time.time() - start_time
            logger.info(f"[{sandbox_id}] Agent initialization completed (elapsed: {elapsed:.2f}s)")

        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(
                f"[{sandbox_id}] Agent initialization failed - {str(e)} (elapsed: {elapsed:.2f}s)",
                exc_info=True,
            )
            raise

    @abstractmethod
    async def _install(self):
        """Install agent-specific dependencies and tools.

        This method must be implemented by subclasses to handle:
        - Package installation (npm, pip, etc.)
        - Tool setup and configuration
        - Environment preparation

        Raises:
            Exception: If installation fails
        """
        pass

    async def _setup_session(self):
        """Create and configure the bash session for agent operations."""
        sandbox_id = self._sandbox.sandbox_id

        try:
            self._log_step(f"Creating bash session: {self.agent_session}", step_name="Setup Session")

            await self._sandbox.create_session(
                CreateBashSessionRequest(
                    session=self.agent_session,
                    env_enable=True,
                    env=self.config.session_envs,
                )
            )

            self._log_step(
                f"Bash session '{self.agent_session}' created successfully", step_name="Setup Session", is_complete=True
            )
        except Exception as e:
            logger.error(
                f"[{sandbox_id}] Failed to setup session: {str(e)}",
                exc_info=True,
            )
            raise

    async def _execute_pre_startup(self):
        """Execute pre-startup configuration commands."""
        sandbox_id = self._sandbox.sandbox_id
        cmd_list = self.config.pre_startup_bash_cmd_list

        if not cmd_list:
            return

        try:
            self._log_step(f"Executing {len(cmd_list)} pre-startup commands", step_name="Pre-startup")

            for idx, cmd in enumerate(cmd_list, 1):
                logger.debug(f"[{sandbox_id}] Executing pre-startup command {idx}/{len(cmd_list)}: {cmd[:100]}...")
                result = await self._sandbox.arun(
                    cmd=cmd,
                    session=self.agent_session,
                )

                if result.exit_code != 0:
                    logger.warning(
                        f"[{sandbox_id}] Pre-startup command {idx} failed with exit code "
                        f"{result.exit_code}: {result.output[:200]}..."
                    )
                else:
                    logger.debug(f"[{sandbox_id}] Pre-startup command {idx} completed successfully")

            self._log_step(f"Completed {len(cmd_list)} pre-startup commands", step_name="Pre-startup", is_complete=True)
        except Exception as e:
            logger.error(
                f"[{sandbox_id}] Pre-startup execution failed: {str(e)}",
                exc_info=True,
            )
            raise

    async def _execute_post_startup(self):
        """Execute post-startup configuration commands."""
        sandbox_id = self._sandbox.sandbox_id
        cmd_list = self.config.post_startup_bash_cmd_list

        if not cmd_list:
            return

        try:
            self._log_step(f"Executing {len(cmd_list)} post-startup commands", step_name="Post-startup")

            for idx, cmd in enumerate(cmd_list, 1):
                logger.debug(f"[{sandbox_id}] Executing post-startup command {idx}/{len(cmd_list)}: {cmd[:100]}...")
                result = await self._sandbox.arun(
                    cmd=cmd,
                    session=self.agent_session,
                )

                if result.exit_code != 0:
                    logger.warning(
                        f"[{sandbox_id}] Post-startup command {idx} failed with exit code "
                        f"{result.exit_code}: {result.output[:200]}..."
                    )
                else:
                    logger.debug(f"[{sandbox_id}] Post-startup command {idx} completed successfully")

            self._log_step(
                f"Completed {len(cmd_list)} post-startup commands", step_name="Post-startup", is_complete=True
            )
        except Exception as e:
            logger.error(
                f"[{sandbox_id}] Post-startup execution failed: {str(e)}",
                exc_info=True,
            )
            raise

    async def _init_model_service(self):
        """Initialize ModelService (install only, not start).

        Creates a ModelService instance and executes installation.
        The service will be started later in run() if needed.
        """
        sandbox_id = self._sandbox.sandbox_id

        try:
            logger.info(f"[{sandbox_id}] Initializing ModelService")

            self.model_service = ModelService(
                sandbox=self._sandbox,
                config=self.config.model_service_config,
            )

            await self.model_service.install()

            logger.info(f"[{sandbox_id}] ModelService initialized successfully")

        except Exception as e:
            logger.error(f"[{sandbox_id}] ModelService initialization failed: {str(e)}", exc_info=True)
            raise

    async def _agent_run(
        self,
        cmd: str,
        session: str,
        wait_timeout: int,
        wait_interval: int,
    ) -> Observation:
        """Execute agent command in nohup mode with optional ModelService watch.

        Args:
            cmd: Command to execute
            session: Bash session name
            wait_timeout: Timeout for process completion (seconds)
            wait_interval: Interval for checking process status (seconds)

        Returns:
            Observation: Execution result with exit code and output

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

    def _log_step(
        self,
        message: str,
        step_name: str = "Step",
        is_complete: bool = False,
        elapsed: float | None = None,
    ):
        """Helper method to log step progress with consistent formatting.

        Args:
            message: Main message to log
            step_name: Name of the step being executed
            is_complete: Whether this is a completion log
            elapsed: Optional elapsed time in seconds
        """
        sandbox_id = self._sandbox.sandbox_id

        if is_complete:
            time_str = f" (elapsed: {elapsed:.2f}s)" if elapsed is not None else ""
            logger.info(f"[{sandbox_id}] {step_name} completed: {message}{time_str}")
        else:
            logger.info(f"[{sandbox_id}] {step_name} started: {message}")
