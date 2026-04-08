"""K8s provider implementations for managing sandbox resources."""

import json
import re
from abc import ABC, abstractmethod
from typing import Any, Protocol

from kubernetes import client
from kubernetes import config as k8s_config

from rock.actions.sandbox.config import RemoteSandboxRuntimeConfig
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.config import K8sConfig, PoolConfig
from rock.deployments.config import DeploymentConfig, DockerDeploymentConfig
from rock.deployments.constants import Port
from rock.logger import init_logger
from rock.sandbox.operator.k8s.api_client import K8sApiClient
from rock.sandbox.operator.k8s.constants import K8sConstants
from rock.sandbox.operator.k8s.template_loader import K8sTemplateLoader
from rock.sandbox.remote_sandbox import RemoteSandboxRuntime

logger = init_logger(__name__)


class PoolSelector(ABC):
    """Abstract base class for pool selection strategy."""

    @abstractmethod
    def select_pool(self, config: DockerDeploymentConfig, pools: dict[str, PoolConfig]) -> str | None:
        """Select a pool for the given deployment config.

        Args:
            config: Docker deployment configuration
            pools: Available pools configuration

        Returns:
            Selected pool name or None if no pool matches
        """
        pass


class ResourceMatchingPoolSelector(PoolSelector):
    """Pool selector that matches by image and resource requirements.

    Selection criteria:
    1. Pool image must exactly match config image
    2. Pool resources (cpus, memory) must be >= config requirements
    3. Among all matching pools, select the one with smallest resource capacity
    """

    def _parse_memory_to_mb(self, memory: str) -> float:
        """Parse memory string to MB for comparison."""
        memory = memory.lower().strip()

        # Extract number and unit
        match = re.match(r"^(\d+(\.\d+)?)\s*([a-z]*)$", memory)
        if not match:
            try:
                return float(memory) / (1024 * 1024)  # Assume bytes
            except (ValueError, TypeError):
                return 0

        value = float(match.group(1))
        unit = match.group(3)

        # Convert to MB
        if unit in ("", "b"):
            return value / (1024 * 1024)
        elif unit in ("k", "kb"):
            return value / 1024
        elif unit in ("m", "mb", "mi"):
            return value
        elif unit in ("g", "gb", "gi"):
            return value * 1024
        elif unit in ("t", "tb", "ti"):
            return value * 1024 * 1024
        else:
            return 0

    def _get_pool_resource_score(self, pool_config: PoolConfig) -> float:
        """Calculate resource score for a pool (lower is better for selection)."""
        memory_mb = self._parse_memory_to_mb(pool_config.memory)
        # Normalize memory to GB and add to cpus for a unified score (lower = smaller capacity)
        return pool_config.cpus + memory_mb / 1024

    def select_pool(self, config: DockerDeploymentConfig, pools: dict[str, PoolConfig]) -> str | None:
        """Select best matching pool based on image and resource requirements."""
        if not pools:
            return None

        config_memory_mb = self._parse_memory_to_mb(config.memory)
        matching_pools: list[tuple[str, PoolConfig, float]] = []

        for pool_name, pool_config in pools.items():
            # Check image match
            if pool_config.image != config.image:
                continue

            # Check resource capacity (pool must have >= required resources)
            pool_memory_mb = self._parse_memory_to_mb(pool_config.memory)
            if pool_config.cpus < config.cpus or pool_memory_mb < config_memory_mb:
                continue

            # Calculate score for this matching pool
            score = self._get_pool_resource_score(pool_config)
            matching_pools.append((pool_name, pool_config, score))

        if not matching_pools:
            return None

        # Select pool with smallest resource capacity (best fit)
        matching_pools.sort(key=lambda x: x[2])
        best_pool_name, _, _ = matching_pools[0]
        return best_pool_name


class K8sProvider(Protocol):
    """Interface for K8s sandbox providers.

    All provider implementations must support these three core operations:
    - submit: Create a new sandbox
    - get_status: Retrieve current sandbox status
    - stop: Stop and delete a sandbox
    """

    async def submit(self, config: DockerDeploymentConfig, user_info: dict = {}) -> SandboxInfo:
        """Submit a sandbox deployment.

        Args:
            config: Deployment configuration
            user_info: User metadata

        Returns:
            SandboxInfo with sandbox metadata
        """
        ...

    async def get_status(self, sandbox_id: str) -> SandboxInfo:
        """Get sandbox status.

        Args:
            sandbox_id: ID of the sandbox

        Returns:
            SandboxInfo with current status
        """
        ...

    async def stop(self, sandbox_id: str) -> bool:
        """Stop and delete a sandbox.

        Args:
            sandbox_id: ID of the sandbox to delete

        Returns:
            True if successful, False otherwise
        """
        ...


