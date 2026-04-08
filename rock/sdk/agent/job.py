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
from rock.sdk.agent.constants import CHECK_INTERVAL, DEFAULT_WAIT_TIMEOUT, USER_DEFINED_LOGS
from rock.sdk.agent.models.job.result import JobResult, JobStatus
from rock.sdk.agent.models.trial.result import TrialResult

logger = init_logger(__name__)

# ---------------------------------------------------------------------------
# Script template
# ---------------------------------------------------------------------------

_RUN_SCRIPT_TEMPLATE = r"""#!/bin/bash
set -e

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

# ── Ensure output directory exists ──────────────────────────────────
mkdir -p {user_defined_dir}

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

    def __init__(self, config):
        from rock.sdk.agent.models.job.config import JobConfig

        if not isinstance(config, JobConfig):
            raise TypeError(f"config must be JobConfig, got {type(config)}")
        self._config = config
        self._sandbox = None
        self._session: str | None = None
        self._pid: int | None = None
        self._tmp_file: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> JobResult:
        """Full lifecycle: submit + wait."""
        await self.submit()
        return await self.wait()

    async def submit(self) -> None:
        """Start sandbox, upload config & script, nohup start harbor."""
        from rock.sdk.sandbox.client import Sandbox

        self._sandbox = Sandbox(self._config.environment)
        await self._sandbox.start()
        logger.info(f"Sandbox started: sandbox_id={self._sandbox.sandbox_id}, job_name={self._config.job_name}")

        await self._prepare_and_start()

    async def wait(self) -> JobResult:
        """Wait for a submitted job to complete and return results."""
        if self._pid is None or self._tmp_file is None:
            raise RuntimeError("No submitted job to wait for. Call submit() first.")

        try:
            success, message = await self._sandbox.wait_for_process_completion(
                pid=self._pid,
                session=self._session,
                wait_timeout=self._get_wait_timeout(),
                wait_interval=CHECK_INTERVAL,
            )

            obs = await self._sandbox.handle_nohup_output(
                tmp_file=self._tmp_file,
                session=self._session,
                success=success,
                message=message,
                ignore_output=False,
                response_limited_bytes_in_nohup=None,
            )

            result = await self._collect_results()
            result.raw_output = obs.output if obs else ""
            result.exit_code = obs.exit_code if obs else 1
            if not success:
                result.status = JobStatus.FAILED
            return result

        finally:
            if self._config.environment.auto_stop and self._sandbox:
                await self._sandbox.close()

    async def cancel(self):
        """Cancel a running job by killing the process."""
        if self._pid is None:
            raise RuntimeError("No submitted job to cancel.")
        await self._sandbox.arun(cmd=f"kill {self._pid}", session=self._session)

    # ------------------------------------------------------------------
    # Private: timeout
    # ------------------------------------------------------------------

    def _get_wait_timeout(self) -> int:
        """Infer wait timeout from agent config, with buffer for env setup + verifier."""
        multiplier = self._config.timeout_multiplier or 1.0
        agents = self._config.agents
        if agents:
            agent = agents[0]
            agent_timeout = agent.max_timeout_sec or agent.override_timeout_sec
            if agent_timeout:
                # agent_timeout * multiplier + 600s buffer for env setup, dataset download, verifier
                return int(agent_timeout * multiplier) + 600
        return int(DEFAULT_WAIT_TIMEOUT * multiplier)

    # ------------------------------------------------------------------
    # Private: core flow
    # ------------------------------------------------------------------

    async def _prepare_and_start(self):
        """Upload files + harbor config YAML + render run script -> nohup start."""
        await self._autofill_sandbox_info()
        await self._create_session()

        # 1. Upload user-specified files/dirs
        for local_path, sandbox_path in self._config.environment.file_uploads:
            logger.info(f"Uploading {local_path} -> {sandbox_path}")
            await self._sandbox.fs.upload_dir(local_path, sandbox_path)

        # 2. Upload harbor config YAML + run script
        config_path = f"{USER_DEFINED_LOGS}/rock_job_{self._config.job_name}.yaml"
        script_path = f"{USER_DEFINED_LOGS}/rock_job_{self._config.job_name}.sh"
        await self._upload_content(self._config.to_harbor_yaml(), config_path)
        await self._upload_content(self._render_run_script(config_path), script_path)
        logger.info(f"Config and script uploaded: {config_path}, {script_path}")

        # 3. Start script via nohup
        self._tmp_file = f"{USER_DEFINED_LOGS}/rock_job_{self._config.job_name}.out"
        pid, error = await self._sandbox.start_nohup_process(
            cmd=f"bash {script_path}",
            tmp_file=self._tmp_file,
            session=self._session,
        )
        if error is not None:
            raise RuntimeError(f"Failed to start harbor job: {error.output}")
        self._pid = pid
        logger.info(
            f"Harbor job started: pid={pid}, job_name={self._config.job_name}, sandbox_id={self._sandbox.sandbox_id}"
        )

    def _render_run_script(self, config_path: str) -> str:
        """Render the run script (dockerd + setup_commands + harbor run)."""
        # Setup commands
        setup_lines = []
        for cmd in self._config.environment.setup_commands:
            setup_lines.append(f"echo '>>> {cmd[:60]}...'")
            setup_lines.append(cmd)
        setup_block = "\n".join(setup_lines) if setup_lines else "echo 'No setup commands'"

        return _RUN_SCRIPT_TEMPLATE.format(
            setup_commands=setup_block,
            config_path=config_path,
            user_defined_dir=USER_DEFINED_LOGS,
        )

    # ------------------------------------------------------------------
    # Private: sandbox / session
    # ------------------------------------------------------------------

    def _build_session_env(self) -> dict[str, str] | None:
        """Merge OSS_* vars from the current process env with explicit config env.

        OSS credentials are forwarded from the process environment so that users
        do not need to write sensitive values in the YAML config file.
        Explicit values in config env always take precedence.
        """
        oss_env = {k: v for k, v in os.environ.items() if k.startswith("OSS")}
        merged = {**oss_env, **self._config.environment.env}
        return merged or None

    async def _create_session(self) -> None:
        """Create a bash session with sandbox_env injected."""
        self._session = f"rock-job-{self._config.job_name}"
        await self._sandbox.create_session(
            CreateBashSessionRequest(
                session=self._session,
                env_enable=True,
                env=self._build_session_env(),
            )
        )

    # ------------------------------------------------------------------
    # Private: result collection
    # ------------------------------------------------------------------

    async def _collect_results(self) -> JobResult:
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
            job_id=self._config.job_name,
            status=JobStatus.COMPLETED if trial_results else JobStatus.FAILED,
            labels=self._config.labels,
            trial_results=trial_results,
        )

    # ------------------------------------------------------------------
    # Private: utilities
    # ------------------------------------------------------------------

    async def _autofill_sandbox_info(self) -> None:
        sandbox_ns = self._sandbox._namespace
        if self._config.namespace is not None and sandbox_ns is not None:
            if self._config.namespace != sandbox_ns:
                raise ValueError(
                    f"namespace mismatch: JobConfig has '{self._config.namespace}', but sandbox returned '{sandbox_ns}'"
                )
        if sandbox_ns is not None:
            self._config.namespace = sandbox_ns

        sandbox_exp = self._sandbox._experiment_id
        if sandbox_exp is not None:
            if self._config.experiment_id is not None and self._config.experiment_id != sandbox_exp:
                raise ValueError(
                    f"experiment_id mismatch: JobConfig has '{self._config.experiment_id}', "
                    f"but sandbox returned '{sandbox_exp}'"
                )
            self._config.experiment_id = sandbox_exp

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
