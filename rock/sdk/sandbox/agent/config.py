from pydantic import BaseModel


class AgentConfig(BaseModel):
    agent_type: str
    version: str
