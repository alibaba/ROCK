import tempfile
import textwrap
from pathlib import Path

import pytest
import yaml

from rock.config import RockConfig, RuntimeConfig, _resolve_k8s_template_includes


@pytest.mark.asyncio
async def test_rock_config():
    rock_config: RockConfig = RockConfig.from_env()
    assert rock_config


@pytest.mark.asyncio
async def test_runtime_config():
    config = {
        "standard_spec": {
            "memory": "8g",
            "cpus": 2,
        },
    }
    runtime_config = RuntimeConfig(**config)

    assert runtime_config.max_allowed_spec.memory == "64g"
    assert runtime_config.max_allowed_spec.cpus == 16
    assert runtime_config.standard_spec.memory == "8g"
    assert runtime_config.standard_spec.cpus == 2

    config_full = {
        "standard_spec": {
            "memory": "8g",
            "cpus": 2,
        },
        "max_allowed_spec": {
            "memory": "32g",
            "cpus": 4,
        },
    }

    runtime_config = RuntimeConfig(**config_full)

    assert runtime_config.max_allowed_spec.memory == "32g"
    assert runtime_config.max_allowed_spec.cpus == 4
    assert runtime_config.standard_spec.memory == "8g"
    assert runtime_config.standard_spec.cpus == 2

    runtime_config = RuntimeConfig()

    assert runtime_config.max_allowed_spec.memory == "64g"
    assert runtime_config.max_allowed_spec.cpus == 16
    assert runtime_config.standard_spec.memory == "8g"
    assert runtime_config.standard_spec.cpus == 2


def test_oss_config_defaults():
    from rock.config import OssAccountConfig, OssConfig

    cfg = OssConfig()
    assert cfg.bucket == ""
    assert cfg.region == ""
    assert isinstance(cfg.primary, OssAccountConfig)
    assert cfg.primary.bucket == ""
    assert cfg.primary.region == ""
    # transfer_prefix moved to SandboxConfig.file_transfer.prefix; archive_prefix /
    # keep_days_before_archive / archive_max_attempts moved to SandboxConfig.log.
    # OssConfig is now purely OSS connectivity (endpoint / bucket / credentials).
    assert not hasattr(cfg, "transfer_prefix")
    assert not hasattr(cfg, "archive_prefix")
    assert not hasattr(cfg, "keep_days_before_archive")
    assert not hasattr(cfg, "archive_max_attempts")


def test_oss_config_primary_dict_coerced():
    from rock.config import OssAccountConfig, OssConfig

    cfg = OssConfig(
        primary={
            "endpoint": "e",
            "bucket": "chatos-rock",
            "access_key_id": "a",
            "access_key_secret": "s",
            "role_arn": "r",
            "region": "cn-hangzhou",
        }
    )
    assert isinstance(cfg.primary, OssAccountConfig)
    assert cfg.primary.bucket == "chatos-rock"
    assert cfg.primary.region == "cn-hangzhou"
    # legacy 顶层字段未提供时仍为默认空,确认 primary 不会污染 legacy
    assert cfg.bucket == ""


def test_sandbox_log_config_defaults():
    from rock.config import SandboxLogConfig

    cfg = SandboxLogConfig()
    # prefix defaults empty: each deployment YAML must opt-in to a value
    # matching its OSS bucket lifecycle rule (e.g. "rock-archives/").
    assert cfg.archive_prefix == ""
    assert cfg.keep_days_before_archive == 3
    assert cfg.archive_max_attempts == 3


def test_sandbox_log_config_overridable():
    from rock.config import SandboxLogConfig

    cfg = SandboxLogConfig(
        archive_prefix="custom-prefix/",
        keep_days_before_archive=1,
        archive_max_attempts=5,
    )
    assert cfg.archive_prefix == "custom-prefix/"
    assert cfg.keep_days_before_archive == 1
    assert cfg.archive_max_attempts == 5


