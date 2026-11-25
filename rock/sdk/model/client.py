import asyncio
import json
import threading
import time

from rock.logger import init_logger
from rock.sdk.model.server.config import (
    LOG_FILE,
    REQUEST_END_MARKER,
    REQUEST_START_MARKER,
    RESPONSE_END_MARKER,
    RESPONSE_START_MARKER,
    SESSION_END_MARKER,
)

logger = init_logger(__name__)


class _NoCompleteRequestError(ValueError):
    """The log does not contain a committed request line yet."""


class ModelClient:
    def __init__(self, log_file_name: str | None = None):
        if log_file_name is not None:
            self.log_file = log_file_name
        else:
            self.log_file = LOG_FILE
        self._response_lock = threading.Lock()

    async def anti_call_llm(self, index: int, last_response: str | None = None) -> str:
        """anti call llm, input is response of llm, output is the next request to llm"""
        if index < 0:
            raise ValueError("index must be greater than 0")

        if 0 == index:
            if last_response is not None:
                raise ValueError("last_response must be None when index is 0")
            await self.wait_for_first_request()
            return await self.pop_request(index + 1)

        if last_response is None:
            raise ValueError("last_response must not be None when index is greater than 0")
        await self.push_response(index=index, last_response=last_response)
        return await self.pop_request(index + 1)

    async def push_response(self, index: int, last_response: str):
        content = await self._construct_response(last_response, index)
        await asyncio.to_thread(self._push_response_sync, index, content)

    def _push_response_sync(self, index: int, content: str) -> None:
        # Keep read/check/append atomic on this client, including when a caller
        # is cancelled while its worker thread is still finishing the write.
        with self._response_lock:
            last_response_line = self._read_last_line(RESPONSE_START_MARKER)
            if last_response_line is None:
                self._append_response_sync(content)
                return
            _, meta = self._parse_line(last_response_line, RESPONSE_START_MARKER, RESPONSE_END_MARKER, "response")
            last_response_index: int = int(meta.get("index"))  # type: ignore
            if index < last_response_index:
                raise ValueError(f"index {index} must not be smaller than last_response_index {last_response_index}")
            if index == last_response_index:
                logger.debug("response index %s already exists, skip", index)
                return
            self._append_response_sync(content)

    async def _append_response(self, content: str):
        await asyncio.to_thread(self._append_response_sync, content)

    def _append_response_sync(self, content: str) -> None:
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(content)

    async def pop_request(self, index: int) -> str:
        while True:
            try:
                last_request_line = await self.read_last_request_line()
            except _NoCompleteRequestError:
                logger.debug("No complete request in log file yet, waiting...")
            else:
                request_json, meta = await self.parse_request_line(last_request_line)
                if SESSION_END_MARKER == request_json:
                    return SESSION_END_MARKER
                if meta.get("index") == index:
                    return request_json
                logger.debug("Last request is not index %s, waiting...", index)
            await asyncio.sleep(1)

    async def parse_request_line(self, line_content: str) -> tuple[str, dict]:
        if line_content.strip() == SESSION_END_MARKER:
            return SESSION_END_MARKER, {}

        return self._parse_line(line_content, REQUEST_START_MARKER, REQUEST_END_MARKER, "request")

    async def parse_response_line(self, line_content: str) -> tuple[str, dict]:
        return self._parse_line(line_content, RESPONSE_START_MARKER, RESPONSE_END_MARKER, "response")

    @staticmethod
    def _parse_line(line_content: str, start_marker: str, end_marker: str, kind: str) -> tuple[str, dict]:
        _, start, content = line_content.partition(start_marker)
        payload, end, meta_json = content.rpartition(end_marker)
        if not start or not end:
            raise ValueError(f"Invalid {kind} log line: missing start or end marker")
        try:
            meta = json.loads(meta_json)
        except json.JSONDecodeError:
            raise ValueError(f"Invalid {kind} log line: malformed metadata JSON") from None
        if not isinstance(meta, dict):
            raise ValueError(f"Invalid {kind} log line: metadata must be a JSON object")
        return payload, meta

    async def read_last_request_line(self) -> str:
        line = await asyncio.to_thread(self._read_last_line, REQUEST_START_MARKER)
        if line is None:
            raise _NoCompleteRequestError(f"No complete request found in log file {self.log_file}")
        return line

    async def read_last_response_line(self) -> str | None:
        return await asyncio.to_thread(self._read_last_line, RESPONSE_START_MARKER)

    def _read_last_line(self, start_marker: str) -> str | None:
        with open(self.log_file, "rb") as f:
            lines = f.readlines()
        marker = start_marker.encode("utf-8")
        for line in reversed(lines):
            # Writers append one newline-terminated record. Ignore a trailing
            # fragment before decoding: it may even end inside a UTF-8 character.
            if not line.endswith(b"\n"):
                continue
            if line.startswith(marker):
                return line.decode("utf-8")
            if start_marker == REQUEST_START_MARKER and line.strip() == SESSION_END_MARKER.encode("utf-8"):
                return line.decode("utf-8")
        return None

    async def wait_for_first_request(self):
        while True:
            try:
                await self.read_last_request_line()
            except (FileNotFoundError, _NoCompleteRequestError):
                logger.debug("Waiting for the first complete request in log file %s", self.log_file)
                await asyncio.sleep(1)
            else:
                return

    async def _construct_response(self, last_response: str, index: int) -> str:
        meta = {
            "timestamp": int(time.time() * 1000),
            "index": index,
        }
        meta_json = json.dumps(meta, ensure_ascii=False)
        content = f"{RESPONSE_START_MARKER}{last_response}{RESPONSE_END_MARKER}{meta_json}\n"
        return content
