from __future__ import annotations  # Postpone annotation evaluation to avoid circular imports.

import copy
import os
import shlex
import tempfile
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Literal

import yaml

from rock import env_vars
from rock.actions import UploadRequest
from rock.logger import init_logger
from rock.sdk.sandbox.agent.base import BaseAgent
from rock.sdk.sandbox.agent.config import BaseAgentConfig
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


class SweAgentConfig(BaseAgentConfig):
    """SWE-agent configuration."""

    agent_type: Literal["swe-agent"] = "swe-agent"
    agent_session: str = "swe-agent-session"

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


class SweAgent(BaseAgent):
    """SWE-agent implementation (subclass only implements _install and _create_agent_run_cmd)."""

    GENERATED_CONFIG_NAME = "generated_config.yaml"

    def __init__(self, sandbox: Sandbox, config: SweAgentConfig):
        super().__init__(sandbox, config)
        self.config: SweAgentConfig = config

    async def _install(self):
        """Install SWE-agent and upload generated_config.yaml (static template)."""
        sandbox_id = self._sandbox.sandbox_id
        start_time = time.time()

        logger.info(f"[{sandbox_id}] Starting SWE-agent installation")

        try:
            await self._create_working_directory()
            await self._install_python()
            await self._install_swe_agent_package()
            await self._upload_generated_config_template()

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

    async def _upload_generated_config_template(self) -> None:
        """Generate and upload a static template config to swe_agent_workdir/generated_config.yaml.

        The prompt/problem_statement text will be injected at runtime via CLI args in _create_agent_run_cmd().
        """
        sandbox_id = self._sandbox.sandbox_id
        step_start = time.time()

        self._log_step("Generating and uploading SWE-agent config template", step_name="Upload Config")

        with self._generated_config_template_context() as local_path:
            target_path = f"{self.config.swe_agent_workdir}/{self.GENERATED_CONFIG_NAME}"

            await self._sandbox.upload(
                UploadRequest(
                    source_path=os.path.abspath(local_path),
                    target_path=target_path,
                )
            )

            logger.debug(f"[{sandbox_id}] Uploaded config template to {target_path}")

        elapsed_step = time.time() - step_start
        self._log_step(
            "Configuration template uploaded",
            step_name="Upload Config",
            is_complete=True,
            elapsed=elapsed_step,
        )

    @contextmanager
    def _generated_config_template_context(self):
        """Create a local temporary YAML config (template) for SWE-agent."""
        new_config = copy.deepcopy(self.config.default_run_single_config)

        # output_dir uses instance_id from config (moved from run args)
        if self.config.instance_id:
            new_config["output_dir"] = f"{self.config.swe_agent_workdir}/{self.config.instance_id}"
        else:
            new_config["output_dir"] = f"{self.config.swe_agent_workdir}/generated_output"

        # repo/project path uses project_path from config (moved from run args)
        project_path = self.config.project_path
        if "env" in new_config and "repo" in new_config["env"]:
            if project_path:
                is_root_level = os.path.dirname(project_path) == "/"
                if is_root_level:
                    repo_name = os.path.basename(project_path)
                    new_config["env"]["repo"]["repo_name"] = repo_name
                    new_config["env"]["repo"]["type"] = "preexisting"
                else:
                    new_config["env"]["repo"]["path"] = project_path
                    new_config["env"]["repo"]["type"] = "local"

        # problem_statement will be injected at runtime; keep empty here
        if "problem_statement" in new_config:
            new_config["problem_statement"]["text"] = ""
            new_config["problem_statement"]["id"] = self.config.instance_id or "generated"

        temp_config_file = tempfile.NamedTemporaryFile(
            mode="w",
            suffix="_generated_config.yaml",
            delete=False,
            encoding="utf-8",
        )
        temp_file_path = temp_config_file.name

        try:
            yaml.dump(new_config, temp_config_file, default_flow_style=False, allow_unicode=True)
            temp_config_file.close()
            yield temp_file_path
        finally:
            try:
                os.unlink(temp_file_path)
                logger.debug(f"Temporary config file cleaned up: {temp_file_path}")
            except OSError as e:
                logger.warning(f"Failed to clean up temporary config file {temp_file_path}: {str(e)}")

    async def _create_agent_run_cmd(self, prompt: str) -> str:
        """Create SWE-agent run command.

        Returns a shell command string (NOT wrapped by bash -c).
        Subclass is responsible for including cd ... && ...
        """
        # NOTE: CLI override syntax may need adjustment if sweagent uses a different flag format.
        prompt_arg = shlex.quote(prompt)

        cmd = (
            f"cd {self.config.swe_agent_workdir} && "
            f"{self.config.swe_agent_workdir}/python/bin/sweagent run "
            f"--config {self.GENERATED_CONFIG_NAME} "
            f"--problem_statement.text {prompt_arg}"
        )
        return cmd
