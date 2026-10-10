from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from tinker_backend.adapters.rock_adapter import RockAdapter


def _write_yaml(tmp_path: Path, name: str, payload: dict) -> str:
    path = tmp_path / name
    path.write_text(yaml.dump(payload), encoding="utf-8")
    return str(path)


def _legacy_job_config(tmp_path: Path) -> str:
    return _write_yaml(
        tmp_path,
        "job_config.yaml",
        {
            "experiment_id": "test",
            "timeout": 3600,
            "environment": {
                "base_url": "https://example.com",
                "xrl_authorization": "",
                "image": "test-image:latest",
                "memory": "8g",
                "cpus": 2,
                "env": {},
            },
            "agents": [{"name": "placeholder", "model_name": "openai/test"}],
            "datasets": [{"registry": {"split": "test"}, "name": "test-dataset", "task_names": [""]}],
            "verifier": {"disable": True},
        },
    )


def _make_adapter(config_path: str) -> RockAdapter:
    return RockAdapter(backend_url="http://localhost:9000", runtime_id="rt_test_001", config_path=config_path)


class BuildJobConfigLegacyTest(unittest.TestCase):
    def test_legacy_path_patches_task_descriptor_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            config_path = _write_yaml(
                tmp_path,
                "adapter_config.yaml",
                {
                    "rock": {
                        "job_config_path": _legacy_job_config(tmp_path),
                        "llm": {"model_name": "qwen3-4b", "temperature": 0.7, "max_tokens": 4096},
                        "agent": {"scaffold_config": "default", "max_iterations": 20, "num_retries": 3},
                        "runtime": {"task_timeout_sec": 600},
                    }
                },
            )
            adapter = _make_adapter(config_path)

            config = adapter._build_job_config(
                task_id="sympy__sympy-19637",
                dataset="princeton-nlp/SWE-bench_Verified",
                split="test",
                env_id="env_abc123",
                job_id="tinker_env_abc123_deadbeef",
            )

        self.assertEqual(config.job_name, "tinker_env_abc123_deadbeef")
        self.assertEqual(config.agents[0].model_name, "openai/qwen3-4b")
        self.assertEqual(config.agents[0].kwargs["api_key"], "env_abc123")
        self.assertEqual(config.datasets[0].task_names, ["sympy__sympy-19637"])
        self.assertEqual(config.datasets[0].name, "princeton-nlp/SWE-bench_Verified")
        self.assertEqual(config.datasets[0].registry.split, "test")
        self.assertEqual(config.environment.env["TASK_ID"], "sympy__sympy-19637")
        self.assertEqual(config.environment.env["DATASET"], "princeton-nlp/SWE-bench_Verified")
        self.assertEqual(config.environment.env["SPLIT"], "test")

    def test_no_job_config_path_or_bench_name_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_path = _write_yaml(Path(td), "empty.yaml", {"rock": {}})
            adapter = _make_adapter(config_path)
            with self.assertRaisesRegex(RuntimeError, "job_config_path or rock.bench_name"):
                adapter._build_job_config("id", "ds", "test", "env", "job")


class BuildJobConfigROCKTest(unittest.TestCase):
    def test_rock_called_with_task_descriptor_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_path = _write_yaml(
                Path(td),
                "rock_adapter.yaml",
                {
                    "rock": {
                        "bench_name": "SWE-bench",
                        "llm": {"model_name": "qwen3-4b"},
                        "job_config_overrides": {"environment.env.CUSTOM_VAR": "custom_value"},
                    }
                },
            )
            adapter = _make_adapter(config_path)
            mock_job_config = MagicMock()
            mock_job_config.environment = MagicMock()

            with patch("tinker_backend.adapters.rock_adapter.load_job_config", return_value=mock_job_config) as mock_load:
                result = adapter._build_job_config(
                    task_id="sympy__sympy-19637",
                    dataset="princeton-nlp/SWE-bench_Verified",
                    split="test",
                    env_id="env_abc",
                    job_id="job_xyz",
                    bench_name="terminal-bench-2",
                )

        self.assertIs(result, mock_job_config)
        self.assertEqual(mock_load.call_args.args[0], "terminal-bench-2")
        self.assertEqual(mock_load.call_args.kwargs["task_names"], ["sympy__sympy-19637"])
        self.assertEqual(mock_load.call_args.kwargs["model_name"], "openai/qwen3-4b")
        self.assertEqual(mock_load.call_args.kwargs["env"], {"OPENAI_BASE_URL": "", "OPENAI_API_KEY": "env_abc"})
        self.assertEqual(mock_load.call_args.kwargs["environment.env.TASK_ID"], "sympy__sympy-19637")
        self.assertEqual(mock_load.call_args.kwargs["environment.env.DATASET"], "princeton-nlp/SWE-bench_Verified")
        self.assertEqual(mock_load.call_args.kwargs["environment.env.SPLIT"], "test")
        self.assertEqual(mock_load.call_args.kwargs["environment.env.CUSTOM_VAR"], "custom_value")
        self.assertEqual(result.job_name, "job_xyz")
        self.assertEqual(result.experiment_id, "rt_test_001")

    def test_rock_does_not_rewrite_template_agents(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_path = _write_yaml(Path(td), "rock.yaml", {"rock": {"bench_name": "SWE-bench"}})
            adapter = _make_adapter(config_path)
            existing_agent = MagicMock()
            existing_agent.kwargs = {"template_key": "template_value"}
            mock_job_config = MagicMock()
            mock_job_config.agents = [existing_agent]
            mock_job_config.environment = MagicMock()

            with patch("tinker_backend.adapters.rock_adapter.load_job_config", return_value=mock_job_config):
                result = adapter._build_job_config("task_1", "ds", "test", "env_1", "job_1")

        self.assertEqual(result.agents[0].kwargs, {"template_key": "template_value"})

    def test_rock_passes_explicit_agent_limits_as_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_path = _write_yaml(
                Path(td),
                "rock_limits.yaml",
                {
                    "rock": {
                        "bench_name": "SWE-bench",
                        "agent": {"max_iterations": 100},
                        "runtime": {"task_timeout_sec": 1800},
                    }
                },
            )
            adapter = _make_adapter(config_path)
            mock_job_config = MagicMock()
            mock_job_config.environment = MagicMock()

            with patch(
                "tinker_backend.adapters.rock_adapter.load_job_config",
                return_value=mock_job_config,
            ) as mock_load:
                result = adapter._build_job_config("task_1", "ds", "test", "env_1", "job_1")

        self.assertIs(result, mock_job_config)
        self.assertEqual(mock_load.call_args.kwargs["agents.0.kwargs.max_iterations"], 100)
        self.assertEqual(mock_load.call_args.kwargs["agents.0.max_timeout_sec"], 1800)

    def test_rejects_nonpositive_agent_max_iterations(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_path = _write_yaml(
                Path(td),
                "invalid_limits.yaml",
                {
                    "rock": {
                        "bench_name": "SWE-bench",
                        "agent": {"max_iterations": 0},
                    }
                },
            )

            with self.assertRaisesRegex(ValueError, "max_iterations must be greater than zero"):
                _make_adapter(config_path)


if __name__ == "__main__":
    unittest.main()
