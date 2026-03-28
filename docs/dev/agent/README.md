# Rock Job SDK 设计文档

## 1. 背景与目标

### 问题

当前 RL 训练框架 (verl/SkyRL) 集成 Harbor benchmark 的方式是通过 HTTP adapter (`AgentService`, `HarborService` in `docs/dev/agent/example.py`)，存在以下问题：

- **HarborService 是同步阻塞的** — `submit_task()` 内部直接 `await job.run()`，无法真正并发
- **依赖 Harbor Python SDK 安装在训练节点** — Harbor 及其依赖（Docker、agent 包等）需要与 RL 训练框架共存
- **配置分散** — Rock sandbox 配置通过 `environment_kwargs` 传递，Harbor 配置通过 Python API 构建，没有统一入口
- **无法复用 Harbor CLI 的完整能力** — 直接调 Python API 需要手动处理 dataset、registry、resume 等逻辑

### 目标

设计 `rock/sdk/agent/job.py`，基于 Rock SDK 的 bash 协议，封装 high-level Job SDK：

1. **在 Rock sandbox 内执行 `harbor run`** — Harbor 运行在容器内，训练节点只需 Rock SDK
2. **支持 TB2 benchmark 运行** — 作为 `AgenticTaskService` 的新 adapter，直接对接 verl 的 rollout 接口
3. **支持 agentic RL 训练** — RL 框架通过 Job SDK 提交 rollout 任务，收集 reward/trajectory/token_ids

### 核心思路

```
RL Trainer (verl)                      Rock Sandbox (container)
┌─────────────────┐                   ┌──────────────────────────┐
│                  │   Rock SDK        │                          │
│  Job.submit()   │──────────────────>│  harbor run -c config.yaml│
│                  │   bash protocol   │                          │
│  Job.result()   │<──────────────────│  result.json / trajectory │
│                  │   read file       │                          │
└─────────────────┘                   └──────────────────────────┘
```

不再在训练节点 import harbor，而是在 sandbox 容器内通过 bash 命令执行 `harbor jobs start`，利用 Rock 的 nohup 模式异步等待结果。

## 2. 架构设计

### 2.1 文件结构

从 Harbor 源码（`/home/dengwx/job/tb2/harbor/src/harbor/models/`）拷贝字段定义，仅保留 schema，去掉运行时方法。目录组织与 Harbor 原始结构保持一致：

```
rock/sdk/agent/
├── __init__.py
├── job.py                          # Job, JobResult, TrialResult
└── models/                         # 从 Harbor 拷贝的 config schema（仅字段定义）
    ├── __init__.py
    ├── job/
    │   ├── __init__.py
    │   └── config.py               # JobConfig, OrchestratorConfig, RetryConfig, DatasetConfig
    ├── trial/
    │   ├── __init__.py
    │   └── config.py               # AgentConfig, EnvironmentConfig, VerifierConfig, TaskConfig, ArtifactConfig
    ├── metric/
    │   ├── __init__.py
    │   ├── config.py               # MetricConfig
    │   └── type.py                 # MetricType (enum)
    ├── orchestrator_type.py        # OrchestratorType (enum)
    └── environment_type.py         # EnvironmentType (enum)
```

对应 Harbor 原始路径的映射：

| Harbor 路径 | Rock 路径 | 说明 |
|---|---|---|
| `harbor/models/job/config.py` | `rock/sdk/agent/models/job/config.py` | 去掉 `get_task_configs()` 等方法 |
| `harbor/models/trial/config.py` | `rock/sdk/agent/models/trial/config.py` | 去掉 `get_task_id()` 等方法 |
| `harbor/models/metric/config.py` | `rock/sdk/agent/models/metric/config.py` | 原样拷贝 |
| `harbor/models/metric/type.py` | `rock/sdk/agent/models/metric/type.py` | 原样拷贝 |
| `harbor/models/orchestrator_type.py` | `rock/sdk/agent/models/orchestrator_type.py` | 原样拷贝 |
| `harbor/models/environment_type.py` | `rock/sdk/agent/models/environment_type.py` | 原样拷贝 |

### 2.2 需要拷贝的类（最小集合）

