from __future__ import annotations

import json
import logging
from enum import Enum

from pydantic import BaseModel, Field

from rock.actions import CreateBashSessionRequest, ReadFileRequest, WriteFileRequest

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TrialResult(BaseModel):
    task_name: str
    status: JobStatus = JobStatus.COMPLETED
    score: float = 0.0
    rewards: dict[str, float] = Field(default_factory=dict)
    trajectory_path: str | None = None
    token_ids: list[int] = Field(default_factory=list)
    duration_sec: float = 0.0
    error: str | None = None


class JobResult(BaseModel):
    job_id: str
    status: JobStatus
    trials: list[TrialResult] = Field(default_factory=list)
    raw_output: str = ""
    exit_code: int = 0

    @property
    def score(self) -> float:
        if not self.trials:
            return 0.0
        return sum(t.score for t in self.trials) / len(self.trials)

    @property
    def n_completed(self) -> int:
        return sum(1 for t in self.trials if t.status == JobStatus.COMPLETED)

    @property
    def n_failed(self) -> int:
        return sum(1 for t in self.trials if t.status == JobStatus.FAILED)

    @classmethod
    def from_harbor_result(cls, result_json: str, job_id: str) -> JobResult:
        """Parse Harbor result.json content into JobResult."""
        data = json.loads(result_json)
        trials = []
        for tr in data.get("trial_results", []):
            has_error = tr.get("exception_info") is not None
            verifier = tr.get("verifier_result") or {}
            rewards = verifier.get("rewards", {})
            score = rewards.get("reward", 0.0) if rewards else 0.0

            # Parse duration from timestamps
            duration_sec = 0.0
            if tr.get("started_at") and tr.get("finished_at"):
                from datetime import datetime

                try:
                    start = datetime.fromisoformat(tr["started_at"].replace("Z", "+00:00"))
                    end = datetime.fromisoformat(tr["finished_at"].replace("Z", "+00:00"))
                    duration_sec = (end - start).total_seconds()
                except (ValueError, TypeError):
                    pass

            # Extract token_ids from agent_result.rollout_details if present
            token_ids = []
            agent_result = tr.get("agent_result") or {}
            for detail in agent_result.get("rollout_details", []):
                token_ids.extend(detail.get("completion_token_ids", []))

            trials.append(
                TrialResult(
                    task_name=tr.get("task_name", ""),
                    status=JobStatus.FAILED if has_error else JobStatus.COMPLETED,
                    score=score if not has_error else 0.0,
                    rewards=rewards,
                    token_ids=token_ids,
                    duration_sec=duration_sec,
                    error=tr.get("exception_info"),
                )
            )

        return cls(job_id=job_id, status=JobStatus.COMPLETED, trials=trials, raw_output=result_json, exit_code=0)


class Job:
    """Execute Harbor benchmark jobs inside Rock sandboxes.

    Serializes JobConfig to YAML, uploads to sandbox, runs `harbor jobs start`
    via nohup, and collects results by reading result.json.
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
        self._config_path = "/tmp/rock_job_config.yaml"

    async def _ensure_sandbox(self):
        """Create sandbox from config if not provided."""
        if self._sandbox is None:
            from rock.sdk.sandbox.client import Sandbox

            if self._config.sandbox is None:
                raise ValueError("Either pass sandbox= to Job() or set config.sandbox")
            self._sandbox = Sandbox(self._config.sandbox)

        if self._config.auto_start_sandbox:
            await self._sandbox.start()

    async def _setup_session(self):
        """Create bash session for job execution."""
        self._session = f"rock-job-{self._config.job_name}"
        await self._sandbox.create_session(CreateBashSessionRequest(session=self._session))

    async def _run_setup_commands(self):
        """Execute setup commands before harbor run."""
        for cmd in self._config.setup_commands:
            await self._sandbox.arun(cmd=cmd, session=self._session)

    async def _upload_config(self):
        """Serialize and upload Harbor config YAML to sandbox."""
        yaml_content = self._config.to_harbor_yaml()
        await self._sandbox.write_file(WriteFileRequest(content=yaml_content, path=self._config_path))

    async def _start_harbor(self) -> tuple[int, str]:
        """Start harbor jobs in nohup mode. Returns (pid, tmp_file)."""
        harbor_cmd = f"harbor jobs start -c {self._config_path}"
        tmp_file = f"/tmp/rock_job_{self._config.job_name}.out"

        pid, error = await self._sandbox.start_nohup_process(cmd=harbor_cmd, tmp_file=tmp_file, session=self._session)
        if error is not None:
            raise RuntimeError(f"Failed to start harbor job: {error.output}")
        return pid, tmp_file

    async def _collect_results(self, job_id: str) -> JobResult:
        """Read result.json from sandbox and parse into JobResult."""
        result_file = self._config.result_file
        if not result_file:
            result_file = f"{self._config.jobs_dir}/{self._config.job_name}/result.json"

        try:
            response = await self._sandbox.read_file(ReadFileRequest(path=result_file))
            return JobResult.from_harbor_result(response.content, job_id=job_id)
        except Exception as e:
            logger.warning(f"Failed to read result file {result_file}: {e}")
            return JobResult(job_id=job_id, status=JobStatus.FAILED, raw_output=str(e), exit_code=1)

    async def run(self) -> JobResult:
        """Execute the full job lifecycle: start -> setup -> harbor run -> collect results."""
        try:
            await self._ensure_sandbox()
            await self._setup_session()
            await self._run_setup_commands()
            await self._upload_config()

            pid, tmp_file = await self._start_harbor()
            self._pid = pid
            self._tmp_file = tmp_file

            # Wait for completion
            success, message = await self._sandbox.wait_for_process_completion(
                pid=pid,
                session=self._session,
                wait_timeout=int(self._config.timeout_multiplier * 3600),
                wait_interval=10,
            )

            # Get output
            await self._sandbox.handle_nohup_output(
                tmp_file=tmp_file, session=self._session, success=success, message=message
            )

            job_id = f"{self._config.job_name}-{pid}"
            return await self._collect_results(job_id)

        finally:
            if self._config.auto_stop_sandbox and self._sandbox:
                await self._sandbox.close()

    async def submit(self) -> str:
        """Async submit — start harbor and return job_id immediately."""
        await self._ensure_sandbox()
        await self._setup_session()
        await self._run_setup_commands()
        await self._upload_config()

        pid, tmp_file = await self._start_harbor()
        self._pid = pid
        self._tmp_file = tmp_file

        return f"{self._config.job_name}-{pid}"

    async def wait(self, job_id: str) -> JobResult:
        """Wait for a submitted job to complete and return results."""
        if self._pid is None or self._tmp_file is None:
            raise RuntimeError("No submitted job to wait for. Call submit() first.")

        success, message = await self._sandbox.wait_for_process_completion(
            pid=self._pid,
            session=self._session,
            wait_timeout=int(self._config.timeout_multiplier * 3600),
            wait_interval=10,
        )

        await self._sandbox.handle_nohup_output(
            tmp_file=self._tmp_file, session=self._session, success=success, message=message
        )

        result = await self._collect_results(job_id)

        if self._config.auto_stop_sandbox and self._sandbox:
            await self._sandbox.close()

        return result

    async def cancel(self, job_id: str):
        """Cancel a running job by killing the process."""
        if self._pid is None:
            raise RuntimeError("No submitted job to cancel.")
        await self._sandbox.arun(cmd=f"kill {self._pid}", session=self._session)
