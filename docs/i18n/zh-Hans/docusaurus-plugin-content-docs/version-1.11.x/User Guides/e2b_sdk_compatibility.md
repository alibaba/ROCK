---
sidebar_position: 10
---

# E2B Python SDK 兼容性

ROCK 针对官方 E2B Python SDK 的特定子集提供了兼容层。现有 E2B 客户端无需替换客户端库，即可创建和查看 ROCK 沙箱、运行命令并执行常用文件操作。

本文说明 ROCK 的 E2B 兼容性约定，其范围有意小于完整的 E2B API：

- 支持且经过测试的 SDK 版本：`e2b==2.34.0`；
- 每个 ROCK 沙箱响应报告的版本：`envdVersion=0.3.0`；
- 本文说明的客户端：同步 Python `Sandbox`。

有关上游概念，请参阅 E2B 官方文档：[沙箱生命周期](https://docs.e2b.dev/sandbox)、[命令](https://docs.e2b.dev/commands)和[文件系统](https://docs.e2b.dev/filesystem)。确切的 SDK 基线是官方的 [`e2b` 2.34.0 源码](https://github.com/e2b-dev/E2B/tree/43db96a0ef2e555b96eee1a52856013fbf0dc644)。ROCK 在 `pyproject.toml` 中固定了该版本，对外报告的 envd 版本定义在 `rock/common/constants.py` 中。

## 能力矩阵

**支持**表示当前 SDK 集成测试/线上测试已覆盖。**协议兼容**表示已实现的路由能够完成调用，但线上验收尚未覆盖该调用。**部分支持**表示存在实质性差异。

| 领域 | E2B Python API | 状态 | ROCK 行为 |
|---|---|---|---|
| 生命周期 | `Sandbox.create()` | 部分支持 | 创建 ROCK 沙箱；只有[创建](#创建)中列出的参数会生效。 |
| 生命周期 | `sandbox.get_info()` / `Sandbox.get_info(id)` | 支持 | 返回 E2B `SandboxInfo`；下文说明 ROCK 到 E2B 的状态映射。 |
| 生命周期 | `Sandbox.list()` | 部分支持 | 要求非空的 `metadata` 过滤条件，仅返回运行中的沙箱，并返回一组未分页的结果。 |
| 生命周期 | `sandbox.kill()` / `Sandbox.kill(id)` | 支持 | 不可逆地删除沙箱；沙箱已不存在时返回 `False`。 |
| 生命周期 | `sandbox.is_running()` | 协议兼容 | 仅当 ROCK 状态为 `RUNNING` 时，`/health` 才返回 `True`。 |
| 生命周期 | `sandbox.set_timeout()` / `Sandbox.set_timeout(id, ...)` | 部分支持 | 重置运行中沙箱现有的 ROCK 自动清理超时。秒数向上取整到整分钟，常规命令/文件活动仍沿用 ROCK 滑动续时。 |
| 命令 | 前台运行 `sandbox.commands.run()` | 支持 | 仅支持非交互式调用；返回 stdout、stderr 和退出码，并支持环境变量、工作目录和有限的超时时间。 |
| 命令 | `sandbox.commands.run(background=True)` | 部分支持 | 返回的句柄可以等待原始响应流；无法终止进程或重新连接进程。 |
| 命令 | `CommandHandle.disconnect()` | 部分支持 | 关闭响应流但不终止命令；不支持重新连接该命令。 |
| 文件 | `files.make_dir()` | 支持 | 递归创建父目录；目录已存在时返回 `False`。 |
| 文件 | `files.write()` / `files.write_files()` | 支持 | 支持 multipart 格式的文本和二进制写入、创建父目录以及覆盖。 |
| 文件 | `files.read(format="text")` | 支持 | 返回 `str`。 |
| 文件 | `files.read(format="bytes")` | 支持 | 返回 `bytearray`。 |
| 文件 | `files.read(format="stream")` | 协议兼容 | 使用相同的流式 `GET /files` 路由，但当前线上验收测试未覆盖。 |
| 文件 | `files.list()` / `files.get_info()` | 支持 | 为文件和目录返回 E2B `EntryInfo`。 |
| 文件 | `files.exists()` | 协议兼容 | 复用已实现的 `Stat` RPC；当前线上验收测试未覆盖。 |
| 其他 | `sandbox.get_host()`、`upload_url()`、`download_url()` | 不支持 | ROCK 未实现 E2B 通配符流量主机或 E2B 签名 URL 校验。 |
| 其他 | E2B 模板、卷、Code Interpreter、MCP、PTY 和快照 API | 不支持 | 缺少所需的 E2B 路由或语义。 |
| 其他 | `sandbox.git.*` 辅助方法 | 不保证 | 部分辅助方法只调用 `commands.run()`，安装 Git 后可能可以工作，但不属于经过测试的约定。 |

集成约定由 `tests/integration/admin/test_e2b_sdk_commands_run.py` 验证，部署链路由 `tests/integration/admin/test_e2b_sdk_commands_run_live.py` 验证。

## 部署与客户端路由

ROCK 将 E2B 控制面与 E2B 沙箱数据面分离。必须始终显式配置这两个 URL；创建响应不包含 E2B `domain`，因此不支持基于 E2B 通配符主机发现的部署模式。

```bash
pip install "e2b==2.34.0"

export E2B_API_KEY="<rock-api-key>"
export E2B_API_URL="https://<rock-control-host>"
export E2B_SANDBOX_URL="https://<rock-data-host>"
export E2B_TEMPLATE_ID="<rock-template-id-or-image>"
```

外部网关必须按下表路由路径：

| 客户端基础 URL | 路径 | ROCK 角色 |
|---|---|---|
| `E2B_API_URL` | `POST /sandboxes` | Admin 角色（`ROCK_ADMIN_ROLE=admin`） |
| `E2B_API_URL` | `GET`, `DELETE /sandboxes/{sandboxID}` | Admin 角色 |
| `E2B_API_URL` | `POST /sandboxes/{sandboxID}/timeout` | Admin 角色 |
| `E2B_API_URL` | `GET /v2/sandboxes` | Proxy 角色，尽管 SDK 会将此列表请求发送到 `api_url` |
| `E2B_SANDBOX_URL` | `/health`, `/process.Process/Start`, `/files`, `/filesystem.Filesystem/*` | Proxy 角色 |

该拆分由 `rock/admin/main.py::_include_routers()` 定义。如果部署将所有 `E2B_API_URL` 路径仅发送到 Admin 角色，`Sandbox.list()` 将会失败。

代理使用 `E2b-Sandbox-Id` 选择运行中的沙箱。命令会委托给配置的沙箱代理后端。文件方法会通过沙箱的 `host_ip` 和映射的 `PROXY` 端口专门访问 Rocklet，因此对于没有这类映射的 OpenSandbox 记录不可用。对于 Connect 流式传输，入口必须保留长时间运行的 HTTP 响应并禁用响应缓冲。

ROCK 期望部署网关对两个平面都进行身份验证。应用将传入的 `X-API-Key` 作为 `envdAccessToken` 返回，随后 E2B SDK 将其作为 `X-Access-Token` 发送到数据面；当前代理不会独立校验该令牌，也不使用 `E2b-Sandbox-Port`。如果 ROCK 密钥不符合 E2B 的密钥格式，请设置 `validate_api_key=False`。

实现位于 `rock/admin/entrypoints/e2b_api.py`、`rock/admin/entrypoints/e2b_proxy_api.py` 和 `rock/admin/service/e2b_proxy_service.py`。

### 后端与操作系统支持

| ROCK 运行时 | 生命周期 API | `commands.run()` | `files.*` |
|---|---|---|---|
| 带有 Rocklet `PROXY` 映射的 Linux 沙箱 | 如本指南所列 | 支持 | 如本指南所列，支持 |
| OpenSandbox Operator | 端到端不兼容：E2B 创建/获取/列出操作要求沙箱 IP，而该 Operator 不会发布这一信息 | 代理可以为预先存在的运行中记录委托执行，但该路径未经过验收测试 | E2B 文件适配器不支持 |
| Windows 沙箱 | 控制面创建取决于后端 | 此 E2B 适配器不支持 | 此 E2B 适配器不支持 |

命令 SDK 始终发送 `/bin/bash -l -c`，文件适配器始终使用以 `/home/user` 为根的 POSIX 路径。因此，数据面兼容性约定要求沙箱为带有 `/bin/bash` 的 Linux/POSIX 沙箱。对于非 OpenSandbox 的文件操作，沙箱状态还必须包含可访问的 `host_ip` 和映射的 Rocklet `PROXY` 端口。

## 沙箱生命周期

### 创建

请使用 `Sandbox.create()`，而不是已弃用的构造函数。该方法会等待 ROCK 启动运行时并获取其 IP。控制面的 `request_timeout` 应覆盖镜像准备与启动所需时间；`180` 秒是一个合理示例，但合适的值取决于具体部署。

| 参数 | 状态 | 行为 |
|---|---|---|
| `template` | 支持 | SDK 始终发送非空值（省略时为 `base`）。就绪的 ROCK 模板会提供其镜像和 CPU/内存/磁盘值。如果没有就绪的模板，ROCK 会将该值视为原始镜像/清单引用。 |
| `timeout` | 部分支持 | 省略或设为 `0` 时，请求发出前会变为 SDK 默认值 300 秒。ROCK 要求请求中的值为正整数，并向上取整到整分钟。命令和文件操作会刷新 ROCK 过期截止时间，因此这是滑动式自动停止超时，而不是 E2B 严格固定时间的 `kill`。 |
| `metadata` | 支持 | 保留字符串到字符串的 `metadata` 键值。空字典有效。如果存在 `ap-sandbox-id`，还会将其用作请求的容器名称。 |
| `envs` | 支持 | 传递给沙箱，并由后续 `commands.run()` 调用继承。单条命令的 `envs` 值会覆盖沙箱中名称相同的值。 |
| `secure` | 忽略 | 为兼容 SDK 而接受，但不会更改 ROCK 运行时安全性或数据面身份验证。请在网关实施身份验证。 |
| `allow_internet_access` | 忽略 | 接受该字段，但 ROCK 不会根据它更改出站策略。 |
| `lifecycle` / `autoPause` / `autoResume` | 不支持 | 接受 SDK 默认值但会忽略。非默认的 pause 生命周期还可能发送 `autoPauseMemory`，ROCK 会以 HTTP 400 拒绝。 |
| `mcp` | 不支持 | 非空配置会以 HTTP 400 拒绝。空配置在请求中会被省略，但仍会触发 SDK 端的 MCP 引导流程，而该功能不受支持。 |
| `network` | 不支持 | 非空配置会以 HTTP 400 拒绝；空配置会被省略。请改用 ROCK 部署/网络配置。 |
| `volume_mounts` | 不支持 | 非空挂载会以 HTTP 400 拒绝；空字典会被省略。E2B 卷不会映射到 ROCK 存储。 |

创建响应包含 `sandboxID`、请求的 `templateID`、`clientID="rock"`、`envdVersion="0.3.0"` 和 `envdAccessToken`。响应不包含 `domain` 或 `trafficAccessToken`。

请求模型和创建映射位于 `rock/admin/proto/request.py` 和 `rock/admin/service/e2b_service.py`。

#### 冷启动 metadata

没有就绪模板、需要冷启动时，可以在 `metadata` 中指定以下可选字段，值均须为字符串。找到就绪模板时，这些字段会被忽略，资源使用模板配置。

| 字段 | 用法 | 省略时的默认值 |
|---|---|---|
| `cpuCount` | CPU 核数，正整数，例如 `"4"`。 | 2 核 |
| `memoryMB` | 内存大小，单位 MB，正整数，例如 `"16384"`（16 GiB）。 | 8192 MB |
| `startup_timeout` | 提交后等待沙箱就绪的最长时间，单位秒，有限正数，例如 `"120"`。 | 85 秒 |

例如，将以下字典作为 `POST /sandboxes` 请求中的 `metadata` 值，或作为 `metadata` 参数传给 `Sandbox.create()`：

```json
{
  "cpuCount": "4",
  "memoryMB": "16384",
  "startup_timeout": "120"
}
```

冷启动时，无效参数会返回 HTTP 400；CPU、内存申请仍受部署配置的资源上限约束。`startup_timeout` 包含提交后的就绪轮询与状态查询等待，不包含准备及提交耗时，也不会调整客户端或网关的 HTTP 超时。请求顶层的 `timeout` 仍控制沙箱存活时间。

### 获取沙箱信息

支持 `sandbox.get_info()` 和 `Sandbox.get_info(id, ...)`。返回的 `SandboxInfo` 包含沙箱 ID、模板/镜像、元数据、开始/结束时间、状态、CPU 数量、内存和 `envdVersion`。ROCK 还在响应中报告磁盘大小，并向 `metadata` 添加 `e2b.agents.kruise.io/sandbox-ip`。详细信息中的 `template_id` 从当前沙箱状态派生，可能是解析后的镜像，而不是创建响应原样返回的 `template` 字符串。

ROCK 的状态映射如下：

| ROCK 状态 | E2B 状态 |
|---|---|
| `PENDING`, `RUNNING` | `RUNNING` |
| `STOPPED`, `ARCHIVED` | `PAUSED` |
| `ARCHIVING`, `DELETED`、缺失或未知 | 未找到 |

`PAUSED` 值仅用于信息层面的兼容：ROCK 不提供 E2B `connect()`/resume，因此无法通过此 SDK 接口恢复已停止或已归档的沙箱。

这些响应要求 ROCK 记录中包含有效的字符串元数据、有效的沙箱 IP、资源值以及带时区的时间戳。后端记录不完整时会失败，而不是返回部分 `SandboxInfo`。

沙箱字段映射位于 `rock/admin/service/e2b_sandbox_info.py`。

### 列出沙箱

ROCK 仅支持 `Sandbox.list(query=SandboxQuery(metadata={...}))`，且有以下差异：

- 必须至少提供一组非空的 `metadata` 键值对；直接调用 `Sandbox.list()` 会返回 HTTP 400；
- 所有 `metadata` 条件以精确匹配的 AND 语义组合；
- 只返回 ROCK `RUNNING` 沙箱；
- 当前会忽略 `query.state`、`limit` 和 `next_token`；
- ROCK 不返回 `x-next-token`，因此 SDK 分页器只有一页；
- 服务端不应用结果数量限制。请使用高选择性的 `metadata`，尤其是在大型部署中。

支持 SDK 编码后的 `metadata` 形式。底层端点还接受供第一方集成使用的 `key:value,key2:value2`。无效、空或重复的条件会返回 HTTP 400。

`/v2/sandboxes` 行为由 `tests/unit/admin/entrypoints/test_e2b_proxy_api.py` 覆盖。

### 删除与健康检查

`sandbox.kill()` 和 `Sandbox.kill(id, ...)` 映射到不可逆的 ROCK 删除。根据后端和状态，ROCK 会直接删除沙箱，或先停止沙箱再删除。重复调用 `kill()` 会收到 404，E2B SDK 返回 `False`。

`sandbox.is_running()` 调用数据面的 `/health` 路由。仅当状态为 `RUNNING` 时返回 `True`；处于 `PENDING`、`STOPPED`、`ARCHIVED`、`DELETED` 状态或缺失的沙箱返回 `False`。此方法协议兼容，但不属于当前线上 SDK 验收测试。

### 设置超时

对于 `RUNNING` 沙箱，支持 `sandbox.set_timeout(timeout)` 以及静态形式 `Sandbox.set_timeout(id, timeout, ...)`。
`timeout` 必须是 E2B `int32` 范围内的严格整数秒数。ROCK 将其映射到现有分钟级自动清理模型，并向上取整到
整分钟；`0` 表示截止时间立即到期。成功响应为空 HTTP 204，Python SDK 返回 `None`；沙箱不存在时返回 E2B
`{code, message}` 格式的 404。

`get_info()`、`set_timeout()` 和 `kill()` 使用的 E2B 控制面状态检查不会刷新超时。其他 ROCK timeout 行为保持
不变：命令/文件活动仍可能刷新滑动式自动清理截止时间，周期性生命周期扫描可能带来延迟，并且过期会停止
沙箱，而不保证 E2B 默认的不可逆 kill。

### 不支持的生命周期 API

ROCK 没有为 `Sandbox.connect()`（恢复）、`pause()`/`beta_pause()`、`fork()`、`update_network()`、`get_metrics()`、快照操作或 E2B 模板管理提供兼容路由。

ROCK 也不提供 `sandbox.get_host()` 所使用的 E2B 通配符主机流量约定，也不提供 `upload_url()` 和 `download_url()` 所需的 E2B 签名校验。请使用常规受支持的 `files.*` 方法传输文件，并通过 ROCK [沙箱代理](./sandbox_proxy.md)访问沙箱中对外提供的服务。

## 命令

ROCK 支持官方的 [`commands.run()` 形式](https://docs.e2b.dev/commands)：

仅支持非交互式 `sandbox.commands.run()` 调用。不支持交互式 stdin、PTY 和后续输入。

| 参数 | 状态 | 行为 |
|---|---|---|
| `cmd` | 支持 | 以 `/bin/bash -l -c <cmd>` 执行。 |
| `envs` | 支持 | 合并到沙箱级环境变量之上。 |
| `cwd` | 支持 | 传递给运行时；无效目录会产生 `InvalidArgumentException`。 |
| `timeout` | 部分支持 | 请使用大于零且不超过 **60 秒**的有限值。解析器当前接受的最大值为 85 秒，但超过 60 秒的值不属于兼容性保证。`0` 和 `None`（无限制）会被拒绝。 |
| `request_timeout` | 仅 SDK 端 | 控制客户端/传输层等待时间；不会延长 ROCK 的命令执行超时时间。 |
| `on_stdout`, `on_stderr` | 部分支持 | 由于 ROCK 会缓冲 stdout 和 stderr，回调只能在命令结束后收到输出；不提供实时流式传输。 |
| `background=True` | 部分支持 | 立即返回一个合成进程句柄。`handle.wait()` 可在读取原始响应流的同时正常工作。PID 不是持久的后端 PID。 |
| `stdin=False` 或省略 | 支持 | 这是唯一支持的 stdin 模式。 |
| `stdin=True` | 拒绝 | 返回 Connect `unimplemented`；没有交互式输入流。 |
| `user` | 忽略 | ROCK 不会为 E2B 命令切换运行时用户。请省略此参数。 |

退出码为零时返回 `CommandResult(stdout, stderr, exit_code=0)`。非零退出会作为正常的进程结束事件发送，随后 Python SDK 抛出 `CommandExitException`；该异常包含 `stdout`、`stderr`、`exit_code` 和 `error`。命令超过截止时间会抛出 `TimeoutException`。无效请求会抛出 `InvalidArgumentException`，每个代理的并发保护会表现为 `RateLimitException`。

Connect 请求封装限制为 1 MiB。每个代理进程最多跟踪 1,024 个活跃 E2B 命令任务。输出会被缓冲，因此调用方应避免无界命令输出或无界并发命令。客户端断开连接不会取消任务，但没有供后续使用的重连 API；在多 Worker 部署中，应确保原始响应流始终路由到同一个 Proxy Worker，并将负载均衡器超时配置为至少覆盖命令持续时间。

以下功能不受支持：`commands.list()`、`commands.connect(pid)`、`commands.kill(pid)`、`CommandHandle.kill()`、`commands.send_stdin()`、`CommandHandle.send_stdin()`、`CommandHandle.close_stdin()`、PTY 命令和带标签命令。

`CommandHandle.disconnect()` 会关闭当前响应流，但后端命令会继续运行，直到退出或达到有限超时时间。由于 `commands.connect()` 和 `commands.kill()` 不可用，此后既无法恢复其输出，也无法控制该命令。

请参阅 ROCK 的 `rock/admin/proto/e2b_connect.py` 映射和官方 [`e2b` 2.34.0 命令源码](https://github.com/e2b-dev/E2B/blob/43db96a0ef2e555b96eee1a52856013fbf0dc644/packages/python-sdk/e2b/sandbox_sync/commands/command.py)。

## 文件系统

支持的文件系统方法遵循官方的[读/写](https://docs.e2b.dev/filesystem/read-write)和[文件信息](https://docs.e2b.dev/filesystem/info)模型。

### 路径与用户

ROCK 应用 Linux/POSIX 路径规则：

- 绝对路径保持不变；
- 相对路径在 `/home/user` 下解析；
- `~` 和 `~/...` 解析为 `/home/user`；
- 对 `.` 和 `..` 组成部分进行规范化；
- 不支持 Windows 路径语义。

ROCK 未实现 E2B 文件系统用户切换。每次调用都应省略 `user`。在对外报告 `envdVersion=0.3.0` 时，SDK 会提供其逻辑默认用户 `user`；对于 `/files` 调用，ROCK 接受该默认值。读/写 HTTP 路由会拒绝其他用户，而 Connect 文件系统路由当前会忽略授权用户。不要依赖这种协议不一致：操作以 Rocklet 进程用户身份运行。

### 创建目录

`make_dir(path, request_timeout=...)` 会递归创建缺失的父目录。首次创建时返回 `True`，目录已存在时返回 `False`。如果路径已作为非目录存在，SDK 会抛出 `InvalidArgumentException`。`path` 不得为空。

### 写入文件

默认的 multipart 上传支持 `str`（UTF-8）和 `bytes`，会创建缺失的父目录并覆盖现有文件。写入一个文件时返回 `WriteInfo`，批量写入时按输入顺序返回 `list[WriteInfo]`。`WriteInfo` 包含 `name`、规范化后的 `path` 和 `type=FileType.FILE`；不返回文件元数据。

在 `envdVersion=0.3.0` 下，二进制/文本类文件 `IO` 对象使用相同的 multipart 路径并且协议兼容，但当前线上验收测试未覆盖。每个批次最多包含 256 个文件部分。文件会依次转发到 Rocklet，且批次并非原子操作：如果后续写入失败，之前的写入仍会保留。E2B 没有专门限制总字节数或单个文件大小，但大文件/高并发上传尚未经过容量验证，调用方应主动限制其规模。

以下写入选项不提供其上游 E2B 语义：

| 选项 | ROCK 行为 |
|---|---|
| `gzip=True` | `envdVersion=0.3.0` 会使 SDK 回退为未压缩的 multipart 上传。 |
| `use_octet_stream=True` | SDK 回退为 multipart；ROCK 未实现 octet-stream 上传。 |
| 非空 `metadata={...}` | 由于文件元数据需要更新的 envd 版本，SDK 会在发送请求前抛出 `TemplateException`。 |
| `metadata={}` | 接受，但不会存储元数据。 |

### 读取文件

数据面以流式方式从 Rocklet 传输字节。对于 `format="text"`，SDK 会缓冲并解码字节；对于 `format="bytes"`，则返回 `bytearray`（不是 `bytes`）。路径缺失会抛出 `FileNotFoundException`；读取目录会抛出 `InvalidArgumentException`。

`format="stream"` 使用相同的流式响应并且协议兼容，但当前线上验收测试未覆盖。使用时，请完整遍历迭代器或关闭其上下文管理器，以便释放连接池中的连接。`gzip=True` 读取选项可能仍会返回正确内容，但 ROCK 不保证压缩传输，不应将该选项用作带宽约定。

### 列出与查看文件

`files.list()` 默认使用 `depth=1`，接受从 1 到 100 的深度，返回按路径排序的条目，每次调用最多返回 10,000 个条目。返回条目超过 10,000 个或 `depth` 无效时会抛出 `InvalidArgumentException`。

`files.list()` 返回 `list[EntryInfo]`；`files.get_info()` 返回一个 `EntryInfo`。每个对象包含：

- `name`、`path` 和 `type`；
- 以字节为单位的 `size`；
- 数字类型的 `mode` 和字符串类型的 `permissions`；
- `owner` 和 `group`；
- 带时区的 `modified_time`；
- 可选的 `symlink_target`。

ROCK 将普通文件映射为 `FileType.FILE`，将目录映射为 `FileType.DIR`。ROCK 不会公开单独的符号链接文件类型：有效的符号链接会映射为目标的文件/目录类型，并可包含解析后的 `symlink_target`。不会填充自定义 E2B 文件元数据。

`files.exists()` 复用已实现的 `Stat` RPC 并返回 `True`/`False`；该方法协议兼容，但不属于当前线上验收测试。

请参阅 `rock/admin/entrypoints/e2b_proxy_api.py` 中的 ROCK 文件路由、`rock/rocklet/file_system.py` 中的 Rocklet 实现，以及官方 [`e2b` 2.34.0 文件系统源码](https://github.com/e2b-dev/E2B/blob/43db96a0ef2e555b96eee1a52856013fbf0dc644/packages/python-sdk/e2b/sandbox_sync/filesystem/filesystem.py)。

### 文件系统错误

| 条件 | Python SDK 结果 |
|---|---|
| 向 `read()`、`get_info()` 或 `exists()` 传入缺失的路径 | `FileNotFoundException`（`exists()` 会将其转换为 `False`） |
| 向 `list()` 传入缺失/非目录路径、空/无效路径、无效深度，或对目录执行读/写 | `InvalidArgumentException` |
| 在文件路由上，沙箱未运行 | `FileNotFoundException` |
| 向 `make_dir()` 传入现有目录 | 返回 `False` |
| 代理无法访问沙箱文件系统 | 通常为 `TimeoutException` 或传输层的 `SandboxException` |
| 权限失败 | `SandboxException` |

在受支持的文件调用中，`request_timeout` 仍作为 E2B 客户端请求超时时间可用。它不会更改沙箱生命周期超时时间。

以下文件系统方法不受支持：`remove()`、`rename()` 和 `watch_dir()`。ROCK 没有为其 RPC 提供兼容的 E2B 路由。

E2B 卷 API、自定义文件元数据、网络挂载监视以及通过 OpenSandbox Operator 执行的文件系统操作不属于此兼容性约定。

## 同步 SDK 示例

这个精简示例会创建沙箱、运行命令、写入并读取文件、按 `metadata` 列出沙箱，然后将其删除。由于 ROCK 要求 `Sandbox.list()` 提供 `metadata` 过滤条件，因此示例特意使用唯一的 `metadata` 值。

```python
import os
import uuid

from e2b import Sandbox, SandboxQuery


request_id = f"e2b-rock-demo-{uuid.uuid4().hex}"
options = {
    "api_url": os.environ["E2B_API_URL"],
    "sandbox_url": os.environ["E2B_SANDBOX_URL"],
    "api_key": os.environ["E2B_API_KEY"],
    "validate_api_key": False,
    "request_timeout": 180,
}

sandbox = Sandbox.create(
    template=os.environ["E2B_TEMPLATE_ID"],
    timeout=300,
    metadata={"demo-request-id": request_id},
    envs={"SANDBOX_ENV": "from-create"},
    **options,
)

try:
    info = sandbox.get_info()
    print("sandbox:", info.sandbox_id, info.state)

    result = sandbox.commands.run(
        'printf "%s:%s\\n" "$SANDBOX_ENV" "$COMMAND_ENV"',
        envs={"COMMAND_ENV": "from-command"},
        stdin=False,
        timeout=60,
    )
    assert result.stdout == "from-create:from-command\n"
    assert result.exit_code == 0

    directory = f"/tmp/{request_id}"
    path = f"{directory}/hello.txt"
    sandbox.files.make_dir(directory)
    sandbox.files.write(path, "hello ROCK\n")
    assert sandbox.files.read(path) == "hello ROCK\n"
    print("file:", sandbox.files.get_info(path))

    paginator = Sandbox.list(
        query=SandboxQuery(metadata={"demo-request-id": request_id}),
        **options,
    )
    assert sandbox.sandbox_id in {item.sandbox_id for item in paginator.next_items()}
finally:
    sandbox.kill()
```

在应用代码中，请将 `kill()` 调用放在 `finally` 块中。`metadata` 查询只能找到已达到 `RUNNING` 状态的沙箱；如果沙箱创建在更早的状态失败，它无法作为完整的清理兜底方案。
