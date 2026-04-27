# ROCK 部署架构

本文档描述 ROCK 支持的三种部署模式（Ray、Kubernetes、FC）的架构设计和职责划分。

## 概述

ROCK 通过统一的抽象层支持多种部署后端，实现对 Sandbox 的生命周期管理。不同后端的调度能力不同，因此架构设计也有所差异。

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              ROCK Admin                                      │
│                        (统一 API 入口，Sandbox 生命周期管理)                   │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                 ▼
             ┌──────────┐      ┌──────────┐      ┌──────────┐
             │   Ray    │      │   K8s    │      │   FC     │
             │ Operator │      │ Operator │      │ (无 Op)  │
             └──────────┘      └──────────┘      └──────────┘
                    │                 │                 │
                    ▼                 ▼                 ▼
             Ray 集群调度      K8s 集群调度       FC 平台调度
                    │                 │                 │
                    ▼                 ▼                 ▼
             DockerDeployment   BatchSandbox      FCDeployment
             (容器执行)          (Pod 执行)        (Session 执行)
```

## 三种部署模式对比

### 架构图

#### Ray 模式

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Ray 模式                                        │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  docker build + push         DockerDeploymentConfig      DockerDeployment  │
│  ──────────────────────►     ──────────────────────►     ────────────────► │
│  预构建 Image                引用 image + 资源参数        启动容器          │
│  (运维/CI)                   (应用层)                    (执行层)          │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      │ RayOperator.submit()
                                      ▼
                              Ray 集群调度
                              (自动选择节点)
```

#### K8s 模式

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              K8s 模式                                        │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  kubectl apply               DockerDeploymentConfig      BatchSandboxProvider│
│  ──────────────────────►     ──────────────────────►     ────────────────► │
│  预定义 Template/Pool         引用 pool + 资源参数        创建 Pod          │
│  (运维/CI)                   (应用层)                    (执行层)          │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      │ K8sOperator.submit()
                                      ▼
                              K8s 集群调度
                              (基于 Pool/Template)
```

#### FC 模式

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              FC 模式                                         │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  s deploy                    FCDeploymentConfig           FCDeployment      │
│  ──────────────────────►     ──────────────────────►     ────────────────► │
│  预部署 FC Function          引用 function + 会话参数     创建 Session     │
│  (运维/CI)                   (应用层)                    (执行层)          │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      │ (无 Operator - FC 自带调度)
                                      ▼
                              FC 平台调度
                              (Serverless)
```

### 职责对比表

| 层级 | Ray | K8s | FC |
|------|-----|-----|-----|
| **基础设施资源** | Docker Image | K8s Template/Pool | FC Function |
| **部署方式** | `docker build/push` | `kubectl apply` | `s deploy` |
| **调度决策者** | RayOperator | K8sOperator | FC 平台 |
| **Config 职责** | 引用 image + 资源 | 引用 pool + 资源 | 引用 function + 会话 |
| **Deployment 职责** | 启动/停止容器 | 创建/删除 Pod | 创建/关闭 Session |
| **Operator** | 有 | 有 | 无 |

### 配置字段对应关系

| 概念 | Ray | K8s | FC |
|------|-----|-----|-----|
| **资源标识** | `image` | `pool` (→ template) | `function_name` |
| **计算资源** | `memory`, `cpus` | `memory`, `cpus` | `memory`, `cpus` |
| **生命周期** | `auto_clear_time` | `auto_clear_time` | `sandbox_ttl` |
| **执行实例** | Container | Pod | Session |

## Operator 职责

### 接口定义

```python
class AbstractOperator(ABC):
    @abstractmethod
    async def submit(self, config: DeploymentConfig, user_info: dict = {}) -> SandboxInfo: ...

    @abstractmethod
    async def get_status(self, sandbox_id: str) -> SandboxInfo: ...

    @abstractmethod
    async def stop(self, sandbox_id: str) -> bool: ...
```

### RayOperator

RayOperator 负责 Ray 集群上的 Sandbox 调度和生命周期管理。

```
RayOperator.submit()
    │
    ├── _generate_actor_options()        # 设置资源: num_cpus, memory
    │
    ├── SandboxActor.options().remote()  # 创建 Ray Actor
    │
    ├── actor.set_metrics_endpoint()     # 设置监控
    ├── actor.set_user_defined_tags()
    ├── actor.set_user_id()              # 设置用户信息
    ├── actor.set_experiment_id()
    ├── actor.set_namespace()
    │
    └── actor.start()                    # 启动容器 (通过 DockerDeployment)
```

