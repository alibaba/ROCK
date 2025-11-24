import time

import pytest

from rock.actions.sandbox.request import CreateBashSessionRequest
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig
from tests.integration.conftest import SKIP_IF_NO_DOCKER, RemoteServer


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_arun_nohup(admin_remote_server: RemoteServer):
    config = SandboxConfig(
        image="python:3.11",
        base_url=f"{admin_remote_server.endpoint}:{admin_remote_server.port}",
    )
    sandbox = Sandbox(config)
    await sandbox.start()
    await sandbox.create_session(CreateBashSessionRequest(session="bash-1"))
    cat_cmd = "cat > /tmp/nohup_test.txt << 'EOF'\n#!/usr/bin/env python3\nimport os\nEOF"
    cmd = f"/bin/bash -c '{cat_cmd}'"
    resp = await sandbox.arun(session="bash-1", cmd=cmd, mode="nohup")
    print(resp.output)
    nohup_test_resp = await sandbox.arun(session="bash-1", cmd="cat /tmp/nohup_test.txt")
    assert "import os" in nohup_test_resp.output
    await sandbox.arun(session="bash-1", cmd="rm -rf /tmp/nohup_test.txt")
    await sandbox.stop()

@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_arun_timeout(admin_remote_server: RemoteServer):
    config = SandboxConfig(
        image="python:3.11",
        base_url=f"{admin_remote_server.endpoint}:{admin_remote_server.port}",
    )
    sandbox = Sandbox(config)
    await sandbox.start()
    await sandbox.create_session(CreateBashSessionRequest(session="bash-1"))
    cmd = r"sed -i '292i\
             {!r}' my_file.txt"
    start_time = time.perf_counter()
    resp = await sandbox.arun(session="bash-1", cmd=f'timeout 180 /bin/bash -c "{cmd}"', mode="nohup")
    print(resp.output)
    assert resp.exit_code == 1
    assert time.perf_counter() - start_time < 180
    assert time.perf_counter() - start_time > 30
    assert resp.output.__contains__("Command execution failed due to timeout")

    await sandbox.stop()
