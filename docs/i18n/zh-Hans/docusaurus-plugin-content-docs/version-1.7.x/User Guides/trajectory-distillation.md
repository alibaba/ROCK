---
sidebar_position: 5
---

# 使用 Job 进行轨迹蒸馏

使用 ROCK Job 系统收集 Agent 轨迹，筛选高质量数据用于模型蒸馏训练。

## 1. 快速开始

以下是一个完整的端到端示例。准备好配置文件和运行脚本后即可直接运行。

### Step 1: 准备配置文件

创建 `distill_job_config.yaml`：

```yaml
experiment_id: "distill-exp-001"

environment:
  base_url: "http://your-rock-service:8080"      # ROCK 服务地址
  extra_headers:
    XRL-Authorization: "Bearer <your-token>"     # 认证 token
  user_id: "<your-user-id>"
  cluster: "<your-cluster>"
  image: "your-harbor-image:latest"              # 包含 Harbor 和 Agent 的镜像
  cpus: 8
  memory: "32g"
  startup_timeout: 1800
  env:
    OPENAI_API_KEY: "<your-api-key>"             # Teacher 模型 API Key
    OPENAI_API_BASE: "<your-api-base-url>"       # Teacher 模型 API Base URL

agents:
  - name: "swe-agent"
    model_name: "openai/<your-teacher-model>"
    max_timeout_sec: 1800
    kwargs:
      per_instance_cost_limit: 0                 # 禁用成本检查（自定义模型必须）
      total_cost_limit: 0

datasets:
  - name: "princeton-nlp/SWE-bench_Verified"
    registry:
      split: "test"
      oss_access_key_id: "<your-oss-key>"
      oss_access_key_secret: "<your-oss-secret>"
      oss_bucket: "<your-oss-bucket>"
      oss_dataset_path: "<your-oss-path>"
      oss_region: "cn-shanghai"
      oss_endpoint: "oss-cn-shanghai.aliyuncs.com"
    task_names:
      - "astropy__astropy-7606"                  # 先跑一个任务验证流程

orchestrator:
  n_concurrent_trials: 4
```

将 `<...>` 占位符替换为实际值。

:::caution
使用非标准模型（非 OpenAI 官方模型）时，**必须设置** `per_instance_cost_limit: 0` 和 `total_cost_limit: 0`，否则 swe-agent 会因 litellm 无法计算成本而直接退出，不产出任何轨迹。
:::

### Step 2: 运行脚本

创建 `collect_trajectories.py`：

```python
"""端到端轨迹收集示例"""

import asyncio
import json

from rock.sdk.job import Job, JobConfig
from rock.sdk.job.operator import ScatterOperator


async def main():
    # 1. 加载配置
    config = JobConfig.from_yaml("distill_job_config.yaml")

    # 2. 创建 Job，ScatterOperator(size=N) 表示用 N 个 sandbox 并行执行
    job = Job(config, operator=ScatterOperator(size=1))

    # 3. 运行并等待结果
    result = await job.run()

    print(f"Job 完成: exit_code={result.exit_code}, score={result.score}")
    print(f"共 {len(result.trial_results)} 个 trial\n")

    # 4. 打印每个 trial 的结果
    for trial in result.trial_results:
        print(f"  {trial.task_name}: score={trial.score}, status={trial.status}")
        if trial.exception_info:
            print(f"    error: {trial.exception_info.exception_message}")


asyncio.run(main())
```

### Step 3: 执行

```bash
python collect_trajectories.py
```

运行结束后，每个 trial 产出的轨迹数据包括：
- `result.json` — 已自动解析为 `HarborTrialResult`，包含 reward（`trial.score`）和执行状态
- `trajectory.json` — ATIF 格式的完整 Agent 交互轨迹，包含每一步的 tool_calls 和 observation

验证通过后，将 `ScatterOperator(size=1)` 改为 `ScatterOperator(size=8)` 即可并行扩展到 8 个 sandbox，将 `task_names` 注释掉即可跑全量数据集。

---

## 2. 前置条件

- 一个可用的 ROCK 服务，参考 [快速开始](../Getting%20Started/quickstart.md) 部署
- Teacher 模型的 API 可访问（OpenAI 兼容接口）
- Harbor 镜像已构建并可用
- （可选）OSS 存储，用于数据集下载和产物持久化

## 3. 配置详解

### 3.1 关键配置项

