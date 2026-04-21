"""
FC Rocklet Runtime Configuration Tests

Tests for the production-ready runtime configurations including
custom runtime and custom container deployment modes.

FC (Function Compute) is Alibaba Cloud's serverless compute service.

Test Coverage:
- IT-RUNTIME-01: Custom Runtime s.yaml configuration validation
- IT-RUNTIME-02: Custom Runtime bootstrap script validation
- IT-RUNTIME-03: Custom Container s.yaml configuration validation
- IT-RUNTIME-04: Custom Container Dockerfile validation
- IT-RUNTIME-05: Session affinity consistency across runtimes
- IT-RUNTIME-06: Resource configuration consistency
"""

import os
import stat
from pathlib import Path

import pytest
import yaml


# ============================================================
# Test Fixtures
# ============================================================


@pytest.fixture
def fc_rocklet_dir() -> Path:
    """Get the fc_rocklet directory path."""
    return Path(__file__).parent.parent.parent.parent / "rock" / "deployments" / "fc_rocklet"


@pytest.fixture
def runtime_dir(fc_rocklet_dir) -> Path:
    """Get the custom runtime directory path."""
    return fc_rocklet_dir / "runtime"


@pytest.fixture
def container_dir(fc_rocklet_dir) -> Path:
    """Get the custom container directory path."""
    return fc_rocklet_dir / "container"


@pytest.fixture
def runtime_config(runtime_dir) -> dict:
    """Load custom runtime s.yaml configuration."""
    config_path = runtime_dir / "s.yaml"
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


@pytest.fixture
def container_config(container_dir) -> dict:
    """Load custom container s.yaml configuration."""
    config_path = container_dir / "s.yaml"
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


# ============================================================
# IT-RUNTIME-01: Custom Runtime s.yaml Configuration
# ============================================================


class TestCustomRuntimeConfig:
    """Integration tests for custom runtime s.yaml configuration.

    Purpose: Verify custom runtime deployment configuration is valid.
    """

    def test_config_file_exists(self, runtime_dir):
        """IT-RUNTIME-01a: Verify s.yaml file exists."""
        config_path = runtime_dir / "s.yaml"
        assert config_path.exists(), f"s.yaml not found at {config_path}"

    def test_config_has_required_fields(self, runtime_config):
        """IT-RUNTIME-01b: Verify required top-level fields exist."""
        assert "edition" in runtime_config
        assert "name" in runtime_config
        assert "resources" in runtime_config

    def test_runtime_is_custom_debian(self, runtime_config):
        """IT-RUNTIME-01c: Verify runtime is custom.debian12."""
        props = runtime_config["resources"]["rock-rocklet"]["props"]
        assert props["runtime"] == "custom.debian12"

    def test_custom_runtime_config_has_port(self, runtime_config):
        """IT-RUNTIME-01d: Verify customRuntimeConfig has port 9000."""
        props = runtime_config["resources"]["rock-rocklet"]["props"]
        assert "customRuntimeConfig" in props
        assert props["customRuntimeConfig"]["port"] == 9000

    def test_custom_runtime_config_has_command(self, runtime_config):
        """IT-RUNTIME-01e: Verify customRuntimeConfig has bootstrap command."""
        props = runtime_config["resources"]["rock-rocklet"]["props"]
        command = props["customRuntimeConfig"]["command"]
        assert "/bin/bash" in command
        assert "/code/bootstrap" in command

    def test_memory_size(self, runtime_config):
        """IT-RUNTIME-01f: Verify memory size is configured."""
        props = runtime_config["resources"]["rock-rocklet"]["props"]
        assert props["memorySize"] == 4096

    def test_cpu_config(self, runtime_config):
        """IT-RUNTIME-01g: Verify CPU is configured."""
        props = runtime_config["resources"]["rock-rocklet"]["props"]
        assert props["cpu"] == 2

    def test_timeout_config(self, runtime_config):
        """IT-RUNTIME-01h: Verify timeout is configured."""
        props = runtime_config["resources"]["rock-rocklet"]["props"]
        assert props["timeout"] == 3600

    def test_instance_concurrency(self, runtime_config):
        """IT-RUNTIME-01i: Verify instance concurrency is configured."""
        props = runtime_config["resources"]["rock-rocklet"]["props"]
        assert props["instanceConcurrency"] == 200

    def test_has_http_trigger(self, runtime_config):
        """IT-RUNTIME-01j: Verify HTTP trigger is configured."""
        props = runtime_config["resources"]["rock-rocklet"]["props"]
        assert "triggers" in props
        triggers = props["triggers"]
        assert len(triggers) > 0
        http_trigger = triggers[0]
        assert http_trigger["triggerType"] == "http"
        assert http_trigger["triggerConfig"]["authType"] == "anonymous"

    def test_http_trigger_methods(self, runtime_config):
        """IT-RUNTIME-01k: Verify HTTP trigger supports required methods."""
        props = runtime_config["resources"]["rock-rocklet"]["props"]
        methods = props["triggers"][0]["triggerConfig"]["methods"]
        assert "GET" in methods
        assert "POST" in methods
        assert "PUT" in methods
        assert "DELETE" in methods


