"""Tests for rock.sdk.job.meta — JobMeta model, render_meta_json, and trial integration.

TDD: written first (RED), then implementation (GREEN).
"""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

import oss2
import pytest


# ---------------------------------------------------------------------------
# Phase 1: Model tests
# ---------------------------------------------------------------------------


class TestJobMeta:
    def test_defaults(self):
        from rock.sdk.job.meta import JobMeta

        m = JobMeta()
        assert m.schema_version == "1"
        assert m.job_name == ""
        assert m.job_type == ""
        assert m.status == ""
        assert m.namespace is None
        assert m.experiment_id is None
        assert m.user_id is None
        assert m.image is None
        assert m.labels == {}
        assert m.started_at is None
        assert m.finished_at is None
        assert m.exit_code is None

    def test_with_values(self):
        from rock.sdk.job.meta import JobMeta

        m = JobMeta(
            job_name="swe-bench_abc",
            job_type="harbor",
            status="completed",
            namespace="ns",
            experiment_id="exp",
            user_id="alice",
            image="python:3.11",
            labels={"env": "test"},
            started_at="2024-01-01T10:00:00Z",
            finished_at="2024-01-01T12:00:00Z",
            exit_code=0,
        )
        assert m.job_type == "harbor"
        assert m.status == "completed"
        assert m.exit_code == 0

    def test_serialization_roundtrip(self):
        from rock.sdk.job.meta import JobMeta

        m = JobMeta(job_name="test", job_type="bash", status="failed", exit_code=1)
        data = json.loads(m.model_dump_json())
        m2 = JobMeta.model_validate(data)
        assert m2.job_name == "test"
        assert m2.exit_code == 1


# ---------------------------------------------------------------------------
# Phase 2: render_meta_json tests
# ---------------------------------------------------------------------------


class TestMetaModuleDocstring:
    def test_docstring_does_not_claim_harbor_writes_meta(self):
        """P1-3: meta.py docstring must not claim Harbor writes rock_meta.json."""
        import rock.sdk.job.meta as meta_module

        docstring = meta_module.__doc__ or ""
        assert "Both Harbor and Bash" not in docstring, (
            "docstring incorrectly claims both job types write rock_meta.json"
        )


class TestRenderMetaJson:
    def test_bash_running(self):
        from rock.sdk.job.config import BashJobConfig
        from rock.sdk.job.meta import render_meta_json

        config = BashJobConfig(job_name="job1", namespace="ns", experiment_id="exp")
        text = render_meta_json(config, job_type="bash", status="running")
        data = json.loads(text)
        assert data["job_name"] == "job1"
        assert data["job_type"] == "bash"
        assert data["status"] == "running"
        assert data["namespace"] == "ns"
        assert data["experiment_id"] == "exp"

    def test_harbor_running(self):
        from rock.sdk.bench.models.job.config import HarborJobConfig
        from rock.sdk.job.meta import render_meta_json

        config = HarborJobConfig(
            job_name="harbor-job",
            namespace="ns",
            experiment_id="exp",
        )
        text = render_meta_json(config, job_type="harbor", status="running")
        data = json.loads(text)
        assert data["job_type"] == "harbor"

    def test_includes_user_id(self):
        from rock.sdk.job.config import BashJobConfig
        from rock.sdk.job.meta import render_meta_json

        config = BashJobConfig(job_name="j")
        config.environment.user_id = "bob"
        text = render_meta_json(config, job_type="bash", status="running")
        data = json.loads(text)
        assert data["user_id"] == "bob"

    def test_includes_labels(self):
        from rock.sdk.job.config import BashJobConfig
        from rock.sdk.job.meta import render_meta_json

        config = BashJobConfig(job_name="j", labels={"team": "ml", "env": "prod"})
        text = render_meta_json(config, job_type="bash", status="running")
        data = json.loads(text)
        assert data["labels"] == {"team": "ml", "env": "prod"}

    def test_includes_image(self):
        from rock.sdk.job.config import BashJobConfig
        from rock.sdk.job.meta import render_meta_json

        config = BashJobConfig(job_name="j")
        config.environment.image = "ubuntu:22.04"
        text = render_meta_json(config, job_type="bash", status="running")
        data = json.loads(text)
        assert data["image"] == "ubuntu:22.04"


# ---------------------------------------------------------------------------
# Phase 3: BashTrial meta integration
# ---------------------------------------------------------------------------


