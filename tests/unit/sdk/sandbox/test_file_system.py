"""Tests for LinuxFileSystem OSS methods."""

from unittest.mock import AsyncMock, MagicMock

from rock.sdk.sandbox.file_system import LinuxFileSystem


def _sandbox(exit_code=0):
    sb = AsyncMock()
    sb.process.execute_script = AsyncMock(return_value=MagicMock(exit_code=exit_code, output=""))
    sb.execute = AsyncMock(return_value=MagicMock(exit_code=exit_code, stdout="v2", stderr=""))
    return sb


class TestEnsureOssutil:
    async def test_success(self):
        assert await LinuxFileSystem(_sandbox()).ensure_ossutil() is True

    async def test_install_failure(self):
        assert await LinuxFileSystem(_sandbox(exit_code=1)).ensure_ossutil() is False
