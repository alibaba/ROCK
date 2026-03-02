"""
Integration test for SDK start with env_dir (docker build context).

- SDK packs env_dir (directory containing Dockerfile) as tar.gz and POSTs to
  /start_async_with_env; admin passes the archive to the SandboxActor via Ray;
  the actor extracts, runs docker build, then docker run.

Run: pytest tests/integration/sdk/sandbox/test_env_dir_build.py -v
  (with admin available and docker on the worker node)
"""
import tempfile
from pathlib import Path

import pytest

from rock.actions.sandbox.request import CreateBashSessionRequest
from rock.logger import init_logger
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig

from tests.integration.conftest import SKIP_IF_NO_DOCKER

logger = init_logger(__name__)

# Content of the file we COPY in the Dockerfile; verified inside the container.
ENV_DIR_TEST_FILE_CONTENT = "rock-env-dir-build-ok"

@pytest.fixture
def minimal_env_dir():
    """A minimal docker build context: Dockerfile + a file to test COPY."""
    with tempfile.TemporaryDirectory(prefix="rock_env_dir_") as tmp:
        path = Path(tmp)
        (path / "app.txt").write_text(ENV_DIR_TEST_FILE_CONTENT)
        (path / "Dockerfile").write_text(
            "FROM python:3.11\n"
            "COPY app.txt /opt/app.txt\n"
        )
        yield path


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_sandbox_start_with_env_dir(admin_remote_server, minimal_env_dir):
    """Start sandbox with env_dir: SDK packs context, admin passes to actor, actor builds and runs.
    When env_dir is set, image is ignored; server uses a generated tag for the built image."""
    config = SandboxConfig(
        memory="2g",
        cpus=1.0,
        startup_timeout=300,
        base_url=f"{admin_remote_server.endpoint}:{admin_remote_server.port}",
        env_dir=minimal_env_dir,
    )
    sandbox = Sandbox(config)
    try:
        await sandbox.start()
        assert sandbox.sandbox_id
        await sandbox.create_session(CreateBashSessionRequest(session="default"))
        result = await sandbox.arun(cmd="echo ok", session="default")
        assert result.output is not None
        assert "ok" in result.output
        # Verify COPY in Dockerfile: file from build context is present with expected content
        cat_result = await sandbox.arun(cmd="cat /opt/app.txt", session="default")
        assert cat_result.output is not None
        assert cat_result.output.strip() == ENV_DIR_TEST_FILE_CONTENT
    finally:
        try:
            await sandbox.stop()
        except Exception as e:
            logger.warning("Failed to stop sandbox: %s", e)
