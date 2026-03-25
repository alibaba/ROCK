# Proxy Enhancements — Implementation Plan

## 背景

admin 与 sandbox 不在同一 K8s 集群，`host_ip` 为宿主机 IP，容器内任意端口无法从 admin 直连。因此：

- **WebSocket proxy 自定义端口**：复用 rocklet 现有的 `/portforward` WebSocket 端点中转（与 `/sandboxes/{id}/portforward` 相同机制）
- **HTTP proxy 自定义端口**：需在 rocklet 新增 `/http_proxy` HTTP 端点，admin 转发请求给 rocklet，rocklet 在容器内访问目标服务

---

## File Changes

| 文件 | 修改类型 | 说明 |
|------|------|------|
| `rock/rocklet/local_api.py` | **新增** | 新增 `ANY /http_proxy/{path:path}?port={port}` 端点 |
| `rock/sandbox/service/sandbox_proxy_service.py` | 修改 | `http_proxy` 有 `port` 时改走 rocklet `/http_proxy` 中转；WebSocket proxy 有 `port` 时改走 rocklet `/portforward` 中转 |
| `rock/admin/entrypoints/sandbox_proxy_api.py` | 无变更 | 已支持，无需修改 |

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

---

## Rollback & Compatibility

- **向后兼容**：`rock_target_port` 未指定时，所有逻辑路径与原实现完全一致
- **回滚**：
  - rocklet：还原 `local_api.py`，重新发布镜像
  - admin：还原 `sandbox_proxy_service.py`

---

## 约束与注意事项

- WebSocket proxy 自定义端口时，`path` 参数不生效（rocklet portforward 是纯 TCP 隧道，不感知 HTTP path）
- rocklet `/http_proxy` 端点的 `port` 参数需要校验（复用 `validate_port_forward_port`）
- rocklet 镜像需要重新发布才能生效
