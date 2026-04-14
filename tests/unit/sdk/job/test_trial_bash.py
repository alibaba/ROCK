"""Tests for rock.sdk.job.trial.bash — BashTrial."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Import bench first to avoid circular-import pitfall in rock.sdk.job.config
import rock.sdk.bench  # noqa: F401
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

    def test_build_with_setup_commands(self):
        cfg = BashJobConfig(
            setup_commands=["pip install -r requirements.txt"],
            script="python main.py",
        )
        trial = BashTrial(cfg)
        out = trial.build()

        assert "pip install -r requirements.txt" in out
        assert "python main.py" in out
        # Setup comes before main script
        assert out.index("pip install -r requirements.txt") < out.index("python main.py")

    def test_build_no_script_only_setup(self):
        cfg = BashJobConfig(setup_commands=["echo setup"])
        trial = BashTrial(cfg)
        out = trial.build()
        assert "#!/bin/bash" in out
        assert "set -e" in out
        assert "echo setup" in out


# ---------------------------------------------------------------------------
# BashTrial.setup()
# ---------------------------------------------------------------------------


class TestBashTrialSetup:
    async def test_setup_uploads_files(self):
        cfg = BashJobConfig(
            script="echo hi",
            file_uploads=[("/local/a", "/sandbox/a"), ("/local/b", "/sandbox/b")],
        )
        trial = BashTrial(cfg)
        mock_sandbox = AsyncMock()
        mock_sandbox.fs.upload_dir = AsyncMock(return_value=_success_obs())

        await trial.setup(mock_sandbox)

        assert mock_sandbox.fs.upload_dir.call_count == 2
        mock_sandbox.fs.upload_dir.assert_any_call(source_dir="/local/a", target_dir="/sandbox/a")
        mock_sandbox.fs.upload_dir.assert_any_call(source_dir="/local/b", target_dir="/sandbox/b")

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