**核心职责**：

| 方法 | 职责 | 实现细节 |
|------|------|----------|
| `submit()` | 调度 + 创建 Actor | 生成 Actor 名称、设置资源、创建 `SandboxActor`、设置 user_info |
| `get_status()` | 状态查询 | 从 Actor 获取 `sandbox_info`、`get_status`、`is_alive`，合并 Redis 缓存 |
| `stop()` | 停止清理 | 调用 `actor.stop()`、`ray.kill(actor)` |

### K8sOperator

K8sOperator 负责 Kubernetes 集群上的 Sandbox 调度和生命周期管理。

```
K8sOperator.submit()
    │
    ├── _get_pool_name()                 # Pool 选择策略
    │   └── ResourceMatchingPoolSelector.select_pool()
    │
    ├── _get_template_name()             # Template 选择
    │
    ├── _build_batchsandbox_manifest()   # 渲染 YAML
    │   ├── _build_pool_manifest()       # Pool 模式
    │   └── _template_loader.build_manifest()  # Template 模式
    │
    └── _k8s_api.create_custom_object()  # 创建 K8s 资源
```

**核心职责**：

| 方法 | 职责 | 实现细节 |
|------|------|----------|
| `submit()` | 调度 + 创建 CRD | Pool/Template 选择、渲染 Manifest、创建 `BatchSandbox` 资源 |
| `get_status()` | 状态查询 | 从 Informer 缓存读取、检查 `is_alive` |
| `stop()` | 停止清理 | 删除 `BatchSandbox` CRD |

### FC 为什么不需要 Operator

| 职责 | Ray | K8s | FC |
|------|-----|-----|-----|
| **调度决策** | RayOperator 选择节点 | K8sOperator 选择 Pool | FC 平台自动调度 |
| **资源分配** | Actor options | Manifest spec | 函数配置 |
| **生命周期管理** | Actor 创建/删除 | CRD 创建/删除 | Session 创建/关闭 |
| **执行层** | DockerDeployment | Pod 内容器 | FC 函数实例 |

FC 是 Serverless 架构，调度和资源分配由平台自动负责，ROCK 只需要 `FCDeployment` 来管理 Session 生命周期。

## Deployment 职责

### 继承关系

```
AbstractDeployment (抽象基类)
    │
    ├── LocalDeployment        # 本地进程（开发/测试）
    │
    ├── RemoteDeployment       # 远程 rocklet 连接
    │
    ├── DockerDeployment       # Docker 容器
    │   │
    │   └── RayDeployment      # Ray Actor 包装 DockerDeployment
    │
    └── FCDeployment           # FC Session 管理
```

### 核心方法

```python
class AbstractDeployment(ABC):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def is_alive(self) -> IsAliveResponse: ...
    async def execute(self, command: Command) -> CommandResponse: ...
    async def read_file(self, request: ReadFileRequest) -> ReadFileResponse: ...
    async def write_file(self, request: WriteFileRequest) -> WriteFileResponse: ...
    async def upload(self, request: UploadRequest) -> UploadResponse: ...
```

### 各实现职责

| Deployment | 职责 | 执行环境 |
|------------|------|----------|
| `LocalDeployment` | 本地进程管理 | 宿主机 |
| `DockerDeployment` | Docker 容器生命周期 | Docker Engine |
| `RayDeployment` | 继承 DockerDeployment，添加 Actor 管理 | Ray 集群 |
| `RemoteDeployment` | HTTP 客户端封装 | 远程 rocklet |
| `FCDeployment` | FC Session 创建、WebSocket 通信、会话管理 | FC 平台 |

## 配置层级

### Ray/K8s 配置层级

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  DockerDeploymentConfig                                                      │
│  ├── image: str              # Docker 镜像                                   │
│  ├── memory: str             # 内存 (e.g., "8g")                            │
│  ├── cpus: float             # CPU 核数                                      │
│  ├── auto_clear_time: int    # 自动清理时间（分钟）                           │
│  └── runtime_config          # 运行时配置                                    │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      │ RayOperator / K8sOperator
                                      ▼
                              创建执行实例
