# WebSocket TCP 端口代理服务 - 实现计划

## 实现步骤

### 步骤 1: 在 rocklet 中添加端口转发端点

**文件**: `rock/rocklet/local_api.py`

添加 WebSocket 端点：
```python
@sandbox_proxy_router.websocket("/portforward")
async def portforward(websocket: WebSocket, port: int):
    """
    容器内 TCP 端口代理端点。
    
    流程:
    1. 接受 WebSocket 连接
    2. 验证端口 (1024-65535, 排除 22)
    3. 连接到 127.0.0.1:{port}
    4. 双向转发二进制数据
    """
```

核心实现：
- 使用 `asyncio.open_connection("127.0.0.1", port)` 连接本地 TCP 端口
- 创建两个异步任务进行双向数据转发
- WebSocket 使用 `receive_bytes()` 和 `send_bytes()` 处理二进制数据

### 步骤 2: 在 SandboxProxyService 中添加辅助方法

**文件**: `rock/sandbox/service/sandbox_proxy_service.py`

添加方法：
```python
def _validate_port(self, port: int) -> tuple[bool, str | None]:
    """验证端口是否在允许范围内。"""

def _get_rocklet_portforward_url(self, sandbox_status_dict: dict, port: int) -> str:
    """获取 rocklet portforward 端点的 WebSocket URL。"""
```

URL 格式：`ws://{host_ip}:{mapped_port}/portforward?port={target_port}`

### 步骤 3: 在 SandboxProxyService 中添加代理方法

**文件**: `rock/sandbox/service/sandbox_proxy_service.py`

添加方法：
```python
async def websocket_to_tcp_proxy(
    self,
    client_websocket: WebSocket,
    sandbox_id: str,
    port: int,
    tcp_connect_timeout: float = 10.0,
    idle_timeout: float = 300.0,
) -> None:
    """
    将客户端 WebSocket 连接代理到 rocklet 的 portforward 端点。
    
    流程:
    1. 验证端口
    2. 获取沙箱状态
    3. 构建 rocklet portforward URL
    4. 连接到 rocklet WebSocket
    5. 双向转发二进制数据
    """
```

核心实现：
- 使用 `websockets.connect()` 连接 rocklet
- 复用现有的 WebSocket 双向转发模式

### 步骤 4: 添加外部 WebSocket 路由端点

**文件**: `rock/admin/entrypoints/sandbox_proxy_api.py`

添加路由：
```python
@sandbox_proxy_router.websocket("/sandboxes/{id}/portforward")
async def portforward(websocket: WebSocket, id: str, port: int):
    """
    外部 WebSocket TCP 端口代理端点。
    """
```

## 架构说明

```
客户端 ──WebSocket──▶ Proxy服务 ──WebSocket──▶ rocklet ──TCP──▶ 目标端口
                         │                        │
                   外部代理层                   内部代理层
              (sandbox_proxy_api.py)       (local_api.py)
```

**为什么需要两层？**

Docker 容器只暴露预定义端口（如 8080），无法直接访问容器内动态启动的服务端口。rocklet 运行在容器内，可以直接访问 `127.0.0.1:{任意端口}`。

## 依赖

无需新增外部依赖：
- `asyncio` - TCP 连接和异步任务
- `websockets` - WebSocket 客户端（已依赖）

## 测试计划

### 单元测试
1. 端口验证逻辑 (`_validate_port`)
2. rocklet portforward URL 构建 (`_get_rocklet_portforward_url`)
3. rocklet 端点存在性验证

### 集成测试
1. 正常连接和断开
2. 双向二进制数据传输
3. 端口限制验证
4. 超时处理
5. 多客户端并发连接