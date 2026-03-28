# Rock Job SDK Implementation Plan

> ⚠️ **历史文档 — 实现已完成**
>
> 本文档是实现前的 TDD 计划，`rock/sdk/agent/` 模块已在 PR #681 中完成实现，并经过后续重构（`86f32bec`、`180d954e`）。
>
> **与实际实现的差异：**
> - `DatasetConfig` 未合并为单一类，保留了 `LocalDatasetConfig`/`RegistryDatasetConfig` 分离
> - `OssRegistryInfo`/`RemoteRegistryInfo`/`LocalRegistryInfo` 完整保留，未内联到 DatasetConfig
> - JobConfig Rock 扩展字段为 `sandbox_config`/`file_uploads`/`sandbox_env`/`auto_stop_sandbox`（非文档中的 `sandbox`/`result_file`/`collect_trajectory`/`auto_start_sandbox`）
> - `TrialResult`/`JobResult` 有独立文件，不在 `job.py` 中
> - `TrialResult` 是丰富的子模型结构（`ExceptionInfo`/`AgentInfo`/`ModelInfo`/`AgentResult`/`VerifierResult`/`TimingInfo`）
> - Job 类 `__init__` 仅接受 `JobConfig`，不接受 `sandbox` 参数；`submit()` 返回 `None`（非 `str`）
> - 执行命令合并为单一 bash 脚本（含 dockerd 检测 + setup + harbor run）
> - 结果从 trial 级 `result.json` 逐个读取，非 job 级
>
> **请参考 `README.md` 获取最新文档。**
>
> ---

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `rock/sdk/agent/` module — a Job SDK that executes Harbor benchmark jobs inside Rock sandboxes via bash protocol, enabling RL training frameworks to run Harbor without importing it.

**Architecture:** Copy Harbor config schemas (Pydantic models only, no runtime methods) into `rock/sdk/agent/models/`. Build `Job` class that serializes config to YAML, uploads to sandbox, runs `harbor jobs start` via nohup, and collects results by reading `result.json`. All I/O is async via the existing `Sandbox.arun()` API.

**Tech Stack:** Pydantic v2 (config models), PyYAML (serialization), asyncio (async execution), pytest + monkeypatch (TDD)

---

## File Structure

```
rock/sdk/agent/
├── __init__.py                          # Public exports: Job, JobResult, TrialResult, JobStatus
├── job.py                               # Job, JobResult, TrialResult, JobStatus
└── models/
    ├── __init__.py                      # Re-export all config classes
    ├── job/
    │   ├── __init__.py                  # Re-export JobConfig, OrchestratorConfig, RetryConfig, DatasetConfig
    │   └── config.py                    # JobConfig, OrchestratorConfig, RetryConfig, DatasetConfig
    ├── trial/
    │   ├── __init__.py                  # Re-export AgentConfig, EnvironmentConfig, VerifierConfig, TaskConfig, ArtifactConfig
    │   └── config.py                    # AgentConfig, EnvironmentConfig, VerifierConfig, TaskConfig, ArtifactConfig
    ├── metric/
    │   ├── __init__.py                  # Re-export MetricConfig, MetricType
    │   ├── config.py                    # MetricConfig
    │   └── type.py                      # MetricType enum
    ├── orchestrator_type.py             # OrchestratorType enum
    └── environment_type.py              # EnvironmentType enum

tests/unit/sdk/agent/
├── __init__.py
├── test_models.py                       # Tests for all config models and enums
├── test_job_config_serialization.py     # Tests for JobConfig YAML serialization
└── test_job.py                          # Tests for Job, JobResult, TrialResult
```

---

## Task 1: Enum Models — OrchestratorType, EnvironmentType, MetricType

**Files:**
- Create: `rock/sdk/agent/models/__init__.py`
- Create: `rock/sdk/agent/models/orchestrator_type.py`
- Create: `rock/sdk/agent/models/environment_type.py`
- Create: `rock/sdk/agent/models/metric/__init__.py`
- Create: `rock/sdk/agent/models/metric/type.py`
- Create: `tests/unit/sdk/agent/__init__.py`
- Create: `tests/unit/sdk/agent/test_models.py`

- [ ] **Step 1: Write failing tests for enums**

```python
# tests/unit/sdk/agent/test_models.py
from rock.sdk.agent.models.orchestrator_type import OrchestratorType
from rock.sdk.agent.models.environment_type import EnvironmentType
from rock.sdk.agent.models.metric.type import MetricType


class TestOrchestratorType:
    def test_values(self):
        assert OrchestratorType.LOCAL == "local"
        assert OrchestratorType.QUEUE == "queue"

    def test_from_string(self):
        assert OrchestratorType("local") == OrchestratorType.LOCAL
        assert OrchestratorType("queue") == OrchestratorType.QUEUE


class TestEnvironmentType:
    def test_values(self):
        assert EnvironmentType.DOCKER == "docker"
        assert EnvironmentType.ROCK == "rock"
        assert EnvironmentType.E2B == "e2b"

    def test_all_types_exist(self):
        expected = {"docker", "daytona", "e2b", "modal", "runloop", "gke", "rock"}
        actual = {e.value for e in EnvironmentType}
        assert actual == expected


class TestMetricType:
    def test_values(self):
        assert MetricType.MEAN == "mean"
        assert MetricType.SUM == "sum"
        assert MetricType.UV_SCRIPT == "uv-script"

    def test_all_types_exist(self):
        expected = {"sum", "min", "max", "mean", "uv-script"}
        actual = {e.value for e in MetricType}
        assert actual == expected
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/sdk/agent/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rock.sdk.agent.models'`

- [ ] **Step 3: Implement enums**

```python
# rock/sdk/agent/models/__init__.py
# (empty for now, will add re-exports later)
```

```python
# rock/sdk/agent/models/orchestrator_type.py
from enum import Enum


class OrchestratorType(str, Enum):
    LOCAL = "local"
    QUEUE = "queue"
```

```python
# rock/sdk/agent/models/environment_type.py
from enum import Enum


class EnvironmentType(str, Enum):
    DOCKER = "docker"
    DAYTONA = "daytona"
    E2B = "e2b"
    MODAL = "modal"
    RUNLOOP = "runloop"
    GKE = "gke"
    ROCK = "rock"
```

