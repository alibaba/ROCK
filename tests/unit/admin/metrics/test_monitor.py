from unittest.mock import patch

from rock.admin.metrics.monitor import MetricsMonitor, aggregate_metrics


def test_aggregate_metrics():
    sandbox_meta: dict[str, dict[str, str]] = {}
    sandbox_meta["123"] = {"image": "iflow_test"}
    sandbox_meta["456"] = {"image": "iflow_test"}
    sandbox_meta["789"] = {"image": "iflow_test"}
    sandbox_meta["101112"] = {"image": "python:3.11"}
    sandbox_meta["131415"] = {"image": "python:3.11"}
    sandbox_meta["161718"] = {"image": "image_test"}

    aggregated_metrics = aggregate_metrics(sandbox_meta, "image")
    assert aggregated_metrics["iflow_test"] == 3
    assert aggregated_metrics["python:3.11"] == 2
    assert aggregated_metrics["image_test"] == 1


def test_endpoint_defaults_to_host_port_when_not_configured():
    monitor = MetricsMonitor(
        host="127.0.0.1",
        port="4318",
        pod="test-pod",
        env="daily",
        role="test",
    )
    assert monitor.endpoint == "http://127.0.0.1:4318/v1/metrics"


def test_endpoint_uses_configured_value():
    custom_endpoint = "http://custom-collector:9090/v1/metrics"
    monitor = MetricsMonitor(
        host="127.0.0.1",
        port="4318",
        pod="test-pod",
        env="daily",
        role="test",
        endpoint=custom_endpoint,
    )
    assert monitor.endpoint == custom_endpoint


def test_endpoint_falls_back_when_empty_string():
    monitor = MetricsMonitor(
        host="10.0.0.1",
        port="5555",
        pod="test-pod",
        env="daily",
        role="test",
        endpoint="",
    )
    assert monitor.endpoint == "http://10.0.0.1:5555/v1/metrics"


@patch("rock.admin.metrics.monitor.get_uniagent_endpoint", return_value=("192.168.1.1", "4318"))
@patch("rock.admin.metrics.monitor.get_instance_id", return_value="test-pod")
@patch("rock.admin.metrics.monitor.env_vars")
def test_create_without_metrics_endpoint(mock_env_vars, mock_instance_id, mock_uniagent):
    mock_env_vars.ROCK_ADMIN_ENV = "daily"
    mock_env_vars.ROCK_ADMIN_ROLE = "test"

    monitor = MetricsMonitor.create(export_interval_millis=10000)
    assert monitor.endpoint == "http://192.168.1.1:4318/v1/metrics"


@patch("rock.admin.metrics.monitor.get_uniagent_endpoint", return_value=("192.168.1.1", "4318"))
@patch("rock.admin.metrics.monitor.get_instance_id", return_value="test-pod")
@patch("rock.admin.metrics.monitor.env_vars")
def test_create_with_metrics_endpoint(mock_env_vars, mock_instance_id, mock_uniagent):
    mock_env_vars.ROCK_ADMIN_ENV = "daily"
    mock_env_vars.ROCK_ADMIN_ROLE = "test"
    custom_endpoint = "http://my-otel-collector:4317/v1/metrics"

    monitor = MetricsMonitor.create(export_interval_millis=10000, metrics_endpoint=custom_endpoint)
    assert monitor.endpoint == custom_endpoint
