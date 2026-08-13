import logging
import os

import pytest
from ap_sandbox import SandboxConfig, SandboxManager
from httpx import Client

TEMPLATE_ID = "st_5efa0210685646998525"
TEST_METADATA = {"ap-job-id": "e2e-sandbox-lifecycle-test"}


@pytest.mark.integration
def test_create_and_delete_sandbox_with_ap_sandbox_sdk(caplog: pytest.LogCaptureFixture):
    domain = os.getenv("E2B_DOMAIN", "")
    api_key = os.getenv("E2B_API_KEY", "")
    if not domain or not api_key:
        pytest.skip("E2B_DOMAIN and E2B_API_KEY are required")

    caplog.set_level(logging.WARNING, logger="hpack")

    config = SandboxConfig(
        e2b_domain=domain,
        e2b_api_key=api_key,
        sandbox_template=TEMPLATE_ID,
        sandbox_timeout_sec=300,
        wait_ready_timeout_sec=120,
        reserve_failed_sandbox_for="10m",
        ap_sandbox_metadata=TEST_METADATA,
    )

    api_url = f"https://{domain.strip().rstrip('/')}"
    api_options = {
        "api_url": api_url,
        "api_key": api_key,
        "validate_api_key": False,
    }
    sandbox_manager = SandboxManager(config)
    sandbox_manager.create(**api_options)

    sandbox_id = sandbox_manager.sandbox_id
    delete_response = None
    try:
        sandbox = sandbox_manager.sandbox
        assert sandbox is not None
        assert sandbox_id not in {None, "", "<unknown>"}

        with Client(
            base_url=api_url,
            headers={"X-API-Key": api_key},
            timeout=config.sandbox_create_timeout,
        ) as client:
            assert sandbox_manager.sandbox_ip

            detail = sandbox.get_info(**api_options)
            assert detail.sandbox_id == sandbox_id
            assert detail.metadata["ap-job-id"] == TEST_METADATA["ap-job-id"]

            list_response = client.get(
                "/v2/sandboxes",
                params={"metadata": f"ap-job-id:{TEST_METADATA['ap-job-id']}"},
            )
            list_response.raise_for_status()
            assert sandbox_id in {item["sandboxID"] for item in list_response.json()}
    finally:
        if sandbox_id not in {None, "", "<unknown>"}:
            try:
                with Client(
                    base_url=api_url,
                    headers={"X-API-Key": api_key},
                    timeout=config.sandbox_create_timeout,
                ) as client:
                    delete_response = client.delete(f"/sandboxes/{sandbox_id}")
                if delete_response.status_code != 204:
                    logging.getLogger(__name__).error(
                        "Failed to delete sandbox %s: HTTP %s",
                        sandbox_id,
                        delete_response.status_code,
                    )
            except Exception:
                logging.getLogger(__name__).exception("Failed to delete sandbox %s", sandbox_id)
        else:
            sandbox_manager.kill()

    assert delete_response is not None
    assert delete_response.status_code == 204
