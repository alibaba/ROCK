# 实施计划：Rock 新增 OpenSandboxOperator（方案 B）

> 目标：把 OpenSandbox 作为 Rock 的一种后端。**方案 B**——生命周期（起停/状态）与命令执行/文件操作**全部委托** OpenSandbox，通过 OpenSandbox 官方 **Python SDK** 对接。全程 TDD。

## 背景与两个改造面

Rock 内部有两条独立的路径，方案 B 都要改：

1. **生命周期 seam（Operator 层）** —— `AbstractOperator`（[rock/sandbox/operator/abstract.py](../../rock/sandbox/operator/abstract.py)）的 submit / get_status / stop / restart / delete，由 `OperatorFactory`（[factory.py:57](../../rock/sandbox/operator/factory.py)）按 `runtime.operator_type` 分发。这一层是**纯加法**，照 `K8sOperator`（委托外部 provider、无本地 Ray actor）的形状即可。

2. **执行/文件 seam（Proxy 层）** —— 用户的 `execute`/`read_file`/`write_file`/`create_session`/`run_in_session`/`upload`/websocket portforward 全部经 `SandboxManager` 转发到 `SandboxProxyService`（[sandbox_proxy_service.py](../../rock/sandbox/service/sandbox_proxy_service.py)）。其 `_send_request(sandbox_id, status_dict, action, ...)` **写死了 rocklet 的 HTTP 协议**（从 `status_dict` 取 `host_ip`+`port_mapping` 拼 URL 调 rocklet）。方案 B 的核心工作量在这里：需要抽象出「后端 Runtime」接口，新增 OpenSandbox SDK 实现，按 sandbox 所属后端路由。

> **关键判断**：方案 B 与方案 A 的唯一差别就是第 2 面。如果只做第 1 面（Operator）而不改 Proxy，那 OpenSandbox 容器里必须自带 rocklet——那是方案 A。方案 B 必须同时改造 Proxy seam。

## 前置调研（Phase 0，✅ 已完成）

> 产出见 [opensandbox-sdk-contract.md](opensandbox-sdk-contract.md)。**结论：OpenSandbox Python SDK 覆盖方案 B 所需全部能力**（生命周期/命令/文件/bash session/后台进程/端口 endpoint/connect-by-id/原生 async）。原"能力缺口"大风险已解除，收敛为 9 条明确工程决策，其中仅 **stop/restart 生命周期语义**需产品拍板。

在写任何代码前，先把 OpenSandbox Python SDK 的真实契约固化成文档，避免基于臆测实现。

- [ ] 拉取 OpenSandbox 仓库（`opensandbox-group/OpenSandbox`）与其 Python SDK，确认：
  - 创建：`Sandbox.create(image, timeout=...)` 的完整入参（cpu/memory/env/labels/network 等）与返回对象（sandbox_id、连接信息）。
  - 执行：`sandbox.commands.run(cmd, ...)` 的入参/返回（stdout/stderr/exit_code/超时/是否支持后台 PID、session）。
  - 文件：`sandbox.files.read_file(path)` / `write_files([WriteEntry(...)])` 的签名与二进制/大文件支持。
  - 会话：是否有 bash session / 交互式流（对应 Rock 的 `create_session`/`run_in_session`/`close_session`）。
  - 端口转发：是否支持 portforward / expose port（对应 Rock 的 websocket `_get_rocklet_portforward_url`）。
  - 生命周期：`sandbox.kill()` / 查询状态 / 通过已有 sandbox_id 重新 attach（get by id）。
  - 鉴权与连接：endpoint、protocol、api_key 的配置方式；SDK 是否 async。
- [ ] 产出 `docs/plans/opensandbox-sdk-contract.md`，逐一映射到 Rock 的数据模型（`Command`/`CommandResponse`、`ReadFileRequest`/`ReadFileResponse`、`SandboxInfo`）。**能力缺口在此显式记录**（如 OpenSandbox 无 bash session → 对应操作返回 NotImplemented / 降级）。
- [ ] 决定依赖引入方式：`opensandbox` SDK 作为可选依赖（`pyproject.toml` 的 optional extra，如 `[project.optional-dependencies] opensandbox = [...]`），避免污染核心安装。

