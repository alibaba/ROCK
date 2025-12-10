import logging
from abc import ABC, abstractmethod

from rock.actions.sandbox.base import AbstractSandbox
from rock.actions.sandbox.request import Command
from rock.actions.sandbox.response import CommandResponse

logger = logging.getLogger(__name__)


class RemoteUser(ABC):
    current_user: str = "root"
    sandbox: AbstractSandbox = None

    def __init__(self, sandbox: AbstractSandbox = None):
        self.sandbox = sandbox

    @abstractmethod
    async def create_remote_user(self, user_name: str):
        pass

    @abstractmethod
    async def is_user_exist(self, user_name: str) -> bool:
        pass


class LinuxRemoteUser(RemoteUser):
    def __init__(self, sandbox: AbstractSandbox = None):
        super().__init__(sandbox)

    async def create_remote_user(self, user_name: str):
        try:
            if await self.is_user_exist(user_name):
                return True

            response: CommandResponse = await self.sandbox.execute(
                Command(command=["useradd", "-m", "-s", "/bin/bash", user_name])
            )
            logger.info(f"user add execute response: {response}")
            # TODO: raise exception
            if response.exit_code != 0:
                return False

            return True
        except Exception as e:
            logger.error("create_remote_user failed", exc_info=e)
            raise e

    async def is_user_exist(self, user_name: str) -> bool:
        try:
            response: CommandResponse = await self.sandbox.execute(Command(command=["id", user_name]))
            if response.exit_code == 0:
                logging.info(f"user {user_name} already exists")
                return True
            else:
                return False
        except Exception as e:
            logger.info(f"is_user_exist exception is {str(e)}")
            raise e
