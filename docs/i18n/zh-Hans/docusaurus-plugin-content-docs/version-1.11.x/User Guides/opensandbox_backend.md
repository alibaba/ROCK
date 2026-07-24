---
sidebar_position: 6
---

# OpenSandbox 后端

OpenSandbox Operator 允许 ROCK 把沙箱生命周期和运行时操作委托给外部
[OpenSandbox](https://github.com/alibaba/OpenSandbox) 部署。ROCK 客户端仍然只调用 ROCK Admin API，无需编写后端专用逻辑。

## 架构

当 `runtime.operator_type: opensandbox` 时，ROCK Admin 通过 OpenSandbox Python SDK 处理：

- 经 OpenSandbox Server 执行的生命周期操作；
- 经 OpenSandbox `execd` 端点执行的命令、文件、session 和沙箱内服务访问。

OpenSandbox 沙箱不需要 Rocklet。ROCK 不会为该后端安装、探测 Rocklet，也不会回退到 Rocklet。
沙箱元数据中的 `extended_params.backend` 是路由依据；字段缺失、未知或与当前 Operator 冲突时会直接失败。

## 安装

安装 Admin 依赖，其中已包含受支持的 OpenSandbox SDK：

```bash
pip install "rl-rock[admin]"
```

源码开发环境使用：

```bash
uv sync --extra admin
```

## 配置

一个 Admin 部署选择一种 Operator：

```yaml
runtime:
  operator_type: opensandbox

opensandbox:
  endpoint: opensandbox.example.com:8090
  protocol: https
  api_key: ""                  # 推荐通过环境变量 OPEN_SANDBOX_API_KEY 提供。
  runtime: docker              # 仅作说明；实际 runtime 由 OpenSandbox Server 选择。
  image_registry_prefix: ""    # 可选：为没有显式 registry 的镜像名添加前缀。
  use_server_proxy: false
  default_timeout: 600

scheduler:
  enabled: false
```

`endpoint` 是 OpenSandbox Server 域名和可选端口，不包含 URL path；协议由 `protocol` 单独配置。
`api_key` 为空时，OpenSandbox SDK 会读取环境变量 `OPEN_SANDBOX_API_KEY`。

`use_server_proxy` 决定命令和文件请求如何到达 `execd`：

- `false` 使用 OpenSandbox Server 返回的端点，ROCK Admin 必须能访问这些端点；
- `true` 经 OpenSandbox Server 转发，仅在目标部署支持 server-proxy 模式时开启。

ROCK 严格遵循该设置，不会失败后自动改走另一条链路。

## 客户端用法

现有 ROCK SDK 对后端保持透明：

```python
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig
from rock.actions import Command

sandbox = Sandbox(
    SandboxConfig(
        image="python:3.11",
        cpus=2,
        memory="4g",
        base_url="http://rock-admin.example.com:8080",
    )
)

await sandbox.start()
result = await sandbox.execute(Command(command="python -V"))
print(result.stdout)
```

Admin 同时保存 ROCK sandbox ID 和 OpenSandbox ID，调用方始终只使用 ROCK ID。

## 能力矩阵

| 能力 | OpenSandbox 后端 | 说明 |
|---|---|---|
| 创建、查询状态、列表 | 支持 | 创建后先返回 `pending`，ROCK 轮询 OpenSandbox 生命周期状态。 |
| 删除运行中的沙箱 | 支持 | 映射为不可逆的 OpenSandbox `kill`。 |
| Stop、Restart | 不支持 | Pause/Resume 要求创建时启用 persistence，当前 ROCK 后端不暴露这组能力。 |
| Archive、Restore、镜像 Commit | 不支持 | 这些路径依赖 ROCK 管理的 worker 存储或 Ray Actor。 |
| 执行命令 | 支持 | `cwd`、显式命令环境变量、超时和退出码检查会映射到 `execd`。 |
| 读文件、写文件、上传文件 | 支持 | 上传时把文件流直接交给 SDK，Admin 不会把整个文件缓存在内存中。 |
| 持久命令 Session | 支持 | Session 名称映射保存在 Redis 中，不同 Admin worker 可复用同一个 session。 |
| 交互式 Session 命令 | 不支持 | `expect` 以及交互式 command/quit 模式会被明确拒绝。 |
| HTTP 服务代理 | 支持 | ROCK 通过 OpenSandbox 解析目标端口，并保留端点要求的鉴权 header。 |
| WebSocket 服务代理 | 支持 | 与 HTTP 代理使用相同的端点发现协议。 |
| WebSocket 上的原始 TCP 端口转发 | 不支持 | 后端会明确拒绝该操作。 |
| Worker 运维 Scheduler | 不适用 | 即使 `scheduler.enabled` 为 true，ROCK 也会跳过依赖 Ray/Rocklet 的 worker scheduler。 |

### Session 环境变量和用户

OpenSandbox session 继承沙箱/容器本身的环境。即使 SDK 请求中 `env_enable=true`，ROCK 也不会跨越信任边界复制
Admin 进程的环境变量。

请通过 session 请求中的显式 `env` 和 `startup_source` 初始化环境变量和 shell 文件。这些命令只在创建 session
时执行一次，该 session 的后续命令可以观察到其效果。

ROCK 无法切换 OpenSandbox session 的运行用户。如果传入 `remote_user`，ROCK 会用 `id -un` 校验；只有它已经
等于沙箱的实际用户时请求才会成功。

## 服务代理

沙箱内监听的应用可继续使用 ROCK 现有代理路由访问。目标端口可通过 path
`/proxy/{sandbox_id}/port/{port}/{path}`、`X-ROCK-Target-Port` header 或 `rock_target_port` query 参数指定，
三种方式只能选择一种。

ROCK 会向 OpenSandbox 生命周期服务查询端点，并转发其返回的鉴权 header。端点既可以是直连地址，也可以是
server-proxy 地址；ROCK 不假设具体的域名路由策略。

## 运维说明

- 多个 Admin worker 必须使用共享 Redis，持久 session 映射保存在其中。
- 不要为该 Operator 启用 Rocklet 专用的 worker 运维任务。OpenSandbox 被选中时，Admin 会记录 warning 并跳过该
  scheduler。
- 把 `use_server_proxy`、端点可达性和鉴权视为部署协议。ROCK 不会在不同链路或不同后端之间静默回退。
- 不要在同一个 Admin 部署后混用 Ray/Kubernetes 与 OpenSandbox 沙箱；Operator 是部署级选择。
