# Proxy Enhancements — Interface Contract

## 1. WebSocket Proxy（支持指定端口）

### Endpoint

```
WS /sandboxes/{id}/proxy/ws
WS /sandboxes/{id}/proxy/ws/{path:path}
```

### 变更

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | path | ✅ | sandbox_id |
| `path` | path | ❌ | 目标路径，默认空字符串 |
| `rock_target_port` | query | ❌ | 目标 WebSocket 端口，默认 8080（Port.SERVER）|
| `X-ROCK-Target-Port` | header | ❌ | 目标 WebSocket 端口，默认 8080（Port.SERVER）|

### 行为规则

- `rock_target_port` 和 `X-ROCK-Target-Port` 都未指定 → 使用 `Port.SERVER = 8080` 的映射端口（向后兼容）
- 仅 `rock_target_port` 或仅 `X-ROCK-Target-Port` 指定 → 通过 rocklet `/portforward` WebSocket 端点中转到容器内目标端口
- `rock_target_port` 和 `X-ROCK-Target-Port` 同时指定 → WebSocket close code=1008，reason=错误信息
- 端口非法（< 1024、> 65535、= 22）→ WebSocket close code=1008，reason=错误信息

> **实现说明**：admin 与 sandbox 不在同一 K8s 集群，`host_ip` 为宿主机 IP，容器内端口无法直连。因此自定义端口时复用 rocklet 的 WebSocket portforward 机制（与 `/sandboxes/{id}/portforward` 端点相同），通过 `ws://{host_ip}:{rocklet_mapped_port}/portforward?port={rock_target_port}` 中转，rocklet 在容器内访问 `localhost:{rock_target_port}`。

### 错误响应（WebSocket Close Frame）

| Code | 原因 | 场景 |
|------|------|------|
| 1008 | `Port {port} is below minimum allowed port 1024` | port < 1024 |
| 1008 | `Port {port} is not allowed for port forwarding` | port = 22 |
| 1008 | `Port {port} is above maximum allowed port 65535` | port > 65535 |
| 1011 | `Proxy error: ...` | 其他代理错误 |

### Examples

```
# 连接到 8888 端口（如 Jupyter）
WS ws://admin-host/sandboxes/my-sandbox/proxy/ws?rock_target_port=8888

# 连接到 8888 端口下的特定路径
WS ws://admin-host/sandboxes/my-sandbox/proxy/ws/api/kernels/xxx/channels?rock_target_port=8888

# 使用 header 指定端口
WS ws://admin-host/sandboxes/my-sandbox/proxy/ws
Headers: X-ROCK-Target-Port: 8888

# 不带 rock_target_port（向后兼容，使用 8080）
WS ws://admin-host/sandboxes/my-sandbox/proxy/ws
```

---

## 2. HTTP Proxy（支持所有 Method + 自定义端口）

### Endpoint

```
ANY /sandboxes/{sandbox_id}/proxy
ANY /sandboxes/{sandbox_id}/proxy/{path:path}
```

> 注：FastAPI 使用 `@router.api_route(..., methods=["GET","POST","PUT","DELETE","PATCH","HEAD","OPTIONS"])` 实现

### 变更

- 原 `POST only` → 支持所有 HTTP method，透传原始 method 给沙箱内服务
- 新增 `rock_target_port` query 参数，支持指定沙箱内任意 HTTP 服务端口

> **实现说明**：需在 rocklet 新增 `ANY /http_proxy/{path:path}?port={port}` 端点，admin 将请求转发到 `http://{host_ip}:{rocklet_mapped_port}/http_proxy/{path}?port={rock_target_port}`，rocklet 在容器内访问 `http://localhost:{port}/{path}`。未指定 `rock_target_port` 时保持原有逻辑（直连 mapped SERVER port），向后兼容。

### Request

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `sandbox_id` | path | ✅ | sandbox_id |
| `path` | path | ❌ | 转发路径 |
| `rock_target_port` | query | ❌ | 目标 HTTP 端口，默认 8080（Port.SERVER）|
| `X-ROCK-Target-Port` | header | ❌ | 目标 HTTP 端口，默认 8080（Port.SERVER）|
| `body` | body | ❌ | 请求体（GET/DELETE 时可为空）|
| Headers | - | - | 透传，排除 `host`、`content-length`、`transfer-encoding` |

### 行为规则

