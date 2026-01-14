import time

import pytest

from rock.actions.sandbox.request import ReadFileRequest
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig
from tests.integration.conftest import SKIP_IF_NO_DOCKER


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_arun_nohup(sandbox_instance: Sandbox):
    cat_cmd = "cat > /tmp/nohup_test.txt << 'EOF'\n#!/usr/bin/env python3\nimport os\nEOF"
    cmd = f"/bin/bash -c '{cat_cmd}'"
    resp = await sandbox_instance.arun(session="default", cmd=cmd, mode="nohup")
    print(resp.output)
    nohup_test_resp = await sandbox_instance.arun(session="default", cmd="cat /tmp/nohup_test.txt")
    assert "import os" in nohup_test_resp.output

    detached_resp = await sandbox_instance.arun(
        session="default",
        cmd="/bin/bash -c 'echo detached-output'",
        mode="nohup",
        ignore_output=True,
    )
    output_line = next((line for line in detached_resp.output.splitlines() if line.startswith("Output file:")), None)
    assert output_line is not None
    output_file = output_line.split(":", 1)[1].strip()
    assert "without streaming the log content" in detached_resp.output
    # Verify file size is included in output
    assert "File size:" in detached_resp.output

    file_content_resp = await sandbox_instance.arun(session="default", cmd=f"cat {output_file}")
    assert "detached-output" in file_content_resp.output
    await sandbox_instance.arun(session="default", cmd=f"rm -f {output_file}")

    await sandbox_instance.arun(session="default", cmd="rm -rf /tmp/nohup_test.txt")

@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_arun_timeout(sandbox_instance: Sandbox):
    cmd = r"sed -i '292i\
             {!r}' my_file.txt"
    start_time = time.perf_counter()
    resp = await sandbox_instance.arun(session="default", cmd=f'timeout 180 /bin/bash -c "{cmd}"', mode="nohup")
    print(resp.output)
    assert resp.exit_code == 1
    assert time.perf_counter() - start_time < 180
    assert time.perf_counter() - start_time > 30
    assert resp.output.__contains__("Command execution failed due to timeout")

    await sandbox_instance.stop()

@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_sandbox_get_status(admin_remote_server):
    config = SandboxConfig(
        image="fake_image:latest",
        memory="8g",
        cpus=2.0,
        base_url=f"{admin_remote_server.endpoint}:{admin_remote_server.port}",
        startup_timeout=10,
    )
    sandbox = Sandbox(config)
    with pytest.raises(Exception) as exc_info:
        await sandbox.start()
    assert "Failed to start sandbox" in str(exc_info.value)
    sandbox.stop()

@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_update_mount(sandbox_instance: Sandbox):
    with pytest.raises(Exception) as exc_info:
        await sandbox_instance.arun(session="default", cmd="rm -rf /tmp/miniforge/bin")
    assert "Read-only file system" in str(exc_info.value)

    with pytest.raises(Exception) as exc_info:
        await sandbox_instance.arun(session="default", cmd="rm -rf /tmp/local_files/docker_run.sh")
    assert "Read-only file system" in str(exc_info.value)

    with pytest.raises(Exception) as exc_info:
        await sandbox_instance.arun(session="default", cmd="chmod +x /tmp/local_files/docker_run.sh")
    assert "Read-only file system" in str(exc_info.value)

    with pytest.raises(Exception) as exc_info:
        await sandbox_instance.arun(session="default", cmd="touch /tmp/local_files/test.txt")
    assert "Read-only file system" in str(exc_info.value)


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_execute(sandbox_instance: Sandbox):
    from rock.actions.sandbox.request import Command

    curr_status = await sandbox_instance.get_status()
    if curr_status.is_alive:
        resp1 = await sandbox_instance.execute(Command(command="pwd", cwd="/root"))
        assert resp1.stdout.strip() == "/root"
        resp2 = await sandbox_instance.execute(Command(command="pwd", cwd="/tmp"))
        assert resp2.stdout.strip() == "/tmp"

@pytest.mark.parametrize(
    "sandbox_instance",
    [{"cpus": 4}],
    indirect=True,
)
@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_start_sandbox_upper_limit(sandbox_instance: Sandbox):
    from rock.actions import SandboxStatusResponse
    status: SandboxStatusResponse = await sandbox_instance.get_status()
    assert status.cpus == 4

@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_arun_ignore_output(sandbox_instance: Sandbox):
    cmd = """for i in {1..5}; do echo "Line $i"; done"""
    output_file = "output"
    error_resp = await sandbox_instance.arun(
        cmd=f"bash -c '{cmd}'", session="default", mode="nohup", ignore_output=True, output_file=output_file
    )
    assert error_resp.exit_code == 1
    assert f"Failed parse output file path: {output_file}" in error_resp.failure_reason
    output_file = "tmp/file.txt"
    file_output = await sandbox_instance.arun(
        cmd=f"bash -c '{cmd}'", session="default", mode="nohup", ignore_output=True, output_file=output_file
    )
    assert output_file in file_output.output
    output_file = "file.txt"
    file_output = await sandbox_instance.arun(
        cmd=f"bash -c '{cmd}'", session="default", mode="nohup", ignore_output=True, output_file=output_file
    )
    assert output_file in file_output.output
    output_file = "/root/mydir/file.txt"
    file_output = await sandbox_instance.arun(
        cmd=f"bash -c '{cmd}'", session="default", mode="nohup", ignore_output=True, output_file=output_file
    )
    assert output_file in file_output.output
    l4_resp = await sandbox_instance.read_file(ReadFileRequest(path=output_file))
    assert "Line 4" in l4_resp.content
