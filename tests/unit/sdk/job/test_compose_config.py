"""Tests for rock.sdk.job.compose.config — ComposeJobConfig v2 (standard docker-compose)."""

from __future__ import annotations

import textwrap

import pytest

from rock.sdk.job.compose.config import ComposeJobConfig
from rock.sdk.job.config import JobConfig

# ---------------------------------------------------------------------------
# Inheritance
# ---------------------------------------------------------------------------


class TestInheritance:
    def test_inherits_job_config(self):
        assert issubclass(ComposeJobConfig, JobConfig)

    def test_does_not_inherit_bash_job_config(self):
        from rock.sdk.job.config import BashJobConfig

        assert not issubclass(ComposeJobConfig, BashJobConfig)


# ---------------------------------------------------------------------------
# compose_file — required & validation
# ---------------------------------------------------------------------------


class TestComposeFile:
    def test_compose_file_required(self):
        """compose_file is a required field; omitting it raises."""
        with pytest.raises(Exception):
            ComposeJobConfig()

    def test_compose_file_empty_raises(self):
        """compose_file must not be empty string."""
        with pytest.raises(Exception, match="compose_file"):
            ComposeJobConfig(compose_file="")

    def test_compose_file_valid(self):
        cfg = ComposeJobConfig(compose_file="./docker-compose.yaml")
        assert cfg.compose_file == "./docker-compose.yaml"

    def test_compose_file_absolute_path_allowed(self):
        cfg = ComposeJobConfig(compose_file="/rock/compose/docker-compose.yaml")
        assert cfg.compose_file == "/rock/compose/docker-compose.yaml"


# ---------------------------------------------------------------------------
# abort_on_container_exit — default True
# ---------------------------------------------------------------------------


class TestAbortOnContainerExit:
    def test_default_true(self):
        cfg = ComposeJobConfig(compose_file="./docker-compose.yaml")
        assert cfg.abort_on_container_exit is True

    def test_explicit_false(self):
        cfg = ComposeJobConfig(compose_file="./docker-compose.yaml", abort_on_container_exit=False)
        assert cfg.abort_on_container_exit is False

    def test_explicit_true(self):
        cfg = ComposeJobConfig(compose_file="./docker-compose.yaml", abort_on_container_exit=True)
        assert cfg.abort_on_container_exit is True


# ---------------------------------------------------------------------------
# extra="forbid" — v1 fields must be rejected
# ---------------------------------------------------------------------------


class TestExtraForbid:
    def test_v1_compose_field_rejected(self):
        """v1-era top-level 'compose' block must be rejected."""
        with pytest.raises(Exception):
            ComposeJobConfig(
                compose_file="./docker-compose.yaml",
                compose={"main": {"image": "main:latest"}},
            )

    def test_script_path_rejected(self):
        """script_path (BashJobConfig field) must be rejected."""
        with pytest.raises(Exception):
            ComposeJobConfig(
                compose_file="./docker-compose.yaml",
                script_path="./main.sh",
            )

    def test_script_rejected(self):
        """script (BashJobConfig field) must be rejected."""
        with pytest.raises(Exception):
            ComposeJobConfig(
                compose_file="./docker-compose.yaml",
                script="echo hello",
            )

    def test_unknown_field_rejected(self):
        with pytest.raises(Exception):
            ComposeJobConfig(compose_file="./docker-compose.yaml", unknown_field="x")


# ---------------------------------------------------------------------------
# Inherited JobConfig fields
# ---------------------------------------------------------------------------


class TestInheritedFields:
    def test_job_name_default_datetime(self):
        import re

        cfg = ComposeJobConfig(compose_file="./docker-compose.yaml")
        assert re.match(r"\d{4}-\d{2}-\d{2}__\d{2}-\d{2}-\d{2}", cfg.job_name)

    def test_explicit_job_name(self):
        cfg = ComposeJobConfig(job_name="my-compose-job", compose_file="./docker-compose.yaml")
        assert cfg.job_name == "my-compose-job"

    def test_namespace_default_none(self):
        cfg = ComposeJobConfig(compose_file="./docker-compose.yaml")
        assert cfg.namespace is None

    def test_namespace_set(self):
        cfg = ComposeJobConfig(compose_file="./docker-compose.yaml", namespace="xrl-sandbox")
        assert cfg.namespace == "xrl-sandbox"

    def test_timeout_default(self):
        cfg = ComposeJobConfig(compose_file="./docker-compose.yaml")
        assert cfg.timeout == 7200

    def test_timeout_custom(self):
        cfg = ComposeJobConfig(compose_file="./docker-compose.yaml", timeout=3600)
        assert cfg.timeout == 3600

    def test_labels_default_empty(self):
        cfg = ComposeJobConfig(compose_file="./docker-compose.yaml")
        assert cfg.labels == {}

    def test_labels_set(self):
        cfg = ComposeJobConfig(compose_file="./docker-compose.yaml", labels={"team": "xrl", "task": "swe"})
        assert cfg.labels == {"team": "xrl", "task": "swe"}

    def test_environment_accessible(self):
        from rock.sdk.envhub import EnvironmentConfig

        cfg = ComposeJobConfig(compose_file="./docker-compose.yaml")
        assert isinstance(cfg.environment, EnvironmentConfig)


# ---------------------------------------------------------------------------
# from_yaml
# ---------------------------------------------------------------------------


class TestFromYaml:
    def test_from_yaml_minimal(self, tmp_path):
        content = "compose_file: ./docker-compose.yaml\n"
        p = tmp_path / "cfg.yaml"
        p.write_text(content)
        cfg = ComposeJobConfig.from_yaml(str(p))
        assert isinstance(cfg, ComposeJobConfig)
        assert cfg.compose_file == "./docker-compose.yaml"

    def test_from_yaml_full(self, tmp_path):
        content = textwrap.dedent(
            """\
            job_name: swe-bench-job
            namespace: xrl-sandbox
            timeout: 3600
            compose_file: ./docker-compose.yaml
            abort_on_container_exit: false
            labels:
              team: xrl
              task: swe-bench
            environment:
              image: docker:27-dind
              memory: "32g"
              cpus: 8
        """
        )
        p = tmp_path / "cfg.yaml"
        p.write_text(content)
        cfg = ComposeJobConfig.from_yaml(str(p))
        assert cfg.job_name == "swe-bench-job"
        assert cfg.namespace == "xrl-sandbox"
        assert cfg.timeout == 3600
        assert cfg.compose_file == "./docker-compose.yaml"
        assert cfg.abort_on_container_exit is False
        assert cfg.labels == {"team": "xrl", "task": "swe-bench"}

    def test_from_yaml_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            ComposeJobConfig.from_yaml("/nonexistent/path.yaml")

    def test_from_yaml_missing_compose_file_raises(self, tmp_path):
        """YAML without compose_file should fail."""
        content = "job_name: my-job\ntimeout: 300\n"
        p = tmp_path / "cfg.yaml"
        p.write_text(content)
        with pytest.raises(Exception):
            ComposeJobConfig.from_yaml(str(p))

    def test_from_yaml_empty_compose_file_raises(self, tmp_path):
        """YAML with compose_file: '' should fail."""
        content = "compose_file: ''\n"
        p = tmp_path / "cfg.yaml"
        p.write_text(content)
        with pytest.raises(Exception):
            ComposeJobConfig.from_yaml(str(p))