```

### FC 配置层级

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  FCConfig (Admin 服务级)                                                     │
│  ├── region: str                 # 阿里云区域                                │
│  ├── account_id: str | None      # 账号 ID                                  │
│  ├── function_name: str          # 默认函数名                                │
│  ├── access_key_id: str | None   # 凭证                                     │
│  ├── access_key_secret: str      # 凭证                                     │
│  ├── default_memory: int         # 默认内存 (MB)                             │
│  ├── default_cpus: float         # 默认 CPU                                 │
│  ├── default_session_ttl: int    # 默认会话 TTL (秒)                         │
│  ├── default_timeout: float      # 默认请求超时 (秒)                          │
│  └── default_session_idle_timeout: int  # 默认空闲超时 (秒)                   │
└─────────────────────────────────────────────────────────────────────────────┘
                              │
                              │ merge_with_fc_config()
                              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  FCDeploymentConfig (API 调用级)                                             │
│  ├── session_id: str | None      # 会话 ID（FC session = ROCK sandbox）     │
│  ├── function_name: str | None   # 函数名（可选，使用默认值）                  │
│  ├── region: str | None          # 区域（可选）                              │
│  ├── memory: int | None          # 内存覆盖                                 │
│  ├── cpus: float | None          # CPU 覆盖                                 │
│  ├── sandbox_ttl: int | None     # 会话 TTL 覆盖                            │
│  ├── sandbox_idle_timeout: int | None  # 空闲超时覆盖                        │
│  └── timeout: float | None       # 请求超时覆盖                              │
└─────────────────────────────────────────────────────────────────────────────┘
                              │
                              │ 调用已部署的 FC 函数
                              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  s.yaml (FC 函数部署配置)                                                    │
│  ├── 定义 FC 函数资源规格                                                    │
│  ├── runtime: python3.11 / custom-container                                 │
│  ├── memorySize: 4096                                                        │
│  ├── cpu: 2.0                                                                │
│  ├── sessionTTLInSeconds: 600                                               │
│  ├── sessionIdleTimeoutInSeconds: 60                                        │
│  └── 通过 `s deploy` 命令部署                                                │
└─────────────────────────────────────────────────────────────────────────────┘
```

### FC 命名约定

| 层级 | 术语 | 说明 |
|------|------|------|
| **FCConfig / FC 平台** | `session` | FC 原生概念，WebSocket 状态路由 |
| **FCDeploymentConfig** | `sandbox` | ROCK 术语，与 ROCK 其他模式保持一致 |
| **映射关系** | `session_id` = `sandbox_id` | 1:1 映射 |

## 设计一致性

### 职责分离原则

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          Operator 职责边界                                   │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   ✅ 调度决策                                                                │
│   ├── Ray: 选择 Ray 集群节点，分配 Actor 资源                                │
│   └── K8s: 选择 Pool/Template，渲染 Manifest                               │
│                                                                             │
│   ✅ 生命周期管理                                                            │
│   ├── submit: 创建资源                                   │
│   ├── get_status: 查询状态                               │
│   └── stop: 删除资源                                     │
│                                                                             │
│   ✅ 元数据管理                                                              │
│   └── user_id, experiment_id, namespace, rock_authorization                │
│                                                                             │
│   ❌ 不负责（由 Deployment 负责）                                            │
│   ├── 容器启动/停止                        │
│   ├── 文件操作                   │
│   └── 命令执行                          │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 基础设施与应用分离

| 部署模式 | 基础设施层 | 应用层 | 工具 |
|----------|-----------|--------|------|
| Ray | Docker Image | DockerDeploymentConfig | `docker build/push` |
| K8s | Template/Pool | DockerDeploymentConfig | `kubectl apply` |
| FC | FC Function | FCDeploymentConfig | `s deploy` |

**设计原则**：
- 基础设施由运维/CI 预先部署，变更频率低
- 应用配置由 ROCK Admin 动态管理，变更频率高
- 类比：Docker Image ≈ K8s Template ≈ FC Function

## 相关文件

### 核心实现

| 模块 | 文件路径 |
|------|----------|
| AbstractOperator | `rock/sandbox/operator/abstract.py` |
| RayOperator | `rock/sandbox/operator/ray.py` |
| K8sOperator | `rock/sandbox/operator/k8s/operator.py` |
| BatchSandboxProvider | `rock/sandbox/operator/k8s/provider.py` |

### 配置定义

| 模块 | 文件路径 |
|------|----------|
| FCConfig | `rock/config.py` |
| FCDeploymentConfig | `rock/deployments/config.py` |
| DockerDeploymentConfig | `rock/deployments/config.py` |
| K8sConfig | `rock/config.py` |

### Deployment 实现

| 模块 | 文件路径 |
|------|----------|
| AbstractDeployment | `rock/deployments/abstract.py` |
| DockerDeployment | `rock/deployments/docker.py` |
| RayDeployment | `rock/deployments/ray.py` |
| FCDeployment | `rock/deployments/fc.py` |