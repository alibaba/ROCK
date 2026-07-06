"""Regression tests for RayOperator.get_remote_status request shape.

PR #985 added ``NonBlankStr sandbox_id`` to ``SandboxCommand`` and
``SandboxReadFileRequest`` in ``rock/admin/proto/request.py``. The rocklet's
``/execute`` and ``/read_file`` endpoints deserialise into those models, so
the request body must carry ``sandbox_id`` -- passing it only in the HTTP
header (the pre-985 behaviour) now triggers 422 Unprocessable Entity from
the rocklet, which bubbles up through ``get_status_v2`` as
``Failed to get status``.
"""

from unittest.mock import AsyncMock, patch

import pytest

from rock.sandbox.utils.rocklet_probe import get_remote_status


@pytest.mark.asyncio
async def test_get_remote_status_passes_sandbox_id_to_execute_body():
    sandbox_id = "sb-abc"
    host_ip = "10.0.0.1"
    with patch("rock.sandbox.utils.rocklet_probe.HttpUtils.post", new=AsyncMock()) as mock_post:
        # exit_code == 2 short-circuits before /read_file is called.
        mock_post.return_value = {"exit_code": 2}
        await get_remote_status(sandbox_id, host_ip)

    assert mock_post.call_count == 1
    kwargs = mock_post.call_args.kwargs
    assert "/execute" in kwargs["url"]
    assert (
        kwargs["data"].get("sandbox_id") == sandbox_id
    ), "rocklet /execute requires sandbox_id in body (NonBlankStr); header-only sandbox_id triggers 422"


@pytest.mark.asyncio
async def test_get_remote_status_passes_sandbox_id_to_read_file_body():
    sandbox_id = "sb-abc"
    host_ip = "10.0.0.1"
    with patch("rock.sandbox.utils.rocklet_probe.HttpUtils.post", new=AsyncMock()) as mock_post:
        mock_post.side_effect = [
            {"exit_code": 0},  # ls succeeded -> proceed to read_file
            {"content": ""},  # empty content path
        ]
        await get_remote_status(sandbox_id, host_ip)

    assert mock_post.call_count == 2
    read_file_kwargs = mock_post.call_args_list[1].kwargs
    assert "/read_file" in read_file_kwargs["url"]
    assert (
        read_file_kwargs["data"].get("sandbox_id") == sandbox_id
    ), "rocklet /read_file requires sandbox_id in body (NonBlankStr); header-only sandbox_id triggers 422"