```python
# rock/sdk/agent/models/metric/__init__.py
# (empty for now)
```

```python
# rock/sdk/agent/models/metric/type.py
from enum import Enum


class MetricType(str, Enum):
    SUM = "sum"
    MIN = "min"
    MAX = "max"
    MEAN = "mean"
    UV_SCRIPT = "uv-script"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/sdk/agent/test_models.py -v`
Expected: PASS — all 6 tests green

- [ ] **Step 5: Commit**

```bash
git add rock/sdk/agent/models/ tests/unit/sdk/agent/
git commit -m "feat(agent): add enum models — OrchestratorType, EnvironmentType, MetricType"
```

---

## Task 2: Trial Config Models — AgentConfig, EnvironmentConfig, VerifierConfig, TaskConfig, ArtifactConfig

**Files:**
- Create: `rock/sdk/agent/models/trial/__init__.py`
- Create: `rock/sdk/agent/models/trial/config.py`
- Modify: `tests/unit/sdk/agent/test_models.py` (append)

- [ ] **Step 1: Write failing tests for trial configs**

Append to `tests/unit/sdk/agent/test_models.py`:

```python
from pathlib import Path

from rock.sdk.agent.models.trial.config import (
    AgentConfig,
    ArtifactConfig,
    EnvironmentConfig,
    TaskConfig,
    VerifierConfig,
)
from rock.sdk.agent.models.environment_type import EnvironmentType


class TestAgentConfig:
    def test_defaults(self):
        agent = AgentConfig()
        assert agent.name is None
        assert agent.import_path is None
        assert agent.model_name is None
        assert agent.override_timeout_sec is None
        assert agent.override_setup_timeout_sec is None
        assert agent.max_timeout_sec is None
        assert agent.kwargs == {}
        assert agent.env == {}

    def test_with_values(self):
        agent = AgentConfig(
            name="terminus-2",
            model_name="hosted_vllm/my-model",
            kwargs={"max_iterations": 30},
            env={"LLM_API_KEY": "sk-xxx"},
        )
        assert agent.name == "terminus-2"
        assert agent.model_name == "hosted_vllm/my-model"
        assert agent.kwargs["max_iterations"] == 30
        assert agent.env["LLM_API_KEY"] == "sk-xxx"


class TestEnvironmentConfig:
    def test_defaults(self):
        env = EnvironmentConfig()
        assert env.type is None
        assert env.force_build is False
        assert env.delete is True
        assert env.env == {}
        assert env.kwargs == {}

    def test_with_type(self):
        env = EnvironmentConfig(type=EnvironmentType.DOCKER, force_build=True)
        assert env.type == EnvironmentType.DOCKER
        assert env.force_build is True

    def test_with_string_type(self):
        env = EnvironmentConfig(type="docker")
        assert env.type == EnvironmentType.DOCKER


class TestVerifierConfig:
    def test_defaults(self):
        v = VerifierConfig()
        assert v.override_timeout_sec is None
        assert v.max_timeout_sec is None
        assert v.disable is False


class TestTaskConfig:
    def test_required_path(self):
        task = TaskConfig(path="/workspace/tasks/fix-bug")
        assert task.path == Path("/workspace/tasks/fix-bug")

    def test_optional_fields(self):
        task = TaskConfig(path="/workspace/tasks/t1", git_url="https://github.com/repo", overwrite=True)
        assert task.git_url == "https://github.com/repo"
        assert task.overwrite is True


class TestArtifactConfig:
    def test_fields(self):
        a = ArtifactConfig(source="logs/*.log")
        assert a.source == "logs/*.log"
        assert a.destination is None

    def test_with_destination(self):
        a = ArtifactConfig(source="result.json", destination="/output/result.json")
        assert a.destination == "/output/result.json"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/sdk/agent/test_models.py::TestAgentConfig -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rock.sdk.agent.models.trial'`

- [ ] **Step 3: Implement trial config models**

```python
# rock/sdk/agent/models/trial/__init__.py
from .config import AgentConfig, ArtifactConfig, EnvironmentConfig, TaskConfig, VerifierConfig

__all__ = ["AgentConfig", "EnvironmentConfig", "VerifierConfig", "TaskConfig", "ArtifactConfig"]
```

```python
# rock/sdk/agent/models/trial/config.py
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from rock.sdk.agent.models.environment_type import EnvironmentType


class AgentConfig(BaseModel):
    name: str | None = None
    import_path: str | None = None
    model_name: str | None = None
    override_timeout_sec: float | None = None
    override_setup_timeout_sec: float | None = None
    max_timeout_sec: float | None = None
    kwargs: dict[str, Any] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)


class EnvironmentConfig(BaseModel):
    type: EnvironmentType | None = None
    import_path: str | None = None
    force_build: bool = False
    delete: bool = True
    override_cpus: int | None = None
    override_memory_mb: int | None = None
    override_storage_mb: int | None = None
    override_gpus: int | None = None
    suppress_override_warnings: bool = False
    mounts_json: list[dict[str, Any]] | None = None
    env: dict[str, str] = Field(default_factory=dict)
    kwargs: dict[str, Any] = Field(default_factory=dict)


class VerifierConfig(BaseModel):
    override_timeout_sec: float | None = None
    max_timeout_sec: float | None = None
    disable: bool = False


class TaskConfig(BaseModel):
    path: Path
    git_url: str | None = None
    git_commit_id: str | None = None
    overwrite: bool = False
    download_dir: Path | None = None
    source: str | None = None


class ArtifactConfig(BaseModel):
    source: str
    destination: str | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/sdk/agent/test_models.py -v`
Expected: PASS — all tests green

- [ ] **Step 5: Commit**

```bash
git add rock/sdk/agent/models/trial/ tests/unit/sdk/agent/test_models.py
git commit -m "feat(agent): add trial config models — AgentConfig, EnvironmentConfig, TaskConfig, etc."
```

---

## Task 3: Metric and Job Config Models — MetricConfig, RetryConfig, OrchestratorConfig, DatasetConfig, JobConfig