**验收**：契约文档 review 通过；`uv add --optional opensandbox <pkg>` 可解析。

---

## Phase 1：生命周期 seam —— OpenSandboxOperator ✅ 已完成

> 提交：`feat(config): add OpenSandboxConfig`、`feat(operator): add OpenSandboxOperator lifecycle backend`。
> 20 个新单测全绿，操作+配置回归 187 passed，ruff 通过，client 调用已对照真实 `opensandbox==0.1.13` SDK 签名验证。Issue #1202。

### 1.1 配置（TDD）

- [ ] 测试 `tests/unit/test_config.py`：给定含 `opensandbox:` 段的 yaml，`RockConfig.from_env()` 能解析出 `OpenSandboxConfig`；`operator_type: opensandbox` 被 `RuntimeConfig` 接受。
- [ ] 实现 [rock/config.py](../../rock/config.py)：
  - 新增 `@dataclass OpenSandboxConfig`：`endpoint: str`、`api_key: str | None`、`protocol: str = "https"`、`runtime: str = "docker"`（docker/k8s）、`namespace: str = "rock"`、`default_timeout: int`、`extra: dict`。
  - `RockConfig` 增字段 `opensandbox: OpenSandboxConfig = field(default_factory=OpenSandboxConfig)`（[config.py:420](../../rock/config.py)）。
  - `from_env` 增 `if "opensandbox" in config: kwargs["opensandbox"] = OpenSandboxConfig(**config["opensandbox"])`（[config.py:466](../../rock/config.py)）。
  - `rock-conf/rock-*.yml` 增示例段（注释掉，默认走 ray）。

### 1.2 OpenSandbox 客户端封装（TDD）

- [ ] 测试 `tests/unit/sandbox/operator/test_opensandbox_client.py`：用 mock 的 OpenSandbox SDK，验证 create/get/kill 的入参映射与异常转译（OpenSandbox 异常 → Rock 的 `BadRequestRockError`/`InternalServerRockError`）。
- [ ] 实现 `rock/sandbox/operator/opensandbox/client.py`：`OpenSandboxClient`，薄封装官方 SDK；集中处理鉴权、超时、异常转译、`sandbox_id` 双向映射（Rock sandbox_id ↔ OpenSandbox sandbox handle）。async 接口。

### 1.3 Operator（TDD）

- [ ] 测试 `tests/unit/sandbox/operator/test_opensandbox_operator.py`（对照现有 K8sOperator 测法）：
  - `submit(DockerDeploymentConfig, user_info)` → 调 client.create，返回合法 `SandboxInfo`（含 sandbox_id、image、cpus、memory、state=PENDING，以及 host/连接信息塞进 `extended_params`）。
  - `get_status` → 存在返回 SandboxInfo，404 返回 None；与 redis 缓存 merge（复用 K8sOperator 的 `_merge_sandbox_info` 思路）。
  - `stop` → 默认明确不支持；OpenSandbox `pause` 需要创建时显式启用 persistence。
  - `restart` → 默认明确不支持；OpenSandbox `resume` 需要创建时显式启用 persistence。
  - `delete` → `sandbox.kill()`（Terminated → Rock `deleted`，不可逆）。
- [ ] 实现 `rock/sandbox/operator/opensandbox/operator.py`：`OpenSandboxOperator(AbstractOperator)`，委托 `OpenSandboxClient`；`set_redis_provider`/`set_nacos_provider` 复用基类。
- [ ] `rock/sandbox/operator/opensandbox/__init__.py` 导出。

### 1.4 工厂与装配（TDD）

- [ ] 测试补充 `test_operator_factory.py`：`operator_type="opensandbox"` 且缺 `opensandbox_config` 抛错；给足依赖返回 `OpenSandboxOperator`。
- [ ] 实现 [factory.py](../../rock/sandbox/operator/factory.py)：
  - `OperatorContext` 增 `opensandbox_config: OpenSandboxConfig | None = None`。
  - `create_operator` 增 `elif operator_type == "opensandbox":` 分支（校验依赖、set providers）。
  - 更新 else 报错文案的 supported types。
  - 增 import。
