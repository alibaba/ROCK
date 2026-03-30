# Rock Job SDK 设计文档

## 1. 愿景与目标

### 问题

当前 RL 训练框架 (verl/SkyRL) 集成 Harbor benchmark 的方式是通过 HTTP adapter (`AgentService`, `HarborService` in `docs/dev/agent/example.py`)，存在以下问题：

- **HarborService 是同步阻塞的** — `submit_task()` 内部直接 `await job.run()`，无法真正并发
- **依赖 Harbor Python SDK 安装在训练节点** — Harbor 及其依赖（Docker、agent 包等）需要与 RL 训练框架共存
- **配置分散** — Rock sandbox 配置通过 `environment_kwargs` 传递，Harbor 配置通过 Python API 构建，没有统一入口
- **无法复用 Harbor CLI 的完整能力** — 直接调 Python API 需要手动处理 dataset、registry、resume 等逻辑

### 目标

设计 `rock/sdk/agent/job.py`，基于 Rock SDK 的 bash 协议，封装 high-level Job SDK：

1. **在 Rock sandbox 内执行 `harbor jobs start`** — Harbor 运行在容器内，训练节点只需 Rock SDK
2. **支持 TB2 benchmark 运行** — 作为 `AgenticTaskService` 的新 adapter，直接对接 verl 的 rollout 接口
3. **支持 agentic RL 训练** — RL 框架通过 Job SDK 提交 rollout 任务，收集 reward/trajectory/token_ids

### 核心思路

```
RL Trainer (verl)                      Rock Sandbox (container)
┌─────────────────┐                   ┌──────────────────────────┐
│                  │   Rock SDK        │                          │
│  Job.submit()   │──────────────────>│  bash script:            │
│                  │   bash protocol   │  - dockerd startup       │
│  Job.wait()     │                   │  - setup_commands        │
│                  │                   │  - harbor jobs start     │
│                  │<──────────────────│                          │
│  JobResult      │   read files      │  trial result.json       │
└─────────────────┘                   └──────────────────────────┘
```

不再在训练节点 import harbor，而是在 sandbox 容器内通过 bash 命令执行 `harbor jobs start`，利用 Rock 的 nohup 模式异步等待结果。

## 2. 架构设计

### 2.1 文件结构

从 Harbor 源码拷贝字段定义，仅保留 schema，去掉运行时方法。目录组织与 Harbor 原始结构保持一致：

```
rock/sdk/agent/
├── __init__.py                      # 公开导出
├── job.py                           # Job 类（核心入口）
└── models/                          # 从 Harbor 拷贝的 config schema（仅字段定义）
    ├── __init__.py
    ├── environment_type.py          # EnvironmentType (enum)
    ├── orchestrator_type.py         # OrchestratorType (enum)
    ├── job/
    │   ├── __init__.py
    │   ├── config.py                # JobConfig, OrchestratorConfig, RetryConfig
    │   │                            # OssRegistryInfo, RemoteRegistryInfo, LocalRegistryInfo
    │   │                            # BaseDatasetConfig, LocalDatasetConfig, RegistryDatasetConfig
    │   └── result.py                # JobResult, JobStatus
    ├── trial/
    │   ├── __init__.py
    │   ├── config.py                # AgentConfig, EnvironmentConfig, RockEnvironmentConfig
    │   │                            # VerifierConfig, TaskConfig, ArtifactConfig
    │   └── result.py                # TrialResult, ExceptionInfo, AgentInfo, ModelInfo
    │                                # AgentResult, VerifierResult, TimingInfo
    └── metric/
        ├── __init__.py
        ├── config.py                # MetricConfig
        └── type.py                  # MetricType (enum)
```

对应 Harbor 原始路径的映射：

| Harbor 路径 | Rock 路径 | 说明 |
|---|---|---|
| `harbor/models/job/config.py` | `rock/sdk/agent/models/job/config.py` | 去掉 `get_task_configs()` 等方法 |
| `harbor/models/job/result.py` | `rock/sdk/agent/models/job/result.py` | JobResult, JobStatus |
| `harbor/models/trial/config.py` | `rock/sdk/agent/models/trial/config.py` | 去掉 `get_task_id()` 等方法 |
| `harbor/models/trial/result.py` | `rock/sdk/agent/models/trial/result.py` | TrialResult 及子模型 |
| `harbor/models/metric/config.py` | `rock/sdk/agent/models/metric/config.py` | 原样拷贝 |
| `harbor/models/metric/type.py` | `rock/sdk/agent/models/metric/type.py` | 原样拷贝 |
| `harbor/models/orchestrator_type.py` | `rock/sdk/agent/models/orchestrator_type.py` | 原样拷贝 |
| `harbor/models/environment_type.py` | `rock/sdk/agent/models/environment_type.py` | 原样拷贝 |

