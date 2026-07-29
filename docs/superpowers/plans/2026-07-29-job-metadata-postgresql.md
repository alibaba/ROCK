# Job 元数据 PostgreSQL 重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 ROCK SDK 中提供可信服务端可直接使用的 PostgreSQL Job/Group 元数据 Repository，支持原子创建、状态更新、namespace Group 列表、Group Job 状态筛选、可选游标分页和动态统计，并停止通过 OSS 读写元数据。

**Architecture:** 新增独立的 `rock.sdk.job.metadata` 包，将 Pydantic DTO、SQLAlchemy ORM、异常和 Repository 分离。Repository 接收外部 `sessionmaker`，不管理数据库凭证和生产建表；Group 统计从 Job 表动态聚合。原有 `job_meta.py`、`run_meta.py` 只保留兼容导出，`JobViewer` 继续负责 OSS 制品但删除元数据 CRUD。

**Tech Stack:** Python 3.10–3.12、Pydantic v2、SQLAlchemy 2.x、PostgreSQL UUID/TIMESTAMPTZ、SQLite 内存数据库单元测试、pytest、ruff。

## Global Constraints

- 生产数据库必须使用 PostgreSQL，SDK 通过 SQLAlchemy 同步 Repository 直接访问。
- 数据库 Engine、连接池、凭证、生命周期和异步线程调度由独立元数据服务负责。
- 只创建 `job_group_metadata` 和 `job_metadata` 两张表。
- `group_id` 和 `job_id` 使用原生 128-bit UUID，由 SDK 在写入前生成。
- 一个 Job 最多属于一个 Group；同一 Group 内非空 `task_id` 唯一。
- `job_name` 及 namespace、experiment、job_name 的组合都不唯一。
- 不持久化 labels、env、凭证、schema version、attempt 或预留扩展字段。
- SDK 只提供元数据创建、读取、更新、筛选和统计，不执行续跑或重试。
- Job/Group 元数据不双写 OSS，也不在数据库失败时回退 OSS。
- 日志、trajectory、result.json 和其他制品继续由 `JobViewer` 从 OSS 读取。

---

### Task 1: 元数据 DTO、查询条件和分页模型

**Files:**
- Create: `rock/sdk/job/metadata/__init__.py`
- Create: `rock/sdk/job/metadata/models.py`
- Modify: `rock/sdk/job/meta.py`
- Test: `tests/unit/sdk/job/metadata/test_models.py`

**Interfaces:**
- Produces: `JobMeta`, `JobGroupMeta`, `RunMeta`, `JobStatus`, `JobGroupStatus`, `JobGroupMode`, `JobUpdate`, `JobUpdateItem`, `JobGroupUpdate`, `JobStatusCategory`, `GroupJobQuery`, `GroupQuery`, `PageRequest`, `JobPage`, `JobGroupPage`, `JobGroupStatistics`, `JobGroupDetail`.
- Consumes: Pydantic v2 `BaseModel`, `Field`, `model_validator`; standard library `UUID`, `uuid4`, timezone-aware `datetime`.

- [ ] **Step 1: 写失败的 DTO 测试**

```python
from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from rock.sdk.job.metadata.models import (
    GroupJobQuery,
    JobGroupMeta,
    JobMeta,
    JobStatus,
    JobStatusCategory,
    PageRequest,
)


def test_job_meta_generates_uuid_and_keeps_names_non_unique():
    first = JobMeta(
        namespace="ns",
        experiment_id="exp",
        job_name="same",
        job_type="bash",
        status=JobStatus.PLANNED,
    )
    second = first.model_copy(update={"job_id": None}, deep=True)
    second = JobMeta(**second.model_dump(exclude={"job_id"}))
    assert isinstance(first.job_id, UUID)
    assert isinstance(second.job_id, UUID)
    assert first.job_id != second.job_id


def test_group_job_query_rejects_category_and_statuses_together():
    with pytest.raises(ValidationError):
        GroupJobQuery(
            category=JobStatusCategory.ACTIVE,
            statuses={JobStatus.FAILED},
        )


def test_page_request_limits_page_size():
    with pytest.raises(ValidationError):
        PageRequest(page_size=1001)


def test_group_meta_uses_timezone_aware_datetimes():
    group = JobGroupMeta(
        namespace="ns",
        experiment_id="exp",
        mode="full",
        status="planning",
        created_at=datetime.now(timezone.utc),
    )
    assert group.created_at.tzinfo is not None
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
uv run pytest tests/unit/sdk/job/metadata/test_models.py -x -v
```

