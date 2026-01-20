from __future__ import annotations  # Postpone annotation evaluation to avoid circular imports.

import json
import os
import re
import shlex
import tempfile
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Literal

from typing_extensions import override

from rock import env_vars
from rock.actions import UploadRequest
from rock.logger import init_logger
from rock.sdk.sandbox.agent.rock_agent import RockAgent, RockAgentConfig
from rock.sdk.sandbox.runtime_env.config import NodeRuntimeEnvConfig

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

    agent_type: Literal["iflow-cli"] = "iflow-cli"
    """Type identifier for IFlow CLI agent."""

    iflow_cli_install_cmd: str = env_vars.ROCK_AGENT_IFLOW_CLI_INSTALL_CMD
    """Command to install iflow-cli in the sandbox."""

    iflow_settings: dict[str, Any] = DEFAULT_IFLOW_SETTINGS
    """Default settings for IFlow CLI configuration."""

    iflow_log_file: str = "~/.iflow/session_info.log"
    """Path to the IFlow session log file."""

    rt_env_config: NodeRuntimeEnvConfig = NodeRuntimeEnvConfig(
        npm_registry="https://registry.npmmirror.com",
    )
    """Node runtime environment configuration with npm registry."""

    session_envs: dict[str, str] = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    """Environment variables for the agent session."""


class IFlowCli(RockAgent):
    """IFlow CLI Agent implementation with NodeRuntimeEnv.

    Extends RockAgent to provide IFlow CLI specific functionality:
    - IFlow CLI installation via npm
    - Configuration directory creation
    - Settings file generation and upload
    - Session management for persistent execution
    """

    def __init__(self, sandbox: Sandbox, config: IFlowCliConfig):
        super().__init__(sandbox, config)
        self.config: IFlowCliConfig = config

    @override
    async def install(self):
        """Install IFlow CLI and configure the environment.

        Steps:
        1. Initialize Node runtime (npm/node) via super().install()
           - npm registry is configured automatically if specified in rt_env_config
        2. Install iflow-cli
        3. Create iflow configuration directories
        4. Upload settings configuration file
        """
        sandbox_id = self._sandbox.sandbox_id
        start_time = time.time()

        logger.info(f"[{sandbox_id}] Starting IFlow CLI installation")

        try:
            # Step 1: Initialize Node runtime via parent class
            await super().install()

            # Step 2: iflow-cli
            await self._install_iflow_cli_package()

            # Step 3: config dirs
            await self._create_iflow_directories()

            # Step 4: upload settings
            await self._upload_iflow_settings()

            elapsed = time.time() - start_time
            logger.info(f"[{sandbox_id}] IFlow CLI installation completed (elapsed: {elapsed:.2f}s)")

        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(
                f"[{sandbox_id}] IFlow CLI installation failed - {str(e)} (elapsed: {elapsed:.2f}s)",
                exc_info=True,
            )
            raise

    async def _install_iflow_cli_package(self):
        sandbox_id = self._sandbox.sandbox_id
        step_start = time.time()

        self._log_step("Installing iflow-cli", step_name="IFlow Install")
        logger.debug(f"[{sandbox_id}] IFlow CLI install command: {self.config.iflow_cli_install_cmd[:100]}...")

        iflow_cli_install_cmd = f"mkdir -p {self.config.agent_installed_dir} && cd {self.config.agent_installed_dir} && {self.config.iflow_cli_install_cmd}"

        # Use node runtime env to run install cmd (wrap is currently bash -c, but uses node_env session)
        await self.rt_env.run(
            cmd=iflow_cli_install_cmd,
            wait_timeout=self.config.agent_install_timeout,
            error_msg="iflow-cli installation failed",
        )

        elapsed_step = time.time() - step_start
        self._log_step(
            "IFlow CLI installation finished", step_name="IFlow Install", is_complete=True, elapsed=elapsed_step
        )

    async def _create_iflow_directories(self):
        sandbox_id = self._sandbox.sandbox_id
        step_start = time.time()

        self._log_step("Creating iflow settings directories", step_name="Create Directories")

        result = await self._sandbox.arun(
            cmd="mkdir -p /root/.iflow && mkdir -p ~/.iflow",
            session=self.agent_session,
        )

        if result.exit_code != 0:
            error_msg = f"Failed to create iflow directories: {result.output}"
            logger.error(f"[{sandbox_id}] {error_msg}")
            raise Exception(error_msg)

        elapsed_step = time.time() - step_start
        self._log_step(
            "IFlow configuration directories created",
            step_name="Create Directories",
            is_complete=True,
            elapsed=elapsed_step,
        )

    async def _upload_iflow_settings(self):
        sandbox_id = self._sandbox.sandbox_id
        step_start = time.time()

        self._log_step("Generating and uploading iflow settings", step_name="Upload Settings")

        with self._temp_iflow_settings_file() as temp_settings_path:
            await self._sandbox.upload(
                UploadRequest(
                    source_path=temp_settings_path,
                    target_path="/root/.iflow/settings.json",
                )
            )
            logger.debug(f"[{sandbox_id}] Settings uploaded to /root/.iflow/settings.json")

        elapsed_step = time.time() - step_start
        self._log_step(
            "IFlow settings configuration uploaded",
            step_name="Upload Settings",
            is_complete=True,
            elapsed=elapsed_step,
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

    @override
    async def create_agent_run_cmd(self, prompt: str) -> str:
        """Create IFlow run command (NOT wrapped by bash -c)."""
        sandbox_id = self._sandbox.sandbox_id

        session_id = await self._get_session_id_from_sandbox()
        if session_id:
            logger.info(f"[{sandbox_id}] Using existing session ID: {session_id}")
        else:
            logger.info(f"[{sandbox_id}] No previous session found, will start fresh execution")

        iflow_cmd = f'iflow -r "{session_id}" -p {shlex.quote(prompt)} --yolo > {self.config.iflow_log_file} 2>&1'

        return self.rt_env.wrap(f"mkdir -p {self.config.project_path} && cd {self.config.project_path} && {iflow_cmd}")
