"""
Unit tests for admin proto response models — disk fields.

Tests cover:
- SandboxStartResponse.disk field
- SandboxStatusResponse.disk field
- SandboxStatusResponse.from_sandbox_info() extraction
"""

from rock.admin.proto.response import SandboxStartResponse, SandboxStatusResponse

# ---- SandboxStartResponse tests ----


class TestSandboxStartResponseDiskLimit:
    def test_disk_default_is_none(self):
        response = SandboxStartResponse()
        assert response.disk is None

    def test_disk_set_value(self):
        response = SandboxStartResponse(disk="20g")
        assert response.disk == "20g"

    def test_all_fields_with_disk(self):
        response = SandboxStartResponse(
            sandbox_id="test-sandbox",
            host_ip="10.0.0.1",
            cpus=4.0,
            memory="16g",
            disk="50g",
        )
        assert response.sandbox_id == "test-sandbox"
        assert response.disk == "50g"
        assert response.cpus == 4.0
        assert response.memory == "16g"


# ---- SandboxStatusResponse tests ----


class TestSandboxStatusResponseDiskLimit:
    def test_disk_default_is_none(self):
        response = SandboxStatusResponse()
        assert response.disk is None

    def test_disk_set_value(self):
        response = SandboxStatusResponse(disk="20g")
        assert response.disk == "20g"

    def test_from_sandbox_info_with_disk(self):
        """from_sandbox_info() should extract disk from SandboxInfo dict."""
        sandbox_info = {
            "sandbox_id": "test-sandbox",
            "phases": {},
            "port_mapping": {},
            "host_ip": "10.0.0.1",
            "cpus": 2.0,
            "memory": "8g",
            "disk": "30g",
        }
        response = SandboxStatusResponse.from_sandbox_info(sandbox_info)
        assert response.disk == "30g"
        assert response.cpus == 2.0
        assert response.memory == "8g"

    def test_from_sandbox_info_without_disk(self):
        """from_sandbox_info() should yield None when disk is absent."""
        sandbox_info = {
            "sandbox_id": "test-sandbox",
            "phases": {},
            "port_mapping": {},
            "cpus": 2.0,
            "memory": "8g",
        }
        response = SandboxStatusResponse.from_sandbox_info(sandbox_info)
        assert response.disk is None

    def test_from_sandbox_info_with_none_disk(self):
        """from_sandbox_info() should surface None when field is explicitly None."""
        sandbox_info = {
            "sandbox_id": "test-sandbox",
            "phases": {},
            "port_mapping": {},
            "disk": None,
        }
        response = SandboxStatusResponse.from_sandbox_info(sandbox_info)
        assert response.disk is None


# ---- actions/sandbox/response.SandboxStatusResponse tests ----


class TestActionsSandboxStatusResponseDiskLimit:
    def test_actions_status_response_disk(self):
        """rock.actions.sandbox.response.SandboxStatusResponse should have disk."""
        from rock.actions.sandbox.response import SandboxStatusResponse as ActionStatusResponse

        response = ActionStatusResponse(disk="20g")
        assert response.disk == "20g"

    def test_actions_status_response_defaults_none(self):
        from rock.actions.sandbox.response import SandboxStatusResponse as ActionStatusResponse

        response = ActionStatusResponse()
        assert response.disk is None