Expected: FAIL，`rock.sdk.job.metadata.models` 不存在。

- [ ] **Step 3: 实现最小 DTO**

`models.py` 定义字符串枚举和 Pydantic 模型。核心签名如下：

```python
class JobMeta(BaseModel):
    job_id: UUID = Field(default_factory=uuid4)
    namespace: str
    experiment_id: str
    job_name: str
    group_id: UUID | None = None
    task_id: str | None = None
    job_type: str
    status: JobStatus
    sandbox_id: str | None = None
    session: str | None = None
    pid: int | None = None
    exit_code: int | None = None
    score: float | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobGroupMeta(BaseModel):
    group_id: UUID = Field(default_factory=uuid4)
    namespace: str
    experiment_id: str
    mode: JobGroupMode
    status: JobGroupStatus
    dataset: str | None = None
    split: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None

    @property
    def run_id(self) -> UUID:
        return self.group_id


RunMeta = JobGroupMeta
```

`GroupJobQuery` 的 `model_validator` 拒绝同时传入 `category` 和 `statuses`。
`PageRequest.page_size` 使用 `ge=1, le=1000`。

- [ ] **Step 4: 导出兼容模型并运行 GREEN**

`rock/sdk/job/meta.py` 改为从 `metadata.models` 重新导出仍需公开的模型。

Run:

```bash
uv run pytest tests/unit/sdk/job/metadata/test_models.py -x -v
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add rock/sdk/job/metadata rock/sdk/job/meta.py tests/unit/sdk/job/metadata/test_models.py
git commit -m "feat(job): add database metadata models"
```

---

### Task 2: PostgreSQL ORM 和基础创建/更新 Repository

**Files:**
- Create: `rock/sdk/job/metadata/schema.py`
- Create: `rock/sdk/job/metadata/errors.py`
- Create: `rock/sdk/job/metadata/repository.py`
- Modify: `pyproject.toml`
- Test: `tests/unit/sdk/job/metadata/conftest.py`
- Test: `tests/unit/sdk/job/metadata/test_schema.py`
- Test: `tests/unit/sdk/job/metadata/test_repository_crud.py`

**Interfaces:**
- Consumes: Task 1 DTO；SQLAlchemy `DeclarativeBase`, `Mapped`, `mapped_column`, `Session`, `sessionmaker`, `Uuid`, `DateTime`, `select`, `update`.
- Produces: `MetadataBase`, `JobGroupRecord`, `JobRecord`, `MetadataRepositoryError`, `MetadataConflictError`, `MetadataNotFoundError`, `MetadataConstraintError`, `MetadataValidationError`, `MetadataPaginationError`, `JobMetadataRepository`.

- [ ] **Step 1: 增加 SQLAlchemy 可选依赖并写 ORM 失败测试**

在 `pyproject.toml` 增加：

```toml
job-metadata = [
    "sqlalchemy>=2.0",
    "psycopg2-binary",
]
```

测试 PostgreSQL DDL 使用 UUID、两张表和约定索引：

