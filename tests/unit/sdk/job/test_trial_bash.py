"""Tests for rock.sdk.job.trial.bash — BashTrial."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from rock.sdk.envhub import EnvironmentConfig
from rock.sdk.envhub.config import OssMirrorConfig
from rock.sdk.job.config import BashJobConfig
from rock.sdk.job.trial.bash import BashTrial
from rock.sdk.job.trial.registry import _create_trial


def _success_obs():
    obs = MagicMock()
    obs.exit_code = 0
    return obs


# ---------------------------------------------------------------------------
# BashTrial.build()
# ---------------------------------------------------------------------------


class TestBashTrialBuild:
    def test_build_basic_script(self):
        cfg = BashJobConfig(script="echo hello")
        trial = BashTrial(cfg)
        out = trial.build()
        assert "#!/bin/bash" in out
        assert "set -e" in out
        assert "echo hello" in out


# ---------------------------------------------------------------------------
# BashTrial.setup()
# ---------------------------------------------------------------------------


class TestBashTrialSetup:
    async def test_setup_uploads_dirs(self, tmp_path):
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        cfg = BashJobConfig(
            script="echo hi",
            environment=EnvironmentConfig(
                uploads=[(str(dir_a), "/sandbox/a"), (str(dir_b), "/sandbox/b")],
            ),
        )
        trial = BashTrial(cfg)
        mock_sandbox = AsyncMock()
        mock_sandbox.fs.upload_dir = AsyncMock(return_value=_success_obs())

        await trial.setup(mock_sandbox)

        assert mock_sandbox.fs.upload_dir.call_count == 2

    async def test_setup_reads_script_path(self):
        expected = "expected content"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
            f.write(expected)
            tmp_path = f.name
        try:
            cfg = BashJobConfig(script_path=tmp_path)
            trial = BashTrial(cfg)
            mock_sandbox = AsyncMock()
            mock_sandbox.fs.upload_dir = AsyncMock(return_value=_success_obs())

            await trial.setup(mock_sandbox)

            assert trial._config.script == expected
        finally:
            Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# BashTrial.collect()
# ---------------------------------------------------------------------------


class TestBashTrialCollect:
    async def test_collect_exit_code_zero(self):
        cfg = BashJobConfig(script="echo hi", job_name="myjob")
        trial = BashTrial(cfg)
        mock_sandbox = AsyncMock()

        result = await trial.collect(mock_sandbox, output="hi\n", exit_code=0)

        assert result.exception_info is None
        assert result.task_name == "myjob"
        assert result.status == "completed"

    async def test_collect_exit_code_nonzero(self):
        cfg = BashJobConfig(script="false", job_name="myjob")
        trial = BashTrial(cfg)
        mock_sandbox = AsyncMock()

        result = await trial.collect(mock_sandbox, output="", exit_code=1)

        assert result.exception_info is not None
        assert result.exception_info.exception_type == "BashExitCode"
        assert result.status == "failed"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestBashTrialRegistration:
    def test_bash_config_creates_bash_trial(self):
        cfg = BashJobConfig(script="echo hi")
        trial = _create_trial(cfg)
        assert isinstance(trial, BashTrial)


# ---------------------------------------------------------------------------
# G4: on_sandbox_ready hook — backfill namespace / experiment_id
# Behavior is inherited from AbstractTrial: BashTrial must also backfill.
# ---------------------------------------------------------------------------


class TestBashTrialOnSandboxReady:
    """G4: BashTrial inherits on_sandbox_ready from AbstractTrial and must backfill namespace/experiment_id."""

    async def test_namespace_backfilled_when_config_unset(self):
        cfg = BashJobConfig(script="echo hi")
        trial = BashTrial(cfg)
        sandbox = MagicMock()
        type(sandbox).namespace = PropertyMock(return_value="sb-ns")
        type(sandbox).experiment_id = PropertyMock(return_value="exp-1")

        await trial.on_sandbox_ready(sandbox)

        assert cfg.namespace == "sb-ns"
        assert cfg.experiment_id == "exp-1"

    async def test_experiment_id_config_takes_priority_over_sandbox(self):
        """Config experiment_id overrides sandbox's different value — no error raised."""
        cfg = BashJobConfig(script="echo hi", experiment_id="claw-eval")
        trial = BashTrial(cfg)
        sandbox = MagicMock()
        type(sandbox).namespace = PropertyMock(return_value=None)
        type(sandbox).experiment_id = PropertyMock(return_value="default")

        await trial.on_sandbox_ready(sandbox)

        assert cfg.experiment_id == "claw-eval"

    async def test_namespace_mismatch_raises(self):
        cfg = BashJobConfig(script="echo hi", namespace="cfg-ns")
        trial = BashTrial(cfg)
        sandbox = MagicMock()
        type(sandbox).namespace = PropertyMock(return_value="sb-ns")
        type(sandbox).experiment_id = PropertyMock(return_value=None)

        with pytest.raises(ValueError, match="namespace mismatch"):
            await trial.on_sandbox_ready(sandbox)


