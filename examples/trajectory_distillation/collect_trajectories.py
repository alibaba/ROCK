"""Trajectory collection example using ROCK Job SDK."""

import asyncio

from rock.sdk.job import Job, JobConfig
from rock.sdk.job.operator import ScatterOperator


async def main():
    config = JobConfig.from_yaml("examples/trajectory_distillation/distill_job_config.yaml")

    job = Job(config, operator=ScatterOperator(size=1))
    result = await job.run()

    print(f"Job completed: exit_code={result.exit_code}, score={result.score}")
    for trial in result.trial_results:
        print(f"  {trial.task_name}: score={trial.score}, status={trial.status}")
        if trial.exception_info:
            print(f"    error: {trial.exception_info.exception_message}")


if __name__ == "__main__":
    asyncio.run(main())
