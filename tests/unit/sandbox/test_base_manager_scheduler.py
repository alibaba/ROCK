from unittest.mock import MagicMock, patch

from rock.sandbox.base_manager import BaseManager


class _ConcreteManager(BaseManager):
    async def _auto_transition(self): ...

    async def _auto_stop_expired(self): ...

    async def _reconcile(self): ...


def _manager() -> _ConcreteManager:
    manager = _ConcreteManager.__new__(_ConcreteManager)
    manager._auto_transition_interval = 180
    manager._reconcile_interval = 30
    return manager


@patch("rock.sandbox.base_manager.AsyncIOScheduler")
@patch("rock.sandbox.base_manager.is_primary_pod", return_value=True)
def test_primary_registers_full_lifecycle_and_reconcile(mock_is_primary, mock_scheduler_cls):
    scheduler = MagicMock()
    mock_scheduler_cls.return_value = scheduler

    manager = _manager()
    manager._setup_job_check_scheduler()

    assert [call.kwargs["id"] for call in scheduler.add_job.call_args_list] == ["auto_transition", "reconcile"]
    assert scheduler.add_job.call_args_list[0].kwargs["func"] == manager._auto_transition
    scheduler.start.assert_called_once_with()
    mock_is_primary.assert_called_once_with()


@patch("rock.sandbox.base_manager.AsyncIOScheduler")
@patch("rock.sandbox.base_manager.is_primary_pod", return_value=False)
def test_non_primary_registers_only_auto_stop_expired(mock_is_primary, mock_scheduler_cls):
    scheduler = MagicMock()
    mock_scheduler_cls.return_value = scheduler

    manager = _manager()
    manager._setup_job_check_scheduler()

    scheduler.add_job.assert_called_once()
    job = scheduler.add_job.call_args.kwargs
    assert job["id"] == "auto_stop_expired"
    assert job["func"] == manager._auto_stop_expired
    scheduler.start.assert_called_once_with()
    mock_is_primary.assert_called_once_with()