### 2.2 模型类清单

#### 配置模型（Pydantic BaseModel，仅字段定义）

| 类名 | 来源 | Rock 目标文件 | 依赖 |
|---|---|---|---|
| `JobConfig` | `harbor/models/job/config.py` | `models/job/config.py` | 下列所有 |
| `OrchestratorConfig` | `harbor/models/job/config.py` | `models/job/config.py` | RetryConfig, OrchestratorType |
| `RetryConfig` | `harbor/models/job/config.py` | `models/job/config.py` | (leaf) |
| `OssRegistryInfo` | `harbor/models/registry.py` | `models/job/config.py` | (leaf) |
| `RemoteRegistryInfo` | `harbor/models/registry.py` | `models/job/config.py` | (leaf) |
| `LocalRegistryInfo` | `harbor/models/registry.py` | `models/job/config.py` | (leaf) |
| `BaseDatasetConfig` | `harbor/models/dataset.py` | `models/job/config.py` | (leaf) |
| `LocalDatasetConfig` | `harbor/models/dataset.py` | `models/job/config.py` | BaseDatasetConfig |
| `RegistryDatasetConfig` | `harbor/models/dataset.py` | `models/job/config.py` | BaseDatasetConfig, RegistryInfo |
| `AgentConfig` | `harbor/models/trial/config.py` | `models/trial/config.py` | (leaf) |
| `EnvironmentConfig` | `harbor/models/trial/config.py` | `models/trial/config.py` | EnvironmentType |
| `VerifierConfig` | `harbor/models/trial/config.py` | `models/trial/config.py` | (leaf) |
| `TaskConfig` | `harbor/models/trial/config.py` | `models/trial/config.py` | (leaf) |
| `MetricConfig` | `harbor/models/metric/config.py` | `models/metric/config.py` | MetricType |
| `ArtifactConfig` | `harbor/models/trial/config.py` | `models/trial/config.py` | (leaf) |

#### 结果模型

| 类名 | 来源 | Rock 目标文件 | 说明 |
|---|---|---|---|
| `JobResult` | `harbor/models/job/result.py` | `models/job/result.py` | Job 级别结果，含 `trial_results` |
| `JobStatus` | `harbor/models/job/result.py` | `models/job/result.py` | 枚举：PENDING/RUNNING/COMPLETED/FAILED/CANCELLED |
| `TrialResult` | `harbor/models/trial/result.py` | `models/trial/result.py` | Trial 级别结果 |
| `ExceptionInfo` | `harbor/models/trial/result.py` | `models/trial/result.py` | 异常详情 |
| `AgentInfo` | `harbor/models/trial/result.py` | `models/trial/result.py` | Agent 信息（name, version, model_info） |
| `ModelInfo` | `harbor/models/trial/result.py` | `models/trial/result.py` | 模型信息（name, provider） |
| `AgentResult` | `harbor/models/trial/result.py` | `models/trial/result.py` | Agent 执行结果（tokens, rollout_details） |
| `VerifierResult` | `harbor/models/trial/result.py` | `models/trial/result.py` | Verifier 结果（rewards） |
| `TimingInfo` | `harbor/models/trial/result.py` | `models/trial/result.py` | 时间信息（started_at, finished_at） |

#### 枚举类型

| 枚举 | Rock 目标文件 | 值 |
|---|---|---|
| `OrchestratorType` | `models/orchestrator_type.py` | `LOCAL`, `QUEUE` |
| `EnvironmentType` | `models/environment_type.py` | `DOCKER`, `DAYTONA`, `E2B`, `MODAL`, `RUNLOOP`, `GKE`, `ROCK` |
| `MetricType` | `models/metric/type.py` | `SUM`, `MIN`, `MAX`, `MEAN`, `UV_SCRIPT` |

### 2.3 类图

