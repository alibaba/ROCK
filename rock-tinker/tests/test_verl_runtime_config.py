from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize(
    "config_name",
    [
        "verl_rollout_runtime.yaml",
        "verl_train_runtime.yaml",
    ],
)
def test_qwen3_4b_verl_configs_use_matching_tool_parser(config_name: str) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    config_path = repo_root / "config" / "tinker_backend_cookbook" / config_name
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    runtime = raw["verl_runtime"]
    assert runtime["model_path"] == "Qwen/Qwen3-4B-Instruct-2507"
    assert runtime["tool_call_parser"] == "hermes"
    assert runtime["enable_auto_tool_choice"] is True
