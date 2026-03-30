# JobConfig 环境配置重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `JobConfig` 中散落的 Rock 扩展字段（`sandbox_config`、`sandbox_env`、`setup_commands` 等）统一收进单一 `environment: RockEnvironmentConfig` 字段，消除用户需要同时理解 Harbor 和 Rock 两套环境概念的困惑。

**Architecture:** 在 `rock/sdk/agent/models/job/config.py` 中新增 `RockEnvironmentConfig(SandboxConfig, _HarborEnvConfig)` 多重继承类，平铺所有字段。序列化时通过 `_HarborEnvConfig.model_validate()` 向上转型自动过滤 Rock 字段。`trial/config.py` 和 `sandbox/config.py` 零改动。

**Tech Stack:** Python 3.10+, Pydantic v2, pytest, uv

---

## 文件结构

| 文件 | 操作 | 职责 |
|------|------|------|
| `rock/sdk/agent/models/job/config.py` | **修改** | 新增 `RockEnvironmentConfig`，删除旧扩展字段，更新 `JobConfig`、`to_harbor_yaml()`、`from_yaml()` |
| `rock/sdk/agent/models/job/__init__.py` | **修改** | 导出 `RockEnvironmentConfig` |
| `rock/sdk/agent/models/__init__.py` | **修改** | `RockEnvironmentConfig` 改从 `job.config` 导入 |
| `rock/sdk/agent/__init__.py` | **修改** | 同上 |
| `rock/sdk/agent/job.py` | **修改** | 更新字段引用 |
| `tests/unit/sdk/agent/test_job_config_serialization.py` | **修改** | 更新 fixture，新增 `RockEnvironmentConfig` 测试 |
| `tests/unit/sdk/agent/test_models.py` | **修改** | 更新 `TestJobConfig` |
| `tests/unit/sdk/agent/test_job.py` | **修改** | 更新字段引用 |
| `examples/harbor/swe_job_config.yaml.template` | **修改** | 新结构 |
| `examples/harbor/tb_job_config.yaml.template` | **修改** | 新结构 |
| `docs/dev/agent/README.md` | **修改** | 更新类图和使用示例 |

---

## Task 1: 新增 `RockEnvironmentConfig` 并更新 `JobConfig`（TDD）

**Files:**
- Modify: `tests/unit/sdk/agent/test_job_config_serialization.py`
- Modify: `rock/sdk/agent/models/job/config.py`

- [ ] **Step 1: 将测试文件完整替换为新版**

