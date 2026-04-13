# Rock Job 重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 根据 docs/dev/job/README.md 架构设计，新建 `rock/sdk/job/` 模块（Job Facade + JobExecutor + Operator + Task），重构 `rock/cli/command/job.py` 使用新模块。`rock/sdk/agent/` 完全不动。

**Architecture:** Config 子类决定 Trial 类型 (BashJobConfig→BashTrial, HarborJobConfig→HarborTrial)。Operator.apply(config) 生成 TrialList，JobExecutor 并行执行。Job 是极薄 Facade。

**Tech Stack:** Python 3.10+, Pydantic v2, asyncio, pytest (asyncio_mode=auto), ruff (line-length=120)

---

### Task 1: Result Models

**Files:**
- Create: `rock/sdk/job/__init__.py` (empty)
- Create: `rock/sdk/job/trial/__init__.py` (empty)
- Create: `rock/sdk/job/result.py`
- Create: `tests/unit/sdk/job/__init__.py` (empty)
- Create: `tests/unit/sdk/job/test_result.py`

- [ ] **Step 1: Create directory structure**

```bash
mkdir -p rock/sdk/job/task tests/unit/sdk/job
touch rock/sdk/job/__init__.py rock/sdk/job/trial/__init__.py tests/unit/sdk/job/__init__.py
```

- [ ] **Step 2: Write failing test**

```python
# tests/unit/sdk/job/test_result.py
from rock.sdk.job.result import JobResult, JobStatus, TrialResult, TrialStatus


class TestTrialStatus:
    def test_values(self):
        assert TrialStatus.COMPLETED == "completed"
        assert TrialStatus.FAILED == "failed"
        assert TrialStatus.CANCELLED == "cancelled"

    def test_is_str(self):
        assert isinstance(TrialStatus.COMPLETED, str)


class TestTrialResult:
    def test_defaults(self):
        r = TrialResult()
        assert r.task_id == ""
        assert r.status == TrialStatus.COMPLETED
        assert r.output == ""
        assert r.exit_code == 0
        assert r.data == {}
        assert r.trial_results == []

    def test_success_when_completed(self):
        r = TrialResult(status=TrialStatus.COMPLETED)
        assert r.success is True

    def test_not_success_when_failed(self):
        r = TrialResult(status=TrialStatus.FAILED)
        assert r.success is False

    def test_score_empty(self):
        r = TrialResult()
        assert r.score == 0.0

    def test_score_with_trials(self):
        from rock.sdk.agent.models.trial.result import TrialResult, VerifierResult

        r = TrialResult(
            trial_results=[
                TrialResult(task_name="t1", verifier_result=VerifierResult(rewards={"reward": 1.0})),
                TrialResult(task_name="t2", verifier_result=VerifierResult(rewards={"reward": 0.5})),
            ]
        )
        assert r.score == 0.75


class TestJobStatus:
    def test_values(self):
        assert JobStatus.COMPLETED == "completed"
        assert JobStatus.FAILED == "failed"


class TestJobResult:
    def test_defaults(self):
        r = JobResult()
        assert r.job_id == ""
        assert r.status == JobStatus.COMPLETED
        assert r.labels == {}
        assert r.task_results == []

    def test_score_empty(self):
        assert JobResult().score == 0.0

    def test_score_with_tasks(self):
        r = JobResult(
            task_results=[
                TrialResult(trial_results=[]),
                TrialResult(trial_results=[]),
            ]
        )
        assert r.score == 0.0

    def test_n_completed_and_n_failed(self):
        r = JobResult(
            task_results=[
                TrialResult(status=TrialStatus.COMPLETED),
                TrialResult(status=TrialStatus.FAILED),
                TrialResult(status=TrialStatus.COMPLETED),
            ]
        )
        assert r.n_completed == 2
        assert r.n_failed == 1

    def test_trial_results_compat_property(self):
        """JobResult.trial_results returns first TrialResult's trial_results for Harbor compat."""
        from rock.sdk.agent.models.trial.result import TrialResult

        r = JobResult(
            task_results=[
                TrialResult(trial_results=[TrialResult(task_name="t1")]),
            ]
        )
        assert len(r.trial_results) == 1
        assert r.trial_results[0].task_name == "t1"

    def test_trial_results_empty(self):
        assert JobResult().trial_results == []
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/unit/sdk/job/test_result.py -x`
Expected: FAIL — `ModuleNotFoundError: No module named 'rock.sdk.job.result'`

- [ ] **Step 4: Write implementation**

```python
# rock/sdk/job/result.py
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from rock.sdk.agent.models.trial.result import TrialResult


class TrialStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TrialResult(BaseModel):
    """单个 Task 的执行结果"""

    task_id: str = ""
    status: TrialStatus = TrialStatus.COMPLETED
    output: str = ""
    exit_code: int = 0
    data: dict = Field(default_factory=dict)
    trial_results: list[TrialResult] = Field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.status == TrialStatus.COMPLETED

    @property
    def score(self) -> float:
        if not self.trial_results:
            return 0.0
        scores = [t.score for t in self.trial_results]
        return sum(scores) / len(scores)


class JobStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"


class JobResult(BaseModel):
    """Job 聚合结果"""

    job_id: str = ""
    status: JobStatus = JobStatus.COMPLETED
    labels: dict[str, str] = Field(default_factory=dict)
    task_results: list[TrialResult] = Field(default_factory=list)

    @property
    def score(self) -> float:
        if not self.task_results:
            return 0.0
        scores = [t.score for t in self.task_results]
        return sum(scores) / len(scores)

    @property
    def n_completed(self) -> int:
        return sum(1 for t in self.task_results if t.status == TrialStatus.COMPLETED)

    @property
    def n_failed(self) -> int:
        return sum(1 for t in self.task_results if t.status == TrialStatus.FAILED)

    @property
    def trial_results(self) -> list[TrialResult]:
        """便捷属性: 直接访问第一个 TrialResult 的 trial_results (Harbor 兼容)"""
        if self.task_results:
            return self.task_results[0].trial_results
        return []
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/sdk/job/test_result.py -x -v`
Expected: ALL PASS

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check rock/sdk/job/result.py tests/unit/sdk/job/test_result.py --fix
uv run ruff format rock/sdk/job/result.py tests/unit/sdk/job/test_result.py
git add rock/sdk/job/ tests/unit/sdk/job/
git commit -m "feat(job): add result models (TrialResult, JobResult)"
```

---

### Task 2: Config Hierarchy

**Files:**
- Create: `rock/sdk/job/config.py`
- Create: `tests/unit/sdk/job/test_config.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/sdk/job/test_config.py
import tempfile
from pathlib import Path

from rock.sdk.agent.models.trial.config import AgentConfig, RockEnvironmentConfig, VerifierConfig
from rock.sdk.job.config import BashJobConfig, HarborJobConfig, JobConfig