Harbor 完整 JobConfig 依赖树有 21+ 个类，但大部分方法（`get_task_configs()`、`get_task_id()` 等）是 Harbor 运行时逻辑，Job SDK 只需要 **字段定义用于序列化成 YAML**。

#### 需要拷贝的 Pydantic model（9 个，仅字段定义）

| 类名 | 来源 | Rock 目标文件 | 依赖 |
|---|---|---|---|
| `JobConfig` | `harbor/models/job/config.py` | `models/job/config.py` | 下列所有 |
| `OrchestratorConfig` | `harbor/models/job/config.py` | `models/job/config.py` | RetryConfig, OrchestratorType |
| `RetryConfig` | `harbor/models/job/config.py` | `models/job/config.py` | (leaf) |
| `AgentConfig` | `harbor/models/trial/config.py` | `models/trial/config.py` | (leaf) |
| `EnvironmentConfig` | `harbor/models/trial/config.py` | `models/trial/config.py` | EnvironmentType |
| `VerifierConfig` | `harbor/models/trial/config.py` | `models/trial/config.py` | (leaf) |
| `TaskConfig` | `harbor/models/trial/config.py` | `models/trial/config.py` | (leaf) |
| `MetricConfig` | `harbor/models/metric/config.py` | `models/metric/config.py` | MetricType |
| `ArtifactConfig` | `harbor/models/trial/config.py` | `models/trial/config.py` | (leaf) |

#### 需要拷贝的枚举（3 个）

| 枚举 | Rock 目标文件 | 值 |
|---|---|---|
| `OrchestratorType` | `models/orchestrator_type.py` | `LOCAL`, `QUEUE` |
| `EnvironmentType` | `models/environment_type.py` | `DOCKER`, `DAYTONA`, `E2B`, `MODAL`, `RUNLOOP`, `GKE`, `ROCK` |
| `MetricType` | `models/metric/type.py` | `SUM`, `MIN`, `MAX`, `MEAN`, `UV_SCRIPT` |

#### 简化处理的类

| Harbor 原始类 | 简化方式 | 原因 |
|---|---|---|
| `BaseDatasetConfig` / `LocalDatasetConfig` / `RegistryDatasetConfig` | 合并为一个 `DatasetConfig` | 原类含 `get_task_configs()` 等方法，依赖 TaskPaths、shortuuid、registry 客户端 |
| `LocalRegistryInfo` / `RemoteRegistryInfo` / `OssRegistryInfo` | 字段内联到 `DatasetConfig` | 仅被 RegistryDatasetConfig 引用 |
| `AgentName` | 默认值直接用字符串 `"oracle"` | 仅用于 AgentConfig 默认值 |
| `ServiceVolumeBind/Volume/Image` (TypedDict) | 用 `dict[str, Any]` 替代 | EnvironmentConfig.mounts_json 的子类型 |
| `LocalTaskId` / `GitTaskId` / `TaskPaths` | 不拷贝 | 仅被运行时方法使用 |

#### DatasetConfig 简化方案

Harbor 原生有 `LocalDatasetConfig` 和 `RegistryDatasetConfig` 两个子类，各自有复杂的 `get_task_configs()` 方法。Job SDK 只需序列化字段，合并为一个：

```python
class DatasetConfig(BaseModel):
    """简化的 dataset 配置，兼容 Harbor YAML 的 datasets 字段。"""
    # 通用字段 (from BaseDatasetConfig)
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
    registry_url: str | None = None       # 简化: 替代 RemoteRegistryInfo
    registry_path: Path | None = None     # 简化: 替代 LocalRegistryInfo
```

### 2.3 类图

