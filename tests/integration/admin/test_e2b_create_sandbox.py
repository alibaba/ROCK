import logging
import os

import pytest
from ap_sandbox import SandboxConfig, SandboxManager

TEMPLATE_ID = "st_5efa0210685646998525"


@pytest.mark.integration
def test_create_sandbox_with_ap_sandbox_sdk(caplog: pytest.LogCaptureFixture):
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
        ap_sandbox_metadata={"ap-job-id": "e2e-create-sandbox-test"},
    )

    sandbox_manager = SandboxManager(config)
    sandbox_manager.create(
        api_url=f"https://{domain}",
        validate_api_key=False,
    )

    assert sandbox_manager.sandbox_id not in {None, "", "<unknown>"}
    assert sandbox_manager.sandbox_ip
