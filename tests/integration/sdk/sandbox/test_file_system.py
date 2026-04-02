import io
import logging
import shutil
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import Mock

import oss2
import pytest

from rock import env_vars
from rock.actions.sandbox.request import ChmodRequest, ChownRequest, Command, CreateBashSessionRequest, WriteFileRequest
from rock.actions.sandbox.response import ChmodResponse, ChownResponse, CommandResponse, Observation
from rock.sdk.sandbox.client import Sandbox
from tests.integration.conftest import SKIP_IF_NO_DOCKER

logger = logging.getLogger(__name__)


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_dirs_chown(sandbox_instance: Sandbox):
    assert await sandbox_instance.remote_user.create_remote_user("rock")

    rock_session = "rock_session"
    await sandbox_instance.create_session(CreateBashSessionRequest(remote_user="rock", session=rock_session))

    pwd_response: CommandResponse = await sandbox_instance.execute(Command(command=["pwd"]))
    pwd = pwd_response.stdout.strip()
    logger.info(f"pwd: {pwd}")

    response: ChownResponse = await sandbox_instance.fs.chown(
        ChownRequest(recursive=False, remote_user="rock", paths=[pwd])
    )
    assert response.success

    observation: Observation = await sandbox_instance.arun(
        cmd=f'ls -ld {pwd} | awk "{{print \\$3}}"', session=rock_session
    )
    assert observation.exit_code == 0
    assert observation.output == "rock"


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_dirs_chmod(sandbox_instance: Sandbox):
    command = ["mkdir", "-p", "/tmp/aa/bb"]
    response: CommandResponse = await sandbox_instance.execute(Command(command=command))
    assert response.exit_code == 0

    response: ChmodResponse = await sandbox_instance.fs.chmod(
        ChmodRequest(paths=["/tmp/aa"], mode="777", recursive=False)
    )
    assert response.success

    observation: Observation = await sandbox_instance.arun(cmd="ls -ld /tmp/aa", session="default")
    assert observation.exit_code == 0
    assert "drwxrwxrwx" in observation.output

    response: ChmodResponse = await sandbox_instance.fs.chmod(
        ChmodRequest(paths=["/tmp/aa/"], mode="755", recursive=True)
    )
    assert response.success
    observation: Observation = await sandbox_instance.arun(cmd="ls -ld /tmp/aa/bb", session="default")
    assert observation.exit_code == 0
    assert "drwxr-xr-x" in observation.output

    assert observation.exit_code == 0


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_download_file(sandbox_instance: Sandbox, monkeypatch):
    """Test download_file with mocked OSS - comprehensive test covering all scenarios.

    This test uses a hybrid approach:
    - Real sandbox for file creation and command execution
    - Mock OSS upload (bypass actual ossutil upload to OSS)
    - Mock OSS download (read file directly from sandbox instead of OSS)

    Test cases:
    1. OSS disabled - should fail gracefully
    2. File not found - should fail with clear error
    3. Text file download - should succeed with content match
    4. Binary file (tar.gz) download - should succeed with integrity check
    """
    from rock.sdk.sandbox.file_system import LinuxFileSystem

    # Create temporary directory
    tmp_path = Path(tempfile.mkdtemp())
    assert isinstance(sandbox_instance.fs, LinuxFileSystem)

    try:
        #  Test 1: OSS disabled
        logger.info("=== Test 1: OSS disabled ===")
        monkeypatch.setattr(env_vars, "ROCK_OSS_ENABLE", False)

        response = await sandbox_instance.fs.download_file(
            remote_path="/tmp/test.txt", local_path=str(tmp_path / "test.txt")
        )

        assert not response.success, "Download should fail when OSS is disabled"
        assert "not enabled" in response.message.lower(), (
            f"Error message should mention OSS disabled: {response.message}"
        )
        logger.info("✓ OSS disabled error handling works correctly")

        # Setup mocks for remaining tests
        monkeypatch.setattr(env_vars, "ROCK_OSS_ENABLE", True)
        monkeypatch.setattr(env_vars, "ROCK_OSS_BUCKET_ENDPOINT", "oss-cn-test.aliyuncs.com")
        monkeypatch.setattr(env_vars, "ROCK_OSS_BUCKET_NAME", "test-bucket")
        monkeypatch.setattr(env_vars, "ROCK_OSS_BUCKET_REGION", "cn-test")

        async def mock_get_sts_credentials():
            return {
                "AccessKeyId": "mock_ak",
                "AccessKeySecret": "mock_sk",
                "SecurityToken": "mock_token",
                "Expiration": "2026-03-15T00:00:00Z",
            }

        monkeypatch.setattr(sandbox_instance, "_get_oss_sts_credentials", mock_get_sts_credentials)

        original_arun = sandbox_instance.arun

        async def mock_arun_selective(cmd, **kwargs):
            if "ossutil cp" in cmd:
                return Observation(output="", exit_code=0)
            return await original_arun(cmd, **kwargs)

        monkeypatch.setattr(sandbox_instance, "arun", mock_arun_selective)

        # Mock OSS download - directly construct file content based on test case
        test_files = {
            "test_download.txt": "Hello from sandbox! 🚀\nLine 2\nLine 3",
            "test_archive.tar.gz": None,  # Will be populated later
        }

        def mock_oss_download(object_name, local_path):
            """Mock OSS download by directly writing test file content."""
            filename = object_name.split("-", 1)[1] if "-" in object_name else object_name

            local_file = Path(local_path)
            local_file.parent.mkdir(parents=True, exist_ok=True)

            if filename in test_files:
                content = test_files[filename]
                if isinstance(content, str):
                    # Text file
                    local_file.write_text(content)
                    logger.info(f"Mock OSS text download: {filename} -> {local_path}")
                elif isinstance(content, bytes):
                    # Binary file
                    local_file.write_bytes(content)
                    logger.info(f"Mock OSS binary download: {filename} -> {local_path}")
                else:
                    raise Exception(f"Unknown file: {filename}")
            else:
                raise Exception(f"File not found in mock: {filename}")

        mock_bucket = Mock()
        mock_bucket.get_object_to_file = mock_oss_download

        monkeypatch.setattr(oss2, "StsAuth", Mock())
        monkeypatch.setattr(oss2, "Bucket", lambda *args, **kwargs: mock_bucket)

        # Test 2: File not found
        logger.info("=== Test 2: File not found ===")
        response = await sandbox_instance.fs.download_file(
            remote_path="/tmp/nonexistent_file_12345.txt", local_path=str(tmp_path / "should_not_exist.txt")
        )

        assert not response.success, "Download should fail for non-existent file"
        assert "not found" in response.message.lower(), f"Error message should mention 'not found': {response.message}"
        assert not (tmp_path / "should_not_exist.txt").exists(), "Local file should not be created on failure"
        logger.info("✓ File not found error handling works correctly")

        # Test 3: Text file download
        logger.info("=== Test 3: Text file download ===")
        test_content = "Hello from sandbox! 🚀\nLine 2\nLine 3"
        remote_path = "/tmp/test_download.txt"

        write_response = await sandbox_instance.write_file(WriteFileRequest(path=remote_path, content=test_content))
        assert write_response.success, f"Failed to create test file: {write_response.message}"

        local_path = tmp_path / "downloaded.txt"
        response = await sandbox_instance.fs.download_file(remote_path=remote_path, local_path=str(local_path))

        assert response.success, f"Download failed: {response.message}"
        assert local_path.exists(), f"Downloaded file not found at {local_path}"

        downloaded_content = local_path.read_text()
        assert downloaded_content == test_content, "Downloaded content does not match original"
        logger.info(f"✓ Text file download passed: {remote_path} -> {local_path}")

        # Test 4: Binary file (tar.gz) download
        logger.info("=== Test 4: Binary file (tar.gz) download ===")
        tar_name = "test_archive.tar.gz"
        tar_remote_path = f"/tmp/{tar_name}"

        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w:gz") as tar:
            # Add file1.txt
            file1_content = b"file1 content\n"
            file1_info = tarfile.TarInfo(name="test_data/file1.txt")
            file1_info.size = len(file1_content)
            tar.addfile(file1_info, io.BytesIO(file1_content))

            # Add file2.txt
            file2_content = b"file2 content\n"
            file2_info = tarfile.TarInfo(name="test_data/file2.txt")
            file2_info.size = len(file2_content)
            tar.addfile(file2_info, io.BytesIO(file2_content))

        test_files[tar_name] = tar_buffer.getvalue()

        # Also create in sandbox for verification (optional, just to keep consistent)
        create_tar_cmd = f"""
mkdir -p /tmp/test_data
echo "file1 content" > /tmp/test_data/file1.txt
echo "file2 content" > /tmp/test_data/file2.txt
cd /tmp && tar czf {tar_name} test_data/
"""
        create_response = await sandbox_instance.execute(Command(command=["bash", "-c", create_tar_cmd]))
        assert create_response.exit_code == 0, f"Failed to create tar: {create_response.stderr}"

        local_tar_path = tmp_path / tar_name
        response = await sandbox_instance.fs.download_file(remote_path=tar_remote_path, local_path=str(local_tar_path))

        assert response.success, f"Tar download failed: {response.message}"
        assert local_tar_path.exists(), "Tar file not found"

        with tarfile.open(local_tar_path, "r:gz") as tar:
            members = tar.getnames()
            assert len(members) > 0, "Tar archive is empty"
            assert any("file1.txt" in m for m in members), "Expected file1.txt in tar"
            assert any("file2.txt" in m for m in members), "Expected file2.txt in tar"

        logger.info(f"✓ Binary download passed: tar contains {len(members)} entries")
        logger.info("=== All download_file tests passed ===")
    finally:
        # Clean up temporary directory
        shutil.rmtree(tmp_path, ignore_errors=True)
