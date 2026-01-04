from __future__ import annotations  # Postpone annotation evaluation to avoid circular imports.

import shlex
import time
from typing import TYPE_CHECKING

from pydantic import BaseModel

from rock import env_vars
from rock.actions import CreateBashSessionRequest
from rock.logger import init_logger
from rock.sdk.sandbox.utils import arun_with_retry

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox

logger = init_logger(__name__)


class ModelServiceConfig(BaseModel):
    """Configuration for ModelService.

    Attributes:
        workdir: Working directory path for model service.
        python_install_cmd: Command to install Python.
        model_service_install_cmd: Command to install model service package.
        python_install_timeout: Timeout for Python installation in seconds.
        model_service_install_timeout: Timeout for model service installation in seconds.
        model_service_session: Session name for model service.
        session_envs: Environment variables for the session.
        config_ini_cmd: Command to initialize config file.
        model_service_type: Type of model service to start.
        start_cmd: Command to start model service with model_service_type placeholder.
        stop_cmd: Command to stop model service.
        watch_agent_cmd: Command to watch agent with pid placeholder.
        anti_call_llm_cmd: Command to anti-call LLM with index and response_payload placeholders.
        anti_call_llm_cmd_no_response: Command to anti-call LLM with only index placeholder.
        logging_path: Path for logging directory. Must be configured when starting ModelService.
        logging_file_name: Name of the log file.
    """

    workdir: str = "/tmp_model_service"
    python_install_cmd: str = env_vars.ROCK_AGENT_PYTHON_INSTALL_CMD
    model_service_install_cmd: str = env_vars.ROCK_AGENT_MODEL_SERVICE_INSTALL_CMD
    python_install_timeout: int = 300
    model_service_install_timeout: int = 300
    model_service_session: str = "model-service-session"
    session_envs: dict[str, str] = {}

    config_ini_cmd: str = "mkdir -p ~/.rock && touch ~/.rock/config.ini"
    model_service_type: str = "local"
    start_cmd: str = "rock model-service start --type {model_service_type}"
    stop_cmd: str = "rock model-service stop"
    watch_agent_cmd: str = "rock model-service watch-agent --pid {pid}"

    anti_call_llm_cmd: str = "rock model-service anti-call-llm --index {index} --response {response_payload}"
    anti_call_llm_cmd_no_response: str = "rock model-service anti-call-llm --index {index}"

    # Logging path must be configured when starting ModelService.
    logging_path: str = "/data/logs"
    logging_file_name: str = "model_service.log"


