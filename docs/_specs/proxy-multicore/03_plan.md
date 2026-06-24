# sandbox_proxy_router 多核化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `admin --role proxy`(sandbox_proxy_router)在单 Pod 内以 uvicorn 多 worker 跑满多核,并复用 httpx 连接池、按 worker 收口 DB 连接、给 Metrics 打 pid 标。

**Architecture:** `main.py` 改为 app-factory:主进程 `main()` 解析 argv 写入 env 并解析出 worker 数,`uvicorn.run("rock.admin.main:create_app", factory=True, workers=N)`;每个 worker 子进程调 `create_app()` 只读 env 构建路由。worker 数与 DB 池大小用纯函数计算(可单测)。仅 proxy role 多进程,admin role 恒 1。`SandboxProxyService` 拆出 `_rpc_client`(控制面)/`_proxy_client`(数据面),`http_proxy`/`host_proxy` 复用后者且绝不关闭共享 client。

**Tech Stack:** Python 3.10–3.12, FastAPI, uvicorn, httpx, SQLAlchemy(asyncpg), pytest(asyncio_mode=auto)。

参考 spec:`docs/_specs/proxy-multicore/01_requirement.md`、`02_design.md`。

---

## File Structure

| 文件 | 责任 | 动作 |
|------|------|------|
| `rock/env_vars.py` | 新增 `ROCK_PROXY_WORKERS` 懒加载默认 | Modify |
| `rock/config.py` | `DatabaseConfig` 增加 `pool_size` 字段 | Modify |
| `rock/admin/core/db_provider.py` | 池大小从 `DatabaseConfig.pool_size` 读取,不再硬编码 | Modify |
| `rock/admin/worker_config.py` | 纯函数 `resolve_workers` / `compute_pool_size`(可单测) | Create |
| `rock/admin/main.py` | app-factory(`create_app`/`_include_routers`)+ `main()` 写 env、起多 worker;lifespan 改读 env、按 worker 算池、proxy 退出 aclose | Modify |
| `rock/sandbox/service/sandbox_proxy_service.py` | 拆 `_rpc_client`/`_proxy_client`、`aclose()`、Metrics pid 标、`http_proxy`/`host_proxy` 复用 client | Modify |
| `tests/unit/admin/test_worker_config.py` | worker/pool 纯函数测试 | Create |
| `tests/unit/admin/test_create_app_routes.py` | 路由按 role 装配测试 | Create |
| `tests/unit/admin/test_db_provider_pool.py` | DB 池可配测试 | Create |
| `tests/unit/sandbox/test_proxy_httpx_reuse.py` | 两 client 拆分 + 复用不关闭测试 | Create |

---

## Task 1: 新增 `ROCK_PROXY_WORKERS` 环境变量

**Files:**
- Modify: `rock/env_vars.py`(`environment_variables` dict,在 `ROCK_ADMIN_ROLE` 之后)
- Test: `tests/unit/admin/test_worker_config.py`(本任务先放一个 env 读取测试)

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/admin/test_worker_config.py`:

```python
import importlib
import os


def test_rock_proxy_workers_defaults_to_zero(monkeypatch):
    monkeypatch.delenv("ROCK_PROXY_WORKERS", raising=False)
    import rock.env_vars as env_vars

    importlib.reload(env_vars)
    assert env_vars.ROCK_PROXY_WORKERS == 0


def test_rock_proxy_workers_reads_env(monkeypatch):
    monkeypatch.setenv("ROCK_PROXY_WORKERS", "6")
    import rock.env_vars as env_vars

    importlib.reload(env_vars)
    assert env_vars.ROCK_PROXY_WORKERS == 6
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/admin/test_worker_config.py -v`
Expected: FAIL（`AttributeError: module 'rock.env_vars' has no attribute 'ROCK_PROXY_WORKERS'`)

- [ ] **Step 3: 实现**

在 `rock/env_vars.py` 的 `environment_variables` 字典中,`"ROCK_ADMIN_ROLE": ...` 那一行后面加:

```python
    "ROCK_PROXY_WORKERS": lambda: int(os.getenv("ROCK_PROXY_WORKERS", "0")),
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/unit/admin/test_worker_config.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add rock/env_vars.py tests/unit/admin/test_worker_config.py
git commit -m "feat(env): add ROCK_PROXY_WORKERS env var (default 0=auto)"
```

---

## Task 2: `DatabaseConfig.pool_size` 可配 + `DatabaseProvider` 读取

**Files:**
- Modify: `rock/config.py`(`DatabaseConfig`,约 `:207-212`)
- Modify: `rock/admin/core/db_provider.py`(`init()`,`:40-47`)
- Test: `tests/unit/admin/test_db_provider_pool.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/admin/test_db_provider_pool.py`:

```python
import pytest

