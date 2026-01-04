from __future__ import annotations  # Postpone annotation evaluation to avoid circular imports.

import asyncio
import os
import shlex
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from httpx import ReadTimeout

from rock import env_vars
from rock.actions import CreateBashSessionRequest, Observation, UploadRequest
from rock.logger import init_logger
from rock.sdk.sandbox.agent.base import Agent
from rock.sdk.sandbox.agent.config import AgentConfig
from rock.sdk.sandbox.model_service.base import ModelService, ModelServiceConfig
from rock.sdk.sandbox.utils import arun_with_retry

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox


logger = init_logger(__name__)


DEFAULT_SYSTEM_TEMPLATE = "You are a helpful assistant that can interact with a computer to solve tasks."

DEFAULT_INSTANCE_TEMPLATE = """<uploaded_files>
{{working_dir}}
</uploaded_files>
I've uploaded a python code repository in the directory {{working_dir}}. Consider the following PR description:

<pr_description>
{{problem_statement}}
</pr_description>

Can you help me implement the necessary changes to the repository so that the requirements specified in the <pr_description> are met?
I've already taken care of all changes to any of the test files described in the <pr_description>. This means you DON'T have to modify the testing logic or any of the tests in any way!
Your task is to make the minimal changes to non-tests files in the {{working_dir}} directory to ensure the <pr_description> is satisfied.
Follow these steps to resolve the issue:
1. As a first step, it might be a good idea to find and read code relevant to the <pr_description>
2. Create a script to reproduce the error and execute it with `python <filename.py>` using the bash tool, to confirm the error
3. Edit the sourcecode of the repo to resolve the issue
4. Rerun your reproduce script and confirm that the error is fixed!
5. Think about edgecases and make sure your fix handles them as well
Your thinking should be thorough and so it's fine if it's very long."""

DEFAULT_SUBMIT_REVIEW_MESSAGES = [
    """Thank you for your work on this issue. Please carefully follow the steps below to help review your changes.

1. If you made any changes to your code after running the reproduction script, please run the reproduction script again.
  If the reproduction script is failing, please revisit your changes and make sure they are correct.
  If you have already removed your reproduction script, please ignore this step.
2. Remove your reproduction script (if you haven't done so already).
3. If you have modified any TEST files, please revert them to the state they had before you started fixing the issue.
  You can do this with `git checkout -- /path/to/test/file.py`. Use below <diff> to find the files you need to revert.
4. Run the submit command again to confirm.

Here is a list of all of your changes:

<diff>
{{diff}}
</diff>"""
]

DEFAULT_PARSE_FUNCTION_TYPE = "function_calling"
DEFAULT_NEXT_STEP_TEMPLATE = "OBSERVATION:\n{{observation}}"
DEFAULT_NEXT_STEP_NO_OUTPUT_TEMPLATE = "Your command ran successfully and did not produce any output."

DEFAULT_RUN_SINGLE_CONFIG: dict[str, Any] = {
    "output_dir": "",
    "env": {
        "repo": {"path": ""},
        "deployment": {"type": "local"},
        "name": "local-deployment",
    },
    "problem_statement": {
        "type": "text",
        "text": "",
        "id": "",
    },
    "agent": {
        "templates": {
            "system_template": DEFAULT_SYSTEM_TEMPLATE,
            "instance_template": DEFAULT_INSTANCE_TEMPLATE,
            "next_step_template": DEFAULT_NEXT_STEP_TEMPLATE,
            "next_step_no_output_template": DEFAULT_NEXT_STEP_NO_OUTPUT_TEMPLATE,
            "max_observation_length": 85000,
        },
        "tools": {
            "execution_timeout": 1000,
            "env_variables": {
                "PAGER": "cat",
                "MANPAGER": "cat",
                "LESS": "-R",
                "PIP_PROGRESS_BAR": "off",
                "TQDM_DISABLE": "1",
                "GIT_PAGER": "cat",
            },
            "bundles": [
                {"path": "tools/registry"},
                {"path": "tools/edit_anthropic"},
                {"path": "tools/review_on_submit_m"},
                {"path": "tools/diff_state"},
            ],
            "registry_variables": {
                "USE_FILEMAP": "true",
                "SUBMIT_REVIEW_MESSAGES": DEFAULT_SUBMIT_REVIEW_MESSAGES,
            },
            "enable_bash_tool": True,
            "parse_function": {"type": "function_calling"},
        },
        "history_processors": [{"type": "cache_control", "last_n_messages": 2}],
        "model": {
            "name": "openai/gpt-4o",
            "per_instance_cost_limit": 0,
            "per_instance_call_limit": 100,
            "total_cost_limit": 0,
            "temperature": 0.0,
            "top_p": 1.0,
            "api_base": "",
            "api_key": "",
        },
    },
}