class TestBashTrialMetaIntegration:
    def _build_bash_config(self):
        from rock.sdk.bench.models.trial.config import RockEnvironmentConfig
        from rock.sdk.envhub.config import OssMirrorConfig
        from rock.sdk.job.config import BashJobConfig

        return BashJobConfig(
            job_name="bash-job-1",
            namespace="ns",
            experiment_id="exp",
            script="echo hello",
            environment=RockEnvironmentConfig(
                oss_mirror=OssMirrorConfig(
                    enabled=True,
                    oss_bucket="bucket",
                    oss_access_key_id="ak",
                    oss_access_key_secret="sk",
                    oss_endpoint="https://oss.example.com",
                    oss_region="cn-hangzhou",
                    namespace="ns",
                    experiment_id="exp",
                ),
            ),
        )

    def test_wrapper_contains_meta_prologue(self):
        from rock.sdk.job.trial.bash import BashTrial

        config = self._build_bash_config()
        trial = BashTrial(config)
        trial._ossutil_ready = True
        script = trial.build()
        assert "rock_meta.json" in script
        assert '"status": "running"' in script

    def test_wrapper_contains_meta_epilogue(self):
        from rock.sdk.job.trial.bash import BashTrial

        config = self._build_bash_config()
        trial = BashTrial(config)
        trial._ossutil_ready = True
        script = trial.build()
        assert "_rock_status" in script
        parts = script.split("rock_meta.json")
        assert len(parts) >= 3, "rock_meta.json should appear at least twice (prologue + epilogue)"

    def test_meta_contains_job_name(self):
        from rock.sdk.job.trial.bash import BashTrial

        config = self._build_bash_config()
        trial = BashTrial(config)
        trial._ossutil_ready = True
        script = trial.build()
        assert '"job_name": "bash-job-1"' in script

    def test_meta_contains_job_type_bash(self):
        from rock.sdk.job.trial.bash import BashTrial

        config = self._build_bash_config()
        trial = BashTrial(config)
        trial._ossutil_ready = True
        script = trial.build()
        assert '"job_type": "bash"' in script

    def test_no_meta_when_mirror_disabled(self):
        from rock.sdk.job.config import BashJobConfig
        from rock.sdk.job.trial.bash import BashTrial

        config = BashJobConfig(job_name="j", script="echo hi")
        trial = BashTrial(config)
        script = trial.build()
        assert "rock_meta.json" not in script

    def test_epilogue_heredoc_is_quoted(self):
        """P1-1: epilogue heredoc must use single-quoted delimiter to prevent
        shell expansion of $() in config values."""
        from rock.sdk.job.trial.bash import BashTrial

        config = self._build_bash_config()
        trial = BashTrial(config)
        trial._ossutil_ready = True
        script = trial.build()
        # Both prologue and epilogue heredocs must be single-quoted
        meta_writes = [line for line in script.splitlines() if "rock_meta.json" in line and "cat >" in line]
        for line in meta_writes:
            assert "'__ROCK_META_EOF__'" in line or "<<" not in line, (
                f"heredoc delimiter must be single-quoted to prevent shell expansion: {line}"
            )

    def test_epilogue_no_shell_variable_expansion_in_heredoc(self):
        """P1-1: config fields with $() must not be shell-executed."""
        from rock.sdk.job.trial.bash import BashTrial

        config = self._build_bash_config()
        config.job_name = "safe-job"
        trial = BashTrial(config)
        trial._ossutil_ready = True
        script = trial.build()
        # The epilogue heredoc body should NOT contain raw $-prefixed
        # values from config (those should go through sed placeholders)
        heredoc_sections = script.split("__ROCK_META_EOF__")
        for section in heredoc_sections:
            if '"status":' in section and '"exit_code":' in section:
                # This is an epilogue heredoc body — must not have
                # unquoted shell vars like $_rock_status directly
                assert "$_rock_status" not in section, (
                    "epilogue heredoc body must use placeholders, not raw shell variables"
                )

    def test_prologue_no_sed_null_replacement(self):
        """P1-2: prologue must not use sed to replace 'null' — it produces
        invalid JSON and can match null in other fields."""
        from rock.sdk.job.trial.bash import BashTrial

        config = self._build_bash_config()
        trial = BashTrial(config)
        trial._ossutil_ready = True
        script = trial.build()
        assert 's/null/' not in script, "must not use sed to replace literal 'null'"

    def test_meta_produces_valid_json_after_sed(self):
        """P1-2: heredoc body + sed substitution must produce valid JSON
        (no unquoted time strings, no bare null replacement)."""
        import re

        from rock.sdk.job.trial.bash import BashTrial

        config = self._build_bash_config()
        trial = BashTrial(config)
        trial._ossutil_ready = True
        script = trial.build()
        heredoc_bodies = re.findall(
            r"<< *'__ROCK_META_EOF__'\n(.*?)__ROCK_META_EOF__", script, re.DOTALL
        )
        assert len(heredoc_bodies) >= 2, "should have at least prologue + epilogue heredocs"
        for body in heredoc_bodies:
            # Simulate sed: replace placeholders with realistic values
            simulated = (
                body.replace("__ROCK_STATUS__", "completed")
                .replace("__ROCK_STARTED__", "2024-01-01T10:00:00Z")
                .replace("__ROCK_FINISHED__", "2024-01-01T12:00:00Z")
                .replace("__ROCK_EXIT_CODE__", "0")
            )
            data = json.loads(simulated)
            assert data["schema_version"] == "1"
            assert data["job_type"] == "bash"

    def test_epilogue_uses_sed_for_runtime_values(self):
        """P1-1/P1-2 fix: runtime values (status, timestamps, exit_code)
        must be injected via sed after a quoted heredoc, not inside it."""
        from rock.sdk.job.trial.bash import BashTrial

        config = self._build_bash_config()
        trial = BashTrial(config)
        trial._ossutil_ready = True
        script = trial.build()
        # The script should contain sed commands that replace placeholders
        assert "__ROCK_STATUS__" in script
        assert "__ROCK_STARTED__" in script
        assert "__ROCK_FINISHED__" in script
        assert "__ROCK_EXIT_CODE__" in script
        assert "sed" in script


