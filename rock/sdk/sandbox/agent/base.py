from abc import ABC, abstractmethod

from rock.actions.sandbox.base import AbstractSandbox


class Agent(ABC):
    def __init__(self, sandbox: AbstractSandbox):
        self._sandbox = sandbox

    @abstractmethod
    async def init(self):
        pass

    @abstractmethod
    async def run(self, **kwargs):
        pass