```
JobConfig (Pydantic, rock/sdk/agent/models/job/config.py)
│
│  ── Rock 扩展字段 ──
├── sandbox: SandboxConfig              # rock.sdk.sandbox.config
├── setup_commands: list[str]           # harbor run 前的准备命令
├── result_file: str                    # sandbox 内 result JSON 路径
├── collect_trajectory: bool            # 是否回传 trajectory 数据
├── auto_start_sandbox: bool            # 自动启动 sandbox
├── auto_stop_sandbox: bool             # 运行结束后自动关闭 sandbox
│
│  ── Harbor 原生字段（从 Harbor 拷贝，保持字段名一致） ──
├── job_name: str                       # Job 名称（默认按时间生成）
├── jobs_dir: Path                      # 输出目录
├── n_attempts: int                     # 每个 task 的重试次数
├── timeout_multiplier: float           # 全局超时倍率
├── orchestrator: OrchestratorConfig    # models/job/config.py
│   ├── type: OrchestratorType          # models/orchestrator_type.py
│   ├── n_concurrent_trials: int
│   ├── quiet: bool
│   └── retry: RetryConfig              # models/job/config.py
├── environment: EnvironmentConfig      # models/trial/config.py
│   ├── type: EnvironmentType           # models/environment_type.py
│   ├── force_build / delete
│   ├── override_cpus / memory_mb
│   └── env / kwargs
├── agents: list[AgentConfig]           # models/trial/config.py
├── datasets: list[DatasetConfig]       # models/job/config.py (简化合并)
├── tasks: list[TaskConfig]             # models/trial/config.py
├── metrics: list[MetricConfig]         # models/metric/config.py
└── artifacts: list[ArtifactConfig]     # models/trial/config.py

Job (rock/sdk/agent/job.py)
├── __init__(config: JobConfig, sandbox?: Sandbox)
├── async run() -> JobResult            # 完整生命周期
├── async submit() -> str               # 异步提交，返回 job_id
├── async wait(job_id) -> JobResult     # 等待已提交的 job
├── async cancel(job_id)                # 取消运行中的 job
└── async get_result() -> JobResult     # 获取结果

JobResult (rock/sdk/agent/job.py)
├── job_id: str
├── status: JobStatus
├── trials: list[TrialResult]
├── raw_output: str
├── exit_code: int
├── score: float (property)
└── n_completed / n_failed (property)

TrialResult (rock/sdk/agent/job.py)
├── task_name: str
├── status: JobStatus
├── score: float
├── rewards: dict[str, float]
├── trajectory_path: str
├── token_ids: list[int]
├── duration_sec: float
└── error: str | None
```

### 2.4 设计原则

**从 Harbor 拷贝 schema，不依赖 harbor 包。**

1. **零额外依赖** — 训练节点只需 `rock-sdk`（已有 pydantic），无需 harbor 及其传递依赖（litellm, Docker SDK, shortuuid 等）
2. **仅拷贝字段定义** — 去掉 `get_task_configs()`、`get_task_id()` 等运行时方法，保留纯 Pydantic schema
3. **YAML 兼容** — 字段名和类型与 Harbor 完全一致，序列化的 YAML 可被 `harbor jobs start -c` 直接加载
4. **目录结构对齐** — `rock/sdk/agent/models/` 与 `harbor/models/` 保持同构，便于对照同步

### 2.5 与现有组件的关系

```
┌─────────────────────────────────────────────────────────────┐
│  RL Training Framework (verl / SkyRL)                       │
│                                                             │
│  AgenticTaskService                                         │
│  ├── SweRlServer     (HTTP → swe-rl-server)                │
│  ├── AgentService    (HTTP → rock admin /jobs)              │
│  ├── HarborService   (Python → harbor Job API) ← 现有方式   │
│  └── RockJobService  (Rock SDK → sandbox bash) ← 新增方式   │
│           │                                                 │
│           ▼                                                 │
│      rock.sdk.agent.job.Job                                 │
│           │                                                 │
│           ▼                                                 │
│      rock.sdk.sandbox.Sandbox                               │
│      ├── .start()                                           │
│      ├── .create_session()                                  │
│      ├── .arun(mode=NOHUP)     ← bash 协议核心              │
│      ├── .arun(mode=NORMAL)    ← 读取结果文件               │
│      └── .close()                                           │
└─────────────────────────────────────────────────────────────┘
```

## 3. 核心流程

### 3.1 完整生命周期 (`Job.run()`)

```
1. Start sandbox (if auto_start)
       │
       ▼
2. Create bash session (env_enable=True, inject agent env vars)
       │
       ▼
3. Run setup_commands (pip install, git clone, upload configs)
       │
       ▼
4. Serialize JobConfig (Harbor 部分) → YAML, upload to sandbox
       │
       ▼
5. Build harbor command:
   "harbor jobs start -c {uploaded_config.yaml}"
       │
       ▼
6. Execute via sandbox.arun(cmd, mode=NOHUP, wait_timeout=timeout)
       │
       ▼
7. Collect results:
   - cat {result_file} → parse JSON → TrialResult list
   - (optional) download trajectory files
       │
       ▼
8. Stop sandbox (if auto_stop)
       │
       ▼
9. Return JobResult
```

