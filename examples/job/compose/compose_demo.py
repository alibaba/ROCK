"""ComposeJobConfig end-to-end demo using ROCK Job SDK.

Runs a harbor task (claude-code agent on terminal-bench / aone-bench-java100)
using ``ComposeJobConfig`` — the multi-container compose variant of JobConfig.

The outer DinD sandbox (docker:27-dind) provides the Docker daemon.
Inside, runner.sh orchestrates:
  - main container  → harbor runner (main.sh)
  - proxy sidecar   → claude-code proxy (port 8082)

Usage:
    python examples/job/compose/compose_demo.py -c examples/job/compose/job_config.yaml.template

Required environment variables (forwarded into the sandbox via environment.env):
    MODEL               Model name, e.g. claude-opus-4-8
    MODEL_API_KEY       API key for the model
    MODEL_BASE_URL      Base URL for the model API
    ROCK_TOKEN          ROCK cluster auth token (injected as XRL-Authorization header)
    OSS_ACCESS_KEY_ID   Alibaba Cloud OSS access key ID
    OSS_ACCESS_KEY_SECRET Alibaba Cloud OSS access key secret
    OSS_REGION          OSS region, e.g. cn-hangzhou
    OSS_ENDPOINT        OSS endpoint, e.g. oss-cn-hangzhou-internal.aliyuncs.com
    OSS_BUCKET          OSS bucket name
"""

import argparse
import asyncio
import logging
import os
import sys

from rock.sdk.job import Job, JobConfig

_REQUIRED_ENV_VARS = [
    "MODEL",
    "MODEL_API_KEY",
    "MODEL_BASE_URL",
    "ROCK_TOKEN",
    "OSS_ACCESS_KEY_ID",
    "OSS_ACCESS_KEY_SECRET",
    "OSS_REGION",
    "OSS_ENDPOINT",
    "OSS_BUCKET",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
# reduce httpx noise
logging.getLogger("httpx").setLevel(logging.WARNING)


def check_env() -> None:
    missing = [v for v in _REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        print("Missing required environment variables:")
        for v in missing:
            print(f"  {v}")
        print("\nSet them with `source .env` or export them manually.")
        sys.exit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a ComposeJobConfig job (harbor + cc-proxy sidecar) inside a ROCK DinD sandbox"
    )
    parser.add_argument("-c", "--config", required=True, help="Path to ComposeJobConfig YAML file")
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> None:
    config = JobConfig.from_yaml(args.config)
    logger.info(f"Loaded config: {config.__class__.__name__}, job_name={config.job_name}")

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
    check_env()
    args = parse_args()
    asyncio.run(async_main(args))
