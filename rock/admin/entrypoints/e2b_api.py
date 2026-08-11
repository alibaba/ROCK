import math
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from rock.admin.proto.request import E2BCreateSandboxRequest, StartHeaders
from rock.admin.proto.response import E2BCreateSandboxResponse
from rock.common.constants import AP_SANDBOX_ID_METADATA_KEY, E2B_CLIENT_ID, E2B_ENVD_VERSION
from rock.deployments.config import DockerDeploymentConfig
from rock.logger import init_logger
from rock.sandbox.sandbox_manager import SandboxManager
from rock.sdk.common.exceptions import BadRequestRockError

logger = init_logger(__name__)


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
    headers: Annotated[StartHeaders, Depends()],
) -> E2BCreateSandboxResponse:
    # ROCK stores lifecycle TTLs in whole minutes. Round up so an E2B timeout
    # never expires a sandbox earlier than the caller requested.
    config = DockerDeploymentConfig(
        image=request.template_id,
        auto_clear_time_minutes=math.ceil(request.timeout / 60),
        container_name=request.metadata.get(AP_SANDBOX_ID_METADATA_KEY),
        metadata=request.metadata,
        env_vars=request.env_vars,
    )
    result = await e2b_sandbox_manager.start(
        config,
        user_info=headers.user_info,
        cluster_info=headers.cluster_info,
    )
    return E2BCreateSandboxResponse(
        sandboxID=result.sandbox_id,
        envdVersion=E2B_ENVD_VERSION,
        clientID=E2B_CLIENT_ID,
        templateID=request.template_id,
    )
