"""Linux/macOS Rocklet for the local sandbox runtime.

Hosts the BashSession implementation (built on pexpect + bashlex) and the
LinuxRocklet that the central dispatcher returns for sys.platform in
{'linux', 'darwin'}.
"""

import asyncio
import os
import re
import subprocess
import time
from copy import deepcopy

import bashlex
import bashlex.ast
import pexpect
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
    BashIncorrectSyntaxError,
    CommandTimeoutError,
    NoExitCodeError,
    NonZeroExitCodeError,
    SessionNotInitializedError,
)
from rock.utils import get_executor

from .rocklet import Rocklet, Session

logger = init_logger(__name__)


def _strip_control_chars(s: str) -> str:
    ansi_escape = re.compile(r"\x1B[@-_][0-?]*[ -/]*[@-~]")
    return ansi_escape.sub("", s)


def _split_bash_command(inpt: str) -> list[str]:
    r"""Split a bash command with linebreaks, escaped newlines, and heredocs into a list of
    individual commands.

    Args:
        inpt: The input string to split into commands.
    Returns:
        A list of commands as strings.

    Examples:

    "cmd1\ncmd2" are two commands
    "cmd1\\\n asdf" is one command (because the linebreak is escaped)
    "cmd1<<EOF\na\nb\nEOF" is one command (because of the heredoc)
    """
    inpt = inpt.strip()
    if not inpt or all(l.strip().startswith("#") for l in inpt.splitlines()):
        # bashlex can't deal with empty strings or the like :/
        return []
    parsed = bashlex.parse(inpt)
    cmd_strings = []

    def find_range(cmd: bashlex.ast.node) -> tuple[int, int]:
        start = cmd.pos[0]  # type: ignore
        end = cmd.pos[1]  # type: ignore
        for part in getattr(cmd, "parts", []):
            part_start, part_end = find_range(part)
            start = min(start, part_start)
            end = max(end, part_end)
        return start, end

    for cmd in parsed:
        start, end = find_range(cmd)
        cmd_strings.append(inpt[start:end])
    return cmd_strings


def _check_bash_command(command: str) -> None:
    """Check if a bash command is valid. Raises BashIncorrectSyntaxError if it's not."""
    _unique_string = "SOUNIQUEEOF"
    cmd = f"/bin/bash -n << '{_unique_string}'\n{command}\n{_unique_string}"
    result = subprocess.run(cmd, shell=True, capture_output=True)
    if result.returncode == 0:
        return
    stdout = result.stdout.decode(errors="backslashreplace")
    stderr = result.stderr.decode(errors="backslashreplace")
    msg = (
        f"Error (exit code {result.returncode}) while checking bash command \n{command!r}\n"
        f"---- Stderr ----\n{stderr}\n---- Stdout ----\n{stdout}"
    )
    exc = BashIncorrectSyntaxError(msg, extra_info={"bash_stdout": stdout, "bash_stderr": stderr})
    raise exc


