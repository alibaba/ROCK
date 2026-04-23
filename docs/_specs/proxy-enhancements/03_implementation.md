# Proxy Enhancements — Implementation Plan

## 背景

admin 与 sandbox 不在同一 K8s 集群，`host_ip` 为宿主机 IP，容器内任意端口无法从 admin 直连。因此：

- **WebSocket proxy 自定义端口**：复用 rocklet 现有的 `/portforward` WebSocket 端点中转（与 `/sandboxes/{id}/portforward` 相同机制）
- **HTTP proxy 自定义端口**：需在 rocklet 新增 `/http_proxy` HTTP 端点，admin 转发请求给 rocklet，rocklet 在容器内访问目标服务

除了端口和 method 能力之外，当前 WebSocket proxy 还存在握手上下文丢失问题：
- `sandbox_proxy_service.websocket_proxy()` 在调用 `websockets.connect(...)` 时，目前只传了 `subprotocols`
- 下游服务收到的是 Admin 重新发起的二跳握手，请求来源会表现为 `Python websockets/...`，而不是客户端原始握手上下文
- 结果是 `Origin`、`Authorization`、`Cookie`、`X-Forwarded-*` 等头全部丢失，依赖这些头的服务会报 `origin not allowed` 或鉴权失败

---

## File Changes

| 文件 | 修改类型 | 说明 |
|------|------|------|
| `rock/rocklet/local_api.py` | **新增** | 新增 `ANY /http_proxy/{path:path}?port={port}` 端点 |
| `rock/sandbox/service/sandbox_proxy_service.py` | 修改 | `http_proxy` 有 `port` 时改走 rocklet `/http_proxy` 中转；WebSocket proxy 有 `port` 时改走 rocklet `/portforward` 中转；补充 WebSocket 通用 headers 白名单透传 |
| `rock/admin/entrypoints/sandbox_proxy_api.py` | 无变更 | 路由签名保持不变，无需调整 |
| `tests/unit/sandbox/test_websocket_proxy_subprotocol.py` | 修改 | 增加 `Origin` / `additional_headers` 透传与禁转头测试 |

---

## 核心逻辑

### 变更 1：WebSocket proxy 自定义端口 → rocklet portforward 中转

当前 `get_sandbox_websocket_url` 在有 port 时直接返回 `ws://{host_ip}:{port}`，这在跨集群部署下不可达。

**修改后逻辑**：

```python
async def get_sandbox_websocket_url(
    self, sandbox_id: str, target_path: str | None = None, port: int | None = None
) -> str:
    status_dicts = await self.get_service_status(sandbox_id)
    host_ip = status_dicts[0].get("host_ip")
    service_status = ServiceStatus.from_dict(status_dicts[0])

    if port is None:
        # 默认行为：连接 SERVER 映射端口（原逻辑不变）
        target_port = service_status.get_mapped_port(Port.SERVER)
        if target_path:
            return f"ws://{host_ip}:{target_port}/{target_path}"
        return f"ws://{host_ip}:{target_port}"
    else:
        # 自定义端口：通过 rocklet portforward 中转
        rocklet_port = service_status.get_mapped_port(Port.PROXY)
        return f"ws://{host_ip}:{rocklet_port}/portforward?port={port}"
        # 注意：target_path 在此场景下通过 WebSocket 协议层传递，不拼入 URL
```

> **注意**：WebSocket proxy 自定义端口时，`target_path` 无法通过 rocklet portforward 传递（rocklet portforward 是纯 TCP 隧道）。如需支持 path，需评估是否在 rocklet portforward 层扩展，本期暂不支持 path + 自定义端口的组合。

### 变更 2：rocklet 新增 HTTP proxy 端点

