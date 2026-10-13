from .._models import BaseModel

__all__ = ["InitTaskEnvResponse"]


class InitTaskEnvResponse(BaseModel):
    """Response from initializing a task environment."""

    env_id: str
    """Environment identifier"""