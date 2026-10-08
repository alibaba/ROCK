from __future__ import annotations

import pytest
import yaml
from tinker_backend.rock_config import ROCKJobDefaults, load_job_config


def template(tmp_path, name="SWE-bench"):
    directory = tmp_path / name
    directory.mkdir()
    path = directory / "job_config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "experiment_id": "test",
                "environment": {
                    "env": {"KEPT": "template", "EXPANDED": "${ROCK_CONFIG_TEST_VALUE}"},
                    "xrl_authorization": "template-key",
                },
                "agents": [
                    {"name": "custom", "model_name": "original", "kwargs": {"scaffold": "keep"}},
                    {"name": "second", "model_name": "second-model", "kwargs": {"keep": True}},
                ],
                "datasets": [{"name": "dataset", "registry": {"split": "test", "oss_bucket": "template-bucket"}}],
                "verifier": {"disable": True},
            }
        )
    )
    return path


def test_config_from_env_oss_optional_and_defaults(monkeypatch):
    monkeypatch.setenv("ROCK_KEY", "environment-key")
    defaults = ROCKJobDefaults(rock_key="default-key", oss_region="default-region")
    config = ROCKJobDefaults.from_env(defaults)
    assert config.rock_key == "environment-key"
    assert config.oss_region == "default-region"
    assert config.oss_access_key_id is None


def test_precedence_agents_and_oss(tmp_path, monkeypatch):
    template(tmp_path)
    monkeypatch.setenv("ROCK_CONFIG_TEST_VALUE", "expanded")
    result = load_job_config(
        "SWE-bench",
        template_root=tmp_path,
        default_config=ROCKJobDefaults(rock_key="default-key", oss_bucket="default-bucket", oss_region="region"),
        task_names=["task"],
        model_name="selected",
        env={"OPENAI_API_KEY": "env-id", "OPENAI_BASE_URL": "", "KEPT": "explicit"},
        **{"environment.env.KEPT": "override", "agents.0.kwargs.max_iterations": 21},
    )
    assert result.environment.xrl_authorization == "template-key"
    assert result.environment.env["KEPT"] == "override"
    assert result.environment.env["EXPANDED"] == "expanded"
    assert result.datasets[0].registry.oss_bucket == "template-bucket"
    assert result.datasets[0].registry.oss_region == "region"
    assert result.datasets[0].task_names == ["task"]
    assert result.agents[0].name == "custom"
    assert result.agents[0].model_name == "selected"
    assert result.agents[0].kwargs == {"scaffold": "keep", "api_key": "env-id", "api_base": "", "max_iterations": 21}
    assert result.agents[1].model_name == "second-model"


@pytest.mark.parametrize("name", ["../outside", ".", "..", "a/b", "a\\b", ""])
def test_rejects_path_segments(tmp_path, name):
    with pytest.raises(ValueError, match="single path segment"):
        load_job_config(name, template_root=tmp_path)


def test_rejects_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "SWE-bench").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="inside template_root"):
        load_job_config("SWE-bench", template_root=root)


def test_missing_template_help(tmp_path):
    with pytest.raises(FileNotFoundError, match="job_config_path"):
        load_job_config("unknown", template_root=tmp_path)


def test_explicit_path_and_array_override(tmp_path):
    path = template(tmp_path)
    result = load_job_config(None, job_config_path=path, **{"agents.1.model_name": "changed"})
    assert result.agents[1].model_name == "changed"
    assert result.agents[0].name == "custom"


def test_invalid_array_index(tmp_path):
    path = template(tmp_path)
    with pytest.raises(ValueError, match="array index"):
        load_job_config(None, job_config_path=path, **{"agents.9.model_name": "changed"})
