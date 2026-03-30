# JobConfig 环境配置重构设计

**日期：** 2026-03-30
**影响范围：** `rock/sdk/agent/models/job/config.py`、`rock/sdk/agent/job.py`、相关测试和示例
**兼容性：** 破坏性变更，不保留 deprecated 兼容层，直接替换

---

## 问题

`JobConfig` 目前对用户暴露了两套重叠的环境概念：

1. **Rock 扩展字段**（`sandbox_config`、`sandbox_env`、`setup_commands`、`file_uploads`、`auto_stop_sandbox`）— 控制 Rock 沙箱生命周期
2. **Harbor 原生 `environment: EnvironmentConfig`** — Harbor 自身的 env 配置，被序列化进 `harbor jobs start -c`

由此引发的混乱：
- 两个语义不同的 `env` 字典（`sandbox_env` vs `EnvironmentConfig.env`），对用户来说都叫"env"
- 资源规格出现在两处（`SandboxConfig.cpus/memory` vs `EnvironmentConfig.override_cpus/memory_mb`）
- 用户必须理解 Rock 内部的层级结构才能正确填写配置
- `sandbox_env` 注入到 bash session 后，子进程自然继承，`EnvironmentConfig.env` 在大多数场景是多余的重复

从用户视角看，他们只是在跑一个 Rock 环境，不需要知道 Harbor 的存在。

---

## 设计

### 新的模型层级

```
JobConfig
├── environment: EnvironmentConfig    # 新：统一的环境概念，平铺所有字段
└── （Harbor 原生字段 — 保持不变）
    job_name, jobs_dir, n_attempts, timeout_multiplier,
    agents, verifier, metrics, orchestrator, datasets, tasks, artifacts, ...
```

### `EnvironmentConfig`（新，多重继承）

新建文件 `rock/sdk/agent/models/job/environment.py`。

同时继承 `SandboxConfig`（Rock 沙箱层）和 trial 层的 `EnvironmentConfig`（Harbor 环境层），所有字段平铺，无嵌套结构。用户可直接填写 Harbor 高级字段（`force_build`、`override_gpus` 等），无需了解层级。

```python
# rock/sdk/agent/models/job/environment.py
from rock.sdk.agent.models.trial.config import EnvironmentConfig as _HarborEnvConfig
from rock.sdk.sandbox.config import SandboxConfig

class EnvironmentConfig(SandboxConfig, _HarborEnvConfig):
    # ── 统一 env vars（override _HarborEnvConfig.env）──
    # 注入到 sandbox bash session；harbor 作为子进程自然继承，无需额外注入
    env: dict[str, str] = Field(default_factory=dict)

    # ── Job 执行配置 ──
    setup_commands: list[str] = Field(default_factory=list)
    file_uploads: list[tuple[str, str]] = Field(
        default_factory=list,
        description="运行前上传的文件/目录：[(本地路径, 沙箱路径), ...]",
    )
    auto_stop: bool = False
```

继承关系：
- `SandboxConfig` 提供：`image`、`memory`、`cpus`、`cluster`、`base_url`、`startup_timeout` 等
- `_HarborEnvConfig`（trial 层 `EnvironmentConfig` 的别名）提供：`type`、`import_path`、`force_build`、`delete`、`override_cpus`、`override_memory_mb`、`mounts_json`、`kwargs` 等
- `env` 字段在新类中显式定义，覆盖 `_HarborEnvConfig.env`，语义统一为注入 sandbox session
- `trial/config.py` 零改动，`_HarborEnvConfig` 只是 `job/environment.py` 内的局部 import 别名

`Sandbox(config.environment)` 天然兼容，无需修改 `Sandbox` 内部逻辑。

### `JobConfig` 字段变更

**删除的字段（破坏性）：**

| 旧字段 | 替代 |
|--------|------|
| `sandbox_config: SandboxConfig \| None` | `environment`（含所有 SandboxConfig 字段） |
| `sandbox_env: dict[str, str]` | `environment.env` |
| `setup_commands: list[str]` | `environment.setup_commands` |
| `file_uploads: list[tuple]` | `environment.file_uploads` |
| `auto_stop_sandbox: bool` | `environment.auto_stop` |
| `environment: EnvironmentConfig`（旧 Harbor 类型） | `environment`（新统一类型，含 Harbor 字段） |

**新增字段：**
- `environment: EnvironmentConfig`（新的多重继承类型，默认值 `EnvironmentConfig()`）

**删除：** `_rock_fields` 类变量。

---

## 序列化：`to_harbor_yaml()`

Harbor 字段（来自 `_HarborEnvConfig`）需要序列化进 harbor YAML 的 `environment` section。
Rock 字段（来自 `SandboxConfig` 和 Job 层）不序列化进去。

通过 Pydantic 的 `model_fields` 从 `_HarborEnvConfig` 类定义自动推导字段集合，无需手动维护列表：

