from __future__ import annotations  # Postpone annotation evaluation to avoid circular imports.

import copy
import os
import shlex
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml

from rock import env_vars
from rock.actions import Observation, UploadRequest
from rock.logger import init_logger
from rock.sdk.sandbox.agent.base import DefaultAgent
from rock.sdk.sandbox.agent.config import DefaultAgentConfig
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
        "repo": {},
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


class SweAgentConfig(DefaultAgentConfig):
    """Configuration dataclass for SWE-agent initialization and execution.

    Inherits common agent configuration and adds SWE-agent specific settings.

    Attributes:
        agent_type: Fixed identifier for this agent type ("swe-agent")
        default_run_single_config: Default configuration object for a single run
        swe_agent_workdir: Working directory for agent installation and execution
        python_install_cmd: Command to install Python environment
        swe_agent_install_cmd: Command to clone and install SWE-agent repository
        python_install_timeout: Maximum seconds to wait for Python installation
        swe_agent_install_timeout: Maximum seconds to wait for SWE-agent installation
        agent_run_timeout: Maximum seconds to wait for agent execution completion
        agent_run_check_interval: Seconds between status checks during execution
    """

    agent_type: Literal["swe-agent"] = "swe-agent"

    agent_session: str = "swe-agent-session"

    pre_startup_bash_cmd_list: list[str] = env_vars.ROCK_AGENT_PRE_STARTUP_BASH_CMD_LIST

    post_startup_bash_cmd_list: list[str] = []

    swe_agent_workdir: str = "/tmp_sweagent"

    python_install_cmd: str = env_vars.ROCK_AGENT_PYTHON_INSTALL_CMD

    swe_agent_install_cmd: str = (
        "[ -d SWE-agent ] && rm -rf SWE-agent; "
        "git clone https://github.com/SWE-agent/SWE-agent.git && "
        "cd SWE-agent && pip install -e . -i https://mirrors.aliyun.com/pypi/simple/"
    )

    python_install_timeout: int = 300

    swe_agent_install_timeout: int = 600

    default_run_single_config: dict[str, Any] = DEFAULT_RUN_SINGLE_CONFIG

    session_envs: dict[str, str] = {}


