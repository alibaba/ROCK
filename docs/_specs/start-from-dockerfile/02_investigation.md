# Start from Dockerfile — 调研：各 Sandbox 平台如何支持从 Dockerfile 启动

## 概述

调研 Daytona、E2B、Modal、Runloop、GKE、Docker 六个 Sandbox 平台如何实现从 Dockerfile 启动沙箱，为 Rock 的实现提供参考。

---

## 各平台接口定义

### Daytona

Daytona 暴露给用户的核心类型有两个：`Image`（客户端构建定义）和 `Snapshot`（服务端持久快照），二者位于不同抽象层。

#### `Image` — 客户端声明对象

`Image` 是 Pydantic BaseModel，**不直接构造**，通过静态工厂方法创建。它仅描述"如何构建"，不持有任何服务端 ID，本身**从不在 Daytona 服务端存在**。

```python
class Image(BaseModel):
    """不直接构造，通过 from_dockerfile / base / debian_slim 等工厂方法创建。"""
    _dockerfile: str = PrivateAttr(default="")          # 生成或读取的 Dockerfile 内容
    _context_list: list[Context] = PrivateAttr(default_factory=list)  # COPY 依赖的本地上下文文件

    @staticmethod
    def from_dockerfile(path: str | Path) -> "Image":
        """读取 Dockerfile，自动提取 COPY 指令依赖的上下文文件。"""
    @staticmethod
    def base(image: str) -> "Image":
        """从已有镜像 tag 构造，等价于 `FROM {image}`。"""
    @staticmethod
    def debian_slim(python_version) -> "Image": ...

    # 链式调用追加 Dockerfile 指令
    def pip_install(self, *packages) -> "Image": ...
    def run_commands(self, *commands) -> "Image": ...
    def add_local_file(self, local_path, remote_path) -> "Image": ...
    def env(self, vars: dict) -> "Image": ...
```

#### `Snapshot` — 服务端持久对象

`Snapshot` 继承自 OpenAPI 生成的 `SnapshotDto`，是 **Daytona 服务端的预配置沙箱快照**，在服务端**永久存在直到手动删除**。

```python
class Snapshot(SnapshotDto):
    id: str
    name: str
    image_name: str
    state: SnapshotState   # PENDING / BUILDING / ACTIVE / ERROR / BUILD_FAILED
    size: float | None
    cpu: int; gpu: int; mem: int; disk: int   # GiB
    entrypoint: list[str] | None
    created_at: str; updated_at: str; last_used_at: str

class CreateSnapshotParams(BaseModel):
    name: str
    image: str | Image                  # str=已有镜像名，Image=声明式构建
    resources: Resources | None = None
    entrypoint: list[str] | None = None
    region_id: str | None = None

class AsyncSnapshotService:
    async def list() -> PaginatedSnapshots
    async def get(name: str) -> Snapshot
    async def create(params: CreateSnapshotParams, *, on_logs=None, timeout=0) -> Snapshot
    async def delete(snapshot: Snapshot) -> None
    async def activate(snapshot: Snapshot) -> Snapshot
```

#### Image 与 Snapshot 的关系

`Image` 是**输入**（构建定义），`Snapshot` 是**输出**（命名持久快照）。一个 Image 可以传入 `snapshot.create()` 产出一个 Snapshot；也可以直接传入 `daytona.create()` 触发一次性构建（不产出命名 Snapshot）。

```
Image (客户端声明)
    ├─→ snapshot.create(CreateSnapshotParams(name=..., image=Image)) ─→ 命名 Snapshot (服务端永久持有)
    │                                                                       │
    │                                                                       ▼
    │                                                   daytona.create(CreateSandboxFromSnapshotParams(snapshot=name))
    │
    └─→ daytona.create(CreateSandboxFromImageParams(image=Image)) ─→ 内部临时构建（24h 隐式缓存，无命名 Snapshot）
```

#### 启动接口

