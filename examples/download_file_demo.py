#!/usr/bin/env python3
"""Demo script for download_file functionality.

This script demonstrates how to download files from a ROCK sandbox to the local machine
using the Aliyun OSS-based download_file() method.

Prerequisites:
-------------
1. Running ROCK admin service
2. Aliyun OSS credentials configured on the admin service
3. Required environment variables set (see below)

Required Environment Variables:
------------------------------
All configuration is read from environment variables (with ROCK defaults):
- ROCK_BASE_URL: Admin service URL (default: http://localhost:8080)
- ROCK_OSS_ENABLE: Set to "true" to enable OSS-based file transfer
- ROCK_OSS_BUCKET_ENDPOINT: OSS endpoint (e.g., "oss-cn-hangzhou.aliyuncs.com")
- ROCK_OSS_BUCKET_NAME: OSS bucket name
- ROCK_OSS_BUCKET_REGION: OSS region (e.g., "cn-hangzhou")

Usage:
------
    # Set required environment variables
    export ROCK_OSS_ENABLE="true"
    export ROCK_OSS_BUCKET_ENDPOINT="<your-oss-endpoint>"
    export ROCK_OSS_BUCKET_NAME="<your-bucket-name>"
    export ROCK_OSS_BUCKET_REGION="<your-region>"

    # Optional: Override default base URL
    export ROCK_BASE_URL="<your-admin-url>"

    # Run the demo (uses default base URL if not set)
    uv run python examples/download_file_demo.py

    # For deployments requiring authentication, pass --auth argument
    uv run python examples/download_file_demo.py --auth <your-auth-token>

Features Demonstrated:
---------------------
1. Creating a text file in sandbox and downloading it
2. Error handling for non-existent files
3. Downloading binary files (tar.gz archives)
4. Content verification after download
"""

import argparse
import asyncio
import logging
import sys
import tempfile
from pathlib import Path

from rock import env_vars
from rock.actions.sandbox.request import WriteFileRequest
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig

# Set logging level to see all logs
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s - %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Demo script for downloading files from ROCK sandbox using OSS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Use default base URL (http://localhost:8080)
  ROCK_OSS_ENABLE=true \\
  ROCK_OSS_BUCKET_ENDPOINT=<your-oss-endpoint> \\
  ROCK_OSS_BUCKET_NAME=<your-bucket-name> \\
  ROCK_OSS_BUCKET_REGION=<your-region> \\
  uv run python examples/download_file_demo.py

  # Override base URL via environment variable
  ROCK_BASE_URL=<your-admin-url> \\
  ROCK_OSS_ENABLE=true \\
  ROCK_OSS_BUCKET_ENDPOINT=<your-oss-endpoint> \\
  ROCK_OSS_BUCKET_NAME=<your-bucket-name> \\
  ROCK_OSS_BUCKET_REGION=<your-region> \\
  uv run python examples/download_file_demo.py

  # Remote deployment with authentication
  ROCK_BASE_URL=<your-admin-url> \\
  ROCK_OSS_ENABLE=true \\
  ROCK_OSS_BUCKET_ENDPOINT=<your-oss-endpoint> \\
  ROCK_OSS_BUCKET_NAME=<your-bucket-name> \\
  ROCK_OSS_BUCKET_REGION=<your-region> \\
  uv run python examples/download_file_demo.py --auth <your-auth-token>

Environment Variables Required:
  ROCK_OSS_ENABLE=true
  ROCK_OSS_BUCKET_ENDPOINT=<your-oss-endpoint>
  ROCK_OSS_BUCKET_NAME=<your-bucket-name>
  ROCK_OSS_BUCKET_REGION=<your-region>

Optional Environment Variables:
  ROCK_BASE_URL=http://localhost:8080  (default if not set)
        """,
    )
    parser.add_argument(
        "--auth",
        help="Optional authorization token (ROCK_XRL_AUTHORIZATION header value)",
    )
    return parser.parse_args()


def check_environment_variables():
    """Check and validate required environment variables."""
    required_vars = {
        "ROCK_OSS_ENABLE": "Set to 'true' to enable OSS",
        "ROCK_OSS_BUCKET_ENDPOINT": "OSS endpoint (e.g., oss-cn-hangzhou.aliyuncs.com)",
        "ROCK_OSS_BUCKET_NAME": "OSS bucket name",
        "ROCK_OSS_BUCKET_REGION": "OSS region (e.g., cn-hangzhou)",
    }

    missing_vars = []
    for var, description in required_vars.items():
        value = getattr(env_vars, var, None)
        if not value or (isinstance(value, str) and not value.strip()):
            missing_vars.append(f"  - {var}: {description}")

    if missing_vars:
        print("❌ Missing required environment variables:\n")
        print("\n".join(missing_vars))
        print("\nPlease set these variables before running the demo.")
        print("\nExample:")
        print('  export ROCK_OSS_ENABLE="true"')
        print('  export ROCK_OSS_BUCKET_ENDPOINT="<your-oss-endpoint>"')
        print('  export ROCK_OSS_BUCKET_NAME="<your-bucket-name>"')
        print('  export ROCK_OSS_BUCKET_REGION="<your-region>"')
        sys.exit(1)

    print("✓ All required environment variables are set\n")


async def test_download_file(auth: str | None = None):
    """Test download_file with real sandbox."""
    # Check environment variables
    check_environment_variables()

    # Read from environment variables
    base_url = env_vars.ROCK_BASE_URL

    print("=== Configuration ===")
    print(f"ROCK_BASE_URL: {base_url}")
    if auth:
        print(f"ROCK_XRL_AUTHORIZATION: {'*' * 10}")
    print(f"ROCK_OSS_ENABLE: {env_vars.ROCK_OSS_ENABLE}")
    print(f"ROCK_OSS_BUCKET_ENDPOINT: {env_vars.ROCK_OSS_BUCKET_ENDPOINT}")
    print(f"ROCK_OSS_BUCKET_NAME: {env_vars.ROCK_OSS_BUCKET_NAME}")
    print(f"ROCK_OSS_BUCKET_REGION: {env_vars.ROCK_OSS_BUCKET_REGION}")
    print()

    # Create sandbox config
    config_kwargs = {"base_url": base_url}
    if auth:
        config_kwargs["xrl_authorization"] = auth
    config = SandboxConfig(**config_kwargs)

    # Create and start sandbox
    sandbox = Sandbox(config)

    print("=== Starting Sandbox ===")
    try:
        await sandbox.start()
        print(f"✓ Sandbox started: {sandbox.sandbox_id}")
        print(f"  Use rockcli to debug: rockcli sandbox {sandbox.sandbox_id} exec <command>")
        print()

        # Add pause to allow manual debugging (commented out for auto test)
        # import time
        # print("Sleeping 10 seconds for manual debugging...")
        # time.sleep(10)

        # Test 1: Create a test file in sandbox
        print("=== Test 1: Create test file in sandbox ===")
        test_content = "Hello from sandbox! 测试内容 🚀\nLine 2\nLine 3"
        remote_file_path = "/tmp/test_download_file.txt"

        write_response = await sandbox.write_file(WriteFileRequest(path=remote_file_path, content=test_content))
        if write_response.success:
            print(f"✓ Test file created: {remote_file_path}")
        else:
            print(f"✗ Failed to create test file: {write_response.message}")
            return
        print()

        # Test 2: Download file using download_file
        print("=== Test 2: Download file using download_file ===")
        with tempfile.TemporaryDirectory() as tmpdir:
            local_file_path = Path(tmpdir) / "downloaded_file.txt"
            print(f"Local target path: {local_file_path}")

            download_response = await sandbox.fs.download_file(
                remote_path=remote_file_path, local_path=str(local_file_path)
            )

            if download_response.success:
                print(f"✓ Download successful: {download_response.message}")
            else:
                print(f"✗ Download failed: {download_response.message}")
                return
            print()

            # Test 3: Verify file content
            print("=== Test 3: Verify file content ===")
            if local_file_path.exists():
                print(f"✓ Downloaded file exists: {local_file_path}")
                with open(local_file_path, encoding="utf-8") as f:
                    downloaded_content = f.read()

                if downloaded_content == test_content:
                    print("✓ Content matches!")
                    print(f"Original content:\n{test_content}")
                    print(f"\nDownloaded content:\n{downloaded_content}")
                else:
                    print("✗ Content mismatch!")
                    print(f"Expected:\n{test_content}")
                    print(f"\nGot:\n{downloaded_content}")
            else:
                print(f"✗ Downloaded file not found at: {local_file_path}")
            print()

        # Test 4: Test error case - non-existent file
        print("=== Test 4: Test error case (non-existent file) ===")
        with tempfile.TemporaryDirectory() as tmpdir:
            local_file_path = Path(tmpdir) / "should_not_exist.txt"

            download_response = await sandbox.fs.download_file(
                remote_path="/tmp/non_existent_file_12345.txt", local_path=str(local_file_path)
            )

            if not download_response.success:
                print(f"✓ Correctly failed: {download_response.message}")
                if "not found" in download_response.message.lower():
                    print("✓ Error message is correct")
                else:
                    print(f"⚠ Error message doesn't contain 'not found': {download_response.message}")
            else:
                print(f"✗ Should have failed but succeeded: {download_response.message}")

            if not local_file_path.exists():
                print("✓ Local file correctly not created")
            else:
                print("✗ Local file was created unexpectedly")
        print()

        # Test 5: Download a tar archive to current directory
        print("=== Test 5: Download tar archive to current directory ===")
        # Create a tar archive in sandbox with some test files
        from rock.actions.sandbox.request import Command

        tar_name = "test_archive.tar.gz"
        tar_remote_path = f"/tmp/{tar_name}"

        # Create test directory structure and tar it
        create_tar_cmd = f"""
mkdir -p /tmp/test_data
echo "file1 content" > /tmp/test_data/file1.txt
echo "file2 content" > /tmp/test_data/file2.txt
mkdir -p /tmp/test_data/subdir
echo "file3 content" > /tmp/test_data/subdir/file3.txt
cd /tmp && tar czf {tar_name} test_data/
ls -lh {tar_remote_path}
"""
        create_response = await sandbox.execute(Command(command=["bash", "-c", create_tar_cmd]))
        if create_response.exit_code == 0:
            print(f"✓ Tar archive created in sandbox: {tar_remote_path}")
            print(f"  Archive info:\n{create_response.stdout}")
        else:
            print(f"✗ Failed to create tar archive: {create_response.stderr}")
            print()
            print("=== All Tests Completed ===")
            return

        # Download to current directory
        current_dir = Path.cwd()
        local_tar_path = current_dir / tar_name
        print(f"Downloading to current directory: {local_tar_path}")

        download_response = await sandbox.fs.download_file(remote_path=tar_remote_path, local_path=str(local_tar_path))

        if download_response.success:
            print(f"✓ Tar download successful: {download_response.message}")
        else:
            print(f"✗ Tar download failed: {download_response.message}")
            print()
            print("=== All Tests Completed ===")
            return

        # Verify tar file exists and is valid
        if local_tar_path.exists():
            print(f"✓ Tar file exists: {local_tar_path}")
            file_size = local_tar_path.stat().st_size
            print(f"  File size: {file_size} bytes")

            # Test extraction to verify integrity
            import tarfile

            try:
                with tarfile.open(local_tar_path, "r:gz") as tar:
                    members = tar.getnames()
                    print(f"✓ Tar file is valid, contains {len(members)} entries:")
                    for member in members[:5]:  # Show first 5 entries
                        print(f"    - {member}")
                    if len(members) > 5:
                        print(f"    ... and {len(members) - 5} more")
            except Exception as e:
                print(f"✗ Tar file is corrupted: {e}")
        else:
            print(f"✗ Tar file not found at: {local_tar_path}")

        # Cleanup downloaded tar (commented out to keep file for inspection)
        print(f"⚠ Keeping tar file for inspection: {local_tar_path}")
        # try:
        #     if local_tar_path.exists():
        #         local_tar_path.unlink()
        #         print(f"✓ Cleaned up downloaded tar: {local_tar_path}")
        # except Exception as e:
        #     print(f"⚠ Failed to cleanup tar: {e}")
        print()

        print("=== All Tests Completed ===")

    except Exception as e:
        print(f"✗ Error during test: {e}")
        import traceback

        traceback.print_exc()

    finally:
        # Cleanup (commented out to keep sandbox alive for debugging)
        print("\n=== Cleanup ===")
        print(f"⚠ Sandbox {sandbox.sandbox_id} is kept alive for debugging")
        print(f'  Use: rockcli sandbox {sandbox.sandbox_id} exec -- "<command>"')
        print(f"  To stop: rockcli sandbox {sandbox.sandbox_id} stop")
        # try:
        #     await sandbox.close()
        #     print("✓ Sandbox closed")
        # except Exception as e:
        #     print(f"⚠ Failed to close sandbox: {e}")


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(test_download_file(auth=args.auth))
