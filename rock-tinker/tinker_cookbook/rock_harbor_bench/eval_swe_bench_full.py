"""
Load tasks from a data file and run evaluation.

Usage:
    TINKER_API_KEY=tml-test python3 tinker_cookbook/rock_harbor_bench/eval_swe_bench.py /cpfs04/user/wuhaotian.wht/data/SWE-Env.jsonl

    # Set TINKER_API_KEY to your API key before running.
    # The data file should be JSONL format, one task per line.

"""

import asyncio
import sys

from tinker_cookbook.eval import EvalConfig, HarborTask, run_eval
from tinker_cookbook.rock_harbor_bench.env import load_harbor_tasks


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: uv run python tinker_cookbook/rock-harbor-bench/eval_swe_bench.py <data_path>")
        sys.exit(1)

    data_path = sys.argv[1]
    config = EvalConfig(
        model_name ="Qwen/Qwen3-4B-Instruct-2507",
        tokenizer_path ="/root/.cache/modelscope/hub/models/Qwen/Qwen3-4B-Instruct-2507",
        renderer_name = "qwen3",
        max_turns=200,
        temperature=0.1,
        max_tokens=8192,
        dataset_name="SWE-Env/SWE-Env",
        dataset_type="v2_2023pr",
        base_url="http://127.0.0.1:9000"
    )
    task_infos = load_harbor_tasks(data_path)
    tasks = [HarborTask(task_name=info["instance_id"]) for info in task_infos]
    asyncio.run(run_eval(config, tasks))


if __name__ == "__main__":
    main()
