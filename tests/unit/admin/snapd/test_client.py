import json
import time

import httpx
import pytest

from rock.admin.snapd.client import (
    SnapdAPIError,
    SnapdClient,
    SnapdProtocolError,
    SnapdResultUnknown,
)
from rock.admin.snapd.protocol import (
    ChildLaunchPlan,
    FinalizePublishedSnapshotRequest,
    ForkSandboxRequest,
    ResolveBaseImageRequest,
    SnapshotSandboxRequest,
    StartSandboxFromSnapshotRequest,
    StopSandboxRequest,
)


def _deadline() -> int:
    return int(time.time() * 1000) + 10_000


@pytest.mark.asyncio
async def test_async_start_accepts_202_without_guest_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/snapd/snapshots/snapshot-a/start-async"
        assert json.loads(request.content)["sandbox_id"] == "sandbox-a"
        return httpx.Response(
            202,
            json={
                "request_id": request.headers["X-Request-ID"],
                "code": "OK",
                "retryable": False,
                "result": {"identity": "sandbox-a", "state": "STARTING"},
            },
        )

    async with httpx.AsyncClient(base_url="http://snapd.test", transport=httpx.MockTransport(handler)) as http_client:
        client = SnapdClient("http://snapd.test", client=http_client)
        result = await client.start_sandbox_from_snapshot_async(
            "snapshot-a",
            StartSandboxFromSnapshotRequest(
                sandbox_id="sandbox-a",
                snapshot_spec={},
                spec_checksum="sha256:" + "a" * 64,
                resource_spec={},
            ),
            deadline_unix_ms=_deadline(),
        )
    assert result.identity == "sandbox-a" and result.state == "STARTING"


def _runtime_observation() -> dict:
    return {
        "guest_endpoint": {
            "transport": "vsock-tcp-forward",
            "host_port": 32123,
            "guest_port": 22555,
            "vsock_cid": 1001,
        },
        "vmm_pid": 4242,
        "rocklet_artifact": {
            "identity": "sha256:" + "d" * 64,
            "version": "1.11.0-test",
            "sha256": "sha256:" + "d" * 64,
            "logical_bytes": 67108864,
        },
    }


@pytest.mark.asyncio
async def test_http_requests_get_fresh_uuid_tracing_ids():
    ids = []
    def handler(request):
        identity = request.headers["X-Request-ID"]
        ids.append(identity)
        return httpx.Response(200, json={"request_id": identity, "code": "OK", "result": {"snapshot_id": "快照", "state": "CREATING", "progress": 0}})
    async with httpx.AsyncClient(base_url="http://snapd.test", transport=httpx.MockTransport(handler)) as http_client:
        client = SnapdClient("http://snapd.test", client=http_client)
        await client.get_snapshot_status("快照", deadline_unix_ms=_deadline())
        await client.get_snapshot_status("快照", deadline_unix_ms=_deadline())
    assert len(set(ids)) == 2
    assert all(len(value) == 32 and value.isascii() for value in ids)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "content", "expected"),
    [(200, b"ok\n", True), (200, b"adoption_pending\n", False), (503, b"adoption_pending\n", False)],
)
async def test_healthcheck_requires_the_ready_snapd_response(status_code, content, expected) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(status_code, content=content))
    async with httpx.AsyncClient(base_url="http://snapd.test", transport=transport) as http_client:
        client = SnapdClient("http://snapd.test", client=http_client)

        assert await client.healthcheck() is expected


@pytest.mark.asyncio
async def test_healthcheck_treats_transport_failure_as_unavailable() -> None:
    def fail(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("not listening")

    async with httpx.AsyncClient(base_url="http://snapd.test", transport=httpx.MockTransport(fail)) as http_client:
        client = SnapdClient("http://snapd.test", client=http_client)

        assert await client.healthcheck() is False


@pytest.mark.asyncio
async def test_finalize_uses_internal_route_and_never_calls_retired_promote() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "request_id": request.headers["X-Request-ID"],
                "code": "OK",
                "retryable": False,
                "result": {"identity": "snapshot-1", "state": "REMOTE_READY", "released_pins": 1},
            },
        )

    async with httpx.AsyncClient(base_url="http://snapd.test", transport=httpx.MockTransport(handler)) as http_client:
        client = SnapdClient("http://snapd.test", client=http_client)
        await client.finalize_published_snapshot(
            "snapshot-1",
            FinalizePublishedSnapshotRequest(
                spec_checksum="sha256:" + "a" * 64,
            ),
            deadline_unix_ms=_deadline(),
        )

    assert seen_paths == ["/v1/snapd/snapshots/snapshot-1/finalize"]


