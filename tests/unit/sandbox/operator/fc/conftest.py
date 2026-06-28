"""Shared fixtures for FC operator unit tests.

These tests verify the FC SDK InvokeFunction session model.
No real FC environment is required: the FC SDK client is mocked.
"""

import json
import sys
from unittest.mock import MagicMock

import pytest

from rock.config import FCConfig


@pytest.fixture
def fc_config() -> FCConfig:
    """A fully-populated FCConfig for tests."""
    return FCConfig(
        region="cn-hangzhou",
        account_id="1234567890",
        function_name="rock-test-function",
        access_key_id="AKIDTEST",
        access_key_secret="SKTEST",
        security_token=None,
        default_memory=4096,
        default_cpus=2.0,
        default_session_ttl=86400,
        default_function_timeout=3600.0,
        default_session_idle_timeout=1800,
    )


@pytest.fixture
def fc_operator_config():
    from rock.sandbox.operator.fc.config import FCOperatorConfig

    return FCOperatorConfig(
        session_id="fc-testsession01",
        function_name="rock-test-function",
        region="cn-hangzhou",
        account_id="1234567890",
        access_key_id="AKIDTEST",
        access_key_secret="SKTEST",
        image="registry.cn-hangzhou.aliyuncs.com/rock/test:latest",
        memory=4096,
        cpus=2.0,
        session_ttl=86400,
    )


@pytest.fixture
def mock_fc_client():
    """Mock FC SDK client for InvokeFunction calls.

    Returns a MagicMock whose invoke_function_with_options returns
    a response with a configurable body. Tests can override the body
    by setting ``mock_fc_client.invoke_function_with_options.return_value.body``.
    """
    client = MagicMock()
    response = MagicMock()
    response.body = json.dumps({"output": "root@fc:~$ "})
    client.invoke_function_with_options = MagicMock(return_value=response)
    return client


@pytest.fixture
def fake_fc_sdk(monkeypatch):
    """Inject a fake alibabacloud_fc20230330 SDK into sys.modules.

    Allows FCOperator._ensure_fc_client / _create_function to run without the
    real package installed.
    """
    fake_client_module = MagicMock()
    fake_client_module.Client = MagicMock(return_value=MagicMock())

    fake_models_module = MagicMock()

    def _make_request_class(name):
        class _Req:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)
            def to_map(self):
                return dict(self.__dict__)
        _Req.__name__ = name
        return _Req

    for cls_name in [
        "CreateFunctionInput",
        "CreateFunctionRequest",
        "CustomContainerConfig",
        "HeaderFieldSessionAffinityConfig",
        "GetFunctionRequest",
        "ListFunctionsRequest",
        "InvokeFunctionRequest",
        "InvokeFunctionHeaders",
    ]:
        setattr(fake_models_module, cls_name, _make_request_class(cls_name))

    fake_tea_openapi_module = MagicMock()
    fake_tea_openapi_module.models = MagicMock()
    fake_tea_openapi_module.models.Config = MagicMock()

    # Mock RuntimeOptions from alibabacloud_tea_util
    class _RuntimeOptions:
        def __init__(self, **kwargs):
            self.common_headers = None
            self.__dict__.update(kwargs)

    fake_tea_util_module = MagicMock()
    fake_tea_util_module.models = MagicMock()
    fake_tea_util_module.models.RuntimeOptions = _RuntimeOptions

    monkeypatch.setitem(sys.modules, "alibabacloud_fc20230330", MagicMock())
    monkeypatch.setitem(sys.modules, "alibabacloud_fc20230330.client", fake_client_module)
    monkeypatch.setitem(sys.modules, "alibabacloud_fc20230330.models", fake_models_module)
    monkeypatch.setitem(sys.modules, "alibabacloud_tea_openapi", fake_tea_openapi_module)
    monkeypatch.setitem(sys.modules, "alibabacloud_tea_openapi.models", fake_tea_openapi_module.models)
    monkeypatch.setitem(sys.modules, "alibabacloud_tea_util", fake_tea_util_module)
    monkeypatch.setitem(sys.modules, "alibabacloud_tea_util.models", fake_tea_util_module.models)
    return fake_client_module
