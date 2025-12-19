import shlex

from pydantic import BaseModel

from rock import env_vars
from rock.actions import CreateBashSessionRequest
from rock.logger import init_logger
from rock.sdk.sandbox.agent.utils import arun_with_retry
from rock.sdk.sandbox.client import Sandbox

logger = init_logger(__name__)


class AgentModelServiceConfig(BaseModel):
    """Configuration for AgentModelService.

    Attributes:
        workdir: Working directory path for model service.
        python_install_cmd: Command to install Python.
        model_service_install_cmd: Command to install model service package.
        python_install_timeout: Timeout for Python installation in seconds.
        model_service_install_timeout: Timeout for model service installation in seconds.
        model_service_session: Session name for model service.
        session_envs: Environment variables for the session.
        config_ini_cmd: Command to initialize config file.
        start_cmd: Command to start model service.
        stop_cmd: Command to stop model service.
        watch_agent_cmd: Command to watch agent with pid placeholder.
        anti_call_llm_cmd: Command to anti-call LLM with index and response placeholders.
        anti_call_llm_cmd_no_response: Command to anti-call LLM with only index placeholder.
    """

    workdir: str = "/tmp_model_service"
    python_install_cmd: str = env_vars.ROCK_AGENT_PYTHON_INSTALL_CMD
    model_service_install_cmd: str = env_vars.ROCK_AGENT_MODEL_SERVICE_INSTALL_CMD
    python_install_timeout: int = 300
    model_service_install_timeout: int = 300
    model_service_session: str = "model-service-session"
    session_envs: dict[str, str] = {}

    config_ini_cmd: str = "mkdir -p ~/.rock && touch ~/.rock/config.ini"
    start_cmd: str = "rock model-service start --type local"
    stop_cmd: str = "rock model-service stop"
    watch_agent_cmd: str = "rock model-service watch-agent --pid {pid}"

    anti_call_llm_cmd: str = "rock model-service anti-call-llm --index {index} --response {response_payload}"
    anti_call_llm_cmd_no_response: str = "rock model-service anti-call-llm --index {index}"


