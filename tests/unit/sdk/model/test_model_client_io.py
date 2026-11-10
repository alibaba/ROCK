"""ModelClient log parsing and asynchronous file I/O regression tests."""

import asyncio
import threading
from unittest.mock import patch

import pytest

from rock.sdk.model.client import ModelClient


@pytest.mark.parametrize("kind", ["request", "response"])
async def test_malformed_metadata_has_safe_context(kind):
    client = ModelClient()
    line = f"LLM_{kind.upper()}_STARTsecret-payloadLLM_{kind.upper()}_END{{invalid-secret-json}}"

    with pytest.raises(ValueError, match=f"Invalid {kind} log line") as error:
        await getattr(client, f"parse_{kind}_line")(line)

    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "operation", ["read_last_request_line", "read_last_response_line", "wait_for_first_request", "push_response"]
)
async def test_file_io_runs_outside_event_loop(tmp_path, operation):
    log_file = tmp_path / "model.log"
    log_file.write_text('LLM_REQUEST_START{}LLM_REQUEST_END{"index": 1}\n')
    client = ModelClient(str(log_file))
    loop_thread = threading.get_ident()
    io_threads = []
    original_open = open

    def record_open(*args, **kwargs):
        io_threads.append(threading.get_ident())
        return original_open(*args, **kwargs)

    with patch("builtins.open", side_effect=record_open):
        if operation == "push_response":
            await client.push_response(1, "reply")
        else:
            await getattr(client, operation)()

    assert io_threads
    assert loop_thread not in io_threads


@pytest.mark.parametrize("kind", ["request", "response"])
async def test_reader_ignores_unfinished_last_line(tmp_path, kind):
    complete = f'LLM_{kind.upper()}_STARTfirstLLM_{kind.upper()}_END{{"index": 1}}\n'
    log_file = tmp_path / "model.log"
    log_file.write_text(complete + f"LLM_{kind.upper()}_STARTpartial-secret")
    client = ModelClient(str(log_file))

    line = await getattr(client, f"read_last_{kind}_line")()

    assert line == complete


async def test_reader_ignores_incomplete_utf8_tail(tmp_path):
    complete = 'LLM_REQUEST_STARTfirstLLM_REQUEST_END{"index": 1}\n'
    log_file = tmp_path / "model.log"
    log_file.write_bytes(complete.encode() + b"LLM_REQUEST_START\xe4\xb8")

    assert await ModelClient(str(log_file)).read_last_request_line() == complete


async def test_pop_request_waits_for_first_complete_line(tmp_path):
    log_file = tmp_path / "model.log"
    log_file.write_text("LLM_REQUEST_STARTpartial")
    client = ModelClient(str(log_file))

    async def finish_line(_delay):
        log_file.write_text('LLM_REQUEST_STARTcompleteLLM_REQUEST_END{"index": 1}\n')

    with patch("rock.sdk.model.client.asyncio.sleep", side_effect=finish_line):
        assert await client.pop_request(1) == "complete"


@pytest.mark.parametrize("kind", ["request", "response"])
@pytest.mark.parametrize("metadata", ["[]", "null", '"secret"'])
async def test_metadata_must_be_an_object(kind, metadata):
    line = f"LLM_{kind.upper()}_STARTsecret-payloadLLM_{kind.upper()}_END{metadata}"

    with pytest.raises(ValueError, match=f"Invalid {kind} log line: metadata must be a JSON object"):
        await getattr(ModelClient(), f"parse_{kind}_line")(line)


@pytest.mark.parametrize("kind", ["request", "response"])
@pytest.mark.parametrize("missing", ["START", "END"])
async def test_missing_marker_has_context(kind, missing):
    line = f'LLM_{kind.upper()}_STARTsecretLLM_{kind.upper()}_END{{"index": 1}}'
    line = line.replace(f"LLM_{kind.upper()}_{missing}", "")

    with pytest.raises(ValueError, match=f"Invalid {kind} log line: missing start or end marker") as error:
        await getattr(ModelClient(), f"parse_{kind}_line")(line)

    assert "secret" not in str(error.value)


