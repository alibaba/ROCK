from __future__ import annotations  # Postpone annotation evaluation to avoid circular imports.

import os
import shlex
import time
from collections.abc import Awaitable, Callable
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
    """Configuration dataclass for SWE-agent initialization and execution."""

    agent_type: Literal["swe-agent"] = "swe-agent"
    agent_session: str = "swe-agent-session"
    pre_startup_bash_cmd_list: list[str] = env_vars.ROCK_AGENT_PRE_STARTUP_BASH_CMD_LIST
    post_startup_bash_cmd_list: list[str] = []
    swe_agent_workdir: str = "/tmp_sweagent"
    python_install_cmd: str = env_vars.ROCK_AGENT_PYTHON_INSTALL_CMD
    swe_agent_install_cmd: str = "[ -d SWE-agent ] && rm -rf SWE-agent; git clone https://github.com/SWE-agent/SWE-agent.git && cd SWE-agent && pip install -e . -i https://mirrors.aliyun.com/pypi/simple/"
    python_install_timeout: int = 300
    swe_agent_install_timeout: int = 600
    default_run_single_config: dict[str, Any] = DEFAULT_RUN_SINGLE_CONFIG
    session_envs: dict[str, str] = {}


class SweAgent(Agent):
    """SWE-agent implementation for automated software engineering tasks."""

    def __init__(self, sandbox: Sandbox, config: SweAgentConfig):
        super().__init__(sandbox)
        self.config = config
        self.agent_session = self.config.agent_session

    async def init(self):
        """Initialize the SWE-agent environment within the sandbox."""

        sandbox_id = self._sandbox.sandbox_id
        start_time = time.time()

        logger.info(f"[{sandbox_id}] Step 0 completed: SWE-agent initialization started (elapsed: 0.00s)")

        await self._install_swe_agent()

        elapsed = time.time() - start_time
        logger.info(f"[{sandbox_id}] SWE-agent installation completed (elapsed: {elapsed:.2f}s)")

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
            logger.info(f"[{sandbox_id}] Step 1.1 completed: Bash session created (elapsed: {elapsed_step:.2f}s)")

            # Step 2: Execute pre-startup commands
            step_start = time.time()
            for cmd in self.config.pre_startup_bash_cmd_list:
                await self._sandbox.arun(
                    cmd=cmd,
                    session=self.agent_session,
                )
            elapsed_step = time.time() - step_start
            logger.info(
                f"[{sandbox_id}] Step 1.2 completed: Pre-startup commands executed (elapsed: {elapsed_step:.2f}s)"
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
            logger.info(f"[{sandbox_id}] Step 1.3 completed: Working directory created (elapsed: {elapsed_step:.2f}s)")

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
            logger.info(
                f"[{sandbox_id}] Step 1.4 completed: Python environment installed (elapsed: {elapsed_step:.2f}s)"
            )

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
                f"[{sandbox_id}] Step 1.5 completed: SWE-agent repository installed (elapsed: {elapsed_step:.2f}s)"
            )

            elapsed_total = time.time() - start_time
            logger.info(
                f"[{sandbox_id}] Step 1 completed: SWE-agent installation succeeded (elapsed: {elapsed_total:.2f}s)"
            )

        except Exception as e:
            elapsed_total = time.time() - start_time
            logger.error(
                f"[{sandbox_id}] Operation failed: SWE-agent installation failed - {str(e)} "
                f"(elapsed: {elapsed_total:.2f}s)",
                exc_info=True,
            )
            raise

    @contextmanager
    def _config_template_context(self, problem_statement: str, project_path: str, instance_id: str):
        """Context manager for temporary config file generation and cleanup."""
        import copy
        import tempfile

        template = self.config.default_run_single_config
        new_config = copy.deepcopy(template)

        # Set output directory
        new_config["output_dir"] = f"/tmp_sweagent/{instance_id}"

        # Update project path
        if "env" in new_config and "repo" in new_config["env"]:
            new_config["env"]["repo"]["path"] = project_path

        # Update problem statement
        if "problem_statement" in new_config:
            new_config["problem_statement"]["text"] = problem_statement
            new_config["problem_statement"]["id"] = instance_id

        # Create temporary config file
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
            logger.error(f"Failed to generate config file: {str(e)}", exc_info=True)
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
        on_start_hooks: list[Callable[[Sandbox, str], Awaitable[None]]] | None = None,
    ) -> Observation:
        """Execute SWE-agent with the specified problem statement and project path.

        Args:
            problem_statement: The problem statement for the task
            project_path: Path to the target project
            instance_id: The instance identifier for the run
            agent_run_timeout: Maximum seconds to wait for agent execution completion (default 1800)
            agent_run_check_interval: Seconds between status checks during execution (default 30)
            on_start_hooks: Optional list of async callback functions to execute after agent
                process starts. Each callback receives sandbox (Sandbox) and pid (str) as arguments.
                Callbacks are executed sequentially in the order provided. (default None)

        Returns:
            Observation: Execution result containing exit code, stdout, and stderr

        Example:
            ```python
            async def watch_hook(sandbox: Sandbox, pid: str):
                if sandbox.model_service:
                    await sandbox.model_service.watch_agent(pid=pid)

            result = await agent.run(
                "Fix the bug in login function",
                "/path/to/project",
                "task001",
                on_start_hooks=[watch_hook]
            )
            ```
        """
        sandbox_id = self._sandbox.sandbox_id
        start_time = time.time()

        logger.info(f"[{sandbox_id}] Step 2 started: SWE-agent execution")

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
                    f"[{sandbox_id}] upload completed: Configuration file uploaded (elapsed: {elapsed_step:.2f}s)"
                )

                # Execute SWE-agent with hooks
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

                result = await self._arun_nohup_with_hook(
                    cmd=full_cmd,
                    session=self.agent_session,
                    wait_timeout=agent_run_timeout,
                    wait_interval=agent_run_check_interval,
                    on_start_hooks=on_start_hooks,
                )
                elapsed_step = time.time() - step_start
                logger.info(
                    f"[{sandbox_id}] Step 2.2 completed: SWE-agent execution completed (elapsed: {elapsed_step:.2f}s)"
                )

            elapsed_total = time.time() - start_time

            if result and result.exit_code == 0:
                logger.info(
                    f"[{sandbox_id}] Step 2 completed: SWE-agent execution succeeded (elapsed: {elapsed_total:.2f}s)"
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

    async def _arun_nohup_with_hook(
        self,
        cmd: str,
        session: str,
        wait_timeout: int,
        wait_interval: int,
        on_start_hooks: list[Callable[[Sandbox, str], Awaitable[None]]] | None = None,
        response_limited_bytes_in_nohup: int | None = None,
        ignore_output: bool = False,
    ) -> Observation:
        """Execute command in nohup mode with optional on-start hooks."""

        try:
            timestamp = str(time.time_ns())
            tmp_file = f"/tmp/tmp_{timestamp}.out"

            # Start nohup process and get PID
            pid, error_response = await self._sandbox._start_nohup_process(cmd=cmd, tmp_file=tmp_file, session=session)

            # If nohup command itself failed, return the error response
            if error_response is not None:
                return error_response

            # If failed to extract PID
            if pid is None:
                msg = "Failed to submit command, nohup failed to extract PID"
                return Observation(output=msg, exit_code=1, failure_reason=msg)

            # Execute on-start hooks if provided
            if on_start_hooks:
                await self._execute_on_start_hooks(pid=str(pid), hooks=on_start_hooks)

            # Wait for process completion
            success, message = await self._sandbox._wait_for_process_completion(
                pid=pid, session=session, wait_timeout=wait_timeout, wait_interval=wait_interval
            )

            # Handle output
            return await self._sandbox._handle_nohup_output(
                tmp_file=tmp_file,
                session=session,
                success=success,
                message=message,
                ignore_output=ignore_output,
                response_limited_bytes_in_nohup=response_limited_bytes_in_nohup,
            )

        except ReadTimeout:
            error_msg = f"Command execution failed due to timeout: '{cmd}'. This may be caused by an interactive command that requires user input."
            return Observation(output=error_msg, exit_code=1, failure_reason=error_msg)
        except Exception as e:
            error_msg = f"Failed to execute nohup command '{cmd}': {str(e)}"
            return Observation(output=error_msg, exit_code=1, failure_reason=error_msg)

    async def _execute_on_start_hooks(
        self,
        pid: str,
        hooks: list[Callable[[Sandbox, str], Awaitable[None]]],
    ):
        """Execute on-start hooks sequentially."""
        sandbox_id = self._sandbox.sandbox_id
        total_start_time = time.time()

        logger.info(f"[{sandbox_id}] Executing {len(hooks)} on-start hook(s) for pid={pid}")
        for hook in hooks:
            hook_start_time = time.time()
            try:
                await hook(self._sandbox, pid)

                elapsed = time.time() - hook_start_time
                logger.info(f"[{sandbox_id}] On-start hook completed for pid {pid} (elapsed: {elapsed:.2f}s)")

            except Exception as e:
                elapsed = time.time() - hook_start_time
                logger.error(
                    f"[{sandbox_id}] On-start hook failed for pid {pid} - {str(e)} (elapsed: {elapsed:.2f}s)",
                    exc_info=True,
                )
                raise

        total_elapsed = time.time() - total_start_time
        logger.info(f"[{sandbox_id}] All on-start hooks completed for pid {pid} (total elapsed: {total_elapsed:.2f}s)")