# ============================================================
# IT-RUNTIME-02: Custom Runtime Bootstrap Script
# ============================================================


class TestCustomRuntimeBootstrap:
    """Integration tests for custom runtime bootstrap script.

    Purpose: Verify bootstrap script is valid and executable.
    """

    def test_bootstrap_file_exists(self, runtime_dir):
        """IT-RUNTIME-02a: Verify bootstrap file exists."""
        bootstrap_path = runtime_dir / "bootstrap"
        assert bootstrap_path.exists(), f"bootstrap not found at {bootstrap_path}"

    def test_bootstrap_is_executable(self, runtime_dir):
        """IT-RUNTIME-02b: Verify bootstrap is executable."""
        bootstrap_path = runtime_dir / "bootstrap"
        mode = bootstrap_path.stat().st_mode
        assert mode & stat.S_IXUSR, "bootstrap is not executable"

    def test_bootstrap_is_bash_script(self, runtime_dir):
        """IT-RUNTIME-02c: Verify bootstrap is a bash script."""
        bootstrap_path = runtime_dir / "bootstrap"
        with open(bootstrap_path, "r") as f:
            first_line = f.readline()
        assert first_line.startswith("#!/bin/bash") or first_line.startswith("#!/usr/bin/env bash")

    def test_bootstrap_uses_fc_server_port(self, runtime_dir):
        """IT-RUNTIME-02d: Verify bootstrap uses FC_SERVER_PORT."""
        bootstrap_path = runtime_dir / "bootstrap"
        with open(bootstrap_path, "r") as f:
            content = f.read()
        assert "FC_SERVER_PORT" in content

    def test_bootstrap_starts_rocklet(self, runtime_dir):
        """IT-RUNTIME-02e: Verify bootstrap starts rocklet server."""
        bootstrap_path = runtime_dir / "bootstrap"
        with open(bootstrap_path, "r") as f:
            content = f.read()
        assert "rock.rocklet" in content

    def test_bootstrap_sets_pythonpath(self, runtime_dir):
        """IT-RUNTIME-02f: Verify bootstrap sets PYTHONPATH."""
        bootstrap_path = runtime_dir / "bootstrap"
        with open(bootstrap_path, "r") as f:
            content = f.read()
        assert "PYTHONPATH" in content

    def test_requirements_file_exists(self, runtime_dir):
        """IT-RUNTIME-02g: Verify requirements.txt exists."""
        requirements_path = runtime_dir / "requirements.txt"
        assert requirements_path.exists(), f"requirements.txt not found at {requirements_path}"

    def test_package_script_exists(self, runtime_dir):
        """IT-RUNTIME-02h: Verify package.sh exists."""
        package_path = runtime_dir / "package.sh"
        assert package_path.exists(), f"package.sh not found at {package_path}"


# ============================================================
# IT-RUNTIME-03: Custom Container s.yaml Configuration
# ============================================================


