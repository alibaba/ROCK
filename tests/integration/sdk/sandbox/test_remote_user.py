import logging

import pytest

import rock
from rock.actions import CreateBashSessionResponse, Observation
from rock.actions.sandbox.request import CreateBashSessionRequest
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


@pytest.mark.asyncio
async def test_create_session_without_remote_user(sandbox_instance: Sandbox):
    response: CreateBashSessionResponse = await sandbox_instance.create_session(
        CreateBashSessionRequest(remote_user="rock", session="bash2")
    )
    logger.info(f"response: {response}")
    assert response.code == rock.codes.COMMAND_ERROR
