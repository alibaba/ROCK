import asyncio
import base64
import posixpath
import re
from collections.abc import AsyncIterator
from pathlib import PurePosixPath
from urllib.parse import parse_qsl, unquote

from fastapi import Response, UploadFile

from rock.actions import CommandResponse, FileEntry
from rock.actions.sandbox.response import State
from rock.admin.proto.e2b_connect import encode_connect_end, encode_connect_message
from rock.admin.proto.request import SandboxCommand
from rock.admin.proto.response import (
    E2BConnectErrorPayload,
    E2BListedSandbox,
    E2BProcessResponse,
    SandboxStatusResponse,
)
from rock.admin.service.e2b_sandbox_info import e2b_sandbox_info_fields
from rock.logger import init_logger
from rock.sandbox.sandbox_meta_store import SandboxMetaStore
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService
from rock.sandbox.utils.timeout import SandboxTimeoutHelper
from rock.sdk.common.exceptions import BadRequestRockError, E2BConnectError, SandboxNotFoundRockError

_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
PROCESS_REGISTRY_LIMIT = 1024
_E2B_DEFAULT_HOME = PurePosixPath("/home/user")
logger = init_logger(__name__)


def _normalize_e2b_path(path: str) -> str:
    if path == "~":
        return str(_E2B_DEFAULT_HOME)
    if path.startswith("~/"):
        path = path[2:]
    normalized = PurePosixPath(path)
    if not normalized.is_absolute():
        normalized = _E2B_DEFAULT_HOME / normalized
    return posixpath.normpath(str(normalized))


