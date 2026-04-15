from pathlib import Path

import yaml

from rock.sdk.bench.models.job.config import (
    HarborJobConfig,
    LocalDatasetConfig,
    RegistryDatasetConfig,
    RemoteRegistryInfo,
    RockEnvironmentConfig,
)
from rock.sdk.bench.models.metric.config import MetricConfig
from rock.sdk.bench.models.trial.config import AgentConfig, TaskConfig


class TestRockEnvironmentConfigInheritance:
    """RockEnvironmentConfig should inherit from both SandboxConfig and EnvironmentConfig."""

    def test_is_sandbox_config_subclass(self):
        from rock.sdk.sandbox.config import SandboxConfig

        assert issubclass(RockEnvironmentConfig, SandboxConfig)

    def test_inherits_sandbox_config_fields(self):
        env = RockEnvironmentConfig()
        assert env.image == "python:3.11"
        assert env.memory == "8g"
        assert env.cpus == 2.0
        assert env.cluster == "zb"

    def test_inherits_harbor_env_fields(self):
        env = RockEnvironmentConfig()
        assert env.force_build is False
        assert env.delete is True
        assert env.type is None
        assert env.kwargs == {}

    def test_job_level_fields(self):
        env = RockEnvironmentConfig()
        assert env.env == {}
        assert env.uploads == []
        assert env.auto_stop is False

    def test_env_field(self):
        env = RockEnvironmentConfig(env={"OPENAI_API_KEY": "sk-xxx"})
        assert env.env == {"OPENAI_API_KEY": "sk-xxx"}

    def test_harbor_fields_settable(self):
        env = RockEnvironmentConfig(force_build=True, override_cpus=4, type="docker")
        assert env.force_build is True
        assert env.override_cpus == 4
        assert env.type.value == "docker"


class TestToHarborEnvironment:
    """to_harbor_environment() should return only harbor-native fields."""

    def test_returns_harbor_fields_only(self):
        env = RockEnvironmentConfig(force_build=True, override_cpus=4)
        result = env.to_harbor_environment()
        assert result["force_build"] is True
        assert result["override_cpus"] == 4

    def test_excludes_rock_sandbox_fields(self):
        env = RockEnvironmentConfig(image="my-image:latest", memory="32g", cpus=8)
        result = env.to_harbor_environment()
        assert "image" not in result
        assert "memory" not in result
        assert "cpus" not in result
        assert "cluster" not in result

    def test_excludes_job_level_fields(self):
        env = RockEnvironmentConfig(
            uploads=[("a", "b")],
            auto_stop=True,
        )
        result = env.to_harbor_environment()
        assert "uploads" not in result
        assert "auto_stop" not in result

    def test_env_passes_through_to_harbor(self):
        env = RockEnvironmentConfig(env={"KEY": "val"})
        result = env.to_harbor_environment()
        assert result["env"] == {"KEY": "val"}

    def test_excludes_none_values(self):
        env = RockEnvironmentConfig(type=None, import_path=None, override_cpus=None)
        result = env.to_harbor_environment()
        assert "type" not in result
        assert "import_path" not in result
        assert "override_cpus" not in result

    def test_empty_config_excludes_rock_fields(self):
        env = RockEnvironmentConfig()
        result = env.to_harbor_environment()
        assert "image" not in result
        assert "uploads" not in result