```python
# rock/rocklet/local_api.py

@local_router.api_route(
    "/http_proxy",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
)
@local_router.api_route(
    "/http_proxy/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
)
async def http_proxy(request: Request, port: int, path: str = ""):
    """Forward HTTP request to localhost:{port}/{path} inside the container."""
    target_url = f"http://localhost:{port}/{path}"

    EXCLUDED_HEADERS = {"host", "content-length", "transfer-encoding"}
    headers = {k: v for k, v in request.headers.items() if k.lower() not in EXCLUDED_HEADERS}

    body = None
    if request.method not in ("GET", "HEAD", "DELETE", "OPTIONS"):
        body = await request.body()

    async with httpx.AsyncClient(timeout=httpx.Timeout(None)) as client:
        resp = await client.send(
            client.build_request(
                method=request.method,
                url=target_url,
                content=body,
                headers=headers,
            ),
            stream=True,
        )
        # 响应透传（支持 SSE streaming）
        ...
```

### 变更 3：admin `http_proxy` service 有 port 时走 rocklet 中转

```python
async def http_proxy(self, sandbox_id, target_path, body, headers, method="POST", port=None):
    await self._update_expire_time(sandbox_id)
    status_list = await self.get_service_status(sandbox_id)
    host_ip = status_list[0].get("host_ip")
    service_status = ServiceStatus.from_dict(status_list[0])

    if port is None:
        # 默认行为：直连 mapped SERVER port（原逻辑不变）
        target_port = service_status.get_mapped_port(Port.SERVER)
        target_url = f"http://{host_ip}:{target_port}/{target_path}"
    else:
        # 自定义端口：通过 rocklet /http_proxy 中转
        rocklet_port = service_status.get_mapped_port(Port.PROXY)
        target_url = f"http://{host_ip}:{rocklet_port}/http_proxy/{target_path}?port={port}"

    # 其余请求构建和响应处理逻辑不变
    ...
```

### 变更 4：WebSocket proxy 通用 headers 黑名单过滤透传

需要在 `sandbox_proxy_service.websocket_proxy()` 内新增一层 header 提取和过滤逻辑，将客户端握手里的”通用请求头”转为上游二跳握手参数。

**设计要点**：

1. **将 `Origin` 单独处理**
   - `websockets.connect()` 在 15.0.1 版本里提供 `origin=` 参数
   - `Origin` 不作为普通 `additional_headers` 重复透传，避免语义混乱和重复 header

2. **其余头走黑名单过滤**
   - 黑名单：WebSocket 握手专用头（`Host`、`Connection`、`Upgrade`、`Sec-WebSocket-Key`、`Sec-WebSocket-Version`、`Sec-WebSocket-Extensions`、`Sec-WebSocket-Protocol`）和 hop-by-hop 头（`Transfer-Encoding`、`TE`、`Trailer`、`Keep-Alive`、`Proxy-Authorization`、`Proxy-Connection`、`Content-Length`）
   - 不在黑名单中的头默认转发，确保用户自定义 header 能到达下游

3. **无可转发头时保持兼容**
   - 若客户端未携带任何非黑名单头，则 `origin=None`、`additional_headers=None`
   - 这样代理行为与当前实现保持一致，不引入额外副作用

**实现代码**（`rock/sandbox/utils/proxy.py`）：

```python
BLOCKED_WS_HEADER_NAMES = {
    “host”,
    “connection”,
    “upgrade”,
    “sec-websocket-key”,
    “sec-websocket-version”,
    “sec-websocket-extensions”,
    “sec-websocket-protocol”,
    “transfer-encoding”,
    “te”,
    “trailer”,
    “keep-alive”,
    “proxy-authorization”,
    “proxy-connection”,
    “content-length”,
}


def build_upstream_ws_headers(client_websocket):
    origin = client_websocket.headers.get(“origin”) or client_websocket.headers.get(“Origin”)
    additional_headers = []

    for key, value in client_websocket.headers.items():
        lower_key = key.lower()
        if lower_key == “origin”:
            continue
        if lower_key in BLOCKED_WS_HEADER_NAMES:
            continue
        additional_headers.append((key, value))

    return origin, additional_headers or None
```

接入方式：

```python
origin, additional_headers = build_upstream_ws_headers(client_websocket)

async with websockets.connect(
    target_url,
    ping_interval=None,
    ping_timeout=None,
    origin=origin,
    additional_headers=additional_headers,
    subprotocols=upstream_subprotocols,
) as target_websocket:
    ...
```

