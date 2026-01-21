from __future__ import annotations  # Postpone annotation evaluation to avoid circular imports.

from abc import ABC, abstractmethod

from rock.actions.sandbox.base import AbstractSandbox
from rock.logger import init_logger
from rock.sdk.sandbox.model_service.base import ModelService
from rock.sdk.sandbox.runtime_env.base import RuntimeEnv

logger = init_logger(__name__)


class Agent(ABC):
    """Abstract agent base class.

    Subclasses must implement:
    - init()
    - run()
    """

    def __init__(self, sandbox: AbstractSandbox):
        self._sandbox = sandbox
        self.model_service: ModelService | None = None
        self.runtime_env: RuntimeEnv | None = None

    @abstractmethod
    async def init(self):
        pass

    @abstractmethod
    async def run(self, **kwargs):
        pass