# ---------------------------------------------------------------------------
# OSS mirror integration
# ---------------------------------------------------------------------------


def _oss_sandbox(ns="ns", exp="exp"):
    """Minimal sandbox mock with oss_mirror support."""
    sb = AsyncMock()
    type(sb).namespace = PropertyMock(return_value=ns)
    type(sb).experiment_id = PropertyMock(return_value=exp)
    sb.arun = AsyncMock(return_value=MagicMock(exit_code=0, output=""))
    sb.fs.ensure_ossutil = AsyncMock(return_value=True)
    sb.fs.upload_dir = AsyncMock(return_value=MagicMock(exit_code=0))
    return sb


_MIRROR = OssMirrorConfig(enabled=True, oss_bucket="b", oss_endpoint="ep", oss_region="rg")


class TestBashTrialOssMirror:
    async def test_setup_installs_ossutil_and_creates_dir(self):
        cfg = BashJobConfig(
            script="echo",
            job_name="j",
            namespace="ns",
            experiment_id="exp",
            environment=EnvironmentConfig(oss_mirror=_MIRROR),
        )
        trial = BashTrial(cfg)
        sb = _oss_sandbox()
        await trial.setup(sb)

        sb.fs.ensure_ossutil.assert_called_once()
        # Initial upload to create OSS path before script runs
        setup_cp_calls = [c for c in sb.arun.call_args_list if "ossutil cp" in str(c)]
        assert len(setup_cp_calls) == 1

    async def test_setup_skips_when_no_mirror(self):
        trial = BashTrial(BashJobConfig(script="echo"))
        sb = _oss_sandbox()
        await trial.setup(sb)
        sb.fs.ensure_ossutil.assert_not_called()

    async def test_collect_uploads(self):
        cfg = BashJobConfig(
            script="echo",
            job_name="j",
            namespace="ns",
            experiment_id="exp",
            environment=EnvironmentConfig(oss_mirror=_MIRROR),
        )
        trial = BashTrial(cfg)
        sb = _oss_sandbox()
        await trial.setup(sb)
        await trial.collect(sb, "ok", 0)

        # setup + collect each call ossutil cp once
        arun_calls = [c for c in sb.arun.call_args_list if "ossutil cp" in str(c)]
        assert len(arun_calls) == 2
        assert all("oss://b/artifacts/ns/exp/j/" in str(c) for c in arun_calls)

    async def test_upload_failure_does_not_fail_job(self):
        cfg = BashJobConfig(
            script="echo",
            job_name="j",
            namespace="ns",
            experiment_id="exp",
            environment=EnvironmentConfig(oss_mirror=_MIRROR),
        )
        trial = BashTrial(cfg)
        sb = _oss_sandbox()
        sb.arun = AsyncMock(return_value=MagicMock(exit_code=1, output="err"))
        await trial.setup(sb)
        result = await trial.collect(sb, "ok", 0)
        assert result.exit_code == 0 and result.exception_info is None

    async def test_skips_upload_when_ossutil_not_ready(self):
        cfg = BashJobConfig(
            script="echo",
            job_name="j",
            namespace="ns",
            experiment_id="exp",
            environment=EnvironmentConfig(oss_mirror=_MIRROR),
        )
        trial = BashTrial(cfg)
        sb = _oss_sandbox()
        sb.fs.ensure_ossutil = AsyncMock(return_value=False)
        await trial.setup(sb)
        await trial.collect(sb, "ok", 0)
        ossutil_calls = [c for c in sb.arun.call_args_list if "ossutil cp" in str(c)]
        assert len(ossutil_calls) == 0