from rock.admin.core.db_provider import DatabaseProvider
from rock.config import DatabaseConfig


def test_database_config_has_pool_size_default():
    cfg = DatabaseConfig(url="")
    assert cfg.pool_size == 100


def test_database_config_pool_size_override():
    cfg = DatabaseConfig(url="", pool_size=10)
    assert cfg.pool_size == 10


@pytest.mark.asyncio
async def test_engine_uses_configured_pool_size_for_postgres(monkeypatch):
    captured = {}

    def fake_create_async_engine(url, **kwargs):
        captured.update(kwargs)
        captured["url"] = url
        return object()

    monkeypatch.setattr(
        "rock.admin.core.db_provider.create_async_engine", fake_create_async_engine
    )
    provider = DatabaseProvider(
        db_config=DatabaseConfig(url="postgresql://u:p@h:5432/db", pool_size=7)
    )
    await provider.init()
    assert captured["pool_size"] == 7
    assert captured["max_overflow"] == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/admin/test_db_provider_pool.py -v`
Expected: FAIL（`TypeError: __init__() got an unexpected keyword argument 'pool_size'`)

- [ ] **Step 3: 实现 — `DatabaseConfig` 加字段**

`rock/config.py`,把 `DatabaseConfig` 改为:

```python
@dataclass
class DatabaseConfig:
    # Supported URL formats:
    #   SQLite:     sqlite:///relative/path.db  or  sqlite:////absolute/path.db
    #   PostgreSQL: postgresql://user:password@host:port/dbname
    url: str = ""
    pool_size: int = 100  # per-process PG pool; multi-worker shrinks this (see worker_config)
```

- [ ] **Step 4: 实现 — `DatabaseProvider` 读取**

`rock/admin/core/db_provider.py`,`__init__` 保存 config,`init()` 用 `self._pool_size`:

`__init__` 改为(保留原 `_convert_url`):

```python
    def __init__(self, db_config: DatabaseConfig) -> None:
        self._url = self._convert_url(db_config.url)
        self._pool_size = db_config.pool_size
        self._engine: AsyncEngine | None = None
```

`init()` 内 asyncpg 分支改为:

```python
        if "asyncpg" in self._url:
            engine_kwargs["connect_args"] = {"statement_cache_size": 0}
            engine_kwargs["pool_size"] = self._pool_size
            engine_kwargs["max_overflow"] = 0
            engine_kwargs["pool_timeout"] = 120
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/unit/admin/test_db_provider_pool.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add rock/config.py rock/admin/core/db_provider.py tests/unit/admin/test_db_provider_pool.py
git commit -m "feat(db): make PG pool_size configurable via DatabaseConfig"
```

---

## Task 3: 纯函数 `resolve_workers` / `compute_pool_size`

**Files:**
- Create: `rock/admin/worker_config.py`
- Test: `tests/unit/admin/test_worker_config.py`(追加)

- [ ] **Step 1: 写失败测试**

在 `tests/unit/admin/test_worker_config.py` 末尾追加:

```python
from rock.admin.worker_config import compute_pool_size, resolve_workers


def test_resolve_workers_admin_always_one():
    assert resolve_workers("admin", override=None, env_workers=8, cpu_count=16) == 1
    assert resolve_workers("admin", override=10, env_workers=8, cpu_count=16) == 1


def test_resolve_workers_proxy_override_wins():
    assert resolve_workers("proxy", override=4, env_workers=8, cpu_count=16) == 4


def test_resolve_workers_proxy_env_when_no_override():
    assert resolve_workers("proxy", override=None, env_workers=8, cpu_count=16) == 8


