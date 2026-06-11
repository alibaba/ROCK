"""Async httpx client for the remote sandbox control-plane API."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from rock.config import RemoteConfig
from rock.utils import REQUEST_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)


class RemoteApiError(RuntimeError):
    """Non-2xx response from the remote sandbox API."""

    def __init__(self, status_code: int, message: str, body: Any = None) -> None:
        super().__init__(f"remote api {status_code}: {message}")
        self.status_code = status_code
        self.body = body


class RemoteClient:
    """Async client for the remote sandbox API."""

    def __init__(self, config: RemoteConfig) -> None:
        if not config.api_endpoint:
            raise ValueError("RemoteConfig.api_endpoint is required")
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.api_endpoint.rstrip("/"),
            timeout=REQUEST_TIMEOUT_SECONDS,
            verify=False,
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
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        try:
            headers = self._headers()
            if extra_headers:
                headers.update(extra_headers)
            response = await self._client.request(
                method,
                path,
                headers=headers,
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

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /sandboxes (async build mode)"""
        body = await self._request(
            "POST", "/sandboxes", json_body=payload,
            extra_headers={"Async-Build": "true"},
        )
        if not body:
            raise RemoteApiError(0, "create returned empty body")
        return body

    async def get(self, sandbox_id: str) -> dict[str, Any] | None:
        """GET /sandboxes/{id} — returns None on 404."""
        return await self._request("GET", f"/sandboxes/{sandbox_id}", allow_404=True)

    async def stop(self, sandbox_id: str) -> bool:
        """POST /sandboxes/{id}/pause — returns False on 404."""
        body = await self._request("POST", f"/sandboxes/{sandbox_id}/pause", allow_404=True)
        return body is not None

    async def restart(self, sandbox_id: str, timeout_seconds: int) -> dict[str, Any] | None:
        """POST /sandboxes/{id}/connect — returns None on 404."""
        return await self._request(
            "POST",
            f"/sandboxes/{sandbox_id}/connect",
            json_body={"timeout": int(timeout_seconds)},
            allow_404=True,
        )

    async def delete(self, sandbox_id: str) -> bool:
        """DELETE /sandboxes/{id} — returns False on 404."""
        body = await self._request("DELETE", f"/sandboxes/{sandbox_id}", allow_404=True)
        return body is not None

    async def keep_alive(self, sandbox_id: str) -> None:
        """POST /sandboxes/{id}/refreshes"""
        await self._request("POST", f"/sandboxes/{sandbox_id}/refreshes")

    async def set_timeout(self, sandbox_id: str, timeout_seconds: int) -> None:
        """POST /sandboxes/{id}/timeout"""
        await self._request(
            "POST",
            f"/sandboxes/{sandbox_id}/timeout",
            json_body={"timeout": int(timeout_seconds)},
        )