# ---------------------------------------------------------------------------
# Phase 4: HarborTrial meta integration
# ---------------------------------------------------------------------------


class TestHarborTrialMetaIntegration:
    def _build_harbor_config(self):
        from rock.sdk.bench.models.trial.config import RockEnvironmentConfig
        from rock.sdk.envhub.config import OssMirrorConfig
        from rock.sdk.bench.models.job.config import HarborJobConfig

        return HarborJobConfig(
            job_name="harbor-job-1",
            namespace="ns",
            experiment_id="exp",
            environment=RockEnvironmentConfig(
                oss_mirror=OssMirrorConfig(
                    enabled=True,
                    oss_bucket="bucket",
                    oss_access_key_id="ak",
                    oss_access_key_secret="sk",
                    oss_endpoint="https://oss.example.com",
                    oss_region="cn-hangzhou",
                    namespace="ns",
                    experiment_id="exp",
                ),
            ),
        )

    def test_script_does_not_contain_meta(self):
        """Harbor's rock_meta.json is written by Harbor itself, not by the wrapper script."""
        from rock.sdk.job.trial.harbor import HarborTrial

        config = self._build_harbor_config()
        trial = HarborTrial(config)
        script = trial.build()
        assert "rock_meta.json" not in script

    def test_script_contains_harbor_start(self):
        from rock.sdk.job.trial.harbor import HarborTrial

        config = self._build_harbor_config()
        trial = HarborTrial(config)
        script = trial.build()
        assert "harbor jobs start" in script


# ---------------------------------------------------------------------------
# Phase 5: JobViewer read meta
# ---------------------------------------------------------------------------


def _make_oss_object(data: bytes):
    obj = BytesIO(data)
    return obj


class TestJobViewerReadMeta:
    @pytest.fixture
    def viewer(self):
        from rock.sdk.job.viewer import JobViewer

        bucket = MagicMock()
        return JobViewer(bucket, namespace="ns", experiment_id="exp"), bucket

    def test_get_job_meta_found(self, viewer):
        from rock.sdk.job.meta import JobMeta

        v, bucket = viewer
        meta_json = JobMeta(
            job_name="j1", job_type="bash", status="completed", exit_code=0
        ).model_dump_json()
        bucket.get_object.return_value = _make_oss_object(meta_json.encode())
        meta = v.get_job_meta("j1")
        assert meta is not None
        assert meta.job_name == "j1"
        assert meta.status == "completed"

    def test_get_job_meta_not_found(self, viewer):
        v, bucket = viewer
        bucket.get_object.side_effect = oss2.exceptions.NoSuchKey(404, {}, b"", {"Code": "NoSuchKey"})
        meta = v.get_job_meta("nonexistent")
        assert meta is None

    def test_get_job_meta_running(self, viewer):
        from rock.sdk.job.meta import JobMeta

        v, bucket = viewer
        meta_json = JobMeta(
            job_name="j1", job_type="harbor", status="running",
            started_at="2024-01-01T10:00:00Z",
        ).model_dump_json()
        bucket.get_object.return_value = _make_oss_object(meta_json.encode())
        meta = v.get_job_meta("j1")
        assert meta.status == "running"
        assert meta.finished_at is None

    def test_get_job_meta_invalid_json(self, viewer):
        v, bucket = viewer
        bucket.get_object.return_value = _make_oss_object(b"not valid json")
        meta = v.get_job_meta("j1")
        assert meta is None


