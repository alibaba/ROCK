import httpx
from fastapi import UploadFile
from starlette.status import HTTP_504_GATEWAY_TIMEOUT

from rock.actions import CommandResponse, ReadFileResponse, UploadResponse, WriteFileResponse
from rock.admin.proto.request import SandboxCommand, SandboxReadFileRequest, SandboxWriteFileRequest
from rock.deployments.constants import Port
from rock.deployments.status import ServiceStatus
from rock.logger import init_logger
from rock.utils import EAGLE_EYE_TRACE_ID, trace_id_ctx_var

logger = init_logger(__name__)


class RockletBackend:
    def __init__(self, rpc_client: httpx.AsyncClient):
        self._rpc_client = rpc_client

    async def execute(self, sandbox_id: str, info: dict, command: SandboxCommand) -> CommandResponse:
        response = await self.request(
            sandbox_id,
            info,
            "execute",
            json_data=command.model_dump(),
            method="POST",
        )
        return CommandResponse(**response)

    async def read_file(self, sandbox_id: str, info: dict, request: SandboxReadFileRequest) -> ReadFileResponse:
        response = await self.request(
            sandbox_id,
            info,
            "read_file",
            json_data=request.model_dump(),
            method="POST",
        )
        return ReadFileResponse(**response)

    async def write_file(self, sandbox_id: str, info: dict, request: SandboxWriteFileRequest) -> WriteFileResponse:
        response = await self.request(
            sandbox_id,
            info,
            "write_file",
            json_data=request.model_dump(),
            method="POST",
        )
        return WriteFileResponse(**response)

    async def upload(self, sandbox_id: str, info: dict, file: UploadFile, target_path: str) -> UploadResponse:
        response = await self.request(
            sandbox_id,
            info,
            "upload",
            data={"target_path": target_path, "unzip": "false"},
            files={"file": (file.filename, file.file, file.content_type)},
            method="POST",
        )
        return UploadResponse(**response)

    async def request(
        self,
        sandbox_id: str,
        info: dict,
        path: str,
        *,
        data: dict | None = None,
        json_data: dict | None = None,
        files: dict | None = None,
        method: str,
    ) -> dict:
        host_ip = info.get("host_ip")
        service_status = ServiceStatus.from_dict(info)
        api_url = f"http://{host_ip}:{service_status.get_mapped_port(Port.PROXY)}"
        full_request_url = f"{api_url}/{path}"
        headers = {"sandbox_id": sandbox_id, EAGLE_EYE_TRACE_ID: trace_id_ctx_var.get()}

        try:
            response = await self._rpc_client.request(
                method=method,
                url=full_request_url,
                headers=headers,
                json=json_data if json_data else None,
                data=data if data else None,
                files=files if files else None,
            )
            if response.status_code == 511:
                return {"exit_code": -1, "failure_reason": response.json()["rockletexception"]["message"]}
            if response.status_code == HTTP_504_GATEWAY_TIMEOUT:
                return {"exit_code": -1, "failure_reason": response.json()["detail"]}
            return response.json()
        except httpx.RequestError as e:
            logger.error("Error forwarding request to %s: %s", full_request_url, e, exc_info=True)
            raise Exception("Service unavailable: Upstream server is not reachable.") from e
