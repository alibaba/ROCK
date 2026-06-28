"""Alibaba Cloud Function Compute (FC) runtime implementation.

This module provides FC runtime support for ROCK sandboxes using
SDK InvokeFunction with session affinity for stateful bash sessions.

FC (Function Compute) is Alibaba Cloud's serverless compute service:
https://www.alibabacloud.com/product/function-compute

Session invocation uses FC SDK's InvokeFunction API with x-rock-session-id
header for session affinity. The rocklet receives invocations at POST /invoke
and dispatches based on the payload's 'action' field.

Note: This module is part of the FC Operator implementation, not the Deployment layer.
"""

import asyncio
import base64
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from rock.sandbox.operator.fc.config import FCOperatorConfig

__all__ = [
    "FCRuntime",
    "FCSessionManager",
    "CircuitBreaker",
    "CircuitState",
    "FCError",
    "FCSandboxNotFoundError",
    "FCRuntimeError",
]

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


# =============================================================================
# Circuit Breaker
# =============================================================================


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation, requests flow through
    OPEN = "open"  # Failing, requests are blocked
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
                logger.warning(f"Circuit breaker state changed: CLOSED -> OPEN (failure_count={self._failure_count})")


# =============================================================================
# Session State
# =============================================================================


@dataclass
class SessionState:
    """State tracking for an FC session (no persistent connection)."""

    session_id: str
    ps1: str = "root@fc:~$ "
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)

    def touch(self):
        """Update last activity timestamp."""
        self.last_activity = time.time()


# =============================================================================
# FC Session Manager
# =============================================================================


