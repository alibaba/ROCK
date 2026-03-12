# WebSocket TCP 端口代理服务

## 1. 概述

在 ROCK 沙箱代理服务层实现 **WebSocket → TCP** 端口代理，允许客户端通过 WebSocket 连接访问沙箱容器内监听的 TCP 端口。

## 2. 架构设计

### 2.1 架构图

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────────────────┐
│ WebSocket 客户端 │────▶│  Proxy 服务     │────▶│  rocklet (容器内 8080 端口)  │
└─────────────────┘     └─────────────────┘     └─────────────────────────────┘
                        /portforward?port=X              │
                                                         │ TCP 连接
                                                         ▼
                                                ┌─────────────────┐
                                                │  沙箱内 TCP 端口  │
                                                │  (如 3000, 8080) │
                                                └─────────────────┘
```

### 2.2 两层代理架构

| 层级 | 组件 | 职责 |
|-----|------|------|
| **外部代理层** | `sandbox_proxy_service.py` | 接收客户端 WebSocket 连接，转发到 rocklet |
| **内部代理层** | `rocklet/local_api.py` | 在容器内连接目标 TCP 端口，实现实际代理 |

### 2.3 为什么需要两层？

Docker 容器默认只暴露预定义端口（如 8080），无法直接从外部访问容器内动态启动的服务端口。通过 rocklet 层代理，可以访问容器内任意 TCP 端口。

## 3. 功能规格

### 3.1 外部 API（客户端调用）

| 项目 | 规格 |
|-----|------|
| **端点路径** | `WS /sandboxes/{sandbox_id}/portforward?port={port}` |
| **sandbox_id** | 沙箱实例的唯一标识符 |
| **port** | Query 参数，指定要代理的目标 TCP 端口号 |

**示例：**
```
ws://localhost:8080/sandboxes/abc123/portforward?port=3000
```

### 3.2 内部 API（rocklet 端点）

| 项目 | 规格 |
|-----|------|
| **端点路径** | `WS /portforward?port={port}` |
| **port** | Query 参数，指定容器内的目标 TCP 端口号 |
| **监听端口** | rocklet 监听 `Port.PROXY` (22555)，通过 Docker 映射到宿主机随机端口 |

### 3.3 数据传输

| 项目 | 规格 |
|-----|------|
| **协议** | WebSocket |
| **数据格式** | 二进制消息（Binary Frame） |
| **转发方式** | 原样透传，不修改数据内容 |

### 3.4 端口安全限制

| 项目 | 规格 |
|-----|------|
| **允许范围** | 1024-65535 |
| **排除端口** | 22 (SSH) |
| **拒绝行为** | WebSocket 关闭码 1008 (Policy Violation) |

### 3.5 连接生命周期

| 场景 | 行为 |
|-----|------|
| **连接建立** | 外部代理 → rocklet → TCP 端口，逐层建立连接 |
| **一对一映射** | 每个 WebSocket 连接对应一个独立的 TCP 连接 |
| **多客户端** | 多个 WebSocket 客户端连接同一端口，各自创建独立的 TCP 连接 |
| **TCP 断开** | 关闭 rocklet WebSocket → 关闭客户端 WebSocket |
| **客户端断开** | 关闭 rocklet WebSocket → 关闭 TCP 连接 |

### 3.6 超时配置

| 超时类型 | 时长 | 说明 |
|---------|------|------|
| **连接超时** | 10 秒 | 建立到 rocklet 或 TCP 端口的连接最大等待时间 |
| **空闲超时** | 300 秒 | 连接无数据传输后自动关闭 |

### 3.7 错误处理

| 错误场景 | 处理方式 |
|---------|---------|
| **端口不在允许范围** | WebSocket 关闭码 1008，提示端口不允许 |
| **端口被排除（如 22）** | WebSocket 关闭码 1008，提示端口不允许 |
| **沙箱不存在** | WebSocket 关闭码 1011，提示沙箱未启动 |
| **TCP 连接超时** | WebSocket 关闭码 1011，提示连接超时 |
| **TCP 连接被拒绝** | WebSocket 关闭码 1011，提示连接失败 |
| **传输中断** | WebSocket 关闭码 1011 |

### 3.8 认证授权

不需要额外的认证/授权机制。

## 4. 实现位置

| 文件 | 修改内容 |
|-----|---------|
| `rock/rocklet/local_api.py` | 添加 `/portforward` WebSocket 端点，实现容器内 TCP 代理 |
| `rock/sandbox/service/sandbox_proxy_service.py` | 添加 `websocket_to_tcp_proxy()` 方法和 `_get_rocklet_portforward_url()` 方法 |
| `rock/admin/entrypoints/sandbox_proxy_api.py` | 添加外部 WebSocket 路由端点 |

## 5. 参考设计

参考 Kubernetes port-forward 的 WebSocket 实现：
- URL: `/api/v1/namespaces/{namespace}/pods/{name}/portforward?ports={port}`
- 数据流：WebSocket 双向透传 TCP 数据