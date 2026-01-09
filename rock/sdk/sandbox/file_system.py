import logging
from abc import ABC, abstractmethod

from rock.actions.sandbox.base import AbstractSandbox
from rock.actions.sandbox.request import ChmodRequest, ChownRequest, Command
from rock.actions.sandbox.response import ChmodResponse, ChownResponse, CommandResponse

logger = logging.getLogger(__name__)


class FileSystem(ABC):
    sandbox: AbstractSandbox = None

    def __init__(self, sandbox: AbstractSandbox = None):
        self.sandbox = sandbox

    @abstractmethod
    async def chown(self, request: ChownRequest) -> ChownResponse:
        pass

    @abstractmethod
    async def chmod(self, request: ChmodRequest) -> ChmodResponse:
        pass


class LinuxFileSystem(FileSystem):
    def __init__(self, sandbox: AbstractSandbox = None):
        super().__init__(sandbox)

    async def chown(self, request: ChownRequest) -> ChownResponse:
        paths = request.paths
        if paths is None or len(paths) == 0:
            raise Exception("paths is empty")

        command = ["chown"]
        if request.recursive:
            command.append("-R")
        command.extend([f"{request.remote_user}:{request.remote_user}"] + paths)
        logger.info(f"chown command: {command}")

        chown_response: CommandResponse = await self.sandbox.execute(Command(command=command))
        if chown_response.exit_code != 0:
            return ChownResponse(success=False, message=str(chown_response))
        return ChownResponse(success=True, message=str(chown_response))

    async def chmod(self, request: ChmodRequest) -> ChmodResponse:
        paths = request.paths
        if paths is None or len(paths) == 0:
            raise Exception("paths is empty")

        command = ["chmod"]
        if request.recursive:
            command.append("-R")

        command.extend([request.mode] + paths)
        logger.info(f"chmod command: {command}")
        chmod_response: CommandResponse = await self.sandbox.execute(Command(command=command))
        if chmod_response.exit_code != 0:
            return ChmodResponse(success=False, message=str(chmod_response))
        return ChmodResponse(success=True, message=str(chmod_response))
