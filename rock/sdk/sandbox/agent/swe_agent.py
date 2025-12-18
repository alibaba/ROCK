"""
SWE-agent Integration Module

This module provides integration with SWE-agent (Software Engineering Agent) for automated
software engineering tasks within a sandboxed environment. It handles the complete lifecycle
of SWE-agent including environment initialization, dependency installation, and execution.

Key Components:
    - SweAgentConfig: Configuration dataclass for SWE-agent setup parameters
    - SweAgent: Main agent implementation managing initialization and execution

Usage Example:
    ```python
    import yaml
    from rock.sdk.sandbox.client import Sandbox

    # Load configuration from file
    with open("path/to/default_config.yaml", "r", encoding="utf-8") as f:
        default_config_data = yaml.safe_load(f)

    # Create SweAgentConfig with custom configuration
    swe_agent_config = SweAgentConfig(
        agent_type="swe-agent",
        version="unknown",
        swe_agent_workdir="/tmp_sweagent",
        agent_session=self.agent_session,
        default_run_single_config=default_config_data
    )

    # Or use with default configuration
    # swe_agent_config = SweAgentConfig(
    #     agent_type="swe-agent",
    #     version="unknown",
    #     swe_agent_workdir="/tmp_sweagent",
    #     agent_session=self.agent_session
    #     # default_run_single_config will use its default value
    # )

    sandbox = Sandbox(...)
    sandbox.agent = SweAgent(sandbox, config)

    await sandbox.agent.init()
    await sandbox.agent.run("Fix the bug in login function", "/path/to/project", "task001")
    ```

Note:
    Currently supports LocalDeployment and RunSingleConfig modes only.
    Requires a Sandbox instance (not AbstractSandbox) for execution.
"""

import os
import shlex
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

import yaml

from rock import env_vars
from rock.actions.sandbox.base import AbstractSandbox
from rock.actions.sandbox.request import CreateBashSessionRequest, UploadRequest
from rock.logger import init_logger
from rock.sdk.sandbox.agent.base import Agent
from rock.sdk.sandbox.agent.config import AgentConfig
from rock.sdk.sandbox.client import Sandbox
from rock.utils import retry_async

logger = init_logger(__name__)


DEFAULT_SYSTEM_TEMPLATE = "You are a helpful assistant that can interact with a computer to solve tasks."

