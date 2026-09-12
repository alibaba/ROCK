# DatasetMetadataClient — Pure DB-Backed Metadata Management

## Summary

引入独立的数据库驱动元数据客户端 `DatasetMetadataClient`，将 Dataset 元数据管理从 OSS 文件操作中解耦。支持 PostgreSQL（生产）和 SQLite（测试/本地开发）双方言。

## Motivation

原有 `DatasetClient` 将文件操作（browse/read/download/upload/sync）与元数据管理耦合在一起。随着 EnvHub 向结构化元数据方向演进，需要一个纯数据库层的 SDK 来管理 Dataset、Instance、Image、Permission 和审计日志，而不依赖 OSS。

## Architecture

```
rock/sdk/envhub/datasets/
├── __init__.py              # 导出 DatasetMetadataClient + 数据模型
├── database.py              # SDK 独立 ORM 模型 (Dataset, Instance, Split, Image, Permission, AuditEvent)，自有 Base
├── metadata_client.py       # 用户侧 SDK 入口
├── models.py                # 数据传输对象 (dataclass)
└── registry/
    └── db.py                # DbDatasetRegistry — SQLAlchemy 实现
```

### 分层设计

| 层 | 模块 | 职责 |
|---|---|---|
| SDK 入口 | `DatasetMetadataClient` | 面向用户的 API，封装连接池配置 |
| 注册中心 | `DbDatasetRegistry` | 所有 SQL 逻辑，session 管理，方言适配 |
| ORM | `rock.sdk.envhub.datasets.database` | SDK 独立的 SQLAlchemy 表定义、关系、约束，自有 `Base(DeclarativeBase)` |
| 数据模型 | `rock.sdk.envhub.datasets.models` | 返回值的 dataclass 定义 |

---

## SDK API Reference

### `DatasetMetadataClient`

```python
from rock.sdk.envhub.datasets import DatasetMetadataClient

client = DatasetMetadataClient(
    db_url="postgresql+psycopg2://user:pass@host:5432/envhub",
    pool_size=10,          # 连接池大小 (default: 10)
    max_overflow=20,       # 最大溢出连接数 (default: 20)
    pool_timeout=30,       # 获取连接超时秒数 (default: 30)
    pool_recycle=1800,     # 连接回收周期秒数 (default: 1800)
    pool_pre_ping=True,    # 连接健康检查 (default: True)
)
```

---

### Dataset 管理

#### `register_dataset`

注册或更新一个 dataset。若已存在则更新字段。

```python
ds = client.register_dataset(
    org="princeton-nlp",
    name="SWE-bench_Verified",
    description="Software engineering benchmark",
    tags=["swe", "coding"],
    owner="admin",
    homepage="https://swe-bench.github.io",
    repo="https://github.com/princeton-nlp/SWE-bench",
    paper="https://arxiv.org/abs/2310.06770",
    leaderboard="https://swe-bench.github.io/leaderboard",
    logo_url=None,
    os="linux",
    version="1.0",
)
# Returns: Dataset ORM object
```

**Parameters:**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `org` | `str` | Yes | 组织名 |
| `name` | `str` | Yes | 数据集名称 |
| `description` | `str` | No | 描述 (default: "") |
| `tags` | `list[str] \| None` | No | 标签列表 |
| `owner` | `str` | No | 所有者 |
| `homepage` | `str \| None` | No | 主页 URL |
| `repo` | `str \| None` | No | 仓库 URL |
| `paper` | `str \| None` | No | 论文 URL |
| `leaderboard` | `str \| None` | No | 排行榜 URL |
| `logo_url` | `str \| None` | No | Logo URL |
| `os` | `str \| None` | No | 目标操作系统 |
| `version` | `str \| None` | No | 版本号 |

---

#### `list_datasets`

分页查询 datasets，支持按 org 过滤、模糊搜索和排序。默认按 `updated_at DESC` 排序。

```python
from rock.sdk.envhub.datasets import SortField, SortOrder

result = client.list_datasets(
    org="princeton-nlp",           # 可选，按组织过滤
    query="SWE",                   # 可选，模糊搜索 org/name
    sort_by=SortField.NAME,        # 可选，排序字段 (name/created_at/updated_at)
    sort_order=SortOrder.ASC,      # 可选，排序方向 (asc/desc)
    offset=0,
    limit=20,
)
# Returns: PageResult[DatasetInfo]
# result.items: list[DatasetInfo]
# result.total: int
# result.offset: int
# result.limit: int | None
```