### 变更 5：测试覆盖扩展

现有测试主要覆盖子协议转发，需要补充 header 透传相关单测。

**新增测试点**：
- `Origin` 存在时，`websockets.connect()` 收到相同 `origin=`
- 已知头（`Authorization`、`Cookie`、`X-Forwarded-*`、`X-Request-Id`、`Traceparent`、`EagleEye-*`）存在时，`websockets.connect()` 收到 `additional_headers`
- 用户自定义头（如 `x-my-custom`）能被正常转发到下游
- 黑名单头（`Host`、`Connection`、`Upgrade`、`Sec-WebSocket-Key`、`Sec-WebSocket-Version`、`Sec-WebSocket-Extensions` 等）不得出现在 `additional_headers`
- `Sec-WebSocket-Protocol` 继续通过 `subprotocols=` 转发，不能出现在 `additional_headers`
- 无可转发头时，`origin` / `additional_headers` 为 `None`，保持向后兼容

---

## Execution Plan

### Step 1：rocklet 新增 `/http_proxy` 端点
- 文件：`rock/rocklet/local_api.py`
- 新增 `ANY /http_proxy` 和 `ANY /http_proxy/{path:path}` 路由
- 接收 `port: int` query 参数，转发到 `http://localhost:{port}/{path}`
- 支持 body 透传、header 透传（排除 hop-by-hop headers）
- 支持 SSE streaming 响应

### Step 2：修改 `get_sandbox_websocket_url`
- 文件：`rock/sandbox/service/sandbox_proxy_service.py`
- 有 `port` 时，改用 `ws://{host_ip}:{rocklet_mapped_port}/portforward?port={port}`
- 无 `port` 时保持原逻辑不变

### Step 3：修改 `http_proxy` service 方法
- 文件：`rock/sandbox/service/sandbox_proxy_service.py`
- 有 `port` 时，改用 `http://{host_ip}:{rocklet_mapped_port}/http_proxy/{path}?port={port}`
- 无 `port` 时保持原逻辑不变

### Step 4：新增 WebSocket 通用 header 黑名单过滤与透传逻辑
- 文件：`rock/sandbox/utils/proxy.py`（独立模块）、`rock/sandbox/service/sandbox_proxy_service.py`（调用方）
- 新增 `build_upstream_ws_headers()` helper，负责从 `client_websocket.headers` 中提取 `Origin` 并通过黑名单过滤 `additional_headers`
- 在 `websocket_proxy()` 调用 `websockets.connect()` 时传入 `origin=` 和 `additional_headers=`
- 保持现有 `subprotocols=` 协商逻辑不变

### Step 5：补充 WebSocket header 透传测试
- 文件：`tests/unit/sandbox/test_websocket_proxy_headers.py`
- 新增 `Origin` 透传、已知 header 透传、自定义 header 透传、黑名单 header 过滤、兼容性测试

---

## Rollback & Compatibility

- **向后兼容**：`rock_target_port` 未指定时，所有逻辑路径与原实现完全一致
- **向后兼容**：客户端未携带任何白名单 header 时，WebSocket 二跳握手行为与现状一致
- **回滚**：
  - rocklet：还原 `local_api.py`，重新发布镜像
  - admin：还原 `sandbox_proxy_service.py` 和对应单元测试

---

## 约束与注意事项

- WebSocket proxy 自定义端口时，`path` 参数不生效（rocklet portforward 是纯 TCP 隧道，不感知 HTTP path）
- rocklet `/http_proxy` 端点的 `port` 参数需要校验（复用 `validate_port_forward_port`）
- rocklet 镜像需要重新发布才能生效
- WebSocket header 透传采用黑名单策略，排除握手专用头和 hop-by-hop 头，允许用户自定义 header 透传
- `Origin` 应通过 `websockets.connect(origin=...)` 传入；不要与 `additional_headers` 重复
- `Sec-WebSocket-Protocol` 必须继续通过 `subprotocols=` 传递，避免和普通 header 透传逻辑冲突
