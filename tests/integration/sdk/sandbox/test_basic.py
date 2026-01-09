import os
import tempfile
import warnings

import pytest

from rock.actions import CreateBashSessionRequest, ReadFileRequest, UploadRequest, WriteFileRequest
from rock.logger import init_logger
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig
from tests.integration.conftest import SKIP_IF_NO_DOCKER, RemoteServer

logger = init_logger(__name__)


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_sandbox_file_operations(admin_remote_server: RemoteServer):
    """Test sandbox file operations including upload, write, and read.

    This test verifies:
    1. Sandbox startup and session creation
    2. Directory navigation
    3. File upload to sandbox
    4. File write operations
    5. File read operations using different methods
    """
    # Configure to use the admin server from fixture

    # 1. Start sandbox
    config = SandboxConfig(
        image="python:3.11",
        startup_timeout=60,
        base_url=f"{admin_remote_server.endpoint}:{admin_remote_server.port}",
    )
    sandbox = Sandbox(config)

    try:
        await sandbox.start()

        # Create a bash session
        await sandbox.create_session(CreateBashSessionRequest(session="bash-session"))

        # 2. cd home and pwd
        await sandbox.arun(cmd="cd ~", session="bash-session")
        response = await sandbox.arun(cmd="pwd", session="bash-session")
        home_dir = response.output.strip()
        assert home_dir, "Home directory should not be empty"

        # 3. Create a temporary test.txt and upload to sandbox home directory
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tmp_file:
            tmp_file.write("Initial content")
            tmp_file_path = tmp_file.name

            try:
                # Use absolute path instead of ~
                target_path = f"{home_dir}/test.txt"
                upload_response = await sandbox.upload(
                    UploadRequest(source_path=tmp_file_path, target_path=target_path)
                )
                assert upload_response.success, "Upload failed"

                # Verify file exists
                verify_response = await sandbox.arun(cmd=f"ls -la {target_path}", session="bash-session")
                assert (
                    target_path in verify_response.output or "test.txt" in verify_response.output
                ), "File not found after upload"

            finally:
                # Clean up local temp file
                os.unlink(tmp_file_path)

        # 4. Write "hello world" to the file
        write_path = f"{home_dir}/test.txt"
        write_response = await sandbox.write_file(WriteFileRequest(content="hello world", path=write_path))
        assert write_response.success, "Write file failed"

        # 5. Read file using both methods and compare
        # Method 1: read_file (complete content)
        read_full_response = await sandbox.read_file(ReadFileRequest(path=write_path))
        full_content = read_full_response.content.strip()
        assert full_content, "File content should not be empty"

        # Method 2: read_file_by_line_range (first line)
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            read_range_response = await sandbox.read_file_by_line_range(write_path, start_line=1, end_line=1)
        range_content = read_range_response.content.strip()

        # Compare results
        assert full_content == "hello world", f"Expected 'hello world', got '{full_content}'"
        assert range_content == "hello world", f"Expected 'hello world', got '{range_content}'"
        assert full_content == range_content, "Content mismatch between read methods"

        # Bonus: verify with cat command
        cat_response = await sandbox.arun(cmd=f"cat {write_path}", session="bash-session")
        cat_output = cat_response.output.strip()
        assert cat_output == "hello world", f"cat command mismatch: expected 'hello world', got '{cat_output}'"

    finally:
        # Cleanup
        await sandbox.stop()


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_read_file_by_line_range_non_exist_file(sandbox_instance: Sandbox):
    file_path = "non_exist_file.txt"
    with pytest.raises(Exception):
        await sandbox_instance.read_file_by_line_range(file_path, start_line=1, end_line=10)


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_sandbox(sandbox_instance: Sandbox):
    result = await sandbox_instance.arun(cmd="echo Hello ROCK", session="default")

    # Assertions
    assert result.output is not None
    assert "Hello ROCK" in result.output

    logger.info("\n" + "*" * 50 + "\n" + result.output + "\n" + "*" * 50 + "\n")
