from urllib.parse import unquote, urlsplit, urlunsplit

from fastapi import UploadFile
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response, StreamingResponse

from rock.actions import (
    BashObservation,
    CloseBashSessionResponse,
    CommandResponse,
    CreateBashSessionResponse,
    IsAliveResponse,
    ReadFileResponse,
    UploadResponse,
    WriteFileResponse,
)
from rock.actions.sandbox.response import State
from rock.admin.metrics.decorator import monitor_sandbox_operation
from rock.admin.proto.request import SandboxBashAction as BashAction
from rock.admin.proto.request import SandboxCloseBashSessionRequest as CloseBashSessionRequest
from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.proto.request import SandboxCreateSessionRequest as CreateSessionRequest
from rock.admin.proto.request import SandboxReadFileRequest as ReadFileRequest
from rock.admin.proto.request import SandboxWriteFileRequest as WriteFileRequest
from rock.admin.proto.response import SandboxStatusResponse
from rock.common.port_validation import validate_port_forward_port
from rock.config import RockConfig
from rock.deployments.constants import Port
from rock.sandbox import __version__ as gateway_version
from rock.sandbox.operator.opensandbox.client import OpenSandboxClient
from rock.sandbox.sandbox_meta_store import SandboxMetaStore
from rock.sandbox.service.backends.opensandbox import OpenSandboxBackend
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService
from rock.sdk.common.exceptions import BadRequestRockError

OPENSANDBOX_BACKEND = "opensandbox"


