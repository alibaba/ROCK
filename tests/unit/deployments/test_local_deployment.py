import platform

import pytest

from rock.admin.proto.request import SandboxBashAction as BashAction
from rock.admin.proto.request import SandboxCreateBashSessionRequest as CreateBashSessionRequest
from rock.deployments.local import LocalDeployment


@pytest.mark.asyncio
async def test_local_deployment():
    d = LocalDeployment()
    assert not await d.is_alive()
    await d.start()
    assert await d.is_alive()
    await d.stop()
    assert not await d.is_alive()


@pytest.mark.skipif(platform.system() != "Linux", reason="nohup behavior differs on non-Linux systems")
async def test_nohup_output_command():
    d = LocalDeployment()
    await d.start()
    sid = "local-test"
    await d.runtime.create_session(CreateBashSessionRequest(session_type="bash", sandbox_id=sid))

    cmd_with_nohup = 'nohup echo "hello, rock" > /tmp/nohup_test.out 2>&1 &'
    await d.runtime.run_in_session(BashAction(command=cmd_with_nohup, sandbox_id=sid))
    nohup_resp = await d.runtime.run_in_session(BashAction(command="cat /tmp/nohup_test.out", sandbox_id=sid))
    assert "nohup: ignoring input" in nohup_resp.output

    cmd_without_nohup = 'nohup echo "hello, rock" < /dev/null > /tmp/nohup_test.out 2>&1 &'
    await d.runtime.run_in_session(BashAction(command=cmd_without_nohup, sandbox_id=sid))
    nohup_resp = await d.runtime.run_in_session(BashAction(command="cat /tmp/nohup_test.out", sandbox_id=sid))
    assert "nohup: ignoring input" not in nohup_resp.output

    await d.runtime.run_in_session(BashAction(command="rm -rf /tmp/nohup_test.out", sandbox_id=sid))
    await d.stop()
    assert not await d.is_alive()