**关键步骤 4-5：** Job SDK 将 `JobConfig` 中 Harbor 原生部分序列化为 YAML，上传到 sandbox 内作为 harbor 的配置文件。这样 harbor CLI 可以直接加载，无需逐个传递 CLI 参数。

### 3.2 异步提交模式 (`Job.submit()` + `Job.wait()`)

对于 RL 训练场景，训练框架需要并发提交多个 rollout 任务：

```python
# 批量提交
jobs = []
for task in task_batch:
    config = JobConfig(sandbox=sandbox_cfg, tasks=[task], agents=[agent_cfg], ...)
    job = Job(config)
    job_id = await job.submit()  # 非阻塞，sandbox.arun(NOHUP) 启动后立即返回
    jobs.append((job, job_id))

# 并发等待
results = await asyncio.gather(*[job.wait(jid) for job, jid in jobs])
rewards = [r.score for r in results]
```

`submit()` 内部流程：
1. 启动 sandbox + session（同 `run()`）
2. 用 `start_nohup_process()` 启动 harbor 命令，获取 PID
3. 立即返回 job_id（PID-based）

`wait()` 内部流程：
1. 用 `wait_for_process_completion(pid)` 轮询进程状态
2. 进程结束后用 `handle_nohup_output()` 获取输出
3. 读取 result_file 解析 TrialResult

### 3.3 与 verl AgenticTaskService 集成

新增 `RockJobService` adapter，实现 `AgenticTaskService` 接口：

```python
class RockJobService(AgenticTaskService):
    """
    通过 Rock Job SDK 在 sandbox 中执行 harbor run，
    替代 HarborService 的 Python API 直接调用方式。
    """

    async def submit_task(self, task_config: AgenticTaskConfig) -> TaskId:
        # 1. 从 AgenticTaskConfig 构建 JobConfig
        # 2. 创建 Job 实例
        # 3. 调用 job.submit() 获取 job_id
        # 4. 返回 TaskId(job_id)

    async def get_task_status(self, task_id: TaskId) -> AgenticTaskStatus:
        # 通过 sandbox.arun("kill -0 {pid}") 检查进程状态

    async def get_task_result(self, task_id: TaskId) -> AgenticTaskResult:
        # 1. 调用 job.wait() 等待完成
        # 2. 将 JobResult 转换为 AgenticTaskResult
        # 3. 提取 score, trajectory, extra_fields
```

## 4. 配置设计

### 4.1 JobConfig 字段总览

JobConfig 分为两部分：Harbor 原生字段直接透传给 `harbor jobs start`，Rock 扩展字段控制 sandbox 运行时。

#### Rock 扩展字段

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `sandbox` | `SandboxConfig` | (required) | Rock sandbox 配置 |
| `setup_commands` | `list[str]` | `[]` | harbor 运行前的准备命令 |
| `result_file` | `str` | `""` | 结果文件路径，空则自动推断 |
| `collect_trajectory` | `bool` | `False` | 是否下载 trajectory 数据 |
| `auto_start_sandbox` | `bool` | `True` | 自动启动 sandbox |
| `auto_stop_sandbox` | `bool` | `False` | 完成后自动关闭 sandbox |

#### Harbor 原生字段（核心）

| 字段 | 类型 | 默认值 | 对应 harbor CLI |
|---|---|---|---|
| `job_name` | `str` | 时间戳 | `--job-name` |
| `jobs_dir` | `Path` | `"jobs"` | `-o/--jobs-dir` |
| `n_attempts` | `int` | `1` | `-k/--n-attempts` |
| `timeout_multiplier` | `float` | `1.0` | `--timeout-multiplier` |
| `orchestrator` | `OrchestratorConfig` | `{}` | `-n`, `-r`, `--orchestrator` |
| `environment` | `EnvironmentConfig` | `{}` | `-e`, `--ek`, `--ee` |
| `agents` | `list[AgentConfig]` | `[oracle]` | `-a`, `-m`, `--ak`, `--ae` |
| `datasets` | `list[DatasetConfig]` | `[]` | `-d`, `--dataset` |
| `tasks` | `list[TaskConfig]` | `[]` | `-p/--path` |
| `metrics` | `list[MetricConfig]` | `[]` | (YAML only) |
| `artifacts` | `list[ArtifactConfig]` | `[]` | (YAML only) |
| `debug` | `bool` | `False` | `--debug` |

