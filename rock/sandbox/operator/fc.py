"""FC Operator implementation for managing sandboxes via Function Compute."""

import asyncio
import uuid

from rock.actions.sandbox.response import State
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.config import FCConfig
from rock.deployments.config import FCDeploymentConfig
from rock.deployments.fc import FCDeployment
from rock.logger import init_logger
from rock.sandbox.operator.abstract import AbstractOperator

logger = init_logger(__name__)


class FCOperator(AbstractOperator):
    """Operator for managing sandboxes via Alibaba Cloud Function Compute.

    This operator manages FC sandbox lifecycle using the WebSocket session API
    for stateful bash sessions. It tracks deployments locally and integrates
    with SandboxManager through the AbstractOperator interface.

    Architecture:
        SandboxManager -> FCOperator -> FCDeployment -> FCRuntime -> FCSessionManager

    Key features:
    - Session affinity via x-rock-session-id header
    - Automatic config merge with FCConfig defaults
    - Local deployment tracking with asyncio lock for thread safety
    """

    def __init__(self, fc_config: FCConfig):
        """Initialize FC operator with configuration.

        Args:
            fc_config: FCConfig from RockConfig containing default credentials and settings.
        """
        self._fc_config = fc_config
        self._deployments: dict[str, FCDeployment] = {}
        self._deployments_lock = asyncio.Lock()

    async def submit(self, config: FCDeploymentConfig, user_info: dict = {}) -> SandboxInfo:
        """Submit (start) an FC sandbox with session affinity.

        Args:
            config: FCDeploymentConfig with sandbox settings.
            user_info: User metadata (user_id, experiment_id, namespace).

        Returns:
            SandboxInfo with sandbox_id, host_name, and other metadata.

        Raises:
            RuntimeError: If FC deployment fails to start.
        """
        # Merge with FCConfig defaults from Admin config
        merged_config = config.merge_with_fc_config(self._fc_config)

        # Generate session_id (serves as both FC session_id and ROCK sandbox_id)
        session_id = merged_config.session_id or f"fc-{uuid.uuid4().hex[:12]}"

        # Create final config with resolved session_id
        final_config = FCDeploymentConfig(
            type="fc",
            session_id=session_id,
            function_name=merged_config.function_name,
            region=merged_config.region,
            account_id=merged_config.account_id,
            access_key_id=merged_config.access_key_id,
            access_key_secret=merged_config.access_key_secret,
            security_token=merged_config.security_token,
            memory=merged_config.memory,
            cpus=merged_config.cpus,
            session_ttl=merged_config.session_ttl,
            session_idle_timeout=merged_config.session_idle_timeout,
            function_timeout=merged_config.function_timeout,
        )

        deployment = FCDeployment.from_config(final_config)

        try:
            await deployment.start()
        except Exception as e:
            logger.error(f"Failed to start FC deployment for {session_id}: {e}")
            raise

        # Track deployment after successful start
        async with self._deployments_lock:
            self._deployments[session_id] = deployment

        logger.info(f"FC sandbox {session_id} submitted successfully")

        return SandboxInfo(
            sandbox_id=session_id,
            type="fc",
            function_name=final_config.function_name,
            region=final_config.region,
            memory=final_config.memory,
            cpus=final_config.cpus,
            state=State.RUNNING,
            host_name=f"{final_config.account_id}.{final_config.region}.fc.aliyuncs.com",
            host_ip=None,
            user_id=user_info.get("user_id", "default"),
            experiment_id=user_info.get("experiment_id", "default"),
            namespace=user_info.get("namespace", "default"),
        )

    async def get_status(self, sandbox_id: str) -> SandboxInfo:
        """Get FC sandbox status.

        Args:
            sandbox_id: The sandbox/session ID to check.

        Returns:
            SandboxInfo with current status.

        Raises:
            ValueError: If sandbox not found in local tracking.
        """
        async with self._deployments_lock:
            deployment = self._deployments.get(sandbox_id)

        if deployment is None:
            # Check Redis for sandbox info if deployment not tracked locally
            if self._redis_provider:
                from rock.admin.core.redis_key import alive_sandbox_key

                sandbox_data = await self._redis_provider.json_get(alive_sandbox_key(sandbox_id), "$")
                if sandbox_data and len(sandbox_data) > 0:
                    return sandbox_data[0]

            raise ValueError(f"FC sandbox {sandbox_id} not found")

        is_alive = await deployment.is_alive()
        config = deployment.config

        return SandboxInfo(
            sandbox_id=sandbox_id,
            type="fc",
            function_name=config.function_name,
            region=config.region,
            memory=config.memory,
            cpus=config.cpus,
            state=State.RUNNING if is_alive.is_alive else State.PENDING,
            host_name=f"{config.account_id}.{config.region}.fc.aliyuncs.com",
            host_ip=None,
        )

    async def stop(self, sandbox_id: str) -> bool:
        """Stop an FC sandbox.

        Args:
            sandbox_id: The sandbox/session ID to stop.

        Returns:
            True if sandbox was stopped successfully.
        """
        async with self._deployments_lock:
            deployment = self._deployments.pop(sandbox_id, None)

        if deployment:
            try:
                await deployment.stop()
                logger.info(f"FC sandbox {sandbox_id} stopped")
                return True
            except Exception as e:
                logger.error(f"Failed to stop FC sandbox {sandbox_id}: {e}")
                return False

        # Not tracked locally, but still return success for cleanup
        logger.warning(f"FC sandbox {sandbox_id} not found in local tracking")
        return True