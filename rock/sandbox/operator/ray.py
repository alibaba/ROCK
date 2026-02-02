from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.deployments.config import DeploymentConfig
from rock.sandbox.operator.abstract import AbstractOperator


class RayOperator(AbstractOperator):
    async def submit(self, config: DeploymentConfig) -> SandboxInfo:
        return SandboxInfo(sandbox_id="test", host_name="test", host_ip="test")

    async def get_status(self, sandbox_id: str) -> SandboxInfo:
        return SandboxInfo(sandbox_id="test", host_name="test", host_ip="test")

    async def stop(self, sandbox_id: str) -> bool:
        return True
