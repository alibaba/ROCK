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
async def test_sdk_close_session(admin_remote_server):
    # Use low resource config to avoid Ray scheduling issues on local machine
    config_params = {"image": "python:3.11", "memory": "512m", "cpus": 0.5}

    # We can't easily pass params to sandbox_instance fixture if we want to customize it heavily,
    # so we'll create our own sandbox here using the server provided by the fixture.
    from rock.sdk.sandbox.config import SandboxConfig

    config = SandboxConfig(
        image=config_params["image"],
        memory=config_params["memory"],
        cpus=config_params["cpus"],
        base_url=f"{admin_remote_server.endpoint}:{admin_remote_server.port}",
    )

    sandbox = Sandbox(config)
    try:
        # 1. Start
        await sandbox.start()

        # 2. Create session
        session_name = "test-close-session"
        await sandbox.create_session(CreateBashSessionRequest(session=session_name, session_type="bash"))

        # 3. Verify alive (use arun instead of deprecated run_in_session)
        obs = await sandbox.arun(cmd="echo 'alive'", session=session_name)
        assert "alive" in obs.output

        # 4. Close session
        resp = await sandbox.close_session(CloseBashSessionRequest(session=session_name, session_type="bash"))
        assert resp is not None

        # 5. Verify closed (expecting error)
        with pytest.raises(Exception):
            await sandbox.arun(cmd="echo 'should fail'", session=session_name)

    finally:
        if sandbox.sandbox_id:
            await sandbox.stop()
