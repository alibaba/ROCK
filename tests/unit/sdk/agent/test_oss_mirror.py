"""Tests for OssMirrorConfig integration into SDK.

Covers:
- OssMirrorConfig model fields and defaults
- EnvironmentConfig.oss_mirror field
- JobConfig top-level namespace/experiment_id fields
- to_harbor_yaml() serialization (namespace at top level)
- from_yaml() deserialization
- enable_oss_mirror() convenience method on JobConfig
"""

import yaml

from rock.sdk.agent.models.trial.config import EnvironmentConfig

# ---------------------------------------------------------------------------
# 1. OssMirrorConfig 模型
# ---------------------------------------------------------------------------


class TestOssMirrorConfig:
    def test_importable_from_trial_config(self):
        from rock.sdk.agent.models.trial.config import OssMirrorConfig

        assert OssMirrorConfig is not None

    def test_importable_from_agent_package(self):
        from rock.sdk.agent import OssMirrorConfig

        assert OssMirrorConfig is not None

    def test_default_is_disabled(self):
        from rock.sdk.agent.models.trial.config import OssMirrorConfig

        cfg = OssMirrorConfig()
        assert cfg.enabled is False
        assert cfg.oss_bucket is None
        assert cfg.oss_access_key_id is None
        assert cfg.oss_access_key_secret is None
        assert cfg.oss_region is None
        assert cfg.oss_endpoint is None

    def test_all_fields_settable(self):
        from rock.sdk.agent.models.trial.config import OssMirrorConfig

        cfg = OssMirrorConfig(
            enabled=True,
            oss_bucket="my-bucket",
            oss_access_key_id="ak-xxx",
            oss_access_key_secret="sk-xxx",
            oss_region="cn-hangzhou",
            oss_endpoint="oss-cn-hangzhou.aliyuncs.com",
        )
        assert cfg.enabled is True
        assert cfg.oss_bucket == "my-bucket"
        assert cfg.oss_access_key_id == "ak-xxx"
        assert cfg.oss_access_key_secret == "sk-xxx"
        assert cfg.oss_region == "cn-hangzhou"
        assert cfg.oss_endpoint == "oss-cn-hangzhou.aliyuncs.com"


# ---------------------------------------------------------------------------
# 2. EnvironmentConfig.oss_mirror 字段
# ---------------------------------------------------------------------------


class TestEnvironmentConfigOssMirror:
    def test_default_oss_mirror_is_none(self):
        env = EnvironmentConfig()
        assert env.oss_mirror is None

    def test_set_oss_mirror(self):
        from rock.sdk.agent.models.trial.config import OssMirrorConfig

        mirror = OssMirrorConfig(enabled=True, oss_bucket="b1", oss_region="r1")
        env = EnvironmentConfig(oss_mirror=mirror)
        assert env.oss_mirror.enabled is True
        assert env.oss_mirror.oss_bucket == "b1"

    def test_set_oss_mirror_from_dict(self):
        env = EnvironmentConfig(
            oss_mirror={
                "enabled": True,
                "oss_bucket": "b2",
            }
        )
        assert env.oss_mirror.enabled is True
        assert env.oss_mirror.oss_bucket == "b2"


# ---------------------------------------------------------------------------
# 3. JobConfig 顶层 namespace/experiment_id 字段
# ---------------------------------------------------------------------------


class TestJobConfigNamespaceFields:
    def test_default_namespace_is_none(self):
        from rock.sdk.agent.models.job.config import JobConfig

        cfg = JobConfig(job_name="test", experiment_id="test-exp")
        assert cfg.namespace is None

    def test_namespace_settable_at_top_level(self):
        from rock.sdk.agent.models.job.config import JobConfig

        cfg = JobConfig(job_name="test", namespace="team-rl", experiment_id="rl-step-42")
        assert cfg.namespace == "team-rl"
        assert cfg.experiment_id == "rl-step-42"


# ---------------------------------------------------------------------------
# 4. to_harbor_yaml() 序列化
# ---------------------------------------------------------------------------


class TestToHarborYamlOssMirror:
    def test_namespace_at_top_level_in_yaml(self):
        """namespace/experiment_id 序列化为 JobConfig 顶层字段。"""
        from rock.sdk.agent.models.job.config import JobConfig
        from rock.sdk.agent.models.trial.config import OssMirrorConfig, RockEnvironmentConfig

        cfg = JobConfig(
            job_name="mirror-test",
            namespace="my-ns",
            experiment_id="exp-1",
            environment=RockEnvironmentConfig(
                oss_mirror=OssMirrorConfig(
                    enabled=True,
                    oss_bucket="test-bucket",
                    oss_access_key_id="ak",
                    oss_access_key_secret="sk",
                    oss_region="cn-hangzhou",
                    oss_endpoint="oss-cn-hangzhou.aliyuncs.com",
                ),
            ),
        )
        data = yaml.safe_load(cfg.to_harbor_yaml())

        assert data["namespace"] == "my-ns"
        assert data["experiment_id"] == "exp-1"
        oss = data["environment"]["oss_mirror"]
        assert oss["enabled"] is True
        assert oss["oss_bucket"] == "test-bucket"
        assert "namespace" not in oss
        assert "experiment_id" not in oss

    def test_disabled_oss_mirror_excluded_from_yaml(self):
        """When oss_mirror is default (disabled), it should not clutter the YAML."""
        from rock.sdk.agent.models.job.config import JobConfig

        cfg = JobConfig(job_name="no-mirror", experiment_id="test-exp")
        data = yaml.safe_load(cfg.to_harbor_yaml())

        env_data = data.get("environment", {})
        assert "oss_mirror" not in env_data


