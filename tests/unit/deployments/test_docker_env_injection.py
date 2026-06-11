"""Unit tests for DockerDeployment._build_env_args.

Covers the env-arg list assembled for `docker run -e ...`, focusing on the
INSTANCE_ROCK_REGISTRY injection sourced from RuntimeConfig.instance_registry_mirrors.
Existing entries (ROCK_LOGGING_*, ROCK_TIME_ZONE, ROCK_KATA_RUNTIME) are regression-checked.
"""

from unittest.mock import patch

from rock.config import RuntimeConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.deployments.docker import DockerDeployment


def _make_deployment(**config_kwargs) -> DockerDeployment:
    with patch("rock.deployments.docker.DockerSandboxValidator"):
        return DockerDeployment.from_config(DockerDeploymentConfig(**config_kwargs))


class TestBuildEnvArgsInstanceRegistry:
    def test_no_mirrors_means_no_registry_env(self):
        deployment = _make_deployment()
        with patch("rock.deployments.docker.env_vars") as mock_env:
            mock_env.ROCK_LOGGING_PATH = ""
            mock_env.ROCK_TIME_ZONE = "UTC"
            args = deployment._build_env_args()
        assert "INSTANCE_ROCK_REGISTRY" not in " ".join(args)

    def test_single_mirror_emitted_unquoted(self):
        deployment = _make_deployment(
            runtime_config=RuntimeConfig(instance_registry_mirrors=["reg-a.example.com/ns"]),
        )
        with patch("rock.deployments.docker.env_vars") as mock_env:
            mock_env.ROCK_LOGGING_PATH = ""
            mock_env.ROCK_TIME_ZONE = "UTC"
            args = deployment._build_env_args()
        assert "-e" in args
        assert "INSTANCE_ROCK_REGISTRY=reg-a.example.com/ns" in args

    def test_multiple_mirrors_comma_joined(self):
        deployment = _make_deployment(
            runtime_config=RuntimeConfig(
                instance_registry_mirrors=[
                    "reg-a.example.com/ns-1",
                    "reg-b.example.com/ns-2",
                ],
            ),
        )
        with patch("rock.deployments.docker.env_vars") as mock_env:
            mock_env.ROCK_LOGGING_PATH = ""
            mock_env.ROCK_TIME_ZONE = "UTC"
            args = deployment._build_env_args()
        assert "INSTANCE_ROCK_REGISTRY=reg-a.example.com/ns-1,reg-b.example.com/ns-2" in args


class TestBuildEnvArgsRegression:
    """The existing env entries must keep working after the extraction."""

    def test_timezone_always_present(self):
        deployment = _make_deployment()
        with patch("rock.deployments.docker.env_vars") as mock_env:
            mock_env.ROCK_LOGGING_PATH = ""
            mock_env.ROCK_TIME_ZONE = "Asia/Shanghai"
            args = deployment._build_env_args()
        assert "ROCK_TIME_ZONE=Asia/Shanghai" in args

    def test_logging_entries_present_when_logging_path_set(self):
        deployment = _make_deployment()
        with patch("rock.deployments.docker.env_vars") as mock_env:
            mock_env.ROCK_LOGGING_PATH = "/var/log/rock"
            mock_env.ROCK_LOGGING_LEVEL = "INFO"
            mock_env.ROCK_TIME_ZONE = "UTC"
            args = deployment._build_env_args()
        assert "ROCK_LOGGING_PATH=/var/log/rock" in args
        assert "ROCK_LOGGING_LEVEL=INFO" in args

    def test_logging_entries_absent_when_logging_path_empty(self):
        deployment = _make_deployment()
        with patch("rock.deployments.docker.env_vars") as mock_env:
            mock_env.ROCK_LOGGING_PATH = ""
            mock_env.ROCK_TIME_ZONE = "UTC"
            args = deployment._build_env_args()
        joined = " ".join(args)
        assert "ROCK_LOGGING_PATH" not in joined
        assert "ROCK_LOGGING_LEVEL" not in joined

    def test_kata_runtime_env_present_when_enabled(self):
        deployment = _make_deployment(use_kata_runtime=True)
        with patch("rock.deployments.docker.env_vars") as mock_env:
            mock_env.ROCK_LOGGING_PATH = ""
            mock_env.ROCK_TIME_ZONE = "UTC"
            args = deployment._build_env_args()
        assert "ROCK_KATA_RUNTIME=true" in args

    def test_kata_runtime_env_absent_when_disabled(self):
        deployment = _make_deployment(use_kata_runtime=False)
        with patch("rock.deployments.docker.env_vars") as mock_env:
            mock_env.ROCK_LOGGING_PATH = ""
            mock_env.ROCK_TIME_ZONE = "UTC"
            args = deployment._build_env_args()
        assert "ROCK_KATA_RUNTIME=true" not in args
