# Proxy Enhancements — Implementation Plan

## File Changes

| 文件 | 修改类型 | 说明 |
|------|------|------|
| `rock/admin/entrypoints/sandbox_proxy_api.py` | 修改 | 1) websocket_proxy 增加 `port` query param；2) post_proxy 改为 api_route 支持所有 method |
| `rock/sandbox/service/sandbox_proxy_service.py` | 修改 | 1) `websocket_proxy` 增加 `port` 参数；2) `get_sandbox_websocket_url` 增加 `port` 参数；3) `post_proxy` 改为 `http_proxy`，透传 method |

---

## 核心逻辑（伪代码）

### 变更 1：WebSocket Proxy 指定端口

```python
# sandbox_proxy_api.py
@sandbox_proxy_router.websocket("/sandboxes/{id}/proxy/ws")
@sandbox_proxy_router.websocket("/sandboxes/{id}/proxy/ws/{path:path}")
async def websocket_proxy(websocket: WebSocket, id: str, path: str = "", port: int | None = None):
    await websocket.accept()
    # 端口校验（仅当 port 显式指定时）
    if port is not None:
        is_valid, error_msg = validate_port_forward_port(port)
        if not is_valid:
            await websocket.close(code=1008, reason=error_msg)
            return
    try:
        await sandbox_proxy_service.websocket_proxy(websocket, id, path, port=port)
    except ...

# sandbox_proxy_service.py
async def websocket_proxy(self, client_websocket, sandbox_id: str,
                          target_path: str | None = None, port: int | None = None):
    target_url = await self.get_sandbox_websocket_url(sandbox_id, target_path, port=port)
    ...  # 其余逻辑不变

async def get_sandbox_websocket_url(self, sandbox_id: str,
                                    target_path: str | None = None,
                                    port: int | None = None) -> str:
    status_dicts = await self.get_service_status(sandbox_id)
    host_ip = status_dicts[0].get("host_ip")
    service_status = ServiceStatus.from_dict(status_dicts[0])
    # port 未指定时使用 SERVER 端口（mapped）
    if port is None:
        target_port = service_status.get_mapped_port(Port.SERVER)
    else:
        target_port = port  # 直接使用用户指定端口（在容器网络内部访问）
    if target_path:
        return f"ws://{host_ip}:{target_port}/{target_path}"
    return f"ws://{host_ip}:{target_port}"
```

> **注意**：用户指定 port 时，该端口是沙箱容器内部端口，需要 docker 端口映射或直接容器网络访问。目前 `portforward` 端点（`/sandboxes/{id}/portforward?port=xxx`）通过 rocklet 中转访问沙箱内任意 TCP 端口。WebSocket proxy 的直连方案需要确认网络拓扑是否支持（host_ip + 容器内端口 vs. 映射端口）。
>
> **推荐实现方式**：与 portforward 对齐，**通过 rocklet `/portforward` 端点中转** WebSocket 流量，或者先确认 `host_ip` 直连沙箱内任意端口是否可行。
>
> **待确认（需用户决策）**：
> - 选项 A：直连 `ws://{host_ip}:{port}/{path}`（简单，但依赖网络拓扑）
> - 选项 B：通过 rocklet portforward 中转（与 portforward 端点保持一致，更安全）

### 变更 2：HTTP Proxy 支持所有 Method

