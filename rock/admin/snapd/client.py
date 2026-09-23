"""Async HTTP client for ROCK-admin -> rock-snapd orchestration calls."""

from __future__ import annotations

import time
import uuid
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .protocol import (
    SNAPD_PROTOCOL_VERSION,
    CreateBaseImageRequest,
    DeleteSandboxRequest,
    FinalizePublishedSnapshotRequest,
    ForkLocalReadyResult,
    ForkSandboxRequest,
    MutationAcceptedResult,
    ResolveBaseImageRequest,
    ResolvedBaseImageResult,
    SandboxStartedResult,
    SandboxStatusResult,
    SnapdErrorCode,
    SnapdReply,
    SnapshotSandboxRequest,
    SnapshotStatusResult,
    StartSandboxFromSnapshotRequest,
    StopSandboxRequest,
)

ResultT = TypeVar("ResultT", bound=BaseModel)


class SnapdProtocolError(RuntimeError):
    """The peer returned a response that violates the versioned contract."""


class SnapdResultUnknown(RuntimeError):
    """A mutation may have committed; callers must reconcile without resubmitting it."""


class SnapdAPIError(RuntimeError):
    def __init__(self, reply: SnapdReply):
        super().__init__(f"snapd returned {reply.code}: {reply.error_message or 'no detail'}")
        self.reply = reply
        self.code = reply.code
        self.retryable = reply.retryable
        self.request_id = reply.request_id

    def __reduce__(self):
        return type(self), (self.reply,)