- [ ] [rock/admin/main.py:156](../../rock/admin/main.py)：`OperatorContext(...)` 传 `opensandbox_config=rock_config.opensandbox`。

**Phase 1 验收**：`operator_type: opensandbox` 下 admin 能起沙箱、查状态、停沙箱（用 mock SDK 的集成测试打通）；`uv run pytest -m "not need_ray and not need_admin"` 全绿；ruff 通过。

---

## Phase 2：执行/文件 seam —— OpenSandbox Runtime backend（方案 B 核心）

> **2026-07-13 交付状态**：本阶段已交付 `execute`、`read_file`、`write_file`、流式 `upload`、`get_status` 和 `is_alive`。OpenSandbox 沙箱通过 `extended_params["backend"]` 严格路由，不安装、不探测、也不回退到 rocklet/22555。session、portforward、scheduler gate、混合 operator 迁移和远程命令取消留待后续 PR。

### 2.1 抽象后端接口（TDD）

当前 `SandboxProxyService` 的 6 个操作（execute/read_file/write_file/upload/create_session/run_in_session/close_session）都走 `_send_request(...)` → rocklet HTTP；websocket 走 `_get_rocklet_portforward_url`。目标：把「和某个沙箱通信」抽象成后端接口。

- [x] 定义 `rock/sandbox/service/backends/base.py`：窄 `SandboxRuntimeBackend` Protocol，覆盖本期命令、文件、上传与状态接口。
  - `async execute(sandbox_id, status, Command) -> CommandResponse`
  - `async read_file / write_file / upload`
  - `async create_session / run_in_session / close_session`（Phase 0 若不支持则 raise `NotImplementedError` 并在 manager 层降级）
  - `async portforward(...)`（websocket；OpenSandbox 若无则明确不支持）
- [x] 把现有 rocklet 逻辑抽出为 `backends/rocklet.py::RockletBackend`（**保持行为不变**，纯重构）。

### 2.2 后端路由（TDD）

- [x] `SandboxInfo.extended_params["backend"] = "rocklet" | "opensandbox"` 作为路由依据。OpenSandbox operator 下缺失、未知或冲突 metadata 均 fail closed；只有非 OpenSandbox operator 的存量 sandbox 可缺省为 rocklet。
- [x] `SandboxProxyService` 按 metadata 选择 backend；OpenSandbox 路径不读取或伪造 `host_ip`/端口映射。

### 2.3 OpenSandbox backend（TDD）

- [x] 测试 `tests/unit/sandbox/service/backends/test_opensandbox_backend.py`（mock SDK）：
  - `execute` → `sandbox.commands.run(command.command, timeout=...)`，把结果映射成 `CommandResponse`（stdout/stderr/exit_code/失败语义对齐）。
  - `read_file`/`write_file` → `sandbox.files.read_file` / `write_files([WriteEntry])`，含二进制与 not-found。
  - session 类：按 Phase 0 结论实现或显式 NotImplemented。
- [x] 实现 `OpenSandboxBackend`：字符串命令按 OpenSandbox shell 语义执行（`shell=False` 仅记录不含命令正文的 warning），列表命令用 `shlex.join`；`check`、timeout 与输出映射保持 ROCK 语义。
- [x] 文件读取在 Admin 侧按 `encoding/errors` 解码；写入保留现有 mode，新文件用 `644`；上传将 `UploadFile.file` 直接交给 SDK 流式写入，不整文件读入内存。
- [x] `use_server_proxy` 严格遵循配置，不在直连和 server-proxy 间自动 fallback。

### 2.4 能力降级与边界

- [x] session 与 portforward 在路由边界返回清晰的 `BadRequestRockError`，且不会回退到 rocklet。
- [ ] `scheduler/tasks/*`（container/image/file cleanup 等）目前直接 `RemoteSandboxRuntime(ip)` 连 rocklet——这些是**worker 维度**的运维任务，仅在 ray/docker 后端有意义。确认 opensandbox 后端下这些定时任务应被跳过（按 operator_type gate），避免对 OpenSandbox 沙箱误发 rocklet 请求。

