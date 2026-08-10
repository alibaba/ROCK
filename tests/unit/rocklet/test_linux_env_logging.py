from unittest.mock import MagicMock, patch

import pytest

from rock.admin.proto.request import SandboxCreateBashSessionRequest
from rock.rocklet.linux import BashSession


@pytest.mark.asyncio
async def test_bash_session_logs_environment_count_without_values():
    request = SandboxCreateBashSessionRequest(
        sandbox_id="sandbox-123",
        env={"TOKEN": "secret-value"},
    )
    session = BashSession(request)
    shell = MagicMock(before="")
    session.refresh_shell = MagicMock()

    with (
        patch("rock.rocklet.linux.pexpect.spawn", return_value=shell),
        patch("rock.rocklet.linux.time.sleep"),
        patch("rock.rocklet.linux.logger.info") as log_info,
    ):
        await session.start()

    log_info.assert_called_once_with("starting shell with %d environment variables", 4)
    assert "secret-value" not in str(log_info.call_args_list)
