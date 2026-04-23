from unittest.mock import patch

from rock.admin.metrics.constants import MetricsConstants
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


def test_monitor_with_user_defined_tags():
    """Test that user_defined_tags are properly added to base_attributes"""
    custom_tags = {"service": "rock-sandbox", "version": "1.0.0"}

    monitor = MetricsMonitor(
        host="127.0.0.1",
        port="4318",
        pod="test-pod",
        env="daily",
        role="test",
        user_defined_tags=custom_tags,
    )

    # Verify that base_attributes contains all default attributes
    assert monitor.base_attributes["host"] == "127.0.0.1"
    assert monitor.base_attributes["pod"] == "test-pod"
    assert monitor.base_attributes["env"] == "daily"
    assert monitor.base_attributes["role"] == "test"

    # Verify that base_attributes contains all custom tags
    assert monitor.base_attributes["service"] == "rock-sandbox"
    assert monitor.base_attributes["version"] == "1.0.0"

    # Verify that attributes property returns the same base_attributes
    assert monitor.attributes == monitor.base_attributes


def test_monitor_without_user_defined_tags():
    """Test that monitor works correctly without user_defined_tags"""
    monitor = MetricsMonitor(
        host="127.0.0.1",
        port="4318",
        pod="test-pod",
        env="daily",
        role="test",
    )

    # Verify that base_attributes only contains default attributes
    assert monitor.base_attributes["host"] == "127.0.0.1"
    assert monitor.base_attributes["pod"] == "test-pod"
    assert monitor.base_attributes["env"] == "daily"
    assert monitor.base_attributes["role"] == "test"

    # Verify no extra keys exist
    assert len(monitor.base_attributes) == 5


def test_monitor_with_empty_user_defined_tags():
    """Test that monitor handles empty user_defined_tags dict"""
    monitor = MetricsMonitor(
        host="127.0.0.1",
        port="4318",
        pod="test-pod",
        env="daily",
        role="test",
        user_defined_tags={},
    )

    # Verify that base_attributes only contains default attributes
    assert len(monitor.base_attributes) == 5
    assert monitor.base_attributes["host"] == "127.0.0.1"


@patch("rock.admin.metrics.monitor.get_uniagent_endpoint", return_value=("192.168.1.1", "4318"))
@patch("rock.admin.metrics.monitor.get_instance_id", return_value="test-pod")
@patch("rock.admin.metrics.monitor.env_vars")
def test_create_with_user_defined_tags(mock_env_vars, mock_instance_id, mock_uniagent):
    """Test MetricsMonitor.create() with user_defined_tags"""
    mock_env_vars.ROCK_ADMIN_ENV = "daily"
    mock_env_vars.ROCK_ADMIN_ROLE = "test"

    custom_tags = {
        "cluster": "prod-cluster",
        "region": "cn-hz",
    }

    monitor = MetricsMonitor.create(export_interval_millis=10000, user_defined_tags=custom_tags)

    # Verify that custom tags are in base_attributes
    assert monitor.base_attributes["cluster"] == "prod-cluster"
    assert monitor.base_attributes["region"] == "cn-hz"

    # Verify default attributes are still present
    assert monitor.base_attributes["host"] == "192.168.1.1"
    assert monitor.base_attributes["pod"] == "test-pod"
    assert monitor.base_attributes["env"] == "daily"
    assert monitor.base_attributes["role"] == "test"


def _create_dev_monitor(metric_prefix: str = "") -> MetricsMonitor:
    """Create a real MetricsMonitor with env=dev (InMemoryMetricReader, no skip)."""
    return MetricsMonitor(
        host="127.0.0.1",
        port="4318",
        pod="test-pod",
        env="dev",
        role="test",
        metric_prefix=metric_prefix,
    )


class TestMetastoreMetricsRegistration:
    """Verify that metastore metric names are registered and usable on a real monitor."""

    def test_metastore_counters_registered(self):
        monitor = _create_dev_monitor()
        for name in [
            MetricsConstants.METASTORE_SUCCESS,
            MetricsConstants.METASTORE_FAILURE,
            MetricsConstants.METASTORE_TOTAL,
            MetricsConstants.METASTORE_DB_SUCCESS,
            MetricsConstants.METASTORE_DB_FAILURE,
            MetricsConstants.METASTORE_DB_TOTAL,
        ]:
            assert name in monitor.counters, f"Counter '{name}' not registered"
            assert monitor.counters[name] is not None, f"Counter '{name}' is None"

    def test_metastore_gauges_registered(self):
        monitor = _create_dev_monitor()
        for name in [
            MetricsConstants.METASTORE_RT,
            MetricsConstants.METASTORE_DB_RT,
        ]:
            assert name in monitor.gauges, f"Gauge '{name}' not registered"
            assert monitor.gauges[name] is not None, f"Gauge '{name}' is None"

    def test_record_counter_by_name_does_not_raise(self):
        """Calling record_counter_by_name with registered metric names should not KeyError."""
        monitor = _create_dev_monitor()
        attrs = {"operation": "create", "method": "create"}
        monitor.record_counter_by_name(MetricsConstants.METASTORE_SUCCESS, 1, attrs)
        monitor.record_counter_by_name(MetricsConstants.METASTORE_TOTAL, 1, attrs)
        monitor.record_counter_by_name(MetricsConstants.METASTORE_DB_SUCCESS, 1, attrs)
        monitor.record_counter_by_name(MetricsConstants.METASTORE_DB_TOTAL, 1, attrs)

    def test_record_gauge_by_name_does_not_raise(self):
        """Calling record_gauge_by_name with registered metric names should not KeyError."""
        monitor = _create_dev_monitor()
        attrs = {"operation": "create", "method": "create"}
        monitor.record_gauge_by_name(MetricsConstants.METASTORE_RT, 1.5, attrs)
        monitor.record_gauge_by_name(MetricsConstants.METASTORE_DB_RT, 0.8, attrs)

    def test_metric_prefix_stored(self):
        monitor = _create_dev_monitor(metric_prefix="meta_store")
        assert monitor.metric_prefix == "meta_store"

    def test_end_to_end_record_does_not_raise(self):
        """Full round-trip: create real monitor, record metrics, no errors.

        OTel's global MeterProvider can only be set once per process, so
        subsequent dev monitors share the first reader.  We verify the
        record path completes without exceptions, which proves the counters
        and gauges are real OTel instruments (not None).
        """
        monitor = _create_dev_monitor(metric_prefix="meta_store")
        attrs = {"operation": "get", "method": "get"}
        # These would raise KeyError if names are unregistered,
        # or AttributeError if instruments are None.
        monitor.record_counter_by_name(MetricsConstants.METASTORE_SUCCESS, 1, attrs)
        monitor.record_counter_by_name(MetricsConstants.METASTORE_FAILURE, 1, {**attrs, "error_type": "ValueError"})
        monitor.record_counter_by_name(MetricsConstants.METASTORE_TOTAL, 1, attrs)
        monitor.record_gauge_by_name(MetricsConstants.METASTORE_RT, 5.0, attrs)
        monitor.record_counter_by_name(MetricsConstants.METASTORE_DB_SUCCESS, 1, attrs)
        monitor.record_counter_by_name(MetricsConstants.METASTORE_DB_FAILURE, 1, {**attrs, "error_type": "IOError"})
        monitor.record_counter_by_name(MetricsConstants.METASTORE_DB_TOTAL, 1, attrs)
        monitor.record_gauge_by_name(MetricsConstants.METASTORE_DB_RT, 2.0, attrs)