def test_sandbox_file_transfer_config_defaults():
    from rock.config import SandboxFileTransferConfig

    cfg = SandboxFileTransferConfig()
    # prefix defaults empty; YAML opts in to "rock-transfer/" for the
    # primary bucket's lifecycle-managed transfer area.
    assert cfg.prefix == ""


def test_sandbox_config_nests_log_and_file_transfer():
    # The reorg puts log + file_transfer under SandboxConfig (not OssConfig
    # or root) since they're sandbox-domain concerns.
    from rock.config import SandboxConfig, SandboxFileTransferConfig, SandboxLogConfig

    cfg = SandboxConfig()
    assert isinstance(cfg.log, SandboxLogConfig)
    assert isinstance(cfg.file_transfer, SandboxFileTransferConfig)


def test_sandbox_config_coerces_nested_dicts_from_yaml():
    # yaml.safe_load returns dicts for nested keys; __post_init__ must coerce
    # them into the right dataclass so callers can keep dotted access.
    from rock.config import SandboxConfig, SandboxFileTransferConfig, SandboxLogConfig

    cfg = SandboxConfig(
        log={"archive_prefix": "rock-archives/", "keep_days_before_archive": 7},
        file_transfer={"prefix": "rock-transfer/"},
    )
    assert isinstance(cfg.log, SandboxLogConfig)
    assert cfg.log.archive_prefix == "rock-archives/"
    assert cfg.log.keep_days_before_archive == 7
    assert isinstance(cfg.file_transfer, SandboxFileTransferConfig)
    assert cfg.file_transfer.prefix == "rock-transfer/"


# ===== _resolve_k8s_template_includes =====


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data))


@pytest.mark.asyncio
async def test_resolve_includes_noop_without_includes():
    """No template_includes key → templates passed through unchanged."""
    k8s = {"templates": {"default": {"a": 1}}}
    _resolve_k8s_template_includes(k8s, Path("/unused"))
    assert k8s == {"templates": {"default": {"a": 1}}}


@pytest.mark.asyncio
async def test_resolve_includes_single_file_relative():
    """Single relative-path include populates templates."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        inc_dir = base / "templates" / "k8s"
        inc_dir.mkdir(parents=True)
        _write_yaml(inc_dir / "default.yml", {"default": {"ports": {"proxy": 8000}}})
        k8s = {"template_includes": ["templates/k8s/default.yml"]}
        _resolve_k8s_template_includes(k8s, base)
        assert "template_includes" not in k8s
        assert k8s["templates"] == {"default": {"ports": {"proxy": 8000}}}


@pytest.mark.asyncio
async def test_resolve_includes_multiple_later_wins():
    """Conflicts across includes resolved by declaration order — later wins."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_yaml(base / "a.yml", {"shared": {"v": 1}, "only_a": {"x": "a"}})
        _write_yaml(base / "b.yml", {"shared": {"v": 2}, "only_b": {"x": "b"}})
        k8s = {"template_includes": ["a.yml", "b.yml"]}
        _resolve_k8s_template_includes(k8s, base)
        assert k8s["templates"]["shared"] == {"v": 2}
        assert k8s["templates"]["only_a"] == {"x": "a"}
        assert k8s["templates"]["only_b"] == {"x": "b"}


