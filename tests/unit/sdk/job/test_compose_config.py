"""Tests for rock.sdk.job.compose.config — ComposeJobConfig and sub-models."""

from __future__ import annotations

import textwrap
from unittest.mock import patch

import pytest

from rock.sdk.job.compose.config import (
    ComposeJobConfig,
    ComposeSpec,
    HealthSpec,
    InitContainerSpec,
    MainContainerSpec,
    OssDep,
    ResourceSpec,
    SecretEnvEntry,
    SidecarSpec,
    VolumeMount,
)
from rock.sdk.job.config import BashJobConfig, JobConfig

# ---------------------------------------------------------------------------
# ResourceSpec
# ---------------------------------------------------------------------------


class TestResourceSpec:
    def test_defaults(self):
        r = ResourceSpec()
        assert r.cpus is None
        assert r.memory is None
        assert r.cpu_limit is None
        assert r.memory_limit is None

    def test_all_fields(self):
        r = ResourceSpec(cpus=4.0, memory="12g", cpu_limit=8.0, memory_limit="24g")
        assert r.cpus == 4.0
        assert r.memory == "12g"
        assert r.cpu_limit == 8.0
        assert r.memory_limit == "24g"

    def test_extra_forbid(self):
        with pytest.raises(Exception):
            ResourceSpec(cpus=1, unknown_field="x")


# ---------------------------------------------------------------------------
# VolumeMount
# ---------------------------------------------------------------------------


class TestVolumeMount:
    def test_defaults(self):
        v = VolumeMount(name="vol", mount_path="/data")
        assert v.name == "vol"
        assert v.mount_path == "/data"
        assert v.main_mount_path is None

    def test_all_fields(self):
        v = VolumeMount(name="vol", mount_path="/data", main_mount_path="/main-data")
        assert v.main_mount_path == "/main-data"

    def test_extra_forbid(self):
        with pytest.raises(Exception):
            VolumeMount(name="v", mount_path="/p", extra_field="x")


# ---------------------------------------------------------------------------
# SecretEnvEntry
# ---------------------------------------------------------------------------


class TestSecretEnvEntry:
    def test_fields(self):
        s = SecretEnvEntry(secret_name="my-secret", secret_key="api-key")
        assert s.secret_name == "my-secret"
        assert s.secret_key == "api-key"

    def test_extra_forbid(self):
        with pytest.raises(Exception):
            SecretEnvEntry(secret_name="s", secret_key="k", bad="x")


# ---------------------------------------------------------------------------
# OssDep
# ---------------------------------------------------------------------------


class TestOssDep:
    def test_defaults(self):
        o = OssDep(key="path/to/obj", target_path="/data/")
        assert o.key == "path/to/obj"
        assert o.target_path == "/data/"
        assert o.extract is False

    def test_extract_true(self):
        o = OssDep(key="archive.tar.gz", target_path="/out/", extract=True)
        assert o.extract is True

    def test_extra_forbid(self):
        with pytest.raises(Exception):
            OssDep(key="k", target_path="/t", bad="x")


# ---------------------------------------------------------------------------
# HealthSpec
# ---------------------------------------------------------------------------


class TestHealthSpec:
    def test_defaults(self):
        h = HealthSpec(port=8080)
        assert h.port == 8080
        assert h.timeout_sec == 60

    def test_custom_timeout(self):
        h = HealthSpec(port=9090, timeout_sec=120)
        assert h.timeout_sec == 120

    def test_extra_forbid(self):
        with pytest.raises(Exception):
            HealthSpec(port=80, extra="x")


# ---------------------------------------------------------------------------
# InitContainerSpec / SidecarSpec (via _ContainerBase)
# ---------------------------------------------------------------------------


