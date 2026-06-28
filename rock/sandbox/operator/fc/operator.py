"""FC Operator implementation for managing sandboxes via Function Compute."""

import asyncio
import json
import uuid

from rock.actions.sandbox.response import State
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.common.constants import StopReason
from rock.config import FCConfig
from rock.deployments.config import DeploymentConfig
from rock.logger import init_logger
from rock.sandbox.operator.abstract import AbstractOperator
from rock.sandbox.operator.fc.config import FCOperatorConfig
from rock.sandbox.operator.fc.runtime import FCRuntime

logger = init_logger(__name__)

SESSION_AFFINITY_HEADER = "x-rock-session-id"


class FCOperator(AbstractOperator):
    """Operator for managing sandboxes via Alibaba Cloud Function Compute.

    Architecture (two-layer design):

    Layer 1 - Sandbox Template (FC Function):
        Functions are created per unique configuration (image + resources + env + session config).
        A template hash is computed from these fields to enable reuse.
        Reference counting tracks how many sandbox instances use each function.

    Layer 2 - Sandbox Instance (FC Session):
        Each sandbox instance is a session on a function, created via InvokeFunction
        with x-rock-session-id header for session affinity.
    """

    def __init__(self, fc_config: FCConfig):
        self._fc_config = fc_config
        self._runtimes: dict[str, FCRuntime] = {}
        self._runtime_configs: dict[str, FCOperatorConfig] = {}
        self._sandbox_functions: dict[str, str] = {}
        self._function_cache: dict[str, str] = {}
        self._function_refs: dict[str, int] = {}
        self._runtimes_lock = asyncio.Lock()
        self._function_lock = asyncio.Lock()
        self._fc_client = None
        self._client_lock = asyncio.Lock()

    async def _ensure_fc_client(self):
        """Ensure FC SDK client is initialized."""
        async with self._client_lock:
            if self._fc_client is None:
                try:
                    from alibabacloud_fc20230330.client import Client
                    from alibabacloud_tea_openapi.models import Config

                    config = Config(
                        region_id=self._fc_config.region,
                        access_key_id=self._fc_config.access_key_id,
                        access_key_secret=self._fc_config.access_key_secret,
                        security_token=self._fc_config.security_token,
                    )
                    self._fc_client = Client(config)
                    logger.info(f"FC SDK client initialized for region {self._fc_config.region}")
                except ImportError:
                    raise RuntimeError("FC SDK not available - install alibabacloud_fc20230330")
                except Exception as e:
                    logger.error(f"Failed to initialize FC SDK client: {e}")
                    raise

    async def _create_function(self, config: FCOperatorConfig, function_name: str) -> str:
        """Create FC function via SDK for custom-container runtime with session affinity."""
        await self._ensure_fc_client()

        from alibabacloud_fc20230330.models import (
            CreateFunctionInput,
            CreateFunctionRequest,
            CustomContainerConfig,
            HeaderFieldSessionAffinityConfig,
        )
        from alibabacloud_tea_util.models import RuntimeOptions

        custom_container_config = CustomContainerConfig(
            image=config.image,
            command=[],
            acceleration_type="Default",
        )

        sa_config = HeaderFieldSessionAffinityConfig(
            affinity_header_field_name=SESSION_AFFINITY_HEADER,
            session_concurrency_per_instance=1,
            session_idle_timeout_in_seconds=config.session_idle_timeout or 300,
            session_ttlin_seconds=config.session_ttl or self._fc_config.default_session_ttl,
        )
        sa_config_str = json.dumps(sa_config.to_map())

        env_vars = dict(config.env or {})
        idle_timeout = config.session_idle_timeout or self._fc_config.default_session_idle_timeout
        env_vars["ROCK_SESSION_IDLE_TIMEOUT"] = str(idle_timeout)

        body = CreateFunctionInput(
            function_name=function_name,
            runtime="custom-container",
            handler="index.handler",
            memory_size=config.memory or self._fc_config.default_memory,
            cpu=config.cpus or self._fc_config.default_cpus,
            timeout=int(config.function_timeout or self._fc_config.default_function_timeout),
            custom_container_config=custom_container_config,
            environment_variables=env_vars,
            disk_size=10240,
            description=f"ROCK sandbox template (hash={config.template_hash()})",
            session_affinity="HEADER_FIELD",
            session_affinity_config=sa_config_str,
            instance_concurrency=200,
            idle_timeout=config.session_idle_timeout,
        )

        request = CreateFunctionRequest(body=body)
        runtime = RuntimeOptions()

        try:
            await asyncio.to_thread(
                self._fc_client.create_function_with_options,
                request,
                None,
                runtime,
            )
            logger.info(f"FC function {function_name} created successfully")
            return function_name
        except Exception as e:
            error_str = str(e)
            if "AlreadyExists" in error_str or "already exists" in error_str.lower():
                logger.info(f"FC function {function_name} already exists, adopting it")
                return function_name
            logger.error(f"Failed to create FC function {function_name}: {e}")
            raise RuntimeError(f"Failed to create FC function: {e}")

    async def _get_or_create_function(self, config: FCOperatorConfig) -> str:
        """Get an existing function by template hash, or create a new one.

        Uses double-checked locking to prevent concurrent duplicate creation.
        """
        template_hash = config.template_hash()

        # Fast path: check cache without lock
        if template_hash in self._function_cache:
            function_name = self._function_cache[template_hash]
            logger.info(f"Reusing existing FC function {function_name} for template hash {template_hash}")
            return function_name

        # Slow path: acquire lock and double-check
        async with self._function_lock:
            if template_hash in self._function_cache:
                function_name = self._function_cache[template_hash]
                logger.info(f"Reusing existing FC function {function_name} for template hash {template_hash}")
                return function_name

            function_name = f"rock-tpl-{template_hash}"

            if await self._function_exists(function_name):
                logger.info(f"FC function {function_name} already exists, adopting it")
            else:
                function_name = await self._create_function(config, function_name)

            self._function_cache[template_hash] = function_name
            return function_name

    async def _function_exists(self, function_name: str) -> bool:
        """Check if a function exists on FC via GetFunction API."""
        await self._ensure_fc_client()

        try:
            from alibabacloud_fc20230330.models import GetFunctionRequest
            from alibabacloud_tea_util.models import RuntimeOptions

            request = GetFunctionRequest()
            runtime = RuntimeOptions()
            await asyncio.to_thread(
                self._fc_client.get_function_with_options,
                function_name,
                request,
                None,
                runtime,
            )
            return True
        except Exception:
            return False

    async def _delete_function(self, function_name: str) -> bool:
        """Delete FC function via SDK."""
        await self._ensure_fc_client()

        from alibabacloud_tea_util.models import RuntimeOptions

        runtime = RuntimeOptions()

        try:
            await asyncio.to_thread(
                self._fc_client.delete_function_with_options,
                function_name,
                None,
                runtime,
            )
            logger.info(f"FC function {function_name} deleted successfully")
            return True
        except Exception as e:
            logger.warning(f"Failed to delete FC function {function_name}: {e}")
            return False

    async def submit(self, config: FCOperatorConfig, user_info: dict = {}) -> SandboxInfo:
        """Submit (start) an FC sandbox with template reuse."""
        merged_config = config.merge_with_fc_config(self._fc_config)
        session_id = merged_config.session_id or f"fc-{uuid.uuid4().hex[:12]}"

        if not merged_config.image:
            raise RuntimeError("image is required for FC sandbox (custom-container runtime)")

        final_config = FCOperatorConfig(
            type="fc",
            session_id=session_id,
            function_name=merged_config.function_name,
            region=merged_config.region,
            account_id=merged_config.account_id,
            access_key_id=merged_config.access_key_id,
            access_key_secret=merged_config.access_key_secret,
            security_token=merged_config.security_token,
            image=merged_config.image,
            env=merged_config.env,
            memory=merged_config.memory,
            cpus=merged_config.cpus,
            session_ttl=merged_config.session_ttl,
            session_idle_timeout=merged_config.session_idle_timeout,
            function_timeout=merged_config.function_timeout,
            extended_params=merged_config.extended_params,
        )

        try:
            function_name = await self._get_or_create_function(final_config)
        except Exception as e:
            logger.error(f"Failed to get/create FC function for {session_id}: {e}")
            raise

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
            env=merged_config.env,
            memory=merged_config.memory,
            cpus=merged_config.cpus,
            session_ttl=merged_config.session_ttl,
            session_idle_timeout=merged_config.session_idle_timeout,
            function_timeout=merged_config.function_timeout,
            extended_params=merged_config.extended_params,
        )

        runtime = FCRuntime(final_config, fc_client=self._fc_client)

        try:
            from rock.actions import CreateSessionRequest

            await runtime.create_session(CreateSessionRequest(session=session_id))
        except Exception as e:
            logger.error(f"Failed to create FC session for {session_id}: {e}")
            raise

        async with self._runtimes_lock:
            self._runtimes[session_id] = runtime
            self._runtime_configs[session_id] = final_config
            self._sandbox_functions[session_id] = function_name
            self._function_refs[function_name] = self._function_refs.get(function_name, 0) + 1

        logger.info(
            f"FC sandbox {session_id} submitted with function {function_name} (ref_count={self._function_refs[function_name]})"
        )

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
        """Get FC sandbox status."""
        async with self._runtimes_lock:
            runtime = self._runtimes.get(sandbox_id)
            config = self._runtime_configs.get(sandbox_id)

        if runtime is None:
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

    async def stop(self, sandbox_id: str, reason: StopReason = StopReason.MANUAL) -> bool:
        """Stop an FC sandbox instance and manage function template lifecycle."""
        async with self._runtimes_lock:
            runtime = self._runtimes.pop(sandbox_id, None)
            _config = self._runtime_configs.pop(sandbox_id, None)  # noqa: F841
            function_name = self._sandbox_functions.pop(sandbox_id, None)

        if runtime:
            try:
                await runtime.close()
                logger.info(f"FC sandbox {sandbox_id} session closed")
            except Exception as e:
                logger.warning(f"Failed to close FC runtime for {sandbox_id}: {e}")

            if function_name:
                async with self._runtimes_lock:
                    ref_count = self._function_refs.get(function_name, 0) - 1
                    if ref_count <= 0:
                        self._function_refs.pop(function_name, None)
                        template_hash = None
                        for h, fn in self._function_cache.items():
                            if fn == function_name:
                                template_hash = h
                                break
                        if template_hash:
                            self._function_cache.pop(template_hash, None)

                        try:
                            await self._delete_function(function_name)
                            logger.info(f"FC function {function_name} deleted (last instance stopped)")
                        except Exception as e:
                            logger.warning(f"Failed to delete FC function {function_name}: {e}")
                    else:
                        self._function_refs[function_name] = ref_count
                        logger.info(f"FC function {function_name} kept ({ref_count} instances still active)")

            logger.info(f"FC sandbox {sandbox_id} stopped successfully")
            return True

        logger.warning(f"FC sandbox {sandbox_id} not found in local tracking")
        return True

    async def restart(self, config: DeploymentConfig, host_ip: str | None = None) -> SandboxInfo:
        """Restart an FC sandbox instance.

        FC sandboxes are stateless (session state is in-memory per instance).
        Restart stops the current session and creates a new one.
        """
        sandbox_id = config.session_id if hasattr(config, "session_id") else config.container_name
        logger.info(f"Restarting FC sandbox {sandbox_id}")
        await self.stop(sandbox_id, reason=StopReason.MANUAL)
        return await self.submit(config)

    async def delete(self, config: DeploymentConfig, host_ip: str | None = None) -> bool:
        """Delete an FC sandbox instance (equivalent to stop + cleanup)."""
        sandbox_id = config.session_id if hasattr(config, "session_id") else config.container_name
        logger.info(f"Deleting FC sandbox {sandbox_id}")
        return await self.stop(sandbox_id, reason=StopReason.MANUAL)

    async def cleanup_orphaned_functions(self) -> int:
        """Delete FC functions with rock-tpl- prefix that are not in the active cache.

        Returns:
            Number of orphaned functions deleted.
        """
        await self._ensure_fc_client()

        from alibabacloud_fc20230330.models import ListFunctionsRequest
        from alibabacloud_tea_util.models import RuntimeOptions

        deleted = 0
        try:
            request = ListFunctionsRequest()
            runtime = RuntimeOptions()
            response = await asyncio.to_thread(
                self._fc_client.list_functions_with_options,
                request,
                None,
                runtime,
            )

            active_functions = set(self._function_cache.values())

            if response.body and response.body.functions:
                for func in response.body.functions:
                    func_name = getattr(func, "function_name", None)
                    if func_name and func_name.startswith("rock-tpl-") and func_name not in active_functions:
                        if await self._delete_function(func_name):
                            deleted += 1
        except Exception as e:
            logger.warning(f"Failed to list functions for cleanup: {e}")

        logger.info(f"Cleanup orphaned functions: deleted {deleted} functions")
        return deleted
