import asyncio
import shutil
import sys
import tempfile
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests
from fastapi import UploadFile
from pydantic import BaseModel
from typing_extensions import Self

from rock.actions import (
    AbstractSandbox,
    CloseResponse,
    CloseSessionResponse,
    CommandResponse,
    CreateSessionResponse,
    EnvCloseRequest,
    EnvCloseResponse,
    EnvListResponse,
    EnvMakeRequest,
    EnvMakeResponse,
    EnvResetRequest,
    EnvResetResponse,
    EnvStepRequest,
    EnvStepResponse,
    IsAliveResponse,
    Observation,
    ReadFileResponse,
    RemoteSandboxRuntimeConfig,
    UploadRequest,
    UploadResponse,
    WriteFileResponse,
    _ExceptionTransfer,
)
from rock.admin.proto.request import SandboxAction as Action
from rock.admin.proto.request import SandboxCloseSessionRequest as CloseSessionRequest
from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.proto.request import SandboxCreateSessionRequest as CreateSessionRequest
from rock.admin.proto.request import SandboxReadFileRequest as ReadFileRequest
from rock.admin.proto.request import SandboxWriteFileRequest as WriteFileRequest
from rock.logger import init_logger
from rock.rocklet.exceptions import RockletException
from rock.utils import SANDBOX_ID, sandbox_id_ctx_var, wait_until_alive

__all__ = ["RemoteSandboxRuntime", "RemoteSandboxRuntimeConfig"]

logger = init_logger(__name__)