class ModelService:
    """Service for managing model service installation and lifecycle in sandbox.

    This class handles model service installation, startup, and agent management
    within a sandboxed environment.

    Note:
        Caller is responsible for ensuring proper sequencing of install/start/stop operations.
    """

    def __init__(self, sandbox: Sandbox, config: ModelServiceConfig):
        """Initialize ModelService.

        Args:
            sandbox: Sandbox instance that this model service belongs to.
            config: Configuration object for model service.
        """
        self._sandbox = sandbox
        self.config = config
        self.is_installed = False
        self.is_started = False
        logger.debug(f"ModelService initialized: workdir={config.workdir}")

    async def install(self) -> None:
        """Install model service in the sandbox.

        Performs the following installation steps:
        1. Create a bash session for model service.
        2. Create working directory and Rock config file.
        3. Install Python.
        4. Install model service package.

        Note:
            Caller should ensure this is not called concurrently or repeatedly.

        Raises:
            Exception: If any installation step fails.
        """
        sandbox_id = self._sandbox.sandbox_id
        install_start_time = time.time()

        try:
            logger.info(f"[{sandbox_id}] Starting model service installation")

            # Step 1: Create bash session
            step_start_time = time.time()
            logger.debug(
                f"[{sandbox_id}] Step 1: Creating bash session: {self.config.model_service_session}, "
                f"env_enable=True, session_envs={self.config.session_envs}"
            )
            await self._sandbox.create_session(
                CreateBashSessionRequest(
                    session=self.config.model_service_session,
                    env_enable=True,
                    env=self.config.session_envs,
                )
            )
            step_elapsed = time.time() - step_start_time
            logger.info(f"[{sandbox_id}] Step 1 completed: Bash session created (elapsed: {step_elapsed:.2f}s)")

            # Step 2: Create working directory and Rock config file
            step_start_time = time.time()
            mkdir_cmd = f"mkdir -p {self.config.workdir}"
            logger.debug(f"[{sandbox_id}] Step 2a: {mkdir_cmd}")
            await self._sandbox.arun(
                cmd=mkdir_cmd,
                session=self.config.model_service_session,
            )

            logger.debug(f"[{sandbox_id}] Step 2b: {self.config.config_ini_cmd}")
            await self._sandbox.arun(
                cmd=self.config.config_ini_cmd,
                session=self.config.model_service_session,
            )
            step_elapsed = time.time() - step_start_time
            logger.info(
                f"[{sandbox_id}] Step 2 completed: Working directory and config initialized (elapsed: {step_elapsed:.2f}s)"
            )

            # Step 3: Install Python
            step_start_time = time.time()
            python_install_cmd = f"cd {self.config.workdir} && {self.config.python_install_cmd}"
            bash_python_cmd = f"bash -c {shlex.quote(python_install_cmd)}"
            logger.debug(
                f"[{sandbox_id}] Step 3: Installing Python (timeout: {self.config.python_install_timeout}s), "
                f"cmd: {bash_python_cmd}"
            )
            await arun_with_retry(
                sandbox=self._sandbox,
                cmd=bash_python_cmd,
                session=self.config.model_service_session,
                mode="nohup",
                wait_timeout=self.config.python_install_timeout,
                error_msg="Python installation failed",
            )
            step_elapsed = time.time() - step_start_time
            logger.info(f"[{sandbox_id}] Step 3 completed: Python installation finished (elapsed: {step_elapsed:.2f}s)")

            # Step 4: Install model service
            step_start_time = time.time()
            model_service_install_cmd = (
                f"export PATH={self.config.workdir}/python/bin:$PATH && "
                f"cd {self.config.workdir} && {self.config.model_service_install_cmd}"
            )
            bash_service_cmd = f"bash -c {shlex.quote(model_service_install_cmd)}"
            logger.debug(
                f"[{sandbox_id}] Step 4: Installing model service package (timeout: {self.config.model_service_install_timeout}s), "
                f"cmd: {bash_service_cmd}"
            )
            await arun_with_retry(
                sandbox=self._sandbox,
                cmd=bash_service_cmd,
                session=self.config.model_service_session,
                mode="nohup",
                wait_timeout=self.config.model_service_install_timeout,
                error_msg="Model service installation failed",
            )
            step_elapsed = time.time() - step_start_time
            logger.info(
                f"[{sandbox_id}] Step 4 completed: Model service package installation finished (elapsed: {step_elapsed:.2f}s)"
            )

            total_elapsed = time.time() - install_start_time
            logger.info(f"[{sandbox_id}] Installation finished successfully (total elapsed: {total_elapsed:.2f}s)")

            self.is_installed = True

        except Exception as e:
            total_elapsed = time.time() - install_start_time
            logger.error(f"[{sandbox_id}] Installation failed: {str(e)} (elapsed: {total_elapsed:.2f}s)", exc_info=True)
            raise

    async def start(self) -> None:
        """Start the model service in the sandbox.

        Starts the service with configured logging settings.

        Note:
            Caller should ensure install() has been called first and this is not called concurrently.

        Raises:
            Exception: If service startup fails.
        """
        sandbox_id = self._sandbox.sandbox_id
        start_time = time.time()

        if not self.is_installed:
            error_msg = (
                f"[{sandbox_id}] Cannot start model service: ModelService has not been installed yet. "
                f"Please call install() first."
            )
            logger.error(error_msg)
            raise RuntimeError(error_msg)

        try:
            start_cmd = (
                f"export ROCK_LOGGING_PATH={self.config.logging_path} && "
                f"export ROCK_LOGGING_FILE_NAME={self.config.logging_file_name} && "
                f"{self.config.workdir}/python/bin/{self.config.stop_cmd} && "
                f"{self.config.workdir}/python/bin/{self.config.start_cmd.format(model_service_type=self.config.model_service_type)}"
            )
            bash_start_cmd = f"bash -c {shlex.quote(start_cmd)}"
            logger.debug(f"[{sandbox_id}] Model service Start command: {bash_start_cmd}")

            await self._sandbox.arun(
                cmd=bash_start_cmd,
                session=None,
                mode="nohup",
            )
            elapsed = time.time() - start_time
            logger.info(f"[{sandbox_id}] Model service started successfully (elapsed: {elapsed:.2f}s)")
            self.is_started = True

        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(
                f"[{sandbox_id}] Model service startup failed: {str(e)} (elapsed: {elapsed:.2f}s)", exc_info=True
            )
            raise

    async def stop(self) -> None:
        """Stop the model service.

        Note:
            Caller should ensure proper sequencing with start().

        Raises:
            Exception: If service stop fails.
        """
        sandbox_id = self._sandbox.sandbox_id
        start_time = time.time()

        if not self.is_started:
            logger.warning(
                f"[{sandbox_id}] Model service is not running, skipping stop operation. is_started={self.is_started}"
            )
            return

        try:
            logger.info(f"[{sandbox_id}] Stopping model service")

            stop_cmd = f"{self.config.workdir}/python/bin/{self.config.stop_cmd}"
            bash_stop_cmd = f"bash -c {shlex.quote(stop_cmd)}"

            await self._sandbox.arun(
                cmd=bash_stop_cmd,
                session=None,
                mode="nohup",
            )

            elapsed = time.time() - start_time
            logger.info(f"[{sandbox_id}] Model service stopped (elapsed: {elapsed:.2f}s)")
            self.is_started = False

        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"[{sandbox_id}] Stop failed: {str(e)} (elapsed: {elapsed:.2f}s)", exc_info=True)
            raise

    async def watch_agent(self, pid: str) -> None:
        """Watch agent process with the specified PID.

        Args:
            pid: Process ID to watch.

        Note:
            Caller should ensure start() has been called first.

        Raises:
            Exception: If watch fails.
        """
        sandbox_id = self._sandbox.sandbox_id
        start_time = time.time()

        if not self.is_started:
            error_msg = f"[{sandbox_id}] Cannot watch agent: ModelService is not started. Please call start() first."
            logger.error(error_msg)
            raise RuntimeError(error_msg)

        try:
            watch_agent_cmd = f"{self.config.workdir}/python/bin/{self.config.watch_agent_cmd.format(pid=pid)}"
            bash_watch_cmd = f"bash -c {shlex.quote(watch_agent_cmd)}"
            logger.debug(f"[{sandbox_id}] Model service watch agent with pid={pid}, cmd: {bash_watch_cmd}")

            await self._sandbox.arun(
                cmd=bash_watch_cmd,
                session=None,
                mode="nohup",
            )
            elapsed = time.time() - start_time
            logger.info(f"[{sandbox_id}] Watch agent completed (elapsed: {elapsed:.2f}s)")

        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"[{sandbox_id}] Watch agent failed: {str(e)} (elapsed: {elapsed:.2f}s)", exc_info=True)
            raise

    async def anti_call_llm(
        self,
        index: int,
        response_payload: str | None = None,
        call_timeout: int = 600,
        check_interval: int = 3,
    ) -> str:
        """Execute anti-call LLM command.

        Executes the anti-call LLM command with optional response payload.
        Uses a new session to avoid session context pollution.

        Args:
            index: Index for anti-call LLM operation.
            response_payload: Optional response payload to include.
            call_timeout: Timeout for operation in seconds.
            check_interval: Interval for checking status in seconds.

        Returns:
            Output from the anti-call LLM command.

        Note:
            Caller should ensure start() has been called first.

        Raises:
            Exception: If operation fails.
        """
        sandbox_id = self._sandbox.sandbox_id
        start_time = time.time()

        if not self.is_started:
            error_msg = (
                f"[{sandbox_id}] Cannot execute anti-call LLM: ModelService is not started. Please call start() first."
            )
            logger.error(error_msg)
            raise RuntimeError(error_msg)

        try:
            logger.info(
                f"[{sandbox_id}] Executing anti-call LLM: index={index}, "
                f"has_response={response_payload is not None}, timeout={call_timeout}s"
            )

            if response_payload:
                cmd = self.config.anti_call_llm_cmd.format(
                    index=index,
                    response_payload=shlex.quote(response_payload),
                )
            else:
                cmd = self.config.anti_call_llm_cmd_no_response.format(index=index)

            full_cmd = f"{self.config.workdir}/python/bin/{cmd}"
            bash_cmd = f"bash -c {shlex.quote(full_cmd)}"
            logger.debug(f"[{sandbox_id}] Executing command: {bash_cmd}")

            result = await self._sandbox.arun(
                cmd=bash_cmd,
                mode="nohup",
                session=None,  # Start a new session to ensure clean context without session interference.
                wait_timeout=call_timeout,
                wait_interval=check_interval,
            )

            elapsed = time.time() - start_time
            logger.info(f"[{sandbox_id}] Anti-call LLM execution completed (elapsed: {elapsed:.2f}s)")

            return result.output

        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"[{sandbox_id}] Anti-call LLM failed: {str(e)} (elapsed: {elapsed:.2f}s)", exc_info=True)
            raise