### 4.2 配置示例

```python
from rock.sdk.agent.job import Job
from rock.sdk.agent.models.job.config import JobConfig
from rock.sdk.agent.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig
from rock.sdk.agent.models.metric.config import MetricConfig
from rock.sdk.sandbox.config import SandboxConfig

config = JobConfig(
    # ── Rock 扩展 ──
    sandbox=SandboxConfig(
        image="harbor-runner:latest",
        base_url="http://rock-admin:8080",
        cluster="zb",
        memory="16g",
        cpus=4,
    ),
    setup_commands=["pip install harbor --quiet"],

    # ── Harbor 原生（与 Harbor YAML / CLI 一一对应） ──
    jobs_dir="/workspace/jobs",
    n_attempts=1,
    environment=EnvironmentConfig(
        type="docker",
        force_build=True,
        delete=True,
    ),
    agents=[AgentConfig(
        name="terminus-2",
        model_name="hosted_vllm/my-model",
        kwargs={"max_iterations": 30, "collect_rollout_details": True},
        env={"LLM_API_KEY": "sk-xxx", "LLM_BASE_URL": "http://vllm:8000/v1"},
    )],
    datasets=[DatasetConfig(
        name="terminal-bench",
        version="2.0",
        n_tasks=50,
    )],
    metrics=[MetricConfig(type="mean")],
)

job = Job(config)
result = await job.run()
```

### 4.3 从 Harbor YAML 加载

已有 Harbor YAML 配置文件可以直接加载，只需补充 `sandbox` 配置：

```python
config = JobConfig.from_yaml(
    "/path/to/harbor-config.yaml",
    sandbox=SandboxConfig(
        image="harbor-runner:latest",
        base_url="http://rock-admin:8080",
    ),
)
job = Job(config)
result = await job.run()
```

### 4.4 Rock sandbox 镜像要求

运行 Harbor 的 sandbox 容器需要预装：
- Python 3.10+ 和 `harbor` CLI (`pip install harbor`)
- 如果 `environment.type = docker`：需要 Docker-in-Docker 支持
- 如果 `environment.type = local`：任务直接在 sandbox 内执行，无需额外容器

## 5. 数据流设计

### 5.1 RL Rollout 数据收集

```
                         Rock Sandbox
                    ┌──────────────────────────┐
                    │  harbor jobs start        │
                    │    ├── Trial 1            │
                    │    │   ├── agent.run()    │
                    │    │   │   └── LLM calls  │ ← token_ids, logprobs
                    │    │   └── verifier       │ ← rewards
                    │    ├── Trial 2            │
                    │    └── ...                │
                    │                          │
                    │  Outputs:                 │
                    │  ├── {jobs_dir}/{job_name}/result.json  │
                    │  ├── {jobs_dir}/{job_name}/trials/*/    │
                    │  │   ├── result.json                    │
                    │  │   └── trajectory.jsonl               │ ← ATIF format
                    │  └── {jobs_dir}/{job_name}/traces/      │ ← ShareGPT export
                    └──────────────────────────┘
                              │
                    Rock SDK (read_file / cat)
                              │
                              ▼
                    JobResult.trials[i]
                    ├── score: float
                    ├── rewards: {"reward": 1.0}
                    ├── token_ids: [...]       ← from trajectory ATIF
                    └── trajectory_path: str
```

### 5.2 结果文件解析

Harbor 生成的 `result.json` 结构：

