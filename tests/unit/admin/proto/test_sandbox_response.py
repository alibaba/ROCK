"""
Unit tests for admin proto response models — disk_limit_rootfs fields.

Tests cover:
- SandboxStartResponse.disk_limit_rootfs field
- SandboxStatusResponse.disk_limit_rootfs field
- SandboxStatusResponse.from_sandbox_info() extraction
"""

from rock.admin.proto.response import SandboxStartResponse, SandboxStatusResponse

# ---- SandboxStartResponse tests ----


class TestSandboxStartResponseDiskLimit:
    def test_disk_limit_rootfs_default_is_none(self):
        response = SandboxStartResponse()
        assert response.disk_limit_rootfs is None

    def test_disk_limit_rootfs_set_value(self):
        response = SandboxStartResponse(disk_limit_rootfs="20g")
        assert response.disk_limit_rootfs == "20g"

    def test_all_fields_with_rootfs_limit(self):
        response = SandboxStartResponse(
            sandbox_id="test-sandbox",
            host_ip="10.0.0.1",
            cpus=4.0,
            memory="16g",
            disk_limit_rootfs="50g",
        )
        assert response.sandbox_id == "test-sandbox"
        assert response.disk_limit_rootfs == "50g"
        assert response.cpus == 4.0
        assert response.memory == "16g"


# ---- SandboxStatusResponse tests ----


class TestSandboxStatusResponseDiskLimit:
    def test_disk_limit_rootfs_default_is_none(self):
        response = SandboxStatusResponse()
        assert response.disk_limit_rootfs is None

    def test_disk_limit_rootfs_set_value(self):
        response = SandboxStatusResponse(disk_limit_rootfs="20g")
        assert response.disk_limit_rootfs == "20g"

    def test_from_sandbox_info_with_rootfs_limit(self):
        """from_sandbox_info() should extract disk_limit_rootfs from SandboxInfo dict."""
        sandbox_info = {
            "sandbox_id": "test-sandbox",
            "phases": {},
            "port_mapping": {},
            "host_ip": "10.0.0.1",
            "cpus": 2.0,
            "memory": "8g",
            "disk_limit_rootfs": "30g",
        }
        response = SandboxStatusResponse.from_sandbox_info(sandbox_info)
        assert response.disk_limit_rootfs == "30g"
        assert response.cpus == 2.0
        assert response.memory == "8g"

    def test_from_sandbox_info_without_limit(self):
        """from_sandbox_info() should yield None when disk_limit_rootfs is absent."""
        sandbox_info = {
            "sandbox_id": "test-sandbox",
            "phases": {},
            "port_mapping": {},
            "cpus": 2.0,
            "memory": "8g",
        }
        response = SandboxStatusResponse.from_sandbox_info(sandbox_info)
        assert response.disk_limit_rootfs is None

    def test_from_sandbox_info_with_none_limit(self):
        """from_sandbox_info() should surface None when field is explicitly None."""
        sandbox_info = {
            "sandbox_id": "test-sandbox",
            "phases": {},
            "port_mapping": {},
            "disk_limit_rootfs": None,
        }
        response = SandboxStatusResponse.from_sandbox_info(sandbox_info)
        assert response.disk_limit_rootfs is None


# ---- actions/sandbox/response.SandboxStatusResponse tests ----


class TestActionsSandboxStatusResponseDiskLimit:
    def test_actions_status_response_rootfs_limit(self):
        """rock.actions.sandbox.response.SandboxStatusResponse should have disk_limit_rootfs."""
        from rock.actions.sandbox.response import SandboxStatusResponse as ActionStatusResponse

        response = ActionStatusResponse(disk_limit_rootfs="20g")
        assert response.disk_limit_rootfs == "20g"

    def test_actions_status_response_defaults_none(self):
        from rock.actions.sandbox.response import SandboxStatusResponse as ActionStatusResponse

        response = ActionStatusResponse()
        assert response.disk_limit_rootfs is None
