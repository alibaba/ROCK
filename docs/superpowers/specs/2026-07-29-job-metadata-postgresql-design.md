# Job 元数据 PostgreSQL 存储设计

## 1. 目标

将 ROCK SDK 中原本以 JSON 文档形式存储在 OSS 的 Job 元数据，改造成基于
PostgreSQL 的 Repository 存储。

SDK 运行在可信的独立元数据服务中。数据库连接地址和凭证由元数据服务管理，
服务通过 SQLAlchemy `sessionmaker` 将数据库访问能力注入 SDK。外部用户只能
调用元数据服务，不能直接连接 PostgreSQL，也不会获得数据库凭证。

本次改造只迁移 Job 和 Job Group 元数据。日志、trajectory、`result.json`
以及其他大文件继续保存在 OSS。

## 2. 范围与约束

- 生产数据库使用 PostgreSQL。
- SDK 通过 SQLAlchemy 直接读写 PostgreSQL，不经过 ROCK Admin。
- Job 可以独立存在，不属于任何 Group。
- 一个 Job 最多属于一个 Group。
- 一个 Group 可以包含零个或多个 Job。
- 同一 Group 内，一个非空 `task_id` 对应一个稳定的 Job 记录。
- `job_name` 不唯一，包括在相同 namespace 和 experiment 中也允许重复。
- `job_id` 和 `group_id` 使用 PostgreSQL 原生的 128-bit `UUID` 类型。
- UUID 由 SDK 在插入数据库前生成。
- 不保存环境变量、数据库或 OSS 凭证、labels 以及预留扩展字段。
- PostgreSQL 上线后是元数据的唯一数据源，不回退到 OSS。
- 历史 OSS 元数据迁移不在本次范围内。

## 3. 方案比较

### 3.1 两张表，Group 关系直接保存在 Job 上——采用

使用 `job_group_metadata` 保存 Group，使用 `job_metadata` 保存 Job。
通过 `job_metadata.group_id` 可空外键表达 Group 成员关系。

该方案符合当前“一个 Job 最多属于一个 Group”的业务语义，不需要额外的成员表、
Repository 方法和 Join 查询。

### 3.2 独立的 Group 成员表——不采用

`job_group_member` 可以支持一个 Job 加入多个 Group，也可以保存关系本身的属性。
当前没有这些需求，因此独立成员表只会增加表数量、事务边界和查询复杂度。

### 3.3 单张 JSONB 文档表——不采用

将原 OSS JSON 原样存入 JSONB 的迁移成本最低，但不利于按状态、Group、Task
以及 Resume 条件查询，也会保留整份文档覆盖写的问题。

## 4. 数据库结构

### 4.1 `job_group_metadata`

Group 表只保存 Group 标识、隔离范围、生命周期以及数据集定位信息。
任务数量和得分汇总从 `job_metadata` 动态计算。

```sql
CREATE TABLE job_group_metadata (
    group_id       UUID PRIMARY KEY,
    namespace      VARCHAR(128) NOT NULL,
    experiment_id  VARCHAR(128) NOT NULL,
    mode           VARCHAR(16) NOT NULL,
    status         VARCHAR(32) NOT NULL,
    dataset        VARCHAR(512),
    split          VARCHAR(128),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ
);

CREATE INDEX ix_job_group_scope_created
    ON job_group_metadata(namespace, experiment_id, created_at DESC);

CREATE INDEX ix_job_group_scope_status
    ON job_group_metadata(namespace, experiment_id, status);
```

| 字段 | 含义 |
|---|---|
| `group_id` | SDK 生成的稳定 Group ID，取代原来带时间戳格式的 OSS `run_id` |
| `namespace` | 租户或业务隔离范围 |
| `experiment_id` | 实验隔离范围 |
| `mode` | 执行模式：`single`、`multi` 或 `full` |
| `status` | Group 当前生命周期状态 |
| `dataset` | Resume 时使用的可选数据集标识 |
| `split` | Resume 时使用的可选数据集分片 |
| `created_at` | Group 创建时间，也是列表默认排序字段 |
| `updated_at` | Group 元数据最近更新时间 |
| `finished_at` | Group 结束时间；未结束时为空 |

Group 支持以下状态：

```text
planning
running
completed
partial
failed
cancelled
```

### 4.2 `job_metadata`

Job 表只使用 `job_id` 标识一条 Job 记录。namespace、experiment 和 Job 名称
都是普通查询属性，允许重复。