| 配置项 | 作用 | 说明 |
|--------|------|------|
| `experiment_id` | 实验标识 | HarborJobConfig 必填字段 |
| `environment.extra_headers` | 认证信息 | 设置 `XRL-Authorization: "Bearer <token>"` 进行服务认证 |
| `environment.user_id` | 用户标识 | ROCK 平台的用户 ID |
| `environment.cluster` | 集群标识 | sandbox 运行的目标集群 |
| `agents[].model_name` | Teacher 模型标识 | 格式 `provider/model`，例如 `openai/gpt-4o` |
| `agents[].max_timeout_sec` | Agent 单任务超时 | 影响 Job 整体超时计算 |
| `agents[].kwargs` | Agent 参数透传 | 透传给 Harbor Agent（非 ROCK 字段），见下文 |
| `orchestrator.n_concurrent_trials` | Harbor 内部并发数 | 控制单个 sandbox 内同时运行的 trial 数量 |
| `timeout_multiplier` | 超时倍数 | 应用于 Agent timeout，生成最终 Job 等待超时 |

#### agents[].kwargs 常用参数

这些参数通过 `kwargs` 透传给 Harbor Agent，不是 ROCK Job 的字段：

| 参数 | 作用 | 说明 |
|------|------|------|
| `per_instance_cost_limit` | 单次运行成本上限 | 使用非标准模型时**必须设为 0**，否则 litellm 无法计算成本会导致 Agent 退出 |
| `total_cost_limit` | 总成本上限 | 同上，**必须设为 0** |
| `collect_rollout_details` | 收集 rollout 详情 | 设为 `true` 时 `result.json` 中可能包含 `rollout_details`（是否生效取决于具体 Agent 实现） |

### 3.2 并行度说明

```
总并行度 = ScatterOperator(size) × orchestrator.n_concurrent_trials
```

- `ScatterOperator(size=N)` — 创建 N 个独立 sandbox（ROCK 层面并行）
- `orchestrator.n_concurrent_trials` — 每个 sandbox 内 Harbor 的并发 trial 数

例如 `size=4, n_concurrent_trials=4` 意味着同时有 4 个 sandbox，每个跑 4 个 trial，总共 16 路并行。

## 4. 轨迹数据详解

### 4.1 产出总览

每个 trial 完成后产出以下数据：

| 数据 | 格式 | 获取方式 | 说明 |
|------|------|----------|------|
| `result.json` | JSON | 已自动解析为 `HarborTrialResult`，通过 `JobResult.trial_results` 访问 | 包含 reward、执行状态、异常信息 |
| `trajectory.json` | ATIF JSON | 保存在 sandbox 文件系统中，需手动读取 | **主要轨迹数据来源**，包含 Agent 完整的交互历史 |

:::info
`result.json` 中的 `agent_result.rollout_details` 和 `token_ids` **是否有数据取决于具体 Agent 的实现**。例如 swe-agent 当前不会填充这些字段。轨迹的完整交互数据在 `trajectory.json` 中。
:::

### 4.2 HarborTrialResult 字段

`result.json` 被自动解析为 `HarborTrialResult`，可通过 `JobResult.trial_results` 直接访问：

```python
trial: HarborTrialResult

# 基本信息
trial.task_name          # str   — 任务名称
trial.trial_name         # str   — trial 名称
trial.status             # str   — "completed" 或 "failed"

# reward（用于筛选）
trial.score              # float — verifier_result.rewards["reward"]

# 异常信息
trial.exception_info     # ExceptionInfo | None
  .exception_type        # str
  .exception_message     # str

# Agent 执行结果（是否有值取决于 Agent 实现）
trial.agent_result       # AgentResult | None
  .n_input_tokens        # int
  .n_output_tokens       # int
  .cost_usd              # float
  .rollout_details       # list[dict] | None
trial.token_ids          # list[int] — rollout_details 中 completion_token_ids 的拼接
```

### 4.3 trajectory.json — Agent 交互轨迹

`trajectory.json` 是 Harbor 产出的 ATIF（Agent Trajectory Interchange Format）格式文件，是**轨迹蒸馏的主要数据来源**。包含 Agent 每一步的交互：

