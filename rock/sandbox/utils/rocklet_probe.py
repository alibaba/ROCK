"""Shared rocklet probe helpers — used by both RayOperator and SandboxProxyService.

These functions talk to rocklet over HTTP to fetch ServiceStatus (phases + port_mapping)
and check liveness. They have no dependency on Ray or Kubernetes operators.

HTTP client parameterization:
- Pass http_client=None to use HttpUtils (per-request, for RayOperator)
- Pass http_client=httpx.AsyncClient to reuse connection pool (for Proxy)
"""

import httpx

from rock import env_vars
from rock.actions import IsAliveResponse
from rock.deployments.constants import Port
from rock.deployments.status import PersistedServiceStatus, ServiceStatus
from rock.logger import init_logger
from rock.utils import EAGLE_EYE_TRACE_ID, trace_id_ctx_var
from rock.utils.http import HttpUtils

logger = init_logger(__name__)


def _parse_response(resp: dict | httpx.Response) -> dict:
    """Normalize response: httpx.Response → dict via .json(), dict passthrough."""
    if isinstance(resp, dict):
        return resp
    return resp.json()


async def get_remote_status(
    sandbox_id: str, host_ip: str, http_client: httpx.AsyncClient | None = None
) -> ServiceStatus:
    service_status_path = PersistedServiceStatus.gen_service_status_path(sandbox_id)
    worker_rocklet_port = env_vars.ROCK_WORKER_ROCKLET_PORT if env_vars.ROCK_WORKER_ROCKLET_PORT else Port.PROXY
    execute_url = f"http://{host_ip}:{worker_rocklet_port}/execute"
    read_file_url = f"http://{host_ip}:{worker_rocklet_port}/read_file"
    headers = {"sandbox_id": sandbox_id, EAGLE_EYE_TRACE_ID: trace_id_ctx_var.get()}

    try:
        # Check if service status file exists
        if http_client is None:
            find_file_rsp_raw = await HttpUtils.post(
                url=execute_url,
                headers=headers,
                data={"command": ["ls", service_status_path], "sandbox_id": sandbox_id},
                read_timeout=60,
            )
        else:
            find_file_rsp_raw = await http_client.post(
                execute_url,
                headers=headers,
                json={"command": ["ls", service_status_path], "sandbox_id": sandbox_id},
            )

        find_file_rsp = _parse_response(find_file_rsp_raw)

        # When the file does not exist, exit_code = 2
        if find_file_rsp.get("exit_code") and find_file_rsp.get("exit_code") == 2:
            return ServiceStatus()

        # Read the service status file
        if http_client is None:
            response_raw = await HttpUtils.post(
                url=read_file_url,
                headers=headers,
                data={"path": service_status_path, "sandbox_id": sandbox_id},
                read_timeout=60,
            )
        else:
            response_raw = await http_client.post(
                read_file_url,
                headers=headers,
                json={"path": service_status_path, "sandbox_id": sandbox_id},
            )

        response = _parse_response(response_raw)

        if response.get("content"):
            return ServiceStatus.from_content(response.get("content"))
        logger.warning(f"{service_status_path} exists, but content is empty")
    except Exception as e:
        logger.warning(f"Failed to get remote status for {sandbox_id}: {e}")

    return ServiceStatus()


async def check_alive_status(
    sandbox_id: str, host_ip: str, remote_status: ServiceStatus, http_client: httpx.AsyncClient | None = None
) -> bool:
    """Check if sandbox is alive"""
    try:
        url = f"http://{host_ip}:{remote_status.get_mapped_port(Port.PROXY)}/is_alive"
        headers = {
            "sandbox_id": sandbox_id,
            EAGLE_EYE_TRACE_ID: trace_id_ctx_var.get(),
        }

        if http_client is None:
            alive_resp_raw = await HttpUtils.get(url=url, headers=headers)
        else:
            alive_resp_raw = await http_client.get(url, headers=headers)

        alive_resp = _parse_response(alive_resp_raw)
        return IsAliveResponse(**alive_resp).is_alive
    except Exception:
        return False