class TestContainerBase:
    """Tests for _ContainerBase validators via InitContainerSpec (concrete subclass)."""

    def test_valid_name_pattern(self):
        c = InitContainerSpec(name="my-container", image="ubuntu:22.04")
        assert c.name == "my-container"

    def test_valid_name_alphanumeric_only(self):
        c = InitContainerSpec(name="abc123", image="ubuntu:22.04")
        assert c.name == "abc123"

    def test_invalid_name_uppercase(self):
        with pytest.raises(Exception, match="invalid"):
            InitContainerSpec(name="MyContainer", image="ubuntu:22.04")

    def test_invalid_name_starts_with_dash(self):
        with pytest.raises(Exception, match="invalid"):
            InitContainerSpec(name="-bad", image="ubuntu:22.04")

    def test_invalid_name_underscore(self):
        with pytest.raises(Exception, match="invalid"):
            InitContainerSpec(name="my_container", image="ubuntu:22.04")

    def test_defaults(self):
        c = InitContainerSpec(name="init", image="ubuntu:22.04")
        assert c.script is None
        assert c.script_path is None
        assert c.command is None
        assert c.args is None
        assert c.env == {}
        assert c.secret_env == {}
        assert c.resources is None
        assert c.privileged is False
        assert c.volume_mounts == []

    def test_script_only(self):
        c = InitContainerSpec(name="init", image="ubuntu:22.04", script="echo hi")
        assert c.script == "echo hi"

    def test_script_path_only(self):
        c = InitContainerSpec(name="init", image="ubuntu:22.04", script_path="/run.sh")
        assert c.script_path == "/run.sh"

    def test_command_only(self):
        c = InitContainerSpec(name="init", image="ubuntu:22.04", command=["dockerd"])
        assert c.command == ["dockerd"]

    def test_command_with_args(self):
        c = InitContainerSpec(name="init", image="ubuntu:22.04", command=["dockerd"], args=["--tls=false"])
        assert c.command == ["dockerd"]
        assert c.args == ["--tls=false"]

    def test_entrypoint_exclusive_script_and_script_path(self):
        with pytest.raises(Exception, match="mutually exclusive"):
            InitContainerSpec(name="init", image="ubuntu:22.04", script="echo hi", script_path="/run.sh")

    def test_entrypoint_exclusive_script_and_command(self):
        with pytest.raises(Exception, match="mutually exclusive"):
            InitContainerSpec(name="init", image="ubuntu:22.04", script="echo hi", command=["bash"])

    def test_entrypoint_exclusive_all_three(self):
        with pytest.raises(Exception, match="mutually exclusive"):
            InitContainerSpec(
                name="init",
                image="ubuntu:22.04",
                script="echo hi",
                script_path="/run.sh",
                command=["bash"],
            )

    def test_args_without_command_raises(self):
        with pytest.raises(Exception, match="args must be used together with command"):
            InitContainerSpec(name="init", image="ubuntu:22.04", args=["--flag"])

    def test_extra_forbid(self):
        with pytest.raises(Exception):
            InitContainerSpec(name="init", image="ubuntu:22.04", unknown="x")

    def test_privileged_default_false(self):
        c = InitContainerSpec(name="init", image="ubuntu:22.04")
        assert c.privileged is False

    def test_privileged_true(self):
        c = InitContainerSpec(name="init", image="ubuntu:22.04", privileged=True)
        assert c.privileged is True


class TestSidecarSpec:
    def test_defaults(self):
        s = SidecarSpec(name="proxy", image="ubuntu:22.04")
        assert s.health is None
        assert s.volume_mounts == []

    def test_health_field(self):
        s = SidecarSpec(name="proxy", image="ubuntu:22.04", health=HealthSpec(port=8082))
        assert s.health.port == 8082

    def test_extra_forbid(self):
        with pytest.raises(Exception):
            SidecarSpec(name="s", image="img", bad_field="x")


# ---------------------------------------------------------------------------
# MainContainerSpec
# ---------------------------------------------------------------------------


class TestMainContainerSpec:
    def test_required_image(self):
        m = MainContainerSpec(image="myregistry/main:latest")
        assert m.image == "myregistry/main:latest"

    def test_defaults(self):
        m = MainContainerSpec(image="img")
        assert m.resources is None
        assert m.env == {}
        assert m.secret_env == {}
        assert m.oss_deps == []
        assert m.volume_mounts == []
        assert m.privileged is False

    def test_extra_forbid(self):
        with pytest.raises(Exception):
            MainContainerSpec(image="img", unknown="x")

    def test_privileged(self):
        m = MainContainerSpec(image="img", privileged=True)
        assert m.privileged is True