class BashSession(Session):
    _UNIQUE_STRING = "UNIQUESTRING29234"

    def __init__(self, request: CreateBashSessionRequest):
        """This basically represents one REPL that we control.

        It's pretty similar to a `pexpect.REPLWrapper`.
        """
        self.request = request
        self._ps1 = "SHELLPS1PREFIX"
        self._shell: pexpect.spawn | None = None
        self._executor = get_executor()
        self._system_user = "root"

    @property
    def shell(self) -> pexpect.spawn:
        if self._shell is None:
            msg = "shell not initialized"
            raise RuntimeError(msg)
        return self._shell

    def _get_reset_commands(self) -> list[str]:
        """Commands to reset the PS1, PS2, and PS0 variables to their default values."""
        return [
            "unset PROMPT_COMMAND",
            f"export PS1='{self._ps1}'",
            "export PS2=''",
            "export PS0=''",
        ]

    async def start(self) -> CreateBashSessionResponse:
        """Spawn the session, source any startupfiles and set the PS1."""
        if self.request.env_enable:
            env = os.environ.copy()
        else:
            env = {}
        env.update({"PS1": self._ps1, "PS2": "", "PS0": ""})
        if self.request.env is not None:
            env.update(self.request.env)
        logger.info(f"env:{env}")
        command = "/bin/bash"
        if self.request.remote_user is not None and self.request.remote_user != self._system_user:
            command = f"su {self.request.remote_user} -c {command}"
        self._shell = pexpect.spawn(
            command,
            encoding="utf-8",
            codec_errors="backslashreplace",
            echo=False,
            env=env,  # type: ignore
            maxread=self.request.max_read_size,
        )
        time.sleep(0.3)
        cmds = []
        if self.request.startup_source:
            cmds += [f"source {path}" for path in self.request.startup_source] + ["sleep 0.3"]
        cmds += self._get_reset_commands()
        cmd = " ; ".join(cmds)
        self.shell.sendline(cmd)
        self.shell.expect(self._ps1, timeout=self.request.startup_timeout)
        output = _strip_control_chars(self.shell.before)  # type: ignore
        self.refresh_shell()
        return CreateBashSessionResponse(output=output)

    def _eat_following_output(self, timeout: float = 0.5) -> str:
        """Return all output that happens in the next `timeout` seconds."""
        time.sleep(timeout)
        try:
            output = self.shell.read_nonblocking(timeout=0.1)
        except pexpect.TIMEOUT:
            return ""
        return _strip_control_chars(output)

    async def run(self, action: BashAction) -> BashObservation:
        """Run a bash action.

        Raises:
            SessionNotInitializedError: If the shell is not initialized.
            CommandTimeoutError: If the command times out.
            NonZeroExitCodeError: If the command has a non-zero exit code and `action.check` is True.
            NoExitCodeError: If we cannot get the exit code of the command.

        Returns:
            BashObservation: The observation of the command.
        """
        if self.shell is None:
            msg = "shell not initialized"
            raise SessionNotInitializedError(msg)
        if action.is_interactive_command or action.is_interactive_quit:
            return await self._aync_run_interactive(action)
        r = await self._async_run_normal(action)
        if action.check == "raise" and r.exit_code != 0:
            msg = f"Command {action.command!r} failed with exit code {r.exit_code}. Here is the output:\n{r.output!r}"
            if action.error_msg:
                msg = f"{action.error_msg}: {msg}"
            raise NonZeroExitCodeError(msg)
        return r

    async def _aync_run_interactive(self, action: BashAction) -> BashObservation:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._run_interactive, action)

    def _run_interactive(self, action: BashAction) -> BashObservation:
        """Run an interactive action. This is different because we don't seek to
        the PS1 and don't attempt to get the exit code.
        """
        assert self.shell is not None
        self.shell.sendline(action.command)
        expect_strings = action.expect + [self._ps1]
        try:
            self.shell.expect(expect_strings, timeout=action.timeout)  # type: ignore
            # matched_expect_string = expect_strings[expect_index]
        except pexpect.TIMEOUT as e:
            msg = f"timeout after {action.timeout} seconds while running command {action.command!r}"
            raise CommandTimeoutError(msg) from e
        finally:
            self.refresh_shell()
        output: str = _strip_control_chars(self.shell.before).strip()  # type: ignore
        if action.is_interactive_quit:
            assert not action.is_interactive_command
            self.shell.setecho(False)
            self.shell.waitnoecho()
            self.shell.sendline(f"stty -echo; echo '{self._UNIQUE_STRING}'")
            # Might need two expects for some reason
            self.shell.expect(self._UNIQUE_STRING, timeout=1)
            self.shell.expect(self._ps1, timeout=1)
        else:
            # Interactive command.
            # For some reason, this often times enables echo mode within the shell.
            output = output.lstrip().removeprefix(action.command).strip()
        return BashObservation(output=output, exit_code=0, expect_string=self.shell.after)

    def refresh_shell(self):
        logger.debug(f"before refresh before_content: {self.shell._before.getvalue()}")
        logger.debug(f"before refresh buffer_content: {self.shell._buffer.getvalue()}")
        self.shell._before.seek(0)
        self.shell._before.truncate(0)
        self.shell._buffer.seek(0)
        self.shell._buffer.truncate(0)
        logger.debug(f"after refresh before_content: {self.shell._before.getvalue()}")
        logger.debug(f"after refresh buffer_content: {self.shell._buffer.getvalue()}")

    async def _async_run_normal(self, action: BashAction) -> BashObservation:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._run_normal, action)

    def _run_normal(self, action: BashAction) -> BashObservation:
        """Run a normal action. This is the default mode.

        There are three steps to this:

        1. Check if the command is valid
        2. Execute the command
        3. Get the exit code
        """
        action = deepcopy(action)

        assert self.shell is not None
        _check_bash_command(action.command)

        # Part 2: Execute the command

        fallback_terminator = False
        # Running multiple interactive commands by sending them with linebreaks would break things
        # because we get multiple PS1s back to back. Instead we just join them with ;
        # However, sometimes bashlex errors and we can't do this. In this case
        # we add a unique string to the end of the command and then seek to that
        # (which is also somewhat brittle, so we don't do this by default).
        try:
            individual_commands = _split_bash_command(action.command)
        except Exception as e:
            # Bashlex is very buggy and can throw a variety of errors, including
            # ParsingErrors, NotImplementedErrors, TypeErrors, possibly more. So we catch them all
            logger.error("Bashlex fail: %s", e)
            action.command += f"\n TMPEXITCODE=$? ; sleep 0.1; echo '{self._UNIQUE_STRING}' ; (exit $TMPEXITCODE)"
            fallback_terminator = True
        else:
            action.command = " ; ".join(individual_commands)
        self.shell.sendline(action.command)
        if not fallback_terminator:
            expect_strings = action.expect + [self._ps1]
        else:
            expect_strings = [self._UNIQUE_STRING]
        try:
            expect_index = self.shell.expect(expect_strings, timeout=action.timeout)  # type: ignore
            matched_expect_string = expect_strings[expect_index]
        except pexpect.TIMEOUT as e:
            msg = f"timeout after {action.timeout} seconds while running command {action.command!r}"
            raise CommandTimeoutError(msg) from e
        finally:
            self.refresh_shell()
        output: str = _strip_control_chars(self.shell.before).strip()  # type: ignore

        # Part 3: Get the exit code
        if action.check == "ignore":
            return BashObservation(output=output, exit_code=None, expect_string=matched_expect_string)

        try:
            _exit_code_prefix = "EXITCODESTART"
            _exit_code_suffix = "EXITCODEEND"
            self.shell.sendline(f"\necho {_exit_code_prefix}$?{_exit_code_suffix}")
            try:
                self.shell.expect(_exit_code_suffix, timeout=1)
            except pexpect.TIMEOUT:
                msg = "timeout while getting exit code"
                raise NoExitCodeError(msg)
            exit_code_raw: str = _strip_control_chars(self.shell.before).strip()  # type: ignore
            exit_code = re.findall(f"{_exit_code_prefix}([0-9]+)", exit_code_raw)
            if len(exit_code) != 1:
                msg = f"failed to parse exit code from output {exit_code_raw!r} (command: {action.command!r}, matches: {exit_code})"
                raise NoExitCodeError(msg)
            output += exit_code_raw.split(_exit_code_prefix)[0]
            exit_code = int(exit_code[0])
            # We get at least one more PS1 here.
            try:
                self.shell.expect(self._ps1, timeout=0.1)
            except pexpect.TIMEOUT:
                msg = "Timeout while getting PS1 after exit code extraction"
                raise CommandTimeoutError(msg)
            output = output.replace(self._UNIQUE_STRING, "").replace(self._ps1, "")
        except Exception:
            # Ignore all exceptions if check == 'silent'
            if action.check == "raise":
                raise
            exit_code = None
        finally:
            self.refresh_shell()
        return BashObservation(output=output, exit_code=exit_code, expect_string=matched_expect_string)

    async def close(self) -> CloseSessionResponse:
        if self._shell is None:
            return CloseBashSessionResponse()
        self.shell.close()
        self._shell = None
        return CloseBashSessionResponse()

    def interact(self) -> None:
        """Enter interactive mode."""
        self.shell.interact()


class LinuxRocklet(Rocklet):
    """Rocklet implementation for sys.platform in {'linux', 'darwin'}."""

    def _build_bash_session(self, request: CreateBashSessionRequest) -> Session:
        return BashSession(request)

    async def get_statistics(self) -> dict:
        return {
            "cpu": psutil.cpu_percent(),
            "mem": psutil.virtual_memory().percent,
            "disk": psutil.disk_usage("/").percent,
            "net": psutil.net_io_counters().bytes_recv + psutil.net_io_counters().bytes_sent,
        }
