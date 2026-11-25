# Rock Job Result 协议说明

## 1. 定位

Rock Job Result 协议用于描述一次 Job 执行后的结构化结果。它服务两个层次：

1. **Job 级结果**：描述整次 Job 的聚合状态。
2. **Trial 级结果**：描述单个 task / trial 的执行结果、分数、异常、agent 输出和 verifier 输出。

SDK 的核心消费对象是 trial 级 `result.json`。`JobResult.score` 由 `trial_results[*].score` 聚合得到。

## 2. 目录布局

### 2.1 通用布局

一个 Job 的结果目录应至少包含 Job 根目录和若干 trial 子目录：

```text
<job_result_root>/
├── result.json
└── <trial_name>/
    ├── result.json
    ├── reward.txt
    └── reward.json
```

说明：

- `<job_result_root>/result.json` 是 Job 级结果。
- `<job_result_root>/<trial_name>/result.json` 是 Trial 级结果。
- `reward.txt` 和 `reward.json` 是可选辅助文件；SDK 以 trial 级 `result.json` 为主。
- `<trial_name>` 必须是单层目录名，不能包含 `/`。

### 2.2 HarborJob 路径

HarborJob 当前读取：

```text
<jobs_dir>/<job_name>/<trial_name>/result.json
```

SDK 查找方式：

```bash
find <jobs_dir>/<job_name> -mindepth 2 -maxdepth 2 -name result.json
```

### 2.3 BashJob 路径

BashJob 默认 artifact root：

```text
/data/logs/user-defined
```

BashJob 当前会在这些 roots 下查找：

```text
LOG_DIR
ROCK_ARTIFACT_DIR
/data/logs/user-defined
```

SDK 查找方式：

```bash
find <root> -mindepth 2 -maxdepth 2 -name result.json
```

因此 BashJob 模板必须写成：

```text
<root>/<trial_name>/result.json
```

不要写成：

```text
<root>/<path>/<trial_name>/result.json
```

如果原始任务名是 `tasks/T01zh_email_triage`，应将它转换为 path-safe `trial_name` 前缀，例如：

```text
tasks_T01zh_email_triage__Ab3xY9k
```

`task_name` 字段仍保留原始任务名。

## 3. Job 级 `result.json`

Job 级 `result.json` 放在 Job 根目录，用于描述整次 Job 聚合状态。

### 3.1 字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 | Job 结果 ID，通常为 UUID |
| `started_at` | string/null | 否 | Job 开始时间，建议 ISO-8601 UTC |
| `finished_at` | string/null | 否 | Job 结束时间，建议 ISO-8601 UTC |
| `n_total_trials` | integer | 否 | 预期 trial 总数 |
| `stats` | object | 否 | 聚合统计 |
| `stats.n_trials` | integer | 否 | 成功完成的 trial 数 |
| `stats.n_errors` | integer | 否 | 失败 trial 数 |
| `stats.evals` | object | 否 | evaluator 维度的扩展统计 |

### 3.2 示例

```json
{
  "id": "job-0a9ddf9d",
  "started_at": "2026-07-06T02:00:00Z",
  "finished_at": "2026-07-06T02:10:00Z",
  "n_total_trials": 1,
  "stats": {
    "n_trials": 1,
    "n_errors": 0,
    "evals": {}
  }
}
```

### 3.3 SDK 行为

当前 Job SDK 不依赖 Job 级 `result.json` 计算 `JobResult.score`。分数来自 trial 级结果。

Job 级 `result.json` 主要用于：

- 外部 viewer 展示聚合状态
- 任务平台做完成度统计
- 排查 Job 是否成功写出协议文件

## 4. Trial 级 `result.json`

Trial 级 `result.json` 是 SDK 解析的核心协议。

