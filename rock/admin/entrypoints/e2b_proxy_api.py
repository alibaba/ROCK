from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from rock.admin.proto.response import E2BSandboxDetail
from rock.logger import init_logger
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService
from rock.sdk.common.exceptions import BadRequestRockError, E2BSandboxNotFoundError

logger = init_logger(__name__)


class E2BProxyAPIRoute(APIRoute):
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
            except E2BSandboxNotFoundError as error:
                return _error_response(404, str(error))
            except Exception:
                logger.exception("E2B get sandbox failed")
                return _error_response(500, "Internal server error")

        return handler


e2b_proxy_router = APIRouter(route_class=E2BProxyAPIRoute)
e2b_proxy_service: SandboxProxyService


def set_e2b_proxy_service(service: SandboxProxyService) -> None:
    global e2b_proxy_service
    e2b_proxy_service = service


def _error_response(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": status_code, "message": message})


@e2b_proxy_router.get(
    "/sandboxes/{sandboxID}",
    response_model=E2BSandboxDetail,
    response_model_by_alias=True,
)
async def get_sandbox(sandboxID: str) -> E2BSandboxDetail:
    try:
        sandbox_status = await e2b_proxy_service.get_status(sandboxID, include_all_states=True)
    except BadRequestRockError as error:
        if str(error) == f"Sandbox {sandboxID} not found":
            raise E2BSandboxNotFoundError(str(error)) from None
        raise
    return E2BSandboxDetail.from_sandbox_status(sandboxID, sandbox_status)
