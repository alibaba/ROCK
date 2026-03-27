#!/usr/bin/env python3
"""Harbor sandbox demo.

Run Harbor benchmark tasks inside a ROCK sandbox using a YAML config file.

Usage:
    python examples/harbor/harbor_demo.py -c examples/harbor/job_config.yaml
"""

import argparse
import asyncio
import logging

from rock.sdk.agent import Job, JobConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
# disable httpx
logging.getLogger("httpx").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Harbor tasks inside a ROCK sandbox")
    parser.add_argument("-c", "--config", required=True, help="Path to JobConfig YAML file")
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> None:
    config = JobConfig.from_yaml(args.config)
    result = await Job(config).run()

    logger.info(f"Job completed: exit_code={result.exit_code}, score={result.score}")
    if result.trials:
        for trial in result.trials:
            logger.info(f"  {trial.task_name}: score={trial.score} ({trial.status})")


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(async_main(args))