### 4.1 字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 否 | Trial 结果 ID |
| `task_name` | string | 是 | 原始任务名 |
| `trial_name` | string | 是 | Trial 目录名，必须 path-safe |
| `trial_uri` | string/null | Bash 建议 | Trial 目录 URI，例如 `file:///data/logs/user-defined/task__abc` |
| `task_id` | object/null | Bash 建议 | 任务 ID 信息，常见形式为 `{ "path": "..." }` |
| `source` | string/null | 否 | 任务来源，例如 `tasks` |
| `task_checksum` | string/null | Bash 建议 | 任务内容或任务名 checksum |
| `config` | object/null | Bash 建议 | Trial 配置快照 |
| `agent_info` | object/null | 否 | Agent 元信息 |
| `agent_result` | object/null | 否 | Agent 执行结果 |
| `verifier_result` | object/null | 是 | Verifier 结果 |
| `exception_info` | object/null | 否 | 异常信息；非空表示 trial failed |
| `started_at` | string/null | 否 | Trial 开始时间 |
| `finished_at` | string/null | 否 | Trial 结束时间 |
| `environment_setup` | object/null | 否 | 环境初始化耗时段 |
| `agent_setup` | object/null | 否 | Agent 初始化耗时段 |
| `agent_execution` | object/null | 否 | Agent 执行耗时段 |
| `verifier` | object/null | 否 | Verifier 执行耗时段 |

### 4.2 `verifier_result`

`verifier_result.rewards` 是 flat dict。

主分数 key 必须是：

```text
reward
```

示例：

```json
{
  "verifier_result": {
    "rewards": {
      "reward": 0.85,
      "task_score": 0.85,
      "accuracy": 0.85
    }
  }
}
```

SDK 的 `trial.score` 读取：

```text
verifier_result.rewards.reward
```

如果缺失，分数为 `0.0`。

### 4.3 `agent_info`

```json
{
  "agent_info": {
    "name": "unknown",
    "version": "",
    "model_info": {
      "name": "gpt-4.1",
      "provider": "openai"
    }
  }
}
```

字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | Agent 名称 |
| `version` | string | Agent 版本 |
| `model_info.name` | string | 模型名 |
| `model_info.provider` | string | 模型提供方 |

### 4.4 `agent_result`

```json
{
  "agent_result": {
    "n_input_tokens": 1000,
    "n_cache_tokens": 200,
    "n_output_tokens": 300,
    "cost_usd": 0.01,
    "rollout_details": [
      {
        "completion_token_ids": [1, 2, 3]
      }
    ]
  }
}
```

字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `n_input_tokens` | integer/null | 输入 token 数 |
| `n_cache_tokens` | integer/null | cache token 数 |
| `n_output_tokens` | integer/null | 输出 token 数 |
| `cost_usd` | number/null | 估算成本 |
| `rollout_details` | array/null | rollout 详情 |

SDK 的 `token_ids` 来自：

```text
agent_result.rollout_details[*].completion_token_ids
```

### 4.5 `exception_info`

```json
{
  "exception_info": {
    "exception_type": "NoScore",
    "exception_message": "task_score missing",
    "exception_traceback": "",
    "occurred_at": "2026-07-06T02:10:00Z"
  }
}
```

字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `exception_type` | string | 异常类型 |
| `exception_message` | string | 异常信息 |
| `exception_traceback` | string | traceback，可为空 |
| `occurred_at` | string/null | 发生时间 |

SDK 的 `trial.status` 规则：

```text
exception_info is null -> completed
exception_info not null -> failed
```

### 4.6 时间段字段

`environment_setup`、`agent_setup`、`agent_execution`、`verifier` 使用同一结构：

```json
{
  "started_at": "2026-07-06T02:00:00Z",
  "finished_at": "2026-07-06T02:01:00Z"
}
```

Trial 顶层 `started_at` / `finished_at` 用于计算 `duration_sec`。

## 5. 完整示例

```json
{
  "id": "trial-2d6f0c23",
  "task_name": "tasks/T01zh_email_triage",
  "trial_name": "tasks_T01zh_email_triage__Ab3xY9k",
  "trial_uri": "file:///data/logs/user-defined/tasks_T01zh_email_triage__Ab3xY9k",
  "task_id": {
    "path": "/data/logs/user-defined/tasks_T01zh_email_triage__Ab3xY9k"
  },
  "source": "tasks",
  "task_checksum": "372f063cd131b",
  "config": {
    "trial_name": "tasks_T01zh_email_triage__Ab3xY9k"
  },
  "agent_info": {
    "name": "unknown",
    "version": "",
    "model_info": null
  },
  "agent_result": {
    "n_input_tokens": null,
    "n_cache_tokens": null,
    "n_output_tokens": null,
    "cost_usd": null,
    "rollout_details": null
  },
  "verifier_result": {
    "rewards": {
      "reward": 0.71,
      "task_score": 0.71
    }
  },
  "exception_info": null,
  "started_at": "2026-07-06T02:00:00Z",
  "finished_at": "2026-07-06T02:10:00Z",
  "environment_setup": null,
  "agent_setup": null,
  "agent_execution": null,
  "verifier": null
}
```