# ---------------------------------------------------------------------------
# 5. from_yaml() 反序列化
# ---------------------------------------------------------------------------


class TestFromYamlOssMirror:
    def test_from_yaml_with_top_level_namespace(self, tmp_path):
        """新方式：namespace/experiment_id 在顶层。"""
        from rock.sdk.agent.models.job.config import JobConfig

        yaml_content = """\
job_name: loaded-mirror
namespace: yaml-ns
experiment_id: yaml-exp
environment:
  oss_mirror:
    enabled: true
    oss_bucket: yaml-bucket
    oss_region: ap-southeast-1
    oss_endpoint: oss-ap-southeast-1.aliyuncs.com
agents:
  - name: test-agent
"""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml_content)

        cfg = JobConfig.from_yaml(str(yaml_file))
        assert cfg.namespace == "yaml-ns"
        assert cfg.experiment_id == "yaml-exp"
        assert cfg.environment.oss_mirror.enabled is True
        assert cfg.environment.oss_mirror.oss_bucket == "yaml-bucket"

    def test_from_yaml_extra_keys_under_oss_mirror_ignored(self, tmp_path):
        """YAML 中 oss_mirror 内多余的 namespace 等字段由 Pydantic 忽略。"""
        from rock.sdk.agent.models.job.config import JobConfig

        yaml_content = """\
job_name: compat-mirror
experiment_id: test-exp
environment:
  oss_mirror:
    enabled: true
    oss_bucket: yaml-bucket
    namespace: legacy-ns
    experiment_id: legacy-exp
    oss_region: ap-southeast-1
    oss_endpoint: oss-ap-southeast-1.aliyuncs.com
agents:
  - name: test-agent
"""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml_content)

        cfg = JobConfig.from_yaml(str(yaml_file))
        assert cfg.environment.oss_mirror.enabled is True
        assert cfg.environment.oss_mirror.oss_bucket == "yaml-bucket"
        dump = cfg.environment.oss_mirror.model_dump(exclude_none=True)
        assert "namespace" not in dump
        assert "experiment_id" not in dump

    def test_from_yaml_without_oss_mirror(self, tmp_path):
        from rock.sdk.agent.models.job.config import JobConfig

        yaml_content = """\
job_name: no-mirror
experiment_id: test-exp
agents:
  - name: test-agent
"""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml_content)

        cfg = JobConfig.from_yaml(str(yaml_file))
        assert cfg.environment.oss_mirror is None


# ---------------------------------------------------------------------------
# 6. enable_oss_mirror() 便捷方法
# ---------------------------------------------------------------------------


class TestEnableOssMirror:
    def test_enable_with_all_params(self):
        from rock.sdk.agent.models.job.config import JobConfig

        cfg = JobConfig(job_name="conv-test", experiment_id="test-exp")
        cfg.enable_oss_mirror(
            oss_bucket="conv-bucket",
            oss_access_key_id="ak-conv",
            oss_access_key_secret="sk-conv",
            oss_region="cn-hangzhou",
            oss_endpoint="oss-cn-hangzhou.aliyuncs.com",
        )
        assert cfg.environment.oss_mirror.enabled is True
        assert cfg.environment.oss_mirror.oss_bucket == "conv-bucket"

    def test_does_not_touch_namespace_or_experiment_id(self):
        """enable_oss_mirror 不修改顶层 namespace / experiment_id。"""
        from rock.sdk.agent.models.job.config import JobConfig

        cfg = JobConfig(
            job_name="no-touch-test",
            namespace="preset-ns",
            experiment_id="preset-exp",
        )
        cfg.enable_oss_mirror(
            oss_bucket="b",
            oss_access_key_id="ak",
            oss_access_key_secret="sk",
            oss_region="r1",
            oss_endpoint="e1",
        )
        assert cfg.namespace == "preset-ns"
        assert cfg.experiment_id == "preset-exp"

    def test_enable_then_serialize_roundtrip(self):
        """to_harbor_yaml: 顶层 namespace / experiment_id 与 oss_mirror 独立设置。"""
        from rock.sdk.agent.models.job.config import JobConfig

        cfg = JobConfig(job_name="roundtrip", namespace="rt-ns", experiment_id="rt-exp")
        cfg.enable_oss_mirror(
            oss_bucket="rt-bucket",
            oss_access_key_id="ak-rt",
            oss_access_key_secret="sk-rt",
            oss_region="ap-southeast-1",
            oss_endpoint="oss-ap-southeast-1.aliyuncs.com",
        )
        data = yaml.safe_load(cfg.to_harbor_yaml())
        assert data["namespace"] == "rt-ns"
        assert data["experiment_id"] == "rt-exp"
        oss = data["environment"]["oss_mirror"]
        assert oss["enabled"] is True
        assert oss["oss_bucket"] == "rt-bucket"
        assert "namespace" not in oss
        assert "experiment_id" not in oss
