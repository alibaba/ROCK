from pydantic import BaseModel

from rock.sdk.sandbox.model_service.base import ModelServiceConfig


class AgentConfig(BaseModel):
    agent_type: str

    version: str


class DefaultAgentConfig(AgentConfig):
    """Base configuration for all sandbox agents.

    Provides common configuration fields shared across different agent types.
    """

    # Session management
    agent_session: str = "default-agent-session"

    # Startup/shutdown commands
    pre_startup_bash_cmd_list: list[str] = []
    post_startup_bash_cmd_list: list[str] = []

    # Environment variables for the session
    session_envs: dict[str, str] = {}

    # Optional ModelService configuration
    model_service_config: ModelServiceConfig | None = None
