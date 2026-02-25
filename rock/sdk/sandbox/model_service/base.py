from __future__ import annotations  # Postpone annotation evaluation to avoid circular imports.

import shlex
from string import Template
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rock import env_vars
from rock.logger import init_logger
from rock.sdk.sandbox.runtime_env import PythonRuntimeEnv, PythonRuntimeEnvConfig, RuntimeEnv
from rock.sdk.sandbox.utils import with_time_logging

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox

logger = init_logger(__name__)


class ModelServiceConfig(BaseModel):
    """Configuration for ModelService.

    Provides unified commands for installation, startup/shutdown,
    agent monitoring, and anti-call LLM operations.
    """

    enabled: bool = Field(default=False)
    """Whether to enable model service."""

    type: str = Field(default="local")
    """Type of model service to start."""

    install_cmd: str = Field(default=env_vars.ROCK_MODEL_SERVICE_INSTALL_CMD)
    """Command to install model service package."""

    install_timeout: int = Field(default=300, gt=0)
    """Timeout for model service installation in seconds."""

    runtime_env_config: PythonRuntimeEnvConfig = Field(default_factory=PythonRuntimeEnvConfig)
    """Runtime environment configuration for the model service."""

    start_cmd: str = Field(default="rock model-service start --type ${type}")
    """Command to start model service with type placeholder."""

    stop_cmd: str = Field(default="rock model-service stop")
    """Command to stop model service."""

    config_ini_cmd: str = Field(default="mkdir -p ~/.rock && touch ~/.rock/config.ini")
    """Command to create Rock config file."""

    watch_agent_cmd: str = Field(default="rock model-service watch-agent --pid ${pid}")
    """Command to watch agent with pid placeholder."""

    anti_call_llm_cmd: str = Field(
        default="PYTHONWARNINGS=ignore rock model-service anti-call-llm --index ${index} --response ${response_payload}"
    )
    """Command to anti-call LLM with index and response_payload placeholders."""

    anti_call_llm_cmd_no_response: str = Field(
        default="PYTHONWARNINGS=ignore rock model-service anti-call-llm --index ${index}"
    )
    """Command to anti-call LLM with only index placeholder."""

    logging_path: str = Field(default="/data/logs")
    """Path for logging directory. Must be configured when starting ModelService."""

    logging_file_name: str = Field(default="model_service.log")
    """Name of the log file."""


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

        self.runtime_env: PythonRuntimeEnv | None = None

        self.is_installed = False
        self.is_started = False

    @with_time_logging("Installing model service")
    async def install(self) -> None:
        """Install model service in the sandbox.

        Performs the following installation steps:
        1. Create and initialize Python runtime environment (via RuntimeEnv).
        2. Install model service package.

        Note:
            Caller should ensure this is not called concurrently or repeatedly.

        Raises:
            Exception: If any installation step fails.
        """
        # Initialize runtime env (installs Python)
        self.runtime_env = await RuntimeEnv.create(self._sandbox, self.config.runtime_env_config)

        await self._create_rock_config()
        await self._install_model_service()

        self.is_installed = True

    async def _create_rock_config(self) -> None:
        """Create Rock config file."""
        await self.runtime_env.run(cmd=self.config.config_ini_cmd)

    @with_time_logging("Installing model service package")
    async def _install_model_service(self) -> None:
        """Install model service package using runtime_env.run()."""
        install_cmd = f"cd {self.runtime_env.workdir} && {self.config.install_cmd}"

        await self.runtime_env.run(
            cmd=install_cmd,
            wait_timeout=self.config.install_timeout,
            error_msg="Model service installation failed",
        )

    @with_time_logging("Starting model service")
    async def start(self) -> None:
        """Start the model service in the sandbox.

        Starts the service with configured logging settings.

        Note:
            Caller should ensure install() has been called first.

        Raises:
            RuntimeError: If service is not installed
            Exception: If service startup fails.
        """
        if not self.is_installed:
            error_msg = (
                f"[{self._sandbox.sandbox_id}] Cannot start model service: ModelService has not been installed yet. "
                f"Please call install() first."
            )
            logger.error(error_msg)
            raise RuntimeError(error_msg)

        bash_start_cmd = (
            f"export ROCK_LOGGING_PATH={self.config.logging_path} && "
            f"export ROCK_LOGGING_FILE_NAME={self.config.logging_file_name} && "
            f"{self.config.stop_cmd} && "
            f"{Template(self.config.start_cmd).safe_substitute(type=self.config.type)}"
        )
        logger.debug(f"[{self._sandbox.sandbox_id}] Model service Start command: {bash_start_cmd}")

        await self.runtime_env.run(cmd=bash_start_cmd)

        self.is_started = True

    @with_time_logging("Stopping model service")
    async def stop(self) -> None:
        """Stop the model service.

        Note:
            Caller should ensure proper sequencing with start().
        """
        if not self.is_started:
            logger.warning(
                f"[{self._sandbox.sandbox_id}] Model service is not running, skipping stop operation. is_started={self.is_started}"
            )
            return

        await self.runtime_env.run(cmd=self.config.stop_cmd)

        self.is_started = False

    @with_time_logging("Watching agent")
    async def watch_agent(self, pid: str) -> None:
        """Watch agent process with the specified PID.

        Args:
            pid: Process ID to watch.

        Note:
            Caller should ensure start() has been called first.

        Raises:
            RuntimeError: If service is not started
            Exception: If watch fails.
        """
        if not self.is_started:
            error_msg = f"[{self._sandbox.sandbox_id}] Cannot watch agent: ModelService is not started. Please call start() first."
            logger.error(error_msg)
            raise RuntimeError(error_msg)

        bash_watch_cmd = Template(self.config.watch_agent_cmd).safe_substitute(pid=pid)
        logger.debug(f"[{self._sandbox.sandbox_id}] Model service watch agent with pid={pid}, cmd: {bash_watch_cmd}")

        await self.runtime_env.run(cmd=bash_watch_cmd)

    @with_time_logging("Executing anti-call LLM")
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
            RuntimeError: If service is not started
            Exception: If operation fails.
        """
        sandbox_id = self._sandbox.sandbox_id

        if not self.is_started:
            error_msg = (
                f"[{sandbox_id}] Cannot execute anti-call LLM: ModelService is not started. Please call start() first."
            )
            logger.error(error_msg)
            raise RuntimeError(error_msg)

        logger.info(
            f"[{sandbox_id}] Executing anti-call LLM: index={index}, "
            f"has_response={response_payload is not None}, timeout={call_timeout}s"
        )

        from rock.sdk.sandbox.client import RunMode

        if response_payload:
            cmd = Template(self.config.anti_call_llm_cmd).safe_substitute(
                index=index,
                response_payload=shlex.quote(response_payload),
            )
        else:
            cmd = Template(self.config.anti_call_llm_cmd_no_response).safe_substitute(index=index)

        # We chose to use runtime_env's wrapped_cmd instead of the run method here,
        # mainly to avoid unexpected behavior caused by sharing a session with runtime_env
        bash_cmd = self.runtime_env.wrapped_cmd(cmd)
        logger.debug(f"[{sandbox_id}] Executing command: {bash_cmd}")

        result = await self._sandbox.arun(
            cmd=bash_cmd,
            mode=RunMode.NOHUP,
            session=None,
            wait_timeout=call_timeout,
            wait_interval=check_interval,
        )

        if result.exit_code != 0:
            raise RuntimeError(f"Anti-call LLM command failed: {result.output}")

        return result.output
