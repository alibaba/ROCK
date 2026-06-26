# Datasets SDK 接口文档

`rock.sdk.envhub.datasets` 提供对 OSS 上托管的数据集的完整 CRUD 操作，包括列表、查询、浏览、下载和上传。

## 快速开始

```python
from rock.sdk.bench.models.job.config import OssRegistryInfo
from rock.sdk.envhub.datasets import DatasetClient

client = DatasetClient(OssRegistryInfo(
    oss_access_key_id="...",
    oss_access_key_secret="...",
    oss_endpoint="oss-ap-southeast-1.aliyuncs.com",
    oss_bucket="rock-agent-pre",
    oss_dataset_path="datasets",
))
```

## 数据模型

### `PageResult[T]`

所有 list 接口的统一返回类型，支持 offset/limit 分页。

| 字段 | 类型 | 说明 |
|------|------|------|
| `items` | `list[T]` | 当前页数据 |
| `total` | `int` | 未分页的总条数 |
| `offset` | `int` | 跳过的条目数 |
| `limit` | `int \| None` | 每页上限，`None` 表示不限 |

### `DatasetSpec`

数据集完整规格，包含所有 task ID。

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | `str` | `"org/dataset"`，如 `"AI4AIScaling/swe-seed"` |
| `split` | `str` | split 名称，如 `"test"` |
| `task_ids` | `list[str]` | 该 split 下所有 task ID |

### `DatasetInfo`

数据集摘要信息。

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | `str` | `"org/dataset"` |
| `splits` | `list[str]` | 所有 split 名称 |
| `task_counts` | `dict[str, int]` | 每个 split 的 task 数量 |

### `TaskInfo`

单个 task 的详情（仅目录型 task）。

| 字段 | 类型 | 说明 |
|------|------|------|
| `task_id` | `str` | task ID |
| `dataset_id` | `str` | `"org/dataset"` |
| `split` | `str` | split 名称 |
| `files` | `list[TaskFileInfo]` | 文件列表 |
| `total_size` | `int` | 所有文件总字节数 |

### `TaskFileInfo`

task 下单个文件的元数据。

| 字段 | 类型 | 说明 |
|------|------|------|
| `path` | `str` | 相对于 task 目录的文件路径 |
| `size` | `int` | 文件字节数 |
| `last_modified` | `str` | ISO 8601 时间戳 |

### `TaskEntry`

task 条目信息，区分文件型和目录型 task。

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | `str` | task 名称（文件型去掉后缀） |
| `path` | `str` | 原始路径，如 `"task-001"` 或 `"task-001.json"` |
| `type` | `str` | `"file"` 或 `"directory"` |
| `size` | `int \| None` | 文件大小（字节），目录为 `None` |
| `file_count` | `int \| None` | 文件数量；file=1，directory=`None` |
| `updated_at` | `str \| None` | ISO 8601 时间戳，目录为 `None` |
| `etag` | `str \| None` | OSS ETag，目录为 `None` |

### `FileEntry`

目录浏览条目，支持文件和子目录。

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | `str` | 文件名或目录名 |
| `path` | `str` | 相对于 task 根的路径 |
| `type` | `str` | `"file"` 或 `"directory"` |
| `size` | `int \| None` | 文件大小，目录为 `None` |
| `media_type` | `str \| None` | MIME 类型（如 `"application/json"`），目录为 `None` |
| `updated_at` | `str \| None` | ISO 8601 时间戳，目录为 `None` |
| `etag` | `str \| None` | OSS ETag，目录为 `None` |

### `TaskMetadata`

task 元数据发现结果。

| 字段 | 类型 | 说明 |
|------|------|------|
| `source` | `str` | 来源文件名（如 `"README.md"`）或 `"generated"` |
| `format` | `str` | `"markdown"` / `"json"` / `"toml"` |
| `content` | `str` | 文本内容 |
| `parsed` | `Any` | JSON 解析结果；非 JSON 为 `None` |
| `generated` | `bool` | 是否为 fallback 自动生成 |

