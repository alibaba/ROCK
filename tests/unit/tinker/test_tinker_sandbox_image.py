"""Task-independent offline agent packaging contracts; no network or daemon."""
import importlib.util
from pathlib import Path

import pytest

_root = Path(__file__).resolve().parents[3]
_path = _root / "examples/tinker_quick_start/prepare_sandbox.py"
_spec = importlib.util.spec_from_file_location("public_sandbox_preparation", _path)
preparation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preparation)


def test_task_selection_delegates_complete_harbor_download(monkeypatch, tmp_path):
    calls = []
    class Download:
        @staticmethod
        def download(target, **kwargs):
            calls.append((target, kwargs))
            return {"task_files_unchanged": True}
    monkeypatch.setattr(preparation, "load_helper", lambda *_: Download)
    cache = tmp_path / "cache"
    target = tmp_path / "pallets__flask-5014"
    assert preparation.task_files(_root, target, cache, "pallets__flask-5014") == {"task_files_unchanged": True}
    assert calls == [(target, {"task_id": "pallets__flask-5014", "cache": cache})]


@pytest.mark.parametrize("copy_fails", [False, True])
def test_export_stopped_agent_container_always_removed(monkeypatch, tmp_path, copy_fails):
    calls = []
    monkeypatch.setattr(preparation.subprocess, "check_output", lambda command, **_: "container-id\n")
    def run(command, **kwargs):
        calls.append(command)
        if copy_fails and command[1] == "cp":
            raise RuntimeError("copy failed")
    monkeypatch.setattr(preparation, "run", run)
    target = tmp_path / "sweagent-runtime.tar.gz"
    if copy_fails:
        with pytest.raises(RuntimeError, match="copy failed"):
            preparation.export_agent_payload("docker", "agent:fixed", target)
    else:
        preparation.export_agent_payload("docker", "agent:fixed", target)
    assert calls == [["docker", "cp", "container-id:/sweagent-runtime.tar.gz", target],
                     ["docker", "rm", "container-id"]]


def test_agent_recipe_contains_no_task_or_verifier_environment():
    directory = _root / "examples/tinker_quick_start"
    recipe = (directory / "Dockerfile.agent").read_text()
    assert "prepare_agent_tools.py /opt/SWE-agent/tools /opt/sweagent-tools" in recipe
    assert "COPY python312/ /opt/python312/" in recipe
    assert "tar -czf /sweagent-runtime.tar.gz" in recipe
    for forbidden in ("TASK_BASE_IMAGE", "/testbed", "/opt/miniconda3", "/opt/verifier", "parser-wheels"):
        assert forbidden not in recipe
    assert "COPY sweagent-runtime.tar.gz /opt/public/sweagent-runtime.tar.gz" in (directory / "Dockerfile").read_text()
