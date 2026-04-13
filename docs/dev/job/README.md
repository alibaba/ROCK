# Rock Job 架构重构方案

## 1. 现状分析

### 1.1 当前实现

| 组件 | 位置 | 职责 | 问题 |
|------|------|------|------|
| `JobCommand` | `rock/cli/command/job.py` | CLI `rock job run`，直接用 Sandbox SDK 执行脚本 | 功能简单，无抽象，不支持 Harbor |
| `Job` | `rock/sdk/agent/job.py` | Harbor benchmark 执行，完整 submit/wait 生命周期 | 与 Harbor 强耦合，不可扩展到其他任务类型 |
| `JobConfig` | `rock/sdk/agent/models/job/config.py` | Harbor 配置 schema（从 Harbor 拷贝） | 既有 Rock 环境字段又有 Harbor 字段，混合关注点 |
| `JobResult` | `rock/sdk/agent/models/job/result.py` | Harbor 结果模型 | 仅支持 Harbor trial 结构 |

### 1.2 核心问题

1. **Job = Harbor** — 当前 `Job` 类硬编码了 Harbor 的 YAML 生成、dockerd 启动、trial 结果收集逻辑，无法支持纯 Bash 脚本任务
2. **CLI 与 SDK 断层** — `rock job run`（CLI）直接操作 Sandbox，完全不经过 Job SDK
3. **无调度抽象** — 单一 sandbox 执行，无 map/并行/分片能力
4. **无数据抽象** — 数据输入硬编码在 Harbor dataset config 中，无法支持 CSV/Pandas 等通用数据源

### 1.3 重构目标

```
用户视角:
  rock job run --type bash --script train.sh                    # BashJob via CLI
  rock job run --type harbor --config harbor.yaml               # HarborJob via CLI
  rock job run --type bash --script eval.sh --map-from data.csv # BashJob + Map调度

SDK 视角:
  job = BashJob(config)           # 简单脚本执行
  job = HarborJob(config)         # Harbor benchmark
  results = await asyncio.gather(*[Job(c).run() for c in configs])  # 并行
  result = await job.run()
```

### 1.4 使用场景

| 调用方 | 场景 | 需要的能力 |
|--------|------|-----------|
| **rock CLI** | `rock job run` 执行脚本或 Harbor benchmark | BashJob、HarborJob，简单配置 |
| **verl / roll** | RL 训练 rollout，批量提交评测任务 | HarborJob，submit/wait 异步，并发执行 |
| **评测平台** | 批量评测多数据集，收集结果 | Map 调度，数据源输入，结果聚合 |
| **数据处理** | 分布式 data pipeline | BashJob + Map，CSV/Pandas 输入，分片并行 |

---

## 2. 架构方案: Config 驱动类型 + Trial 内部抽象 + Job 编排层

**核心思路**: Job 是 Facade 入口。JobExecutor 是主控方，驱动 Operator（算子）。Operator 决定分发 8 份 Trial（类比 torch.distributed.scatter），JobExecutor 并行执行 TrialList。

```
┌────────────────────────────────────────────────────────────┐
│  Job (Facade) — 极薄入口                                    │
│  Job(config, operator?).run() / .submit() / .wait()         │
│                                                            │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  JobExecutor (主控方)                                  │  │
│  │                                                      │  │
│  │  run = submit + wait                                 │  │
│  │                                                      │  │
│  │  submit(operator, config):                           │  │
│  │    1. operator.apply(config)                          │  │
│  │       → 创建 Trial, 决定分发数量                        │  │
│  │       → 返回 list[AbstractTrial]  (TrialList)          │  │
│  │    2. 并行 _(do_submit(trial) for each task            │  │
│  │       → sandbox + trial.setup/build + nohup           │  │
│  │    → 返回 JobClient                                   │  │
│  │                                                      │  │
│  │  wait(job_client):                                   │  │
│  │    → _do_wait(tc) for each TrialClient                │  │
│  │      → trial.collect(sandbox, output, exit_code)      │  │
│  │    → 返回 list[TrialResult]                           │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                            │
│  Operator (算子, 可替换):                                    │
│   ├── ScatterOperator(size=8) — 分发 8 份 Trial              │
│   └── (future: RayScatterOperator, K8sOperator, ...)       │
│                                                            │
│  Trial (内部, 由 Config 子类决定):                              │
│   ├── BashTrial     ← BashJobConfig                         │
│   ├── HarborTrial   ← HarborJobConfig                      │
│   └── (扩展...)                                             │
└────────────────────────────────────────────────────────────┘
```

#### 设计原则

