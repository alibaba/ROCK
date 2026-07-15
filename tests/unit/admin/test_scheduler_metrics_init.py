from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from rock.admin.main import _init_scheduler_metrics


def test_scheduler_monitor_uses_30_minute_fallback_and_runtime_otlp_settings():
    config = SimpleNamespace(
        runtime=SimpleNamespace(
            metrics_endpoint="http://collector:4318/v1/metrics",
            user_defined_tags={"business": "shared"},
        )
    )
    monitor = MagicMock()

    with patch("rock.admin.main.MetricsMonitor.create", return_value=monitor) as create:
        created_monitor, scheduler_metrics = _init_scheduler_metrics(config)

    assert created_monitor is monitor
    assert scheduler_metrics._monitor is monitor
    create.assert_called_once_with(
        export_interval_millis=1_800_000,
        metrics_endpoint="http://collector:4318/v1/metrics",
        user_defined_tags={"business": "shared"},
        metric_prefix="scheduler",
    )