# ---------------------------------------------------------------------------
# ComposeSpec
# ---------------------------------------------------------------------------


class TestComposeSpec:
    def test_minimal(self):
        cs = ComposeSpec(main=MainContainerSpec(image="main:latest"))
        assert cs.init_containers == []
        assert cs.sidecars == []

    def test_unique_names_ok(self):
        cs = ComposeSpec(
            main=MainContainerSpec(image="main:latest"),
            init_containers=[InitContainerSpec(name="init1", image="img")],
            sidecars=[SidecarSpec(name="sidecar1", image="img")],
        )
        assert len(cs.init_containers) == 1
        assert len(cs.sidecars) == 1

    def test_duplicate_names_raises(self):
        with pytest.raises(Exception, match="unique"):
            ComposeSpec(
                main=MainContainerSpec(image="main:latest"),
                init_containers=[InitContainerSpec(name="dup", image="img")],
                sidecars=[SidecarSpec(name="dup", image="img")],
            )

    def test_duplicate_among_sidecars_raises(self):
        with pytest.raises(Exception, match="unique"):
            ComposeSpec(
                main=MainContainerSpec(image="main:latest"),
                sidecars=[
                    SidecarSpec(name="dup", image="img"),
                    SidecarSpec(name="dup", image="img2"),
                ],
            )

    def test_extra_forbid(self):
        with pytest.raises(Exception):
            ComposeSpec(main=MainContainerSpec(image="img"), unknown="x")


# ---------------------------------------------------------------------------
# ComposeJobConfig
# ---------------------------------------------------------------------------