class OpenSandboxProxyService(SandboxProxyService):
    """Proxy runtime operations directly to OpenSandbox instead of Rocklet."""

    def __init__(
        self,
        rock_config: RockConfig,
        meta_store: SandboxMetaStore,
        backend: OpenSandboxBackend | None = None,
    ):
        super().__init__(rock_config=rock_config, meta_store=meta_store)
        self._opensandbox_backend = backend or OpenSandboxBackend(OpenSandboxClient(rock_config.opensandbox))
        self._opensandbox_protocol = rock_config.opensandbox.protocol

    async def aclose(self) -> None:
        await self._opensandbox_backend.aclose()

    @staticmethod
    def _validate_backend(info: dict) -> None:
        backend = (info.get("extended_params") or {}).get("backend")
        if backend is None:
            raise BadRequestRockError("OpenSandbox sandbox metadata is missing required backend")
        if backend != OPENSANDBOX_BACKEND:
            raise BadRequestRockError(f"Sandbox backend {backend} conflicts with configured operator opensandbox")

    async def _get_runtime_info(self, sandbox_id: str) -> dict:
        info = await self._meta_store.get(sandbox_id)
        if not info:
            raise BadRequestRockError(f"Sandbox {sandbox_id} not found")
        self._validate_backend(info)
        if info.get("state") != State.RUNNING:
            raise BadRequestRockError(f"Sandbox {sandbox_id} is not running (state={info.get('state')})")
        return info

    async def _unsupported(self, sandbox_id: str, capability: str) -> None:
        await self._update_expire_time(sandbox_id)
        await self._get_runtime_info(sandbox_id)
        raise BadRequestRockError(f"OpenSandbox backend does not support {capability} in this release")

    @monitor_sandbox_operation()
    async def create_session(self, request: CreateSessionRequest) -> CreateBashSessionResponse:
        await self._unsupported(request.sandbox_id, "sessions")

    @monitor_sandbox_operation()
    async def run_in_session(self, action: BashAction) -> BashObservation:
        await self._unsupported(action.sandbox_id, "sessions")

    @monitor_sandbox_operation()
    async def close_session(self, request: CloseBashSessionRequest) -> CloseBashSessionResponse:
        await self._unsupported(request.sandbox_id, "sessions")

    @monitor_sandbox_operation()
    async def is_alive(self, sandbox_id: str) -> IsAliveResponse:
        status = await self.get_status(sandbox_id)
        return IsAliveResponse(is_alive=status.is_alive)

    @monitor_sandbox_operation()
    async def read_file(self, request: ReadFileRequest) -> ReadFileResponse:
        await self._update_expire_time(request.sandbox_id)
        info = await self._get_runtime_info(request.sandbox_id)
        return await self._opensandbox_backend.read_file(request.sandbox_id, info, request)

    @monitor_sandbox_operation()
    async def write_file(self, request: WriteFileRequest) -> WriteFileResponse:
        await self._update_expire_time(request.sandbox_id)
        info = await self._get_runtime_info(request.sandbox_id)
        return await self._opensandbox_backend.write_file(request.sandbox_id, info, request)

    @monitor_sandbox_operation()
    async def upload(self, file: UploadFile, target_path: str, sandbox_id: str) -> UploadResponse:
        await self._update_expire_time(sandbox_id)
        info = await self._get_runtime_info(sandbox_id)
        return await self._opensandbox_backend.upload(sandbox_id, info, file, target_path)

    @monitor_sandbox_operation()
    async def execute(self, command: Command) -> CommandResponse:
        await self._update_expire_time(command.sandbox_id)
        info = await self._get_runtime_info(command.sandbox_id)
        return await self._opensandbox_backend.execute(command.sandbox_id, info, command)

    def _service_url(self, endpoint: str, target_path: str | None, query_string: str = "", *, websocket=False) -> str:
        decoded_path = target_path or ""
        while True:
            next_path = unquote(decoded_path)
            if next_path == decoded_path:
                break
            decoded_path = next_path
        if any(segment in {".", ".."} for segment in decoded_path.replace("\\", "/").split("/")):
            raise BadRequestRockError("invalid proxy path: relative path segments are not allowed")

        if "://" not in endpoint:
            endpoint = f"{self._opensandbox_protocol}://{endpoint}"
        parsed = urlsplit(endpoint)
        scheme = parsed.scheme
        if websocket:
            scheme = {"http": "ws", "https": "wss"}.get(scheme, scheme)
        path = f"{parsed.path.rstrip('/')}/{(target_path or '').lstrip('/')}"
        query = "&".join(part for part in (parsed.query, query_string) if part)
        return urlunsplit((scheme, parsed.netloc, path, query, parsed.fragment))

    async def http_proxy(
        self,
        sandbox_id: str,
        target_path: str,
        body: bytes | None,
        headers: Headers,
        method: str = "POST",
        port: int | None = None,
        proxy_prefix: str | None = None,
        query_string: str = "",
    ) -> JSONResponse | StreamingResponse | Response:
        await self._update_expire_time(sandbox_id)
        info = await self._get_runtime_info(sandbox_id)
        endpoint = await self._opensandbox_backend.get_endpoint(
            sandbox_id,
            info,
            Port.SERVER if port is None else port,
        )
        return await SandboxProxyService._http_proxy_to_target(
            self,
            self._service_url(endpoint.endpoint, target_path, query_string),
            body,
            headers,
            method=method,
            proxy_prefix=proxy_prefix,
            endpoint_headers=endpoint.headers,
        )

    async def websocket_proxy(
        self,
        client_websocket,
        sandbox_id: str,
        target_path: str | None = None,
        port: int | None = None,
        forward_ws_headers: bool = True,
    ):
        await self._update_expire_time(sandbox_id)
        info = await self._get_runtime_info(sandbox_id)
        endpoint = await self._opensandbox_backend.get_endpoint(
            sandbox_id,
            info,
            Port.SERVER if port is None else port,
        )
        return await SandboxProxyService._websocket_proxy_to_target(
            self,
            client_websocket,
            self._service_url(endpoint.endpoint, target_path, websocket=True),
            endpoint_headers=endpoint.headers,
            forward_ws_headers=forward_ws_headers,
        )

    async def websocket_to_tcp_proxy(
        self,
        client_websocket,
        sandbox_id: str,
        port: int,
        tcp_connect_timeout: float = 10.0,
        idle_timeout: float = 300.0,
    ) -> None:
        del client_websocket, tcp_connect_timeout, idle_timeout
        is_valid, error_msg = validate_port_forward_port(port)
        if not is_valid:
            raise ValueError(error_msg)
        await self._unsupported(sandbox_id, "portforward")

    @monitor_sandbox_operation()
    async def get_status(self, sandbox_id: str, include_all_states: bool = False) -> SandboxStatusResponse:
        sandbox_info = await self._meta_store.get(sandbox_id, check_db=True)
        if sandbox_info is None:
            raise BadRequestRockError(f"Sandbox {sandbox_id} not found")
        self._validate_backend(sandbox_info)

        state = sandbox_info.get("state")
        if not include_all_states and state not in (State.PENDING, State.RUNNING):
            raise BadRequestRockError(f"Sandbox {sandbox_id} not found")

        is_alive = False
        operator_sandbox_info = None
        if state in (State.RUNNING, State.PENDING):
            remote_state = await self._opensandbox_backend.get_state(sandbox_info)
            is_alive = remote_state == State.RUNNING
            operator_sandbox_info = dict(sandbox_info)
            operator_sandbox_info["state"] = remote_state
            if is_alive:
                await self._update_expire_time(sandbox_id)

        if state == State.PENDING and is_alive:
            from rock.sandbox.sandbox_statemachine import SandboxStateMachine

            sm = await SandboxStateMachine.from_state_value(state, sandbox_info=sandbox_info)
            await sm.send(
                "alive",
                sandbox_id=sandbox_id,
                meta_store=self._meta_store,
                sandbox_info=operator_sandbox_info,
            )
            sandbox_info = sm.sandbox_info

        info = operator_sandbox_info if operator_sandbox_info is not None else sandbox_info
        return SandboxStatusResponse(
            sandbox_id=sandbox_id,
            status=info.get("phases"),
            port_mapping=info.get("port_mapping"),
            state=info.get("state"),
            host_name=info.get("host_name"),
            host_ip=info.get("host_ip"),
            is_alive=is_alive,
            image=info.get("image"),
            swe_rex_version=None,
            gateway_version=gateway_version,
            user_id=info.get("user_id"),
            experiment_id=info.get("experiment_id"),
            namespace=info.get("namespace"),
            cpus=info.get("cpus"),
            memory=info.get("memory"),
            num_gpus=info.get("num_gpus"),
            accelerator_type=info.get("accelerator_type"),
            disk=info.get("disk"),
            disk_limit_rootfs=info.get("disk"),
            start_time=info.get("start_time"),
            stop_time=info.get("stop_time"),
            create_time=info.get("create_time"),
            state_history=sandbox_info.get("state_history", []),
        )