```sql
CREATE TABLE job_metadata (
    job_id         UUID PRIMARY KEY,
    namespace      VARCHAR(128) NOT NULL,
    experiment_id  VARCHAR(128) NOT NULL,
    job_name       VARCHAR(255) NOT NULL,
    group_id       UUID,
    task_id        VARCHAR(255),
    job_type       VARCHAR(32) NOT NULL,
    status         VARCHAR(32) NOT NULL,
    sandbox_id     VARCHAR(128),
    session        VARCHAR(128),
    pid            BIGINT,
    exit_code      INTEGER,
    score          DOUBLE PRECISION,
    error          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at     TIMESTAMPTZ,
    finished_at    TIMESTAMPTZ,
    FOREIGN KEY (group_id)
        REFERENCES job_group_metadata(group_id)
        ON DELETE SET NULL
);

CREATE INDEX ix_job_scope_name_created
    ON job_metadata(namespace, experiment_id, job_name, created_at DESC);

CREATE UNIQUE INDEX uq_job_group_task
    ON job_metadata(group_id, task_id)
    WHERE group_id IS NOT NULL AND task_id IS NOT NULL;

CREATE INDEX ix_job_group_status
    ON job_metadata(group_id, status);

CREATE INDEX ix_job_sandbox
    ON job_metadata(sandbox_id)
    WHERE sandbox_id IS NOT NULL;
```

| 字段 | 含义 |
|---|---|
| `job_id` | SDK 生成的稳定 Job ID，精确查询和更新必须使用该字段 |
| `namespace` | Job 所属租户或业务范围 |
| `experiment_id` | Job 所属实验范围 |
| `job_name` | 用户可见且允许重复的 Job 名称 |
| `group_id` | 可空的 Group 外键；独立 Job 为空 |
| `task_id` | Job 对应的可选数据集 Task |
| `job_type` | 执行类型，例如 `bash` 或 `harbor` |
| `status` | Job 当前生命周期状态 |
| `sandbox_id` | Resume 所需的 Sandbox ID |
| `session` | Resume 所需的 Sandbox 进程 Session |
| `pid` | Resume 所需的 Sandbox 进程 ID |
| `exit_code` | Job 结束后的进程退出码 |
| `score` | Job 结束后的得分 |
| `error` | 失败或不可恢复原因 |
| `created_at` | 元数据创建时间 |
| `updated_at` | 元数据最近更新时间 |
| `started_at` | Job 开始执行时间 |
| `finished_at` | Job 结束时间 |

Job 支持以下状态：

```text
planned
starting
sandbox_ready
running
completed
failed
cancelled
unrecoverable
```

namespace、experiment、Job 名称及其组合均不设置唯一约束。精确修改必须提供
`job_id`，根据业务属性查询时返回列表。

同一 Group 中，非空 `task_id` 唯一，因为当前 Run 模型要求一个 Task 只对应
一个当前 Job。Resume 更新该稳定 Job 记录，不插入同一 Task 的重试历史记录。

## 5. SDK Model 调整

`JobMeta` 调整为数据库侧 Job DTO：

- 增加必填的 `job_id: UUID`，默认使用 `uuid4` 生成。
- 将 `run_id` 替换为可空的 `group_id: UUID`。
- 只保留 `job_metadata` 表中存在的字段。
- 时间字段从字符串调整为带时区的 `datetime`。
- 删除 `schema_version`、`attempt`、`status_reason`、`user_id`、`image`、
  `labels`、`env`、`tmp_file` 和 `script_path`。

`RunMeta` 继续作为 CLI 使用的兼容类名，但持久化标识改为必填的
`group_id: UUID`。提供只读兼容属性 `run_id`，其值等于 `group_id`。

以下字段不存储在 Group 表中，而是在查询 Group 时动态计算：

- `total_tasks`
- `pending_tasks`
- `task_job_map`
- `summary`

`task_job_map` 从原来的 Task 到非唯一 Job 名称，改为 Task 到 Job UUID。

## 6. Repository 边界

SDK 提供同步 SQLAlchemy Repository。独立元数据服务负责 Engine、连接池、
数据库凭证、连接生命周期以及异步服务中的线程调度。

Repository 接收已经配置好的 `sessionmaker`：

```python
repository = JobMetadataRepository(session_factory)
```

Repository 不读取数据库环境变量，也不在生产环境自动建表。它会导出 SQLAlchemy
metadata，供元数据服务的 migration 工具创建表。单元测试可以调用
`Base.metadata.create_all`。

### 6.1 Job 操作

```python
create_job(meta: JobMeta) -> JobMeta
get_job(job_id: UUID) -> JobMeta | None
list_jobs(query: JobQuery) -> list[JobMeta]
update_job(job_id: UUID, changes: JobMetaUpdate) -> JobMeta
list_group_jobs(group_id: UUID) -> list[JobMeta]
```