class SweAgentConfig(AgentConfig):
    """Configuration dataclass for SWE-agent initialization and execution.

    This class defines all configurable parameters for setting up and running
    SWE-agent in a sandboxed environment, including installation commands,
    working directories, and execution timeouts.

    Attributes:
        agent_type: Fixed identifier for this agent type ("swe-agent")
        default_run_single_config: Default configuration object for a single run
        agent_session: Name of the bash session used for SWE-agent execution
        pre_startup_bash_cmd_list: Commands executed before agent initialization
        post_startup_bash_cmd_list: Commands executed after agent initialization
        swe_agent_workdir: Working directory for agent installation and execution
        python_install_cmd: Command to install Python environment
        swe_agent_install_cmd: Command to clone and install SWE-agent repository
        python_install_timeout: Maximum seconds to wait for Python installation
        swe_agent_install_timeout: Maximum seconds to wait for SWE-agent installation
        agent_run_timeout: Maximum seconds to wait for agent execution completion
        agent_run_check_interval: Seconds between status checks during execution
        model_service_config: Configuration for ModelService (optional)
    """

    agent_type: Literal["swe-agent"] = "swe-agent"

    agent_session: str = "swe-agent-session"

    # Commands to execute before agent initialization (e.g., bashrc setup, hosts config)
    pre_startup_bash_cmd_list: list[str] = env_vars.ROCK_AGENT_PRE_STARTUP_BASH_CMD_LIST

    # Commands to execute after agent initialization
    post_startup_bash_cmd_list: list[str] = []

    # Working directory where SWE-agent will be installed and executed
    swe_agent_workdir: str = "/tmp_sweagent"

    # Command to download and set up Python environment
    python_install_cmd: str = env_vars.ROCK_AGENT_PYTHON_INSTALL_CMD

    # Command to clone SWE-agent repository and install dependencies
    swe_agent_install_cmd: str = "[ -d SWE-agent ] && rm -rf SWE-agent; git clone https://github.com/SWE-agent/SWE-agent.git && cd SWE-agent && pip install -e . -i https://mirrors.aliyun.com/pypi/simple/"

    python_install_timeout: int = 300

    swe_agent_install_timeout: int = 600

    default_run_single_config: dict[str, Any] = DEFAULT_RUN_SINGLE_CONFIG

    session_envs: dict[str, str] = {}

    model_service_config: ModelServiceConfig | None = None


