# JobConfig 字段分析: `namespace` 与 `experiment_id`

## 1. 现状分析

### 1.1 字段定义位置

| 字段 | 定义位置 | 类型 |
|------|---------|------|
| `namespace` | `JobConfig` (`models/job/config.py:143`) | `str \| None = None` |
| `experiment_id` | `JobConfig` (`models/job/config.py:147`) | `str \| None = None` |
| `experiment_id` | `SandboxConfig` (`sdk/sandbox/config.py:40`) | `str \| None = None` |
| `namespace` | `SandboxConfig` (`sdk/sandbox/config.py:42`) | `str \| None = None` |
| `_namespace` | `Sandbox` (`sdk/sandbox/client.py:78`) | `str \| None = None` |
| `_experiment_id` | `Sandbox` (`sdk/sandbox/client.py:79`) | `str \| None = None` |

注意: `JobConfig.environment` 类型为 `RockEnvironmentConfig`，继承自 `SandboxConfig`，因此 `self.environment.experiment_id` 来自 `SandboxConfig`。

### 1.2 当前数据流

```
用户构造 JobConfig(experiment_id="exp-1", environment=RockEnvironmentConfig(experiment_id=?))
                                                        ↑ 来自 SandboxConfig

Sandbox.start()
  └─ get_status() 获取 sandbox_info
       ├─ sandbox_info.namespace  → Sandbox._namespace   (client.py:203)
       └─ sandbox_info.experiment_id → Sandbox._experiment_id (client.py:205)

Job.submit()
  └─ _prepare_and_start()
       └─ _autofill_sandbox_info()   (job.py:268-269)
            └─ self._config.namespace = self._sandbox._namespace
            └─ (experiment_id 未处理)
```

### 1.3 `to_harbor_yaml()` 序列化

`JobConfig.to_harbor_yaml()` 将 `namespace` 和 `experiment_id` 序列化到 Harbor YAML 中（`exclude={"environment"}, exclude_none=True`），最终传给 `harbor jobs start -c`。

---

## 2. 问题

### 2.1 `experiment_id` 两处定义未同步

- `JobConfig.experiment_id` — Harbor YAML 层面的实验标识
- `SandboxConfig.experiment_id`（通过 `JobConfig.environment`）— sandbox 创建时传递的实验标识
- **问题**: 两个 `experiment_id` 各自独立，没有同步或校验逻辑。用户可能在两处设置不同的值，导致 sandbox 创建和 Harbor 执行使用不同的 experiment_id。

### 2.2 `namespace` 缺少一致性校验

- `_autofill_sandbox_info()` 直接覆盖 `self._config.namespace = self._sandbox._namespace`
- **问题**: 如果用户已经设置了 `namespace`（非 None），当前逻辑会静默覆盖，不做任何校验。

---

## 3. 改进方案

### 3.1 `experiment_id`: 在 JobConfig 中增加 model_validator (post init)

**保留** `JobConfig.experiment_id` 作为唯一权威来源，通过 `model_validator(mode="after")` 做三件事：

1. **校验非空**: `JobConfig.experiment_id` 不能为 None 或空字符串
2. **一致性校验**: 如果 `environment.experiment_id`（即 SandboxConfig 的）已有值，必须与 `JobConfig.experiment_id` 一致，否则抛异常
3. **向下同步**: 将 `JobConfig.experiment_id` 设置到 `environment.experiment_id`

```python
@model_validator(mode="after")
def _sync_experiment_id(self):
    if not self.experiment_id:
        raise ValueError("experiment_id must not be empty")
    env_exp = self.environment.experiment_id
    if env_exp is not None and env_exp != self.experiment_id:
        raise ValueError(
            f"experiment_id mismatch: JobConfig has '{self.experiment_id}', "
            f"but environment (SandboxConfig) has '{env_exp}'"
        )
    self.environment.experiment_id = self.experiment_id
    return self
```

**行为矩阵**:

| JobConfig.experiment_id | environment.experiment_id | 行为 |
|------------------------|--------------------------|------|
| `None` 或 `""` | 任意 | **抛出 ValueError**: experiment_id 不能为空 |
| `"exp-1"` | `None` | 同步: `environment.experiment_id = "exp-1"` |
| `"exp-1"` | `"exp-1"` | 通过，一致 |
| `"exp-1"` | `"exp-2"` | **抛出 ValueError**: mismatch |

### 3.2 `namespace`: 保持由 sandbox 返回值设置（运行时回填）

`namespace` 与 `experiment_id` 不同 — 它的权威来源是 sandbox 运行时返回值，用户通常不需要设置。保持在 `_autofill_sandbox_info()` 中处理，但增加一致性校验：

| 用户设置 | sandbox 返回 | 行为 |
|---------|-------------|------|
| `None` | 有值 | 自动回填 sandbox 返回值 |
| `None` | `None` | 保持 `None` |
| 有值 | 有值且一致 | 保持不变 |
| 有值 | 有值且不一致 | **抛出 ValueError** |
| 有值 | `None` | 保留用户设置值 |

---

## 4. 涉及文件

| 文件 | 变更内容 |
|------|---------|
| `rock/sdk/agent/models/job/config.py` | 新增 `_sync_experiment_id` model_validator |
| `rock/sdk/agent/job.py` | 更新 `_autofill_sandbox_info()`，namespace 增加一致性校验 |
| `tests/unit/sdk/agent/` | 补充测试: validator 校验逻辑、mismatch 异常、空值异常 |