**Files:**
- Create: `rock/sdk/agent/models/metric/config.py`
- Create: `rock/sdk/agent/models/job/__init__.py`
- Create: `rock/sdk/agent/models/job/config.py`
- Modify: `rock/sdk/agent/models/metric/__init__.py`
- Modify: `tests/unit/sdk/agent/test_models.py` (append)

- [ ] **Step 1: Write failing tests for MetricConfig**

Append to `tests/unit/sdk/agent/test_models.py`:

```python
from rock.sdk.agent.models.metric.config import MetricConfig
from rock.sdk.agent.models.metric.type import MetricType


class TestMetricConfig:
    def test_defaults(self):
        m = MetricConfig()
        assert m.type == MetricType.MEAN
        assert m.kwargs == {}

    def test_with_type(self):
        m = MetricConfig(type="sum")
        assert m.type == MetricType.SUM
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/sdk/agent/test_models.py::TestMetricConfig -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rock.sdk.agent.models.metric.config'`

- [ ] **Step 3: Implement MetricConfig**

```python
# rock/sdk/agent/models/metric/config.py
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from rock.sdk.agent.models.metric.type import MetricType


class MetricConfig(BaseModel):
    type: MetricType = Field(default=MetricType.MEAN)
    kwargs: dict[str, Any] = Field(default_factory=dict)
```

Update `rock/sdk/agent/models/metric/__init__.py`:

```python
from .config import MetricConfig
from .type import MetricType

__all__ = ["MetricConfig", "MetricType"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/sdk/agent/test_models.py::TestMetricConfig -v`
Expected: PASS

- [ ] **Step 5: Write failing tests for job config models**

Append to `tests/unit/sdk/agent/test_models.py`:

```python
from rock.sdk.agent.models.job.config import (
    DatasetConfig,
    JobConfig,
    OrchestratorConfig,
    RetryConfig,
)
from rock.sdk.agent.models.orchestrator_type import OrchestratorType


class TestRetryConfig:
    def test_defaults(self):
        r = RetryConfig()
        assert r.max_retries == 0
        assert r.wait_multiplier == 1.0
        assert r.min_wait_sec == 1.0
        assert r.max_wait_sec == 60.0


class TestOrchestratorConfig:
    def test_defaults(self):
        o = OrchestratorConfig()
        assert o.type == OrchestratorType.LOCAL
        assert o.n_concurrent_trials == 4
        assert o.quiet is False

    def test_with_values(self):
        o = OrchestratorConfig(type="queue", n_concurrent_trials=8, quiet=True)
        assert o.type == OrchestratorType.QUEUE
        assert o.n_concurrent_trials == 8


class TestDatasetConfig:
    def test_minimal(self):
        d = DatasetConfig()
        assert d.task_names is None
        assert d.n_tasks is None
        assert d.path is None
        assert d.name is None

    def test_local_dataset(self):
        d = DatasetConfig(path="/data/tasks")
        assert d.path == Path("/data/tasks")

    def test_registry_dataset(self):
        d = DatasetConfig(name="terminal-bench", version="2.0", n_tasks=50)
        assert d.name == "terminal-bench"
        assert d.version == "2.0"
        assert d.n_tasks == 50


class TestJobConfig:
    def test_defaults(self):
        cfg = JobConfig()
        assert cfg.n_attempts == 1
        assert cfg.timeout_multiplier == 1.0
        assert cfg.debug is False
        assert isinstance(cfg.orchestrator, OrchestratorConfig)
        assert isinstance(cfg.environment, EnvironmentConfig)
        assert cfg.agents == [AgentConfig()]
        assert cfg.datasets == []
        assert cfg.tasks == []
        assert cfg.metrics == []
        assert cfg.artifacts == []

    def test_rock_extension_defaults(self):
        cfg = JobConfig()
        assert cfg.sandbox is None
        assert cfg.setup_commands == []
        assert cfg.result_file == ""
        assert cfg.collect_trajectory is False
        assert cfg.auto_start_sandbox is True
        assert cfg.auto_stop_sandbox is False

    def test_with_full_config(self):
        cfg = JobConfig(
            job_name="test-job",
            n_attempts=2,
            agents=[AgentConfig(name="terminus-2", model_name="hosted_vllm/m")],
            datasets=[DatasetConfig(name="terminal-bench", version="2.0")],
            setup_commands=["pip install harbor"],
            collect_trajectory=True,
        )
        assert cfg.job_name == "test-job"
        assert cfg.n_attempts == 2
        assert len(cfg.agents) == 1
        assert cfg.agents[0].name == "terminus-2"
        assert cfg.collect_trajectory is True
```

- [ ] **Step 6: Run test to verify it fails**

Run: `uv run pytest tests/unit/sdk/agent/test_models.py::TestJobConfig -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rock.sdk.agent.models.job'`

- [ ] **Step 7: Implement job config models**

```python
# rock/sdk/agent/models/job/__init__.py
from .config import DatasetConfig, JobConfig, OrchestratorConfig, RetryConfig

__all__ = ["JobConfig", "OrchestratorConfig", "RetryConfig", "DatasetConfig"]
```