```python
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from rock.sdk.job.metadata.schema import JobGroupRecord, JobRecord


def test_postgresql_schema_uses_native_uuid():
    group_ddl = str(CreateTable(JobGroupRecord.__table__).compile(dialect=postgresql.dialect()))
    job_ddl = str(CreateTable(JobRecord.__table__).compile(dialect=postgresql.dialect()))
    assert "group_id UUID NOT NULL" in group_ddl
    assert "job_id UUID NOT NULL" in job_ddl
    assert "job_group_metadata" in group_ddl
    assert "job_metadata" in job_ddl


def test_schema_contains_expected_indexes():
    names = {index.name for index in JobGroupRecord.__table__.indexes | JobRecord.__table__.indexes}
    assert "ix_job_group_namespace_created" in names
    assert "uq_job_group_task" in names
    assert "ix_job_group_status" in names
```

- [ ] **Step 2: 运行 ORM 测试并确认 RED**

Run:

```bash
uv run pytest tests/unit/sdk/job/metadata/test_schema.py -x -v
```

Expected: FAIL，`schema` 模块不存在。

- [ ] **Step 3: 实现两张 ORM 表**

`schema.py` 使用 SQLAlchemy `Uuid(as_uuid=True)` 和
`DateTime(timezone=True)`，定义：

```python
class JobGroupRecord(MetadataBase):
    __tablename__ = "job_group_metadata"
    group_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    experiment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    dataset: Mapped[str | None] = mapped_column(String(512))
    split: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobRecord(MetadataBase):
    __tablename__ = "job_metadata"
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    experiment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    job_name: Mapped[str] = mapped_column(String(255), nullable=False)
    group_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("job_group_metadata.group_id", ondelete="SET NULL"),
    )
    task_id: Mapped[str | None] = mapped_column(String(255))
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    sandbox_id: Mapped[str | None] = mapped_column(String(128))
    session: Mapped[str | None] = mapped_column(String(128))
    pid: Mapped[int | None] = mapped_column(BigInteger)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    score: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

通过 `Index(..., unique=True, postgresql_where=...)` 实现同一 Group/Task 的部分
唯一索引。

- [ ] **Step 4: 写 Repository CRUD 失败测试**

`conftest.py` 创建 SQLite 内存 Engine、开启 foreign key pragma、调用
`MetadataBase.metadata.create_all`，并返回 `JobMetadataRepository(sessionmaker)`。

覆盖：

```python
def test_create_and_get_group(repo, group):
    created = repo.create_group(group)
    assert repo.get_group(created.group_id) == created


def test_duplicate_job_names_get_distinct_ids(repo, group, make_job):
    repo.create_group(group)
    first = repo.create_job(make_job(group, job_name="same", task_id="t1"))
    second = repo.create_job(make_job(group, job_name="same", task_id="t2"))
    assert first.job_id != second.job_id


def test_update_job_changes_only_mutable_fields(repo, group, make_job):
    repo.create_group(group)
    job = repo.create_job(make_job(group))
    updated = repo.update_job(job.job_id, JobUpdate(status="running", pid=123))
    assert updated.status == JobStatus.RUNNING
    assert updated.pid == 123
    assert updated.job_name == job.job_name


def test_batch_update_is_atomic(repo, group, make_job):
    repo.create_group(group)
    job = repo.create_job(make_job(group))
    with pytest.raises(MetadataNotFoundError):
        repo.batch_update_jobs([
            JobUpdateItem(job_id=job.job_id, changes=JobUpdate(status="running")),
            JobUpdateItem(job_id=uuid4(), changes=JobUpdate(status="failed")),
        ])
    assert repo.get_job(job.job_id).status == JobStatus.PLANNED