**本 PR 验收边界**：mock SDK 跑通 execute/read/write/upload/status，且 rocklet 回归测试通过。真实 OpenSandbox E2E 证据单独记录；scheduler、session、portforward、混合 operator 迁移与远程取消不计入本 PR 完成范围。

---

## Phase 3：SDK / 端到端 / 文档

- [ ] SDK 客户端（`rock/sdk/sandbox/client.py`）无需感知后端——它只对 admin 说话，天然透明。补一条针对 opensandbox 后端的集成测试（标 `need_admin`，用 mock SDK 或真实 OpenSandbox docker 后端）。
- [ ] 文档：用 `rock-docs` skill 加一篇「OpenSandbox 后端」使用说明（配置样例、能力矩阵、限制）。
- [ ] `pyproject.toml`：opensandbox extra；markers 若新增需在此注册。

## 涉及文件清单（速查）

**新增**
- `rock/sandbox/operator/opensandbox/{__init__,operator,client}.py`
- `rock/sandbox/service/backends/{base,rocklet,opensandbox}.py`
- 对应 `tests/unit/...` 测试
- `docs/plans/opensandbox-sdk-contract.md`、文档页

**修改**
- [rock/config.py](../../rock/config.py)（OpenSandboxConfig + RockConfig + from_env）
- [rock/sandbox/operator/factory.py](../../rock/sandbox/operator/factory.py)（OperatorContext + 分支 + import）
- [rock/admin/main.py](../../rock/admin/main.py)（传 opensandbox_config）
- [rock/sandbox/service/sandbox_proxy_service.py](../../rock/sandbox/service/sandbox_proxy_service.py)（抽后端 + 路由）
- `pyproject.toml`、`rock-conf/rock-*.yml`

## 风险与开放问题（经 Phase 0 更新）

1. ~~**SDK 能力覆盖**~~ —— ✅ 已确认全覆盖（见契约文档 §4）。session/portforward/文件均有对应，portforward 一期先 NotImplemented。
2. **stop/restart 生命周期语义** —— ✅ **已定**：Phase 1 默认不支持 `stop/restart`；`delete → sandbox.kill()`（Terminated → Rock `deleted`，不可逆）。OpenSandbox `pause/resume` 需要创建时显式启用 persistence，普通 sandbox 调 `pause` 会被服务端拒绝，不能隐式标记为 Rock `stopped`。
3. **sandbox_id 双标识**：已定方案——Rock id 主键 + `extended_params["opensandbox_id"]` + OpenSandbox `metadata["rock_sandbox_id"]`（契约 §1）。
4. **session_id 映射持久化**：OpenSandbox 返回不透明 session_id，需 `{sandbox_id:session_name → os_session_id}` 存 redis（多 worker 安全）。
5. **memory 单位/ disk quota**：`8g`→`8Gi` 需转换；rootfs disk quota OpenSandbox 无直接对应（先忽略+warn）。
6. **定时运维任务 gate**（Phase 2.4）：必须做，否则 rocklet 运维任务会打到 OpenSandbox 沙箱。
7. **异步一致性**：✅ SDK 原生 async，直接用，无需 executor 包裹。
8. **网络/鉴权简化**：建议 `ConnectionConfig.use_server_proxy=True`，Rock admin 单出口到 OpenSandbox server，契合现有代理架构。

## 建议提交拆分（原子提交）

先建 GitHub Issue，从 master 拉 `feat/opensandbox-operator`：
1. `feat(config): add OpenSandboxConfig`
2. `feat(operator): OpenSandboxClient + OpenSandboxOperator + factory wiring`（Phase 1）
3. `refactor(proxy): extract SandboxRuntimeBackend, keep rocklet behavior`（Phase 2.1–2.2，纯重构）
4. `feat(proxy): OpenSandbox runtime backend`（Phase 2.3–2.4）
5. `docs: OpenSandbox backend guide`

每个 PR body 关联 Issue（`refs #<id>`），CI 绿后合并。