class E2BProxyService:
    def __init__(
        self,
        meta_store: SandboxMetaStore,
        *,
        sandbox_manager: SandboxProxyService | None = None,
    ) -> None:
        self._meta_store = meta_store
        self._sandbox_manager = sandbox_manager
        self._next_pid = 0
        self._process_tasks: dict[tuple[str, int], asyncio.Task[CommandResponse]] = {}

    async def _sandbox_state(self, sandbox_id: str) -> State | str | None:
        record = await self._meta_store.get(sandbox_id, check_db=True)
        return record.get("state") if record else None

    async def require_running_sandbox(self, sandbox_id: str) -> None:
        state = await self._sandbox_state(sandbox_id)
        if state != State.RUNNING and state != State.RUNNING.value:
            raise SandboxNotFoundRockError("sandbox is not running")

    async def is_running(self, sandbox_id: str) -> bool:
        state = await self._sandbox_state(sandbox_id)
        return state == State.RUNNING or state == State.RUNNING.value

    def _filesystem_manager(self) -> SandboxProxyService:
        if self._sandbox_manager is None:
            raise RuntimeError("E2B sandbox manager is not configured")
        return self._sandbox_manager

    async def e2b_fs_make_dir(self, sandbox_id: str, path: str) -> bool:
        normalized_path = _normalize_e2b_path(path)
        created = await self._filesystem_manager().e2b_fs_make_dir(sandbox_id, normalized_path)
        if not created:
            raise E2BConnectError("already_exists", f"directory already exists: {normalized_path}")
        return True

    async def e2b_fs_list(self, sandbox_id: str, path: str, depth: int) -> list[FileEntry]:
        return await self._filesystem_manager().e2b_fs_list(sandbox_id, _normalize_e2b_path(path), depth)

    async def e2b_fs_stat(self, sandbox_id: str, path: str) -> FileEntry:
        return await self._filesystem_manager().e2b_fs_stat(sandbox_id, _normalize_e2b_path(path))

    async def e2b_fs_write_files(
        self,
        sandbox_id: str,
        entries: list[tuple[str, UploadFile]],
    ) -> list[str]:
        normalized_entries = [(_normalize_e2b_path(path), file) for path, file in entries]
        return await self._filesystem_manager().e2b_fs_write_files(sandbox_id, normalized_entries)

    async def e2b_fs_read(self, sandbox_id: str, path: str) -> Response:
        return await self._filesystem_manager().e2b_fs_read(sandbox_id, _normalize_e2b_path(path))

    async def start_process(
        self,
        command: SandboxCommand,
        *,
        keepalive_interval: float = 50.0,
    ) -> AsyncIterator[bytes]:
        if self._sandbox_manager is None:
            raise RuntimeError("E2B sandbox manager is not configured")
        if len(self._process_tasks) >= PROCESS_REGISTRY_LIMIT:
            yield encode_connect_end(
                E2BConnectErrorPayload(
                    code="resource_exhausted",
                    message="too many E2B commands are running",
                )
            )
            return

        self._next_pid = 1 if self._next_pid >= 2**32 - 1 else self._next_pid + 1
        pid = self._next_pid
        task = asyncio.create_task(
            self._sandbox_manager.execute(
                command,
                propagate_rocklet_errors=True,
            )
        )
        task_key = (command.sandbox_id, pid)
        self._process_tasks[task_key] = task

        def remove_task(completed: asyncio.Task[CommandResponse]) -> None:
            self._process_tasks.pop(task_key, None)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(remove_task)
        yield encode_connect_message(E2BProcessResponse.started(pid))

        try:
            while not task.done():
                await asyncio.wait({task}, timeout=keepalive_interval)
                if not task.done():
                    yield encode_connect_message(E2BProcessResponse.keepalive_event())
            result: CommandResponse = await asyncio.shield(task)
        except TimeoutError:
            yield encode_connect_end(E2BConnectErrorPayload(code="deadline_exceeded", message="command timed out"))
            return
        except ValueError:
            logger.warning("E2B process command was rejected", exc_info=True)
            yield encode_connect_end(E2BConnectErrorPayload(code="invalid_argument", message="invalid command request"))
            return
        except ConnectionError:
            logger.warning("E2B process backend is unavailable", exc_info=True)
            yield encode_connect_end(
                E2BConnectErrorPayload(code="unavailable", message="sandbox command service is unavailable")
            )
            return
        except Exception:
            logger.exception("E2B process execution failed")
            yield encode_connect_end(E2BConnectErrorPayload(code="internal", message="internal server error"))
            return
        if result.stdout:
            yield encode_connect_message(E2BProcessResponse.stdout(base64.b64encode(result.stdout.encode()).decode()))
        if result.stderr:
            yield encode_connect_message(E2BProcessResponse.stderr(base64.b64encode(result.stderr.encode()).decode()))
        exit_code = 0 if result.exit_code is None else int(result.exit_code)
        yield encode_connect_message(E2BProcessResponse.ended(exit_code))
        yield encode_connect_end()

    async def aclose(self) -> None:
        tasks = list(self._process_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._process_tasks.clear()

    async def list_sandboxes(self, metadata: str) -> list[E2BListedSandbox]:
        records = await self._meta_store.list_running_by_metadata(self._parse_metadata_filter(metadata))
        timeout_infos = await asyncio.gather(
            *(self._meta_store.get_timeout(record["sandbox_id"]) for record in records)
        )
        return [
            self._listed_sandbox(record, timeout_info)
            for record, timeout_info in zip(records, timeout_infos, strict=True)
        ]

    def _listed_sandbox(self, record: dict, timeout_info: dict[str, str] | None) -> E2BListedSandbox:
        auto_stop_time, _, _ = SandboxTimeoutHelper.auto_transition_times_for_status(
            record.get("state"),
            record,
            timeout_info,
        )
        if auto_stop_time is None:
            auto_stop_time = SandboxTimeoutHelper.persisted_auto_stop_time(record)
        sandbox_id = record["sandbox_id"]
        sandbox_status = SandboxStatusResponse(
            state=record.get("state"),
            source_snapshot_id=record.get("source_snapshot_id"),
            metadata=record.get("metadata") or record.get("labels"),
            host_ip=record.get("host_ip"),
            image=record.get("image"),
            cpus=record.get("cpus"),
            memory=record.get("memory"),
            disk=record.get("disk"),
            start_time=record.get("start_time"),
            create_time=record.get("create_time"),
            auto_stop_time=auto_stop_time,
        )
        return E2BListedSandbox(**e2b_sandbox_info_fields(sandbox_id, sandbox_status))

    @staticmethod
    def _parse_metadata_filter(value: str) -> dict[str, str]:
        if _INVALID_PERCENT_ESCAPE.search(value):
            raise BadRequestRockError("metadata contains invalid URL encoding")

        equals_index = value.find("=")
        colon_index = value.find(":")
        uses_form_encoding = equals_index >= 0 and (colon_index < 0 or equals_index < colon_index)

        try:
            if uses_form_encoding:
                pairs = parse_qsl(
                    value,
                    keep_blank_values=True,
                    strict_parsing=True,
                    encoding="utf-8",
                    errors="strict",
                )
            else:
                pairs = []
                for pair in value.split(","):
                    key, separator, item = pair.partition(":")
                    if not separator:
                        raise ValueError
                    pairs.append(
                        (
                            unquote(key, encoding="utf-8", errors="strict"),
                            unquote(item, encoding="utf-8", errors="strict"),
                        )
                    )
        except (UnicodeDecodeError, ValueError):
            raise BadRequestRockError("metadata must contain key=value or key:value pairs") from None

        if not pairs:
            raise BadRequestRockError("metadata must contain at least one key=value pair")

        result: dict[str, str] = {}
        for key, item in pairs:
            if not key or not item:
                raise BadRequestRockError("metadata keys and values must not be empty")
            if key in result:
                raise BadRequestRockError(f"duplicate metadata key: {key}")
            result[key] = item
        return result