class TestHarborJobConfigToHarborYaml:
    def test_basic_serialization(self):
        cfg = HarborJobConfig(
            job_name="test-job",
            experiment_id="test-exp",
            n_attempts=2,
            agents=[AgentConfig(name="terminus-2", model_name="hosted_vllm/m")],
        )
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)

        # job_name is re-injected so harbor uses it as the directory name
        assert data["job_name"] == "test-job"
        assert data["experiment_id"] == "test-exp"
        assert data["n_attempts"] == 2
        assert data["agents"][0]["name"] == "terminus-2"

    def test_excludes_rock_fields(self):
        cfg = HarborJobConfig(
            experiment_id="test-exp",
            environment=RockEnvironmentConfig(
                uploads=[("local.txt", "/sandbox/remote.txt")],
                env={"API_KEY": "sk-xxx"},
                auto_stop=True,
                image="my-image:latest",
                memory="32g",
            ),
        )
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)

        # Rock fields must not appear at top level
        assert "sandbox_config" not in data
        assert "uploads" not in data
        assert "sandbox_env" not in data
        assert "auto_stop_sandbox" not in data
        assert "auto_stop" not in data
        # environment block should only contain harbor fields
        assert "environment" not in data or "image" not in data.get("environment", {})

    def test_excludes_none_values(self):
        cfg = HarborJobConfig(
            job_name="test",
            experiment_id="test-exp",
            agents=[AgentConfig(name="t2")],
        )
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)

        assert "agent_timeout_multiplier" not in data

    def test_labels_included_in_harbor_yaml(self):
        """labels is a base HarborJobConfig field, included in harbor YAML."""
        cfg = HarborJobConfig(
            job_name="labeled-job",
            experiment_id="test-exp",
            labels={"step": "42", "env": "prod"},
        )
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)

        assert data["labels"] == {"step": "42", "env": "prod"}
        assert data["job_name"] == "labeled-job"

    def test_path_fields_serialized_as_strings(self):
        cfg = HarborJobConfig(
            experiment_id="test-exp",
            jobs_dir=Path("/workspace/jobs"),
            tasks=[TaskConfig(path="/workspace/tasks/t1")],
        )
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)

        assert data["jobs_dir"] == "/workspace/jobs"
        assert data["tasks"][0]["path"] == "/workspace/tasks/t1"

    def test_harbor_env_fields_serialized(self):
        cfg = HarborJobConfig(
            job_name="full-test",
            experiment_id="test-exp",
            n_attempts=3,
            environment=RockEnvironmentConfig(
                type="docker",
                force_build=True,
                delete=True,
                override_cpus=4,
            ),
            agents=[
                AgentConfig(
                    name="terminus-2",
                    model_name="hosted_vllm/my-model",
                    kwargs={"max_iterations": 30},
                    env={"LLM_API_KEY": "sk-xxx"},
                )
            ],
            datasets=[
                RegistryDatasetConfig(registry=RemoteRegistryInfo(), name="terminal-bench", version="2.0", n_tasks=50)
            ],
            metrics=[MetricConfig(type="mean")],
        )
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)

        assert data["job_name"] == "full-test"
        assert data["environment"]["type"] == "docker"
        assert data["environment"]["force_build"] is True
        assert data["environment"]["override_cpus"] == 4
        # Rock fields must not be in environment section
        assert "image" not in data.get("environment", {})
        assert data["agents"][0]["kwargs"]["max_iterations"] == 30
        assert data["datasets"][0]["name"] == "terminal-bench"

    def test_env_in_harbor_yaml(self):
        """env is passed to both sandbox session and harbor YAML."""
        cfg = HarborJobConfig(
            experiment_id="test-exp", environment=RockEnvironmentConfig(env={"OPENAI_API_KEY": "sk-xxx"})
        )
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)

        assert "sandbox_env" not in data
        assert data["environment"]["env"] == {"OPENAI_API_KEY": "sk-xxx"}


class TestHarborJobConfigFromYaml:
    def test_from_yaml_basic(self, tmp_path):
        yaml_content = """
job_name: loaded-job
experiment_id: test-exp
n_attempts: 2
agents:
  - name: terminus-2
    model_name: hosted_vllm/my-model
datasets:
  - registry:
      url: https://example.com/registry.json
    name: terminal-bench
    version: "2.0"
"""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml_content)

        cfg = HarborJobConfig.from_yaml(str(yaml_file))
        assert cfg.job_name == "loaded-job"
        assert cfg.n_attempts == 2
        assert cfg.agents[0].name == "terminus-2"
        assert cfg.datasets[0].name == "terminal-bench"

    def test_from_yaml_with_environment_block(self, tmp_path):
        yaml_content = """
job_name: env-job
experiment_id: test-exp
environment:
  image: my-image:latest
  memory: "32g"
  cpus: 8
  env:
    OPENAI_API_KEY: sk-xxx
  auto_stop: true
agents:
  - name: terminus-2
"""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml_content)

        cfg = HarborJobConfig.from_yaml(str(yaml_file))
        assert cfg.environment.image == "my-image:latest"
        assert cfg.environment.memory == "32g"
        assert cfg.environment.env == {"OPENAI_API_KEY": "sk-xxx"}
        assert cfg.environment.auto_stop is True

    def test_from_yaml_with_local_dataset(self, tmp_path):
        yaml_content = """
job_name: local-dataset-job
experiment_id: test-exp
datasets:
  - path: /data/tasks
    task_names:
      - task-1
      - task-2
"""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml_content)

        cfg = HarborJobConfig.from_yaml(str(yaml_file))
        assert cfg.job_name == "local-dataset-job"
        assert isinstance(cfg.datasets[0], LocalDatasetConfig)
        assert cfg.datasets[0].path == Path("/data/tasks")

    def test_from_yaml_with_labels(self, tmp_path):
        yaml_content = """
job_name: labeled-job
experiment_id: test-exp
labels:
  step: "42"
  env: prod
agents:
  - name: terminus-2
"""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml_content)

        cfg = HarborJobConfig.from_yaml(str(yaml_file))
        assert cfg.labels == {"step": "42", "env": "prod"}