- `rock_target_port` 和 `X-ROCK-Target-Port` 都未指定 → 使用 `Port.SERVER = 8080` 的映射端口（向后兼容）
- 仅 `rock_target_port` 或仅 `X-ROCK-Target-Port` 指定 → 转发到指定端口
- `rock_target_port` 和 `X-ROCK-Target-Port` 同时指定 → 返回 400 错误

### Response

与原 `post_proxy` 行为一致：

| Content-Type | 响应类型 |
|------|------|
| `application/json` | JSONResponse |
| `text/event-stream` | StreamingResponse（SSE）|
| 其他 | Response（raw bytes）|

### Examples

```
# GET 查询
GET /sandboxes/my-sandbox/proxy/v1/models

# POST（向后兼容）
POST /sandboxes/my-sandbox/proxy/v1/chat/completions
Body: {"model": "gpt-4", "messages": [...]}

# DELETE
DELETE /sandboxes/my-sandbox/proxy/items/42

# PUT 更新
PUT /sandboxes/my-sandbox/proxy/config
Body: {"key": "value"}
```

---

---

## 3. HTTP Proxy 路径前缀路由（Path-Based Port）

### 背景

`rock_target_port` query 参数方案适合单次 API 调用，但对浏览器场景（如 VNC、Jupyter）不适用：浏览器加载 HTML 后发起的静态资源请求（JS、CSS、图片）不会自动携带 query 参数，导致后续请求无法路由到正确端口。

路径前缀方案将端口嵌入路径，浏览器相对路径请求可自然继承端口信息。

### Endpoint

```
ANY /sandboxes/{sandbox_id}/proxy/port/{port}
ANY /sandboxes/{sandbox_id}/proxy/port/{port}/{path:path}
```

### 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `sandbox_id` | path | ✅ | sandbox_id |
| `port` | path | ✅ | 目标 HTTP 服务端口，须通过端口合法性校验 |
| `path` | path | ❌ | 转发路径，默认空字符串 |
| body | body | ❌ | 请求体（GET/DELETE 时可为空）|
| Headers | - | - | 透传，排除 `host`、`content-length`、`transfer-encoding` |

### 行为规则

- 等价于 `ANY /sandboxes/{sandbox_id}/proxy/{path}?rock_target_port={port}`
- `port` 须通过 `validate_port_forward_port` 校验，非法时返回 HTTP 400
- 响应处理与现有 `http_proxy` 完全一致（JSON / SSE / raw bytes）
- **注册优先级**：此路由须在 `/sandboxes/{sandbox_id}/proxy/{path:path}` 之前注册，避免 FastAPI 路由匹配歧义

### Examples

```
# 访问沙箱内 8006 端口（VNC 入口）
GET /sandboxes/my-sandbox/proxy/port/8006/

# 访问 VNC 静态资源（浏览器自动跟随相对路径）
GET /sandboxes/my-sandbox/proxy/port/8006/core/rfb.js
GET /sandboxes/my-sandbox/proxy/port/8006/app/styles/base.css

# 访问 Jupyter（8888）
GET /sandboxes/my-sandbox/proxy/port/8888/api/kernels
```

---

## 4. WebSocket Proxy 路径前缀路由（Path-Based Port）

### Endpoint

```
WS /sandboxes/{id}/proxy/port/{port}/ws
WS /sandboxes/{id}/proxy/port/{port}/ws/{path:path}
```

### 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | path | ✅ | sandbox_id |
| `port` | path | ✅ | 目标 WebSocket 端口，须通过端口合法性校验 |
| `path` | path | ❌ | 目标路径，默认空字符串 |

### 行为规则

- 等价于 `WS /sandboxes/{id}/proxy/ws/{path}?rock_target_port={port}`
- `port` 须通过 `validate_port_forward_port` 校验，非法时 WebSocket close code=1008
- 其余行为（portforward 中转、错误处理）与 Section 1 完全一致

### Examples

```
# VNC WebSocket 连接（noVNC websockify）
WS ws://admin-host/sandboxes/my-sandbox/proxy/port/8006/ws
WS ws://admin-host/sandboxes/my-sandbox/proxy/port/8006/ws/websockify

# Jupyter kernel channel
WS ws://admin-host/sandboxes/my-sandbox/proxy/port/8888/ws/api/kernels/xxx/channels
```

---

## 关于向后兼容

- 所有原来使用 `POST /sandboxes/{sandbox_id}/proxy` 的调用**无需修改**，行为不变
- 原 WebSocket `WS /sandboxes/{id}/proxy/ws`（不带 rock_target_port）的调用**无需修改**，行为不变
- 新增路径前缀路由（Section 3、4）为扩展接口，不影响任何现有调用
