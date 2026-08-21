"""SandboxNext provider — talks to the SandboxNext Gateway REST API.

Implements the RemoteProvider Protocol using httpx.AsyncClient.
See docs/proposals/sandbox-next.yaml for the OpenAPI spec.
"""

from __future__ import annotations

from typing import Any

import httpx

from rock.actions.sandbox.response import State
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.config import RemoteOperatorConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.deployments.constants import Port
from rock.logger import init_logger
from rock.sandbox.operator.remote.constants import EXT_BACKEND, EXT_ENDPOINT, EXT_REMOTE_ID, BACKEND_NAME

logger = init_logger(__name__)

# --- SandboxNext SandboxState -> Rock State ---

_DEFAULT_STATE_MAP: dict[str, State] = {
    "creating": State.PENDING,
    "running": State.RUNNING,
    "pausing": State.STOPPED,
    "paused": State.STOPPED,
    "resuming": State.PENDING,
    "failed": State.STOPPED,
}


def _map_state(sn_state: str | None, state_map: dict[str, State] | None = None) -> State:
    table = state_map or _DEFAULT_STATE_MAP
    return table.get(sn_state or "", State.PENDING)


def _parse_mem_to_mb(mem: str) -> int:
    """Convert docker-style memory string (``8g``/``4096m``/``2048``) to MB."""
    s = mem.strip().lower()
    if not s:
        return 0
    if s.endswith("g"):
        return int(float(s[:-1]) * 1024)
    if s.endswith("m"):
        return int(float(s[:-1]))
    return int(float(s))


def _parse_disk_to_mb(disk: str | None) -> int:
    """Convert docker-style disk string (``50G``/``51200M``) to MB."""
    if not disk:
        return 0
    return _parse_mem_to_mb(disk)