1. **Job = 极薄 Facade** — 只有 `config` + `operator` 两个参数
2. **JobExecutor = 主控方** — 驱动 Operator 生成 TrialList，并行执行，收集结果
3. **Operator = 算子 (通用接口 apply)** — 从 config 生成 TrialList，ScatterOperator 是默认实现
4. **Trial = 任务逻辑** — 数据 IO、脚本生成、结果解析，全部由 Trial 自行控制
5. **Config 子类 = 类型** — `BashJobConfig` / `HarborJobConfig` 携带类型信息

#### 调用链

```
Job.run() = Job.submit() + Job.wait()

submit 阶段:
  Job.submit()
    → JobExecutor.submit(operator, config)
      1. trial_list = operator.apply(config)     # Operator 决定分发 8 份 Trial
         → _create_trial(config)                    # 创建 Trial
         → 返回 [trial] * size                      # size=0 返回 [], 什么都不做
      2. 并行 _(do_submit(trial) for trial in trial_list  # Executor 并行启动
      → 返回 JobClient(tasks=[TrialClient, ...])

wait 阶段:
  Job.wait()
    → JobExecutor.wait(job_client)
      → _do_wait(tc) for each TrialClient            # 等待 + trial.collect()
      → 返回 list[TrialResult]                       # size=0 时返回 []
```

**关键**:
- **Operator 只做 apply** — 生成 TrialList，不启动 sandbox，不管执行
- **JobExecutor 做并行执行** — 拿到 TrialList 后并行 submit + 并行 wait
- **size=0 安全返回** — Operator 返回空 list，Executor 什么都不执行，Job 返回空结果

#### 职责分离

| 组件 | 职责 | 不负责 | 类比 |
|------|------|--------|------|
| **Job** | 极薄 Facade: 接收 config，组装组件 | 不包含任何执行/调度逻辑 | — |
| **Operator** | 算子: apply(config) → TrialList | 不启动 sandbox，不执行 | torch.distributed.scatter |
| **JobExecutor** | 主控方: 并行执行 TrialList，管理 sandbox 生命周期 | 不决定分发数量 | Executor |
| **Trial** | 任务逻辑: setup/build/collect | 不管理 sandbox 生命周期 | UDF |

#### 类设计