class TestCustomContainerConfig:
    """Integration tests for custom container s.yaml configuration.

    Purpose: Verify custom container deployment configuration is valid.
    """

    def test_config_file_exists(self, container_dir):
        """IT-RUNTIME-03a: Verify s.yaml file exists."""
        config_path = container_dir / "s.yaml"
        assert config_path.exists(), f"s.yaml not found at {config_path}"

    def test_config_has_required_fields(self, container_config):
        """IT-RUNTIME-03b: Verify required top-level fields exist."""
        assert "edition" in container_config
        assert "name" in container_config
        assert "resources" in container_config

    def test_runtime_is_custom_container(self, container_config):
        """IT-RUNTIME-03c: Verify runtime is custom-container."""
        props = container_config["resources"]["rock-rocklet"]["props"]
        assert props["runtime"] == "custom-container"

    def test_custom_container_config_has_port(self, container_config):
        """IT-RUNTIME-03d: Verify customContainerConfig has port 9000."""
        props = container_config["resources"]["rock-rocklet"]["props"]
        assert "customContainerConfig" in props
        assert props["customContainerConfig"]["port"] == 9000

    def test_custom_container_config_has_image(self, container_config):
        """IT-RUNTIME-03e: Verify customContainerConfig has image field."""
        props = container_config["resources"]["rock-rocklet"]["props"]
        assert "image" in props["customContainerConfig"]

    def test_health_check_config(self, container_config):
        """IT-RUNTIME-03f: Verify health check configuration."""
        props = container_config["resources"]["rock-rocklet"]["props"]
        health_check = props["customContainerConfig"]["healthCheckConfig"]
        assert health_check["httpGetUrl"] == "/is_alive"
        assert health_check["initialDelaySeconds"] == 3
        assert health_check["periodSeconds"] == 10
        assert health_check["timeoutSeconds"] == 5
        assert health_check["failureThreshold"] == 3

    def test_memory_size(self, container_config):
        """IT-RUNTIME-03g: Verify memory size is configured."""
        props = container_config["resources"]["rock-rocklet"]["props"]
        assert props["memorySize"] == 4096

    def test_cpu_config(self, container_config):
        """IT-RUNTIME-03h: Verify CPU is configured."""
        props = container_config["resources"]["rock-rocklet"]["props"]
        assert props["cpu"] == 2

    def test_disk_size(self, container_config):
        """IT-RUNTIME-03i: Verify disk size is configured."""
        props = container_config["resources"]["rock-rocklet"]["props"]
        assert props["diskSize"] == 10240

    def test_timeout_config(self, container_config):
        """IT-RUNTIME-03j: Verify timeout is configured."""
        props = container_config["resources"]["rock-rocklet"]["props"]
        assert props["timeout"] == 3600

    def test_instance_concurrency(self, container_config):
        """IT-RUNTIME-03k: Verify instance concurrency is configured."""
        props = container_config["resources"]["rock-rocklet"]["props"]
        assert props["instanceConcurrency"] == 200


# ============================================================
# IT-RUNTIME-04: Custom Container Dockerfile
# ============================================================


class TestCustomContainerDockerfile:
    """Integration tests for custom container Dockerfile.

    Purpose: Verify Dockerfile is valid and follows best practices.
    """

    def test_dockerfile_exists(self, container_dir):
        """IT-RUNTIME-04a: Verify Dockerfile exists."""
        dockerfile_path = container_dir / "Dockerfile"
        assert dockerfile_path.exists(), f"Dockerfile not found at {dockerfile_path}"

    def test_dockerfile_uses_python_base(self, container_dir):
        """IT-RUNTIME-04b: Verify Dockerfile uses Python base image."""
        dockerfile_path = container_dir / "Dockerfile"
        with open(dockerfile_path, "r") as f:
            content = f.read()
        assert "python" in content.lower()

    def test_dockerfile_exposes_port(self, container_dir):
        """IT-RUNTIME-04c: Verify Dockerfile exposes port 9000."""
        dockerfile_path = container_dir / "Dockerfile"
        with open(dockerfile_path, "r") as f:
            content = f.read()
        assert "EXPOSE 9000" in content

    def test_dockerfile_has_healthcheck(self, container_dir):
        """IT-RUNTIME-04d: Verify Dockerfile has HEALTHCHECK."""
        dockerfile_path = container_dir / "Dockerfile"
        with open(dockerfile_path, "r") as f:
            content = f.read()
        assert "HEALTHCHECK" in content
        assert "/is_alive" in content

    def test_dockerfile_starts_rocklet(self, container_dir):
        """IT-RUNTIME-04e: Verify Dockerfile starts rocklet server."""
        dockerfile_path = container_dir / "Dockerfile"
        with open(dockerfile_path, "r") as f:
            content = f.read()
        assert "rock.rocklet" in content

    def test_dockerfile_sets_python_env_vars(self, container_dir):
        """IT-RUNTIME-04f: Verify Dockerfile sets Python environment variables."""
        dockerfile_path = container_dir / "Dockerfile"
        with open(dockerfile_path, "r") as f:
            content = f.read()
        assert "PYTHONUNBUFFERED" in content

    def test_dockerfile_uses_workdir(self, container_dir):
        """IT-RUNTIME-04g: Verify Dockerfile sets WORKDIR."""
        dockerfile_path = container_dir / "Dockerfile"
        with open(dockerfile_path, "r") as f:
            content = f.read()
        assert "WORKDIR" in content

    def test_dockerfile_copies_project(self, container_dir):
        """IT-RUNTIME-04h: Verify Dockerfile copies project files."""
        dockerfile_path = container_dir / "Dockerfile"
        with open(dockerfile_path, "r") as f:
            content = f.read()
        assert "COPY" in content


# ============================================================
# IT-RUNTIME-05: Session Affinity Consistency
# ============================================================