**排序说明：**

| `sort_by` | `sort_order` | 行为 |
|-----------|-------------|------|
| `None` | `None` | 默认 `updated_at DESC` |
| `SortField.NAME` | `None` | `name ASC`（大小写不敏感） |
| `SortField.CREATED_AT` | `SortOrder.DESC` | `created_at DESC` |
| `SortField.UPDATED_AT` | `SortOrder.ASC` | `updated_at ASC` |

> **Note:** `SortField.NAME` 排序为大小写不敏感（`lower(name)`），确保 `Apple` 和 `apple` 相邻排列。

---

#### `get_dataset`

获取单个 dataset 信息。`splits` 字段包含完整的 `SplitInfo` 列表（含 task_count 和时间戳）。

```python
info = client.get_dataset("princeton-nlp", "SWE-bench_Verified")
# Returns: DatasetInfo | None
# info.splits -> [SplitInfo(name="test", task_count=500, ...), ...]
# info.task_counts -> {"test": 500}  (computed property)
```

---

#### `delete_dataset`

删除 dataset 及其所有 instances（级联删除）。

```python
ok = client.delete_dataset("princeton-nlp", "SWE-bench_Verified")
# Returns: bool
```

---

### Instance 管理

#### `register_instance`

注册或更新一个 instance（task）。若 dataset 不存在则自动创建。注册时会自动创建对应的 Split 记录并更新 `task_count`。

```python
inst = client.register_instance(
    org="princeton-nlp",
    dataset="SWE-bench_Verified",
    split="test",
    instance_name="django__django-11099",
    description="Fix QuerySet.union() with values()/values_list()",
    type="directory",
    format="git-patch",
    repo="https://github.com/django/django",
    language="python",
    difficulty="medium",
    base_commit="abc123",
    image_uris=["registry.example.com/swebench/django:11099"],
    tags=["django", "queryset", "union"],
    raw='{"hints_text": "..."}',
    source_revision="v1.0",
    imported_from="swebench-raw",
    created_by="importer-v2",
)
# Returns: Instance ORM object
```

**Parameters:**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `org` | `str` | Yes | 组织名 |
| `dataset` | `str` | Yes | 数据集名称 |
| `split` | `str` | Yes | 数据划分 (train/test/dev) |
| `instance_name` | `str` | Yes | 实例唯一标识 |
| `description` | `str` | No | 描述 |
| `type` | `str` | No | 类型 (default: "directory") |
| `format` | `str \| None` | No | 数据格式 (git-patch, json, etc.) |
| `repo` | `str \| None` | No | 源代码仓库 |
| `language` | `str \| None` | No | 编程语言 |
| `difficulty` | `str \| None` | No | 难度 (easy/medium/hard) |
| `base_commit` | `str \| None` | No | 基准 commit SHA |
| `image_uris` | `list[str] \| None` | No | 关联镜像 URI 列表 |
| `tags` | `list[str] \| None` | No | 标签列表 |
| `raw` | `str \| None` | No | 原始数据 (JSON string) |
| `source_revision` | `str \| None` | No | 导入时的源版本 |
| `imported_from` | `str \| None` | No | 导入来源标识 |
| `created_by` | `str \| None` | No | 创建者 |

---

#### `register_instances_batch`

批量注册 instances。自动创建对应的 Split 记录并更新 `task_count`。支持 `tags` 字段。

```python
count = client.register_instances_batch(
    org="princeton-nlp",
    dataset="SWE-bench_Verified",
    split="test",
    instances=[
        {"name": "django__django-11099", "language": "python", "difficulty": "medium", "tags": ["django"]},
        {"name": "django__django-11283", "language": "python", "difficulty": "hard", "tags": ["django", "orm"]},
    ],
)
# Returns: int (处理的总条目数)
```

---

#### `get_instance`

获取单个 instance。

```python
inst = client.get_instance("princeton-nlp", "SWE-bench_Verified", "test", "django__django-11099")
# Returns: Instance | None
```

---

#### `delete_instance`

删除单个 instance，自动更新对应 Split 的 `task_count`。