```python
# rock/sdk/agent/models/job/config.py
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from rock.sdk.agent.models.metric.config import MetricConfig
from rock.sdk.agent.models.orchestrator_type import OrchestratorType
from rock.sdk.agent.models.trial.config import (
    AgentConfig,
    ArtifactConfig,
    EnvironmentConfig,
    TaskConfig,
    VerifierConfig,
)


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


class DatasetConfig(BaseModel):
    """Simplified dataset config, compatible with Harbor YAML datasets field.

    Merges LocalDatasetConfig and RegistryDatasetConfig into one class —
    only field definitions for YAML serialization, no runtime methods.
    """

    # Common fields (from BaseDatasetConfig)
    task_names: list[str] | None = None
    exclude_task_names: list[str] | None = None
    n_tasks: int | None = None

    # Local dataset (from LocalDatasetConfig)
    path: Path | None = None

    # Registry dataset (from RegistryDatasetConfig)
    name: str | None = None
    version: str | None = None
    overwrite: bool = False
    download_dir: Path | None = None
    registry_url: str | None = None
    registry_path: Path | None = None


class JobConfig(BaseModel):
    """Job configuration combining Harbor-native fields with Rock extensions.

    Harbor-native fields are serialized to YAML and passed to `harbor jobs start -c`.
    Rock extension fields control sandbox lifecycle.
    """

    # ── Rock extension fields ──
    sandbox: Any | None = None  # SandboxConfig, optional to allow headless config building
    setup_commands: list[str] = Field(default_factory=list)
    result_file: str = ""
    collect_trajectory: bool = False
    auto_start_sandbox: bool = True
    auto_stop_sandbox: bool = False

    # ── Harbor native fields ──
    job_name: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d__%H-%M-%S"))
    jobs_dir: Path = Path("jobs")
    n_attempts: int = 1
    timeout_multiplier: float = 1.0
    agent_timeout_multiplier: float | None = None
    verifier_timeout_multiplier: float | None = None
    agent_setup_timeout_multiplier: float | None = None
    environment_build_timeout_multiplier: float | None = None
    debug: bool = False
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    verifier: VerifierConfig = Field(default_factory=VerifierConfig)
    metrics: list[MetricConfig] = Field(default_factory=list)
    agents: list[AgentConfig] = Field(default_factory=lambda: [AgentConfig()])
    datasets: list[DatasetConfig] = Field(default_factory=list)
    tasks: list[TaskConfig] = Field(default_factory=list)
    artifacts: list[str | ArtifactConfig] = Field(default_factory=list)
```

- [ ] **Step 8: Run all model tests to verify they pass**

Run: `uv run pytest tests/unit/sdk/agent/test_models.py -v`
Expected: PASS — all tests green

- [ ] **Step 9: Commit**

```bash
git add rock/sdk/agent/models/ tests/unit/sdk/agent/test_models.py
git commit -m "feat(agent): add job config models — JobConfig, OrchestratorConfig, DatasetConfig, etc."
```

---

## Task 4: JobConfig YAML Serialization — `to_harbor_yaml()` and `from_yaml()`

**Files:**
- Modify: `rock/sdk/agent/models/job/config.py` (add methods to JobConfig)
- Create: `tests/unit/sdk/agent/test_job_config_serialization.py`

- [ ] **Step 1: Write failing tests for YAML serialization**

```python
# tests/unit/sdk/agent/test_job_config_serialization.py
import yaml
from pathlib import Path

from rock.sdk.agent.models.job.config import DatasetConfig, JobConfig
from rock.sdk.agent.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig
from rock.sdk.agent.models.metric.config import MetricConfig


class TestJobConfigToHarborYaml:
    """Test serializing JobConfig to Harbor-compatible YAML."""

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
            setup_commands=["pip install harbor"],
            collect_trajectory=True,
            auto_start_sandbox=False,
        )
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)

        assert "sandbox" not in data
        assert "setup_commands" not in data
        assert "collect_trajectory" not in data
        assert "auto_start_sandbox" not in data
        assert "auto_stop_sandbox" not in data
        assert "result_file" not in data

    def test_excludes_none_values(self):
        cfg = JobConfig(
            job_name="test",
            agents=[AgentConfig(name="t2")],
        )
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)

        # None fields should be excluded
        assert "agent_timeout_multiplier" not in data
        assert data["agents"][0].get("import_path") is None or "import_path" not in data["agents"][0]

    def test_path_fields_serialized_as_strings(self):
        cfg = JobConfig(
            jobs_dir=Path("/workspace/jobs"),
            tasks=[TaskConfig(path="/workspace/tasks/t1")],
        )
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)

        assert data["jobs_dir"] == "/workspace/jobs"
        assert data["tasks"][0]["path"] == "/workspace/tasks/t1"

    def test_full_config_roundtrip(self):
        cfg = JobConfig(
            job_name="full-test",
            n_attempts=3,
            environment=EnvironmentConfig(type="docker", force_build=True, delete=True),
            agents=[AgentConfig(
                name="terminus-2",
                model_name="hosted_vllm/my-model",
                kwargs={"max_iterations": 30},
                env={"LLM_API_KEY": "sk-xxx"},
            )],
            datasets=[DatasetConfig(name="terminal-bench", version="2.0", n_tasks=50)],
            metrics=[MetricConfig(type="mean")],
        )
        yaml_str = cfg.to_harbor_yaml()
        data = yaml.safe_load(yaml_str)

        assert data["job_name"] == "full-test"
        assert data["environment"]["type"] == "docker"
        assert data["environment"]["force_build"] is True
        assert data["agents"][0]["kwargs"]["max_iterations"] == 30
        assert data["datasets"][0]["name"] == "terminal-bench"


class TestJobConfigFromYaml:
    """Test loading JobConfig from Harbor YAML file."""

    def test_from_yaml_string(self, tmp_path):
        yaml_content = """
job_name: loaded-job
n_attempts: 2
agents:
  - name: terminus-2
    model_name: hosted_vllm/my-model
datasets:
  - name: terminal-bench
    version: "2.0"
"""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml_content)

        cfg = JobConfig.from_yaml(str(yaml_file))
        assert cfg.job_name == "loaded-job"
        assert cfg.n_attempts == 2
        assert cfg.agents[0].name == "terminus-2"
        assert cfg.datasets[0].name == "terminal-bench"

    def test_from_yaml_with_sandbox_override(self, tmp_path):
        yaml_content = """
job_name: loaded-job
agents:
  - name: terminus-2
"""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml_content)

        cfg = JobConfig.from_yaml(str(yaml_file), setup_commands=["pip install harbor"])
        assert cfg.job_name == "loaded-job"
        assert cfg.setup_commands == ["pip install harbor"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/sdk/agent/test_job_config_serialization.py -v`
Expected: FAIL — `AttributeError: type object 'JobConfig' has no attribute 'to_harbor_yaml'`

- [ ] **Step 3: Implement to_harbor_yaml() and from_yaml()**

Add these methods to `JobConfig` in `rock/sdk/agent/models/job/config.py`:

