"""FC Operator implementation for managing sandboxes via Function Compute."""

import asyncio
import uuid

from rock.actions.sandbox.response import State
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.config import FCConfig
from rock.deployments.config import FCDeploymentConfig
from rock.deployments.fc import FCRuntime
from rock.logger import init_logger
from rock.sandbox.operator.abstract import AbstractOperator

logger = init_logger(__name__)


class FCOperator(AbstractOperator):
    """Operator for managing sandboxes via Alibaba Cloud Function Compute.

    This operator manages FC sandbox lifecycle using the WebSocket session API
    for stateful bash sessions. It tracks FCRuntime instances directly without
    using the Deployment pattern.

    Architecture:
        SandboxManager -> FCOperator -> FCRuntime -> FCSessionManager

    Key features:
    - Session affinity via x-rock-session-id header
    - Automatic config merge with FCConfig defaults
    - Local runtime tracking with asyncio lock for thread safety
    - Direct FCRuntime management (no FCDeployment wrapper)
    """

    def __init__(self, fc_config: FCConfig):
        """Initialize FC operator with configuration.

        Args:
            fc_config: FCConfig from RockConfig containing default credentials and settings.
        """
        self._fc_config = fc_config
        self._runtimes: dict[str, FCRuntime] = {}
        self._runtime_configs: dict[str, FCDeploymentConfig] = {}
        self._runtimes_lock = asyncio.Lock()

    async def submit(self, config: FCDeploymentConfig, user_info: dict = {}) -> SandboxInfo:
        """Submit (start) an FC sandbox with session affinity.

        Args:
            config: FCDeploymentConfig with sandbox settings.
            user_info: User metadata (user_id, experiment_id, namespace).

        Returns:
            SandboxInfo with sandbox_id, host_name, and other metadata.

        Raises:
            RuntimeError: If FC runtime fails to start.
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
            extended_params=merged_config.extended_params,
        )

        # Create FCRuntime directly (no FCDeployment wrapper)
        runtime = FCRuntime(final_config)

        try:
            # Create WebSocket session for this sandbox
            await runtime.session_manager.create_session(session_id)
        except Exception as e:
            logger.error(f"Failed to create FC session for {session_id}: {e}")
            raise

        # Track runtime and config after successful session creation
        async with self._runtimes_lock:
            self._runtimes[session_id] = runtime
            self._runtime_configs[session_id] = final_config

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
        async with self._runtimes_lock:
            runtime = self._runtimes.get(sandbox_id)
            config = self._runtime_configs.get(sandbox_id)

        if runtime is None:
            # Check Redis for sandbox info if runtime not tracked locally
            if self._redis_provider:
                from rock.admin.core.redis_key import alive_sandbox_key

                sandbox_data = await self._redis_provider.json_get(alive_sandbox_key(sandbox_id), "$")
                if sandbox_data and len(sandbox_data) > 0:
                    return sandbox_data[0]

            raise ValueError(f"FC sandbox {sandbox_id} not found")

        is_alive = await runtime.is_alive()

        return SandboxInfo(
            sandbox_id=sandbox_id,
            type="fc",
            function_name=config.function_name if config else None,
            region=config.region if config else None,
            memory=config.memory if config else None,
            cpus=config.cpus if config else None,
            state=State.RUNNING if is_alive.is_alive else State.PENDING,
            host_name=f"{config.account_id}.{config.region}.fc.aliyuncs.com" if config else None,
            host_ip=None,
        )

    async def stop(self, sandbox_id: str) -> bool:
        """Stop an FC sandbox.

        Args:
            sandbox_id: The sandbox/session ID to stop.

        Returns:
            True if sandbox was stopped successfully.
        """
        async with self._runtimes_lock:
            runtime = self._runtimes.pop(sandbox_id, None)
            self._runtime_configs.pop(sandbox_id, None)

        if runtime:
            try:
                await runtime.close()
                logger.info(f"FC sandbox {sandbox_id} stopped")
                return True
            except Exception as e:
                logger.error(f"Failed to stop FC sandbox {sandbox_id}: {e}")
                return False

        # Not tracked locally, but still return success for cleanup
        logger.warning(f"FC sandbox {sandbox_id} not found in local tracking")
        return True