`JobQuery` 支持以下可选过滤条件：

- `namespace`
- `experiment_id`
- `job_name`
- `group_id`
- `task_id`
- `status`
- 基于 `created_at` 的确定性分页

`JobMetaUpdate` 只允许更新以下运行态字段：

- `status`
- `sandbox_id`
- `session`
- `pid`
- `exit_code`
- `score`
- `error`
- `started_at`
- `finished_at`

`updated_at` 由 Repository 自动设置。

### 6.2 Group 操作

```python
create_group(meta: RunMeta) -> RunMeta
create_group_with_jobs(meta: RunMeta, jobs: list[JobMeta]) -> RunMeta
get_group(group_id: UUID) -> RunMeta | None
list_groups(query: GroupQuery) -> list[RunMeta]
update_group(group_id: UUID, changes: GroupMetaUpdate) -> RunMeta
resolve_group_id_for_resume(scope) -> UUID | None
find_completed_tasks(group_id: UUID) -> set[str]
```

Group 的任务数和得分汇总通过 Job 表动态计算：

```sql
SELECT
    COUNT(*) AS total_tasks,
    COUNT(*) FILTER (
        WHERE status NOT IN ('completed', 'failed', 'cancelled', 'unrecoverable')
    ) AS pending_tasks,
    COUNT(*) FILTER (WHERE status = 'completed') AS completed,
    COUNT(*) FILTER (WHERE status IN ('failed', 'unrecoverable')) AS failed,
    COALESCE(AVG(score), 0) AS avg_score,
    COALESCE(SUM(score), 0) AS total_score
FROM job_metadata
WHERE group_id = :group_id;
```

## 7. 写入与更新流程

创建一次 Run 使用一个事务：

1. 生成一个 Group UUID。
2. 插入 Group。
3. 为每个计划执行的 Task 生成一个 Job UUID。
4. 使用同一个 Group UUID 批量插入所有 `planned` Job。
5. 提交事务。

以上步骤由 `create_group_with_jobs` 提供，保证 Group 和初始 Job 不会部分可见。

运行回调始终使用同一个 Job UUID，并执行字段级更新：

1. Job 启动：更新 status、Sandbox ID、session、PID 和开始时间。
2. Job 结束：更新 status、exit code、score、error 和结束时间。
3. Resume：查询 Group 中的 Job，恢复处于可恢复状态且 Sandbox 进程坐标完整的 Job。
4. Group 结束：更新 Group status 和结束时间；汇总字段在读取 Group 时计算。

Repository 不允许根据 `job_name` 定位需要修改的 Job。Job 名称查询可能返回多条，
所有更新操作必须提供 `job_id`。

## 8. 错误与事务行为

- 重复的 `job_id` 或 `group_id` 抛出 `MetadataConflictError`。
- 更新不存在的 UUID 抛出 `MetadataNotFoundError`。
- 关联不存在的 Group 抛出 `MetadataConstraintError`。
- Job 与 Group 的 namespace 或 experiment 不一致时，在提交前抛出
  `MetadataConstraintError`。
- SQLAlchemy 异常必须回滚事务，并包装为元数据 Repository 异常，同时使用
  exception cause 保留原始异常。
- 一次元数据更新要么完整提交所有字段，要么全部不提交。

## 9. OSS 兼容边界

改造完成后，PostgreSQL 是 Job 和 Group 元数据的唯一数据源。

`JobViewer` 继续从 OSS 读取制品和日志，但不再负责以下操作：

- `write_job_meta`
- `get_job_meta`
- `write_run_meta`
- `get_run_meta`
- `list_runs`

不进行双写，也不在 PostgreSQL 失败时自动回退 OSS。历史 OSS 元数据如有需要，
后续通过独立的一次性迁移工具处理。

## 10. 测试范围

测试需要覆盖：

- ORM 表结构、PostgreSQL UUID、外键和索引。
- 独立 Job 和 Group Job 创建。
- 相同 Job 名称使用不同 UUID 保存多条记录。
- 根据 UUID 精确读取和更新 Job。
- 根据业务属性查询并返回多条 Job。
- Group Job 列表以及 Task 到 Job UUID 的映射。
- Group 的总任务数、未完成数、得分汇总和已完成 Task 动态计算。
- Resume Group 选择以及 Sandbox 进程坐标恢复。
- 拒绝 Job 关联不同 namespace 或 experiment 的 Group。
- 冲突或约束异常时回滚事务。
- CLI 从 `planned`、`running` 到终态始终使用同一个 Job UUID。
- 元数据代码不再执行任何 OSS 元数据读写。
