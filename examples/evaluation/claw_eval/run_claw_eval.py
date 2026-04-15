"""Run claw-eval via BashJob SDK.

Usage:
    cd examples/agents/claw_eval
    cp claw_eval_bashjob.yaml.template claw_eval_bashjob.yaml
    # fill in real values
    python run_claw_eval.py
    # or specify a config path:
    python run_claw_eval.py my_config.yaml
"""

import asyncio
import os
import sys
from pathlib import Path

from rock.sdk.job import Job, JobConfig


async def main() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "claw_eval_bashjob.yaml"
    config = JobConfig.from_yaml(config_path)
    result = await Job(config).run()
    print(f"Job completed: status={result.status}, trials={len(result.trial_results)}")


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent)
    asyncio.run(main())
