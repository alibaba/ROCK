## Job 级指标汇报配置方案 — 集成 Harbor ml_tracker

### 背景

ROCK 的 Job 系统需要在 **Bench 评测** 和 **RL 训练** 场景中汇报运行指标。Harbor 框架已内置 `ml_tracker` 模块，可汇报以下关键指标：

| 类别 | 指标 |
|------|------|
| **Reward** | `reward/*`（verifier 输出的各 reward key） |
| **Duration** | `total_duration_sec`、`agent_duration_sec` |
| **Token** | `input_tokens`、`output_tokens`、`cache_tokens`、`cost_usd` |
| **RL 训练** | `logprobs_mean`、`entropy`、`loss`、`kl_divergence`、`advantage`、`grad_norm`、`clip_fraction`、`value_loss`、`explained_variance` |
| **Running** | `pass_rate`、`avg_reward`、`error_rate` |
| **Summary** | `final_pass_rate`、`final_avg_reward`、`final_error_rate`、`total_trials`、`total_errors`、`total_duration_sec` |

但当前 ml_tracker 的启用方式依赖**环境变量** `ROCK_API_KEY` 的存在性（硬编码判断），用户无法通过 Job 配置声明式地控制是否启用、传入超参数等。

**改动前**（Harbor `job.py`）：

```python
# 硬编码检查环境变量，无配置入口
if os.environ.get("ROCK_API_KEY"):
    self._tracker = MLTrackerFactory.create(...)
```

---

### 目标

在 `EnvironmentConfig` 上新增 **`tracking`** 字段，让用户在 YAML 的 `environment` 段中声明式地启用 Harbor 内置的 ml_tracker，汇报 Bench/RL 训练指标。

**设计原则**：
- **字段名不绑定具体 SDK**：用 `tracking`（而非 `ml_tracker`），避免配置字段与具体包名耦合
- **复用 Harbor 已有能力**：不另起炉灶，底层仍调用 Harbor `ml_tracker` 模块
- 所有字段可选，零配置向后兼容（默认不启用，保持现有行为）
- 不侵入 `HarborJobConfig.metrics: list[MetricConfig]`（那是评测结果的聚合策略，语义不同）

---

### 方案（已实现）

#### 模型定义

**ROCK 侧** — `rock/sdk/envhub/config.py`：

`TrackingConfig` 定义在 `EnvironmentConfig` 同级，作为 `EnvironmentConfig` 的二级字段：

```python
class TrackingConfig(BaseModel):
    """Experiment tracking configuration.

    When present and enabled, activates Harbor's built-in ml_tracker to report
    per-trial metrics (reward, duration, token usage, RL training signals)
    and a final job-level summary.
    """

    enabled: bool = Field(
        default=True,
        description="Whether to enable experiment tracking for this job.",
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "User-defined hyperparameters merged into ml_tracker.init(config=...). "
            "Combined with auto-collected job metadata (agents, datasets, etc.)."
        ),
    )

class EnvironmentConfig(SandboxConfig):
    uploads: list[tuple[str, str]] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    oss_mirror: OssMirrorConfig | None = None
    tracking: TrackingConfig | None = Field(
        default=None,
        description="Experiment tracking configuration. None = disabled (default).",
    )
```

**Harbor 侧** — `harbor/ml_tracker/config.py`（内部模块名保持 `ml_tracker` 不变）：

```python
class MLTrackerConfig(BaseModel):
    enabled: bool = Field(default=True)
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="User-defined hyperparameters merged into ml_tracker.init(config=...).",
    )
```

> **命名决策**：
> - 用户配置字段名 = `tracking`（不绑定具体 SDK，未来可扩展到其他 tracker）
> - 子字段 = `params`（而非 `config`，避免 `tracking.config` 语义重复）
> - Harbor 内部模块目录仍叫 `ml_tracker/`（内部实现，不暴露给用户）

#### 在配置层次中的位置

`tracking` 放在 `EnvironmentConfig` 下作为二级字段，而非 `JobConfig` 的一级字段。原因：

- **与 `oss_mirror` 同层**：`tracking` 和 `oss_mirror` 都是环境级别的能力配置，放在 environment 下更内聚
- **Harbor 的 `EnvironmentConfig` 天然包含这类配置**：Harbor YAML 中 environment 段是 tracking 信息的自然归属
- **简化序列化**：`to_harbor_yaml()` 通过 `to_harbor_environment()` 序列化 environment 时自然携带 tracking

```python
# JobConfig 不直接暴露 tracking，通过 environment 间接访问
class JobConfig(BaseModel):
    environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    job_name: str | None = None
    namespace: str | None = None
    experiment_id: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    timeout: int = 7200
```

> **默认 `None`**：不写 `tracking` 时行为等价于改动前（不启用）。用户显式写 `environment.tracking: {}` 即可启用。

---

#### YAML 配置示例

**最简启用**（所有默认值，自动采集 agent/dataset 信息）：

```yaml
experiment_id: exp-rl-001
job_name: qwen-72b-swe-bench
environment:
  tracking: {}
```

**记录额外超参数**（RL 训练场景）：

```yaml
experiment_id: exp-rl-002
job_name: rl-grpo-run-3
environment:
  tracking:
    params:
      model: qwen-72b-instruct
      algorithm: GRPO
      learning_rate: 1.0e-5
      batch_size: 64
      kl_coeff: 0.05
      num_rollouts: 4
```

**显式禁用**（覆盖团队默认配置）：

```yaml
environment:
  tracking:
    enabled: false
```

**不写 `tracking`**（默认行为，等同于禁用）：

```yaml
experiment_id: exp-001
job_name: my-job
environment: {}
# tracking 不出现 → None → 不启用
```

---