# ---------------------------------------------------------------------------
# Phase 6: RunMeta and RunScoreSummary models
# ---------------------------------------------------------------------------


class TestRunScoreSummary:
    def test_defaults(self):
        from rock.sdk.job.meta import RunScoreSummary

        s = RunScoreSummary(completed=10, failed=2, skipped=0, avg_score=0.5, total_score=5.0, pass_rate=0.83)
        assert s.completed == 10
        assert s.failed == 2
        assert s.skipped == 0
        assert s.avg_score == 0.5
        assert s.total_score == 5.0
        assert s.pass_rate == 0.83
        assert s.scores == {}

    def test_with_scores(self):
        from rock.sdk.job.meta import RunScoreSummary

        s = RunScoreSummary(
            completed=2, failed=0, skipped=0, avg_score=0.75, total_score=1.5, pass_rate=1.0,
            scores={"task_a": 1.0, "task_b": 0.5},
        )
        assert s.scores == {"task_a": 1.0, "task_b": 0.5}

    def test_serialization_roundtrip(self):
        from rock.sdk.job.meta import RunScoreSummary

        s = RunScoreSummary(
            completed=5, failed=1, skipped=2, avg_score=0.6, total_score=3.0, pass_rate=0.625,
            scores={"t1": 1.0, "t2": 0.0},
        )
        data = json.loads(s.model_dump_json())
        s2 = RunScoreSummary.model_validate(data)
        assert s2.completed == 5
        assert s2.scores == {"t1": 1.0, "t2": 0.0}


class TestRunMeta:
    def test_required_fields(self):
        from rock.sdk.job.meta import RunMeta

        m = RunMeta(
            run_id="20260706T143052-a1b2c3d4",
            dataset="alibaba/aone-bench",
            split="test",
            total_tasks=100,
            pending_tasks=100,
            started_at="2026-07-06T14:30:52Z",
            status="running",
            task_job_map={"task_001": "aone-bench_task_001_20260706T143052-a1b2c3d4"},
        )
        assert m.run_id == "20260706T143052-a1b2c3d4"
        assert m.dataset == "alibaba/aone-bench"
        assert m.split == "test"
        assert m.total_tasks == 100
        assert m.pending_tasks == 100
        assert m.status == "running"
        assert m.finished_at is None
        assert m.summary is None

    def test_completed_with_summary(self):
        from rock.sdk.job.meta import RunMeta, RunScoreSummary

        m = RunMeta(
            run_id="20260706T143052-a1b2c3d4",
            dataset="alibaba/aone-bench",
            split="test",
            total_tasks=100,
            pending_tasks=100,
            started_at="2026-07-06T14:30:52Z",
            finished_at="2026-07-06T18:00:00Z",
            status="completed",
            task_job_map={"task_001": "job_001"},
            summary=RunScoreSummary(
                completed=80, failed=20, skipped=0, avg_score=0.42, total_score=33.6, pass_rate=0.8,
            ),
        )
        assert m.status == "completed"
        assert m.summary.avg_score == 0.42

    def test_serialization_roundtrip(self):
        from rock.sdk.job.meta import RunMeta

        m = RunMeta(
            run_id="20260706T143052-a1b2c3d4",
            dataset="org/ds",
            split="test",
            total_tasks=50,
            pending_tasks=50,
            started_at="2026-07-06T14:30:52Z",
            status="running",
            task_job_map={"t1": "j1", "t2": "j2"},
        )
        data = json.loads(m.model_dump_json())
        m2 = RunMeta.model_validate(data)
        assert m2.run_id == "20260706T143052-a1b2c3d4"
        assert m2.task_job_map == {"t1": "j1", "t2": "j2"}

    def test_run_id_format(self):
        """run_id should follow {YYYYMMDD}T{HHmmss}-{8hex} format."""
        import re

        from rock.sdk.job.meta import RunMeta

        m = RunMeta(
            run_id="20260706T143052-a1b2c3d4",
            dataset="org/ds",
            split="test",
            total_tasks=10,
            pending_tasks=10,
            started_at="2026-07-06T14:30:52Z",
            status="running",
            task_job_map={},
        )
        pattern = r"^\d{8}T\d{6}-[0-9a-f]{8}$"
        assert re.match(pattern, m.run_id)
