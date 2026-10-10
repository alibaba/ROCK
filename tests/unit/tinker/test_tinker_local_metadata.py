"""Isolation contract for Tinker dependencies and sandbox exports."""
from pathlib import Path
import tomllib
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[3]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())
LOCAL = tomllib.loads((ROOT / "rock-tinker/local/pyproject.toml").read_text())

def names(requirements):
    return {Requirement(s).name.replace("_", "-") for s in requirements if isinstance(s, str)}

def test_tinker_dependencies_do_not_change_rock_default_install():
    assert PROJECT["project"]["requires-python"] == "<4.0,>=3.10"
    assert not {"torch", "transformers", "chz", "alembic", "pillow"} & names(PROJECT["project"]["dependencies"])
    assert PROJECT["tool"]["uv"] == {"index": [{"url": "https://mirrors.aliyun.com/pypi/simple/", "default": True}]}
    extras = PROJECT["project"]["optional-dependencies"]
    assert Requirement(extras["admin"][0]).extras == {"rocklet"}
    assert "gem-llm" in names(extras["rocklet"])
    assert not {"admin-core", "rocklet-core", "tinker-local", "tinker-sandbox"} & extras.keys()

def test_control_group_has_services_and_scoped_cpu_torch_source():
    deps = names(LOCAL["dependency-groups"]["control"])
    assert {"torch", "ray", "omegaconf", "datasets"} <= deps
    assert {"include-group": "sandbox"} in LOCAL["dependency-groups"]["control"]
    assert LOCAL["tool"]["uv"]["sources"]["torch"] == {"index": "pytorch-cpu"}
    assert LOCAL["tool"]["uv"]["sources"]["rl-rock"] == {"path": "../..", "editable": True}
    assert LOCAL["tool"]["uv"]["package"] is False
    assert "nacos-sdk-python==0.1.14" in LOCAL["dependency-groups"]["control"]
    assert not {"vllm", "sglang", "megatron-core", "transformer-engine", "gem-llm"} & deps

def test_sdk_backend_requirements_are_in_tinker_environment():
    available = names(LOCAL["project"]["dependencies"]) | names(PROJECT["project"]["dependencies"])
    for path in ("rock-tinker/pyproject.toml", "tinker-backend/pyproject.toml"):
        project = tomllib.loads((ROOT / path).read_text())["project"]
        assert names(project["dependencies"]) <= available

def test_sandbox_group_excludes_control_and_gpu_packages():
    deps = names(LOCAL["dependency-groups"]["sandbox"]) | names(LOCAL["project"]["dependencies"])
    assert {"harbor", "litellm", "openai", "fastapi", "psutil"} <= deps
    assert not {"torch", "ray", "gem-llm", "vllm", "sglang", "megatron-core", "transformer-engine"} & deps