```python
ok = client.delete_instance("princeton-nlp", "SWE-bench_Verified", "test", "django__django-11099")
# Returns: bool
```

---

#### `recalculate_task_counts`

重新计算 dataset 各 split 的 `task_count`（从 instances 表聚合，更新 splits 表）。

```python
counts = client.recalculate_task_counts("princeton-nlp", "SWE-bench_Verified")
# Returns: dict[str, int]  e.g. {"test": 500, "dev": 50}
```

---

### 层级浏览

#### `list_organizations`

列出所有注册了 dataset 的组织。

```python
result = client.list_organizations(offset=0, limit=50)
# Returns: PageResult[str]
```

---

#### `list_org_datasets`

列出某组织下所有 dataset 名称。

```python
result = client.list_org_datasets("princeton-nlp", offset=0, limit=50)
# Returns: PageResult[str]
```

---

#### `list_dataset_splits`

列出 dataset 的所有 split 名称（从 splits 表查询）。

```python
splits = client.list_dataset_splits("princeton-nlp", "SWE-bench_Verified")
# Returns: list[str]  e.g. ["test", "dev"]
```

---

#### `list_dataset_split_info`

列出 dataset 的所有 split 详细信息（分页，支持排序）。默认按 `name ASC` 排序。

```python
result = client.list_dataset_split_info(
    "princeton-nlp", "SWE-bench_Verified",
    sort_by=SortField.CREATED_AT,
    sort_order=SortOrder.DESC,
    offset=0, limit=50,
)
# Returns: PageResult[SplitInfo]
# result.items[0].name       -> "test"
# result.items[0].task_count -> 500
# result.items[0].created_by -> "importer-v2"
```

---

#### `list_dataset_tasks`

列出某个 split 下所有 instance 名称（分页，支持排序）。默认按 `name ASC` 排序。

```python
result = client.list_dataset_tasks(
    "princeton-nlp", "SWE-bench_Verified", "test",
    query="django",                    # 可选模糊搜索
    sort_by=SortField.NAME,            # 可选排序字段
    sort_order=SortOrder.DESC,         # 可选排序方向
    offset=0, limit=100,
)
# Returns: PageResult[str]
```

---

#### `list_dataset_task_entries`

列出某个 split 下所有 instance 的详细信息（分页，支持排序）。默认按 `name ASC` 排序。返回的 `TaskEntry` 包含 `tags` 和 `created_at` 字段。

```python
result = client.list_dataset_task_entries(
    "princeton-nlp", "SWE-bench_Verified", "test",
    query="django",
    sort_by=SortField.UPDATED_AT,
    sort_order=SortOrder.DESC,
    offset=0, limit=20,
)
# Returns: PageResult[TaskEntry]
```

---

### Image 管理

#### `register_image`

注册或更新镜像信息。

```python
img = client.register_image(
    "docker.io/swebench/django:11099",
    image_uri_sg="registry-sg.example.com/swebench/django:11099",
    image_uri_sh="registry-sh.example.com/swebench/django:11099",
    image_hash="sha256:abc123...",
    status="ready",
    created_by="image-sync-job",
)
# Returns: Image ORM object
```

---

#### `get_image`

```python
img = client.get_image("docker.io/swebench/django:11099")
# Returns: Image | None
```

---

#### `list_images`

```python
result = client.list_images(status="ready", offset=0, limit=50)
# Returns: PageResult[ImageInfo]
```

---

#### `update_image`

部分更新镜像字段。

```python
img = client.update_image(
    "docker.io/swebench/django:11099",
    status="syncing",
    last_job_id="job-456",
)
# Returns: Image | None
```

---

#### `delete_image`

```python
ok = client.delete_image("docker.io/swebench/django:11099")
# Returns: bool
```

---

### Permission 管理

#### `grant_permission`

授予用户对 dataset 的访问权限。若已有则更新角色。

```python
perm = client.grant_permission(
    "princeton-nlp", "SWE-bench_Verified",
    user_id="user@example.com",
    role="editor",         # viewer | editor | admin
    granted_by="admin@example.com",
)
# Returns: DatasetPermission ORM object
```

---

#### `revoke_permission`

```python
ok = client.revoke_permission("princeton-nlp", "SWE-bench_Verified", "user@example.com")
# Returns: bool
```

