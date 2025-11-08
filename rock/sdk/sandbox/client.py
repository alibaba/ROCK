import asyncio
import logging
import mimetypes
import os
import time
import uuid
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import oss2
from typing_extensions import deprecated

from rock import env_vars
from rock.actions import (
    Action,
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
    SandboxStatusResponse,
    UploadRequest,
    UploadResponse,
    WriteFileRequest,
    WriteFileResponse,
)
from rock.sdk.common.constants import RunModeType
from rock.sdk.sandbox.config import SandboxConfig, SandboxGroupConfig
from rock.utils import HttpUtils, extract_nohup_pid


class Sandbox:
    config: SandboxConfig
    _url: str
    _route_key: str
    _sandbox_id: str | None = None
    _host_name: str | None = None
    _host_ip: str | None = None
    _oss_bucket: oss2.Bucket | None = None
    _cluster: str | None = None

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
        
        logging.debug(f"Start container response: {response}")
        if "Success" != response.get("status"):
            raise Exception(f"Failed to start container: {response}")
        self._sandbox_id = response.get("result").get("sandbox_id")
        self._host_name = response.get("result").get("host_name")
        self._host_ip = response.get("result").get("host_ip")

        start_time = time.time()
        while time.time() - start_time < self.config.startup_timeout:
            try:
                status = await self.get_status()
                logging.debug(f"Get status response: {status}")
                if status.is_alive:
                    break
            except Exception as e:
                logging.warning(f"Failed to get status, {str(e)}")
            await asyncio.sleep(1)

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
        url = f"{self._url}/get_status?sandbox_id={self._sandbox_id}"
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
            "container_name": self._sandbox_id,
            "sandbox_id": self._sandbox_id,
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
        if not self._sandbox_id:
            return
        try:
            url = f"{self._url}/stop"
            headers = self._build_headers()
            data = {
                "container_name": self._sandbox_id,
                "sandbox_id": self._sandbox_id,
            }
            await HttpUtils.post(url, headers, data)
        except Exception as e:
            logging.warning(f"Failed to stop container, IGNORE: {e}")

    async def commit(self, image_tag: str, username: str, password: str):
        if not self._sandbox_id:
            return

        url = f"{self._url}/commit"
        headers = self._build_headers()
        data = {
            "container_name": self._sandbox_id,
            "sandbox_id": self._sandbox_id,
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
            "container_name": self._sandbox_id,
            "session_type": "bash",
            "sandbox_id": self._sandbox_id,
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
            "container_name": self._sandbox_id,
            "action_type": "bash",
            "session": action.session,
            "command": action.command,
            "sandbox_id": self._sandbox_id,
            "check": action.check,
        }
        try:
            response = await HttpUtils.post(url, headers, data)
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
            nohup_command = f"nohup {cmd} < /dev/null > {redirect_file_path} 2>&1 & echo $!;disown"
            action = Action(command=nohup_command, session=temp_session)
            response = await self._run_in_session(action)

            # Parse
            pid = extract_nohup_pid(response.output)
            logging.info(f"sandbox {self.sandbox_id} cmd {cmd} pid {pid}")
            if not pid:
                return ExecuteBashSessionResponse(
                    success=False, message=f"Failed to extract PID from output: {response.output}"
                )

            # Wait for process completion
            success, message = await self._wait_for_process_completion(
                pid=pid, session=temp_session, wait_timeout=wait_timeout, wait_interval=wait_interval
            )

            return ExecuteBashSessionResponse(success=success, message=message)

        except Exception as e:
            error_msg = f"Failed to execute nohup command '{cmd}': {str(e)}"
            return ExecuteBashSessionResponse(success=False, message=error_msg)

    async def arun(
        self, cmd: str, session: str = None, wait_timeout=300, wait_interval=10, mode: RunModeType = "normal"
    ) -> Observation:
        if mode == "nohup":
            try:
                timestamp = str(time.time_ns())
                if session is None:
                    temp_session = f"bash-{timestamp}"
                    await self.create_session(CreateBashSessionRequest(session=temp_session))
                    session = temp_session
                tmp_file = f"/tmp/tmp_{timestamp}.out"
                nohup_command = f"nohup {cmd} < /dev/null > {tmp_file} 2>&1 & echo $!;disown"
                action = Action(command=nohup_command, session=session)
                response: Observation = await self._run_in_session(action)

                pid = extract_nohup_pid(response.output)
                if not pid:
                    return Observation(
                        output="",
                        exit_code=1,
                        failure_reason=f"Failed to submit command, nohup output: {response.output}",
                    )

                success, message = await self._wait_for_process_completion(
                    pid=pid, session=session, wait_timeout=wait_timeout, wait_interval=wait_interval
                )
                exec_result: Observation = await self._run_in_session(
                    Action(session=session, command=f"cat {tmp_file}")
                )
                if success:
                    return Observation(output=exec_result.output, exit_code=0)
                else:
                    return Observation(output=exec_result.output, exit_code=1, failure_reason=message)

            except Exception as e:
                error_msg = f"Failed to execute nohup command '{cmd}': {str(e)}"
                return Observation(output="", exit_code=1, failure_reason=error_msg)
        elif mode == "normal":
            return await self._run_in_session(action=Action(command=cmd, session=session))
        else:
            return Observation(output="", exit_code=1, failure_reason="Unsupported arun mode")

    async def write_file(self, request: WriteFileRequest) -> WriteFileResponse:
        url = f"{self._url}/write_file"
        headers = self._build_headers()
        content = request.content
        path = request.path
        sandbox_id = self._sandbox_id if request.container_name is None else request.container_name
        data = {
            "content": content,
            "path": path,
            "sandbox_id": sandbox_id,
        }
        response = await HttpUtils.post(url, headers, data)
        if "Success" != response.get("status"):
            return WriteFileResponse(success=False, message=f"Failed to write file {path}: upload response: {response}")
        return WriteFileResponse(success=True, message=f"Successfully write content to file {path}")

    async def write_file_directly(self, content: str, path: str) -> WriteFileResponse:
        url = f"{self._url}/write_file"
        headers = self._build_headers()
        data = {
            "content": content,
            "path": path,
            "sandbox_id": self._sandbox_id,
        }
        response = await HttpUtils.post(url, headers, data)
        if "Success" != response.get("status"):
            return WriteFileResponse(success=False, message=f"Failed to write file {path}: upload response: {response}")
        return WriteFileResponse(success=True, message=f"Successfully write content to file {path}")

    async def _wait_for_process_completion(
        self, pid: str, session: str, wait_timeout: int, wait_interval: int
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
                    self.run_in_session(Action(session=session, command=check_alive_cmd)),
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

    async def upload(self, request: UploadRequest) -> UploadResponse:
        return await self.aupload(file_path=request.source_path, target_path=request.target_path)

    @deprecated("Use aupload instead")
    async def upload_by_path(self, file_path: str | Path, target_path: str) -> UploadResponse:
        return await self.aupload(file_path=file_path, target_path=target_path)

    async def aupload(self, file_path: str | Path, target_path: str) -> UploadResponse:
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

        data = {"target_path": target_path, "sandbox_id": self._sandbox_id, "container_name": self._sandbox_id}

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
        sandbox_id = self._sandbox_id if request.container_name is None else request.container_name
        data = {
            "path": request.path,
            "encoding": request.encoding,
            "errors": request.errors,
            "sandbox_id": sandbox_id,
        }
        response = await HttpUtils.post(url, headers, data)
        result: dict = response.get("result")
        return ReadFileResponse(content=result["content"])

    async def read_file_with_line_range(
        self, file_path: str, start_line: int, end_line: int, session: str | None = None
    ) -> ReadFileResponse:
        # Pre check
        if start_line < 0 or end_line < start_line:
            raise Exception(f"start_line({start_line}) must be positive and start_line < end_line({end_line})")
        if end_line - start_line > 1000:
            raise Exception(f"end_line({end_line}) - start_line({start_line}) must be less than 1000")

        if session is None:
            session = await self._generate_tmp_session_name()
            await self.create_session(CreateBashSessionRequest(session=session))

        sed_result = await self.arun(f"sed -n '{start_line},{end_line}p' {file_path}", session=session)

        if sed_result.exit_code != 0:
            raise Exception(f"Failed to read file {file_path}, sed result: {sed_result}")
        result = ReadFileResponse(content=sed_result.output)
        return result

    async def download_file(self, file_path: str | Path) -> dict:
        url = f"{self._url}/read_file"
        headers = self._build_headers()

        data = {
            "path": file_path,
            "sandbox_id": self._sandbox_id,
        }
        response = await HttpUtils.post(url, headers, data)
        result: dict = response.get("result")
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
                action=Action(command=check_file_cmd, session=check_file_session)
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


class SandboxGroup:
    config: SandboxGroupConfig
    sandbox_list: list[Sandbox]

    def __init__(self, config: SandboxGroupConfig):
        self.config = config
        self.sandbox_list = [Sandbox(config) for _ in range(config.size)]

    async def start(self):
        semaphore = asyncio.Semaphore(self.config.start_concurrency)

        async def start_sandbox_with_retry(sandbox: Sandbox) -> None:
            async with semaphore:
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

        tasks = [start_sandbox_with_retry(sandbox) for sandbox in self.sandbox_list]
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
