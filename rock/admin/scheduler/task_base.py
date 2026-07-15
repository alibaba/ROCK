# rock/admin/scheduler/task_base.py
import asyncio
import json
import os
import time
import traceback
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from rock import env_vars
from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.proto.request import SandboxReadFileRequest as ReadFileRequest
from rock.admin.proto.request import SandboxWriteFileRequest as WriteFileRequest
from rock.common.constants import SCHEDULER_LOG_NAME
from rock.deployments.constants import Port
from rock.logger import init_logger
from rock.sandbox.remote_sandbox import RemoteSandboxRuntime

logger = init_logger(name="task_base", file_name=SCHEDULER_LOG_NAME)

_MAX_ERROR_DETAIL_BYTES = 4096
_TRUNCATED_SUFFIX = "...[truncated]"


def _truncate_error_detail(error: object) -> str:
    detail = str(error)
    encoded = detail.encode("utf-8", errors="replace")
    if len(encoded) <= _MAX_ERROR_DETAIL_BYTES:
        return detail
    suffix = _TRUNCATED_SUFFIX.encode()
    truncated = encoded[: _MAX_ERROR_DETAIL_BYTES - len(suffix)].decode("utf-8", errors="ignore")
    return f"{truncated}{_TRUNCATED_SUFFIX}"


class IdempotencyType(Enum):
    """Idempotency type for task execution."""

    IDEMPOTENT = "idempotent"  # Safe to repeat
    NON_IDEMPOTENT = "non_idempotent"  # Requires status check