```python
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Config 层: 用户直接接触，Config 子类决定 Job 类型
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class JobConfig(BaseModel):
    """Job 基础配置 — 所有 Job 类型共享的字段"""
    environment: RockEnvironmentConfig      # sandbox 环境
    job_name: str | None = None
    namespace: str | None = None
    experiment_id: str | None = None
    labels: dict[str, str] = {}
    auto_stop: bool = False
    setup_commands: list[str] = []          # 前置命令
    file_uploads: list[tuple[str, str]] = [] # 文件上传
    env: dict[str, str] = {}                # 环境变量
    timeout: int = 3600                     # 超时


class BashJobConfig(JobConfig):
    """Bash 脚本 Job 配置"""
    script: str | None = None               # 脚本内容 (与 script_path 二选一)
    script_path: str | None = None          # 脚本文件路径


class HarborJobConfig(JobConfig):
    """Harbor benchmark Job 配置

    Harbor 原生字段直接平铺（与现有 rock.sdk.agent.JobConfig 的 Harbor 字段一致），
    序列化为 YAML 传给 harbor jobs start -c
    """
    agents: list[AgentConfig] = Field(default_factory=lambda: [AgentConfig()])
    datasets: list[LocalDatasetConfig | RegistryDatasetConfig] = Field(default_factory=list)
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    verifier: VerifierConfig = Field(default_factory=VerifierConfig)
    tasks: list[TaskConfig] = Field(default_factory=list)
    metrics: list[MetricConfig] = Field(default_factory=list)
    artifacts: list[str | ArtifactConfig] = Field(default_factory=list)
    n_attempts: int = 1
    timeout_multiplier: float = 1.0
    agent_timeout_multiplier: float | None = None
    verifier_timeout_multiplier: float | None = None
    jobs_dir: Path = Path(USER_DEFINED_LOGS) / "jobs"
    debug: bool = False

    def to_harbor_yaml(self) -> str: ...

    @classmethod
    def from_yaml(cls, path: str) -> HarborJobConfig: ...


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Trial 层: 定义"在 sandbox 中做什么"，三阶段接口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AbstractTrial(ABC):
    """Task: 定义在 Sandbox 中执行什么

    三阶段接口:
      setup()   — 执行前: 准备环境 (上传文件、写入配置)
      build()   — 构建: 生成要执行的脚本
      collect() — 执行后: 从 sandbox 收集解析结果

    Trial 不管理 sandbox 生命周期 (由 JobExecutor 负责)，
    也不关心调度策略 (由 Operator 负责)。
    """

    def __init__(self, config: JobConfig):
        self._config = config

    @abstractmethod
    async def setup(self, sandbox: Sandbox) -> None:
        """执行前: 准备 sandbox 环境 — 上传文件、写入配置"""

    @abstractmethod
    def build(self) -> str:
        """构建: 生成要在 sandbox 中执行的 bash 脚本"""

    @abstractmethod
    async def collect(self, sandbox: Sandbox, output: str, exit_code: int) -> TrialResult:
        """执行后: 从 sandbox 收集并解析结果"""

    async def _upload_files(self, sandbox: Sandbox) -> None:
        """共享: 上传 config.file_uploads"""
        for local_path, sandbox_path in self._config.file_uploads:
            await sandbox.fs.upload_dir(local_path, sandbox_path)


class BashTrial(AbstractTrial):
    """Bash 脚本执行"""

    async def setup(self, sandbox):
        await self._upload_files(sandbox)
        if self._config.script_path:
            self._config.script = Path(self._config.script_path).read_text()

    def build(self) -> str:
        setup = "\n".join(self._config.setup_commands) or "true"
        return f"#!/bin/bash\nset -e\n{setup}\n{self._config.script}"

    async def collect(self, sandbox, output, exit_code):
        return TrialResult(
            task_id=self._config.job_name or "",
            status=TrialStatus.COMPLETED if exit_code == 0 else TrialStatus.FAILED,
            output=output,
            exit_code=exit_code,
        )


class HarborTrial(AbstractTrial):
    """Harbor benchmark 执行 (重构自现有 rock.sdk.agent.job.Job)"""

    async def setup(self, sandbox):
        await self._upload_files(sandbox)
        yaml_content = self._config.to_harbor_yaml()
        await self._upload_content(sandbox, yaml_content, self._config_path)

    def build(self) -> str:
        return _HARBOR_SCRIPT_TEMPLATE.format(...)

    async def collect(self, sandbox, output, exit_code):
        trial_results = await self._collect_trial_results(sandbox)
        return TrialResult(
            task_id=self._config.job_name or "",
            status=TrialStatus.COMPLETED if trial_results else TrialStatus.FAILED,
            output=output, exit_code=exit_code,
            trial_results=trial_results,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Trial Registry: Config 类型 → Trial 实现
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_TRIAL_REGISTRY: dict[type[JobConfig], type[AbstractTrial]] = {
    BashJobConfig: BashTrial,
    HarborJobConfig: HarborTrial,
}

def register_trial(config_type: type[JobConfig], task_type: type[AbstractTrial]):
    """注册新的 Config → Task 映射 (扩展点)"""
    _TRIAL_REGISTRY[config_type] = task_type

def _create_trial(config: JobConfig) -> AbstractTrial:
    """根据 config 类型创建对应的 Task 实例"""
    task_cls = _TRIAL_REGISTRY.get(type(config))
    if task_cls is None:
        raise TypeError(
            f"No task registered for {type(config).__name__}. "
            f"Supported: {[c.__name__ for c in _TRIAL_REGISTRY]}"
        )
    return task_cls(config)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Operator: 算子基类 — 通用接口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class Operator(ABC):
    """算子基类: 从 config 生成 TrialList

    通用接口 apply(config) → list[AbstractTrial]:
      - 输入: JobConfig
      - 输出: TrialList (待执行的 Task 列表)
      - 返回空 list 表示什么都不做

    Operator 只负责生成 TrialList，不启动 sandbox，不执行。
    执行由 JobExecutor 负责。
    """

    @abstractmethod
    def apply(self, config: JobConfig) -> list[AbstractTrial]:
        """从 config 生成 TrialList

        返回空 list 表示什么都不做。
        """
        ...


class ScatterOperator(Operator):
    """Scatter 算子: 将 config 分发 8 份 Trial

    类比 torch.distributed.scatter:
      scatter_list = [tensor] * size   → 每个 rank 拿一份
      trial_list    = [trial] * size     → 每个 sandbox 执行一份

    用法:
      ScatterOperator()         # size=1，默认 1 份 Task
      ScatterOperator(size=8)   # 8 份 Task，由 JobExecutor 并行执行
      ScatterOperator(size=0)   # 什么都不做，返回 []
    """

    def __init__(self, size: int = 1):
        self.size = size

    def apply(self, config) -> list[AbstractTrial]:
        if self.size <= 0:
            return []
        trial = _create_trial(config)
        return [trial] * self.size


# 未来扩展 — 不同的 Operator 实现:
#
# class DataScatterOperator(Operator):
#     """数据驱动: 按数据列表生成 TrialList，每个 Task 注入不同 env"""
#     def __init__(self, data: list[dict[str, str]]):
#         self.data = data
#     def apply(self, config):
#         if not self.data:
#             return []
#         return [
#             _create_trial(config.model_copy(update={"env": {**config.env, **shard}}))
#             for shard in self.data
#         ]
#
# class RayScatterOperator(Operator):
#     """Ray 分发: 生成 TrialList，JobExecutor 通过 ray.remote 执行"""
#
# class K8sOperator(Operator):
#     """K8s 分发: 生成 TrialList，JobExecutor 创建 K8s Job 执行"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# JobExecutor: 主控方 — 驱动 Operator，管理 sandbox 生命周期
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class JobExecutor:
    """执行引擎 + 主控方

    职责:
      1. 调用 operator.apply(config) 获取 TrialList
      2. 并行启动每个 Task (_do_submit)
      3. 并行等待完成 + 收集结果 (_do_wait)

    JobExecutor 负责并行执行，Operator 只负责生成 TrialList。
    """

    # ── run = submit + wait ──

    async def run(self, operator: Operator, config: JobConfig) -> list[TrialResult]:
        """完整生命周期: submit + wait"""
        job_client = await self.submit(operator, config)
        return await self.wait(job_client)

    # ── submit: Operator 生成 TrialList → 并行启动 ──

    async def submit(self, operator: Operator, config: JobConfig) -> JobClient:
        """Operator apply 生成 TrialList，并行启动所有 sandbox"""
        # 1. Operator 决定分发 8 份 Trial
        trial_list = operator.apply(config)

        # 2. size=0 → 什么都不做
        if not trial_list:
            return JobClient(tasks=[])

        # 3. 并行启动每个 Task
        task_clients = await asyncio.gather(*[
            self._(do_submit(trial) for trial in trial_list
        ])
        return JobClient(tasks=list(task_clients))

    # ── wait: 并行等待 + 收集结果 ──

    async def wait(self, job_client: JobClient) -> list[TrialResult]:
        """并行等待所有 task 完成，收集结果"""
        if not job_client.tasks:
            return []
        return list(await asyncio.gather(*[
            self._do_wait(tc) for tc in job_client.tasks
        ]))

    # ── 内部: 单个 task 的 submit / wait ──

    async def _do_submit(self, trial: AbstractTrial) -> TrialClient:
        """启动单个 sandbox + 执行脚本 (被 Operator 回调)"""
        config = task._config  # Task 已持有 config

        sandbox = Sandbox(config.environment)
        await sandbox.start()

        session = f"rock-job-{config.job_name or 'default'}"
        await sandbox.create_session(
            CreateBashSessionRequest(
                session=session, env_enable=True, env=config.env or None,
            )
        )

        # Task: setup → build
        await trial.setup(sandbox)
        script_content = trial.build()

        # 上传脚本 + nohup 启动
        script_path = f"/tmp/rock_job_{config.job_name}.sh"
        await self._upload_script(sandbox, script_content, script_path)
        pid, error = await sandbox.start_nohup_process(
            cmd=f"bash {script_path}", session=session,
            tmp_file=f"/tmp/rock_job_{config.job_name}.out",
        )
        if error:
            raise RuntimeError(f"Failed to start task: {error.output}")

        return TrialClient(sandbox=sandbox, session=session, pid=pid, task=task)

    async def _do_wait(self, client: TrialClient) -> TrialResult:
        """等待单个 task 完成，调用 trial.collect() 收集结果"""
        config = client.trial._config
        try:
            success, message = await client.sandbox.wait_for_process_completion(
                pid=client.pid, session=client.session, wait_timeout=config.timeout,
            )
            obs = await client.sandbox.handle_nohup_output(
                tmp_file=f"/tmp/rock_job_{config.job_name}.out",
                session=client.session, success=success, message=message,
            )
            result = await client.trial.collect(client.sandbox, obs.output or "", obs.exit_code or 1)
            if not success:
                result.status = TrialStatus.FAILED
            return result
        finally:
            if config.auto_stop:
                await client.sandbox.close()

    async def _upload_script(self, sandbox, content: str, path: str) -> None:
        """上传脚本内容到 sandbox"""
        ...


@dataclass
class TrialClient:
    """单个运行中 task 的句柄 (config 通过 task._config 访问)"""
    sandbox: Sandbox
    session: str
    pid: int
    trial: AbstractTrial

@dataclass
class JobClient:
    """Job.submit() 返回的句柄，持有多个 TrialClient，类比 Flink JobClient"""
    tasks: list[TrialClient]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Job: 极薄 Facade — 用户唯一入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class Job:
    """Job Facade: 极薄入口层

    只有两个参数: config + operator。
    不包含执行逻辑、调度逻辑 — 全部委托给 JobExecutor。

    调用链:
      Job.run() = Job.submit() + Job.wait()
        submit: → JobExecutor.submit(operator, config)
                    → operator.apply(config) → TrialList      # 算子生成 N 份 Task
                    → 并行 _(do_submit(trial) for each          # Executor 并行启动
                    → 返回 JobClient
        wait:   → JobExecutor.wait(job_client)
                    → 并行 _do_wait(tc) for each              # 等待 + trial.collect()
                    → 返回 list[TrialResult]

    用法:
        # 单次执行
        result = await Job(BashJobConfig(script="echo hello")).run()

        # 异步 submit/wait
        job = Job(config)
        await job.submit()
        result = await job.wait()

        # 并行: 每个 config 一个 Job，asyncio.gather 并发
        results = await asyncio.gather(*[Job(c).run() for c in configs])
    """

    def __init__(
        self,
        config: JobConfig,
        operator: Operator | None = None,
    ):
        self._config = config
        self._executor = JobExecutor()
        self._operator = operator or ScatterOperator()
        self._job_client: JobClient | None = None

    async def run(self) -> JobResult:
        """完整生命周期: submit + wait"""
        await self.submit()
        return await self.wait()

    async def submit(self) -> None:
        """非阻塞提交"""
        self._job_client = await self._executor.submit(self._operator, self._config)

    async def wait(self) -> JobResult:
        """等待完成"""
        if not self._job_client:
            raise RuntimeError("No submitted job. Call submit() first.")
        task_results = await self._executor.wait(self._job_client)
        return self._build_result(task_results)

    async def cancel(self) -> None:
        """取消运行中的 job"""
        if self._job_client:
            for tc in self._job_client.tasks:
                await tc.sandbox.arun(cmd=f"kill {tc.pid}", session=tc.session)

    def _build_result(self, task_results: list[TrialResult]) -> JobResult:
        all_success = all(r.success for r in task_results)
        return JobResult(
            job_id=self._config.job_name or "",
            status=JobStatus.COMPLETED if all_success else JobStatus.FAILED,
            labels=self._config.labels,
            task_results=task_results,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Result 模型
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TrialResult(BaseModel):
    """单个 Task 的执行结果"""
    task_id: str = ""
    status: TrialStatus = TrialStatus.COMPLETED
    output: str = ""
    exit_code: int = 0
    data: dict[str, Any] = {}           # 通用数据输出
    trial_results: list[TrialResult] = []  # Harbor 专用

    @property
    def score(self) -> float: ...

    @property
    def success(self) -> bool:
        return self.status == TrialStatus.COMPLETED

class JobResult(BaseModel):
    """Job 聚合结果"""
    job_id: str = ""
    status: JobStatus = JobStatus.COMPLETED
    labels: dict[str, str] = {}
    task_results: list[TrialResult] = []

    @property
    def score(self) -> float: ...

    @property
    def n_completed(self) -> int: ...

    @property
    def n_failed(self) -> int: ...

    # 便捷属性: 直接访问第一个 TrialResult 的 trial_results (Harbor 兼容)
    @property
    def trial_results(self) -> list[TrialResult]:
        if self.task_results:
            return self.task_results[0].trial_results
        return []
```

