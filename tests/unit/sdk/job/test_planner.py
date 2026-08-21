from __future__ import annotations

from uuid import UUID

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
        planner = SingleTaskPlanner()
        job_id = UUID("12345678-1234-5678-1234-567812345678")

        planned = planner.plan(
            template,
            task=ResolvedTask(task_id="task-001", org="alibaba", dataset="alibaba/aone-bench", split="test"),
            job_id=job_id,
        )

        assert planned.job_id == job_id
        assert planned.task_id == "task-001"
        assert planned.config is not template
        assert planned.config.datasets[0].name == "alibaba/aone-bench"
        assert planned.config.datasets[0].task_names == ["task-001"]
        assert planned.config.datasets[0].registry.split == "test"
        assert planned.config.datasets[0].version == "test"
        assert planned.config.job_name == "aone-bench_task-001_12345678"
        assert planned.config.labels["rock_job_id"] == str(job_id)
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
        planner = SingleTaskPlanner()
        job_id = UUID("12345678-1234-5678-1234-567812345678")

        planned = planner.plan(
            template,
            task=ResolvedTask(task_id="task-001", dataset="bench", split="test"),
            job_id=job_id,
        )

        assert planned.job_id == job_id
        assert planned.config is not template
        assert planned.config.environment.env["TASK"] == "task-001"
        assert planned.config.environment.env["ROCK_TASK_ID"] == "task-001"
        assert planned.config.environment.env["ROCK_JOB_ID"] == str(job_id)
        assert planned.config.environment.env["ROCK_JOB_NAME"] == "bench_task-001_12345678"
        assert planned.config.environment.env["ROCK_DATASET"] == "bench"
        assert planned.config.environment.env["ROCK_SPLIT"] == "test"
        assert template.environment.env["TASK"] == "old"
        assert isinstance(planned.trial, BashTrial)

    def test_plan_preserves_config_job_name_when_requested(self):
        from rock.sdk.job.planner import ResolvedTask, SingleTaskPlanner

        template = BashJobConfig(job_name="yaml-job", script="echo $TASK")
        planner = SingleTaskPlanner(preserve_job_name=True)
        job_id = UUID("12345678-1234-5678-1234-567812345678")

        planned = planner.plan(
            template,
            task=ResolvedTask(task_id="task-001", dataset="bench", split="test"),
            job_id=job_id,
        )

        assert planned.job_id == job_id
        assert planned.job_name == "yaml-job"
        assert planned.config.job_name == "yaml-job"
        assert planned.config.labels["rock_job_id"] == str(job_id)
        assert planned.config.environment.env["ROCK_JOB_NAME"] == "yaml-job"

    def test_plan_same_task_twice_gets_distinct_job_ids_and_names(self):
        from rock.sdk.job.planner import ResolvedTask, SingleTaskPlanner

        planner = SingleTaskPlanner()
        template = BashJobConfig(script="echo $TASK")
        task = ResolvedTask(task_id="task-001", dataset="bench")

        first = planner.plan(template, task=task)
        second = planner.plan(template, task=task)

        assert first.job_id != second.job_id
        assert first.job_name != second.job_name
