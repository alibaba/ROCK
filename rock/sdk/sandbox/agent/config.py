import uuid

from pydantic import BaseModel


class AgentConfig(BaseModel):
    """Abstract configuration for all agents."""

    agent_type: str = "default"
    """Type identifier for the agent."""

    agent_name: str = uuid.uuid4().hex
    """Unique name for the agent instance."""

    version: str = "default"
    """Version identifier for the agent."""
