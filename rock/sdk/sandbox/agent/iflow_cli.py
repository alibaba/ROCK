from __future__ import annotations  # Postpone annotation evaluation to avoid circular imports.

import json
import os
import re
import shlex
import tempfile
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from rock import env_vars
from rock.actions import UploadRequest
from rock.logger import init_logger
from rock.sdk.sandbox.agent.base import BaseAgent
from rock.sdk.sandbox.agent.config import BaseAgentConfig
from rock.sdk.sandbox.agent.runtime_env import NodeAgentRuntimeEnv
from rock.sdk.sandbox.client import RunMode

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


class IFlowCliConfig(BaseAgentConfig):
    """IFlow CLI Agent Configuration."""

    agent_type: str = "iflow-cli"

    # Node runtime install
    npm_install_cmd: str = env_vars.ROCK_AGENT_NPM_INSTALL_CMD

    npm_install_timeout: int = 300

    # iflow-cli install
    iflow_cli_install_cmd: str = env_vars.ROCK_AGENT_IFLOW_CLI_INSTALL_CMD

    iflow_settings: dict[str, Any] = DEFAULT_IFLOW_SETTINGS

    # NOTE: keep same template; _create_agent_run_cmd will fill session_id/prompt/log_file
    iflow_run_cmd: str = "iflow -r {session_id} -p {problem_statement} --yolo > {iflow_log_file} 2>&1"

    iflow_log_file: str = "~/.iflow/session_info.log"

    session_envs: dict[str, str] = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


class IFlowCli(BaseAgent):
    """IFlow CLI Agent implementation with NodeAgentRuntimeEnv."""

    def __init__(self, sandbox: Sandbox, config: IFlowCliConfig):
        super().__init__(sandbox, config)
        self.config: IFlowCliConfig = config

        # runtime env maintains its own session
        self.node_env = NodeAgentRuntimeEnv(
            sandbox=self._sandbox,
            workdir=self.config.agent_installed_dir,
            node_install_cmd=self.config.npm_install_cmd,
            prepare_timeout=self.config.npm_install_timeout,
        )

    async def _install(self):
        """Install IFlow CLI and configure the environment.

        Steps:
        1. Prepare Node runtime (npm/node)
        2. Configure npm registry
        3. Install iflow-cli
        4. Create iflow configuration directories
        5. Upload settings configuration file
        """
        sandbox_id = self._sandbox.sandbox_id
        start_time = time.time()

        logger.info(f"[{sandbox_id}] Starting IFlow CLI installation")

        try:
            # Step 1: Node runtime
            await self._prepare_node_runtime()

            # Step 2: npm registry
            await self._configure_npm_registry()

            # Step 3: iflow-cli
            await self._install_iflow_cli_package()

            # Step 4: config dirs
            await self._create_iflow_directories()

            # Step 5: upload settings
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

    async def _prepare_node_runtime(self):
        step_start = time.time()

        self._log_step("Preparing Node runtime env (npm/node)", step_name="Node Runtime")
        await self.node_env.prepare()

        elapsed_step = time.time() - step_start
        self._log_step("Node runtime env prepared", step_name="Node Runtime", is_complete=True, elapsed=elapsed_step)

    async def _configure_npm_registry(self):
        """Configure npm to use mirror registry for faster downloads."""
        sandbox_id = self._sandbox.sandbox_id
        step_start = time.time()

        self._log_step("Configuring npm registry", step_name="NPM Registry")

        # registry config doesn't strictly need runtime env, but running under node_env session is fine.
        await self.node_env.ensure_session()
        result = await self._sandbox.arun(
            cmd="npm config set registry https://registry.npmmirror.com",
            session=self.node_env.session,
        )

        if result.exit_code != 0:
            logger.warning(f"[{sandbox_id}] Failed to set npm registry: {result.output}")
        else:
            logger.debug(f"[{sandbox_id}] Npm registry configured successfully")

        elapsed_step = time.time() - step_start
        self._log_step("NPM registry configured", step_name="NPM Registry", is_complete=True, elapsed=elapsed_step)

    async def _install_iflow_cli_package(self):
        sandbox_id = self._sandbox.sandbox_id
        step_start = time.time()

        self._log_step("Installing iflow-cli", step_name="IFlow Install")
        logger.debug(f"[{sandbox_id}] IFlow CLI install command: {self.config.iflow_cli_install_cmd[:100]}...")

        # Use node runtime env to run install cmd (wrap is currently bash -c, but uses node_env session)
        await self.node_env.run(
            cmd=self.config.iflow_cli_install_cmd,
            mode=RunMode.NOHUP,
            wait_timeout=self.config.npm_install_timeout,
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

    async def _create_agent_run_cmd(self, prompt: str) -> str:
        """Create IFlow run command (NOT wrapped by bash -c)."""
        sandbox_id = self._sandbox.sandbox_id

        workdir = self.config.workdir
        if not workdir:
            raise ValueError("IFlowCliConfig.project_path is required (moved from run() args into config).")

        session_id = await self._get_session_id_from_sandbox()
        if session_id:
            logger.info(f"[{sandbox_id}] Using existing session ID: {session_id}")
        else:
            logger.info(f"[{sandbox_id}] No previous session found, will start fresh execution")

        iflow_run_cmd = self.config.iflow_run_cmd.format(
            session_id=f'"{session_id}"',
            problem_statement=shlex.quote(prompt),
            iflow_log_file=self.config.iflow_log_file,
        )

        cmd = f"cd {shlex.quote(workdir)} && {iflow_run_cmd}"
        return cmd
