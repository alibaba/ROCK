"""
Backward compatibility test: verify that old server responses containing
disk_limit_log are safely ignored by the new SDK models (which no longer
define that field).

Pydantic v2 default behavior (extra='ignore') discards unknown fields silently.
"""

from rock.actions.sandbox.response import SandboxStatusResponse
from rock.admin.proto.response import SandboxStartResponse
from rock.admin.proto.response import SandboxStatusResponse as AdminSandboxStatusResponse


class TestNewSdkOldServerCompat:
    """New SDK (disk_limit_log removed) + Old server (still sends disk_limit_log)."""

    def test_actions_status_response_ignores_extra_disk_limit_log(self):
        server_response = {
            "sandbox_id": "test-123",
            "status": {},
            "port_mapping": {},
            "host_ip": "10.0.0.1",
            "cpus": 2.0,
            "memory": "8g",
            "disk_limit_rootfs": "50g",
            "disk_limit_log": "50g",
        }
        response = SandboxStatusResponse(**server_response)
        assert response.disk_limit_rootfs == "50g"
        assert not hasattr(response, "disk_limit_log")

    def test_admin_start_response_ignores_extra_disk_limit_log(self):
        server_response = {
            "sandbox_id": "test-123",
            "host_ip": "10.0.0.1",
            "disk_limit_rootfs": "50g",
            "disk_limit_log": "10g",
        }
        response = SandboxStartResponse(**server_response)
        assert response.disk_limit_rootfs == "50g"
        assert not hasattr(response, "disk_limit_log")

    def test_admin_status_response_ignores_extra_disk_limit_log(self):
        server_response = {
            "sandbox_id": "test-123",
            "status": {},
            "port_mapping": {},
            "disk_limit_rootfs": "50g",
            "disk_limit_log": "50g",
        }
        response = AdminSandboxStatusResponse(**server_response)
        assert response.disk_limit_rootfs == "50g"
        assert not hasattr(response, "disk_limit_log")
