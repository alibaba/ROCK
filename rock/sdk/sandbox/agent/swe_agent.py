from typing import Literal

from rock.actions.sandbox.base import AbstractSandbox
from rock.sdk.sandbox.agent.base import Agent
from rock.sdk.sandbox.agent.config import AgentConfig


class SweAgentConfig(AgentConfig):
    agent_type: Literal["swe-agent"] = "swe-agent"


class SweAgent(Agent):
    def __init__(self, sandbox: AbstractSandbox, config: SweAgentConfig):
        super().__init__(sandbox)
        self.config = config

    async def init(self):
        # Initialization logic for SWE agent
        pass

    async def run(self, **kwargs):
        # Execution logic for SWE agent
        pass