```

- [ ] **Step 5: 运行 CRUD 测试并确认 RED**

Run:

```bash
uv run pytest tests/unit/sdk/job/metadata/test_repository_crud.py -x -v
```

Expected: FAIL，Repository 方法不存在。

- [ ] **Step 6: 实现异常映射和 CRUD**

Repository 每个方法使用独立 Session 和 `with session.begin()`。实现：

```python
create_group(group)
create_job(job)
create_group_with_jobs(group, jobs)
get_group(group_id)
get_job(job_id)
update_group(group_id, changes)
update_job(job_id, changes)
batch_update_jobs(updates)
```

插入前校验 Job 和 Group 的 namespace/experiment 一致。`IntegrityError` 映射为
`MetadataConflictError` 或 `MetadataConstraintError`，不存在记录映射为
`MetadataNotFoundError`。

- [ ] **Step 7: 运行 GREEN**

Run:

```bash
uv run pytest tests/unit/sdk/job/metadata/test_schema.py tests/unit/sdk/job/metadata/test_repository_crud.py -x -v
```

Expected: PASS。

- [ ] **Step 8: 提交**

```bash
git add pyproject.toml rock/sdk/job/metadata tests/unit/sdk/job/metadata
git commit -m "feat(job): persist metadata in PostgreSQL"
```

---

### Task 3: Group 列表、状态筛选、分页、统计与聚合详情

**Files:**
- Modify: `rock/sdk/job/metadata/repository.py`
- Test: `tests/unit/sdk/job/metadata/test_repository_queries.py`
- Test: `tests/unit/sdk/job/metadata/test_repository_pagination.py`

**Interfaces:**
- Consumes: Task 1 查询/Page DTO，Task 2 Repository 和 ORM。
- Produces: `list_group_jobs`, `list_namespace_groups`, `get_group_statistics`, `get_group_detail`。

- [ ] **Step 1: 写 Group Job 状态筛选失败测试**

```python
@pytest.mark.parametrize(
    ("category", "expected"),
    [
        (JobStatusCategory.ALL, {"planned", "running", "completed", "failed"}),
        (JobStatusCategory.ACTIVE, {"planned", "running"}),
        (JobStatusCategory.COMPLETED, {"completed"}),
        (JobStatusCategory.UNSUCCESSFUL, {"failed"}),
        (JobStatusCategory.NOT_COMPLETED, {"planned", "running", "failed"}),
    ],
)
def test_list_group_jobs_filters_categories(seeded_repo, group_id, category, expected):
    page = seeded_repo.list_group_jobs(group_id, GroupJobQuery(category=category))
    assert {job.status.value for job in page.items} == expected


def test_list_group_jobs_supports_exact_statuses(seeded_repo, group_id):
    page = seeded_repo.list_group_jobs(
        group_id,
        GroupJobQuery(statuses={JobStatus.FAILED, JobStatus.RUNNING}),
    )
    assert {job.status for job in page.items} == {JobStatus.FAILED, JobStatus.RUNNING}
```

- [ ] **Step 2: 写 namespace Group 列表和统计失败测试**

```python
def test_list_namespace_groups_filters_experiment(repo, make_group):
    wanted = repo.create_group(make_group(namespace="ns", experiment_id="e1"))
    repo.create_group(make_group(namespace="ns", experiment_id="e2"))
    page = repo.list_namespace_groups("ns", GroupQuery(experiment_id="e1"))
    assert [item.group_id for item in page.items] == [wanted.group_id]


def test_group_statistics_counts_statuses_and_scores(seeded_repo, group_id):
    stats = seeded_repo.get_group_statistics(group_id)
    assert stats.total_jobs == 4
    assert stats.active_jobs == 2
    assert stats.completed_jobs == 1
    assert stats.failed_jobs == 1
    assert stats.scored_jobs == 2
    assert stats.total_score == pytest.approx(1.5)
```

- [ ] **Step 3: 运行筛选和统计测试并确认 RED**

Run:

```bash
uv run pytest tests/unit/sdk/job/metadata/test_repository_queries.py -x -v
```

Expected: FAIL，查询方法不存在。

- [ ] **Step 4: 实现筛选、Group 列表和统计**

使用 SQLAlchemy `select`、`func.count().filter(...)`、`func.avg/sum/min/max`。
所有 Group 相关方法先检查 Group 存在。`list_namespace_groups` 必须接收非空
namespace，并支持 experiment/status/mode/dataset 过滤。

- [ ] **Step 5: 运行查询测试并确认 GREEN**

Run:

```bash
uv run pytest tests/unit/sdk/job/metadata/test_repository_queries.py -x -v
```

Expected: PASS。

- [ ] **Step 6: 写可选游标分页失败测试**

```python
def test_group_jobs_can_return_all_without_pagination(seeded_repo, group_id):
    page = seeded_repo.list_group_jobs(group_id)
    assert len(page.items) == page.total
    assert page.next_cursor is None