```python
    # ── Rock extension field names (excluded from Harbor YAML) ──
    _rock_fields: ClassVar[set[str]] = {
        "sandbox", "setup_commands", "result_file",
        "collect_trajectory", "auto_start_sandbox", "auto_stop_sandbox",
    }

    def to_harbor_yaml(self) -> str:
        """Serialize Harbor-native fields to YAML string.

        Excludes Rock extension fields and None values so the output
        can be loaded by `harbor jobs start -c`.
        """
        import yaml

        data = self.model_dump(mode="json", exclude=self._rock_fields, exclude_none=True)
        return yaml.dump(data, default_flow_style=False, allow_unicode=True)

    @classmethod
    def from_yaml(cls, path: str, **overrides) -> "JobConfig":
        """Load JobConfig from a Harbor YAML config file.

        Args:
            path: Path to the YAML file.
            **overrides: Additional fields to set (e.g., sandbox, setup_commands).
        """
        import yaml

        with open(path) as f:
            data = yaml.safe_load(f)
        data.update(overrides)
        return cls(**data)
```

Also add `ClassVar` import at top of file:

```python
from typing import Any, ClassVar
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/sdk/agent/test_job_config_serialization.py -v`
Expected: PASS — all tests green

- [ ] **Step 5: Commit**

```bash
git add rock/sdk/agent/models/job/config.py tests/unit/sdk/agent/test_job_config_serialization.py
git commit -m "feat(agent): add JobConfig YAML serialization — to_harbor_yaml() and from_yaml()"
```

---

## Task 5: JobResult, TrialResult, JobStatus

**Files:**
- Create: `rock/sdk/agent/job.py`
- Create: `tests/unit/sdk/agent/test_job.py`

- [ ] **Step 1: Write failing tests for result models**

```python
# tests/unit/sdk/agent/test_job.py
import json

from rock.sdk.agent.job import JobResult, JobStatus, TrialResult


class TestJobStatus:
    def test_values(self):
        assert JobStatus.PENDING == "pending"
        assert JobStatus.RUNNING == "running"
        assert JobStatus.COMPLETED == "completed"
        assert JobStatus.FAILED == "failed"
        assert JobStatus.CANCELLED == "cancelled"


class TestTrialResult:
    def test_defaults(self):
        t = TrialResult(task_name="fix-bug", score=1.0)
        assert t.task_name == "fix-bug"
        assert t.score == 1.0
        assert t.status == JobStatus.COMPLETED
        assert t.rewards == {}
        assert t.trajectory_path is None
        assert t.token_ids == []
        assert t.duration_sec == 0.0
        assert t.error is None

    def test_failed_trial(self):
        t = TrialResult(
            task_name="fix-bug",
            score=0.0,
            status=JobStatus.FAILED,
            error="TimeoutError",
            duration_sec=300.0,
        )
        assert t.status == JobStatus.FAILED
        assert t.error == "TimeoutError"


class TestJobResult:
    def test_basic(self):
        r = JobResult(
            job_id="job-123",
            status=JobStatus.COMPLETED,
            trials=[
                TrialResult(task_name="t1", score=1.0),
                TrialResult(task_name="t2", score=0.5),
            ],
            raw_output="",
            exit_code=0,
        )
        assert r.job_id == "job-123"
        assert r.score == 0.75
        assert r.n_completed == 2
        assert r.n_failed == 0

    def test_score_with_failed_trials(self):
        r = JobResult(
            job_id="job-456",
            status=JobStatus.COMPLETED,
            trials=[
                TrialResult(task_name="t1", score=1.0),
                TrialResult(task_name="t2", score=0.0, status=JobStatus.FAILED, error="err"),
            ],
            raw_output="",
            exit_code=0,
        )
        assert r.score == 0.5
        assert r.n_completed == 1
        assert r.n_failed == 1

    def test_empty_trials(self):
        r = JobResult(job_id="job-789", status=JobStatus.FAILED, trials=[], raw_output="error", exit_code=1)
        assert r.score == 0.0
        assert r.n_completed == 0
        assert r.n_failed == 0


class TestParseHarborResult:
    """Test parsing Harbor result.json into JobResult."""

    def test_parse_result_json(self):
        harbor_result = {
            "job_name": "my-job",
            "stats": {"n_trials": 2, "n_errors": 0, "evals": {"tb": {"metrics": {"mean": 0.72}}}},
            "trial_results": [
                {
                    "trial_name": "trial-001",
                    "task_name": "fix-dockerfile",
                    "started_at": "2026-03-27T10:00:00Z",
                    "finished_at": "2026-03-27T10:05:30Z",
                    "verifier_result": {"rewards": {"reward": 1.0}},
                    "agent_result": {"n_input_tokens": 15000, "n_output_tokens": 3000},
                    "exception_info": None,
                },
                {
                    "trial_name": "trial-002",
                    "task_name": "fix-syntax",
                    "started_at": "2026-03-27T10:06:00Z",
                    "finished_at": "2026-03-27T10:08:00Z",
                    "verifier_result": {"rewards": {"reward": 0.0}},
                    "agent_result": {"n_input_tokens": 8000, "n_output_tokens": 1500},
                    "exception_info": None,
                },
            ],
        }
        result = JobResult.from_harbor_result(json.dumps(harbor_result), job_id="test-job")
        assert result.job_id == "test-job"
        assert result.status == JobStatus.COMPLETED
        assert len(result.trials) == 2
        assert result.trials[0].task_name == "fix-dockerfile"
        assert result.trials[0].score == 1.0
        assert result.trials[0].rewards == {"reward": 1.0}
        assert result.trials[1].score == 0.0

    def test_parse_result_with_error(self):
        harbor_result = {
            "job_name": "my-job",
            "stats": {"n_trials": 1, "n_errors": 1},
            "trial_results": [
                {
                    "trial_name": "trial-001",
                    "task_name": "fix-bug",
                    "started_at": "2026-03-27T10:00:00Z",
                    "finished_at": "2026-03-27T10:01:00Z",
                    "verifier_result": None,
                    "agent_result": None,
                    "exception_info": "AgentTimeoutError: agent timed out after 300s",
                },
            ],
        }
        result = JobResult.from_harbor_result(json.dumps(harbor_result), job_id="err-job")
        assert result.trials[0].status == JobStatus.FAILED
        assert result.trials[0].error == "AgentTimeoutError: agent timed out after 300s"
        assert result.trials[0].score == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/sdk/agent/test_job.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rock.sdk.agent.job'`