class TestJobConfig:
    def test_defaults(self):
        c = JobConfig()
        assert c.job_name is None
        assert c.timeout == 3600
        assert c.env == {}
        assert c.setup_commands == []
        assert c.file_uploads == []
        assert c.auto_stop is False
        assert c.labels == {}

    def test_custom_values(self):
        c = JobConfig(
            job_name="test",
            timeout=60,
            env={"K": "V"},
            setup_commands=["echo hi"],
            auto_stop=True,
        )
        assert c.job_name == "test"
        assert c.timeout == 60
        assert c.env == {"K": "V"}
        assert c.auto_stop is True


class TestBashJobConfig:
    def test_inherits_job_config(self):
        assert issubclass(BashJobConfig, JobConfig)

    def test_bash_fields(self):
        c = BashJobConfig(script="echo hello")
        assert c.script == "echo hello"
        assert c.script_path is None

    def test_script_path(self):
        c = BashJobConfig(script_path="/tmp/test.sh")
        assert c.script_path == "/tmp/test.sh"


class TestHarborJobConfig:
    def test_inherits_job_config(self):
        assert issubclass(HarborJobConfig, JobConfig)

    def test_harbor_fields(self):
        c = HarborJobConfig(
            agents=[AgentConfig(name="t2")],
            n_attempts=3,
        )
        assert c.agents[0].name == "t2"
        assert c.n_attempts == 3

    def test_to_harbor_yaml(self):
        c = HarborJobConfig(
            agents=[AgentConfig(name="t2", model_name="gpt-4")],
            setup_commands=["pip install harbor"],
            env={"KEY": "VAL"},
        )
        yaml_str = c.to_harbor_yaml()
        assert "agents" in yaml_str
        assert "t2" in yaml_str
        # Rock-level fields should be excluded from Harbor YAML
        assert "setup_commands" not in yaml_str
        assert "timeout" not in yaml_str

    def test_from_yaml(self):
        import yaml

        data = {
            "agents": [{"name": "t2"}],
            "n_attempts": 2,
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            path = f.name

        c = HarborJobConfig.from_yaml(path)
        assert c.agents[0].name == "t2"
        assert c.n_attempts == 2
        Path(path).unlink()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/sdk/job/test_config.py -x`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
# rock/sdk/job/config.py
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from rock.sdk.agent.constants import USER_DEFINED_LOGS
from rock.sdk.agent.models.trial.config import (
    AgentConfig,
    ArtifactConfig,
    RockEnvironmentConfig,
    TaskConfig,
    VerifierConfig,
)


class JobConfig(BaseModel):
    """Job 基础配置 — 所有 Job 类型共享的字段"""

    environment: RockEnvironmentConfig = Field(default_factory=RockEnvironmentConfig)
    job_name: str | None = None
    namespace: str | None = None
    experiment_id: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    auto_stop: bool = False
    setup_commands: list[str] = Field(default_factory=list)
    file_uploads: list[tuple[str, str]] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    timeout: int = 3600


class BashJobConfig(JobConfig):
    """Bash 脚本 Job 配置"""

    script: str | None = None
    script_path: str | None = None


class HarborJobConfig(JobConfig):
    """Harbor benchmark Job 配置"""

    agents: list[AgentConfig] = Field(default_factory=lambda: [AgentConfig()])
    datasets: list = Field(default_factory=list)
    orchestrator: dict[str, Any] = Field(default_factory=dict)
    verifier: VerifierConfig = Field(default_factory=VerifierConfig)
    tasks: list[TaskConfig] = Field(default_factory=list)
    metrics: list = Field(default_factory=list)
    artifacts: list[str | ArtifactConfig] = Field(default_factory=list)
    n_attempts: int = 1
    timeout_multiplier: float = 1.0
    agent_timeout_multiplier: float | None = None
    verifier_timeout_multiplier: float | None = None
    jobs_dir: Path = Path(USER_DEFINED_LOGS) / "jobs"
    debug: bool = False

    # Rock-level fields to exclude when serializing to Harbor YAML
    _ROCK_FIELDS: set[str] = {
        "environment", "job_name", "namespace", "experiment_id", "labels",
        "auto_stop", "setup_commands", "file_uploads", "env", "timeout",
    }

    def to_harbor_yaml(self) -> str:
        """Serialize Harbor-native fields to YAML for harbor jobs start -c"""
        import yaml

        data = self.model_dump(mode="json", exclude=self._ROCK_FIELDS, exclude_none=True)
        harbor_env = self.environment.to_harbor_environment()
        if harbor_env:
            data["environment"] = harbor_env
        return yaml.dump(data, default_flow_style=False, allow_unicode=True)

    @classmethod
    def from_yaml(cls, path: str) -> HarborJobConfig:
        """Load from Harbor YAML config file"""
        import yaml

        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/sdk/job/test_config.py -x -v`
Expected: ALL PASS

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check rock/sdk/job/config.py tests/unit/sdk/job/test_config.py --fix
uv run ruff format rock/sdk/job/config.py tests/unit/sdk/job/test_config.py
git add rock/sdk/job/config.py tests/unit/sdk/job/test_config.py
git commit -m "feat(job): add config hierarchy (JobConfig, BashJobConfig, HarborJobConfig)"
```

---

### Task 3: Task Interface + Registry

**Files:**
- Create: `rock/sdk/job/trial/abstract.py`
- Create: `rock/sdk/job/trial/registry.py`
- Create: `tests/unit/sdk/job/test_trial_registry.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/sdk/job/test_trial_registry.py
import pytest

from rock.sdk.job.config import JobConfig
from rock.sdk.job.result import TrialResult, TrialStatus
from rock.sdk.job.trial.abstract import AbstractTrial
from rock.sdk.job.trial.registry import _create_trial, register_trial


class _StubConfig(JobConfig):
    stub_field: str = "test"


class _StubTask(AbstractTrial):
    async def setup(self, sandbox):
        pass

    def build(self) -> str:
        return "echo stub"

    async def collect(self, sandbox, output, exit_code):
        return TrialResult(task_id="stub", output=output, exit_code=exit_code)


class TestAbstractTrial:
    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            AbstractTrial(JobConfig())

    def test_concrete_subclass(self):
        config = _StubConfig()
        task = _StubTask(config)
        assert task._config is config


class TestRegistry:
    def test_create_unregistered_raises(self):
        class _UnknownConfig(JobConfig):
            pass

        with pytest.raises(TypeError, match="No task registered"):
            _create_trial(_UnknownConfig())

    def test_register_and_create(self):
        register_trial(_StubConfig, _StubTask)
        trial = _create_trial(_StubConfig())
        assert isinstance(task, _StubTask)
        assert task._config.stub_field == "test"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/sdk/job/test_trial_registry.py -x`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
# rock/sdk/job/trial/abstract.py
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rock.sdk.job.config import JobConfig
    from rock.sdk.job.result import TrialResult
    from rock.sdk.sandbox.client import Sandbox


class AbstractTrial(ABC):
    """Trial 基类: 在 Sandbox 中执行一个任务的三阶段接口"""

    def __init__(self, config: JobConfig):
        self._config = config

    @abstractmethod
    async def setup(self, sandbox: Sandbox) -> None:
        """执行前: 准备 sandbox 环境"""

    @abstractmethod
    def build(self) -> str:
        """构建: 生成要执行的 bash 脚本"""

    @abstractmethod
    async def collect(self, sandbox: Sandbox, output: str, exit_code: int) -> TrialResult:
        """执行后: 收集并解析结果"""

    async def _upload_files(self, sandbox: Sandbox) -> None:
        """共享: 上传 config.file_uploads"""
        for local_path, sandbox_path in self._config.file_uploads:
            await sandbox.fs.upload_dir(local_path, sandbox_path)
```

```python
# rock/sdk/job/trial/registry.py
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rock.sdk.job.config import JobConfig
    from rock.sdk.job.trial.abstract import AbstractTrial

_TRIAL_REGISTRY: dict[type, type] = {}


def register_trial(config_type: type, task_type: type) -> None:
    """注册 Config → Task 映射"""
    _TRIAL_REGISTRY[config_type] = task_type


def _create_trial(config: JobConfig) -> AbstractTrial:
    """根据 config 类型创建对应的 Task 实例"""
    task_cls = _TRIAL_REGISTRY.get(type(config))
    if task_cls is None:
        raise TypeError(
            f"No task registered for {type(config).__name__}. "
            f"Supported: {[c.__name__ for c in _TRIAL_REGISTRY]}"
        )
    return task_cls(config)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/sdk/job/test_trial_registry.py -x -v`
Expected: ALL PASS

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check rock/sdk/job/trial/ tests/unit/sdk/job/test_trial_registry.py --fix
uv run ruff format rock/sdk/job/trial/ tests/unit/sdk/job/test_trial_registry.py
git add rock/sdk/job/trial/ tests/unit/sdk/job/test_trial_registry.py
git commit -m "feat(job): add AbstractTrial and task registry"
```

---

### Task 4: BashTrial

**Files:**
- Create: `rock/sdk/job/trial/bash.py`
- Create: `tests/unit/sdk/job/test_trial_bash.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/sdk/job/test_trial_bash.py
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

from rock.sdk.job.config import BashJobConfig
from rock.sdk.job.result import TrialStatus
from rock.sdk.job.trial.bash import BashTrial
from rock.sdk.job.trial.registry import _create_trial


class TestBashTrialBuild:
    def test_basic_script(self):
        config = BashJobConfig(script="echo hello")
        task = BashTrial(config)
        script = trial.build()
        assert script.startswith("#!/bin/bash")
        assert "set -e" in script
        assert "echo hello" in script

    def test_with_setup_commands(self):
        config = BashJobConfig(
            script="python main.py",
            setup_commands=["pip install -r requirements.txt", "export FOO=bar"],
        )
        task = BashTrial(config)
        script = trial.build()
        assert "pip install -r requirements.txt" in script
        assert "export FOO=bar" in script
        assert "python main.py" in script
        # setup commands should appear before the main script
        assert script.index("pip install") < script.index("python main.py")

    def test_no_setup_commands(self):
        config = BashJobConfig(script="ls")
        task = BashTrial(config)
        script = trial.build()
        assert "#!/bin/bash" in script
        assert "ls" in script


class TestBashTrialSetup:
    async def test_upload_files(self):
        config = BashJobConfig(
            script="echo hi",
            file_uploads=[("/local/a", "/sandbox/a"), ("/local/b", "/sandbox/b")],
        )
        task = BashTrial(config)
        mock_sandbox = AsyncMock()
        mock_sandbox.fs.upload_dir = AsyncMock()

        await trial.setup(mock_sandbox)

        assert mock_sandbox.fs.upload_dir.call_count == 2
        mock_sandbox.fs.upload_dir.assert_any_call("/local/a", "/sandbox/a")
        mock_sandbox.fs.upload_dir.assert_any_call("/local/b", "/sandbox/b")

    async def test_reads_script_path(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
            f.write("echo from file")
            script_file = f.name

        try:
            config = BashJobConfig(script_path=script_file)
            task = BashTrial(config)
            mock_sandbox = AsyncMock()
            await trial.setup(mock_sandbox)
            assert config.script == "echo from file"
        finally:
            Path(script_file).unlink()


class TestBashTrialCollect:
    async def test_success(self):
        config = BashJobConfig(script="echo hi", job_name="test-job")
        task = BashTrial(config)
        result = await trial.collect(AsyncMock(), "output text", 0)
        assert result.status == TrialStatus.COMPLETED
        assert result.output == "output text"
        assert result.exit_code == 0
        assert result.task_id == "test-job"

    async def test_failure(self):
        config = BashJobConfig(script="exit 1", job_name="fail-job")
        task = BashTrial(config)
        result = await trial.collect(AsyncMock(), "error", 1)
        assert result.status == TrialStatus.FAILED
        assert result.exit_code == 1


class TestBashTrialRegistration:
    def test_registered_in_registry(self):
        trial = _create_trial(BashJobConfig(script="echo test"))
        assert isinstance(task, BashTrial)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/sdk/job/test_trial_bash.py -x`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
# rock/sdk/job/trial/bash.py
from __future__ import annotations

from pathlib import Path

from rock.sdk.job.config import BashJobConfig
from rock.sdk.job.result import TrialResult, TrialStatus
from rock.sdk.job.trial.abstract import AbstractTrial
from rock.sdk.job.trial.registry import register_trial


class BashTrial(AbstractTrial):
    """Bash 脚本执行"""

    _config: BashJobConfig

    async def setup(self, sandbox) -> None:
        await self._upload_files(sandbox)
        if self._config.script_path:
            self._config.script = Path(self._config.script_path).read_text()

    def build(self) -> str:
        lines = ["#!/bin/bash", "set -e", ""]
        if self._config.setup_commands:
            for cmd in self._config.setup_commands:
                lines.append(f"echo '>>> {cmd[:60]}...'")
                lines.append(cmd)
            lines.append("")
        if self._config.script:
            lines.append(self._config.script)
        return "\n".join(lines)

    async def collect(self, sandbox, output: str, exit_code: int) -> TrialResult:
        return TrialResult(
            task_id=self._config.job_name or "",
            status=TrialStatus.COMPLETED if exit_code == 0 else TrialStatus.FAILED,
            output=output,
            exit_code=exit_code,
        )


register_trial(BashJobConfig, BashTrial)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/sdk/job/test_trial_bash.py -x -v`
Expected: ALL PASS

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check rock/sdk/job/trial/bash.py tests/unit/sdk/job/test_trial_bash.py --fix
uv run ruff format rock/sdk/job/trial/bash.py tests/unit/sdk/job/test_trial_bash.py
git add rock/sdk/job/trial/bash.py tests/unit/sdk/job/test_trial_bash.py
git commit -m "feat(job): add BashTrial"
```

---

### Task 5: Operator

**Files:**
- Create: `rock/sdk/job/operator.py`
- Create: `tests/unit/sdk/job/test_operator.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/sdk/job/test_operator.py
import pytest

from rock.sdk.job.config import BashJobConfig
from rock.sdk.job.operator import Operator, ScatterOperator
from rock.sdk.job.trial.bash import BashTrial


class TestOperatorABC:
    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            Operator()


class TestScatterOperator:
    def test_default_size_1(self):
        op = ScatterOperator()
        assert op.size == 1

    def test_apply_returns_one_task(self):
        op = ScatterOperator()
        config = BashJobConfig(script="echo hi")
        tasks = op.apply(config)
        assert len(tasks) == 1
        assert isinstance(tasks[0], BashTrial)

    def test_apply_returns_n_tasks(self):
        op = ScatterOperator(size=3)
        config = BashJobConfig(script="echo hi")
        tasks = op.apply(config)
        assert len(tasks) == 3
        for t in tasks:
            assert isinstance(t, BashTrial)

    def test_size_zero_returns_empty(self):
        op = ScatterOperator(size=0)
        config = BashJobConfig(script="echo hi")
        tasks = op.apply(config)
        assert tasks == []

    def test_size_negative_returns_empty(self):
        op = ScatterOperator(size=-1)
        config = BashJobConfig(script="echo hi")
        tasks = op.apply(config)
        assert tasks == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/sdk/job/test_operator.py -x`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
# rock/sdk/job/operator.py
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from rock.sdk.job.trial.registry import _create_trial

if TYPE_CHECKING:
    from rock.sdk.job.config import JobConfig
    from rock.sdk.job.trial.abstract import AbstractTrial


class Operator(ABC):
    """算子基类: 从 config 生成 TrialList"""

    @abstractmethod
    def apply(self, config: JobConfig) -> list[AbstractTrial]:
        """从 config 生成 TrialList, 返回空 list 表示什么都不做"""
        ...


class ScatterOperator(Operator):
    """Scatter 算子: 将 config 分发 8 份 Trial"""

    def __init__(self, size: int = 1):
        self.size = size

    def apply(self, config: JobConfig) -> list[AbstractTrial]:
        if self.size <= 0:
            return []
        trial = _create_trial(config)
        return [trial] * self.size
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/sdk/job/test_operator.py -x -v`
Expected: ALL PASS

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check rock/sdk/job/operator.py tests/unit/sdk/job/test_operator.py --fix
uv run ruff format rock/sdk/job/operator.py tests/unit/sdk/job/test_operator.py
git add rock/sdk/job/operator.py tests/unit/sdk/job/test_operator.py
git commit -m "feat(job): add Operator ABC and ScatterOperator"
```

---

### Task 6: JobExecutor

**Files:**
- Create: `rock/sdk/job/executor.py`
- Create: `tests/unit/sdk/job/test_executor.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/sdk/job/test_executor.py
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.sdk.job.config import BashJobConfig
from rock.sdk.job.executor import JobClient, JobExecutor, TrialClient
from rock.sdk.job.operator import ScatterOperator
from rock.sdk.job.result import TrialStatus


def _make_mock_sandbox():
    sandbox = AsyncMock()
    sandbox.sandbox_id = "sb-123"
    sandbox._namespace = None
    sandbox._experiment_id = None
    sandbox.start = AsyncMock()
    sandbox.close = AsyncMock()
    sandbox.create_session = AsyncMock()
    sandbox.write_file_by_path = AsyncMock(return_value=MagicMock(success=True))
    sandbox.fs = AsyncMock()
    sandbox.fs.upload_dir = AsyncMock()
    sandbox.start_nohup_process = AsyncMock(return_value=(12345, None))
    sandbox.wait_for_process_completion = AsyncMock(return_value=(True, "done"))
    nohup_obs = MagicMock()
    nohup_obs.output = "hello output"
    nohup_obs.exit_code = 0
    sandbox.handle_nohup_output = AsyncMock(return_value=nohup_obs)
    return sandbox


class TestJobExecutorRun:
    async def test_run_bash_success(self):
        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            executor = JobExecutor()
            operator = ScatterOperator(size=1)
            config = BashJobConfig(script="echo hello", job_name="test")
            results = await executor.run(operator, config)

            assert len(results) == 1
            assert results[0].status == TrialStatus.COMPLETED
            assert results[0].output == "hello output"
            mock_sandbox.start.assert_called_once()

    async def test_run_empty_operator(self):
        executor = JobExecutor()
        operator = ScatterOperator(size=0)
        config = BashJobConfig(script="echo hi")
        results = await executor.run(operator, config)
        assert results == []


class TestJobExecutorSubmitWait:
    async def test_submit_returns_job_client(self):
        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            executor = JobExecutor()
            operator = ScatterOperator(size=1)
            config = BashJobConfig(script="echo hello", job_name="test")
            job_client = await executor.submit(operator, config)

            assert isinstance(job_client, JobClient)
            assert len(job_client.tasks) == 1
            assert isinstance(job_client.tasks[0], TrialClient)

    async def test_wait_returns_results(self):
        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            executor = JobExecutor()
            operator = ScatterOperator(size=1)
            config = BashJobConfig(script="echo hello", job_name="test")
            job_client = await executor.submit(operator, config)
            results = await executor.wait(job_client)

            assert len(results) == 1
            assert results[0].status == TrialStatus.COMPLETED


class TestJobExecutorAutoStop:
    async def test_auto_stop_closes_sandbox(self):
        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            executor = JobExecutor()
            config = BashJobConfig(script="echo hi", job_name="test", auto_stop=True)
            await executor.run(ScatterOperator(), config)
            mock_sandbox.close.assert_called_once()

    async def test_no_auto_stop(self):
        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            executor = JobExecutor()
            config = BashJobConfig(script="echo hi", job_name="test", auto_stop=False)
            await executor.run(ScatterOperator(), config)
            mock_sandbox.close.assert_not_called()


class TestJobExecutorFailure:
    async def test_process_failure(self):
        mock_sandbox = _make_mock_sandbox()
        mock_sandbox.wait_for_process_completion = AsyncMock(return_value=(False, "timeout"))
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            executor = JobExecutor()
            config = BashJobConfig(script="echo hi", job_name="test")
            results = await executor.run(ScatterOperator(), config)
            assert results[0].status == TrialStatus.FAILED

    async def test_nohup_start_error(self):
        mock_sandbox = _make_mock_sandbox()
        error_obs = MagicMock()
        error_obs.output = "command not found"
        mock_sandbox.start_nohup_process = AsyncMock(return_value=(None, error_obs))
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            executor = JobExecutor()
            config = BashJobConfig(script="echo hi", job_name="test")
            with pytest.raises(RuntimeError, match="Failed to start task"):
                await executor.run(ScatterOperator(), config)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/sdk/job/test_executor.py -x`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
# rock/sdk/job/executor.py
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rock.actions import CreateBashSessionRequest
from rock.logger import init_logger
from rock.sdk.job.operator import Operator
from rock.sdk.job.result import TrialResult, TrialStatus
from rock.sdk.sandbox.client import Sandbox

if TYPE_CHECKING:
    from rock.sdk.job.config import JobConfig
    from rock.sdk.job.trial.abstract import AbstractTrial

logger = init_logger(__name__)


@dataclass
class TrialClient:
    """单个运行中 task 的句柄"""

    sandbox: Sandbox
    session: str
    pid: int
    trial: AbstractTrial


@dataclass
class JobClient:
    """Job.submit() 返回的句柄，持有多个 TrialClient"""

    tasks: list[TrialClient]


class JobExecutor:
    """执行引擎: 驱动 Operator 生成 TrialList，并行执行"""

    async def run(self, operator: Operator, config: JobConfig) -> list[TrialResult]:
        """完整生命周期: submit + wait"""
        job_client = await self.submit(operator, config)
        return await self.wait(job_client)

    async def submit(self, operator: Operator, config: JobConfig) -> JobClient:
        """Operator apply 生成 TrialList，并行启动所有 sandbox"""
        trial_list = operator.apply(config)
        if not trial_list:
            return JobClient(tasks=[])
        task_clients = await asyncio.gather(*[self._(do_submit(trial) for trial in trial_list])
        return JobClient(tasks=list(task_clients))

    async def wait(self, job_client: JobClient) -> list[TrialResult]:
        """并行等待所有 task 完成，收集结果"""
        if not job_client.tasks:
            return []
        return list(await asyncio.gather(*[self._do_wait(tc) for tc in job_client.tasks]))

    async def _do_submit(self, trial: AbstractTrial) -> TrialClient:
        """启动单个 sandbox + 执行脚本"""
        config = task._config

        sandbox = Sandbox(config.environment)
        await sandbox.start()
        logger.info(f"Sandbox started: sandbox_id={sandbox.sandbox_id}, job_name={config.job_name}")

        session = f"rock-job-{config.job_name or 'default'}"
        env = self._build_session_env(config)
        await sandbox.create_session(
            CreateBashSessionRequest(session=session, env_enable=True, env=env)
        )

        await trial.setup(sandbox)
        script_content = trial.build()

        script_path = f"/tmp/rock_job_{config.job_name or 'default'}.sh"
        await sandbox.write_file_by_path(script_content, script_path)

        tmp_file = f"/tmp/rock_job_{config.job_name or 'default'}.out"
        pid, error = await sandbox.start_nohup_process(
            cmd=f"bash {script_path}", tmp_file=tmp_file, session=session,
        )
        if error is not None:
            raise RuntimeError(f"Failed to start task: {error.output}")

        logger.info(f"Task started: pid={pid}, job_name={config.job_name}")
        return TrialClient(sandbox=sandbox, session=session, pid=pid, task=task)

    async def _do_wait(self, client: TrialClient) -> TrialResult:
        """等待单个 task 完成，收集结果"""
        config = client.trial._config
        try:
            success, message = await client.sandbox.wait_for_process_completion(
                pid=client.pid,
                session=client.session,
                wait_timeout=config.timeout,
                wait_interval=30,
            )
            obs = await client.sandbox.handle_nohup_output(
                tmp_file=f"/tmp/rock_job_{config.job_name or 'default'}.out",
                session=client.session,
                success=success,
                message=message,
                ignore_output=False,
                response_limited_bytes_in_nohup=None,
            )
            result = await client.trial.collect(client.sandbox, obs.output or "", obs.exit_code or 1)
            if not success:
                result.status = TrialStatus.FAILED
            return result
        finally:
            if config.auto_stop:
                await client.sandbox.close()

    @staticmethod
    def _build_session_env(config: JobConfig) -> dict[str, str] | None:
        """Merge OSS_* from process env with config.env"""
        oss_env = {k: v for k, v in os.environ.items() if k.startswith("OSS")}
        merged = {**oss_env, **config.env}
        return merged or None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/sdk/job/test_executor.py -x -v`
Expected: ALL PASS

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check rock/sdk/job/executor.py tests/unit/sdk/job/test_executor.py --fix
uv run ruff format rock/sdk/job/executor.py tests/unit/sdk/job/test_executor.py
git add rock/sdk/job/executor.py tests/unit/sdk/job/test_executor.py
git commit -m "feat(job): add JobExecutor with TrialClient/JobClient"
```

---

### Task 7: Job Facade

**Files:**
- Create: `rock/sdk/job/job.py`
- Create: `tests/unit/sdk/job/test_job.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/sdk/job/test_job.py
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.sdk.job.config import BashJobConfig
from rock.sdk.job.job import Job
from rock.sdk.job.operator import ScatterOperator
from rock.sdk.job.result import JobStatus, TrialResult, TrialStatus


def _make_mock_sandbox():
    sandbox = AsyncMock()
    sandbox.sandbox_id = "sb-test"
    sandbox._namespace = None
    sandbox._experiment_id = None
    sandbox.start = AsyncMock()
    sandbox.close = AsyncMock()
    sandbox.create_session = AsyncMock()
    sandbox.write_file_by_path = AsyncMock(return_value=MagicMock(success=True))
    sandbox.fs = AsyncMock()
    sandbox.arun = AsyncMock()
    sandbox.start_nohup_process = AsyncMock(return_value=(99, None))
    sandbox.wait_for_process_completion = AsyncMock(return_value=(True, "done"))
    obs = MagicMock()
    obs.output = "ok"
    obs.exit_code = 0
    sandbox.handle_nohup_output = AsyncMock(return_value=obs)
    return sandbox


class TestJobRun:
    async def test_run_returns_job_result(self):
        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            job = Job(BashJobConfig(script="echo hi", job_name="test"))
            result = await job.run()
            assert result.status == JobStatus.COMPLETED
            assert len(result.task_results) == 1

    async def test_run_failed(self):
        mock_sandbox = _make_mock_sandbox()
        mock_sandbox.wait_for_process_completion = AsyncMock(return_value=(False, "fail"))
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            job = Job(BashJobConfig(script="exit 1", job_name="test"))
            result = await job.run()
            assert result.status == JobStatus.FAILED


class TestJobSubmitWait:
    async def test_submit_then_wait(self):
        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            job = Job(BashJobConfig(script="echo hi", job_name="test"))
            await job.submit()
            result = await job.wait()
            assert result.status == JobStatus.COMPLETED

    async def test_wait_without_submit_raises(self):
        job = Job(BashJobConfig(script="echo hi"))
        with pytest.raises(RuntimeError, match="No submitted job"):
            await job.wait()


class TestJobCancel:
    async def test_cancel(self):
        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            job = Job(BashJobConfig(script="sleep 100", job_name="test"))
            await job.submit()
            await job.cancel()
            mock_sandbox.arun.assert_called_once()
            assert "kill" in str(mock_sandbox.arun.call_args)


class TestJobWithOperator:
    async def test_custom_scatter_size(self):
        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            job = Job(
                BashJobConfig(script="echo hi", job_name="test"),
                operator=ScatterOperator(size=2),
            )
            result = await job.run()
            assert len(result.task_results) == 2

    async def test_default_operator(self):
        mock_sandbox = _make_mock_sandbox()
        with patch("rock.sdk.job.executor.Sandbox", return_value=mock_sandbox):
            job = Job(BashJobConfig(script="echo hi", job_name="test"))
            result = await job.run()
            assert len(result.task_results) == 1  # default ScatterOperator(size=1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/sdk/job/test_job.py -x`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
# rock/sdk/job/job.py
from __future__ import annotations

from typing import TYPE_CHECKING

from rock.sdk.job.executor import JobExecutor
from rock.sdk.job.operator import ScatterOperator
from rock.sdk.job.result import JobResult, JobStatus

if TYPE_CHECKING:
    from rock.sdk.job.config import JobConfig
    from rock.sdk.job.executor import JobClient
    from rock.sdk.job.operator import Operator
    from rock.sdk.job.result import TrialResult


class Job:
    """Job Facade: 极薄入口层"""

    def __init__(self, config: JobConfig, operator: Operator | None = None):
        self._config = config
        self._executor = JobExecutor()
        self._operator = operator or ScatterOperator()
        self._job_client: JobClient | None = None

    async def run(self) -> JobResult:
        """完整生命周期: submit + wait"""
        await self.submit()
        return await self.wait()

    async def submit(self) -> None:
        """非阻塞提交"""
        self._job_client = await self._executor.submit(self._operator, self._config)

    async def wait(self) -> JobResult:
        """等待完成"""
        if not self._job_client:
            raise RuntimeError("No submitted job. Call submit() first.")
        task_results = await self._executor.wait(self._job_client)
        return self._build_result(task_results)

    async def cancel(self) -> None:
        """取消运行中的 job"""
        if self._job_client:
            for tc in self._job_client.tasks:
                await tc.sandbox.arun(cmd=f"kill {tc.pid}", session=tc.session)

    def _build_result(self, task_results: list[TrialResult]) -> JobResult:
        all_success = all(r.success for r in task_results)
        return JobResult(
            job_id=self._config.job_name or "",
            status=JobStatus.COMPLETED if all_success else JobStatus.FAILED,
            labels=self._config.labels,
            task_results=task_results,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/sdk/job/test_job.py -x -v`
Expected: ALL PASS

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check rock/sdk/job/job.py tests/unit/sdk/job/test_job.py --fix
uv run ruff format rock/sdk/job/job.py tests/unit/sdk/job/test_job.py
git add rock/sdk/job/job.py tests/unit/sdk/job/test_job.py
git commit -m "feat(job): add Job facade"
```

---

### Task 8: HarborTrial

**Files:**
- Create: `rock/sdk/job/trial/harbor.py`
- Create: `tests/unit/sdk/job/test_trial_harbor.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/sdk/job/test_trial_harbor.py
import json
from unittest.mock import AsyncMock, MagicMock

from rock.sdk.agent.models.trial.config import AgentConfig
from rock.sdk.job.config import HarborJobConfig
from rock.sdk.job.result import TrialStatus
from rock.sdk.job.trial.harbor import HarborTrial
from rock.sdk.job.trial.registry import _create_trial


class TestHarborTrialBuild:
    def test_contains_harbor_command(self):
        config = HarborJobConfig(job_name="test")
        task = HarborTrial(config)
        script = trial.build()
        assert "harbor jobs start -c" in script
        assert "#!/bin/bash" in script
        assert "set -e" in script

    def test_contains_dockerd_startup(self):
        config = HarborJobConfig(job_name="test")
        task = HarborTrial(config)
        script = trial.build()
        assert "dockerd" in script

    def test_with_setup_commands(self):
        config = HarborJobConfig(
            job_name="test",
            setup_commands=["pip install harbor --quiet"],
        )
        task = HarborTrial(config)
        script = trial.build()
        assert "pip install harbor --quiet" in script


class TestHarborTrialSetup:
    async def test_uploads_harbor_yaml(self):
        config = HarborJobConfig(
            job_name="test",
            agents=[AgentConfig(name="t2")],
        )
        task = HarborTrial(config)
        mock_sandbox = AsyncMock()
        mock_sandbox.write_file_by_path = AsyncMock(return_value=MagicMock(success=True))
        mock_sandbox.fs = AsyncMock()

        await trial.setup(mock_sandbox)

        mock_sandbox.write_file_by_path.assert_called_once()
        yaml_content = mock_sandbox.write_file_by_path.call_args[0][0]
        assert "agents" in yaml_content
        assert "t2" in yaml_content


class TestHarborTrialCollect:
    async def test_with_trial_results(self):
        config = HarborJobConfig(job_name="test")
        task = HarborTrial(config)

        mock_sandbox = AsyncMock()
        execute_result = MagicMock()
        execute_result.stdout = "/jobs/test/trials/trial-001/result.json"
        mock_sandbox.execute = AsyncMock(return_value=execute_result)

        read_response = MagicMock()
        read_response.content = json.dumps({
            "trial_name": "trial-001",
            "task_name": "t1",
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:01:00Z",
            "verifier_result": {"rewards": {"reward": 1.0}},
            "agent_result": {},
            "exception_info": None,
        })
        mock_sandbox.read_file = AsyncMock(return_value=read_response)

        result = await trial.collect(mock_sandbox, "harbor completed", 0)
        assert result.status == TrialStatus.COMPLETED
        assert len(result.trial_results) == 1
        assert result.trial_results[0].score == 1.0

    async def test_no_trial_results(self):
        config = HarborJobConfig(job_name="test")
        task = HarborTrial(config)

        mock_sandbox = AsyncMock()
        execute_result = MagicMock()
        execute_result.stdout = ""
        mock_sandbox.execute = AsyncMock(return_value=execute_result)

        result = await trial.collect(mock_sandbox, "error", 1)
        assert result.status == TrialStatus.FAILED
        assert result.trial_results == []


class TestHarborTrialRegistration:
    def test_registered(self):
        trial = _create_trial(HarborJobConfig(job_name="test"))
        assert isinstance(task, HarborTrial)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/sdk/job/test_trial_harbor.py -x`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
# rock/sdk/job/trial/harbor.py
from __future__ import annotations

import json

from rock.actions import Command, ReadFileRequest
from rock.logger import init_logger
from rock.sdk.agent.constants import USER_DEFINED_LOGS
from rock.sdk.agent.models.trial.result import TrialResult
from rock.sdk.job.config import HarborJobConfig
from rock.sdk.job.result import TrialResult, TrialStatus
from rock.sdk.job.trial.abstract import AbstractTrial
from rock.sdk.job.trial.registry import register_trial

logger = init_logger(__name__)

_HARBOR_SCRIPT_TEMPLATE = r"""#!/bin/bash
set -e

# ── Detect and start dockerd ─────────────────────────────────────────
if command -v docker &>/dev/null; then
    echo "docker OK: $(command -v docker)"
    if ! pgrep -x dockerd &>/dev/null; then
        echo "Starting dockerd..."
        nohup dockerd &>/var/log/dockerd.log &
    fi
    for i in $(seq 1 60); do
        if docker info &>/dev/null; then echo "dockerd is ready"; break; fi
        sleep 1
        if [ "$i" -eq 60 ]; then echo "WARN: dockerd failed to start within 60s"; fi
    done
fi

# ── Ensure output directory exists ──────────────────────────────────
mkdir -p {user_defined_dir}

# ── Setup commands ───────────────────────────────────────────────────
{setup_commands}

# ── Harbor run ───────────────────────────────────────────────────────
harbor jobs start -c {config_path}
"""


class HarborTrial(AbstractTrial):
    """Harbor benchmark 执行"""

    _config: HarborJobConfig

    async def setup(self, sandbox) -> None:
        await self._upload_files(sandbox)
        yaml_content = self._config.to_harbor_yaml()
        config_path = f"{USER_DEFINED_LOGS}/rock_job_{self._config.job_name}.yaml"
        await sandbox.write_file_by_path(yaml_content, config_path)

    def build(self) -> str:
        setup_lines = []
        for cmd in self._config.setup_commands:
            setup_lines.append(f"echo '>>> {cmd[:60]}...'")
            setup_lines.append(cmd)
        setup_block = "\n".join(setup_lines) if setup_lines else "echo 'No setup commands'"

        config_path = f"{USER_DEFINED_LOGS}/rock_job_{self._config.job_name}.yaml"
        return _HARBOR_SCRIPT_TEMPLATE.format(
            setup_commands=setup_block,
            config_path=config_path,
            user_defined_dir=USER_DEFINED_LOGS,
        )

    async def collect(self, sandbox, output: str, exit_code: int) -> TrialResult:
        trial_results = await self._collect_trial_results(sandbox)
        return TrialResult(
            task_id=self._config.job_name or "",
            status=TrialStatus.COMPLETED if trial_results else TrialStatus.FAILED,
            output=output,
            exit_code=exit_code,
            trial_results=trial_results,
        )

    async def _collect_trial_results(self, sandbox) -> list[TrialResult]:
        """Read trial-level result.json files from sandbox."""
        job_dir = f"{self._config.jobs_dir}/{self._config.job_name}"
        try:
            list_result = await sandbox.execute(
                Command(command=["find", job_dir, "-mindepth", "2", "-maxdepth", "2", "-name", "result.json"])
            )
            trial_files = [
                line.strip() for line in (list_result.stdout or "").strip().split("\n") if line.strip()
            ]
        except Exception:
            trial_files = []

        results: list[TrialResult] = []
        for trial_file in trial_files:
            try:
                response = await sandbox.read_file(ReadFileRequest(path=trial_file))
                data = json.loads(response.content)
                results.append(TrialResult.from_harbor_json(data))
            except Exception as e:
                logger.warning(f"Failed to parse trial result {trial_file}: {e}")

        return results


register_trial(HarborJobConfig, HarborTrial)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/sdk/job/test_trial_harbor.py -x -v`
Expected: ALL PASS

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check rock/sdk/job/trial/harbor.py tests/unit/sdk/job/test_trial_harbor.py --fix
uv run ruff format rock/sdk/job/trial/harbor.py tests/unit/sdk/job/test_trial_harbor.py
git add rock/sdk/job/trial/harbor.py tests/unit/sdk/job/test_trial_harbor.py
git commit -m "feat(job): add HarborTrial (extracted from agent/job.py)"
```

---

### Task 9: CLI Update

**Files:**
- Modify: `rock/cli/command/job.py`
- Create: `tests/unit/sdk/job/test_cli_job.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/sdk/job/test_cli_job.py
import argparse
from unittest.mock import AsyncMock, MagicMock, patch

from rock.cli.command.job import JobCommand
from rock.sdk.job.config import BashJobConfig, HarborJobConfig


class TestJobCommandParser:
    async def test_parser_has_type_argument(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        await JobCommand.add_parser_to(subparsers)

        args = parser.parse_args(["job", "run", "--type", "bash", "--script-content", "echo hi"])
        assert args.type == "bash"

    async def test_parser_default_type_is_bash(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        await JobCommand.add_parser_to(subparsers)

        args = parser.parse_args(["job", "run", "--script-content", "echo hi"])
        assert args.type == "bash"

    async def test_parser_harbor_type(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        await JobCommand.add_parser_to(subparsers)

        args = parser.parse_args(["job", "run", "--type", "harbor", "--config", "/tmp/c.yaml"])
        assert args.type == "harbor"
        assert args.config == "/tmp/c.yaml"


class TestJobCommandRun:
    async def test_bash_run(self):
        args = argparse.Namespace(
            job_command="run",
            type="bash",
            script=None,
            script_content="echo hello",
            image=None,
            memory=None,
            cpus=None,
            local_path=None,
            target_path="/root/job",
            timeout=3600,
            config=None,
            base_url=None,
            cluster=None,
            extra_headers=None,
        )
        mock_result = MagicMock()
        mock_result.exit_code = 0
        mock_result.raw_output = "hello"

        with patch("rock.cli.command.job.Job") as MockJob:
            mock_job_instance = AsyncMock()
            mock_job_instance.run = AsyncMock(return_value=mock_result)
            MockJob.return_value = mock_job_instance

            cmd = JobCommand()
            await cmd.arun(args)

            MockJob.assert_called_once()
            called_config = MockJob.call_args[0][0]
            assert isinstance(called_config, BashJobConfig)
            assert called_config.script == "echo hello"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/sdk/job/test_cli_job.py -x`
Expected: FAIL — old JobCommand doesn't have `--type` or use new Job

- [ ] **Step 3: Rewrite CLI**

```python
# rock/cli/command/job.py
import argparse

from rock.cli.command.command import Command
from rock.logger import init_logger

logger = init_logger(__name__)


class JobCommand(Command):
    name = "job"

    async def arun(self, args: argparse.Namespace):
        if args.job_command == "run":
            await self._job_run(args)
        else:
            logger.error(f"Unknown job subcommand: {args.job_command}")

    async def _job_run(self, args: argparse.Namespace):
        from rock.sdk.agent.models.trial.config import RockEnvironmentConfig
        from rock.sdk.job.config import BashJobConfig, HarborJobConfig
        from rock.sdk.job.job import Job

        job_type = args.type or "bash"

        if job_type == "bash":
            if not args.script and not args.script_content:
                logger.error("Either --script or --script-content is required for bash type")
                return
            if args.script and args.script_content:
                logger.error("--script and --script-content cannot be used together")
                return

            env_kwargs = {}
            if args.image:
                env_kwargs["image"] = args.image
            if args.memory:
                env_kwargs["memory"] = args.memory
            if args.cpus:
                env_kwargs["cpus"] = args.cpus
            if args.base_url:
                env_kwargs["base_url"] = args.base_url
            if args.cluster:
                env_kwargs["cluster"] = args.cluster

            file_uploads = []
            if args.local_path:
                file_uploads.append((args.local_path, args.target_path))

            extra_headers = {}
            if args.extra_headers:
                extra_headers = args.extra_headers

            if extra_headers:
                env_kwargs["extra_headers"] = extra_headers

            config = BashJobConfig(
                script=args.script_content,
                script_path=args.script,
                environment=RockEnvironmentConfig(**env_kwargs),
                file_uploads=file_uploads,
                timeout=args.timeout,
                auto_stop=True,
            )

        elif job_type == "harbor":
            if not args.config:
                logger.error("--config is required for harbor type")
                return
            config = HarborJobConfig.from_yaml(args.config)
            if args.image:
                config.environment.image = args.image
            config.auto_stop = True

        else:
            logger.error(f"Unknown job type: {job_type}")
            return

        try:
            result = await Job(config).run()
            if result.task_results:
                for tr in result.task_results:
                    if tr.output:
                        print(tr.output)
            logger.info(f"Job completed: status={result.status}, score={result.score}")
        except Exception as e:
            logger.error(f"Job failed: {e}")

    @staticmethod
    async def add_parser_to(subparsers: argparse._SubParsersAction):
        job_parser = subparsers.add_parser("job", help="Manage sandbox jobs")
        job_subparsers = job_parser.add_subparsers(dest="job_command")

        run_parser = job_subparsers.add_parser("run", help="Run a job in a sandbox")
        run_parser.add_argument("--type", choices=["bash", "harbor"], default="bash", help="Job type")
        # bash args
        run_parser.add_argument("--script", default=None, help="Path to script file")
        run_parser.add_argument("--script-content", default=None, help="Inline script content")
        # harbor args
        run_parser.add_argument("--config", default=None, help="Harbor YAML config path")
        # shared args
        run_parser.add_argument("--image", default=None, help="Sandbox image")
        run_parser.add_argument("--memory", default=None, help="Memory (e.g. 8g)")
        run_parser.add_argument("--cpus", default=None, type=float, help="CPU count")
        run_parser.add_argument("--timeout", type=int, default=3600, help="Timeout in seconds")
        run_parser.add_argument("--local-path", default=None, help="Local dir to upload")
        run_parser.add_argument("--target-path", default="/root/job", help="Target dir in sandbox")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/sdk/job/test_cli_job.py -x -v`
Expected: ALL PASS

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check rock/cli/command/job.py tests/unit/sdk/job/test_cli_job.py --fix
uv run ruff format rock/cli/command/job.py tests/unit/sdk/job/test_cli_job.py
git add rock/cli/command/job.py tests/unit/sdk/job/test_cli_job.py
git commit -m "feat(job): update CLI with --type bash/harbor routing"
```

---

### Task 10: Integration Wiring + Final Verification

**Files:**
- Modify: `rock/sdk/job/__init__.py`
- Modify: `rock/sdk/job/trial/__init__.py`
- Create: `tests/unit/sdk/job/test_integration.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/sdk/job/test_integration.py


class TestPublicImports:
    def test_import_job(self):
        from rock.sdk.job import Job

        assert Job is not None

    def test_import_configs(self):
        from rock.sdk.job import BashJobConfig, HarborJobConfig, JobConfig

        assert issubclass(BashJobConfig, JobConfig)
        assert issubclass(HarborJobConfig, JobConfig)

    def test_import_results(self):
        from rock.sdk.job import JobResult, JobStatus, TrialResult, TrialStatus

        assert TrialStatus.COMPLETED == "completed"
        assert JobStatus.COMPLETED == "completed"

    def test_import_operator(self):
        from rock.sdk.job import Operator, ScatterOperator

        assert issubclass(ScatterOperator, Operator)

    def test_import_task(self):
        from rock.sdk.job import AbstractTrial, register_trial

        assert AbstractTrial is not None
        assert callable(register_trial)

    def test_trial_registry_has_both_types(self):
        from rock.sdk.job import BashJobConfig, HarborJobConfig
        from rock.sdk.job.trial.registry import _TRIAL_REGISTRY

        assert BashJobConfig in _TRIAL_REGISTRY
        assert HarborJobConfig in _TRIAL_REGISTRY


class TestAgentBackwardCompat:
    def test_old_imports_still_work(self):
        from rock.sdk.agent import Job, JobConfig, JobResult, JobStatus

        assert Job is not None
        assert JobConfig is not None
        assert JobResult is not None
        assert JobStatus is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/sdk/job/test_integration.py -x`
Expected: FAIL — `ImportError` from empty `__init__.py`

- [ ] **Step 3: Write __init__.py files**

```python
# rock/sdk/job/trial/__init__.py
from rock.sdk.job.trial.abstract import AbstractTrial
from rock.sdk.job.trial.registry import _create_trial, register_trial

__all__ = ["AbstractTrial", "register_trial", "_create_trial"]
```

```python
# rock/sdk/job/__init__.py
from rock.sdk.job.config import BashJobConfig, HarborJobConfig, JobConfig
from rock.sdk.job.executor import JobClient, JobExecutor, TrialClient
from rock.sdk.job.job import Job
from rock.sdk.job.operator import Operator, ScatterOperator
from rock.sdk.job.result import JobResult, JobStatus, TrialResult, TrialStatus
from rock.sdk.job.trial import AbstractTrial, register_trial

# Auto-register task types
import rock.sdk.job.trial.bash  # noqa: F401
import rock.sdk.job.trial.harbor  # noqa: F401

__all__ = [
    "Job",
    "JobConfig",
    "BashJobConfig",
    "HarborJobConfig",
    "JobResult",
    "JobStatus",
    "TrialResult",
    "TrialStatus",
    "JobExecutor",
    "JobClient",
    "TrialClient",
    "Operator",
    "ScatterOperator",
    "AbstractTrial",
    "register_trial",
]
```

- [ ] **Step 4: Run ALL tests**

```bash
# New module tests
uv run pytest tests/unit/sdk/job/ -x -v

# Backward compat: existing agent tests still pass
uv run pytest tests/unit/sdk/agent/ -x -v

# Full lint
uv run ruff check rock/sdk/job/ tests/unit/sdk/job/ --fix
uv run ruff format rock/sdk/job/ tests/unit/sdk/job/
```

Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add rock/sdk/job/__init__.py rock/sdk/job/trial/__init__.py tests/unit/sdk/job/test_integration.py
git commit -m "feat(job): integration wiring and public exports"
```

---

## Verification

After all tasks complete:

```bash
# 1. Run all new tests
uv run pytest tests/unit/sdk/job/ -v

# 2. Run existing agent tests (must not break)
uv run pytest tests/unit/sdk/agent/ -v

# 3. Run full fast test suite
uv run pytest -m "not need_ray and not need_admin and not need_admin_and_network" --reruns 1

# 4. Lint everything
uv run ruff check rock/sdk/job/ rock/cli/command/job.py --fix
uv run ruff format rock/sdk/job/ rock/cli/command/job.py
```