## 6. `reward.txt` 和 `reward.json`

### 6.1 `reward.txt`

`reward.txt` 是 trial 主分数的纯文本表示。

```text
0.71
```

要求：

- 内容是裸数字。
- 不要求换行。
- 应与 `verifier_result.rewards.reward` 一致。

### 6.2 `reward.json`

`reward.json` 是 trial reward 字典的独立表示。

```json
{
  "reward": 0.71,
  "task_score": 0.71,
  "accuracy": 0.71
}
```

要求：

- 必须包含主分数 key `reward`。
- 应与 trial 级 `result.json` 中的 `verifier_result.rewards` 一致。

### 6.3 SDK 优先级

当前 SDK 读取优先级：

1. trial 级 `result.json`
2. BashJob stdout `score:` fallback
3. 基础 `TrialResult(score=0.0)`

当前 SDK 不直接读取 `reward.txt` 或 `reward.json` 计算分数。

## 7. stdout `score:` fallback

BashJob 兼容 stdout 分数行：

```text
=== Score Summary ===
score: 0.71
```

规则：

- 只匹配行首 `score: <value>`。
- 多个 `score:` 行时取最后一个。
- `score: N/A` 不作为有效分数。
- 解析成功后生成最小 `RewardTrialResult`，其 `verifier_result.rewards.reward` 为该分数。

该 fallback 只用于兼容没有 trial 级 `result.json` 的旧 BashJob。新任务应优先写结构化 result。

## 8. Harbor 与 Bash 当前模型关系

SDK 使用同一套 reward protocol 模型解析 Harbor 和 Bash 的 trial 级 `result.json`：

- 共享模型定义在 `rock.sdk.reward.result`。
- `RewardTrialResult` 负责协议字段、`score`、`token_ids`、状态和耗时计算。
- `HarborTrialResult` 是 `RewardTrialResult` 的薄子类，保留 `from_harbor_json()` 和 Harbor 历史 import 路径。
- `rock.sdk.job.result` 只保留 Job 聚合结果模型，并 re-export reward model 兼容旧 import。

当前行为差异主要来自 collector，而不是协议模型：

| 行为 | Bash | Harbor |
|------|------|--------|
| trial result 路径 | `<root>/<trial_name>/result.json` | `<jobs_dir>/<job_name>/<trial_name>/result.json` |
| root 查找来源 | `LOG_DIR`、`ROCK_ARTIFACT_DIR`、默认 artifact root | `jobs_dir/job_name` |
| no trial result fallback | stdout `score:`，再基础 `TrialResult` | `HarborNoTrials` 基础 `TrialResult` |
| exit code 传播 | 传播到每个 parsed `RewardTrialResult` | Harbor collector 按 Harbor 流程处理 |

## 9. 生产者要求

写 result 的脚本或框架必须满足：

1. `trial_name` 是 path-safe 单层目录名。
2. trial 级 `result.json` 放在 `<root>/<trial_name>/result.json`。
3. `verifier_result.rewards.reward` 是主分数。
4. `exception_info` 为空表示成功，非空表示失败。
5. `task_name` 保留原始任务名，不要为了 path-safe 改写原始语义。
6. `reward.txt`、`reward.json` 和 trial `result.json` 的主分数保持一致。

## 10. 消费者要求

消费 result 的 SDK / viewer 应遵循：

1. 优先读取 trial 级 `result.json`。
2. 不把 Job 根目录 `result.json` 当成 trial 结果。
3. 不默认递归读取深层 `result.json`，避免误读 benchmark 内部产物。
4. 用 `verifier_result.rewards.reward` 作为主分数。
5. 用 `exception_info` 判断 trial 成败。
6. 允许未知字段存在，以便协议向前扩展。
