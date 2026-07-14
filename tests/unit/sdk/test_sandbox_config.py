import pytest
from pydantic import ValidationError

from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig


class TestSandboxConfigAutoDeleteSeconds:
    def test_default_is_none(self):
        config = SandboxConfig()
        assert config.auto_delete_seconds is None

    def test_none_is_valid(self):
        config = SandboxConfig(auto_delete_seconds=None)
        assert config.auto_delete_seconds is None

    def test_zero_is_valid(self):
        config = SandboxConfig(auto_delete_seconds=0)
        assert config.auto_delete_seconds == 0

    def test_positive_value_is_valid(self):
        config = SandboxConfig(auto_delete_seconds=300)
        assert config.auto_delete_seconds == 300

    def test_negative_value_raises_error(self):
        with pytest.raises(ValidationError, match="auto_delete_seconds must be >= 0"):
            SandboxConfig(auto_delete_seconds=-1)

    def test_large_negative_value_raises_error(self):
        with pytest.raises(ValidationError, match="auto_delete_seconds must be >= 0"):
            SandboxConfig(auto_delete_seconds=-100)


class TestSandboxConfigAutoArchiveSeconds:
    def test_default_is_none(self):
        config = SandboxConfig()
        assert config.auto_archive_seconds is None

    def test_none_is_valid(self):
        config = SandboxConfig(auto_archive_seconds=None)
        assert config.auto_archive_seconds is None

    def test_zero_is_valid(self):
        config = SandboxConfig(auto_archive_seconds=0)
        assert config.auto_archive_seconds == 0

    def test_positive_value_is_valid(self):
        config = SandboxConfig(auto_archive_seconds=300)
        assert config.auto_archive_seconds == 300

    def test_negative_value_raises_error(self):
        with pytest.raises(ValidationError, match="auto_archive_seconds must be >= 0"):
            SandboxConfig(auto_archive_seconds=-1)

    def test_cannot_specify_auto_archive_and_auto_delete_together(self):
        with pytest.raises(
            ValidationError,
            match="auto_archive_seconds and auto_delete_seconds cannot be specified together",
        ):
            SandboxConfig(auto_archive_seconds=300, auto_delete_seconds=600)


class TestSandboxCluster:
    def test_sandbox_cluster(self):
        fake_auth_token = "fake_auth_token"
        config = SandboxConfig(
            image="python:3.11",
            xrl_authorization=fake_auth_token,
            cluster="sg",
        )
        sandbox = Sandbox(config)
        assert sandbox.cluster == "sg"
        common_headers = sandbox._build_headers()
        assert common_headers["X-Cluster"] == "sg"
        assert common_headers["XRL-Authorization"] == f"Bearer {fake_auth_token}"

        # default cluster is vpc-nt-a
        config = SandboxConfig(
            image="python:3.11",
            xrl_authorization=fake_auth_token,
        )

        sandbox = Sandbox(config)
        assert sandbox.cluster == "vpc-nt-a"
        common_headers = sandbox._build_headers()
        assert common_headers["X-Cluster"] == "vpc-nt-a"
