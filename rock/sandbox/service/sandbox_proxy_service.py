import asyncio
import json
import time

import httpx
import oss2
import websockets
from aliyunsdkcore import client
from aliyunsdkcore.request import CommonRequest
from fastapi import UploadFile

from rock import env_vars
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
from rock.admin.core.redis_key import alive_sandbox_key, timeout_sandbox_key
from rock.admin.metrics.decorator import monitor_sandbox_operation
from rock.admin.metrics.monitor import MetricsMonitor
from rock.config import OssConfig, ProxyServiceConfig, RockConfig
from rock.deployments.constants import Port
from rock.deployments.status import ServiceStatus
from rock.logger import init_logger
from rock.rocklet.proto.request import BashInterruptAction
from rock.rocklet.proto.request import InternalBashAction as BashAction
from rock.rocklet.proto.request import InternalCloseBashSessionRequest as CloseBashSessionRequest
from rock.rocklet.proto.request import InternalCommand as Command
from rock.rocklet.proto.request import InternalCreateSessionRequest as CreateSessionRequest
from rock.rocklet.proto.request import InternalReadFileRequest as ReadFileRequest
from rock.rocklet.proto.request import InternalWriteFileRequest as WriteFileRequest
from rock.utils.providers import RedisProvider

logger = init_logger(__name__)