- [ ] **Step 3: Implement JobResult, TrialResult, JobStatus**

```python
# rock/sdk/agent/job.py
from __future__ import annotations

import json
import logging
from enum import Enum

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TrialResult(BaseModel):
    task_name: str
    status: JobStatus = JobStatus.COMPLETED
    score: float = 0.0
    rewards: dict[str, float] = Field(default_factory=dict)
    trajectory_path: str | None = None
    token_ids: list[int] = Field(default_factory=list)
    duration_sec: float = 0.0
    error: str | None = None


class JobResult(BaseModel):
    job_id: str
    status: JobStatus
    trials: list[TrialResult] = Field(default_factory=list)
    raw_output: str = ""
    exit_code: int = 0

    @property
    def score(self) -> float:
        if not self.trials:
            return 0.0
        return sum(t.score for t in self.trials) / len(self.trials)

    @property
    def n_completed(self) -> int:
        return sum(1 for t in self.trials if t.status == JobStatus.COMPLETED)

    @property
    def n_failed(self) -> int:
        return sum(1 for t in self.trials if t.status == JobStatus.FAILED)

    @classmethod
    def from_harbor_result(cls, result_json: str, job_id: str) -> "JobResult":
        """Parse Harbor result.json content into JobResult."""
        data = json.loads(result_json)
        trials = []
        for tr in data.get("trial_results", []):
            has_error = tr.get("exception_info") is not None
            verifier = tr.get("verifier_result") or {}
            rewards = verifier.get("rewards", {})
            score = rewards.get("reward", 0.0) if rewards else 0.0

            # Parse duration from timestamps
            duration_sec = 0.0
            if tr.get("started_at") and tr.get("finished_at"):
                from datetime import datetime

                try:
                    start = datetime.fromisoformat(tr["started_at"].replace("Z", "+00:00"))
                    end = datetime.fromisoformat(tr["finished_at"].replace("Z", "+00:00"))
                    duration_sec = (end - start).total_seconds()
                except (ValueError, TypeError):
                    pass

            # Extract token_ids from agent_result.rollout_details if present
            token_ids = []
            agent_result = tr.get("agent_result") or {}
            for detail in agent_result.get("rollout_details", []):
                token_ids.extend(detail.get("completion_token_ids", []))

            trials.append(
                TrialResult(
                    task_name=tr.get("task_name", ""),
                    status=JobStatus.FAILED if has_error else JobStatus.COMPLETED,
                    score=score if not has_error else 0.0,
                    rewards=rewards,
                    token_ids=token_ids,
                    duration_sec=duration_sec,
                    error=tr.get("exception_info"),
                )
            )

        status = JobStatus.COMPLETED if data.get("stats", {}).get("n_errors", 0) == 0 else JobStatus.COMPLETED
        return cls(job_id=job_id, status=status, trials=trials, raw_output=result_json, exit_code=0)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/sdk/agent/test_job.py -v`
Expected: PASS — all tests green

- [ ] **Step 5: Commit**

```bash
git add rock/sdk/agent/job.py tests/unit/sdk/agent/test_job.py
git commit -m "feat(agent): add JobResult, TrialResult, JobStatus with Harbor result parsing"
```

---

## Task 6: Job Class — run(), submit(), wait(), cancel()

**Files:**
- Modify: `rock/sdk/agent/job.py` (add Job class)
- Modify: `tests/unit/sdk/agent/test_job.py` (append)

- [ ] **Step 1: Write failing tests for Job.run()**

Append to `tests/unit/sdk/agent/test_job.py`:

