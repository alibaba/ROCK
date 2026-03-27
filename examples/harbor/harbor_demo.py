#!/usr/bin/env python3
"""Harbor sandbox demo.

Run Harbor benchmark tasks inside a ROCK sandbox using a YAML config file.

Usage:
    python examples/harbor/harbor_demo.py -c examples/harbor/job_config.yaml
    python examples/harbor/harbor_demo.py -c examples/harbor/tb_job_config.yaml -t mailman
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
    parser.add_argument("-t", "--task", default=None, help="Task name to run (overrides config)")
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> None:
    config = JobConfig.from_yaml(args.config)

    # Override task_names if specified via CLI
    if args.task and config.datasets:
        config.datasets[0].task_names = [args.task]

    result = await Job(config).run()

    logger.info(f"result: {result}")
    logger.info(f"Job completed: exit_code={result.exit_code}, score={result.score}")
    if result.trial_results:
        for trial in result.trial_results:
            logger.info(f"  {trial.task_name}: score={trial.score} ({trial.status})")
            if trial.exception_info:
                logger.info(
                    f"    error: {trial.exception_info.exception_type}: {trial.exception_info.exception_message}"
                )


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(async_main(args))