class TestComposeJobConfig:
    def _minimal_compose(self):
        return {"main": {"image": "main:latest"}}

    def test_inherits_bash_job_config(self):
        assert issubclass(ComposeJobConfig, BashJobConfig)

    def test_inherits_job_config(self):
        assert issubclass(ComposeJobConfig, JobConfig)

    def test_minimal_config(self):
        cfg = ComposeJobConfig(compose={"main": {"image": "main:latest"}})
        assert isinstance(cfg.compose, ComposeSpec)
        assert cfg.compose.main.image == "main:latest"

    def test_job_name_default_datetime(self):
        import re

        cfg = ComposeJobConfig(compose={"main": {"image": "main:latest"}})
        assert re.match(r"\d{4}-\d{2}-\d{2}__\d{2}-\d{2}-\d{2}", cfg.job_name)

    def test_explicit_job_name_preserved(self):
        cfg = ComposeJobConfig(job_name="my-job", compose={"main": {"image": "main:latest"}})
        assert cfg.job_name == "my-job"

    def test_extra_forbid(self):
        with pytest.raises(Exception):
            ComposeJobConfig(compose={"main": {"image": "img"}}, unknown_field="x")

    def test_compose_required(self):
        with pytest.raises(Exception):
            ComposeJobConfig()

    def test_proxy_conflict_check_raises(self):
        with pytest.raises(Exception, match="proxy"):
            ComposeJobConfig(
                compose={
                    "main": {"image": "main:latest"},
                    "sidecars": [{"name": "proxy", "image": "proxy:latest"}],
                },
                environment={
                    "proxy": {"enabled": True},
                },
            )

    def test_proxy_conflict_check_ok_when_proxy_disabled(self):
        """No conflict when environment.proxy.enabled=False."""

        # Should not raise
        cfg = ComposeJobConfig(
            compose={
                "main": {"image": "main:latest"},
                "sidecars": [{"name": "proxy", "image": "proxy:latest"}],
            },
            environment={
                "proxy": {"enabled": False},
            },
        )
        assert cfg is not None

    def test_proxy_conflict_check_ok_when_no_proxy_sidecar(self):
        """No conflict when sidecar is not named 'proxy'."""

        cfg = ComposeJobConfig(
            compose={
                "main": {"image": "main:latest"},
                "sidecars": [{"name": "notproxy", "image": "proxy:latest"}],
            },
            environment={
                "proxy": {"enabled": True},
            },
        )
        assert cfg is not None

    def test_resource_budget_check_logs_warning_when_over(self):
        """_resource_budget_check logs a warning when inner cpus exceed outer."""
        import rock.sdk.job.compose.config as compose_module

        with patch.object(compose_module.logger, "warning") as mock_warn:
            ComposeJobConfig(
                environment={"cpus": 2, "memory": "4g"},
                compose={
                    "main": {"image": "main:latest", "resources": {"cpus": 4}},
                    "sidecars": [{"name": "side", "image": "side:latest", "resources": {"cpus": 4}}],
                },
            )
        # Warning should have been called (inner total cpus > outer)
        assert mock_warn.called

    def test_resource_budget_check_no_warning_within_budget(self):
        """No warning when inner resources are within outer sandbox budget."""
        import rock.sdk.job.compose.config as compose_module

        with patch.object(compose_module.logger, "warning") as mock_warn:
            ComposeJobConfig(
                environment={"cpus": 16, "memory": "32g"},
                compose={
                    "main": {"image": "main:latest", "resources": {"cpus": 4, "memory": "8g"}},
                    "sidecars": [{"name": "side", "image": "side:latest", "resources": {"cpus": 1, "memory": "2g"}}],
                },
            )
        assert not mock_warn.called

    def test_full_config(self):
        """Test a full ComposeJobConfig round-trip."""
        cfg = ComposeJobConfig(
            job_name="test-job",
            compose={
                "main": {
                    "image": "main:latest",
                    "resources": {"cpus": 4, "memory": "16g"},
                    "env": {"KEY": "VALUE"},
                    "oss_deps": [{"key": "path/to/obj", "target_path": "/data/", "extract": True}],
                },
                "init_containers": [
                    {
                        "name": "init1",
                        "image": "init:latest",
                        "script_path": "/init.sh",
                        "volume_mounts": [{"name": "vol", "mount_path": "/data", "main_mount_path": "/main-data"}],
                    }
                ],
                "sidecars": [
                    {
                        "name": "sidecar1",
                        "image": "sidecar:latest",
                        "script": "echo hello",
                        "health": {"port": 8080},
                    }
                ],
            },
        )
        assert cfg.job_name == "test-job"
        assert cfg.compose.main.image == "main:latest"
        assert cfg.compose.main.resources.cpus == 4
        assert cfg.compose.main.resources.memory == "16g"
        assert cfg.compose.main.env == {"KEY": "VALUE"}
        assert cfg.compose.main.oss_deps[0].key == "path/to/obj"
        assert cfg.compose.main.oss_deps[0].extract is True
        assert cfg.compose.init_containers[0].name == "init1"
        assert cfg.compose.init_containers[0].script_path == "/init.sh"
        assert cfg.compose.init_containers[0].volume_mounts[0].name == "vol"
        assert cfg.compose.sidecars[0].name == "sidecar1"
        assert cfg.compose.sidecars[0].health.port == 8080


# ---------------------------------------------------------------------------
# ComposeJobConfig.from_yaml
# ---------------------------------------------------------------------------


class TestComposeJobConfigFromYaml:
    def test_from_yaml_minimal(self, tmp_path):
        content = textwrap.dedent(
            """\
            compose:
              main:
                image: main:latest
        """
        )
        p = tmp_path / "cfg.yaml"
        p.write_text(content)
        cfg = ComposeJobConfig.from_yaml(str(p))
        assert isinstance(cfg, ComposeJobConfig)
        assert cfg.compose.main.image == "main:latest"

    def test_from_yaml_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            ComposeJobConfig.from_yaml("/nonexistent/path.yaml")

    def test_from_yaml_full(self, tmp_path):
        content = textwrap.dedent(
            """\
            job_name: my-compose-job
            timeout: 3600
            compose:
              main:
                image: myregistry/main:latest
                resources:
                  cpus: 4
                  memory: "16g"
                env:
                  DATASET: my-dataset
                oss_deps:
                  - key: path/to/archive.tar.gz
                    target_path: /data/
                    extract: true
              init_containers:
                - name: init1
                  image: init:latest
                  script_path: /init.sh
              sidecars:
                - name: proxy
                  image: proxy:latest
                  script_path: /proxy.sh
                  health:
                    port: 8082
                    timeout_sec: 60
        """
        )
        p = tmp_path / "cfg.yaml"
        p.write_text(content)
        cfg = ComposeJobConfig.from_yaml(str(p))
        assert cfg.job_name == "my-compose-job"
        assert cfg.timeout == 3600
        assert cfg.compose.main.resources.cpus == 4
        assert cfg.compose.main.oss_deps[0].extract is True
        assert cfg.compose.init_containers[0].name == "init1"
        assert cfg.compose.sidecars[0].name == "proxy"
        assert cfg.compose.sidecars[0].health.port == 8082


