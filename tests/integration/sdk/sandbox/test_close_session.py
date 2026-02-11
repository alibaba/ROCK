import pytest
import os
from pathlib import Path
from rock.sdk.sandbox.client import Sandbox
from rock.actions import CreateBashSessionRequest, CloseBashSessionRequest, BashAction
from tests.integration.conftest import SKIP_IF_NO_DOCKER

# Set writable status directory for sandbox deployment
os.environ["ROCK_SERVICE_STATUS_DIR"] = "/tmp/rock_status"


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_sdk_close_session(sandbox_instance: Sandbox):
    session_name = "test-close-session"
    await sandbox_instance.create_session(CreateBashSessionRequest(session=session_name, session_type="bash"))

    obs = await sandbox_instance.arun(cmd="echo 'alive'", session=session_name)
    assert "alive" in obs.output

    resp = await sandbox_instance.close_session(CloseBashSessionRequest(session=session_name, session_type="bash"))
    assert resp is not None

    with pytest.raises(Exception):
        await sandbox_instance.arun(cmd="echo 'should fail'", session=session_name)