@pytest.mark.asyncio
async def test_resolve_includes_inline_overrides_include():
    """Inline templates entry replaces the include version wholesale."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_yaml(base / "base.yml", {"default": {"from": "include"}})
        k8s = {
            "template_includes": ["base.yml"],
            "templates": {"default": {"from": "inline"}},
        }
        _resolve_k8s_template_includes(k8s, base)
        assert k8s["templates"] == {"default": {"from": "inline"}}


@pytest.mark.asyncio
async def test_resolve_includes_absolute_path():
    """Absolute paths bypass base_dir resolution."""
    with tempfile.TemporaryDirectory() as tmp:
        abs_file = Path(tmp) / "abs.yml"
        _write_yaml(abs_file, {"default": {"k": "v"}})
        k8s = {"template_includes": [str(abs_file)]}
        _resolve_k8s_template_includes(k8s, Path("/nonexistent"))
        assert k8s["templates"] == {"default": {"k": "v"}}


@pytest.mark.asyncio
async def test_resolve_includes_missing_file_raises():
    """Missing include path surfaces a clear FileNotFoundError."""
    with tempfile.TemporaryDirectory() as tmp:
        k8s = {"template_includes": ["does-not-exist.yml"]}
        with pytest.raises(FileNotFoundError, match="does-not-exist.yml"):
            _resolve_k8s_template_includes(k8s, Path(tmp))


@pytest.mark.asyncio
async def test_resolve_includes_non_mapping_raises():
    """A list-shaped include yaml is rejected."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "bad.yml").write_text("- not_a_mapping\n")
        k8s = {"template_includes": ["bad.yml"]}
        with pytest.raises(ValueError, match="must be a mapping"):
            _resolve_k8s_template_includes(k8s, base)


# ---------------------------------------------------------------------------
# Unit tests for RockConfig._deep_merge
# ---------------------------------------------------------------------------


class TestDeepMerge:
    """Tests for RockConfig._deep_merge static method."""

    def test_disjoint_keys(self):
        """Non-overlapping keys are all preserved."""
        base = {"a": 1, "b": 2}
        override = {"c": 3}
        result = RockConfig._deep_merge(base, override)
        assert result == {"a": 1, "b": 2, "c": 3}

    def test_override_scalar(self):
        """Override value replaces base for scalar keys."""
        base = {"a": 1, "b": 2}
        override = {"b": 99}
        result = RockConfig._deep_merge(base, override)
        assert result == {"a": 1, "b": 99}

    def test_nested_dict_merge(self):
        """Nested dicts are recursively merged."""
        base = {"x": {"a": 1, "b": 2}}
        override = {"x": {"b": 20, "c": 30}}
        result = RockConfig._deep_merge(base, override)
        assert result == {"x": {"a": 1, "b": 20, "c": 30}}

    def test_deeply_nested_merge(self):
        """Three-level nested dicts merge correctly."""
        base = {"l1": {"l2": {"a": 1, "b": 2}}}
        override = {"l1": {"l2": {"b": 99, "c": 3}}}
        result = RockConfig._deep_merge(base, override)
        assert result == {"l1": {"l2": {"a": 1, "b": 99, "c": 3}}}

    def test_override_dict_with_scalar(self):
        """Override a dict value with a scalar replaces entirely."""
        base = {"a": {"nested": 1}}
        override = {"a": "flat"}
        result = RockConfig._deep_merge(base, override)
        assert result == {"a": "flat"}

    def test_override_scalar_with_dict(self):
        """Override a scalar with a dict replaces entirely."""
        base = {"a": "flat"}
        override = {"a": {"nested": 1}}
        result = RockConfig._deep_merge(base, override)
        assert result == {"a": {"nested": 1}}

    def test_empty_base(self):
        base = {}
        override = {"a": 1}
        assert RockConfig._deep_merge(base, override) == {"a": 1}

    def test_empty_override(self):
        base = {"a": 1}
        override = {}
        assert RockConfig._deep_merge(base, override) == {"a": 1}

    def test_both_empty(self):
        assert RockConfig._deep_merge({}, {}) == {}

    def test_base_not_mutated(self):
        """_deep_merge must not mutate the base dict."""
        base = {"a": {"b": 1}}
        override = {"a": {"b": 2}}
        RockConfig._deep_merge(base, override)
        assert base == {"a": {"b": 1}}

    def test_list_values_delegate_to_merge_lists(self):
        """When both values are lists, _merge_lists is invoked."""
        base = {"items": [1, 2]}
        override = {"items": [3, 4]}
        result = RockConfig._deep_merge(base, override)
        # No merge key → override replaces base
        assert result == {"items": [3, 4]}


