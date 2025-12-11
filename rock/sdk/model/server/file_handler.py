"""File handler for reading/writing LLM requests and responses."""
import asyncio
import json
import logging
import time
from typing import Any

from rock.sdk.model.server.config import (
    LOG_FILE,
    POLLING_INTERVAL_SECONDS,
    REQUEST_END_MARKER,
    REQUEST_START_MARKER,
    RESPONSE_END_MARKER,
    RESPONSE_START_MARKER,
    SESSION_END_MARKER,
)

logger = logging.getLogger(__name__)


class FileHandler:
    """Handles file-based communication with Roll process."""

    def __init__(self, log_file: str = LOG_FILE):
        self.log_file = log_file

    def write_request(self, request_data: dict[str, Any], index: int) -> None:
        """
        Write LLM request to log file with file locking.

        Format: LLM_REQUEST_START{json}LLM_REQUEST_END{meta}
        """
        meta = {"timestamp": int(time.time() * 1000), "index": index}

        request_json = json.dumps(request_data, ensure_ascii=False)
        meta_json = json.dumps(meta, ensure_ascii=False)

        line = f"{REQUEST_START_MARKER}{request_json}{REQUEST_END_MARKER}{meta_json}\n"

        # Write with file locking
        with open(self.log_file, "a") as f:
            f.write(line)
            f.flush()

        logger.info(f"Wrote request with index {index} to log file")

    async def poll_for_response(self, request_index: int) -> dict[str, Any] | None:
        """
        Poll log file for response matching the request index.

        Format: LLM_RESPONSE_START{json}LLM_RESPONSE_END{meta}

        Returns the response data or None if not found.
        """
        # Keep track of file position to avoid re-reading entire file
        last_position = 0

        while True:
            try:
                with open(self.log_file) as f:
                    # Seek to last read position
                    f.seek(last_position)

                    # Read new lines
                    lines = f.readlines()
                    last_position = f.tell()

                    # Parse lines for response
                    for line in lines:
                        if RESPONSE_START_MARKER in line and RESPONSE_END_MARKER in line:
                            response_data, meta = self._parse_response_line(line)
                            if response_data and meta and meta.get("index") == request_index:
                                logger.info(f"Found response for index {request_index}")
                                return response_data

                        # Check for session end
                        if SESSION_END_MARKER in line:
                            logger.info("Session ended")
                            return None

                # Wait before polling again (async sleep allows other requests to be processed)
                await asyncio.sleep(POLLING_INTERVAL_SECONDS)

            except asyncio.CancelledError:
                logger.info(f"Request {request_index} cancelled (client disconnected)")
                raise
            except Exception as e:
                logger.error(f"Error polling for response: {e}")
                await asyncio.sleep(POLLING_INTERVAL_SECONDS)

    def _parse_response_line(self, line: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """
        Parse a response line to extract response data and meta.

        Returns: (response_data, meta)
        """
        try:
            # Extract response JSON
            start_idx = line.find(RESPONSE_START_MARKER) + len(RESPONSE_START_MARKER)
            end_idx = line.find(RESPONSE_END_MARKER)

            if start_idx == -1 or end_idx == -1:
                return None, None

            response_json = line[start_idx:end_idx]
            response_data = json.loads(response_json)

            # Extract meta JSON
            meta_start = end_idx + len(RESPONSE_END_MARKER)
            meta_json = line[meta_start:].strip()
            meta = json.loads(meta_json) if meta_json else {}

            return response_data, meta

        except Exception as e:
            logger.error(f"Error parsing response line: {e}")
            return None, None

    def write_session_end(self) -> None:
        """Write SESSION_END marker to log file."""
        with open(self.log_file, "a") as f:
            f.write(f"{SESSION_END_MARKER}\n")
            f.flush()

        logger.info("Wrote SESSION_END to log file")