# Long string constants (only extract really long/complex strings)
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
    """
    Configuration dataclass for SWE-agent initialization and execution.

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


class SweAgent(Agent):
    """
    SWE-agent implementation for automated software engineering tasks.

    This class manages the complete lifecycle of SWE-agent including environment
    initialization, dependency installation, and task execution within a sandboxed
    environment. It provides an asynchronous interface for agent operations.

    Attributes:
        config: Configuration parameters for agent setup and execution
        agent_session: Name of the bash session used for agent operations

    Note:
        Currently requires a Sandbox instance (not AbstractSandbox).
        Only supports LocalDeployment and RunSingleConfig modes.
    """

    def __init__(self, sandbox: AbstractSandbox, config: SweAgentConfig):
        """
        Initialize SWE-agent with sandbox environment and configuration.

        Args:
            sandbox: Sandbox instance for isolated agent execution
            config: Configuration parameters for agent setup

        Raises:
            AssertionError: If sandbox is not an instance of Sandbox class
        """
        super().__init__(sandbox)
        self.config = config
        self.agent_session = self.config.agent_session

    async def init(self):
        """
        Initialize the SWE-agent environment within the sandbox.

        Performs the following initialization steps in sequence:
        1. Creates a dedicated bash session for agent execution
        2. Executes pre-startup configuration commands
        3. Creates working directory for agent installation
        4. Installs Python environment
        5. Clones and installs SWE-agent

        The initialization process is asynchronous and uses the configured
        timeouts for long-running operations like dependency installation.

        Raises:
            Exception: If any initialization step fails
        """
        assert isinstance(self._sandbox, Sandbox), "Sandbox must be an instance of Sandbox class"

        sandbox_id = self._sandbox.sandbox_id

        logger.info(f"[{sandbox_id}] Starting SWE-agent initialization")

        # Step 1: Create dedicated bash session for agent operations
        logger.info(f"[{sandbox_id}] Creating bash session: {self.agent_session}")
        await self._sandbox.create_session(
            CreateBashSessionRequest(
                session=self.agent_session,
                env_enable=True,
                env=self.config.session_envs,
            )
        )

        # Step 2: Execute pre-startup configuration commands
        logger.info(f"[{sandbox_id}] Executing {len(self.config.pre_startup_bash_cmd_list)} pre-startup commands")
        for idx, cmd in enumerate(self.config.pre_startup_bash_cmd_list, 1):
            logger.debug(f"→ Pre-startup command {idx}/{len(self.config.pre_startup_bash_cmd_list)}: {cmd[:100]}...")
            await self._sandbox.arun(
                cmd=cmd,
                session=self.agent_session,
            )

        # Step 3: Create working directory structure
        logger.info(f"[{sandbox_id}] Creating working directory: {self.config.swe_agent_workdir}")
        await self._sandbox.arun(
            cmd=f"mkdir -p {self.config.swe_agent_workdir}",
            session=self.agent_session,
        )

        # Step 4: Install Python environment with retry
        logger.info(f"[{sandbox_id}] Installing Python environment")

        python_install_cmd = f"cd {self.config.swe_agent_workdir} && {self.config.python_install_cmd}"
        await self._arun_with_retry(
            cmd=f"bash -c {shlex.quote(python_install_cmd)}",
            session=self.agent_session,
            mode="nohup",
            wait_timeout=self.config.python_install_timeout,
            error_msg="Python installation failed",
        )
        logger.info(f"[{sandbox_id}] Python installation completed")

        # Step 5: Install SWE-agent repository with retry
        # Note: Temporarily using standalone pip from installed Python
        logger.info(f"[{sandbox_id}] Installing SWE-agent from repository")

        swe_agent_install_cmd = f"export PATH={self.config.swe_agent_workdir}/python/bin:$PATH && cd {self.config.swe_agent_workdir} && {self.config.swe_agent_install_cmd}"
        await self._arun_with_retry(
            cmd=f"bash -c {shlex.quote(swe_agent_install_cmd)}",
            session=self.agent_session,
            mode="nohup",
            wait_timeout=self.config.swe_agent_install_timeout,
            error_msg="SWE-agent installation failed",
        )
        logger.info(f"[{sandbox_id}] SWE-agent installation completed successfully")

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
        Execute a command with retry logic based on exit code.

        Args:
            cmd: Command to execute
            session: Session name to execute command in
            mode: Execution mode (normal, nohup, etc.)
            wait_timeout: Timeout for command execution
            wait_interval: Check interval for nohup commands
            error_msg: Error message to use when raising exception

        Returns:
            Command result upon success
        """
        result = await self._sandbox.arun(
            cmd=cmd, session=session, mode=mode, wait_timeout=wait_timeout, wait_interval=wait_interval
        )
        # If exit_code is not 0, raise an exception to trigger retry
        if result.exit_code != 0:
            raise Exception(f"{error_msg} with exit code: {result.exit_code}, output: {result.output}")
        return result

    @contextmanager
    def _config_template_context(self, problem_statement: str, project_path: str, instance_id: str):
        """
        Context manager for temporary config file generation and cleanup.

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
                logger.debug(f"✓ Cleaned up temporary config file: {temp_file_path}")
            except OSError as e:
                logger.warning(f"⚠ Could not clean up temporary config file {temp_file_path}: {e}")

    async def run(
        self,
        problem_statement: str,
        project_path: str,
        instance_id: str,
        agent_run_timeout: int = 1800,
        agent_run_check_interval: int = 30,
    ):
        """
        Execute SWE-agent with the specified problem statement and project path.

        This method generates a configuration file from the default template,
        uploads it to the sandbox and executes SWE-agent with monitoring for completion.
        The execution runs in nohup mode with periodic status checks based on the configured interval.

        Args:
            problem_statement: The problem statement for the task
            project_path: Path to the target project
            instance_id: The instance identifier for the run
            agent_run_timeout: Maximum seconds to wait for agent execution completion (default 1800)
            agent_run_check_interval: Seconds between status checks during execution (default 30)

        Returns:
            CommandResult: Execution result containing exit code, stdout, and stderr

        Raises:
            AssertionError: If sandbox is not an instance of Sandbox class
            Exception: If file upload or command execution fails, or if default_run_single_config is not set

        Example:
            ```python
            result = await agent.run("Fix the bug in login function", "/path/to/project", "task001")
            if result.exit_code == 0:
                print("Agent completed successfully")
            ```
        """
        assert isinstance(self._sandbox, Sandbox), "Sandbox must be an instance of Sandbox class"

        # Use the context manager for temporary config file generation and cleanup
        with self._config_template_context(problem_statement, project_path, instance_id) as generated_config_path:
            logger.info(f"→ Starting SWE-agent execution with config: {generated_config_path}")

            config_filename = Path(generated_config_path).name

            # Upload configuration file to sandbox working directory
            logger.info(f"↑ Uploading configuration file: {config_filename}")
            await self._sandbox.upload(
                UploadRequest(
                    source_path=os.path.abspath(generated_config_path),
                    target_path=f"{self.config.swe_agent_workdir}/{config_filename}",
                )
            )
            logger.debug(f"✓ Configuration file uploaded to: {self.config.swe_agent_workdir}/{config_filename}")

            # Construct and execute SWE-agent run command
            swe_agent_run_cmd = f"cd {self.config.swe_agent_workdir} && {self.config.swe_agent_workdir}/python/bin/sweagent run --config {config_filename}"
            logger.info(
                f"▶ Executing SWE-agent (timeout: {agent_run_timeout}s, check interval: {agent_run_check_interval}s)"
            )

            result = await self._sandbox.arun(
                cmd=f"bash -c {shlex.quote(swe_agent_run_cmd)}",
                session=self.agent_session,
                mode="nohup",
                wait_timeout=agent_run_timeout,
                wait_interval=agent_run_check_interval,
            )

        # Log execution outcome
        if result and result.exit_code == 0:
            logger.info(f"✓ SWE-agent completed successfully (exit_code: {result.exit_code})")
        elif result:
            logger.error(f"✗ SWE-agent failed with exit_code: {result.exit_code}")
        else:
            logger.error("✗ SWE-agent execution failed - no result returned")

        return result
