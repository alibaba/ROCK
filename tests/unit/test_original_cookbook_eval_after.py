"""CPU contract tests for the optional original-cookbook evaluation path.

Load only the orchestration functions: importing the full cookbook would load
GPU-facing dependencies despite these tests using no backend or model.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
from contextlib import redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
COOKBOOK = ROOT / "rock-tinker/tinker_cookbook/tinker_backend_cookbook"
if not COOKBOOK.exists():  # Local standalone check before remote relay.
    COOKBOOK = Path(__file__).resolve().parent


def load_functions(filename, names, namespace):
    namespace["__file__"] = str(COOKBOOK / filename)
    tree = ast.parse((COOKBOOK / filename).read_text())
    selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    assert len(selected) == len(names)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(COOKBOOK / filename), "exec"), namespace)
    return namespace


class EvaluationContractTests(unittest.IsolatedAsyncioTestCase):
    def helper_namespace(self, *, raises=False):
        envs = []
        policies = []

        class Env:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.env_id = f"fresh-{len(envs)}"
                envs.append(self)

            def get_env_id(self):
                return self.env_id

        class Policy:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                policies.append(self)

        async def rollout(policy, env):
            if raises:
                raise RuntimeError("actual rollout failure")
            return SimpleNamespace(transitions=[SimpleNamespace(reward=0.0, episode_done=True)])

        namespace = {
            "Path": Path, "time": time, "json": json,
            "RemoteSandboxEnv": Env, "RuntimePromptCompleter": Policy,
            "do_single_rollout": rollout, "task_name": lambda task: "same-task",
            "format_structured_trajectory": lambda trajectory, **kwargs: "real-zero-trajectory",
        }
        load_functions("train_swe_bench.py", {"evaluate_after_training"}, namespace)
        return namespace, envs, policies

    async def test_fresh_env_returned_sampler_and_zero_reward_preserved(self):
        ns, envs, policies = self.helper_namespace()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            before = {"reward": 1.0, "train_tokens": 5}
            sampler = object()
            result = await ns["evaluate_after_training"](
                task="task", runtime="runtime", sampling_client=sampler, results_dir=Path(tmp),
                before=before, max_turns=7, max_tokens=128, temperature=.7,
            )
            self.assertEqual(len(envs), 1)
            self.assertEqual(envs[0].kwargs, {"task": "task", "runtime": "runtime", "max_turns": 7})
            self.assertIs(policies[0].kwargs["sampling_client"], sampler)
            self.assertEqual(result["after"]["reward"], 0.0)
            self.assertTrue(result["after"]["episode_done"])
            self.assertEqual(result["before"], before)
            self.assertEqual((Path(tmp) / "after/trajectory.txt").read_text(), "real-zero-trajectory")
            self.assertEqual(json.loads((Path(tmp) / "after/result.json").read_text()), result)
            self.assertIn("not_statistical", result["comparison"])

    async def test_after_error_propagates_without_fabricated_score(self):
        ns, _, _ = self.helper_namespace(raises=True)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "actual rollout failure"):
                await ns["evaluate_after_training"](
                    task="task", runtime="runtime", sampling_client="published", results_dir=Path(tmp),
                    before={"reward": 0}, max_turns=7, max_tokens=128, temperature=.7,
                )
            self.assertFalse((Path(tmp) / "after/result.json").exists())

    async def run_main(self, filename, enabled, *, eval_error=False, save=False, save_error=False):
        events = []
        published = []
        evaluated = []
        training_results = []
        saved = []

        class Training:
            async def publish_to_sampler(self):
                sampler = f"published-{len(published)}"
                published.append(sampler)
                events.append("publish")
                return sampler

            async def save_state(self, name):
                events.append("save")
                if save_error:
                    raise RuntimeError("actual checkpoint failure")
                result = {"name": name, "path": "tinker://actual-model/weights/" + name}
                saved.append(result)
                return SimpleNamespace(path=result["path"])

        training = Training()

        class Runtime:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                events.append("runtime-close")

            async def create_lora_training(self, **kwargs):
                return training

            async def create_sampling_client(self, **kwargs):
                return "frozen-reference"

        runtime = Runtime()

        class Job:
            job_id = "job"
            base_url = "local"

            @classmethod
            def open(cls, **kwargs):
                return cls()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                events.append("job-close")

            async def get_client(self):
                async def create_runtime(**kwargs):
                    return runtime
                return SimpleNamespace(create_runtime=create_runtime)

        def add_task_args(parser):
            parser.add_argument("--task-id", default="same-task")

        async def load_task(args):
            return SimpleNamespace(task_id="same-task", dataset="public", split="test", bench_name="swe")

        async def train_step(**kwargs):
            events.append("train")
            result = {"reward": 0.0, "train_skipped": False}
            training_results.append(result)
            return result

        async def grpo_step(**kwargs):
            events.append("train")
            result = {"group_returns": [0., 1., 0., 0.], "group_std": .433, "rollouts_failed": 0}
            training_results.append(result)
            return result, f"grpo-after-{len(training_results)}"

        async def evaluate(**kwargs):
            events.append("eval")
            evaluated.append(kwargs)
            if eval_error:
                raise RuntimeError("after failure")
            return {"after": {"reward": 0}}

        namespace = {
            "argparse": argparse, "Path": Path, "datetime": datetime, "json": json,
            "tinker": SimpleNamespace(ServerJob=Job),
            "add_task_args": add_task_args, "load_one_task": load_task,
            "write_task_manifest": lambda *args: None, "emit_server_job_marker": lambda job: None,
            "run_one_task_and_train": train_step, "run_one_grpo_step": grpo_step,
            "evaluate_after_training": evaluate,
        }
        load_functions(filename, {"main_async"}, namespace)
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            args = [filename, "--output-path", tmp]
            if filename.endswith("_grpo.py"):
                args.extend(["--num-steps", "2", "--group-size", "4"])
            if enabled:
                args.append("--eval-after")
            if save:
                args.extend(["--save-state", "actual-state"])
            with patch.object(sys, "argv", args):
                if eval_error:
                    with self.assertRaisesRegex(RuntimeError, "after failure"):
                        await namespace["main_async"]()
                elif save_error:
                    with self.assertRaisesRegex(RuntimeError, "actual checkpoint failure"):
                        await namespace["main_async"]()
                else:
                    await namespace["main_async"]()
            checkpoints = list(Path(tmp).glob("*/checkpoint.json"))
            if save and not save_error:
                self.assertEqual(len(checkpoints), 1)
                self.assertEqual(json.loads(checkpoints[0].read_text()), saved[0])
            else:
                self.assertFalse(checkpoints)
        return events, published, evaluated, training_results, saved

    async def test_default_disabled_has_no_extra_publish_or_eval(self):
        for filename in ["train_swe_bench.py", "train_swe_bench_kl.py", "train_swe_bench_grpo.py"]:
            with self.subTest(filename=filename):
                events, published, evaluated, results, saved = await self.run_main(filename, False)
                self.assertEqual(len(published), 1)  # Original bootstrap only.
                self.assertFalse(evaluated)
                self.assertFalse(saved)
                self.assertEqual(events.count("train"), 2 if filename.endswith("_grpo.py") else 1)

    async def test_ppo_and_kl_after_uses_published_client_and_original_before(self):
        for filename in ["train_swe_bench.py", "train_swe_bench_kl.py"]:
            with self.subTest(filename=filename):
                events, published, evaluated, results, saved = await self.run_main(filename, True)
                self.assertEqual(len(evaluated), 1)
                self.assertEqual(evaluated[0]["sampling_client"], published[-1])
                self.assertIs(evaluated[0]["before"], results[0])
                self.assertEqual(results[0]["reward"], 0.0)
                self.assertLess(events.index("train"), events.index("eval"))

    async def test_grpo_evaluates_once_after_last_step_and_keeps_whole_group(self):
        events, published, evaluated, results, saved = await self.run_main("train_swe_bench_grpo.py", True)
        self.assertEqual(len(published), 1)
        self.assertEqual(len(evaluated), 1)
        self.assertEqual(evaluated[0]["sampling_client"], "grpo-after-2")
        self.assertIs(evaluated[0]["before"], results[-1])
        self.assertEqual(evaluated[0]["before"]["group_returns"], [0., 1., 0., 0.])
        self.assertEqual(events[:4], ["publish", "train", "train", "eval"])

    async def test_save_happens_after_training_before_optional_eval(self):
        for filename in ["train_swe_bench.py", "train_swe_bench_kl.py", "train_swe_bench_grpo.py"]:
            with self.subTest(filename=filename):
                events, _, evaluated, _, saved = await self.run_main(filename, True, save=True)
                self.assertEqual(len(saved), 1)
                self.assertEqual(len(evaluated), 1)
                self.assertLess(max(i for i, event in enumerate(events) if event == "train"), events.index("save"))
                self.assertLess(events.index("save"), events.index("eval"))

    async def test_save_without_eval_keeps_independent_checkpoint(self):
        for filename in ["train_swe_bench.py", "train_swe_bench_kl.py", "train_swe_bench_grpo.py"]:
            with self.subTest(filename=filename):
                events, _, evaluated, _, saved = await self.run_main(filename, False, save=True)
                self.assertEqual(len(saved), 1)
                self.assertFalse(evaluated)
                self.assertNotIn("eval", events)

    async def test_checkpoint_record_survives_after_evaluation_failure(self):
        for filename in ["train_swe_bench.py", "train_swe_bench_kl.py", "train_swe_bench_grpo.py"]:
            with self.subTest(filename=filename):
                events, _, evaluated, _, saved = await self.run_main(filename, True, save=True, eval_error=True)
                self.assertEqual(len(saved), 1)
                self.assertEqual(len(evaluated), 1)
                self.assertLess(events.index("save"), events.index("eval"))
                self.assertEqual(events[-2:], ["runtime-close", "job-close"])

    async def test_save_failure_propagates_no_eval_and_context_cleanup(self):
        for filename in ["train_swe_bench.py", "train_swe_bench_kl.py", "train_swe_bench_grpo.py"]:
            with self.subTest(filename=filename):
                events, _, evaluated, _, saved = await self.run_main(filename, True, save=True, save_error=True)
                self.assertFalse(saved)
                self.assertFalse(evaluated)
                self.assertEqual(events[-2:], ["runtime-close", "job-close"])

    async def test_main_after_failure_keeps_exception_and_context_cleanup(self):
        for filename in ["train_swe_bench.py", "train_swe_bench_kl.py", "train_swe_bench_grpo.py"]:
            with self.subTest(filename=filename):
                events, _, _, _, _ = await self.run_main(filename, True, eval_error=True)
                self.assertEqual(events[-2:], ["runtime-close", "job-close"])


if __name__ == "__main__":
    unittest.main()
