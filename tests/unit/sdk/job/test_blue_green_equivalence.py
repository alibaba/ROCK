"""G1-G7 blue-green equivalence: rock.sdk.bench.Job vs rock.sdk.job.Job.

Both paths must produce equivalent JobResult for the same HarborJobConfig +
identical mock sandbox behavior. This locks in the contract across the
deprecation window.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from rock.sdk.bench.models.job.config import (
    HarborJobConfig,
    RegistryDatasetConfig,
    RemoteRegistryInfo,
)
from rock.sdk.bench.models.trial.config import AgentConfig, RockEnvironmentConfig


def _build_mock_sandbox():
    sandbox = AsyncMock()
    sandbox.sandbox_id = "sb-bg"
    sandbox._namespace = "bg-ns"
    sandbox._experiment_id = "bg-exp"
    sandbox.start = AsyncMock()
    sandbox.close = AsyncMock()
    sandbox.create_session = AsyncMock()
    sandbox.upload_by_path = AsyncMock(return_value=MagicMock(success=True))
    sandbox.write_file_by_path = AsyncMock()
    sandbox.fs = AsyncMock()
    sandbox.fs.upload_dir = AsyncMock(return_value=MagicMock(exit_code=0))

    # find lists 2 result.json files
    find_obs = MagicMock()
    find_obs.stdout = (
        "/data/logs/user-defined/jobs/bg-exp/trial-0/result.json\n"
        "/data/logs/user-defined/jobs/bg-exp/trial-1/result.json\n"
    )
    sandbox.execute = AsyncMock(return_value=find_obs)

    async def _read(req):
        path = str(req.path)
        idx = int(path.rsplit("trial-", 1)[1].split("/")[0])
        resp = MagicMock()
        resp.content = json.dumps(
            {
                "task_name": f"t-{idx}",
                "trial_name": f"trial-{idx:03d}",
                "verifier_result": {"rewards": {"reward": 1.0 if idx == 0 else 0.0}},
                "agent_result": {},
                "exception_info": None,
            }
        )
        return resp

    sandbox.read_file = AsyncMock(side_effect=_read)
    sandbox.start_nohup_process = AsyncMock(return_value=(42, None))
    sandbox.wait_for_process_completion = AsyncMock(return_value=(True, "done"))

    obs = MagicMock()
    obs.output = "harbor stdout"
    obs.exit_code = 0
    sandbox.handle_nohup_output = AsyncMock(return_value=obs)
    return sandbox


def _make_config():
    return HarborJobConfig(
        experiment_id="bg-exp",
        job_name="bg-job",
        labels={"team": "rl", "step": "1"},
        agents=[AgentConfig(name="a", max_timeout_sec=1800)],
        datasets=[
            RegistryDatasetConfig(
                registry=RemoteRegistryInfo(),
                name="terminal-bench",
                version="2.0",
            )
        ],
        environment=RockEnvironmentConfig(),
    )


class TestBlueGreenEquivalence:
    async def test_two_sub_trials_flattened_on_both_paths(self):
        from rock.sdk.bench import Job as BlueJob
        from rock.sdk.job import Job as GreenJob

        mock_blue = _build_mock_sandbox()
        with patch("rock.sdk.sandbox.client.Sandbox", return_value=mock_blue):
            blue_result = await BlueJob(_make_config()).run()

        mock_green = _build_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_green):
            green_result = await GreenJob(_make_config()).run()

        assert blue_result.status == green_result.status
        assert len(blue_result.trial_results) == len(green_result.trial_results) == 2
        blue_tasks = sorted(t.task_name for t in blue_result.trial_results)
        green_tasks = sorted(t.task_name for t in green_result.trial_results)
        assert blue_tasks == green_tasks

    async def test_labels_preserved_on_both_paths(self):
        from rock.sdk.bench import Job as BlueJob
        from rock.sdk.job import Job as GreenJob

        mock_blue = _build_mock_sandbox()
        with patch("rock.sdk.sandbox.client.Sandbox", return_value=mock_blue):
            blue_result = await BlueJob(_make_config()).run()

        mock_green = _build_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_green):
            green_result = await GreenJob(_make_config()).run()

        assert blue_result.labels == green_result.labels == {"team": "rl", "step": "1"}

    async def test_raw_output_and_exit_code_populated_on_both(self):
        from rock.sdk.bench import Job as BlueJob
        from rock.sdk.job import Job as GreenJob

        mock_blue = _build_mock_sandbox()
        with patch("rock.sdk.sandbox.client.Sandbox", return_value=mock_blue):
            blue_result = await BlueJob(_make_config()).run()

        mock_green = _build_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_green):
            green_result = await GreenJob(_make_config()).run()

        assert blue_result.raw_output == "harbor stdout"
        assert green_result.raw_output == "harbor stdout"
        assert blue_result.exit_code == 0
        assert green_result.exit_code == 0

    async def test_sandbox_not_closed_on_both_paths(self):
        from rock.sdk.bench import Job as BlueJob
        from rock.sdk.job import Job as GreenJob

        mock_blue = _build_mock_sandbox()
        with patch("rock.sdk.sandbox.client.Sandbox", return_value=mock_blue):
            await BlueJob(_make_config()).run()

        mock_green = _build_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_green):
            await GreenJob(_make_config()).run()

        mock_blue.close.assert_not_called()
        mock_green.close.assert_not_called()
