# rock/admin/scheduler/task_registry.py
from rock.admin.scheduler.task_base import BaseTask


class TaskRegistry:
    """Task registry for managing scheduled tasks."""

    _tasks: dict[str, BaseTask] = {}

    @classmethod
    def register(cls, task: BaseTask):
        """Register a task."""
        cls._tasks[task.type] = task

    @classmethod
    def unregister(cls, task_type: str) -> BaseTask | None:
        """Unregister a task by type and return it."""
        return cls._tasks.pop(task_type, None)

    @classmethod
    def clear(cls):
        """Remove all registered tasks."""
        cls._tasks.clear()

    @classmethod
    def get_task(cls, name: str) -> BaseTask:
        """Get a task by name."""
        return cls._tasks.get(name)

    @classmethod
    def get_all_tasks(cls) -> dict[str, BaseTask]:
        """Get all registered tasks."""
        return cls._tasks.copy()