```json
{
  "job_name": "my-job",
  "stats": {
    "n_trials": 50,
    "n_errors": 2,
    "evals": {
      "terminal-bench": {
        "metrics": {"mean": 0.72}
      }
    }
  },
  "trial_results": [
    {
      "trial_name": "trial-001",
      "task_name": "fix-dockerfile-syntax",
      "started_at": "2026-03-27T10:00:00Z",
      "finished_at": "2026-03-27T10:05:30Z",
      "verifier_result": {
        "rewards": {"reward": 1.0}
      },
      "agent_result": {
        "n_input_tokens": 15000,
        "n_output_tokens": 3000,
        "rollout_details": [
          {
            "prompt_token_ids": [[...]],
            "completion_token_ids": [[...]],
            "logprobs": [[...]]
          }
        ]
      },
      "exception_info": null
    }
  ]
}
```

Job SDK 的 `_collect_results()` 读取该文件并映射到 `TrialResult`。

## 6. 使用示例

### 6.1 基本用法 — TB2 Benchmark

```python
from rock.sdk.agent.job import Job
from rock.sdk.agent.models.job.config import JobConfig, DatasetConfig
from rock.sdk.agent.models.trial.config import AgentConfig
from rock.sdk.sandbox.config import SandboxConfig

config = JobConfig(
    sandbox=SandboxConfig(
        image="harbor-runner:latest",
        base_url="http://rock-admin:8080",
        cluster="zb",
        memory="16g",
        cpus=4,
    ),
    agents=[AgentConfig(
        name="terminus-2",
        model_name="hosted_vllm/my-model",
        env={"LLM_API_KEY": "sk-xxx"},
    )],
    datasets=[DatasetConfig(name="terminal-bench", version="2.0", n_tasks=50)],
    setup_commands=["pip install harbor --quiet"],
)

job = Job(config)
result = await job.run()

print(f"Score: {result.score}")
print(f"Completed: {result.n_completed}, Failed: {result.n_failed}")
for trial in result.trials:
    print(f"  {trial.task_name}: {trial.score} ({trial.status})")
```

### 6.2 RL 训练集成 — verl Rollout

```python
from rock.sdk.agent.job import Job
from rock.sdk.agent.models.job.config import JobConfig
from rock.sdk.agent.models.trial.config import AgentConfig, TaskConfig
from rock.sdk.sandbox.config import SandboxConfig

class RockJobService(AgenticTaskService):
    """Rock Job SDK adapter for verl RL training."""

    def __init__(self, config: OmegaConf):
        super().__init__(config)
        self._sandbox_config = SandboxConfig(
            image=config.image,
            base_url=config.rock_base_url,
            cluster=config.cluster,
            memory=config.get("memory", "16g"),
        )
        self._jobs: dict[TaskId, Job] = {}

    async def submit_task(self, task_config: AgenticTaskConfig) -> TaskId:
        job_config = JobConfig(
            sandbox=self._sandbox_config,
            agents=[AgentConfig(
                name=task_config.agent_config.scaffold,
                model_name=f"hosted_vllm/{task_config.llm_config.model_name}",
                env={
                    "LLM_API_KEY": task_config.llm_config.api_key,
                    "LLM_BASE_URL": task_config.infer_callback_url,
                },
            )],
            tasks=[TaskConfig(
                path=f"/workspace/tasks/{task_config.data_config.instance_id}",
            )],
            timeout_multiplier=task_config.runtime_config.task_timeout_sec / 3600,
            collect_trajectory=True,
        )

        job = Job(job_config)
        job_id = await job.submit()
        self._jobs[TaskId(job_id)] = job
        return TaskId(job_id)

    async def get_task_result(self, task_id: TaskId) -> AgenticTaskResult:
        job = self._jobs[task_id]
        result = await job.wait(task_id)

        return AgenticTaskResult(
            status=AgenticTaskStatus.FINISHED if result.status == "completed" else AgenticTaskStatus.FAILED,
            score=result.score,
            trajectory={"messages": []},
            extra_fields={
                "empty_patch": result.n_completed == 0,
                "token_ids": result.trials[0].token_ids if result.trials else None,
            },
        )
```

### 6.3 批量并发 Rollout