def test_group_jobs_cursor_has_no_duplicates(repo_with_many_jobs, group_id):
    first = repo_with_many_jobs.list_group_jobs(group_id, pagination=PageRequest(page_size=2))
    second = repo_with_many_jobs.list_group_jobs(
        group_id,
        pagination=PageRequest(page_size=2, cursor=first.next_cursor),
    )
    assert {job.job_id for job in first.items}.isdisjoint({job.job_id for job in second.items})


def test_invalid_cursor_raises(repo, group):
    repo.create_group(group)
    with pytest.raises(MetadataPaginationError):
        repo.list_group_jobs(group.group_id, pagination=PageRequest(cursor="not-a-cursor"))
```

- [ ] **Step 7: 运行分页测试并确认 RED**

Run:

```bash
uv run pytest tests/unit/sdk/job/metadata/test_repository_pagination.py -x -v
```

Expected: FAIL，cursor 编解码或分页尚未实现。

- [ ] **Step 8: 实现稳定游标和 Group 详情**

cursor 使用 URL-safe base64 编码 JSON：

```json
{"created_at": "2026-07-29T08:00:00+00:00", "id": "<uuid>"}
```

Job 使用升序 `(created_at, job_id)`，Group 使用降序 `(created_at, group_id)`。
查询 `page_size + 1` 条判断是否存在下一页。`get_group_detail` 组合
`get_group`、`get_group_statistics` 和 `list_group_jobs` 的结果。

- [ ] **Step 9: 运行完整查询 GREEN**

Run:

```bash
uv run pytest tests/unit/sdk/job/metadata/test_repository_queries.py tests/unit/sdk/job/metadata/test_repository_pagination.py -x -v
```

Expected: PASS。

- [ ] **Step 10: 提交**

```bash
git add rock/sdk/job/metadata/repository.py tests/unit/sdk/job/metadata
git commit -m "feat(job): query group metadata with pagination"
```

---

### Task 4: 公共 SDK 导出和 OSS 元数据职责移除

**Files:**
- Modify: `rock/sdk/job/metadata/__init__.py`
- Modify: `rock/sdk/job/__init__.py`
- Modify: `rock/sdk/job/job_meta.py`
- Modify: `rock/sdk/job/run_meta.py`
- Modify: `rock/sdk/job/viewer.py`
- Test: `tests/unit/sdk/job/metadata/test_public_api.py`
- Modify: `tests/unit/sdk/job/test_viewer.py`
- Replace: `tests/unit/sdk/job/test_run_meta_repository.py`

**Interfaces:**
- Consumes: Task 1–3 的统一 `JobMetadataRepository`。
- Produces: `rock.sdk.job.JobMetadataRepository` 公共入口；旧模块兼容导出；只读制品 `JobViewer`。

- [ ] **Step 1: 写公共 API 和无 OSS 元数据失败测试**

```python
def test_job_package_exports_database_metadata_repository():
    from rock.sdk.job import JobMetadataRepository
    from rock.sdk.job.metadata import JobMetadataRepository as Expected
    assert JobMetadataRepository is Expected


def test_legacy_repository_modules_reexport_database_repository():
    from rock.sdk.job.job_meta import JobMetaRepository
    from rock.sdk.job.metadata import JobMetadataRepository
    from rock.sdk.job.run_meta import RunMetaRepository
    assert JobMetaRepository is JobMetadataRepository
    assert RunMetaRepository is JobMetadataRepository


def test_job_viewer_has_no_metadata_write_api():
    from rock.sdk.job.viewer import JobViewer
    assert not hasattr(JobViewer, "write_job_meta")
    assert not hasattr(JobViewer, "write_run_meta")
