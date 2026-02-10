import asyncio
import logging
import mimetypes
import os
import time
import uuid
import warnings
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

import oss2
from httpx import ReadTimeout
from typing_extensions import deprecated

from rock import env_vars
from rock.actions import (
    AbstractSandbox,
    Action,
    BashAction,
    CloseResponse,
    CloseSessionRequest,
    CloseSessionResponse,
    Command,
    CommandResponse,
    CreateBashSessionRequest,
    CreateBashSessionResponse,
    ExecuteBashSessionResponse,
    IsAliveResponse,
    Observation,
    OssSetupResponse,
    ReadFileRequest,
    ReadFileResponse,
    SandboxResponse,
    SandboxStatusResponse,
    UploadRequest,
    UploadResponse,
    WriteFileRequest,
    WriteFileResponse,
)
from rock.common.constants import PID_PREFIX, PID_SUFFIX
from rock.sdk.common.constants import RunModeType
from rock.sdk.common.exceptions import (
    BadRequestRockError,
    InternalServerRockError,
    InvalidParameterRockException,
    raise_for_code,
)
from rock.sdk.sandbox.agent.rock_agent import RockAgent
from rock.sdk.sandbox.config import SandboxConfig, SandboxGroupConfig
from rock.sdk.sandbox.deploy import Deploy
from rock.sdk.sandbox.file_system import FileSystem, LinuxFileSystem
from rock.sdk.sandbox.model_service.base import ModelService
from rock.sdk.sandbox.network import Network
from rock.sdk.sandbox.process import Process
from rock.sdk.sandbox.remote_user import LinuxRemoteUser, RemoteUser
from rock.sdk.sandbox.runtime_env.base import RuntimeEnv, RuntimeEnvId
from rock.utils import HttpUtils, extract_nohup_pid, retry_async

logger = logging.getLogger(__name__)


class RunMode(str, Enum):
    NORMAL = "normal"
    NOHUP = "nohup"


