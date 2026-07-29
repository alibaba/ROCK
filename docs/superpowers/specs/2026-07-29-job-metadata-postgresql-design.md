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

CREATE INDEX ix_job_group_namespace_created
    ON job_group_metadata(namespace, created_at DESC, group_id DESC);
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

### 5.1 持久化 DTO

`JobMeta` 调整为数据库侧 Job DTO：

- 增加必填的 `job_id: UUID`，默认使用 `uuid4` 生成。
- 将 `run_id` 替换为可空的 `group_id: UUID`。
- 只保留 `job_metadata` 表中存在的字段。
- 时间字段从字符串调整为带时区的 `datetime`。
- 删除 `schema_version`、`attempt`、`status_reason`、`user_id`、`image`、
  `labels`、`env`、`tmp_file` 和 `script_path`。

新增 `JobGroupMeta` 作为 Group DTO，只包含 `job_group_metadata` 中存在的字段。
`RunMeta` 保留为 `JobGroupMeta` 的兼容别名，其 `run_id` 只读属性返回
`group_id`。

### 5.2 状态筛选

```python
class JobStatusCategory(str, Enum):
    ALL = "all"
    ACTIVE = "active"
    COMPLETED = "completed"
    UNSUCCESSFUL = "unsuccessful"
    NOT_COMPLETED = "not_completed"
```

| Category | 包含状态 |
|---|---|
| `all` | 所有状态 |
| `active` | `planned`、`starting`、`sandbox_ready`、`running` |
| `completed` | `completed` |
| `unsuccessful` | `failed`、`cancelled`、`unrecoverable` |
| `not_completed` | 除 `completed` 外的所有状态 |

`GroupJobQuery` 支持：

```python
class GroupJobQuery(BaseModel):
    category: JobStatusCategory | None = None
    statuses: set[JobStatus] | None = None
    task_ids: set[str] | None = None
    job_type: str | None = None
```

`category` 和 `statuses` 不能同时指定；两者都不传表示查询全部状态。`statuses`
用于调用方传入精确状态集合。

### 5.3 分页

```python
class PageRequest(BaseModel):
    page_size: int = Field(default=100, ge=1, le=1000)
    cursor: str | None = None

class JobPage(BaseModel):
    items: list[JobMeta]
    total: int
    next_cursor: str | None

class JobGroupPage(BaseModel):
    items: list[JobGroupMeta]
    total: int
    next_cursor: str | None
```

分页参数是可选项：

- `pagination=None` 时返回全部匹配记录，`next_cursor=None`。
- Job 按 `(created_at ASC, job_id ASC)` 使用游标分页。
- Group 按 `(created_at DESC, group_id DESC)` 使用游标分页。
- `total` 表示当前过滤条件下的总记录数。
- cursor 是 SDK 生成和解析的不透明字符串，调用方不依赖其内部格式。

### 5.4 Group 统计和详情

```python
class JobGroupStatistics(BaseModel):
    group_id: UUID
    total_jobs: int
    active_jobs: int
    completed_jobs: int
    failed_jobs: int
    cancelled_jobs: int
    unrecoverable_jobs: int
    scored_jobs: int
    avg_score: float
    total_score: float
    min_score: float | None
    max_score: float | None

class JobGroupDetail(BaseModel):
    group: JobGroupMeta
    statistics: JobGroupStatistics
    jobs: JobPage
```

这些统计全部从 `job_metadata` 动态计算，不增加 Group 表字段。

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

SDK 只负责元数据创建、查询、更新和统计。真正的 Job 执行、断点续跑和失败重试
均由调用 SDK 的客户端负责。

### 6.1 创建操作

```python
create_group(group: JobGroupMeta) -> JobGroupMeta
create_job(job: JobMeta) -> JobMeta
create_group_with_jobs(
    group: JobGroupMeta,
    jobs: Sequence[JobMeta],
) -> JobGroupMeta
```

`create_group_with_jobs` 在一个事务中创建 Group 和所有初始 Job，避免部分写入。

### 6.2 更新操作

```python
update_group(
    group_id: UUID,
    changes: JobGroupUpdate,
) -> JobGroupMeta

update_job(
    job_id: UUID,
    changes: JobUpdate,
) -> JobMeta

batch_update_jobs(
    updates: Sequence[JobUpdateItem],
) -> list[JobMeta]
```

`JobUpdateItem` 包含 `job_id` 和对应的 `JobUpdate`。批量更新在一个事务中执行，
任意一条更新失败时全部回滚。

`JobUpdate` 只允许修改运行态字段：

- `status`
- `sandbox_id`
- `session`
- `pid`
- `exit_code`
- `score`
- `error`
- `started_at`
- `finished_at`

`updated_at` 由 Repository 自动设置。所有 Job 更新必须使用 `job_id`，不能根据
非唯一的 `job_name` 定位记录。

### 6.3 基础查询

```python
get_group(group_id: UUID) -> JobGroupMeta | None
get_job(job_id: UUID) -> JobMeta | None

list_group_jobs(
    group_id: UUID,
    query: GroupJobQuery | None = None,
    pagination: PageRequest | None = None,
) -> JobPage
```

`list_group_jobs` 返回完整 `JobMeta`。客户端通过筛选条件得到需要断点续跑或失败
重试的 Job，但后续执行不属于 SDK 职责。

常见查询：

```python
# 断点续跑候选：只包含仍处于执行流程中的 Job
list_group_jobs(
    group_id,
    query=GroupJobQuery(category=JobStatusCategory.ACTIVE),
)

# 所有尚未成功的 Job，包括执行中和失败终态
list_group_jobs(
    group_id,
    query=GroupJobQuery(category=JobStatusCategory.NOT_COMPLETED),
)

# 只查询需要由客户端重试的失败 Job
list_group_jobs(
    group_id,
    query=GroupJobQuery(statuses={JobStatus.FAILED}),
)
```