def test_resolve_workers_proxy_auto_uses_cpu_count():
    assert resolve_workers("proxy", override=None, env_workers=0, cpu_count=16) == 16


def test_resolve_workers_proxy_auto_floor_one():
    assert resolve_workers("proxy", override=None, env_workers=0, cpu_count=None) == 1


def test_compute_pool_size_divides_by_workers():
    assert compute_pool_size(base=100, workers=8) == 12


def test_compute_pool_size_floor_two():
    assert compute_pool_size(base=100, workers=200) == 2


def test_compute_pool_size_single_worker_keeps_base():
    assert compute_pool_size(base=100, workers=1) == 100
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/admin/test_worker_config.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'rock.admin.worker_config'`)

- [ ] **Step 3: 实现**

创建 `rock/admin/worker_config.py`:

```python
"""Pure helpers for multi-worker proxy sizing. No I/O, fully unit-testable."""

from __future__ import annotations

MIN_POOL_SIZE = 2


def resolve_workers(
    role: str,
    override: int | None,
    env_workers: int,
    cpu_count: int | None,
) -> int:
    """Resolve uvicorn worker count.

    admin role is always single-process (owns scheduler/Ray singletons).
    proxy role: explicit override > env > auto(cpu_count), floored at 1.
    """
    if role != "proxy":
        return 1
    if override and override > 0:
        return override
    if env_workers and env_workers > 0:
        return env_workers
    return cpu_count or 1


def compute_pool_size(base: int, workers: int) -> int:
    """Per-worker DB pool so that workers * pool ~= base (no per-pod regression)."""
    if workers <= 1:
        return base
    return max(MIN_POOL_SIZE, base // workers)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/unit/admin/test_worker_config.py -v`
Expected: PASS（全部）

- [ ] **Step 5: 提交**

```bash
git add rock/admin/worker_config.py tests/unit/admin/test_worker_config.py
git commit -m "feat(admin): add pure resolve_workers/compute_pool_size helpers"
```

---

## Task 4: `main.py` 改 app-factory + 多 worker 启动

**Files:**
- Modify: `rock/admin/main.py`(顶层 argparse、`app = FastAPI(...)` 块、`main()`)
- Test: `tests/unit/admin/test_create_app_routes.py`

本任务把"模块级 app + main() 里 include_router"重构为工厂函数 `create_app()`,并把路由装配抽成可测的 `_include_routers(app, role)`。

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/admin/test_create_app_routes.py`:

```python
from fastapi import FastAPI

from rock.admin.main import _include_routers


def _paths(app: FastAPI) -> set[str]:
    return {getattr(r, "path", "") for r in app.routes}


def test_proxy_role_mounts_proxy_router():
    app = FastAPI()
    _include_routers(app, role="proxy")
    paths = _paths(app)
    # proxy-only endpoint from sandbox_proxy_router
    assert any(p.endswith("/get_token") for p in paths)


def test_proxy_role_excludes_admin_router():
    app = FastAPI()
    _include_routers(app, role="proxy")
    paths = _paths(app)
    # admin-ops router must NOT be present on proxy role
    assert not any("/ops" in p for p in paths)


def test_admin_role_mounts_admin_routers():
    app = FastAPI()
    _include_routers(app, role="admin")
    paths = _paths(app)
    assert any("/ops" in p for p in paths)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/admin/test_create_app_routes.py -v`
Expected: FAIL（`ImportError: cannot import name '_include_routers'`)

- [ ] **Step 3: 实现 — 删顶层 argparse,加工厂**

`rock/admin/main.py` 顶部:删除模块级的 `parser = argparse.ArgumentParser()` / `add_argument` 三行 / `args = parser.parse_args()`(`:52-57`),替换为一个 `_parse_args()` 函数:

```python
def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=str, default="local")
    parser.add_argument("--role", type=str, default="admin", choices=["admin", "proxy"])
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--workers", type=int, default=None)
    return parser.parse_args()