@pytest.mark.asyncio
async def test_base_image_resolve_uses_internal_digest_route() -> None:
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "request_id": request.headers["X-Request-ID"],
                "code": "OK",
                "retryable": False,
                "result": {
                    "oci_ref": "registry.example/image:tag",
                    "oci_digest": "sha256:" + "a" * 64,
                    "oci_platform": "linux/amd64",
                    "base_image_builder_version": "test-builder-v1",
                    "base_image_build_key": "sha256:" + "b" * 64,
                },
            },
        )

    async with httpx.AsyncClient(base_url="http://snapd.test", transport=httpx.MockTransport(handler)) as http_client:
        client = SnapdClient("http://snapd.test", client=http_client)
        resolved = await client.resolve_base_image(
            ResolveBaseImageRequest(oci_ref="registry.example/image:tag"),
            deadline_unix_ms=_deadline(),
        )

    assert resolved.base_image_build_key == "sha256:" + "b" * 64
    assert seen == [
        (
            "/v1/snapd/base-images/resolve",
            {"oci_ref": "registry.example/image:tag", "platform": "linux/amd64"},
        )
    ]


@pytest.mark.asyncio
async def test_client_calls_the_versioned_rust_wire_contract_without_authorization():
    state = "RUNNING"

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal state
        assert "Authorization" not in request.headers
        request_id = request.headers["X-Request-ID"]
        assert request.headers["X-ROCK-Snapshot-Protocol-Version"] == "1"
        if request.url.path == "/v1/snapd/sandboxes/closed-loop/status":

            result = {"sandbox_id": "closed-loop",  "state": state}
            if state == "RUNNING":
                result.update(_runtime_observation())
        elif request.url.path == "/v1/snapd/sandboxes/closed-loop/stop":
            assert request.method == "POST"

            state = "STOPPED"
            result = {"identity": "closed-loop", "state": state, "result_code": "STOPPED"}
        else:  # pragma: no cover - a route drift must fail with its exact path
            raise AssertionError(f"unexpected Rust snapd route: {request.method} {request.url}")
        return httpx.Response(
            200,
            json={"request_id": request_id, "code": "OK", "retryable": False, "result": result},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url="http://snapd.test", transport=transport) as http_client:
        client = SnapdClient("http://snapd.test", client=http_client)
        running = await client.get_sandbox_status(
            "closed-loop",
            deadline_unix_ms=_deadline(),
        )
        stopped = await client.stop_sandbox(
            "closed-loop",
            StopSandboxRequest( reason="closed-loop test", deadline_unix_ms=_deadline()),
            deadline_unix_ms=_deadline(),
        )
        final = await client.get_sandbox_status(
            "closed-loop",
            deadline_unix_ms=_deadline(),
        )

    assert running.state == "RUNNING"

    assert stopped == {"identity": "closed-loop", "state": "STOPPED", "result_code": "STOPPED"}
    assert final.state == "STOPPED"


@pytest.mark.asyncio
async def test_start_requires_the_ready_guest_runtime_observation():
    request = StartSandboxFromSnapshotRequest(
        sandbox_id="guest-1",
        snapshot_spec={"backend_payload": {"value": "opaque"}},
        spec_checksum="sha256:" + "e" * 64,
    )

    async def handler(http_request: httpx.Request) -> httpx.Response:
        assert http_request.url.path == "/v1/snapd/snapshots/snap-1/start"
        return httpx.Response(
            200,
            json={
                "request_id": http_request.headers["X-Request-ID"],
                "code": "OK",
                "retryable": False,
                "result": {"identity": "guest-1", "state": "RUNNING", **_runtime_observation()},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url="http://snapd.test", transport=transport) as http_client:
        client = SnapdClient("http://snapd.test", client=http_client)
        result = await client.start_sandbox_from_snapshot(
            "snap-1",
            request,
            deadline_unix_ms=_deadline(),
        )

    assert result.identity == "guest-1"
    assert result.guest_endpoint.host_port == 32123
    assert result.vmm_pid == 4242


@pytest.mark.asyncio
async def test_fork_uses_slash_action_route():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/snapd/sandboxes/source-1/fork"
        assert json.loads(request.content)["snapshot_id"] == "snap-1"
        return httpx.Response(
            200,
            json={
                "request_id": request.headers["X-Request-ID"],
                "code": "OK",
                "result": {"snapshot_id": "snap-1", "state": "LOCAL_READY", "children": []},
            },
        )

    async with httpx.AsyncClient(base_url="http://snapd.test", transport=httpx.MockTransport(handler)) as http_client:
        client = SnapdClient("http://snapd.test", client=http_client)
        result = await client.fork_sandbox(
            "source-1",
            ForkSandboxRequest(snapshot_id="snap-1", children=[ChildLaunchPlan(sandbox_id="child-1")]),
            deadline_unix_ms=_deadline(),
        )
    assert result.snapshot_id == "snap-1"


def test_start_p2p_source_hint_requires_an_ip_literal():
    with pytest.raises(ValueError, match="IP literal"):
        StartSandboxFromSnapshotRequest(
            sandbox_id="guest-1",
            snapshot_spec={"backend_payload": {"value": "opaque"}},
            spec_checksum="sha256:" + "e" * 64,
            p2p_source_worker_ip="snapd.internal",
        )
    request = StartSandboxFromSnapshotRequest(
        sandbox_id="guest-1",
        snapshot_spec={"backend_payload": {"value": "opaque"}},
        spec_checksum="sha256:" + "e" * 64,
        p2p_source_worker_ip="2001:db8::7",
    )
    assert request.p2p_source_worker_ip == "2001:db8::7"


@pytest.mark.asyncio
async def test_start_running_without_guest_endpoint_is_result_unknown():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "request_id": request.headers["X-Request-ID"],
                "code": "OK",
                "retryable": False,
                "result": {"identity": "guest-1", "state": "RUNNING"},
            },
        )
    )
    async with httpx.AsyncClient(base_url="http://snapd.test", transport=transport) as http_client:
        client = SnapdClient("http://snapd.test", client=http_client)
        with pytest.raises(SnapdResultUnknown, match="invalid snapd result"):
            await client.start_sandbox_from_snapshot(
                "snap-1",
                StartSandboxFromSnapshotRequest(
                    sandbox_id="guest-1",
                    snapshot_spec={"backend_payload": {"value": "opaque"}},
                    spec_checksum="sha256:" + "e" * 64,
                ),
                deadline_unix_ms=_deadline(),
            )