class RemoteSandboxRuntime(AbstractSandbox):
    def __init__(
        self,
        *,
        executor: ThreadPoolExecutor | None = None,
        **kwargs: Any,
    ):
        """A runtime that connects to a remote server.

        Args:
            **kwargs: Keyword arguments to pass to the `RemoteRuntimeConfig` constructor.
        """
        self._config = RemoteSandboxRuntimeConfig(**kwargs)
        self._executor = executor
        if not self._config.host.startswith("http"):
            logger.warning("Host %s does not start with http, adding http://", self._config.host)
            self._config.host = f"http://{self._config.host}"

    @classmethod
    def from_config(cls, config: RemoteSandboxRuntimeConfig) -> Self:
        return cls(**config.model_dump())

    def _get_timeout(self, timeout: float | None = None) -> float:
        if timeout is None:
            return self._config.timeout
        return timeout

    def set_executor(self, executor: ThreadPoolExecutor | None) -> None:
        self._executor = executor

    @property
    def _headers(self) -> dict[str, str]:
        """Request headers to use for authentication."""
        headers = {SANDBOX_ID: sandbox_id_ctx_var.get()}
        return headers

    @property
    def _api_url(self) -> str:
        if self._config.port is None:
            return self._config.host
        return f"{self._config.host}:{self._config.port}"

    def _handle_transfer_exception(self, exc_transfer: _ExceptionTransfer) -> None:
        """Reraise exceptions that were thrown on the remote."""
        if exc_transfer.traceback:
            logger.critical("Traceback: \n%s", exc_transfer.traceback)
        module, _, exc_name = exc_transfer.class_path.rpartition(".")
        print(module, exc_name)
        if module == "builtins":
            module_obj = __builtins__
        else:
            if module not in sys.modules:
                logger.debug("Module %s not in sys.modules, trying to import it", module)
                try:
                    __import__(module)
                except ImportError:
                    logger.debug("Failed to import module %s", module)
                    exc = RockletException(exc_transfer.message)
                    raise exc from None
            module_obj = sys.modules[module]
        try:
            if isinstance(module_obj, dict):
                # __builtins__, sometimes
                exception = module_obj[exc_name](exc_transfer.message)
            else:
                exception = getattr(module_obj, exc_name)(exc_transfer.message)
        except (AttributeError, TypeError):
            logger.error(
                f"Could not initialize transferred exception: {exc_transfer.class_path!r}. "
                f"Transfer object: {exc_transfer}"
            )
            exception = RockletException(exc_transfer.message)
        exception.extra_info = exc_transfer.extra_info
        raise exception from None

    def _handle_response_errors(self, response: requests.Response) -> None:
        """Raise exceptions found in the request response."""
        if response.status_code == 511:
            exc_transfer = _ExceptionTransfer(**response.json()["rockletexception"])
            self._handle_transfer_exception(exc_transfer)
        try:
            response.raise_for_status()
        except Exception:
            logger.critical("Received error response: %s", response.json())
            raise

    def _is_alive(self, timeout: float | None = None) -> IsAliveResponse:
        try:
            response = requests.get(
                f"{self._api_url}/is_alive", headers=self._headers, timeout=self._get_timeout(timeout)
            )
            if response.status_code == 200:
                return IsAliveResponse(**response.json())
            elif response.status_code == 511:
                exc_transfer = _ExceptionTransfer(**response.json()["rockletexception"])
                self._handle_transfer_exception(exc_transfer)
            msg = (
                f"Status code {response.status_code} from {self._api_url}/is_alive. "
                f"Message: {response.json().get('detail')}"
            )
            return IsAliveResponse(is_alive=False, message=msg)
        except requests.RequestException:
            msg = f"Failed to connect to {self._config.host}\n"
            msg += traceback.format_exc()
            return IsAliveResponse(is_alive=False, message=msg)
        except Exception:
            msg = f"Failed to connect to {self._config.host}\n"
            msg += traceback.format_exc()
            return IsAliveResponse(is_alive=False, message=msg)

    async def is_alive(self, *, timeout: float | None = None) -> IsAliveResponse:
        """Checks if the runtime is alive.

        Internal server errors are thrown, everything else just has us return False
        together with the message.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._is_alive, timeout)

    async def wait_until_alive(self, *, timeout: float = 60.0):
        return await wait_until_alive(self.is_alive, timeout=timeout)

    def _request(self, endpoint: str, request: BaseModel | None, output_class: Any):
        """Small helper to make requests to the server and handle errors and output."""
        response = requests.post(
            f"{self._api_url}/{endpoint}", json=request.model_dump() if request else None, headers=self._headers
        )
        self._handle_response_errors(response)
        return output_class(**response.json())

    async def create_session(self, request: CreateSessionRequest) -> CreateSessionResponse:
        """Creates a new session."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, self._request, "create_session", request, CreateSessionResponse
        )

    async def run_in_session(self, action: Action) -> Observation:
        """Runs a command in a session."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._request, "run_in_session", action, Observation)

    async def close_session(self, request: CloseSessionRequest) -> CloseSessionResponse:
        """Closes a shell session."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._request, "close_session", request, CloseSessionResponse)

    async def execute(self, command: Command) -> CommandResponse:
        """Executes a command (independent of any shell session)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._request, "execute", command, CommandResponse)

    async def read_file(self, request: ReadFileRequest) -> ReadFileResponse:
        """Reads a file"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._request, "read_file", request, ReadFileResponse)

    async def write_file(self, request: WriteFileRequest) -> WriteFileResponse:
        """Writes a file"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._request, "write_file", request, WriteFileResponse)

    async def get_statistics(self) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._get_statistics)

    async def env_make(self, request: EnvMakeRequest) -> EnvMakeResponse:
        """Creates a new environment"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._request, "env/make", request, EnvMakeResponse)

    async def env_step(self, request: EnvStepRequest) -> EnvStepResponse:
        """Steps an environment"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._request, "env/step", request, EnvStepResponse)

    async def env_reset(self, request: EnvResetRequest) -> EnvResetResponse:
        """Resets an environment"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._request, "env/reset", request, EnvResetResponse)

    async def env_close(self, request: EnvCloseRequest) -> EnvCloseResponse:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._request, "env/close", request, EnvCloseResponse)

    async def env_list(self) -> EnvListResponse:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._request, "env/list", {}, EnvListResponse)

    def _get_statistics(self):
        try:
            response = requests.get(f"{self._api_url}/get_statistics", headers=self._headers)
            return response.json()
        except Exception:
            msg = f"Failed to connect to {self._config.host}\n"
            msg += traceback.format_exc()
            return {}

    async def upload(self, request: UploadRequest) -> UploadResponse:
        """Uploads a file"""
        source = Path(request.source_path).resolve()
        logger.debug("Uploading file from %s to %s", request.source_path, request.target_path)
        if source.is_dir():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
                zip_path = Path(temp_dir) / "zipped_transfer.zip"
                shutil.make_archive(str(zip_path.with_suffix("")), "zip", source)
                logger.debug("Created zip file at %s", zip_path)
                files = {"file": zip_path.open("rb")}
                data = {"target_path": request.target_path, "unzip": "true"}
                response = requests.post(f"{self._api_url}/upload", files=files, data=data, headers=self._headers)
                self._handle_response_errors(response)
                return UploadResponse(**response.json())
        elif source.is_file():
            logger.debug("Uploading file from %s to %s", source, request.target_path)
            files = {"file": source.open("rb")}
            data = {"target_path": request.target_path, "unzip": "false"}
            response = requests.post(f"{self._api_url}/upload", files=files, data=data, headers=self._headers)
            self._handle_response_errors(response)
            return UploadResponse(**response.json())
        else:
            msg = f"Source path {source} is not a file or directory"
            raise ValueError(msg)

    def client_upload(self, files: dict[str, Any], target_path: str) -> UploadResponse:
        data = {"target_path": target_path, "unzip": "false"}
        response = requests.post(f"{self._api_url}/upload", files=files, data=data, headers=self._headers)
        self._handle_response_errors(response)
        logger.info(f"Uploaded file from {files} to {target_path}: {response.json()}")
        return UploadResponse(**response.json())

    async def async_upload(self, file: UploadFile, target_path: str) -> UploadResponse:
        loop = asyncio.get_running_loop()
        files = {"file": (file.filename, file.file, file.content_type)}
        return await loop.run_in_executor(self._executor, self.client_upload, files, target_path)

    def close(self) -> CloseResponse:
        """Closes the runtime."""
        return self._request("close", None, CloseResponse)
