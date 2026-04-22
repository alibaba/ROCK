# Proxy Enhancements — Requirement Spec

## Background

ROCK Admin 目前提供两类代理能力：

1. **WebSocket Proxy** (`/sandboxes/{id}/proxy/ws[/{path}]`)：将 WebSocket 连接转发到沙箱内的固定 SERVER 端口（`Port.SERVER = 8080`），不支持用户指定目标端口。
2. **HTTP Proxy** (`/sandboxes/{sandbox_id}/proxy[/{path}]`)：将 HTTP 请求转发到沙箱内的固定 SERVER 端口（`Port.SERVER = 8080`），且硬编码为 `method="POST"`，不支持 GET / PUT / DELETE / PATCH 等其他 HTTP 方法，也不支持用户指定目标端口。

除了端口和 method 能力不足之外，WebSocket proxy 还有一个上下文丢失问题：
- Admin 在转发 WebSocket 握手到下游服务时，目前只处理 `Sec-WebSocket-Protocol` 子协议，不透传通用请求头
- 当下游服务依赖 `Origin`、`Authorization`、`Cookie`、`X-Forwarded-*` 等头做来源校验、认证、会话恢复或审计时，二跳握手会丢失这些上下文
- 典型失败现象是下游日志出现 `origin not allowed`，或者因为缺失 token / cookie 导致握手或后续鉴权失败

这些限制阻碍了以下场景：
- 沙箱内运行了多个 WebSocket 服务（如 Jupyter Kernel、VS Code Server、自定义推理服务），需要连接到不同端口
- 沙箱内服务使用 RESTful 风格 API，需要 GET 查询、PUT 更新、DELETE 删除
- 沙箱内运行了多个 HTTP 服务，需要访问非 8080 端口
- 沙箱内 WebSocket 服务依赖浏览器来源校验、认证头、cookie 或链路追踪头，要求代理保留客户端请求上下文

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

3. **HTTP Proxy 支持用户指定目标端口**
   - 在现有 `/sandboxes/{sandbox_id}/proxy[/{path}]` 路由上，允许通过 query param `port` 指定目标 HTTP 端口
   - 当 `port` 未指定时，保持现有行为（使用 `Port.SERVER = 8080`）

4. **WebSocket Proxy 支持黑名单过滤透传通用请求头**
   - 通过 `/sandboxes/{id}/proxy/{path:path}` 建立 WebSocket 代理时，默认将客户端请求中的通用 header 转发到下游服务，通过黑名单排除不应转发的头
   - 黑名单排除的头包括：WebSocket 握手专用头（`Host`、`Connection`、`Upgrade`、`Sec-WebSocket-Key`、`Sec-WebSocket-Version`、`Sec-WebSocket-Extensions`、`Sec-WebSocket-Protocol`）和 hop-by-hop 头（`Transfer-Encoding`、`TE`、`Trailer`、`Keep-Alive`、`Proxy-Authorization`、`Proxy-Connection`、`Content-Length`）
   - `Origin` 是必须支持的关键头，因为下游服务可能使用来源白名单（例如 `gateway.controlUi.allowedOrigins`）校验 WebSocket 握手
   - `Sec-WebSocket-Protocol` 继续通过 WebSocket 子协议协商传递，不作为普通 header 透传
   - 采用黑名单策略，确保用户自定义 header 能被透传到下游服务

### Out（本次不做的）

- WebSocket Proxy 的认证/鉴权增强
- HTTP Proxy 的 multipart/form-data 支持（upload 接口已单独处理）
- `host_proxy` 的 method 扩展（范围外）
- SDK 客户端侧的封装更新
- 自动伪造、补默认值或重写任意 `Origin`
- 透传 WebSocket 握手专用头（如 `Host`、`Connection`、`Upgrade`、`Sec-WebSocket-Key`）

---

## Acceptance Criteria

