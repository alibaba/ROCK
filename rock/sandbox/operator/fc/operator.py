"""FC Operator implementation for managing sandboxes via Function Compute."""

import asyncio
import uuid

from rock.actions.sandbox.response import State
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.config import FCConfig
from rock.logger import init_logger
from rock.sandbox.operator.abstract import AbstractOperator
from rock.sandbox.operator.fc.config import FCOperatorConfig
from rock.sandbox.operator.fc.runtime import FCRuntime

logger = init_logger(__name__)


class FCOperator(AbstractOperator):
    """Operator for managing sandboxes via Alibaba Cloud Function Compute.

    This operator manages FC sandbox lifecycle using the WebSocket session API
    for stateful bash sessions. It creates FC functions dynamically per sandbox
    (single-function-single-session design) and tracks FCRuntime instances directly
    without using the Deployment pattern.

    Architecture:
        SandboxManager -> FCOperator -> FC SDK (CreateFunction) -> FCRuntime -> FCSessionManager

    Key features:
    - Dynamic function creation via FC SDK (alibabacloud_fc20230330)
    - Custom-container runtime mode with user-provided images
    - Session affinity via x-rock-session-id header
    - Automatic config merge with FCConfig defaults
    - Local runtime tracking with asyncio lock for thread safety
    - Direct FCRuntime management (no Deployment wrapper)
    """

    def __init__(self, fc_config: FCConfig):
        """Initialize FC operator with configuration.

        Args:
            fc_config: FCConfig from RockConfig containing default credentials and settings.
        """
        self._fc_config = fc_config
        self._runtimes: dict[str, FCRuntime] = {}
        self._runtime_configs: dict[str, FCOperatorConfig] = {}
        self._function_names: dict[str, str] = {}  # sandbox_id -> function_name mapping
        self._runtimes_lock = asyncio.Lock()
        self._fc_client = None
        self._client_lock = asyncio.Lock()

    async def _ensure_fc_client(self):
        """Ensure FC SDK client is initialized."""
        async with self._client_lock:
            if self._fc_client is None:
                try:
                    from alibabacloud_fc20230330.client import Client
                    from alibabacloud_openapi_openapi.models import Config

                    config = Config(
                        region_id=self._fc_config.region,
                        access_key_id=self._fc_config.access_key_id,
                        access_key_secret=self._fc_config.access_key_secret,
                        security_token=self._fc_config.security_token,
                    )
                    self._fc_client = Client(config)
                    logger.info(f"FC SDK client initialized for region {self._fc_config.region}")
                except ImportError:
                    logger.warning("alibabacloud_fc20230330 not installed, dynamic function creation disabled")
                    raise RuntimeError("FC SDK not available - install alibabacloud_fc20230330")
                except Exception as e:
                    logger.error(f"Failed to initialize FC SDK client: {e}")
                    raise

    async def _create_function(self, config: FCOperatorConfig) -> str:
        """Create FC function via SDK for custom-container runtime.

        Args:
            config: FCOperatorConfig with function parameters including image.

        Returns:
            The created function name.

        Raises:
            RuntimeError: If function creation fails.
        """
        await self._ensure_fc_client()

        from alibabacloud_fc20230330.models import (
            CreateFunctionRequest,
            CreateFunctionRequestCAConfig,
            CreateFunctionRequestCustomContainerConfig,
        )

        # Generate unique function name based on session_id
        function_name = f"rock-sandbox-{config.session_id.replace('fc-', '', 1) if config.session_id else uuid.uuid4().hex[:8]}"

        # Build custom-container config
        custom_container_config = CreateFunctionRequestCustomContainerConfig(
            image=config.image,
            command="[]",  # Empty command - let container run its default
            args="[]",
            acceleration_type="Default",  # Container acceleration for faster cold start
        )

        # Build CA config for stateful session support
        ca_config = CreateFunctionRequestCAConfig(
            on_demand_config=None,
            singleton_concurrency=1,  # Single instance per session
            singleton_lifetime=config.session_ttl or self._fc_config.default_session_ttl,
        )

        # Create function request
        request = CreateFunctionRequest(
            function_name=function_name,
            runtime="custom-container",  # Custom-container runtime mode
            handler="index.handler",  # Placeholder for custom-container
            memory_size=config.memory or self._fc_config.default_memory,
            cpu=config.cpus or self._fc_config.default_cpus,
            timeout=int(config.function_timeout or self._fc_config.default_function_timeout),
            custom_container_config=custom_container_config,
            ca_config=ca_config,
            disk_size=10240,  # 10GB default disk size
            instance_type="e1",  # Elastic instance type
            description=f"ROCK sandbox {config.session_id}",
        )

        try:
            response = await asyncio.to_thread(
                self._fc_client.create_function_with_options,
                request,
                None,  # runtime options
            )
            logger.info(f"FC function {function_name} created successfully: {response.body.function_name}")
            return function_name
        except Exception as e:
            logger.error(f"Failed to create FC function {function_name}: {e}")
            raise RuntimeError(f"Failed to create FC function: {e}")

    async def _delete_function(self, function_name: str) -> bool:
        """Delete FC function via SDK.

        Args:
            function_name: The function to delete.

        Returns:
            True if deletion successful, False otherwise.
        """
        await self._ensure_fc_client()

        from alibabacloud_fc20230330.models import DeleteFunctionRequest

        request = DeleteFunctionRequest(function_name=function_name)

        try:
            await asyncio.to_thread(
                self._fc_client.delete_function_with_options,
                request,
                None,
            )
            logger.info(f"FC function {function_name} deleted successfully")
            return True
        except Exception as e:
            logger.warning(f"Failed to delete FC function {function_name}: {e}")
            return False

    async def submit(self, config: FCOperatorConfig, user_info: dict = {}) -> SandboxInfo:
        """Submit (start) an FC sandbox with dynamic function creation.

        Creates a custom-container FC function using the provided image,
        then establishes a WebSocket session for stateful bash operations.

        Args:
            config: FCOperatorConfig with sandbox settings including image.
            user_info: User metadata (user_id, experiment_id, namespace).

        Returns:
            SandboxInfo with sandbox_id, host_name, and other metadata.

        Raises:
            RuntimeError: If FC function creation or session fails.
        """
        # Merge with FCConfig defaults from Admin config
        merged_config = config.merge_with_fc_config(self._fc_config)

        # Generate session_id (serves as both FC session_id and ROCK sandbox_id)
        session_id = merged_config.session_id or f"fc-{uuid.uuid4().hex[:12]}"

        # Require image for custom-container runtime
        if not merged_config.image:
            raise RuntimeError("image is required for FC sandbox (custom-container runtime)")

        # Create final config with resolved session_id
        final_config = FCOperatorConfig(
            type="fc",
            session_id=session_id,
            function_name=merged_config.function_name,  # Will be updated after function creation
            region=merged_config.region,
            account_id=merged_config.account_id,
            access_key_id=merged_config.access_key_id,
            access_key_secret=merged_config.access_key_secret,
            security_token=merged_config.security_token,
            image=merged_config.image,
            memory=merged_config.memory,
            cpus=merged_config.cpus,
            session_ttl=merged_config.session_ttl,
            session_idle_timeout=merged_config.session_idle_timeout,
            function_timeout=merged_config.function_timeout,
            extended_params=merged_config.extended_params,
        )

        # Create FC function via SDK (dynamic creation per sandbox)
        try:
            function_name = await self._create_function(final_config)
        except Exception as e:
            logger.error(f"Failed to create FC function for {session_id}: {e}")
            raise

        # Update config with the actual function name created
        final_config = FCOperatorConfig(
            type="fc",
            session_id=session_id,
            function_name=function_name,
            region=merged_config.region,
            account_id=merged_config.account_id,
            access_key_id=merged_config.access_key_id,
            access_key_secret=merged_config.access_key_secret,
            security_token=merged_config.security_token,
            image=merged_config.image,
            memory=merged_config.memory,
            cpus=merged_config.cpus,
            session_ttl=merged_config.session_ttl,
            session_idle_timeout=merged_config.session_idle_timeout,
            function_timeout=merged_config.function_timeout,
            extended_params=merged_config.extended_params,
        )

        # Create FCRuntime directly (no Deployment wrapper)
        runtime = FCRuntime(final_config)

        try:
            # Create WebSocket session for this sandbox
            await runtime.session_manager.create_session(session_id)
        except Exception as e:
            logger.error(f"Failed to create FC session for {session_id}, cleaning up function: {e}")
            # Clean up the created function if session creation fails
            await self._delete_function(function_name)
            raise

        # Track runtime, config, and function_name after successful session creation
        async with self._runtimes_lock:
            self._runtimes[session_id] = runtime
            self._runtime_configs[session_id] = final_config
            self._function_names[session_id] = function_name

        logger.info(f"FC sandbox {session_id} submitted successfully with function {function_name}")

        return SandboxInfo(
            sandbox_id=session_id,
            type="fc",
            function_name=function_name,
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
        """Stop an FC sandbox and delete its function.

        Closes the WebSocket session and deletes the FC function created for this sandbox.

        Args:
            sandbox_id: The sandbox/session ID to stop.

        Returns:
            True if sandbox was stopped successfully.
        """
        async with self._runtimes_lock:
            runtime = self._runtimes.pop(sandbox_id, None)
            _config = self._runtime_configs.pop(sandbox_id, None)  # noqa: F841 - config cleanup
            function_name = self._function_names.pop(sandbox_id, None)

        if runtime:
            try:
                # Close the runtime (session cleanup)
                await runtime.close()
                logger.info(f"FC sandbox {sandbox_id} session closed")
            except Exception as e:
                logger.warning(f"Failed to close FC runtime for {sandbox_id}: {e}")

            # Delete the FC function (cleanup resources)
            if function_name:
                try:
                    await self._delete_function(function_name)
                    logger.info(f"FC function {function_name} deleted for sandbox {sandbox_id}")
                except Exception as e:
                    logger.warning(f"Failed to delete FC function {function_name}: {e}")

            logger.info(f"FC sandbox {sandbox_id} stopped successfully")
            return True

        # Not tracked locally, but still return success for cleanup
        logger.warning(f"FC sandbox {sandbox_id} not found in local tracking")
        return True
