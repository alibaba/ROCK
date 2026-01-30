# rock/admin/scheduler/task_registry.py
from rock.admin.scheduler.task_base import BaseTask


class TaskRegistry:
    """Task registry for managing scheduled tasks."""

    _tasks: dict[str, BaseTask] = {}

    @classmethod
    def register(cls, task: BaseTask):
        """Register a task."""
        cls._tasks[task.name] = task

    @classmethod
    def get_task(cls, name: str) -> BaseTask:
        """Get a task by name."""
        return cls._tasks.get(name)

    @classmethod
    def get_all_tasks(cls) -> dict[str, BaseTask]:
        """Get all registered tasks."""
        return cls._tasks.copy()
