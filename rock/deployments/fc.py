"""Alibaba Cloud Function Compute (FC) deployment implementation.

This module provides FC runtime support for ROCK sandboxes using
WebSocket session API to maintain stateful bash sessions.

FC (Function Compute) is Alibaba Cloud's serverless compute service:
https://www.alibabacloud.com/product/function-compute
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from rock.actions import (
    CloseResponse,
    CloseSessionRequest,
    CloseSessionResponse,
    Command,
    CommandResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    IsAliveResponse,
    ReadFileRequest,
    ReadFileResponse,
    UploadRequest,
    UploadResponse,
    WriteFileRequest,
    WriteFileResponse,
)
from rock.actions.sandbox.base import AbstractSandbox
from rock.actions.sandbox.request import Action
from rock.actions.sandbox.response import Observation
from rock.logger import init_logger

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError:
    websockets = None
    ConnectionClosed = Exception

__all__ = ["FCRuntime", "FCSessionManager", "ReconnectConfig", "CircuitBreaker", "CircuitState", "FCError", "FCSandboxNotFoundError", "FCRuntimeError"]

logger = init_logger(__name__)


# =============================================================================
# Custom Exceptions
# =============================================================================

class FCError(Exception):
    """Base exception for FC (Function Compute) related errors."""
    pass


class FCSandboxNotFoundError(FCError):
    """FC sandbox not found."""
    pass


class FCRuntimeError(FCError):
    """FC runtime error."""
    pass


@dataclass
class ReconnectConfig:
    """Configuration for WebSocket reconnection behavior."""

    max_retries: int = 3
    """Maximum number of reconnection attempts."""

    base_delay: float = 1.0
    """Base delay in seconds for exponential backoff."""

    max_delay: float = 30.0
    """Maximum delay in seconds between retries."""

    backoff_factor: float = 2.0
    """Multiplier for delay increase after each retry."""

    def get_delay(self, attempt: int) -> float:
        """Calculate delay for a given retry attempt."""
        delay = self.base_delay * (self.backoff_factor ** attempt)
        return min(delay, self.max_delay)


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"  # Normal operation, requests flow through
    OPEN = "open"     # Failing, requests are blocked
    HALF_OPEN = "half_open"  # Testing if recovered


@dataclass
class CircuitBreaker:
    """Circuit breaker for protecting against cascading failures.

    Prevents repeated calls to a failing service, allowing it time to recover.
    Thread-safe via asyncio.Lock for state transitions.
    """

    failure_threshold: int = 5
    """Number of consecutive failures before opening the circuit."""

    success_threshold: int = 2
    """Number of consecutive successes in half-open state to close the circuit."""

    recovery_timeout: float = 30.0
    """Seconds to wait before attempting recovery (half-open state)."""

    # Internal state
    _state: CircuitState = field(default=CircuitState.CLOSED, repr=False)
    _failure_count: int = field(default=0, repr=False)
    _success_count: int = field(default=0, repr=False)
    _last_failure_time: float = field(default=0.0, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def state(self) -> CircuitState:
        """Get current circuit state, checking for recovery timeout."""
        return self._state

    async def can_execute(self) -> bool:
        """Check if a request can be executed (async for lock safety)."""
        async with self._lock:
            if self._state == CircuitState.OPEN:
                if time.time() - self._last_failure_time >= self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._success_count = 0
                    logger.info("Circuit breaker state changed: OPEN -> HALF_OPEN")
                    return True
                return False
            return True

    async def record_success(self):
        """Record a successful operation (async for lock safety)."""
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    logger.info("Circuit breaker state changed: HALF_OPEN -> CLOSED")
            else:
                self._failure_count = 0

    async def record_failure(self):
        """Record a failed operation (async for lock safety)."""
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()

            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                logger.warning("Circuit breaker state changed: HALF_OPEN -> OPEN")
            elif self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                logger.warning(
                    f"Circuit breaker state changed: CLOSED -> OPEN "
                    f"(failure_count={self._failure_count})"
                )


@dataclass
class SessionState:
    """State tracking for a WebSocket session."""

    session_id: str
    ps1: str = "root@fc:~$ "
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    reconnect_count: int = 0
    websocket: Any = None

    def touch(self):
        """Update last activity timestamp."""
        self.last_activity = time.time()


class FCSessionManager:
    """Function Compute (FC) WebSocket session manager.

    Manages WebSocket connections to FC function instances,
    maintaining long-lived bash sessions for stateful command execution.

    Features:
    - Automatic reconnection with exponential backoff
    - Session state tracking and recovery
    - Resource leak prevention via async context managers
    """

    def __init__(
        self,
        config: "FCDeploymentConfig",
        reconnect_config: ReconnectConfig | None = None,
    ):
        from rock.deployments.config import FCDeploymentConfig

        self.config: FCDeploymentConfig = config
        self.reconnect_config = reconnect_config or ReconnectConfig()
        self.sessions: dict[str, SessionState] = {}  # session_id -> SessionState
        self._websocket_url = self._build_websocket_url()
        self._http_url = self._build_http_url()
        self._lock = asyncio.Lock()

    def _build_websocket_url(self) -> str:
        """Build WebSocket URL for FC stateful invocation."""
        # FC WebSocket endpoint format
        return (
            f"wss://{self.config.account_id}.{self.config.region}.fc.aliyuncs.com"
            f"/2023-03-30/functions/{self.config.function_name}/stateful-async-invocation"
        )

    def _build_http_url(self) -> str:
        """Build HTTP URL for FC standard invocation."""
        return (
            f"https://{self.config.account_id}.{self.config.region}.fc.aliyuncs.com"
            f"/2023-03-30/functions/{self.config.function_name}/invocations"
        )

    def _build_auth_headers(
        self,
        method: str = "POST",
        path: str = "",
        body: str = "",
        session_id: str | None = None,
    ) -> dict[str, str]:
        """Build authentication headers for FC API calls using signature.

        FC signature algorithm reference:
        https://help.aliyun.com/document_detail/52877.html

        StringToSign format:
        {METHOD}\\n{Content-MD5}\\n{Content-Type}\\n{Date}\\n{CanonicalizedFCHeaders}{CanonicalizedResource}
        """
        import email.utils

        # Build canonical headers
        host = f"{self.config.account_id}.{self.config.region}.fc.aliyuncs.com"
        content_md5 = base64.b64encode(hashlib.md5(body.encode()).digest()).decode() if body else ""
        content_type = "application/json"
        # Use RFC 2822 date format (same as email.utils.formatdate)
        date_header = email.utils.formatdate(usegmt=True)

        headers = {
            "Host": host,
            "Date": date_header,
            "Content-Type": content_type,
            "x-fc-account-id": self.config.account_id,
        }

        # Add x-fc-* headers
        if content_md5:
            headers["Content-MD5"] = content_md5

        if self.config.security_token:
            headers["x-fc-security-token"] = self.config.security_token

        if session_id:
            # 使用自定义 Header 名称，与 s.yaml 中 affinityHeaderFieldName 保持一致
            headers["x-rock-session-id"] = session_id

        # Build canonical resource (path + queries)
        canonical_uri = path or f"/2023-03-30/functions/{self.config.function_name}/invocations"
        canonical_resource = canonical_uri + "\n"  # No query params for now

        # Build canonical headers (only x-fc-* headers, sorted, lowercase keys)
        # Format: "key:value\n" for each header, with trailing newline
        fc_headers = sorted(
            [(k.lower(), v) for k, v in headers.items() if k.lower().startswith("x-fc-")],
            key=lambda x: x[0]
        )
        canonical_headers = ""
        if fc_headers:
            canonical_headers = "\n".join(f"{k}:{v}" for k, v in fc_headers) + "\n"

        # Build string to sign per official spec:
        # {METHOD}\n{Content-MD5}\n{Content-Type}\n{Date}\n{CanonicalizedFCHeaders}{CanonicalizedResource}
        string_to_sign = "\n".join([
            method.upper(),
            content_md5,
            content_type,
            date_header,
            canonical_headers + canonical_resource
        ])

        # Calculate signature using HMAC-SHA256
        signature = hmac.new(
            self.config.access_key_secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        signature_b64 = base64.b64encode(signature).decode("utf-8")

        # Add Authorization header
        headers["Authorization"] = f"FC {self.config.access_key_id}:{signature_b64}"

        return headers

    async def create_session(self, session_id: str, ps1: str = "root@fc:~$ ") -> dict:
        """Create a new bash session via WebSocket.

        Args:
            session_id: Unique session identifier.
            ps1: Shell prompt string.

        Returns:
            Session initialization response.

        Raises:
            ImportError: If websockets package is not installed.
            RuntimeError: If session creation fails after all retries.
        """
        if websockets is None:
            raise ImportError("websockets package is required for FC session API")

        async with self._lock:
            if session_id in self.sessions:
                logger.warning(f"Session {session_id} already exists, closing old connection")
                await self._close_session_internal(session_id)

            # Try to create session with retries
            last_error = None
            for attempt in range(self.reconnect_config.max_retries + 1):
                try:
                    headers = self._build_auth_headers(session_id=session_id)
                    ws = await websockets.connect(
                        self._websocket_url,
                        additional_headers=headers,
                        ping_interval=30,
                        ping_timeout=10,
                    )

                    # Send init_bash command
                    init_payload = {
                        "action": "init_bash",
                        "ps1": ps1,
                    }
                    await ws.send(json.dumps(init_payload))

                    # Wait for init response
                    response = await asyncio.wait_for(ws.recv(), timeout=30)
                    result = json.loads(response)

                    if result.get("type") == "error":
                        await ws.close()
                        raise RuntimeError(f"Failed to init bash: {result.get('message')}")

                    # Store session state
                    self.sessions[session_id] = SessionState(
                        session_id=session_id,
                        ps1=ps1,
                        websocket=ws,
                    )
                    logger.info(f"Session {session_id} created successfully (attempt {attempt + 1})")
                    return result

                except Exception as e:
                    last_error = e
                    logger.warning(f"Failed to create session {session_id} (attempt {attempt + 1}): {e}")

                    if attempt < self.reconnect_config.max_retries:
                        delay = self.reconnect_config.get_delay(attempt)
                        logger.info(f"Retrying in {delay:.1f}s...")
                        await asyncio.sleep(delay)

            logger.error(f"Failed to create session {session_id} after {self.reconnect_config.max_retries + 1} attempts")
            raise RuntimeError(f"Failed to create session: {last_error}")

    async def _reconnect_session(self, session_id: str) -> bool:
        """Attempt to reconnect a disconnected session.

        Args:
            session_id: Session to reconnect.

        Returns:
            True if reconnection successful, False otherwise.
        """
        async with self._lock:
            if session_id not in self.sessions:
                return False
            state = self.sessions[session_id]
            state.reconnect_count += 1

        logger.info(f"Attempting to reconnect session {session_id} (attempt {state.reconnect_count})")

        try:
            headers = self._build_auth_headers(session_id=session_id)
            ws = await websockets.connect(
                self._websocket_url,
                additional_headers=headers,
                ping_interval=30,
                ping_timeout=10,
            )

            # Re-initialize bash session
            init_payload = {
                "action": "init_bash",
                "ps1": state.ps1,
            }
            await ws.send(json.dumps(init_payload))

            response = await asyncio.wait_for(ws.recv(), timeout=30)
            result = json.loads(response)

            if result.get("type") == "error":
                await ws.close()
                logger.error(f"Failed to re-init bash for {session_id}: {result.get('message')}")
                return False

            # Update session state with new websocket
            async with self._lock:
                if session_id in self.sessions:
                    self.sessions[session_id].websocket = ws
                    self.sessions[session_id].touch()
            logger.info(f"Session {session_id} reconnected successfully")
            return True

        except Exception as e:
            logger.error(f"Failed to reconnect session {session_id}: {e}")
            return False

    async def execute_command(
        self,
        session_id: str,
        command: str,
        timeout: float = 60.0,
        auto_reconnect: bool = True,
    ) -> tuple[str, int]:
        """Execute a command in an existing session.

        Args:
            session_id: Target session ID.
            command: Command to execute.
            timeout: Command timeout in seconds.
            auto_reconnect: Whether to attempt reconnection on connection failure.

        Returns:
            Tuple of (output, exit_code).
        """
        async with self._lock:
            if session_id not in self.sessions:
                raise ValueError(f"Session {session_id} not found")
            state = self.sessions[session_id]
            ws = state.websocket

        # Check if connection is still alive
        if ws is None or not ws.open:
            if auto_reconnect:
                logger.warning(f"Session {session_id} connection is dead, attempting reconnect")
                if not await self._reconnect_session(session_id):
                    raise RuntimeError(f"Session {session_id} disconnected and reconnection failed")
                ws = state.websocket
            else:
                raise RuntimeError(f"Session {session_id} connection is closed")

        try:
            # Send execute command
            execute_payload = {
                "action": "execute",
                "command": command,
                "timeout": timeout,
            }
            await ws.send(json.dumps(execute_payload))
            state.touch()

            # Collect streaming output
            output = ""
            exit_code = 0

            while True:
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    chunk = json.loads(message)

                    if chunk.get("type") == "output":
                        output += chunk.get("data", "")
                    elif chunk.get("type") == "exit":
                        exit_code = chunk.get("exit_code", 0)
                        break
                    elif chunk.get("type") == "error":
                        output = chunk.get("message", "Unknown error")
                        exit_code = -1
                        break

                except asyncio.TimeoutError:
                    output += "\n[Command timed out]"
                    exit_code = -1
                    break

            state.touch()
            return output, exit_code

        except ConnectionClosed as e:
            logger.error(f"WebSocket connection closed for session {session_id}: {e}")

            # Try to reconnect if enabled
            if auto_reconnect and await self._reconnect_session(session_id):
                logger.info(f"Session {session_id} reconnected, retrying command")
                # Retry the command once after reconnection
                return await self.execute_command(
                    session_id, command, timeout, auto_reconnect=False
                )

            # Reconnection failed or not enabled - raise error but keep session state
            # Caller can decide to close session explicitly
            raise RuntimeError(f"Session {session_id} disconnected: {e}")
        except Exception as e:
            logger.error(f"Error executing command in session {session_id}: {e}")
            raise

    async def _close_session_internal(self, session_id: str) -> None:
        """Internal method to close a session without acquiring lock.

        Args:
            session_id: Session to close.
        """
        state = self.sessions.pop(session_id, None)
        if state and state.websocket:
            try:
                # Send close command
                close_payload = {"action": "close"}
                await state.websocket.send(json.dumps(close_payload))
                await state.websocket.close()
                logger.info(f"Session {session_id} closed")
            except Exception as e:
                logger.warning(f"Error closing session {session_id}: {e}")

    async def close_session(self, session_id: str) -> None:
        """Close a session and its WebSocket connection.

        Args:
            session_id: Session to close.
        """
        async with self._lock:
            await self._close_session_internal(session_id)

    async def close_all_sessions(self) -> None:
        """Close all active sessions."""
        session_ids = list(self.sessions.keys())
        for session_id in session_ids:
            await self.close_session(session_id)

    async def is_session_alive(self, session_id: str) -> bool:
        """Check if a session is alive.

        Args:
            session_id: Session to check.

        Returns:
            True if session exists and WebSocket is open.
        """
        async with self._lock:
            state = self.sessions.get(session_id)
            return state is not None and state.websocket is not None and state.websocket.open

    async def get_session_stats(self, session_id: str) -> dict | None:
        """Get statistics for a session.

        Args:
            session_id: Session to query.

        Returns:
            Session stats dict or None if session not found.
        """
        async with self._lock:
            state = self.sessions.get(session_id)
            if state is None:
                return None
            return {
                "session_id": state.session_id,
                "created_at": state.created_at,
                "last_activity": state.last_activity,
                "reconnect_count": state.reconnect_count,
                "is_alive": state.websocket is not None and state.websocket.open,
            }


class FCRuntime(AbstractSandbox):
    """Function Compute (FC) Runtime implementation using WebSocket session API.

    Provides stateful bash sessions on FC function instances,
    with semantics compatible with Docker Runtime (cd, export, nohup work correctly).

    Features:
    - Automatic retry with exponential backoff for HTTP calls
    - WebSocket reconnection for session commands
    - Connection pooling for HTTP client
    """

    # Default retry configuration
    DEFAULT_MAX_RETRIES = 3
    DEFAULT_RETRY_BASE_DELAY = 1.0
    DEFAULT_RETRY_MAX_DELAY = 30.0
    # HTTP status codes that should trigger retry
    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

    def __init__(self, config: "FCDeploymentConfig"):
        from rock.deployments.config import FCDeploymentConfig

        self.config: FCDeploymentConfig = config
        self.session_manager = FCSessionManager(config)
        self._http_client = None
        self._started = False
        self._http_lock = asyncio.Lock()
        # Circuit breaker for fault tolerance
        self._circuit_breaker = CircuitBreaker()

    async def _ensure_http_client(self):
        """Ensure HTTP client is initialized with connection pooling."""
        async with self._http_lock:
            if self._http_client is None:
                try:
                    import httpx
                    # Configure connection pool and timeouts
                    self._http_client = httpx.AsyncClient(
                        timeout=httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0),
                        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                    )
                    logger.debug("HTTP client initialized with connection pooling")
                except ImportError:
                    import aiohttp
                    # aiohttp has built-in connection pooling
                    connector = aiohttp.TCPConnector(limit=10, limit_per_host=5)
                    timeout = aiohttp.ClientTimeout(total=60, connect=10)
                    self._http_client = aiohttp.ClientSession(connector=connector, timeout=timeout)
                    logger.debug("HTTP client initialized with aiohttp")

    async def _http_request_with_retry(
        self,
        payload: dict,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> dict:
        """Execute HTTP request with automatic retry on transient failures.

        Uses circuit breaker pattern to prevent cascading failures when
        FC service is unavailable.

        Args:
            payload: Request payload.
            timeout: Request timeout in seconds.
            max_retries: Maximum retry attempts (default: 3).

        Returns:
            Response JSON dict.

        Raises:
            RuntimeError: If circuit is open or all retries exhausted.
        """
        # Check circuit breaker
        if not await self._circuit_breaker.can_execute():
            raise RuntimeError(
                f"Circuit breaker is OPEN - FC service appears unavailable. "
                f"Will retry after {self._circuit_breaker.recovery_timeout}s"
            )

        max_retries = max_retries or self.DEFAULT_MAX_RETRIES
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                headers = self.session_manager._build_auth_headers()
                request_timeout = timeout or 60.0

                if hasattr(self._http_client, 'post'):
                    # httpx client
                    response = await self._http_client.post(
                        self.session_manager._http_url,
                        headers=headers,
                        json=payload,
                        timeout=request_timeout,
                    )

                    # Check for retryable status codes
                    if response.status_code in self.RETRYABLE_STATUS_CODES:
                        raise RuntimeError(f"HTTP {response.status_code}: {response.text}")

                    response.raise_for_status()
                    result = response.json()
                    await self._circuit_breaker.record_success()
                    return result
                else:
                    # aiohttp client
                    async with self._http_client.post(
                        self.session_manager._http_url,
                        headers=headers,
                        json=payload,
                    ) as response:
                        if response.status in self.RETRYABLE_STATUS_CODES:
                            text = await response.text()
                            raise RuntimeError(f"HTTP {response.status}: {text}")

                        response.raise_for_status()
                        result = await response.json()
                        await self._circuit_breaker.record_success()
                        return result

            except Exception as e:
                last_error = e
                is_retryable = (
                    isinstance(e, RuntimeError) or
                    "timeout" in str(e).lower() or
                    "connection" in str(e).lower()
                )

                if is_retryable and attempt < max_retries:
                    delay = min(
                        self.DEFAULT_RETRY_BASE_DELAY * (2 ** attempt),
                        self.DEFAULT_RETRY_MAX_DELAY
                    )
                    logger.warning(
                        f"HTTP request failed (attempt {attempt + 1}/{max_retries + 1}): {e}. "
                        f"Retrying in {delay:.1f}s..."
                    )
                    await asyncio.sleep(delay)
                else:
                    break

        # Record failure in circuit breaker
        await self._circuit_breaker.record_failure()
        raise RuntimeError(f"HTTP request failed after {max_retries + 1} attempts: {last_error}")

    async def is_alive(self, *, timeout: float | None = None) -> IsAliveResponse:
        """Check if the FC runtime is alive."""
        try:
            await self._ensure_http_client()

            payload = {"action": "health_check"}
            result = await self._http_request_with_retry(payload, timeout=timeout or 10.0, max_retries=1)

            return IsAliveResponse(is_alive=result.get("status") == "ok")
        except Exception as e:
            logger.warning(f"FC health check failed: {e}")
            return IsAliveResponse(is_alive=False)

    async def create_session(self, request: CreateSessionRequest) -> CreateSessionResponse:
        """Create a new bash session."""
        result = await self.session_manager.create_session(
            session_id=request.session,
            ps1=getattr(request, 'ps1', 'root@fc:~$ '),
        )
        return CreateSessionResponse(
            output=result.get("ps1", "root@fc:~$ "),
        )

    async def run_in_session(self, action: Action) -> Observation:
        """Execute an action within an existing session."""
        from rock.actions import BashObservation

        output, exit_code = await self.session_manager.execute_command(
            session_id=action.session,
            command=action.command,
            timeout=getattr(action, 'timeout', 60.0),
        )
        return BashObservation(
            output=output,
            exit_code=exit_code,
        )

    async def close_session(self, request: CloseSessionRequest) -> CloseSessionResponse:
        """Close an existing session."""
        await self.session_manager.close_session(request.session)
        return CloseSessionResponse()

    async def execute(self, command: Command) -> CommandResponse:
        """Execute a one-time command via HTTP (stateless).

        Raises:
            RuntimeError: If the command execution fails due to RPC/network errors.
            Note: Command failure (non-zero exit code) returns normally with exit_code != 0.
        """
        await self._ensure_http_client()

        payload = {
            "action": "execute_command",
            "command": command.command,
            "cwd": getattr(command, 'cwd', '/tmp'),
            "env": getattr(command, 'env', {}),
            "timeout": getattr(command, 'timeout', 60),
        }

        result = await self._http_request_with_retry(payload)
        return CommandResponse(
            stdout=result.get("stdout", ""),
            stderr=result.get("stderr", ""),
            exit_code=result.get("exit_code", 0),
        )

    async def read_file(self, request: ReadFileRequest) -> ReadFileResponse:
        """Read file content from the FC environment."""
        await self._ensure_http_client()

        payload = {
            "action": "read_file",
            "path": request.path,
            "encoding": getattr(request, 'encoding', 'utf-8'),
        }

        try:
            result = await self._http_request_with_retry(payload)

            if "error" in result:
                raise RuntimeError(result["error"])

            return ReadFileResponse(content=result.get("content", ""))
        except Exception as e:
            logger.error(f"Read file failed: {e}")
            raise

    async def write_file(self, request: WriteFileRequest) -> WriteFileResponse:
        """Write content to a file in the FC environment."""
        await self._ensure_http_client()

        payload = {
            "action": "write_file",
            "path": request.path,
            "content": request.content,
            "encoding": getattr(request, 'encoding', 'utf-8'),
        }

        try:
            result = await self._http_request_with_retry(payload)

            if "error" in result:
                raise RuntimeError(result["error"])

            return WriteFileResponse(success=True)
        except Exception as e:
            logger.error(f"Write file failed: {e}")
            raise

    async def upload(self, request: UploadRequest) -> UploadResponse:
        """Upload a file to the FC environment.

        Reads the file from source_path locally and uploads it to the FC function.
        """
        import pathlib

        # Read the file from local source_path
        source = pathlib.Path(request.source_path)
        if not source.exists():
            raise FileNotFoundError(f"Source file not found: {request.source_path}")

        content = source.read_bytes()
        content_b64 = base64.b64encode(content).decode('utf-8')

        await self._ensure_http_client()

        payload = {
            "action": "upload_file",
            "path": request.target_path,
            "content": content_b64,
            "encoding": "base64",
        }

        try:
            result = await self._http_request_with_retry(payload)

            if "error" in result:
                raise RuntimeError(result["error"])

            return UploadResponse(success=True, file_name=request.target_path)
        except Exception as e:
            logger.error(f"Upload file failed: {e}")
            raise

    async def close(self) -> CloseResponse:
        """Close the runtime and cleanup resources."""
        await self.session_manager.close_all_sessions()
        if self._http_client:
            await self._http_client.aclose() if hasattr(self._http_client, 'aclose') else await self._http_client.close()
            self._http_client = None
        self._started = False
        return CloseResponse()