class TestSessionAffinityConsistency:
    """Integration tests for session affinity consistency across runtimes.

    Purpose: Verify all runtimes have consistent session affinity configuration.
    """

    def test_all_runtimes_have_session_affinity(
        self, runtime_config, container_config
    ):
        """IT-RUNTIME-05a: Verify all runtimes have session affinity config."""
        for config in [runtime_config, container_config]:
            props = config["resources"]["rock-rocklet"]["props"]
            assert "sessionAffinity" in props
            assert props["sessionAffinity"] == "HEADER_FIELD"

    def test_all_runtimes_use_same_header_field(
        self, runtime_config, container_config
    ):
        """IT-RUNTIME-05b: Verify all runtimes use same affinity header field."""
        expected_header = "x-rock-session-id"
        for config in [runtime_config, container_config]:
            props = config["resources"]["rock-rocklet"]["props"]
            assert props["sessionAffinityConfig"]["affinityHeaderFieldName"] == expected_header

    def test_all_runtimes_have_same_session_concurrency(
        self, runtime_config, container_config
    ):
        """IT-RUNTIME-05c: Verify all runtimes have same session concurrency."""
        expected_concurrency = 1
        for config in [runtime_config, container_config]:
            props = config["resources"]["rock-rocklet"]["props"]
            assert props["sessionAffinityConfig"]["sessionConcurrencyPerInstance"] == expected_concurrency

    def test_all_runtimes_have_same_idle_timeout(
        self, runtime_config, container_config
    ):
        """IT-RUNTIME-05d: Verify all runtimes have same idle timeout."""
        expected_timeout = 1800  # 30分钟
        for config in [runtime_config, container_config]:
            props = config["resources"]["rock-rocklet"]["props"]
            assert props["sessionAffinityConfig"]["sessionIdleTimeoutInSeconds"] == expected_timeout

    def test_all_runtimes_have_same_session_ttl(
        self, runtime_config, container_config
    ):
        """IT-RUNTIME-05e: Verify all runtimes have same session TTL."""
        expected_ttl = 86400  # 24小时
        for config in [runtime_config, container_config]:
            props = config["resources"]["rock-rocklet"]["props"]
            assert props["sessionAffinityConfig"]["sessionTTLInSeconds"] == expected_ttl

    def test_all_runtimes_use_session_exclusive_mode(
        self, runtime_config, container_config
    ):
        """IT-RUNTIME-05f: Verify all runtimes use SESSION_EXCLUSIVE mode."""
        expected_mode = "SESSION_EXCLUSIVE"
        for config in [runtime_config, container_config]:
            props = config["resources"]["rock-rocklet"]["props"]
            assert props["instanceIsolationMode"] == expected_mode


# ============================================================
# IT-RUNTIME-06: Resource Configuration Consistency
# ============================================================


class TestResourceConfigConsistency:
    """Integration tests for resource configuration consistency across runtimes.

    Purpose: Verify all runtimes have consistent resource configuration.
    """

    def test_all_runtimes_have_same_memory(
        self, runtime_config, container_config
    ):
        """IT-RUNTIME-06a: Verify all runtimes have same memory size."""
        expected_memory = 4096
        for config in [runtime_config, container_config]:
            props = config["resources"]["rock-rocklet"]["props"]
            assert props["memorySize"] == expected_memory

    def test_all_runtimes_have_same_cpu(
        self, runtime_config, container_config
    ):
        """IT-RUNTIME-06b: Verify all runtimes have same CPU."""
        expected_cpu = 2
        for config in [runtime_config, container_config]:
            props = config["resources"]["rock-rocklet"]["props"]
            assert props["cpu"] == expected_cpu

    def test_all_runtimes_have_same_timeout(
        self, runtime_config, container_config
    ):
        """IT-RUNTIME-06c: Verify all runtimes have same timeout."""
        expected_timeout = 3600
        for config in [runtime_config, container_config]:
            props = config["resources"]["rock-rocklet"]["props"]
            assert props["timeout"] == expected_timeout

    def test_all_runtimes_have_same_instance_concurrency(
        self, runtime_config, container_config
    ):
        """IT-RUNTIME-06d: Verify all runtimes have same instance concurrency."""
        expected_concurrency = 200
        for config in [runtime_config, container_config]:
            props = config["resources"]["rock-rocklet"]["props"]
            assert props["instanceConcurrency"] == expected_concurrency

    def test_all_runtimes_have_pythonunbuffered(
        self, runtime_config, container_config
    ):
        """IT-RUNTIME-06e: Verify all runtimes have PYTHONUNBUFFERED set."""
        for config in [runtime_config, container_config]:
            props = config["resources"]["rock-rocklet"]["props"]
            env = props["environmentVariables"]
            assert env.get("PYTHONUNBUFFERED") == "1"