```

- [ ] **Step 4: 实现 — lifespan 改读 env(本任务先改 env/role 来源,池逻辑留 Task 5)**

`lifespan` 内把 `args.env` 改为 `env_vars.ROCK_ADMIN_ENV`,`args.role` 改为 `env_vars.ROCK_ADMIN_ROLE`。具体:

- `config_file_path` 那行 `f"rock-{args.env}.yml"` → `f"rock-{env_vars.ROCK_ADMIN_ENV}.yml"`
- 删除 `env_vars.ROCK_ADMIN_ENV = args.env` 与 `env_vars.ROCK_ADMIN_ROLE = args.role` 两行(env 由 `main()` 写,模块属性赋值对 `__getattr__` 模块无效)
- 所有 `if args.role == "admin":` / `else:` → `if env_vars.ROCK_ADMIN_ROLE == "admin":`
- `if args.env in ["local", "test", "dev"]` → `if env_vars.ROCK_ADMIN_ENV in ["local", "test", "dev"]`

- [ ] **Step 5: 实现 — 抽 `_include_routers` + `create_app`,移动 app 级注册**

把模块级的 `app = FastAPI(lifespan=lifespan)`、CORS、`add_exception_handler`、`@app.exception_handler`、`@app.get("/")`、`@app.middleware("http")` 全部移入工厂。做法:

1. 把现有 `@app.exception_handler(Exception)` 的 `base_exception_handler`、`@app.get("/")` 的 `root`、`@app.middleware("http")` 的 `log_requests_and_responses` 改成**不带装饰器的普通 async 函数**(函数体不变)。
2. 新增:

```python
def _include_routers(app: FastAPI, role: str) -> None:
    if role == "admin":
        app.include_router(sandbox_router, prefix="/apis/envs/sandbox/v1", tags=["sandbox"])
        app.include_router(admin_ops_router, prefix="/apis/envs/sandbox/v1/ops", tags=["admin-ops"])
    else:
        app.include_router(sandbox_proxy_router, prefix="/apis/envs/sandbox/v1", tags=["sandbox"])
    app.include_router(warmup_router, prefix="/apis/envs/sandbox/v1", tags=["warmup"])
    app.include_router(gem_router, prefix="/apis/v1/envs/gem", tags=["gem"])


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    app.add_exception_handler(Exception, base_exception_handler)
    app.middleware("http")(log_requests_and_responses)
    app.add_api_route("/", root, methods=["GET"])
    _include_routers(app, role=env_vars.ROCK_ADMIN_ROLE)
    return app
```

> 注意:`base_exception_handler(request, exc)`、`root()`、`log_requests_and_responses(request, call_next)` 现在是普通函数,签名保持不变。

- [ ] **Step 6: 实现 — `main()` 写 env、解析 worker、起多 worker**

`main()` 改为:

```python
def main():
    import os

    from rock.admin.worker_config import resolve_workers

    args = _parse_args()
    os.environ["ROCK_ADMIN_ENV"] = args.env
    os.environ["ROCK_ADMIN_ROLE"] = args.role

    workers = resolve_workers(
        role=args.role,
        override=args.workers,
        env_workers=int(os.getenv("ROCK_PROXY_WORKERS", "0")),
        cpu_count=os.cpu_count(),
    )
    # write the *resolved* count back so each worker's lifespan sizes pools deterministically
    os.environ["ROCK_PROXY_WORKERS"] = str(workers)
    logger.info(f"starting role={args.role} port={args.port} workers={workers}")

    uvicorn.run(
        "rock.admin.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=args.port,
        workers=workers,
        ws_ping_interval=None,
        ws_ping_timeout=None,
        timeout_keep_alive=30,
    )
