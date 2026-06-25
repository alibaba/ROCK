# Datasets SDK v2 设计文档

## 目录

- [背景与目标](#背景与目标)
- [核心概念](#核心概念)
- [OSS 存储结构](#oss-存储结构)
- [模块结构](#模块结构)
- [数据模型](#数据模型)
- [核心组件](#核心组件)
  - [BaseDatasetRegistry](#basedatasetregistry)
  - [OssDatasetRegistry](#ossdatasetregistry)
  - [DatasetClient](#datasetclient)
  - [FormatParser](#formatparser)
- [SDK 对外接口](#sdk-对外接口)
  - [Listing APIs](#listing-apis)
  - [Query APIs](#query-apis)
  - [Task File Operations](#task-file-operations)
  - [Upload](#upload)
  - [Format Parsing](#format-parsing)
- [CLI 命令](#cli-命令)
- [分页机制](#分页机制)
- [数据流](#数据流)
- [错误处理](#错误处理)
- [性能优化](#性能优化)
- [测试策略](#测试策略)

---

## 背景与目标

ROCK 的 Datasets SDK（`rock.sdk.envhub.datasets`）提供对 OSS 上 benchmark 数据集的统一访问能力。v1 版本仅支持基本的列表和上传操作，在构建交互式数据集浏览工具和 benchmark runner 时存在明显不足：

1. **无分页** — 列表 API 一次性返回全部结果，大规模数据集下性能差、内存占用高
2. **无文件级操作** — 无法浏览、读取、下载单个 task 内的文件
3. **无结构化查询** — 无法查看 dataset 的 splits/task 数量概览，无法获取 task 的 metadata
4. **无格式解析** — 不同 benchmark（SWE-bench、PinchBench、TB2）的 task payload 结构各异，缺乏统一解析能力
5. **性能问题** — 每次操作都重建 OSS Bucket 实例，大量列表场景下无法流式遍历

### 目标

**1. 统一分页** — 所有列表 API 返回 `PageResult[T]`，支持 `offset` / `limit` 参数，CLI 展示 "Showing X-Y of Z" 摘要。

**2. 新增查询 API** — `get_dataset()` 获取 dataset 概览（splits 列表 + 每个 split 的 task 数），`get_task()` 获取 task 详情（文件列表与总大小），`get_task_metadata()` 自动发现并解析 task 级元数据文件。

**3. 文件级操作** — 浏览（`browse_task_files`）、列表（`list_task_files`）、读取（`read_task_file`）、下载单文件（`download_task_file`）、下载整个 task（`download_task`）。

**4. 可插拔格式解析** — 注册制 `FormatParser`，支持 PinchBench / SWE-bench / TB2 等 benchmark 格式的结构化 task 解析。

**5. 性能优化** — OSS Bucket 实例缓存；基于 continuation token 的迭代式列表（不再受 1000 条单页限制）。

---

## 核心概念

| 概念 | 说明 |
|------|------|
| Organization | 数据集的组织归属，如 `princeton-nlp`、`pinchbench` |
| Dataset | 一个 benchmark 数据集，如 `SWE-bench_Verified` |
| Split | 数据集的划分，如 `test`、`train`、`v1.0` |
| Task | Split 下的一个评测实例，可以是一个目录（包含多个文件）或单个文件 |
| Task ID | Task 的唯一标识，对应 OSS 上的目录名或去后缀的文件名 |
| PageResult | 分页结果容器，包含 `items`、`total`、`offset`、`limit` |

### 数据集标识约定

- Dataset ID 格式为 `{organization}/{dataset_name}`，如 `princeton-nlp/SWE-bench_Verified`
- 在 CLI 和 API 中，`organization` 和 `dataset` 作为两个独立参数传递
- `split` 默认值为 `"test"`

---

## OSS 存储结构

数据集存储在 OSS Bucket 的固定路径下（可通过 `oss_dataset_path` 配置，默认 `datasets`）：

```
{oss_dataset_path}/
├── {organization}/
│   ├── {dataset}/
│   │   ├── {split}/
│   │   │   ├── {task_id}/          # 目录型 task
│   │   │   │   ├── README.md       # 可选：task metadata
│   │   │   │   ├── metadata.json   # 可选：task metadata
│   │   │   │   ├── patch.diff
│   │   │   │   └── ...
│   │   │   ├── {task_id}.json      # 文件型 task
│   │   │   └── ...
│   │   └── {split2}/
│   └── {dataset2}/
└── {organization2}/
```

**Task 的两种形态**：
- **目录型 task** — OSS prefix（子目录），可包含多个文件
- **文件型 task** — 直接位于 split 目录下的文件（task ID 为去后缀的文件名）

两种形态会被合并去重后返回。

---

## 模块结构

```
rock/
└── sdk/
    └── envhub/
        └── datasets/
            ├── __init__.py              # 对外入口：DatasetClient + 全部数据模型
            ├── client.py                # DatasetClient — SDK 统一入口
            ├── models.py                # 数据模型：PageResult, DatasetSpec, DatasetInfo, ...
            ├── registry/
            │   ├── base.py              # BaseDatasetRegistry — 抽象基类
            │   └── oss.py               # OssDatasetRegistry — OSS 后端实现
            └── formats/
                ├── __init__.py           # 对外入口：FormatParser, get_parser, register_format
                ├── base.py              # FormatParser 抽象基类 + 注册表
                ├── pinchbench.py        # PinchBench 格式解析器
                ├── swe.py               # SWE-bench 格式解析器
                └── tb2.py               # TB2 格式解析器
```

---

## 数据模型

所有模型定义在 `rock/sdk/envhub/datasets/models.py`，使用 `dataclass`：

### PageResult[T]（新增）

分页结果的泛型容器，所有列表 API 的返回类型。

```python
@dataclass
class PageResult(Generic[T]):
    items: list[T]      # 当前页数据
    total: int           # 总数（分页前）
    offset: int          # 起始偏移
    limit: int | None    # 请求的最大条数，None 表示不限
```

### DatasetSpec（已有，语义不变）

```python
@dataclass
class DatasetSpec:
    id: str                              # "{org}/{dataset}"，如 "princeton-nlp/SWE-bench_Verified"
    split: str                           # split 名称
    task_ids: list[str] = field(...)     # split 下的 task ID 列表
```

### DatasetInfo（新增）

数据集概览信息，由 `get_dataset()` 返回。

```python
@dataclass
class DatasetInfo:
    id: str                              # "org/dataset"
    splits: list[str] = field(...)       # split 名称列表
    task_counts: dict[str, int] = field(...)  # 每个 split 的 task 数量
```

### TaskInfo（新增）

单个 task 的详细信息，由 `get_task()` 返回。

```python
@dataclass
class TaskInfo:
    task_id: str                         # task 标识
    dataset_id: str                      # "org/dataset"
    split: str                           # 所属 split
    files: list[TaskFileInfo] = field(...)  # 文件列表
    total_size: int = 0                  # 所有文件总大小（bytes）
```

### TaskFileInfo（新增）

task 下单个文件的元信息，用于 `list_task_files()` 和 `TaskInfo.files`。

```python
@dataclass
class TaskFileInfo:
    path: str            # 相对于 task 目录的路径
    size: int            # 文件大小（bytes）
    last_modified: str   # ISO 8601 格式的修改时间
```

### TaskEntry（新增）

task 条目信息（含类型、大小等元信息），用于 `list_dataset_task_entries()`。与 `list_dataset_tasks()` 返回纯字符串不同，此类型携带更丰富的元信息。

```python
@dataclass
class TaskEntry:
    name: str                   # task 名称（去后缀）
    path: str                   # 原始路径
    type: str                   # "file" 或 "directory"
    size: int | None = None     # 文件大小（仅文件型 task）
    file_count: int | None = None  # 文件数（仅文件型 task = 1）
    updated_at: str | None = None  # ISO 8601 修改时间
    etag: str | None = None     # OSS ETag
```

### FileEntry（新增）

目录浏览的条目信息，用于 `browse_task_files()`。

```python
@dataclass
class FileEntry:
    name: str                        # 文件/目录名
    path: str                        # 相对于 task 的路径
    type: str                        # "file" 或 "directory"
    size: int | None = None          # 文件大小（仅文件）
    media_type: str | None = None    # MIME 类型（自动推测）
    updated_at: str | None = None    # ISO 8601 修改时间
    etag: str | None = None          # OSS ETag
```

### TaskMetadata（新增）

task 级元数据，由 `get_task_metadata()` 返回。

```python
@dataclass
class TaskMetadata:
    source: str          # 来源文件名（如 "README.md"、"metadata.json"）或 "generated"
    format: str          # "markdown"、"json"、"toml"、"text"
    content: str         # 原始内容
    parsed: Any = None   # 解析后的结构化数据（仅 json 格式）
    generated: bool = False  # 是否为自动生成的（无元数据文件时的 fallback）
```

### UploadResult（已有，语义不变）

```python
@dataclass
class UploadResult:
    id: str              # "org/dataset"
    split: str
    uploaded: int        # 成功上传的 task 数
    skipped: int         # 跳过的 task 数（已存在）
    failed: int          # 失败的 task 数
```

---

## 核心组件

### BaseDatasetRegistry

`rock/sdk/envhub/datasets/registry/base.py` — 抽象基类，定义了 Dataset Registry 的完整接口契约。

接口分为四组：

| 组别 | 方法 | 说明 |
|------|------|------|
| Listing | `list_organizations`, `list_org_datasets`, `list_dataset_splits`, `list_all_datasets`, `list_datasets`, `list_dataset_tasks`, `list_dataset_task_entries` | 各层级的列表查询，均返回 `PageResult` |
| Query | `get_dataset`, `get_task`, `get_task_metadata` | 单个资源的详情查询 |
| File Ops | `list_task_files`, `browse_task_files`, `read_task_file`, `download_task_file`, `download_task` | task 内文件的浏览、读取、下载 |
| Upload | `upload_dataset` | 上传本地数据集到 registry |

### OssDatasetRegistry

`rock/sdk/envhub/datasets/registry/oss.py` — OSS 后端的完整实现。

**关键设计：**

1. **Bucket 缓存** — `_cached_bucket` 避免每次操作重建 `oss2.Bucket` 实例（包含鉴权初始化）。

2. **迭代式列表** — `_iter_objects()` 方法基于 OSS v2 的 `continuation_token` 分页机制，以 generator 形式遍历所有结果，突破单次 1000 条的限制。同时处理 `delimiter` 场景下的 `prefix_list`（子目录）和 `object_list`（文件）。

   ```python
   def _iter_objects(self, prefix: str, *, delimiter: str | None = None) -> Iterator[Any]:
       # 使用 continuation_token 自动翻页
       # delimiter="/" 时同时 yield prefix_list（作为 _PrefixEntry）和 object_list
   ```

3. **Task 提取** — `_extract_tasks_from_split()` 合并目录型 task（来自 prefix_list）和文件型 task（来自 object_list，去后缀），去重后排序返回。

4. **并发扫描** — `list_all_datasets()` 和 `list_datasets()` 在多 organization 场景下使用 `ThreadPoolExecutor` 并发扫描，`max_workers` 上限为 10。

5. **Metadata 发现** — `get_task_metadata()` 按优先级依次尝试 `README.md` → `readme.md` → `metadata.json` → `task.toml`。JSON 格式会自动解析为 `parsed` 字段。全部未命中时生成一份包含文件列表的 Markdown 作为 fallback（`generated=True`）。

6. **目录浏览** — `browse_task_files()` 使用 `delimiter="/"` 实现单层目录浏览，目录排在文件前，自动推测文件的 MIME 类型。

7. **下载** — `download_task()` 使用 `ThreadPoolExecutor` 并发下载 task 下所有文件，默认 4 并发。自动创建本地目录结构。

### DatasetClient

`rock/sdk/envhub/datasets/client.py` — SDK 对外统一入口。

接受 `OssRegistryInfo` 配置，内部构建 `OssDatasetRegistry` 实例。方法签名与 `BaseDatasetRegistry` 一一对应，是纯粹的委托层（delegation），不包含额外业务逻辑。

```python
class DatasetClient:
    def __init__(self, registry: OssRegistryInfo) -> None:
        self._registry = OssDatasetRegistry(registry)
```

典型用法：

```python
from rock.sdk.envhub.datasets import DatasetClient, PageResult

client = DatasetClient(OssRegistryInfo(
    oss_bucket="my-bucket",
    oss_endpoint="oss-cn-hangzhou.aliyuncs.com",
    oss_access_key_id="...",
    oss_access_key_secret="...",
))

# 分页列出所有 dataset
page: PageResult = client.list_all_datasets(offset=0, limit=20)
print(f"Total: {page.total}, showing {len(page.items)}")

# 获取 dataset 概览
info = client.get_dataset("princeton-nlp", "SWE-bench_Verified")
print(f"Splits: {info.splits}, task counts: {info.task_counts}")

# 浏览 task 文件
files = client.list_task_files("princeton-nlp", "SWE-bench_Verified", "test", "django__django-11099")
for f in files.items:
    print(f"  {f.path}  {f.size} bytes")

# 读取文件内容
data = client.read_task_file("princeton-nlp", "SWE-bench_Verified", "test", "django__django-11099", "patch.diff")

# 下载整个 task
client.download_task("princeton-nlp", "SWE-bench_Verified", "test", "django__django-11099", Path("./output"))
```

**预留接口**（未实现，抛出 `NotImplementedError`）：

| 方法 | 说明 |
|------|------|
| `transfer_images(**kwargs)` | 镜像迁移（与 regionless 能力联动） |
| `audit_dataset(**kwargs)` | 数据集完整性审计 |

### FormatParser

`rock/sdk/envhub/datasets/formats/` — 可插拔的 benchmark 格式解析系统。

**抽象基类**（`base.py`）：

```python
class FormatParser(ABC):
    @abstractmethod
    def extract(self, raw: dict) -> dict:
        """提取结构化字段：instance_id, repo, language, difficulty, base_commit, image_uri"""

    @abstractmethod
    def extract_source_files(self, raw: dict) -> list[dict]:
        """提取外部文件引用：[{path, source_uri, sha256, size_bytes}]"""

    def validate(self, raw: dict) -> list[str]:
        """校验原始数据，返回告警列表"""
```

**注册机制**：

```python
register_format("swe", SweFormatParser)      # 注册时指定格式名
parser = get_parser("swe")                     # 运行时按名称获取
result = parser.extract({"instance_id": "django__django-11099", "repo": "django/django", ...})
```

**已注册格式**：

| 格式名 | 解析器类 | 必填字段 | 特殊处理 |
|--------|----------|----------|----------|
| `pinchbench` | `PinchBenchFormatParser` | `instance_id` | `docker_image` → `image_uri`；`patch` → `patch.diff` |
| `swe` | `SweFormatParser` | `instance_id`, `repo`, `base_commit` | `language` 固定 `"python"`；提取 `patch.diff` + `test_patch.diff` |
| `tb2` | `Tb2FormatParser` | `instance_id` | `files` dict → 逐项提取为文件引用 |

---

## SDK 对外接口

### Listing APIs

所有列表 API 支持 `offset`/`limit` 关键字参数，返回 `PageResult[T]`。

```python
# 列出所有 organization
list_organizations(*, offset=0, limit=None) -> PageResult[str]

# 列出某 organization 下的 dataset 名
list_org_datasets(organization, *, offset=0, limit=None) -> PageResult[str]

# 列出某 dataset 下的 split 名
list_dataset_splits(organization, dataset, *, offset=0, limit=None) -> PageResult[str]

# 列出所有 (org, dataset) 对，支持 query 过滤
list_all_datasets(concurrency=10, *, query=None, offset=0, limit=None) -> PageResult[tuple[str, str]]

# 列出所有 DatasetSpec（完整扫描，含 task_ids）
list_datasets(organization=None, *, offset=0, limit=None) -> PageResult[DatasetSpec]

# 列出某 split 下的 task ID
list_dataset_tasks(organization, dataset, split="test", *, query=None, offset=0, limit=None) -> PageResult[str] | None

# 列出某 split 下的 task 条目（含类型、大小等元信息）
list_dataset_task_entries(organization, dataset, split="test", *, query=None, offset=0, limit=None) -> PageResult[TaskEntry] | None
```

### Query APIs

```python
# 获取 dataset 概览（splits 列表 + 每个 split 的 task 数）
get_dataset(organization, dataset) -> DatasetInfo | None

# 获取 task 详情（文件列表 + 总大小）
get_task(organization, dataset, split, task_id) -> TaskInfo | None

# 获取 task metadata（自动发现 README.md / metadata.json / task.toml）
get_task_metadata(organization, dataset, split, task_id) -> TaskMetadata | None
```

### Task File Operations

```python
# 浏览 task 内某一层目录（类似 ls）
browse_task_files(organization, dataset, split, task_id, prefix="", *, offset=0, limit=None) -> PageResult[FileEntry]

# 递归列出 task 下所有文件
list_task_files(organization, dataset, split, task_id, *, offset=0, limit=None) -> PageResult[TaskFileInfo]

# 读取文件内容（返回 bytes）
read_task_file(organization, dataset, split, task_id, file_path) -> bytes

# 下载单个文件到本地
download_task_file(organization, dataset, split, task_id, file_path, local_path) -> Path

# 下载整个 task 到本地目录（并发下载）
download_task(organization, dataset, split, task_id, local_dir, concurrency=4) -> Path
```

### Upload

```python
# 上传本地数据集（目录结构：local_dir/{task_id}/...）
upload_dataset(source: LocalDatasetConfig, target: RegistryDatasetConfig, concurrency=4) -> UploadResult
```

### Format Parsing

```python
from rock.sdk.envhub.datasets.formats import get_parser, register_format, FormatParser

# 获取已注册的解析器
parser = get_parser("swe")
structured = parser.extract(raw_payload)
files = parser.extract_source_files(raw_payload)
warnings = parser.validate(raw_payload)

# 注册自定义解析器
class MyFormatParser(FormatParser):
    def extract(self, raw: dict) -> dict: ...
    def extract_source_files(self, raw: dict) -> list[dict]: ...

register_format("my_format", MyFormatParser)
```

---

## CLI 命令

所有命令通过 `rock datasets <subcommand>` 调用。全局 OSS 参数：`--bucket`、`--endpoint`、`--access-key-id`、`--access-key-secret`、`--region`。

### rock datasets list

列出数据集，支持按层级和 organization 过滤。

```bash
# 列出所有 org + dataset
rock datasets list

# 仅列出 organizations（depth=1）
rock datasets list --depth 1

# 列出某 org 下的 datasets
rock datasets list --org princeton-nlp

# 分页
rock datasets list --offset 10 --limit 20
```

### rock datasets info（新增）

查看 dataset 概览（splits 列表与 task 数量）。

```bash
rock datasets info --org princeton-nlp --dataset SWE-bench_Verified
# 输出：
# Dataset: princeton-nlp/SWE-bench_Verified
# Splits:  2
#
# Split  Tasks
# -----  -----
# test     500
# train   1600
#
# Total: 2100 tasks across 2 splits.
```

### rock datasets tasks

列出某 split 下的 task ID。

```bash
rock datasets tasks --org princeton-nlp --dataset SWE-bench_Verified --split test
rock datasets tasks --org princeton-nlp --dataset SWE-bench_Verified --split test --offset 0 --limit 50
```

### rock datasets splits

列出某 dataset 下的 split。

```bash
rock datasets splits --org princeton-nlp --dataset SWE-bench_Verified
```

### rock datasets files（新增）

列出某 task 下的所有文件。

```bash
rock datasets files --org princeton-nlp --dataset SWE-bench_Verified --split test --task django__django-11099
# 输出：
# Path          Size  Last Modified
# ----------    ----  -------------
# patch.diff    1234  2026-06-01T12:00:00+00:00
# README.md      567  2026-06-01T12:00:00+00:00
#
# 2 total. Total size: 1801 bytes.
```

### rock datasets cat（新增）

输出文件内容到 stdout。

```bash
rock datasets cat --org princeton-nlp --dataset SWE-bench_Verified --split test --task django__django-11099 --file patch.diff
```

### rock datasets download（新增）

下载 task 文件到本地。

```bash
# 下载整个 task
rock datasets download --org princeton-nlp --dataset SWE-bench_Verified --split test --task django__django-11099 --dir ./output

# 下载单个文件
rock datasets download --org princeton-nlp --dataset SWE-bench_Verified --split test --task django__django-11099 --file patch.diff --dir ./output

# 指定并发数
rock datasets download --org ... --dataset ... --task ... --dir ./output --concurrency 8
```

### rock datasets upload

上传本地数据集。

```bash
rock datasets upload --org my-org --dataset my-bench --split test --dir ./local-tasks --concurrency 8 --overwrite
```

---

## 分页机制

### 实现方式

分页采用 **内存分页** 策略：先通过 `_iter_objects()` 流式获取全部 OSS 结果到内存列表，再用 `_paginate()` 切片。

```python
def _paginate(items: list, offset: int = 0, limit: int | None = None) -> PageResult:
    total = len(items)
    end = offset + limit if limit is not None else None
    return PageResult(items=items[offset:end], total=total, offset=offset, limit=limit)
```

### 设计考量

| 方案 | 优点 | 缺点 |
|------|------|------|
| **内存分页**（当前选择） | 实现简单；`total` 精确；支持任意 offset 跳转 | 首次请求需遍历全部 OSS 对象 |
| OSS 原生 continuation token | 无需全量加载 | 只能顺序翻页；无法提供精确 total；跨请求无状态 |
| 服务端缓存 + cursor | 支持大数据集 | 需要额外的状态管理服务 |

当前选择内存分页是因为：数据集 registry 的规模通常在数百到数千级别，单次全量列表的耗时可接受。`_iter_objects()` 已通过 continuation token 解决了 OSS 单页 1000 条的限制，确保全量遍历的正确性。

### query 过滤

`list_all_datasets()` 和 `list_dataset_tasks()` 支持 `query` 参数，执行大小写不敏感的子串匹配，在分页前过滤。

---

## 数据流

### list_all_datasets（全量列表 + 分页）

```
list_all_datasets(query="swe", offset=0, limit=10)
  ├─ list_organizations() → ["org-a", "org-b", ...]
  ├─ ThreadPoolExecutor(max_workers=10)
  │     └─ for org in orgs: list_org_datasets(org) → [(org, ds), ...]
  ├─ pairs.sort()
  ├─ query 过滤: [p for p in pairs if "swe" in f"{p[0]}/{p[1]}".lower()]
  └─ _paginate(filtered, offset=0, limit=10) → PageResult
```

### get_task_metadata（自动发现）

```
get_task_metadata(org, dataset, split, task_id)
  ├─ for (filename, fmt) in [("README.md","markdown"), ("readme.md","markdown"),
  │                           ("metadata.json","json"), ("task.toml","toml")]:
  │     ├─ read_task_file(org, dataset, split, task_id, filename)
  │     │     └─ 成功 → 解析并返回 TaskMetadata(source=filename, format=fmt, ...)
  │     └─ NoSuchKey → continue
  └─ 全部未命中 → list_task_files() → 生成文件列表 Markdown → TaskMetadata(generated=True)
```

### download_task（并发下载）

```
download_task(org, dataset, split, task_id, local_dir, concurrency=4)
  ├─ list_task_files() → files: list[TaskFileInfo]
  ├─ mkdir local_dir/{task_id}/
  └─ ThreadPoolExecutor(max_workers=concurrency)
        └─ for fi in files: download_task_file(org, dataset, split, task_id, fi.path, local_path)
              └─ bucket.get_object_to_file(key, local_path)
```

---

## 错误处理

| 场景 | 行为 |
|------|------|
| organization/dataset/split 不存在 | 列表 API 返回空 `PageResult`；query API 返回 `None` |
| task 不存在 | `get_task()` 返回 `None`；`read_task_file()` 抛出 `oss2.exceptions.NoSuchKey` |
| 文件不存在 | `read_task_file()` / `download_task_file()` 抛出 `oss2.exceptions.NoSuchKey` |
| OSS 认证失败 | `oss2.exceptions.ServerError` 冒泡到调用方 |
| 并发下载部分失败 | `download_task()` 记录 `logger.error` 后 re-raise，中断整个下载 |
| 上传部分失败 | `upload_dataset()` 记录 `logger.error`，继续处理其余 task，最终 `UploadResult.failed > 0` |
| `get_task_metadata()` 所有候选文件不存在 | 生成 fallback Markdown（`generated=True`），不抛异常 |
| CLI 命令结果为空 | 打印友好提示（如 "No tasks found for ..."），不抛异常 |

---

## 性能优化

### 1. Bucket 实例缓存

```python
def _build_bucket(self) -> oss2.Bucket:
    if self._cached_bucket is None:
        auth = oss2.Auth(...)
        self._cached_bucket = oss2.Bucket(auth, endpoint, bucket)
    return self._cached_bucket
```

避免每次操作重复构建 `oss2.Auth` + `oss2.Bucket`，在批量操作（如 `list_all_datasets` 扫描数十个 org）时显著减少开销。

### 2. 迭代式列表（突破 1000 条限制）

```python
def _iter_objects(self, prefix, *, delimiter=None) -> Iterator:
    marker = ""
    while True:
        result = bucket.list_objects_v2(prefix=prefix, continuation_token=marker, max_keys=1000)
        # yield entries...
        if result.is_truncated:
            marker = result.next_continuation_token
        else:
            break
```

### 3. 并发扫描与下载

- `list_all_datasets()` — 最多 10 并发扫描各 organization 的 dataset 列表
- `list_datasets()` — 多 org 时并发扫描
- `download_task()` — 可配置 1-16 并发下载文件

---

## 测试策略

### 测试文件

```
tests/unit/datasets/
├── test_client.py              # DatasetClient 委托层测试
├── test_oss_registry.py        # OssDatasetRegistry 全方法测试
├── test_datasets_command.py    # CLI 命令测试
└── test_formats.py             # FormatParser 注册 + 解析测试
```

### 测试手法

**OSS Registry 测试**（`test_oss_registry.py`）— mock `oss2.Bucket`，验证 prefix 构建、分页、迭代、并发逻辑：

```python
@pytest.fixture()
def registry():
    info = OssRegistryInfo(oss_bucket="test-bucket", oss_endpoint="oss-cn-hangzhou.aliyuncs.com", ...)
    return OssDatasetRegistry(info)

def test_list_organizations(registry, mocker):
    mock_bucket = mocker.MagicMock()
    mocker.patch.object(registry, "_build_bucket", return_value=mock_bucket)
    mock_bucket.list_objects_v2.return_value = MockResult(prefix_list=["datasets/org-a/", "datasets/org-b/"])
    page = registry.list_organizations()
    assert page.items == ["org-a", "org-b"]
    assert page.total == 2
```

**Client 测试**（`test_client.py`）— mock 内部 `_registry`，验证参数透传：

```python
def test_list_datasets_delegates(client, mock_registry):
    mock_registry.list_datasets.return_value = PageResult(items=[], total=0, offset=0, limit=None)
    result = client.list_datasets("my-org", offset=5, limit=10)
    mock_registry.list_datasets.assert_called_once_with("my-org", offset=5, limit=10)
```

**CLI 测试**（`test_datasets_command.py`）— mock `DatasetClient`，验证命令行参数解析与输出格式。

**Format 测试**（`test_formats.py`）— 直接构造 raw dict，验证 `extract` / `extract_source_files` / `validate` 的输出：

```python
def test_swe_extract():
    parser = get_parser("swe")
    result = parser.extract({"instance_id": "foo", "repo": "bar/baz", "base_commit": "abc123"})
    assert result["instance_id"] == "foo"
    assert result["language"] == "python"
```
