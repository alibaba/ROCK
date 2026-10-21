"""Unit tests for OperatorFactory dispatch (opensandbox branch)."""

from unittest.mock import MagicMock

import pytest

from rock.config import OpenSandboxConfig, RuntimeConfig
from rock.sandbox.operator.factory import (
    OperatorContext,
    OperatorFactory,
    operator_requires_ray,
    operator_supports_scheduler,
)
from rock.sandbox.operator.opensandbox.operator import OpenSandboxOperator
from rock.sandbox.operator.snapd import SnapdOperator


def test_operator_requires_ray():
    assert operator_requires_ray("ray") is True
    assert operator_requires_ray("Ray") is True  # case-insensitive
    assert operator_requires_ray("k8s") is False
    assert operator_requires_ray("opensandbox") is False
    assert operator_requires_ray("snapd") is True


def test_operator_supports_scheduler():
    assert operator_supports_scheduler("ray") is True
    assert operator_supports_scheduler("k8s") is True
    assert operator_supports_scheduler("opensandbox") is False
    assert operator_supports_scheduler("OpenSandbox") is False
    assert operator_supports_scheduler("remote") is False
    assert operator_supports_scheduler("Remote") is False
    assert operator_supports_scheduler("snapd") is True
    assert operator_supports_scheduler("Snapd") is True
    assert operator_supports_scheduler("custom_operator") is True


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


def test_create_snapd_operator_needs_only_ray_without_database_repository():
    ray_service = MagicMock()
    context = OperatorContext(
        runtime_config=_runtime("snapd"),
        ray_service=ray_service,
    )

    operator = OperatorFactory.create_operator(context)

    assert isinstance(operator, SnapdOperator)
    assert operator._ray_service is ray_service
    assert not hasattr(operator, "_repository")
    assert "snapd_repository" not in OperatorContext.__dataclass_fields__


def test_create_snapd_operator_requires_ray():
    with pytest.raises(ValueError, match="RayService"):
        OperatorFactory.create_operator(OperatorContext(runtime_config=_runtime("snapd")))
