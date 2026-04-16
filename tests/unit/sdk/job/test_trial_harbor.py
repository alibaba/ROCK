"""Tests for rock.sdk.job.trial.harbor — HarborTrial."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from rock.sdk.bench.models.job.config import HarborJobConfig
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
    async def test_collect_returns_list_of_all_sub_trials(self):
        """Harbor 一个 sandbox 常产出 N 个子 trial；collect 必须返回全部，不能只取第一条。"""
        cfg = HarborJobConfig(job_name="test", experiment_id="exp-1")
        trial = HarborTrial(cfg)

        trial_jsons = [
            {
                "task_name": f"task-{i}",
                "trial_name": f"trial-{i:03d}",
                "verifier_result": {"rewards": {"reward": float(i) / 3}},
                "agent_result": {},
                "exception_info": None,
            }
            for i in range(3)
        ]

        mock_sandbox = AsyncMock()
        list_result = MagicMock()
        list_result.stdout = "\n".join(f"{cfg.jobs_dir}/test/trial-{i:03d}/result.json" for i in range(3))
        mock_sandbox.execute = AsyncMock(return_value=list_result)

        async def _read(req):
            path = str(req.path)
            idx = int(path.split("trial-")[1].split("/")[0])
            resp = MagicMock()
            resp.content = json.dumps(trial_jsons[idx])
            return resp

        mock_sandbox.read_file = AsyncMock(side_effect=_read)

        result = await trial.collect(mock_sandbox, output="", exit_code=0)

        assert isinstance(result, list), f"collect must return list, got {type(result)}"
        assert len(result) == 3, f"expected 3 sub-trials, got {len(result)}"
        assert {r.task_name for r in result} == {"task-0", "task-1", "task-2"}

    async def test_collect_returns_list_with_synthetic_failure_when_no_trials(self):
        """Harbor 没写出任何 result.json 时，返回长度为 1 的 list，携带 HarborNoTrials 异常。"""
        cfg = HarborJobConfig(job_name="test", experiment_id="exp-1")
        trial = HarborTrial(cfg)

        mock_sandbox = AsyncMock()
        list_result = MagicMock()
        list_result.stdout = ""
        mock_sandbox.execute = AsyncMock(return_value=list_result)

        result = await trial.collect(mock_sandbox, output="", exit_code=0)

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].exception_info is not None
        assert result[0].exception_info.exception_type == "HarborNoTrials"


# ---------------------------------------------------------------------------
# Auto-registration
# ---------------------------------------------------------------------------


class TestHarborTrialRegistration:
    def test_harbor_config_creates_harbor_trial(self):
        cfg = HarborJobConfig(experiment_id="exp-1")
        trial = _create_trial(cfg)
        assert isinstance(trial, HarborTrial)


# ---------------------------------------------------------------------------
# G4: on_sandbox_ready hook — backfill namespace / experiment_id
# ---------------------------------------------------------------------------


class TestHarborTrialOnSandboxReady:
    """G4: HarborTrial must backfill namespace / experiment_id from sandbox into config."""

    async def test_namespace_backfilled_when_config_unset(self):
        cfg = HarborJobConfig(experiment_id="exp-1")
        trial = HarborTrial(cfg)
        sandbox = MagicMock()
        sandbox._namespace = "sb-ns"
        sandbox._experiment_id = "exp-1"

        await trial.on_sandbox_ready(sandbox)

        assert cfg.namespace == "sb-ns"

    async def test_experiment_id_config_takes_priority_over_sandbox(self):
        """Config experiment_id overrides sandbox's different value — no error raised."""
        cfg = HarborJobConfig(experiment_id="claw-eval")
        trial = HarborTrial(cfg)
        sandbox = MagicMock()
        sandbox._namespace = None
        sandbox._experiment_id = "default"

        await trial.on_sandbox_ready(sandbox)

        assert cfg.experiment_id == "claw-eval"

    async def test_namespace_mismatch_raises(self):
        import pytest

        cfg = HarborJobConfig(experiment_id="exp-1", namespace="cfg-ns")
        trial = HarborTrial(cfg)
        sandbox = MagicMock()
        sandbox._namespace = "sb-ns"
        sandbox._experiment_id = None

        with pytest.raises(ValueError, match="namespace mismatch"):
            await trial.on_sandbox_ready(sandbox)