- **AC1**：`ws://admin/sandboxes/{id}/proxy/ws?port=8888` 能成功代理到沙箱内 8888 端口的 WebSocket 服务
- **AC2**：`ws://admin/sandboxes/{id}/proxy/ws?port=8080` 与不带 `port` 参数的行为一致（向后兼容）
- **AC3**：`port` 参数不合法时（< 1024、> 65535、= 22），WebSocket 连接以 code=1008 关闭并返回错误信息
- **AC4**：`GET /sandboxes/{sandbox_id}/proxy/hello` 能成功代理 GET 请求到沙箱内服务
- **AC5**：`DELETE /sandboxes/{sandbox_id}/proxy/items/1` 能成功代理 DELETE 请求
- **AC6**：原有 `POST /sandboxes/{sandbox_id}/proxy` 行为不变（向后兼容）
- **AC7**：SSE streaming（`text/event-stream`）在所有 method 下仍然正常工作
- **AC8**：`GET /sandboxes/{sandbox_id}/proxy/api/health?port=9000` 能成功代理到沙箱内 9000 端口的 HTTP 服务
- **AC9**：HTTP proxy 不带 `port` 参数时行为不变（向后兼容）
- **AC10**：当客户端 WebSocket 握手包含 `Origin` 时，代理发起到下游的二跳握手必须携带相同 `Origin`，以满足下游来源校验
- **AC11**：当客户端握手包含 `Authorization`、`Cookie`、`X-Forwarded-*`、`X-Request-Id`、`Traceparent` 或任意自定义头时，代理发起到下游的二跳握手必须一并转发
- **AC12**：`Sec-WebSocket-Protocol` 必须继续通过现有 `subprotocols` 机制转发和协商，不能降级为普通 header 透传
- **AC13**：黑名单头（`Host`、`Connection`、`Upgrade`、`Sec-WebSocket-Key`、`Sec-WebSocket-Version`、`Sec-WebSocket-Extensions`、`Transfer-Encoding`、`TE`、`Trailer`、`Keep-Alive`、`Proxy-Authorization`、`Proxy-Connection`、`Content-Length`）不得被转发到下游
- **AC14**：当客户端未携带任何白名单头时，WebSocket proxy 的默认行为与现状保持一致（向后兼容）

---

## Constraints

- 不引入新的外部依赖
- 不修改 `Port.SERVER` / `Port.PROXY` 枚举定义
- `port_validation` 逻辑复用现有 `validate_port_forward_port`（但 WebSocket proxy 端口校验需新增对 `Port.SERVER = 8080` 的允许，或直接用同一校验函数）
- 保持现有 portforward 端点（`/sandboxes/{id}/portforward`）不变
- WebSocket header 转发采用**黑名单**策略，排除握手专用头和 hop-by-hop 头，允许用户自定义 header 透传
- `Origin` 通过 WebSocket 客户端库的显式参数透传，不与普通 `additional_headers` 混用
- `Sec-WebSocket-Protocol` 继续通过 `subprotocols` 参数处理，不走通用 header 透传逻辑

---

## Risks & Rollout

- **风险**：WebSocket proxy 中用户可以指定任意端口访问沙箱内服务，存在横向访问风险 → 通过 `sandbox_id` 鉴权已覆盖，端口范围校验作为防护层
- **风险**：黑名单策略下，未列入黑名单的头会默认转发 → 通过单元测试确保握手专用头和 hop-by-hop 头被正确过滤
- **风险**：不同下游服务对 `Origin`、`Cookie`、`Authorization` 的要求不同，透传后暴露出原本被代理层掩盖的问题 → 以“尽可能保留客户端上下文、但不伪造默认值”为原则
- **回滚**：修改集中在 `sandbox_proxy_service.py` 和对应单元测试；如需回滚，可仅还原 WebSocket header 透传逻辑
- **上线策略**：无数据库变更，直接部署；建议先在依赖 `Origin` 校验的控制台类服务上验证
