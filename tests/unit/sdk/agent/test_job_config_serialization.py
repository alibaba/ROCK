from pathlib import Path

import yaml

from rock.sdk.agent.models.job.config import DatasetConfig, JobConfig
from rock.sdk.agent.models.metric.config import MetricConfig
from rock.sdk.agent.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig


class TestJobConfigToHarborYaml:
    """Test serializing JobConfig to Harbor-compatible YAML."""

    def test_basic_serialization(self):
        cfg = JobConfig(
            job_name="test-job",
            n_attempts=2,
            agents=[AgentConfig(name="terminus-2", model_name="hosted_vllm/m")],
        )
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)

        assert data["job_name"] == "test-job"
        assert data["n_attempts"] == 2
        assert data["agents"][0]["name"] == "terminus-2"

    def test_excludes_rock_fields(self):
        cfg = JobConfig(
            setup_commands=["pip install harbor"],
            collect_trajectory=True,
            auto_start_sandbox=False,
        )
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)

        assert "sandbox" not in data
        assert "setup_commands" not in data
        assert "collect_trajectory" not in data
        assert "auto_start_sandbox" not in data
        assert "auto_stop_sandbox" not in data
        assert "result_file" not in data

    def test_excludes_none_values(self):
        cfg = JobConfig(
            job_name="test",
            agents=[AgentConfig(name="t2")],
        )
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)

        assert "agent_timeout_multiplier" not in data

    def test_path_fields_serialized_as_strings(self):
        cfg = JobConfig(
            jobs_dir=Path("/workspace/jobs"),
            tasks=[TaskConfig(path="/workspace/tasks/t1")],
        )
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)

        assert data["jobs_dir"] == "/workspace/jobs"
        assert data["tasks"][0]["path"] == "/workspace/tasks/t1"

    def test_full_config_roundtrip(self):
        cfg = JobConfig(
            job_name="full-test",
            n_attempts=3,
            environment=EnvironmentConfig(type="docker", force_build=True, delete=True),
            agents=[
                AgentConfig(
                    name="terminus-2",
                    model_name="hosted_vllm/my-model",
                    kwargs={"max_iterations": 30},
                    env={"LLM_API_KEY": "sk-xxx"},
                )
            ],
            datasets=[DatasetConfig(name="terminal-bench", version="2.0", n_tasks=50)],
            metrics=[MetricConfig(type="mean")],
        )
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)

        assert data["job_name"] == "full-test"
        assert data["environment"]["type"] == "docker"
        assert data["environment"]["force_build"] is True
        assert data["agents"][0]["kwargs"]["max_iterations"] == 30
        assert data["datasets"][0]["name"] == "terminal-bench"


class TestJobConfigFromYaml:
    """Test loading JobConfig from Harbor YAML file."""

    def test_from_yaml_string(self, tmp_path):
        yaml_content = """
job_name: loaded-job
n_attempts: 2
agents:
  - name: terminus-2
    model_name: hosted_vllm/my-model
datasets:
  - name: terminal-bench
    version: "2.0"
"""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml_content)

        cfg = JobConfig.from_yaml(str(yaml_file))
        assert cfg.job_name == "loaded-job"
        assert cfg.n_attempts == 2
        assert cfg.agents[0].name == "terminus-2"
        assert cfg.datasets[0].name == "terminal-bench"

    def test_from_yaml_with_overrides(self, tmp_path):
        yaml_content = """
job_name: loaded-job
agents:
  - name: terminus-2
"""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml_content)

        cfg = JobConfig.from_yaml(str(yaml_file), setup_commands=["pip install harbor"])
        assert cfg.job_name == "loaded-job"
        assert cfg.setup_commands == ["pip install harbor"]
