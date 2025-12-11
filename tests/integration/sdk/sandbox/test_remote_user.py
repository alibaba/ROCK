import logging

import pytest

from rock.actions.sandbox.request import CreateBashSessionRequest
from rock.actions.sandbox.response import Observation
from rock.sdk.sandbox.client import Sandbox

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_remote_user_create(sandbox_instance: Sandbox):
    assert await sandbox_instance.remote_user.create_remote_user("rock")
    assert await sandbox_instance.remote_user.is_user_exist("rock")


@pytest.mark.asyncio
async def test_create_session_with_remote_user(sandbox_instance: Sandbox):
    assert await sandbox_instance.remote_user.create_remote_user("rock")
    await sandbox_instance.create_session(CreateBashSessionRequest(remote_user="rock", session="bash2"))
    observation: Observation = await sandbox_instance.arun(cmd="whoami", session="bash2")
    assert observation.output.strip() == "rock"