class SandboxProxyService:
    _redis_provider: RedisProvider = None
    _httpx_client = None

    def __init__(self, rock_config: RockConfig, redis_provider: RedisProvider | None = None):
        self._redis_provider = redis_provider
        self.metrics_monitor = MetricsMonitor.create()
        self.oss_config: OssConfig = rock_config.oss
        self.proxy_config: ProxyServiceConfig = rock_config.proxy_service
        logger.info(f"proxy config: {self.proxy_config}")
        # Initialize httpx client with configuration
        self._httpx_client = httpx.AsyncClient(
            timeout=self.proxy_config.timeout,
            limits=httpx.Limits(
                max_connections=self.proxy_config.max_connections,
                max_keepalive_connections=self.proxy_config.max_keepalive_connections,
            ),
        )

        self.sts_client = client.AcsClient(
            self.oss_config.access_key_id,
            self.oss_config.access_key_secret,
            env_vars.ROCK_OSS_BUCKET_REGION,
        )

    @monitor_sandbox_operation()
    async def create_session(self, request: CreateSessionRequest) -> CreateBashSessionResponse:
        sandbox_id = request.container_name
        await self._update_expire_time(sandbox_id)
        sandbox_status_dicts = await self.get_service_status(sandbox_id)
        response = await self._send_request(
            sandbox_id, sandbox_status_dicts[0], "create_session", None, request.model_dump(), None, "POST"
        )
        return CreateBashSessionResponse(**response)

    @monitor_sandbox_operation()
    async def run_in_session(self, action: BashAction | BashInterruptAction) -> BashObservation:
        sandbox_id = action.container_name
        await self._update_expire_time(sandbox_id)
        sandbox_status_dicts = await self.get_service_status(sandbox_id)
        response = await self._send_request(
            sandbox_id, sandbox_status_dicts[0], "run_in_session", None, action.model_dump(), None, "POST"
        )
        return BashObservation(**response)

    @monitor_sandbox_operation()
    async def close_session(self, request: CloseBashSessionRequest) -> CloseBashSessionResponse:
        sandbox_id = request.container_name
        await self._update_expire_time(sandbox_id)
        sandbox_status_dicts = await self.get_service_status(sandbox_id)
        response = await self._send_request(
            sandbox_id, sandbox_status_dicts[0], "close_session", None, request.model_dump(), None, "POST"
        )
        return CloseBashSessionResponse(**response)

    @monitor_sandbox_operation()
    async def is_alive(self, sandbox_id: str) -> IsAliveResponse:
        sandbox_status_dicts = await self.get_service_status(sandbox_id)
        response = await self._send_request(sandbox_id, sandbox_status_dicts[0], "is_alive", None, None, None, "GET")
        return IsAliveResponse(**response)

    @monitor_sandbox_operation()
    async def read_file(self, request: ReadFileRequest) -> ReadFileResponse:
        sandbox_id = request.container_name
        await self._update_expire_time(sandbox_id)
        sandbox_status_dicts = await self.get_service_status(sandbox_id)
        response = await self._send_request(
            sandbox_id, sandbox_status_dicts[0], "read_file", None, request.model_dump(), None, "POST"
        )
        return ReadFileResponse(**response)

    @monitor_sandbox_operation()
    async def write_file(self, request: WriteFileRequest) -> WriteFileResponse:
        sandbox_id = request.container_name
        await self._update_expire_time(sandbox_id)
        sandbox_status_dicts = await self.get_service_status(sandbox_id)
        response = await self._send_request(
            sandbox_id, sandbox_status_dicts[0], "write_file", None, request.model_dump(), None, "POST"
        )
        return WriteFileResponse(**response)

    @monitor_sandbox_operation()
    async def upload(self, file: UploadFile, target_path: str, sandbox_id: str) -> UploadResponse:
        await self._update_expire_time(sandbox_id)
        sandbox_status_dicts = await self.get_service_status(sandbox_id)
        data = {"target_path": target_path, "unzip": "false"}
        files = {"file": (file.filename, file.file, file.content_type)}
        response = await self._send_request(sandbox_id, sandbox_status_dicts[0], "upload", data, None, files, "POST")
        return UploadResponse(**response)

    @monitor_sandbox_operation()
    async def execute(self, command: Command) -> CommandResponse:
        sandbox_id = command.container_name
        await self._update_expire_time(sandbox_id)
        sandbox_status_dicts = await self.get_service_status(sandbox_id)
        response = await self._send_request(
            sandbox_id, sandbox_status_dicts[0], "execute", None, command.model_dump(), None, "POST"
        )
        return CommandResponse(**response)

    async def websocket_proxy(self, client_websocket, sandbox_id: str, target_path: str | None = None):
        target_url = await self.get_sandbox_websocket_url(sandbox_id, target_path)

        try:
            # Connect to target WebSocket service
            async with websockets.connect(target_url, ping_interval=None, ping_timeout=None) as target_websocket:
                # Create bidirectional forwarding tasks
                client_to_target = asyncio.create_task(
                    self._forward_messages(client_websocket, target_websocket, "client->target")
                )
                target_to_client = asyncio.create_task(
                    self._forward_messages(target_websocket, client_websocket, "target->client")
                )

                # Wait for any task to complete (usually connection disconnection)
                done, pending = await asyncio.wait(
                    [client_to_target, target_to_client], return_when=asyncio.FIRST_COMPLETED
                )

                # Cancel unfinished tasks
                for task in pending:
                    task.cancel()

        except Exception as e:
            logger.error(f"WebSocket proxy error: {e}")
            await client_websocket.close(code=1011, reason=f"Proxy error: {str(e)}")

    async def get_service_status(self, sandbox_id: str):
        sandbox_status_dicts = await self._redis_provider.json_get(alive_sandbox_key(sandbox_id), "$")
        if not sandbox_status_dicts or sandbox_status_dicts[0].get("host_ip") is None:
            raise Exception(f"sandbox {sandbox_id} not started")
        return sandbox_status_dicts

    async def _send_request(
        self,
        sandbox_id: str,
        sandbox_status_dict: dict,
        path: str,
        data: dict | None,
        json_data: dict | None,
        files: dict | None,
        method: str,
    ):
        host_ip = sandbox_status_dict.get("host_ip")
        service_status = ServiceStatus.from_dict(sandbox_status_dict)
        api_url = self._api_url(host_ip, service_status)
        headers = self._headers(sandbox_id)
        logger.info(f"headers: {headers}")
        full_request_url = f"{api_url}/{path}"
        logger.info(f"full_request_url: {full_request_url}")
        logger.info(f"data: {data}")
        logger.info(f"json_data: {json_data}")

        # Make request
        try:
            response = await self._httpx_client.request(
                method=method,
                url=full_request_url,
                headers=headers,
                json=json_data if json_data else None,
                data=data if data else None,
                files=files if files else None,
            )
            if response.status_code == 511:
                return {"exit_code": -1, "failure_reason": response.json()["rockletexception"]["message"]}
            return response.json()
        except httpx.RequestError as e:
            # Handle network-level errors, such as DNS resolution failure, connection timeout, etc.
            logger.error(f"Error forwarding request to full_request_url: {str(e)}", exc_info=True)
            raise Exception("Service unavailable: Upstream server is not reachable.")

    def _headers(self, sandbox_id: str) -> dict[str, str]:
        headers = {"sandbox_id": sandbox_id}
        return headers

    def _api_url(self, host_ip: str, service_status: ServiceStatus) -> str:
        port = service_status.get_mapped_port(Port.PROXY)
        return f"http://{host_ip}:{port}"

    def gen_oss_sts_token(self):
        role_arn = self.oss_config.role_arn
        request = CommonRequest(product="Sts", version="2015-04-01", action_name="AssumeRole")
        request.set_method("POST")
        request.set_protocol_type("https")
        request.add_query_param("RoleArn", role_arn)
        request.add_query_param("RoleSessionName", "sessiontest")
        # at least 900s
        request.add_query_param("DurationSeconds", "900")
        request.set_accept_format("JSON")
        try:
            body = self.sts_client.do_action_with_exception(request)
            token = json.loads(oss2.to_unicode(body))
            return token["Credentials"]
        except Exception:
            logger.error("generate oss sts token failed")
            return None

    async def get_sandbox_websocket_url(self, sandbox_id: str, target_path: str | None = None) -> str:
        # if sandbox_id == "iflow-local":   # Local debugging for iflow-cli
        #     return "ws://127.0.0.1:8090/acp"
        # if sandbox_id == "local":   # Local debugging for general ws service
        #     return "ws://127.0.0.1:8090/ws"
        # Get sandbox  address based on sandbox_id
        status_dicts = await self.get_service_status(sandbox_id)
        host_ip = status_dicts[0].get("host_ip")
        service_status = ServiceStatus.from_dict(status_dicts[0])
        port = service_status.get_mapped_port(Port.SERVER)

        if target_path:
            return f"ws://{host_ip}:{port}/{target_path}"
        else:
            return f"ws://{host_ip}:{port}"

    async def _forward_messages(self, source_ws, target_ws, direction: str):
        """Forward messages"""
        try:
            while True:
                message = None

                # Receive message
                if hasattr(source_ws, "receive_text"):
                    # FastAPI WebSocket
                    try:
                        message = await source_ws.receive_text()
                    except Exception:
                        try:
                            message = await source_ws.receive_bytes()
                        except Exception as e:
                            if "websocket.disconnect" in str(e).lower():
                                logger.info(f"FastAPI WebSocket disconnected in {direction}")
                                break
                            raise
                elif hasattr(source_ws, "recv"):
                    # websockets library
                    message = await source_ws.recv()
                else:
                    raise ValueError(f"Unsupported WebSocket type: {type(source_ws)}")

                logger.info(f"Forwarding message {direction}: length={len(str(message))} chars")
                logger.info(f"Forwarding message {direction}: {type(message)}")
                logger.info(f"Forwarding message {direction}: {message}")

                # Send message
                if hasattr(target_ws, "send_text"):
                    # FastAPI WebSocket
                    if isinstance(message, str):
                        await target_ws.send_text(message)
                    elif isinstance(message, bytes):
                        await target_ws.send_bytes(message)
                    else:
                        await target_ws.send_text(str(message))
                elif hasattr(target_ws, "send"):
                    # websockets library
                    await target_ws.send(message)
                else:
                    raise ValueError(f"Unsupported target WebSocket type: {type(target_ws)}")
                await asyncio.sleep(0.1)
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"WebSocket connection closed in {direction}")
        except Exception as e:
            logger.error(f"Error forwarding message {direction}: {e}")

    async def _update_expire_time(self, sandbox_id):
        if self._redis_provider is None:
            return
        sandbox_status_dict = await self._redis_provider.json_get(alive_sandbox_key(sandbox_id), "$")
        if not sandbox_status_dict or len(sandbox_status_dict) == 0:
            logger.info(f"sandbox-{sandbox_id} is not alive, skip update expire time")
            return
        origin_info = await self._redis_provider.json_get(timeout_sandbox_key(sandbox_id), "$")
        if origin_info is None or len(origin_info) == 0:
            logger.info(f"sandbox-{sandbox_id} is not initialized, skip update expire time")
            return
        auto_clear_time: str = origin_info[0].get(env_vars.ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY)
        expire_time: int = int(time.time()) + int(auto_clear_time) * 60
        logger.info(f"sandbox-{sandbox_id} update expire time: {expire_time}")
        new_dict = {
            env_vars.ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY: auto_clear_time,
            env_vars.ROCK_SANDBOX_EXPIRE_TIME_KEY: str(expire_time),
        }
        await self._redis_provider.json_set(timeout_sandbox_key(sandbox_id), "$", new_dict)
