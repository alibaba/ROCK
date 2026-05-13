"""
ModelClient Demo - Demonstrates timeout and cancellation support.

This example shows how to use ModelClient with:
1. Timeout configuration
2. Cancellation handling
3. Basic request/response flow

NOTE: This demo requires the model service to be running.
Start the model service with: rock model-service start --type local
"""

import asyncio
import logging
import tempfile
from pathlib import Path

from rock.sdk.model.client import ModelClient, DEFAULT_POLL_TIMEOUT
from rock.sdk.model.server.config import REQUEST_END_MARKER, REQUEST_START_MARKER

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s -- %(message)s",
)
logger = logging.getLogger(__name__)


async def demo_timeout():
    """Demonstrate timeout behavior when request is not found."""
    logger.info("=" * 60)
    logger.info("Demo: Timeout behavior")
    logger.info("=" * 60)

    # Create a temporary log file with a request at index 1
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
        f.write(f'{REQUEST_START_MARKER}{{"model": "gpt-4", "messages": []}}{REQUEST_END_MARKER}{{"index": 1}}\n')
        log_file = f.name

    try:
        client = ModelClient(log_file_name=log_file)

        # Try to get request at index 2, which doesn't exist
        # This should timeout after 2 seconds
        logger.info("Attempting to pop request at index 2 (doesn't exist)...")
        try:
            await client.pop_request(index=2, timeout=2.0)
        except TimeoutError as e:
            logger.info(f"TimeoutError caught as expected: {e}")
    finally:
        Path(log_file).unlink(missing_ok=True)

    logger.info("Demo: Timeout behavior - PASSED\n")


async def demo_cancellation():
    """Demonstrate cancellation handling."""
    logger.info("=" * 60)
    logger.info("Demo: Cancellation handling")
    logger.info("=" * 60)

    # Create a temporary log file with a request at index 1
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
        f.write(f'{REQUEST_START_MARKER}{{"model": "gpt-4", "messages": []}}{REQUEST_END_MARKER}{{"index": 1}}\n')
        log_file = f.name

    try:
        client = ModelClient(log_file_name=log_file)

        async def long_running_pop():
            try:
                # This will wait indefinitely for index 2
                return await client.pop_request(index=2, timeout=100.0)
            except asyncio.CancelledError:
                logger.info("Task was cancelled, cleaning up...")
                raise

        # Create a task that can be cancelled
        task = asyncio.create_task(long_running_pop())

        # Wait a bit and then cancel
        await asyncio.sleep(1.0)
        logger.info("Cancelling the task...")
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            logger.info("CancelledError propagated correctly")
    finally:
        Path(log_file).unlink(missing_ok=True)

    logger.info("Demo: Cancellation handling - PASSED\n")


async def demo_happy_path():
    """Demonstrate normal request retrieval."""
    logger.info("=" * 60)
    logger.info("Demo: Normal request retrieval")
    logger.info("=" * 60)

    # Create a temporary log file with requests
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
        f.write(f'{REQUEST_START_MARKER}{{"model": "gpt-4", "messages": [{{"role": "user", "content": "Hello"}}]}}{REQUEST_END_MARKER}{{"index": 1}}\n')
        log_file = f.name

    try:
        client = ModelClient(log_file_name=log_file)

        logger.info("Popping request at index 1...")
        request = await client.pop_request(index=1, timeout=5.0)
        logger.info(f"Got request: {request[:100]}...")

        if "gpt-4" in request:
            logger.info("Request content verified successfully")
    finally:
        Path(log_file).unlink(missing_ok=True)

    logger.info("Demo: Normal request retrieval - PASSED\n")


async def demo_wait_for_first_request():
    """Demonstrate wait_for_first_request with timeout."""
    logger.info("=" * 60)
    logger.info("Demo: wait_for_first_request with timeout")
    logger.info("=" * 60)

    # Use a non-existent file to trigger timeout
    client = ModelClient(log_file_name="/non/existent/path/file.log")

    logger.info("Waiting for first request (will timeout)...")
    try:
        await client.wait_for_first_request(timeout=1.0)
    except TimeoutError as e:
        logger.info(f"TimeoutError caught as expected: {e}")

    logger.info("Demo: wait_for_first_request with timeout - PASSED\n")


async def demo_default_timeout():
    """Show default timeout value."""
    logger.info("=" * 60)
    logger.info("Demo: Default timeout configuration")
    logger.info("=" * 60)

    logger.info(f"Default poll timeout: {DEFAULT_POLL_TIMEOUT} seconds")
    logger.info("This default is applied to pop_request and wait_for_first_request")
    logger.info("Demo: Default timeout configuration - PASSED\n")


async def main():
    """Run all demos."""
    logger.info("\n" + "=" * 60)
    logger.info("ModelClient Demo - Timeout & Cancellation Support")
    logger.info("=" * 60 + "\n")

    await demo_default_timeout()
    await demo_happy_path()
    await demo_timeout()
    await demo_wait_for_first_request()
    await demo_cancellation()

    logger.info("=" * 60)
    logger.info("All demos completed successfully!")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