```

删除旧 `main()` 里所有 `app.include_router(...)` 行(已移入 `_include_routers`)与旧 `uvicorn.run(app, ...)`。删除模块级 `app = FastAPI(...)` 及其下 CORS/handler 装饰器块(已移入 `create_app`)。

- [ ] **Step 7: 运行测试确认通过**

Run: `uv run pytest tests/unit/admin/test_create_app_routes.py -v`
Expected: PASS

- [ ] **Step 8: 冒烟 — 工厂可导入、单 worker 解析正确**

Run: `uv run python -c "import os; os.environ['ROCK_ADMIN_ROLE']='proxy'; from rock.admin.main import create_app; a=create_app(); print('routes', len(a.routes))"`
Expected: 打印 routes 数量,无异常。

- [ ] **Step 9: 提交**

```bash
git add rock/admin/main.py tests/unit/admin/test_create_app_routes.py
git commit -m "refactor(admin): app-factory + uvicorn multi-worker for proxy role"
```

---

## Task 5: lifespan 按 worker 算 DB 池 + proxy 退出 aclose

**Files:**
- Modify: `rock/admin/main.py`(`lifespan` 内 db_provider 构建、teardown)

- [ ] **Step 1: 实现 — 按 worker 计算 pool_size**

`lifespan` 内,把:

```python
    db_provider = DatabaseProvider(db_config=DatabaseConfig(url=db_url))
```

改为:

```python
    from rock.admin.worker_config import compute_pool_size

    workers = int(os.getenv("ROCK_PROXY_WORKERS", "1")) or 1
    effective_pool = compute_pool_size(base=rock_config.database.pool_size, workers=workers)
    db_provider = DatabaseProvider(db_config=DatabaseConfig(url=db_url, pool_size=effective_pool))
    logger.info(f"db pool_size={effective_pool} (base={rock_config.database.pool_size}, workers={workers})")
```

> 文件顶部确保 `import os` 存在(已有 `import asyncio` 等;若无 `os` 则在导入区加 `import os`)。
> 说明:admin role 时 `main()` 写入的 `ROCK_PROXY_WORKERS=1` → `compute_pool_size` 返回 base,行为不变。

- [ ] **Step 2: 实现 — proxy 退出时 aclose service 的 httpx clients**

`lifespan` 的 `else:` 分支(proxy)里保存引用:

```python
    else:
        sandbox_manager = SandboxProxyService(rock_config=rock_config, meta_store=meta_store)
        set_sandbox_proxy_service(sandbox_manager)
        proxy_service_ref = sandbox_manager
```

在 `yield` 之前先 `proxy_service_ref = None`(admin 分支默认);在 `yield` 之后的 teardown 段(`if db_provider:` 之前)加:

```python
    if proxy_service_ref is not None:
        await proxy_service_ref.aclose()
        logger.info("proxy httpx clients closed")
```

> `aclose()` 在 Task 6 实现;本步骤先接好调用点。两任务在同会话顺序执行,Task 6 紧随其后即可。

- [ ] **Step 3: 运行相关测试(确保未破坏既有 admin 测试)**

Run: `uv run pytest tests/unit/admin -v -m "not need_ray and not need_admin and not need_admin_and_network" --reruns 1`
Expected: PASS（已存在的 admin 单测 + 本计划新增的 admin 单测)

- [ ] **Step 4: 提交**

```bash
git add rock/admin/main.py
git commit -m "feat(admin): size DB pool per-worker and aclose proxy clients on shutdown"
```

---

## Task 6: `SandboxProxyService` 拆两个 httpx client + aclose + Metrics pid 标

**Files:**
- Modify: `rock/sandbox/service/sandbox_proxy_service.py`(`__init__` `:54-95`、`_send_request` `:659`)
- Test: `tests/unit/sandbox/test_proxy_httpx_reuse.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/sandbox/test_proxy_httpx_reuse.py`:

```python
import os

import httpx
import pytest


@pytest.mark.asyncio
async def test_service_has_two_distinct_httpx_clients(sandbox_proxy_service):
    svc = sandbox_proxy_service
    assert isinstance(svc._rpc_client, httpx.AsyncClient)
    assert isinstance(svc._proxy_client, httpx.AsyncClient)
    assert svc._rpc_client is not svc._proxy_client


@pytest.mark.asyncio
async def test_metrics_monitor_has_worker_pid_tag(sandbox_proxy_service):
    tags = sandbox_proxy_service.metrics_monitor.user_defined_tags
    assert tags.get("worker_pid") == str(os.getpid())