#### 使用示例

```python
# ── 1. 单次 Bash Job ──
result = await Job(BashJobConfig(
    script="echo 'hello world'",
    environment=RockEnvironmentConfig(image="python:3.11"),
)).run()
# 内部: Job.submit()
#   → JobExecutor.submit(ScatterOperator(size=1), config)
#     → ScatterOperator.apply(config) → [BashTrial]       (1 份)
#     → _do_submit(BashTrial) → sandbox → trial.setup/build
#   Job.wait() → _do_wait → trial.collect

# ── 2. 单次 Harbor Benchmark ──
result = await Job(HarborJobConfig(
    environment=RockEnvironmentConfig(image="harbor-runner:latest", memory="16g"),
    setup_commands=["pip install harbor --quiet"],
    agents=[AgentConfig(name="terminus-2", model_name="hosted_vllm/my-model")],
    datasets=[RegistryDatasetConfig(registry=OssRegistryInfo(split="v2.0"), name="tb")],
    auto_stop=True, experiment_id="exp-001",
)).run()

# ── 3. Harbor from YAML ──
result = await Job(HarborJobConfig.from_yaml("job_config.yaml")).run()

# ── 4. 异步 submit/wait ──
job = Job(HarborJobConfig(...))
await job.submit()
# ... 做其他事 ...
result = await job.wait()

# ── 5. 并行: 多个 Job + asyncio.gather ──
import asyncio

async def run_one(tc):
    return await Job(HarborJobConfig(
        environment=base_env, agents=[agent_cfg], tasks=[tc],
        auto_stop=True, experiment_id="exp-001",
    )).run()

results = await asyncio.gather(*[run_one(tc) for tc in task_batch])
rewards = [r.score for r in results]

# ── 6. 评测平台: CSV → 多个 Job → 信号量控制并发 ──
import csv

with open("eval_tasks.csv") as f:
    rows = list(csv.DictReader(f))

sem = asyncio.Semaphore(8)

async def run_eval(row):
    async with sem:
        return await Job(BashJobConfig(
            script="python eval.py",
            env={"TASK_ID": row["task_id"], "INPUT": row["input_path"]},
            environment=RockEnvironmentConfig(image="eval-runner:latest"),
            auto_stop=True,
        )).run()

results = await asyncio.gather(*[run_eval(row) for row in rows])

# ── 7. 自定义 Trial 类型 ──
class EvalJobConfig(JobConfig):
    eval_script: str
    model_endpoint: str

class EvalTask(AbstractTrial):
    async def setup(self, sandbox):
        await sandbox.fs.upload_dir(self._config.eval_script, "/workspace/")

    def build(self) -> str:
        return f"python /workspace/{self._config.eval_script} --endpoint {self._config.model_endpoint}"

    async def collect(self, sandbox, output, exit_code):
        result_content = await sandbox.read_file(ReadFileRequest(path="/workspace/result.json"))
        return TrialResult(output=output, exit_code=exit_code, data=json.loads(result_content.content))

register_trial(EvalJobConfig, EvalTask)
result = await Job(EvalJobConfig(eval_script="eval.py", model_endpoint="http://...")).run()
```

