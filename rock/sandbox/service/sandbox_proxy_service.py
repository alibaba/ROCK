import asyncio  # noqa: I001
import json
import time
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.datastructures import Headers

import httpx
import oss2
import websockets
from aliyunsdkcore import client
from aliyunsdkcore.request import CommonRequest
from fastapi import Response, UploadFile
from starlette.status import HTTP_504_GATEWAY_TIMEOUT

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
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.admin.core.redis_key import ALIVE_PREFIX, alive_sandbox_key, timeout_sandbox_key
from rock.admin.metrics.decorator import monitor_sandbox_operation
from rock.admin.metrics.monitor import MetricsMonitor
from rock.admin.proto.request import SandboxBashAction as BashAction
from rock.admin.proto.request import SandboxCloseBashSessionRequest as CloseBashSessionRequest
from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.proto.request import SandboxCreateSessionRequest as CreateSessionRequest
from rock.admin.proto.request import SandboxQueryParams
from rock.admin.proto.request import SandboxReadFileRequest as ReadFileRequest
from rock.admin.proto.request import SandboxWriteFileRequest as WriteFileRequest
from rock.admin.proto.response import SandboxListResponse, SandboxListStatusResponse, SandboxStatusResponse
from rock.config import OssConfig, ProxyServiceConfig, RockConfig
from rock.deployments.constants import Port
from rock.deployments.status import ServiceStatus
from rock.logger import init_logger
from rock.sdk.common.exceptions import BadRequestRockError
from rock.utils import EAGLE_EYE_TRACE_ID, trace_id_ctx_var
from rock.utils.providers import RedisProvider

logger = init_logger(__name__)


