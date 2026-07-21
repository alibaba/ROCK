---
sidebar_position: 6
---

# Sandbox Proxy

ROCK Admin exposes a proxy layer that lets you reach services running **inside** a sandbox from **outside** the cluster, without giving each sandbox its own public address. The proxy supports two transport modes:

| Mode | Endpoint | Use it for |
|------|----------|------------|
| HTTP Proxy | `/sandboxes/:sandbox_id/proxy/...` | REST APIs, web UIs, file downloads, any HTTP/1.1 traffic |
| WebSocket Proxy | `ws(s)://.../sandboxes/:sandbox_id/proxy/...` | Real-time channels, streaming, browser-based WS clients |

Both modes route by `sandbox_id`. The sandbox does **not** need a public IP — Admin terminates the client connection and forwards it to the right runtime inside the cluster.

---

## 1. HTTP Proxy

Forward any HTTP request to a service inside the sandbox.

### Endpoint

```text
Methods : GET | POST | PUT | DELETE | PATCH | HEAD | OPTIONS
URL     : $ROCK_BASE_URL/sandboxes/:sandbox_id/proxy[/:path]
```

- Method, headers, query string, and body are forwarded as-is to the target service.
- The response (status code, headers, body) is streamed back to the client.

### Choosing the target port

The proxy needs to know which port inside the sandbox to hit. You can specify it in one of three ways — **pick exactly one**; mixing them returns `400 Bad Request`.

| Priority | Mechanism | Example |
|----------|-----------|---------|
| 1 | **Path prefix** | `/sandboxes/abc/proxy/port/8080/api/users` |
| 2 | **Request header** | `X-ROCK-Target-Port: 8080` |
| 3 | **Query parameter** | `?rock_target_port=8080` |

If no port is specified, the request is delivered to the sandbox's default service port.

### Examples

```bash
# REST call via path-style port
curl -X POST \
  "$ROCK_BASE_URL/sandboxes/sb-123/proxy/port/8080/v1/predict" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "hello"}'

# Same call via header-style port
curl -X POST \
  "$ROCK_BASE_URL/sandboxes/sb-123/proxy/v1/predict" \
  -H "X-ROCK-Target-Port: 8080" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "hello"}'

# Query-string port + extra query params
curl "$ROCK_BASE_URL/sandboxes/sb-123/proxy/items?rock_target_port=8080&limit=10"
```

---

## 2. WebSocket Proxy

For services that expect a `ws://` (or `wss://`) connection — typical for streaming, chat, terminal sessions, or any full-duplex channel.

### Endpoint

```text
ws(s)://$ROCK_BASE_URL/sandboxes/:sandbox_id/proxy/:path
```

- The original WebSocket handshake (subprotocols, custom headers) is forwarded.
- Both text and binary frames pass through transparently in both directions.
- Closing either side cleanly tears down the upstream connection.

### Choosing the target port

Identical to HTTP Proxy — path, header, or query parameter — and again, only one at a time:

| Priority | Mechanism | Example |
|----------|-----------|---------|
| 1 | Path prefix | `/sandboxes/abc/proxy/port/9000/socket` |
| 2 | Header | `X-ROCK-Target-Port: 9000` |
| 3 | Query | `?rock_target_port=9000` |

Invalid port values (see [Port restrictions](#3-port-restrictions)) cause the WebSocket to close immediately with code `1008` (Policy Violation).

### Example

```bash
# Using wscat
wscat -c "$ROCK_WS_BASE/sandboxes/sb-123/proxy/port/9000/events"
```

```javascript
// Browser client
const ws = new WebSocket(
  "wss://rock.example.com/sandboxes/sb-123/proxy/events?rock_target_port=9000"
);
ws.onmessage = (evt) => console.log(evt.data);
ws.send("ping");
```

---

## 3. Port restrictions

The WebSocket proxy enforces the following rules on the **target port** inside the sandbox:

| Rule | Allowed range / value |
|------|----------------------|
| Minimum port | `1024` |
| Maximum port | `65535` |
| Forbidden | `22` (SSH) |

Requests violating these rules are rejected:

- **HTTP Proxy** → `400 Bad Request` with a `detail` message.
- **WebSocket Proxy** → connection closed with code `1008` and a reason string.

> Ports below `1024` are blocked because they are reserved for privileged services; port `22` is blocked to prevent inadvertently exposing SSH.

---

## 4. Error handling reference

| Symptom | Likely cause |
|---------|--------------|
| `400 Bad Request: Cannot specify target port via multiple sources` | You set the port in two of path, header or query — pick one. |
| `400 Bad Request` / WS close `1008` with port-range message | Target port is `< 1024`, `> 65535`, or equal to `22`. |
| WS close `1011` (`Proxy error: ...`) | Upstream service inside the sandbox returned an error or could not be reached. Check that the service is actually listening on the target port. |
| `404 Not Found` from the upstream | The HTTP path inside the sandbox does not exist — verify the service's route, not the proxy URL. |
| Connection hangs on handshake | The sandbox may still be initializing. Confirm `is_alive` returns `True` before proxying. |

---

## 5. Quick decision guide

```text
Need to call a REST API inside the sandbox?     → HTTP Proxy
Need a bidirectional WebSocket to a WS server?  → WebSocket Proxy
```

## Related documents

- [API Reference](../References/api.md)
- [Configuration](configuration.md)
- [Quick Start](../Getting%20Started/quickstart.md)
