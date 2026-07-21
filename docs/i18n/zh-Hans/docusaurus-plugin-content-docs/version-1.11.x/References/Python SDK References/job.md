# Use Job to Run Agent

> 这是 ROCK 两种并列的 agent 使用能力中 **Job** 的参考文档,核心 API 是 `rock.sdk.job.Job` 与 `JobConfig`,用于在沙箱里跑一次 agent 评测/任务。有两种 backend:**Bash Job** 与 **Harbor Bench Job**。
>
> 另一种能力是在单个沙箱里安装并运行 agent,见 [Install Agent in Sandbox](./rock-agent.md)。两种能力使用各自独立的配置 schema,**不要互相套用**。

`rock.sdk.job` 通过同一套 `Job` API 支持两种模式,通过配置类型区分:

- **Bash Job**:在沙箱中运行自定义 Shell 脚本,适合数据处理、外部评测工具等
- **Harbor Bench Job**:通过 Harbor 框架运行 AI agent 基准评测任务(SWE-bench、Terminal Bench 等)

## 端到端示例

最小可跑通的 Python 用法:

```python
import asyncio
from rock.sdk.job import Job, JobConfig

async def main():
    config = JobConfig.from_yaml("swe_job_config.yaml")  # 含 agents: 与 datasets:
    result = await Job(config).run()

    print(f"status={result.status}, score={result.score}")
    for trial in result.trial_results:
        print(f"  {trial.task_name}: score={trial.score} ({trial.status})")

asyncio.run(main())
```

完整 yaml 模板参考 `examples/job/harbor/swe_job_config.yaml.template`。

---

## Bash Job

Bash Job 适用于在沙箱内执行任意 Shell 脚本的场景,例如运行评测工具、数据处理流程等。

完整示例参考:[`examples/job/bash/claw_eval/`](https://github.com/alibaba/ROCK/tree/master/examples/job/bash/claw_eval)

- `run_claw_eval.py` — 主入口,演示 `JobConfig.from_yaml()` + `Job(config).run()`
- `claw_eval_bashjob.yaml.template` — YAML 配置模板,含 `script_path`、`environment`、`uploads`、`env` 等字段
- `run_claw_eval.sh` — 沙箱内实际执行的脚本,演示 DinD 启动、日志写入和评分输出

### BashJobConfig 配置字段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `script` | `str \| None` | `None` | 内联脚本内容,与 `script_path` 二选一 |
| `script_path` | `str \| None` | `None` | 本地脚本文件路径,运行时读取并上传执行 |
| `job_name` | `str` | 当前时间戳 | 任务名称,用于日志和产物路径区分 |
| `environment` | `EnvironmentConfig` | — | 沙箱连接及资源配置,详见下表 |
| `namespace` | `str \| None` | `None` | 命名空间 |
| `experiment_id` | `str \| None` | `None` | 实验 ID |
| `timeout` | `int` | `7200` | 整体超时秒数(2 小时) |

**`environment` 常用字段:**

| 字段 | 类型 | 说明 |
|------|------|------|
| `image` | `str` | 沙箱 Docker 镜像 |
| `base_url` | `str` | ROCK 平台地址 |
| `xrl_authorization` | `str` | 鉴权 Token |
| `cluster` | `str` | 目标集群 |
| `memory` | `str` | 内存大小(如 `"64g"`) |
| `cpus` | `int` | CPU 核数 |
| `auto_stop` | `bool` | 任务完成后是否自动停止沙箱 |
| `uploads` | `list` | 本地文件/目录上传列表,格式:`[本地路径, 沙箱目标路径]` |
| `env` | `dict[str, str]` | 注入沙箱会话的环境变量 |

---

## Harbor Bench Job

Harbor Bench Job 适用于通过 Harbor 框架运行 AI agent 基准评测任务,如 SWE-bench、Terminal Bench 等。

> **注意**:`rock.sdk.bench.Job` 已废弃,将在未来移除。请改用 `rock.sdk.job.Job` + `HarborJobConfig`。

完整示例参考:[`examples/job/harbor/`](https://github.com/alibaba/ROCK/tree/master/examples/job/harbor)

- `harbor_demo.py` — 主入口,演示 `JobConfig.from_yaml()` + `Job(config).run()` + 结果遍历
- `swe_job_config.yaml.template` — SWE-bench 任务配置模板
- `swe_job_config-verifier.yaml.template` — 附带 `verifier.mode: native` 的变体
- `tb_job_config.yaml.template` — Terminal Bench 任务配置模板

### HarborJobConfig 核心配置字段

**基础字段:**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `experiment_id` | `str` | 必填 | 实验 ID,Harbor 中必须提供 |
| `job_name` | `str \| None` | 自动生成 | 格式:`{dataset}_{task}_{uuid[:8]}` |
| `namespace` | `str \| None` | `None` | 命名空间,从沙箱自动反填 |
| `environment` | `RockEnvironmentConfig` | — | 沙箱连接及资源配置 |

**执行控制字段:**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `n_attempts` | `int` | `1` | 每个 Trial 的尝试次数 |
| `timeout` | `int` | `7200` | 整体超时秒数(自动从 agent_timeout 推算) |
| `debug` | `bool` | `False` | 调试模式,保留更多中间产物 |

**组件字段:**

| 字段 | 类型 | 说明 |
|------|------|------|
| `agents` | `list[AgentConfig]` | Harbor 框架自身的 agent 配置(典型字段:`name`、`model_name`),完整字段见 `examples/job/harbor/swe_job_config.yaml.template` |
| `datasets` | `list[DatasetConfig]` | 数据集配置列表 |
| `verifier` | `VerifierConfig` | Verifier 评测配置 |
| `orchestrator` | `OrchestratorConfig` | 并发调度配置 |

---

## 结果处理

两种 Job 模式均返回 `JobResult`:

```python
result = await Job(config).run()

print(f"status={result.status}, score={result.score}")
for trial in result.trial_results:
    print(f"  {trial.task_name}: score={trial.score} ({trial.status})")
    if trial.exception_info:
        print(f"    {trial.exception_info.exception_type}: {trial.exception_info.exception_message}")
```

### JobResult 字段

| 字段 / 属性 | 类型 | 说明 |
|------------|------|------|
| `status` | `JobStatus` | 任务整体状态 |
| `trial_results` | `list[TrialResult]` | 所有 Trial 结果列表 |
| `score` | `float`(属性) | 所有 Trial `score` 的平均值 |
| `n_completed` | `int`(属性) | 状态为 `completed` 的 Trial 数 |
| `n_failed` | `int`(属性) | 状态为 `failed` 的 Trial 数 |

### TrialResult 字段

| 字段 / 属性 | 类型 | 说明 |
|------------|------|------|
| `task_name` | `str` | 任务名称 |
| `exit_code` | `int` | 进程退出码 |
| `raw_output` | `str` | 进程原始输出 |
| `exception_info` | `ExceptionInfo \| None` | 若有异常则填充 |
| `status` | `str`(属性) | `"completed"` 或 `"failed"` |
| `duration_sec` | `float`(属性) | 执行耗时(秒) |
| `score` | `float`(属性) | 评分(Bash Job 默认 `0.0`,Harbor 模式来自 verifier) |
