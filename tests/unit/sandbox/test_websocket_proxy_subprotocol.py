"""Tests for WebSocket proxy subprotocol forwarding and performance fixes."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_service():
    service = MagicMock(spec=SandboxProxyService)
    service._update_expire_time = AsyncMock()
    service.get_sandbox_websocket_url = AsyncMock(return_value="ws://10.0.0.1:8006/websockify")
    return service


def _make_client_ws(subprotocols=None):
    """Simulate a FastAPI WebSocket object."""
    ws = MagicMock()
    ws.subprotocols = subprotocols or []
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.receive_text = AsyncMock(side_effect=Exception("disconnect"))
    ws.receive_bytes = AsyncMock(side_effect=Exception("websocket.disconnect"))
    return ws


class FakeStarletteWebSocket:
    """Simulates a real Starlette WebSocket with shared ASGI message queue.

    receive_text() and receive_bytes() both consume from the same queue,
    exactly like the real Starlette implementation. This means if
    receive_text() pops a binary-only message, that message is lost when
    the caller falls back to receive_bytes() which pops the NEXT message.
    """

    def __init__(self, messages: list[dict]):
        self._queue: asyncio.Queue = asyncio.Queue()
        for msg in messages:
            self._queue.put_nowait(msg)

    async def receive(self) -> dict:
        return await self._queue.get()

    async def receive_text(self) -> str:
        msg = await self.receive()
        if msg["type"] == "websocket.disconnect":
            raise Exception("websocket.disconnect")
        return msg["text"]  # KeyError when message has no "text" key

    async def receive_bytes(self) -> bytes:
        msg = await self.receive()
        if msg["type"] == "websocket.disconnect":
            raise Exception("websocket.disconnect")
        return msg["bytes"]

    async def send_text(self, text: str) -> None:
        pass

    async def send_bytes(self, data: bytes) -> None:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Subprotocol forwarding
# ─────────────────────────────────────────────────────────────────────────────


class TestWebSocketSubprotocolForwarding:
    """websocket_proxy must forward client subprotocols to upstream and accept with negotiated subprotocol."""

    async def test_subprotocols_passed_to_upstream_connect(self):
        """websockets.connect() must receive the client's requested subprotocols."""
        service = _make_service()
        client_ws = _make_client_ws(subprotocols=["binary"])

        mock_target_ws = AsyncMock()
        mock_target_ws.subprotocol = "binary"
        mock_target_ws.__aenter__ = AsyncMock(return_value=mock_target_ws)
        mock_target_ws.__aexit__ = AsyncMock(return_value=False)
        mock_target_ws.recv = AsyncMock(side_effect=Exception("closed"))

        connect_kwargs = {}

        def fake_connect(url, **kwargs):
            connect_kwargs.update(kwargs)
            return mock_target_ws

        with patch("rock.sandbox.service.sandbox_proxy_service.websockets.connect", side_effect=fake_connect):
            await SandboxProxyService.websocket_proxy(service, client_ws, "sb1", "websockify", port=8006)

        assert "subprotocols" in connect_kwargs
        assert "binary" in connect_kwargs["subprotocols"]

    async def test_no_subprotocol_falls_back_to_binary_base64(self):
        """When client sends no subprotocols, connect() should fall back to ['binary', 'base64'].

        websockify rejects connections without a Sec-WebSocket-Protocol header,
        so we always send the default subprotocols when the client doesn't declare any.
        """
        service = _make_service()
        client_ws = _make_client_ws(subprotocols=[])

        mock_target_ws = AsyncMock()
        mock_target_ws.subprotocol = "binary"
        mock_target_ws.__aenter__ = AsyncMock(return_value=mock_target_ws)
        mock_target_ws.__aexit__ = AsyncMock(return_value=False)
        mock_target_ws.recv = AsyncMock(side_effect=Exception("closed"))

        connect_kwargs = {}

        def fake_connect(url, **kwargs):
            connect_kwargs.update(kwargs)
            return mock_target_ws

        with patch("rock.sandbox.service.sandbox_proxy_service.websockets.connect", side_effect=fake_connect):
            await SandboxProxyService.websocket_proxy(service, client_ws, "sb1", None, port=8006)

        assert connect_kwargs.get("subprotocols") == ["binary", "base64"]

    async def test_negotiated_subprotocol_passed_to_client_accept(self):
        """After upstream negotiates subprotocol, client accept() must be called with it."""
        service = _make_service()
        client_ws = _make_client_ws(subprotocols=["binary"])

        mock_target_ws = AsyncMock()
        mock_target_ws.subprotocol = "binary"  # upstream 协商结果
        mock_target_ws.__aenter__ = AsyncMock(return_value=mock_target_ws)
        mock_target_ws.__aexit__ = AsyncMock(return_value=False)
        mock_target_ws.recv = AsyncMock(side_effect=Exception("closed"))

        with patch("rock.sandbox.service.sandbox_proxy_service.websockets.connect", return_value=mock_target_ws):
            await SandboxProxyService.websocket_proxy(service, client_ws, "sb1", "websockify", port=8006)

        # accept 必须带上协商好的子协议
        client_ws.accept.assert_called_once()
        call_kwargs = client_ws.accept.call_args
        subprotocol = call_kwargs.kwargs.get("subprotocol") or (
            call_kwargs.args[0] if call_kwargs.args else None
        )
        assert subprotocol == "binary"


