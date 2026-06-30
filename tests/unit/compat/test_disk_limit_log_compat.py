"""
Backward compatibility tests:
1. Old server responses containing disk_limit_log (legacy field, never restored)
   are safely ignored by Pydantic v2 default behavior (extra='ignore').
2. disk_limit_rootfs (deprecated alias) is present on response models and
   mirrors the disk field value when explicitly set.
"""

from rock.actions.sandbox.response import SandboxStatusResponse
from rock.admin.proto.response import SandboxStartResponse
from rock.admin.proto.response import SandboxStatusResponse as AdminSandboxStatusResponse


class TestNewSdkOldServerCompat:
    """New SDK + Old server (still sends disk_limit_log)."""

    def test_actions_status_response_ignores_extra_disk_limit_log(self):
        server_response = {
            "sandbox_id": "test-123",
            "status": {},
            "port_mapping": {},
            "host_ip": "10.0.0.1",
            "cpus": 2.0,
            "memory": "8g",
            "disk": "50g",
            "disk_limit_log": "50g",
        }
        response = SandboxStatusResponse(**server_response)
        assert response.disk == "50g"

    def test_admin_start_response_ignores_extra_disk_limit_log(self):
        server_response = {
            "sandbox_id": "test-123",
            "host_ip": "10.0.0.1",
            "disk": "50g",
            "disk_limit_log": "10g",
        }
        response = SandboxStartResponse(**server_response)
        assert response.disk == "50g"

    def test_admin_status_response_ignores_extra_disk_limit_log(self):
        server_response = {
            "sandbox_id": "test-123",
            "status": {},
            "port_mapping": {},
            "disk": "50g",
            "disk_limit_log": "50g",
        }
        response = AdminSandboxStatusResponse(**server_response)
        assert response.disk == "50g"


class TestDiskLimitRootfsDeprecatedCompat:
    """disk_limit_rootfs is a deprecated alias that mirrors disk."""

    def test_actions_status_response_has_disk_limit_rootfs(self):
        response = SandboxStatusResponse(disk="50g", disk_limit_rootfs="50g")
        assert response.disk == "50g"
        assert response.disk_limit_rootfs == "50g"

    def test_admin_start_response_has_disk_limit_rootfs(self):
        response = SandboxStartResponse(disk="50g", disk_limit_rootfs="50g")
        assert response.disk == "50g"
        assert response.disk_limit_rootfs == "50g"

    def test_admin_status_response_has_disk_limit_rootfs(self):
        response = AdminSandboxStatusResponse(disk="50g", disk_limit_rootfs="50g")
        assert response.disk == "50g"
        assert response.disk_limit_rootfs == "50g"

    def test_admin_status_from_sandbox_info_sets_disk_limit_rootfs(self):
        sandbox_info = {
            "sandbox_id": "test-123",
            "phases": {},
            "port_mapping": {},
            "disk": "30g",
        }
        response = AdminSandboxStatusResponse.from_sandbox_info(sandbox_info)
        assert response.disk == "30g"
        assert response.disk_limit_rootfs == "30g"
