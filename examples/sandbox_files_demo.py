import asyncio
import os
import tempfile
import warnings

from rock.actions import CreateBashSessionRequest
from rock.actions.sandbox.request import ReadFileRequest, UploadRequest, WriteFileRequest
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig


async def file_operations():
    """Combined test: start sandbox, navigate, upload, write, and read file"""

    # 1. Start sandbox
    config = SandboxConfig(image="python:3.11", startup_timeout=60)
    sandbox = Sandbox(config)
    await sandbox.start()
    print("✓ Sandbox started")

    # Create a bash session
    await sandbox.create_session(CreateBashSessionRequest(session="bash-session"))
    print("✓ Bash session created")

    # 2. cd home and pwd
    await sandbox.arun(cmd="cd ~", session="bash-session")
    response = await sandbox.arun(cmd="pwd", session="bash-session")
    home_dir = response.output.strip()
    print(f"✓ Changed to home directory: {home_dir}")

    # 3. Create a temporary test.txt and upload to sandbox home directory
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tmp_file:
        tmp_file.write("Initial content")
        tmp_file_path = tmp_file.name

        try:
            # Use absolute path instead of ~
            target_path = f"{home_dir}/test.txt"
            upload_response = await sandbox.upload(UploadRequest(source_path=tmp_file_path, target_path=target_path))
            assert upload_response.success, "Upload failed"
            print(f"✓ Uploaded {tmp_file_path} to {target_path}")

            # Verify file exists
            verify_response = await sandbox.arun(cmd=f"ls -la {target_path}", session="bash-session")
            print(f"✓ File verified: {verify_response.output.strip()}")

        finally:
            # Clean up local temp file
            os.unlink(tmp_file_path)

    # 4. Write "hello world" to the file
    write_path = f"{home_dir}/test.txt"
    write_response = await sandbox.write_file(WriteFileRequest(content="hello world", path=write_path))
    assert write_response.success, "Write file failed"
    print(f"✓ Wrote 'hello world' to {write_path}")

    # 5. Read file using both methods and compare
    # Method 1: read_file (complete content)
    read_full_response = await sandbox.read_file(ReadFileRequest(path=write_path))
    full_content = read_full_response.content.strip()
    print(f"✓ read_file result: '{full_content}'")

    # Method 2: read_file_by_line_range (first line)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        read_range_response = await sandbox.read_file_by_line_range(write_path, start_line=1, end_line=1)
    range_content = read_range_response.content.strip()
    print(f"✓ read_file_by_line_range result: '{range_content}'")

    # Compare results
    assert full_content == "hello world", f"Expected 'hello world', got '{full_content}'"
    assert range_content == "hello world", f"Expected 'hello world', got '{range_content}'"
    assert full_content == range_content, "Content mismatch between read methods"
    print("✓ Both read methods returned identical content: 'hello world'")

    # Bonus: verify with cat command
    cat_response = await sandbox.arun(cmd=f"cat {write_path}", session="bash-session")
    print(f"✓ cat command result: '{cat_response.output.strip()}'")
    assert cat_response.output.strip() == "hello world", "cat command mismatch"

    # Cleanup
    await sandbox.stop()
    print("✓ Sandbox stopped")

    print("\n✅ All operations completed successfully!")


if __name__ == "__main__":
    asyncio.run(file_operations())