---

#### `get_permission`

```python
info = client.get_permission("princeton-nlp", "SWE-bench_Verified", "user@example.com")
# Returns: PermissionInfo | None
```

---

#### `list_dataset_permissions`

列出某 dataset 的所有权限记录。

```python
result = client.list_dataset_permissions("princeton-nlp", "SWE-bench_Verified", offset=0, limit=50)
# Returns: PageResult[PermissionInfo]
```

---

#### `list_user_permissions`

列出某用户在所有 datasets 上的权限。

```python
result = client.list_user_permissions("user@example.com", offset=0, limit=50)
# Returns: PageResult[PermissionInfo]
```

---

### Audit 审计日志

#### `log_event`

记录审计事件。

```python
event = client.log_event(
    target_type="dataset",
    target_id="princeton-nlp/SWE-bench_Verified",
    event_type="create",
    operator="admin@example.com",
    changes={"description": {"old": "", "new": "SWE benchmark"}},
)
# Returns: AuditEvent ORM object
```

---

#### `list_audit_events`

查询审计日志，支持多维过滤。

```python
result = client.list_audit_events(
    target_type="dataset",
    target_id="princeton-nlp/SWE-bench_Verified",
    event_type="create",
    operator="admin@example.com",
    offset=0,
    limit=100,
)
# Returns: PageResult[AuditEventInfo]
```

---

## Data Models

### `PageResult[T]`

通用分页结果。

```python
@dataclass
class PageResult(Generic[T]):
    items: list[T]    # 当前页数据
    total: int        # 总记录数
    offset: int       # 偏移量
    limit: int | None # 每页大小 (None = 不限)
```

### `SortOrder`

排序方向枚举。

```python
class SortOrder(str, Enum):
    ASC = "asc"
    DESC = "desc"
```

### `SortField`

排序字段枚举，适用于 Dataset、Task、Split 列表。

```python
class SortField(str, Enum):
    NAME = "name"
    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"
```

### `DatasetInfo`

```python
@dataclass
class DatasetInfo:
    id: str                          # "org/dataset"
    description: str = ""
    tags: list[str] = []
    owner: str = ""
    homepage: str | None = None
    repo: str | None = None
    paper: str | None = None
    leaderboard: str | None = None
    logo_url: str | None = None
    os: str | None = None
    version: str | None = None
    splits: list[SplitInfo] = []     # Split 详情列表（含 task_count、时间戳等）
    created_at: str | None = None    # 创建时间 (ISO 8601)
    updated_at: str | None = None    # 更新时间 (ISO 8601)

    @property
    def task_counts(self) -> dict[str, int]:
        """从 splits 计算得出 {split_name: count}，向后兼容。"""
        return {s.name: s.task_count for s in self.splits if s.task_count}
```

### `TaskEntry`

```python
@dataclass
class TaskEntry:
    name: str
    path: str
    type: str                        # "file" | "directory"
    size: int | None = None
    file_count: int | None = None
    etag: str | None = None
    description: str = ""
    format: str | None = None
    repo: str | None = None
    language: str | None = None
    difficulty: str | None = None
    base_commit: str | None = None
    image_uris: list[str] | None = None
    raw: str | None = None
    source_revision: str | None = None
    imported_from: str | None = None
    tags: list[str] | None = None    # 标签列表
    created_by: str | None = None
    created_at: str | None = None    # 创建时间 (ISO 8601)
    updated_at: str | None = None
```

### `SplitInfo`

Split 详细信息。内嵌在 `DatasetInfo.splits` 中返回，也由 `list_dataset_split_info` 独立返回。

```python
@dataclass
class SplitInfo:
    name: str
    task_count: int = 0              # 该 split 下的 instance 数量
    created_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
```

### `ImageInfo`

```python
@dataclass
class ImageInfo:
    source_image_uri: str
    image_uri_sg: str | None = None
    image_uri_sh: str | None = None
    image_hash: str | None = None
    status: str = "pending"          # pending | syncing | ready | failed
    last_error: str | None = None
    last_job_id: str | None = None
    created_by: str = "system"
    created_at: str | None = None
    updated_at: str | None = None
```

### `PermissionInfo`

