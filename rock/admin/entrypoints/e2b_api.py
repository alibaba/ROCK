import math
from typing import Any

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from rock.common.validation import NonBlankStr
from rock.deployments.config import DockerDeploymentConfig
from rock.logger import init_logger
from rock.sandbox.sandbox_manager import SandboxManager
from rock.sdk.common.exceptions import BadRequestRockError

logger = init_logger(__name__)


class E2BCreateSandboxRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    template_id: NonBlankStr = Field(alias="templateID")
    timeout: int = Field(gt=0, strict=True)
    metadata: dict[str, str]
    secure: bool | None = None
    allow_internet_access: bool | None = None
    env_vars: dict[str, str] = Field(default_factory=dict, alias="envVars")
    auto_pause: bool | None = Field(default=None, alias="autoPause")
    auto_resume: dict[str, Any] | None = Field(default=None, alias="autoResume")


class E2BCreateSandboxResponse(BaseModel):
    sandbox_id: str = Field(alias="sandboxID")
    envd_version: str = Field(alias="envdVersion")
    client_id: str = Field(alias="clientID")
    template_id: str = Field(alias="templateID")


class E2BAPIRoute(APIRoute):
    def get_route_handler(self):
        route_handler = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await route_handler(request)
            except RequestValidationError as error:
                message = "; ".join(
                    f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in error.errors()
                )
                return _error_response(400, message)
            except BadRequestRockError as error:
                logger.warning("E2B create sandbox rejected: %s", error)
                return _error_response(400, str(error))
            except Exception:
                logger.exception("E2B create sandbox failed")
                return _error_response(500, "Internal server error")

        return handler


e2b_router = APIRouter(route_class=E2BAPIRoute)
e2b_sandbox_manager: SandboxManager


def set_e2b_sandbox_manager(service: SandboxManager) -> None:
    global e2b_sandbox_manager
    e2b_sandbox_manager = service


def _error_response(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": status_code, "message": message})


@e2b_router.post(
    "/sandboxes",
    status_code=201,
    response_model=E2BCreateSandboxResponse,
    response_model_by_alias=True,
)
async def create_sandbox(
    request: E2BCreateSandboxRequest,
) -> E2BCreateSandboxResponse:
    # ROCK stores lifecycle TTLs in whole minutes. Round up so an E2B timeout
    # never expires a sandbox earlier than the caller requested.
    config = DockerDeploymentConfig(
        image=request.template_id,
        auto_clear_time_minutes=math.ceil(request.timeout / 60),
        metadata=request.metadata,
        env_vars=request.env_vars,
    )
    result = await e2b_sandbox_manager.start(config)
    return E2BCreateSandboxResponse(
        sandboxID=result.sandbox_id,
        envdVersion="0.1.0",
        clientID="rock",
        templateID=request.template_id,
    )