class AgentModelService:
    """Service for installing and managing model service in sandbox.

    This class handles the lifecycle of model service installation and execution
    within a sandbox environment, including Python installation, service startup,
    and agent management.

    Attributes:
        config: Configuration for the model service.
        has_installed: Flag indicating if the service has been installed.
        is_started: Flag indicating if the service is currently running.
    """

    def __init__(self, config: AgentModelServiceConfig):
        """Initialize AgentModelService.

        Args:
            config: Configuration object for model service.
        """
        self.config = config
        self.has_installed = False
        self.is_started = False
        logger.debug(
            f"AgentModelService initialized with config: "
            f"workdir={config.workdir}, session={config.model_service_session}"
        )

    async def _install(self, sandbox: Sandbox) -> None:
        """Install model service in the sandbox.

        This method performs the following steps:
        1. Create a bash session for model service.
        2. Create working directory and config file.
        3. Install Python.
        4. Install model service package.

        Args:
            sandbox: Sandbox instance where installation will occur.

        Raises:
            Exception: If any installation step fails.
        """
        if self.has_installed:
            return

        sandbox_id = sandbox.sandbox_id

        try:
            logger.info(f"[{sandbox_id}] Starting model service installation")

            # Step 1: Create bash session
            logger.info(f"[{sandbox_id}] Creating bash session: {self.config.model_service_session}")
            await sandbox.create_session(
                CreateBashSessionRequest(
                    session=self.config.model_service_session,
                    env_enable=True,
                    env=self.config.session_envs,
                )
            )
            logger.debug(f"[{sandbox_id}] Bash session created successfully")

            # Step 2: Create working directory and config file
            logger.info(f"[{sandbox_id}] Creating working directory: {self.config.workdir}")
            await sandbox.arun(
                cmd=f"mkdir -p {self.config.workdir} && {self.config.config_ini_cmd}",
                session=self.config.model_service_session,
            )
            logger.debug(f"[{sandbox_id}] Working directory and config file created")

            # Step 3: Install Python
            logger.info(f"[{sandbox_id}] Installing Python (timeout: {self.config.python_install_timeout}s)")
            python_install_cmd = f"cd {self.config.workdir} && {self.config.python_install_cmd}"
            logger.debug(f"[{sandbox_id}] Python install command: {python_install_cmd}")

            await arun_with_retry(
                sandbox=sandbox,
                cmd=f"bash -c {shlex.quote(python_install_cmd)}",
                session=self.config.model_service_session,
                mode="nohup",
                wait_timeout=self.config.python_install_timeout,
                error_msg="Python installation failed",
            )
            logger.info(f"[{sandbox_id}] Python installation completed successfully")

            # Step 4: Install model service
            logger.info(
                f"[{sandbox_id}] Installing model service (timeout: {self.config.model_service_install_timeout}s)"
            )
            model_service_install_cmd = (
                f"export PATH={self.config.workdir}/python/bin:$PATH && "
                f"cd {self.config.workdir} && {self.config.model_service_install_cmd}"
            )
            logger.debug(f"[{sandbox_id}] Model service install command: {model_service_install_cmd}")

            await arun_with_retry(
                sandbox=sandbox,
                cmd=f"bash -c {shlex.quote(model_service_install_cmd)}",
                session=self.config.model_service_session,
                mode="nohup",
                wait_timeout=self.config.model_service_install_timeout,
                error_msg="Model service installation failed",
            )
            logger.info(f"[{sandbox_id}] Model service installation completed successfully")

            self.has_installed = True

        except Exception as e:
            logger.error(
                f"[{sandbox_id}] Model service installation failed: {str(e)}",
                exc_info=True,
            )
            raise

    async def start(
        self,
        sandbox: Sandbox,
        logging_path: str = "/data/logs",
        logging_file_name: str = "model_service.log",
    ) -> None:
        """Start the model service in the sandbox.

        This method installs the service if not already installed, then starts it
        with the specified logging configuration.

        Args:
            sandbox: Sandbox instance where service will be started.
            logging_path: Path for logging directory.
            logging_file_name: Name of the log file.

        Raises:
            Exception: If service startup fails.
        """
        await self._install(sandbox=sandbox)

        sandbox_id = sandbox.sandbox_id
        try:
            start_cmd = (
                f"export ROCK_LOGGING_PATH={logging_path} && "
                f"export ROCK_LOGGING_FILE_NAME={logging_file_name} && "
                f"{self.config.workdir}/python/bin/{self.config.stop_cmd} && "
                f"{self.config.workdir}/python/bin/{self.config.start_cmd}"
            )

            logger.info(f"[{sandbox_id}] Starting model service: {start_cmd}")

            await sandbox.arun(
                cmd=start_cmd,
                session=self.config.model_service_session,
            )
            self.is_started = True
            logger.info(f"[{sandbox_id}] Model service started successfully")

        except Exception as e:
            logger.error(
                f"[{sandbox_id}] Model service startup failed: {str(e)}",
                exc_info=True,
            )
            self.is_started = False
            raise

    async def watch_agent(self, sandbox: Sandbox, pid: str) -> None:
        """Watch agent process with the specified PID.

        Args:
            sandbox: Sandbox instance.
            pid: Process ID to watch.

        Raises:
            Exception: If service is not started or watch fails.
        """
        if not self.is_started:
            raise Exception("Model service is not started")

        sandbox_id = sandbox.sandbox_id
        watch_agent_cmd = f"{self.config.workdir}/python/bin/{self.config.watch_agent_cmd.format(pid=pid)}"

        logger.info(f"[{sandbox_id}] Watching agent with PID: {pid}")
        logger.debug(f"[{sandbox_id}] Watch command: {watch_agent_cmd}")

        await sandbox.arun(
            cmd=watch_agent_cmd,
            session=None,
            mode="nohup",
        )
        logger.info(f"[{sandbox_id}] Agent watch completed")

    async def anti_call_llm(
        self,
        sandbox: Sandbox,
        index: int,
        response_payload: str | None = None,
        call_timeout: int = 120,
        check_interval: int = 5,
    ) -> str:
        """Execute anti-call LLM command.

        This method executes the anti-call LLM command with optional response payload.
        It ensures session context is isolated to prevent pollution.

        Args:
            sandbox: Sandbox instance.
            index: Index for anti-call LLM operation.
            response_payload: Optional response payload to include in command.
            call_timeout: Timeout for the operation in seconds.
            check_interval: Interval for checking operation status in seconds.

        Returns:
            Output from the anti-call LLM command.

        Raises:
            Exception: If service is not started or operation fails.
        """
        if not self.is_started:
            raise Exception("Model service is not started")

        sandbox_id = sandbox.sandbox_id
        if response_payload:
            cmd = self.config.anti_call_llm_cmd.format(
                index=index,
                response_payload=shlex.quote(response_payload),
            )
        else:
            cmd = self.config.anti_call_llm_cmd_no_response.format(index=index)

        cmd = f"{self.config.workdir}/python/bin/{cmd}"

        logger.info(
            f"[{sandbox_id}] Executing anti-call LLM with index={index}, has_response={response_payload is not None}"
        )
        logger.debug(f"[{sandbox_id}] Anti-call LLM command: {cmd}")

        result = await sandbox.arun(
            cmd=cmd,
            mode="nohup",
            session=None,
            wait_timeout=call_timeout,
            wait_interval=check_interval,
        )

        logger.info(f"[{sandbox_id}] Anti-call LLM execution completed")
        return result.output