class SweAgent(Agent):
    """SWE-agent implementation with integrated ModelService support.

    This class manages the complete lifecycle of SWE-agent including environment
    initialization, dependency installation, and task execution within a sandboxed
    environment. It provides an asynchronous interface for agent operations.

    The agent can optionally integrate with ModelService for LLM handling.
    If model_service_config is provided during initialization, ModelService will be
    installed during init() and started during run().

    Attributes:
        config: Configuration parameters for agent setup and execution
        agent_session: Name of the bash session used for agent operations
        model_service: ModelService instance (created if configured)
    """

    def __init__(self, sandbox: Sandbox, config: SweAgentConfig):
        """Initialize SWE-agent with sandbox environment and configuration.

        Args:
            sandbox: Sandbox instance for isolated agent execution
            config: Configuration parameters for agent setup

        Raises:
            AssertionError: If sandbox is not an instance of Sandbox class
        """
        super().__init__(sandbox)
        self._sandbox = sandbox
        self.config = config
        self.agent_session = self.config.agent_session

        # ModelService instance (created during init if configured)
        self.model_service: ModelService | None = None

    async def init(self):
        """Initialize the SWE-agent environment within the sandbox.

        Performs the following initialization steps in sequence:
        1. Creates a dedicated bash session for agent execution
        2. Executes pre-startup configuration commands
        3. Creates working directory for agent installation
        4. Installs Python environment
        5. Clones and installs SWE-agent
        6. Initializes ModelService if configured (parallel with step 5)

        The initialization process is asynchronous and uses the configured
        timeouts for long-running operations like dependency installation.

        Raises:
            Exception: If any initialization step fails
        """

        sandbox_id = self._sandbox.sandbox_id
        start_time = time.time()

        # Prepare tasks to run in parallel
        tasks = [self._install_swe_agent()]

        # Initialize ModelService if configured
        if self.config.model_service_config:
            tasks.append(self._init_model_service())

        # Run tasks in parallel
        await asyncio.gather(*tasks)

        elapsed = time.time() - start_time
        logger.info(f"[{sandbox_id}] SWE-agent init completed (elapsed: {elapsed:.2f}s)")

    async def _install_swe_agent(self):
        """Install SWE-agent and configure the environment."""

        sandbox_id = self._sandbox.sandbox_id
        start_time = time.time()

        logger.info(f"[{sandbox_id}] Step 1 started: SWE-agent installation")

        try:
            # Step 1: Create dedicated bash session
            step_start = time.time()
            logger.debug(f"[{sandbox_id}] Creating bash session: {self.agent_session}")
            await self._sandbox.create_session(
                CreateBashSessionRequest(
                    session=self.agent_session,
                    env_enable=True,
                    env=self.config.session_envs,
                )
            )
            elapsed_step = time.time() - step_start
            logger.info(f"[{sandbox_id}] Step 1 completed: Bash session created (elapsed: {elapsed_step:.2f}s)")

            # Step 2: Execute pre-startup commands
            step_start = time.time()
            for cmd in self.config.pre_startup_bash_cmd_list:
                await self._sandbox.arun(
                    cmd=cmd,
                    session=self.agent_session,
                )
            elapsed_step = time.time() - step_start
            logger.info(
                f"[{sandbox_id}] Step 2 completed: Pre-startup commands executed (elapsed: {elapsed_step:.2f}s)"
            )

            # Step 3: Create working directory
            step_start = time.time()
            mkdir_cmd = f"mkdir -p {self.config.swe_agent_workdir}"
            logger.debug(f"[{sandbox_id}] Command: {mkdir_cmd}")
            await self._sandbox.arun(
                cmd=mkdir_cmd,
                session=self.agent_session,
            )
            elapsed_step = time.time() - step_start
            logger.info(f"[{sandbox_id}] Step 3 completed: Working directory created (elapsed: {elapsed_step:.2f}s)")

            # Step 4: Install Python
            step_start = time.time()
            python_install_cmd = f"cd {self.config.swe_agent_workdir} && {self.config.python_install_cmd}"
            full_cmd = f"bash -c {shlex.quote(python_install_cmd)}"
            logger.debug(f"[{sandbox_id}] Command: {full_cmd}")

            await arun_with_retry(
                sandbox=self._sandbox,
                cmd=full_cmd,
                session=self.agent_session,
                mode="nohup",
                wait_timeout=self.config.python_install_timeout,
                error_msg="Python installation failed",
            )
            elapsed_step = time.time() - step_start
            logger.info(f"[{sandbox_id}] Step 4 completed: Python environment installed (elapsed: {elapsed_step:.2f}s)")

            # Step 5: Install SWE-agent
            step_start = time.time()
            swe_agent_install_cmd = (
                f"export PATH={self.config.swe_agent_workdir}/python/bin:$PATH && "
                f"cd {self.config.swe_agent_workdir} && "
                f"{self.config.swe_agent_install_cmd}"
            )
            full_cmd = f"bash -c {shlex.quote(swe_agent_install_cmd)}"
            logger.debug(f"[{sandbox_id}] Command: {full_cmd}")

            await arun_with_retry(
                sandbox=self._sandbox,
                cmd=full_cmd,
                session=self.agent_session,
                mode="nohup",
                wait_timeout=self.config.swe_agent_install_timeout,
                error_msg="SWE-agent installation failed",
            )
            elapsed_step = time.time() - step_start
            logger.info(
                f"[{sandbox_id}] Step 5 completed: SWE-agent repository installed (elapsed: {elapsed_step:.2f}s)"
            )

        except Exception as e:
            elapsed_total = time.time() - start_time
            logger.error(
                f"[{sandbox_id}] Operation failed: SWE-agent installation failed - {str(e)} "
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

            # Create ModelService instance
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

    @contextmanager
    def _config_template_context(self, problem_statement: str, project_path: str, instance_id: str):
        """Context manager for temporary config file generation and cleanup.

        Args:
            problem_statement: The problem statement for the task
            project_path: Path to the target project
            instance_id: The instance identifier for the run

        Yields:
            Path to the temporary config file
        """
        import copy
        import tempfile

        # Get the default template config from the config attribute
        template = self.config.default_run_single_config

        # Create a copy to avoid modifying the original
        new_config = copy.deepcopy(template)

        # Set output directory
        new_config["output_dir"] = f"/tmp_sweagent/{instance_id}"

        # Update project path
        if "env" in new_config and "repo" in new_config["env"]:
            new_config["env"]["repo"]["path"] = project_path
            # base_commit is set using default value in template

        # Update problem statement
        if "problem_statement" in new_config:
            new_config["problem_statement"]["text"] = problem_statement
            new_config["problem_statement"]["id"] = instance_id

        # Create a temporary config file using Python's tempfile
        temp_config_file = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=f"_{instance_id}_generated_config.yaml",
            delete=False,  # We'll manage the lifecycle through context manager
            encoding="utf-8",
        )

        temp_file_path = temp_config_file.name
        try:
            yaml.dump(new_config, temp_config_file, default_flow_style=False, allow_unicode=True)
            temp_config_file.close()  # Close the file so it can be read by other processes
            yield temp_file_path
        except Exception as e:
            # In exceptional cases, if file couldn't be processed, try to cleanup
            raise e
        finally:
            # Always cleanup the temporary file
            try:
                os.unlink(temp_file_path)
                logger.debug(f"Temporary config file cleaned up: {temp_file_path}")
            except OSError as e:
                logger.warning(f"Failed to clean up temporary config file {temp_file_path}: {str(e)}")

    async def run(
        self,
        problem_statement: str,
        project_path: str,
        instance_id: str,
        agent_run_timeout: int = 1800,
        agent_run_check_interval: int = 30,
    ) -> Observation:
        """Execute SWE-agent with the specified problem statement and project path.

        This method generates a configuration file from the default template,
        uploads it to the sandbox and executes SWE-agent. If ModelService is configured,
        it will be started and watch_agent will be called to monitor the agent process.

        Args:
            problem_statement: The problem statement for the task
            project_path: Path to the target project
            instance_id: The instance identifier for the run
            agent_run_timeout: Maximum seconds to wait for agent execution completion (default 1800)
            agent_run_check_interval: Seconds between status checks during execution (default 30)

        Returns:
            Observation: Execution result containing exit code, stdout, and stderr

        Raises:
            Exception: If agent execution fails
        """
        sandbox_id = self._sandbox.sandbox_id
        start_time = time.time()

        logger.info(f"[{sandbox_id}] SWE-agent execution started")

        try:
            with self._config_template_context(problem_statement, project_path, instance_id) as generated_config_path:
                config_filename = Path(generated_config_path).name

                step_start = time.time()
                target_path = f"{self.config.swe_agent_workdir}/{config_filename}"
                logger.debug(
                    f"[{sandbox_id}] UploadRequest(source_path={os.path.abspath(generated_config_path)}, "
                    f"target_path={target_path})"
                )

                await self._sandbox.upload(
                    UploadRequest(
                        source_path=os.path.abspath(generated_config_path),
                        target_path=target_path,
                    )
                )
                elapsed_step = time.time() - step_start
                logger.info(
                    f"[{sandbox_id}] Upload completed: Configuration file uploaded (elapsed: {elapsed_step:.2f}s)"
                )

                # Execute SWE-agent
                step_start = time.time()
                swe_agent_run_cmd = (
                    f"cd {self.config.swe_agent_workdir} && "
                    f"{self.config.swe_agent_workdir}/python/bin/sweagent run --config {config_filename}"
                )
                full_cmd = f"bash -c {shlex.quote(swe_agent_run_cmd)}"
                logger.debug(
                    f"[{sandbox_id}] Command: {full_cmd}\n"
                    f"Timeout: {agent_run_timeout}s, Check interval: {agent_run_check_interval}s"
                )

                result = await self._agent_run(
                    cmd=full_cmd,
                    session=self.agent_session,
                    wait_timeout=agent_run_timeout,
                    wait_interval=agent_run_check_interval,
                )
                elapsed_step = time.time() - step_start
                logger.info(f"[{sandbox_id}] SWE-agent execution completed (elapsed: {elapsed_step:.2f}s)")

            elapsed_total = time.time() - start_time

            if result and result.exit_code == 0:
                logger.info(
                    f"[{sandbox_id}] Agent Run completed: SWE-agent execution succeeded (elapsed: {elapsed_total:.2f}s)"
                )
            else:
                error_msg = result.failure_reason if result else "No result returned"
                logger.error(
                    f"[{sandbox_id}] Operation failed: SWE-agent execution failed - {error_msg} "
                    f"(elapsed: {elapsed_total:.2f}s)"
                )

            return result

        except Exception as e:
            elapsed_total = time.time() - start_time
            logger.error(
                f"[{sandbox_id}] Operation failed: SWE-agent execution failed - {str(e)} "
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
    ) -> Observation:
        """Execute agent command in nohup mode with optional ModelService watch.

        Starts the agent process and if ModelService is configured, calls watch_agent
        to monitor the process. The caller is responsible for the anti_call_llm loop
        and Whale API interactions.

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
