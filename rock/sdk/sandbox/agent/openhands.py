"""
Solving software engineering(SWE) problem with [Openhands Benchmarks SDK](https://github.com/OpenHands/benchmarks.git).
Implementation framework reference: `rock/sdk/sandbox/agent/swe_agent.py`
"""
from __future__ import annotations

import copy
import json
import os
import shlex
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from rock import env_vars
from rock.actions import Observation, UploadRequest, WriteFileRequest
from rock.logger import init_logger
from rock.sdk.sandbox.agent.base import DefaultAgent
from rock.sdk.sandbox.agent.config import DefaultAgentConfig
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.utils import arun_with_retry

logger = init_logger(__name__)

# default prompt template in openhands
DEFAULT_PROMPT = """I have access to a python code repository in the directory {{ instance.repo_path }} . You can explore and modify files using the available tools. Consider the following issue description:

<issue_description>
{{ instance.problem_statement }}
</issue_description>

Can you help me implement the necessary changes to the repository so that the requirements specified in the <issue_description> are met?
I've already taken care of all changes to any of the test files described in the <issue_description>. This means you DON'T have to modify the testing logic or any of the tests in any way!
Also the development Python environment is already set up for you (i.e., all dependencies already installed), so you don't need to install other packages.
Your task is to make the minimal changes to non-test files in the {{ instance.repo_path }} directory to ensure the <issue_description> is satisfied.

Follow these phases to resolve the issue:

Phase 1. READING: read the problem and reword it in clearer terms
   1.1 If there are code or config snippets. Express in words any best practices or conventions in them.
   1.2 Hightlight message errors, method names, variables, file names, stack traces, and technical details.
   1.3 Explain the problem in clear terms.
   1.4 Enumerate the steps to reproduce the problem.
   1.5 Hightlight any best practices to take into account when testing and fixing the issue

Phase 2. RUNNING: install and run the tests on the repository
   2.1 Activate the environment by running
       ./opt/miniconda3/etc/profile.d/conda.sh ; conda activate testbed
   2.2 Follow the readme
   2.3 Install the environment and anything needed
   2.4 Iterate and figure out how to run the tests

Phase 3. EXPLORATION: find the files that are related to the problem and possible solutions
   3.1 Use `grep` to search for relevant methods, classes, keywords and error messages.
   3.2 Identify all files related to the problem statement.
   3.3 Propose the methods and files to fix the issue and explain why.
   3.4 From the possible file locations, select the most likely location to fix the issue.

Phase 4. TEST CREATION: before implementing any fix, create a script to reproduce and verify the issue.
   4.1 Look at existing test files in the repository to understand the test format/structure.
   4.2 Create a minimal reproduction script that reproduces the located issue.
   4.3 Run the reproduction script to confirm you are reproducing the issue.
   4.4 Adjust the reproduction script as necessary.

Phase 5. FIX ANALYSIS: state clearly the problem and how to fix it
   5.1 State clearly what the problem is.
   5.2 State clearly where the problem is located.
   5.3 State clearly how the test reproduces the issue.
   5.4 State clearly the best practices to take into account in the fix.
   5.5 State clearly how to fix the problem.

Phase 6. FIX IMPLEMENTATION: Edit the source code to implement your chosen solution.
   6.1 Make minimal, focused changes to fix the issue.

Phase 7. VERIFICATION: Test your implementation thoroughly.
   7.1 Run your reproduction script to verify the fix works.
   7.2 Add edge cases to your test script to ensure comprehensive coverage.
   7.3 Run existing tests related to the modified code to ensure you haven't broken anything.

8. FINAL REVIEW: Carefully re-read the problem description and compare your changes with the base commit {{ instance.base_commit }}.
   8.1 Ensure you've fully addressed all requirements.
   8.2 Run any tests in the repository related to:
     8.2.1 The issue you are fixing
     8.2.2 The files you modified
     8.2.3 The functions you changed
   8.3 If any tests fail, revise your implementation until all tests pass

Be thorough in your exploration, testing, and reasoning. It's fine if your thinking process is lengthy - quality and completeness are more important than brevity.
"""

