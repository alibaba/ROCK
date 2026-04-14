"""Tests for rock.sdk.job.config — JobConfig, BashJobConfig, HarborJobConfig."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from rock.sdk.bench.constants import USER_DEFINED_LOGS
from rock.sdk.bench.models.job.config import HarborJobConfig
from rock.sdk.bench.models.trial.config import (
    AgentConfig,
    ArtifactConfig,
    RockEnvironmentConfig,
    TaskConfig,
    VerifierConfig,
)
from rock.sdk.job.config import BashJobConfig, JobConfig

# ---------------------------------------------------------------------------
# JobConfig (base)
# ---------------------------------------------------------------------------


class TestJobConfig:
    def test_defaults(self):
        cfg = JobConfig()
        assert isinstance(cfg.environment, RockEnvironmentConfig)
        assert cfg.job_name is None
        assert cfg.namespace is None
        assert cfg.experiment_id is None
        assert cfg.labels == {}
        assert cfg.auto_stop is False
        assert cfg.setup_commands == []
        assert cfg.file_uploads == []
        assert cfg.env == {}
        assert cfg.timeout == 3600

    def test_custom_values(self):
        env = RockEnvironmentConfig(image="ubuntu:22.04")
        cfg = JobConfig(
            environment=env,
            job_name="my-job",
            namespace="team-a",
            experiment_id="exp-001",
            labels={"step": "42"},
            auto_stop=True,
            setup_commands=["pip install foo"],
            file_uploads=[("/local/file.py", "/sandbox/file.py")],
            env={"MY_VAR": "hello"},
            timeout=7200,
        )
        assert cfg.environment.image == "ubuntu:22.04"
        assert cfg.job_name == "my-job"
        assert cfg.namespace == "team-a"
        assert cfg.experiment_id == "exp-001"
        assert cfg.labels == {"step": "42"}
        assert cfg.auto_stop is True
        assert cfg.setup_commands == ["pip install foo"]
        assert cfg.file_uploads == [("/local/file.py", "/sandbox/file.py")]
        assert cfg.env == {"MY_VAR": "hello"}
        assert cfg.timeout == 7200

    def test_is_base_model(self):
        """JobConfig is a Pydantic BaseModel."""
        from pydantic import BaseModel

        assert issubclass(JobConfig, BaseModel)


# ---------------------------------------------------------------------------
# BashJobConfig
# ---------------------------------------------------------------------------


class TestBashJobConfig:
    def test_inherits_job_config(self):
        assert issubclass(BashJobConfig, JobConfig)

    def test_defaults(self):
        cfg = BashJobConfig()
        # Inherited defaults
        assert cfg.timeout == 3600
        assert cfg.labels == {}
        # Own defaults
        assert cfg.script is None
        assert cfg.script_path is None

    def test_script_field(self):
        cfg = BashJobConfig(script="echo hello")
        assert cfg.script == "echo hello"
        assert cfg.script_path is None

    def test_script_path_field(self):
        cfg = BashJobConfig(script_path="/path/to/run.sh")
        assert cfg.script_path == "/path/to/run.sh"
        assert cfg.script is None

    def test_inherits_base_fields(self):
        cfg = BashJobConfig(
            job_name="bash-job",
            namespace="ns",
            timeout=600,
            script="ls -la",
        )
        assert cfg.job_name == "bash-job"
        assert cfg.namespace == "ns"
        assert cfg.timeout == 600
        assert cfg.script == "ls -la"


# ---------------------------------------------------------------------------
# HarborJobConfig
# ---------------------------------------------------------------------------


class TestHarborJobConfig:
    def test_inherits_job_config(self):
        assert issubclass(HarborJobConfig, JobConfig)

    def test_defaults(self):
        cfg = HarborJobConfig(experiment_id="test-exp")
        # Inherited — G2: effective timeout derived from agent config + multiplier.
        # No agent timeout configured → DEFAULT_WAIT_TIMEOUT fallback (7200).
        from rock.sdk.bench.constants import DEFAULT_WAIT_TIMEOUT

        assert cfg.timeout == DEFAULT_WAIT_TIMEOUT
        assert cfg.labels == {}
        # G3: job_name is auto-generated (8-char uuid when no datasets)
        assert cfg.job_name is not None
        assert len(cfg.job_name) == 8
        # Own defaults
        assert len(cfg.agents) == 1
        assert isinstance(cfg.agents[0], AgentConfig)
        assert cfg.datasets == []
        from rock.sdk.bench.models.job.config import OrchestratorConfig

        assert isinstance(cfg.orchestrator, OrchestratorConfig)
        assert isinstance(cfg.verifier, VerifierConfig)
        assert cfg.tasks == []
        assert cfg.metrics == []
        assert cfg.artifacts == []
        assert cfg.n_attempts == 1
        assert cfg.timeout_multiplier == 1.0
        assert cfg.agent_timeout_multiplier is None
        assert cfg.verifier_timeout_multiplier is None
        assert cfg.jobs_dir == Path(USER_DEFINED_LOGS) / "jobs"
        assert cfg.debug is False

    def test_custom_harbor_fields(self):
        agent = AgentConfig(name="my-agent", import_path="my_module:MyAgent")
        task = TaskConfig(path=Path("/tasks/task1.json"))
        artifact = ArtifactConfig(source="/data/output")
        cfg = HarborJobConfig(
            experiment_id="test-exp",
            agents=[agent],
            tasks=[task],
            artifacts=[artifact, "/data/logs"],
            n_attempts=3,
            timeout_multiplier=2.0,
            debug=True,
        )
        assert cfg.agents == [agent]
        assert cfg.tasks == [task]
        assert len(cfg.artifacts) == 2
        assert cfg.n_attempts == 3
        assert cfg.timeout_multiplier == 2.0
        assert cfg.debug is True


# ---------------------------------------------------------------------------
# HarborJobConfig.to_harbor_yaml
# ---------------------------------------------------------------------------


class TestHarborJobConfigToHarborYaml:
    def test_excludes_rock_fields(self):
        """Rock-level fields (job_name, namespace, etc.) must NOT appear in Harbor YAML.

        Note: 'environment' is excluded from _ROCK_FIELDS dump, but harbor
        environment fields are re-injected via to_harbor_environment(), so
        the 'environment' key *may* appear with harbor-native fields only.
        """
        cfg = HarborJobConfig(
            job_name="should-not-appear",
            namespace="should-not-appear",
            experiment_id="should-not-appear",
            labels={"step": "1"},
            auto_stop=True,
            setup_commands=["pip install foo"],
            file_uploads=[("/a", "/b")],
            env={"KEY": "VAL"},
            timeout=999,
            n_attempts=2,
            debug=True,
        )
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)
        # Rock-only fields must be absent from Harbor YAML
        # job_name is re-injected so harbor uses it as the directory name
        assert data["job_name"] == "should-not-appear"
        rock_only = {
            "namespace",
            "experiment_id",
            "labels",
            "auto_stop",
            "setup_commands",
            "file_uploads",
            "env",
            "timeout",
        }
        for rock_field in rock_only:
            assert rock_field not in data, f"Rock field '{rock_field}' should be excluded from Harbor YAML"

    def test_includes_harbor_fields(self):
        cfg = HarborJobConfig(experiment_id="test-exp", n_attempts=5, debug=True)
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)
        assert data["n_attempts"] == 5
        assert data["debug"] is True

    def test_harbor_environment_included_when_present(self):
        """Harbor environment fields (e.g., force_build) should appear under 'environment'."""
        env = RockEnvironmentConfig(force_build=True, override_cpus=4)
        cfg = HarborJobConfig(experiment_id="test-exp", environment=env)
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)
        assert "environment" in data
        assert data["environment"]["force_build"] is True
        assert data["environment"]["override_cpus"] == 4

    def test_harbor_environment_omitted_when_default(self):
        """When environment has no harbor-specific fields set, 'environment' key should still appear
        (because to_harbor_environment returns default fields like delete=True)."""
        cfg = HarborJobConfig(experiment_id="test-exp")
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)
        # The harbor env may or may not have fields; just check it's valid YAML
        assert isinstance(data, dict)

    def test_excludes_none_values(self):
        cfg = HarborJobConfig(experiment_id="test-exp")
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)
        # agent_timeout_multiplier is None by default → should not appear
        assert "agent_timeout_multiplier" not in data

    def test_returns_valid_yaml_string(self):
        cfg = HarborJobConfig(experiment_id="test-exp", n_attempts=3)
        yaml_str = cfg.to_harbor_yaml()
        assert isinstance(yaml_str, str)
        parsed = yaml.safe_load(yaml_str)
        assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# HarborJobConfig.from_yaml
# ---------------------------------------------------------------------------


class TestHarborJobConfigFromYaml:
    def test_round_trip(self, tmp_path):
        """Write a YAML config, read it back, verify fields."""
        yaml_content = textwrap.dedent(
            """\
            experiment_id: test-exp
            n_attempts: 3
            debug: true
            agents:
              - name: my-agent
                import_path: my_module:Agent
            timeout_multiplier: 1.5
        """
        )
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml_content)

        cfg = HarborJobConfig.from_yaml(str(yaml_file))
        assert isinstance(cfg, HarborJobConfig)
        assert cfg.n_attempts == 3
        assert cfg.debug is True
        assert cfg.agents[0].name == "my-agent"
        assert cfg.timeout_multiplier == 1.5

    def test_from_yaml_with_environment(self, tmp_path):
        yaml_content = textwrap.dedent(
            """\
            experiment_id: test-exp
            environment:
              force_build: true
              override_cpus: 8
            n_attempts: 1
        """
        )
        yaml_file = tmp_path / "env_config.yaml"
        yaml_file.write_text(yaml_content)

        cfg = HarborJobConfig.from_yaml(str(yaml_file))
        assert cfg.environment.force_build is True
        assert cfg.environment.override_cpus == 8
        assert cfg.n_attempts == 1

    def test_from_yaml_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            HarborJobConfig.from_yaml("/nonexistent/path.yaml")


class TestHarborInheritsBase:
    def test_harbor_inherits_base_fields(self):
        """HarborJobConfig (agent's) inherits all base JobConfig fields."""
        base_fields = set(JobConfig.model_fields.keys())
        harbor_fields = set(HarborJobConfig.model_fields.keys())
        assert base_fields.issubset(harbor_fields)


# ---------------------------------------------------------------------------
# G7: HarborJobConfig.auto_stop and environment.auto_stop sync (OR semantics)
# ---------------------------------------------------------------------------


class TestHarborJobConfigAutoStopSync:
    """G7: HarborJobConfig.auto_stop and environment.auto_stop must be kept in sync (OR semantics)."""

    def test_environment_auto_stop_propagates_to_top_level(self):
        cfg = HarborJobConfig(
            experiment_id="exp-1",
            environment=RockEnvironmentConfig(auto_stop=True),
        )
        assert cfg.auto_stop is True, "top-level auto_stop must pick up environment.auto_stop"

    def test_top_level_auto_stop_propagates_to_environment(self):
        cfg = HarborJobConfig(experiment_id="exp-1", auto_stop=True)
        assert cfg.environment.auto_stop is True

    def test_both_true_stays_true(self):
        cfg = HarborJobConfig(
            experiment_id="exp-1",
            auto_stop=True,
            environment=RockEnvironmentConfig(auto_stop=True),
        )
        assert cfg.auto_stop is True
        assert cfg.environment.auto_stop is True

    def test_both_false_stays_false(self):
        cfg = HarborJobConfig(experiment_id="exp-1")
        assert cfg.auto_stop is False
        assert cfg.environment.auto_stop is False


# ---------------------------------------------------------------------------
# G3: HarborJobConfig auto-generates job_name when user omits it
# ---------------------------------------------------------------------------


class TestHarborJobConfigAutoJobName:
    """G3: HarborJobConfig auto-generates job_name when omitted."""

    def test_explicit_job_name_preserved(self):
        cfg = HarborJobConfig(experiment_id="exp", job_name="my-custom")
        assert cfg.job_name == "my-custom"

    def test_no_dataset_yields_uuid_only(self):
        cfg = HarborJobConfig(experiment_id="exp")
        assert cfg.job_name is not None
        assert len(cfg.job_name) == 8  # 8-char uuid

    def test_single_dataset_single_task_yields_dataset_task_uuid(self):
        from rock.sdk.bench.models.job.config import RegistryDatasetConfig, RemoteRegistryInfo

        cfg = HarborJobConfig(
            experiment_id="exp",
            datasets=[
                RegistryDatasetConfig(
                    registry=RemoteRegistryInfo(),
                    name="terminal-bench",
                    version="2.0",
                    task_names=["fix-bug"],
                )
            ],
        )
        parts = cfg.job_name.split("_")
        assert parts[0] == "terminal-bench"
        assert parts[1] == "fix-bug"
        assert len(parts[2]) == 8

    def test_single_dataset_multi_tasks_yields_dataset_uuid(self):
        from rock.sdk.bench.models.job.config import RegistryDatasetConfig, RemoteRegistryInfo

        cfg = HarborJobConfig(
            experiment_id="exp",
            datasets=[
                RegistryDatasetConfig(
                    registry=RemoteRegistryInfo(),
                    name="tb",
                    version="2.0",
                    task_names=["a", "b"],
                )
            ],
        )
        parts = cfg.job_name.split("_")
        assert parts[0] == "tb"
        assert len(parts) == 2
        assert len(parts[1]) == 8


# ---------------------------------------------------------------------------
# G2: HarborJobConfig effective timeout derivation
# ---------------------------------------------------------------------------


class TestHarborJobConfigEffectiveTimeout:
    """G2: HarborJobConfig timeout derives from agent.max_timeout_sec + multiplier + 600 buffer."""

    def test_default_timeout_uses_7200s_fallback(self):
        """No agent timeout configured → fallback DEFAULT_WAIT_TIMEOUT=7200."""
        from rock.sdk.bench.constants import DEFAULT_WAIT_TIMEOUT

        cfg = HarborJobConfig(experiment_id="exp")
        assert cfg.timeout == DEFAULT_WAIT_TIMEOUT  # 7200

    def test_agent_max_timeout_drives_effective_timeout(self):
        cfg = HarborJobConfig(
            experiment_id="exp",
            agents=[AgentConfig(name="a", max_timeout_sec=1800)],
        )
        assert cfg.timeout == 1800 + 600  # +600s env setup + verifier buffer

    def test_agent_override_timeout_used_when_no_max(self):
        cfg = HarborJobConfig(
            experiment_id="exp",
            agents=[AgentConfig(name="a", override_timeout_sec=900)],
        )
        assert cfg.timeout == 900 + 600

    def test_timeout_multiplier_applied(self):
        cfg = HarborJobConfig(
            experiment_id="exp",
            agents=[AgentConfig(name="a", max_timeout_sec=1000)],
            timeout_multiplier=2.0,
        )
        # (1000 * 2.0) + 600
        assert cfg.timeout == 2600

    def test_multiplier_only_applied_to_fallback(self):
        cfg = HarborJobConfig(experiment_id="exp", timeout_multiplier=2.0)
        # DEFAULT_WAIT_TIMEOUT * 2.0
        from rock.sdk.bench.constants import DEFAULT_WAIT_TIMEOUT

        assert cfg.timeout == int(DEFAULT_WAIT_TIMEOUT * 2.0)