#### CLI 集成

```python
# rock/cli/command/job.py

class JobCommand(Command):
    name = "job"

    async def arun(self, args):
        if args.job_command == "run":
            job_type = args.type or "bash"

            if job_type == "bash":
                config = BashJobConfig(
                    script=args.script_content,
                    script_path=args.script,
                    file_uploads=[(args.local_path, args.target_path)] if args.local_path else [],
                    environment=RockEnvironmentConfig(image=args.image, ...),
                    timeout=args.timeout,
                )
            elif job_type == "harbor":
                config = HarborJobConfig.from_yaml(args.config)
                if args.image:
                    config.environment.image = args.image

            result = await Job(config).run()

    @staticmethod
    async def add_parser_to(subparsers):
        job_parser = subparsers.add_parser("job", help="Manage sandbox jobs")
        job_sub = job_parser.add_subparsers(dest="job_command")

        run = job_sub.add_parser("run", help="Run a job in a sandbox")
        run.add_argument("--type", choices=["bash", "harbor"], default="bash")
        run.add_argument("--script", help="Script file path")
        run.add_argument("--script-content", help="Inline script content")
        run.add_argument("--config", help="Harbor YAML config path")
        run.add_argument("--image", help="Sandbox image")
        run.add_argument("--memory", help="Memory (e.g. 8g)")
        run.add_argument("--cpus", type=float, help="CPU count")
        run.add_argument("--timeout", type=int, default=3600)
        run.add_argument("--local-path", help="Local dir to upload")
        run.add_argument("--target-path", default="/root/job")
```

