from pydantic import BaseModel, Field

from rock import env_vars
from rock.sdk.sandbox.model_service.base import ModelServiceConfig


class AgentConfig(BaseModel):
    agent_type: str
    version: str


class AgentBashCommand(BaseModel):
    """Configuration for a command execution with timeout control."""

    command: str = Field(..., description="The command to execute")
    timeout_seconds: int = Field(default=300, description="Timeout in seconds for command execution")


class BaseAgentConfig(AgentConfig):
    """Base configuration for all sandbox agents.

    Provides common configuration fields shared across different agent types.
    """

    # Unified runtime identifiers (moved from run() args into config)
    project_path: str = "./"
    instance_id: str = "default-instance-id"

    # Session management
    agent_session: str = "default-agent-session"

    # Startup/shutdown commands
    pre_init_bash_cmd_list: list[AgentBashCommand] = [
        AgentBashCommand(**agent_bash_cmd) for agent_bash_cmd in env_vars.ROCK_AGENT_PRE_INIT_BASH_CMD_LIST
    ]
    post_init_bash_cmd_list: list[AgentBashCommand] = Field(default_factory=list)

    # Environment variables for the session
    session_envs: dict[str, str] = {}

    # Optional ModelService configuration
    model_service_config: ModelServiceConfig | None = None
