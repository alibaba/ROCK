# rock/admin/scheduler/task_factory.py
import importlib

from rock.admin.scheduler.task_base import BaseTask
from rock.config import TaskConfig


class TaskFactory:
    """Task factory - dynamically creates tasks from config."""

    @staticmethod
    def _load_task_class(class_path: str) -> type[BaseTask]:
        """
        Dynamically load a task class.

        Args:
            class_path: Full class path, e.g. rock.admin.scheduler.tasks.image_cleanup_task.ImageCleanupTask

        Returns:
            Task class
        """
        module_path, class_name = class_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        return getattr(module, class_name)

    @classmethod
    def create_task(cls, task_config: TaskConfig) -> BaseTask:
        """
        Create task instance from config.

        Args:
            task_config: Task configuration

        Returns:
            Task instance
        """
        task_class = cls._load_task_class(task_config.task_class)
        return task_class.from_config(task_config)
