import asyncio
import re
from urllib.parse import parse_qsl, unquote

from rock.admin.proto.response import E2BListedSandbox, SandboxStatusResponse
from rock.admin.service.e2b_sandbox_info import e2b_sandbox_info_fields
from rock.sandbox.sandbox_meta_store import SandboxMetaStore
from rock.sandbox.utils.timeout import SandboxTimeoutHelper
from rock.sdk.common.exceptions import BadRequestRockError

_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


class E2BProxyService:
    def __init__(self, meta_store: SandboxMetaStore, *, sandbox_service: object | None = None) -> None:
        # Kept as a keyword-only compatibility argument so main.py wiring does
        # not need to change; list operations depend only on the metadata store.
        del sandbox_service
        self._meta_store = meta_store

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