@pytest.mark.asyncio
async def test_snapshot_sends_versioned_envelope_and_returns_result():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/snapd/sandboxes/source-1/snapshot"
        assert request.headers["X-ROCK-Snapshot-Protocol-Version"] == "1"
        assert len(request.headers["X-Request-ID"]) == 32
        assert request.headers["X-Deadline-Unix-Ms"]
        assert request.read()
        return httpx.Response(
            200,
            json={
                "request_id": request.headers["X-Request-ID"],
                "code": "OK",
                "retryable": False,
                "result": {"snapshot_id": "snap-1", "state": "LOCAL_READY"},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url="http://snapd.test", transport=transport) as http_client:
        client = SnapdClient("http://snapd.test", client=http_client)
        result = await client.snapshot_sandbox(
            "source-1",
            SnapshotSandboxRequest(
                snapshot_id="snap-1",

            ),
            deadline_unix_ms=_deadline(),
        )

    assert result == {"snapshot_id": "snap-1", "state": "LOCAL_READY"}


@pytest.mark.asyncio
async def test_typed_error_preserves_retryability():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            409,
            json={
                "request_id": request.headers["X-Request-ID"],
                "code": "IDENTITY_CONFLICT",
                "retryable": False,
                "error_message": "stable identity was used with different input",
            },
        )
    )
    async with httpx.AsyncClient(base_url="http://snapd.test", transport=transport) as http_client:
        client = SnapdClient("http://snapd.test", client=http_client)
        with pytest.raises(SnapdAPIError) as raised:
            await client.get_snapshot_status(
                "snap-1",
                deadline_unix_ms=_deadline(),
            )

    assert raised.value.code == "IDENTITY_CONFLICT"
    assert raised.value.retryable is False


@pytest.mark.asyncio
async def test_transport_failure_is_unknown_not_failed():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection lost", request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url="http://snapd.test", transport=transport) as http_client:
        client = SnapdClient("http://snapd.test", client=http_client)
        with pytest.raises(SnapdResultUnknown):
            await client.get_snapshot_status(
                "snap-1",
                deadline_unix_ms=_deadline(),
            )


@pytest.mark.asyncio
async def test_reply_request_id_must_match():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"request_id": "other", "code": "OK", "retryable": False, "result": {}},
        )
    )
    async with httpx.AsyncClient(base_url="http://snapd.test", transport=transport) as http_client:
        client = SnapdClient("http://snapd.test", client=http_client)
        with pytest.raises(SnapdProtocolError, match="request_id mismatch"):
            await client.get_snapshot_status(
                "snap-1",
                deadline_unix_ms=_deadline(),
            )


@pytest.mark.asyncio
async def test_typed_status_requires_result():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"request_id": request.headers["X-Request-ID"], "code": "OK", "retryable": False, "result": None},
        )
    )
    async with httpx.AsyncClient(base_url="http://snapd.test", transport=transport) as http_client:
        client = SnapdClient("http://snapd.test", client=http_client)
        with pytest.raises(SnapdProtocolError, match="omitted required result"):
            await client.get_snapshot_status(
                "snap-1",
                deadline_unix_ms=_deadline(),
            )


@pytest.mark.asyncio
async def test_mutation_with_unverifiable_reply_is_result_unknown():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"not-json"))
    async with httpx.AsyncClient(base_url="http://snapd.test", transport=transport) as http_client:
        client = SnapdClient("http://snapd.test", client=http_client)
        with pytest.raises(SnapdResultUnknown, match="invalid snapd reply"):
            await client.snapshot_sandbox(
                "source-1",
                SnapshotSandboxRequest(
                    snapshot_id="snap-1",

                ),
                deadline_unix_ms=_deadline(),
            )


@pytest.mark.asyncio
async def test_mutation_response_protocol_disconnect_is_result_unknown():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("peer closed after committing", request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url="http://snapd.test", transport=transport) as http_client:
        client = SnapdClient("http://snapd.test", client=http_client)
        with pytest.raises(SnapdResultUnknown, match="result is unknown"):
            await client.snapshot_sandbox(
                "source-1",
                SnapshotSandboxRequest(
                    snapshot_id="snap-1",

                ),
                deadline_unix_ms=_deadline(),
            )
