"""Windows Rocklet for the local sandbox runtime.

Hosts the PowerShellSession implementation (built on subprocess + a background
reader thread) and the WindowsRocklet that the central dispatcher returns for
sys.platform == 'win32'.
"""

import asyncio
import os
import queue as queue_module
import re
import shutil
import subprocess
import threading
import time

import psutil

from rock.actions import (
    BashObservation,
    CloseBashSessionResponse,
    CloseSessionResponse,
    CreateBashSessionResponse,
)
from rock.admin.proto.request import SandboxBashAction as BashAction
from rock.admin.proto.request import SandboxCreateBashSessionRequest as CreateBashSessionRequest
from rock.logger import init_logger
from rock.rocklet.exceptions import (
    CommandTimeoutError,
    NonZeroExitCodeError,
    PowerShellNotFoundError,
    SessionNotInitializedError,
)
from rock.utils import get_executor

from .rocklet import Rocklet, Session

logger = init_logger(__name__)


def _strip_control_chars(s: str) -> str:
    ansi_escape = re.compile(r"\x1B[@-_][0-?]*[ -/]*[@-~]")
    return ansi_escape.sub("", s)


class PowerShellSession(Session):
    """A session that runs PowerShell commands on Windows.

    Uses subprocess.Popen with a background reader thread to interact
    with PowerShell interactively via stdin/stdout pipes.
    Unique marker strings are used to delimit command output boundaries
    and extract exit codes.
    """

    _BEGIN_MARKER = "ROCKLET_PS_BEGIN_29234"
    _END_MARKER = "ROCKLET_PS_END_29234"
    _EXIT_MARKER = "ROCKLET_PS_EXIT_29234:"

    def __init__(self, request: CreateBashSessionRequest):
        self.request = request
        self._process: subprocess.Popen | None = None
        self._executor = get_executor()
        self._output_queue: queue_module.Queue = queue_module.Queue()
        self._reader_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _find_powershell() -> str:
        """Find the PowerShell executable. Prefers pwsh (PowerShell Core) over powershell."""
        for cmd in ["pwsh", "powershell"]:
            if shutil.which(cmd):
                return cmd
        raise PowerShellNotFoundError(
            "PowerShell executable not found. Install PowerShell Core ('pwsh') or ensure 'powershell.exe' is in PATH."
        )

    def _read_stdout(self) -> None:
        """Background thread that continuously reads stdout line by line."""
        try:
            assert self._process is not None and self._process.stdout is not None
            for line in iter(self._process.stdout.readline, ""):
                if not line:
                    break
                self._output_queue.put(line)
        except Exception:
            pass

    def _drain_queue(self, timeout: float = 0.1) -> str:
        """Drain all currently available lines from the output queue."""
        lines = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = self._output_queue.get(timeout=0.05)
                lines.append(line.rstrip("\r\n"))
            except queue_module.Empty:
                if lines:
                    break
                continue
        return "\n".join(lines)

    def _send_line(self, text: str) -> None:
        """Send a line of text to PowerShell's stdin."""
        assert self._process is not None and self._process.stdin is not None
        self._process.stdin.write(text + "\n")
        self._process.stdin.flush()

    async def start(self) -> CreateBashSessionResponse:
        """Start the PowerShell session."""
        env = os.environ.copy()
        if self.request.env is not None:
            env.update(self.request.env)

        ps_cmd = self._find_powershell()
        logger.info(f"Starting PowerShell session with: {ps_cmd}")

        # Force PowerShell to use UTF-8 for stdin/stdout encoding from the very
        # first byte. Required on non-UTF-8 Windows locales (e.g. zh-CN/GBK):
        # without this, PowerShell writes its output in the system code page
        # while subprocess.Popen below decodes as UTF-8, producing mojibake
        # (e.g. "目录" -> "Ŀ¼") that then crashes the GBK-encoded console
        # logger with UnicodeEncodeError.
        #
        # `-Command` runs the setup expression at startup, then `-NoExit` drops
        # PowerShell into interactive mode for stdin-driven commands. Doing this
        # at startup (instead of via _send_line after spawn) is essential because
        # PowerShell caches its [Console].Out TextWriter at process init — late
        # OutputEncoding mutations don't affect already-cached writers used by
        # built-in formatters (Get-ChildItem, Format-Table, etc.).
        ps_setup = (
            "chcp 65001 > $null; "
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "$OutputEncoding = [System.Text.Encoding]::UTF8"
        )
        self._process = subprocess.Popen(
            [ps_cmd, "-NoLogo", "-NoProfile", "-NoExit", "-Command", ps_setup],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )

        self._reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader_thread.start()

        # Wait for PowerShell to initialize
        time.sleep(0.5)

        # Drain startup output
        startup_output = self._drain_queue(timeout=0.5)
        logger.info(f"PowerShell session started, startup output: {startup_output[:200]}")
        return CreateBashSessionResponse(output=startup_output)

    async def run(self, action: BashAction) -> BashObservation:
        """Run a command in the PowerShell session.

        Raises:
            SessionNotInitializedError: If the PowerShell process is not running.
            CommandTimeoutError: If the command times out.
            NonZeroExitCodeError: If the command has a non-zero exit code and action.check is 'raise'.
        """
        if self._process is None or self._process.poll() is not None:
            raise SessionNotInitializedError("PowerShell session not initialized or has terminated")

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(self._executor, self._run_command, action)

        if action.check == "raise" and result.exit_code is not None and result.exit_code != 0:
            msg = (
                f"Command {action.command!r} failed with exit code {result.exit_code}. "
                f"Here is the output:\n{result.output!r}"
            )
            if hasattr(action, "error_msg") and action.error_msg:
                msg = f"{action.error_msg}: {msg}"
            raise NonZeroExitCodeError(msg)

        return result

    def _run_command(self, action: BashAction) -> BashObservation:
        """Execute a single command in the PowerShell session (blocking).

        Wraps the command with unique markers to reliably detect output boundaries
        and extract the exit code ($LASTEXITCODE).
        """
        with self._lock:
            # Drain any leftover output from previous commands
            self._drain_queue(timeout=0.1)

            # Send command wrapped with markers
            self._send_line(f"Write-Host '{self._BEGIN_MARKER}'")
            # Send command lines
            for line in action.command.splitlines():
                self._send_line(line)
            # Capture exit code: use $LASTEXITCODE for native commands,
            # but also check $? for PowerShell cmdlet errors
            self._send_line(
                f"if ($LASTEXITCODE -ne $null -and $LASTEXITCODE -ne 0) "
                f"{{ Write-Host '{self._EXIT_MARKER}'$LASTEXITCODE }} "
                f"elseif (-not $?) {{ Write-Host '{self._EXIT_MARKER}1' }} "
                f"else {{ Write-Host '{self._EXIT_MARKER}0' }}"
            )
            self._send_line(f"Write-Host '{self._END_MARKER}'")

            # Collect output until end marker
            output_lines: list[str] = []
            started = False
            exit_code: int | None = None
            timeout = action.timeout if action.timeout is not None else 1200
            deadline = time.time() + timeout

            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise CommandTimeoutError(
                        f"timeout after {timeout} seconds while running command {action.command!r}"
                    )
                try:
                    line = self._output_queue.get(timeout=min(remaining, 1.0))
                    line = line.rstrip("\r\n")
                except queue_module.Empty:
                    # Check if process is still alive
                    if self._process is not None and self._process.poll() is not None:
                        raise SessionNotInitializedError("PowerShell process terminated unexpectedly")
                    continue

                if self._BEGIN_MARKER in line:
                    started = True
                    continue
                if self._EXIT_MARKER in line:
                    after_marker = line.split(self._EXIT_MARKER)[-1].strip()
                    try:
                        exit_code = int(after_marker) if after_marker else 0
                    except ValueError:
                        exit_code = 0
                    continue
                if self._END_MARKER in line:
                    break
                if started:
                    output_lines.append(line)

            output = "\n".join(output_lines).strip()
            output = _strip_control_chars(output)

            if action.check == "ignore":
                return BashObservation(output=output, exit_code=None)

            return BashObservation(output=output, exit_code=exit_code)

    async def close(self) -> CloseSessionResponse:
        """Close the PowerShell session."""
        if self._process is not None:
            try:
                self._send_line("exit")
            except Exception:
                pass
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None
        return CloseBashSessionResponse()

    def interact(self) -> None:
        """Interactive mode is not supported for PowerShell sessions."""
        raise NotImplementedError("Interactive mode is not supported for PowerShell sessions")


class WindowsRocklet(Rocklet):
    """Rocklet implementation for sys.platform == 'win32'."""

    def _build_bash_session(self, request: CreateBashSessionRequest) -> Session:
        return PowerShellSession(request)

    async def get_statistics(self) -> dict:
        return {
            "cpu": psutil.cpu_percent(),
            "mem": psutil.virtual_memory().percent,
            "disk": psutil.disk_usage("C:\\").percent,
            "net": psutil.net_io_counters().bytes_recv + psutil.net_io_counters().bytes_sent,
        }
