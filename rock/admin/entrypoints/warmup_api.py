from fastapi import APIRouter

from rock.actions import RockResponse
from rock.admin.proto.request import WarmupRequest
from rock.common.exception import handle_exceptions
from rock.sandbox.service.warmup_service import WarmupService

warmup_router = APIRouter()
warmup_service: WarmupService


def set_warmup_service(service: WarmupService):
    global warmup_service
    warmup_service = service


@warmup_router.post("/warmup/tasks")
@handle_exceptions(error_message="start warmup failed")
async def start(request: WarmupRequest) -> RockResponse[None]:
    await warmup_service.warmup(request)
    return RockResponse(message="warmup task started", result=None)