#### 向后兼容

```
现有代码 (rock/sdk/agent)                新架构 (rock/sdk/job)
─────────────────────────────          ─────────────────────────────
from rock.sdk.agent import             from rock.sdk.job import
  Job, JobConfig                         Job, HarborJobConfig

job = Job(JobConfig(                   job = Job(HarborJobConfig(
  experiment_id="exp",                   experiment_id="exp",
  agents=[...],                          agents=[...],
  datasets=[...],                        datasets=[...],
))                                     ))
result = await job.run()               result = await job.run()
result.score                           result.score
result.trial_results                   result.trial_results  ← 兼容属性
```

**兼容策略: 两套并存，逐步迁移**

1. `rock/sdk/agent/` **完全不动** — 现有 `Job(JobConfig(...))` 继续工作
2. `rock/sdk/job/` **全新模块** — 新架构，新能力
3. `HarborJobConfig` 的 Harbor 字段与现有 `JobConfig` 完全一致 — 迁移只需改 import + 类名
4. `JobResult.trial_results` 提供便捷属性 — 兼容 `result.trial_results` 访问方式
5. 未来标记 `rock.sdk.agent.Job` 为 deprecated，引导到 `rock.sdk.job.Job`

现有调用方 (`examples/harbor/harbor_demo.py`, `tests/unit/sdk/agent/`, verl 集成) **零改动**。

#### 优点

- **职责清晰** — Job(Facade) / JobExecutor(主控+执行) / Operator(算子) / Task(逻辑) 各司其职
- **Operator 可扩展** — ABC 基类，ScatterOperator 默认实现，未来可加 DataScatterOperator / RayScatterOperator
- **三层都是 run=submit+wait** — Job / JobExecutor / 内部全统一为 submit+wait 模式
- **Trial 接口清晰** — `setup/build/collect` 三阶段，语义明确
- **`Job(config)` 签名一致** — 用户只需记 Config 子类
- **注册表扩展** — `register_trial()` 支持第三方扩展
- **完全兼容** — `rock/sdk/agent/` 不动

