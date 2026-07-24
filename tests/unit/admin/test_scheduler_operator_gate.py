from unittest.mock import MagicMock

from rock.admin.main import _init_ops_service
from rock.config import RockConfig, SchedulerConfig, TaskConfig


def test_opensandbox_ops_service_does_not_register_worker_tasks(monkeypatch):
    config = RockConfig()
    config.runtime.operator_type = "opensandbox"
    config.scheduler = SchedulerConfig(
        enabled=True,
        tasks=[
            TaskConfig(
                task_class="rock.admin.scheduler.tasks.image_cleanup_task.ImageCleanupTask",
            )
        ],
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("OpenSandbox ops service must not initialize Ray/Rocklet worker tasks")

    monkeypatch.setattr("rock.admin.main.TaskFactory.create_task", fail_if_called)
    monkeypatch.setattr("rock.admin.main.WorkerIPCache", fail_if_called)

    service = _init_ops_service(config, MagicMock())

    assert service._task_registry == {}
    assert service._alive_workers_provider() == set()
