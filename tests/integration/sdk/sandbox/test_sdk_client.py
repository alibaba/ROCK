import time

import pytest

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
