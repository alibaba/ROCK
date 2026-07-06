# BashJob reward result 解析适配方案

## 背景

ROCK Job SDK 中，`HarborTrial.collect()` 会读取 Harbor 生成的 trial 级 `result.json`，并从 `verifier_result.rewards.reward` 得到 `trial.score`。但 `BashTrial.collect()` 之前只按进程退出码构造基础 `TrialResult`：

- `exit_code == 0` 时标记 completed
- `exit_code != 0` 时填充 `BashExitCode`
- `TrialResult.score` 恒为 `0.0`

这导致 BashJob 即使在 sandbox 内写出了 reward protocol 产物，SDK 侧也不会读取结构化结果。外部系统如果只看 stdout 的 `score:` 还能拿到分数，但 `JobResult.trial_results` 中的分数、trial 元数据、token 信息和异常信息都会丢失。

对应 issue: https://github.com/alibaba/ROCK/issues/1214

## 目标

1. BashJob 支持读取 reward protocol 的 trial 级 `result.json`。
2. `trial.score` 与 `verifier_result.rewards.reward` 对齐。
3. 保持 `JobExecutor` / `Job` 的现有抽象：`Trial.collect()` 可以返回单个 `TrialResult` 或 `list[TrialResult]`，由 Job 层统一 flatten。
4. 对老 BashJob 保持兼容：没有 `result.json` 时仍返回基础 `TrialResult`；如果 stdout 中有标准 `score:` 行，可作为 fallback。
5. 不改变 Harbor 的 result 解析路径。

## 非目标

- 不在 SDK 侧修复 path-like `trial_name` 造成的嵌套目录问题。SDK 只读取协议约定的位置，模板或任务脚本需要保证 `trial_name` 是 path-safe。
- 不把 BashJob 强制绑定到某个 benchmark 模板。
- 不引入新的 job result 协议，只适配已有 reward protocol。

## reward protocol 约定

BashJob 产物根目录默认为：

```text
/data/logs/user-defined
```

单 trial 场景需要写出：

```text
/data/logs/user-defined/
├── result.json
└── <trial_name>/
    ├── result.json
    ├── reward.txt
    └── reward.json
```

SDK 解析的核心文件是 trial 级：

```text
<artifact_root>/<trial_name>/result.json
```

主分数读取路径：

```text
verifier_result.rewards.reward
```

`trial_name` 必须是单层目录名，例如 `task_1__Ab3xY9k`。如果任务名是 `tasks/T01zh_email_triage` 这类 path-like 值，模板应先生成 path-safe label，例如 `tasks_T01zh_email_triage__Ab3xY9k`，但 `task_name` 字段仍保留原始任务名，便于追踪。

## 方案

### 1. 新增 `RewardTrialResult`

在 `rock.sdk.job.result` 中新增 `RewardTrialResult(TrialResult)`，作为 Bash reward protocol 的结构化结果模型。

核心字段：

| 字段 | 来源 | 说明 |
|------|------|------|
| `task_name` | trial `result.json` | 原始任务名 |
| `trial_name` | trial `result.json` | trial 目录名 |
| `trial_uri` | trial `result.json` | trial 目录 URI |
| `task_id` / `source` / `task_checksum` | trial `result.json` | 任务追踪信息 |
| `agent_info` | trial `result.json` | agent / model 信息 |
| `agent_result` | trial `result.json` | token / cost / rollout details |
| `verifier_result` | trial `result.json` | reward 字典 |
| `exception_info` | trial `result.json` 或 Bash exit code | 异常信息 |

`score` 属性覆盖为：

```python
return float(self.verifier_result.rewards.get("reward", 0.0))
```

这样 `JobResult.score` 仍可复用现有逻辑：对所有 trial 的 `score` 求平均。

### 2. BashJob session 默认注入 `LOG_DIR`

`BashTrial.on_sandbox_ready()` 中注入：

```python
env.setdefault("LOG_DIR", env_vars.ROCK_BASH_JOB_ARTIFACT_DIR)
```

目的：

- 给 Bash 模板一个稳定默认写入位置。
- 不覆盖用户显式传入的 `LOG_DIR`。
- 与已有 OSS mirror 的 `ROCK_ARTIFACT_DIR` 保持兼容。

### 3. `BashTrial.collect()` 优先读取 trial result

收集顺序：

1. 在候选 artifact roots 下查找 trial 级 `result.json`
2. 找到后读取并解析为 `RewardTrialResult`
3. 若进程 exit code 非 0 且 result 中没有 `exception_info`，补充 `BashExitCode`
4. 返回 `list[RewardTrialResult]`

候选 roots：