```
JobConfig (Pydantic, rock/sdk/agent/models/job/config.py)
│
│  ── Rock environment (Rock sandbox + Harbor env, not serialized to Harbor YAML) ──
├── environment: RockEnvironmentConfig
│   ├── (from SandboxConfig) image, memory, cpus, cluster, base_url, startup_timeout, ...
│   ├── (from EnvironmentConfig) type, force_build, override_cpus, override_memory_mb, ...
│   ├── env: dict[str, str]            # reuses EnvironmentConfig.env — injected into sandbox session and passed to harbor
│   ├── setup_commands: list[str]       # commands to run before harbor
│   ├── file_uploads: list[tuple]       # files to upload: (local_path, sandbox_path)
│   └── auto_stop: bool                 # close sandbox after completion
│
│  ── Harbor native fields (serialized to YAML, passed to harbor CLI) ──
├── job_name: str                           # default: timestamp %Y-%m-%d__%H-%M-%S
├── jobs_dir: Path                          # 输出目录，默认 "jobs"
├── n_attempts: int                         # 每个 task 的重试次数
├── timeout_multiplier: float               # 全局超时倍率
├── agent_timeout_multiplier: float | None  # Agent 超时倍率
├── verifier_timeout_multiplier: float | None
├── agent_setup_timeout_multiplier: float | None
├── environment_build_timeout_multiplier: float | None
├── debug: bool                             # 调试模式
├── orchestrator: OrchestratorConfig
│   ├── type: OrchestratorType              # LOCAL 或 QUEUE
│   ├── n_concurrent_trials: int
│   ├── quiet: bool
│   ├── retry: RetryConfig
│   │   ├── max_retries: int
│   │   ├── include_exceptions: set[str] | None
│   │   ├── exclude_exceptions: set[str] | None
│   │   ├── wait_multiplier: float
│   │   ├── min_wait_sec / max_wait_sec: float
│   └── kwargs: dict[str, Any]
├── verifier: VerifierConfig
│   ├── override_timeout_sec / max_timeout_sec
│   └── disable: bool
├── agents: list[AgentConfig]
│   ├── name / import_path / model_name
│   ├── override_timeout_sec / override_setup_timeout_sec / max_timeout_sec
│   ├── kwargs / env: dict
├── datasets: list[LocalDatasetConfig | RegistryDatasetConfig]
│   ├── (Local) path: Path
│   ├── (Registry) registry: OssRegistryInfo | RemoteRegistryInfo | LocalRegistryInfo
│   ├── name / version / download_dir / overwrite
│   └── task_names / exclude_task_names / n_tasks
├── tasks: list[TaskConfig]
│   ├── path: Path
│   ├── git_url / git_commit_id / source
│   └── overwrite / download_dir
├── metrics: list[MetricConfig]
│   ├── type: MetricType
│   └── kwargs: dict
└── artifacts: list[str | ArtifactConfig]
    ├── (str) source path
    └── (ArtifactConfig) source / destination


Job (rock/sdk/agent/job.py)
│
├── __init__(config: JobConfig)             # 仅接受 JobConfig
├── async run() -> JobResult                # submit + wait 组合
├── async submit() -> None                  # 启动 sandbox，执行脚本
├── async wait() -> JobResult               # 等待完成，收集结果
├── async cancel()                          # 取消运行中的 job
│
├── Private:
│   ├── _sandbox: Sandbox                   # 内部创建的 Sandbox 实例
│   ├── _session: str                       # bash session 名称
│   ├── _pid: int                           # nohup 进程 PID
│   ├── _prepare_and_start()                # 上传文件 + 启动脚本
│   ├── _render_run_script()                # 渲染 bash 脚本模板
│   ├── _create_session()                   # 创建 bash session
│   ├── _collect_results()                  # 读取 trial result.json
│   ├── _get_wait_timeout()                 # 推断超时时间
│   └── _upload_content()                   # 上传文本内容


JobResult (rock/sdk/agent/models/job/result.py)
│
├── job_id: str                             # job_name
├── status: JobStatus                       # COMPLETED / FAILED / CANCELLED
├── trial_results: list[TrialResult]        # 注意：字段名是 trial_results
├── raw_output: str                         # nohup 输出
├── exit_code: int                          # 进程退出码
│
├── Properties:
│   ├── score: float                        # 平均 score
│   ├── n_completed: int                    # 成功 trial 数
│   └── n_failed: int                       # 失败 trial 数


TrialResult (rock/sdk/agent/models/trial/result.py)
│
├── task_name: str
├── trial_name: str
├── source: str | None
├── agent_info: AgentInfo                   # Agent 信息
│   ├── name / version
│   └── model_info: ModelInfo | None
│       └── name / provider
├── agent_result: AgentResult | None        # Agent 执行结果
│   ├── n_input_tokens / n_cache_tokens / n_output_tokens
│   ├── cost_usd
│   └── rollout_details: list[dict]         # 包含 token_ids, logprobs
├── verifier_result: VerifierResult | None  # Verifier 结果
│   └── rewards: dict[str, float | int]     # {"reward": 1.0}
├── exception_info: ExceptionInfo | None    # 异常详情（失败时）
│   ├── exception_type / exception_message / exception_traceback
│   └── occurred_at
├── started_at / finished_at: str | None    # ISO 时间戳
├── environment_setup / agent_setup / agent_execution / verifier: TimingInfo | None
│
├── Properties:
│   ├── score: float                        # 从 verifier_result.rewards["reward"] 取值
│   ├── status: str                         # "failed" 或 "completed"
│   ├── duration_sec: float                 # started_at → finished_at 间隔
│   └── token_ids: list[int]                # 从 rollout_details 提取
│
├── Class method:
│   └── from_harbor_json(data: dict)        # 解析 Harbor trial result.json
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
│      ├── .start_nohup_process() ← 启动脚本                  │
│      ├── .wait_for_process_completion() ← 等待完成          │
│      ├── .handle_nohup_output() ← 获取输出                  │
│      ├── .read_file() / .execute() ← 收集结果               │
│      └── .close()                                           │
└─────────────────────────────────────────────────────────────┘
```