class BatchSandboxProvider(K8sProvider):
    """Provider for BatchSandbox CRD with Informer-based local cache.

    This provider uses Kubernetes watch API to maintain a local cache of BatchSandbox
    resources. All get_status queries read from this cache instead of querying API Server,
    which significantly improves performance and reduces API Server load.

    The watch task runs in the background and automatically reconnects on network failures.
    """

    def __init__(self, k8s_config: K8sConfig):
        """Initialize BatchSandbox provider.

        Args:
            k8s_config: K8sConfig object containing kubeconfig and templates
        """
        self.kubeconfig_path = k8s_config.kubeconfig_path
        self.namespace = k8s_config.namespace
        self._k8s_config = k8s_config
        self._api_client = None
        self._k8s_api: K8sApiClient | None = None
        self._initialized = False
        self._nacos_provider = None

        # Initialize template loader with config templates
        self._template_loader = K8sTemplateLoader(
            templates=k8s_config.templates,
            default_namespace=k8s_config.namespace,
        )
        logger.info(f"Available K8S templates: {', '.join(self._template_loader.available_templates)}")

    def set_nacos_provider(self, nacos_provider):
        """Set Nacos config provider for dynamic pool configuration.

        Args:
            nacos_provider: NacosConfigProvider instance
        """
        self._nacos_provider = nacos_provider
        logger.info("Set nacos provider for K8s provider")

    async def _get_pools(self) -> dict[str, PoolConfig]:
        """Get pool configurations from Nacos.

        Returns:
            Dictionary of pool name to PoolConfig
        """
        if self._nacos_provider:
            nacos_config = await self._nacos_provider.get_config()
            if nacos_config and K8sConstants.NACOS_POOLS_KEY in nacos_config:
                pools_data = nacos_config[K8sConstants.NACOS_POOLS_KEY]
                pools = {}
                for name, config in pools_data.items():
                    if isinstance(config, dict):
                        pools[name] = PoolConfig(**config)
                    else:
                        pools[name] = config
                logger.debug(f"Loaded {len(pools)} pools from Nacos")
                return pools

        return {}

    async def submit(self, config: DockerDeploymentConfig, user_info: dict = {}) -> SandboxInfo:
        """Create a sandbox and return sandbox info immediately without waiting for IP.

        Args:
            config: Docker deployment configuration
            user_info: User metadata (user_id, experiment_id, namespace, rock_authorization)

        Returns:
            SandboxInfo with sandbox metadata (state=PENDING, no IP/ports yet)
        """
        from rock.actions.sandbox.response import State

        sandbox_id = config.container_name
        logger.info(f"Creating sandbox {sandbox_id}")

        try:
            # Create the sandbox without waiting for IP
            created_sandbox_id, resource_version = await self._create(config)

            # Extract and set user info
            user_id = user_info.get("user_id", "default")
            experiment_id = user_info.get("experiment_id", "default")
            namespace = user_info.get("namespace", "default")
            rock_authorization = user_info.get("rock_authorization", "default")
            extended_params = dict(config.extended_params)
            extended_params[K8sConstants.EXT_RESOURCE_VERSION] = resource_version

            # Build sandbox info with empty IP and port_mapping
            sandbox_info = SandboxInfo(
                sandbox_id=created_sandbox_id,
                host_name=created_sandbox_id,
                host_ip="",
                user_id=user_id,
                experiment_id=experiment_id,
                namespace=namespace,
                rock_authorization=rock_authorization,
                image=config.image,
                cpus=config.cpus,
                memory=config.memory,
                port_mapping={},
                state=State.PENDING,
                phases={},
                extended_params=extended_params,
            )

            logger.info(f"sandbox {sandbox_id} is submitted")
            return sandbox_info

        except Exception as e:
            logger.error(f"Failed to create sandbox {sandbox_id}: {e}", exc_info=True)
            # Clean up on failure
            try:
                await self.stop(sandbox_id)
                logger.info(f"Cleaned up failed sandbox {sandbox_id}")
            except Exception:
                pass
            raise

    async def get_status(self, sandbox_id: str) -> SandboxInfo:
        """Get sandbox status and check if alive.

        This method fetches the sandbox resource from K8s and checks if the sandbox
        is alive by calling its is_alive endpoint. The state is determined by:
        - RUNNING: IP allocated AND is_alive returns true
        - PENDING: IP not allocated OR is_alive returns false

        Args:
            sandbox_id: Sandbox identifier

        Returns:
            SandboxInfo with current status and resource_version in extended_params
            (without user_info fields)
        """
        from rock.actions.sandbox.response import State

        # Get host_ip, port_mapping and resource_version
        host_ip, port_mapping, resource_version = await self._get_sandbox_runtime_info(sandbox_id)

        # Check is_alive through runtime
        is_alive = False
        if host_ip:
            runtime = self._build_runtime(host_ip, port_mapping)
            try:
                is_alive_response = await runtime.is_alive()
                is_alive = is_alive_response.is_alive
            except Exception as e:
                logger.debug(f"Failed to check is_alive for {sandbox_id}: {e}")

        # Build sandbox info with current state and resource_version
        sandbox_info = SandboxInfo(
            sandbox_id=sandbox_id,
            host_name=sandbox_id,
            host_ip=host_ip,
            port_mapping=port_mapping,
            state=State.RUNNING if is_alive else State.PENDING,
            phases={},
            extended_params={K8sConstants.EXT_RESOURCE_VERSION: resource_version},
        )

        return sandbox_info

    async def stop(self, sandbox_id: str) -> bool:
        """Delete a BatchSandbox resource."""
        await self._ensure_initialized()

        logger.info(f"Deleting BatchSandbox: {sandbox_id}")

        try:
            await self._k8s_api.delete_custom_object(name=sandbox_id)
            logger.info(f"Deleted BatchSandbox: {sandbox_id} from namespace: {self.namespace}")
            return True

        except client.exceptions.ApiException as e:
            if e.status == 404:
                logger.warning(f"BatchSandbox {sandbox_id} not found, already deleted")
                return True
            logger.error(f"Failed to delete sandbox {sandbox_id}: {e}", exc_info=True)
            return False
        except Exception as e:
            logger.error(f"Unexpected error deleting sandbox {sandbox_id}: {e}", exc_info=True)
            return False

    async def _ensure_initialized(self):
        """Ensure K8s client is initialized and start watch task.

        Lazy initialization of K8S client and API abstraction layer:
        1. Load kubeconfig (from file, in-cluster, or default)
        2. Create K8sApiClient with rate limiting and caching
        3. Start background watch task for cache synchronization

        Thread-safe: Uses _initialized flag to prevent duplicate initialization.
        """
        if self._initialized:
            return

        try:
            if self.kubeconfig_path:
                k8s_config.load_kube_config(config_file=self.kubeconfig_path)
            else:
                # Try in-cluster config first, fallback to default kubeconfig
                try:
                    k8s_config.load_incluster_config()
                except k8s_config.ConfigException:
                    k8s_config.load_kube_config()

            self._api_client = client.ApiClient()
            self._k8s_api = K8sApiClient(
                api_client=self._api_client,
                group=K8sConstants.CRD_GROUP,
                version=K8sConstants.CRD_VERSION,
                plural=K8sConstants.CRD_PLURAL,
                namespace=self.namespace,
                qps=self._k8s_config.api_qps,
                resync_period=self._k8s_config.resync_period,
            )
            await self._k8s_api.start()
            self._initialized = True

            logger.info("Initialized K8s provider with informer")
        except Exception as e:
            logger.error(f"Failed to initialize K8s client: {e}", exc_info=True)
            raise

    async def _get_pool_name(self, config: DockerDeploymentConfig) -> str | None:
        """Get pool name using selection strategy.

        Priority:
        1. Check extended_params for explicit pool name
        2. Use ResourceMatchingPoolSelector to find best matching pool

        Args:
            config: Docker deployment configuration

        Returns:
            Pool name if found, None otherwise
        """
        # Priority 1: Check extended_params for explicit pool name
        pool_name = config.extended_params.get(K8sConstants.EXT_POOL_NAME)
        if pool_name:
            return pool_name

        # Priority 2: Use pool selector to find best match
        pools = await self._get_pools()
        logger.info(f"Available pools from Nacos: {list(pools.keys())}")
        return ResourceMatchingPoolSelector().select_pool(config, pools)

    def _get_template_name(self, config: DockerDeploymentConfig) -> str:
        """Get template name from extended_params or config template_map.

        Priority:
        1. Check extended_params for template name
        2. Fallback to template_map based on image_os matching
        3. Return 'default' if not found

        Args:
            config: Docker deployment configuration

        Returns:
            Template name (defaults to 'default')
        """
        # Priority 1: Check extended_params
        template_name = config.extended_params.get(K8sConstants.EXT_TEMPLATE_NAME)
        if template_name:
            return template_name

        # Priority 2: Check template_map based on image_os
        if config.image_os and self._k8s_config.template_map:
            mapped_template = self._k8s_config.template_map.get(config.image_os)
            if mapped_template:
                return mapped_template

        # Priority 3: Return default
        return "default"

    def _normalize_memory(self, memory: str) -> str:
        """Normalize memory format to Kubernetes standard.

        Convert formats like '2g', '2G', '2048m' to K8s format like '2Gi', '2048Mi'.
        """
        # Already in K8s format
        if re.match(r"^\d+(\.\d+)?(Ei|Pi|Ti|Gi|Mi|Ki)$", memory):
            return memory

        # Parse value and unit
        match = re.match(r"^(\d+(\.\d+)?)([a-zA-Z]*)$", memory)
        if not match:
            # Fallback: assume it's bytes and convert to Mi
            try:
                bytes_val = int(memory)
                return f"{bytes_val // (1024 * 1024)}Mi"
            except (ValueError, TypeError):
                return memory  # Return as-is if can't parse

        value = float(match.group(1))
        unit = match.group(3).lower()

        # Convert to K8s format - use int() for whole numbers, preserve decimals otherwise
        if unit in ("", "b"):
            mi_value = value / (1024 * 1024)
            return f"{int(mi_value) if mi_value == int(mi_value) else mi_value:.2f}Mi"
        elif unit in ("k", "kb"):
            return f"{int(value) if value == int(value) else value:.2f}Ki"
        elif unit in ("m", "mb"):
            return f"{int(value) if value == int(value) else value:.2f}Mi"
        elif unit in ("g", "gb"):
            return f"{int(value) if value == int(value) else value:.2f}Gi"
        elif unit in ("t", "tb"):
            return f"{int(value) if value == int(value) else value:.2f}Ti"
        else:
            return memory

    def _build_pool_manifest(self, sandbox_id: str, pool_name: str, ports_config: dict[str, int]) -> dict[str, Any]:
        """Build BatchSandbox manifest for pool mode.

        Args:
            sandbox_id: Sandbox identifier
            pool_name: Pool name to reference
            ports_config: Port configuration dictionary

        Returns:
            Manifest dictionary
        """

        manifest = {
            "apiVersion": K8sConstants.CRD_API_VERSION,
            "kind": K8sConstants.CRD_KIND,
            "metadata": {
                "name": sandbox_id,
                "namespace": self.namespace,
                "labels": {
                    K8sConstants.LABEL_SANDBOX_ID: sandbox_id,
                },
                "annotations": {
                    K8sConstants.ANNOTATION_PORTS: json.dumps(ports_config),
                },
            },
            "spec": {
                "poolRef": pool_name,
                "replicas": 1,
            },
        }

        return manifest

    async def _get_pool_ports(self, pool_name: str) -> dict[str, int]:
        """Get port configuration for a pool from config.

        Args:
            pool_name: Name of the pool

        Returns:
            Port configuration dict with proxy, server, ssh ports
        """
        pools = await self._get_pools()
        pool_config = pools.get(pool_name)
        if pool_config:
            return pool_config.ports
        # Fallback to defaults if pool not found
        return {"proxy": 8000, "server": 8080, "ssh": 22}

    async def _build_batchsandbox_manifest(self, config: DockerDeploymentConfig) -> dict[str, Any]:
        """Build BatchSandbox manifest from template and deployment config.

        Uses the template loader to get a base template and applies
        user-specified parameters from DockerDeploymentConfig (image, cpus, memory).
        All other fields (command, volumes, tolerations, etc.) come from the template.

        Returns:
            Manifest dictionary
        """
        sandbox_id = config.container_name

        # Check if using pool mode
        pool_name = await self._get_pool_name(config)

        if pool_name:
            ports_config = await self._get_pool_ports(pool_name)
            manifest = self._build_pool_manifest(sandbox_id, pool_name, ports_config)

            logger.debug(
                f"Built BatchSandbox manifest for {sandbox_id} using pool '{pool_name}' in namespace '{self.namespace}'"
            )
            return manifest

        # Template mode: build from template
        template_name = self._get_template_name(config)

        # Build manifest using template loader
        manifest = self._template_loader.build_manifest(
            template_name=template_name,
            sandbox_id=sandbox_id,
            image=config.image,
            cpus=config.cpus,
            memory=self._normalize_memory(config.memory),
        )

        logger.debug(
            f"Built BatchSandbox manifest for {sandbox_id} in namespace '{self.namespace}' using template '{template_name}'"
        )
        return manifest

    async def _create(self, config: DockerDeploymentConfig) -> tuple[str, str]:
        """Create a BatchSandbox resource without waiting for IP allocation.

        Args:
            config: Docker deployment configuration

        Returns:
            tuple: (sandbox_id, resource_version)
            - sandbox_id: same as config.container_name
            - resource_version: K8s resource version for optimistic concurrency control

        Raises:
            Exception: If creation fails or sandbox already exists
        """
        await self._ensure_initialized()

        sandbox_id = config.container_name

        try:
            manifest = await self._build_batchsandbox_manifest(config)

            # Create BatchSandbox resource and get the created object
            created_resource = await self._k8s_api.create_custom_object(body=manifest)
            resource_version = created_resource.get("metadata", {}).get("resourceVersion", "")

            logger.info(f"Created BatchSandbox: {sandbox_id} in namespace: {self.namespace}")
            return sandbox_id, resource_version

        except client.exceptions.ApiException as e:
            if e.status == 409:
                logger.warning(f"BatchSandbox {sandbox_id} already exists")
                raise Exception(f"Sandbox {sandbox_id} already exists")
            logger.error(f"Failed to create BatchSandbox: {e}", exc_info=True)
            raise Exception(f"Failed to create sandbox: {e.reason}")
        except Exception as e:
            logger.error(f"Unexpected error creating sandbox: {e}", exc_info=True)
            raise

    async def _get_sandbox_runtime_info(self, sandbox_id: str) -> tuple[str, dict[int, int], str]:
        """Get sandbox runtime info (host_ip, port_mapping and resource_version).

        Args:
            sandbox_id: ID of the sandbox

        Returns:
            tuple: (host_ip, port_mapping, resource_version)
            - host_ip: Pod IP from endpoints annotation (empty string if not allocated)
            - port_mapping: Port configuration from annotations
            - resource_version: K8s resource version for optimistic concurrency control

        Raises:
            Exception: If sandbox not found
            ValueError: If ports annotation is missing or invalid
        """
        await self._ensure_initialized()

        try:
            # Get from cache or API Server (handled by api_client)
            resource = await self._k8s_api.get_custom_object(name=sandbox_id)

            # Extract metadata
            metadata = resource.get("metadata", {})
            resource_version = metadata.get("resourceVersion", "")
            annotations = metadata.get("annotations", {})

            # Check if resource is being deleted
            deletion_timestamp = metadata.get("deletionTimestamp")
            if deletion_timestamp:
                raise Exception(f"Sandbox '{sandbox_id}' is being deleted (deletionTimestamp: {deletion_timestamp})")

            # Parse endpoints from annotations
            endpoints_str = annotations.get(K8sConstants.ANNOTATION_ENDPOINTS)
            pod_ips = []
            if endpoints_str:
                try:
                    pod_ips = json.loads(endpoints_str)
                except (json.JSONDecodeError, TypeError):
                    logger.warning(f"Failed to parse endpoints for {sandbox_id}: {endpoints_str}")

            # Get pod IP for host_ip if available
            host_ip = pod_ips[0] if pod_ips else ""

            # Get port configuration from annotations
            ports_str = annotations.get(K8sConstants.ANNOTATION_PORTS)
            if not ports_str:
                raise ValueError(
                    f"Sandbox '{sandbox_id}' is missing required '{K8sConstants.ANNOTATION_PORTS}' annotation. "
                    f"This sandbox may have been created with an older version."
                )

            try:
                ports_config = json.loads(ports_str)
            except (json.JSONDecodeError, TypeError) as e:
                raise ValueError(
                    f"Failed to parse ports annotation for sandbox '{sandbox_id}': {ports_str}. Error: {e}"
                )

            # Build port_mapping
            port_mapping = {
                Port.PROXY: ports_config["proxy"],
                Port.SERVER: ports_config["server"],
                Port.SSH: ports_config["ssh"],
            }

            return host_ip, port_mapping, resource_version

        except Exception as e:
            logger.error(f"Failed to fetch resource from cache for {sandbox_id}: {e}", exc_info=True)
            raise

    def _build_runtime(self, host_ip: str, port_mapping: dict[int, int]) -> RemoteSandboxRuntime:
        """Build runtime for communicating with the sandbox.

        Args:
            host_ip: Pod IP address
            port_mapping: Port mapping configuration

        Returns:
            RemoteSandboxRuntime instance
        """
        proxy_port = port_mapping.get(Port.PROXY, 8000)
        runtime_config = RemoteSandboxRuntimeConfig(
            host=f"http://{host_ip}",
            port=proxy_port,
        )
        return RemoteSandboxRuntime.from_config(runtime_config)
