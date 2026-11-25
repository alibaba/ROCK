from __future__ import annotations

import inspect
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

from rock.sandbox.operator.snapd_actor import SnapdSandboxActor, select_snapd_worker


def test_actor_import_does_not_load_snapd_http_or_admin_database_dependencies():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc
import sys

class NoAdminImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if (fullname == "sqlalchemy" or fullname.startswith("rock.deployments")
                or fullname.startswith("rock.admin.snapd")):
            raise AssertionError(f"resource actor loaded Admin dependency: {fullname}")

sys.meta_path.insert(0, NoAdminImports())
from rock.sandbox.operator.snapd_actor import SnapdSandboxActor, select_snapd_worker
""",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_worker_selection_only_reports_ray_placement(monkeypatch):
    assert select_snapd_worker._default_options == {"num_cpus": 1, "max_retries": 0}
    assert not inspect.signature(select_snapd_worker._function).parameters
    monkeypatch.setattr("rock.sandbox.operator.snapd_actor.ray.util.get_node_ip_address", lambda: "10.0.0.7")
    assert select_snapd_worker._function() == "10.0.0.7"


@pytest.fixture
def actor_factory(monkeypatch):
    monkeypatch.setattr(
        "rock.sandbox.operator.snapd_actor.ray.get_runtime_context",
        lambda: MagicMock(get_node_id=lambda: "node-a"),
    )
    monkeypatch.setattr("rock.sandbox.operator.snapd_actor.ray.util.get_node_ip_address", lambda: "10.0.0.1")
    return SnapdSandboxActor.__ray_metadata__.modified_class


def test_actor_only_holds_identity_resources_and_placement(actor_factory):
    actor = actor_factory("sandbox", "namespace", {"vcpus": 2}, actor_generation="reservation")
    assert actor.identity() == {
        "sandbox_id": "sandbox",
        "sandbox_namespace": "namespace",
        "resource_spec": {"vcpus": 2},
        "actor_generation": "reservation",
    }
    assert actor.placement() == {"ray_node_id": "node-a", "host_ip": "10.0.0.1"}
    for name in (
        "start",
        "stop",
        "delete",
        "get_status",
        "runtime_status",
        "capture_snapshot",
        "fork_snapshot",
        "_snapd",
        "_client",
        "_phase",
        "_snapshot_spec",
    ):
        assert not hasattr(actor, name)


def test_actor_returns_defensive_copies(actor_factory):
    resources = {"vcpus": 2, "nested": {"memory_mib": 1024}}
    actor = actor_factory("sandbox", "namespace", resources)
    resources["nested"]["memory_mib"] = 1
    identity = actor.identity()
    identity["resource_spec"]["nested"]["memory_mib"] = 2
    placement = actor.placement()
    placement["host_ip"] = "wrong-host"
    assert actor.identity()["resource_spec"]["nested"]["memory_mib"] == 1024
    assert actor.placement()["host_ip"] == "10.0.0.1"


@pytest.mark.parametrize(("sandbox_id", "namespace"), [("", "namespace"), ("sandbox", "")])
def test_actor_rejects_empty_reservation_identity(actor_factory, sandbox_id, namespace):
    with pytest.raises(ValueError, match="must not be empty"):
        actor_factory(sandbox_id, namespace, {})


@pytest.mark.parametrize(
    "argument", ["snapshot_spec", "source_snapshot_id", "p2p_source_worker_ip", "request_timeout_ms"]
)
def test_actor_does_not_accept_snapd_request_inputs(actor_factory, argument):
    with pytest.raises(TypeError, match=argument):
        actor_factory("sandbox", "namespace", {}, **{argument: None})
