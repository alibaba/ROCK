from pydantic import BaseModel, Field

from rock.sdk.sandbox.model_service.base import ModelServiceConfig


class AgentConfig(BaseModel):
    agent_type: str
    version: str


class LongRunningCommand(BaseModel):
    command: str = Field(..., description="The command to execute via nohup")
    timeout_seconds: int = Field(default=300, description="Timeout in seconds for command execution")


class DefaultAgentConfig(AgentConfig):
    """Base configuration for all sandbox agents.

    Provides common configuration fields shared across different agent types.
    """

    # Session management
    agent_session: str = "default-agent-session"

    # Startup/shutdown commands
    pre_startup_bash_cmd_list: list[str] = []

    pre_startup_long_running_cmd_list: list[LongRunningCommand] = []

    post_startup_bash_cmd_list: list[str] = []

    # Environment variables for the session
    session_envs: dict[str, str] = {}

    # Optional ModelService configuration
    model_service_config: ModelServiceConfig | None = None