## 3. 核心流程

### 3.1 执行脚本模板

Job SDK 将 setup_commands + harbor run 统一为一个 bash 脚本，通过 nohup 模式执行：

```bash
#!/bin/bash
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

# ── Setup commands ───────────────────────────────────────────────────
echo '>>> pip install harbor --quiet...'
pip install harbor --quiet

# ── Harbor run ───────────────────────────────────────────────────────
harbor jobs start -c /tmp/rock_job_xxx.yaml
```

**为什么合并为单一脚本？**
- 减少 sandbox.arun() 调用次数，降低网络开销
- dockerd 检测/启动逻辑复用 Harbor 自身的 Docker-in-Docker 支持
- setup_commands 可能包含 pip install 等长时间操作，统一在 nohup 进程中执行

### 3.2 完整生命周期 (`Job.run()`)

```
1. Job.__init__(config)
       │  验证 config 类型，初始化内部状态
       ▼
2. submit()
       │  ├─ Sandbox(environment).start()
       │  ├─ create_session(session_name, env=environment.env)
       │  ├─ upload environment.file_uploads (fs.upload_dir)
       │  ├─ upload Harbor YAML config (to_harbor_yaml())
       │  ├─ render bash script (dockerd + setup + harbor)
       │  └─ start_nohup_process(bash script) → pid, tmp_file
       │  返回 None
       ▼
3. wait()
       │  ├─ wait_for_process_completion(pid, timeout)
       │  ├─ handle_nohup_output(tmp_file) → raw_output
       │  └─ _collect_results()
       │      ├─ find {jobs_dir}/{job_name} -name result.json (depth 2)
       │      ├─ read each trial's result.json
       │      ├─ TrialResult.from_harbor_json(data)
       │      └─ assemble JobResult
       │  ├─ if environment.auto_stop: sandbox.close()
       │  返回 JobResult
       ▼
4. Return JobResult
```

### 3.3 异步提交模式 (`Job.submit()` + `Job.wait()`)

对于 RL 训练场景，训练框架需要并发提交多个 rollout 任务：

```python
# 批量提交（非阻塞）
jobs = []
for task in task_batch:
    config = JobConfig(
        environment=base_env,
        tasks=[task],
        agents=[agent_cfg],
    )
    job = Job(config)
    await job.submit()  # 启动后立即返回，不返回 job_id
    jobs.append(job)

# 并发等待
results = await asyncio.gather(*[job.wait() for job in jobs])
rewards = [r.score for r in results]
```

**注意：**
- `submit()` 返回 `None`，不再返回 `job_id` 字符串
- Job 实例内部持有 `_sandbox`、`_session`、`_pid`，用于后续 `wait()` 和 `cancel()`
- 每个 Job 对应一个独立的 Sandbox，不支持复用外部 Sandbox

### 3.4 结果收集方式