```python
class CreateSandboxFromImageParams(BaseModel):
    image: str | Image                          # 必填，str 或 Image 声明
    resources: Resources | None = None
    env_vars: dict[str, str] | None = None
    auto_stop_interval: int | None = None       # 分钟
    auto_delete_interval: int | None = None
    network_block_all: bool | None = None
    # ... 其他可选字段

class CreateSandboxFromSnapshotParams(BaseModel):
    snapshot: str                               # 已存在的 Snapshot 名称
    auto_stop_interval: int | None = None
    auto_delete_interval: int | None = None
    network_block_all: bool | None = None
    # ... 其他可选字段（不含 image / resources，资源由 Snapshot 决定）

class AsyncDaytona:
    async def create(
        self,
        params: CreateSandboxFromImageParams | CreateSandboxFromSnapshotParams | None = None,
        *,
        timeout: float = 60,
        on_snapshot_create_logs: Callable[[str], None] | None = None,
    ) -> AsyncSandbox: ...
```

#### 关键观察：两条路径在服务端是同一构建流程

从 SDK 源码 (`daytona/_async/daytona.py` 第 474-489 行) 可见，即使用户传 `CreateSandboxFromImageParams`，SDK 也会把 `Image` 序列化为 `CreateBuildInfo(dockerfile_content=..., context_hashes=...)` 发给服务端，服务端的处理流程（`PENDING_BUILD` 状态、流式 build_logs）与 `snapshot.create()` 完全相同。

```python
# AsyncDaytona._create() 内部
if isinstance(params, CreateSandboxFromImageParams) and params.image:
    if isinstance(params.image, str):
        sandbox_data.build_info = CreateBuildInfo(
            dockerfile_content=Image.base(params.image).dockerfile(),
        )
    else:
        context_hashes = await AsyncSnapshotService.process_image_context(...)
        sandbox_data.build_info = CreateBuildInfo(
            context_hashes=context_hashes,
            dockerfile_content=params.image.dockerfile(),
        )
```

两条路径的差异仅在产物归属与生命周期：

| 路径 | 调用 | 产物 | 生命周期 |
|------|------|------|---------|
| Image → 一次性构建 | `daytona.create(CreateSandboxFromImageParams(image=Image))` | 匿名构建产物 | 平台侧 24 小时隐式缓存，过期自动清理 |
| Image → 命名 Snapshot | `daytona.snapshot.create(CreateSnapshotParams(name=..., image=Image))` 然后 `daytona.create(CreateSandboxFromSnapshotParams(snapshot=name))` | 命名 Snapshot | 永久持有，需 `snapshot.delete()` 显式清理 |

#### Harbor 的实际使用模式

