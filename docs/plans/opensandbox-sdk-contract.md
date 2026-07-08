# OpenSandbox Python SDK ↔ Rock 集成契约（Phase 0 产出）

> 来源：本地 `~/work/OpenSandbox`（`opensandbox-group/OpenSandbox`），Python SDK 版本约 0.1.13。
> SDK 根：`sdks/sandbox/python/src/opensandbox/`；Server：`server/opensandbox_server/api/`；OpenAPI：`specs/sandbox-lifecycle.yml`、`specs/execd-api.yaml`。
>
> **结论先行**：方案 B 需要的能力 OpenSandbox **全部具备**（生命周期 / 命令 / 文件 / bash session / 后台进程 / 端口 endpoint / connect-by-id / 原生 async）。原计划里"能力缺口"这个最大风险基本解除，剩下的是**语义映射与单位/标识对齐**这类工程细节。

---

## 0. 客户端构造与鉴权

`opensandbox.config.connection.ConnectionConfig`：

| 字段 | 默认 | 说明 |
|------|------|------|
| `api_key` | env `OPEN_SANDBOX_API_KEY` | 鉴权，HTTP 头 `OPEN-SANDBOX-API-KEY: <key>` |
| `domain` | env `OPEN_SANDBOX_DOMAIN` / `localhost:8080` | 服务器地址 |
| `protocol` | `http` | http/https |
| `request_timeout` | 30s | |
| `use_server_proxy` | `False` | **对 Rock 很关键**：置 `True` 时所有 execd（命令/文件）请求经 OpenSandbox server 代理，client 无需直连沙箱网络 |

→ 映射到 Rock 的 `OpenSandboxConfig`（endpoint/api_key/protocol/use_server_proxy/default_timeout）。**`use_server_proxy` 默认 `False`**（与 SDK 默认一致，且并非所有 OpenSandbox 部署都支持 server-proxy 模式）；仅当目标部署确认支持时才在 yaml 里显式设 `true`。它只影响 execd（命令/文件）路由，即 Phase 2；生命周期始终直连服务端，不受影响。

**Async 模型**：SDK 原生 `async def`（也有 `opensandbox.sync.*` 同步包装）。Rock proxy/operator 均为 async → **直接用 async 接口，无需 executor 包裹**（与 rocklet 的 `RemoteSandbox` 用线程池不同，这里更简单）。

---

## 1. 生命周期 seam 映射（→ OpenSandboxOperator，Phase 1）

Rock `DockerDeploymentConfig` → `Sandbox.create(...)`（`sandbox.py:66`，async classmethod）：

| Rock 字段 | OpenSandbox create 入参 | 备注 / 转换 |
|-----------|------------------------|-------------|
| `image: str` | `image: str` | 直接透传 |
| `cpus: float=2` | `resource={"cpu": "2"}` | float → str |
| `memory: "8g"` | `resource={"memory": "8Gi"}` | **单位转换**：docker `g/m` → k8s `Gi/Mi`（`8g`→`8Gi`，`4096m`→`4096Mi`） |
| `disk: "20g"` | ⚠️ create 无 disk 字段 | OpenSandbox 磁盘走 `volumes`；rootfs quota 无直接对应 → 记为**部分缺口**（见 §5） |
| runtime_env（env 注入） | `env: dict[str,str]` | Rock 的 `INSTANCE_ROCK_*` 等注入项放这里 |
| user_info（user_id/experiment_id/namespace） | `metadata: dict[str,str]` | 放 metadata，便于按标签检索与配额归属 |
| （启动命令） | `entrypoint: list[str]` | 默认 `["tail","-f","/dev/null"]`；Rock 一般不覆盖 |
| `startup_timeout` | `timeout` / `ready_timeout: timedelta` | float 秒 → timedelta |

**返回**：`Sandbox` 实例，`.id`（OpenSandbox 自己的 sandbox id）。`await sandbox.get_info()` → `SandboxInfo(id, status, entrypoint, expires_at, created_at, image, metadata, ...)`。

**其他 Operator 方法**：

| AbstractOperator | OpenSandbox SDK | 备注 |
|------------------|-----------------|------|
| `submit` | `Sandbox.create(...)` | 见上 |
| `get_status` | `Sandbox.connect(id)` + `get_info()`（或 server `GET /v1/sandboxes/{id}`） | 状态映射见下 |
| `stop` | `sandbox.pause()` | ✅ 已定：Paused → Rock `stopped`，可复用 |
| `restart` | `Sandbox.resume(id)` | ✅ 已定：回到 Running |
| `delete` | `sandbox.kill()`（terminate，不可逆） | Terminated → Rock `deleted`；OpenSandbox 无软删 |

**状态映射**（OpenSandbox `SandboxState` → Rock `State`，Rock 只有 `pending/running/stopped/deleted`）：

