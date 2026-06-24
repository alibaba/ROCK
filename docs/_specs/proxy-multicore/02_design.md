# sandbox_proxy_router 多核化 — 设计

> 配套 `01_requirement.md`。本文给出四部分设计 + 容量预算 + 测试策略。

## 第 1 部分:多 worker 启动(app-factory + env 传参)

改造 `rock/admin/main.py`:

```python
# 顶层不再有 args = parse_args()(消除 import 期对 argv 的依赖)

def create_app() -> FastAPI:
    """工厂:每个 worker 子进程都会调用,只读 env,不碰 argv。"""
    role = env_vars.ROCK_ADMIN_ROLE        # 由主进程写入 env
    env  = env_vars.ROCK_ADMIN_ENV
    app = FastAPI(lifespan=lifespan)
    # CORS / 异常处理器 / 访问日志中间件 / include_router(按 role) 全部在此
    if role == "admin":
        app.include_router(sandbox_router, prefix="/apis/envs/sandbox/v1", tags=["sandbox"])
        app.include_router(admin_ops_router, prefix="/apis/envs/sandbox/v1/ops", tags=["admin-ops"])
    else:
        app.include_router(sandbox_proxy_router, prefix="/apis/envs/sandbox/v1", tags=["sandbox"])
    app.include_router(warmup_router, ...)
    app.include_router(gem_router, ...)
    return app

def main():
    args = _parse_args()                   # 仅主进程
    os.environ["ROCK_ADMIN_ROLE"] = args.role
    os.environ["ROCK_ADMIN_ENV"]  = args.env
    workers = resolve_workers(args.role, args.workers, int(os.getenv("ROCK_PROXY_WORKERS", "0")))
    uvicorn.run(
        "rock.admin.main:create_app", factory=True,
        host="0.0.0.0", port=args.port, workers=workers,
        ws_ping_interval=None, ws_ping_timeout=None, timeout_keep_alive=30,
    )

# rock/utils/worker.py(纯函数 util,无 I/O)
SINGLE_WORKER_ENVS = frozenset({"local", "test", "dev"})

def resolve_workers(role, override, env_workers, env=None) -> int:
    if role != "proxy":
        return 1                           # admin 恒单进程(scheduler/Ray 单例)
    if env in SINGLE_WORKER_ENVS:
        return 1                           # local/test/dev 强制单进程(fakeredis/in-mem 状态进程私有,多 worker 会不共享);优先级高于 override
    if override and override > 0:
        return override
    if env_workers and env_workers > 0:
        return env_workers
    return 1                               # 必须显式设置;不做 cpu_count 自动探测
```

要点:
- `resolve_workers` / `compute_pool_size` 放在 `rock/utils/worker.py`(纯函数 util,可单测)。
- `lifespan` 内所有 `args.env/args.role` 改读 `env_vars.ROCK_ADMIN_ENV/ROCK_ADMIN_ROLE`。
- 新增 CLI `--workers`(可选,覆盖 env)。
- `env_vars.py` 新增懒加载默认:`ROCK_PROXY_WORKERS`(默认 `0`)。**worker 数必须显式设置**(`--workers` 或 `ROCK_PROXY_WORKERS`);两者都未设则单 worker(=1),不按 cpu_count 自动探测。
- 运维侧建议 worker 数 `≤ min(物理核数, 可用内存/单进程RSS)`(见容量预算),由运维显式决定而非进程自选。

## 第 2 部分:连接池 / Metrics 治理(必要正确性)

每个 worker 各跑一遍 `lifespan` → 各自独立的 Redis 池、DB 池、httpx 池、MetricsMonitor。收口:

### 2.1 DB 池可配 + 按 worker 缩小

- `DatabaseConfig` 新增 `pool_size`(env 可覆盖),`db_provider.init()` 不再硬编码 100。
- proxy role 实际生效值:`pool_size = max(2, base // workers)`,`base` 默认 100。
  - 整除兜底,给下限 2,避免 worker 很大时算出 0/1。
  - 按 Pod 维度,`workers × (base//workers) ≈ base`,**对 PG 的压力与现状单进程一致,不退化**。
  - proxy 几乎只读 metadata,生产可在此基础上进一步下调。
- admin role 保持单 worker → `pool_size = base`(=100,不变)。

### 2.2 Redis 池

同理可配、按需调小;Redis 连接成本低(每条几 KB),优先级低于 DB。

### 2.3 Metrics 多进程打标

多 worker 用相同 `user_defined_tags` 上报会互相覆盖/串味。`create_app()`/`lifespan` 构建 `MetricsMonitor` 时注入 `worker_pid`(`os.getpid()`)标签,使各 worker 指标可区分(或交由后端按 tag 求和聚合)。

### 2.4 日志文件并发(部署清空 + 各 worker append)

现状 `init_file_handler` 用 `mode="w+"`:多 worker 下每个进程启动都 **truncate 同一文件**、各持独立 offset 互相覆盖 → 日志错乱。修法是把"清空"从 FileHandler(每进程各清、会打架)挪到**部署时只清一次**,之后所有 worker 各自 append:

- `init_file_handler` 模式由新 env `ROCK_LOGGING_APPEND` 决定:置位 → `"a"`(append);**默认 `"w+"` 不变**,故 rocklet / cli 等单进程服务行为不受影响。
- admin/proxy `main()`(master,spawn worker 之前):先 `reset_log_file()` 清空一次,再 `os.environ["ROCK_LOGGING_APPEND"]="true"`;worker 继承 env → 全部 append。
- 安全性:master 在 import 期不写日志,清空发生在写入之前,不会在已有 writer offset>0 时 truncate 产生空洞。
- 残留边界(本地盘 + 正常行可忽略):单条 > BufferedWriter 缓冲(~8KB)的日志(如访问日志 dump 大 body)在多进程 append 下可能交错;NFS 不保证 O_APPEND 原子。需彻底免疫则改 per-pid 文件名(本期不做,需求方仅要"清空一次 + 各 pid append")。

