import asyncio
import os
from pathlib import Path

from rock.sdk.agent.job import Job
from rock.sdk.agent.models.job.config import JobConfig


async def main() -> None:
    config = JobConfig.from_yaml("claw_eval_job_config.yaml")
    await Job(config).run()


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent)
    asyncio.run(main())
