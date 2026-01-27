from __future__ import annotations  # Postpone annotation evaluation to avoid circular imports.

import copy
import os
import shlex
import tempfile
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import Field
from typing_extensions import override

from rock.logger import init_logger
from rock.sdk.sandbox.agent.rock_agent import RockAgent, RockAgentConfig
from rock.sdk.sandbox.runtime_env import PythonRuntimeEnvConfig, RuntimeEnvConfig
from rock.sdk.sandbox.utils import with_time_logging

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


class SweAgentConfig(RockAgentConfig):
    """SWE-agent configuration."""

    agent_type: str = "swe-agent"
    """OVERRIDE: Type identifier for SWE-agent."""

    runtime_env_config: RuntimeEnvConfig | None = Field(default_factory=lambda: PythonRuntimeEnvConfig(version="3.12"))
    """OVERRIDE: Runtime environment configuration."""

    swe_agent_install_cmd: str = Field(
        default=(
            "[ -d SWE-agent ] && rm -rf SWE-agent; "
            "git clone https://github.com/SWE-agent/SWE-agent.git && "
            "cd SWE-agent && pip install -e ."
        )
    )
    """Command to install SWE-agent in the sandbox."""

    default_run_single_config: dict[str, Any] = Field(default_factory=lambda: DEFAULT_RUN_SINGLE_CONFIG.copy())
    """Default configuration for SWE-agent run_single mode."""


class SweAgent(RockAgent):
    """SWE-agent implementation with automatic pre_existing/local configuration.

    Automatically configures repo deployment type based on project_path and provides
    a default run_single_config for simplified usage.
    """

    GENERATED_CONFIG_NAME = "generated_config.yaml"
    """Filename for the generated SWE-agent configuration."""

    def __init__(self, sandbox: Sandbox):
        super().__init__(sandbox)
        self.config: SweAgentConfig | None = None

    @property
    def config_path(self) -> str:
        """Path to the generated SWE-agent configuration file in the sandbox."""
        return f"{self.config.agent_installed_dir}/{self.GENERATED_CONFIG_NAME}"

    @override
    @with_time_logging("Installing SWE-agent")
    async def install(self, config: str | SweAgentConfig) -> None:
        """Install SWE-agent after Python runtime environment is ready.

        This extends the parent install() to perform SWE-agent specific installation:
        1. Clones SWE-agent repository and installs it
        2. Generates and uploads the YAML configuration template
        """
        await super().install(config)

        swe_agent_install_cmd = (
            f"mkdir -p {self.config.agent_installed_dir} "
            f"&& cd {self.config.agent_installed_dir} "
            f"&& {self.config.swe_agent_install_cmd}"
        )

        await self.runtime_env.run(
            cmd=swe_agent_install_cmd,
            wait_timeout=self.config.agent_install_timeout,
            error_msg="SWE-agent installation failed",
        )

        await self._upload_generated_config_template()

    @override
    async def _create_agent_run_cmd(self, prompt: str) -> str:
        """Create the sweagent CLI command for running the agent.

        Returns a command that invokes sweagent with the generated config
        and injects the prompt as the problem_statement.

        Example:
            sweagent run --config /installed_agent/generated_config.yaml --problem_statement.text "fix this bug"
        """

        return (
            f"{self.runtime_env.bin_dir}/sweagent run "
            f"--config {self.config_path} "
            f"--problem_statement.text {shlex.quote(prompt)}"
        )

    @with_time_logging("Uploading SWE-agent config template")
    async def _upload_generated_config_template(self) -> None:
        """Generate and upload the SWE-agent configuration template.

        Creates a temporary YAML config file based on default_run_single_config,
        with dynamic values for output_dir and repository paths.
        The problem_statement text is injected at runtime via CLI args in _create_agent_run_cmd().
        """
        with self._generated_config_template_context() as local_path:
            await self._sandbox.upload_by_path(
                file_path=os.path.abspath(local_path),
                target_path=self.config_path,
            )

    @contextmanager
    def _generated_config_template_context(self):
        """Context manager to create a temporary SWE-agent YAML config file.

        Populates the config template with:
        - output_dir: Based on agent_installed_dir and instance_id
        - repo: Based on project_path (local path or preexisting)
        - problem_statement: Empty (injected at runtime)

        Yields the path to the temporary file, which is cleaned up on exit.
        """
        new_config = copy.deepcopy(self.config.default_run_single_config)

        # output_dir uses instance_id from config
        new_config["output_dir"] = f"{self.config.agent_installed_dir}/{self.config.instance_id}"

        # repo/project path uses project_path from config
        project_path = self.config.project_path
        if "env" in new_config and "repo" in new_config["env"]:
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
            new_config["problem_statement"]["id"] = self.config.instance_id

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