#### 缺点

- **组件多** — 4 个核心组件（Job, JobExecutor, Operator, Task），理解成本略高
- **两套代码暂时并存** — `rock/sdk/agent/Job` 和 `rock/sdk/job/Job`

#### 重构影响

| 现有组件 | 变更 |
|---------|------|
| `rock/sdk/agent/` | **不动**，保持向后兼容 |
| `rock/sdk/job/` (新) | Job(Facade), JobExecutor, Operator, Task |
| `rock/sdk/job/trial/harbor.py` | 从 `rock/sdk/agent/job.py` 提取 setup/build/collect |
| `rock/sdk/job/trial/harbor.py` | 复用 `rock/sdk/agent/models/` 的 Harbor schema |
| `rock/cli/command/job.py` | 使用新 `rock.sdk.job.Job`，支持 `--type` |
| 现有测试/示例 | **不改**，新模块新增独立测试 |

---

## 3. 备选方案对比

在确定当前方案前，评估了另外两种方案:

| 维度 | A: Job 层继承 | B: 策略模式 | **当前方案: Config 驱动** |
|------|-------------|-----------|------------------------|
| **核心思路** | `AbstractJob` → `BashJob` / `HarborJob` 子类 | 单一 `Job` + `TaskStrategy` / `Operator` / `DataSource` 组合 | `Job(config)` Config 子类决定类型，Task 内部 factory |
| **用户 API** | `BashJob(BashJobConfig(...))` | `Job(config, task_strategy=BashTrialStrategy())` | `Job(BashJobConfig(...))` |
| **Trial 类型扩展** | 新增 Job 子类 + Config 子类 | 新增 Strategy 类 | 新增 Config 子类 + Trial 子类 + register |
| **调度策略** | Scheduler 挂在 Job 上，继承限制组合 | Strategy 可替换 | Operator 可替换 |
| **类型安全** | 强 | 弱 (Config 混合所有字段) | 强 |
| **使用门槛** | 中 (需记 BashJob/HarborJob 类名) | 高 (需组合三个 Strategy) | **低** (只需记 Config 子类) |
| **向后兼容** | 一般 (Job 类要改) | 差 (接口全变) | **好** (`rock/sdk/agent` 不动) |
| **`Job(config)` 签名** | 不同子类不同构造 | 需要额外 strategy 参数 | **统一 `Job(config)`** |

**选择当前方案的理由**: 保持 `Job(config)` 统一签名，Config 子类携带类型信息（无需额外参数），Task 作为内部实现细节不暴露给用户，同时 `rock/sdk/agent` 完全不动实现零成本兼容。

---

## 4. 文件结构

```
rock/sdk/job/                           # 新模块 (核心只有 6 个文件)
├── __init__.py                         # 公开导出
├── job.py                              # Job Facade (极薄)
├── executor.py                         # JobExecutor 执行引擎
├── operator.py                         # Operator ABC + ScatterOperator (scatter 算子)
├── config.py                           # JobConfig, BashJobConfig, HarborJobConfig
├── result.py                           # JobResult, TrialResult, TrialStatus
└── trial/
    ├── __init__.py
    ├── abstract.py                     # AbstractTrial (setup/build/collect)
    ├── registry.py                     # _TRIAL_REGISTRY + register_trial()
    ├── bash.py                         # BashTrial
    └── harbor.py                       # HarborTrial (复用 agent/models)

rock/sdk/agent/                         # 保留不动，完全向后兼容
├── __init__.py
├── job.py
├── models/                             # Harbor schema (HarborTrial 复用)
└── constants.py
```

---

## 5. 迁移路径

```
Phase 1: Config + Task + JobExecutor
  - 创建 rock/sdk/job/ 模块
  - JobConfig → BashJobConfig / HarborJobConfig 继承体系
  - AbstractTrial (setup/build/collect), BashTrial, HarborTrial + _TRIAL_REGISTRY
  - JobExecutor: sandbox 生命周期 + 调用 Trial 三阶段
  - HarborTrial 从 rock/sdk/agent/job.py 提取逻辑，复用 agent/models

Phase 2: Job Facade + Operator + Result
  - Job(config, operator?) 极薄 Facade
  - ScatterOperator(size) 默认 scatter 算子
  - Job.run_batch() 批量并行
  - TrialResult / JobResult 结果模型

Phase 3: 更新 CLI
  - rock job run --type bash/harbor

Phase 4: 标记旧接口 deprecated
  - rock/sdk/agent/Job + JobConfig 标记 DeprecationWarning
  - 文档引导迁移到 rock/sdk/job
```

---

