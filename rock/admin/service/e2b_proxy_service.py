import datetime
import math
import re
from ipaddress import ip_address
from typing import Literal
from urllib.parse import parse_qsl, unquote

from rock.actions.sandbox.response import State
from rock.admin.proto.response import E2BListedSandbox, E2BSandboxDetail, SandboxStatusResponse
from rock.common.constants import E2B_CLIENT_ID, E2B_ENVD_VERSION, E2B_SANDBOX_IP_METADATA_KEY, E2B_STATE_BY_ROCK_STATE
from rock.sandbox.sandbox_meta_store import SandboxMetaStore
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService
from rock.sdk.common.exceptions import BadRequestRockError, E2BSandboxNotFoundError, SandboxNotFoundRockError
from rock.utils.format import parse_size_to_bytes

_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


class E2BProxyService:
    def __init__(self, sandbox_service: SandboxProxyService, meta_store: SandboxMetaStore) -> None:
        self._sandbox_service = sandbox_service
        self._meta_store = meta_store

    async def list_sandboxes(self, metadata: str) -> list[E2BListedSandbox]:
        records = await self._meta_store.list_by_metadata(self._parse_metadata_filter(metadata))
        result: list[E2BListedSandbox] = []
        for record in records:
            state = self._e2b_state(record.get("state"))
            if state is None:
                continue
            result.append(
                E2BListedSandbox(
                    sandboxID=record["sandbox_id"],
                    metadata=self._metadata_with_sandbox_ip(
                        record["sandbox_id"],
                        record["labels"],
                        record.get("host_ip"),
                    ),
                    state=state,
                )
            )
        return result

    async def get_sandbox(self, sandbox_id: str) -> E2BSandboxDetail:
        try:
            sandbox_status = await self._sandbox_service.get_status(sandbox_id, include_all_states=True)
        except SandboxNotFoundRockError as error:
            raise E2BSandboxNotFoundError(str(error)) from None
        return self._sandbox_detail(sandbox_id, sandbox_status)

    def _sandbox_detail(self, sandbox_id: str, sandbox_status: SandboxStatusResponse) -> E2BSandboxDetail:
        state = self._e2b_state(sandbox_status.state)
        if state is None:
            raise E2BSandboxNotFoundError(f"Sandbox {sandbox_id} not found")
        end_at = (
            sandbox_status.auto_stop_time
            if state == "running"
            else sandbox_status.auto_delete_time or sandbox_status.archive_time
        )

        return E2BSandboxDetail(
            sandboxID=sandbox_id,
            metadata=self._metadata_with_sandbox_ip(
                sandbox_id,
                sandbox_status.metadata,
                sandbox_status.host_ip,
            ),
            state=state,
            clientID=E2B_CLIENT_ID,
            templateID=str(sandbox_status.image),
            envdVersion=E2B_ENVD_VERSION,
            cpuCount=max(1, math.ceil(float(sandbox_status.cpus))),
            memoryMB=parse_size_to_bytes(str(sandbox_status.memory)) // (1024**2),
            diskSizeMB=parse_size_to_bytes(str(sandbox_status.disk)) // (1024**2),
            startedAt=self._iso8601_timestamp(
                sandbox_id,
                "start time",
                sandbox_status.start_time or sandbox_status.create_time,
            ),
            endAt=self._iso8601_timestamp(sandbox_id, "end time", end_at),
        )

    @staticmethod
    def _metadata_with_sandbox_ip(
        sandbox_id: str,
        metadata: object,
        host_ip: object,
    ) -> dict[str, str]:
        if not isinstance(metadata, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in metadata.items()
        ):
            raise ValueError(f"Sandbox {sandbox_id} metadata is invalid")
        if not isinstance(host_ip, str) or not host_ip.strip():
            raise ValueError(f"Sandbox {sandbox_id} IP is missing")
        ip_address(host_ip)
        return {**metadata, E2B_SANDBOX_IP_METADATA_KEY: host_ip}

    @staticmethod
    def _iso8601_timestamp(sandbox_id: str, field: str, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError(f"Sandbox {sandbox_id} {field} is invalid")
        try:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"Sandbox {sandbox_id} {field} is invalid") from None
        if parsed.tzinfo is None:
            raise ValueError(f"Sandbox {sandbox_id} {field} must include a timezone")
        return parsed.isoformat(timespec="seconds")

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

    @staticmethod
    def _e2b_state(value: object) -> Literal["running", "paused"] | None:
        try:
            rock_state = value if isinstance(value, State) else State(value)
            return E2B_STATE_BY_ROCK_STATE.get(rock_state.value)
        except (TypeError, ValueError):
            return None