**关键设计：** Harbor 的 job 级 `result.json` **不包含** `trial_results` 字段，因此 Job SDK 从每个 trial 的子目录中读取 `result.json`：

```
{jobs_dir}/{job_name}/
├── result.json              # job 级结果（不含 trial_results）
├── trials/
│   ├── trial-001/
│   │   └── result.json      # ← TrialResult 数据源
│   ├── trial-002/
│   │   └── result.json
│   └── ...
```

`_collect_results()` 使用 `find` 命令定位所有 trial 级 `result.json`，逐个解析为 `TrialResult`。

### 3.5 与 verl AgenticTaskService 集成

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
        # 3. 调用 job.submit()
        # 4. 返回 TaskId（可用 job.config.job_name）

    async def get_task_status(self, task_id: TaskId) -> AgenticTaskStatus:
        # 通过进程状态判断

    async def get_task_result(self, task_id: TaskId) -> AgenticTaskResult:
        # 1. 调用 job.wait() 等待完成
        # 2. 将 JobResult 转换为 AgenticTaskResult
        # 3. 提取 score, trajectory, extra_fields
```

## 4. 配置设计

### 4.1 JobConfig 字段总览

JobConfig 分为两部分：Harbor 原生字段直接透传给 `harbor jobs start`，Rock 扩展字段控制 sandbox 运行时。

#### Rock 环境字段（不序列化到 Harbor YAML，统一在 `environment: RockEnvironmentConfig` 内）

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `environment.image` | `str` | `"python:3.11"` | sandbox 镜像 |
| `environment.memory` | `str` | `"8g"` | 内存限制 |
| `environment.cpus` | `float` | `2` | CPU 限制 |
| `environment.cluster` | `str` | `"zb"` | 集群名 |
| `environment.base_url` | `str` | env var | Rock admin URL |
| `environment.setup_commands` | `list[str]` | `[]` | harbor 运行前的准备命令 |
| `environment.file_uploads` | `list[tuple[str, str]]` | `[]` | 上传文件/目录：(local_path, sandbox_path) |
| `environment.env` | `dict[str, str]` | `{}` | 复用 EnvironmentConfig.env — 注入 sandbox session，同时传给 harbor |
| `environment.auto_stop` | `bool` | `False` | 完成后自动关闭 sandbox |

#### Harbor 原生字段（核心）

| 字段 | 类型 | 默认值 | 对应 harbor CLI |
|---|---|---|---|
| `job_name` | `str` | 时间戳 | `--job-name` |
| `jobs_dir` | `Path` | `"jobs"` | `-o/--jobs-dir` |
| `n_attempts` | `int` | `1` | `-k/--n-attempts` |
| `timeout_multiplier` | `float` | `1.0` | `--timeout-multiplier` |
| `agent_timeout_multiplier` | `float \| None` | `None` | `--agent-timeout-multiplier` |
| `verifier_timeout_multiplier` | `float \| None` | `None` | `--verifier-timeout-multiplier` |
| `agent_setup_timeout_multiplier` | `float \| None` | `None` | `--agent-setup-timeout-multiplier` |
| `environment_build_timeout_multiplier` | `float \| None` | `None` | `--environment-build-timeout-multiplier` |
| `orchestrator` | `OrchestratorConfig` | `{}` | `-n`, `-r`, `--orchestrator` |
| `environment` | `RockEnvironmentConfig` | `RockEnvironmentConfig()` | `-e`, `--ek`, `--ee` |
| `agents` | `list[AgentConfig]` | `[AgentConfig()]` | `-a`, `-m`, `--ak`, `--ae` |
| `datasets` | `list[...]` | `[]` | `-d`, `--dataset` |
| `tasks` | `list[TaskConfig]` | `[]` | `-p/--path` |
| `metrics` | `list[MetricConfig]` | `[]` | (YAML only) |
| `artifacts` | `list[str \| ArtifactConfig]` | `[]` | (YAML only) |
| `debug` | `bool` | `False` | `--debug` |

#### DatasetConfig 结构

```python
# 本地 dataset
LocalDatasetConfig(
    path: Path,
    task_names: list[str] | None,
    exclude_task_names: list[str] | None,
    n_tasks: int | None,
)

# Registry dataset（OSS / Remote / Local registry）
RegistryDatasetConfig(
    registry: OssRegistryInfo | RemoteRegistryInfo | LocalRegistryInfo,
    name: str,
    version: str | None,
    overwrite: bool,
    download_dir: Path | None,
    task_names / exclude_task_names / n_tasks,
)