# ---------------------------------------------------------------------------
# Unit tests for RockConfig._merge_lists
# ---------------------------------------------------------------------------


class TestMergeLists:
    """Tests for RockConfig._merge_lists static method."""

    # --- edge cases: empty inputs ---

    def test_empty_base_returns_override(self):
        assert RockConfig._merge_lists([], [{"a": 1}]) == [{"a": 1}]

    def test_empty_override_returns_base(self):
        assert RockConfig._merge_lists([{"a": 1}], []) == [{"a": 1}]

    def test_both_empty(self):
        assert RockConfig._merge_lists([], []) == []

    # --- no merge key: override replaces ---

    def test_no_merge_key_plain_values(self):
        """Lists of non-dicts → override replaces base."""
        assert RockConfig._merge_lists([1, 2], [3, 4]) == [3, 4]

    def test_no_common_key_in_dicts(self):
        """Dicts without a shared identity key → override replaces base."""
        base = [{"foo": 1}]
        override = [{"bar": 2}]
        assert RockConfig._merge_lists(base, override) == [{"bar": 2}]

    # --- merge by task_class ---

    def test_merge_by_task_class_override(self):
        """Matched items are deep-merged by task_class."""
        base = [
            {"task_class": "cleanup", "interval": 60, "enabled": True},
            {"task_class": "report", "interval": 300},
        ]
        override = [
            {"task_class": "cleanup", "interval": 120},
        ]
        result = RockConfig._merge_lists(base, override)
        assert len(result) == 2
        assert result[0] == {"task_class": "cleanup", "interval": 120, "enabled": True}
        assert result[1] == {"task_class": "report", "interval": 300}

    def test_merge_by_task_class_append_new(self):
        """New items in override are appended."""
        base = [{"task_class": "cleanup", "interval": 60}]
        override = [
            {"task_class": "cleanup", "interval": 120},
            {"task_class": "audit", "interval": 600},
        ]
        result = RockConfig._merge_lists(base, override)
        assert len(result) == 2
        assert result[0]["task_class"] == "cleanup"
        assert result[0]["interval"] == 120
        assert result[1] == {"task_class": "audit", "interval": 600}

    def test_merge_by_task_class_disable(self):
        """Override can disable a base task via enabled: false."""
        base = [{"task_class": "cleanup", "interval": 60, "enabled": True}]
        override = [{"task_class": "cleanup", "enabled": False}]
        result = RockConfig._merge_lists(base, override)
        assert result[0]["enabled"] is False
        assert result[0]["interval"] == 60  # preserved from base

    # --- merge by name ---

    def test_merge_by_name(self):
        base = [{"name": "svc-a", "port": 80}]
        override = [{"name": "svc-a", "port": 8080}]
        result = RockConfig._merge_lists(base, override)
        assert result == [{"name": "svc-a", "port": 8080}]

    # --- merge by id ---

    def test_merge_by_id(self):
        base = [{"id": "x1", "value": 10}]
        override = [{"id": "x1", "value": 20}]
        result = RockConfig._merge_lists(base, override)
        assert result == [{"id": "x1", "value": 20}]

    # --- key priority: task_class > name > id ---

    def test_merge_key_priority_task_class_over_name(self):
        """When both task_class and name exist, task_class is used."""
        base = [{"task_class": "A", "name": "na", "v": 1}]
        override = [{"task_class": "A", "name": "nb", "v": 2}]
        result = RockConfig._merge_lists(base, override)
        assert result[0]["v"] == 2
        assert result[0]["name"] == "nb"  # overridden

    # --- nested deep merge within list items ---

    def test_nested_dict_merge_within_list_item(self):
        """Dict values inside matched list items are recursively merged."""
        base = [{"task_class": "t1", "params": {"a": 1, "b": 2}}]
        override = [{"task_class": "t1", "params": {"b": 20, "c": 30}}]
        result = RockConfig._merge_lists(base, override)
        assert result[0]["params"] == {"a": 1, "b": 20, "c": 30}

    # --- preserving order ---

    def test_order_preserved(self):
        """Base order is preserved; new override items appended at end."""
        base = [
            {"task_class": "B", "v": 1},
            {"task_class": "A", "v": 2},
        ]
        override = [
            {"task_class": "C", "v": 3},
            {"task_class": "A", "v": 22},
        ]
        result = RockConfig._merge_lists(base, override)
        assert [item["task_class"] for item in result] == ["B", "A", "C"]
        assert result[1]["v"] == 22


