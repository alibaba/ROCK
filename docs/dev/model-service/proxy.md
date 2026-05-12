# model-service `proxy` 模式

`rock model-service` 的 proxy 模式在 `/v1/chat/completions` 上提供一个 OpenAI 兼容的转发层，
两种工作模式互斥：

| 模式      | 触发条件                              | 上游调用 | 写盘                 |
|-----------|---------------------------------------|----------|----------------------|
| Recording | 默认                                  | 真实调用 | append 到 JSONL traj |
| Replay    | `--replay-file` / `replay_file` 设置  | 不调用   | 不写                 |

设计目标是让 SWE-agent / mini-swe-agent / OpenHands 等 agent 框架在录制 → 回放之间无感切换：
agent 不变，只换 base URL。

下文所有命令以 `rock model-service start` 启动；该子命令最终会 `subprocess` 拉起
`rock.sdk.model.server.main`，两者支持的 flag 一致。直接调试时也可以用
`python -m rock.sdk.model.server.main` 跳过 PID 文件管理。

---

## 1. Recording（默认）

转发到单个上游，每次调用 append 一行 JSONL 到 `recording_file`（缺省 `LOG_DIR/LLMTraj.jsonl`，
其中 `LOG_DIR = $ROCK_MODEL_SERVICE_DATA_DIR`）：

```bash
export OPENAI_API_KEY="sk-..."
export ROCK_MODEL_SERVICE_DATA_DIR=/tmp/rock-traj

rock model-service start \
    --type proxy \
    --proxy-base-url https://api.openai.com/v1 \
    --port 8080
```

调用：

```bash
curl -X POST http://127.0.0.1:8080/v1/chat/completions \
    -H "Authorization: Bearer $OPENAI_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"model":"gpt-3.5-turbo","messages":[{"role":"user","content":"hi"}]}'

cat /tmp/rock-traj/LLMTraj.jsonl | jq '.model, .response.choices[0].message.content'
```

流式同样支持，上游字节原样转给客户端，recorder 在后台聚合最终的 `ChatCompletion` 写盘
（用 openai SDK 的 `ChatCompletionStreamState`，所以 `tool_calls.function.arguments` 等
跨 chunk 拼接的字段会被还原成完整形态）：

```bash
curl -N -X POST http://127.0.0.1:8080/v1/chat/completions \
    -H "Authorization: Bearer $OPENAI_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"model":"gpt-3.5-turbo","stream":true,"messages":[{"role":"user","content":"count to 5"}]}'
```

显式指定写到别的路径：

```bash
rock model-service start \
    --type proxy \
    --proxy-base-url https://api.openai.com/v1 \
    --recording-file /tmp/my-session.jsonl \
    --port 8080
```

---

## 2. Replay

把 `--replay-file` 指到一个录好的 jsonl，proxy 不再访问真实 LLM，按录制顺序返回响应；
agent 把 base URL 换成 `http://127.0.0.1:8081/v1` 即可重放：

```bash
rock model-service start \
    --type proxy \
    --replay-file /tmp/rock-traj/LLMTraj.jsonl \
    --port 8081
```

行为细节：

- cursor 单调推进，每次请求消耗一条记录；用尽后返回 **404**。
- 流式请求会拿录制的 `ChatCompletion` 重新发一帧 SSE chunk + `[DONE]`。
  `tool_calls` 的 `index` 字段会被自动注入（OpenAI 的流式协议要求 chunk delta 上有 `index`，
  但录制态的 `message.tool_calls` 没有）。
- request 里的 `model` 会跟录制的 `model` 比对，不一致只打 warning，不阻断。

`recording_file` 和 `replay_file` 是**互斥**的——同时配置（无论是 CLI 还是 YAML）会在启动时
被 Pydantic `model_validator` 拦下并报 `ValidationError`，避免"录到一半把源文件覆盖"这类隐性 bug。

---

## 3. 重试和超时

- 默认对 connection error / timeout 和 `retryable_status_codes`（默认 `[429, 500]`）触发重试，
  最多 6 次，指数退避 2s 起步 ×2 + 抖动；最后一次仍失败时把上游响应原样转给客户端
  （**不**包装成 502/504，让 agent 自己看到真实状态码）。
- 对**流式**请求，重试只发生在第一个字节抵达客户端**之前**——一旦字节流开始转发，
  连接中断不会重试（已发出去的字节无法收回）。

```bash
rock model-service start \
    --type proxy \
    --proxy-base-url https://api.openai.com/v1 \
    --retryable-status-codes 429,500,502,503 \
    --request-timeout 60 \
    --port 8080
```

---

## 4. 多模型路由（YAML）

按 model name 分流到不同上游需要 YAML（CLI 只暴露单一 `--proxy-base-url`）。新建 `routes.yaml`：

```yaml
proxy_rules:
  gpt-3.5-turbo: "https://api.openai.com/v1"
  gpt-4o:        "https://api.openai.com/v1"
  default:       "https://api-inference.modelscope.cn/v1"

retryable_status_codes: [429, 500, 502]
request_timeout: 60
recording_file: /tmp/rock-traj/multi.jsonl
```

启动：

```bash
rock model-service start \
    --type proxy \
    --config-file routes.yaml \
    --port 8080
```

CLI flag（`--proxy-base-url` / `--port` / `--retryable-status-codes` / ...）覆盖 YAML 同名字段。
路由解析顺序：`proxy_base_url` → `proxy_rules[model]` → `proxy_rules["default"]`，都没有则 400。

---

## 5. 实现要点（仅供参考）

- `chat_completions` endpoint 把请求分发给 `app.state.backend`，后者要么是 `ForwardBackend`
  要么是 `ReplayBackend`，由启动时的 `_configure_proxy_integrations` 根据 `replay_file`
  是否设置二选一注入。
- `ForwardBackend` 走 httpx 字节透传：non-stream 是 `await resp.aread()`，stream 是
  `resp.aiter_bytes()` 直接 yield 给客户端，**不**经过任何 SDK 的反序列化/再序列化，所以上游
  返回的 `reasoning_content` / `provider_specific_fields` 等任意 vendor 字段都不会被吃掉。
  recorder 在另一条独立路径上把字节流喂给 openai SDK 的 stream-state aggregator，仅用于写盘。
- `ReplayBackend` 完全本地，不持有 httpx client。

更深入的代码导览看 [rock/sdk/model/server/api/proxy.py](../../../rock/sdk/model/server/api/proxy.py)
顶部的 module docstring。
