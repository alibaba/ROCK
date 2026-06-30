"""Unit tests for FCOperatorConfig.

Verifies config merge semantics:
- W14: FCOperatorConfig should inherit DeploymentConfig (type contract)
- merge_with_fc_config: request overrides, fc_config defaults fill None fields
- access_key_secret empty-string boundary
"""

import pytest

from rock.deployments.config import DeploymentConfig
from rock.sandbox.operator.fc.config import FCOperatorConfig


class TestFCOperatorConfigContract:
    def test_type_discriminator_is_fc(self):
        config = FCOperatorConfig()
        assert config.type == "fc"

    @pytest.mark.xfail(reason="W14: FCOperatorConfig does not inherit DeploymentConfig")
    def test_is_deployment_config_subtype(self):
        config = FCOperatorConfig()
        assert isinstance(config, DeploymentConfig), (
            "FCOperatorConfig must be a DeploymentConfig subtype to satisfy SandboxManager / "
            "DeploymentManager type contracts"
        )


class TestMergeWithFCConfig:
    def test_request_overrides_fc_config_defaults(self, fc_config):
        config = FCOperatorConfig(
            function_name="req-func",
            region="cn-shanghai",
            memory=8192,
            cpus=4.0,
        )
        merged = config.merge_with_fc_config(fc_config)

        assert merged.function_name == "req-func"
        assert merged.region == "cn-shanghai"
        assert merged.memory == 8192
        assert merged.cpus == 4.0

    def test_fc_config_defaults_fill_none_fields(self, fc_config):
        config = FCOperatorConfig()
        merged = config.merge_with_fc_config(fc_config)

        assert merged.function_name == fc_config.function_name
        assert merged.region == fc_config.region
        assert merged.account_id == fc_config.account_id
        assert merged.memory == fc_config.default_memory
        assert merged.cpus == fc_config.default_cpus
        assert merged.session_ttl == fc_config.default_session_ttl

    def test_image_is_not_defaulted_from_fc_config(self, fc_config):
        config = FCOperatorConfig()
        merged = config.merge_with_fc_config(fc_config)
        assert merged.image is None, "image is sandbox-specific and must not be defaulted"

    def test_merge_returns_new_object(self, fc_config, fc_operator_config):
        merged = fc_operator_config.merge_with_fc_config(fc_config)
        assert merged is not fc_operator_config

    def test_access_key_secret_empty_string_falls_back_to_fc_config(self, fc_config):
        """Empty-string secret is falsy and should fall back to FCConfig default."""
        config = FCOperatorConfig(access_key_secret="")
        merged = config.merge_with_fc_config(fc_config)
        assert merged.access_key_secret == fc_config.access_key_secret

    def test_env_is_passed_through_merge(self, fc_config):
        """env field should be preserved through merge."""
        config = FCOperatorConfig(env={"FOO": "bar"})
        merged = config.merge_with_fc_config(fc_config)
        assert merged.env == {"FOO": "bar"}


class TestTemplateHash:
    """Tests for FCOperatorConfig.template_hash() method."""

    def test_same_config_produces_same_hash(self):
        """Identical configs should produce identical hashes."""
        c1 = FCOperatorConfig(
            image="registry.cn-hangzhou.aliyuncs.com/rock/test:latest",
            memory=4096,
            cpus=2.0,
            session_ttl=86400,
            session_idle_timeout=1800,
            function_timeout=3600.0,
            env={"FOO": "bar"},
        )
        c2 = FCOperatorConfig(
            image="registry.cn-hangzhou.aliyuncs.com/rock/test:latest",
            memory=4096,
            cpus=2.0,
            session_ttl=86400,
            session_idle_timeout=1800,
            function_timeout=3600.0,
            env={"FOO": "bar"},
        )
        assert c1.template_hash() == c2.template_hash()

    def test_different_image_produces_different_hash(self):
        """Different image should produce different hash."""
        c1 = FCOperatorConfig(image="img:v1", memory=4096, cpus=2.0)
        c2 = FCOperatorConfig(image="img:v2", memory=4096, cpus=2.0)
        assert c1.template_hash() != c2.template_hash()

    def test_different_memory_produces_different_hash(self):
        """Different memory should produce different hash."""
        c1 = FCOperatorConfig(image="img:latest", memory=4096, cpus=2.0)
        c2 = FCOperatorConfig(image="img:latest", memory=8192, cpus=2.0)
        assert c1.template_hash() != c2.template_hash()

    def test_different_cpus_produces_different_hash(self):
        """Different cpus should produce different hash."""
        c1 = FCOperatorConfig(image="img:latest", memory=4096, cpus=2.0)
        c2 = FCOperatorConfig(image="img:latest", memory=4096, cpus=4.0)
        assert c1.template_hash() != c2.template_hash()

    def test_different_env_produces_different_hash(self):
        """Different env should produce different hash."""
        c1 = FCOperatorConfig(image="img:latest", memory=4096, cpus=2.0, env={"A": "1"})
        c2 = FCOperatorConfig(image="img:latest", memory=4096, cpus=2.0, env={"A": "2"})
        assert c1.template_hash() != c2.template_hash()

    def test_different_session_ttl_produces_different_hash(self):
        """Different session_ttl should produce different hash."""
        c1 = FCOperatorConfig(image="img:latest", memory=4096, cpus=2.0, session_ttl=3600)
        c2 = FCOperatorConfig(image="img:latest", memory=4096, cpus=2.0, session_ttl=7200)
        assert c1.template_hash() != c2.template_hash()

    def test_different_function_timeout_produces_different_hash(self):
        """Different function_timeout should produce different hash."""
        c1 = FCOperatorConfig(image="img:latest", memory=4096, cpus=2.0, function_timeout=60)
        c2 = FCOperatorConfig(image="img:latest", memory=4096, cpus=2.0, function_timeout=120)
        assert c1.template_hash() != c2.template_hash()

    def test_different_session_idle_timeout_produces_different_hash(self):
        """Different session_idle_timeout should produce different hash."""
        c1 = FCOperatorConfig(image="img:latest", memory=4096, cpus=2.0, session_idle_timeout=300)
        c2 = FCOperatorConfig(image="img:latest", memory=4096, cpus=2.0, session_idle_timeout=600)
        assert c1.template_hash() != c2.template_hash()

    def test_connection_settings_not_in_hash(self):
        """Region, credentials, function_name should NOT affect hash."""
        c1 = FCOperatorConfig(
            image="img:latest", memory=4096, cpus=2.0,
            region="cn-hangzhou", function_name="func-a",
            access_key_id="ak1", access_key_secret="sk1",
        )
        c2 = FCOperatorConfig(
            image="img:latest", memory=4096, cpus=2.0,
            region="cn-shanghai", function_name="func-b",
            access_key_id="ak2", access_key_secret="sk2",
        )
        assert c1.template_hash() == c2.template_hash()

    def test_hash_is_16_chars(self):
        """Hash should be 16 hex characters."""
        config = FCOperatorConfig(image="img:latest")
        h = config.template_hash()
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)
