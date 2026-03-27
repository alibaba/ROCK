#!/usr/bin/env python3
"""Harbor 沙箱运行示例

通过 YAML 配置文件在 ROCK Sandbox 内运行 Harbor 任务。

使用示例:
    python examples/harbor/harbor_demo.py -c examples/harbor/job_config.yaml
"""

import argparse
import asyncio
import logging

from rock.sdk.agent import Job, JobConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="在 ROCK 沙箱内运行 Harbor 任务")
    parser.add_argument("-c", "--config", required=True, help="JobConfig YAML 配置文件路径")
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> None:
    config = JobConfig.from_yaml(args.config)
    result = await Job(config).run()

    logger.info(f"任务完成: exit_code={result.exit_code}, score={result.score}")
    if result.trials:
        for trial in result.trials:
            logger.info(f"  {trial.task_name}: score={trial.score} ({trial.status})")


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(async_main(args))
