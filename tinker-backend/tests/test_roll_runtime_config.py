"""Public ROLL runtime registration and combined-config materialization."""
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError

from tinker_backend.protocol.schemas import CreateRuntimeRequest
from tinker_backend.services.runtime_lifecycle import materialize_runtime_config


@pytest.mark.parametrize("runtime_type", ["ROLL", "roll", " Roll "])
def test_roll_registration_is_normalized(runtime_type):
    request = CreateRuntimeRequest(runtime_type=runtime_type, config_content="{}")
    assert request.runtime_type == "roll"


def test_unknown_runtime_rejected():
    with pytest.raises(ValidationError):
        CreateRuntimeRequest(runtime_type="unknown-engine", config_content="{}")


def test_combined_roll_engine_is_materialized_without_losing_runtime_options(tmp_path):
    config = {
        "roll": {"pretrain": "Qwen/Qwen3-4B-Instruct-2507", "seed": 42},
        "tinker_runtime": {"claim_limit": 2, "backend_config": {"ray_address": "local"}},
    }
    original = yaml.safe_dump(config)
    path = materialize_runtime_config(SimpleNamespace(runtime_state_dir=str(tmp_path)),
                                      "rt-test", "yaml", original)
    wrapper = yaml.safe_load(Path(path).read_text())
    backend = wrapper["tinker_runtime"]["backend_config"]
    engine = Path(backend["generated_config_path"])
    assert engine == tmp_path / "rt-test" / "roll" / "config.yaml"
    assert yaml.safe_load(engine.read_text()) == config["roll"]
    assert backend["config_path"] == str(engine.parent)
    assert backend["config_name"] == "config"
    assert backend["ray_address"] == "local"
    assert wrapper["tinker_runtime"]["claim_limit"] == 2
    assert Path(wrapper["tinker_backend"]["original_config_path"]).read_text() == original
