"""Thin httpx-based client wrapping the Infra-style sandbox control plane.

Lives one layer below RemoteOperator so the operator can stay focused on
ROCK-side semantics (state machine, sandbox lifecycle) and delegate all
HTTP / auth / endpoint concerns here.

Authentication: every request carries the ``X-API-Key`` header sourced from
``RemoteConfig.resolved_api_key()`` (env var wins over inlined value).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from rock.config import RemoteConfig

logger = logging.getLogger(__name__)


class RemoteApiError(RuntimeError):
    """Raised for non-2xx responses from the remote sandbox API.

    Carries ``status_code`` so callers can distinguish 404 (treated as
    "sandbox not found" — a normal control-flow signal) from real server
    errors.
    """

    def __init__(self, status_code: int, message: str, body: Any = None) -> None:
        super().__init__(f"remote api {status_code}: {message}")
        self.status_code = status_code
        self.body = body


class RemoteClient:
    """Async client for the Infra-style sandbox API.

    Stateless except for the underlying ``httpx.AsyncClient``. Lifecycle is
    bound to the ``RemoteOperator`` instance, which closes the client on
    shutdown via :meth:`close`.
    """

    def __init__(self, config: RemoteConfig) -> None:
        if not config.api_endpoint:
            raise ValueError("RemoteConfig.api_endpoint is required")
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.api_endpoint.rstrip("/"),
            timeout=config.timeout_seconds,
            verify=config.verify_ssl,
        )

    @property
    def config(self) -> RemoteConfig:
        return self._config

    def _headers(self) -> dict[str, str]:
        api_key = self._config.resolved_api_key()
        if not api_key:
            raise RuntimeError(
                "RemoteConfig has no api_key (neither inline nor env var resolved a value)"
            )
        return {
            "X-API-Key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> dict[str, Any] | None:
        try:
            response = await self._client.request(
                method,
                path,
                headers=self._headers(),
                json=json_body,
            )
        except httpx.HTTPError as exc:
            logger.error("remote api transport error %s %s: %r", method, path, exc)
            raise RemoteApiError(0, f"transport error: {exc}") from exc

        if allow_404 and response.status_code == 404:
            return None

        if 200 <= response.status_code < 300:
            if response.status_code == 204 or not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                return {"raw": response.text}

        # Non-2xx: surface a structured error including the body for log triage.
        body: Any
        try:
            body = response.json()
        except ValueError:
            body = response.text
        message = ""
        if isinstance(body, dict):
            message = str(body.get("message") or body.get("error") or body)
        else:
            message = str(body)
        logger.error(
            "remote api %s %s -> %s: %s",
            method,
            path,
            response.status_code,
            message,
        )
        raise RemoteApiError(response.status_code, message, body)

    # ------------------------------------------------------------------
    # Endpoints (per docs/openapi.yml)
    # ------------------------------------------------------------------

    async def create_sandbox(self, payload: dict[str, Any]) -> dict[str, Any]:
        """``POST /sandboxes`` — returns the ``Sandbox`` body."""
        body = await self._request("POST", "/sandboxes", json_body=payload)
        if not body:
            raise RemoteApiError(0, "create_sandbox returned empty body")
        return body

    async def get_sandbox(self, sandbox_id: str) -> dict[str, Any] | None:
        """``GET /sandboxes/{id}`` — returns ``SandboxDetail`` or ``None`` on 404."""
        return await self._request("GET", f"/sandboxes/{sandbox_id}", allow_404=True)

    async def kill_sandbox(self, sandbox_id: str) -> bool:
        """``DELETE /sandboxes/{id}`` — returns ``False`` if the sandbox was already gone."""
        body = await self._request("DELETE", f"/sandboxes/{sandbox_id}", allow_404=True)
        return body is not None

    async def set_timeout(self, sandbox_id: str, timeout_seconds: int) -> None:
        """``POST /sandboxes/{id}/timeout`` — extend or reset the auto-clear window."""
        await self._request(
            "POST",
            f"/sandboxes/{sandbox_id}/timeout",
            json_body={"timeout": int(timeout_seconds)},
        )

    async def refresh_sandbox(self, sandbox_id: str) -> None:
        """``POST /sandboxes/{id}/refreshes`` — keep-alive ping."""
        await self._request("POST", f"/sandboxes/{sandbox_id}/refreshes")
