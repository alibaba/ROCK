# rock/admin/scheduler/task_base.py
import asyncio
import json
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
    ):
        self.type = type
        self.interval_seconds = interval_seconds
        self.idempotency = idempotency
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
            logger.error(f"execute task on worker error:[{e}]")

    async def get_task_status(self, runtime: RemoteSandboxRuntime) -> TaskStatus | None:
        """Get task status from worker."""
        check_file_resp = await runtime.execute(Command(command=f"ls {self.status_file_path}", shell=True))
        if check_file_resp.exit_code == 2:
            logger.info(f"task status file not exist: {self.status_file_path}")
            return None

        response = await runtime.read_file(ReadFileRequest(path=self.status_file_path))
        if response.content:
            try:
                return TaskStatus.from_json(response.content)
            except Exception:
                pass
        return None

    async def save_task_status(self, runtime: RemoteSandboxRuntime, status: TaskStatus):
        """Save task status to worker file."""
        await runtime.write_file(WriteFileRequest(path=self.status_file_path, content=status.to_json()))

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
            pid_exists = await runtime.check_pid_exists(status.pid)
            if pid_exists:
                return False  # Process still running, skip
        if status.pid is None and status.status == TaskStatusEnum.FAILED:
            return False

        return True

    async def run_on_worker(self, ip: str) -> bool:
        """Run task on a single worker."""
        runtime = self._get_runtime(ip)
        try:
            # Check if should run
            if not await self.should_run(runtime):
                return True

            # Run task (status managed in single_run)
            await self.single_run(runtime, ip)
            return True

        except Exception:
            return False

    async def run(self, worker_ips: list[str], max_concurrency: int = 50):
        """Run task on all workers with concurrency control.

        Args:
            worker_ips: List of worker IP addresses
            max_concurrency: Maximum number of concurrent tasks (default: 50)
        """
        semaphore = asyncio.Semaphore(max_concurrency)

        async def run_with_limit(ip: str) -> bool:
            async with semaphore:
                return await self.run_on_worker(ip)

        tasks = [run_with_limit(ip) for ip in worker_ips]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return results