@pytest.mark.parametrize("kind", ["request", "response"])
async def test_payload_can_contain_protocol_markers(kind):
    payload = f'{{"text": "SESSION_END LLM_{kind.upper()}_START LLM_{kind.upper()}_END"}}'
    line = f'LLM_{kind.upper()}_START{payload}LLM_{kind.upper()}_END{{"index": 2}}'

    parsed, metadata = await getattr(ModelClient(), f"parse_{kind}_line")(line)

    assert parsed == payload
    assert metadata == {"index": 2}


async def test_concurrent_responses_do_not_duplicate_or_interleave(tmp_path):
    log_file = tmp_path / "model.log"
    log_file.write_text("")
    client = ModelClient(str(log_file))

    await asyncio.gather(*(client.push_response(1, "中文 response") for _ in range(10)))

    lines = log_file.read_text().splitlines()
    assert len(lines) == 1
    response, metadata = await client.parse_response_line(lines[0])
    assert response == "中文 response"
    assert metadata["index"] == 1
    with pytest.raises(ValueError, match="must not be smaller"):
        await client.push_response(0, "older")


async def test_cancelled_response_writer_keeps_append_serialized(tmp_path):
    log_file = tmp_path / "model.log"
    log_file.write_text("")
    client = ModelClient(str(log_file))
    append_started = threading.Event()
    release_append = threading.Event()
    original_open = open

    def delayed_open(file, mode="r", **kwargs):
        if mode == "a":
            append_started.set()
            if not release_append.wait(3):
                raise TimeoutError("test did not release file append")
        return original_open(file, mode, **kwargs)

    with patch("builtins.open", side_effect=delayed_open):
        writer = asyncio.create_task(client.push_response(1, "reply"))
        try:
            assert await asyncio.to_thread(append_started.wait, 3)
            writer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await writer
            follower = asyncio.create_task(client.push_response(1, "reply"))
            release_append.set()
            await asyncio.wait_for(follower, 3)
        finally:
            release_append.set()

    assert len(log_file.read_text().splitlines()) == 1


@pytest.mark.parametrize("initial_content", [None, "", "LLM_REQUEST_STARTpartial"])
async def test_wait_for_first_request_waits_for_complete_record(tmp_path, initial_content):
    log_file = tmp_path / "model.log"
    if initial_content is not None:
        log_file.write_text(initial_content)
    client = ModelClient(str(log_file))

    async def finish_line(_delay):
        log_file.write_text('LLM_REQUEST_STARTcompleteLLM_REQUEST_END{"index": 1}\n')

    with patch("rock.sdk.model.client.asyncio.sleep", side_effect=finish_line) as sleep:
        await client.wait_for_first_request()

    sleep.assert_awaited_once()


async def test_anti_call_llm_file_exchange_and_session_end(tmp_path):
    log_file = tmp_path / "model.log"
    log_file.write_text('LLM_REQUEST_STARTfirstLLM_REQUEST_END{"index": 1}\n')
    client = ModelClient(str(log_file))
    assert await client.anti_call_llm(0) == "first"
    with log_file.open("a") as file:
        file.write('LLM_REQUEST_STARTsecondLLM_REQUEST_END{"index": 2}\n')
    assert await client.anti_call_llm(1, "reply") == "second"
    response = await client.read_last_response_line()
    assert (await client.parse_response_line(response))[0] == "reply"
    with log_file.open("a") as file:
        file.write("SESSION_END\n")
    assert await client.pop_request(3) == "SESSION_END"


async def test_complete_corrupt_record_fails_instead_of_polling_forever(tmp_path):
    log_file = tmp_path / "model.log"
    log_file.write_text("LLM_REQUEST_STARTsecretLLM_REQUEST_END{invalid}\n")

    with pytest.raises(ValueError, match="Invalid request log line: malformed metadata JSON"):
        await ModelClient(str(log_file)).pop_request(1)
