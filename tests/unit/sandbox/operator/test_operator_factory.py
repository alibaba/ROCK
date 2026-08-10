"""Unit tests for OperatorFactory dispatch (opensandbox branch)."""

from unittest.mock import MagicMock, patch

import pytest

from rock.config import K8sConfig, OpenSandboxConfig, RuntimeConfig
from rock.sandbox.operator.factory import (
    OperatorContext,
    OperatorFactory,
    operator_requires_ray,
    operator_supports_scheduler,
)
from rock.sandbox.operator.opensandbox.operator import OpenSandboxOperator


def test_operator_requires_ray():
    assert operator_requires_ray("ray") is True
    assert operator_requires_ray("Ray") is True  # case-insensitive
    assert operator_requires_ray("k8s") is False
    assert operator_requires_ray("opensandbox") is False


def test_operator_supports_scheduler():
    assert operator_supports_scheduler("ray") is True
    assert operator_supports_scheduler("k8s") is True
    assert operator_supports_scheduler("opensandbox") is False
    assert operator_supports_scheduler("OpenSandbox") is False


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


def test_create_k8s_operator_receives_template_table():
    template_table = MagicMock()
    k8s_config = K8sConfig()
    ctx = OperatorContext(
        runtime_config=_runtime("k8s"),
        k8s_config=k8s_config,
        template_table=template_table,
    )

    with patch("rock.sandbox.operator.factory.K8sOperator") as operator_class:
        OperatorFactory.create_operator(ctx)

    operator_class.assert_called_once_with(k8s_config=k8s_config, template_table=template_table)


def test_unsupported_operator_type_lists_opensandbox():
    ctx = OperatorContext(runtime_config=_runtime("bogus"))
    with pytest.raises(ValueError, match="opensandbox"):
        OperatorFactory.create_operator(ctx)