# OSS registry（内部存储）
OssRegistryInfo(
    split: str | None,
    revision: str | None,
    oss_dataset_path: str | None,
    oss_access_key_id / oss_access_key_secret / oss_region / oss_endpoint / oss_bucket,
)

# Remote registry（默认 GitHub）
RemoteRegistryInfo(
    name: str | None,
    url: str = "https://raw.githubusercontent.com/laude-institute/harbor/main/registry.json",
)

# Local registry
LocalRegistryInfo(
    name: str | None,
    path: Path,
)
```

### 4.2 配置示例

```python
from rock.sdk.agent import Job, JobConfig, RegistryDatasetConfig, AgentConfig, RockEnvironmentConfig, OssRegistryInfo

config = JobConfig(
    environment=RockEnvironmentConfig(
        image="harbor-runner:latest",
        base_url="http://rock-admin:8080",
        cluster="zb",
        memory="16g",
        cpus=4,
        setup_commands=["pip install harbor --quiet"],
        file_uploads=[
            ("/local/tasks", "/workspace/tasks"),
            ("/local/config.yaml", "/workspace/config.yaml"),
        ],
        env={
            "OSS_ACCESS_KEY_ID": "xxx",
            "OSS_ACCESS_KEY_SECRET": "yyy",
        },
        # Harbor advanced fields (optional)
        force_build=True,
        delete=True,
    ),
    jobs_dir="/workspace/jobs",
    n_attempts=1,
    agents=[AgentConfig(
        name="terminus-2",
        model_name="hosted_vllm/my-model",
        kwargs={"max_iterations": 30, "collect_rollout_details": True},
        env={"LLM_API_KEY": "sk-xxx", "LLM_BASE_URL": "http://vllm:8000/v1"},
    )],
    datasets=[RegistryDatasetConfig(
        registry=OssRegistryInfo(
            split="v2.0",
            oss_dataset_path="terminal-bench",
            oss_access_key_id="xxx",
            oss_access_key_secret="yyy",
            oss_region="cn-shanghai",
            oss_endpoint="oss-cn-shanghai.aliyuncs.com",
            oss_bucket="my-bucket",
        ),
        name="terminal-bench",
        n_tasks=50,
    )],
)

job = Job(config)
result = await job.run()
```

### 4.3 从 Harbor YAML 加载

YAML 文件中直接写 `environment:` 块，`from_yaml` 原样加载：

```python
config = JobConfig.from_yaml("/path/to/config.yaml")
job = Job(config)
result = await job.run()
```

### 4.4 Rock sandbox 镜像要求

运行 Harbor 的 sandbox 容器需要预装：
- Python 3.10+ 和 `harbor` CLI (`pip install harbor`)
- 如果 `environment.type = docker`：需要 Docker-in-Docker 支持（脚本会自动检测并启动 dockerd）
- 如果 `environment.type = rock`：任务通过 Rock SDK 执行，无需额外容器

## 5. 数据流设计

### 5.1 RL Rollout 数据收集

```
                         Rock Sandbox
                    ┌──────────────────────────┐
                    │  bash script (nohup)      │
                    │    ├─ dockerd startup     │
                    │    ├─ setup_commands      │
                    │    └─ harbor jobs start   │
                    │        ├── Trial 1        │
                    │        │   ├── agent.run()│
                    │        │   │   └── LLM    │ ← token_ids, logprobs
                    │        │   └── verifier   │ ← rewards
                    │        ├── Trial 2        │
                    │        └── ...            │
                    │                          │
                    │  Outputs:                 │
                    │  ├── {jobs_dir}/{job_name}/result.json (job 级)
                    │  ├── {jobs_dir}/{job_name}/trials/*/result.json (trial 级)
                    │  ├── {jobs_dir}/{job_name}/trials/*/trajectory.jsonl ← ATIF
                    │  └── {jobs_dir}/{job_name}/traces/ ← ShareGPT export
                    └──────────────────────────┘
                              │
                    Rock SDK (read_file / execute)
                              │
                              ▼
                    JobResult.trial_results[i]
                    ├── score: float (property)
                    ├── agent_result
                    │   ├── n_input_tokens / n_output_tokens
                    │   └── rollout_details: [{"completion_token_ids": [...]}]
                    ├── verifier_result.rewards: {"reward": 1.0}
                    ├── token_ids: list[int] (property, from rollout_details)
                    └── exception_info (if failed)
