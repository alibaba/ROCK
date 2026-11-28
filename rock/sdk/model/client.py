import asyncio
import json
import logging
import time
from pathlib import Path

from rock.sdk.model.server.config import (
    LOG_FILE,
    REQUEST_END_MARKER,
    REQUEST_START_MARKER,
    RESPONSE_END_MARKER,
    RESPONSE_START_MARKER,
    SESSION_END_MARKER,
)

logger = logging.getLogger(__name__)


class ModelClient:
    def __init__(self, log_file_name: str | None = None):
        if log_file_name is not None:
            self.log_file = log_file_name
        else:
            self.log_file = LOG_FILE

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
        last_response_line = await self.read_last_response_line()
        if last_response_line is None:
            await self._append_response(content)
            return
        response_json, meta = await self.parse_response_line(last_response_line)
        last_response_index: int = int(meta.get("index"))  # type: ignore
        if index < last_response_index:
            raise ValueError(f"index {index} must not be smaller than last_response_index {last_response_index}")
        if index == last_response_index:
            logger.debug(f"response index {index} already exists, skip. content is {last_response_line}")
            return
        await self._append_response(content)

    async def _append_response(self, content: str):
        await asyncio.to_thread(self._append_response_sync, content)

    def _append_response_sync(self, content: str):
        with open(self.log_file, "a") as file:
            file.write(content)

    async def pop_request(self, index: int) -> str:
        while True:
            last_request_line = await self.read_last_request_line()
            request_json, meta = await self.parse_request_line(last_request_line)
            if SESSION_END_MARKER == request_json:
                return SESSION_END_MARKER
            if meta.get("index") == index:
                return request_json
            logger.debug(f"Last request {last_request_line} is not the index {index} we want, waiting...")
            await asyncio.sleep(1)

    async def parse_request_line(self, line_content: str) -> tuple[str, dict]:
        if SESSION_END_MARKER in line_content:
            return SESSION_END_MARKER, {}

        meta_json = line_content.split(REQUEST_END_MARKER)[1]
        request_json = line_content.split(REQUEST_END_MARKER)[0].split(REQUEST_START_MARKER)[1]
        meta = self._parse_metadata(meta_json, REQUEST_START_MARKER)
        return request_json, meta

    async def parse_response_line(self, line_content: str) -> tuple[str, dict]:
        meta_json = line_content.split(RESPONSE_END_MARKER)[1]
        response_json = line_content.split(RESPONSE_END_MARKER)[0].split(RESPONSE_START_MARKER)[1]
        meta = self._parse_metadata(meta_json, RESPONSE_START_MARKER)
        return response_json, meta

    @staticmethod
    def _parse_metadata(meta_json: str, marker: str) -> dict:
        try:
            return json.loads(meta_json)
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid metadata after {marker}: {meta_json!r}") from error

    async def read_last_request_line(self) -> str:
        lines = await asyncio.to_thread(self._read_lines)
        line_index = len(lines) - 1
        while line_index >= 0:
            line = lines[line_index]
            if REQUEST_START_MARKER in line or SESSION_END_MARKER in line:
                return line
            line_index -= 1
        raise ValueError(f"No request found in log file {self.log_file}")

    def _read_lines(self) -> list[str]:
        with open(self.log_file) as file:
            return file.readlines()

    async def read_last_response_line(self) -> str | None:
        lines = await asyncio.to_thread(self._read_lines)
        line_index = len(lines) - 1
        while line_index >= 0:
            line = lines[line_index]
            if RESPONSE_START_MARKER in line:
                return line
            line_index -= 1
        return None

    async def wait_for_first_request(self):
        while True:
            if not Path(self.log_file).exists():
                logger.debug(f"Log file {self.log_file} not found, waiting...")
                await asyncio.sleep(1)
                continue
            if not await asyncio.to_thread(self._read_lines):
                logger.debug(f"Log file {self.log_file} is empty, waiting for the first request...")
                await asyncio.sleep(1)
                continue
            return

    async def _construct_response(self, last_response: str, index: int) -> str:
        meta = {
            "timestamp": int(time.time() * 1000),
            "index": index,
        }
        meta_json = json.dumps(meta, ensure_ascii=False)
        content = f"{RESPONSE_START_MARKER}{last_response}{RESPONSE_END_MARKER}{meta_json}\n"
        return content
