import uuid

from pydantic import BaseModel, Field


class AgentConfig(BaseModel):
    """Abstract configuration for all agents."""

    agent_type: str = "default"
    """Type identifier for the agent."""

    agent_name: str = uuid.uuid4().hex
    """Unique name for the agent instance."""

    version: str = "default"
    """Version identifier for the agent."""


class AgentBashCommand(BaseModel):
    """Configuration for a command execution with timeout control."""

    command: str = Field(...)
    """The command to execute."""

    timeout_seconds: int = Field(default=300)
    """Timeout in seconds for command execution."""