class SweAgent(DefaultAgent):
    """SWE-agent implementation with integrated ModelService support.

    Manages the complete lifecycle of SWE-agent including environment
    initialization, dependency installation, and task execution within
    a sandboxed environment.
    """

    def __init__(self, sandbox: Sandbox, config: SweAgentConfig):
        """Initialize SWE-agent with sandbox environment and configuration.

        Args:
            sandbox: Sandbox instance for isolated agent execution
            config: Configuration parameters for agent setup
        """
        super().__init__(sandbox, config)

        self.config: SweAgentConfig = config

    async def _install(self):
        """Install SWE-agent and configure the environment.

        Steps:
        1. Create working directory
        2. Install Python environment
        3. Clone and install SWE-agent repository
        """
        sandbox_id = self._sandbox.sandbox_id
        start_time = time.time()

        logger.info(f"[{sandbox_id}] Starting SWE-agent installation")

        try:
            # Step 1: Create working directory
            await self._create_working_directory()

            # Step 2: Install Python
            await self._install_python()

            # Step 3: Install SWE-agent
            await self._install_swe_agent_package()

            elapsed = time.time() - start_time
            logger.info(f"[{sandbox_id}] SWE-agent installation completed (elapsed: {elapsed:.2f}s)")

        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(
                f"[{sandbox_id}] SWE-agent installation failed - {str(e)} (elapsed: {elapsed:.2f}s)",
                exc_info=True,
            )
            raise

    async def _create_working_directory(self):
        """Create working directory for SWE-agent."""
        sandbox_id = self._sandbox.sandbox_id
        step_start = time.time()

        self._log_step(f"Creating working directory: {self.config.swe_agent_workdir}", step_name="Create Workdir")

        mkdir_cmd = f"mkdir -p {self.config.swe_agent_workdir}"
        logger.debug(f"[{sandbox_id}] Command: {mkdir_cmd}")

        await self._sandbox.arun(
            cmd=mkdir_cmd,
            session=self.agent_session,
        )

        elapsed_step = time.time() - step_start
        self._log_step("Working directory created", step_name="Create Workdir", is_complete=True, elapsed=elapsed_step)

    async def _install_python(self):
        """Install Python environment."""
        sandbox_id = self._sandbox.sandbox_id
        step_start = time.time()

        self._log_step("Installing Python environment", step_name="Python Install")

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
        self._log_step(
            "Python environment installed", step_name="Python Install", is_complete=True, elapsed=elapsed_step
        )

    async def _install_swe_agent_package(self):
        """Clone and install SWE-agent repository."""
        sandbox_id = self._sandbox.sandbox_id
        step_start = time.time()

        self._log_step("Installing SWE-agent repository", step_name="SWE-agent Install")

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
        self._log_step(
            "SWE-agent repository installed",
            step_name="SWE-agent Install",
            is_complete=True,
            elapsed=elapsed_step,
        )

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
        # Create a copy to avoid modifying the original
        new_config = copy.deepcopy(self.config.default_run_single_config)

        # Set output directory
        new_config["output_dir"] = f"/tmp_sweagent/{instance_id}"

        # Update project path
        if "env" in new_config and "repo" in new_config["env"]:
            is_root_level = os.path.dirname(project_path) == "/"

            if is_root_level:
                repo_name = os.path.basename(project_path)
                new_config["env"]["repo"]["repo_name"] = repo_name
                new_config["env"]["repo"]["type"] = "preexisting"
            else:
                new_config["env"]["repo"]["path"] = project_path
                new_config["env"]["repo"]["type"] = "local"
            # base_commit is set using default value in template

        # Update problem statement
        if "problem_statement" in new_config:
            new_config["problem_statement"]["text"] = problem_statement
            new_config["problem_statement"]["id"] = instance_id

        # Create a temporary config file
        temp_config_file = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=f"_{instance_id}_generated_config.yaml",
            delete=False,
            encoding="utf-8",
        )

        temp_file_path = temp_config_file.name
        try:
            yaml.dump(new_config, temp_config_file, default_flow_style=False, allow_unicode=True)
            temp_config_file.close()
            yield temp_file_path
        except Exception as e:
            raise e
        finally:
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
            agent_run_timeout: Maximum seconds to wait for agent execution (default: 1800)
            agent_run_check_interval: Seconds between status checks (default: 30)

        Returns:
            Observation: Execution result containing exit code, stdout, and stderr

        Raises:
            Exception: If agent execution fails
        """
        sandbox_id = self._sandbox.sandbox_id
        start_time = time.time()

        logger.info(f"[{sandbox_id}] Starting SWE-agent run operation")
        logger.debug(
            f"[{sandbox_id}] Project path: {project_path}, Instance ID: {instance_id}, "
            f"Problem statement: {problem_statement[:100]}..."
        )

        try:
            with self._config_template_context(problem_statement, project_path, instance_id) as generated_config_path:
                config_filename = Path(generated_config_path).name

                # Upload configuration file
                step_start = time.time()
                target_path = f"{self.config.swe_agent_workdir}/{config_filename}"
                logger.debug(
                    f"[{sandbox_id}] UploadRequest(source_path={os.path.abspath(generated_config_path)}, "
                    f"target_path={target_path})"
                )

                self._log_step("Uploading configuration file", step_name="Upload Config")

                await self._sandbox.upload(
                    UploadRequest(
                        source_path=os.path.abspath(generated_config_path),
                        target_path=target_path,
                    )
                )
                elapsed_step = time.time() - step_start
                self._log_step(
                    "Configuration file uploaded",
                    step_name="Upload Config",
                    is_complete=True,
                    elapsed=elapsed_step,
                )

                # Execute SWE-agent
                step_start = time.time()
                self._log_step(
                    f"Running SWE-agent with timeout {agent_run_timeout}s",
                    step_name="SWE-agent Run",
                )

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
                self._log_step(
                    "SWE-agent execution completed",
                    step_name="SWE-agent Run",
                    is_complete=True,
                    elapsed=elapsed_step,
                )

            elapsed_total = time.time() - start_time

            if result and result.exit_code == 0:
                logger.info(
                    f"[{sandbox_id}] ✓ SWE-agent completed successfully "
                    f"(exit_code: {result.exit_code}, elapsed: {elapsed_total:.2f}s)"
                )
            else:
                error_msg = result.failure_reason if result else "No result returned"
                logger.error(f"[{sandbox_id}] ✗ SWE-agent failed - {error_msg} (elapsed: {elapsed_total:.2f}s)")

            return result

        except Exception as e:
            elapsed_total = time.time() - start_time
            logger.error(
                f"[{sandbox_id}] SWE-agent execution failed - {str(e)} (elapsed: {elapsed_total:.2f}s)",
                exc_info=True,
            )
            raise