```python
from unittest.mock import AsyncMock, MagicMock, patch

from rock.sdk.agent.job import Job
from rock.sdk.agent.models.job.config import DatasetConfig, JobConfig
from rock.sdk.agent.models.trial.config import AgentConfig


def _make_mock_sandbox():
    """Create a mock Sandbox with all required async methods."""
    sandbox = AsyncMock()
    sandbox.sandbox_id = "sb-123"
    sandbox.start = AsyncMock()
    sandbox.close = AsyncMock()
    sandbox.create_session = AsyncMock()
    sandbox.write_file = AsyncMock()

    # Default arun: returns successful observation
    obs = MagicMock()
    obs.output = ""
    obs.exit_code = 0
    obs.failure_reason = ""
    sandbox.arun = AsyncMock(return_value=obs)

    # start_nohup_process returns (pid, None) — success
    sandbox.start_nohup_process = AsyncMock(return_value=(12345, None))

    # wait_for_process_completion returns (True, "done")
    sandbox.wait_for_process_completion = AsyncMock(return_value=(True, "done"))

    # handle_nohup_output returns observation
    nohup_obs = MagicMock()
    nohup_obs.output = "harbor completed"
    nohup_obs.exit_code = 0
    nohup_obs.failure_reason = ""
    sandbox.handle_nohup_output = AsyncMock(return_value=nohup_obs)

    # read_file returns result.json content
    read_response = MagicMock()
    read_response.content = json.dumps({
        "job_name": "test",
        "stats": {"n_trials": 1, "n_errors": 0},
        "trial_results": [
            {
                "trial_name": "trial-001",
                "task_name": "t1",
                "started_at": "2026-03-27T10:00:00Z",
                "finished_at": "2026-03-27T10:05:00Z",
                "verifier_result": {"rewards": {"reward": 1.0}},
                "agent_result": {},
                "exception_info": None,
            }
        ],
    })
    sandbox.read_file = AsyncMock(return_value=read_response)

    return sandbox


class TestJobRun:
    async def test_run_full_lifecycle(self):
        sandbox = _make_mock_sandbox()
        config = JobConfig(
            auto_start_sandbox=False,
            agents=[AgentConfig(name="t2")],
            datasets=[DatasetConfig(name="tb", version="2.0")],
        )
        job = Job(config, sandbox=sandbox)
        result = await job.run()

        assert result.status == JobStatus.COMPLETED
        assert len(result.trials) == 1
        assert result.trials[0].score == 1.0

        # Verify config was uploaded
        sandbox.write_file.assert_called_once()

        # Verify harbor command was started via nohup
        sandbox.start_nohup_process.assert_called_once()
        cmd = sandbox.start_nohup_process.call_args[1]["cmd"]
        assert "harbor" in cmd
        assert ".yaml" in cmd or ".yml" in cmd

    async def test_run_with_setup_commands(self):
        sandbox = _make_mock_sandbox()
        config = JobConfig(
            auto_start_sandbox=False,
            setup_commands=["pip install harbor --quiet", "echo ready"],
        )
        job = Job(config, sandbox=sandbox)
        await job.run()

        # arun should have been called for each setup command
        arun_calls = [call for call in sandbox.arun.call_args_list]
        setup_cmds = [call[1].get("cmd", call[0][0] if call[0] else "") for call in arun_calls]
        assert "pip install harbor --quiet" in setup_cmds
        assert "echo ready" in setup_cmds

    async def test_run_auto_start_stop_sandbox(self):
        sandbox = _make_mock_sandbox()
        config = JobConfig(auto_start_sandbox=True, auto_stop_sandbox=True)
        job = Job(config, sandbox=sandbox)
        await job.run()

        sandbox.start.assert_called_once()
        sandbox.close.assert_called_once()

    async def test_run_skips_start_stop_when_disabled(self):
        sandbox = _make_mock_sandbox()
        config = JobConfig(auto_start_sandbox=False, auto_stop_sandbox=False)
        job = Job(config, sandbox=sandbox)
        await job.run()

        sandbox.start.assert_not_called()
        sandbox.close.assert_not_called()


class TestJobSubmitWait:
    async def test_submit_returns_job_id(self):
        sandbox = _make_mock_sandbox()
        config = JobConfig(auto_start_sandbox=False)
        job = Job(config, sandbox=sandbox)
        job_id = await job.submit()

        assert job_id is not None
        assert isinstance(job_id, str)
        sandbox.start_nohup_process.assert_called_once()

    async def test_wait_returns_result(self):
        sandbox = _make_mock_sandbox()
        config = JobConfig(auto_start_sandbox=False)
        job = Job(config, sandbox=sandbox)
        job_id = await job.submit()
        result = await job.wait(job_id)

        assert isinstance(result, JobResult)
        assert result.status == JobStatus.COMPLETED
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/sdk/agent/test_job.py::TestJobRun -v`
Expected: FAIL — `ImportError: cannot import name 'Job' from 'rock.sdk.agent.job'`

- [ ] **Step 3: Implement Job class**

Add to `rock/sdk/agent/job.py`, after `JobResult`:

```python
from rock.actions import CreateBashSessionRequest, ReadFileRequest, WriteFileRequest


class Job:
    """Execute Harbor benchmark jobs inside Rock sandboxes.

    Serializes JobConfig to YAML, uploads to sandbox, runs `harbor jobs start`
    via nohup, and collects results by reading result.json.
    """

    _config: "JobConfig"
    _sandbox: object | None
    _session: str | None
    _pid: int | None
    _tmp_file: str | None
    _config_path: str

    def __init__(self, config: "JobConfig", sandbox=None):
        from rock.sdk.agent.models.job.config import JobConfig as JC

        if not isinstance(config, JC):
            raise TypeError(f"config must be JobConfig, got {type(config)}")
        self._config = config
        self._sandbox = sandbox
        self._session = None
        self._pid = None
        self._tmp_file = None
        self._config_path = "/tmp/rock_job_config.yaml"

    async def _ensure_sandbox(self):
        """Create sandbox from config if not provided."""
        if self._sandbox is None:
            from rock.sdk.sandbox.client import Sandbox

            if self._config.sandbox is None:
                raise ValueError("Either pass sandbox= to Job() or set config.sandbox")
            self._sandbox = Sandbox(self._config.sandbox)

        if self._config.auto_start_sandbox:
            await self._sandbox.start()

    async def _setup_session(self):
        """Create bash session for job execution."""
        self._session = f"rock-job-{self._config.job_name}"
        await self._sandbox.create_session(CreateBashSessionRequest(session=self._session))

    async def _run_setup_commands(self):
        """Execute setup commands before harbor run."""
        for cmd in self._config.setup_commands:
            await self._sandbox.arun(cmd=cmd, session=self._session)

    async def _upload_config(self):
        """Serialize and upload Harbor config YAML to sandbox."""
        yaml_content = self._config.to_harbor_yaml()
        await self._sandbox.write_file(WriteFileRequest(content=yaml_content, path=self._config_path))

    async def _start_harbor(self) -> tuple[int, str]:
        """Start harbor jobs in nohup mode. Returns (pid, tmp_file)."""
        harbor_cmd = f"harbor jobs start -c {self._config_path}"
        tmp_file = f"/tmp/rock_job_{self._config.job_name}.out"

        pid, error = await self._sandbox.start_nohup_process(
            cmd=harbor_cmd, tmp_file=tmp_file, session=self._session
        )
        if error is not None:
            raise RuntimeError(f"Failed to start harbor job: {error.output}")
        return pid, tmp_file

    async def _collect_results(self, job_id: str) -> JobResult:
        """Read result.json from sandbox and parse into JobResult."""
        result_file = self._config.result_file
        if not result_file:
            result_file = f"{self._config.jobs_dir}/{self._config.job_name}/result.json"

        try:
            response = await self._sandbox.read_file(ReadFileRequest(path=result_file))
            return JobResult.from_harbor_result(response.content, job_id=job_id)
        except Exception as e:
            logger.warning(f"Failed to read result file {result_file}: {e}")
            return JobResult(
                job_id=job_id, status=JobStatus.FAILED, raw_output=str(e), exit_code=1
            )

    async def run(self) -> JobResult:
        """Execute the full job lifecycle: start → setup → harbor run → collect results."""
        try:
            await self._ensure_sandbox()
            await self._setup_session()
            await self._run_setup_commands()
            await self._upload_config()

            pid, tmp_file = await self._start_harbor()
            self._pid = pid
            self._tmp_file = tmp_file

            # Wait for completion
            success, message = await self._sandbox.wait_for_process_completion(
                pid=pid, session=self._session,
                wait_timeout=int(self._config.timeout_multiplier * 3600),
                wait_interval=10,
            )

            # Get output
            await self._sandbox.handle_nohup_output(
                tmp_file=tmp_file, session=self._session, success=success, message=message
            )

            job_id = f"{self._config.job_name}-{pid}"
            return await self._collect_results(job_id)

        finally:
            if self._config.auto_stop_sandbox and self._sandbox:
                await self._sandbox.close()

    async def submit(self) -> str:
        """Async submit — start harbor and return job_id immediately."""
        await self._ensure_sandbox()
        await self._setup_session()
        await self._run_setup_commands()
        await self._upload_config()

        pid, tmp_file = await self._start_harbor()
        self._pid = pid
        self._tmp_file = tmp_file

        return f"{self._config.job_name}-{pid}"

    async def wait(self, job_id: str) -> JobResult:
        """Wait for a submitted job to complete and return results."""
        if self._pid is None or self._tmp_file is None:
            raise RuntimeError("No submitted job to wait for. Call submit() first.")

        success, message = await self._sandbox.wait_for_process_completion(
            pid=self._pid, session=self._session,
            wait_timeout=int(self._config.timeout_multiplier * 3600),
            wait_interval=10,
        )

        await self._sandbox.handle_nohup_output(
            tmp_file=self._tmp_file, session=self._session, success=success, message=message
        )

        result = await self._collect_results(job_id)

        if self._config.auto_stop_sandbox and self._sandbox:
            await self._sandbox.close()

        return result

    async def cancel(self, job_id: str):
        """Cancel a running job by killing the process."""
        if self._pid is None:
            raise RuntimeError("No submitted job to cancel.")
        await self._sandbox.arun(cmd=f"kill {self._pid}", session=self._session)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/sdk/agent/test_job.py -v`
