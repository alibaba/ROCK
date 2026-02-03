# rock/admin/scheduler/task_factory.py
import importlib

from rock.admin.scheduler.task_base import BaseTask
from rock.admin.scheduler.task_registry import TaskRegistry
from rock.common.constants import SCHEDULER_LOG_NAME
from rock.config import SchedulerConfig, TaskConfig
from rock.logger import init_logger

logger = init_logger("task_factory", file_name=SCHEDULER_LOG_NAME)


class TaskFactory:
    """Task factory - dynamically creates and registers tasks from config."""

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
        # Dynamically load task class
        task_class = cls._load_task_class(task_config.task_class)

        # Create task instance with config params
        task = task_class.from_config(task_config)

        return task

    @classmethod
    def register_all_tasks(cls, scheduler_config: SchedulerConfig):
        """Register all enabled tasks from config."""
        for task_config in scheduler_config.tasks:
            if not task_config.enabled:
                logger.info(f"Task '{task_config.task_class}' is disabled, skipping")
                continue

            if not task_config.task_class:
                logger.warning(f"Task '{task_config.task_class}' has no task_class, skipping")
                continue

            try:
                task = cls.create_task(task_config)
                TaskRegistry.register(task)
                logger.info(
                    f"Registered task '{task.type}' with interval {task.interval_seconds}s"
                )
            except Exception as e:
                logger.error(f"Failed to create task '{task_config.task_class}': {e}")
