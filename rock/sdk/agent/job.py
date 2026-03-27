"""Job SDK: 在 ROCK sandbox 内通过 harbor CLI 执行 benchmark 任务。

核心设计：将 setup + harbor run 归一化为单个 bash 脚本，通过 sandbox 的
nohup 协议（start_nohup_process / wait_for_process_completion / handle_nohup_output）
异步执行和等待。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from enum import Enum

from pydantic import BaseModel, Field

from rock.actions import CreateBashSessionRequest, ReadFileRequest

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

            duration_sec = 0.0
            if tr.get("started_at") and tr.get("finished_at"):
                from datetime import datetime

                try:
                    start = datetime.fromisoformat(tr["started_at"].replace("Z", "+00:00"))
                    end = datetime.fromisoformat(tr["finished_at"].replace("Z", "+00:00"))
                    duration_sec = (end - start).total_seconds()
                except (ValueError, TypeError):
                    pass

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


# ---------------------------------------------------------------------------
# 脚本模板
# ---------------------------------------------------------------------------

_RUN_SCRIPT_TEMPLATE = r"""#!/bin/bash
set -e
export PATH="/usr/local/bin:/usr/bin:/usr/sbin:/bin:/sbin:$PATH"

# ── 环境变量 ─────────────────────────────────────────────────────────
{env_exports}

# ── dockerd 检测与启动 ───────────────────────────────────────────────
if command -v docker &>/dev/null; then
    echo "docker OK: $(command -v docker)"
    if ! pgrep -x dockerd &>/dev/null; then
        echo "正在启动 dockerd..."
        nohup dockerd &>/var/log/dockerd.log &
    fi
    for i in $(seq 1 60); do
        if docker info &>/dev/null; then echo "dockerd 已就绪"; break; fi
        sleep 1
        if [ "$i" -eq 60 ]; then echo "WARN: dockerd 60 秒内未启动"; fi
    done
fi

# ── setup commands ───────────────────────────────────────────────────
{setup_commands}

# ── harbor run ───────────────────────────────────────────────────────
harbor jobs start -c {config_path}
"""


class Job:
    """在 ROCK sandbox 内运行 harbor 任务。

    将 setup_commands + harbor run 归一化为单个 bash 脚本，通过 sandbox
    nohup 协议执行：
    - ``run()``: 完整生命周期（阻塞等待）
    - ``submit()``: 启动后立即返回 job_id
    - ``wait()``: 等待已提交的 job
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
        """完整生命周期：start → 上传配置和脚本 → nohup 执行 → 等待 → 收集结果。"""
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
        """异步提交：上传配置和脚本 → nohup 启动 → 立即返回 job_id。"""
        await self._ensure_sandbox()
        await self._prepare_and_start()
        return self._config.job_name

    async def wait(self, job_id: str | None = None) -> JobResult:
        """等待已 submit 的 job 完成并返回结果。"""
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
        """取消运行中的 job。"""
        if self._pid is None:
            raise RuntimeError("No submitted job to cancel.")
        await self._sandbox.arun(cmd=f"kill {self._pid}", session=self._session)

    # ------------------------------------------------------------------
    # Private: 核心流程
    # ------------------------------------------------------------------

    async def _prepare_and_start(self):
        """上传文件 + harbor config YAML + 渲染运行脚本 → nohup 启动。"""
        await self._setup_session()

        # 1. 上传用户指定的文件/目录（如本地 clone 的 harbor 源码）
        for local_path, sandbox_path in self._config.file_uploads:
            logger.info(f"上传 {local_path} → {sandbox_path}")
            await self._sandbox.fs.upload_dir(local_path, sandbox_path)

        # 2. 上传 harbor config YAML
        config_path = f"/tmp/rock_job_{self._config.job_name}.yaml"
        yaml_content = self._config.to_harbor_yaml()
        await self._upload_content(yaml_content, config_path)
        logger.info(f"harbor 配置已上传: {config_path}")

        # 3. 渲染并上传运行脚本
        script_path = f"/tmp/rock_job_{self._config.job_name}.sh"
        script_content = self._render_run_script(config_path)
        await self._upload_content(script_content, script_path)
        logger.info(f"运行脚本已上传: {script_path}")

        # 4. nohup 启动脚本
        self._tmp_file = f"/tmp/rock_job_{self._config.job_name}.out"
        pid, error = await self._sandbox.start_nohup_process(
            cmd=f"bash {script_path}",
            tmp_file=self._tmp_file,
            session=self._session,
        )
        if error is not None:
            raise RuntimeError(f"启动 harbor 任务失败: {error.output}")
        self._pid = pid
        logger.info(f"harbor 任务已启动: pid={pid}, job_name={self._config.job_name}")

    def _render_run_script(self, config_path: str) -> str:
        """渲染完整的运行脚本（env + dockerd + setup_commands + harbor run）。"""
        # 环境变量
        env_lines = []
        for k, v in self._config.sandbox_env.items():
            escaped = v.replace("'", "'\\''")
            env_lines.append(f"export {k}='{escaped}'")
        env_block = "\n".join(env_lines) if env_lines else "# (no extra env vars)"

        # setup commands
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
            logger.info(f"沙箱已启动: sandbox_id={self._sandbox.sandbox_id}")

    async def _setup_session(self):
        self._session = f"rock-job-{self._config.job_name}"
        await self._sandbox.create_session(CreateBashSessionRequest(session=self._session))

    # ------------------------------------------------------------------
    # Private: 结果收集
    # ------------------------------------------------------------------

    async def _collect_results(self, job_id: str) -> JobResult:
        result_file = self._config.result_file
        if not result_file:
            result_file = f"{self._config.jobs_dir}/{self._config.job_name}/result.json"

        try:
            response = await self._sandbox.read_file(ReadFileRequest(path=result_file))
            return JobResult.from_harbor_result(response.content, job_id=job_id)
        except Exception as e:
            logger.warning(f"Failed to read result file {result_file}: {e}")
            return JobResult(job_id=job_id, status=JobStatus.FAILED, raw_output=str(e), exit_code=1)

    # ------------------------------------------------------------------
    # Private: 工具方法
    # ------------------------------------------------------------------

    async def _upload_content(self, content: str, sandbox_path: str) -> None:
        """将文本内容写入本地临时文件，通过 upload_by_path 上传到沙箱。"""
        local_tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".tmp", delete=False) as f:
                f.write(content)
                local_tmp = f.name
            result = await self._sandbox.upload_by_path(local_tmp, sandbox_path)
            if not result.success:
                raise RuntimeError(f"上传到 {sandbox_path} 失败: {result.message}")
        finally:
            if local_tmp and os.path.exists(local_tmp):
                os.remove(local_tmp)
