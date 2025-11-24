import asyncio
import logging
import mimetypes
import time
from collections.abc import Callable
from typing import BinaryIO

import httpx
from httpx import Response


class HttpUtils:
    """HTTP client utilities"""

    @staticmethod
    async def post(url: str, headers: dict, data: dict, read_timeout: float = 300.0) -> dict:
        """Send POST request"""
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout=300.0, connect=300.0, read=read_timeout if read_timeout else 300.0)
        ) as client:
            try:
                response: Response = await client.post(url, headers=headers, json=data)
                response.raise_for_status()
                return response.json()
            except Exception as e:
                logging.exception(f"Failed to post to {url}: {e}")
                raise e

    @staticmethod
    async def get(url: str, headers: dict) -> dict:
        """Send GET request"""
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout=300.0, connect=300.0, read=300.0)) as client:
            try:
                response: Response = await client.get(url, headers=headers)
                response.raise_for_status()
                return response.json()
            except Exception as e:
                logging.exception(f"Failed to get from {url}: {e}")
                raise e

    @staticmethod
    async def post_multipart(
        url: str,
        headers: dict,
        data: dict[str, str | int | float] | None = None,
        files: dict[str, BinaryIO | bytes | tuple] | None = None,
    ) -> dict:
        """
        Send multipart/form-data request

        Args:
            url: Request URL
            headers: Request headers
            data: Form data dictionary
            files: File data dictionary, format:
                - key: Field name
                - value: Can be:
                  - bytes: Direct byte data
                  - BinaryIO: File object
                  - tuple: (filename, content, content_type) or (filename, content)

        Returns:
            Response JSON data
        """
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout=300.0, connect=300.0, read=300.0)) as client:
            try:
                # Build multipart data
                multipart_data = {}
                multipart_files = {}

                # Add form fields
                if data:
                    for key, value in data.items():
                        if value is not None:
                            multipart_data[key] = str(value)

                # Add file fields
                if files:
                    for field_name, file_data in files.items():
                        if file_data is not None:
                            multipart_files[field_name] = HttpUtils._process_file_data(file_data)

                # Send request
                response: httpx.Response = await client.post(
                    url, headers=headers, data=multipart_data, files=multipart_files
                )
                response.raise_for_status()
                return response.json()

            except Exception as e:
                logging.exception(f"Failed to post multipart to {url}: {e}")
                raise e

    @staticmethod
    def _process_file_data(file_data: BinaryIO | bytes | tuple) -> tuple:
        """
        Process file data, uniformly convert to the format needed by httpx

        Args:
            file_data: File data

        Returns:
            Tuple in (filename, content, content_type) format
        """
        if isinstance(file_data, tuple):
            if len(file_data) == 2:
                # (filename, content)
                filename, content = file_data
                content_type = HttpUtils._guess_content_type(filename)
                return (filename, content, content_type)
            elif len(file_data) == 3:
                # (filename, content, content_type)
                return file_data
            else:
                raise ValueError(f"Invalid file tuple format: {file_data}")

        elif isinstance(file_data, bytes | bytearray):
            # Direct byte data
            return ("file", file_data, "application/octet-stream")

        elif hasattr(file_data, "read"):
            # File object
            content = file_data.read()
            filename = getattr(file_data, "name", "file")
            if hasattr(file_data, "seek"):
                file_data.seek(0)  # Reset file pointer
            content_type = HttpUtils._guess_content_type(filename)
            return (filename, content, content_type)

        else:
            raise ValueError(f"Unsupported file data type: {type(file_data)}")

    @staticmethod
    def _guess_content_type(filename: str) -> str:
        """Guess MIME type based on filename"""
        content_type, _ = mimetypes.guess_type(filename)
        return content_type or "application/octet-stream"


async def wait_until_alive(
    function: Callable, timeout: float = 10.0, function_timeout: float | None = 0.1, sleep: float = 0.25
):
    """Wait until the function returns a truthy value.

    Args:
        function: The function to wait for.
        timeout: The maximum time to wait.
        function_timeout: The timeout passed to the function.
        sleep: The time to sleep between attempts.

    Raises:
        TimeoutError
    """
    end_time = time.time() + timeout
    n_attempts = 0
    await_response = None
    while time.time() < end_time:
        await_response = await function(timeout=function_timeout)
        if await_response:
            return
        await asyncio.sleep(sleep)
        n_attempts += 1
    last_response_message = await_response.message if await_response else None
    msg = (
        f"Runtime did not start within {timeout}s (tried to connect {n_attempts} times). "
        f"The last await response was:\n{last_response_message}"
    )
    raise TimeoutError(msg)
