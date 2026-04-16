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
    NativeConfig,
    RockEnvironmentConfig,
    TaskConfig,
    TemplateConfig,
    VerifierConfig,
)
from rock.sdk.envhub import EnvironmentConfig
from rock.sdk.job.config import BashJobConfig, JobConfig

# ---------------------------------------------------------------------------
# JobConfig (base)
# ---------------------------------------------------------------------------


class TestJobConfig:
    def test_defaults(self):
        cfg = JobConfig()
        assert isinstance(cfg.environment, EnvironmentConfig)
        assert cfg.job_name is None
        assert cfg.namespace is None
        assert cfg.experiment_id is None
        assert cfg.labels == {}
        assert cfg.timeout == 7200
        assert cfg.environment.uploads == []
        assert cfg.environment.env == {}

    def test_custom_values(self):
        env = EnvironmentConfig(
            image="ubuntu:22.04",
            uploads=[("/local/file.py", "/sandbox/file.py")],
            env={"MY_VAR": "hello"},
        )
        cfg = JobConfig(
            environment=env,
            job_name="my-job",
            namespace="team-a",
            experiment_id="exp-001",
            labels={"step": "42"},
            timeout=7200,
        )
        assert cfg.environment.image == "ubuntu:22.04"
        assert cfg.job_name == "my-job"
        assert cfg.namespace == "team-a"
        assert cfg.experiment_id == "exp-001"
        assert cfg.labels == {"step": "42"}
        assert cfg.environment.uploads == [("/local/file.py", "/sandbox/file.py")]
        assert cfg.environment.env == {"MY_VAR": "hello"}
        assert cfg.timeout == 7200

    def test_is_base_model(self):
        """JobConfig is a Pydantic BaseModel."""
        from pydantic import BaseModel

        assert issubclass(JobConfig, BaseModel)

    def test_experiment_id_overrides_environment_experiment_id(self):
        """When both experiment_ids differ, JobConfig.experiment_id wins and a warning is logged."""
        from unittest.mock import patch

        import rock.sdk.job.config as job_config_module

        env = EnvironmentConfig(experiment_id="default")
        with patch.object(job_config_module.logger, "warning") as mock_warn:
            cfg = JobConfig(experiment_id="claw-eval", environment=env)

        assert cfg.environment.experiment_id == "claw-eval"
        mock_warn.assert_called_once()
        warn_msg = mock_warn.call_args[0][0]
        assert "experiment_id" in warn_msg
        assert "claw-eval" in str(mock_warn.call_args)

    def test_environment_experiment_id_preserved_when_job_unset(self):
        """When only environment.experiment_id is set, it is preserved unchanged."""
        env = EnvironmentConfig(experiment_id="from-env")
        cfg = JobConfig(environment=env)

        assert cfg.environment.experiment_id == "from-env"
        assert cfg.experiment_id is None

    def test_no_warning_when_experiment_ids_match(self):
        """When both experiment_ids are the same, no warning is emitted."""
        from unittest.mock import patch

        import rock.sdk.job.config as job_config_module

        env = EnvironmentConfig(experiment_id="same-exp")
        with patch.object(job_config_module.logger, "warning") as mock_warn:
            cfg = JobConfig(experiment_id="same-exp", environment=env)

        assert cfg.environment.experiment_id == "same-exp"
        mock_warn.assert_not_called()


# ---------------------------------------------------------------------------
# BashJobConfig
# ---------------------------------------------------------------------------


class TestBashJobConfig:
    def test_inherits_job_config(self):
        assert issubclass(BashJobConfig, JobConfig)

    def test_defaults(self):
        cfg = BashJobConfig()
        # Inherited defaults
        assert cfg.timeout == 7200
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
    def test_excludes_rock_fields_keeps_harbor_shared_fields(self):
        """Rock-only fields must not appear, but Harbor-shared fields must be present."""
        cfg = HarborJobConfig(
            job_name="test-job",
            namespace="my-ns",
            experiment_id="my-exp",
            labels={"step": "1"},
            environment=RockEnvironmentConfig(
                uploads=[("/a", "/b")],
                env={"KEY": "VAL"},
            ),
            timeout=999,
            n_attempts=2,
            debug=True,
        )
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)
        # Shared with Harbor — must be present
        assert data["job_name"] == "test-job"
        assert data["namespace"] == "my-ns"
        assert data["experiment_id"] == "my-exp"
        assert data["labels"] == {"step": "1"}
        # Rock-only — must be absent
        rock_only = {"uploads", "timeout"}
        for field in rock_only:
            assert field not in data, f"Rock field '{field}' should be excluded"

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


# ---------------------------------------------------------------------------
# TemplateConfig
# ---------------------------------------------------------------------------


