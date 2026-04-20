# EnvHub & Dataset 开发设计文档

## 目录

- [EnvHub（现有）](#envhub现有)
- [Dataset 功能设计](#dataset-功能设计)
  - [背景与目标](#背景与目标)
  - [OSS 路径约定](#oss-路径约定)
  - [模块结构](#模块结构)
  - [核心模型](#核心模型)
  - [Registry 抽象层](#registry-抽象层)
  - [OssDatasetRegistry](#ossdatasetregistry)
  - [DatasetClient](#datasetclient)
  - [CLI 命令](#cli-命令)
  - [配置文件扩展](#配置文件扩展)
  - [数据流](#数据流)
  - [错误处理](#错误处理)
  - [测试策略](#测试策略)

---

## EnvHub（现有）

EnvHub 是 ROCK 的环境管理服务，提供 Docker 环境的注册、查询、列举和删除功能。

| 入口           | 模块                        | 说明                         |
|----------------|-----------------------------|------------------------------|
| `envhub` 服务  | `rock.envhub.server`        | FastAPI 服务，端口 8081       |
| SDK Client     | `rock.sdk.envhub.client`    | `EnvHubClient`，HTTP 调用服务 |

REST 端点：`POST /env/register`、`POST /env/get`、`POST /env/list`、`POST /env/delete`、`GET /health`

---

## Dataset 功能设计

### 背景与目标

在 ROCK 中引入 dataset 管理能力，核心目标如下：

**1. 约束 datasets**

统一 dataset 的路径约定、命名规范和存储格式，避免各服务自行散落地写 OSS 路径。所有 dataset 必须遵循 `datasets/{organization}/{dataset_name}/{split}/{task_id}/` 结构，由本模块作为唯一入口强制执行。

**2. 提供 SDK 和 CLI 供其他服务集成**

- **SDK**（`rock.sdk.envhub.datasets`）：提供 `DatasetClient`，供 Python 代码直接调用 list / upload，适合 admin、job 等服务在流程中集成 dataset 操作。
- **CLI**（`rock datasets`）：提供 `list` 和 `upload` 子命令，供运维、研究人员在终端操作 dataset，也适合脚本化批量处理。

**3. 为后续权限管理预留扩展点**

当前阶段 CLI 直接对接 OSS，不经过 envhub server。后续可在 envhub server 增加 `/datasets/*` 端点，在 SDK/CLI 与 OSS 之间插入权限校验、审计日志等能力，Registry 抽象层的设计为此预留了扩展空间。

---

### OSS 路径约定

```
oss://{bucket}/datasets/{organization}/{dataset_name}/{split}/{task_id}/
```

示例：

```
oss://my-bucket/
└── datasets/
    └── qwen/
        └── my-bench/
            └── train/
                ├── task-001/
                │   ├── task.toml
                │   └── tests/
                └── task-002/
                    ├── task.toml
                    └── tests/
```

- `organization`：数据集所属组织，如 `qwen`、`alibaba`
- `dataset_name`：数据集名称
- `split`：分片标识，语义等同于版本，如 `train`、`test`、`v1.0`
- `task_id`：单个 task 目录名，目录内文件原样保留（相对路径不变）

---

### 模块结构

```
rock/
├── sdk/
│   └── envhub/
│       ├── client.py          # 现有：EnvHubClient
│       ├── config.py          # 现有
│       ├── schema.py          # 现有
│       └── datasets/          # 新增
│           ├── __init__.py    # 对外入口：暴露 DatasetClient、DatasetSpec 等
│           ├── models.py      # DatasetSpec, OssRegistryConfig, UploadResult
│           ├── client.py      # DatasetClient（对外统一入口）
│           └── registry/
│               ├── __init__.py
│               ├── base.py    # BaseDatasetRegistry ABC
│               └── oss.py     # OssDatasetRegistry
└── cli/
    └── command/
        └── datasets.py        # DatasetsCommand（继承 Command ABC）
```

---

### 核心模型

**`rock/sdk/envhub/datasets/models.py`**

```python
@dataclass
class OssRegistryConfig:
    bucket: str
    endpoint: str | None = None          # 如 https://oss-cn-hangzhou.aliyuncs.com
    region: str | None = None
    access_key_id: str | None = None
    access_key_secret: str | None = None
    base_path: str = "datasets"          # OSS 根前缀，固定为 datasets

@dataclass
class DatasetSpec:
    organization: str
    name: str
    split: str
    task_ids: list[str]

    @property
    def full_name(self) -> str:
        return f"{self.organization}/{self.name}"

@dataclass
class UploadResult:
    organization: str
    name: str
    split: str
    uploaded: int        # 成功上传的文件数
    skipped: int         # 已存在跳过的 task 数（overwrite=False）
    failed: int          # 失败数
```

---

### Registry 抽象层

**`rock/sdk/envhub/datasets/registry/base.py`**

```python
class BaseDatasetRegistry(ABC):

    @abstractmethod
    def list_datasets(self, organization: str | None = None) -> list[DatasetSpec]:
        """枚举 registry 中的所有 datasets。
        organization 不为 None 时只返回该 org 下的 datasets。
        """
        ...

    @abstractmethod
    def upload_dataset(
        self,
        organization: str,
        name: str,
        split: str,
        local_dir: Path,
        concurrency: int = 4,
        overwrite: bool = False,
    ) -> UploadResult:
        """将 local_dir/{task_id}/ 批量上传到 registry。"""
        ...
```

---

### OssDatasetRegistry

**`rock/sdk/envhub/datasets/registry/oss.py`**

```python
class OssDatasetRegistry(BaseDatasetRegistry):
    def __init__(self, config: OssRegistryConfig): ...
```

**路径构建：**

```python
def _build_prefix(self, org: str, name: str, split: str | None = None) -> str:
    parts = [self._config.base_path, org, name]
    if split:
        parts.append(split)
    return "/".join(parts)
# → "datasets/qwen/my-bench/train"
```

**list_datasets 逻辑：**

1. 以 `datasets/` 为前缀列出三级目录（org → name → split）
2. 对每个 `datasets/{org}/{name}/{split}/`，列出直接子目录作为 `task_ids`
3. 返回 `list[DatasetSpec]`

OSS 列举使用 `list_objects_v2` with `delimiter="/"` 逐层枚举目录，避免全量遍历。

**upload_dataset 逻辑：**

1. 遍历 `local_dir` 下的一级子目录，每个子目录视为一个 task（`task_id = subdir.name`）
2. 若 `overwrite=False` 且 OSS 上已存在该 task 目录，跳过
3. 并发上传（`asyncio` + `ThreadPoolExecutor`，`concurrency` 控制并发数）
4. 目标 key：`datasets/{org}/{name}/{split}/{task_id}/{relative_file_path}`
5. 返回 `UploadResult`

---

### DatasetClient

**`rock/sdk/envhub/datasets/client.py`**

薄封装层，负责从配置创建 registry 并提供面向业务的方法。

```python
class DatasetClient:
    def __init__(self, config: OssRegistryConfig):
        self._registry = OssDatasetRegistry(config)

    def list_datasets(self, organization: str | None = None) -> list[DatasetSpec]:
        return self._registry.list_datasets(organization)

    def upload_dataset(
        self,
        organization: str,
        name: str,
        split: str,
        local_dir: Path,
        concurrency: int = 4,
        overwrite: bool = False,
    ) -> UploadResult:
        return self._registry.upload_dataset(
            organization, name, split, local_dir, concurrency, overwrite
        )
```

---

### CLI 命令

**`rock/cli/command/datasets.py`**，继承 `Command` ABC，`name = "datasets"`。

#### rock datasets list

```
rock datasets list [OPTIONS]

Options:
  --org TEXT                 只列出指定 organization 的 datasets
  --bucket TEXT              OSS bucket 名称（覆盖 config.ini）
  --endpoint TEXT            OSS endpoint（覆盖 config.ini）
  --access-key-id TEXT       OSS access key ID（覆盖 config.ini）
  --access-key-secret TEXT   OSS access key secret（覆盖 config.ini）
```

输出示例：

```
Organization  Dataset    Split   Tasks
qwen          my-bench   train   42
qwen          my-bench   test    10
alibaba       code-eval  train   100
```

#### rock datasets upload

```
rock datasets upload [OPTIONS]

Required:
  --org TEXT       Organization 名称
  --dataset TEXT   Dataset 名称
  --split TEXT     Split 名称（如 train、test、v1.0）
  --dir PATH       本地 task 目录（内含 {task_id}/ 子目录）

Options:
  --bucket TEXT            OSS bucket（覆盖 config.ini）
  --endpoint TEXT          OSS endpoint（覆盖 config.ini）
  --access-key-id TEXT     OSS access key ID（覆盖 config.ini）
  --access-key-secret TEXT OSS access key secret（覆盖 config.ini）
  --concurrency INT        并发上传数（默认 4，范围 1-16）
  --overwrite              覆盖 OSS 上已存在的 task 目录（默认跳过）
```

输出示例：

```
Uploading to oss://my-bucket/datasets/qwen/my-bench/train/
  ✓ task-001  (5 files)
  ✓ task-002  (5 files)
  - task-003  skipped (already exists)

Done: 2 uploaded, 1 skipped, 0 failed
```

---

### 配置文件扩展

在 `.rock/config.ini` 新增 `[dataset]` section，用于存储 OSS 凭证默认值：

```ini
[rock]
base_url = http://localhost:8080

[dataset]
oss_bucket = my-bucket
oss_endpoint = https://oss-cn-hangzhou.aliyuncs.com
oss_access_key_id = LTAI5t...
oss_access_key_secret = xxxxxxx
```

**优先级（高→低）**：CLI 参数 > `config.ini [dataset]` section > 报错（必填项缺失）

`ConfigManager` 扩展：新增 `get_dataset_config() -> OssRegistryConfig | None`，读取 `[dataset]` section。`DatasetCommand` 在初始化时合并 config + CLI args 构建 `OssRegistryConfig`。

---

### 数据流

**list:**

```
rock datasets list --org qwen
  └─ DatasetCommand.list()
      ├─ ConfigManager.get_dataset_config()   # 读 config.ini [dataset]
      ├─ 合并 CLI 参数 → OssRegistryConfig
      ├─ DatasetClient(config)
      └─ OssDatasetRegistry.list_datasets(org="qwen")
          └─ alibabacloud_oss_v2: list_objects_v2(prefix="datasets/qwen/", delimiter="/")
              → 枚举 name/split 层
              → 构建 DatasetSpec 列表
              → 打印表格
```

**upload:**

```
rock datasets upload --org qwen --dataset my-bench --split train --dir ./tasks/
  └─ DatasetCommand.upload()
      ├─ ConfigManager.get_dataset_config()
      ├─ 合并 CLI 参数 → OssRegistryConfig
      ├─ DatasetClient(config)
      └─ OssDatasetRegistry.upload_dataset(org, name, split, local_dir, concurrency, overwrite)
          ├─ 遍历 ./tasks/ 下子目录：task-001/, task-002/, ...
          ├─ ThreadPoolExecutor(max_workers=concurrency)
          └─ 每个 task：
              ├─ 若 overwrite=False 且 OSS 存在 → skip
              └─ 遍历文件 → PutObject(key="datasets/qwen/my-bench/train/task-001/{file}")
```

---

### 错误处理

| 场景 | 行为 |
|------|------|
| OSS 凭证缺失 | 启动时立即报错，提示配置 `[dataset]` section 或传 CLI 参数 |
| OSS 权限错误（401/403） | 立即抛出，打印明确错误信息，不重试 |
| OSS 网络错误（5xx/timeout） | 指数退避重试（最多 3 次），超限后报错 |
| `--dir` 不存在或为空 | 命令入口检查，立即报错 |
| 单个 task 上传失败 | 记录到 `UploadResult.failed`，继续上传其他 tasks，命令结束后汇总报告 |
| `--org`/`--dataset`/`--split` 缺失 | argparse required 校验，自动报错 |

---

### 测试策略

| 测试类型 | 覆盖范围 | 标记 |
|----------|----------|------|
| 单元测试 | `OssDatasetRegistry` 路径构建、`DatasetSpec` 模型、`ConfigManager` 解析 `[dataset]` | 无特殊标记 |
| 集成测试（mock OSS） | `list_datasets`、`upload_dataset` 逻辑，使用 `unittest.mock` mock OSS SDK | `@pytest.mark.integration` |
| 集成测试（真实 OSS） | 端到端 upload → list 验证 | `@pytest.mark.need_admin`（需要 OSS 凭证） |

测试文件位置：

```
tests/
├── unit/
│   └── datasets/
│       ├── test_models.py
│       ├── test_oss_registry.py
│       └── test_config.py
└── integration/
    └── datasets/
        └── test_oss_e2e.py
```