# ---------------------------------------------------------------------------
# Integration test: from_env with _base inheritance
# ---------------------------------------------------------------------------


class TestFromEnvBaseInheritance:
    """Test RockConfig.from_env _base config inheritance using temp files."""

    def test_base_inheritance_deep_merges(self, tmp_path: Path):
        """Child config inherits and overrides base via _deep_merge."""
        base_file = tmp_path / "base.yml"
        base_file.write_text(
            textwrap.dedent("""\
            ray:
              namespace: "base-ns"
              runtime_env:
                working_dir: ./
            warmup:
              images:
                - "python:3.11"
        """)
        )

        child_file = tmp_path / "child.yml"
        child_file.write_text(
            textwrap.dedent("""\
            _base: base.yml
            ray:
              namespace: "child-ns"
        """)
        )

        config = RockConfig.from_env(config_path=str(child_file))
        # Overridden
        assert config.ray.namespace == "child-ns"
        # Inherited from base (runtime_env is deep-merged: child has no runtime_env so base is kept)
        assert config.ray.runtime_env == {"working_dir": "./"}
        assert config.warmup.images == ["python:3.11"]

    def test_base_inheritance_scheduler_tasks_merge(self, tmp_path: Path):
        """Scheduler tasks list is merged by task_class key."""
        base_file = tmp_path / "base.yml"
        base_file.write_text(
            textwrap.dedent("""\
            scheduler:
              tasks:
                - task_class: "rock.admin.scheduler.tasks.cleanup.CleanupTask"
                  enabled: true
                  interval_seconds: 60
                - task_class: "rock.admin.scheduler.tasks.report.ReportTask"
                  enabled: true
                  interval_seconds: 300
        """)
        )

        child_file = tmp_path / "child.yml"
        child_file.write_text(
            textwrap.dedent("""\
            _base: base.yml
            scheduler:
              tasks:
                - task_class: "rock.admin.scheduler.tasks.cleanup.CleanupTask"
                  interval_seconds: 120
                - task_class: "rock.admin.scheduler.tasks.audit.AuditTask"
                  enabled: true
                  interval_seconds: 600
        """)
        )

        config = RockConfig.from_env(config_path=str(child_file))
        tasks = config.scheduler.tasks
        task_map = {t.task_class: t for t in tasks}

        # Overridden interval
        assert task_map["rock.admin.scheduler.tasks.cleanup.CleanupTask"].interval_seconds == 120
        assert task_map["rock.admin.scheduler.tasks.cleanup.CleanupTask"].enabled is True  # inherited
        # Preserved from base
        assert task_map["rock.admin.scheduler.tasks.report.ReportTask"].interval_seconds == 300
        # Newly appended
        assert task_map["rock.admin.scheduler.tasks.audit.AuditTask"].interval_seconds == 600

    def test_base_not_found_raises(self, tmp_path: Path):
        child_file = tmp_path / "child.yml"
        child_file.write_text(
            textwrap.dedent("""\
            _base: nonexistent.yml
            ray:
              namespace: "ns"
        """)
        )
        with pytest.raises(Exception, match="base config file.*not found"):
            RockConfig.from_env(config_path=str(child_file))
