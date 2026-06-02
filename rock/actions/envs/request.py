from pydantic import BaseModel

from rock.common.validation import NonBlankStr


class EnvMakeRequest(BaseModel):
    env_id: str
    sandbox_id: NonBlankStr


class EnvResetRequest(BaseModel):
    sandbox_id: NonBlankStr
    seed: int | None = None


class EnvStepRequest(BaseModel):
    sandbox_id: NonBlankStr
    action: str


class EnvCloseRequest(BaseModel):
    sandbox_id: NonBlankStr