### `UploadResult`

上传操作的结果汇总。

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | `str` | `"org/dataset"` |
| `split` | `str` | split 名称 |
| `uploaded` | `int` | 成功上传的 task 数 |
| `skipped` | `int` | 跳过的 task 数（已存在） |
| `failed` | `int` | 失败的 task 数 |

### `DatasetSyncSummary`

同步操作的统计汇总。

| 字段 | 类型 | 说明 |
|------|------|------|
| `source_objects` | `int` | 源端文件数 |
| `target_objects` | `int` | 目标端文件数 |
| `to_copy` | `int` | 待拷贝文件数 |
| `to_delete` | `int` | 待删除文件数 |
| `copied` | `int` | 实际拷贝数 |
| `deleted` | `int` | 实际删除数 |
| `skipped` | `int` | 跳过数（一致） |
| `failed` | `int` | 失败数 |

### `DatasetSyncDiffList`

同步差异列表（预览模式返回）。

| 字段 | 类型 | 说明 |
|------|------|------|
| `items` | `list[str]` | 差异文件路径预览（最多 `limit` 条） |
| `total` | `int` | 差异总数 |
| `truncated` | `bool` | 是否被截断 |
| `omitted` | `int` | 省略条数 |

### `DatasetSyncDiff`

同步差异预览。

| 字段 | 类型 | 说明 |
|------|------|------|
| `limit` | `int` | 差异预览上限 |
| `to_copy` | `DatasetSyncDiffList` | 待拷贝文件列表 |
| `to_delete` | `DatasetSyncDiffList` | 待删除文件列表 |

### `DatasetSyncFailure`

同步失败详情。

| 字段 | 类型 | 说明 |
|------|------|------|
| `path` | `str` | 失败文件路径 |
| `operation` | `str` | `"copy"` 或 `"delete"` |
| `message` | `str` | 错误信息 |

### `DatasetSyncResult`

跨 Bucket 同步操作的返回结果。

| 字段 | 类型 | 说明 |
|------|------|------|
| `dataset` | `str` | 数据集路径 |
| `path` | `str` | 同步路径 |
| `scope` | `str` | `"file"` 或 `"folder"` |
| `dry_run` | `bool` | 是否为预览模式 |
| `delete_extra` | `bool` | 是否删除目标端多余文件 |
| `summary` | `DatasetSyncSummary` | 统计汇总 |
| `diff` | `DatasetSyncDiff \| None` | 差异预览（仅 dry_run=True 时有值） |
| `failures` | `list[DatasetSyncFailure]` | 失败详情列表 |

---

## 接口列表

### 1. 列出所有组织

```python
client.list_organizations(*, offset=0, limit=None) -> PageResult[str]
```

返回 OSS registry 中所有组织名称。

```python
page = client.list_organizations()
# PageResult(items=['AI4AIScaling', 'JobBench', ...], total=111, offset=0, limit=None)

page = client.list_organizations(offset=2, limit=3)
# PageResult(items=['AI4AIScaling', 'AIScaling', 'AmazonScience'], total=111, offset=2, limit=3)
```

---

### 2. 列出组织下的数据集

```python
client.list_org_datasets(organization, *, offset=0, limit=None) -> PageResult[str]
```

返回指定组织下的所有数据集名称。

```python
page = client.list_org_datasets("AI4AIScaling")
# PageResult(items=['swe-seed'], total=1, offset=0, limit=None)
```

---

### 3. 列出所有数据集

```python
client.list_all_datasets(concurrency=10, *, query=None, offset=0, limit=None) -> PageResult[tuple[str, str]]
```

并发扫描所有组织，返回 `(organization, dataset)` 元组列表。支持 `query` 关键词过滤（对 `"org/dataset"` 做大小写不敏感子串匹配）。