class SnapdClient:
    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
    ):
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=base_url)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def healthcheck(self, *, timeout_seconds: float = 2.0) -> bool:
        """Return whether this worker-local snapd is ready to serve requests."""

        try:
            response = await self._client.get(
                "/healthz",
                timeout=httpx.Timeout(timeout_seconds),
            )
        except httpx.HTTPError:
            return False
        return response.status_code == 200 and response.content == b"ok\n"

    def _headers(self, request_id: str, deadline_unix_ms: int) -> dict[str, str]:
        if deadline_unix_ms <= int(time.time() * 1000):
            raise ValueError("deadline must be in the future")
        return {
            "X-ROCK-Snapshot-Protocol-Version": str(SNAPD_PROTOCOL_VERSION),
            "X-Request-ID": request_id,
            "X-Deadline-Unix-Ms": str(deadline_unix_ms),
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        deadline_unix_ms: int,
        body: BaseModel | dict[str, Any] | None = None,
        result_type: type[ResultT] | None = None,
    ) -> ResultT | dict[str, Any] | None:
        def invalid_reply(message: str, cause: Exception | None = None):
            error_type = SnapdProtocolError if method == "GET" else SnapdResultUnknown
            error = error_type(message)
            if cause is None:
                raise error
            raise error from cause

        request_id = uuid.uuid4().hex
        headers = self._headers(request_id, deadline_unix_ms)
        timeout = max((deadline_unix_ms - int(time.time() * 1000)) / 1000, 0.001)
        json_body = body.model_dump(mode="json") if isinstance(body, BaseModel) else body
        try:
            response = await self._send(method, path, headers, json_body, timeout)
        except httpx.TransportError as exc:
            raise SnapdResultUnknown(
                f"snapd result is unknown for request_id={request_id}; reconcile durable state and do not blindly resubmit a mutation"
            ) from exc

        if response.status_code >= 500:
            raise SnapdResultUnknown(
                f"snapd returned HTTP {response.status_code} for request_id={request_id}; result is unknown"
            )
        try:
            reply = SnapdReply.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            invalid_reply(f"invalid snapd reply for request_id={request_id}", exc)
        if reply.request_id != request_id:
            invalid_reply(f"reply request_id mismatch: expected {request_id}, got {reply.request_id}")
        if reply.code != SnapdErrorCode.OK:
            raise SnapdAPIError(reply)
        if not response.is_success:
            invalid_reply(f"snapd returned HTTP {response.status_code} with code=OK")
        if result_type is not None and reply.result is None:
            invalid_reply(f"snapd reply omitted required result for request_id={request_id}")
        if result_type is None:
            return reply.result
        try:
            return result_type.model_validate(reply.result)
        except ValidationError as exc:
            invalid_reply(f"invalid snapd result for request_id={request_id}", exc)

    async def _send(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        json_body: dict[str, Any] | None,
        timeout: float,
    ) -> httpx.Response:
        return await self._client.request(
            method,
            path,
            headers=headers,
            json=json_body,
            timeout=httpx.Timeout(timeout),
        )

    async def create_base_image(
        self, request: CreateBaseImageRequest, *, deadline_unix_ms: int
    ) -> MutationAcceptedResult:
        result = await self._request(
            "POST",
            "/v1/snapd/base-images",
            deadline_unix_ms=deadline_unix_ms,
            body=request,
            result_type=MutationAcceptedResult,
        )
        assert isinstance(result, MutationAcceptedResult)
        return result

    async def resolve_base_image(
        self, request: ResolveBaseImageRequest, *, deadline_unix_ms: int
    ) -> ResolvedBaseImageResult:
        result = await self._request(
            "POST",
            "/v1/snapd/base-images/resolve",
            deadline_unix_ms=deadline_unix_ms,
            body=request,
            result_type=ResolvedBaseImageResult,
        )
        assert isinstance(result, ResolvedBaseImageResult)
        return result

    async def snapshot_sandbox(
        self,
        source_sandbox_id: str,
        request: SnapshotSandboxRequest,
        *,
        deadline_unix_ms: int,
    ) -> dict[str, Any] | None:
        return await self._request(
            "POST",
            f"/v1/snapd/sandboxes/{source_sandbox_id}/snapshot",
            deadline_unix_ms=deadline_unix_ms,
            body=request,
        )

    async def fork_sandbox(
        self,
        source_sandbox_id: str,
        request: ForkSandboxRequest,
        *,
        deadline_unix_ms: int,
    ) -> ForkLocalReadyResult:
        result = await self._request(
            "POST",
            f"/v1/snapd/sandboxes/{source_sandbox_id}/fork",
            deadline_unix_ms=deadline_unix_ms,
            body=request,
            result_type=ForkLocalReadyResult,
        )
        assert isinstance(result, ForkLocalReadyResult)
        return result

    async def start_sandbox_from_snapshot_async(
        self,
        snapshot_id: str,
        request: StartSandboxFromSnapshotRequest,
        *,
        deadline_unix_ms: int,
    ) -> MutationAcceptedResult:
        result = await self._request(
            "POST",
            f"/v1/snapd/snapshots/{snapshot_id}/start-async",
            deadline_unix_ms=deadline_unix_ms,
            body=request,
            result_type=MutationAcceptedResult,
        )
        assert isinstance(result, MutationAcceptedResult)
        return result

    async def start_sandbox_from_snapshot(
        self,
        snapshot_id: str,
        request: StartSandboxFromSnapshotRequest,
        *,
        deadline_unix_ms: int,
    ) -> SandboxStartedResult:
        result = await self._request(
            "POST",
            f"/v1/snapd/snapshots/{snapshot_id}/start",
            deadline_unix_ms=deadline_unix_ms,
            body=request,
            result_type=SandboxStartedResult,
        )
        assert isinstance(result, SandboxStartedResult)
        return result

    async def get_snapshot_status(
        self,
        snapshot_id: str,
        *,
        deadline_unix_ms: int,
    ) -> SnapshotStatusResult:
        result = await self._request(
            "GET",
            f"/v1/snapd/snapshots/{snapshot_id}/status",
            deadline_unix_ms=deadline_unix_ms,
            result_type=SnapshotStatusResult,
        )
        assert isinstance(result, SnapshotStatusResult)
        return result

    async def get_sandbox_status(
        self, sandbox_id: str, *, deadline_unix_ms: int
    ) -> SandboxStatusResult:
        result = await self._request(
            "GET",
            f"/v1/snapd/sandboxes/{sandbox_id}/status",
            deadline_unix_ms=deadline_unix_ms,
            result_type=SandboxStatusResult,
        )
        assert isinstance(result, SandboxStatusResult)
        return result

    async def stop_sandbox(
        self,
        sandbox_id: str,
        request: StopSandboxRequest,
        *,
        deadline_unix_ms: int,
    ) -> dict[str, Any] | None:
        return await self._request(
            "POST",
            f"/v1/snapd/sandboxes/{sandbox_id}/stop",
            deadline_unix_ms=deadline_unix_ms,
            body=request,
        )

    async def delete_sandbox(
        self,
        sandbox_id: str,
        request: DeleteSandboxRequest,
        *,
        deadline_unix_ms: int,
    ) -> dict[str, Any] | None:
        return await self._request(
            "DELETE",
            f"/v1/snapd/sandboxes/{sandbox_id}",
            deadline_unix_ms=deadline_unix_ms,
            body=request,
        )

    async def finalize_published_snapshot(
        self,
        snapshot_id: str,
        request: FinalizePublishedSnapshotRequest,
        *,
        deadline_unix_ms: int,
    ) -> dict[str, Any] | None:
        return await self._request(
            "POST",
            f"/v1/snapd/snapshots/{snapshot_id}/finalize",
            deadline_unix_ms=deadline_unix_ms,
            body=request,
        )