Expected: PASS — all tests green

- [ ] **Step 5: Commit**

```bash
git add rock/sdk/agent/job.py tests/unit/sdk/agent/test_job.py
git commit -m "feat(agent): implement Job class — run(), submit(), wait(), cancel()"
```

---

## Task 7: Module __init__.py Exports and Final Wiring

**Files:**
- Create: `rock/sdk/agent/__init__.py`
- Modify: `rock/sdk/agent/models/__init__.py` (add re-exports)

- [ ] **Step 1: Write failing test for public API imports**

Append to `tests/unit/sdk/agent/test_models.py`:

```python
class TestPublicAPI:
    def test_import_from_agent_package(self):
        from rock.sdk.agent import Job, JobResult, JobStatus, TrialResult

        assert Job is not None
        assert JobResult is not None
        assert JobStatus is not None
        assert TrialResult is not None

    def test_import_from_models_package(self):
        from rock.sdk.agent.models import (
            AgentConfig,
            ArtifactConfig,
            DatasetConfig,
            EnvironmentConfig,
            EnvironmentType,
            JobConfig,
            MetricConfig,
            MetricType,
            OrchestratorConfig,
            OrchestratorType,
            RetryConfig,
            TaskConfig,
            VerifierConfig,
        )

        assert JobConfig is not None
        assert AgentConfig is not None
        assert EnvironmentType is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/sdk/agent/test_models.py::TestPublicAPI -v`
Expected: FAIL — `ImportError: cannot import name 'Job' from 'rock.sdk.agent'`

- [ ] **Step 3: Implement __init__.py exports**

```python
# rock/sdk/agent/__init__.py
from rock.sdk.agent.job import Job, JobResult, JobStatus, TrialResult

__all__ = ["Job", "JobResult", "JobStatus", "TrialResult"]
```

```python
# rock/sdk/agent/models/__init__.py
from rock.sdk.agent.models.environment_type import EnvironmentType
from rock.sdk.agent.models.job.config import DatasetConfig, JobConfig, OrchestratorConfig, RetryConfig
from rock.sdk.agent.models.metric.config import MetricConfig
from rock.sdk.agent.models.metric.type import MetricType
from rock.sdk.agent.models.orchestrator_type import OrchestratorType
from rock.sdk.agent.models.trial.config import AgentConfig, ArtifactConfig, EnvironmentConfig, TaskConfig, VerifierConfig

__all__ = [
    "JobConfig",
    "OrchestratorConfig",
    "RetryConfig",
    "DatasetConfig",
    "AgentConfig",
    "EnvironmentConfig",
    "VerifierConfig",
    "TaskConfig",
    "ArtifactConfig",
    "MetricConfig",
    "MetricType",
    "OrchestratorType",
    "EnvironmentType",
]
```

- [ ] **Step 4: Run all tests to verify they pass**

Run: `uv run pytest tests/unit/sdk/agent/ -v`
Expected: PASS — all tests green

- [ ] **Step 5: Run lint**

Run: `uv run ruff check rock/sdk/agent/ tests/unit/sdk/agent/ --fix && uv run ruff format rock/sdk/agent/ tests/unit/sdk/agent/`

- [ ] **Step 6: Commit**

```bash
git add rock/sdk/agent/__init__.py rock/sdk/agent/models/__init__.py
git commit -m "feat(agent): wire up public API exports for rock.sdk.agent"
```

---

## Task 8: Final Verification — Full Test Suite

**Files:** None (verification only)

- [ ] **Step 1: Run all agent tests**

Run: `uv run pytest tests/unit/sdk/agent/ -v`
Expected: All tests PASS

- [ ] **Step 2: Run full unit test suite to ensure no regressions**

Run: `uv run pytest -m "not need_ray and not need_admin and not need_admin_and_network" --reruns 1 -x`
Expected: All tests PASS, no regressions

- [ ] **Step 3: Run lint on all new code**

Run: `uv run ruff check rock/sdk/agent/ tests/unit/sdk/agent/ && uv run ruff format --check rock/sdk/agent/ tests/unit/sdk/agent/`
Expected: No lint errors

- [ ] **Step 4: Final commit if any cleanup needed**

```bash
git add -A
git commit -m "chore(agent): final cleanup for Rock Job SDK"
```
