import time

import pytest

from rock.sdk.sandbox.client import Sandbox
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
        collect_output=False,
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