```python
import asyncio
from rock.sdk.agent.job import Job
from rock.sdk.agent.models.job.config import JobConfig
from rock.sdk.agent.models.trial.config import AgentConfig, TaskConfig
from rock.sdk.sandbox.config import SandboxConfig, SandboxGroupConfig

# 预创建 sandbox 池
sandbox_pool = SandboxGroup(SandboxGroupConfig(
    image="harbor-runner:latest",
    base_url="http://rock-admin:8080",
    size=8,
    start_concurrency=4,
))
await sandbox_pool.start()

# 并发提交 rollout
async def run_rollout(sandbox, task):
    job = Job(
        config=JobConfig(
            sandbox=sandbox.config,
            tasks=[task],
            agents=[AgentConfig(name="terminus-2", model_name="hosted_vllm/my-model")],
            auto_start_sandbox=False,
        ),
        sandbox=sandbox,
    )
    return await job.run()

results = await asyncio.gather(*[
    run_rollout(sandbox_pool[i], tasks[i])
    for i in range(len(tasks))
])

rewards = [r.score for r in results]
```

## 7. 与 HarborService 的对比

| 维度 | HarborService (现有) | Rock Job SDK (新) |
|---|---|---|
| Harbor 安装位置 | 训练节点 | Sandbox 容器内 |
| 调用方式 | Python API (`harbor.Job`) | bash CLI (`harbor jobs start`) |
| 配置模型 | Harbor `JobConfig` (Python import) | 同 schema 拷贝到 `rock/sdk/agent/models/` |
| 依赖隔离 | 差 — Harbor + Docker + agents 全在训练节点 | 好 — 训练节点只需 `rock-sdk` |
| 并发模型 | `submit_task()` 同步阻塞 | `submit()` 异步 + `wait()` 轮询 |
| 能力完整度 | 需手动实现 dataset/registry/resume | 复用 Harbor CLI 全部能力 |
| 适用场景 | 单进程少量任务 | 大规模 RL 训练 rollout |

## 8. 关键设计决策

### Q1: 为什么从 Harbor 拷贝 schema 而不是 import harbor?

- **零依赖**: harbor 包带来 litellm、Docker SDK、shortuuid 等大量传递依赖，不应污染训练节点
- **只需 schema**: Job SDK 只需将配置序列化为 YAML 传入 sandbox，不需要 Harbor 的运行时方法
- **拷贝量小**: 9 个 Pydantic model + 3 个 enum，目录结构与 Harbor 对齐，便于后续同步
- **YAML 兼容**: 字段名和类型与 Harbor 完全一致，生成的 YAML 可被 `harbor jobs start -c` 直接加载

### Q2: 为什么用 bash 命令而不是在训练节点直接调 harbor Python API?

- **隔离性**: Harbor 有大量依赖（litellm, Docker SDK, agent packages），不应污染训练节点
- **版本独立**: sandbox 镜像可以固定 Harbor 版本，不受训练环境影响
- **CLI 完整能力**: `harbor jobs start` 包含 dataset registry、resume、trace export 等完整功能
- **与 Rock 环境模型一致**: Harbor 自身的 `RockEnvironment` 也是通过 Rock SDK 的 bash 协议工作的

### Q3: 为什么用 NOHUP 模式?

- Harbor job 执行时间可能很长（几分钟到数小时）
- NOHUP 模式允许：进程在后台运行、SDK 轮询状态、超时可控
- 与 Rock 的 `RockAgent.run()` 模式一致

### Q4: 结果如何传回?

- Harbor 将结果写入 `{jobs_dir}/{job_name}/result.json`
- Job SDK 通过 `sandbox.arun("cat {path}")` 读取
- 对于 trajectory 等大文件，使用 `sandbox.read_file()` 或 `sandbox.fs.download_file()` 下载

### Q5: 如何支持 sandbox 复用?

- `Job.__init__` 接受可选的 `sandbox` 参数
- `auto_start_sandbox=False` 跳过启动，直接复用
- 支持 `SandboxGroup` 池化管理多个 sandbox

## 9. 后续工作

1. **实现 `rock/sdk/agent/models/`** — 从 Harbor 拷贝 config schema
2. **实现 `rock/sdk/agent/job.py`** — Job, JobResult, TrialResult
3. **实现 `RockJobService`** — 作为 `AgenticTaskService` adapter，供 verl 直接使用
4. **构建 Harbor runner 镜像** — 预装 harbor CLI + 常用 agent 的 Docker 镜像
5. **集成测试** — 在 Rock 测试集群上验证 TB2 benchmark 端到端运行
6. **Trajectory 回传优化** — 评估直接通过共享存储（NFS/OSS）传递 trajectory 的可行性