```python
page = client.list_all_datasets(offset=0, limit=3)
# PageResult(items=[('AI4AIScaling', 'swe-seed'), ('JobBench', 'job-bench'), ...], total=248, ...)

page = client.list_all_datasets(query="pinch")
# 只返回 org/dataset 中包含 "pinch" 的记录
```

> **性能提示**：此接口会并发请求所有组织，首次调用延迟较高。

---

### 4. 列出数据集的 split

```python
client.list_dataset_splits(organization, dataset, *, offset=0, limit=None) -> PageResult[str]
```

返回数据集下的所有 split 名称。

```python
page = client.list_dataset_splits("JobBench", "job-bench")
# PageResult(items=['easy', 'easy-assets', 'main', 'main-assets'], total=4, ...)
```

---

### 5. 列出完整数据集规格

```python
client.list_datasets(org=None, *, offset=0, limit=None) -> PageResult[DatasetSpec]
```

按组织列出数据集的完整规格，包含每个 split 的所有 task ID。

```python
page = client.list_datasets(org="AI4AIScaling")
# PageResult(items=[DatasetSpec(id='AI4AIScaling/swe-seed', split='test', task_ids=[...])], ...)
```

> **性能提示**：不指定 `org` 时需遍历所有组织并枚举全部 task，建议传入 `org`。

---

### 6. 列出 split 下的 task ID

```python
client.list_dataset_tasks(organization, dataset, split="test",
                          *, query=None, offset=0, limit=None) -> PageResult[str] | None
```

返回 split 下所有 task ID 的字符串列表。无 task 时返回 `None`。支持 `query` 关键词过滤。

```python
page = client.list_dataset_tasks("AI4AIScaling", "swe-seed", "test")
# PageResult(items=['dask__dask-9250', 'pydantic__pydantic-5021', ...], total=6, ...)

page = client.list_dataset_tasks("AI4AIScaling", "swe-seed", "test", query="dask")
# 只返回 task ID 中包含 "dask" 的记录
```

---

### 7. 列出 task 条目（含类型信息）

```python
client.list_dataset_task_entries(organization, dataset, split="test",
                                 *, query=None, offset=0, limit=None) -> PageResult[TaskEntry] | None
```

与 `list_dataset_tasks` 类似，但返回丰富的 `TaskEntry` 对象，区分文件型和目录型 task，并携带 size/etag/updated_at 元数据。

```python
page = client.list_dataset_task_entries("AI4AIScaling", "swe-seed", "test")
# 文件型 task:
# TaskEntry(name='dask__dask-9250', path='dask__dask-9250.json', type='file',
#           size=12345, file_count=1, updated_at='2026-06-10T...', etag='"abc"')

page = client.list_dataset_task_entries("JobBench", "job-bench", "easy-assets")
# 目录型 task:
# TaskEntry(name='biostatisticians_task1', path='biostatisticians_task1', type='directory',
#           size=None, file_count=None, updated_at=None, etag=None)
```

---

### 8. 获取数据集详情

```python
client.get_dataset(organization, dataset) -> DatasetInfo | None
```

返回数据集的 splits 列表和每个 split 的 task 数量。不存在时返回 `None`。

```python
info = client.get_dataset("AI4AIScaling", "swe-seed")
# DatasetInfo(id='AI4AIScaling/swe-seed', splits=['test'], task_counts={'test': 6})
```

---

### 9. 获取 task 详情

```python
client.get_task(organization, dataset, split, task_id) -> TaskInfo | None
```

返回 task 的文件列表和总大小。不存在时返回 `None`。

```python
task = client.get_task("JobBench", "job-bench", "easy-assets", "biostatisticians_task1")
# TaskInfo(task_id='biostatisticians_task1', dataset_id='JobBench/job-bench', split='easy-assets',
#          files=[TaskFileInfo(path='content.tgz', size=4317316, ...)], total_size=4317316)
```