DEFAULT_RUN_SINGLE_CONFIG = {
    # instance
    "instance": {
        "instance_id": "",
        "problem_statement": "",
        "repo": "",
        "base_commit": "",
        "image_name": "",
        "FAIL_TO_PASS": "",
        "remote_user": "/root",
        "project_path": "",  # repo data store path
        "script_folder": "/root",  # test script directory
    },
    # model service
    "llm": {
        "model": "",
        "base_url": "",
        "api_key": "",
        "num_retries": 100,
        "retry_multiplier": 8.0,
        "retry_min_wait": 8,
        "retry_max_wait": 64,
        "timeout": None,
        "max_message_chars": 1000000,
        "temperature": 0.3,
        "top_p": 0.75,
        "top_k": 100,
        "custom_llm_provider": None,
        "max_input_tokens": 134144,
        "max_output_tokens": 134144,
        "extra_headers": None,
        "stream": False,
        "caching_prompt": True,
        "log_completions": True,
        "log_completions_folder": "logs/completions",
        "custom_tokenizer": None,
        "native_tool_calling": True,
        "extended_thinking_budget": 200000,
    },
}


class OpenhandsConfig(DefaultAgentConfig):
    """Configuration dataclass for Openhands initialization and execution.

    This class defines all configurable parameters for setting up and running
    Openhands in a sandboxed environment, including installation commands,
    working directories, and execution timeouts.

    Attributes:
        agent_type: Fixed identifier for this agent type ("openhands")
        agent_session: Name of the bash session used for Openhands execution
        agent_workdir: Working directory for agent installation and execution
        python_install_cmd: Command to install Python environment
        openhands_sdk_install_cmd_list: Commands to clone and install Openhands/benchmarks repository
        python_install_timeout: Maximum seconds to wait for Python installation
        agent_install_timeout: Maximum seconds to wait for Openhands installation
        default_run_single_config: Default configuration object for a single run
        agent_prompt: user prompt
        max_iteration: max interactive turns with model service
    """

    agent_type: Literal["openhands"] = "openhands"

    agent_session: str = "openhands-rollout-session"

    # directory where Openhands will be installed
    agent_workdir: str = "/openhands"

    python_install_cmd: str = env_vars.ROCK_RTENV_PYTHON_V31212_INSTALL_CMD

    # Command to clone Openhands/benchmarks repository and install dependencies
    openhands_sdk_install_cmd_list: list[str] = [
        f"/openhands/runtime-env/bin/pip config set global.index-url {env_vars.ROCK_PIP_INDEX_URL}",
        "/openhands/runtime-env/bin/pip install openhands-agent-server==1.6.0 openhands-sdk==1.6.0",
        "/openhands/runtime-env/bin/pip install openhands-tools==1.6.0 openhands-workspace==1.6.0",
        "rm -rf /openhands/benchmarks",
        "git clone -b features/local_workspace_fix_early_stop https://github.com/shayue-wt/benchmarks.git /openhands/benchmarks",
        "/openhands/runtime-env/bin/pip install datasets huggingface-hub jinja2 pandas Pillow toml swebench",
        "/openhands/runtime-env/bin/pip install tqdm 'unidiff>=0.7.5,<0.8.0' 'modal>=1.1.4' commit0 pytest-json-report",
    ]

    python_install_timeout: int = 300

    agent_install_timeout: int = 600

    default_run_single_config: dict[str, Any] = DEFAULT_RUN_SINGLE_CONFIG

    session_envs: dict[str, str] = {}

    agent_prompt: str = DEFAULT_PROMPT

    max_iteration: int = 300


