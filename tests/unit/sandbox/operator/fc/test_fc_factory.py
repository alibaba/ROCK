"""Unit tests for OperatorFactory FC branch.

Verifies review findings:
- Phase 3: factory creates FCOperator when operator_type='fc'
- C1: missing fc_config raises ValueError (admin wiring relies on this)
"""

from unittest.mock import MagicMock

import pytest

from rock.config import RuntimeConfig
from rock.sandbox.operator.abstract import AbstractOperator
from rock.sandbox.operator.factory import OperatorContext, OperatorFactory


class TestFCOperatorFactory:
    def test_creates_fc_operator(self, fc_config):
        ctx = OperatorContext(
            runtime_config=RuntimeConfig(operator_type="fc"),
            fc_config=fc_config,
        )
        operator = OperatorFactory.create_operator(ctx)
        assert isinstance(operator, AbstractOperator)
        assert operator.__class__.__name__ == "FCOperator"

    def test_sets_redis_provider_when_provided(self, fc_config):
        redis_provider = MagicMock()
        ctx = OperatorContext(
            runtime_config=RuntimeConfig(operator_type="fc"),
            fc_config=fc_config,
            redis_provider=redis_provider,
        )
        operator = OperatorFactory.create_operator(ctx)
        assert operator._redis_provider is redis_provider

    def test_missing_fc_config_raises(self):
        ctx = OperatorContext(runtime_config=RuntimeConfig(operator_type="fc"))
        with pytest.raises(ValueError, match="FCConfig is required"):
            OperatorFactory.create_operator(ctx)
