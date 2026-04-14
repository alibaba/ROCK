"""Tests for rock.sdk.job.trial.harbor — HarborTrial."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

# Pre-import bench to avoid circular-import pitfalls in rock.sdk.job.config
import rock.sdk.bench  # noqa: F401
from rock.sdk.bench.models.job.config import JobConfig as HarborJobConfig
from rock.sdk.job.trial.harbor import HarborTrial
from rock.sdk.job.trial.registry import _create_trial


def _success_obs():
    obs = MagicMock()
    obs.exit_code = 0
    return obs


# ---------------------------------------------------------------------------
# HarborTrial.build()
# ---------------------------------------------------------------------------


class TestHarborTrialBuild:
    def test_build_contains_harbor_jobs_start(self):
        cfg = HarborJobConfig(job_name="test", experiment_id="exp-1")
        trial = HarborTrial(cfg)
        script = trial.build()
        assert "harbor jobs start -c" in script

    def test_build_contains_dockerd_startup(self):
        cfg = HarborJobConfig(job_name="test", experiment_id="exp-1")
        trial = HarborTrial(cfg)
        script = trial.build()
        assert "dockerd" in script

    def test_build_contains_shebang_and_set_e(self):
        cfg = HarborJobConfig(job_name="test", experiment_id="exp-1")
        trial = HarborTrial(cfg)
        script = trial.build()
        assert "#!/bin/bash" in script
        assert "set -e" in script

    def test_build_with_setup_commands_includes_them(self):
        cfg = HarborJobConfig(
            job_name="test",
            experiment_id="exp-1",
            setup_commands=["pip install harbor"],
        )
        trial = HarborTrial(cfg)
        script = trial.build()
        assert "pip install harbor" in script

    def test_build_without_setup_commands_uses_placeholder(self):
        cfg = HarborJobConfig(job_name="test", experiment_id="exp-1")
        trial = HarborTrial(cfg)
        script = trial.build()
        assert "No setup commands" in script


# ---------------------------------------------------------------------------
# HarborTrial.setup()
# ---------------------------------------------------------------------------


class TestHarborTrialSetup:
    async def test_setup_uploads_harbor_yaml(self):
        cfg = HarborJobConfig(job_name="test", experiment_id="exp-1")
        trial = HarborTrial(cfg)
        mock_sandbox = AsyncMock()
        mock_sandbox.fs.upload_dir = AsyncMock(return_value=_success_obs())
        mock_sandbox.write_file_by_path = AsyncMock()

        await trial.setup(mock_sandbox)

        mock_sandbox.write_file_by_path.assert_called_once()
        args, kwargs = mock_sandbox.write_file_by_path.call_args
        yaml_content = args[0] if args else kwargs.get("content")
        # Harbor YAML serializes `agents` field from HarborJobConfig
        assert "agents:" in yaml_content


# ---------------------------------------------------------------------------
# HarborTrial.collect()
# ---------------------------------------------------------------------------


class TestHarborTrialCollect:
    async def test_collect_with_trial_results_found(self):
        cfg = HarborJobConfig(job_name="test", experiment_id="exp-1")
        trial = HarborTrial(cfg)

        trial_json = {
            "task_name": "fix-dockerfile",
            "trial_name": "trial-001",
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:01:00Z",
            "verifier_result": {"rewards": {"reward": 1.0}},
            "agent_result": {},
            "exception_info": None,
        }

        mock_sandbox = AsyncMock()
        list_result = MagicMock()
        list_result.stdout = f"{cfg.jobs_dir}/test/trial-001/result.json\n"
        mock_sandbox.execute = AsyncMock(return_value=list_result)

        read_response = MagicMock()
        read_response.content = json.dumps(trial_json)
        mock_sandbox.read_file = AsyncMock(return_value=read_response)

        result = await trial.collect(mock_sandbox, output="", exit_code=0)

        assert result.task_name == "fix-dockerfile"
        assert result.exception_info is None
        assert result.score == 1.0

    async def test_collect_with_no_trials(self):
        cfg = HarborJobConfig(job_name="test", experiment_id="exp-1")
        trial = HarborTrial(cfg)

        mock_sandbox = AsyncMock()
        list_result = MagicMock()
        list_result.stdout = ""
        mock_sandbox.execute = AsyncMock(return_value=list_result)

        result = await trial.collect(mock_sandbox, output="", exit_code=0)

        assert result.exception_info is not None
        assert result.exception_info.exception_type == "HarborNoTrials"


# ---------------------------------------------------------------------------
# Auto-registration
# ---------------------------------------------------------------------------


class TestHarborTrialRegistration:
    def test_harbor_config_creates_harbor_trial(self):
        cfg = HarborJobConfig(experiment_id="exp-1")
        trial = _create_trial(cfg)
        assert isinstance(trial, HarborTrial)