```python
[
    env.get("LOG_DIR"),
    env.get("ROCK_ARTIFACT_DIR"),
    env_vars.ROCK_BASH_JOB_ARTIFACT_DIR,
]
```

查找命令：

```bash
find <root> -mindepth 2 -maxdepth 2 -name result.json
```

`mindepth=2/maxdepth=2` 的含义是只匹配：

```text
<root>/<trial_name>/result.json
```

不会把 job 根目录的 `result.json` 当成 trial 结果，也不会递归吞掉任意深层文件。

### 4. stdout `score:` fallback

如果没有找到 trial result 文件，则解析 stdout 中最后一个标准分数行：

```text
score: 0.625
```

解析成功时返回 `RewardTrialResult`，只填充最小字段：

- `task_name = job_name`
- `raw_output`
- `exit_code`
- `verifier_result.rewards.reward = parsed_score`

这个 fallback 用于兼容已有 Bash 模板或 CI 输出，不替代结构化 result。

### 5. 最后回退到基础 `TrialResult`

如果既没有 trial result，也没有可解析的 stdout score，则保持旧行为：

- `exit_code == 0`: `TrialResult(status=completed, score=0.0)`
- `exit_code != 0`: `TrialResult(exception_info=BashExitCode, score=0.0)`

## 数据流

```text
Bash script
  ├─ writes <LOG_DIR>/<trial_name>/result.json
  ├─ optionally prints "score: <float>"
  └─ exits with code

JobExecutor._do_wait()
  ├─ captures stdout / exit_code
  └─ calls BashTrial.collect(sandbox, output, exit_code)

BashTrial.collect()
  ├─ parse trial result.json -> list[RewardTrialResult]
  ├─ else parse stdout score -> RewardTrialResult
  └─ else base TrialResult

Job._build_result()
  └─ flatten list results into JobResult.trial_results
```

## 兼容性

- 对没有 reward protocol 的 BashJob 无行为破坏，仍返回基础 `TrialResult`。
- 对只打印 `score:` 的 BashJob，`trial.score` 从 stdout fallback 得到非零值。
- 对产出多个 trial result 的 BashJob，`collect()` 返回 list，Job 层已有 flatten 支持。
- 对 HarborJob 无影响，Harbor 仍使用 `HarborTrialResult.from_harbor_json()`。

## 边界与风险

### path-like `trial_name`

如果模板把原始 `TASKS=tasks/T01zh_email_triage` 直接拼进 `TRIAL_NAME`，实际文件会落到：

```text
/data/logs/user-defined/tasks/T01zh_email_triage__xxx/result.json
```

这不符合 `<root>/<trial_name>/result.json` 的单层目录协议，SDK 不会读取。修复点应在模板侧：`trial_name` 使用 path-safe label，`task_name` 保留原始值。

### 多个 roots 重复发现

`LOG_DIR`、`ROCK_ARTIFACT_DIR` 和默认 artifact dir 可能指向同一路径。SDK 对发现到的 `result.json` 路径去重，避免重复 trial。

### result 文件解析失败

单个文件解析失败只记录 warning，不阻断其它 trial result。若全部失败，则进入 stdout fallback / 基础 fallback。

## 测试方案

新增和覆盖以下单元测试：

1. `RewardTrialResult.from_reward_json()` 能从 `verifier_result.rewards.reward` 得到 score。
2. `BashTrial.collect()` 能读取 trial 级 `result.json` 并返回 `list[RewardTrialResult]`。
3. 没有 result 文件时，`BashTrial.collect()` 能从 stdout `score:` 行得到 score。
4. 公开导出 `RewardTrialResult`，保证 SDK import 面可用。

验证命令：

```bash
uv run --extra admin --group test pytest tests/unit/sdk/job -q
uv run ruff check rock/sdk/job/__init__.py rock/sdk/job/result.py rock/sdk/job/trial/bash.py tests/unit/sdk/job/test_integration.py tests/unit/sdk/job/test_result.py tests/unit/sdk/job/test_trial_bash.py
uv run ruff format --check rock/sdk/job/__init__.py rock/sdk/job/result.py rock/sdk/job/trial/bash.py tests/unit/sdk/job/test_integration.py tests/unit/sdk/job/test_result.py tests/unit/sdk/job/test_trial_bash.py
```

## 后续

1. 如果需要支持嵌套历史产物，可另开 issue 讨论是否在 SDK 中增加递归扫描模式；默认不建议，因为会扩大误读范围。
2. Bash 模板侧应统一生成 path-safe `trial_name`，避免 `TASKS` / 文件路径 / CSV 路径直接成为目录结构。
3. 可在未来的 JobViewer 文档中补充 Bash reward result 的读取路径和兼容层级。