@pytest.mark.asyncio
async def test_aclose_closes_both_clients(sandbox_proxy_service):
    svc = sandbox_proxy_service
    await svc.aclose()
    assert svc._rpc_client.is_closed
    assert svc._proxy_client.is_closed
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/sandbox/test_proxy_httpx_reuse.py -v`
Expected: FAIL（`AttributeError: ... '_rpc_client'`)

- [ ] **Step 3: 实现 — `__init__` 拆 client + pid 标**

`rock/sandbox/service/sandbox_proxy_service.py`:

文件顶部导入区加 `import os`(若无)。

把类属性 `_httpx_client = None`(`:52`)删除。

`__init__` 中,把 MetricsMonitor 构建改为带 pid 标:

```python
        self.metrics_monitor = MetricsMonitor.create(
            export_interval_millis=20_000,
            metrics_endpoint=rock_config.runtime.metrics_endpoint,
            user_defined_tags={
                **(rock_config.runtime.user_defined_tags or {}),
                "worker_pid": str(os.getpid()),
            },
        )
```

把原来单个 `self._httpx_client = httpx.AsyncClient(...)` 块(`:66-72`)替换为两个 client:

```python
        # Control-plane RPC client: short JSON calls to rocklet.
        self._rpc_client = httpx.AsyncClient(
            timeout=self.proxy_config.timeout,
            limits=httpx.Limits(
                max_connections=self.proxy_config.max_connections,
                max_keepalive_connections=self.proxy_config.max_keepalive_connections,
            ),
        )
        # Data-plane proxy client: streaming/SSE/large bodies. No total timeout;
        # per-request timeout is set via build_request(timeout=...). NEVER closed
        # per-request — lives for the process lifetime, closed in aclose().
        self._proxy_client = httpx.AsyncClient(
            timeout=httpx.Timeout(None),
            limits=httpx.Limits(
                max_connections=self.proxy_config.max_connections,
                max_keepalive_connections=self.proxy_config.max_keepalive_connections,
            ),
        )
```

- [ ] **Step 4: 实现 — `_send_request` 用 `_rpc_client`**

`_send_request` 内 `await self._httpx_client.request(...)`(`:659`)改为 `await self._rpc_client.request(...)`。

- [ ] **Step 5: 实现 — 新增 `aclose`**

在类内(`__init__` 之后任意位置)加:

```python
    async def aclose(self) -> None:
        """Close both shared httpx clients. Called on proxy shutdown."""
        await self._rpc_client.aclose()
        await self._proxy_client.aclose()
```

- [ ] **Step 6: 运行测试确认通过**

Run: `uv run pytest tests/unit/sandbox/test_proxy_httpx_reuse.py -v`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add rock/sandbox/service/sandbox_proxy_service.py tests/unit/sandbox/test_proxy_httpx_reuse.py
git commit -m "feat(proxy): split rpc/proxy httpx clients, add aclose, tag metrics with pid"
```

---

## Task 7: `http_proxy` 复用 `_proxy_client`(不关闭)

**Files:**
- Modify: `rock/sandbox/service/sandbox_proxy_service.py`(`http_proxy` `:901-1019`)
- Test: `tests/unit/sandbox/test_proxy_httpx_reuse.py`(追加)

- [ ] **Step 1: 写失败测试**

在 `tests/unit/sandbox/test_proxy_httpx_reuse.py` 追加:

```python
from unittest.mock import AsyncMock

import httpx
import pytest

from rock.deployments.constants import Port
from starlette.datastructures import Headers


def _fake_status():
    return {"host_ip": "1.2.3.4", "port_mapping": {Port.SERVER.value: 18080}}


@pytest.mark.asyncio
async def test_http_proxy_reuses_proxy_client_without_closing(sandbox_proxy_service, monkeypatch):
    svc = sandbox_proxy_service

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    svc._proxy_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(svc, "_update_expire_time", AsyncMock())
    monkeypatch.setattr(svc, "get_service_status", AsyncMock(return_value=[_fake_status()]))

    resp = await svc.http_proxy(
        sandbox_id="sb1",
        target_path="hello",
        body=None,
        headers=Headers({}),
        method="GET",
    )

    assert resp.status_code == 200
    # shared client must remain open for reuse
    assert not svc._proxy_client.is_closed
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/sandbox/test_proxy_httpx_reuse.py::test_http_proxy_reuses_proxy_client_without_closing -v`
Expected: FAIL（当前实现新建本地 client 并在 finally `await client.aclose()`,不会复用 `_proxy_client`;且本地 client 会被关闭——断言 `_proxy_client` 未关闭可能"假通过",所以下一步实现后这条才真正验证复用。若失败信息为本地 client 行为,继续实现)