> **注意**：仅适用于目录型 task。文件型 task（单个 `.json` 文件）返回 `None`。

---

### 10. 获取 task 元数据

```python
client.get_task_metadata(organization, dataset, split, task_id) -> TaskMetadata | None
```

智能发现 task 的元数据文件。按以下优先级依次查找：`README.md` → `readme.md` → `metadata.json` → `task.toml`。找到后返回内容，JSON 文件额外解析到 `parsed` 字段。全部不存在时自动生成文件列表摘要（`generated=True`）。task 完全不存在时返回 `None`。

```python
# 有 README.md 时:
meta = client.get_task_metadata("org", "ds", "split", "task-with-readme")
# TaskMetadata(source='README.md', format='markdown', content='# ...', parsed=None, generated=False)

# 有 metadata.json 时:
meta = client.get_task_metadata("org", "ds", "split", "task-with-json")
# TaskMetadata(source='metadata.json', format='json', content='{"title": "..."}',
#              parsed={"title": "..."}, generated=False)

# fallback 自动生成:
meta = client.get_task_metadata("JobBench", "job-bench", "easy-assets", "biostatisticians_task1")
# TaskMetadata(source='generated', format='markdown',
#              content='# biostatisticians_task1\n\nFiles:\n\n- content.tgz (4317316 bytes)',
#              parsed=None, generated=True)
```

---

### 11. 层级浏览 task 文件

```python
client.browse_task_files(organization, dataset, split, task_id,
                         prefix="", *, offset=0, limit=None) -> PageResult[FileEntry]
```

按目录层级浏览 task 内部的文件和子目录（类似文件管理器）。子目录排在文件前面。通过 `prefix` 参数进入子目录。

```python
# 根目录
page = client.browse_task_files("JobBench", "job-bench", "easy-assets", "biostatisticians_task1")
# PageResult(items=[
#     FileEntry(name='content.tgz', path='content.tgz', type='file',
#               size=4317316, media_type='application/gzip', ...)
# ], total=1, ...)

# 进入子目录
page = client.browse_task_files("org", "ds", "split", "task-1", prefix="data")
# PageResult(items=[
#     FileEntry(name='subdir', path='data/subdir', type='directory', ...),
#     FileEntry(name='input.json', path='data/input.json', type='file', size=500, ...)
# ], ...)
```

---

### 12. 列出 task 下的所有文件（扁平）

```python
client.list_task_files(organization, dataset, split, task_id,
                       *, offset=0, limit=None) -> PageResult[TaskFileInfo]
```

递归列出 task 下的所有文件（扁平结构，不含目录条目），返回路径、大小、修改时间。

```python
page = client.list_task_files("JobBench", "job-bench", "easy-assets", "biostatisticians_task1")
# PageResult(items=[TaskFileInfo(path='content.tgz', size=4317316, last_modified='...')], ...)
```

---

### 13. 读取文件内容

```python
client.read_task_file(organization, dataset, split, task_id, file_path) -> bytes
```

以 `bytes` 形式读取 task 下指定文件的全部内容到内存。

```python
data = client.read_task_file("JobBench", "job-bench", "easy-assets", "biostatisticians_task1", "content.tgz")
# bytes, len=4317316
```

---

### 14. 下载单个文件

```python
client.download_task_file(organization, dataset, split, task_id, file_path, local_path) -> Path
```

下载指定文件到本地路径，自动创建父目录。

```python
from pathlib import Path
result = client.download_task_file(
    "JobBench", "job-bench", "easy-assets", "biostatisticians_task1",
    "content.tgz", Path("/tmp/content.tgz")
)
# Path('/tmp/content.tgz'), exists=True, size=4317316
```

---

### 15. 下载整个 task

```python
client.download_task(organization, dataset, split, task_id, local_dir, concurrency=4) -> Path
```

并发下载 task 下的所有文件到本地目录。返回 task 子目录路径。

