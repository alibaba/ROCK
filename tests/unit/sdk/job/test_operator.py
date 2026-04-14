"""Tests for rock.sdk.job.operator — Operator, ScatterOperator."""

from __future__ import annotations

import pytest

# Import bench first to avoid circular-import pitfall in rock.sdk.job.config
import rock.sdk.bench  # noqa: F401
from rock.sdk.job.config import BashJobConfig, JobConfig
from rock.sdk.job.operator import Operator, ScatterOperator
from rock.sdk.job.trial.abstract import AbstractTrial
from rock.sdk.job.trial.bash import BashTrial


class TestOperatorABC:
    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            Operator()


class TestScatterOperator:
    def test_default_size_is_one(self):
        op = ScatterOperator()
        assert op.size == 1

    def test_apply_returns_one_trial_by_default(self):
        op = ScatterOperator()
        trials = op.apply(BashJobConfig(script="echo hi"))
        assert len(trials) == 1
        assert isinstance(trials[0], BashTrial)

    def test_apply_returns_n_trials(self):
        op = ScatterOperator(size=3)
        trials = op.apply(BashJobConfig(script="echo hi"))
        assert len(trials) == 3
        for t in trials:
            assert isinstance(t, BashTrial)

    def test_size_zero_returns_empty(self):
        op = ScatterOperator(size=0)
        assert op.apply(BashJobConfig(script="echo hi")) == []

    def test_size_negative_returns_empty(self):
        op = ScatterOperator(size=-5)
        assert op.apply(BashJobConfig(script="echo hi")) == []

    def test_returns_correct_trial_type_for_bash_config(self):
        op = ScatterOperator(size=2)
        trials = op.apply(BashJobConfig(script="ls"))
        assert all(isinstance(t, BashTrial) for t in trials)


class TestCustomOperator:
    def test_custom_subclass_can_override_apply(self):
        class FixedOperator(Operator):
            def __init__(self, trials: list[AbstractTrial]):
                self._trials = trials

            def apply(self, config: JobConfig) -> list[AbstractTrial]:
                return list(self._trials)

        fixed_trials = [BashTrial(BashJobConfig(script="a")), BashTrial(BashJobConfig(script="b"))]
        op = FixedOperator(fixed_trials)
        result = op.apply(BashJobConfig(script="ignored"))
        assert result == fixed_trials
        assert len(result) == 2
