---
sidebar_position: 6
---

# 沙箱代理 (Sandbox Proxy)

ROCK Admin 提供了一层代理能力，可以让你**从集群外部**访问运行在沙箱**内部**的服务，而无需为每个沙箱单独分配公网地址。代理支持两种传输模式：

| 模式 | 接入路径 | 适用场景 |
|------|---------|---------|
| HTTP 代理 | `/sandboxes/:sandbox_id/proxy/...` | REST API、Web UI、文件下载等任何 HTTP/1.1 流量 |
| WebSocket 代理 | `ws(s)://.../sandboxes/:sandbox_id/proxy/...` | 实时通道、流式输出、浏览器端 WS 客户端 |

两种模式都通过 `sandbox_id` 路由。沙箱本身**不需要**公网 IP — Admin 负责接收客户端请求，并将其转发到集群内对应的运行时实例。

---

## 1. HTTP 代理

将任意 HTTP 请求转发到沙箱内部的服务。

### 接入路径

```text
方法 (Methods) : GET | POST | PUT | DELETE | PATCH | HEAD | OPTIONS
URL            : $ROCK_BASE_URL/sandboxes/:sandbox_id/proxy[/:path]
```

- 请求方法、请求头、查询字符串和请求体会原样转发到目标服务。
- 目标服务的响应（状态码、响应头、响应体）会被流式回传给客户端。

### 指定目标端口

代理需要知道要访问沙箱内的哪个端口。共有三种方式可以指定 — **只能选其一**，同时使用多种方式会返回 `400 Bad Request`。

| 优先级 | 指定方式 | 示例 |
|--------|---------|------|
| 1 | **路径前缀** | `/sandboxes/abc/proxy/port/8080/api/users` |
| 2 | **请求头** | `X-ROCK-Target-Port: 8080` |
| 3 | **查询参数** | `?rock_target_port=8080` |

如果不指定目标端口，请求会被路由到沙箱的默认服务端口。

### 使用示例

```bash
# 通过路径方式指定端口
curl -X POST \
  "$ROCK_BASE_URL/sandboxes/sb-123/proxy/port/8080/v1/predict" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "hello"}'

# 通过请求头方式指定端口
curl -X POST \
  "$ROCK_BASE_URL/sandboxes/sb-123/proxy/v1/predict" \
  -H "X-ROCK-Target-Port: 8080" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "hello"}'

# 通过查询参数指定端口，并附带其他业务查询参数
curl "$ROCK_BASE_URL/sandboxes/sb-123/proxy/items?rock_target_port=8080&limit=10"
```

---

## 2. WebSocket 代理

适用于需要 `ws://`（或 `wss://`）协议的服务 — 常见于流式输出、聊天、终端会话或任何全双工通道。

### 接入路径

```text
ws(s)://$ROCK_BASE_URL/sandboxes/:sandbox_id/proxy/:path
```

- 原始的 WebSocket 握手信息（subprotocol、自定义 header）会被透传。
- 文本帧和二进制帧在双向之间透明转发。
- 任意一端关闭连接，都会同步关闭对端的上游连接。

### 指定目标端口

与 HTTP 代理完全相同 — 支持路径、请求头、查询参数三种方式，同样**只能选其一**：

| 优先级 | 指定方式 | 示例 |
|--------|---------|------|
| 1 | 路径前缀 | `/sandboxes/abc/proxy/port/9000/socket` |
| 2 | 请求头 | `X-ROCK-Target-Port: 9000` |
| 3 | 查询参数 | `?rock_target_port=9000` |

如果端口不合法（参见[端口限制](#3-端口限制)），WebSocket 会立刻以状态码 `1008`（策略违反）关闭。

### 使用示例

```bash
# 使用 wscat
wscat -c "$ROCK_WS_BASE/sandboxes/sb-123/proxy/port/9000/events"
```

```javascript
// 浏览器端
const ws = new WebSocket(
  "wss://rock.example.com/sandboxes/sb-123/proxy/events?rock_target_port=9000"
);
ws.onmessage = (evt) => console.log(evt.data);
ws.send("ping");
```

---

## 3. 端口限制

WebSocket 代理对**沙箱内的目标端口**有以下限制：

| 规则 | 允许范围 / 值 |
|------|--------------|
| 最小端口 | `1024` |
| 最大端口 | `65535` |
| 禁用端口 | `22`（SSH） |

违反上述规则的请求会被拒绝：

- **HTTP 代理** → 返回 `400 Bad Request`，并在 `detail` 字段中说明原因。
- **WebSocket 代理** → 连接以状态码 `1008` 关闭，并在 `reason` 中给出原因。

> `1024` 以下的端口为系统保留特权端口，因此被禁止；`22` 端口被禁止则是为了避免无意中暴露 SSH 服务。

---

## 4. 常见错误对照

| 现象 | 可能原因 |
|------|---------|
| `400 Bad Request: Cannot specify target port via multiple sources` | 同时使用了路径、请求头、查询参数中的两种以上方式指定端口 — 只保留其中一种。 |
| `400 Bad Request` / WS `1008` 且提示端口范围错误 | 目标端口小于 `1024`、大于 `65535` 或等于 `22`。 |
| WS 关闭码 `1011`（`Proxy error: ...`） | 沙箱内的上游服务返回了错误，或无法被连通。请确认目标端口上确实有服务在监听。 |
| 上游返回 `404 Not Found` | 沙箱内服务并不存在该 HTTP 路径 — 应检查目标服务的路由，而不是代理 URL。 |
| 握手阶段连接卡住 | 沙箱可能仍在初始化。请先调用 `is_alive` 接口确认沙箱已就绪。 |

---

## 5. 快速选型

```text
要调用沙箱内的 REST API？        → HTTP 代理
要连接沙箱内的 WebSocket 服务？  → WebSocket 代理
```

## 相关文档

- [API 参考](../References/api.md)
- [配置指南](configuration.md)
- [快速开始](../Getting%20Started/quickstart.md)