```json
{
  "schema_version": "ATIF-v1.5",
  "session_id": "...",
  "agent": {"name": "swe-agent", "version": "1.1.0", "model_name": "..."},
  "steps": [
    {
      "step_id": 1,
      "source": "system",
      "message": "You are a helpful assistant..."
    },
    {
      "step_id": 2,
      "source": "agent",
      "message": "I'll help you fix this issue...",
      "tool_calls": [
        {
          "function_name": "swe_agent_action",
          "arguments": {"raw_action": "find /testbed -type f -name '*.py' | head -20"}
        }
      ],
      "observation": {
        "results": [{"content": "/testbed/astropy/..."}]
      }
    }
  ]
}
```

每个 step 包含：
- `source` — `"system"` / `"agent"` / `"tool"`
- `message` — Agent 的思考或系统提示
- `tool_calls` — Agent 调用的工具及参数
- `observation` — 工具执行的返回结果

### 4.4 sandbox 中的文件布局

```
/data/logs/user-defined/jobs/{job_name}/
├── result.json                              # 顶层汇总结果
├── config.json                              # 运行配置
└── {task_name}__{trial_id}/                 # 每个 trial 的目录
    ├── result.json                          # trial 级别结果（解析为 HarborTrialResult）
    ├── config.json
    ├── agent/
    │   ├── trajectory.json                  # ATIF 格式轨迹（主要数据）
    │   └── swe-agent.trajectory.json        # Agent 原始格式轨迹
    ├── verifier/
    │   └── report.json                      # 验证结果
    └── artifacts/
        └── manifest.json
```

## 5. 进阶用法

### 5.1 异步模式（RL 训练集成）

在 RL 训练循环中，使用 `submit()` / `wait()` 实现非阻塞提交：

```python
async def rl_training_loop():
    config = JobConfig.from_yaml("distill_job_config.yaml")
    job = Job(config, operator=ScatterOperator(size=4))

    # 非阻塞提交
    await job.submit()

    # ... 在此期间执行其他操作（如模型参数更新）...

    # 阻塞等待结果
    result = await job.wait()

    # 用 score 筛选，轨迹数据需从 trajectory.json 获取
    for trial in result.trial_results:
        if trial.score > 0:
            # 使用 trial.task_name 和 trial.trial_name 定位 trajectory.json
            pass
```

### 5.2 Rejection Sampling（多次采样取最优）

对同一任务多次采样，只保留最高 reward 的轨迹。

`ScatterOperator(size=8)` 会创建 8 个独立的 sandbox，每个 sandbox 运行**相同的完整配置**。如果配置中有 1 个 task，则产出 8 个结果（同一 task 的 8 次独立尝试）；如果有 10 个 task，则产出 80 个结果（每个 task 各 8 次尝试）。

```python
from collections import defaultdict

async def rejection_sampling_collect():
    config = JobConfig.from_yaml("distill_job_config.yaml")

    # 8 个 sandbox 并行，每个 sandbox 运行完整配置
    # 同一 task 会有 8 次独立尝试，可从中选最优
    result = await Job(config, operator=ScatterOperator(size=8)).run()

    # 按 task 分组，每组取 top-1
    by_task = defaultdict(list)
    for trial in result.trial_results:
        if trial.exception_info is None:
            by_task[trial.task_name].append(trial)

    best_trials = []
    for task_name, trials in by_task.items():
        best = max(trials, key=lambda t: t.score)
        if best.score > 0:
            best_trials.append(best)

    print(f"Rejection sampling: {len(best_trials)} best trajectories from {len(by_task)} tasks")
    return best_trials
```

### 5.3 构造 DPO 偏好对

从同一任务的多次尝试中构造 (chosen, rejected) 对：

```python
def to_dpo_pairs(trials):
    by_task = defaultdict(list)
    for trial in trials:
        by_task[trial.task_name].append(trial)

    pairs = []
    for task_name, task_trials in by_task.items():
        sorted_trials = sorted(task_trials, key=lambda t: t.score, reverse=True)
        if len(sorted_trials) < 2:
            continue
        chosen, rejected = sorted_trials[0], sorted_trials[-1]
        if chosen.score > rejected.score:
            pairs.append({
                "task": task_name,
                "chosen_trial": chosen.trial_name,
                "rejected_trial": rejected.trial_name,
                "chosen_score": chosen.score,
                "rejected_score": rejected.score,
            })
    return pairs
```

## 相关文档

- [快速开始](../Getting%20Started/quickstart.md) — 部署 ROCK 服务
- [配置指南](./configuration.md) — 运行时环境配置
- [API 文档](../References/api.md) — Sandbox API 接口
- [Python SDK 文档](../References/Python%20SDK%20References/python_sdk.md) — SDK 完整参考
