from typing import Literal

from rock.actions.sandbox.base import AbstractSandbox
from rock.sdk.sandbox.agent.base import Agent
from rock.sdk.sandbox.agent.config import AgentConfig


class IFlowCliConfig(AgentConfig):
    agent_type: Literal["iflow-cli"] = "iflow-cli"
    install_url: str


class IFlowCli(Agent):
    def __init__(self, sandbox: AbstractSandbox, config: IFlowCliConfig):
        super().__init__(sandbox)
        self.config = config

    async def init(self):
        # Initialization logic for IFlow CLI agent
        pass

    async def run(self, **kwargs):
        # Execution logic for IFlow CLI agent
        pass
