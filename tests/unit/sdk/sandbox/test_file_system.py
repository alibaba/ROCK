"""Tests for platform-specific FileSystem implementations."""

import base64
import tempfile
import time
import zipfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.actions import BashAction, Observation
from rock.actions.sandbox.request import ChmodRequest, ChownRequest, Command, UploadMode
from rock.actions.sandbox.response import DownloadFileResponse
from rock.sdk.common.exceptions import BadRequestRockError
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig
from rock.sdk.sandbox.file_system import LinuxFileSystem, WindowsFileSystem


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


def _windows_sandbox(*, exit_code: int = 0, arun_output: str = ""):
    sb = MagicMock()
    sb.execute = AsyncMock(
        return_value=MagicMock(exit_code=exit_code, stdout="", stderr="", __str__=lambda _: "command response")
    )
    sb.create_session = AsyncMock()
    sb.arun = AsyncMock(return_value=Observation(exit_code=0, output=arun_output))
    sb.run_in_session = AsyncMock(return_value=Observation(exit_code=0))
    sb.upload_by_path = AsyncMock(return_value=MagicMock(success=True, message="ok"))
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


class TestWindowsPermissions:
    async def test_chown(self):
        sb = _windows_sandbox()
        response = await WindowsFileSystem(sb).chown(
            ChownRequest(remote_user="rock", paths=[r"C:\work"], recursive=True)
        )
        assert response.success is True
        sb.execute.assert_awaited_once_with(Command(command=["icacls", r"C:\work", "/setowner", "rock", "/T"]))

        failed_response = await WindowsFileSystem(_windows_sandbox(exit_code=5)).chown(
            ChownRequest(remote_user="rock", paths=[r"C:\work"], recursive=False)
        )
        assert failed_response.success is False
        assert "command response" in failed_response.message

        with pytest.raises(BadRequestRockError, match="paths is empty"):
            await WindowsFileSystem(_windows_sandbox()).chown(ChownRequest(remote_user="rock", paths=[]))

    async def test_chmod(self):
        sb = _windows_sandbox()
        response = await WindowsFileSystem(sb).chmod(ChmodRequest(paths=[r"C:\work\a.txt"], mode="444"))
        assert response.success is True
        sb.execute.assert_awaited_once_with(Command(command=["attrib", "+R", r"C:\work\a.txt"]))

        recursive_sb = _windows_sandbox()
        recursive_response = await WindowsFileSystem(recursive_sb).chmod(
            ChmodRequest(paths=[r"C:\work"], mode="755", recursive=True)
        )
        assert recursive_response.success is True
        recursive_command = recursive_sb.arun.await_args.kwargs["cmd"]
        assert "$target = 'C:\\work'" in recursive_command
        assert "& attrib.exe -R $target" in recursive_command
        assert "Test-Path -LiteralPath $target -PathType Container" in recursive_command
        assert "& attrib.exe -R (Join-Path -Path $target -ChildPath '*') /S /D" in recursive_command
        recursive_sb.execute.assert_not_awaited()

        failed_sb = _windows_sandbox()
        failed_sb.arun.side_effect = RuntimeError("attrib failed")
        failed_response = await WindowsFileSystem(failed_sb).chmod(
            ChmodRequest(paths=[r"C:\work"], mode="755", recursive=True)
        )
        assert failed_response.success is False
        assert "attrib failed" in failed_response.message

        invalid_response = await WindowsFileSystem(_windows_sandbox()).chmod(
            ChmodRequest(paths=[r"C:\work"], mode="u+x")
        )
        assert invalid_response.success is False
        assert "octal mode" in invalid_response.message

        with pytest.raises(BadRequestRockError, match="paths is empty"):
            await WindowsFileSystem(_windows_sandbox()).chmod(ChmodRequest(paths=[], mode="755"))


class TestWindowsTransfers:
    async def test_upload_dir(self, tmp_path, monkeypatch):
        source = tmp_path / "source"
        source.mkdir()
        (source / "hello.txt").write_text("hello")
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr(time, "time_ns", lambda: 123)
        uploaded_names = []
        uploaded_mode = None
        sb = _windows_sandbox()
        sb.arun.side_effect = [
            Observation(exit_code=0, output="C:\\SandboxTemp\\"),
            Observation(exit_code=0),
        ]

        async def capture_upload(file_path, target_path, upload_mode=None):
            nonlocal uploaded_mode
            with zipfile.ZipFile(file_path) as archive:
                uploaded_names.extend(archive.namelist())
            assert target_path == r"C:\SandboxTemp\rock_upload_123.zip"
            uploaded_mode = upload_mode
            return MagicMock(success=True, message="ok")

        sb.upload_by_path.side_effect = capture_upload

        response = await WindowsFileSystem(sb).upload_dir(
            source,
            "C:\\target'; Write-Output pwned; '",
            extract_timeout=37,
        )

        assert response.exit_code == 0
        assert uploaded_names == ["hello.txt"]
        assert uploaded_mode == UploadMode.DIRECT
        extract_action = sb.run_in_session.await_args.args[0]
        assert isinstance(extract_action, BashAction)
        assert "Expand-Archive" in extract_action.command
        assert extract_action.timeout == 37
        assert "'C:\\target''; Write-Output pwned; '''" in extract_action.command
        assert sb.arun.await_count == 2
        assert not (tmp_path / "rock_upload_123.zip").exists()

        invalid_response = await WindowsFileSystem(_windows_sandbox()).upload_dir(source, "relative\\target")
        assert invalid_response.exit_code == 1
        assert "absolute Windows path" in invalid_response.failure_reason

    async def test_download_file(self, tmp_path):
        encoded = base64.b64encode(b"\x00rock").decode()
        sb = _windows_sandbox(arun_output=encoded)
        target = tmp_path / "nested" / "download.bin"
        response = await WindowsFileSystem(sb).download_file(r"C:\remote.bin", target)
        assert response.success is True
        assert target.read_bytes() == b"\x00rock"
        assert "[System.IO.File]::ReadAllBytes('C:\\remote.bin')" in sb.arun.await_args.kwargs["cmd"]

        invalid_target = tmp_path / "invalid.bin"
        invalid_response = await WindowsFileSystem(_windows_sandbox(arun_output="not base64!")).download_file(
            r"C:\remote.bin",
            invalid_target,
        )
        assert invalid_response.success is False
        assert not invalid_target.exists()


def test_sandbox_selects_file_system_by_image_os():
    assert isinstance(Sandbox(SandboxConfig(image_os="WiNdOwS")).fs, WindowsFileSystem)
    assert isinstance(Sandbox(SandboxConfig()).fs, LinuxFileSystem)