### 字段说明

| 字段路径 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| **`environment.tracking`** | `TrackingConfig \| None` | `None` | 开关。`None` = 不启用（向后兼容）；写 `{}` = 启用。 |
| **`environment.tracking.enabled`** | `bool` | `True` | 细粒度开关。配合 `tracking: { enabled: false }` 可显式禁用。 |
| **`environment.tracking.params`** | `dict[str, Any]` | `{}` | 用户自定义超参数，与自动采集的 job metadata 合并后传给 `ml_tracker.init(config=...)`。 |

两层开关的设计意图：
- `tracking` 不写 / `null` → 不启用（向后兼容，默认路径）
- `tracking: {}` → 启用（`enabled` 默认 `True`）
- `tracking: { enabled: false }` → 显式禁用（团队配置模板中可以预留 `tracking` 段落但暂时关闭）

---

### 汇报的指标详情

启用 tracking 后，Harbor 框架会在以下时机自动汇报：

**每个 Trial 结束时**（`TrialEvent.END` hook）：

```
reward/*              — verifier 输出的 reward 值（每个 key 单独上报）
total_duration_sec    — Trial 总耗时
agent_duration_sec    — Agent 执行耗时
input_tokens          — 输入 token 数
output_tokens         — 输出 token 数
cache_tokens          — 缓存 token 数
cost_usd              — 推理花费（USD）
logprobs_mean         — rollout log probabilities 均值（RL）
entropy               — 策略熵 = -logprobs_mean（RL）
loss                  — 训练 loss（RL，来自 agent metadata）
kl_divergence         — KL 散度（RL）
advantage             — 优势值（RL）
grad_norm             — 梯度范数（RL）
clip_fraction         — PPO clip fraction（RL）
value_loss            — 值函数 loss（RL）
explained_variance    — 解释方差（RL）
pass_rate             — 截至当前的通过率（running）
avg_reward            — 截至当前的平均 reward（running）
error_rate            — 截至当前的错误率（running）
```

**Job 结束时**（`report_job_summary`）：

```
final_pass_rate       — 最终通过率
final_avg_reward      — 最终平均 reward
final_error_rate      — 最终错误率
total_trials          — 总 trial 数
total_errors          — 总错误数
total_duration_sec    — Job 总耗时
```

---

### 与现有体系的关系

```
JobConfig
├── environment: EnvironmentConfig
│   ├── uploads, env, ...               ← 已有: 环境级配置
│   ├── oss_mirror: OssMirrorConfig     ← 已有: OSS 镜像配置
│   └── tracking: TrackingConfig | None ← NEW: 实验追踪配置
├── labels: dict[str, str]              ← 已有: Job 级标签
└── ...

HarborJobConfig(JobConfig)
├── environment.tracking (inherited)    ← NEW: 通过 environment 继承
├── metrics: list[MetricConfig]         ← 已有: 评测结果聚合方式（sum/mean/max）
└── ...

BashJobConfig(JobConfig)
├── environment.tracking (inherited)    ← NEW: 通过 environment 继承
└── ...
```

**关键区分**：
- **`environment.tracking`**（新增）= "实验追踪：每个 Trial 的 **业务指标怎么记录**"（reward/token/RL signals → ml_tracker SDK）
- **`metrics`**（HarborJobConfig 已有）= "评测聚合：多个 Trial 的结果 **怎么聚合成最终分数**"（mean/sum/max）

两者语义正交，互不冲突。`tracking` 与 `oss_mirror` 同层，都属于环境级别的能力配置。

---

### 改动文件清单

#### ROCK 侧

| 文件 | 改动 |
|------|------|
| `rock/sdk/envhub/config.py` | 新增 `TrackingConfig` 类 + `EnvironmentConfig.tracking` 字段 |

#### Harbor 侧

| 文件 | 改动 |
|------|------|
| `harbor/ml_tracker/config.py` | `config` 字段重命名为 `params` |
| `harbor/ml_tracker/factory.py` | 新增 `tracker_config` 参数，合并用户 `params` 到自动采集的 config |
| `harbor/models/job/config.py` | 新增 `tracking: MLTrackerConfig \| None` 字段 |
| `harbor/job.py` | 从 `self.config.tracking` 读取配置替代 env var 硬编码；传递 `tracker_config` 给 factory |
| `tests/unit/ml_tracker/test_config.py` | `config` → `params` 适配 |
| `tests/unit/ml_tracker/test_factory.py` | 新增 `test_create_merges_user_params` 测试 |
| `tests/unit/ml_tracker/test_job_integration.py` | 重写为测试 `tracking` 字段的配置集成 |

#### 配置传递链路

```
用户 YAML
  → rock HarborJobConfig.environment.tracking (解析 + 校验)
    → to_harbor_yaml() → environment 段携带 tracking
      → harbor JobConfig.tracking (反序列化)
        → harbor Job.__init__ 读取 → MLTrackerFactory.create(tracker_config=...)
```

---

### 向后兼容性

- `EnvironmentConfig.tracking` 默认为 `None`，不写等价于改动前行为（不启用）。
- `SandboxConfig` 基类不受影响（`tracking` 只加在 `EnvironmentConfig` 层）。
- `BashJobConfig` / `HarborJobConfig`：通过 `environment` 间接访问，不涉及 `extra="forbid"` 问题。
- `_HarborJobFields`：environment 中 `tracking` 为 `None` 时被序列化过滤，不出现在 Harbor YAML 中。
- `ROCK_API_KEY` 环境变量：Harbor `job.py` 中同时检查 `tracking is not None and tracking.enabled` **和** `ROCK_API_KEY`，两个条件都满足才启用。这保证了即使配置启用了 tracking，没有 API key 也不会报错。