class Openhands(DefaultAgent):
    """
    Openhands implementation for automated software engineering tasks.

    This class handles the installation of [Openhands/benchmarks](https://github.com/OpenHands/benchmarks.git) library
    and modifies file [run_infer.py](https://github.com/OpenHands/benchmarks/tree/main/benchmarks/swebench/run_infer.py)
    to be compatible with SWE tasks other than SWE-Bench. It orchestrates rollout generation, patch retrieval,
    and result validation.
    """

    sandbox: Sandbox
    config: OpenhandsConfig

    def __init__(self, sandbox: Sandbox):
        """Initialize Agent with sandbox environment.

        Args:
            sandbox: Sandbox instance for isolated agent execution
        """
        super().__init__(sandbox)

        self.agent_prompt_path: str | None = None

    async def install(self, config: OpenhandsConfig) -> None:
        """Install and configure Openhands.

        Args:
            config: Configuration parameters for agent setup
        """
        await super().install(config)
        self.agent_prompt_path = f"{self.config.agent_workdir}/benchmarks/benchmarks/swebench/prompts/custom.j2"

    async def _install(self):
        """Install Openhands/benchmarks and configure the environment."""

        sandbox_id = self._sandbox.sandbox_id
        start_time = time.time()

        logger.info(f"[{sandbox_id}] Starting Openhands initialization")

        try:
            # Step 1: Create working directory
            step_start = time.time()
            mkdir_cmd = f"mkdir -p {self.config.agent_workdir}"
            logger.debug(f"[{sandbox_id}] Command: {mkdir_cmd}")
            await self._sandbox.arun(cmd=mkdir_cmd, session=self.agent_session)
            elapsed_step = time.time() - step_start
            logger.info(f"[{sandbox_id}] Step 1 completed: Working directory created (elapsed: {elapsed_step:.2f}s)")

            # Step 2: Install Python
            step_start = time.time()
            python_install_cmd = f"cd {self.config.agent_workdir} && {self.config.python_install_cmd}"
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
            logger.info(f"[{sandbox_id}] Step 2 completed: Python environment installed (elapsed: {elapsed_step:.2f}s)")

            # Step 3: Install Openhands/benchmarks
            step_start = time.time()
            full_cmd = f"bash -c {shlex.quote(' && '.join(self.config.openhands_sdk_install_cmd_list))}"
            logger.debug(f"[{sandbox_id}] Command: {full_cmd}")

            await arun_with_retry(
                sandbox=self._sandbox,
                cmd=full_cmd,
                session=self.agent_session,
                mode="nohup",
                wait_timeout=self.config.agent_install_timeout,
                error_msg="Openhands/benchmarks sdk installation failed",
            )
            elapsed_step = time.time() - step_start
            logger.info(
                f"[{sandbox_id}] Step 3 completed: Openhands/benchmarks installed (elapsed: {elapsed_step:.2f}s)"
            )

            # Step 4: Prepare configs
            await self._upload_config()
            logger.info(f"[{sandbox_id}] Step 4 completed: Done configuration (elapsed: {elapsed_step:.2f}s)")

        except Exception as e:
            elapsed_total = time.time() - start_time
            logger.error(
                f"[{sandbox_id}] Operation failed: Openhands installation failed - {str(e)} "
                f"(elapsed: {elapsed_total:.2f}s)",
                exc_info=True,
            )
            raise

    async def _upload_config(self):
        # Hijack Openhands/benchmarks
        sandbox_id = self._sandbox.sandbox_id

        config = self.config.default_run_single_config
        logger.debug(f"[{sandbox_id}] Config: {config}")

        if self.config.agent_prompt != DEFAULT_PROMPT:
            r = await self._sandbox.write_file(
                WriteFileRequest(content=self.config.agent_prompt, path=self.agent_prompt_path)
            )
            assert r.success, f"agent prompt write failed: {r.error}"
            logger.debug("agent prompt write successfully...")

        r = await self._sandbox.write_file(
            WriteFileRequest(
                content=json.dumps(config["llm"], indent=4),
                path=f"{self.config.agent_workdir}/benchmarks/.llm_config.json",
            )
        )
        assert r.success, f"llm configuration write failed: {r.error}"
        logger.debug("llm configuration write successfully...")

    @contextmanager
    def _config_template_context(
        self, problem_statement: str, project_path: str, instance_id: str, repo_name: str, base_commit: str
    ):
        """Context manager for temporary config file generation and cleanup.

        Args:
            problem_statement: The problem statement for the task
            project_path: Path to the target project
            instance_id: The instance identifier for the run
            repo_name: The name of repository
            base_commit: The base commit hash

        Yields:
            Path to the temporary config file
        """

        # Get the default template config from the config attribute
        template = self.config.default_run_single_config

        # Create a copy to avoid modifying the original
        instance_config = copy.deepcopy(template)["instance"]

        # Set output directory
        instance_config["instance_id"] = instance_id
        instance_config["base_commit"] = base_commit
        instance_config["problem_statement"] = problem_statement
        instance_config["repo_name"] = repo_name
        instance_config["project_path"] = project_path

        # Create a temporary config file using Python's tempfile
        temp_config_file = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=f"_{instance_id}.json",
            delete=False,  # We'll manage the lifecycle through context manager
            encoding="utf-8",
        )

        temp_file_path = temp_config_file.name
        try:
            json.dump(instance_config, temp_config_file, indent=4, ensure_ascii=False)
            temp_config_file.close()  # Close the file so it can be read by other processes
            yield temp_file_path
        except Exception as e:
            # In exceptional cases, if file couldn't be processed, try to clean up
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
    ) -> Observation:
        """Execute Openhands with the specified problem statement and project path.

        This method generates a configuration file from the default template,
        uploads it to the sandbox and executes Openhands. If ModelService is configured,
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
        instance_data = {
            "instance_id": instance_id,
            "problem_statement": problem_statement,
            "project_path": project_path,
            "repo_name": instance_id.split("-")[0].replace("__", "/"),
            "base_commit": "",
        }
        start_time = time.time()

        logger.info(f"[{sandbox_id}] Openhands execution started")

        try:
            with self._config_template_context(**instance_data) as generated_config_path:
                instance_config = Path(generated_config_path).name

                step_start = time.time()
                target_path = f"{self.config.agent_workdir}/benchmarks/{instance_config}"
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

                # Execute Openhands
                step_start = time.time()
                agent_run_cmd = (
                    f"cd {self.config.agent_workdir}/benchmarks && "
                    "export PYTHONPATH='.' && "
                    f"{self.config.agent_workdir}/runtime-env/bin/python "
                    "./benchmarks/swebench/run_infer.py "
                    ".llm_config.json --dataset eval --split test --note rock_rollout "
                    f"--select ./{instance_config} --max-iterations {self.config.max_iteration}"
                )
                if self.config.agent_prompt != DEFAULT_PROMPT:
                    agent_run_cmd += " --prompt-path benchmarks/swebench/prompts/custom.j2"

                full_cmd = f"bash -c {shlex.quote(agent_run_cmd)}"
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
                logger.info(f"[{sandbox_id}] Openhands execution completed (elapsed: {elapsed_step:.2f}s)")

                elapsed_total = time.time() - start_time

                if result and result.exit_code == 0:
                    logger.info(
                        f"[{sandbox_id}] Agent Run completed: Rollout execution succeeded (elapsed: {elapsed_total:.2f}s)"
                    )
                else:
                    error_msg = result.failure_reason if result else "No result returned"
                    logger.error(
                        f"[{sandbox_id}] Operation failed: Rollout execution failed - {error_msg} "
                        f"(elapsed: {elapsed_total:.2f}s)"
                    )

                return result

        except Exception as e:
            elapsed_total = time.time() - start_time
            logger.error(
                f"[{sandbox_id}] Operation failed: Rollout execution failed - {str(e)} "
                f"(elapsed: {elapsed_total:.2f}s)",
                exc_info=True,
            )
            raise
