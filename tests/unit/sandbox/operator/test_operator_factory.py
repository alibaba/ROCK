"""Unit tests for OperatorFactory dispatch (opensandbox branch)."""

from unittest.mock import MagicMock

import pytest

from rock.config import OpenSandboxConfig, RuntimeConfig
from rock.sandbox.operator.factory import OperatorContext, OperatorFactory
from rock.sandbox.operator.opensandbox.operator import OpenSandboxOperator


def _runtime(operator_type: str) -> RuntimeConfig:
    return RuntimeConfig(
        operator_type=operator_type,
        python_env_path="/usr",
        envhub_db_url="sqlite:////tmp/test.db",
    )


def test_create_opensandbox_operator():
    ctx = OperatorContext(
        runtime_config=_runtime("opensandbox"),
        opensandbox_config=OpenSandboxConfig(endpoint="opensandbox.local"),
        redis_provider=MagicMock(),
    )
    operator = OperatorFactory.create_operator(ctx)
    assert isinstance(operator, OpenSandboxOperator)
    assert operator._redis_provider is ctx.redis_provider


def test_create_opensandbox_operator_requires_config():
    ctx = OperatorContext(runtime_config=_runtime("opensandbox"), opensandbox_config=None)
    with pytest.raises(ValueError, match="OpenSandboxConfig"):
        OperatorFactory.create_operator(ctx)


def test_unsupported_operator_type_lists_opensandbox():
    ctx = OperatorContext(runtime_config=_runtime("bogus"))
    with pytest.raises(ValueError, match="opensandbox"):
        OperatorFactory.create_operator(ctx)
