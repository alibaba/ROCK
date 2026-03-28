"""Job SDK: Execute Harbor benchmark tasks inside ROCK sandboxes.

Core design: Unify setup + harbor run into a single bash script, executed via
the sandbox nohup protocol (start_nohup_process / wait_for_process_completion /
handle_nohup_output).
"""

from __future__ import annotations

import json
import os
import tempfile

from rock.actions import Command, CreateBashSessionRequest, ReadFileRequest
from rock.logger import init_logger
from rock.sdk.agent.models.job.result import JobResult, JobStatus
from rock.sdk.agent.models.trial.result import TrialResult

logger = init_logger(__name__)


# ---------------------------------------------------------------------------
# Script template
# ---------------------------------------------------------------------------

_RUN_SCRIPT_TEMPLATE = r"""#!/bin/bash
set -e

# ── Environment variables ────────────────────────────────────────────
{env_exports}

# ── Detect and start dockerd ─────────────────────────────────────────
if command -v docker &>/dev/null; then
    echo "docker OK: $(command -v docker)"
    if ! pgrep -x dockerd &>/dev/null; then
        echo "Starting dockerd..."
        nohup dockerd &>/var/log/dockerd.log &
    fi
    for i in $(seq 1 60); do
        if docker info &>/dev/null; then echo "dockerd is ready"; break; fi
        sleep 1
        if [ "$i" -eq 60 ]; then echo "WARN: dockerd failed to start within 60s"; fi
    done
fi

# ── Setup commands ───────────────────────────────────────────────────
{setup_commands}

# ── Harbor run ───────────────────────────────────────────────────────
harbor jobs start -c {config_path}
"""