Harbor 在 [harbor/src/harbor/environments/daytona.py](file:///root/harbor/src/harbor/environments/daytona.py) 第 165-217 行采取**外部预置 Snapshot + 客户端动态构建**的混合策略，**不在客户端代码内调用 `snapshot.create()`**：

```python
# 1. 检查外部预置的命名 Snapshot 是否已 ACTIVE
snapshot_name = snapshot_template_name.format(name=environment_name)
try:
    snapshot = await daytona.snapshot.get(snapshot_name)
    snapshot_exists = (snapshot.state == SnapshotState.ACTIVE)
except Exception:
    snapshot_exists = False

if snapshot_exists:
    # 热路径：复用命名 Snapshot
    params = CreateSandboxFromSnapshotParams(snapshot=snapshot_name, ...)
elif force_build or not docker_image:
    # 冷路径：从 Dockerfile 一次性构建（仅 24h 隐式缓存）
    image = Image.from_dockerfile(dockerfile_path)
    params = CreateSandboxFromImageParams(image=image, ...)
else:
    # 备用路径：直接用 prebuilt image tag
    image = Image.base(docker_image)
    params = CreateSandboxFromImageParams(image=image, ...)

await daytona.create(params=params)
```

命名 Snapshot 的生命周期完全由运维通过 Daytona Dashboard / CLI 管理。Harbor 客户端代码只负责"先查 Snapshot，命中就走快路径，否则走 Image 一次性构建"。

---

### E2B

**核心类型：**

```python
class TemplateBase:
    def from_dockerfile(self, dockerfile_content_or_path: str) -> TemplateBuilder: ...
    def from_image(self, image: str, username: str | None = None, password: str | None = None) -> TemplateBuilder: ...
```

`from_dockerfile()` 返回 `TemplateBuilder`，支持链式调用追加指令：

```python
class TemplateBuilder:
    def run_cmd(self, command: str | list[str]) -> TemplateBuilder: ...
    def copy(self, src, dest) -> TemplateBuilder: ...
    def set_envs(self, envs: dict[str, str]) -> TemplateBuilder: ...
    def apt_install(self, packages) -> TemplateBuilder: ...
    def pip_install(self, packages) -> TemplateBuilder: ...
    # ... 其他 builder 方法
```

**构建接口：**

```python
class AsyncTemplate(TemplateBase):
    @staticmethod
    async def build(
        template: TemplateBuilder,
        name: str | None = None,
        *,
        alias: str | None = None,
        cpu_count: int = 2,
        memory_mb: int = 1024,
        skip_cache: bool = False,
    ) -> BuildInfo: ...

    @staticmethod
    async def alias_exists(alias: str) -> bool: ...
```

**启动接口：**

```python
class AsyncSandbox:
    @classmethod
    async def create(
        cls,
        template: str | None = None,    # template name 或 ID
        timeout: int | None = None,
        envs: dict[str, str] | None = None,
        allow_internet_access: bool = True,
    ) -> Self: ...
```

- 两步模型：先 `build()` Template，再从 Template `create()` Sandbox
- Template 按 alias 缓存，内容哈希作为 alias 一部分

---

### Modal

**核心类型：**

```python
class Image(_Object):
    """不直接构造，通过静态工厂方法创建。"""

    @staticmethod
    def from_dockerfile(
        path: str | Path,
        *,
        force_build: bool = False,
        context_dir: Path | str | None = None,
        build_args: dict[str, str] = {},
        secrets: Collection[Secret] | None = None,
        gpu: GPU_T = None,
        add_python: str | None = None,
    ) -> "Image": ...

    @staticmethod
    def from_registry(
        tag: str,
        secret: Secret | None = None,
        *,
        force_build: bool = False,
        add_python: str | None = None,
    ) -> "Image": ...
```

**启动接口：**

```python
class Sandbox(_Object):
    @staticmethod
    async def create(
        *args: str,
        app: App | None = None,
        image: Image | None = None,
        cpu: float | tuple[float, float] | None = None,
        memory: int | tuple[int, int] | None = None,    # MiB
        gpu: GPU_T = None,
        timeout: int = 300,
        block_network: bool = False,
        volumes: dict[str | PathLike, Volume | CloudBucketMount] = {},
        env: dict[str, str | None] | None = None,
    ) -> "Sandbox": ...
```

- `Image` 是惰性声明，实际构建在 `Sandbox.create()` 时由平台触发
- 平台内部按内容哈希缓存

---

### Runloop

**核心类型：**

```python
class BlueprintCreateParams(TypedDict, total=False):
    name: Required[str]
    dockerfile: str | None                      # Dockerfile 内容（原始文本）
    build_context: BuildContext | None           # 构建上下文
    build_args: dict[str, str] | None
    launch_parameters: LaunchParameters | None
    # ... 其他可选字段

class BuildContext(TypedDict, total=False):
    object_id: Required[str]                    # storage object ID
    type: Required[Literal["object"]]

class LaunchParameters(BaseModel):
    architecture: Literal["x86_64", "arm64"] | None = None
    custom_cpu_cores: int | None = None
    custom_gb_memory: int | None = None         # GiB
    custom_disk_size: int | None = None         # GiB
    keep_alive_time_seconds: int | None = None
    # ... 其他字段

class BlueprintView(BaseModel):
    id: str
    name: str
    status: Literal["queued", "provisioning", "building", "failed", "build_complete"]
    # ... 其他字段
```

**构建接口：**

```python
class AsyncRunloopSDK:
    storage_object: AsyncStorageObjectOps
    blueprint: AsyncBlueprintOps
    devbox: AsyncDevboxOps

# 上传构建上下文
storage_object = await sdk.storage_object.upload_from_dir(
    dir_path: Path, name: str, ttl: timedelta,
) -> StorageObject

# 创建 Blueprint
blueprint = await sdk.blueprint.create(
    name: str, dockerfile: str, build_context: BuildContext, ...
) -> AsyncBlueprint
```

**启动接口：**

```python
devbox = await sdk.devbox.create_from_blueprint_id(
    blueprint_id: str, name: str | None = None, ...
) -> AsyncDevbox
```

- 三步模型：上传上下文 → 创建 Blueprint → 从 Blueprint 创建 Devbox
- Blueprint 按名称缓存

---

### GKE

无平台 SDK，通过 `gcloud` CLI 和 Kubernetes Python SDK 组合实现。

**构建：**

```bash
gcloud builds submit \
    --tag <registry>/<env_name>:latest \
    --timeout 2400 \
    --machine-type E2_HIGHCPU_8 \
    <environment_dir>
```

**镜像检查：**

```bash
gcloud artifacts docker images describe <image_url>
```

**启动：**

```python
from kubernetes import client as k8s_client

core_api = k8s_client.CoreV1Api()
core_api.create_namespaced_pod(namespace=..., body=pod)
# pod spec 中引用 Cloud Build 产出的镜像
```

- 构建和启动分离：Cloud Build 产出镜像 → Kubernetes 从镜像创建 Pod
- 按 `{environment_name}:latest` 检查 Artifact Registry 中镜像是否存在

---

### Docker

无平台 SDK，直接通过 `docker compose` CLI 操作。

```bash
# 构建
docker compose -f base.yaml -f build.yaml build

# 启动
docker compose ... up --detach --wait
```

- 构建和启动由 compose 统一管理
- 依赖本地 Docker daemon，Docker layer cache 天然缓存

---

## 缓存机制

### Daytona — 双层缓存：命名 Snapshot（显式）+ 平台 24h 隐式缓存

Daytona 的缓存有两层：

**第一层：调用方显式管理的命名 Snapshot**（热缓存）

调用方按命名约定（如 `harbor__{name}__snapshot`）查找预创建的 Snapshot，命中即走快路径：

```python
snapshot_name = snapshot_template_name.format(name=environment_name)

# 检查 Snapshot 是否存在且可用
snapshot = await daytona.snapshot.get(snapshot_name)   # REST GET，不存在则抛异常
if snapshot.state == SnapshotState.ACTIVE:
    # 从 Snapshot 启动，跳过构建
    params = CreateSandboxFromSnapshotParams(snapshot=snapshot_name, ...)
```

- 缓存 key：调用方约定的 Snapshot 名称
- 内容变更检测：无，Snapshot 必须由运维（Dashboard/CLI/`snapshot.create()`）外部预创建和更新
- `force_build` 无法绕过 Snapshot（如果存在则始终使用）

**第二层：Image 路径下平台侧 24 小时隐式缓存**（温缓存）

当 Snapshot 不存在或 `force_build=True`，调用方走 `CreateSandboxFromImageParams(image=Image.from_dockerfile(...))`。SDK 把 Image 转为 `CreateBuildInfo(dockerfile_content, context_hashes)` 发给服务端，服务端按内容哈希自动缓存构建产物 24 小时（过期清理）。

- 缓存 key：服务端按 `dockerfile_content` + `context_hashes` 计算
- 内容变更检测：自动，但只在 24h 窗口内有效
- 不产生命名 Snapshot，即不会进入第一层缓存

### E2B — Template 内容哈希

缓存基于 `environment_dir` 目录内容的 SHA-256 哈希，嵌入 Template alias。

```python
# alias 格式：<environment_name>__<sha256[:8]>
template_name = f"{environment_name}__{dirhash(environment_dir, 'sha256')[:8]}".replace(".", "-")

# 检查 Template 是否已存在
exists = await AsyncTemplate.alias_exists(template_name)   # REST GET /templates/aliases/{alias}

if not force_build and exists:
    pass   # 跳过构建，直接用已有 Template 启动
else:
    await AsyncTemplate.build(template=..., alias=template_name, ...)
```

- 缓存 key：`environment_name` + 目录内容哈希
- 内容变更检测：自动，任何文件变化产生新哈希 → 新 alias → 触发重建
- 旧 Template 不会自动清理

### Modal — 平台侧隐式缓存

调用方无需管理缓存。`Image` 对象在 `Sandbox.create()` 时发送给 Modal 服务端，服务端根据完整的镜像定义（Dockerfile 内容、上下文文件、构建参数等）计算缓存 key。

```python
# 调用方代码中无任何缓存逻辑
image = Image.from_dockerfile(path, context_dir=environment_dir)
sandbox = await Sandbox.create(image=image, ...)

# SDK 内部：将完整镜像定义序列化为 protobuf，发送 ImageGetOrCreate 请求
# 服务端判断是否命中缓存，命中则直接返回已有镜像
req = api_pb2.ImageGetOrCreateRequest(image=image_definition, force_build=force_build, ...)
resp = await client.stub.ImageGetOrCreate(req)
```

- 缓存 key：服务端根据镜像定义 protobuf 计算（包含 Dockerfile 内容、上下文文件哈希）
- 内容变更检测：自动，服务端按内容哈希判断
- `force_build` 通过 `Image.from_dockerfile(force_build=True)` 传递

### Runloop — Blueprint 名称查找

缓存基于 Blueprint 名称查找，无内容哈希。

```python
blueprint_name = f"harbor_{environment_name}_blueprint"

# 查找已有 Blueprint：查私有 + 公有列表，取最新的 build_complete 状态
private_page = await client.api.blueprints.list(name=blueprint_name)
public_page  = await client.api.blueprints.list_public(name=blueprint_name)
candidates = [bp for bp in all_blueprints if bp.name == blueprint_name and bp.status == "build_complete"]
candidates.sort(key=lambda bp: bp.create_time_ms, reverse=True)
blueprint_id = candidates[0].id if candidates else None

if not force_build and blueprint_id:
    pass   # 复用已有 Blueprint
else:
    blueprint_id = await client.blueprint.create(name=blueprint_name, dockerfile=..., ...)
```

- 缓存 key：`harbor_{environment_name}_blueprint`（仅名称）
- 内容变更检测：无，`environment_dir` 内容变化但名称不变时，静默复用旧 Blueprint
- 同名 Blueprint 可共存多个，取最新的 `build_complete`

### GKE — Registry 镜像检查

缓存基于 Artifact Registry 中镜像是否存在。

```python
image_url = f"{registry_location}-docker.pkg.dev/{project_id}/{registry_name}/{environment_name}:latest"

# 检查镜像是否存在
check_cmd = ["gcloud", "artifacts", "docker", "images", "describe", image_url, "--project", project_id]
result = await asyncio.create_subprocess_exec(*check_cmd, stdout=DEVNULL, stderr=DEVNULL)
exists = (result.returncode == 0)

if not force_build and exists:
    pass   # 使用已有镜像
else:
    await _build_and_push_image()   # gcloud builds submit，覆盖 :latest
```

- 缓存 key：`{environment_name}:latest`（固定 tag）
- 内容变更检测：无，`environment_dir` 内容变化但名称不变时，静默复用旧镜像
- `force_build=True` 重新构建并覆盖 `:latest`

### Docker — Layer Cache + 进程内锁

缓存依赖 Docker daemon 自身的 layer cache，进程内通过 `asyncio.Lock` 去重并发构建。

```python
# 类级别锁字典
_image_build_locks: dict[str, asyncio.Lock] = {}

# 构建时按 environment_name 加锁
lock = _image_build_locks.setdefault(environment_name, asyncio.Lock())
async with lock:
    await docker_compose(["build"])   # Docker layer cache 处理增量构建
```

- 缓存 key：Docker layer cache（按 Dockerfile 指令 + 文件内容）
- 内容变更检测：自动，Docker 逐层比对，变化的层及后续层重建
- 进程内锁保证同一 `environment_name` 不并发构建，但不跨进程

---

## 构建产物存储

> 本节统一从五个维度描述每个平台：**产物类型 / 存储位置 / 用户可见的管理 API / 生命周期 / 用户控制粒度**。E2B 的服务端实现（Firecracker pipeline、SHA-256 层哈希链等）放在小节末尾的"补充"作为深入参考。

### Daytona — 两层产物：匿名构建产物 + 命名 Snapshot

Daytona 同一个底层存储承载两种命名的产物，调用方需明确选哪一种：

#### A. 匿名构建产物（`Image` 直走 `daytona.create()`）

- **产物类型**：服务端按 Dockerfile 内容 + 上下文哈希计算的匿名快照（无名字、无 `id` 暴露给调用方）
- **存储位置**：Daytona 平台内部 Object Storage（S3 兼容），调用方不可直达底层
- **管理 API**：**无**。调用方拿不到 ID，也不能 list/delete 这一层产物
- **生命周期**：服务端自动缓存 **24 小时**，过期清理
- **用户控制**：`Image.from_dockerfile(force_build=True)` 强制重建当次

#### B. 命名 Snapshot（`AsyncSnapshotService.create()`）

- **产物类型**：注册到 Daytona 数据库的 Snapshot 对象（`id` / `name` / `state` / `image_name` / `size` / `cpu/gpu/mem/disk` 等字段）。**Snapshot 不是标准 Docker 镜像**，是平台专有快照格式
- **存储位置**：同上，但产物在数据库中有名字、有状态、可查询
- **管理 API**：完整的 CRUD 接口

  ```python
  class AsyncSnapshotService:
      async def list(page=None, limit=None) -> PaginatedSnapshots
      async def get(name: str) -> Snapshot
      async def create(params: CreateSnapshotParams, *, on_logs=None, timeout=0) -> Snapshot
      async def delete(snapshot: Snapshot) -> None
      async def activate(snapshot: Snapshot) -> Snapshot   # 激活归档态的 Snapshot
  ```
- **生命周期**：永久持有，需手动删除
- **用户控制**：`snapshot.delete()` / Dashboard / CLI

#### 构建上下文传输

`Image` 对象的 `_context_list`（`COPY` 引用的本地文件）通过 `AsyncObjectStorage.upload()` 上传，bucket 由服务端 `get_push_access()` 动态下发（SDK 的默认 fallback bucket 是 `daytona-volume-builds`，但生产环境通常不用 fallback）。上传产生 content hash 数组随 `CreateBuildInfo(context_hashes=..., dockerfile_content=...)` 提交给服务端。

---

### E2B — 命名 Template

- **产物类型**：注册到 E2B 后端的 Template（暴露给调用方的标识是 `template_id` 或 `alias`）。底层是 Firecracker microVM 快照（rootfs/memfile/snapfile），但调用方不直接接触这一层
- **存储位置**：E2B 平台云对象存储，元数据存数据库
- **管理 API**：

  ```python
  class AsyncTemplate:
      @staticmethod
      async def build(template, name=None, *, alias=None, cpu_count=2, memory_mb=1024, skip_cache=False) -> BuildInfo
      @staticmethod
      async def alias_exists(alias: str) -> bool      # REST GET /templates/aliases/{alias}
      # 删除走 CLI: `e2b template delete <name>`
  ```
- **生命周期**：永久保留，无自动清理；构建失败时服务端自动回收已上传对象
- **用户控制**：
  - 缓存复用：alias 相同则复用（Harbor 把 `dirhash[:8]` 嵌入 alias 实现内容寻址）
  - 强制重建：`AsyncTemplate.build(skip_cache=True)`
  - 删除：`e2b template delete` CLI / API

#### 补充：服务端实现细节（如不关心可跳过）

E2B 后端把 Dockerfile 拆成阶段流水线 `BaseBuilder → UserBuilder → StepBuilders(每条指令) → PostProcessing → Optimize`，每阶段计算 SHA-256 哈希作为缓存 key（输入含 `provision_version`、`disk_size`、`from_image`、`step_args`、`files_hash` 等），命中即跳过该阶段。每阶段产出 dirty-block 差异层(`rootfs.ext4.header`、`memfile.header`)。最终产物按 `buildID` 组织在 GCS/S3 (`TEMPLATE_BUCKET_NAME`)，构建缓存索引在另一个 bucket (`BUILD_CACHE_BUCKET_NAME`)。这部分对调用方完全不可见，仅决定缓存命中率。

---

### Modal — 隐式哈希缓存（无显式产物）

- **产物类型**：文件系统快照，**调用方完全无法引用**——SDK 不返回 `image_id` 给用户代码持有，下次调用时按内容重新计算哈希查找缓存
- **存储位置**：Modal 平台内部，完全抽象
- **管理 API**：**无**列表 / 查询 / 删除 API。Image 只是一个声明式 `_Image` 对象，调用 `Sandbox.create(image=image)` 时通过 gRPC `ImageGetOrCreate(image_definition_pb, force_build=...)` 提交给服务端，服务端按内容哈希返回已有或触发新构建
- **生命周期**：随镜像定义自动缓存；定义变化（Dockerfile 内容、build_args、context_files、`force_build`）即触发重建
- **用户控制**：
  - 强制重建：`Image.from_dockerfile(force_build=True)` 或 `MODAL_FORCE_BUILD=1` 环境变量
  - 无手动删除入口（旧产物由平台按使用情况和容量策略自行回收）

---

### Runloop — 命名 Blueprint + 独立的 build context 对象

- **产物类型**：Blueprint（平台托管的容器镜像），独立有 `id` / `name` / `status`(`queued`/`provisioning`/`building`/`failed`/`build_complete`) / `create_time_ms`。同名 Blueprint 可共存多个版本
- **存储位置**：Runloop 平台内部
- **管理 API**：

  ```python
  client.api.blueprints.list(name=...)           # 私有列表
  client.api.blueprints.list_public(name=...)    # 公开列表
  client.blueprint.create(name=..., dockerfile=..., build_context=BuildContext(object_id=...))
  client.blueprint.delete(blueprint_id)
  ```
- **特殊：构建上下文是独立托管对象**

  ```python
  storage_object = await sdk.storage_object.upload_from_dir(
      dir_path=Path, name=str, ttl=timedelta,    # 上下文有自己的 TTL
  ) -> StorageObject
  ```
  Blueprint 创建请求引用 `BuildContext(object_id=storage_object.id, type="object")`，因此构建上下文与 Blueprint 解耦：上下文短命（TTL 1h 即可），Blueprint 永久。
- **生命周期**：Blueprint **永久保留并持续计费**（官方文档明确提醒）；StorageObject 按 TTL 自动过期
- **用户控制**：
  - 缓存复用：按 `name` 查 list，取最新 `build_complete`；**无内容哈希**，同名同 dockerfile 改了内容也不会触发重建
  - 强制重建：调用 `blueprint.create()` 不复用旧 ID 即产生新 Blueprint
  - 删除：`blueprint.delete()`（官方建议主动清理旧版本控制成本）

---

### GKE — 用户自管 Artifact Registry

- **产物类型**：标准 OCI/Docker 镜像（这是六个平台中唯一让用户拿到原生 Docker 镜像的）
- **存储位置**：**用户自有的** Google Artifact Registry，按 region 存储。Daytona/E2B/Modal/Runloop 都是平台托管，GKE 是用户托管
- **管理 API**：

  ```bash
  gcloud builds submit --tag <repo>/<name>:latest <env_dir>     # 构建并推送
  gcloud artifacts docker images describe <image_url>           # 检查存在
  gcloud artifacts docker images delete <image_url>             # 删除
  ```
  Repository 也支持 cleanup policy（按 tag 状态、版本数、镜像年龄自动清理）
- **生命周期**：用户完全自管,Cloud Build 缓存层由 GCP 自动管理
- **用户控制**：
  - 缓存复用:tag 固定为 `{environment_name}:latest`,**无内容哈希**,内容变化但 tag 不变会静默复用旧镜像
  - 强制重建：`force_build=True` 走 `gcloud builds submit` 覆盖 `:latest`
  - 删除：CLI / Console / cleanup policy
- **费用**：用户 Artifact Registry 按 GB/月计费 + 跨 region 拉取的出网费用

---

### Docker — 本地 Docker daemon（无远端存储）

- **产物类型**：标准 Docker 镜像
- **存储位置**：**宿主机本地磁盘**(无 push 到 registry)
- **管理 API**：原生 Docker CLI

  ```bash
  docker images                                          # 列表
  docker rmi <image>                                     # 删除
  docker image prune                                     # 清理悬挂镜像
  docker compose down --rmi all                          # 一并删除 compose 镜像
  ```
- **生命周期**：持久存在直到显式 `docker rmi` 或 `docuum` 等清理工具
- **用户控制**：
  - 缓存复用：Docker daemon 自动按 layer cache,Dockerfile 指令或文件内容变化即触发对应层及其后所有层重建（**自动内容感知**）
  - 进程内并发去重：Harbor 通过类级别 `_image_build_locks: dict[name, asyncio.Lock]` 串行化同名镜像的并发构建,跨进程不生效
  - 强制重建：`docker compose build --no-cache`

---

## 对比

### 接口与缓存

| 平台 | 接口模式 | 缓存 key | 内容变更检测 |
|------|---------|---------|------------|
| Daytona | 热路径 `snapshot.get(name)` → `create(FromSnapshot)`；冷路径 `Image.from_dockerfile()` → `create(FromImage)` | 命名 Snapshot 名称（显式）+ 服务端构建定义哈希（24h 隐式） | 仅冷路径自动（24h 内） |
| E2B | `from_dockerfile()` → `build()` → `create()` | `name__sha256[:8]` | 自动（目录哈希） |
| Modal | `Image.from_dockerfile()` → `Sandbox.create()` | 平台侧计算（镜像定义哈希） | 自动（平台侧） |
| Runloop | `upload` → `blueprint.create()` → `devbox.create()` | `harbor_{name}_blueprint` | 无 |
| GKE | `gcloud builds submit` → `create_pod()` | `{name}:latest` | 无 |
| Docker | `docker compose build` → `up` | Docker layer cache | 自动（逐层比对） |

### 构建产物与存储

| 平台 | 产物可见性 | 暴露给用户的标识 | 存储位置 | 默认生命周期 | 显式删除 API |
|------|----------|---------------|---------|------------|------------|
| Daytona（Image 路径） | 不可见 | 无 | 平台 S3 兼容存储 | 24h 自动过期 | 无（不可主动删） |
| Daytona（Snapshot 路径） | 可见，平台专有快照 | `name` / `id` / `state` | 同上 | 永久 | `snapshot.delete()` |
| E2B | 可见，命名 Template | `template_id` / `alias` | GCS/S3（平台托管） | 永久 | `e2b template delete` CLI |
| Modal | 不可见 | 无（无 `image_id` 句柄） | 平台内部抽象 | 平台自行回收 | 无（仅 `force_build`） |
| Runloop | 可见，命名 Blueprint | `id` / `name` / `status` | 平台内部 | 永久且持续计费 | `blueprint.delete()` |
| GKE | 可见，标准 OCI 镜像 | 镜像 URL `repo/name:tag` | **用户自有** Artifact Registry | 永久（cleanup policy 可选） | `gcloud artifacts docker images delete` |
| Docker | 可见，标准 Docker 镜像 | 本地 image name/id | **本地** Docker daemon | 永久直到 `docker rmi` | `docker rmi` / `docker image prune` |

> **观察一**：六个平台只有 GKE 和 Docker 让用户拿到原生 OCI/Docker 镜像；其余四个均为平台专有的不透明产物。
>
> **观察二**：仅有 Daytona（Image 路径）和 Modal 不暴露产物 ID，其它都暴露命名标识，可以查、可以删。
>
> **观察三**：除 GKE 和 Docker 外，存储位置都在平台侧；Runloop 还会持续计费，意味着调用方需要主动管理生命周期。

### Harbor 使用方式参考

Harbor 的 `BaseEnvironment` 通过 `start(force_build: bool)` 统一入口，各环境在 `start()` 内部完成从 Dockerfile 到沙箱运行的完整流程。构建上下文统一为 `environment_dir`，Dockerfile 位于 `environment_dir / "Dockerfile"`。