> 注:这条测试的核心是实现后断言 `_proxy_client` 真正被用到且未被关闭。失败阶段主要确保测试能跑(无导入/构造错误)。

- [ ] **Step 3: 实现 — 复用 `_proxy_client`,移除本地 client 创建与关闭**

`http_proxy` 内:

删除:

```python
        client = httpx.AsyncClient(timeout=httpx.Timeout(None))

        try:
            resp = await client.send(
                client.build_request(
                    method=method,
                    url=target_url,
                    headers=request_headers,
                    timeout=120,
                    **request_kwargs,
                ),
                stream=True,
            )
        except Exception:
            await client.aclose()
            raise
```

替换为:

```python
        try:
            resp = await self._proxy_client.send(
                self._proxy_client.build_request(
                    method=method,
                    url=target_url,
                    headers=request_headers,
                    timeout=120,
                    **request_kwargs,
                ),
                stream=True,
            )
        except Exception:
            raise
```

SSE 分支的 `event_stream()` 的 `finally`:把

```python
                finally:
                    await resp.aclose()
                    await client.aclose()
```

改为(只关 response,不关共享 client):

```python
                finally:
                    await resp.aclose()
```

非流式分支末尾的 `finally`:把

```python
        finally:
            await resp.aclose()
            await client.aclose()
```

改为:

```python
        finally:
            await resp.aclose()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/unit/sandbox/test_proxy_httpx_reuse.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add rock/sandbox/service/sandbox_proxy_service.py tests/unit/sandbox/test_proxy_httpx_reuse.py
git commit -m "perf(proxy): reuse shared _proxy_client in http_proxy (no per-request client)"
```

---

## Task 8: `host_proxy` 复用 `_proxy_client`(不关闭)

**Files:**
- Modify: `rock/sandbox/service/sandbox_proxy_service.py`(`host_proxy` `:843-899`)
- Test: `tests/unit/sandbox/test_proxy_httpx_reuse.py`(追加)

- [ ] **Step 1: 写失败测试**

追加:

```python
@pytest.mark.asyncio
async def test_host_proxy_reuses_proxy_client_without_closing(sandbox_proxy_service):
    svc = sandbox_proxy_service

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"pong": True})

    svc._proxy_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    resp = await svc.host_proxy(
        host_ip="10.0.0.1",
        target_path="ping",
        body={"a": 1},
        headers=Headers({}),
    )

    assert resp.status_code == 200
    assert not svc._proxy_client.is_closed
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/sandbox/test_proxy_httpx_reuse.py::test_host_proxy_reuses_proxy_client_without_closing -v`
Expected: FAIL（当前 `host_proxy` 用 `async with httpx.AsyncClient(...)` 本地 client,不会用 `_proxy_client`;实现后此条验证复用)

- [ ] **Step 3: 实现 — 复用 `_proxy_client`**

`host_proxy` 内,把:

```python
        async with httpx.AsyncClient(timeout=httpx.Timeout(90)) as http_client:
            try:
                resp = await http_client.post(
                    url=target_url,
                    json=payload,
                    headers=request_headers,
                )
            except httpx.RequestError as exc:
                logger.error(f"Error forwarding request to {target_url}: {exc}", exc_info=True)
                raise Exception(f"Service unavailable: Rocklet at {host_ip}:{Port.PROXY.value} is not reachable.")

            content_type = resp.headers.get("content-type", "")
            response_headers = filter_headers(resp.headers)

            if "application/json" in content_type:
                return JSONResponse(
                    status_code=resp.status_code,
                    content=resp.json(),
                    headers=response_headers,
                )

            return Response(
                status_code=resp.status_code,
                content=resp.content,
                media_type=content_type or "application/octet-stream",
                headers=response_headers,
            )
```

替换为(去掉 `async with`,改用共享 client + 每请求 timeout=90;不关闭):

