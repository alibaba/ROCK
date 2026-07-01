# DatasetMetadataClient — Pure DB-Backed Metadata Management

## Summary

引入独立的数据库驱动元数据客户端 `DatasetMetadataClient`，将 Dataset 元数据管理从 OSS 文件操作中解耦。支持 PostgreSQL（生产）和 SQLite（测试/本地开发）双方言。

## Motivation

原有 `DatasetClient` 将文件操作（browse/read/download/upload/sync）与元数据管理耦合在一起。随着 EnvHub 向结构化元数据方向演进，需要一个纯数据库层的 SDK 来管理 Dataset、Instance、Image、Permission 和审计日志，而不依赖 OSS。

## Architecture

```
rock/sdk/envhub/datasets/
├── __init__.py              # 导出 DatasetMetadataClient + 数据模型
├── database.py              # SDK 独立 ORM 模型 (Dataset, Instance, Image, Permission, AuditEvent)，自有 Base
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

分页查询 datasets，支持按 org 过滤和模糊搜索。

```python
result = client.list_datasets(
    org="princeton-nlp",  # 可选，按组织过滤
    query="SWE",          # 可选，模糊搜索 org/name
    offset=0,
    limit=20,
)
# Returns: PageResult[DatasetInfo]
# result.items: list[DatasetInfo]
# result.total: int
# result.offset: int
# result.limit: int | None
```

---

#### `get_dataset`

获取单个 dataset 信息。

```python
info = client.get_dataset("princeton-nlp", "SWE-bench_Verified")
# Returns: DatasetInfo | None
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

注册或更新一个 instance（task）。若 dataset 不存在则自动创建。

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
| `raw` | `str \| None` | No | 原始数据 (JSON string) |
| `source_revision` | `str \| None` | No | 导入时的源版本 |
| `imported_from` | `str \| None` | No | 导入来源标识 |
| `created_by` | `str \| None` | No | 创建者 |

---

#### `register_instances_batch`

批量注册 instances。

```python
count = client.register_instances_batch(
    org="princeton-nlp",
    dataset="SWE-bench_Verified",
    split="test",
    instances=[
        {"name": "django__django-11099", "language": "python", "difficulty": "medium"},
        {"name": "django__django-11283", "language": "python", "difficulty": "hard"},
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

删除单个 instance，自动更新 task_counts。

```python
ok = client.delete_instance("princeton-nlp", "SWE-bench_Verified", "test", "django__django-11099")
# Returns: bool
```

---

#### `recalculate_task_counts`

重新计算 dataset 的 task_counts（从 instances 表聚合）。

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

列出 dataset 的所有 splits。

```python
splits = client.list_dataset_splits("princeton-nlp", "SWE-bench_Verified")
# Returns: list[str]  e.g. ["test", "dev"]
```

---

#### `list_dataset_tasks`

列出某个 split 下所有 instance 名称（分页）。

```python
result = client.list_dataset_tasks(
    "princeton-nlp", "SWE-bench_Verified", "test",
    query="django",  # 可选模糊搜索
    offset=0, limit=100,
)
# Returns: PageResult[str]
```

---

#### `list_dataset_task_entries`

列出某个 split 下所有 instance 的详细信息（分页）。

```python
result = client.list_dataset_task_entries(
    "princeton-nlp", "SWE-bench_Verified", "test",
    query="django",
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
    splits: list[str] = []           # 可用 splits
    task_counts: dict[str, int] = {} # {split: count}
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
    created_by: str | None = None
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

### Tables

| Table | Primary Key | Unique Constraints |
|-------|------------|-------------------|
| `datasets` | `id` (auto) | `(org, name)` |
| `instances` | `id` (auto) | `(dataset_id, split, name)` |
| `images` | `source_image_uri` | — |
| `dataset_permissions` | `id` (auto) | `(dataset_id, user_id)` |
| `audit_events` | `id` (auto) | — |

### Indexes

- `datasets.org` — 按组织查询
- `instances.(dataset_id, split)` — 复合索引
- `instances.format` — 按格式过滤
- `instances.language` — 按语言过滤
- `images.status` — 按状态过滤
- `dataset_permissions.user_id` — 按用户查询权限
- `audit_events.target_type` — 审计查询
- `audit_events.event_type` — 审计查询
- `audit_events.(target_type, target_id)` — 复合索引

### Relationships

```
Dataset 1──N Instance       (cascade delete)
Dataset 1──N DatasetPermission (cascade delete)
```

---

## Usage Example

```python
from rock.sdk.envhub.datasets import DatasetMetadataClient

# Initialize
client = DatasetMetadataClient("postgresql+psycopg2://user:pass@localhost/envhub")

# Register a benchmark dataset
client.register_dataset(
    "princeton-nlp", "SWE-bench_Verified",
    description="Verified subset of SWE-bench",
    tags=["swe", "verified"],
    owner="swe-bench-team",
)

# Batch import instances
instances = [
    {"name": f"task-{i}", "language": "python", "difficulty": "medium"}
    for i in range(500)
]
client.register_instances_batch("princeton-nlp", "SWE-bench_Verified", "test", instances)

# Browse
info = client.get_dataset("princeton-nlp", "SWE-bench_Verified")
print(f"Splits: {info.splits}, Counts: {info.task_counts}")

# Permission control
client.grant_permission("princeton-nlp", "SWE-bench_Verified", "alice", role="editor")

# Audit trail
client.log_event("dataset", "princeton-nlp/SWE-bench_Verified", "import", "system",
                 changes={"instances_added": 500})
```

---

## Testing

41 unit tests covering:
- Dataset CRUD (register/list/get/delete)
- Instance CRUD (register/batch/get/delete)
- Image CRUD (register/list/update/delete)
- Permission CRUD (grant/revoke/get/list)
- Audit event logging and querying
- task_counts automatic maintenance
- SQLite dialect fallback

Run tests:
```bash
uv run pytest tests/unit/datasets/test_metadata_client.py -v
```