```

### 5.2 Trial result.json 结构

Harbor 生成的 trial 级 `result.json` 结构：

```json
{
  "task_name": "fix-dockerfile-syntax",
  "trial_name": "trial-001",
  "source": "terminal-bench",
  "started_at": "2026-03-27T10:00:00Z",
  "finished_at": "2026-03-27T10:05:30Z",
  "agent_info": {
    "name": "terminus-2",
    "version": "1.0",
    "model_info": {
      "name": "hosted_vllm/my-model",
      "provider": "vllm"
    }
  },
  "verifier_result": {
    "rewards": {"reward": 1.0}
  },
  "agent_result": {
    "n_input_tokens": 15000,
    "n_output_tokens": 3000,
    "cost_usd": 0.15,
    "rollout_details": [
      {
        "prompt_token_ids": [[...]],
        "completion_token_ids": [[...]],
        "logprobs": [[...]]
      }
    ]
  },
  "exception_info": null,
  "environment_setup": {"started_at": "...", "finished_at": "..."},
  "agent_setup": {"started_at": "...", "finished_at": "..."},
  "agent_execution": {"started_at": "...", "finished_at": "..."},
  "verifier": {"started_at": "...", "finished_at": "..."}
}
```

`TrialResult.from_harbor_json()` 解析此结构，计算属性 `score`、`status`、`duration_sec`、`token_ids`。

## 6. 使用示例

### 6.1 基本用法 — TB2 Benchmark

```python
from rock.sdk.agent import Job, JobConfig, RegistryDatasetConfig, AgentConfig, RockEnvironmentConfig, OssRegistryInfo

config = JobConfig(
    environment=RockEnvironmentConfig(
        image="harbor-runner:latest",
        base_url="http://rock-admin:8080",
        cluster="zb",
        memory="16g",
        cpus=4,
        setup_commands=["pip install harbor --quiet"],
        env={
            "OSS_ACCESS_KEY_ID": "xxx",
            "OSS_ACCESS_KEY_SECRET": "yyy",
        },
    ),
    agents=[AgentConfig(
        name="terminus-2",
        model_name="hosted_vllm/my-model",
        env={"LLM_API_KEY": "sk-xxx"},
    )],
    datasets=[RegistryDatasetConfig(
        registry=OssRegistryInfo(split="v2.0"),
        name="terminal-bench",
        n_tasks=50,
    )],
)

job = Job(config)
result = await job.run()

print(f"Score: {result.score}")
print(f"Completed: {result.n_completed}, Failed: {result.n_failed}")
for trial in result.trial_results:
    print(f"  {trial.task_name}: {trial.score} ({trial.status})")
```

### 6.2 RL 训练集成 — verl Rollout

```python
from rock.sdk.agent import Job, JobConfig, AgentConfig, TaskConfig, RockEnvironmentConfig

class RockJobService(AgenticTaskService):
    """Rock Job SDK adapter for verl RL training."""

    def __init__(self, config: OmegaConf):
        super().__init__(config)
        self._base_env = RockEnvironmentConfig(
            image=config.image,
            base_url=config.rock_base_url,
            cluster=config.cluster,
            memory=config.get("memory", "16g"),
            setup_commands=["pip install harbor --quiet"],
            auto_stop=True,
        )
        self._jobs: dict[TaskId, Job] = {}

    async def submit_task(self, task_config: AgenticTaskConfig) -> TaskId:
        job_config = JobConfig(
            environment=self._base_env.model_copy(update={
                "env": {
                    "LLM_API_KEY": task_config.llm_config.api_key,
                    "LLM_BASE_URL": task_config.infer_callback_url,
                },
            }),
            agents=[AgentConfig(
                name=task_config.agent_config.scaffold,
                model_name=f"hosted_vllm/{task_config.llm_config.model_name}",
            )],
            tasks=[TaskConfig(
                path=f"/workspace/tasks/{task_config.data_config.instance_id}",
            )],
            timeout_multiplier=task_config.runtime_config.task_timeout_sec / 3600,
        )

        job = Job(job_config)
        await job.submit()
        task_id = TaskId(job.config.job_name)
        self._jobs[task_id] = job
        return task_id

    async def get_task_result(self, task_id: TaskId) -> AgenticTaskResult:
        job = self._jobs[task_id]
        result = await job.wait()

        return AgenticTaskResult(
            status=AgenticTaskStatus.FINISHED if result.status == "completed" else AgenticTaskStatus.FAILED,
            score=result.score,
            trajectory={"messages": []},
            extra_fields={
                "empty_patch": result.n_completed == 0,
                "token_ids": result.trial_results[0].token_ids if result.trial_results else None,
            },
        )
