from __future__ import annotations

from pathlib import Path

import yaml


def test_verl_train_runtime_uses_disaggregated_weight_sync() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    path = repo_root / "config" / "tinker_backend_cookbook" / "verl_train_runtime.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    runtime = raw["verl_runtime"]
    assert runtime["model_path"] == "Qwen/Qwen3-4B-Instruct-2507"
    assert runtime["tokenizer_path"] == "Qwen/Qwen3-4B-Instruct-2507"
    assert runtime["rollout"]["n_gpus_per_node"] == 2
    assert runtime["rollout"]["checkpoint_engine"] == {
        "backend": "nccl",
        "update_weights_bucket_megabytes": 64,
    }
    assert runtime["training"]["n_gpus_per_node"] == 2
    assert runtime["training"]["lora_alpha"] == 32
    assert runtime["training"]["merge_lora_for_publish"] is True
    assert all("Qwen3.5" not in override for override in runtime["training"]["overrides"])