| OpenSandbox | Rock State |
|-------------|-----------|
| `Pending` | `pending` |
| `Running` | `running` |
| `Pausing` / `Paused` | `stopped`（Rock 无 paused 概念；配合 restart→resume） |
| `Stopping` / `Terminated` | `stopped`（terminated 亦可映射 `deleted`，按调用上下文） |
| `Failed` | `stopped` + 失败原因写入 `phases`/`reason`（Rock State 枚举无 FAILED） |
| `Unknown` | 保留上次已知，缺省 `pending` |

**标识对齐（关键设计决策）**：Rock 用自己生成的 `sandbox_id` 作主键（redis/db/ctx var），OpenSandbox 另有 `.id`。
- 方案：**Rock sandbox_id 仍为主键**；`submit` 时把 OpenSandbox id 写入 `SandboxInfo.extended_params["opensandbox_id"]`，并把 Rock sandbox_id 作为 `metadata["rock_sandbox_id"]` 传给 OpenSandbox（双向可查）。
- `extended_params["backend"] = "opensandbox"` 作为后端路由标记（Phase 2 用）。

---

## 2. 执行/文件 seam 映射（→ OpenSandboxBackend，Phase 2）

后端 attach 方式：`OpenSandboxBackend` 从 status_dict 取 `opensandbox_id`，`await Sandbox.connect(opensandbox_id, connection_config=...)` 拿到 handle 后调用。

### 2.1 命令执行

Rock `Command`（`command: str|list`, `timeout: float=1200`, `env`, `cwd`, `session_type="bash"`）→ `sandbox.commands.run(command, opts=RunCommandOpts(...))`（`services/command.py:34`）：

| Rock Command | RunCommandOpts |
|--------------|----------------|
| `command: str \| list` | `command: str`（list 需 `shlex.join` 或 `" ".join`） |
| `timeout: float`（秒） | `timeout: timedelta`（`timedelta(seconds=...)`；`None`=无限） |
| `cwd` | `working_directory` |
| `env` | `envs` |
| — | `background=False`（Rock execute 是同步取结果） |

**返回** `Execution` → Rock `CommandResponse`：
- `stdout` = `"".join(m.text for m in execution.logs.stdout)`
- `stderr` = `"".join(m.text for m in execution.logs.stderr)`
- `exit_code` = `execution.exit_code`
- `execution.error`（`ExecutionError`）非空时按 Rock 约定填 stderr / 抛 `CommandRockError`

> 注意：OpenSandbox `/command` 是 SSE 流式；SDK 默认累积到 `logs`。Rock `execute` 只要最终结果 → 用默认累积、不传 handlers 即可。

### 2.2 文件操作

| Rock | OpenSandbox `sandbox.files.*`（`services/filesystem.py:37`） | 备注 |
|------|-----------------------------------------------------------|------|
| `read_file(ReadFileRequest{path,encoding,errors})` → `ReadFileResponse{content}` | `read_file(path, encoding="utf-8")` → `str` | Rock `errors` 无对应 → 忽略并 debug 记录 |
| `write_file(WriteFileRequest{content,path})` → `WriteFileResponse{success,message}` | `write_file(path, data, encoding="utf-8", mode=755)` | 成功即 `success=True` |
| `upload(file, target_path)` → `UploadResponse` | `write_file(target_path, data=bytes)` | OpenSandbox 后端不走 OSS，`UploadMode` 优化对其 no-op（直写） |

### 2.3 bash session（✅ 支持，但键映射需处理）

Rock 用**会话名**（`session: str="default"`）作 key；OpenSandbox `create_session` 返回**不透明 session_id**，且入参仅 `working_directory`。

| Rock | OpenSandbox `sandbox.commands.*` | 缺口/处理 |
|------|----------------------------------|-----------|
| `create_session(CreateBashSessionRequest{session, startup_source, env, env_enable, remote_user})` | `create_session(working_directory=None) -> session_id` | **需维护 `{(sandbox_id, rock_session_name) → os_session_id}` 映射**；`startup_source`→创建后先 `run_in_session` 逐条执行；`env`/`remote_user`→部分缺口（OpenSandbox session 级无 env/user，可在每次命令的 `envs`/`uid`/`gid` 传） |
| `run_in_session(BashAction{command, session, timeout, check})` → `BashObservation{output, exit_code, failure_reason, expect_string}` | `run_in_session(session_id, command, timeout=...)` → `Execution` | `output`=`execution.text`；`check`(raise/silent/ignore)在 Rock 侧处理；`expect_string`（swe-rex 概念）→ `""` |
| `close_session(CloseBashSessionRequest{session})` | `delete_session(session_id)` | 清映射 |

**session_id 映射存储决策**：OpenSandbox 在服务端保存 session 状态，Rock 需能跨请求/多 worker 找回 `os_session_id`。建议存 redis（key 按 `sandbox_id:session_name`），或 `extended_params`。列为 Phase 2 设计点。