```python
@dataclass
class PermissionInfo:
    dataset_id: str                  # "org/dataset"
    user_id: str
    role: str = "viewer"             # viewer | editor | admin
    granted_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
```

### `AuditEventInfo`

```python
@dataclass
class AuditEventInfo:
    id: int
    target_type: str                 # "dataset" | "instance" | "image" | "permission"
    target_id: str
    event_type: str                  # "create" | "update" | "delete" | ...
    operator: str
    changes: dict | None = None
    created_at: str | None = None
```

---

## Database Schema

### Tables Overview

| Table | Primary Key | Unique Constraints |
|-------|------------|-------------------|
| `datasets` | `id` (auto) | `(org, name)` |
| `instances` | `id` (auto) | `(dataset_id, split, name)` |
| `splits` | `id` (auto) | `(dataset_id, name)` |
| `images` | `source_image_uri` | — |
| `dataset_permissions` | `id` (auto) | `(dataset_id, user_id)` |
| `audit_events` | `id` (auto) | — |

### Table: `datasets`

数据集元数据主表。

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `id` | `Integer` | No | autoincrement | 主键 |
| `org` | `String(255)` | No | — | 组织名（索引） |
| `name` | `String(255)` | No | — | 数据集名称 |
| `description` | `Text` | Yes | `""` | 描述 |
| `tags` | `JSON` | Yes | `[]` | 标签列表 |
| `owner` | `String(255)` | Yes | `""` | 所有者 |
| `homepage` | `String(512)` | Yes | `NULL` | 主页 URL |
| `repo` | `String(512)` | Yes | `NULL` | 仓库 URL |
| `paper` | `String(512)` | Yes | `NULL` | 论文 URL |
| `leaderboard` | `String(512)` | Yes | `NULL` | 排行榜 URL |
| `logo_url` | `String(512)` | Yes | `NULL` | Logo URL |
| `os` | `String(64)` | Yes | `NULL` | 目标操作系统 |
| `version` | `String(64)` | Yes | `NULL` | 版本号 |
| `created_at` | `DateTime` | Yes | `now()` | 创建时间 |
| `updated_at` | `DateTime` | Yes | `now()` | 更新时间（自动维护） |

**Unique:** `(org, name)`
**Relationships:** `instances` (1:N, cascade delete), `splits` (1:N, cascade delete), `permissions` (1:N, cascade delete)

### Table: `instances`

数据集实例表，每条记录对应一个 task/instance。

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `id` | `Integer` | No | autoincrement | 主键 |
| `dataset_id` | `Integer` | No | — | 外键 → `datasets.id`（ON DELETE CASCADE） |
| `split` | `String(255)` | No | — | 数据划分 (train/test/dev) |
| `name` | `String(255)` | No | — | 实例唯一标识 |
| `description` | `Text` | Yes | `""` | 描述 |
| `type` | `String(16)` | Yes | `"directory"` | 类型 (file/directory) |
| `size` | `BigInteger` | Yes | `NULL` | 文件大小 (bytes) |
| `file_count` | `Integer` | Yes | `NULL` | 文件数量 |
| `etag` | `String(255)` | Yes | `NULL` | ETag |
| `format` | `String(64)` | Yes | `NULL` | 数据格式（索引） |
| `repo` | `String(512)` | Yes | `NULL` | 源代码仓库 |
| `language` | `String(64)` | Yes | `NULL` | 编程语言（索引） |
| `difficulty` | `String(64)` | Yes | `NULL` | 难度 (easy/medium/hard) |
| `base_commit` | `String(64)` | Yes | `NULL` | 基准 commit SHA |
| `image_uris` | `JSON` | Yes | `NULL` | 关联镜像 URI 列表 |
| `tags` | `JSON` | Yes | `[]` | 标签列表 |
| `raw` | `Text` | Yes | `NULL` | 原始数据 (JSON string) |
| `source_revision` | `String(128)` | Yes | `NULL` | 导入时的源版本 |
| `imported_from` | `String(512)` | Yes | `NULL` | 导入来源标识 |
| `created_by` | `String(255)` | Yes | `NULL` | 创建者 |
| `created_at` | `DateTime` | Yes | `now()` | 创建时间 |
| `updated_at` | `DateTime` | Yes | `now()` | 更新时间（自动维护） |

**Unique:** `(dataset_id, split, name)`
**Indexes:** `(dataset_id, split)` 复合索引, `format`, `language`

