"""Tests for WebSocket proxy header forwarding (blacklist-based)."""

from types import SimpleNamespace

import websockets

from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService
from rock.sandbox.utils.proxy import BLOCKED_WS_HEADER_NAMES, build_upstream_ws_headers

# ─────────────────────────────────────────────────────────────────────────────
# Unit tests: build_upstream_ws_headers (pure function, no mocks)
# ─────────────────────────────────────────────────────────────────────────────


def _ws_with_headers(headers: dict):
    """Create a minimal object with a .headers dict, mimicking Starlette WebSocket."""
    return SimpleNamespace(headers=headers)


class TestBuildUpstreamWsHeaders:
    def test_known_headers_forwarded(self):
        ws = _ws_with_headers(
            {
                "Origin": "https://example.com",
                "Authorization": "Bearer token123",
                "cookie": "session=abc",
                "traceparent": "00-trace-span-01",
                "EagleEye-TraceId": "eagle-trace-001",
            }
        )
        origin, additional = build_upstream_ws_headers(ws)
        assert origin == "https://example.com"
        assert additional is not None
        forwarded = dict(additional)
        assert forwarded["Authorization"] == "Bearer token123"
        assert forwarded["cookie"] == "session=abc"
        assert forwarded["traceparent"] == "00-trace-span-01"
        assert forwarded["EagleEye-TraceId"] == "eagle-trace-001"

    def test_custom_headers_forwarded(self):
        ws = _ws_with_headers(
            {
                "x-my-custom": "custom-value",
                "x-pictor-callid": "pictor-123",
                "web-server-type": "nginx",
            }
        )
        origin, additional = build_upstream_ws_headers(ws)
        assert origin is None
        assert additional is not None
        forwarded = dict(additional)
        assert forwarded["x-my-custom"] == "custom-value"
        assert forwarded["x-pictor-callid"] == "pictor-123"
        assert forwarded["web-server-type"] == "nginx"

    def test_blocked_headers_excluded(self):
        ws = _ws_with_headers(
            {
                "Authorization": "Bearer token123",
                "host": "should-be-excluded",
                "connection": "upgrade",
                "upgrade": "websocket",
                "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==",
                "sec-websocket-version": "13",
                "sec-websocket-extensions": "permessage-deflate",
                "sec-websocket-protocol": "binary",
                "transfer-encoding": "chunked",
                "keep-alive": "timeout=5",
            }
        )
        origin, additional = build_upstream_ws_headers(ws)
        assert origin is None
        assert additional is not None
        forwarded_keys = {k.lower() for k, _ in additional}
        assert forwarded_keys == {"authorization"}

    def test_no_forwardable_headers_returns_none(self):
        ws = _ws_with_headers({"host": "localhost", "connection": "upgrade"})
        origin, additional = build_upstream_ws_headers(ws)
        assert origin is None
        assert additional is None

    def test_origin_only_no_additional(self):
        ws = _ws_with_headers({"origin": "https://example.com"})
        origin, additional = build_upstream_ws_headers(ws)
        assert origin == "https://example.com"
        assert additional is None

    def test_origin_not_in_additional(self):
        ws = _ws_with_headers(
            {
                "origin": "https://example.com",
                "Authorization": "Bearer xxx",
            }
        )
        origin, additional = build_upstream_ws_headers(ws)
        assert origin == "https://example.com"
        forwarded_keys = {k.lower() for k, _ in additional}
        assert "origin" not in forwarded_keys

    def test_all_blocked_headers_covered(self):
        headers = {name: "value" for name in BLOCKED_WS_HEADER_NAMES}
        ws = _ws_with_headers(headers)
        origin, additional = build_upstream_ws_headers(ws)
        assert origin is None
        assert additional is None


# ─────────────────────────────────────────────────────────────────────────────
# E2E tests: real WebSocket server verifying header arrival
# ─────────────────────────────────────────────────────────────────────────────


class FakeClientWebSocket:
    """Simulates a Starlette WebSocket for websocket_proxy() input."""

    def __init__(self, headers: dict, subprotocols: list | None = None):
        self.headers = headers
        self.subprotocols = subprotocols or []
        self._accepted = False
        self._closed = False

    async def accept(self, subprotocol=None):
        self._accepted = True

    async def close(self, code=1000, reason=""):
        self._closed = True

    async def receive(self):
        return {"type": "websocket.disconnect", "code": 1000}


class TestWebSocketHeaderForwardingE2E:
    """Start a real websockets server and verify headers arrive downstream."""

    async def _run_proxy_with_server(self, client_headers: dict, subprotocols: list | None = None):
        """Helper: start WS server, run websocket_proxy(), return captured request headers."""
        captured_headers = {}

        async def handler(ws):
            captured_headers.update(dict(ws.request.headers))
            await ws.close()

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            target_url = f"ws://127.0.0.1:{port}"

            service = SandboxProxyService.__new__(SandboxProxyService)

            async def noop_update(*a, **kw):
                pass

            async def fake_get_url(*a, **kw):
                return target_url

            service._update_expire_time = noop_update
            service.get_sandbox_websocket_url = fake_get_url

            client_ws = FakeClientWebSocket(client_headers, subprotocols)
            await service.websocket_proxy(client_ws, "test-sandbox")

        return captured_headers

    async def test_origin_received_by_downstream(self):
        headers = await self._run_proxy_with_server({"origin": "https://my-app.example.com"})
        assert headers.get("origin") == "https://my-app.example.com"

    async def test_known_headers_received_by_downstream(self):
        client_headers = {
            "authorization": "Bearer secret-token",
            "eagleeye-traceid": "eagle-trace-e2e",
            "x-request-id": "req-e2e-001",
            "traceparent": "00-abcdef-123456-01",
        }
        headers = await self._run_proxy_with_server(client_headers)
        assert headers.get("authorization") == "Bearer secret-token"
        assert headers.get("eagleeye-traceid") == "eagle-trace-e2e"
        assert headers.get("x-request-id") == "req-e2e-001"
        assert headers.get("traceparent") == "00-abcdef-123456-01"

    async def test_custom_headers_received_by_downstream(self):
        client_headers = {
            "x-my-custom-app": "my-value",
            "x-pictor-callid": "pictor-123",
            "web-server-type": "nginx",
        }
        headers = await self._run_proxy_with_server(client_headers)
        assert headers.get("x-my-custom-app") == "my-value"
        assert headers.get("x-pictor-callid") == "pictor-123"
        assert headers.get("web-server-type") == "nginx"

    async def test_blocked_headers_not_duplicated_by_proxy(self):
        client_headers = {
            "authorization": "Bearer xxx",
            "host": "evil.example.com",
            "connection": "upgrade",
        }
        headers = await self._run_proxy_with_server(client_headers)
        assert headers.get("authorization") == "Bearer xxx"
        assert headers.get("host") != "evil.example.com"

    async def test_no_extra_headers_backward_compatible(self):
        headers = await self._run_proxy_with_server({})
        assert "authorization" not in headers
        assert "origin" not in headers
