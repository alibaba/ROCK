from unittest.mock import AsyncMock, patch

import pytest

from rock.actions.sandbox.response import SandboxStatusResponse
from rock.sdk.common.exceptions import InternalServerRockError
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig

_START_POST_RESPONSE = {
    "status": "Success",
    "result": {"sandbox_id": "test-sandbox-id", "host_name": "host1", "host_ip": "1.2.3.4"},
}


def _create_sandbox() -> Sandbox:
    config = SandboxConfig(image="python:3.11", startup_timeout=300, base_url="http://localhost:8080")
    return Sandbox(config)


def _make_status(is_alive: bool, status: dict | None = None) -> SandboxStatusResponse:
    return SandboxStatusResponse(sandbox_id="test-sandbox-id", status=status or {}, is_alive=is_alive)


async def _run_start_with_polls(alive_after: int) -> list[int]:
    """Run sandbox.start() where get_status returns alive after `alive_after` polls. Returns recorded sleep intervals."""
    sandbox = _create_sandbox()
    not_alive = _make_status(is_alive=False)
    alive = _make_status(is_alive=True)
    call_count = 0

    async def mock_get_status():
        nonlocal call_count
        call_count += 1
        return alive if call_count >= alive_after else not_alive

    sleep_intervals = []

    async def mock_sleep(seconds):
        sleep_intervals.append(seconds)

    with (
        patch.object(sandbox, "get_status", side_effect=mock_get_status),
        patch.object(sandbox, "_parse_error_message_from_status", new_callable=AsyncMock, return_value=None),
        patch("rock.sdk.sandbox.client.asyncio.sleep", side_effect=mock_sleep),
        patch("rock.utils.http.HttpUtils.post", new_callable=AsyncMock, return_value=_START_POST_RESPONSE),
    ):
        await sandbox.start()

    return sleep_intervals


# --- _calculate_poll_interval tests ---


def test_interval_returns_base_when_backoff_disabled():
    for poll_count in range(1, 20):
        assert Sandbox._calculate_poll_interval(poll_count, enable_backoff=False) == 3


def test_interval_returns_base_before_threshold():
    for poll_count in range(1, 5):
        assert Sandbox._calculate_poll_interval(poll_count, enable_backoff=True) == 3


def test_interval_backoff_starts_at_threshold():
    assert Sandbox._calculate_poll_interval(5, enable_backoff=True) == 5


def test_interval_increases_gradually():
    expected = {5: 5, 6: 7, 7: 9, 8: 11, 9: 13}
    for poll_count, expected_interval in expected.items():
        assert Sandbox._calculate_poll_interval(poll_count, enable_backoff=True) == expected_interval


def test_interval_caps_at_max():
    for poll_count in range(10, 50):
        assert Sandbox._calculate_poll_interval(poll_count, enable_backoff=True) <= 15


def test_interval_custom_parameters():
    result = Sandbox._calculate_poll_interval(
        poll_count=8,
        enable_backoff=True,
        base_interval=5,
        max_interval=20,
        backoff_threshold=3,
        backoff_step=3,
    )
    assert result == 20


def test_interval_exact_sequence():
    expected = [3, 3, 3, 3, 5, 7, 9, 11, 13, 15, 15, 15]
    actual = [Sandbox._calculate_poll_interval(i, enable_backoff=True) for i in range(1, 13)]
    assert actual == expected


# --- start() integration tests ---


@pytest.mark.asyncio
async def test_start_succeeds_on_first_poll():
    sandbox = _create_sandbox()
    alive = _make_status(is_alive=True)

    with (
        patch.object(sandbox, "get_status", new_callable=AsyncMock, return_value=alive),
        patch("rock.utils.http.HttpUtils.post", new_callable=AsyncMock, return_value=_START_POST_RESPONSE),
    ):
        await sandbox.start()

    assert sandbox.sandbox_id == "test-sandbox-id"


@pytest.mark.asyncio
async def test_start_intervals_before_threshold():
    sleep_intervals = await _run_start_with_polls(alive_after=4)
    assert sleep_intervals == [3, 3, 3]


@pytest.mark.asyncio
async def test_start_intervals_with_backoff():
    sleep_intervals = await _run_start_with_polls(alive_after=9)
    assert sleep_intervals == [3, 3, 3, 3, 5, 7, 9, 11]


@pytest.mark.asyncio
async def test_start_intervals_cap_at_max():
    sleep_intervals = await _run_start_with_polls(alive_after=15)
    assert all(interval <= 15 for interval in sleep_intervals)
    assert 15 in sleep_intervals


@pytest.mark.asyncio
async def test_start_intervals_full_sequence():
    sleep_intervals = await _run_start_with_polls(alive_after=13)
    assert sleep_intervals == [3, 3, 3, 3, 5, 7, 9, 11, 13, 15, 15, 15]


@pytest.mark.asyncio
async def test_start_raises_on_error_status():
    sandbox = _create_sandbox()
    failed_status = {"build": {"status": "failed", "message": "image pull failed"}}
    not_alive_with_error = _make_status(is_alive=False, status=failed_status)

    with (
        patch.object(sandbox, "get_status", new_callable=AsyncMock, return_value=not_alive_with_error),
        patch("rock.utils.http.HttpUtils.post", new_callable=AsyncMock, return_value=_START_POST_RESPONSE),
    ):
        with pytest.raises(InternalServerRockError, match="image pull failed"):
            await sandbox.start()