class TaskStatusEnum(str, Enum):
    """Task execution status."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class WorkerRunOutcome(str, Enum):
    SUCCESS = "success"
    STARTED = "started"
    SKIPPED = "skipped"
    FAILED = "failed"
    TIMEOUT = "timeout"


class TaskRunOutcome(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"
    NO_WORKERS = "no_workers"


@dataclass(slots=True)
class WorkerRunResult:
    worker_ip: str
    outcome: WorkerRunOutcome
    error_type: str | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome not in {WorkerRunOutcome.FAILED, WorkerRunOutcome.TIMEOUT}

    def to_report_dict(self) -> dict:
        return {
            "worker_ip": self.worker_ip,
            "outcome": self.outcome.value,
            "error_type": self.error_type,
            "error": self.error,
        }


@dataclass(slots=True)
class TaskRunReport:
    task_type: str
    timestamp: str
    duration_ms: float
    worker_results: list[WorkerRunResult]

    @property
    def total_count(self) -> int:
        return len(self.worker_results)

    def _count(self, outcome: WorkerRunOutcome) -> int:
        return sum(1 for result in self.worker_results if result.outcome == outcome)

    @property
    def started_count(self) -> int:
        return self._count(WorkerRunOutcome.STARTED)

    @property
    def skipped_count(self) -> int:
        return self._count(WorkerRunOutcome.SKIPPED)

    @property
    def timeout_count(self) -> int:
        return self._count(WorkerRunOutcome.TIMEOUT)

    @property
    def failed_count(self) -> int:
        """Count failed or timed-out workers; ``timeout_count`` is a subset."""
        return sum(1 for result in self.worker_results if not result.succeeded)

    @property
    def success_count(self) -> int:
        """Backward-compatible count of workers whose invocation did not fail."""
        return sum(1 for result in self.worker_results if result.succeeded)

    @property
    def outcome(self) -> TaskRunOutcome:
        if not self.worker_results:
            return TaskRunOutcome.NO_WORKERS
        if self.failed_count == self.total_count:
            return TaskRunOutcome.FAILED
        if self.failed_count:
            return TaskRunOutcome.PARTIAL
        if self.skipped_count == self.total_count:
            return TaskRunOutcome.SKIPPED
        return TaskRunOutcome.SUCCESS

    def to_report_dict(self) -> dict:
        success_results = [result for result in self.worker_results if result.succeeded]
        failed_results = [result for result in self.worker_results if not result.succeeded]
        return {
            "task_type": self.task_type,
            "timestamp": self.timestamp,
            "duration_ms": self.duration_ms,
            "outcome": self.outcome.value,
            "total": self.total_count,
            "success_count": self.success_count,
            "started_count": self.started_count,
            "skipped_count": self.skipped_count,
            "failed_count": self.failed_count,
            "timeout_count": self.timeout_count,
            "success_ips": [result.worker_ip for result in success_results],
            "failed_details": [
                {"ip": result.worker_ip, "reason": result.error or "unknown error"} for result in failed_results
            ],
            "worker_results": [result.to_report_dict() for result in self.worker_results],
        }


@dataclass
class TaskStatus:
    """Task execution status record."""

    task_name: str
    worker_ip: str
    pid: int | None = None
    status: TaskStatusEnum = TaskStatusEnum.PENDING
    last_run: str | None = None
    error: str | None = None
    extra: dict = field(default_factory=dict)

    def to_json(self) -> str:
        data = self.__dict__.copy()
        data["status"] = self.status.value
        return json.dumps(data, indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> "TaskStatus":
        data = json.loads(json_str)
        if "status" in data and isinstance(data["status"], str):
            data["status"] = TaskStatusEnum(data["status"])
        return cls(**data)


class BaseTask(ABC):
    """Abstract base class for scheduled tasks."""

    def __init__(
        self,
        type: str,
        interval_seconds: int,
        idempotency: IdempotencyType = IdempotencyType.IDEMPOTENT,
        process_name: str | None = None,
    ):
        self.type = type
        self.interval_seconds = interval_seconds
        self.idempotency = idempotency
        self.process_name = process_name
        self.status_file_path = f"{env_vars.ROCK_SCHEDULER_STATUS_DIR}/{type}_status.json"
        self._executor = ThreadPoolExecutor(max_workers=100)

    def _get_runtime(self, ip: str) -> RemoteSandboxRuntime:
        """Create a new RemoteSandboxRuntime instance for the given worker IP.

        Each call creates a new instance to ensure thread-safety in concurrent scenarios.
        The shared thread pool executor is reused across all instances.
        """
        return RemoteSandboxRuntime(
            host=ip,
            port=Port.PROXY.value,
            executor=self._executor,
        )

    @classmethod
    def from_config(cls, task_config) -> "BaseTask":
        """
        Create task instance from config. Subclasses may override for custom params.

        Args:
            task_config: TaskConfig object

        Returns:
            Task instance
        """
        return cls(
            interval_seconds=task_config.interval_seconds,
        )

    @abstractmethod
    async def run_action(self, runtime: RemoteSandboxRuntime) -> dict:
        """
        Run the task action. Must be implemented by subclasses.

        Args:
            runtime: RemoteSandboxRuntime instance for the worker

        Returns:
            Result dict, e.g. {"pid": 123, ...}
        """
        pass

    async def single_run(self, runtime: RemoteSandboxRuntime, ip: str) -> dict:
        """
        Run task on a single worker with unified status management.

        Status flow: PENDING -> RUNNING -> SUCCESS/FAILED
        """
        # Initialize status as PENDING
        status = TaskStatus(
            task_name=self.type,
            worker_ip=ip,
            status=TaskStatusEnum.PENDING,
            last_run=datetime.now().isoformat(),
        )
        await self.save_task_status(runtime, status)

        try:
            # Run the action
            result = await self.run_action(runtime)

            # Update status
            status.status = result.get("status")
            status.pid = result.get("pid")
            status.extra = result
            await self.save_task_status(runtime, status)

            return result

        except Exception as e:
            # Mark as FAILED
            status.status = TaskStatusEnum.FAILED
            status.error = str(e)
            await self.save_task_status(runtime, status)
            logger.exception(f"run action on worker[{ip}] error:[{e}]")
            raise e

    async def get_task_status(self, runtime: RemoteSandboxRuntime) -> TaskStatus | None:
        """Get task status from worker."""
        check_file_resp = await runtime.execute(
            Command(command=f"ls {self.status_file_path}", shell=True, sandbox_id="scheduler-task")
        )
        if check_file_resp.exit_code == 2:
            logger.info(f"task status file not exist: {self.status_file_path}")
            return None

        response = await runtime.read_file(ReadFileRequest(path=self.status_file_path, sandbox_id="scheduler-task"))
        if response.content:
            try:
                return TaskStatus.from_json(response.content)
            except Exception:
                pass
        return None

    async def save_task_status(self, runtime: RemoteSandboxRuntime, status: TaskStatus):
        """Save task status to worker file."""
        await runtime.write_file(
            WriteFileRequest(path=self.status_file_path, content=status.to_json(), sandbox_id="scheduler-task")
        )

    async def _clear_task_status(self, runtime: RemoteSandboxRuntime) -> None:
        """Remove the status file from worker."""
        await runtime.execute(
            Command(command=f"rm -f {self.status_file_path}", shell=True, sandbox_id="scheduler-task")
        )

    async def cleanup_on_worker(self, ip: str) -> None:
        """Stop any long-running process spawned by this task on a single worker.

        For idempotent tasks this is a no-op (no daemon process to kill).
        """
        if self.idempotency == IdempotencyType.IDEMPOTENT:
            return
        runtime = self._get_runtime(ip)
        status = await self.get_task_status(runtime)
        if status is None or not status.pid:
            return
        if await runtime.check_pid_exists(status.pid, sandbox_id="scheduler-task", process_name=self.process_name):
            kill_cmd = f"pkill -9 -P {status.pid}; kill -9 {status.pid}"
            await runtime.execute(Command(command=kill_cmd, shell=True, sandbox_id="scheduler-task"))
            logger.info(f"[{self.type}] killed pid {status.pid} on worker[{ip}]")
        await self._clear_task_status(runtime)

    async def cleanup(self, worker_ips: set[str], max_concurrency: int = 50) -> None:
        """Cleanup task across all workers, parallel and best-effort.

        Idempotent tasks return immediately. For non-idempotent tasks, kills the
        recorded daemon process and clears the status file on each worker. Failures
        on individual workers are logged but do not propagate.
        """
        if self.idempotency == IdempotencyType.IDEMPOTENT:
            return
        semaphore = asyncio.Semaphore(max_concurrency)

        async def cleanup_with_limit(ip: str) -> None:
            async with semaphore:
                try:
                    await self.cleanup_on_worker(ip)
                except Exception as e:
                    logger.warning(f"[{self.type}] cleanup failed on worker[{ip}]: {e}")

        await asyncio.gather(*[cleanup_with_limit(ip) for ip in worker_ips])

    async def should_run(self, runtime: RemoteSandboxRuntime) -> bool:
        """Determine if the task should be run."""
        if self.idempotency == IdempotencyType.IDEMPOTENT:
            return True

        # For non-idempotent tasks, check status
        status = await self.get_task_status(runtime)
        if status is None:
            return True

        # Check if process is still running
        if status.pid and status.status == TaskStatusEnum.RUNNING:
            pid_exists = await runtime.check_pid_exists(
                status.pid, sandbox_id="scheduler-task", process_name=self.process_name
            )
            if pid_exists:
                return False  # Process still running, skip
        if status.pid is None and status.status == TaskStatusEnum.FAILED:
            return False

        return True

    async def run_on_worker(self, ip: str):
        """Run task on a single worker."""
        runtime = self._get_runtime(ip)
        # Check if should run
        if not await self.should_run(runtime):
            return

        # Run task (status managed in single_run)
        logger.info(f"[{self.type}] start to run task on worker[{ip}]")
        return await self.single_run(runtime, ip)

    async def run(self, worker_ips: set[str], max_concurrency: int = 50) -> TaskRunReport:
        """Run task on all workers with concurrency control.

        Args:
            worker_ips: Set of worker IP addresses
            max_concurrency: Maximum number of concurrent tasks (default: 50)
        """
        started_at = time.perf_counter()
        semaphore = asyncio.Semaphore(max_concurrency)

        async def run_with_limit(ip: str) -> WorkerRunResult:
            async with semaphore:
                try:
                    result = await asyncio.wait_for(self.run_on_worker(ip), timeout=90)
                    if result is None:
                        return WorkerRunResult(worker_ip=ip, outcome=WorkerRunOutcome.SKIPPED)

                    raw_status = result.get("status")
                    status = raw_status.value if isinstance(raw_status, Enum) else raw_status
                    if status == TaskStatusEnum.FAILED.value:
                        return WorkerRunResult(
                            worker_ip=ip,
                            outcome=WorkerRunOutcome.FAILED,
                            error_type="TaskResultFailed",
                            error=_truncate_error_detail(result.get("error") or "task returned failed status"),
                        )
                    if status == TaskStatusEnum.RUNNING.value:
                        outcome = WorkerRunOutcome.STARTED
                    else:
                        outcome = WorkerRunOutcome.SUCCESS
                    return WorkerRunResult(
                        worker_ip=ip,
                        outcome=outcome,
                    )
                except TimeoutError:
                    return WorkerRunResult(
                        worker_ip=ip,
                        outcome=WorkerRunOutcome.TIMEOUT,
                        error_type="TimeoutError",
                        error=_truncate_error_detail(traceback.format_exc()),
                    )
                except Exception as exc:
                    return WorkerRunResult(
                        worker_ip=ip,
                        outcome=WorkerRunOutcome.FAILED,
                        error_type=type(exc).__name__,
                        error=_truncate_error_detail(traceback.format_exc()),
                    )

        tasks = [run_with_limit(ip) for ip in worker_ips]
        results = await asyncio.gather(*tasks)
        report = TaskRunReport(
            task_type=self.type,
            timestamp=datetime.now().isoformat(),
            duration_ms=(time.perf_counter() - started_at) * 1000,
            worker_results=results,
        )

        logger.info(
            f"[{self.type}] task completed: total={report.total_count}, "
            f"success={report.success_count}, failed={report.failed_count}, outcome={report.outcome.value}"
        )

        report_path = f"{env_vars.ROCK_SCHEDULER_STATUS_DIR}/{self.type}_run_report.json"
        try:
            os.makedirs(os.path.dirname(report_path), exist_ok=True)
            with open(report_path, "w", encoding="utf-8") as report_file:
                json.dump(report.to_report_dict(), report_file, indent=2, ensure_ascii=False)
            logger.info(f"[{self.type}] run report saved to {report_path}")
        except Exception as write_exc:
            logger.error(f"[{self.type}] failed to save run report: {write_exc}")
        return report
