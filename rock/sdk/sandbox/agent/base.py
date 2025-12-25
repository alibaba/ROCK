from abc import ABC, abstractmethod

from rock.actions.sandbox.base import AbstractSandbox
from rock.sdk.sandbox.model_service.base import ModelService


class Agent(ABC):
    def __init__(self, sandbox: AbstractSandbox):
        self._sandbox = sandbox
        self.model_service: ModelService | None = None

    @abstractmethod
    async def init(self):
        pass

    @abstractmethod
    async def run(self, **kwargs):
        pass