### Table: `splits`

数据集分片元数据表，记录每个 split 的 task 数量和创建信息。注册 instance 时自动创建。

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `id` | `Integer` | No | autoincrement | 主键 |
| `dataset_id` | `Integer` | No | — | 外键 → `datasets.id`（ON DELETE CASCADE） |
| `name` | `String(255)` | No | — | Split 名称 (train/test/dev) |
| `task_count` | `Integer` | No | `0` | 该 split 下的 instance 数量 |
| `created_by` | `String(255)` | Yes | `NULL` | 创建者 |
| `created_at` | `DateTime` | Yes | `now()` | 创建时间 |
| `updated_at` | `DateTime` | Yes | `now()` | 更新时间（自动维护） |

**Unique:** `(dataset_id, name)`
**Indexes:** `(dataset_id, name)` 复合索引

### Table: `images`

镜像注册表，跟踪镜像在不同区域的同步状态。

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `source_image_uri` | `String(512)` | No | — | 主键，源镜像 URI |
| `image_uri_sg` | `String(512)` | Yes | `NULL` | 新加坡区域镜像 URI |
| `image_uri_sh` | `String(512)` | Yes | `NULL` | 上海区域镜像 URI |
| `image_hash` | `String(71)` | Yes | `NULL` | 镜像摘要 (sha256:...) |
| `status` | `String(32)` | No | `"pending"` | 同步状态（索引）：pending/syncing/ready/failed |
| `last_error` | `Text` | Yes | `NULL` | 最近一次错误信息 |
| `last_job_id` | `String(64)` | Yes | `NULL` | 最近一次同步 job ID |
| `created_by` | `String(255)` | No | `"system"` | 创建者 |
| `created_at` | `DateTime` | Yes | `now()` | 创建时间 |
| `updated_at` | `DateTime` | Yes | `now()` | 更新时间（自动维护） |

**Indexes:** `status`

### Table: `dataset_permissions`

数据集权限控制表。

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `id` | `Integer` | No | autoincrement | 主键 |
| `dataset_id` | `Integer` | No | — | 外键 → `datasets.id`（ON DELETE CASCADE） |
| `user_id` | `String(255)` | No | — | 用户标识 |
| `role` | `String(32)` | No | `"viewer"` | 角色：viewer/editor/admin |
| `granted_by` | `String(255)` | Yes | `NULL` | 授权人 |
| `created_at` | `DateTime` | Yes | `now()` | 创建时间 |
| `updated_at` | `DateTime` | Yes | `now()` | 更新时间（自动维护） |

**Unique:** `(dataset_id, user_id)`
**Indexes:** `user_id`

### Table: `audit_events`

审计日志表，记录所有元数据变更操作。

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `id` | `Integer` | No | autoincrement | 主键 |
| `target_type` | `String(32)` | No | — | 目标类型（索引）：dataset/instance/image/permission |
| `target_id` | `String(512)` | No | — | 目标标识 (如 `org/name`) |
| `event_type` | `String(64)` | No | — | 事件类型（索引）：create/update/delete/... |
| `operator` | `String(255)` | No | — | 操作人 |
| `changes` | `JSON` | Yes | `NULL` | 变更详情 `{field: {old, new}}` |
| `created_at` | `DateTime` | Yes | `now()` | 事件时间 |

**Indexes:** `target_type`, `event_type`, `(target_type, target_id)` 复合索引

### Relationships

```
Dataset 1──N Instance            (cascade delete)
Dataset 1──N Split               (cascade delete)
Dataset 1──N DatasetPermission   (cascade delete)
```

### ER Diagram

