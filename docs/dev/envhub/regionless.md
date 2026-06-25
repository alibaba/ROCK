# Regionless（地域无感）功能设计文档

## 目录

- [背景与目标](#背景与目标)
- [核心概念](#核心概念)
- [解析规则（Resolution Rule）](#解析规则resolution-rule)
- [模块结构](#模块结构)
- [核心组件](#核心组件)
  - [RegionlessResolver](#regionlessresolver)
  - [resolve_image](#resolve_image)
  - [resolve_dockerfile](#resolve_dockerfile)
  - [resolve_compose / compose_pull（新增核心能力）](#resolve_compose--compose_pull新增核心能力)
- [SDK 对外接口](#sdk-对外接口)
- [配置](#配置)
- [Harbor 集成与迁移](#harbor-集成与迁移)
- [数据流](#数据流)
- [错误处理与降级](#错误处理与降级)
- [测试策略](#测试策略)

---

## 背景与目标

ROCK 沙箱在多地域（multi-region）部署时面临一个共性问题：**同一个容器镜像在不同地域的拉取体验差异巨大**。一个发布在 Docker Hub / ghcr.io / gcr.io 上的公共镜像（如 `swebench/sweb.eval.x86_64.foo:latest`），在国内地域拉取往往很慢甚至超时。常见做法是把这些镜像预先镜像（mirror）到各地域本地的 registry（如阿里云 ACR），但这就要求**调用方感知自己所在的地域、并手动改写镜像引用** —— 这是一种"地域有感"的耦合。

**Regionless（地域无感）** 的目标是：让调用方继续使用原始的、地域无关的镜像引用，由框架在运行时**自动探测并改写**到当前实例可达的 ROCK 镜像源。调用方无需知道自己在哪个地域、镜像被镜像到了哪里。

本功能的具体目标：

**1. 把镜像解析能力下沉到 envhub SDK**

当前 harbor 在 `harbor/src/harbor/utils/rock_registry.py` 中自己维护了一份镜像改写逻辑。本设计将这份能力以 1:1 语义迁移进 ROCK 的 `rock.sdk.envhub.regionless`，使其成为 envhub SDK 的标准能力，供 harbor 及其他服务统一集成，**替代 harbor 自维护的 `rock_registry.py`**。

**2. 把 `docker compose pull` 封装进 envhub SDK（核心新增）**

在 `resolve_image` / `resolve_dockerfile` 之上，新增对 **Docker Compose** 工作流的支持：解析 compose 文件中所有 service 的 `image:` 引用，逐个改写到 ROCK 镜像源，再执行 `docker compose pull`。这是本次新增的核心能力，让以 compose 编排的环境也能享受地域无感。

**3. 严格保持降级安全（fail-safe）**

任何一步失败（环境变量未配置、探测超时、镜像在镜像源不存在、网络错误）都**回退到原始镜像**，绝不让地域无感能力本身成为环境启动的阻断点。

---

## 核心概念

| 概念 | 说明 |
|------|------|
| 原始镜像（original image） | 调用方提供的、地域无关的镜像引用，如 `swebench/sweb.eval.x86_64.foo:latest` |
| ROCK 镜像源（ROCK registry） | 各地域本地可达的镜像仓库地址，形如 `host/namespace`，如 `reg-a.aliyuncs.com/mirror-1` |
| 候选镜像（candidate） | 用镜像名 + tag/digest 拼到 ROCK 镜像源下得到的引用，如 `reg-a.aliyuncs.com/mirror-1/sweb.eval.x86_64.foo:latest` |
| 探测（probe） | 通过 Docker Registry v2 manifest API 检查候选镜像是否真实存在 |
| 解析（resolve） | 探测命中后，把原始镜像改写为候选镜像；未命中则保持原始镜像不变 |

`INSTANCE_ROCK_REGISTRY` 环境变量由部署实例按地域注入 —— 这是"地域感知"被收敛到的**唯一一处**，对上层调用方完全透明。

---

## 解析规则（Resolution Rule）

解析逻辑与 harbor 现有 `RockRegistryResolver` 保持 **完全一致的语义**，迁移时 1:1 移植：

1. **读取镜像源列表** —— 从 `INSTANCE_ROCK_REGISTRY` 读取一个或多个 `host/namespace` 条目，以 `,` 或 `;` 分隔，如 `reg-a.aliyuncs.com/mirror-1,reg-b.aliyuncs.com/mirror-2`。未设置或为空时，**原样返回原始镜像**。

2. **构建候选镜像** —— 取镜像引用的镜像名部分（剥离原 registry 与第一级 namespace，保留嵌套 namespace 与镜像名），组合原始 tag/digest，拼到镜像源下：

   | 原始镜像 | 镜像源 | 候选镜像 |
   |----------|--------|----------|
   | `swebench/sweb.eval.x86_64.foo:latest` | `reg/mirror` | `reg/mirror/sweb.eval.x86_64.foo:latest` |
   | `docker.io/library/python:3.12` | `reg/ns` | `reg/ns/python:3.12` |
   | `ghcr.io/foo/bar/baz:v1` | `reg/ns` | `reg/ns/bar/baz:v1`（保留嵌套 namespace） |
   | `foo/bar`（无 tag） | `reg/ns` | `reg/ns/bar:latest`（默认 tag） |

3. **按序探测，命中即返回** —— 对每个配置的镜像源按顺序，通过 Docker Registry v2 manifest API（`GET /v2/{repo}/manifests/{tag}`）探测候选，支持 Bearer token 认证与短超时（默认 5s）。返回**第一个**存在的候选；若都不存在（或探测超时/失败），回退到原始镜像。

4. **进程内缓存** —— 探测结果按 `registries|image` 缓存在进程内，避免并发 trial 反复打 registry。缓存 key 包含镜像源列表，因此镜像源变化时不会命中旧缓存。

5. **digest-pinned 不改写** —— 形如 `...@sha256:...` 的 digest 锁定引用是内容锁定的，**永不改写、永不探测**，直接原样返回。

---

## 模块结构

```
rock/
└── sdk/
    └── envhub/
        ├── client.py            # 现有：EnvHubClient
        ├── config.py            # 现有
        ├── schema.py            # 现有
        ├── datasets/            # 现有：dataset 管理
        └── regionless/          # 新增
            ├── __init__.py      # 对外入口：resolve_image / resolve_dockerfile /
            │                    #          resolve_compose / compose_pull /
            │                    #          RegionlessResolver / ROCK_REGISTRY_ENV
            ├── resolver.py      # RegionlessResolver（从 harbor RockRegistryResolver 1:1 迁移）
            └── compose.py       # compose pull 封装：resolve_compose + compose_pull
```

> 命名说明：harbor 中的类名为 `RockRegistryResolver`。迁移到 envhub 后建议主名为 `RegionlessResolver`（贴合功能语义），并在 `__init__.py` 中保留 `RockRegistryResolver = RegionlessResolver` 别名，降低 harbor 的迁移成本。

---

## 核心组件

### RegionlessResolver

`rock/sdk/envhub/regionless/resolver.py` —— 与 harbor `RockRegistryResolver` 行为完全一致，逐方法迁移：

```python
ROCK_REGISTRY_ENV = "INSTANCE_ROCK_REGISTRY"
_DEFAULT_PROBE_TIMEOUT_SEC = 5.0
_REGISTRY_SEPARATORS = (",", ";")


class RegionlessResolver:
    """Resolves container image references to ROCK mirror registries."""

    def __init__(self) -> None:
        self._resolve_cache: dict[str, str] = {}
        self._cache_lock = asyncio.Lock()

    # —— 纯函数工具（无 I/O，便于单测）——
    @staticmethod
    def parse_registries(raw: str) -> list[str]: ...
    @staticmethod
    def split_tag_or_digest(image: str) -> tuple[str, str]: ...
    @staticmethod
    def build_candidate(image: str, registry: str) -> str: ...

    # —— 探测（唯一 I/O 点，单测中被 mock）——
    async def _http_probe_manifest(self, image: str, timeout_sec: float) -> bool: ...

    # —— 对外解析能力 ——
    async def resolve_image(self, image: str, *, timeout_sec=...) -> str: ...
    async def resolve_dockerfile(self, dockerfile: Path, *, timeout_sec=...) -> bool: ...
    async def resolve_compose(self, compose_path: Path, *, timeout_sec=...) -> bool: ...  # 新增

    def reset_cache(self) -> None: ...  # 测试辅助
```

**设计要点（保持与 harbor 一致）：**

- `parse_registries` / `split_tag_or_digest` / `build_candidate` 是**无 I/O 的静态纯函数**，单测直接断言输入输出。
- `_http_probe_manifest` 是**唯一的 I/O 点**，所有 `resolve_*` 的单测都通过 `patch.object(..., "_http_probe_manifest", new=AsyncMock(...))` mock 掉，使解析逻辑可在无网络环境下测试。
- 模块级提供进程内默认实例与便捷函数（绑定方法），与 harbor 一致：

  ```python
  _default_resolver = RegionlessResolver()
  resolve_image = _default_resolver.resolve_image
  resolve_dockerfile = _default_resolver.resolve_dockerfile
  resolve_compose = _default_resolver.resolve_compose
  ```

### resolve_image

签名与语义与 harbor 完全一致：

```python
async def resolve_image(image: str, *, timeout_sec: float = 5.0) -> str
```

在以下情况是 no-op（原样返回 `image`）：`INSTANCE_ROCK_REGISTRY` 未配置 / 无可用条目；`image` 为空；`image` 为 digest-pinned；所有镜像源都不含候选；所有探测超时或失败。多个镜像源按序探测，第一个命中者胜出。

### resolve_dockerfile

```python
async def resolve_dockerfile(dockerfile: Path, *, timeout_sec: float = 5.0) -> bool
```

逐行匹配 `FROM` 指令（正则 `^(?P<prefix>\s*FROM\s+(?:--\S+\s+)*)(?P<image>\S+)(?P<suffix>.*)$`，大小写不敏感），对镜像部分调用 `resolve_image` 改写，保留 `--platform=...` flag、`AS builder` 子句与原始换行符；跳过注释行。任意行被改写则返回 `True` 并写回文件。

### resolve_compose / compose_pull（新增核心能力）

这是本次新增的核心：把 `docker compose pull` 封装进 envhub SDK，让 compose 编排的环境也地域无感。

**`resolve_compose`** —— 解析 compose 文件中所有 service 的 `image:`，逐个改写到镜像源（与 `resolve_dockerfile` 改写 `FROM` 同构）：

```python
async def resolve_compose(compose_path: Path, *, timeout_sec: float = 5.0) -> bool:
    """Rewrite ``image:`` of every service in a compose file to ROCK mirrors when available.

    Returns True if any service image was rewritten.
    """
```

实现要点：
- 用 `ruamel.yaml`（保序、保注释）或 `yaml.safe_load` 加载 compose 文件，遍历 `services.*.image`。
- 对每个 `image` 调用 `self.resolve_image(...)`；命中改写则标记 `changed`。
- 复用 `resolve_image` 的进程内缓存，多 service 引用同一镜像时只探测一次。
- 仅当 `changed` 时写回文件。**只改写 `image:` 字段，不触碰 `build:` 段**（`build:` 段的 Dockerfile 由 `resolve_dockerfile` 负责）。

**`compose_pull`** —— `rock/sdk/envhub/regionless/compose.py`，先解析再拉取：

```python
async def compose_pull(
    compose_path: Path,
    *,
    services: list[str] | None = None,
    project_name: str | None = None,
    env: dict[str, str] | None = None,
    timeout_sec: float = 5.0,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    """Resolve images in *compose_path* to ROCK mirrors, then ``docker compose pull``.

    1. resolve_compose(compose_path)  —— 把 service 镜像改写到地域本地镜像源
    2. docker compose -f <compose_path> [-p <project>] pull [services...]
    """
```

实现要点：
- 第一步 `await resolve_compose(compose_path, timeout_sec=timeout_sec)`；解析失败/无命中不阻断，继续用原始镜像拉取（fail-safe）。
- 第二步 shell out：`docker compose -f <compose_path> pull`，支持指定 `services`、`-p project_name`、注入 `env`、追加 `extra_args`（如 `--ignore-pull-failures`、`--quiet`）。
- 失败时抛出携带 stdout/stderr 的异常，与 `ImageMirror`（`rock/sdk/builder/image_mirror.py`）中现有 `subprocess.run` 错误处理风格对齐。

---

## SDK 对外接口

`rock/sdk/envhub/regionless/__init__.py` 暴露：

```python
from rock.sdk.envhub.regionless import (
    ROCK_REGISTRY_ENV,        # "INSTANCE_ROCK_REGISTRY"
    RegionlessResolver,       # 类（主名）
    RockRegistryResolver,     # = RegionlessResolver（harbor 迁移兼容别名）
    resolve_image,            # async (str) -> str
    resolve_dockerfile,       # async (Path) -> bool
    resolve_compose,          # async (Path) -> bool   —— 新增
    compose_pull,             # async (Path, ...) -> CompletedProcess  —— 新增
)
```

调用方典型用法：

```python
from pathlib import Path
from rock.sdk.envhub.regionless import resolve_image, resolve_dockerfile, compose_pull

# 1. 单镜像（prebuilt 场景）
image = await resolve_image("swebench/sweb.eval.x86_64.foo:latest")

# 2. Dockerfile（build 场景）
await resolve_dockerfile(Path("Dockerfile"))

# 3. Compose（编排场景，核心新增）
await compose_pull(Path("docker-compose.yml"), services=["app"])
```

---

## 配置

| 配置项 | 来源 | 默认值 | 说明 |
|--------|------|--------|------|
| `INSTANCE_ROCK_REGISTRY` | 进程环境变量（`os.environ`） | 未设置 | 一个或多个 `host/namespace`，以 `,`/`;` 分隔。由部署实例按地域注入 |
| 探测超时 | `timeout_sec` 入参 | `5.0` 秒 | 单次 manifest 探测超时 |

> **关于是否纳入 `rock/env_vars.py`**：`INSTANCE_ROCK_REGISTRY` 是**实例注入**的运行时变量（非 ROCK 自身配置），与 harbor 保持一致，直接通过 `os.environ.get(ROCK_REGISTRY_ENV, "")` 读取，**不**纳入 `env_vars.py` 的 `ROCK_*` 体系，避免与 lazy default 机制混淆。

---

## Harbor 集成与迁移

### 现状

harbor 在两处使用自维护的 resolver（`harbor/src/harbor/environments/docker/docker.py`）：

```python
from harbor.utils.rock_registry import resolve_dockerfile, resolve_image

# DockerEnvironment.start() 内：
# prebuilt 镜像场景
if self._use_prebuilt and self._env_vars.prebuilt_image_name:
    resolved = await resolve_image(self._env_vars.prebuilt_image_name)
    if resolved != self._env_vars.prebuilt_image_name:
        self._env_vars.prebuilt_image_name = resolved

# build 场景
if not self._use_prebuilt:
    if self._dockerfile_path.exists():
        await resolve_dockerfile(self._dockerfile_path)
    ...
    await self._run_docker_compose_command(["build"])
...
await self._run_docker_compose_command(["up", "--detach", "--wait"])
```

### 迁移方案

harbor 依赖 `rl-rock`（envhub SDK）后，**仅需替换 import**，调用点不变：

```python
# Before
from harbor.utils.rock_registry import resolve_dockerfile, resolve_image
# After
from rock.sdk.envhub.regionless import resolve_dockerfile, resolve_image
```

随后删除 `harbor/src/harbor/utils/rock_registry.py` 与 `harbor/tests/unit/test_rock_registry.py`（其覆盖由 ROCK 侧测试承接，见下）。

> 兼容别名 `RockRegistryResolver = RegionlessResolver` 与一致的便捷函数签名，保证迁移期间任何直接引用 `RockRegistryResolver` 的代码无需改动。

### 可选增强

harbor 的 build 场景目前是 `resolve_dockerfile` + `compose build`；prebuilt 场景是 `resolve_image` + `compose up`。后续可让 harbor 在 `compose up` 前改用 `compose_pull` 拉取 prebuilt 镜像，把多 service 的地域无感统一交给 envhub SDK 处理（非本次迁移强制项）。

---

## 数据流

**resolve_image（单镜像）**

```
resolve_image("swebench/foo:latest")
  ├─ image 为空 / digest-pinned？ → 原样返回
  ├─ parse_registries(os.environ["INSTANCE_ROCK_REGISTRY"]) → [] ? → 原样返回
  ├─ 命中进程内缓存？ → 返回缓存
  └─ for registry in registries:               # 按序
      ├─ candidate = build_candidate(image, registry)
      ├─ _http_probe_manifest(candidate)        # GET /v2/{repo}/manifests/{tag}
      │     └─ 401 → 解析 Bearer challenge → 取 token → 重试
      └─ 命中 → resolved = candidate; break
     写入缓存 → 返回 resolved（命中候选）或 image（全部未命中）
```

**compose_pull（编排，核心新增）**

```
compose_pull(Path("docker-compose.yml"), services=["app"])
  ├─ resolve_compose(compose_path)
  │     └─ load YAML → for svc in services: resolve_image(svc.image)
  │         └─ 任一改写 → 写回 compose 文件
  └─ subprocess: docker compose -f docker-compose.yml pull app
        └─ 失败 → 抛出含 stdout/stderr 的异常
```

---

## 错误处理与降级

地域无感是**尽力而为（best-effort）**能力，核心原则：**解析失败绝不阻断环境启动**。

| 场景 | 行为 |
|------|------|
| `INSTANCE_ROCK_REGISTRY` 未设置 / 为空 | 原样返回原始镜像，不探测 |
| `image` 为空 / digest-pinned | 原样返回，不探测、不改写 |
| 候选在某镜像源不存在（manifest 404） | 探测下一个镜像源；全部未命中 → 回退原始镜像 |
| 探测超时 | 视为未命中，回退原始镜像 |
| 探测网络/协议错误（`httpx.HTTPError`） | `logger.debug` 记录，视为未命中 |
| 探测响应解析错误（`ValueError`/`KeyError`） | `logger.debug` 记录，视为未命中 |
| 探测未知异常 | `logger.warning` 记录，视为未命中（不抛出） |
| `resolve_compose` 解析 YAML 失败 | `logger.warning`，跳过改写，用原始镜像继续 |
| `compose_pull` 的 `docker compose pull` 失败 | 抛出携带 stdout/stderr 的异常（这是真实的拉取失败，需暴露给调用方） |

注意区分：**解析/探测阶段**的失败一律静默降级；**实际拉取（compose pull）阶段**的失败属于真实错误，需抛出。

---

## 测试策略

测试与 harbor 现有 `tests/unit/test_rock_registry.py` **逐用例对齐**，迁移后这些用例由 ROCK 侧承接，并补充 compose 相关新增能力的测试。

测试文件位置：

```
tests/
└── unit/
    └── envhub/
        └── regionless/
            ├── test_resolver.py    # 迁移自 harbor test_rock_registry.py（1:1 对齐）
            └── test_compose.py     # 新增：resolve_compose / compose_pull
```

### test_resolver.py（与 harbor 用例对齐）

| 测试类 | 覆盖用例（与 harbor 一致） |
|--------|---------------------------|
| `TestSplitTagOrDigest` | 显式 tag、无 tag 默认 `:latest`、digest、`host:port` 无 tag、`host:port` 带 tag |
| `TestParseRegistries` | 空/空白、单条目、逗号分隔、分号分隔、混合分隔符 + 去尾斜杠、跳过空条目 |
| `TestBuildCandidate` | 只剥第一级 namespace、显式 dockerhub host、ghcr 镜像、缺省 tag、registry 去尾斜杠、深层嵌套 namespace 保留 |
| `TestResolveImage` | 无 env 返回原始、空 env 返回原始、空镜像返回原始、digest-pinned 跳过探测、命中返回候选、未命中返回原始、缓存跳过二次探测、缓存按 registry 区分、探测超时回退、多 registry 首个命中胜出、多 registry 落到第二个、多 registry 全未命中、连接错误回退 |
| `TestResolveDockerfile` | 改写 `FROM`、未命中不改、无 env 不改、保留 `AS` 子句、处理 `--platform` flag、跳过注释行 |

**关键测试手法（沿用 harbor）：**

```python
@pytest.fixture()
def resolver():
    r = RegionlessResolver()
    yield r
    r.reset_cache()              # 每个用例后清缓存，避免串扰

# 纯函数：直接断言
def test_strips_first_namespace_only():
    assert RegionlessResolver.build_candidate(
        "swebench/sweb.eval.x86_64.foo:latest", "reg.example.com/mirror"
    ) == "reg.example.com/mirror/sweb.eval.x86_64.foo:latest"

# 解析逻辑：mock 唯一 I/O 点 + monkeypatch 环境变量
async def test_hit_returns_candidate(resolver, monkeypatch):
    monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
    with patch.object(
        RegionlessResolver, "_http_probe_manifest", new=AsyncMock(return_value=True)
    ) as probe:
        result = await resolver.resolve_image("swebench/foo:latest")
    assert result == "reg.example.com/ns/foo:latest"
    probe.assert_awaited_once()
```

> ROCK 使用 `asyncio_mode = "auto"`，async 测试无需逐个标 `@pytest.mark.asyncio`（harbor 侧需要显式标注）。从 harbor 迁移用例时可去掉该标注。

### test_compose.py（新增）

| 用例 | 说明 |
|------|------|
| `test_resolve_compose_rewrites_service_image` | mock 探测命中，断言 compose 文件中 `services.*.image` 被改写 |
| `test_resolve_compose_no_change_on_miss` | mock 探测未命中，断言文件内容不变、返回 `False` |
| `test_resolve_compose_no_env_noop` | 未设 `INSTANCE_ROCK_REGISTRY`，断言不改写 |
| `test_resolve_compose_dedupes_probe` | 多 service 引用同一镜像，断言只探测一次（缓存生效） |
| `test_resolve_compose_ignores_build_section` | 含 `build:` 段的 service 不被 `image:` 改写逻辑影响 |
| `test_compose_pull_calls_resolve_then_pull` | mock `resolve_compose` 与 `subprocess`，断言先解析后 `docker compose ... pull`，命令参数正确（`-f`、services） |
| `test_compose_pull_propagates_pull_failure` | mock `subprocess` 返回非零，断言抛出含 stderr 的异常 |
| `test_compose_pull_resolve_failure_is_non_blocking` | mock `resolve_compose` 抛异常，断言仍继续执行 pull（fail-safe） |

`compose_pull` 中的 `docker compose` 调用通过 mock `subprocess.run` / `asyncio.create_subprocess_exec` 隔离，单测不依赖真实 docker；真实 docker 端到端验证归入集成测试（`@pytest.mark.integration`）。