class SandboxProxyService:
    _redis_provider: RedisProvider = None
    _httpx_client = None

    def __init__(self, rock_config: RockConfig, redis_provider: RedisProvider | None = None):
        self._rock_config = rock_config
        self._redis_provider = redis_provider
        self.metrics_monitor = MetricsMonitor.create(
            export_interval_millis=20_000,
            metrics_endpoint=rock_config.runtime.metrics_endpoint,
        )
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

        self._batch_get_status_max_count = rock_config.proxy_service.batch_get_status_max_count

    @monitor_sandbox_operation()
    async def create_session(self, request: CreateSessionRequest) -> CreateBashSessionResponse:
        sandbox_id = request.sandbox_id
        await self._update_expire_time(sandbox_id)
        sandbox_status_dicts = await self.get_service_status(sandbox_id)
        response = await self._send_request(
            sandbox_id, sandbox_status_dicts[0], "create_session", None, request.model_dump(), None, "POST"
        )
        return CreateBashSessionResponse(**response)

    @monitor_sandbox_operation()
    async def run_in_session(self, action: BashAction) -> BashObservation:
        sandbox_id = action.sandbox_id
        await self._update_expire_time(sandbox_id)
        sandbox_status_dicts = await self.get_service_status(sandbox_id)
        response = await self._send_request(
            sandbox_id, sandbox_status_dicts[0], "run_in_session", None, action.model_dump(), None, "POST"
        )
        return BashObservation(**response)

    @monitor_sandbox_operation()
    async def close_session(self, request: CloseBashSessionRequest) -> CloseBashSessionResponse:
        sandbox_id = request.sandbox_id
        await self._update_expire_time(sandbox_id)
        sandbox_status_dicts = await self.get_service_status(sandbox_id)
        response = await self._send_request(
            sandbox_id, sandbox_status_dicts[0], "close_session", None, request.model_dump(), None, "POST"
        )
        return CloseBashSessionResponse(**response)

    @monitor_sandbox_operation()
    async def is_alive(self, sandbox_id: str) -> IsAliveResponse:
        sandbox_status_dicts = await self.get_service_status(sandbox_id)
        return await self._is_alive(sandbox_id, sandbox_status_dicts[0])

    async def _is_alive(self, sandbox_id: str, sandbox_status_dict: dict) -> IsAliveResponse:
        try:
            response = await self._send_request(sandbox_id, sandbox_status_dict, "is_alive", None, None, None, "GET")
            return IsAliveResponse(**response)
        except Exception as e:
            logger.error(f"sandbox not alive for {str(e)}")
            return IsAliveResponse(is_alive=False)

    @monitor_sandbox_operation()
    async def read_file(self, request: ReadFileRequest) -> ReadFileResponse:
        sandbox_id = request.sandbox_id
        await self._update_expire_time(sandbox_id)
        sandbox_status_dicts = await self.get_service_status(sandbox_id)
        response = await self._send_request(
            sandbox_id, sandbox_status_dicts[0], "read_file", None, request.model_dump(), None, "POST"
        )
        return ReadFileResponse(**response)

    @monitor_sandbox_operation()
    async def write_file(self, request: WriteFileRequest) -> WriteFileResponse:
        sandbox_id = request.sandbox_id
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
        sandbox_id = command.sandbox_id
        await self._update_expire_time(sandbox_id)
        sandbox_status_dicts = await self.get_service_status(sandbox_id)
        response = await self._send_request(
            sandbox_id, sandbox_status_dicts[0], "execute", None, command.model_dump(), None, "POST"
        )
        return CommandResponse(**response)

    @monitor_sandbox_operation()
    async def batch_get_sandbox_status_from_redis(self, sandbox_ids: list[str]) -> list[SandboxStatusResponse]:
        if self._redis_provider is None:
            logger.info("batch_get_sandbox_status_from_redis, redis provider is None, return empty")
            return []
        if sandbox_ids is None:
            raise BadRequestRockError(message="sandbox_ids is None")
        if len(sandbox_ids) > self._batch_get_status_max_count:
            raise BadRequestRockError(
                message=f"sandbox_ids count too large, max count is {self._batch_get_status_max_count}"
            )
        logger.info(f"batch_get_sandbox_status_from_redis, sandbox_ids count is {len(sandbox_ids)}")
        results = []
        alive_keys = [alive_sandbox_key(sandbox_id) for sandbox_id in sandbox_ids]
        sandbox_infos: list[SandboxInfo] = await self._redis_provider.json_mget(alive_keys, "$")
        for sandbox_info in sandbox_infos:
            if sandbox_info:
                results.append(SandboxStatusResponse.from_sandbox_info(sandbox_info))
        logger.info(f"batch_get_sandbox_status_from_redis succ, result count is {len(results)}")
        return results

    @monitor_sandbox_operation()
    async def list_sandboxes(self, query_params: SandboxQueryParams) -> SandboxListResponse:
        if self._redis_provider is None:
            logger.warning("Redis provider is not available, list_sandboxes returning empty result")
            return SandboxListResponse()
        page = int(query_params.pop("page", "1"))
        page_size = int(query_params.pop("page_size", "500"))
        if page < 1 or page_size < 1:
            raise BadRequestRockError(f"page parameter invalid, page is {page}, page_size is {page_size}")
        if page_size > self._batch_get_status_max_count:
            raise BadRequestRockError(f"page_size exceeds maximum {self._batch_get_status_max_count}")
        logger.info(f"list sandboxes with filters: {query_params}, page: {page}, page_size: {page_size}")
        try:
            all_sandbox_data = await self.list_all_sandboxes_by_query_params(query_params)
            total = len(all_sandbox_data)
            start_index = (page - 1) * page_size
            end_index = start_index + page_size
            page_data = all_sandbox_data[start_index:end_index]
            has_more = end_index < total
            logger.info(f"Returning page {page} with {len(page_data)} items, total: {total}, has_more: {has_more}")
            return SandboxListResponse(items=page_data, total=total, has_more=has_more)
        except Exception as e:
            logger.error(f"Error filtering sandboxes: {e}", exc_info=True)
            raise

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
            if response.status_code == HTTP_504_GATEWAY_TIMEOUT:
                return {"exit_code": -1, "failure_reason": response.json()["detail"]}
            return response.json()
        except httpx.RequestError as e:
            # Handle network-level errors, such as DNS resolution failure, connection timeout, etc.
            logger.error(f"Error forwarding request to full_request_url: {str(e)}", exc_info=True)
            raise Exception("Service unavailable: Upstream server is not reachable.")

    def _headers(self, sandbox_id: str) -> dict[str, str]:
        headers = {"sandbox_id": sandbox_id, EAGLE_EYE_TRACE_ID: trace_id_ctx_var.get()}
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

    async def list_all_sandboxes_by_query_params(self, query_params: SandboxQueryParams):
        all_keys = []
        async for key in self._redis_provider.client.scan_iter(match=f"{ALIVE_PREFIX}*", count=1000):  # type: ignore
            all_keys.append(key)
        if not all_keys:
            return []

        all_sandbox_data = []
        batch_size = self._batch_get_status_max_count
        for i in range(0, len(all_keys), batch_size):
            batch_keys = all_keys[i : i + batch_size]
            sandbox_infos_list = await self._redis_provider.json_mget(batch_keys, "$")

            for sandbox_info in sandbox_infos_list:
                if not sandbox_info:
                    continue
                if self._matches_query_params(sandbox_info, query_params):
                    all_sandbox_data.append(SandboxListStatusResponse.from_sandbox_info(sandbox_info))
        return all_sandbox_data

    def _matches_query_params(self, sandbox_info: SandboxInfo, query_params: SandboxQueryParams) -> bool:
        if not query_params:
            return True
        for filter_key, filter_value in query_params.items():
            if filter_key not in sandbox_info or sandbox_info[filter_key] != filter_value:
                return False
        return True

    async def post_proxy(
        self,
        sandbox_id: str,
        target_path: str,
        body: dict | None,
        headers: Headers,
    ) -> JSONResponse | StreamingResponse | Response:
        """HTTP POST proxy that supports both streaming (SSE) and non-streaming responses."""
        await self._update_expire_time(sandbox_id)

        EXCLUDED_HEADERS = {"host", "content-length", "transfer-encoding"}

        def filter_headers(raw_headers: Headers) -> dict:
            return {k: v for k, v in raw_headers.items() if k.lower() not in EXCLUDED_HEADERS}

        status_list = await self.get_service_status(sandbox_id)
        service_status = ServiceStatus.from_dict(status_list[0])

        host_ip = status_list[0].get("host_ip")
        port = service_status.get_mapped_port(Port.SERVER)
        target_url = f"http://{host_ip}:{port}/{target_path}"

        request_headers = filter_headers(headers)
        payload = body or {}

        client = httpx.AsyncClient(timeout=httpx.Timeout(None))

        try:
            resp = await client.send(
                client.build_request(
                    method="POST",
                    url=target_url,
                    json=payload,
                    headers=request_headers,
                    timeout=120,
                ),
                stream=True,
            )
        except Exception:
            await client.aclose()
            raise

        content_type = resp.headers.get("content-type", "")
        is_sse = "text/event-stream" in content_type
        response_headers = filter_headers(resp.headers)

        if is_sse:

            async def event_stream():
                """Forward upstream bytes to downstream as soon as they arrive."""
                try:
                    if resp.status_code >= 400:
                        yield await resp.aread()
                        return

                    async for chunk in resp.aiter_bytes():
                        if chunk:
                            yield chunk
                finally:
                    await resp.aclose()
                    await client.aclose()

            return StreamingResponse(
                event_stream(),
                status_code=resp.status_code,
                media_type="text/event-stream",
                headers=response_headers,
            )

        try:
            raw_content = await resp.aread()

            if "application/json" in content_type:
                return JSONResponse(
                    status_code=resp.status_code,
                    content=resp.json(),
                    headers=response_headers,
                )

            return Response(
                status_code=resp.status_code,
                content=raw_content,
                media_type=content_type or "application/octet-stream",
                headers=response_headers,
            )
        finally:
            await resp.aclose()
            await client.aclose()