```
┌─────────────────┐       ┌──────────────────────┐
│    datasets      │       │      instances        │
├─────────────────┤       ├──────────────────────┤
│ id (PK)         │──┐    │ id (PK)              │
│ org             │  ├───>│ dataset_id (FK)      │
│ name            │  │    │ split                 │
│ description     │  │    │ name                  │
│ tags            │  │    │ type, size, format... │
│ owner           │  │    │ language, difficulty  │
│ homepage, repo  │  │    │ image_uris, tags, raw │
│ created_at      │  │    │ created_at/updated_at │
│ updated_at      │  │    └──────────────────────┘
└─────────────────┘  │
                     │    ┌──────────────────────┐
                     │    │       splits          │
                     │    ├──────────────────────┤
                     ├───>│ id (PK)              │
                     │    │ dataset_id (FK)      │
                     │    │ name                  │
                     │    │ task_count            │
                     │    │ created_by            │
                     │    │ created_at/updated_at │
                     │    └──────────────────────┘
                     │
                     │    ┌──────────────────────┐
                     │    │ dataset_permissions   │
                     │    ├──────────────────────┤
                     └───>│ id (PK)              │
                          │ dataset_id (FK)      │
                          │ user_id              │
                          │ role                 │
                          │ granted_by           │
                          │ created_at/updated_at│
                          └──────────────────────┘

┌──────────────────┐      ┌──────────────────────┐
│     images       │      │    audit_events       │
├──────────────────┤      ├──────────────────────┤
│ source_image_uri │      │ id (PK)              │
│   (PK)           │      │ target_type          │
│ image_uri_sg     │      │ target_id            │
│ image_uri_sh     │      │ event_type           │
│ image_hash       │      │ operator             │
│ status           │      │ changes              │
│ last_error       │      │ created_at           │
│ last_job_id      │      └──────────────────────┘
│ created_by       │
│ created_at       │
│ updated_at       │
└──────────────────┘
```

---

## Usage Example

```python
from rock.sdk.envhub.datasets import DatasetMetadataClient, SortField, SortOrder

# Initialize
client = DatasetMetadataClient("postgresql+psycopg2://user:pass@localhost/envhub")

# Register a benchmark dataset
client.register_dataset(
    "princeton-nlp", "SWE-bench_Verified",
    description="Verified subset of SWE-bench",
    tags=["swe", "verified"],
    owner="swe-bench-team",
)

# Batch import instances (with tags)
instances = [
    {"name": f"task-{i}", "language": "python", "difficulty": "medium", "tags": ["python", "swe"]}
    for i in range(500)
]
client.register_instances_batch("princeton-nlp", "SWE-bench_Verified", "test", instances)

# Browse — dataset info includes SplitInfo objects with full metadata
info = client.get_dataset("princeton-nlp", "SWE-bench_Verified")
for s in info.splits:
    print(f"Split: {s.name}, Tasks: {s.task_count}, Created: {s.created_at}")
# Backward-compatible property: info.task_counts -> {"test": 500}
print(f"Counts: {info.task_counts}")

# List datasets sorted by update time (default)
result = client.list_datasets(org="princeton-nlp")

# List datasets sorted by name
result = client.list_datasets(sort_by=SortField.NAME, sort_order=SortOrder.ASC)

# List split details
splits = client.list_dataset_split_info("princeton-nlp", "SWE-bench_Verified")
for s in splits.items:
    print(f"Split: {s.name}, Tasks: {s.task_count}")

# List task entries with sorting and tags
entries = client.list_dataset_task_entries(
    "princeton-nlp", "SWE-bench_Verified", "test",
    sort_by=SortField.UPDATED_AT, sort_order=SortOrder.DESC,
)
for e in entries.items:
    print(f"Task: {e.name}, Tags: {e.tags}, Created: {e.created_at}")

# Permission control
client.grant_permission("princeton-nlp", "SWE-bench_Verified", "alice", role="editor")

# Audit trail
client.log_event("dataset", "princeton-nlp/SWE-bench_Verified", "import", "system",
                 changes={"instances_added": 500})
```

---

## Testing

63 unit tests covering:
- Dataset CRUD (register/list/get/delete)
- Instance CRUD (register/batch/get/delete)
- Image CRUD (register/list/update/delete)
- Permission CRUD (grant/revoke/get/list)
- Audit event logging and querying
- **Dataset 排序** — 默认 `updated_at DESC`，按 name/created_at/updated_at 排序
- **Task 排序** — 按 name/updated_at 排序，task entries 包含 created_at
- **Instance 标签** — 注册带 tags，task entry 返回 tags，批量注册带 tags
- **Split 表** — 自动创建、列表查询、排序、task_count 计数、删除级联、dataset info 聚合、recalculate、批量注册创建 split
- SQLite dialect fallback

Run tests:
```bash
uv run pytest tests/unit/datasets/test_metadata_client.py -v
```
