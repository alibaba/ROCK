"""Job SDK: Execute Harbor benchmark tasks inside ROCK sandboxes.

Core design: Unify setup + harbor run into a single bash script, executed via
the sandbox nohup protocol (start_nohup_process / wait_for_process_completion /
handle_nohup_output).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from rock.actions import CreateBashSessionRequest, ReadFileRequest

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result models (aligned with harbor.models.trial.result / job.result)
# ---------------------------------------------------------------------------


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExceptionInfo(BaseModel):
    """Aligned with harbor.models.trial.result.ExceptionInfo"""

    exception_type: str = ""
    exception_message: str = ""
    exception_traceback: str = ""
    occurred_at: str | None = None


class ModelInfo(BaseModel):
    """Aligned with harbor.models.trial.result.ModelInfo"""

    name: str = ""
    provider: str = ""


class AgentInfo(BaseModel):
    """Aligned with harbor.models.trial.result.AgentInfo"""

    name: str = ""
    version: str = ""
    model_info: ModelInfo | None = None


class VerifierResult(BaseModel):
    """Aligned with harbor.models.verifier.result.VerifierResult"""

    rewards: dict[str, float | int] | None = None


class AgentResult(BaseModel):
    """Aligned with harbor.models.agent.context.AgentContext (subset)"""

    n_input_tokens: int | None = None
    n_cache_tokens: int | None = None
    n_output_tokens: int | None = None
    cost_usd: float | None = None
    rollout_details: list[dict[str, Any]] | None = None


class TimingInfo(BaseModel):
    started_at: str | None = None
    finished_at: str | None = None


class TrialResult(BaseModel):
    """Aligned with harbor.models.trial.result.TrialResult"""

    task_name: str = ""
    trial_name: str = ""
    source: str | None = None
    agent_info: AgentInfo = Field(default_factory=AgentInfo)
    agent_result: AgentResult | None = None
    verifier_result: VerifierResult | None = None
    exception_info: ExceptionInfo | None = None
    started_at: str | None = None
    finished_at: str | None = None
    environment_setup: TimingInfo | None = None
    agent_setup: TimingInfo | None = None
    agent_execution: TimingInfo | None = None
    verifier: TimingInfo | None = None

    @property
    def score(self) -> float:
        if self.verifier_result and self.verifier_result.rewards:
            return self.verifier_result.rewards.get("reward", 0.0)
        return 0.0

    @property
    def status(self) -> JobStatus:
        return JobStatus.FAILED if self.exception_info else JobStatus.COMPLETED

    @property
    def duration_sec(self) -> float:
        if self.started_at and self.finished_at:
            try:
                start = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
                end = datetime.fromisoformat(self.finished_at.replace("Z", "+00:00"))
                return (end - start).total_seconds()
            except (ValueError, TypeError):
                pass
        return 0.0

    @property
    def token_ids(self) -> list[int]:
        if self.agent_result and self.agent_result.rollout_details:
            ids = []
            for detail in self.agent_result.rollout_details:
                ids.extend(detail.get("completion_token_ids", []))
            return ids
        return []

    @classmethod
    def from_harbor_json(cls, data: dict[str, Any]) -> TrialResult:
        """Parse a harbor trial-level result.json dict into TrialResult."""
        exception_info = None
        if data.get("exception_info"):
            ei = data["exception_info"]
            if isinstance(ei, dict):
                exception_info = ExceptionInfo(**ei)
            else:
                exception_info = ExceptionInfo(exception_type="unknown", exception_message=str(ei))

        agent_info_data = data.get("agent_info") or {}
        model_info = None
        if agent_info_data.get("model_info"):
            model_info = ModelInfo(**agent_info_data["model_info"])
        agent_info = AgentInfo(
            name=agent_info_data.get("name", ""),
            version=agent_info_data.get("version", ""),
            model_info=model_info,
        )

        verifier_result = None
        if data.get("verifier_result"):
            verifier_result = VerifierResult(**data["verifier_result"])

        agent_result = None
        if data.get("agent_result"):
            agent_result = AgentResult(**data["agent_result"])

        return cls(
            task_name=data.get("task_name", ""),
            trial_name=data.get("trial_name", ""),
            source=data.get("source"),
            agent_info=agent_info,
            agent_result=agent_result,
            verifier_result=verifier_result,
            exception_info=exception_info,
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            environment_setup=TimingInfo(**data["environment_setup"]) if data.get("environment_setup") else None,
            agent_setup=TimingInfo(**data["agent_setup"]) if data.get("agent_setup") else None,
            agent_execution=TimingInfo(**data["agent_execution"]) if data.get("agent_execution") else None,
            verifier=TimingInfo(**data["verifier"]) if data.get("verifier") else None,
        )


class JobResult(BaseModel):
    """Aligned with harbor.models.job.result.JobResult"""

    job_id: str = ""
    status: JobStatus = JobStatus.COMPLETED
    trial_results: list[TrialResult] = Field(default_factory=list)
    raw_output: str = ""
    exit_code: int = 0

    @property
    def score(self) -> float:
        if not self.trial_results:
            return 0.0
        scores = [t.score for t in self.trial_results]
        return sum(scores) / len(scores)

    @property
    def n_completed(self) -> int:
        return sum(1 for t in self.trial_results if t.status == JobStatus.COMPLETED)

    @property
    def n_failed(self) -> int:
        return sum(1 for t in self.trial_results if t.status == JobStatus.FAILED)


# ---------------------------------------------------------------------------
# Script template
# ---------------------------------------------------------------------------

_RUN_SCRIPT_TEMPLATE = r"""#!/bin/bash
set -e
export PATH="/usr/local/bin:/usr/bin:/usr/sbin:/bin:/sbin:$PATH"

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

        # 1. Upload user-specified files/dirs (e.g., locally cloned harbor source)
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
        await self._sandbox.create_session(CreateBashSessionRequest(session=self._session))

    # ------------------------------------------------------------------
    # Private: result collection
    # ------------------------------------------------------------------

    async def _collect_results(self, job_id: str) -> JobResult:
        """Read trial-level result.json files from sandbox.

        Harbor's job-level result.json excludes trial_results, so we read
        each trial's result.json individually from subdirectories.
        """
        job_dir = f"{self._config.jobs_dir}/{self._config.job_name}"

        # List trial subdirectories
        try:
            list_result = await self._sandbox.arun(
                cmd=f"find {job_dir} -mindepth 2 -maxdepth 2 -name result.json",
                session=self._session,
            )
            trial_result_files = [
                line.strip() for line in (list_result.output or "").strip().split("\n") if line.strip()
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