class SandboxNextProvider:
    """Provider that talks to the SandboxNext Gateway REST API."""

    def __init__(self, config: RemoteOperatorConfig, *, client: httpx.AsyncClient | None = None):
        self._config = config
        self._state_map = config.state_mapping or _DEFAULT_STATE_MAP
        self._retry_max = config.provider_options.get("retry_max", 3)
        self._retry_backoff = config.provider_options.get("retry_backoff_base", 0.5)

        base_url = f"{config.protocol}://{config.endpoint}"
        headers: dict[str, str] = {}
        if config.api_key:
            headers["X-Api-Key"] = config.api_key
        if config.access_token:
            headers["Authorization"] = f"Bearer {config.access_token}"

        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=config.default_timeout,
        )
        logger.info("Initialized SandboxNextProvider (endpoint=%s, region=%s)", config.endpoint, config.region)

    # --- HTTP helpers ---

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Send an HTTP request with limited retry on 5xx errors."""
        response = await self._client.request(method, path, **kwargs)
        retry_count = 0
        while response.status_code >= 500 and retry_count < self._retry_max:
            retry_count += 1
            import asyncio

            await asyncio.sleep(self._retry_backoff * (2 ** (retry_count - 1)))
            response = await self._client.request(method, path, **kwargs)
        return response

    # --- Lifecycle ---

    async def submit(self, config: DockerDeploymentConfig, user_info: dict) -> SandboxInfo:
        sandbox_id = config.container_name
        user_id = user_info.get("user_id", "default")
        experiment_id = user_info.get("experiment_id", "default")
        namespace = user_info.get("namespace", "default")

        body: dict[str, Any] = {
            "request_id": sandbox_id,
            "region": self._config.region,
            "class": self._config.sandbox_class,
            "resources": {
                "vcpu": int(config.cpus),
                "memory_mb": _parse_mem_to_mb(config.memory),
                "disk_mb": _parse_disk_to_mb(config.disk),
            },
            "metadata": {
                "rock_sandbox_id": sandbox_id or "",
                "user_id": user_id,
                "experiment_id": experiment_id,
                "namespace": namespace,
            },
        }
        if config.env_vars:
            body["env_vars"] = config.env_vars

        response = await self._request("POST", "/v1/sandboxes", json=body)
        response.raise_for_status()
        data = response.json()

        sn_id = data["sandbox_id"]
        sn_state = data.get("state")
        access = data.get("access") or {}
        endpoint_template = access.get("endpoint_template", "")
        agent_token = access.get("agent_token", "")

        logger.info("[%s] sandbox_next submitted, remote_id=%s, state=%s", sandbox_id, sn_id, sn_state)

        info: SandboxInfo = {
            "sandbox_id": sandbox_id,
            "image": config.image,
            "cpus": config.cpus,
            "memory": config.memory,
            "user_id": user_id,
            "experiment_id": experiment_id,
            "namespace": namespace,
            "state": _map_state(sn_state, self._state_map),
            "host_ip": endpoint_template,
            "port_mapping": {
                Port.PROXY: 8000,
                Port.SERVER: 8080,
                Port.SSH: 22,
            },
            "auth_token": agent_token,
            "extended_params": {
                EXT_BACKEND: BACKEND_NAME,
                EXT_REMOTE_ID: sn_id,
                EXT_ENDPOINT: endpoint_template,
            },
        }
        return info

    async def get_status(self, remote_sandbox_id: str) -> SandboxInfo | None:
        response = await self._request("GET", f"/v1/sandboxes/{remote_sandbox_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()

        sn_state = data.get("state")
        access = data.get("access") or {}
        endpoint_template = access.get("endpoint_template", "")
        agent_token = access.get("agent_token", "")

        info: SandboxInfo = {
            "sandbox_id": remote_sandbox_id,
            "state": _map_state(sn_state, self._state_map),
            "host_ip": endpoint_template,
            "port_mapping": {
                Port.PROXY: 8000,
                Port.SERVER: 8080,
                Port.SSH: 22,
            },
            "auth_token": agent_token,
            "extended_params": {
                EXT_BACKEND: BACKEND_NAME,
                EXT_REMOTE_ID: remote_sandbox_id,
                EXT_ENDPOINT: endpoint_template,
            },
        }
        return info

    async def stop(self, remote_sandbox_id: str) -> bool:
        """Pause the sandbox. Falls back to delete if pause is unsupported (501)."""
        response = await self._request("POST", f"/v1/sandboxes/{remote_sandbox_id}/pause")
        if response.status_code == 501:
            logger.info("[%s] pause not supported (501), falling back to delete", remote_sandbox_id)
            return await self.delete(remote_sandbox_id)
        response.raise_for_status()
        return True

    async def delete(self, remote_sandbox_id: str) -> bool:
        response = await self._request("DELETE", f"/v1/sandboxes/{remote_sandbox_id}")
        if response.status_code == 404:
            return True
        response.raise_for_status()
        return True

    # --- Template API ---

    async def create_template(self, spec: Any) -> dict:
        body = self._template_spec_to_new(spec)
        response = await self._request("POST", "/v1/templates", json=body)
        if response.status_code == 409:
            # Idempotent: fetch existing by request_id
            request_id = body.get("request_id", "")
            if request_id:
                get_resp = await self._request("GET", f"/v1/templates/{request_id}")
                if get_resp.status_code == 200:
                    return self._template_to_dict(get_resp.json())
            response.raise_for_status()
        if response.status_code == 501:
            raise NotImplementedError("template_create not supported on this class")
        response.raise_for_status()
        return self._template_to_dict(response.json())

    async def get_template_status(self, template_id: str) -> dict | None:
        response = await self._request("GET", f"/v1/templates/{template_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return self._template_to_dict(response.json())

    async def delete_template(self, template_id: str) -> bool:
        response = await self._request("DELETE", f"/v1/templates/{template_id}")
        if response.status_code == 404:
            return True
        if response.status_code == 501:
            raise NotImplementedError("template_create not supported on this class")
        response.raise_for_status()
        return True

    # --- Template mapping helpers ---

    def _template_spec_to_new(self, spec: Any) -> dict:
        """Convert a Rock TemplateSpec-like object to SandboxNext NewTemplate."""
        # Accept both dict and dataclass/pydantic model
        if hasattr(spec, "model_dump"):
            spec = spec.model_dump()
        elif hasattr(spec, "__dict__"):
            spec = {k: v for k, v in vars(spec).items() if not k.startswith("_")}
        elif not isinstance(spec, dict):
            spec = dict(spec)

        body: dict[str, Any] = {
            "request_id": spec.get("template_id") or spec.get("request_id", ""),
            "region": spec.get("region", self._config.region),
            "class": spec.get("sandbox_class") or spec.get("class") or self._config.sandbox_class,
            "name": spec.get("name", "default"),
        }
        resources = spec.get("resources")
        if resources:
            body["resources"] = resources
        else:
            cpus = spec.get("cpus")
            memory = spec.get("memory")
            disk = spec.get("disk")
            res: dict[str, int] = {}
            if cpus is not None:
                res["vcpu"] = int(cpus)
            if memory is not None:
                res["memory_mb"] = _parse_mem_to_mb(memory)
            if disk is not None:
                res["disk_mb"] = _parse_disk_to_mb(disk)
            if res:
                body["resources"] = res
        if spec.get("image"):
            body["from_image"] = spec["image"]
        if spec.get("env_vars"):
            body["env_vars"] = spec["env_vars"]
        return body

    def _template_to_dict(self, data: dict) -> dict:
        """Convert SandboxNext Template response to Rock template status dict."""
        return {
            "template_id": data.get("template_id", ""),
            "name": data.get("name", ""),
            "status": data.get("status", "pending"),
            "resources": data.get("resources", {}),
            "failure": data.get("failure"),
        }