## 第 3 部分:httpx 连接池复用(选定优化)

### 现状

- `_send_request`(控制面 JSON RPC)已用池化 `self._httpx_client` ✅。
- `http_proxy`(`sandbox_proxy_service.py:951`)、`host_proxy`(`:873`)**每请求新建 `AsyncClient`** ❌ → 反复 TCP+TLS 握手、无 keepalive 复用。

### 改造:拆两个共享 client

避免数据面长流(SSE/大响应)阻塞控制面短 RPC:

| client | 用途 | 超时 | 池 |
|--------|------|------|-----|
| `_rpc_client` | `_send_request` 控制面 | 短(`proxy_config.timeout`) | 小 |
| `_proxy_client` | `http_proxy` / `host_proxy` 数据面(含 SSE 流式) | 读超时放宽/无总超时 | 大 |

两者均在 `__init__` 创建、随进程存活,在服务关闭时统一 `aclose()`。

### 复用正确性要点

- 流式:`resp = await self._proxy_client.send(req, stream=True)`;生成器 `finally` **只 `await resp.aclose()`,绝不关闭共享 client**。
- 非流式:`aread()` 后 `resp.aclose()`;同样不关 client。
- **每请求超时**经 `build_request(timeout=...)` 覆盖,保留现有语义:SSE 无总超时 / 普通 120s / host_proxy 90s。
- 池上限 `max_connections`/`max_keepalive` 经 `ProxyServiceConfig` 可配;长连接占池槽会形成期望中的背压,需按并发量设值。
- WebSocket 走 `websockets.connect`,不受影响。

## 第 4 部分:容量预算(capacity budget)

上线前按本节填数,定 worker 数与各池大小。

### 4.1 连接数(可精确控制)

```
PG 总连接 = pool_size × workers × pod_数
约束:    ≤ PG_max_connections(留余量给 admin/其他)
默认:    pool_size = max(2, 100 // workers) → 每 Pod ≈ 100,与现状一致
```

例:PG `max_connections=500`,留一半给其他,proxy 预算 250;`250 / (8 worker × 3 pod) ≈ 10`,设 proxy `pool_size=10`。

### 4.2 内存(需实测校准)

- **worker 进程内存**:每个 worker 是完整 Python 进程(spawn 下几乎不共享),粗估单进程 RSS 100–300MB。`N worker ≈ N × 单进程RSS`,通常是多 worker 的**主要内存成本**,决定 worker 上限:运维显式设 `workers ≤ min(物理核数, 可用内存 / 单进程RSS)`(代码不自动按 cpu_count 选)。
- **PG 每连接内存**:常被引用为 5–10MB/连接,但**此为经验估计,非本系统实测**;`top`/`ps` 的 RSS 因含共享内存(shared_buffers)会高估。上线前以私有内存实测校准:

```bash
# 每个 PG backend 的私有内存(PSS,比 RSS 准)
for pid in $(pgrep -f "postgres:.*"); do
  awk '/^Pss:/{s+=$2} END{printf "%.1f MB\n", s/1024}' /proc/$pid/smaps_rollup 2>/dev/null
done
# 连接现状
# SELECT count(*) FROM pg_stat_activity;   SHOW max_connections;
```

> 原则:连接数用硬公式精确控制;内存数字标注为经验估计、以实测为准,不拿估计值当拍 worker 数的依据。

## 第 5 部分:测试策略(TDD)

进程 spawn 本身难单测,把可测逻辑抽出:

1. **`create_app()` 按 role 返回正确路由集**:proxy 含 `sandbox_proxy_router`、不含 `sandbox_router`;admin 反之。
2. **`resolve_workers(role, override, env_workers, env)`**(`rock/utils/worker.py`):admin 恒 1;**local/test/dev 恒 1(优先级高于 override,避免 fakeredis/in-mem 状态跨 worker 不共享)**;proxy 其余走 override>env>1;无 cpu_count 自动探测。
3. **DB 池公式**:`max(2, base // workers)` 的边界(workers 极大 → 2;admin → base)。
4. **httpx 复用**:调用 `http_proxy`/`host_proxy` 后断言**共享 client 未关闭、仍可用**;数据面/控制面使用各自共享实例(可注入 mock client)。
5. **SSE 流式**:断言生成器结束只 `resp.aclose()`,不 close client。

CI 标记:大多为快测;涉及真实转发的归 `integration`。

---
## 实现状态(2026-06-22)

已实现并通过 627 项 admin+sandbox 单测:env `ROCK_PROXY_WORKERS`;`DatabaseConfig.pool_size` 可配;`resolve_workers`/`compute_pool_size` 纯函数;`main.py` app-factory + 多 worker(仅 proxy role);lifespan 按 worker 算池 + proxy 退出 `aclose`;`SandboxProxyService` 拆 `_rpc_client`/`_proxy_client` + `worker_pid` Metrics 标 + `http_proxy`/`host_proxy` 复用共享 client(不关闭)。

实现期连带修复:`tests/unit/sandbox/test_proxy_enhancements.py` 原先 patch `httpx.AsyncClient` 构造器,因 `http_proxy` 不再每请求建连而改为注入 `service._proxy_client`。
