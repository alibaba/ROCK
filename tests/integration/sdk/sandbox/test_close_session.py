import pytest

from rock.actions import CloseBashSessionRequest, CreateBashSessionRequest
from rock.sdk.sandbox.client import Sandbox
from tests.integration.conftest import SKIP_IF_NO_DOCKER


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
