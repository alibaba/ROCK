"""Tests for LinuxFileSystem OSS methods."""

from unittest.mock import AsyncMock, MagicMock

from rock.actions.sandbox.response import DownloadFileResponse
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.file_system import LinuxFileSystem


def _sandbox(exit_code=0):
    sb = AsyncMock()
    sb.process.execute_script = AsyncMock(return_value=MagicMock(exit_code=exit_code, output=""))
    sb.execute = AsyncMock(return_value=MagicMock(exit_code=exit_code, stdout="v2", stderr=""))
    return sb


def _sandbox_with_oss(oss_setup_returns: bool = True, ossutil_ok: bool = True, download_response=None):
    """Build a mock Sandbox with _oss + ensure_ossutil dependencies.

    Uses spec=Sandbox so isinstance(self.sandbox, Sandbox) check inside
    download_file passes.
    """
    sb = AsyncMock(spec=Sandbox)
    sb.process = MagicMock()
    sb.process.execute_script = AsyncMock(return_value=MagicMock(exit_code=0 if ossutil_ok else 1, output=""))
    sb.execute = AsyncMock(return_value=MagicMock(exit_code=0 if ossutil_ok else 1, stdout="v2", stderr=""))

    sb._oss = MagicMock()
    sb._oss.ensure_setup = AsyncMock(return_value=oss_setup_returns)
    sb._oss.download_via_oss = AsyncMock(
        return_value=download_response or DownloadFileResponse(success=True, message="ok")
    )
    return sb


class TestEnsureOssutil:
    async def test_success(self):
        assert await LinuxFileSystem(_sandbox()).ensure_ossutil() is True

    async def test_install_failure(self):
        assert await LinuxFileSystem(_sandbox(exit_code=1)).ensure_ossutil() is False


class TestDownloadFileDelegatesToOssClient:
    async def test_delegates_to_oss_client_after_ensure_setup_and_ossutil(self, tmp_path):
        sb = _sandbox_with_oss(oss_setup_returns=True, ossutil_ok=True)

        fs = LinuxFileSystem(sb)
        resp = await fs.download_file("/sandbox/foo.txt", tmp_path / "foo.txt")

        assert resp.success is True
        sb._oss.ensure_setup.assert_awaited_once()
        sb._oss.download_via_oss.assert_awaited_once()

    async def test_returns_oss_unavailable_when_setup_fails(self, tmp_path):
        sb = _sandbox_with_oss(oss_setup_returns=False)

        fs = LinuxFileSystem(sb)
        resp = await fs.download_file("/sandbox/foo.txt", tmp_path / "foo.txt")

        assert resp.success is False
        assert "OSS is not available" in resp.message
        sb._oss.download_via_oss.assert_not_awaited()

    async def test_returns_failure_when_ossutil_install_fails(self, tmp_path):
        sb = _sandbox_with_oss(oss_setup_returns=True, ossutil_ok=False)

        fs = LinuxFileSystem(sb)
        resp = await fs.download_file("/sandbox/foo.txt", tmp_path / "foo.txt")

        assert resp.success is False
        assert "ossutil" in resp.message
        sb._oss.download_via_oss.assert_not_awaited()
