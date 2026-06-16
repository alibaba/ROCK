import asyncio

from fastapi import APIRouter

from rock.actions import RockResponse
from rock.common.exception import handle_exceptions
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService

image_router = APIRouter()
image_service: SandboxProxyService


def set_image_service(service: SandboxProxyService):
    global image_service
    image_service = service


@image_router.post("/generate_registry_credentials")
@handle_exceptions(error_message="generate registry credentials failed")
async def generate_registry_credentials():
    """Return ACR registry credentials with temporary token."""
    result = await asyncio.to_thread(image_service.generate_acr_credentials)
    return RockResponse(result=result)