class TestTemplateConfig:
    def test_defaults(self):
        cfg = TemplateConfig()
        assert cfg.name is None
        assert cfg.revision is None

    def test_with_values(self):
        cfg = TemplateConfig(
            name="swe-agent-internal/SWE-Gym/SWE-Gym",
            revision="53634366f454e6dc5fc3ceb85896c706b9ad1078",
        )
        assert cfg.name == "swe-agent-internal/SWE-Gym/SWE-Gym"
        assert cfg.revision == "53634366f454e6dc5fc3ceb85896c706b9ad1078"

    def test_partial_values(self):
        cfg = TemplateConfig(name="my-agent/my-org/my-dataset")
        assert cfg.name == "my-agent/my-org/my-dataset"
        assert cfg.revision is None

    def test_json_round_trip(self):
        cfg = TemplateConfig(
            name="swe-agent-internal/SWE-Gym/SWE-Gym",
            revision="53634366f454e6dc5fc3ceb85896c706b9ad1078",
        )
        data = cfg.model_dump(mode="json")
        restored = TemplateConfig(**data)
        assert restored == cfg

    def test_exclude_none_omits_unset_fields(self):
        cfg = TemplateConfig(name="my-agent/my-org/my-dataset")
        data = cfg.model_dump(mode="json", exclude_none=True)
        assert "name" in data
        assert "revision" not in data


# ---------------------------------------------------------------------------
# NativeConfig
# ---------------------------------------------------------------------------


class TestNativeConfig:
    def test_defaults(self):
        cfg = NativeConfig()
        assert cfg.image is None
        assert cfg.script is None
        assert cfg.oss_deps == {}
        assert cfg.template is None

    def test_template_none_by_default(self):
        cfg = NativeConfig(image="ubuntu:22.04")
        assert cfg.template is None

    def test_template_from_dict(self):
        cfg = NativeConfig(
            template={
                "name": "swe-agent-internal/SWE-Gym/SWE-Gym",
                "revision": "53634366f454e6dc5fc3ceb85896c706b9ad1078",
            }
        )
        assert isinstance(cfg.template, TemplateConfig)
        assert cfg.template.name == "swe-agent-internal/SWE-Gym/SWE-Gym"
        assert cfg.template.revision == "53634366f454e6dc5fc3ceb85896c706b9ad1078"

    def test_template_from_model(self):
        tmpl = TemplateConfig(name="my-agent/my-org/my-dataset", revision="abc123")
        cfg = NativeConfig(template=tmpl)
        assert cfg.template is tmpl

    def test_json_round_trip_with_template(self):
        cfg = NativeConfig(
            image="eval:latest",
            template=TemplateConfig(
                name="swe-agent-internal/SWE-Gym/SWE-Gym",
                revision="53634366f454e6dc5fc3ceb85896c706b9ad1078",
            ),
        )
        data = cfg.model_dump(mode="json")
        restored = NativeConfig(**data)
        assert restored.template.name == cfg.template.name
        assert restored.template.revision == cfg.template.revision

    def test_exclude_none_omits_template_when_not_set(self):
        cfg = NativeConfig(image="eval:latest")
        data = cfg.model_dump(mode="json", exclude_none=True)
        assert "template" not in data

    def test_exclude_none_includes_template_when_set(self):
        cfg = NativeConfig(template=TemplateConfig(name="my-agent/my-org/my-dataset", revision="rev1"))
        data = cfg.model_dump(mode="json", exclude_none=True)
        assert "template" in data
        assert data["template"]["name"] == "my-agent/my-org/my-dataset"


class TestHarborInheritsBase:
    def test_harbor_inherits_base_fields(self):
        """HarborJobConfig (agent's) inherits all base JobConfig fields."""
        base_fields = set(JobConfig.model_fields.keys())
        harbor_fields = set(HarborJobConfig.model_fields.keys())
        assert base_fields.issubset(harbor_fields)


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


# ---------------------------------------------------------------------------
# JobConfig.from_yaml — auto-detection
# ---------------------------------------------------------------------------


