from pathlib import Path

import yaml

from rock.sdk.agent.models.job.config import (
    JobConfig,
    LocalDatasetConfig,
    RegistryDatasetConfig,
    RemoteRegistryInfo,
    RockEnvironmentConfig,
)
from rock.sdk.agent.models.metric.config import MetricConfig
from rock.sdk.agent.models.trial.config import AgentConfig, TaskConfig


class TestRockEnvironmentConfigInheritance:
    """RockEnvironmentConfig 应同时继承 SandboxConfig 和 HarborEnvConfig。"""

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
        assert env.envs == {}
        assert env.setup_commands == []
        assert env.file_uploads == []
        assert env.auto_stop is False

    def test_envs_field(self):
        env = RockEnvironmentConfig(envs={"OPENAI_API_KEY": "sk-xxx"})
        assert env.envs == {"OPENAI_API_KEY": "sk-xxx"}

    def test_harbor_fields_settable(self):
        env = RockEnvironmentConfig(force_build=True, override_cpus=4, type="docker")
        assert env.force_build is True
        assert env.override_cpus == 4
        assert env.type.value == "docker"


class TestToHarborEnvironment:
    """to_harbor_environment() 应只返回 harbor 原生字段。"""

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
            setup_commands=["pip install x"],
            file_uploads=[("a", "b")],
            auto_stop=True,
        )
        result = env.to_harbor_environment()
        assert "setup_commands" not in result
        assert "file_uploads" not in result
        assert "auto_stop" not in result

    def test_excludes_envs_field(self):
        env = RockEnvironmentConfig(envs={"KEY": "val"})
        result = env.to_harbor_environment()
        assert "envs" not in result

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
        assert "setup_commands" not in result


class TestJobConfigToHarborYaml:
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
            environment=RockEnvironmentConfig(
                setup_commands=["pip install harbor"],
                file_uploads=[("local.txt", "/sandbox/remote.txt")],
                envs={"API_KEY": "sk-xxx"},
                auto_stop=True,
                image="my-image:latest",
                memory="32g",
            )
        )
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)

        # Rock fields must not appear at top level
        assert "sandbox_config" not in data
        assert "setup_commands" not in data
        assert "file_uploads" not in data
        assert "sandbox_env" not in data
        assert "auto_stop_sandbox" not in data
        assert "auto_stop" not in data
        # environment block should only contain harbor fields
        assert "environment" not in data or "image" not in data.get("environment", {})
        assert "environment" not in data or "setup_commands" not in data.get("environment", {})

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

    def test_harbor_env_fields_serialized(self):
        cfg = JobConfig(
            job_name="full-test",
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
        assert "envs" not in data.get("environment", {})
        assert data["agents"][0]["kwargs"]["max_iterations"] == 30
        assert data["datasets"][0]["name"] == "terminal-bench"

    def test_envs_not_in_harbor_yaml(self):
        """envs goes to sandbox session, not harbor YAML."""
        cfg = JobConfig(environment=RockEnvironmentConfig(envs={"OPENAI_API_KEY": "sk-xxx"}))
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)

        assert "sandbox_env" not in data
        env_section = data.get("environment", {})
        assert "envs" not in env_section


class TestJobConfigFromYaml:
    def test_from_yaml_basic(self, tmp_path):
        yaml_content = """
job_name: loaded-job
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

        cfg = JobConfig.from_yaml(str(yaml_file))
        assert cfg.job_name == "loaded-job"
        assert cfg.n_attempts == 2
        assert cfg.agents[0].name == "terminus-2"
        assert cfg.datasets[0].name == "terminal-bench"

    def test_from_yaml_with_environment_block(self, tmp_path):
        yaml_content = """
job_name: env-job
environment:
  image: my-image:latest
  memory: "32g"
  cpus: 8
  envs:
    OPENAI_API_KEY: sk-xxx
  setup_commands:
    - pip install harbor
  auto_stop: true
agents:
  - name: terminus-2
"""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml_content)

        cfg = JobConfig.from_yaml(str(yaml_file))
        assert cfg.environment.image == "my-image:latest"
        assert cfg.environment.memory == "32g"
        assert cfg.environment.envs == {"OPENAI_API_KEY": "sk-xxx"}
        assert cfg.environment.setup_commands == ["pip install harbor"]
        assert cfg.environment.auto_stop is True

    def test_from_yaml_with_environment_override(self, tmp_path):
        yaml_content = """
job_name: loaded-job
agents:
  - name: terminus-2
"""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml_content)

        cfg = JobConfig.from_yaml(
            str(yaml_file),
            environment={"setup_commands": ["pip install harbor"], "image": "custom:latest"},
        )
        assert cfg.job_name == "loaded-job"
        assert cfg.environment.setup_commands == ["pip install harbor"]
        assert cfg.environment.image == "custom:latest"

    def test_from_yaml_environment_override_merges(self, tmp_path):
        """Override merges into existing environment block, not replaces."""
        yaml_content = """
job_name: merge-job
environment:
  image: base-image:latest
  memory: "16g"
"""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml_content)

        cfg = JobConfig.from_yaml(
            str(yaml_file),
            environment={"setup_commands": ["echo hello"]},
        )
        assert cfg.environment.image == "base-image:latest"  # preserved
        assert cfg.environment.memory == "16g"  # preserved
        assert cfg.environment.setup_commands == ["echo hello"]  # merged

    def test_from_yaml_with_local_dataset(self, tmp_path):
        yaml_content = """
job_name: local-dataset-job
datasets:
  - path: /data/tasks
    task_names:
      - task-1
      - task-2
"""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml_content)

        cfg = JobConfig.from_yaml(str(yaml_file))
        assert cfg.job_name == "local-dataset-job"
        assert isinstance(cfg.datasets[0], LocalDatasetConfig)
        assert cfg.datasets[0].path == Path("/data/tasks")
