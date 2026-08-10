import os
from unittest.mock import patch

from rock.admin.proto.request import SandboxCommand
from rock.rocklet.rocklet import Rocklet


def test_execute_merges_request_environment_over_rocklet_environment():
    command = SandboxCommand(
        sandbox_id="sandbox-123",
        command="pwd",
        env={"SHARED": "request", "COMMAND_ONLY": "value"},
    )
    rocklet = Rocklet.create()

    with (
        patch.dict(os.environ, {"BASE_ONLY": "base", "SHARED": "base"}, clear=True),
        patch("rock.rocklet.rocklet.subprocess.run") as subprocess_run,
    ):
        rocklet._run_subprocess_blocking(command)

    assert subprocess_run.call_args.kwargs["env"] == {
        "BASE_ONLY": "base",
        "SHARED": "request",
        "COMMAND_ONLY": "value",
    }