class Job:
    """Execute Harbor benchmark tasks inside ROCK sandboxes.

    Unifies setup_commands + harbor run into a single bash script, executed
    via the sandbox nohup protocol:
    - ``run()``: Full lifecycle (blocking wait)
    - ``submit()``: Start and return job_id immediately
    - ``wait()``: Wait for a submitted job to complete
    """

    def __init__(self, config, sandbox=None):
        from rock.sdk.agent.models.job.config import JobConfig

        if not isinstance(config, JobConfig):
            raise TypeError(f"config must be JobConfig, got {type(config)}")
        self._config = config
        self._sandbox = sandbox
        self._session: str | None = None
        self._pid: int | None = None
        self._tmp_file: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> JobResult:
        """Full lifecycle: start sandbox -> upload config & script -> nohup execute -> wait -> collect results."""
        try:
            await self._ensure_sandbox()
            await self._prepare_and_start()

            success, message = await self._sandbox.wait_for_process_completion(
                pid=self._pid,
                session=self._session,
                wait_timeout=int(self._config.timeout_multiplier * 3600),
                wait_interval=10,
            )

            obs = await self._sandbox.handle_nohup_output(
                tmp_file=self._tmp_file,
                session=self._session,
                success=success,
                message=message,
                ignore_output=False,
                response_limited_bytes_in_nohup=None,
            )

            job_id = self._config.job_name
            result = await self._collect_results(job_id)
            result.raw_output = obs.output if obs else ""
            result.exit_code = obs.exit_code if obs else 1
            if not success:
                result.status = JobStatus.FAILED
            return result

        finally:
            if self._config.auto_stop_sandbox and self._sandbox:
                await self._sandbox.close()

    async def submit(self) -> str:
        """Async submit: upload config & script -> nohup start -> return job_id immediately."""
        await self._ensure_sandbox()
        await self._prepare_and_start()
        return self._config.job_name

    async def wait(self, job_id: str | None = None) -> JobResult:
        """Wait for a submitted job to complete and return results."""
        if self._pid is None or self._tmp_file is None:
            raise RuntimeError("No submitted job to wait for. Call submit() first.")

        success, message = await self._sandbox.wait_for_process_completion(
            pid=self._pid,
            session=self._session,
            wait_timeout=int(self._config.timeout_multiplier * 3600),
            wait_interval=10,
        )

        obs = await self._sandbox.handle_nohup_output(
            tmp_file=self._tmp_file,
            session=self._session,
            success=success,
            message=message,
            ignore_output=False,
            response_limited_bytes_in_nohup=None,
        )

        jid = job_id or self._config.job_name
        result = await self._collect_results(jid)
        result.raw_output = obs.output if obs else ""
        result.exit_code = obs.exit_code if obs else 1
        if not success:
            result.status = JobStatus.FAILED

        if self._config.auto_stop_sandbox and self._sandbox:
            await self._sandbox.close()

        return result

    async def cancel(self, job_id: str | None = None):
        """Cancel a running job by killing the process."""
        if self._pid is None:
            raise RuntimeError("No submitted job to cancel.")
        await self._sandbox.arun(cmd=f"kill {self._pid}", session=self._session)

    # ------------------------------------------------------------------
    # Private: core flow
    # ------------------------------------------------------------------

    async def _prepare_and_start(self):
        """Upload files + harbor config YAML + render run script -> nohup start."""
        await self._setup_session()

        # 1. Upload user-specified files/dirs
        for local_path, sandbox_path in self._config.file_uploads:
            logger.info(f"Uploading {local_path} -> {sandbox_path}")
            await self._sandbox.fs.upload_dir(local_path, sandbox_path)

        # 2. Upload harbor config YAML
        config_path = f"/tmp/rock_job_{self._config.job_name}.yaml"
        yaml_content = self._config.to_harbor_yaml()
        await self._upload_content(yaml_content, config_path)
        logger.info(f"Harbor config uploaded: {config_path}")

        # 3. Render and upload run script
        script_path = f"/tmp/rock_job_{self._config.job_name}.sh"
        script_content = self._render_run_script(config_path)
        await self._upload_content(script_content, script_path)
        logger.info(f"Run script uploaded: {script_path}")

        # 4. Start script via nohup
        self._tmp_file = f"/tmp/rock_job_{self._config.job_name}.out"
        pid, error = await self._sandbox.start_nohup_process(
            cmd=f"bash {script_path}",
            tmp_file=self._tmp_file,
            session=self._session,
        )
        if error is not None:
            raise RuntimeError(f"Failed to start harbor job: {error.output}")
        self._pid = pid
        logger.info(f"Harbor job started: pid={pid}, job_name={self._config.job_name}")

    def _render_run_script(self, config_path: str) -> str:
        """Render the full run script (env + dockerd + setup_commands + harbor run)."""
        # sandbox_env only — AgentConfig.env is injected by harbor via docker exec -e
        env_lines = []
        for k, v in self._config.sandbox_env.items():
            escaped = v.replace("'", "'\\''")
            env_lines.append(f"export {k}='{escaped}'")
        env_block = "\n".join(env_lines) if env_lines else "# (no extra env vars)"

        # Setup commands
        setup_lines = []
        for cmd in self._config.setup_commands:
            setup_lines.append(f"echo '>>> {cmd[:60]}...'")
            setup_lines.append(cmd)
        setup_block = "\n".join(setup_lines) if setup_lines else "echo 'No setup commands'"

        return _RUN_SCRIPT_TEMPLATE.format(
            env_exports=env_block,
            setup_commands=setup_block,
            config_path=config_path,
        )

    # ------------------------------------------------------------------
    # Private: sandbox / session
    # ------------------------------------------------------------------

    async def _ensure_sandbox(self):
        if self._sandbox is None:
            from rock.sdk.sandbox.client import Sandbox

            if self._config.sandbox_config is None:
                raise ValueError("Either pass sandbox= to Job() or set config.sandbox_config")
            self._sandbox = Sandbox(self._config.sandbox_config)

        if self._config.auto_start_sandbox:
            await self._sandbox.start()
            logger.info(f"Sandbox started: sandbox_id={self._sandbox.sandbox_id}")

    async def _setup_session(self):
        self._session = f"rock-job-{self._config.job_name}"
        await self._sandbox.create_session(CreateBashSessionRequest(session=self._session, env_enable=True))

    # ------------------------------------------------------------------
    # Private: result collection
    # ------------------------------------------------------------------

    async def _collect_results(self, job_id: str) -> JobResult:
        """Read trial-level result.json files from sandbox.

        Harbor's job-level result.json excludes trial_results, so we read
        each trial's result.json individually from subdirectories.
        """
        job_dir = f"{self._config.jobs_dir}/{self._config.job_name}"

        # List trial subdirectories via execute (not arun)
        try:
            list_result = await self._sandbox.execute(
                Command(command=["find", job_dir, "-mindepth", "2", "-maxdepth", "2", "-name", "result.json"])
            )
            trial_result_files = [
                line.strip() for line in (list_result.stdout or "").strip().split("\n") if line.strip()
            ]
        except Exception:
            trial_result_files = []

        # Parse each trial result
        trial_results: list[TrialResult] = []
        for trial_file in trial_result_files:
            try:
                response = await self._sandbox.read_file(ReadFileRequest(path=trial_file))
                data = json.loads(response.content)
                trial_results.append(TrialResult.from_harbor_json(data))
            except Exception as e:
                logger.warning(f"Failed to parse trial result {trial_file}: {e}")

        return JobResult(
            job_id=job_id,
            status=JobStatus.COMPLETED if trial_results else JobStatus.FAILED,
            trial_results=trial_results,
        )

    # ------------------------------------------------------------------
    # Private: utilities
    # ------------------------------------------------------------------

    async def _upload_content(self, content: str, sandbox_path: str) -> None:
        """Write text content to a local temp file and upload to sandbox via upload_by_path."""
        local_tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".tmp", delete=False) as f:
                f.write(content)
                local_tmp = f.name
            result = await self._sandbox.upload_by_path(local_tmp, sandbox_path)
            if not result.success:
                raise RuntimeError(f"Failed to upload to {sandbox_path}: {result.message}")
        finally:
            if local_tmp and os.path.exists(local_tmp):
                os.remove(local_tmp)