### 6.4 namespace 下的 Group 查询

```python
class GroupQuery(BaseModel):
    experiment_id: str | None = None
    statuses: set[JobGroupStatus] | None = None
    modes: set[JobGroupMode] | None = None
    dataset: str | None = None

list_namespace_groups(
    namespace: str,
    query: GroupQuery | None = None,
    pagination: PageRequest | None = None,
) -> JobGroupPage
```

`namespace` 是必填参数，SDK 不提供无范围的全库 Group 列表接口。

### 6.5 Group 状态、统计与详情

```python
get_group_statistics(group_id: UUID) -> JobGroupStatistics

get_group_detail(
    group_id: UUID,
    query: GroupJobQuery | None = None,
    pagination: PageRequest | None = None,
) -> JobGroupDetail
```

`get_group` 返回 Group 自身保存的状态。`get_group_statistics` 返回所有 Job 状态
计数和分数统计。`get_group_detail` 一次返回 Group、统计和经过筛选/分页后的 Job
详情，减少元数据服务的调用次数。

Group 聚合使用条件聚合：

```sql
SELECT
    COUNT(*) AS total_jobs,
    COUNT(*) FILTER (
        WHERE status IN ('planned', 'starting', 'sandbox_ready', 'running')
    ) AS active_jobs,
    COUNT(*) FILTER (WHERE status = 'completed') AS completed_jobs,
    COUNT(*) FILTER (WHERE status = 'failed') AS failed_jobs,
    COUNT(*) FILTER (WHERE status = 'cancelled') AS cancelled_jobs,
    COUNT(*) FILTER (WHERE status = 'unrecoverable') AS unrecoverable_jobs,
    COUNT(score) AS scored_jobs,
    COALESCE(AVG(score), 0) AS avg_score,
    COALESCE(SUM(score), 0) AS total_score,
    MIN(score) AS min_score,
    MAX(score) AS max_score
FROM job_metadata
WHERE group_id = :group_id;
```

## 7. 使用流程与数据流

### 7.1 创建 Group 和 Job

客户端生成或接收 Group/Job DTO，调用 `create_group_with_jobs`。Repository 在同一
事务中写入 Group 和所有初始 `planned` Job。

### 7.2 更新 Job 和 Group 状态

客户端在执行生命周期中调用 `update_job` 或 `batch_update_jobs` 更新 Job，再调用
`update_group` 更新 Group。Repository 不主动推导或修改 Group 保存的 status。

### 7.3 客户端断点续跑

客户端调用 `list_group_jobs(category=ACTIVE)` 获取尚未结束的 Job 详情，自行检查
外部运行环境并完成续跑。SDK 不探测 Sandbox 是否存活，也不启动任务。

### 7.4 统计 Group

客户端调用 `get_group_statistics` 获取状态和分数聚合，或调用 `get_group_detail`
同时获取 Group、聚合统计和 Job 明细。

### 7.5 客户端重试失败 Job

客户端调用 `list_group_jobs(statuses={FAILED})` 获得失败 Job，执行重试，并使用
原 `job_id` 调用 `update_job` 更新状态与运行结果。SDK 不触发重试，也不保存重试
历史。

### 7.6 浏览 namespace 下的 Group

客户端调用 `list_namespace_groups`，可按 experiment、Group status、mode、dataset
筛选，并可选择一次返回全部或使用游标分页。

## 8. 错误与事务行为

- 重复的 `job_id` 或 `group_id` 抛出 `MetadataConflictError`。
- 更新不存在的 UUID 抛出 `MetadataNotFoundError`。
- 查询不存在的 Group 时，`get_group` 返回 `None`；需要 Group 必须存在的聚合和
  列表接口抛出 `MetadataNotFoundError`。
- 关联不存在的 Group 抛出 `MetadataConstraintError`。
- Job 与 Group 的 namespace 或 experiment 不一致时，在提交前抛出
  `MetadataConstraintError`。
- 同时指定 `GroupJobQuery.category` 和 `statuses` 抛出
  `MetadataValidationError`。
- 非法或过期 cursor 抛出 `MetadataPaginationError`。
- SQLAlchemy 异常必须回滚事务，并包装为元数据 Repository 异常，同时使用
  exception cause 保留原始异常。
- 单次更新和批量更新要么完整提交，要么全部不提交。

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

- ORM 表结构、PostgreSQL UUID、外键和全部索引。
- 独立 Job，以及原子创建 Group 和初始 Job。
- 相同 Job 名称使用不同 UUID 保存多条记录。
- 根据 UUID 精确读取、单条更新和事务性批量更新 Job。
- Group Job 的 `all`、`active`、`completed`、`unsuccessful`、
  `not_completed` 和精确 statuses 筛选。
- `category` 与 `statuses` 互斥校验。
- Group Job 查询不分页、游标首/中/末页、空页以及非法 cursor。
- namespace 下 Group 列表及 experiment、status、mode、dataset 筛选。
- namespace Group 列表不分页和游标分页。
- Group 保存状态查询、各 Job 状态计数以及平均分、总分、最小分、最大分。
- Group 详情同时返回 Group、统计和分页 Job 明细。
- 拒绝 Job 关联不同 namespace 或 experiment 的 Group。
- 冲突或约束异常时回滚事务。
- CLI 从 `planned`、`running` 到终态始终使用同一个 Job UUID。
- 元数据代码不再执行任何 OSS 元数据读写。