```python
        try:
            resp = await self._proxy_client.post(
                url=target_url,
                json=payload,
                headers=request_headers,
                timeout=90,
            )
        except httpx.RequestError as exc:
            logger.error(f"Error forwarding request to {target_url}: {exc}", exc_info=True)
            raise Exception(f"Service unavailable: Rocklet at {host_ip}:{Port.PROXY.value} is not reachable.")

        content_type = resp.headers.get("content-type", "")
        response_headers = filter_headers(resp.headers)

        if "application/json" in content_type:
            return JSONResponse(
                status_code=resp.status_code,
                content=resp.json(),
                headers=response_headers,
            )

        return Response(
            status_code=resp.status_code,
            content=resp.content,
            media_type=content_type or "application/octet-stream",
            headers=response_headers,
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/unit/sandbox/test_proxy_httpx_reuse.py -v`
Expected: PASS（全部)

- [ ] **Step 5: 提交**

```bash
git add rock/sandbox/service/sandbox_proxy_service.py tests/unit/sandbox/test_proxy_httpx_reuse.py
git commit -m "perf(proxy): reuse shared _proxy_client in host_proxy"
```

---

## Task 9: 回归 + lint + 容量预算文档落地

**Files:**
- Modify: `docs/_specs/proxy-multicore/02_design.md`(补一行"实现状态")

- [ ] **Step 1: 跑相关单测全集**

Run:
```bash
uv run pytest tests/unit/admin tests/unit/sandbox -m "not need_ray and not need_admin and not need_admin_and_network" --reruns 1
```
Expected: PASS（无 regression)

- [ ] **Step 2: lint + format**

Run:
```bash
uv run ruff check --fix rock/ tests/unit/admin tests/unit/sandbox
uv run ruff format rock/admin/main.py rock/admin/worker_config.py rock/sandbox/service/sandbox_proxy_service.py rock/config.py rock/env_vars.py rock/admin/core/db_provider.py
```
Expected: 无错误

- [ ] **Step 3: 文档补实现状态**

在 `docs/_specs/proxy-multicore/02_design.md` 末尾追加:

```markdown
---
## 实现状态(2026-06-22)

已实现:env `ROCK_PROXY_WORKERS`;`DatabaseConfig.pool_size` 可配;`resolve_workers`/`compute_pool_size` 纯函数;`main.py` app-factory + 多 worker(仅 proxy);lifespan 按 worker 算池 + proxy 退出 aclose;`SandboxProxyService` 拆 `_rpc_client`/`_proxy_client` + pid 标 + `http_proxy`/`host_proxy` 复用。
```

- [ ] **Step 4: 提交**

```bash
git add docs/_specs/proxy-multicore/02_design.md
git commit -m "docs(spec): mark proxy-multicore implementation status"
```

---

## 部署侧后续(本计划范围外,交付说明)

- 生产启动命令需带 worker 数:`admin --role proxy --port 8080 --env <env> --workers <N>` 或设 `ROCK_PROXY_WORKERS`。不设则默认 `os.cpu_count()`。
- 上线前按 `02_design.md` 第 4 节"容量预算"填数:`pool_size × workers × pods ≤ PG_max_connections`;worker 上限 `min(cpu_count, 可用内存 / 单进程RSS)`,内存以 `smaps_rollup` 实测校准。
- k8s 资源:Pod 内存需 ≥ `workers × 单进程RSS`,否则会 OOM。

---

## Self-Review 检查记录

- **Spec 覆盖**:多 worker(T1/T3/T4)、env 传参(T4)、仅 proxy 多进程(T3 `resolve_workers`)、DB 池可配+按 worker 缩(T2/T3/T5)、Metrics pid 标(T6)、httpx 拆池复用(T6/T7/T8)、aclose 生命周期(T5/T6)、容量预算文档(T9)——均有任务对应。
- **Placeholder 扫描**:无 TBD/TODO;每个代码步骤含完整代码。
- **类型/命名一致**:`resolve_workers`/`compute_pool_size` 跨 T3/T4/T5 签名一致;`_rpc_client`/`_proxy_client`/`aclose` 跨 T6/T7/T8 一致;`_include_routers`/`create_app` 跨 T4 测试与实现一致。