class FCSessionManager:
    """Function Compute (FC) session manager using SDK InvokeFunction.

    Manages stateful bash sessions on FC function instances via
    synchronous InvokeFunction calls with session affinity.

    Features:
    - Session affinity via x-rock-session-id header
    - Automatic retry with exponential backoff
    - Circuit breaker for fault tolerance
    - Session state tracking
    """

    DEFAULT_MAX_RETRIES = 3
    DEFAULT_RETRY_BASE_DELAY = 1.0
    DEFAULT_RETRY_MAX_DELAY = 30.0
    SESSION_AFFINITY_HEADER = "x-rock-session-id"

    def __init__(
        self,
        config: "FCOperatorConfig",
        fc_client: Any = None,
    ):
        from rock.sandbox.operator.fc.config import FCOperatorConfig

        self.config: FCOperatorConfig = config
        self._fc_client = fc_client
        self.sessions: dict[str, SessionState] = {}
        self._lock = asyncio.Lock()
        self._circuit_breaker = CircuitBreaker()

    async def _invoke_function(
        self,
        payload: dict,
        session_id: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> dict:
        """Invoke FC function via SDK InvokeFunction with retry and circuit breaker.

        Args:
            payload: Request payload (JSON dict, must include 'action' field).
            session_id: Session ID for session affinity (passed via x-rock-session-id header).
            timeout: Request timeout in seconds (unused by SDK, kept for interface compat).
            max_retries: Max retry attempts.

        Returns:
            Response JSON dict from the function.

        Raises:
            FCRuntimeError: If circuit is open or all retries exhausted.
        """
        if self._fc_client is None:
            raise FCRuntimeError("FC SDK client not initialized - cannot invoke function")

        if not await self._circuit_breaker.can_execute():
            raise FCRuntimeError(
                f"Circuit breaker is OPEN - FC service appears unavailable. "
                f"Will retry after {self._circuit_breaker.recovery_timeout}s"
            )

        max_retries = max_retries or self.DEFAULT_MAX_RETRIES
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                from alibabacloud_fc20230330.models import (
                    InvokeFunctionHeaders,
                    InvokeFunctionRequest,
                )
                from alibabacloud_tea_util.models import RuntimeOptions

                request = InvokeFunctionRequest(body=json.dumps(payload))
                headers = InvokeFunctionHeaders()
                runtime = RuntimeOptions(
                    read_timeout=120000,
                    connect_timeout=10000,
                )

                if session_id:
                    headers.common_headers = {
                        self.SESSION_AFFINITY_HEADER: session_id,
                    }

                response = await asyncio.to_thread(
                    self._fc_client.invoke_function_with_options,
                    self.config.function_name,
                    request,
                    headers,
                    runtime,
                )

                if response.body:
                    if hasattr(response.body, "read"):
                        body_str = response.body.read().decode("utf-8")
                    elif isinstance(response.body, bytes):
                        body_str = response.body.decode("utf-8")
                    else:
                        body_str = response.body if isinstance(response.body, str) else str(response.body)
                    result = json.loads(body_str) if body_str else {}
                else:
                    result = {}
                await self._circuit_breaker.record_success()
                return result

            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    delay = min(
                        self.DEFAULT_RETRY_BASE_DELAY * (2**attempt),
                        self.DEFAULT_RETRY_MAX_DELAY,
                    )
                    logger.warning(
                        f"InvokeFunction failed (attempt {attempt + 1}/{max_retries + 1}): {e}. "
                        f"Retrying in {delay:.1f}s..."
                    )
                    await asyncio.sleep(delay)

        await self._circuit_breaker.record_failure()
        raise FCRuntimeError(f"InvokeFunction failed after {max_retries + 1} attempts: {last_error}")

    async def create_session(self, session_id: str, ps1: str = "root@fc:~$ ") -> dict:
        """Create a new bash session via InvokeFunction.

        FC auto-creates session on first invocation with session ID (passive creation).
        The rocklet's /invoke handler processes the 'create_session' action.

        Args:
            session_id: Unique session identifier.
            ps1: Shell prompt string.

        Returns:
            Session initialization response from the rocklet.
        """
        async with self._lock:
            if session_id in self.sessions:
                logger.warning(f"Session {session_id} already exists, closing old session")
                await self._close_session_internal(session_id)

        payload = {
            "action": "create_session",
            "session": session_id,
            "session_type": "bash",
        }
        result = await self._invoke_function(payload, session_id=session_id)

        self.sessions[session_id] = SessionState(
            session_id=session_id,
            ps1=ps1,
        )
        logger.info(f"Session {session_id} created successfully")
        return result

    async def execute_command(
        self,
        session_id: str,
        command: str,
        timeout: float = 60.0,
    ) -> tuple[str, int]:
        """Execute a command in an existing session via InvokeFunction.

        Args:
            session_id: Target session ID.
            command: Command to execute.
            timeout: Command timeout in seconds.

        Returns:
            Tuple of (output, exit_code).
        """
        if session_id not in self.sessions:
            raise ValueError(f"Session {session_id} not found")

        payload = {
            "action": "run_in_session",
            "session": session_id,
            "action_type": "bash",
            "command": command,
            "timeout": timeout,
        }
        result = await self._invoke_function(payload, session_id=session_id, timeout=timeout)
        self.sessions[session_id].touch()
        return result.get("output", ""), result.get("exit_code", 0)

    async def _close_session_internal(self, session_id: str) -> None:
        """Internal method to close a session without acquiring lock.

        Args:
            session_id: Session to close.
        """
        state = self.sessions.pop(session_id, None)
        if state is None:
            return

        payload = {
            "action": "close_session",
            "session": session_id,
            "session_type": "bash",
        }
        try:
            await self._invoke_function(payload, session_id=session_id, max_retries=1)
        except Exception as e:
            logger.warning(f"Error closing session {session_id}: {e}")
        logger.info(f"Session {session_id} closed")

    async def close_session(self, session_id: str) -> None:
        """Close a session.

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
            True if session exists in local tracking.
        """
        return session_id in self.sessions

    async def get_session_stats(self, session_id: str) -> dict | None:
        """Get statistics for a session.

        Args:
            session_id: Session to query.

        Returns:
            Session stats dict or None if session not found.
        """
        state = self.sessions.get(session_id)
        if state is None:
            return None
        return {
            "session_id": state.session_id,
            "created_at": state.created_at,
            "last_activity": state.last_activity,
            "is_alive": True,
        }


# =============================================================================
# FC Runtime
# =============================================================================


class FCRuntime(AbstractSandbox):
    """Function Compute (FC) Runtime implementation using SDK InvokeFunction.

    Provides stateful bash sessions on FC function instances,
    with semantics compatible with Docker Runtime (cd, export, nohup work correctly).

    Features:
    - Automatic retry with exponential backoff for InvokeFunction calls
    - Circuit breaker for fault tolerance
    - Session affinity via x-rock-session-id header
    """

    def __init__(self, config: "FCOperatorConfig", fc_client: Any = None):
        from rock.sandbox.operator.fc.config import FCOperatorConfig

        self.config: FCOperatorConfig = config
        self.session_manager = FCSessionManager(config, fc_client=fc_client)
        self._started = False

    async def is_alive(self, *, timeout: float | None = None) -> IsAliveResponse:
        """Check if the FC runtime is alive."""
        try:
            payload = {"action": "is_alive"}
            result = await self.session_manager._invoke_function(payload, max_retries=1)
            return IsAliveResponse(is_alive=result.get("is_alive", False))
        except Exception as e:
            logger.warning(f"FC health check failed: {e}")
            return IsAliveResponse(is_alive=False)

    async def create_session(self, request: CreateSessionRequest) -> CreateSessionResponse:
        """Create a new bash session."""
        result = await self.session_manager.create_session(
            session_id=request.session,
            ps1=getattr(request, "ps1", "root@fc:~$ "),
        )
        return CreateSessionResponse(
            output=result.get("output", result.get("ps1", "root@fc:~$ ")),
        )

    async def run_in_session(self, action: Action) -> Observation:
        """Execute an action within an existing session."""
        from rock.actions import BashObservation

        output, exit_code = await self.session_manager.execute_command(
            session_id=action.session,
            command=action.command,
            timeout=getattr(action, "timeout", 60.0),
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
        """Execute a one-time command via InvokeFunction (stateless, no session).

        Raises:
            FCRuntimeError: If the command execution fails due to RPC/network errors.
            Note: Command failure (non-zero exit code) returns normally with exit_code != 0.
        """
        payload = {
            "action": "execute",
            "command": command.command,
            "cwd": getattr(command, "cwd", "/tmp"),
            "env": getattr(command, "env", {}),
            "timeout": getattr(command, "timeout", 60),
            "shell": getattr(command, "shell", True),
        }

        result = await self.session_manager._invoke_function(payload)
        return CommandResponse(
            stdout=result.get("stdout", ""),
            stderr=result.get("stderr", ""),
            exit_code=result.get("exit_code", 0),
        )

    async def read_file(self, request: ReadFileRequest) -> ReadFileResponse:
        """Read file content from the FC environment."""
        payload = {
            "action": "read_file",
            "path": request.path,
            "encoding": getattr(request, "encoding", "utf-8"),
        }

        result = await self.session_manager._invoke_function(payload, session_id=self.config.session_id)

        if "error" in result:
            raise RuntimeError(result["error"])

        return ReadFileResponse(content=result.get("content", ""))

    async def write_file(self, request: WriteFileRequest) -> WriteFileResponse:
        """Write content to a file in the FC environment."""
        payload = {
            "action": "write_file",
            "path": request.path,
            "content": request.content,
            "encoding": getattr(request, "encoding", "utf-8"),
        }

        result = await self.session_manager._invoke_function(payload, session_id=self.config.session_id)

        if "error" in result:
            raise RuntimeError(result["error"])

        return WriteFileResponse(success=True)

    async def upload(self, request: UploadRequest) -> UploadResponse:
        """Upload a file to the FC environment.

        Reads the file from source_path locally and uploads it to the FC function
        via InvokeFunction with base64-encoded content.
        """
        import pathlib

        source = pathlib.Path(request.source_path)
        if not source.exists():
            raise FileNotFoundError(f"Source file not found: {request.source_path}")

        content = source.read_bytes()
        content_b64 = base64.b64encode(content).decode("utf-8")

        payload = {
            "action": "upload",
            "path": request.target_path,
            "content": content_b64,
            "encoding": "base64",
        }

        result = await self.session_manager._invoke_function(payload, session_id=self.config.session_id)

        if "error" in result:
            raise RuntimeError(result["error"])

        return UploadResponse(success=True, file_name=request.target_path)

    async def close(self) -> CloseResponse:
        """Close the runtime and cleanup resources."""
        await self.session_manager.close_all_sessions()
        self._started = False
        return CloseResponse()
