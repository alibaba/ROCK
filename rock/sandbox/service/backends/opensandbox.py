import shlex

from rock.actions import CommandResponse
from rock.admin.proto.request import SandboxCommand
from rock.logger import init_logger
from rock.rocklet.exceptions import NonZeroExitCodeError
from rock.sandbox.operator.opensandbox.client import OpenSandboxClient
from rock.sdk.common.exceptions import BadRequestRockError

logger = init_logger(__name__)


class OpenSandboxBackend:
    def __init__(self, client: OpenSandboxClient):
        self._client = client

    @staticmethod
    def _opensandbox_id(info: dict) -> str:
        opensandbox_id = (info.get("extended_params") or {}).get("opensandbox_id")
        if not opensandbox_id:
            raise BadRequestRockError("OpenSandbox sandbox metadata is missing opensandbox_id")
        return opensandbox_id

    async def execute(self, sandbox_id: str, info: dict, command: SandboxCommand) -> CommandResponse:
        opensandbox_id = self._opensandbox_id(info)
        command_text = shlex.join(command.command) if isinstance(command.command, list) else command.command
        if isinstance(command.command, str) and not command.shell:
            logger.warning(
                "[%s] OpenSandbox executes string commands with shell semantics although shell=False",
                sandbox_id,
            )
        execution = await self._client.execute(
            opensandbox_id,
            command_text,
            timeout=command.timeout,
            cwd=command.cwd,
            env=command.env,
        )
        stdout = "".join(message.text for message in execution.logs.stdout)
        stderr = "".join(message.text for message in execution.logs.stderr)
        if execution.error:
            stderr = f"{stderr}\n{execution.error}" if stderr else str(execution.error)
        response = CommandResponse(stdout=stdout, stderr=stderr, exit_code=execution.exit_code)
        if command.check and execution.exit_code != 0:
            message = (
                f"Command failed with exit code {execution.exit_code}. "
                f"Stdout:\n{response.stdout!r}\nStderr:\n{response.stderr!r}"
            )
            if command.error_msg:
                message = f"{command.error_msg}: {message}"
            raise NonZeroExitCodeError(message)
        return response

    async def aclose(self) -> None:
        await self._client.aclose()
