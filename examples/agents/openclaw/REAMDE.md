# OpenClaw Demo Guide

## Prerequisites(Important)

> If you don't have a running ROCK server, please follow the [Quick Start guide](https://alibaba.github.io/ROCK/docs/Getting%20Started/quickstart/) to try set one up first.


## 1. Activate Virtual Environment

```bash
source .venv/bin/activate
```

---

## 2. Start Rock Admin with Proxy

### 2.1 Start Redis

> Skip this step if Redis is already running.

```bash
docker run -d -p 6379:6379 redis/redis-stack-server:latest
```

### 2.2 Start Admin Service

```bash
rock admin start --env local-proxy --role admin --port 9000
```

### 2.3 Start Proxy Service

```bash
rock admin start --env local-proxy --role proxy --port 9001
```

> 💡 The port numbers (9000 / 9001) above are just examples. Adjust them as needed.

---

## 3. Configuration

| File | Description | Required |
|---|---|---|
| `rock_agent_config.yaml` | Configure model-related parameters | ✅ Required |
| `rock_agent_config.yaml` | Adjust `OPENCLAW_GATEWAY_TOKEN` | ⚙️ Optional |
| `openclaw.json` | Add `channels` or other settings | ⚙️ Optional |

---

## 4. Run the Demo

```bash
cd examples/agents/openclaw
python openclaw_demo.py
```

---

## 5. API Usage Example

ROCK Proxy provides the ability to proxy sandbox endpoints. By default, it forwards traffic to port 8080 inside the sandbox container. Therefore, the OpenClaw Gateway must be started on port 8080.
For example, it can expose the `v1/chat/completions` interface of a given `<<SANDBOX-ID>>` as shown below:

**Endpoint:**

```
http://localhost:9001/apis/envs/sandbox/v1/sandboxes/<<SANDBOX-ID>>/proxy/v1/chat/completions
```

**Request Example:**

```bash
curl --location 'http://localhost:9001/apis/envs/sandbox/v1/sandboxes/<<SANDBOX-ID>>/proxy/v1/chat/completions' \
  --header 'Authorization: bearer <<OPENCLAW_GATEWAY_TOKEN>>' \
  --header 'Content-Type: application/json' \
  --data '{
    "model": "openclaw",
    "stream": true,
    "messages": [
        {
            "role": "user",
            "content": "Who are you?"
        }
    ]
}'
```

> ⚠️ **Note:**
> - Replace `<<SANDBOX-ID>>` with your actual Sandbox ID
> - Replace `<<OPENCLAW_GATEWAY_TOKEN>>` with your actual Token value