# ─────────────────────────────────────────────────────────────────────────────
# _forward_messages — no sleep
# ─────────────────────────────────────────────────────────────────────────────


class TestForwardMessagesNoSleep:
    """_forward_messages must not sleep between messages (asyncio.sleep(0.1) removed)."""

    async def test_no_sleep_between_messages(self):
        """asyncio.sleep should NOT be called during message forwarding."""
        service = MagicMock(spec=SandboxProxyService)

        # source: FastAPI WS，先发一条消息，再断开
        source_ws = MagicMock()
        source_ws.receive_text = AsyncMock(side_effect=["hello", Exception("websocket.disconnect")])

        # target: websockets 库对象
        target_ws = MagicMock()
        target_ws.send = AsyncMock()

        with patch("rock.sandbox.service.sandbox_proxy_service.asyncio.sleep") as mock_sleep:
            await SandboxProxyService._forward_messages(service, source_ws, target_ws, "client->target")

        mock_sleep.assert_not_called()

    async def test_binary_message_forwarded_without_sleep(self):
        """Binary messages should be forwarded directly without any sleep."""
        service = MagicMock(spec=SandboxProxyService)

        source_ws = FakeStarletteWebSocket([
            {"type": "websocket.receive", "bytes": b"\x00\x01\x02"},
            {"type": "websocket.disconnect", "code": 1000},
        ])

        target_ws = MagicMock(spec=["recv", "send"])
        target_ws.send = AsyncMock()

        with patch("rock.sandbox.service.sandbox_proxy_service.asyncio.sleep") as mock_sleep:
            await SandboxProxyService._forward_messages(service, source_ws, target_ws, "client->target")

        mock_sleep.assert_not_called()
        target_ws.send.assert_called_once_with(b"\x00\x01\x02")


# ─────────────────────────────────────────────────────────────────────────────
# _forward_messages — binary data loss (the actual bug)
# ─────────────────────────────────────────────────────────────────────────────


class TestForwardMessagesBinaryDataLoss:
    """_forward_messages must forward ALL binary frames without dropping any.

    Bug: receive_text() and receive_bytes() share the same ASGI message queue.
    When receive_text() consumes a binary frame, it raises KeyError("text").
    The fallback receive_bytes() then reads the NEXT message from the queue,
    silently losing the binary frame that receive_text() already consumed.
    """

    async def test_all_binary_frames_forwarded_to_target(self):
        """Every binary frame from client must reach the upstream target."""
        service = MagicMock(spec=SandboxProxyService)

        source_ws = FakeStarletteWebSocket([
            {"type": "websocket.receive", "bytes": b"vnc_frame_1"},
            {"type": "websocket.receive", "bytes": b"vnc_frame_2"},
            {"type": "websocket.receive", "bytes": b"vnc_frame_3"},
            {"type": "websocket.disconnect", "code": 1000},
        ])

        target_ws = MagicMock(spec=["recv", "send"])
        target_ws.send = AsyncMock()

        await SandboxProxyService._forward_messages(
            service, source_ws, target_ws, "client->target"
        )

        forwarded = [c.args[0] for c in target_ws.send.call_args_list]
        assert forwarded == [b"vnc_frame_1", b"vnc_frame_2", b"vnc_frame_3"]

    async def test_mixed_text_and_binary_frames_all_forwarded(self):
        """Interleaved text and binary frames must all be forwarded correctly."""
        service = MagicMock(spec=SandboxProxyService)

        source_ws = FakeStarletteWebSocket([
            {"type": "websocket.receive", "text": "hello"},
            {"type": "websocket.receive", "bytes": b"\x00\x01"},
            {"type": "websocket.receive", "text": "world"},
            {"type": "websocket.disconnect", "code": 1000},
        ])

        target_ws = MagicMock(spec=["recv", "send"])
        target_ws.send = AsyncMock()

        await SandboxProxyService._forward_messages(
            service, source_ws, target_ws, "client->target"
        )

        forwarded = [c.args[0] for c in target_ws.send.call_args_list]
        assert forwarded == ["hello", b"\x00\x01", "world"]

    async def test_single_binary_frame_not_lost(self):
        """Even a single binary frame must not be silently dropped."""
        service = MagicMock(spec=SandboxProxyService)

        source_ws = FakeStarletteWebSocket([
            {"type": "websocket.receive", "bytes": b"rfb_handshake"},
            {"type": "websocket.disconnect", "code": 1000},
        ])

        target_ws = MagicMock(spec=["recv", "send"])
        target_ws.send = AsyncMock()

        await SandboxProxyService._forward_messages(
            service, source_ws, target_ws, "client->target"
        )

        target_ws.send.assert_called_once_with(b"rfb_handshake")
