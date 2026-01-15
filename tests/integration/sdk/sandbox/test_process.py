"""Tests for Process.upload_dir (tar-based directory upload) using one sandbox."""

from __future__ import annotations

from pathlib import Path

import pytest

from rock.actions import Command
from rock.logger import init_logger
from rock.sdk.sandbox.client import Sandbox
from tests.integration.conftest import SKIP_IF_NO_DOCKER

logger = init_logger(__name__)


def _make_local_fixture_dir(tmp_path: Path) -> Path:
    root = tmp_path / "upload_dir_fixture"
    root.mkdir(parents=True, exist_ok=True)

    (root / "a.txt").write_text("hello-a\n", encoding="utf-8")

    sub = root / "sub"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "b.txt").write_text("hello-b\n", encoding="utf-8")

    nested = root / "sub2" / "nested"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "c.txt").write_text("hello-c\n", encoding="utf-8")

    return root


async def _assert_upload_dir_success(sandbox: Sandbox, tmp_path: Path):
    local_dir = _make_local_fixture_dir(tmp_path)
    target_dir = "/tmp/test_process_upload_dir_success"

    obs = await sandbox.process.upload_dir(source_dir=local_dir, target_dir=target_dir)
    assert obs.exit_code == 0, f"upload_dir failed: {obs.failure_reason or obs.output}"

    res = await sandbox.execute(Command(command=["bash", "-lc", f"cat {target_dir}/a.txt"]))
    assert res.exit_code == 0
    assert res.stdout == "hello-a\n"

    res = await sandbox.execute(Command(command=["bash", "-lc", f"cat {target_dir}/sub/b.txt"]))
    assert res.exit_code == 0
    assert res.stdout == "hello-b\n"

    res = await sandbox.execute(Command(command=["bash", "-lc", f"cat {target_dir}/sub2/nested/c.txt"]))
    assert res.exit_code == 0
    assert res.stdout == "hello-c\n"


async def _assert_upload_dir_invalid_source(sandbox: Sandbox, tmp_path: Path):
    missing_dir = tmp_path / "missing"
    target_dir = "/tmp/test_process_upload_dir_invalid_source"

    obs = await sandbox.process.upload_dir(source_dir=missing_dir, target_dir=target_dir)
    assert obs.exit_code != 0
    assert obs.failure_reason


async def _assert_upload_dir_invalid_target(sandbox: Sandbox, tmp_path: Path):
    local_dir = _make_local_fixture_dir(tmp_path)
    obs = await sandbox.process.upload_dir(source_dir=local_dir, target_dir="relative/path/not/allowed")
    assert obs.exit_code != 0
    assert "absolute" in (obs.failure_reason or "").lower()


async def _assert_upload_dir_overwrite_existing(sandbox: Sandbox, tmp_path: Path):
    local_dir = _make_local_fixture_dir(tmp_path)
    target_dir = "/tmp/test_process_upload_dir_overwrite_existing"

    r = await sandbox.execute(
        Command(command=["bash", "-lc", f"mkdir -p {target_dir} && echo junk > {target_dir}/junk.txt"])
    )
    assert r.exit_code == 0

    obs = await sandbox.process.upload_dir(source_dir=local_dir, target_dir=target_dir)
    assert obs.exit_code == 0, f"upload_dir failed: {obs.failure_reason or obs.output}"

    res = await sandbox.execute(Command(command=["bash", "-lc", f"test ! -f {target_dir}/junk.txt"]))
    assert res.exit_code == 0

    res = await sandbox.execute(Command(command=["bash", "-lc", f"cat {target_dir}/a.txt"]))
    assert res.exit_code == 0
    assert res.stdout == "hello-a\n"


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_process_upload_dir_all_in_one(sandbox_instance: Sandbox, tmp_path: Path):
    """Run all upload_dir checks in one sandbox."""
    await _assert_upload_dir_success(sandbox_instance, tmp_path)
    await _assert_upload_dir_invalid_source(sandbox_instance, tmp_path)
    await _assert_upload_dir_invalid_target(sandbox_instance, tmp_path)
    await _assert_upload_dir_overwrite_existing(sandbox_instance, tmp_path)