```python
task_dir = client.download_task(
    "JobBench", "job-bench", "easy-assets", "biostatisticians_task1",
    Path("/tmp/download"), concurrency=4
)
# Path('/tmp/download/biostatisticians_task1')
```

---

### 16. 刷新元数据缓存

```python
client.refresh_metadata(organization, dataset, split=None, concurrency=4) -> dict
```

重新计算每个 split 的 task 数量，写入 `meta/{org}/{dataset}/{split}.json` 缓存。后续 `get_dataset()` 调用优先读取缓存而非实时计数。

```python
# 刷新全部 splits
meta = client.refresh_metadata("org", "ds")
# {"splits": {"test": {"task_count": 500}, "train": {"task_count": 1600}}}

# 仅刷新指定 split
meta = client.refresh_metadata("org", "ds", split="test")
# {"splits": {"test": {"task_count": 500}}}
```

**自动触发**：
- `upload_dataset()` 上传成功后自动刷新对应 split
- `sync_dataset()` 非 dry-run 且有文件拷贝时在 target 上自动刷新

---

### 17. 上传数据集

```python
client.upload_dataset(source, target, concurrency=4) -> UploadResult
```

将本地目录结构上传到 OSS。每个子目录作为一个 task 上传。

```python
from rock.sdk.bench.models.job.config import LocalDatasetConfig, RegistryDatasetConfig

source = LocalDatasetConfig(path=Path("/data/my-bench"))
target = RegistryDatasetConfig(
    name="org/my-bench", version="test", overwrite=False, registry=registry_info
)
result = client.upload_dataset(source, target, concurrency=8)
# UploadResult(id='org/my-bench', split='test', uploaded=10, skipped=2, failed=0)
```

---

### 18. 跨 Bucket 同步

```python
client.sync_dataset(
    dataset, target: OssRegistryInfo,
    *, split=None, dry_run=True, delete_extra=False
) -> DatasetSyncResult
```

将当前 registry 的数据集增量同步到另一个 OSS Bucket。基于 key/size/etag 对比，仅拷贝变更的文件。