```

### 6.3 批量并发 Rollout

```python
import asyncio
from rock.sdk.agent import Job, JobConfig, AgentConfig, TaskConfig, RockEnvironmentConfig

async def run_rollout(task_path, agent_config):
    """Single rollout task."""
    config = JobConfig(
        environment=RockEnvironmentConfig(
            image="harbor-runner:latest",
            base_url="http://rock-admin:8080",
            memory="16g",
            setup_commands=["pip install harbor --quiet"],
            env={"LLM_API_KEY": "sk-xxx"},
            auto_stop=True,
        ),
        tasks=[TaskConfig(path=task_path)],
        agents=[AgentConfig(
            name="terminus-2",
            model_name="hosted_vllm/my-model",
        )],
    )
    job = Job(config)
    return await job.run()

# 并发提交多个 rollout
tasks = ["/workspace/tasks/task-001", "/workspace/tasks/task-002", ...]
results = await asyncio.gather(*[run_rollout(t, agent_cfg) for t in tasks])
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
| 结果收集 | 从 job 级 result.json | 从 trial 级 result.json 逐个读取 |
| 适用场景 | 单进程少量任务 | 大规模 RL 训练 rollout |

## 8. 关键设计决策

### Q1: 为什么从 Harbor 拷贝 schema 而不是 import harbor?

- **零依赖**: harbor 包带来 litellm、Docker SDK、shortuuid 等大量传递依赖，不应污染训练节点
- **只需 schema**: Job SDK 只需将配置序列化为 YAML 传入 sandbox，不需要 Harbor 的运行时方法
- **拷贝量小**: 配置类 + 结果类约 20 个 Pydantic model，目录结构与 Harbor 对齐，便于后续同步
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

- Harbor 将每个 trial 结果写入 `{jobs_dir}/{job_name}/trials/*/result.json`
- Job SDK 通过 `find` 定位所有 result.json，逐个读取解析
- 对于 trajectory 等大文件，可使用 `sandbox.fs.download_file()` 下载

### Q5: 为什么合并为单一 bash 脚本?

- **减少调用开销**: 多次 `sandbox.arun()` 增加网络延迟，合并为一次 nohup 启动更高效
- **dockerd 自动化**: Harbor 使用 Docker 环境时需要 Docker-in-Docker，脚本内置 dockerd 检测/启动逻辑
- **setup_commands 集成**: pip install 等长时间操作统一在 nohup 进程中执行，避免阻塞

### Q6: 为什么 submit() 不返回 job_id?

- Job 实例内部持有所有必要状态（`_sandbox`, `_session`, `_pid`）
- 用户直接使用 Job 对象调用 `wait()` 或 `cancel()`，无需额外传递 job_id
- 如需标识符，可访问 `job.config.job_name`

## 9. 实现状态

| 项目 | 状态 |
|---|---|
| `rock/sdk/agent/models/` — 配置 schema 拷贝 | ✅ 完成 |
| `rock/sdk/agent/models/` — 结果模型 | ✅ 完成 |
| `rock/sdk/agent/job.py` — Job 类 | ✅ 完成 |
| `rock/sdk/agent/__init__.py` — 公开导出 | ✅ 完成 |
| Harbor runner 镜像 | 🔲 待构建 |
| `RockJobService` — verl adapter | 🔲 待实现 |
| 集成测试 | 🔲 待完成 |
| Trajectory 回传优化 | 🔲 待评估 |

## 10. 公开 API

从 `rock.sdk.agent` 导出的类：

```python
from rock.sdk.agent import (
    # 核心
    Job,
    JobResult,
    JobStatus,
    TrialResult,

    # 结果详情
    VerifierResult,
    AgentInfo,
    AgentResult,
    ExceptionInfo,

    # 配置
    JobConfig,
    RockEnvironmentConfig,
    RegistryDatasetConfig,
    LocalDatasetConfig,
    OssRegistryInfo,
    RemoteRegistryInfo,
    OrchestratorConfig,
    RetryConfig,
    AgentConfig,
    VerifierConfig,
    TaskConfig,
    ArtifactConfig,
    MetricConfig,
)
```