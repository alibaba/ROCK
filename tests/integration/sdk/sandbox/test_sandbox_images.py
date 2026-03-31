"""
Verify that sandbox can start and run on each image in the list.
"""

import pytest

from rock.sdk.sandbox.client import Sandbox
from tests.integration.conftest import SKIP_IF_NO_DOCKER

SANDBOX_IMAGES_TO_CHECK = [
    # python
    "python:3.11",
    # ubuntu
    "ubuntu:16.04",
    "ubuntu:24.04",
    # alpine
    # "alpine:3.23",
    # "alpine:3.14",
    # nix
    "nixos/nix:2.20.9",
    "nixos/nix:2.32.6",
]


@pytest.mark.parametrize(
    "sandbox_instance",
    [{"image": img} for img in SANDBOX_IMAGES_TO_CHECK],
    ids=SANDBOX_IMAGES_TO_CHECK,
    indirect=True,
)
@pytest.mark.need_admin_and_network
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_sandbox_can_start_with_image(request: pytest.FixtureRequest, sandbox_instance: Sandbox):
    image_id = request.node.callspec.id
    result = await sandbox_instance.arun(cmd="echo ok", session="default")
    assert result.output is not None
    assert "ok" in result.output
    print(f"PASSED: {image_id}")