class Sandbox(AbstractSandbox):
    config: SandboxConfig
    _url: str
    _route_key: str
    _sandbox_id: str | None = None
    _host_name: str | None = None
    _host_ip: str | None = None
    _oss_bucket: oss2.Bucket | None = None
    _cluster: str | None = None
    agent: RockAgent | None = None
    model_service: ModelService | None = None
    remote_user: RemoteUser | None = None
    process: Process | None = None
    network: Network | None = None
    fs: FileSystem | None = None
    runtime_envs: dict[RuntimeEnvId, RuntimeEnv]
    deploy: Deploy | None = None

    def __init__(self, config: SandboxConfig):
        self._pod_name = None
        self._ip = None
        self.config = config
        endpoint = self.config.base_url
        self._url = f"{endpoint}/apis/envs/sandbox/v1"
        if not self.config.route_key:
            self._route_key = uuid.uuid4().hex
        else:
            self._route_key = self.config.route_key

        self._oss_token_expire_time = self._generate_utc_iso_time()
        self._cluster = self.config.cluster
        self.remote_user = LinuxRemoteUser(self)
        self.process = Process(self)
        self.network = Network(self)
        self.fs = LinuxFileSystem(self)
        self.runtime_envs = {}
        self.deploy = Deploy(self)
        self.agent = RockAgent(self)

    @property
    def sandbox_id(self) -> str:
        return self._sandbox_id

    @property
    def host_name(self) -> str:
        return self._host_name

    @property
    def host_ip(self) -> str:
        return self._host_ip

    @property
    def cluster(self) -> str:
        return self._cluster

    @property
    def url(self) -> str:
        return self._url

    @url.setter
    def url(self, value: str):
        self._url = value

    def _build_headers(self) -> dict[str, str]:
        """Build basic request headers."""
        headers = {
            "ROUTE-KEY": self._route_key,
            "X-Cluster": self._cluster,
        }

        # Add authentication header
        if self.config.xrl_authorization:
            warnings.warn(
                "XRL-Authorization is deprecated, use extra_headers instead", category=DeprecationWarning, stacklevel=2
            )
            headers["XRL-Authorization"] = f"Bearer {self.config.xrl_authorization}"

        # Add extra headers from config
        if self.config.extra_headers:
            headers.update(self.config.extra_headers)

        self._add_user_defined_tag_into_headers(headers)

        return headers

    async def _parse_error_message_from_status(self, status: dict):
        # Traverse each stage in the status dictionary
        for stage, details in status.items():
            # Check if the status of the current stage is "failed" or "timeout"
            if details.get("status") == "failed" or details.get("status") == "timeout":
                # Return error message, including stage name and specific error message
                return f"{stage}: {details.get('message', 'No message provided')}"
        # If no failed stage is found, return None
        return None

    async def start(self):
        url = f"{self._url}/start_async"
        headers = self._build_headers()
        data = {
            "image": self.config.image,
            "auto_clear_time": self.config.auto_clear_seconds / 60,
            "auto_clear_time_minutes": self.config.auto_clear_seconds / 60,
            "startup_timeout": self.config.startup_timeout,
            "memory": self.config.memory,
            "cpus": self.config.cpus,
        }
        try:
            response = await HttpUtils.post(url, headers, data)
        except Exception as e:
            raise Exception(f"Failed to start standbox: {str(e)}, post url {url}")

        logging.debug(f"Start sandbox response: {response}")
        if "Success" != response.get("status"):
            result = response.get("result", None)
            if result is not None:
                rock_response = SandboxResponse(**result)
                raise_for_code(rock_response.code, f"Failed to start container: {response}")
            raise Exception(f"Failed to start sandbox: {response}")
        self._sandbox_id = response.get("result").get("sandbox_id")
        self._host_name = response.get("result").get("host_name")
        self._host_ip = response.get("result").get("host_ip")

        start_time = time.time()
        while time.time() - start_time < self.config.startup_timeout:
            sandbox_info = await self.get_status()
            logging.debug(f"Get status response: {sandbox_info}")
            if sandbox_info.is_alive:
                return
            error_msg = await self._parse_error_message_from_status(sandbox_info.status)
            if error_msg:
                raise InternalServerRockError(f"Failed to start sandbox because {error_msg}, sandbox: {str(self)}")
            await asyncio.sleep(3)
        raise InternalServerRockError(
            f"Failed to start sandbox within {self.config.startup_timeout}s, sandbox: {str(self)}"
        )

    async def is_alive(self) -> IsAliveResponse:
        try:
            status_response = await self.get_status()
            is_alive = status_response.is_alive
            message = status_response.host_name
            return IsAliveResponse(is_alive=is_alive, message=message)
        except Exception as e:
            logging.warning(f"Failed to get is alive, {str(e)}")
            raise Exception(f"Failed to get is alive: {str(e)}")

    async def get_status(self) -> SandboxStatusResponse:
        url = f"{self._url}/get_status?sandbox_id={self.sandbox_id}"
        headers = self._build_headers()
        response = await HttpUtils.get(url, headers)
        logging.debug(f"Get status response: {response}")
        if "Success" != response.get("status"):
            raise Exception(f"Failed to get status: {response}")
        result: dict = response.get("result")  # type: ignore
        return SandboxStatusResponse(**result)

    async def execute(self, command: Command) -> CommandResponse:
        url = f"{self._url}/execute"
        headers = self._build_headers()
        data = {
            "command": command.command,
            "sandbox_id": self.sandbox_id,
            "timeout": command.timeout,
            "cwd": command.cwd,
            "env": command.env,
        }
        try:
            response = await HttpUtils.post(url, headers, data)
        except Exception as e:
            raise Exception(f"Failed to execute command {data}: {str(e)}, post url {url}")

        logging.debug(f"Execute command response: {response}")
        if "Success" != response.get("status"):
            logging.info(f"Failed to execute command {data}, response: {response}")
            raise Exception(f"Failed to execute command {data}, response: {response}")
        result: dict = response.get("result")  # type: ignore
        return CommandResponse(**result)

    async def stop(self):
        if not self.sandbox_id:
            return
        try:
            url = f"{self._url}/stop"
            headers = self._build_headers()
            data = {
                "sandbox_id": self.sandbox_id,
            }
            await HttpUtils.post(url, headers, data)
        except Exception as e:
            logging.warning(f"Failed to stop sandbox, IGNORE: {e}")

    async def commit(self, image_tag: str, username: str, password: str):
        if not self.sandbox_id:
            return

        url = f"{self._url}/commit"
        headers = self._build_headers()
        data = {
            "sandbox_id": self.sandbox_id,
            "image_tag": image_tag,
            "username": username,
            "password": password,
        }
        response = await HttpUtils.post(url, headers, data)
        logging.debug(f"Commit sandbox response: {response}")
        if "Success" != response.get("status"):
            raise Exception(f"Failed to execute command: {response}")
        result: dict = response.get("result")
        return CommandResponse(**result)

    async def create_session(self, create_session_request: CreateBashSessionRequest) -> CreateBashSessionResponse:
        url = f"{self._url}/create_session"
        headers = self._build_headers()
        data = {
            "sandbox_id": self.sandbox_id,
            **create_session_request.model_dump(),
        }
        try:
            response = await HttpUtils.post(url, headers, data)
        except Exception as e:
            raise Exception(f"Failed to create session: {str(e)}, post url {url}")

        logging.debug(f"Create session response: {response}")
        if "Success" != response.get("status"):
            raise Exception(f"Failed to execute command: {response}")
        result: dict = response.get("result")  # type: ignore
        return CreateBashSessionResponse(**result)

    @deprecated("Use arun instead")
    async def run_in_session(self, action: Action) -> Observation:
        return await self._run_in_session(action)

    async def _run_in_session(self, action: Action) -> Observation:
        url = f"{self._url}/run_in_session"
        headers = self._build_headers()
        data = {
            "action_type": "bash",
            "session": action.session,
            "command": action.command,
            "sandbox_id": self.sandbox_id,
            "check": action.check,
            "timeout": action.timeout,
        }
        try:
            response = await HttpUtils.post(url, headers, data, action.timeout)
        except ReadTimeout:
            raise ReadTimeout(
                f"Command execution failed due to timeout: '{action.command}' in {action.timeout} seconds"
            )
        except Exception as e:
            raise Exception(f"Failed to run in session: {str(e)}, post url {url}")

        logging.debug(f"Run in session response: {response}")
        if "Success" != response.get("status"):
            raise Exception(f"Failed to execute command: {response}")
        result: dict = response.get("result")  # type: ignore
        return Observation(**result)

    @deprecated("Use arun instead")
    async def run_nohup_and_wait(
        self, cmd: str, redirect_file_path: str = "/dev/null", wait_timeout: int = 300, wait_interval: int = 10
    ) -> ExecuteBashSessionResponse:
        timestamp = str(time.time_ns())
        temp_session = f"bash-{timestamp}"

        try:
            # Create session
            await self.create_session(CreateBashSessionRequest(session=temp_session))

            # Build and execute nohup command
            nohup_command = (
                f"nohup {cmd} < /dev/null > {redirect_file_path} 2>&1 & echo {PID_PREFIX}${{!}}{PID_SUFFIX};disown"
            )
            # todo:
            # Theoretically, the nohup command should return in a very short time, but the total time online is longer,
            # so time_out is set larger to avoid affecting online usage. It will be reduced after optimizing the read cluster time.
            action = BashAction(command=nohup_command, session=temp_session, timeout=30)
            response: Observation = await self._run_in_session(action)
            if response.exit_code != 0:
                return response

            # Parse
            pid = extract_nohup_pid(response.output)
            logging.info(f"sandbox {self.sandbox_id} cmd {cmd} pid {pid}")
            if not pid:
                return ExecuteBashSessionResponse(
                    success=False, message=f"Failed to extract PID from output: {response.output}"
                )

            # Wait for process completion
            success, message = await self.wait_for_process_completion(
                pid=pid, session=temp_session, wait_timeout=wait_timeout, wait_interval=wait_interval
            )

            return ExecuteBashSessionResponse(success=success, message=message)

        except Exception as e:
            error_msg = f"Failed to execute nohup command '{cmd}': {str(e)}"
            return ExecuteBashSessionResponse(success=False, message=error_msg)

    async def arun(
        self,
        cmd: str,
        session: str = None,
        wait_timeout=300,
        wait_interval=10,
        mode: RunModeType = RunMode.NORMAL,
        response_limited_bytes_in_nohup: int | None = None,
        ignore_output: bool = False,
        output_file: str | None = None,
    ) -> Observation:
        """
        Asynchronously run a command in the sandbox environment.
        This method supports two execution modes:
        - NORMAL: Execute command synchronously and wait for completion
        - NOHUP: Execute command in background using nohup, suitable for long-running tasks
        Args:
            cmd (str): The command to execute in the sandbox
            session (str, optional): The session identifier to run the command in.
                If None, a temporary session will be created for nohup mode. Defaults to None.
            wait_timeout (int, optional): Maximum time in seconds to wait for nohup command completion.
                Defaults to 300.
            wait_interval (int, optional): Interval in seconds between process completion checks for nohup mode.
                Minimum value is 5 seconds. Defaults to 10.
            mode (RunModeType, optional): Execution mode - either "normal" or "nohup".
                Defaults to RunMode.NORMAL.
            response_limited_bytes_in_nohup (int | None, optional): Maximum bytes to read from nohup output file.
                If None, reads entire output. Only applies to nohup mode. Defaults to None.
            nohup_command_timeout (int, optional): Timeout in seconds for the nohup command submission itself.
                Defaults to 60.
            ignore_output (bool, optional): Whether to ignore command output.If set to True, save the output content to 'output_file' and return it; if False, return the original content.
                Defaults to False.
            output_file (str, optional): The file path where the command output will be saved. Used in conjunction with the ignore_output field. '
                Defaults to None.
        Returns:
            Observation: Command execution result containing output, exit code, and failure reason if any.
                - For normal mode: Returns immediate execution result
                - For nohup mode: Returns result after process completion or timeout
        Raises:
            InvalidParameterRockException: If an unsupported run mode is provided
            ReadTimeout: If command execution times out (nohup mode)
            Exception: For other execution failures in nohup mode
        Examples:
            # Normal synchronous execution
            result = await sandbox.arun("ls -la")
            # Background execution with nohup
            result = await sandbox.arun(
                "python long_running_script.py",
                mode="nohup",
                wait_timeout=600
            )
            # Limited output reading in nohup mode
            result = await sandbox.arun(
                "generate_large_output.sh",
                mode="nohup",
                response_limited_bytes_in_nohup=1024
            )
        """
        if mode not in (RunMode.NORMAL, RunMode.NOHUP):
            raise InvalidParameterRockException(f"Unsupported arun mode: {mode}")

        if mode == RunMode.NORMAL:
            return await self._run_in_session(action=Action(command=cmd, session=session))
        if mode == RunMode.NOHUP:
            return await self._arun_with_nohup(
                cmd=cmd,
                session=session,
                wait_timeout=wait_timeout,
                wait_interval=wait_interval,
                response_limited_bytes_in_nohup=response_limited_bytes_in_nohup,
                ignore_output=ignore_output,
                output_file=output_file,
            )

    async def _arun_with_nohup(
        self,
        cmd: str,
        session: str | None,
        wait_timeout: int,
        wait_interval: int,
        response_limited_bytes_in_nohup: int | None,
        ignore_output: bool,
        output_file: str | None = None,
    ) -> Observation:
        """Execute command in nohup mode with process monitoring."""
        try:
            timestamp = str(time.time_ns())

            if session is None:
                temp_session = f"bash-{timestamp}"
                await self.create_session(CreateBashSessionRequest(session=temp_session))
                session = temp_session

            if output_file:
                dir_path, file_name = os.path.split(output_file)
                if file_name is None or not file_name.__contains__("."):
                    error_msg = f"Failed parse output file path: {output_file}"
                    raise BadRequestRockError(error_msg)
                dir_path = dir_path if dir_path else "."
                create_file_cmd = f"mkdir -p {dir_path}"
                response: Observation = await self._run_in_session(Action(command=create_file_cmd, session=session))
                if response.exit_code != 0:
                    error_msg = f"Failed mkdir for output file path: {output_file}, because {response.failure_reason}"
                    raise InternalServerRockError(error_msg)

            tmp_file = output_file if output_file else f"/tmp/tmp_{timestamp}.out"

            # Start nohup process and get PID
            pid, error_response = await self.start_nohup_process(cmd=cmd, tmp_file=tmp_file, session=session)

            # If nohup command itself failed, return the error response
            if error_response is not None:
                return error_response

            # If failed to extract PID
            if pid is None:
                msg = "Failed to submit command, nohup failed to extract PID"
                return Observation(output=msg, exit_code=1, failure_reason=msg)

            # Wait for process completion
            success, message = await self.wait_for_process_completion(
                pid=pid, session=session, wait_timeout=wait_timeout, wait_interval=wait_interval
            )

            # Handle output based on ignore_output flag
            return await self.handle_nohup_output(
                tmp_file=tmp_file,
                session=session,
                success=success,
                message=message,
                ignore_output=ignore_output,
                response_limited_bytes_in_nohup=response_limited_bytes_in_nohup,
            )

        except ReadTimeout:
            error_msg = f"Command execution failed due to timeout: '{cmd}'. This may be caused by an interactive command that requires user input."
            return Observation(output=error_msg, exit_code=1, failure_reason=error_msg)
        except Exception as e:
            error_msg = f"Failed to execute nohup command '{cmd}': {str(e)}"
            return Observation(output=error_msg, exit_code=1, failure_reason=error_msg)

    async def start_nohup_process(self, cmd: str, tmp_file: str, session: str) -> tuple[int | None, Observation | None]:
        """
        Start a nohup process and extract its PID.

        Args:
            cmd: Command to execute in nohup
            tmp_file: Output file path for nohup
            session: Bash session name

        Returns:
            Tuple of (PID, error_observation). If successful, returns (pid, None).
            If failed, returns (None, error_observation) or (None, None) if PID extraction failed.
        """
        nohup_command = f"nohup {cmd} < /dev/null > {tmp_file} 2>&1 & echo {PID_PREFIX}${{!}}{PID_SUFFIX};disown"

        # todo:
        # Theoretically, the nohup command should return in a very short time, but the total time online is longer,
        # so time_out is set larger to avoid affecting online usage. It will be reduced after optimizing the read cluster time.
        action = BashAction(command=nohup_command, session=session, timeout=30)
        response: Observation = await self._run_in_session(action)

        if response.exit_code != 0:
            return None, response

        pid = extract_nohup_pid(response.output)
        if not pid:
            return None, None

        return pid, None

    async def handle_nohup_output(
        self,
        tmp_file: str,
        session: str,
        success: bool,
        message: str,
        ignore_output: bool,
        response_limited_bytes_in_nohup: int | None,
    ) -> Observation:
        """
        Handle the output of a completed nohup process.

        Args:
            tmp_file: Path to the output file
            session: Bash session name
            success: Whether the process completed successfully
            message: Status message from process monitoring
            ignore_output: Whether to ignore the actual output content
            response_limited_bytes_in_nohup: Maximum bytes to read from output

        Returns:
            Observation containing the result
        """
        if ignore_output:
            # Get file size to help user decide how to read it
            file_size = None
            try:
                size_result: Observation = await self._run_in_session(
                    BashAction(session=session, command=f"stat -c %s {tmp_file} 2>/dev/null || stat -f %z {tmp_file}")
                )
                if size_result.exit_code == 0 and size_result.output.strip().isdigit():
                    file_size = int(size_result.output.strip())
            except Exception:
                # Best-effort; ignore file-size errors
                pass

            detached_msg = self._build_nohup_detached_message(tmp_file, success, message, file_size)
            if success:
                return Observation(output=detached_msg, exit_code=0)
            return Observation(output=detached_msg, exit_code=1, failure_reason=message)

        # Read output from file
        check_res_command = f"cat {tmp_file}"
        if response_limited_bytes_in_nohup:
            check_res_command = f"head -c {response_limited_bytes_in_nohup} {tmp_file}"

        exec_result: Observation = await self._run_in_session(BashAction(session=session, command=check_res_command))

        if success:
            return Observation(output=exec_result.output, exit_code=0)
        else:
            return Observation(output=exec_result.output, exit_code=1, failure_reason=message)

    async def write_file(self, request: WriteFileRequest) -> WriteFileResponse:
        content = request.content
        path = request.path
        return await self._do_write_file(content, path)

    async def write_file_by_path(self, content: str, path: str) -> WriteFileResponse:
        return await self._do_write_file(content, path)

    async def _do_write_file(self, content: str, path: str) -> WriteFileResponse:
        url = f"{self._url}/write_file"
        headers = self._build_headers()
        data = {
            "content": content,
            "path": path,
            "sandbox_id": self.sandbox_id,
        }
        response = await HttpUtils.post(url, headers, data)
        if "Success" != response.get("status"):
            return WriteFileResponse(success=False, message=f"Failed to write file {path}: upload response: {response}")
        return WriteFileResponse(success=True, message=f"Successfully write content to file {path}")

    async def wait_for_process_completion(
        self, pid: int, session: str, wait_timeout: int, wait_interval: int
    ) -> tuple[bool, str]:
        """
        Wait for process completion.

        Returns:
                tuple[bool, str]: (success status, message)
        """
        wait_interval = max(5, wait_interval)  # Minimum interval 5 seconds
        check_alive_cmd = f"kill -0 {pid}"
        check_alive_timeout = min(wait_interval * 2, wait_timeout)  # Not greater than wait_timeout

        start_time = time.perf_counter()
        end_time = start_time + wait_timeout
        consecutive_failures = 0
        max_consecutive_failures = 3

        while time.perf_counter() < end_time:
            try:
                # Check if process still exists
                await asyncio.wait_for(
                    self.run_in_session(BashAction(session=session, command=check_alive_cmd)),
                    timeout=check_alive_timeout,
                )

                # Process still exists, reset failure count
                consecutive_failures = 0
                elapsed = time.perf_counter() - start_time

            except asyncio.TimeoutError:
                # Check command timeout
                consecutive_failures += 1
                elapsed = time.perf_counter() - start_time

                if consecutive_failures >= max_consecutive_failures:
                    return False, f"Process check failed after {elapsed:.1f}s due to consecutive timeouts"

            except Exception:
                # Process does not exist or other error, consider process completed
                elapsed = time.perf_counter() - start_time
                return True, f"Process completed successfully in {elapsed:.1f}s"

            # Wait for next check
            await asyncio.sleep(wait_interval)

        # Timeout
        elapsed = time.perf_counter() - start_time
        timeout_msg = f"Process {pid} did not complete within {elapsed:.1f}s (timeout: {wait_timeout}s)"
        return False, timeout_msg

    def _build_nohup_detached_message(
        self, tmp_file: str, success: bool, detail: str | None, file_size: int | None = None
    ) -> str:
        status = "completed" if success else "finished with errors"
        lines = [
            "Command executed in nohup mode without streaming the log content.",
            f"Status: {status}",
            f"Output file: {tmp_file}",
        ]
        if file_size is not None:
            if file_size < 1024:
                size_str = f"{file_size} bytes"
            elif file_size < 1024 * 1024:
                size_str = f"{file_size / 1024:.2f} KB"
            else:
                size_str = f"{file_size / (1024 * 1024):.2f} MB"
            lines.append(f"File size: {size_str}")
        lines.append(
            f"Use Sandbox.read_file(...), download APIs, or run 'cat {tmp_file}' inside the session to inspect the result."
        )
        if detail:
            lines.append(f"Detail: {detail}")
        return "\n".join(lines)

    async def upload(self, request: UploadRequest) -> UploadResponse:
        return await self.upload_by_path(file_path=request.source_path, target_path=request.target_path)

    async def upload_by_path(self, file_path: str | Path, target_path: str) -> UploadResponse:
        path_str = file_path
        file_path = Path(file_path)
        if not file_path.exists():
            return UploadResponse(success=False, message=f"File not found: {file_path}")
        if env_vars.ROCK_OSS_ENABLE and os.path.getsize(file_path) > 1024 * 1024 * 1:
            return await self._upload_via_oss(path_str, target_path)
        url = f"{self._url}/upload"
        headers = self._build_headers()

        # Process file data
        if isinstance(file_path, str | Path):
            with open(file_path, "rb") as f:
                file_content = f.read()

            filename = file_path.name
            content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"

        else:
            return UploadResponse(success=False, message=f"Unsupported file input type: {type(file_path)}")

        data = {
            "target_path": target_path,
            "sandbox_id": self.sandbox_id,
        }

        files = {"file": (filename, file_content, content_type)}

        response = await HttpUtils.post_multipart(url, headers, data=data, files=files)
        logging.debug(f"Upload response: {response}")
        if "Success" != response.get("status"):
            return UploadResponse(success=False, message=f"Failed to execute command: upload response: {response}")
        else:
            return UploadResponse(success=True, message=f"Successfully uploaded file {filename} to {target_path}")

    async def read_file(self, request: ReadFileRequest) -> ReadFileResponse:
        url = f"{self._url}/read_file"
        headers = self._build_headers()
        data = {
            "path": request.path,
            "encoding": request.encoding,
            "errors": request.errors,
            "sandbox_id": self.sandbox_id,
        }
        response = await HttpUtils.post(url, headers, data)
        result: dict = response.get("result")
        return ReadFileResponse(content=result["content"])

    @deprecated(
        "The function cannot guarantee complete consistency with the original file content and may lose newline characters or other information."
    )
    async def read_file_by_line_range(
        self,
        file_path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        lines_per_request: int = 1000,
        session: str | None = None,
    ) -> ReadFileResponse:
        if session is not None:
            warnings.warn(
                "The 'session' parameter is deprecated and will be removed in a future version.",
                DeprecationWarning,
                stacklevel=2,
            )

        # Pre check
        if start_line is None:
            start_line = 1
        if start_line < 1:
            raise Exception(f"start_line({start_line}) must be positive")
        if end_line is not None and end_line < start_line:
            raise Exception(f"end_line({end_line}) must be greater than start_line({start_line})")
        if lines_per_request < 1 or lines_per_request > 10000:
            raise Exception(f"lines_per_request({lines_per_request}) must be between 1 and 10000")

        if end_line is None:

            @retry_async(max_attempts=3, delay_seconds=1.0)
            async def _count_lines() -> int:
                result = await self.execute(Command(command=["wc", "-l", file_path]))
                if result.exit_code != 0:
                    raise Exception(f"Failed to count lines of file {file_path}, wc result: {result}")
                return int(result.stdout.strip().split()[0])

            end_line = await _count_lines()
            logger.info(f"file {file_path} has {end_line} lines")

        @retry_async(max_attempts=3, delay_seconds=1.0)
        async def _read_lines(start_line: int, end_line: int) -> str:
            sed_result = await self.execute(Command(command=["sed", "-n", f"{start_line},{end_line}p", file_path]))
            if sed_result.exit_code != 0:
                raise Exception(f"Failed to read file {file_path}, sed result: {sed_result}")
            return sed_result.stdout

        # read lines
        read_times, last_time_lines = divmod(end_line - start_line + 1, lines_per_request)
        result = ""
        for i in range(read_times):
            tmp_start_line = start_line + i * lines_per_request
            tmp_end_line = start_line + (i + 1) * lines_per_request - 1
            logger.info(f"read lines from {tmp_start_line} to {tmp_end_line}")
            content = await _read_lines(tmp_start_line, tmp_end_line)
            result += content
        if last_time_lines > 0:
            logger.info(f"read last lines from {start_line + read_times * lines_per_request} to {end_line}")
            last_result = await _read_lines(start_line + read_times * lines_per_request, end_line)
            result += last_result

        result = ReadFileResponse(content=result)
        return result

    async def _generate_tmp_session_name(self) -> str:
        timestamp = str(time.time_ns())
        return f"bash-{timestamp}"

    async def _upload_via_oss(self, file_path: str | Path, target_path: str):
        if self._oss_bucket is None or self._is_token_expired():
            setup_response: OssSetupResponse = await self._setup_oss()
            if not setup_response.success:
                return UploadResponse(success=False, message="Failed to upload file, please setup oss bucket first")
        timestamp = str(time.time_ns())
        file_name = file_path.split("/")[-1]
        tmp_obj_name = f"{timestamp}-{file_name}"
        oss2.resumable_upload(self._oss_bucket, tmp_obj_name, file_path)
        url = self._oss_bucket.sign_url("GET", tmp_obj_name, 600, slash_safe=True)
        try:
            download_cmd = f"wget -c -O {target_path} '{url}'"
            await self.run_nohup_and_wait(cmd=download_cmd, wait_timeout=600)
            check_file_session = f"bash-{timestamp}"
            await self.create_session(CreateBashSessionRequest(session=check_file_session))
            check_file_cmd = f"test -f {target_path}"
            check_response: Observation = await self.run_in_session(
                action=BashAction(command=check_file_cmd, session=check_file_session)
            )
            if not check_response.exit_code == 0:
                return UploadResponse(
                    success=False, message=f"Failed to upload file {file_name}, sandbox download phase failed"
                )
            else:
                return UploadResponse(success=True, message=f"Successfully uploaded file {file_name} to {target_path}")
        except Exception:
            return UploadResponse(success=False, message=f"Failed to upload file {file_name} to {target_path}")

    async def _setup_oss(self) -> OssSetupResponse:
        url = f"{self._url}/get_token"
        headers = self._build_headers()

        try:
            response = await HttpUtils.get(url, headers)
            if not response["status"] == "Success":
                return False
            auth = oss2.StsAuth(
                response["result"]["AccessKeyId"],
                response["result"]["AccessKeySecret"],
                response["result"]["SecurityToken"],
            )
            self._oss_token_expire_time = response["result"]["Expiration"]

            self._oss_bucket = oss2.Bucket(
                auth=auth,
                endpoint=env_vars.ROCK_OSS_BUCKET_ENDPOINT,
                bucket_name=env_vars.ROCK_OSS_BUCKET_NAME,
                region=env_vars.ROCK_OSS_BUCKET_REGION,
            )
        except Exception as e:
            return OssSetupResponse(success=False, message=f"Failed to setup oss bucket: {e}")
        return OssSetupResponse(success=True, message="Successfully setup oss bucket")

    def _add_user_defined_tag_into_headers(self, headers: dict):
        if self.config.user_id:
            headers["X-User-Id"] = self.config.user_id
        if self.config.experiment_id:
            headers["X-Experiment-Id"] = self.config.experiment_id
        if self.config.namespace:
            headers["X-Namespace"] = self.config.namespace

    def _is_token_expired(self) -> bool:
        try:
            expire_time = datetime.fromisoformat(self._oss_token_expire_time.replace("Z", "+00:00"))
            current_time = datetime.now(timezone.utc)

            buffer_time = timedelta(minutes=5)
            effective_expire_time = expire_time - buffer_time

            return current_time >= effective_expire_time

        except (ValueError, AttributeError):
            return True

    def _generate_utc_iso_time(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def close_session(self, request: CloseSessionRequest) -> CloseSessionResponse:
        # TODO: implement this
        pass

    async def close(self) -> CloseResponse:
        await self.stop()

    def __str__(self):
        """返回用户友好的字符串表示，包含主要成员变量"""
        return (
            f"Sandbox(sandbox_id={self._sandbox_id}, "
            f"host_name={self._host_name!r}, "
            f"host_ip={self._host_ip}, "
            f"image={self.config.image}, "
            f"cluster={self._cluster})"
        )

    def __repr__(self):
        """返回开发者友好的字符串表示，包含所有成员变量"""
        return (
            f"Sandbox("
            f"config={self.config!r}, "
            f"_url={self._url!r}, "
            f"_route_key={self._route_key!r}, "
            f"_sandbox_id={self._sandbox_id!r}, "
            f"_host_name={self._host_name!r}, "
            f"_host_ip={self._host_ip!r}, "
            f"_oss_bucket={self._oss_bucket!r}, "
            f"_cluster={self._cluster!r}, "
            f"_pod_name={self._pod_name!r}, "
            f"_ip={self._ip!r}, "
            f"_oss_token_expire_time={self._oss_token_expire_time!r}"
            f")"
        )


class SandboxGroup:
    config: SandboxGroupConfig
    sandbox_list: list[Sandbox]

    def __init__(self, config: SandboxGroupConfig):
        self.config = config
        self.sandbox_list = [Sandbox(config) for _ in range(config.size)]

    async def start(self):
        semaphore = asyncio.Semaphore(self.config.start_concurrency)

        async def start_sandbox_with_retry(index: int, sandbox: Sandbox) -> None:
            async with semaphore:
                logging.info(f"Starting sandbox {index} with {sandbox.config.image} ...")
                for attempt in range(self.config.start_retry_times):
                    try:
                        await sandbox.start()
                        return
                    except Exception as e:
                        if attempt == self.config.start_retry_times - 1:
                            logging.error(
                                f"Failed to start sandbox after {self.config.start_retry_times} attempts: {e}"
                            )
                            raise
                        else:
                            logging.warning(
                                f"Failed to start sandbox (attempt {attempt + 1}/{self.config.start_retry_times}): {e}, retrying..."
                            )
                            await asyncio.sleep(1)  # Wait 1 second before retry

        tasks = [start_sandbox_with_retry(index, sandbox) for index, sandbox in enumerate(self.sandbox_list)]
        await asyncio.gather(*tasks)
        logging.info(
            f"Successfully started {len(self.sandbox_list)} sandboxes with concurrency {self.config.start_concurrency}"
        )

    async def stop(self):
        tasks = [sandbox.stop() for sandbox in self.sandbox_list]
        await asyncio.gather(
            *tasks, return_exceptions=True
        )  # Use return_exceptions=True to ensure continuation even if some fail
        logging.info(f"Stopped {len(self.sandbox_list)} sandboxes")