```python
# sandbox_proxy_api.py
# 移除原来的两个 @sandbox_proxy_router.post(...)
# 改为：
@sandbox_proxy_router.api_route(
    "/sandboxes/{sandbox_id}/proxy",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
)
@sandbox_proxy_router.api_route(
    "/sandboxes/{sandbox_id}/proxy/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
)
@handle_exceptions(error_message="http proxy failed")
async def http_proxy(
    sandbox_id: str,
    request: Request,
    path: str = "",
):
    body = None
    # GET / HEAD / DELETE 等通常没有 body
    if request.method not in ("GET", "HEAD", "DELETE", "OPTIONS"):
        try:
            body = await request.json()
        except Exception:
            body = None
    return await sandbox_proxy_service.http_proxy(
        sandbox_id, path, body, request.headers, method=request.method
    )

# sandbox_proxy_service.py
async def http_proxy(
    self,
    sandbox_id: str,
    target_path: str,
    body: dict | None,
    headers: Headers,
    method: str = "POST",
) -> JSONResponse | StreamingResponse | Response:
    """HTTP proxy supporting all methods, with SSE streaming support."""
    await self._update_expire_time(sandbox_id)
    ...
    resp = await client.send(
        client.build_request(
            method=method,          # 透传 method
            url=target_url,
            json=body if body is not None else None,
            headers=request_headers,
            timeout=120,
        ),
        stream=True,
    )
    # 其余响应处理逻辑不变（SSE / JSON / raw）
```

---

## Execution Plan

### Step 1：修改 `get_sandbox_websocket_url` 支持 port 参数
- 文件：`rock/sandbox/service/sandbox_proxy_service.py:646`
- 增加 `port: int | None = None` 参数
- 有 port 时直接使用，无 port 时保持原有 mapped port 逻辑

### Step 2：修改 `websocket_proxy` service 方法传递 port
- 文件：`rock/sandbox/service/sandbox_proxy_service.py:210`
- 增加 `port: int | None = None` 参数，透传给 `get_sandbox_websocket_url`

### Step 3：修改 websocket_proxy API 端点，增加 port query param + 校验
- 文件：`rock/admin/entrypoints/sandbox_proxy_api.py:115`
- 增加 `port: int | None = Query(None)` 参数
- 在 accept 前（或 accept 后）进行端口校验

### Step 4：将 `post_proxy` 改为通用 `http_proxy` service 方法
- 文件：`rock/sandbox/service/sandbox_proxy_service.py:817`
- 增加 `method: str = "POST"` 参数
- 将 `method="POST"` 硬编码改为透传 `method`
- 方法重命名为 `http_proxy`（或保留 `post_proxy` 并内部调用 `http_proxy`，维持兼容）

### Step 5：修改 API 端点注册方式
- 文件：`rock/admin/entrypoints/sandbox_proxy_api.py:183`
- 将两个 `@sandbox_proxy_router.post(...)` 改为 `@sandbox_proxy_router.api_route(..., methods=[...])`
- body 改为从 `request.json()` 动态读取（非强制 Body 参数）
- 调用 `sandbox_proxy_service.http_proxy(..., method=request.method)`

---

## Rollback & Compatibility

- 向后兼容：`port=None` 时 WebSocket proxy 行为与之前完全一致
- 向后兼容：POST 请求仍按 POST 处理，行为不变
- 回滚：还原 `sandbox_proxy_api.py` 和 `sandbox_proxy_service.py` 两个文件即可

---

## 待确认事项（开始实现前需用户决策）

**Q1**：WebSocket proxy 指定端口时，网络访问方式如何？

| 选项 | 方式 | 优点 | 缺点 |
|------|------|------|------|
| **A（推荐）** | 直连 `ws://{host_ip}:{port}` | 简单，与现有 websocket_proxy 逻辑一致 | 要求沙箱所有端口对 admin 可路由 |
| **B** | 通过 rocklet portforward 中转 | 与 portforward 端点一致，更安全 | 需要 rocklet 支持 WS→WS 的转发（与 TCP portforward 不同），实现复杂 |

当前 `get_sandbox_websocket_url` 返回 `ws://{host_ip}:{mapped_server_port}`，说明 admin 可以直连 host_ip。如果沙箱内部端口在宿主机上可路由（docker 桥接网络 or K8s pod IP 直连），选项 A 可行。

**Q2**：HTTP proxy 的 method 是否需要白名单控制（只允许特定 method），还是全部透传？
