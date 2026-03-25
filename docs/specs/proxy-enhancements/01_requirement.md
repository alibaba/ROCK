# Proxy Enhancements — Requirement Spec

## Background

ROCK Admin 目前提供两类代理能力：

1. **WebSocket Proxy** (`/sandboxes/{id}/proxy/ws[/{path}]`)：将 WebSocket 连接转发到沙箱内的固定 SERVER 端口（`Port.SERVER = 8080`），不支持用户指定目标端口。
2. **HTTP Proxy** (`/sandboxes/{sandbox_id}/proxy[/{path}]`)：将 HTTP 请求转发到沙箱内的 SERVER 端口，但硬编码为 `method="POST"`，不支持 GET / PUT / DELETE / PATCH 等其他 HTTP 方法。

这两个限制阻碍了以下场景：
- 沙箱内运行了多个 WebSocket 服务（如 Jupyter Kernel、VS Code Server、自定义推理服务），需要连接到不同端口
- 沙箱内服务使用 RESTful 风格 API，需要 GET 查询、PUT 更新、DELETE 删除

---

## In / Out

### In（本次要做的）

1. **WebSocket Proxy 支持用户指定目标端口**
   - 在现有 `/sandboxes/{id}/proxy/ws[/{path}]` 路由上，允许通过 query param `port` 指定目标 WebSocket 端口
   - 当 `port` 未指定时，保持现有行为（使用 `Port.SERVER = 8080`）
   - 端口合法性校验：范围 1024–65535，排除 SSH（22）

2. **HTTP Proxy 支持所有 HTTP Method**
   - 将 `post_proxy` 扩展为通用 `http_proxy`，透传客户端的原始 method（GET/POST/PUT/DELETE/PATCH 等）
   - API 路径从 `POST /sandboxes/{sandbox_id}/proxy[/{path}]` 改为 `ANY /sandboxes/{sandbox_id}/proxy[/{path}]`
   - 对于 GET/DELETE 等无 body 的请求，body 参数为可选
   - 保持 SSE streaming 支持

### Out（本次不做的）

- WebSocket Proxy 的认证/鉴权增强
- HTTP Proxy 的 multipart/form-data 支持（upload 接口已单独处理）
- `host_proxy` 的 method 扩展（范围外）
- SDK 客户端侧的封装更新

---

## Acceptance Criteria

- **AC1**：`ws://admin/sandboxes/{id}/proxy/ws?port=8888` 能成功代理到沙箱内 8888 端口的 WebSocket 服务
- **AC2**：`ws://admin/sandboxes/{id}/proxy/ws?port=8080` 与不带 `port` 参数的行为一致（向后兼容）
- **AC3**：`port` 参数不合法时（< 1024、> 65535、= 22），WebSocket 连接以 code=1008 关闭并返回错误信息
- **AC4**：`GET /sandboxes/{sandbox_id}/proxy/hello` 能成功代理 GET 请求到沙箱内服务
- **AC5**：`DELETE /sandboxes/{sandbox_id}/proxy/items/1` 能成功代理 DELETE 请求
- **AC6**：原有 `POST /sandboxes/{sandbox_id}/proxy` 行为不变（向后兼容）
- **AC7**：SSE streaming（`text/event-stream`）在所有 method 下仍然正常工作

---

## Constraints

- 不引入新的外部依赖
- 不修改 `Port.SERVER` / `Port.PROXY` 枚举定义
- `port_validation` 逻辑复用现有 `validate_port_forward_port`（但 WebSocket proxy 端口校验需新增对 `Port.SERVER = 8080` 的允许，或直接用同一校验函数）
- 保持现有 portforward 端点（`/sandboxes/{id}/portforward`）不变

---

## Risks & Rollout

- **风险**：WebSocket proxy 中用户可以指定任意端口访问沙箱内服务，存在横向访问风险 → 通过 `sandbox_id` 鉴权已覆盖，端口范围校验作为防护层
- **回滚**：修改仅在 `sandbox_proxy_api.py` 和 `sandbox_proxy_service.py` 内，回滚只需还原这两个文件
- **上线策略**：无数据库变更，直接部署