```python
# EnvironmentConfig 内
_HARBOR_ENV_FIELDS: ClassVar[set[str]] = (
    set(_HarborEnvConfig.model_fields.keys()) - {"env"}
    # env 排除：覆盖了它的语义（走 sandbox session，harbor 自然继承）
)

def to_harbor_environment(self) -> dict:
    """提取需要序列化进 harbor YAML environment section 的字段。"""
    return self.model_dump(mode="json", include=self._HARBOR_ENV_FIELDS, exclude_none=True)
```

`_HarborEnvConfig` 以后新增字段，`_HARBOR_ENV_FIELDS` 自动更新，零维护。

`JobConfig.to_harbor_yaml()` 实现：

```python
def to_harbor_yaml(self) -> str:
    import yaml
    data = self.model_dump(mode="json", exclude={"environment"}, exclude_none=True)
    harbor_env = self.environment.to_harbor_environment()
    if harbor_env:
        data["environment"] = harbor_env
    return yaml.dump(data, default_flow_style=False, allow_unicode=True)
```

---

## `Job` 类字段引用变更

| 旧引用 | 新引用 |
|--------|--------|
| `self._config.sandbox_config` | `self._config.environment` |
| `self._config.sandbox_env` | `self._config.environment.env` |
| `self._config.setup_commands` | `self._config.environment.setup_commands` |
| `self._config.file_uploads` | `self._config.environment.file_uploads` |
| `self._config.auto_stop_sandbox` | `self._config.environment.auto_stop` |

---

## `from_yaml()` 接口变更

重构后，传入 Rock 字段的 override 需要使用嵌套形式：

```python
# 重构前
JobConfig.from_yaml(path, setup_commands=["pip install x"])

# 重构后
JobConfig.from_yaml(path, environment={"setup_commands": ["pip install x"]})
```

`from_yaml()` 实现需处理 `environment` override 与 YAML 加载的 `environment` dict 的合并逻辑。

---

## 用户 YAML 对比（重构前 / 后）

**重构前：**
```yaml
sandbox_config:
  base_url: "http://rock-admin:8080"
  image: "my-harbor-image:latest"
  cluster: "zb"
  memory: "32g"
  cpus: 8
  startup_timeout: 1800
  auto_clear_seconds: 7200

sandbox_env:
  OPENAI_API_KEY: "sk-xxx"
  OPENAI_BASE_URL: "https://api.openai.com/v1"

setup_commands:
  - "pip install my-package"

auto_stop_sandbox: false

agents:
  - name: "swe-agent"
    model_name: "custom_openai/my-model"
    env:                          # 不得不重复填一遍
      OPENAI_API_KEY: "sk-xxx"
      OPENAI_BASE_URL: "https://api.openai.com/v1"
```

**重构后：**
```yaml
environment:
  base_url: "http://rock-admin:8080"
  image: "my-harbor-image:latest"
  cluster: "zb"
  memory: "32g"
  cpus: 8
  startup_timeout: 1800
  auto_clear_seconds: 7200
  env:
    OPENAI_API_KEY: "sk-xxx"
    OPENAI_BASE_URL: "https://api.openai.com/v1"
  setup_commands:
    - "pip install my-package"
  auto_stop: false
  # Harbor 高级字段（按需填，不填即用默认值）
  # force_build: false
  # override_gpus: 1

agents:
  - name: "swe-agent"
    model_name: "custom_openai/my-model"
```

---

## 文件结构变化

新增文件：
```
rock/sdk/agent/models/job/
├── config.py        # JobConfig + 数据集相关（修改）
├── environment.py   # 新增：EnvironmentConfig（多重继承）
└── result.py
```

## 需要修改的文件

| 文件 | 变更内容 |
|------|----------|
| `rock/sdk/agent/models/job/environment.py` | **新建**：`EnvironmentConfig`（多重继承 `SandboxConfig` + `_HarborEnvConfig`） |
| `rock/sdk/agent/models/job/config.py` | 删除旧 Rock 扩展字段，新增 `environment: EnvironmentConfig`，更新 `to_harbor_yaml()` |
| `rock/sdk/agent/models/job/__init__.py` | 导出 `EnvironmentConfig` |
| `rock/sdk/agent/__init__.py` | `EnvironmentConfig` 改从 `job.environment` 导入 |
| `rock/sdk/agent/models/__init__.py` | 同上 |
| `rock/sdk/agent/job.py` | 更新所有字段引用 |
| `rock/sdk/sandbox/config.py` | 不变 |
| `rock/sdk/agent/models/trial/config.py` | 不变 |
| `examples/harbor/swe_job_config.yaml.template` | 更新为新结构 |
| `examples/harbor/tb_job_config.yaml.template` | 更新为新结构 |
| `tests/unit/sdk/agent/test_job_config_serialization.py` | 更新所有测试 fixture |
| `tests/unit/sdk/agent/test_models.py` | 更新模型测试 |
| `tests/unit/sdk/agent/test_job.py` | 更新 job 测试 |
| `docs/dev/agent/README.md` | 更新使用示例 |

---

## 不在本次范围内

- `rock/sdk/sandbox/client.py` 内部逻辑（除构造函数签名外）
- `SandboxGroupConfig` 的变更
- Harbor 本身的任何修改
