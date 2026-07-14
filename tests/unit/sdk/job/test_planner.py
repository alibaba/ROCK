from __future__ import annotations

import pytest

from rock.sdk.bench.models.job.config import HarborJobConfig, OssRegistryInfo, RegistryDatasetConfig
from rock.sdk.job.config import BashJobConfig


def make_harbor_config() -> HarborJobConfig:
    return HarborJobConfig(
        experiment_id="exp-1",
        job_name="template",
        labels={"keep": "yes"},
        datasets=[
            RegistryDatasetConfig(
                name="old/bench",
                registry=OssRegistryInfo(split="dev"),
            )
        ],
    )


class TestSingleTaskPlanner:
    def test_plan_clones_harbor_config_for_single_task_without_mutating_template(self):
        from rock.sdk.job.planner import ResolvedTask, SingleTaskPlanner
        from rock.sdk.job.trial.harbor import HarborTrial

        template = make_harbor_config()
        planner = SingleTaskPlanner(run_id="run-123")

        planned = planner.plan(
            template,
            task=ResolvedTask(task_id="task-001", org="alibaba", dataset="alibaba/aone-bench", split="test"),
        )

        assert planned.task_id == "task-001"
        assert planned.config is not template
        assert planned.config.datasets[0].name == "alibaba/aone-bench"
        assert planned.config.datasets[0].task_names == ["task-001"]
        assert planned.config.datasets[0].registry.split == "test"
        assert planned.config.datasets[0].version == "test"
        assert planned.config.job_name == "aone-bench_task-001_run-123"
        assert planned.config.labels["rock_run_id"] == "run-123"
        assert planned.config.labels["rock_task_id"] == "task-001"
        assert planned.config.labels["keep"] == "yes"
        assert isinstance(planned.trial, HarborTrial)

        assert template.datasets[0].name == "old/bench"
        assert template.datasets[0].task_names is None
        assert template.labels == {"keep": "yes"}

    def test_plan_clones_bash_config_and_injects_task_env(self):
        from rock.sdk.job.planner import ResolvedTask, SingleTaskPlanner
        from rock.sdk.job.trial.bash import BashTrial

        template = BashJobConfig(script="echo $TASK", environment={"env": {"TASK": "old", "KEEP": "1"}})
        planner = SingleTaskPlanner(run_id="run-123")

        planned = planner.plan(template, task=ResolvedTask(task_id="task-001", dataset="bench", split="test"))

        assert planned.config is not template
        assert planned.config.environment.env["TASK"] == "task-001"
        assert planned.config.environment.env["ROCK_TASK_ID"] == "task-001"
        assert planned.config.environment.env["ROCK_RUN_ID"] == "run-123"
        assert planned.config.environment.env["ROCK_JOB_NAME"] == "bench_task-001_run-123"
        assert planned.config.environment.env["ROCK_DATASET"] == "bench"
        assert planned.config.environment.env["ROCK_SPLIT"] == "test"
        assert template.environment.env["TASK"] == "old"
        assert isinstance(planned.trial, BashTrial)

    def test_plan_preserves_config_job_name_when_requested(self):
        from rock.sdk.job.planner import ResolvedTask, SingleTaskPlanner

        template = BashJobConfig(job_name="yaml-job", script="echo $TASK")
        planner = SingleTaskPlanner(run_id="run-123", preserve_job_name=True)

        planned = planner.plan(template, task=ResolvedTask(task_id="task-001", dataset="bench", split="test"))

        assert planned.job_name == "yaml-job"
        assert planned.config.job_name == "yaml-job"
        assert planned.config.environment.env["ROCK_JOB_NAME"] == "yaml-job"