```python
# tests/unit/sdk/agent/test_job_config_serialization.py
from pathlib import Path

import yaml

from rock.sdk.agent.models.job.config import (
    JobConfig,
    LocalDatasetConfig,
    RegistryDatasetConfig,
    RemoteRegistryInfo,
    RockEnvironmentConfig,
)
from rock.sdk.agent.models.metric.config import MetricConfig
from rock.sdk.agent.models.trial.config import AgentConfig, TaskConfig


class TestRockEnvironmentConfigInheritance:
    """RockEnvironmentConfig 应同时继承 SandboxConfig 和 HarborEnvConfig。"""

    def test_is_sandbox_config_subclass(self):
        from rock.sdk.sandbox.config import SandboxConfig

        assert issubclass(RockEnvironmentConfig, SandboxConfig)

    def test_inherits_sandbox_config_fields(self):
        env = RockEnvironmentConfig()
        assert env.image == "python:3.11"
        assert env.memory == "8g"
        assert env.cpus == 2.0
        assert env.cluster == "zb"

    def test_inherits_harbor_env_fields(self):
        env = RockEnvironmentConfig()
        assert env.force_build is False
        assert env.delete is True
        assert env.type is None
        assert env.kwargs == {}

    def test_job_level_fields(self):
        env = RockEnvironmentConfig()
        assert env.envs == {}
        assert env.setup_commands == []
        assert env.file_uploads == []
        assert env.auto_stop is False

    def test_envs_field(self):
        env = RockEnvironmentConfig(env:"OPENAI_API_KEY": "sk-xxx"})
        assert env.envs == {"OPENAI_API_KEY": "sk-xxx"}

    def test_harbor_fields_settable(self):
        env = RockEnvironmentConfig(force_build=True, override_cpus=4, type="docker")
        assert env.force_build is True
        assert env.override_cpus == 4
        assert env.type.value == "docker"


class TestToHarborEnvironment:
    """to_harbor_environment() 应只返回 harbor 原生字段。"""

    def test_returns_harbor_fields_only(self):
        env = RockEnvironmentConfig(force_build=True, override_cpus=4)
        result = env.to_harbor_environment()
        assert result["force_build"] is True
        assert result["override_cpus"] == 4

    def test_excludes_rock_sandbox_fields(self):
        env = RockEnvironmentConfig(image="my-image:latest", memory="32g", cpus=8)
        result = env.to_harbor_environment()
        assert "image" not in result
        assert "memory" not in result
        assert "cpus" not in result
        assert "cluster" not in result

    def test_excludes_job_level_fields(self):
        env = RockEnvironmentConfig(
            setup_commands=["pip install x"],
            file_uploads=[("a", "b")],
            auto_stop=True,
        )
        result = env.to_harbor_environment()
        assert "setup_commands" not in result
        assert "file_uploads" not in result
        assert "auto_stop" not in result

    def test_excludes_envs_field(self):
        env = RockEnvironmentConfig(env:"KEY": "val"})
        result = env.to_harbor_environment()
        assert "envs" not in result
        assert "env" not in result

    def test_excludes_none_values(self):
        env = RockEnvironmentConfig(type=None, import_path=None, override_cpus=None)
        result = env.to_harbor_environment()
        assert "type" not in result
        assert "import_path" not in result
        assert "override_cpus" not in result

    def test_empty_config_excludes_rock_fields(self):
        env = RockEnvironmentConfig()
        result = env.to_harbor_environment()
        assert "image" not in result
        assert "setup_commands" not in result


class TestJobConfigToHarborYaml:
    def test_basic_serialization(self):
        cfg = JobConfig(
            job_name="test-job",
            n_attempts=2,
            agents=[AgentConfig(name="terminus-2", model_name="hosted_vllm/m")],
        )
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)

        assert data["job_name"] == "test-job"
        assert data["n_attempts"] == 2
        assert data["agents"][0]["name"] == "terminus-2"

    def test_excludes_rock_fields(self):
        cfg = JobConfig(
            environment=RockEnvironmentConfig(
                setup_commands=["pip install harbor"],
                file_uploads=[("local.txt", "/sandbox/remote.txt")],
                env:"API_KEY": "sk-xxx"},
                auto_stop=True,
                image="my-image:latest",
                memory="32g",
            )
        )
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)

        # Rock fields must not appear at top level
        assert "sandbox_config" not in data
        assert "setup_commands" not in data
        assert "file_uploads" not in data
        assert "sandbox_env" not in data
        assert "auto_stop_sandbox" not in data
        assert "auto_stop" not in data
        # environment block should only contain harbor fields
        assert "environment" not in data or "image" not in data.get("environment", {})
        assert "environment" not in data or "setup_commands" not in data.get("environment", {})

    def test_excludes_none_values(self):
        cfg = JobConfig(
            job_name="test",
            agents=[AgentConfig(name="t2")],
        )
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)

        assert "agent_timeout_multiplier" not in data

    def test_path_fields_serialized_as_strings(self):
        cfg = JobConfig(
            jobs_dir=Path("/workspace/jobs"),
            tasks=[TaskConfig(path="/workspace/tasks/t1")],
        )
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)

        assert data["jobs_dir"] == "/workspace/jobs"
        assert data["tasks"][0]["path"] == "/workspace/tasks/t1"

    def test_harbor_env_fields_serialized(self):
        cfg = JobConfig(
            job_name="full-test",
            n_attempts=3,
            environment=RockEnvironmentConfig(
                type="docker",
                force_build=True,
                delete=True,
                override_cpus=4,
            ),
            agents=[
                AgentConfig(
                    name="terminus-2",
                    model_name="hosted_vllm/my-model",
                    kwargs={"max_iterations": 30},
                    env={"LLM_API_KEY": "sk-xxx"},
                )
            ],
            datasets=[
                RegistryDatasetConfig(registry=RemoteRegistryInfo(), name="terminal-bench", version="2.0", n_tasks=50)
            ],
            metrics=[MetricConfig(type="mean")],
        )
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)

        assert data["job_name"] == "full-test"
        assert data["environment"]["type"] == "docker"
        assert data["environment"]["force_build"] is True
        assert data["environment"]["override_cpus"] == 4
        # Rock fields must not be in environment section
        assert "image" not in data.get("environment", {})
        assert "envs" not in data.get("environment", {})
        assert "env" not in data.get("environment", {})
        assert data["agents"][0]["kwargs"]["max_iterations"] == 30
        assert data["datasets"][0]["name"] == "terminal-bench"

    def test_envs_not_in_harbor_yaml(self):
        """envs goes to sandbox session, not harbor YAML."""
        cfg = JobConfig(
            environment=RockEnvironmentConfig(env:"OPENAI_API_KEY": "sk-xxx"})
        )
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)

        assert "sandbox_env" not in data
        env_section = data.get("environment", {})
        assert "envs" not in env_section
        assert "env" not in env_section


class TestJobConfigFromYaml:
    def test_from_yaml_basic(self, tmp_path):
        yaml_content = """
job_name: loaded-job
n_attempts: 2
agents:
  - name: terminus-2
    model_name: hosted_vllm/my-model
datasets:
  - registry:
      url: https://example.com/registry.json
    name: terminal-bench
    version: "2.0"
"""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml_content)

        cfg = JobConfig.from_yaml(str(yaml_file))
        assert cfg.job_name == "loaded-job"
        assert cfg.n_attempts == 2
        assert cfg.agents[0].name == "terminus-2"
        assert cfg.datasets[0].name == "terminal-bench"

    def test_from_yaml_with_environment_block(self, tmp_path):
        yaml_content = """
job_name: env-job
environment:
  image: my-image:latest
  memory: "32g"
  cpus: 8
  env:
    OPENAI_API_KEY: sk-xxx
  setup_commands:
    - pip install harbor
  auto_stop: true
agents:
  - name: terminus-2
"""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml_content)

        cfg = JobConfig.from_yaml(str(yaml_file))
        assert cfg.environment.image == "my-image:latest"
        assert cfg.environment.memory == "32g"
        assert cfg.environment.env == {"OPENAI_API_KEY": "sk-xxx"}
        assert cfg.environment.setup_commands == ["pip install harbor"]
        assert cfg.environment.auto_stop is True

    def test_from_yaml_with_environment_override(self, tmp_path):
        yaml_content = """
job_name: loaded-job
agents:
  - name: terminus-2
"""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml_content)

        cfg = JobConfig.from_yaml(
            str(yaml_file),
            environment={"setup_commands": ["pip install harbor"], "image": "custom:latest"},
        )
        assert cfg.job_name == "loaded-job"
        assert cfg.environment.setup_commands == ["pip install harbor"]
        assert cfg.environment.image == "custom:latest"

    def test_from_yaml_environment_override_merges(self, tmp_path):
        """Override merges into existing environment block, not replaces."""
        yaml_content = """
job_name: merge-job
environment:
  image: base-image:latest
  memory: "16g"
"""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml_content)

        cfg = JobConfig.from_yaml(
            str(yaml_file),
            environment={"setup_commands": ["echo hello"]},
        )
        assert cfg.environment.image == "base-image:latest"   # preserved
        assert cfg.environment.memory == "16g"                 # preserved
        assert cfg.environment.setup_commands == ["echo hello"]  # merged

    def test_from_yaml_with_local_dataset(self, tmp_path):
        yaml_content = """
job_name: local-dataset-job
datasets:
  - path: /data/tasks
    task_names:
      - task-1
      - task-2
"""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml_content)

        cfg = JobConfig.from_yaml(str(yaml_file))
        assert cfg.job_name == "local-dataset-job"
        assert isinstance(cfg.datasets[0], LocalDatasetConfig)
        assert cfg.datasets[0].path == Path("/data/tasks")
```

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run pytest tests/unit/sdk/agent/test_job_config_serialization.py -v
```

预期：`ImportError: cannot import name 'RockEnvironmentConfig'` 等大量失败

- [ ] **Step 3: 更新 `config.py` — 新增 `RockEnvironmentConfig`，删除旧字段，更新 `JobConfig`**

将 `rock/sdk/agent/models/job/config.py` 完整替换为：

```python
"""Job configuration models aligned with harbor.models.job.config.

Harbor-native fields are serialized to YAML and passed to ``harbor jobs start -c``.
Rock environment fields live in RockEnvironmentConfig (unified SandboxConfig + HarborEnvConfig).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from rock.sdk.agent.constants import USER_DEFINED_LOGS
from rock.sdk.agent.models.metric.config import MetricConfig
from rock.sdk.agent.models.orchestrator_type import OrchestratorType
from rock.sdk.agent.models.trial.config import (
    AgentConfig,
    ArtifactConfig,
    EnvironmentConfig as _HarborEnvConfig,
    TaskConfig,
    VerifierConfig,
)
from rock.sdk.sandbox.config import SandboxConfig


# ---------------------------------------------------------------------------
# RockEnvironmentConfig — unified environment (SandboxConfig + HarborEnvConfig)
# ---------------------------------------------------------------------------


class RockEnvironmentConfig(SandboxConfig, _HarborEnvConfig):
    """统一的 Rock 环境配置。

    多重继承 SandboxConfig（Rock 沙箱层）和 _HarborEnvConfig（Harbor 环境层），
    所有字段平铺，用户只需填写这一个块。

    序列化时通过 to_harbor_environment() 向上转型到 _HarborEnvConfig，
    自动过滤 Rock 字段，只输出 harbor 原生字段。
    """

    # ── Rock env vars ──
    # 注入到 sandbox bash session；harbor 作为子进程自然继承
    env: dict[str, str] = Field(default_factory=dict)
    # env 继承自 _HarborEnvConfig，保留不动

    # ── Job 执行配置 ──
    setup_commands: list[str] = Field(default_factory=list)
    file_uploads: list[tuple[str, str]] = Field(
        default_factory=list,
        description="运行前上传的文件/目录：[(本地路径, 沙箱路径), ...]",
    )
    auto_stop: bool = False

    def to_harbor_environment(self) -> dict:
        """向上转型到 _HarborEnvConfig，自动丢弃 Rock 字段，只保留 harbor 字段。

        envs 不属于 harbor 字段，Pydantic model_validate 自动忽略。
        """
        harbor = _HarborEnvConfig.model_validate(self.model_dump(mode="json"))
        return harbor.model_dump(mode="json", exclude_none=True)


# ---------------------------------------------------------------------------
# RetryConfig / OrchestratorConfig
# ---------------------------------------------------------------------------


class RetryConfig(BaseModel):
    max_retries: int = Field(default=0, ge=0)
    include_exceptions: set[str] | None = None
    exclude_exceptions: set[str] | None = Field(
        default_factory=lambda: {
            "AgentTimeoutError",
            "VerifierTimeoutError",
            "RewardFileNotFoundError",
            "RewardFileEmptyError",
            "VerifierOutputParseError",
        }
    )
    wait_multiplier: float = 1.0
    min_wait_sec: float = 1.0
    max_wait_sec: float = 60.0


class OrchestratorConfig(BaseModel):
    type: OrchestratorType = OrchestratorType.LOCAL
    n_concurrent_trials: int = 4
    quiet: bool = False
    retry: RetryConfig = Field(default_factory=RetryConfig)
    kwargs: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Registry info (aligned with harbor.models.registry, field definitions only)
# ---------------------------------------------------------------------------


class OssRegistryInfo(BaseModel):
    """OSS registry, corresponds to CLI ``--registry-type oss``."""

    split: str | None = None
    revision: str | None = None
    oss_dataset_path: str | None = None
    oss_access_key_id: str | None = None
    oss_access_key_secret: str | None = None
    oss_region: str | None = None
    oss_endpoint: str | None = None
    oss_bucket: str | None = None


class RemoteRegistryInfo(BaseModel):
    """Remote registry (default GitHub), corresponds to CLI ``--registry-url``."""

    name: str | None = None
    url: str = "https://raw.githubusercontent.com/laude-institute/harbor/main/registry.json"


class LocalRegistryInfo(BaseModel):
    """Local registry, corresponds to CLI ``--registry-path``."""

    name: str | None = None
    path: Path


# ---------------------------------------------------------------------------
# DatasetConfig (aligned with harbor's LocalDatasetConfig / RegistryDatasetConfig)
# ---------------------------------------------------------------------------


class BaseDatasetConfig(BaseModel):
    """Common dataset fields."""

    task_names: list[str] | None = None
    exclude_task_names: list[str] | None = None
    n_tasks: int | None = None


class LocalDatasetConfig(BaseDatasetConfig):
    """Local dataset directory, corresponds to CLI ``-p/--path`` (when pointing to a dataset dir)."""

    path: Path


class RegistryDatasetConfig(BaseDatasetConfig):
    """Registry dataset, corresponds to CLI ``-d/--dataset`` + ``--registry-type``."""

    registry: OssRegistryInfo | RemoteRegistryInfo | LocalRegistryInfo
    name: str
    version: str | None = None
    overwrite: bool = False
    download_dir: Path | None = None

    @model_validator(mode="after")
    def _infer_version_from_split(self):
        """Align with harbor CLI behavior: auto-fill version from OssRegistryInfo.split."""
        if self.version is None and isinstance(self.registry, OssRegistryInfo) and self.registry.split:
            self.version = (
                f"{self.registry.split}@{self.registry.revision}" if self.registry.revision else self.registry.split
            )
        return self


# Convenience alias
DatasetConfig = LocalDatasetConfig | RegistryDatasetConfig


class JobConfig(BaseModel):
    """Job configuration: Rock environment + Harbor-native benchmark fields.

    All Rock sandbox/lifecycle configuration lives in ``environment``.
    Harbor-native fields (agents, datasets, etc.) are serialized to YAML
    and passed to ``harbor jobs start -c``.
    """

    # ── Rock environment (sandbox + lifecycle) ──
    environment: RockEnvironmentConfig = Field(default_factory=RockEnvironmentConfig)

    # ── Harbor native fields ──
    job_name: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d__%H-%M-%S"))
    jobs_dir: Path = Path(USER_DEFINED_LOGS) / "jobs"
    n_attempts: int = 1
    timeout_multiplier: float = 1.0
    agent_timeout_multiplier: float | None = None
    verifier_timeout_multiplier: float | None = None
    agent_setup_timeout_multiplier: float | None = None
    environment_build_timeout_multiplier: float | None = None
    debug: bool = False
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    verifier: VerifierConfig = Field(default_factory=VerifierConfig)
    metrics: list[MetricConfig] = Field(default_factory=list)
    agents: list[AgentConfig] = Field(default_factory=lambda: [AgentConfig()])
    datasets: list[LocalDatasetConfig | RegistryDatasetConfig] = Field(default_factory=list)
    tasks: list[TaskConfig] = Field(default_factory=list)
    artifacts: list[str | ArtifactConfig] = Field(default_factory=list)

    def to_harbor_yaml(self) -> str:
        """Serialize Harbor-native fields to YAML for ``harbor jobs start -c``.

        Rock environment fields are excluded. Harbor environment fields
        (force_build, override_cpus, etc.) are included under ``environment``.
        """
        import yaml

        data = self.model_dump(mode="json", exclude={"environment"}, exclude_none=True)
        harbor_env = self.environment.to_harbor_environment()
        if harbor_env:
            data["environment"] = harbor_env
        return yaml.dump(data, default_flow_style=False, allow_unicode=True)

    @classmethod
    def from_yaml(cls, path: str, **overrides) -> "JobConfig":
        """Load JobConfig from a Harbor YAML config file.

        Args:
            path: Path to the YAML file.
            **overrides: Fields to override. Pass ``environment`` as a dict
                to merge into the loaded environment block, e.g.:
                ``from_yaml(path, environment={"setup_commands": ["pip install x"]})``
        """
        import yaml

        with open(path) as f:
            data = yaml.safe_load(f)

        # Merge environment overrides into the loaded environment block
        if "environment" in overrides:
            env_override = overrides.pop("environment")
            existing = data.get("environment") or {}
            if isinstance(env_override, dict):
                existing.update(env_override)
            elif hasattr(env_override, "model_dump"):
                existing.update(env_override.model_dump(exclude_none=True))
            data["environment"] = existing

        data.update(overrides)
        return cls(**data)
```

注意变更要点：
- import `EnvironmentConfig as _HarborEnvConfig`（别名，不再直接 import `EnvironmentConfig`）
- 新增 `RockEnvironmentConfig(SandboxConfig, _HarborEnvConfig)` 类，新增 `envs` 字段，`env` 继承自 `_HarborEnvConfig` 保留不动
- `JobConfig` 删除旧 Rock 扩展字段：`sandbox_config`、`setup_commands`、`file_uploads`、`sandbox_env`、`auto_stop_sandbox`
- `JobConfig` 删除 `_rock_fields` ClassVar 和 `ClassVar` import
- `JobConfig.environment` 类型从 `EnvironmentConfig` 改为 `RockEnvironmentConfig`
- `to_harbor_yaml()` 改用新逻辑
- `from_yaml()` 新增 `environment` override 合并逻辑

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/unit/sdk/agent/test_job_config_serialization.py -v
```

预期：全部 PASS

- [ ] **Step 5: Commit**

```bash
git add rock/sdk/agent/models/job/config.py tests/unit/sdk/agent/test_job_config_serialization.py
git commit -m "feat: add RockEnvironmentConfig, replace JobConfig Rock extension fields with unified environment"
```

---

## Task 2: 更新 `test_models.py` 中的 `TestJobConfig`

**Files:**
- Modify: `tests/unit/sdk/agent/test_models.py`

- [ ] **Step 1: 更新 import 和 TestJobConfig**

在文件顶部，将 import 改为：

```python
from pathlib import Path

from rock.sdk.agent.models.environment_type import EnvironmentType
from rock.sdk.agent.models.job.config import (
    JobConfig,
    LocalDatasetConfig,
    OrchestratorConfig,
    RegistryDatasetConfig,
    RemoteRegistryInfo,
    RetryConfig,
    RockEnvironmentConfig,
)
from rock.sdk.agent.models.metric.config import MetricConfig
from rock.sdk.agent.models.metric.type import MetricType
from rock.sdk.agent.models.orchestrator_type import OrchestratorType
from rock.sdk.agent.models.trial.config import (
    AgentConfig,
    ArtifactConfig,
    EnvironmentConfig as HarborEnvironmentConfig,
    TaskConfig,
    VerifierConfig,
)
```

将 `TestEnvironmentConfig` 中的 `EnvironmentConfig` 替换为 `HarborEnvironmentConfig`：

```python
class TestEnvironmentConfig:
    def test_defaults(self):
        env = HarborEnvironmentConfig()
        assert env.type is None
        assert env.force_build is False
        assert env.delete is True
        assert env.env == {}
        assert env.kwargs == {}

    def test_with_type(self):
        env = HarborEnvironmentConfig(type=EnvironmentType.DOCKER, force_build=True)
        assert env.type == EnvironmentType.DOCKER
        assert env.force_build is True

    def test_with_string_type(self):
        env = HarborEnvironmentConfig(type="docker")
        assert env.type == EnvironmentType.DOCKER
```

将 `TestJobConfig` 替换为：

```python
class TestJobConfig:
    def test_defaults(self):
        cfg = JobConfig()
        assert cfg.n_attempts == 1
        assert cfg.timeout_multiplier == 1.0
        assert cfg.debug is False
        assert isinstance(cfg.orchestrator, OrchestratorConfig)
        assert isinstance(cfg.environment, RockEnvironmentConfig)
        assert cfg.agents == [AgentConfig()]
        assert cfg.datasets == []
        assert cfg.tasks == []
        assert cfg.metrics == []
        assert cfg.artifacts == []

    def test_environment_defaults(self):
        cfg = JobConfig()
        assert cfg.environment.setup_commands == []
        assert cfg.environment.file_uploads == []
        assert cfg.environment.env == {}
        assert cfg.environment.auto_stop is False

    def test_with_full_config(self):
        cfg = JobConfig(
            job_name="test-job",
            n_attempts=2,
            agents=[AgentConfig(name="terminus-2", model_name="hosted_vllm/m")],
            datasets=[RegistryDatasetConfig(registry=RemoteRegistryInfo(), name="terminal-bench", version="2.0")],
            environment=RockEnvironmentConfig(setup_commands=["pip install harbor"]),
        )
        assert cfg.job_name == "test-job"
        assert cfg.n_attempts == 2
        assert len(cfg.agents) == 1
        assert cfg.agents[0].name == "terminus-2"
        assert cfg.environment.setup_commands == ["pip install harbor"]
```

`TestPublicAPI` 保持不变（后续 Task 4 更新 `__init__.py` 后自然通过）。

- [ ] **Step 2: 运行测试确认通过**

```bash
uv run pytest tests/unit/sdk/agent/test_models.py -v
```

预期：全部 PASS

- [ ] **Step 3: Commit**

```bash
git add tests/unit/sdk/agent/test_models.py
git commit -m "test: update test_models.py for RockEnvironmentConfig"
```

---

## Task 3: 更新 `Job` 类和 `test_job.py`

**Files:**
- Modify: `tests/unit/sdk/agent/test_job.py`
- Modify: `rock/sdk/agent/job.py`

- [ ] **Step 1: 更新 `test_job.py` 中使用旧字段的测试**

在文件顶部 import 加：

```python
from rock.sdk.agent.models.job.config import RockEnvironmentConfig
```

替换 `test_run_auto_stop_sandbox` 和 `test_run_does_not_stop_when_disabled`：

```python
    async def test_run_auto_stop_sandbox(self):
        mock_sandbox = _make_mock_sandbox()

        with patch("rock.sdk.sandbox.client.Sandbox", return_value=mock_sandbox):
            config = JobConfig(job_name="test-job", environment=RockEnvironmentConfig(auto_stop=True))
            job = Job(config)
            await job.run()

            mock_sandbox.close.assert_called_once()

    async def test_run_does_not_stop_when_disabled(self):
        mock_sandbox = _make_mock_sandbox()

        with patch("rock.sdk.sandbox.client.Sandbox", return_value=mock_sandbox):
            config = JobConfig(job_name="test-job", environment=RockEnvironmentConfig(auto_stop=False))
            job = Job(config)
            await job.run()

            mock_sandbox.close.assert_not_called()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run pytest tests/unit/sdk/agent/test_job.py -v
```

预期：`test_run_auto_stop_sandbox` 等测试 FAIL，因为 `job.py` 还引用旧字段

- [ ] **Step 3: 更新 `job.py` 字段引用**

在 `rock/sdk/agent/job.py` 中做以下 5 处替换：

1. `submit()` 方法中（第 88 行）：
   - 旧：`self._sandbox = Sandbox(self._config.sandbox_config)`
   - 新：`self._sandbox = Sandbox(self._config.environment)`

2. `wait()` 方法中（第 124 行）：
   - 旧：`if self._config.auto_stop_sandbox and self._sandbox:`
   - 新：`if self._config.environment.auto_stop and self._sandbox:`

3. `_prepare_and_start()` 方法中（第 158 行）：
   - 旧：`for local_path, sandbox_path in self._config.file_uploads:`
   - 新：`for local_path, sandbox_path in self._config.environment.file_uploads:`

4. `_render_run_script()` 方法中（第 187 行）：
   - 旧：`for cmd in self._config.setup_commands:`
   - 新：`for cmd in self._config.environment.setup_commands:`

5. `_create_session()` 方法中（第 209 行）：
   - 旧：`env=self._config.sandbox_env or None,`
   - 新：`env=self._config.environment.env or None,`

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/unit/sdk/agent/test_job.py -v
```

预期：全部 PASS

- [ ] **Step 5: Commit**

```bash
git add rock/sdk/agent/job.py tests/unit/sdk/agent/test_job.py
git commit -m "refactor: update Job class to use environment.* field references"
```

---

## Task 4: 更新 `__init__.py` 导出

**Files:**
- Modify: `rock/sdk/agent/models/job/__init__.py`
- Modify: `rock/sdk/agent/models/__init__.py`
- Modify: `rock/sdk/agent/__init__.py`

- [ ] **Step 1: 更新 `job/__init__.py`**

```python
from .config import (
    JobConfig,
    LocalDatasetConfig,
    OrchestratorConfig,
    OssRegistryInfo,
    RegistryDatasetConfig,
    RemoteRegistryInfo,
    RetryConfig,
    RockEnvironmentConfig,
)
from .result import JobResult, JobStatus

__all__ = [
    "JobConfig",
    "OrchestratorConfig",
    "RetryConfig",
    "RegistryDatasetConfig",
    "LocalDatasetConfig",
    "OssRegistryInfo",
    "RemoteRegistryInfo",
    "RockEnvironmentConfig",
    "JobResult",
    "JobStatus",
]
```

- [ ] **Step 2: 更新 `models/__init__.py`**

```python
from rock.sdk.agent.models.environment_type import EnvironmentType
from rock.sdk.agent.models.job.config import DatasetConfig, JobConfig, OrchestratorConfig, RetryConfig
from rock.sdk.agent.models.job.config import RockEnvironmentConfig
from rock.sdk.agent.models.metric.config import MetricConfig
from rock.sdk.agent.models.metric.type import MetricType
from rock.sdk.agent.models.orchestrator_type import OrchestratorType
from rock.sdk.agent.models.trial.config import (
    AgentConfig,
    ArtifactConfig,
    TaskConfig,
    VerifierConfig,
)

__all__ = [
    "JobConfig",
    "OrchestratorConfig",
    "RetryConfig",
    "DatasetConfig",
    "AgentConfig",
    "RockEnvironmentConfig",
    "VerifierConfig",
    "TaskConfig",
    "ArtifactConfig",
    "MetricConfig",
    "MetricType",
    "OrchestratorType",
    "EnvironmentType",
]
```

注意：`EnvironmentConfig` 从 `trial.config` 的导入已移除，替换为 `RockEnvironmentConfig` 从 `job.config` 导入。

- [ ] **Step 3: 更新 `agent/__init__.py`**

```python
from rock.sdk.agent.job import Job
from rock.sdk.agent.models.job.config import (
    JobConfig,
    LocalDatasetConfig,
    OrchestratorConfig,
    OssRegistryInfo,
    RegistryDatasetConfig,
    RemoteRegistryInfo,
    RetryConfig,
    RockEnvironmentConfig,
)
from rock.sdk.agent.models.job.result import JobResult, JobStatus
from rock.sdk.agent.models.metric.config import MetricConfig
from rock.sdk.agent.models.trial.config import (
    AgentConfig,
    ArtifactConfig,
    TaskConfig,
    VerifierConfig,
)
from rock.sdk.agent.models.trial.result import (
    AgentInfo,
    AgentResult,
    ExceptionInfo,
    TrialResult,
    VerifierResult,
)

__all__ = [
    "Job",
    "JobResult",
    "JobStatus",
    "TrialResult",
    "VerifierResult",
    "AgentInfo",
    "AgentResult",
    "ExceptionInfo",
    "JobConfig",
    "RockEnvironmentConfig",
    "RegistryDatasetConfig",
    "LocalDatasetConfig",
    "OssRegistryInfo",
    "RemoteRegistryInfo",
    "OrchestratorConfig",
    "RetryConfig",
    "AgentConfig",
    "VerifierConfig",
    "TaskConfig",
    "ArtifactConfig",
    "MetricConfig",
]
```

注意：`EnvironmentConfig` 导入和 `__all__` 导出均替换为 `RockEnvironmentConfig`。

- [ ] **Step 4: 运行全量测试**

```bash
uv run pytest tests/unit/sdk/agent/ -v
```

预期：全部 PASS

- [ ] **Step 5: Commit**

```bash
git add rock/sdk/agent/models/job/__init__.py rock/sdk/agent/models/__init__.py rock/sdk/agent/__init__.py
git commit -m "refactor: update __init__.py exports to use RockEnvironmentConfig"
```

---

## Task 5: 更新 YAML 模板

**Files:**
- Modify: `examples/harbor/swe_job_config.yaml.template`
- Modify: `examples/harbor/tb_job_config.yaml.template`

- [ ] **Step 1: 更新 `swe_job_config.yaml.template`**

```yaml
# ── Rock Environment ─────────────────────────────────
environment:
  base_url: "<your-rock-base-url>"
  image: "<your-harbor-image:tag>"
  cluster: "<your-cluster>"
  memory: "32g"
  cpus: 8
  startup_timeout: 1800
  auto_clear_seconds: 7200
  auto_stop: false
  env:
    OPENAI_API_KEY: "<your-openai-api-key>"
    OPENAI_BASE_URL: "<your-openai-base-url>"

# ── Harbor Native ────────────────────────────────────
agents:
  - name: "swe-agent"
    model_name: "custom_openai/<your-model>"

datasets:
  - name: "princeton-nlp/SWE-bench_Verified"
    registry:
      split: "test"
      oss_access_key_id: "<your-oss-access-key-id>"
      oss_access_key_secret: "<your-oss-access-key-secret>"
      oss_bucket: "<your-oss-bucket>"
      oss_dataset_path: "<your-oss-dataset-path>"
      oss_region: "<your-oss-region>"
      oss_endpoint: "<your-oss-endpoint>"
    task_names:
      - "astropy__astropy-7606"
```

注意变更：
- `sandbox_config:` + 顶层 Rock 字段 → `environment:` 统一块
- `agents.env` 中的重复 API key 删除（sandbox session env 自动继承到子进程）

- [ ] **Step 2: 更新 `tb_job_config.yaml.template`**

```yaml
# ── Rock Environment ─────────────────────────────────
environment:
  base_url: "<your-rock-base-url>"
  image: "<your-harbor-image:tag>"
  cluster: "<your-cluster>"
  memory: "16g"
  cpus: 4
  startup_timeout: 1800
  auto_clear_seconds: 7200
  auto_stop: true
  env:
    LLM_API_KEY: "<your-llm-api-key>"
    LLM_BASE_URL: "<your-llm-base-url>"

# ── Harbor Native ────────────────────────────────────
agents:
  - name: "openhands"
    model_name: "<your-provider>/<your-model>"

orchestrator:
  n_concurrent_trials: 1

datasets:
  - name: "terminal-bench-2-test"
    registry:
      split: "test"
      oss_access_key_id: "<your-oss-access-key-id>"
      oss_access_key_secret: "<your-oss-access-key-secret>"
      oss_region: "<your-oss-region>"
      oss_endpoint: "<your-oss-endpoint>"
      oss_bucket: "<your-oss-bucket>"
      oss_dataset_path: "<your-oss-dataset-path>"
    task_names:
      - "crack-7z-hash"
```

注意变更：
- `sandbox_config:` + `auto_stop_sandbox:` + `sandbox_env:` → `environment:` 统一块
- `agents.env` 中的重复 LLM key 删除

- [ ] **Step 3: Commit**

```bash
git add examples/harbor/swe_job_config.yaml.template examples/harbor/tb_job_config.yaml.template
git commit -m "docs: update YAML templates for new environment block"
```

---

## Task 6: 更新 README 文档

**Files:**
- Modify: `docs/dev/agent/README.md`

- [ ] **Step 1: 更新文件结构图（约 44-58 行）**

将 `job/` 目录描述更新为：

```
    ├── job/
    │   ├── __init__.py
    │   ├── config.py                # JobConfig, RockEnvironmentConfig,
    │   │                            # OrchestratorConfig, RetryConfig
    │   │                            # OssRegistryInfo, RemoteRegistryInfo, LocalRegistryInfo
    │   │                            # BaseDatasetConfig, LocalDatasetConfig, RegistryDatasetConfig
    │   └── result.py                # JobResult, JobStatus
```

- [ ] **Step 2: 更新类图（约 131-190 行）中的 JobConfig 部分**

将 Rock 扩展字段部分替换为：

```
JobConfig (Pydantic, rock/sdk/agent/models/job/config.py)
│
│  ── Rock 环境（统一配置，不序列化到 Harbor YAML） ──
├── environment: RockEnvironmentConfig
│   ├── (继承 SandboxConfig) image, memory, cpus, cluster, base_url, ...
│   ├── (继承 HarborEnvConfig) type, force_build, override_cpus, ...
│   ├── env: dict[str, str]          # sandbox session 环境变量
│   ├── setup_commands: list[str]      # harbor run 前的准备命令
│   ├── file_uploads: list[tuple]      # 上传文件：(local_path, sandbox_path)
│   └── auto_stop: bool               # 完成后自动关闭 sandbox
│
│  ── Harbor 原生字段（序列化到 YAML，传给 harbor CLI） ──
├── job_name: str
...（其余不变）
```

- [ ] **Step 3: 更新配置示例（约 511-565 行）**

将代码示例中的 `sandbox_config=SandboxConfig(...)` + `setup_commands` + `sandbox_env` 替换为 `environment=RockEnvironmentConfig(...)` 用法。

- [ ] **Step 4: 运行全量快速测试**

```bash
uv run pytest -m "not need_ray and not need_admin and not need_admin_and_network" --reruns 1 -q
```

预期：全部 PASS，无 FAIL

- [ ] **Step 5: Commit**

```bash
git add docs/dev/agent/README.md
git commit -m "docs: update README for RockEnvironmentConfig"
```
