# sandbox_proxy_router 多核化 — 需求与决策

> 日期:2026-06-22 · 状态:设计已确认,待出实施计划

## 1. 背景

`sandbox_proxy_router`(`admin --role proxy`)当前通过 `rock/admin/main.py:310` 的 `uvicorn.run(app, ...)` 以**单进程、单事件循环**运行,在多核机器上只能吃满一个核心。proxy role 是数据/控制面的转发层,职责为:

- 把 HTTP / WebSocket / SSE 请求转发到 sandbox(rocklet);
- 从 Redis/DB(`meta_store`)读取 sandbox metadata;
- 生成 OSS STS token。

proxy 进程内**无跨请求会话状态**(bash session 在 rocklet;WebSocket 连接天然绑定单连接),因此适合多进程横向扩展。

## 2. 目标

1. 让 `sandbox_proxy_router` 在单 Pod 内充分利用多核心(吞吐随核数近似线性提升)。
2. 复用 httpx 连接池,消除转发热路径上的每请求建连开销。
3. 在不打爆后端(PostgreSQL)、不失控内存的前提下安全多进程化。

## 3. 关键决策(已与需求方确认)

| 决策项 | 选择 | 理由 |
|--------|------|------|
| 扩展方式 | **单 Pod 内多进程**(进程内扩展) | 直接吃满本机核心,不依赖 k8s 扩副本 |
| 进程管理 | **uvicorn 原生 `--workers`** | 改动最小,内置 master 管理子进程,原生支持 ASGI/WebSocket |
| 多进程范围 | **仅 proxy role**;admin role 恒 `workers=1` | admin 持有 scheduler 线程(`is_primary_pod()` 为 Pod 级)与 Ray/单例,多进程会重复调度 |
| 优化范围 | **httpx 连接池复用** | 修复 `http_proxy`/`host_proxy` 每请求新建 `AsyncClient` |
| 连接池/Metrics 治理 | 作为多进程化的**必要正确性项**纳入 | 多 worker 必然放大连接数、Metrics 标签冲突 |

**明确不做(本期)**:访问日志中间件瘦身、sandbox 状态查询缓存(需求方未勾选)。但留观测点——多 worker 后日志中间件的 `await request.json()+json.dumps(indent=2)` CPU 开销会在每个核各付一份,若压测发现单核仍被日志吃满需回头处理。

## 4. 约束与硬前提

### 4.1 argv 不可跨 worker(为什么必须改 env 传参)

`uvicorn.run("module:app", workers=N)` 会 fork/spawn 出 N 个 worker 子进程,**每个子进程重新 import 模块构建 app**。当前 `main.py` 顶层 `args = parser.parse_args()` 在 import 期执行,而 worker 子进程(尤其 spawn 模式)的 `sys.argv` **不是**启动命令的 argv,会退回 argparse 默认值(`role=admin`!)甚至 `SystemExit`。

→ 必须改为:**主进程 `main()` 解析 argv 并写入 env;`create_app()`/`lifespan`(每 worker 执行)只读 env**。顶层 `parse_args()` 必须删除。env 是进程继承属性,fork/spawn 都能正确继承。

### 4.2 连接池不能跨进程共享

Redis/DB 连接池 = TCP socket + 协议状态机 + 绑定的 event loop,三者都绑死单进程单 loop:同一条连接被多进程并发读写会导致协议帧错乱;asyncio 连接对象持有当前 loop 引用,跨进程 loop 无法驱动;spawn 下连 fd 都不继承。

→ **每个 worker 必须各自创建 Redis/DB/httpx 池**。共享的是后端服务实例本身(同一个 Redis / 同一个 PG),不是连接对象。能做的是**调小单池 + 文档写明总量预算**。

### 4.3 当前 DB 池对多 worker 是危险默认

`rock/admin/core/db_provider.py:43-45`(仅 PostgreSQL 生效,硬编码、不可配):

```python
pool_size = 100      # 每进程最多 100 条常驻连接
max_overflow = 0     # 100 为硬上限
pool_timeout = 120
```

乘法风险:`8 worker × 100 = 800`、`3 pod × 8 worker × 100 = 2400`,远超 PG 默认 `max_connections=100`。**第一个倒下的不是 CPU,而是 PostgreSQL**。

→ 多 worker 上线的**前置硬条件**:`pool_size` 改为可配,并按 `pool_size × workers × pods ≤ PG_max_connections` 反推。proxy 对 DB 基本只读 metadata、热路径走 Redis,可设很小。

## 5. 成功标准

- proxy role 以 `workers=N` 启动,N 个进程均分负载(SO_REUSEPORT/uvicorn master 分发),压测吞吐随 worker 数近似线性提升至核数上限。
- worker 子进程构建出**正确的 role 路由集合**(不会因 argv 丢失退回 admin)。
- admin role 仍为单进程,scheduler 不重复启动。
- `http_proxy`/`host_proxy` 复用共享 httpx client,不再每请求建连;共享 client 生命周期=进程,流式响应只关 response 不关 client。
- 单 Pod PG 连接总量 ≤ 现状(`pool_size = max(2, base // workers)` 保证不退化)。