```python
from rock.sdk.bench.models.job.config import OssRegistryInfo

target = OssRegistryInfo(
    oss_bucket="target-bucket",
    oss_endpoint="oss-ap-southeast-1.aliyuncs.com",
    oss_access_key_id="...",
    oss_access_key_secret="...",
)

# 预览模式（dry_run=True，默认）— 只返回差异，不执行拷贝
result = client.sync_dataset("org/ds", target, split="test")
print(f"To copy: {result.summary.to_copy}, To delete: {result.summary.to_delete}")

# 执行同步
result = client.sync_dataset("org/ds", target, split="test", dry_run=False)
print(f"Copied: {result.summary.copied}, Skipped: {result.summary.skipped}")

# 删除 target 上多余的文件
result = client.sync_dataset("org/ds", target, dry_run=False, delete_extra=True)
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `dataset` | `str` | — | 数据集路径，如 `"org/ds"` 或 `"org/ds/test"` |
| `target` | `OssRegistryInfo` | — | 目标 Bucket 配置 |
| `split` | `str \| None` | `None` | 仅同步指定 split，`None` 同步全部 |
| `dry_run` | `bool` | `True` | `True` 预览差异，`False` 执行拷贝 |
| `delete_extra` | `bool` | `False` | 删除目标端多余的文件 |

**同步原理**：
- 递归列出 source/target 的所有对象
- 对比 size + etag，仅标记变更为待拷贝
- `dry_run=False` 时使用 OSS `copy_object`（同 region 零流量），失败时降级为 `get` + `put`
- 同步完成后自动刷新 target 的元数据缓存

---

### 19. transfer_images（预留）

```python
client.transfer_images(**kwargs) -> None  # raises NotImplementedError
```

---

### 20. audit_dataset（预留）

```python
client.audit_dataset(**kwargs) -> None  # raises NotImplementedError
```

---

## 接口速查表

| # | 方法 | 返回类型 | 分页 | 搜索 |
|---|------|----------|------|------|
| 1 | `list_organizations()` | `PageResult[str]` | Yes | — |
| 2 | `list_org_datasets(org)` | `PageResult[str]` | Yes | — |
| 3 | `list_all_datasets(concurrency, query)` | `PageResult[tuple[str, str]]` | Yes | Yes |
| 4 | `list_dataset_splits(org, ds)` | `PageResult[str]` | Yes | — |
| 5 | `list_datasets(org)` | `PageResult[DatasetSpec]` | Yes | — |
| 6 | `list_dataset_tasks(org, ds, split, query)` | `PageResult[str] \| None` | Yes | Yes |
| 7 | `list_dataset_task_entries(org, ds, split, query)` | `PageResult[TaskEntry] \| None` | Yes | Yes |
| 8 | `get_dataset(org, ds)` | `DatasetInfo \| None` | — | — |
| 9 | `get_task(org, ds, split, task_id)` | `TaskInfo \| None` | — | — |
| 10 | `get_task_metadata(org, ds, split, task_id)` | `TaskMetadata \| None` | — | — |
| 11 | `browse_task_files(org, ds, split, task_id, prefix)` | `PageResult[FileEntry]` | Yes | — |
| 12 | `list_task_files(org, ds, split, task_id)` | `PageResult[TaskFileInfo]` | Yes | — |
| 13 | `read_task_file(...)` | `bytes` | — | — |
| 14 | `download_task_file(...)` | `Path` | — | — |
| 15 | `download_task(...)` | `Path` | — | — |
| 16 | `refresh_metadata(org, ds, split, concurrency)` | `dict` | — | — |
| 17 | `upload_dataset(source, target, concurrency)` | `UploadResult` | — | — |
| 18 | `sync_dataset(dataset, target, split, dry_run, delete_extra)` | `DatasetSyncResult` | — | — |
| 19 | `transfer_images()` | `NotImplementedError` | — | — |
| 20 | `audit_dataset()` | `NotImplementedError` | — | — |

## 分页说明

所有 list 接口统一使用 `offset/limit` 分页：

- `offset`（默认 `0`）：跳过前 N 条记录
- `limit`（默认 `None`）：最大返回条数，`None` 表示不限
- `PageResult.total` 始终反映未分页的总数

```python
# 第 2 页，每页 10 条
page = client.list_organizations(offset=10, limit=10)
print(f"第 {page.offset // page.limit + 1} 页，共 {(page.total + page.limit - 1) // page.limit} 页")
```

> 分页在 SDK 层以内存切片实现：OSS 侧获取完整列表后在内存中截取。`limit` 不会减少首次请求延迟。

## 搜索说明

支持 `query` 参数的接口（`list_all_datasets`、`list_dataset_tasks`、`list_dataset_task_entries`）使用大小写不敏感的子串匹配：

```python
# 搜索包含 "dask" 的 task
page = client.list_dataset_tasks("AI4AIScaling", "swe-seed", "test", query="dask")

# 搜索包含 "pinch" 的数据集
page = client.list_all_datasets(query="pinch")
```

## 文件型 vs 目录型 task

OSS 上的 task 存在两种形式：

| 形式 | OSS 结构 | 示例 |
|------|----------|------|
| **目录型** | `datasets/org/ds/split/task-id/file1, file2, ...` | JobBench/job-bench |
| **文件型** | `datasets/org/ds/split/task-id.json` | AI4AIScaling/swe-seed |

- `list_dataset_tasks` 和 `list_dataset_task_entries` 同时支持两种形式
- `get_task`、`list_task_files`、`browse_task_files`、`download_task` 仅适用于目录型 task
- `list_dataset_task_entries` 通过 `TaskEntry.type` 字段区分两种形式
