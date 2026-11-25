import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from rock.admin.snapd.protocol import (
    ForkSandboxRequest,
    SnapdReply,
    SnapshotStatusResult,
)

FIXTURE_ROOT = Path(__file__).parents[3] / "fixtures" / "snapd_protocol"


def _assert_python_canonical_fixture(model_type, name: str) -> None:
    fixture = (FIXTURE_ROOT / name).read_bytes()
    model = model_type.model_validate_json(fixture)
    encoded = json.dumps(
        model.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert (encoded + "\n").encode() == fixture


def test_python_models_match_rust_admin_protocol_goldens() -> None:
    _assert_python_canonical_fixture(ForkSandboxRequest, "fork_sandbox_request_v1.json")
    _assert_python_canonical_fixture(SnapshotStatusResult, "snapshot_status_result_v1.json")
    _assert_python_canonical_fixture(SnapdReply, "snapd_reply_v1.json")


def test_snapshot_status_reports_sizes_separately_from_opaque_spec() -> None:
    spec = {"private_v2": {"references": [None, "opaque"]}}
    status = SnapshotStatusResult(
        snapshot_id="snapshot-1", state="REMOTE_READY", progress=1,
        snapshot_spec=spec, memory_bytes=134217728, rootfs_bytes=67108864,
    )
    assert status.snapshot_spec == spec
    assert status.memory_bytes == 134217728
    assert status.rootfs_bytes == 67108864


@pytest.mark.parametrize("field", ["memory_bytes", "rootfs_bytes"])
@pytest.mark.parametrize("value", [0, -1, True, "4096"])
def test_snapshot_size_summaries_require_positive_integers(field, value) -> None:
    with pytest.raises(ValidationError):
        SnapshotStatusResult(snapshot_id="snapshot-1", state="REMOTE_READY", progress=1, **{field: value})
