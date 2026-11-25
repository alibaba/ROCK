import json
from unittest.mock import AsyncMock, Mock

import pytest
from starlette.requests import Request
from starlette.responses import Response

from rock.admin import main


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_access_log_preserves_existing_request_and_response_fields(monkeypatch, method):
    logger = Mock()
    monkeypatch.setattr(main, "init_logger", lambda name: logger)
    body = {"sandbox_id": "sandbox-1"}

    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": method,
            "scheme": "http",
            "path": "/apis/envs/sandbox/v1/get_status",
            "query_string": b"sandbox_id=sandbox-1",
            "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")]
            if method == "POST" else [(b"host", b"testserver")],
        },
        receive=receive,
    )
    response = Response("ok")
    call_next = AsyncMock(return_value=response)

    assert await main.log_requests_and_responses(request, call_next) is response
    call_next.assert_awaited_once_with(request)
    assert json.loads(logger.info.call_args_list[0].args[0]) == {
        "access_type": "request",
        "method": method,
        "url": str(request.url),
        "headers": dict(request.headers),
        "request_content": body,
    }
    logged_response = json.loads(logger.info.call_args_list[1].args[0])
    assert set(logged_response) == {"access_type", "status_code", "process_time"}
    assert logged_response["access_type"] == "response"
    assert logged_response["status_code"] == 200
