# Job Config 层次重构

**Date**: 2026-04-15
**PR Branch**: `feature/xinshi/harbor-jobs`

## 背景

Job 模块重构（#779/#780, #788/#789）引入了 Job/Operator/Executor/Trial 抽象，但 config 层存在以下问题：

1. **依赖倒置** — 基类 `JobConfig` 依赖 bench 模块的 `RockEnvironmentConfig`，导致循环导入
2. **字段错位** — `auto_stop`/`setup_commands`/`file_uploads`/`env` 同时存在于 `JobConfig` 顶层和 `RockEnvironmentConfig` 中
3. **`to_harbor_yaml()` 丢字段** — `namespace`/`experiment_id`/`labels` 被错误排除
4. **`OssMirrorConfig` 缺字段** — 比 harbor 版本少 `namespace`/`experiment_id`
5. **缺 `HFRegistryInfo`** — harbor 支持 4 种 registry 类型，ROCK 只有 3 种

## 修改内容

### 1. 新建 `JobEnvironmentConfig`

```
rock/sdk/job/config.py

JobEnvironmentConfig(SandboxConfig)
  ├── setup_commands: list[str]
  ├── file_uploads: list[tuple[str, str]]
  ├── auto_stop: bool
  └── env: dict[str, str]
```

承载从 `JobConfig` 下沉的环境字段。`JobConfig` 不再依赖 bench 模块。

### 2. `JobConfig` 瘦身

```python
class JobConfig(BaseModel):
    environment: JobEnvironmentConfig
    job_name: str | None
    namespace: str | None
    experiment_id: str | None
    labels: dict[str, str]
    timeout: int = 3600
```

### 3. `RockEnvironmentConfig` 改继承

```
Before: RockEnvironmentConfig(SandboxConfig, EnvironmentConfig)
After:  RockEnvironmentConfig(JobEnvironmentConfig, EnvironmentConfig)
```

不再重复声明 `setup_commands`/`file_uploads`/`auto_stop`（从 `JobEnvironmentConfig` 继承）。

### 4. `to_harbor_yaml()` 使用镜像模型

引入 `_HarborJobFields` 镜像模型，字段对齐 `harbor.models.job.config.JobConfig`。
使用 `model_validate` 模式自动过滤（与 `to_harbor_environment()` 一致），
取代手动 `_BASE_FIELDS` exclude + re-inject。

**修复**：`namespace`/`experiment_id`/`labels` 现在正确出现在 Harbor YAML 中。

### 5. `OssMirrorConfig` 补齐字段

新增 `namespace` 和 `experiment_id`，对齐 `harbor.environments.base.OssMirrorConfig`。
通过 `_sync_experiment_id` 和 `_sync_namespace_to_oss_mirror` validator 自动同步。

### 6. 新增 `HFRegistryInfo`

对齐 harbor 的 4 种 registry 类型（`Oss|Remote|Local|HF`）。

## 修改后的层次结构

```
JobEnvironmentConfig(SandboxConfig)         ← rock.sdk.job.config（新增）
  ├── image, memory, cpus, ...              ← SandboxConfig
  └── setup_commands, file_uploads,         ← job 环境字段
      auto_stop, env

JobConfig(BaseModel)                        ← rock.sdk.job.config（瘦身）
  ├── environment: JobEnvironmentConfig
  ├── job_name, namespace, experiment_id
  ├── labels, timeout
  └── BashJobConfig(JobConfig)

RockEnvironmentConfig(                      ← rock.sdk.bench.models.trial.config（改继承）
    JobEnvironmentConfig,
    EnvironmentConfig
)
  └── to_harbor_environment() → dict

HarborJobConfig(JobConfig)                  ← rock.sdk.bench.models.job.config
  ├── environment: RockEnvironmentConfig    ← override
  ├── jobs_dir, agents, datasets, ...       ← harbor 原生字段
  └── to_harbor_yaml() → str               ← 使用 _HarborJobFields 镜像模型
```

## 受影响的文件

| 文件 | 修改类型 |
|------|----------|
| `rock/sdk/job/config.py` | 新增 `JobEnvironmentConfig`，瘦身 `JobConfig` |
| `rock/sdk/bench/models/trial/config.py` | `OssMirrorConfig` 补字段，`RockEnvironmentConfig` 改继承 |
| `rock/sdk/bench/models/job/config.py` | 新增 `HFRegistryInfo`/`_HarborJobFields`，修复 validators 和 `to_harbor_yaml` |
| `rock/sdk/job/__init__.py` | 删除循环导入 workaround，导出 `JobEnvironmentConfig` |
| `rock/sdk/job/executor.py` | `config.env` → `config.environment.env` 等 |
| `rock/sdk/job/trial/*.py` | 字段访问路径更新 |
| `rock/cli/command/job.py` | `auto_stop`/`file_uploads` 移入 environment |
| `rock/sdk/bench/__init__.py` | 导出 `HFRegistryInfo` |
