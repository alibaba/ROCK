"""Integration tests for disk limit functionality."""

import pytest

from rock.actions import Command
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig
from rock.utils.docker import DockerUtil
from tests.integration.conftest import SKIP_IF_LOG_PATH_NOT_XFS, SKIP_IF_NO_DOCKER, SKIP_IF_NO_STORAGE_OPT


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@SKIP_IF_NO_STORAGE_OPT
@pytest.mark.asyncio
async def test_disk_limit_enforcement(admin_remote_server):
    """Test that the server-side rootfs disk limit is enforced when storage-opt is supported.

    This test is only run when storage-opt is supported (overlay2 + xfs + prjquota).

    Steps:
    1. Start a sandbox (server applies default disk_limit)
    2. Check sandbox status to verify disk_limit is reported
    3. Try to create a file larger than the limit (should fail)
    4. Create a small file (should succeed)
    """
    config = SandboxConfig(
        image="ubuntu:22.04",
        memory="2g",
        cpus=1.0,
        base_url=f"{admin_remote_server.endpoint}:{admin_remote_server.port}",
        startup_timeout=60,
    )

    sandbox = Sandbox(config)
    await sandbox.start()

    try:
        status = await sandbox.get_status()
        print(f"Sandbox status: disk_limit={status.disk_limit_rootfs}")

        if status.disk_limit_rootfs is None:
            pytest.skip(
                "Server has no disk limit configured (sandbox_disk_limit_rootfs not set in rock-xxx.yml or nacos)"
            )
        print(f"✅ Disk limit is set to {status.disk_limit_rootfs}")

        # Parse limit to determine a file size that exceeds it
        result = await sandbox.execute(
            Command(
                command=[
                    "/bin/bash",
                    "-c",
                    f"fallocate -l {status.disk_limit_rootfs.replace('g', '')}G /tmp/large_file.bin 2>&1 || echo 'EXPECTED_ERROR'",
                ]
            )
        )

        output = result.stdout + result.stderr
        print(f"fallocate output: {output}")

        error_occurred = (
            result.exit_code != 0
            or "No space left on device" in output
            or "fallocate failed" in output
            or "EXPECTED_ERROR" in output
        )

        assert error_occurred, (
            f"Expected disk space error when filling disk, " f"but got exit_code={result.exit_code}, output={output}"
        )
        print("✅ Disk limit enforcement verified")

        small_file_result = await sandbox.execute(
            Command(command=["/bin/bash", "-c", "fallocate -l 100M /tmp/small_file.bin && echo 'SUCCESS'"])
        )

        small_output = small_file_result.stdout + small_file_result.stderr
        assert small_file_result.exit_code == 0, (
            f"Expected small file (100MB) creation to succeed, " f"but got exit_code={small_file_result.exit_code}"
        )
        assert "SUCCESS" in small_output
        print("✅ Small file (100MB) creation succeeded")

    finally:
        await sandbox.stop()


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_disk_limit_default_value(admin_remote_server):
    """Test that the server applies a default disk_limit visible in status."""
    config = SandboxConfig(
        image="ubuntu:22.04",
        memory="2g",
        cpus=1.0,
        base_url=f"{admin_remote_server.endpoint}:{admin_remote_server.port}",
        startup_timeout=60,
    )

    sandbox = Sandbox(config)
    await sandbox.start()

    try:
        status = await sandbox.get_status()
        print(f"Sandbox status: disk_limit={status.disk_limit_rootfs}")

        storage_opt_supported = DockerUtil.detect_storage_opt_support()

        if not storage_opt_supported:
            assert (
                status.disk_limit_rootfs is None
            ), f"Expected disk_limit=None when storage-opt not supported, got {status.disk_limit_rootfs}"
            print("✅ Storage-opt not supported: disk_limit is None")
        else:
            # When storage-opt is supported, disk_limit reflects server config (may be None if not configured)
            print(f"✅ Server-reported disk_limit: {status.disk_limit_rootfs}")

    finally:
        await sandbox.stop()


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@SKIP_IF_NO_STORAGE_OPT
@SKIP_IF_LOG_PATH_NOT_XFS
@pytest.mark.asyncio
async def test_logging_path_disk_limit_enforcement(admin_remote_server):
    """Test that ROCK_LOGGING_PATH is also limited by disk quota.

    This test verifies that the log directory (ROCK_LOGGING_PATH) has a quota
    enforced via XFS project quota, separate from the rootfs limit.

    Steps:
    1. Start a sandbox (server applies default log dir quota)
    2. Check that ROCK_LOGGING_PATH env var is set in container
    3. Try to create a file larger than the log quota (should fail)
    4. Create a 500MB file in ROCK_LOGGING_PATH (should succeed)
    5. Verify rootfs and log directory are independently limited
    """
    config = SandboxConfig(
        image="ubuntu:22.04",
        memory="2g",
        cpus=1.0,
        base_url=f"{admin_remote_server.endpoint}:{admin_remote_server.port}",
        startup_timeout=60,
    )

    sandbox = Sandbox(config)
    await sandbox.start()

    try:
        env_result = await sandbox.execute(Command(command=["/bin/bash", "-c", "echo $ROCK_LOGGING_PATH"]))
        logging_path = env_result.stdout.strip()
        print(f"ROCK_LOGGING_PATH in container: {logging_path}")

        assert logging_path, "ROCK_LOGGING_PATH should be set in container"
        print(f"✅ ROCK_LOGGING_PATH is set to: {logging_path}")

        # Try to create a 1.5GB file in logging path (should fail due to log quota)
        large_log_result = await sandbox.execute(
            Command(
                command=[
                    "/bin/bash",
                    "-c",
                    f"fallocate -l 1500M {logging_path}/large_log.bin 2>&1 || echo 'EXPECTED_ERROR'",
                ]
            )
        )

        large_output = large_log_result.stdout + large_log_result.stderr
        print(f"Large log file creation output: {large_output}")
        print(f"Large log file creation exit_code: {large_log_result.exit_code}")

        error_occurred = (
            large_log_result.exit_code != 0
            or "No space left on device" in large_output
            or "Disk quota exceeded" in large_output
            or "fallocate failed" in large_output
            or "EXPECTED_ERROR" in large_output
        )

        assert error_occurred, (
            f"Expected disk quota error when creating 1.5GB file in log dir, "
            f"but got exit_code={large_log_result.exit_code}, output={large_output}"
        )
        print("✅ Log directory quota verified: 1.5GB file creation failed as expected")

        small_log_result = await sandbox.execute(
            Command(
                command=[
                    "/bin/bash",
                    "-c",
                    f"fallocate -l 500M {logging_path}/small_log.bin && echo 'SUCCESS'",
                ]
            )
        )

        small_output = small_log_result.stdout + small_log_result.stderr
        print(f"Small log file creation output: {small_output}")
        assert small_log_result.exit_code == 0, (
            f"Expected small log file (500MB) creation to succeed, "
            f"but got exit_code={small_log_result.exit_code}, output={small_output}"
        )
        assert "SUCCESS" in small_output
        print("✅ Small log file (500MB) creation in ROCK_LOGGING_PATH succeeded")

        rootfs_result = await sandbox.execute(
            Command(
                command=[
                    "/bin/bash",
                    "-c",
                    "fallocate -l 1G /tmp/rootfs_file.bin && echo 'ROOTFS_SUCCESS'",
                ]
            )
        )

        rootfs_output = rootfs_result.stdout + rootfs_result.stderr
        print(f"Rootfs file creation output: {rootfs_output}")
        assert rootfs_result.exit_code == 0, (
            f"Expected 1GB file on rootfs to succeed, "
            f"but got exit_code={rootfs_result.exit_code}, output={rootfs_output}"
        )
        assert "ROOTFS_SUCCESS" in rootfs_output
        print("✅ Rootfs and log directory are independently limited")

    finally:
        await sandbox.stop()
