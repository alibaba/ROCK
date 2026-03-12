"""
Unit tests for SandboxStartRequest parameter passthrough.

Verifies that all fields in SandboxStartRequest — especially image_os —
are correctly propagated through the full chain:
  SandboxConfig (SDK) → HTTP request data → SandboxStartRequest → DockerDeploymentConfig
"""

from rock.admin.proto.request import SandboxStartRequest
from rock.deployments.config import DockerDeploymentConfig
from rock.sdk.sandbox.config import SandboxConfig


def test_image_os_default_value():
    """SandboxStartRequest should default image_os to 'linux'."""
    request = SandboxStartRequest()
    assert request.image_os == "linux"


def test_image_os_custom_value():
    """SandboxStartRequest should accept a custom image_os value."""
    request = SandboxStartRequest(image_os="windows")
    assert request.image_os == "windows"


def test_image_os_propagates_to_docker_deployment_config():
    """image_os in SandboxStartRequest must be forwarded to DockerDeploymentConfig."""
    request = SandboxStartRequest(image="ubuntu:22.04", image_os="windows")
    config = DockerDeploymentConfig.from_request(request)
    assert config.image_os == "windows"


def test_image_os_default_propagates_to_docker_deployment_config():
    """Default image_os ('linux') must be forwarded to DockerDeploymentConfig."""
    request = SandboxStartRequest(image="ubuntu:22.04")
    config = DockerDeploymentConfig.from_request(request)
    assert config.image_os == "linux"


def test_all_fields_propagate_from_request_to_docker_config():
    """Every field set on SandboxStartRequest must appear unchanged in DockerDeploymentConfig."""
    request = SandboxStartRequest(
        image="my-registry/my-image:latest",
        image_os="windows",
        pull="always",
        memory="16g",
        cpus=4.0,
        limit_cpus=2.0,
        sandbox_id="test-sandbox-001",
        registry_username="user",
        registry_password="secret",
        use_kata_runtime=True,
    )
    config = DockerDeploymentConfig.from_request(request)

    assert config.image == "my-registry/my-image:latest"
    assert config.image_os == "windows"
    assert config.pull == "always"
    assert config.memory == "16g"
    assert config.cpus == 4.0
    assert config.limit_cpus == 2.0
    assert config.container_name == "test-sandbox-001"
    assert config.registry_username == "user"
    assert config.registry_password == "secret"
    assert config.use_kata_runtime is True


def test_sandbox_id_becomes_container_name():
    """sandbox_id from SandboxStartRequest should map to container_name in DockerDeploymentConfig."""
    request = SandboxStartRequest(sandbox_id="my-sandbox")
    config = DockerDeploymentConfig.from_request(request)
    assert config.container_name == "my-sandbox"


def test_none_sandbox_id_yields_none_container_name():
    """When sandbox_id is None, container_name in DockerDeploymentConfig should also be None."""
    request = SandboxStartRequest(sandbox_id=None)
    config = DockerDeploymentConfig.from_request(request)
    assert config.container_name is None


def test_image_os_default_matches_between_sdk_config_and_start_request():
    """SandboxConfig and SandboxStartRequest must share the same default value for image_os."""
    sdk_config = SandboxConfig()
    start_request = SandboxStartRequest()
    assert sdk_config.image_os == start_request.image_os


def test_sdk_config_image_os_survives_full_chain():
    """
    Simulate the SDK start() flow:
        SandboxConfig.image_os → data dict → SandboxStartRequest → DockerDeploymentConfig
    """
    sdk_config = SandboxConfig(image="python:3.11", image_os="windows")

    # Mimic what Sandbox.start() puts into the HTTP request body
    http_request_data = {
        "image": sdk_config.image,
        "image_os": sdk_config.image_os,
        "memory": sdk_config.memory,
        "cpus": sdk_config.cpus,
        "limit_cpus": sdk_config.limit_cpus,
        "registry_username": sdk_config.registry_username,
        "registry_password": sdk_config.registry_password,
        "use_kata_runtime": sdk_config.use_kata_runtime,
        "sandbox_id": sdk_config.sandbox_id,
    }

    # SandboxStartRequest deserialises the HTTP body
    start_request = SandboxStartRequest(**http_request_data)
    assert start_request.image_os == "windows"

    # DockerDeploymentConfig is built from the request
    docker_config = DockerDeploymentConfig.from_request(start_request)
    assert docker_config.image_os == "windows"
