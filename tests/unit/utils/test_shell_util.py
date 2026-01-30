import asyncio
import time
import uuid

import pytest

from rock.admin.proto.request import SandboxBashAction as BashAction
from rock.admin.proto.request import SandboxCreateBashSessionRequest
from rock.common.constants import PID_PREFIX, PID_SUFFIX
from rock.deployments.local import LocalDeployment
from rock.logger import init_logger
from rock.utils.system import extract_nohup_pid

logger = init_logger("test/utils/test_shell_util")


async def mock_arun(cmd: str, response_limited_bytes: int = 1024 * 64):
    temp_id = str(uuid.uuid4())[:8]
    out_file = f"/tmp/nohup_test_{temp_id}.out"
    session_name = f"bash-{temp_id}"
    d = LocalDeployment()
    await d.start()
    await d.runtime.create_session(
        SandboxCreateBashSessionRequest(session=session_name)
    )
    cmd = f"/bin/bash -c '{cmd}'"
    nohup_command = f"nohup {cmd} < /dev/null > {out_file} 2>&1 & echo {PID_PREFIX}$!{PID_SUFFIX};disown"
    resp = await d.runtime.run_in_session(
        BashAction(command=nohup_command, session=session_name)
    )
    logger.info(f"nohup_command response: {resp.output}")
    pid = extract_nohup_pid(resp.output)
    start_time = time.perf_counter()
    end_time = start_time + 60
    while time.perf_counter() < end_time:
        try:
            await asyncio.wait_for(
                d.runtime.run_in_session(
                    BashAction(command=f"kill -0 {pid}", session=session_name)
                ),
                timeout=30,
            )
            await asyncio.sleep(1)
        except Exception as e:
            print(str(e))
            break
    nohup_resp = await d.runtime.run_in_session(
        BashAction(command=f"head -c {response_limited_bytes} {out_file}", session=session_name)
    )
    yield pid, nohup_resp.output
    await d.runtime.run_in_session(
        BashAction(command=f"rm -rf {out_file}", session=session_name)
    )
    await d.stop()


@pytest.mark.asyncio
async def test_history_expansion_command():
    cmd = "cat > /tmp/nohup_test.txt << 'EOF'\n#!/usr/bin/env python3\nimport os\nEOF"
    async for pid, output in mock_arun(cmd):
        assert pid
        async for cat_pid, nohup_test_output in mock_arun("cat /tmp/nohup_test.txt"):
            assert cat_pid
            assert nohup_test_output.__contains__("import os")


@pytest.mark.asyncio
async def test_parameter_command():
    cmd = 'NAME=\'TestUser\' && echo "Hello $NAME" && echo "Current time: $(date)"'
    async for pid, output in mock_arun(cmd):
        assert pid
        assert output.startswith("Hello TestUser")


@pytest.mark.asyncio
async def test_awk_command():
    cmd = r'awk "{print \$2}" <<< "first second third"'
    async for pid, output in mock_arun(cmd):
        assert pid
        assert output == "second"


@pytest.mark.asyncio
async def test_awk_double_quotes_command():
    cmd = 'awk "{print $2}" <<< "first second third"'
    async for pid, output in mock_arun(cmd):
        assert pid
        assert output == "first second third"


@pytest.mark.asyncio
async def test_last_cmd_command():
    cmd = 'pwd;echo "$_"'
    async for pid, output in mock_arun(cmd):
        assert pid
        assert output.endswith("pwd")


@pytest.mark.asyncio
async def test_extract_pid():
    nohup_out = "[1] 40369^M\nPIDSTART40369PIDEND^M\nbash: /parameter: No such file or directory"
    pid = extract_nohup_pid(nohup_out)
    assert pid == 40369


@pytest.mark.asyncio
async def test_loop_echo_command():
    cmd = 'for i in 1 2 3; do echo "Line $i"; done'
    async for pid, output in mock_arun(cmd):
        assert pid
        assert output.endswith("Line 3")


@pytest.mark.asyncio
async def test_continuous_echo_command_1():
    cmd = "echo 'Line X' && echo 'Line Y' && echo 'Line Z'"
    async for pid, output in mock_arun(cmd):
        assert pid
        assert output == "Line"


@pytest.mark.asyncio
async def test_continuous_echo_command_2():
    cmd = 'echo "Line X" && echo "Line Y" && echo "Line Z"'
    async for pid, output in mock_arun(cmd):
        assert pid
        assert output.endswith("Line Z")


@pytest.mark.asyncio
async def test_top_command():
    cmd = "export TERM=xterm && top"
    async for pid, output in mock_arun(cmd):
        assert pid
        assert "failed tty get" in output or "Processes:" in output


@pytest.mark.asyncio
async def test_continuous_echo_script_command():
    temp_id = str(uuid.uuid4())[:8]
    script_file = f"/tmp/script_{temp_id}.sh"

    script_content = """#!/bin/bash
# Auto-generated script for nohup execution
echo "a"
echo "b"
echo "c"
"""
    write_script_cmd = (
        f"cat > {script_file} << 'SCRIPT_EOF'\n{script_content}SCRIPT_EOF"
    )
    async for pid, _ in mock_arun(write_script_cmd):
        assert pid
    async for pid, _ in mock_arun(f"chmod +x {script_file}"):
        assert pid
    async for pid, output in mock_arun(f"cat {script_file}"):
        assert pid
        assert output.__contains__("echo")

    async for pid, output in mock_arun(f"bash {script_file}"):
        assert pid
        assert output.__contains__("c")


@pytest.mark.asyncio
async def test_arun_with_limited_response():
    max_read_bytes = 1024 * 64
    large_file_size = 1024 * 1024 * 10
    cmd = f"openssl rand -hex {large_file_size}"
    try:
        async with asyncio.timeout(30):
            async for pid, output in mock_arun(cmd, max_read_bytes):
                assert pid
                assert len(output.encode("utf-8")) == max_read_bytes
    except asyncio.TimeoutError:
        pytest.fail("arun with limited response timeout")

    cmd = "echo abc"
    async for pid, output in mock_arun(cmd, max_read_bytes):
        assert pid
        assert output == "abc"