```

- [ ] **Step 2: 运行公共 API 测试并确认 RED**

Run:

```bash
uv run pytest tests/unit/sdk/job/metadata/test_public_api.py -x -v
```

Expected: FAIL，统一 Repository 未导出，Viewer 仍包含 OSS 元数据方法。

- [ ] **Step 3: 调整导出并删除 Viewer 元数据 CRUD**

- `rock.sdk.job.metadata.__init__` 导出全部公共 DTO、异常、ORM metadata 和 Repository。
- `rock.sdk.job.__init__` 使用惰性导入公开 `JobMetadataRepository` 和 DTO，避免没有
  安装 `job-metadata` extra 的普通 Job 执行用户在 import 时加载 SQLAlchemy。
- `job_meta.py` 将 `JobMetaRepository` 指向统一 Repository。
- `run_meta.py` 将 `RunMetaRepository` 指向统一 Repository。
- 从 `viewer.py` 删除 Job/Run 元数据读写、列表、resume helper；保留制品、日志和
  trial result 查询。

- [ ] **Step 4: 更新原 OSS 元数据测试**

删除只验证 `meta.json`、`rock_meta.json` 和 `_meta/run_*.json` 的用例，保留
JobViewer 制品/日志用例。用数据库 Repository 测试替换原
`test_run_meta_repository.py` 的 viewer mock 委托测试。

- [ ] **Step 5: 运行公共 API 和 Viewer 回归测试**

Run:

```bash
uv run pytest tests/unit/sdk/job/metadata tests/unit/sdk/job/test_viewer.py -x -v
```

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add rock/sdk/job tests/unit/sdk/job pyproject.toml
git commit -m "refactor(job): replace OSS metadata repositories"
```

---

### Task 5: 完整验证和文档一致性

**Files:**
- Modify: `docs/superpowers/specs/2026-07-29-job-metadata-postgresql-design.md` only if implementation exposes a verified naming mismatch.
- Modify: `docs/superpowers/plans/2026-07-29-job-metadata-postgresql.md` checkbox statuses.

**Interfaces:**
- Consumes: 所有实现和测试。
- Produces: 通过的定向测试、SDK Job 回归测试和 ruff 输出。

- [ ] **Step 1: 运行全部新元数据测试**

```bash
uv run pytest tests/unit/sdk/job/metadata -v
```

Expected: PASS。

- [ ] **Step 2: 运行 SDK Job 回归测试**

```bash
uv run pytest tests/unit/sdk/job -m "not need_admin and not need_admin_and_network" -v
```

Expected: PASS。

- [ ] **Step 3: 运行 Ruff**

```bash
uv run ruff check rock/sdk/job tests/unit/sdk/job
uv run ruff format --check rock/sdk/job tests/unit/sdk/job
```

Expected: 两条命令均成功，无 lint 或格式问题。

- [ ] **Step 4: 生成并检查 PostgreSQL DDL**

```bash
uv run python -c "from sqlalchemy.dialects import postgresql; from sqlalchemy.schema import CreateIndex, CreateTable; from rock.sdk.job.metadata.schema import MetadataBase; d=postgresql.dialect(); print('\\n'.join([str(CreateTable(t).compile(dialect=d)) for t in MetadataBase.metadata.sorted_tables]))"
```

Expected: 只包含 `job_group_metadata` 和 `job_metadata`，主键列为 UUID，Job 的
`group_id` 外键指向 Group。

- [ ] **Step 5: 检查工作区和差异**

```bash
git diff --check
git status --short
git diff --stat upstream/master...HEAD
```

Expected: `git diff --check` 无输出；工作区只包含本需求文件。

- [ ] **Step 6: 提交最终验证修正**

如果验证引起格式或文档命名修正：

```bash
git add rock/sdk/job tests/unit/sdk/job docs/superpowers
git commit -m "test(job): verify database metadata repository"
```

若没有修正，不创建空提交。
