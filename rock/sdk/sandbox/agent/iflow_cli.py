from __future__ import annotations  # Postpone annotation evaluation to avoid circular imports.

import json
import os
import re
import shlex
import tempfile
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from pydantic import Field
from typing_extensions import override

from rock import env_vars
from rock.logger import init_logger
from rock.sdk.sandbox.agent.rock_agent import RockAgent, RockAgentConfig
from rock.sdk.sandbox.runtime_env import NodeRuntimeEnvConfig
from rock.sdk.sandbox.utils import with_time_logging

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox

logger = init_logger(__name__)


DEFAULT_IFLOW_SETTINGS: dict[str, Any] = {
    "selectedAuthType": "openai-compatible",
    "apiKey": "",
    "baseUrl": "",
    "modelName": "",
    "searchApiKey": "88888888",
    "disableAutoUpdate": True,
    "shellTimeout": 360000,
    "tokensLimit": 128000,
    "coreTools": [
        "Edit",
        "exit_plan_mode",
        "glob",
        "list_directory",
        "multi_edit",
        "plan",
        "read plan",
        "read_file",
        "read_many_files",
        "save_memory",
        "Search",
        "Shell",
        "task",
        "web_fetch",
        "web_search",
        "write_file",
        "xml_escape",
    ],
}


class IFlowCliConfig(RockAgentConfig):
    """IFlow CLI Agent Configuration."""

    agent_type: str = Field(default="iflow-cli")
    """OVERRIDE: Type identifier for IFlow CLI agent."""

    runtime_env_config: NodeRuntimeEnvConfig = Field(
        default_factory=lambda: NodeRuntimeEnvConfig(
            npm_registry="https://registry.npmmirror.com",
        )
    )
    """OVERRIDE: Node runtime environment configuration with npm registry."""

    env: dict[str, str] = Field(
        default_factory=lambda: {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
    )
    """OVERRIDE: Environment variables for the agent session."""

    iflow_cli_install_cmd: str = Field(default=env_vars.ROCK_AGENT_IFLOW_CLI_INSTALL_CMD)
    """Command to install iflow-cli in the sandbox."""

    iflow_settings: dict[str, Any] = Field(default_factory=lambda: DEFAULT_IFLOW_SETTINGS.copy())
    """Default settings for IFlow CLI configuration."""

    iflow_log_file: str = Field(default="~/.iflow/session_info.log")
    """Path to the IFlow session log file."""


class IFlowCli(RockAgent):
    """Specialized IFlowCLI implementation that automatically retrieves session_id from the sandbox
    for better checkpoint/resume support. It also supports custom settings files and configurations,
    making this implementation more convenient in certain scenarios."""

    def __init__(self, sandbox: Sandbox):
        super().__init__(sandbox)
        self.config: IFlowCliConfig | None = None

    @override
    @with_time_logging("Installing IFlow CLI")
    async def install(self, config: str | IFlowCliConfig) -> None:
        """Install IFlow CLI and configure the environment.

        Steps:
        1. Initialize Node runtime (npm/node) via super().install()
           - npm registry is configured automatically if specified in rt_env_config
        2. Install iflow-cli
        3. Create iflow configuration directories
        4. Upload settings configuration file
        """
        # Step 1: Initialize Node runtime via parent class
        await super().install(config)

        # Step 2: iflow-cli
        await self._install_iflow_cli_package()

        # Step 3: config dirs
        await self._create_iflow_directories()

        # Step 4: upload settings
        await self._upload_iflow_settings()

    @override
    async def _create_agent_run_cmd(self, prompt: str) -> str:
        """Create IFlow run command (NOT wrapped by bash -c)."""
        sandbox_id = self._sandbox.sandbox_id

        session_id = await self._get_session_id_from_sandbox()
        if session_id:
            logger.info(f"[{sandbox_id}] Using existing session ID: {session_id}")
        else:
            logger.info(f"[{sandbox_id}] No previous session found, will start fresh execution")

        iflow_cmd = f'iflow -r "{session_id}" -p {shlex.quote(prompt)} --yolo > {self.config.iflow_log_file} 2>&1'

        return self.runtime_env.wrapped_cmd(
            f"mkdir -p {self.config.project_path} && cd {self.config.project_path} && {iflow_cmd}"
        )

    @with_time_logging("Installing iflow-cli package")
    async def _install_iflow_cli_package(self):
        iflow_cli_install_cmd = f"mkdir -p {self.config.agent_installed_dir} && cd {self.config.agent_installed_dir} && {self.config.iflow_cli_install_cmd}"

        # Use node runtime env to run install cmd (wrap is currently bash -c, but uses node_env session)
        await self.runtime_env.run(
            cmd=iflow_cli_install_cmd,
            wait_timeout=self.config.agent_install_timeout,
            error_msg="iflow-cli installation failed",
        )

    async def _create_iflow_directories(self):
        result = await self._sandbox.arun(
            cmd="mkdir -p /root/.iflow && mkdir -p ~/.iflow",
            session=self.agent_session,
        )

        if result.exit_code != 0:
            error_msg = f"Failed to create iflow directories: {result.output}"
            logger.error(f"[{self._sandbox.sandbox_id}] {error_msg}")
            raise Exception(error_msg)

    async def _upload_iflow_settings(self):
        with self._temp_iflow_settings_file() as temp_settings_path:
            await self._sandbox.upload_by_path(
                file_path=temp_settings_path,
                target_path="/root/.iflow/settings.json",
            )

    @contextmanager
    def _temp_iflow_settings_file(self):
        settings_content = json.dumps(self.config.iflow_settings, indent=2)

        with tempfile.NamedTemporaryFile(mode="w", suffix="_iflow_settings.json", delete=False) as temp_file:
            temp_file.write(settings_content)
            temp_settings_path = temp_file.name

        try:
            yield temp_settings_path
        finally:
            os.unlink(temp_settings_path)

    async def _get_session_id_from_sandbox(self) -> str:
        sandbox_id = self._sandbox.sandbox_id
        logger.info(f"[{sandbox_id}] Retrieving session ID from sandbox log file")

        try:
            log_file_path = self.config.iflow_log_file
            result = await self._sandbox.arun(
                cmd=f"tail -1000 {log_file_path} 2>/dev/null || echo ''",
                session=self.agent_session,
            )

            log_content = result.output.strip()
            if not log_content:
                return ""

            return self._extract_session_id_from_log(log_content)

        except Exception as e:
            logger.error(f"[{sandbox_id}] Error retrieving session ID: {str(e)}")
            return ""

    def _extract_session_id_from_log(self, log_content: str) -> str:
        sandbox_id = self._sandbox.sandbox_id
        logger.debug(f"[{sandbox_id}] Attempting to extract session-id from log content")

        try:
            json_match = re.search(r"<Execution Info>\s*(.*?)\s*</Execution Info>", log_content, re.DOTALL)
            if not json_match:
                return ""

            json_str = json_match.group(1).strip()
            data = json.loads(json_str)
            session_id = data.get("session-id", "")
            if session_id:
                logger.info(f"[{sandbox_id}] Successfully extracted session-id: {session_id}")
            return session_id or ""

        except json.JSONDecodeError as e:
            logger.warning(f"[{sandbox_id}] Failed to parse JSON in Execution Info: {str(e)}")
            return ""
        except Exception as e:
            logger.warning(f"[{sandbox_id}] Error extracting session-id: {str(e)}")
            return ""
