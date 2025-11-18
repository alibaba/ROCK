import pytest
from fastapi.testclient import TestClient

from tests.integration.conftest import SKIP_IF_NO_DOCKER


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
def test_env_api_sequence(admin_client: TestClient):
    """Test the complete workflow of env API in order"""
    client = admin_client
    base_url = "/apis/v1/envs/gem"
    session_name = "test-session-sequence"
    # 1. Test make API
    make_url = f"{base_url}/make"
    make_payload = {"env_id": "game:Sokoban-v0-easy"}
    sandbox_id = client.post(make_url, json=make_payload)
    assert sandbox_id.status_code == 200
    make_result = sandbox_id.json()
    sandbox_id = make_result["sandbox_id"]
    # 2. Test reset API
    reset_url = f"{base_url}/reset"
    reset_payload = {"seed": 42, "sandbox_id": sandbox_id}
    reset_response = client.post(reset_url, json=reset_payload)
    assert reset_response.status_code == 200
    reset_result = reset_response.json()
    assert "observation" in reset_result
    assert "info" in reset_result
    # 3. Test step API
    step_url = f"{base_url}/step"
    step_payload = {
        "session": session_name,
        "action": "\\boxed{up}",
        "sandbox_id": sandbox_id,
    }
    step_response = client.post(step_url, json=step_payload)
    assert step_response.status_code == 200
    step_result = step_response.json()

    assert "truncated" in step_result
    assert "info" in step_result
    assert "observation" in step_result
    assert "reward" in step_result
    assert "terminated" in step_result

    # 4. Test close API
    close_url = f"{base_url}/close"
    close_payload = {"sandbox_id": sandbox_id}
    close_response = client.post(close_url, json=close_payload)
    assert close_response.status_code == 200