# ---------------------------------------------------------------------------
# JobConfig.from_yaml — three-way auto-detection
# ---------------------------------------------------------------------------


class TestJobConfigFromYamlThreeWay:
    """JobConfig.from_yaml dispatches to the correct subclass including ComposeJobConfig."""

    def test_bash_script_detected(self, tmp_path):
        yaml_content = "script: echo hello\ntimeout: 60\n"
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml_content)
        cfg = JobConfig.from_yaml(str(p))
        assert isinstance(cfg, BashJobConfig)
        assert not isinstance(cfg, ComposeJobConfig)

    def test_harbor_experiment_id_detected(self, tmp_path):
        from rock.sdk.bench.models.job.config import HarborJobConfig

        yaml_content = "experiment_id: exp-1\nagents:\n  - name: my-agent\n"
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml_content)
        cfg = JobConfig.from_yaml(str(p))
        assert isinstance(cfg, HarborJobConfig)

    def test_compose_key_detected(self, tmp_path):
        yaml_content = textwrap.dedent(
            """\
            compose:
              main:
                image: main:latest
        """
        )
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml_content)
        cfg = JobConfig.from_yaml(str(p))
        assert isinstance(cfg, ComposeJobConfig)

    def test_compose_with_script_path(self, tmp_path):
        """script_path at top-level (from BashJobConfig) + compose block → ComposeJobConfig."""
        yaml_content = textwrap.dedent(
            """\
            script_path: ./main.sh
            compose:
              main:
                image: main:latest
        """
        )
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml_content)
        cfg = JobConfig.from_yaml(str(p))
        assert isinstance(cfg, ComposeJobConfig)
        assert cfg.script_path == "./main.sh"

    def test_compose_with_full_config(self, tmp_path):
        yaml_content = textwrap.dedent(
            """\
            job_name: swe-job
            script_path: ./main.sh
            timeout: 7200
            compose:
              main:
                image: myregistry/main:latest
                resources:
                  cpus: 4
                  memory: "16g"
              sidecars:
                - name: proxy
                  image: proxy:latest
                  script_path: /proxy.sh
        """
        )
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml_content)
        cfg = JobConfig.from_yaml(str(p))
        assert isinstance(cfg, ComposeJobConfig)
        assert cfg.job_name == "swe-job"
        assert cfg.compose.sidecars[0].name == "proxy"

    def test_invalid_compose_raises_value_error(self, tmp_path):
        """A YAML with 'compose' key but an invalid compose structure raises a descriptive error."""
        yaml_content = textwrap.dedent(
            """\
            compose:
              main: {}
        """
        )
        # compose.main is missing the required 'image' field → ComposeJobConfig fails
        # BashJobConfig rejects 'compose' (extra=forbid) → also fails
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml_content)
        with pytest.raises(ValueError, match="does not match any known job type"):
            JobConfig.from_yaml(str(p))

    def test_bash_direct_from_yaml_unaffected(self, tmp_path):
        """BashJobConfig.from_yaml() still works independently."""
        yaml_content = "script: ls -la\ntimeout: 120\n"
        p = tmp_path / "bash.yaml"
        p.write_text(yaml_content)
        cfg = BashJobConfig.from_yaml(str(p))
        assert isinstance(cfg, BashJobConfig)
        assert cfg.script == "ls -la"
