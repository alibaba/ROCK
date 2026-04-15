"""Harbor benchmark demo using ROCK Job SDK (new path).

Uses ``rock.sdk.job.Job`` with ``HarborJobConfig`` — the recommended path
with full feature parity (G1-G7 fixed) and scatter / multiple trial types.

For the legacy path (``rock.sdk.bench.Job``), see ``harbor_demo_legacy.py``.

Usage:
    python examples/harbor/harbor_demo.py -c examples/harbor/swe.intern.yaml
    python examples/harbor/harbor_demo.py -c examples/harbor/tb_job_config.yaml -t mailman

Required environment variables (OSS_* are auto-forwarded into the sandbox):
    OSS_ACCESS_KEY_ID       Alibaba Cloud OSS access key ID
    OSS_ACCESS_KEY_SECRET   Alibaba Cloud OSS access key secret
    OSS_REGION              OSS region, e.g. cn-shanghai
    OSS_ENDPOINT            OSS endpoint, e.g. oss-cn-shanghai.aliyuncs.com
    OSS_BUCKET              OSS bucket name
    OSS_DATASET_PATH        Path prefix inside the bucket for datasets

Recommended setup:
    1. Copy .env.example to .env and fill in your credentials
    2. source .env
    3. python examples/harbor/harbor_demo.py -c ...
"""

import argparse
import asyncio
import logging
import os
import sys

from rock.sdk.job import Job, JobConfig

_REQUIRED_ENV_VARS = [
    "OSS_ACCESS_KEY_ID",
    "OSS_ACCESS_KEY_SECRET",
    "OSS_REGION",
    "OSS_ENDPOINT",
    "OSS_BUCKET",
    "OSS_DATASET_PATH",
]


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
# disable httpx
logging.getLogger("httpx").setLevel(logging.WARNING)


def check_oss_env() -> None:
    missing = [v for v in _REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        print("Missing required environment variables:")
        for v in missing:
            print(f"  {v}")
        print("\nSet them with `source .env` or export them manually.")
        sys.exit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Harbor tasks inside a ROCK sandbox")
    parser.add_argument("-c", "--config", required=True, help="Path to HarborJobConfig YAML file")
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
    check_oss_env()
    args = parse_args()
    asyncio.run(async_main(args))