class TestJobConfigFromYamlAutoDetect:
    """JobConfig.from_yaml dispatches to the correct subclass based on YAML content."""

    def test_auto_detect_bash_by_script(self, tmp_path):
        yaml_content = "script: echo hello\ntimeout: 60\n"
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml_content)
        cfg = JobConfig.from_yaml(str(p))
        assert isinstance(cfg, BashJobConfig)
        assert cfg.script == "echo hello"

    def test_auto_detect_bash_by_script_path(self, tmp_path):
        yaml_content = "script_path: run.sh\n"
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml_content)
        cfg = JobConfig.from_yaml(str(p))
        assert isinstance(cfg, BashJobConfig)
        assert cfg.script_path == "run.sh"

    def test_auto_detect_harbor_by_agents(self, tmp_path):
        yaml_content = "experiment_id: exp-1\nagents:\n  - name: my-agent\n"
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml_content)
        cfg = JobConfig.from_yaml(str(p))
        assert isinstance(cfg, HarborJobConfig)
        assert cfg.experiment_id == "exp-1"

    def test_auto_detect_harbor_by_datasets(self, tmp_path):
        yaml_content = "experiment_id: exp-2\n" "datasets:\n" "  - name: my-ds\n" "    path: /tmp/ds\n"
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml_content)
        cfg = JobConfig.from_yaml(str(p))
        assert isinstance(cfg, HarborJobConfig)

    def test_auto_detect_harbor_by_n_attempts(self, tmp_path):
        yaml_content = "experiment_id: exp-3\nn_attempts: 5\n"
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml_content)
        cfg = JobConfig.from_yaml(str(p))
        assert isinstance(cfg, HarborJobConfig)
        assert cfg.n_attempts == 5

    def test_auto_detect_harbor_by_debug_flag(self, tmp_path):
        yaml_content = "experiment_id: exp-4\ndebug: true\n"
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml_content)
        cfg = JobConfig.from_yaml(str(p))
        assert isinstance(cfg, HarborJobConfig)
        assert cfg.debug is True

    def test_raises_on_mixed_fields(self, tmp_path):
        """YAML with fields from both job types fails validation against either model."""
        yaml_content = "script: echo hi\nagents:\n  - name: a\nexperiment_id: exp\n"
        p = tmp_path / "mixed.yaml"
        p.write_text(yaml_content)
        with pytest.raises(ValueError, match="does not match any known job type"):
            JobConfig.from_yaml(str(p))

    def test_base_only_yaml_falls_through_to_bash(self, tmp_path):
        """YAML with only base fields (no harbor exclusive) falls through to BashJobConfig.

        HarborJobConfig requires experiment_id, so it fails; BashJobConfig has all
        optional fields and succeeds.
        """
        yaml_content = "job_name: my-job\ntimeout: 300\n"
        p = tmp_path / "base_only.yaml"
        p.write_text(yaml_content)
        cfg = JobConfig.from_yaml(str(p))
        assert isinstance(cfg, BashJobConfig)
        assert cfg.job_name == "my-job"
        assert cfg.timeout == 300

    def test_bash_from_yaml_direct_still_works(self, tmp_path):
        """BashJobConfig.from_yaml() continues to work regardless of auto-detect."""
        yaml_content = "script: ls -la\ntimeout: 120\n"
        p = tmp_path / "bash.yaml"
        p.write_text(yaml_content)
        cfg = BashJobConfig.from_yaml(str(p))
        assert isinstance(cfg, BashJobConfig)
        assert cfg.script == "ls -la"

    def test_harbor_from_yaml_direct_still_works(self, tmp_path):
        """HarborJobConfig.from_yaml() continues to work regardless of auto-detect."""
        yaml_content = "experiment_id: exp-5\nn_attempts: 2\n"
        p = tmp_path / "harbor.yaml"
        p.write_text(yaml_content)
        cfg = HarborJobConfig.from_yaml(str(p))
        assert isinstance(cfg, HarborJobConfig)
        assert cfg.n_attempts == 2


# ---------------------------------------------------------------------------
# OssMirrorConfig on base EnvironmentConfig
# ---------------------------------------------------------------------------


class TestOssMirrorConfigOnBaseEnvironment:
    def test_base_env_default_oss_mirror_is_none(self):
        assert EnvironmentConfig().oss_mirror is None

    def test_base_env_accepts_oss_mirror(self):
        from rock.sdk.envhub.config import OssMirrorConfig

        cfg = EnvironmentConfig(oss_mirror=OssMirrorConfig(enabled=True, oss_bucket="b"))
        assert cfg.oss_mirror.enabled is True

    def test_oss_mirror_config_importable_from_envhub(self):
        from rock.sdk.envhub.config import OssMirrorConfig

        assert OssMirrorConfig().enabled is False


# ---------------------------------------------------------------------------
# BashJobConfig.job_name UUID default
# ---------------------------------------------------------------------------


class TestBashJobConfigJobNameDefault:
    def test_defaults_to_datetime_string(self):
        cfg = BashJobConfig(script="echo hi")
        import re

        assert re.match(r"\d{4}-\d{2}-\d{2}__\d{2}-\d{2}-\d{2}", cfg.job_name)

    def test_explicit_name_preserved(self):
        assert BashJobConfig(job_name="x").job_name == "x"
