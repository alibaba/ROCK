# model-service proxy 用法示例

`rock model-service` 的 `proxy` 模式把 `/v1/chat/completions` 转发到上游 LLM，并把每次调用以
`StandardLoggingPayload` 格式 append 到 JSONL traj 文件。配合 `--traj-file` 可以让相同 base URL 的
agent（SWE-agent / mini-swe-agent / OpenHands）从录制的 traj 回放，实现"无 LLM 成本"调试。

下面所有命令都用 `python -m rock.sdk.model.server.main` 启动，等价于 `rock model-service start`。

## 1. Record 模式（默认）

转发到单个上游，每次调用 append 到 `LOG_DIR/LLMTraj.jsonl`：

```bash
export OPENAI_API_KEY="sk-..."
export ROCK_MODEL_SERVICE_DATA_DIR=/tmp/rock-traj   # traj 文件落盘根目录

python -m rock.sdk.model.server.main \
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

# 查看 traj
cat /tmp/rock-traj/LLMTraj.jsonl | jq '.id, .model, .response.choices[0].message.content'
```

支持流式（litellm 自动聚合写入 traj）：

```bash
curl -N -X POST http://127.0.0.1:8080/v1/chat/completions \
    -H "Authorization: Bearer $OPENAI_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"model":"gpt-3.5-turbo","stream":true,"messages":[{"role":"user","content":"count to 5"}]}'
```

## 2. Replay 模式

把 `--traj-file` 指到一个录好的 jsonl，proxy 不再访问真实 LLM，按录制顺序返回响应：

```bash
python -m rock.sdk.model.server.main \
    --type proxy \
    --traj-file /tmp/rock-traj/LLMTraj.jsonl \
    --port 8081
```

agent 把 base URL 换成 `http://127.0.0.1:8081/v1` 即可重放，cursor 用尽后返回 404。
`--traj-file` 必须是单个 jsonl 文件路径。

## 3. 调整重试和超时

```bash
python -m rock.sdk.model.server.main \
    --type proxy \
    --proxy-base-url https://api.openai.com/v1 \
    --num-retries 3 \
    --request-timeout 60 \
    --port 8080
```

## 4. 多模型路由（需要 YAML）

只有在按 model name 分流到不同上游时才需要 YAML（CLI 只暴露单一 `--proxy-base-url`）。新建
`routes.yaml`：

```yaml
proxy_rules:
  gpt-3.5-turbo: "https://api.openai.com/v1"
  gpt-4o: "https://api.openai.com/v1"
  default: "https://api-inference.modelscope.cn/v1"
```

启动时配合 CLI：

```bash
python -m rock.sdk.model.server.main \
    --type proxy \
    --config-file routes.yaml \
    --port 8080
```

CLI 上指定的 `--proxy-base-url` / `--port` / `--num-retries` 等仍会覆盖 YAML 的同名字段。
