from rock.deployments.local import LocalDeployment
from rock.rocklet.proto.request import InternalBashAction as BashAction
from rock.rocklet.proto.request import InternalCreateBashSessionRequest as CreateBashSessionRequest
from rock.utils import extract_nohup_pid


async def test_nohup_exatract_id():
    d = LocalDeployment()
    await d.start()
    await d.runtime.create_session(CreateBashSessionRequest())
    cat_cmd = "cat > /tmp/nohup_test.txt << 'EOF'\n#!/usr/bin/env python3\nimport os\nEOF"
    cmd = f"/bin/bash -c '{cat_cmd}'"
    nohup_command = f"nohup {cmd} < /dev/null > /tmp/nohup_test.out 2>&1 & echo $!;disown"
    resp = await d.runtime.run_in_session(BashAction(command=nohup_command))
    pid = extract_nohup_pid(resp.output)
    assert pid.isdigit()
    nohup_resp = await d.runtime.run_in_session(BashAction(command="cat /tmp/nohup_test.txt"))
    assert "import os" in nohup_resp.output
    await d.runtime.run_in_session(BashAction(command="rm -rf /tmp/nohup_test.txt"))
    await d.runtime.run_in_session(BashAction(command="rm -rf /tmp/nohup_test.out"))
    await d.stop()
    assert not await d.is_alive()
