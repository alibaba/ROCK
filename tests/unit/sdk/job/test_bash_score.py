"""Tests for BashTrialResult score parsing."""

from __future__ import annotations

from rock.sdk.job.result import BashTrialResult


class TestBashTrialResultScore:
    def test_score_from_score_summary(self):
        output = """Running tests...
=== Score Summary ===
score: 0.85
task_score=0.85
"""
        result = BashTrialResult(task_name="test", raw_output=output)
        assert result.score == 0.85

    def test_score_from_task_score(self):
        output = """=== Score Summary ===
task_score: 0.92
"""
        result = BashTrialResult(task_name="test", raw_output=output)
        assert result.score == 0.92

    def test_score_zero_when_no_summary(self):
        output = "Running tests...\nAll passed."
        result = BashTrialResult(task_name="test", raw_output=output)
        assert result.score == 0.0

    def test_score_zero_when_empty_output(self):
        result = BashTrialResult(task_name="test", raw_output="")
        assert result.score == 0.0

    def test_score_zero_when_parse_fails(self):
        output = """=== Score Summary ===
score: not-a-number
"""
        result = BashTrialResult(task_name="test", raw_output=output)
        assert result.score == 0.0

    def test_score_caches_after_first_parse(self):
        output = """=== Score Summary ===
score: 0.75
"""
        result = BashTrialResult(task_name="test", raw_output=output)
        score1 = result.score
        score2 = result.score
        assert score1 == 0.75
        assert score2 == 0.75
        assert result._parsed_score == 0.75