## 6. Flink / Ray Data 对比分析

对比 Flink 和 Ray Data 的核心设计模式，评估当前方案的完善点。

### 6.1 核心架构对比

| 维度 | Flink | Ray Data | Rock Job (当前方案) |
|------|-------|----------|-------------------|
| **核心抽象** | Operator → Task → SubTask (DAG) | Dataset → Transform chain (lazy pipeline) | Job(Facade) → JobExecutor(执行) → Task(逻辑) |
| **执行模型** | 流式 DAG，operator chaining 自动融合 | 惰性 pipeline，`.show()` 触发流式执行 | JobExecutor 管理 sandbox 生命周期，nohup 模式 |
| **调度与执行** | Scheduler 分配 slot，TaskManager 执行 | Scheduler 放置 task，Worker 执行 | JobExecutor 驱动 Operator，Operator 回调执行 |
| **并行度** | 每个 operator 独立 parallelism | 每个 operation 独立 `concurrency` + `num_cpus/gpus` | ScatterOperator `size` 控制 Trial 数量 |
| **数据输入** | Source (SourceReader + SplitEnumerator) | Datasource ABC (`read_csv`, `read_parquet`, custom) | Task.setup() 自行控制 |
| **数据输出** | Sink (SinkWriter) | Datasink ABC (`write_parquet`, `write_json`, custom) | Task.collect() 自行控制 |
| **容错** | Checkpoint + Savepoint + 自动重启策略 | Ray Core task retry + lineage reconstruction | Job.run_batch(max_retries=N) |
| **状态管理** | 丰富的 Job 状态机 + 指标累加器 | progress bar + per-operation metrics | JobStatus enum，无进度跟踪 |
| **Pipeline** | Operator chain → JobGraph → ExecutionGraph 三级图 | `ds.map().map_batches().filter()` 链式调用 | 单步，无 pipeline |

### 6.2 可借鉴的设计 + 当前方案改进建议

#### 对比总结: 数据 IO 策略

| | Flink | Ray Data | Rock Job |
|---|---|---|---|
| **数据输入** | Source 是 DAG 中的 operator | `read_csv()` / `Datasource` ABC | **Task.setup() 自行控制** |
| **数据输出** | Sink 是 DAG 中的 operator | `write_parquet()` / `Datasink` ABC | **Task.collect() 自行控制** |
| **为什么不同** | Flink/Ray 是数据处理引擎，数据流是核心 | 同左 | Rock 是 sandbox 执行引擎，数据 IO 是 Trial 逻辑的一部分 |

Rock 不需要 DataSource/DataSink 抽象的原因:
- Flink/Ray 的 operator 是轻量级函数，需要框架管理数据流
- Rock 的 Task 运行在独立 sandbox 中，有完整文件系统，Task 自己读写数据更自然
- 批量并行通过 `Job.run_batch(configs)` 实现，每个 config 自带不同的输入参数

#### 已吸收的改进

| # | 改进项 | 参考 | 实现方式 |
|---|--------|------|---------|
| 1 | **Retry** — 失败重试 | Flink RestartStrategy, Ray task retry | `Job.run_batch(max_retries=N)` |
| 2 | **JobExecutor** — 独立执行引擎 | Flink JobExecutor, Ray Worker | `JobExecutor` 类 |
| 3 | **调度与执行分离** | Flink Scheduler ≠ TaskManager | `JobExecutor`(主控) 驱动 `Operator`(算子) |
| 4 | **Per-job Resource** — 每个 job 不同资源 | Ray Data per-op resources | 每个 config 有独立 `environment` |

#### 未来方向 (不在第一版)

| # | 方向 | 参考 |
|---|------|------|
| 1 | **Pipeline** — 多 Job 串联 | Flink DAG, Ray Data pipeline chain |
| 2 | **Progress** — 批量执行进度回调 | Ray progress bar |
| 3 | **DataSource 抽象** — 如果 Task.setup() 模式不够用 | Ray `read_datasource()` |

### 6.6 不需要借鉴的设计

| Flink / Ray Data 特性 | 不采用原因 |
|----------------------|----------|
| **Operator DAG / Pipeline chain** | Rock 的执行单元是 sandbox（重量级容器），不是轻量级 operator。DAG 在 sandbox 粒度上没有意义 |
| **Operator Fusion** | 同上，Rock 不做 operator-level 的计算融合 |
| **Streaming 执行** | Rock Job 是 batch 模式（提交 → 等待 → 收集），sandbox 不支持流式数据传递 |
| **Checkpoint / Savepoint** | Rock sandbox 是无状态的一次性容器，不需要持久化计算状态 |
| **Type System** | Flink 的 TypeInformation 用于序列化优化，Rock 的数据通过文件/环境变量传递，不需要类型系统 |
