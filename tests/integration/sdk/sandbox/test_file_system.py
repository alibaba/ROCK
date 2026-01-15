import logging

from rock.actions.sandbox.request import ChmodRequest, ChownRequest, Command, CreateBashSessionRequest
from rock.actions.sandbox.response import ChmodResponse, ChownResponse, CommandResponse, Observation
from rock.sdk.sandbox.client import Sandbox

logger = logging.getLogger(__name__)


async def test_dirs_chown(sandbox_instance: Sandbox):
    assert await sandbox_instance.remote_user.create_remote_user("rock")

    rock_session = "rock_session"
    await sandbox_instance.create_session(CreateBashSessionRequest(remote_user="rock", session=rock_session))

    pwd_response: CommandResponse = await sandbox_instance.execute(Command(command=["pwd"]))
    pwd = pwd_response.stdout.strip()
    logger.info(f"pwd: {pwd}")

    response: ChownResponse = await sandbox_instance.fs.chown(
        ChownRequest(recursive=False, remote_user="rock", paths=[pwd])
    )
    assert response.success

    observation: Observation = await sandbox_instance.arun(
        cmd=f'ls -ld {pwd} | awk "{{print \\$3}}"', session=rock_session
    )
    assert observation.exit_code == 0
    assert observation.output == "rock"


async def test_dirs_chmod(sandbox_instance: Sandbox):
    command = ["mkdir", "-p", "/tmp/aa/bb"]
    response: CommandResponse = await sandbox_instance.execute(Command(command=command))
    assert response.exit_code == 0

    response: ChmodResponse = await sandbox_instance.fs.chmod(
        ChmodRequest(paths=["/tmp/aa"], mode="777", recursive=False)
    )
    assert response.success

    observation: Observation = await sandbox_instance.arun(cmd="ls -ld /tmp/aa", session="default")
    assert observation.exit_code == 0
    assert "drwxrwxrwx" in observation.output

    response: ChmodResponse = await sandbox_instance.fs.chmod(
        ChmodRequest(paths=["/tmp/aa/"], mode="755", recursive=True)
    )
    assert response.success
    observation: Observation = await sandbox_instance.arun(cmd="ls -ld /tmp/aa/bb", session="default")
    assert observation.exit_code == 0
    assert "drwxr-xr-x" in observation.output

    assert observation.exit_code == 0