### 2.4 端口转发（websocket portforward）

Rock `_get_rocklet_portforward_url` 把客户端 ws 代理到 rocklet 的 `/portforward`。OpenSandbox 提供 `sandbox.get_endpoint(port) -> SandboxEndpoint{endpoint, headers}` 与 `get_signed_endpoint(port, expires)`。
- 映射：portforward 请求 → `get_endpoint(port)` 拿到 URL+headers → Rock 侧把客户端 ws 桥接到该 endpoint。
- 协议不完全对等（rocklet 是 Rock 私有 ws 帧，OpenSandbox 给的是通用 endpoint）→ 列为 Phase 2 中**优先级最低**的一项，可先返回 `BadRequestRockError("portforward not supported on opensandbox backend")`，二期再实现。

---

## 3. Server REST API（备用，若不走 SDK）

`specs/sandbox-lifecycle.yml`（base `/v1`）：`POST /sandboxes`、`GET /sandboxes/{id}`、`DELETE /sandboxes/{id}`、`POST /sandboxes/{id}/pause|resume|renew-expiration`、`PATCH /sandboxes/{id}/metadata`。
execd（`specs/execd-api.yaml`）：`POST /command`(SSE)、`POST /session`、`POST /session/{id}/run`、`POST /files/read|write`、`GET /files/info` 等。
→ **本集成走官方 Python SDK**，REST 仅作 debug 参考。

---

## 4. 能力矩阵（对照 Rock 需求）

| Rock 需要 | OpenSandbox | 结论 |
|-----------|-------------|------|
| 创建带 cpu/mem/env/metadata/entrypoint | ✅ `create(resource,env,metadata,entrypoint)` | 完备 |
| 按 id 重连（exec 前 attach） | ✅ `connect(id)` | 完备（后端设计的基石） |
| 同步取命令结果 | ✅ `commands.run` 累积 | 完备 |
| 后台进程 | ✅ `RunCommandOpts.background` + `get_command_status` | 完备（Rock 当前 execute 用不到） |
| 读/写/上传文件 | ✅ `files.read_file/write_file/write_files` | 完备 |
| bash session 有状态 | ✅ `create_session/run_in_session/delete_session` | 完备，需键映射 |
| 端口转发 | ⚠️ `get_endpoint`（协议需桥接） | 二期实现 |
| rootfs disk quota | ⚠️ 无直接对应（有 volumes） | 部分缺口 |
| 原生 async | ✅ | 完备 |

---

## 5. 遗留缺口 / 设计决策清单（进入 Phase 1/2 实现）

1. **memory 单位转换** `8g`→`8Gi`：写一个 `docker_mem_to_k8s()` 工具（`g→Gi`, `m→Mi`）。
2. **cpus 语义**：Rock `cpus`=cpu-shares 软配，`limit_cpus`=硬限；OpenSandbox `resource.cpu` 更接近硬 request。取 `limit_cpus or cpus`。
3. **disk quota 缺口**：OpenSandbox create 无 rootfs quota，Rock `disk` 暂无处安放 → 先忽略并 warn，或后续用 volumes 方案；文档标注该后端不支持 rootfs 限额。
4. **sandbox_id 双标识**：Rock id 主键 + `extended_params["opensandbox_id"]` + OpenSandbox `metadata["rock_sandbox_id"]`。
5. **session_id 映射持久化**：`{sandbox_id:session_name → os_session_id}` 存 redis（多 worker 安全）。
6. **stop/restart/delete 语义（✅ 已定）**：`stop→pause`（Paused=Rock `stopped`）、`restart→resume`、`delete→kill`（Terminated=Rock `deleted`）。文档需说明：pause 的沙箱是否仍占资源取决于 OpenSandbox 实现，与 ray/docker 后端 stop 的语义存在差异。
7. **portforward**：一期 `NotImplementedError`，二期基于 `get_endpoint` 桥接。
8. **use_server_proxy 默认 False**：与 SDK 默认一致；部分部署不支持 server-proxy 模式，需要时再显式开启（仅影响 Phase 2 的 execd 路由）。
9. **定时运维任务 gate**（沿用主计划 Phase 2.4）：`scheduler/tasks/*` 的 rocklet 直连任务在 opensandbox 后端下跳过。

---

## 6. 对主计划的影响

- **Phase 0 完成**：SDK 契约确定，能力缺口从"未知大风险"收敛为上面 9 条明确的工程决策，其中只有 #6（stop/restart 语义）需要产品拍板，其余可在实现中直接处理。
- Phase 1/2 的 mock 测试可直接照本文档的签名 mock `Sandbox.create/connect/kill`、`commands.run/create_session/run_in_session`、`files.read_file/write_file`。
- 依赖：`opensandbox` 作为 `pyproject.toml` optional extra 引入（async 版即可，无需 sync 包装）。
