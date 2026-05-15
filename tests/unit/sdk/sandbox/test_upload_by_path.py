"""Test Sandbox.upload_by_path async OSS persistence integration."""

from unittest.mock import AsyncMock, MagicMock, patch


async def test_small_file_triggers_async_persistence(tmp_path):
    """Small admin /upload success path schedules async OSS persistence."""
    f = tmp_path / "small.txt"
    f.write_text("hi")

    from rock.sdk.sandbox.client import Sandbox
    from rock.sdk.sandbox.config import SandboxConfig

    sandbox = Sandbox(SandboxConfig(base_url="http://x"))
    # Replace the whole _oss with a MagicMock so we can set is_available
    # (it's a @property on the real OssClient and not assignable).
    sandbox._oss = MagicMock()
    sandbox._oss.ensure_setup = AsyncMock(return_value=True)
    sandbox._oss.is_available = True
    sandbox._oss.schedule_async_persistence = AsyncMock(return_value="hash-small.txt")

    with patch(
        "rock.sdk.sandbox.client.HttpUtils.post_multipart",
        AsyncMock(return_value={"status": "Success"}),
    ):
        response = await sandbox.upload_by_path(str(f), "/sandbox/small.txt")

    assert response.success is True
    sandbox._oss.ensure_setup.assert_awaited()
    sandbox._oss.schedule_async_persistence.assert_awaited_once_with(str(f), "/sandbox/small.txt")


async def test_small_file_no_persistence_when_oss_unavailable(tmp_path):
    """Admin /upload success but OSS unavailable: no persistence scheduled."""
    f = tmp_path / "small.txt"
    f.write_text("hi")

    from rock.sdk.sandbox.client import Sandbox
    from rock.sdk.sandbox.config import SandboxConfig

    sandbox = Sandbox(SandboxConfig(base_url="http://x"))
    sandbox._oss = MagicMock()
    sandbox._oss.ensure_setup = AsyncMock(return_value=False)
    sandbox._oss.is_available = False
    sandbox._oss.schedule_async_persistence = AsyncMock()

    with patch(
        "rock.sdk.sandbox.client.HttpUtils.post_multipart",
        AsyncMock(return_value={"status": "Success"}),
    ):
        response = await sandbox.upload_by_path(str(f), "/sandbox/small.txt")

    assert response.success is True
    sandbox._oss.schedule_async_persistence.assert_not_awaited()


async def test_failed_upload_no_persistence(tmp_path):
    """Admin /upload failure: persistence must NOT be scheduled."""
    f = tmp_path / "small.txt"
    f.write_text("hi")

    from rock.sdk.sandbox.client import Sandbox
    from rock.sdk.sandbox.config import SandboxConfig

    sandbox = Sandbox(SandboxConfig(base_url="http://x"))
    sandbox._oss = MagicMock()
    sandbox._oss.ensure_setup = AsyncMock(return_value=True)
    sandbox._oss.is_available = True
    sandbox._oss.schedule_async_persistence = AsyncMock()

    with patch(
        "rock.sdk.sandbox.client.HttpUtils.post_multipart",
        AsyncMock(return_value={"status": "Failed", "message": "boom"}),
    ):
        response = await sandbox.upload_by_path(str(f), "/sandbox/small.txt")

    assert response.success is False
    sandbox._oss.schedule_async_persistence.assert_not_awaited()


async def test_sandbox_close_awaits_oss_pending_tasks():
    """Sandbox.close() must await OssClient.close() so pending persistence
    tasks have a chance to finish before the sandbox lifecycle ends."""
    from rock.sdk.sandbox.client import Sandbox
    from rock.sdk.sandbox.config import SandboxConfig

    sandbox = Sandbox(SandboxConfig(base_url="http://x"))
    sandbox._oss = MagicMock()
    sandbox._oss.close = AsyncMock()

    # stop() would otherwise issue a real HTTP request; replace with a noop.
    # Track call order to verify _oss.close runs BEFORE stop (so pending OSS
    # tasks aren't aborted by sandbox teardown).
    call_order: list[str] = []
    sandbox._oss.close.side_effect = lambda: call_order.append("oss_close")

    async def fake_stop():
        call_order.append("stop")

    with patch.object(sandbox, "stop", AsyncMock(side_effect=fake_stop)):
        await sandbox.close()

    sandbox._oss.close.assert_awaited_once()
    assert call_order == ["oss_close", "stop"]
