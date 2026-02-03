import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from rock.admin.metrics.decorator import (
    _build_attributes,
    _extract_sandbox_id,
    _get_user_info,
    _record_metrics,
    _update_sandbox_id_from_result,
)
from rock.admin.metrics.monitor import MetricsMonitor
from rock.utils.providers import RedisProvider


class SampleObject:
    """测试对象类，用于模拟具有特定属性的对象"""

    def __init__(self, sandbox_id=None, container_name=None):
        self.sandbox_id = sandbox_id
        self.container_name = container_name


def test_extract_sandbox_id_with_custom_extractor():
    def custom_extractor(*args, **kwargs):
        return "custom-sandbox-id"

    result = _extract_sandbox_id(
        args=("arg1", "arg2"), kwargs={"param1": "value1"}, extract_sandbox_id=custom_extractor
    )
    assert result == "custom-sandbox-id"


# 测试从kwargs中提取sandbox_id
def test_extract_sandbox_id_with_param():
    result = _extract_sandbox_id(
        args=("arg1", "arg2"), kwargs={"sandbox_id": "param-sandbox-id"}, sandbox_id_param="sandbox_id"
    )
    assert result == "param-sandbox-id"


def test_extract_sandbox_id_with_position():
    result = _extract_sandbox_id(args=("arg1", "position-sandbox-id", "arg3"), kwargs={}, sandbox_id_position=2)
    assert result == "position-sandbox-id"


def test_extract_sandbox_id_with_first_arg_string():
    result = _extract_sandbox_id(args=("first-sandbox-id", "arg2"), kwargs={})
    assert result == "first-sandbox-id"


def test_extract_sandbox_id_with_first_arg_object():
    test_obj = SampleObject(container_name="object-sandbox-id")
    result = _extract_sandbox_id(args=(test_obj, "arg2"), kwargs={})
    assert result == "object-sandbox-id"


def test_extract_sandbox_id_prefers_container_name_over_sandbox_id():
    test_obj = SampleObject(container_name="container-name", sandbox_id="sandbox-id")
    result = _extract_sandbox_id(args=(test_obj, "arg2"), kwargs={})

    assert result == "container-name"


@patch("rock.admin.metrics.decorator.alive_sandbox_key")
def test_get_user_info_success(mock_alive_key):
    mock_redis_provider = Mock(spec=RedisProvider)

    async def async_mock_return_value(*args, **kwargs):
        return [{"user_id": "user123", "experiment_id": "exp456", "namespace": "ns789"}]

    mock_alive_key.return_value = "alive:test-sandbox"
    mock_redis_provider.json_get = AsyncMock(side_effect=async_mock_return_value)

    # Run the async function in a blocking way
    user_id, experiment_id, namespace = asyncio.run(_get_user_info(mock_redis_provider, "test-sandbox"))
    assert user_id == "user123"
    assert experiment_id == "exp456"
    assert namespace == "ns789"


@patch("rock.admin.metrics.decorator.alive_sandbox_key")
def test_get_user_info_no_data(mock_alive_key):
    mock_redis_provider = Mock(spec=RedisProvider)

    async def async_mock_return_value(*args, **kwargs):
        return []

    mock_alive_key.return_value = "alive:test-sandbox"
    mock_redis_provider.json_get = AsyncMock(side_effect=async_mock_return_value)

    # Run the async function in a blocking way
    user_id, experiment_id, namespace = asyncio.run(_get_user_info(mock_redis_provider, "test-sandbox"))
    assert user_id == "default"
    assert experiment_id == "default"
    assert namespace == "default"


def test_build_attributes():
    mock_func = Mock()
    mock_func.__name__ = "test_function"

    attributes = _build_attributes("test_operation", "test-sandbox", mock_func, "user123", "exp456", "ns789")

    expected = {
        "operation": "test_operation",
        "sandbox_id": "test-sandbox",
        "method": "test_function",
        "user_id": "user123",
        "experiment_id": "exp456",
        "namespace": "ns789",
    }
    assert attributes == expected


def test_update_sandbox_id_from_result():
    mock_result = Mock()
    mock_result.sandbox_id = "result-sandbox-id"
    attributes = {"sandbox_id": "original-sandbox-id"}

    updated_attributes = _update_sandbox_id_from_result(mock_result, attributes)
    assert updated_attributes["sandbox_id"] == "result-sandbox-id"


def test_record_metrics_success():
    mock_metrics_monitor = Mock(spec=MetricsMonitor)
    attributes = {"operation": "test_op"}
    start_time = 0
    result = "success"

    # Mock time.perf_counter to return a fixed value for testing
    with patch("rock.admin.metrics.decorator.time.perf_counter", return_value=1.0):
        try:
            _record_metrics(mock_metrics_monitor, result, attributes, start_time, "test")
        except Exception:
            pass  # We expect this to return the result, not raise

    # Verify success counter was called
    mock_metrics_monitor.record_counter_by_name.assert_any_call("test.success", 1, attributes)

    # Verify gauge and total counter were called
    assert mock_metrics_monitor.record_gauge_by_name.called
    mock_metrics_monitor.record_counter_by_name.assert_any_call("test.total", 1, attributes)


def test_record_metrics_failure():
    mock_metrics_monitor = Mock(spec=MetricsMonitor)
    attributes = {"operation": "test_op"}
    start_time = 0
    exception = Exception("test error")

    # Mock time.perf_counter to return a fixed value for testing
    with patch("rock.admin.metrics.decorator.time.perf_counter", return_value=1.0):
        with pytest.raises(Exception) as context:
            _record_metrics(mock_metrics_monitor, exception, attributes, start_time, "test")

    assert str(context.value) == "test error"

    # Verify failure counter was called with error attributes
    error_attrs = {**attributes, "error_type": "Exception"}
    mock_metrics_monitor.record_counter_by_name.assert_called_with("test.failure", 1, error_attrs)
