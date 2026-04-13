"""Job SDK: Execute benchmark tasks inside ROCK sandboxes.

Unified concurrency model: each task gets its own sandbox.
- ``concurrency`` controls how many sandboxes run in parallel.
- Both harbor and rock-native agents use the same orchestration.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from typing import TYPE_CHECKING

from rock.actions import Command, CreateBashSessionRequest, ReadFileRequest
from rock.logger import init_logger
from rock.sdk.agent.constants import CHECK_INTERVAL, DEFAULT_WAIT_TIMEOUT, USER_DEFINED_LOGS
from rock.sdk.agent.models.job.result import JobResult, JobStatus
from rock.sdk.agent.models.trial.result import TrialResult

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox

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
    """Execute benchmark tasks inside ROCK sandboxes.

    Each task runs in its own sandbox. ``concurrency`` controls parallelism.

    Supports two agent modes via the ``type`` field of agents[0]:
    - **harbor** (default): Runs ``harbor jobs start -c`` with a single-task config.
    - **rock-native**: Uses ``sandbox.agent.install()`` + ``sandbox.agent.run()``.

    Public API:
    - ``run()``: Full lifecycle (submit + wait).
    - ``submit()``: Start all trials, return immediately.
    - ``wait()``: Block until all trials complete, return JobResult.
    - ``cancel()``: Cancel running trials.
    """

    def __init__(self, config):
        from rock.sdk.agent.models.job.config import JobConfig

        if not isinstance(config, JobConfig):
            raise TypeError(f"config must be JobConfig, got {type(config)}")
        self._config = config
        self._gather_task: asyncio.Task | None = None
        self._trial_results: list[TrialResult] = []

    @property
    def _is_rock_agent_mode(self) -> bool:
        from rock.sdk.sandbox.agent.rock_agent import RockAgentConfig

        return any(isinstance(a, RockAgentConfig) for a in self._config.agents)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> JobResult:
        """Full lifecycle: submit + wait."""
        await self.submit()
        return await self.wait()

    async def submit(self) -> None:
        """Start all trials as background asyncio tasks."""
        self._generate_default_job_name()
        tasks = self._collect_tasks()
        if not tasks:
            logger.warning("No tasks found in datasets")
            return

        logger.info(f"Job '{self._config.job_name}': {len(tasks)} tasks, concurrency={self._config.concurrency}")
        sem = asyncio.Semaphore(self._config.concurrency)
        self._gather_task = asyncio.ensure_future(
            asyncio.gather(
                *[self._run_one_trial(task, sem) for task in tasks],
                return_exceptions=True,
            )
        )

    async def wait(self) -> JobResult:
        """Wait for all trials to complete and return results."""
        if self._gather_task is None:
            raise RuntimeError("No submitted job. Call submit() first.")

        raw_results = await self._gather_task
        for r in raw_results:
            if isinstance(r, TrialResult):
                self._trial_results.append(r)
            elif isinstance(r, Exception):
                logger.error(f"Trial failed with exception: {r}")

        return JobResult(
            job_id=self._config.job_name,
            status=JobStatus.COMPLETED if self._trial_results else JobStatus.FAILED,
            labels=self._config.labels,
            trial_results=self._trial_results,
        )

    async def cancel(self) -> None:
        """Cancel all running trials."""
        if self._gather_task is not None and not self._gather_task.done():
            self._gather_task.cancel()

    # ------------------------------------------------------------------
    # Private: trial execution
    # ------------------------------------------------------------------

    async def _run_one_trial(self, task: str, sem: asyncio.Semaphore) -> TrialResult:
        """Run a single task in its own sandbox."""
        from rock.sdk.sandbox.client import Sandbox

        async with sem:
            sandbox = Sandbox(self._config.environment)
            try:
                await sandbox.start()
                logger.info(f"[{task}] sandbox={sandbox.sandbox_id}")
                await self._autofill_sandbox_info(sandbox)
                await self._upload_all(sandbox)

                if self._is_rock_agent_mode:
                    return await self._exec_rock_native(sandbox, task)
                else:
                    return await self._exec_harbor(sandbox, task)

            except Exception as exc:
                logger.error(f"[ERR] {task}: {exc}")
                return TrialResult(task_name=task, trial_name=f"{task}_trial0")
            finally:
                if self._config.environment.auto_stop:
                    try:
                        await sandbox.close()
                    except Exception:
                        pass

    async def _exec_rock_native(self, sandbox: Sandbox, task: str) -> TrialResult:
        """Execute a rock-native trial: install agent + run prompt."""
        from rock.sdk.sandbox.agent.rock_agent import RockAgentConfig

        rock_config = next(a for a in self._config.agents if isinstance(a, RockAgentConfig))
        assert isinstance(rock_config, RockAgentConfig)

        await sandbox.agent.install(rock_config)
        obs = await sandbox.agent.run(task)
        status = "OK" if obs.exit_code == 0 else "FAIL"
        logger.info(f"[{status}] {task} exit_code={obs.exit_code}")
        return TrialResult(task_name=task, trial_name=f"{task}_trial0")

    async def _exec_harbor(self, sandbox: Sandbox, task: str) -> TrialResult:
        """Execute a harbor trial: generate single-task config, run harbor CLI."""
        session = f"rock-job-{task}-{uuid.uuid4().hex[:8]}"
        await sandbox.create_session(
            CreateBashSessionRequest(
                session=session,
                env_enable=True,
                env=self._build_session_env(),
            )
        )

        harbor_yaml = self._make_single_task_harbor_yaml(task)
        config_path = f"{USER_DEFINED_LOGS}/rock_job_{task}.yaml"
        script_path = f"{USER_DEFINED_LOGS}/rock_job_{task}.sh"
        tmp_file = f"{USER_DEFINED_LOGS}/rock_job_{task}.out"

        await self._upload_content(sandbox, harbor_yaml, config_path)
        await self._upload_content(sandbox, self._render_run_script(config_path), script_path)

        pid, error = await sandbox.start_nohup_process(
            cmd=f"bash {script_path}",
            tmp_file=tmp_file,
            session=session,
        )
        if error is not None:
            raise RuntimeError(f"Failed to start harbor job for {task}: {error.output}")
        logger.info(f"[harbor] {task} started: pid={pid}, sandbox={sandbox.sandbox_id}")

        success, message = await sandbox.wait_for_process_completion(
            pid=pid,
            session=session,
            wait_timeout=self._get_wait_timeout(),
            wait_interval=CHECK_INTERVAL,
        )

        await sandbox.handle_nohup_output(
            tmp_file=tmp_file,
            session=session,
            success=success,
            message=message,
            ignore_output=False,
            response_limited_bytes_in_nohup=None,
        )

        return await self._collect_trial_result(sandbox, task)

    # ------------------------------------------------------------------
    # Private: harbor helpers
    # ------------------------------------------------------------------

    def _make_single_task_harbor_yaml(self, task: str) -> str:
        """Generate a harbor YAML config for a single task.

        Deep-copies the full config, preserves all dataset fields (path, registry, etc.),
        but overrides task_names to contain only the target task.
        """
        import yaml

        data = self._config.model_dump(mode="json", exclude={"environment", "concurrency"}, exclude_none=True)

        # Override datasets: keep all fields, only change task_names
        if "datasets" in data:
            for ds in data["datasets"]:
                ds["task_names"] = [task]
                ds.pop("exclude_task_names", None)
                ds.pop("n_tasks", None)

        # Add harbor environment fields
        harbor_env = self._config.environment.to_harbor_environment()
        if harbor_env:
            data["environment"] = harbor_env

        return yaml.dump(data, default_flow_style=False, allow_unicode=True)

    def _render_run_script(self, config_path: str) -> str:
        """Render the harbor run script."""
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

    async def _collect_trial_result(self, sandbox: Sandbox, task: str) -> TrialResult:
        """Read trial result.json from sandbox after harbor run."""
        job_dir = f"{self._config.jobs_dir}/{self._config.job_name}"
        try:
            list_result = await sandbox.execute(
                Command(command=["find", job_dir, "-mindepth", "2", "-maxdepth", "2", "-name", "result.json"])
            )
            for line in (list_result.stdout or "").strip().split("\n"):
                path = line.strip()
                if not path:
                    continue
                response = await sandbox.read_file(ReadFileRequest(path=path))
                data = json.loads(response.content)
                return TrialResult.from_harbor_json(data)
        except Exception as e:
            logger.warning(f"Failed to collect result for {task}: {e}")

        return TrialResult(task_name=task, trial_name=f"{task}_trial0")

    def _get_wait_timeout(self) -> int:
        """Infer wait timeout from agent config."""
        multiplier = self._config.timeout_multiplier or 1.0
        agents = self._config.agents
        if agents:
            agent = agents[0]
            agent_timeout = getattr(agent, "max_timeout_sec", None) or getattr(agent, "override_timeout_sec", None)
            if agent_timeout:
                return int(agent_timeout * multiplier) + 600
        return int(DEFAULT_WAIT_TIMEOUT * multiplier)

    # ------------------------------------------------------------------
    # Private: shared helpers
    # ------------------------------------------------------------------

    def _collect_tasks(self) -> list[str]:
        """Collect all task_names from datasets."""
        tasks = []
        for ds in self._config.datasets:
            if ds.task_names:
                tasks.extend(ds.task_names)
        return tasks

    async def _upload_all(self, sandbox: Sandbox) -> None:
        """Upload all configured files/dirs to a sandbox."""
        for local_path, sandbox_path in self._config.environment.uploads:
            logger.info(f"Uploading {local_path} -> {sandbox_path}")
            if os.path.isfile(local_path):
                result = await sandbox.upload_by_path(local_path, sandbox_path)
                if not result.success:
                    raise RuntimeError(f"Failed to upload file {local_path}: {result.message}")
            elif os.path.isdir(local_path):
                result = await sandbox.fs.upload_dir(local_path, sandbox_path)
                if result.exit_code != 0:
                    raise RuntimeError(f"Failed to upload dir {local_path}: {result.failure_reason}")
            else:
                raise FileNotFoundError(f"Upload source not found: {local_path}")

    async def _upload_content(self, sandbox: Sandbox, content: str, sandbox_path: str) -> None:
        """Upload text content to sandbox."""
        local_tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".tmp", delete=False) as f:
                f.write(content)
                local_tmp = f.name
            result = await sandbox.upload_by_path(local_tmp, sandbox_path)
            if not result.success:
                raise RuntimeError(f"Failed to upload to {sandbox_path}: {result.message}")
        finally:
            if local_tmp and os.path.exists(local_tmp):
                os.remove(local_tmp)

    def _build_session_env(self) -> dict[str, str] | None:
        """Merge OSS env vars with config env."""
        oss_env = {k: v for k, v in os.environ.items() if k.startswith("OSS")}
        merged = {**oss_env, **self._config.environment.env}
        return merged or None

    async def _autofill_sandbox_info(self, sandbox: Sandbox) -> None:
        """Sync namespace/experiment_id from sandbox."""
        sandbox_ns = sandbox._namespace
        if self._config.namespace is not None and sandbox_ns is not None:
            if self._config.namespace != sandbox_ns:
                raise ValueError(
                    f"namespace mismatch: JobConfig has '{self._config.namespace}', but sandbox returned '{sandbox_ns}'"
                )
        if sandbox_ns is not None:
            self._config.namespace = sandbox_ns

        sandbox_exp = sandbox._experiment_id
        if sandbox_exp is not None:
            if self._config.experiment_id is not None and self._config.experiment_id != sandbox_exp:
                raise ValueError(
                    f"experiment_id mismatch: JobConfig has '{self._config.experiment_id}', "
                    f"but sandbox returned '{sandbox_exp}'"
                )
            self._config.experiment_id = sandbox_exp

    def _generate_default_job_name(self) -> None:
        """Generate job_name if not set."""
        if self._config.job_name is not None:
            return

        parts = []
        if self._config.datasets:
            dataset = self._config.datasets[0]
            if hasattr(dataset, "name") and dataset.name:
                parts.append(dataset.name)
            task_names = dataset.task_names
            if task_names and len(task_names) == 1:
                parts.append(task_names[0])

        parts.append(uuid.uuid4().hex[:8])
        self._config.job_name = "_".join(parts)
        logger.info(f"Auto-generated job_name: {self._config.job_name}")
