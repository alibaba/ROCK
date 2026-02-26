"""K8s provider implementations for managing sandbox resources."""

from abc import abstractmethod
from typing import Any, Optional, Protocol

from kubernetes import client, config as k8s_config
import json
import asyncio

from rock.config import K8sConfig
from rock.deployments.config import DeploymentConfig, DockerDeploymentConfig
from rock.deployments.constants import Port
from rock.sandbox.operator.k8s.constants import K8sConstants
from rock.sandbox.operator.k8s.api_client import K8sApiClient
from rock.sandbox.operator.k8s.template_loader import K8sTemplateLoader
from rock.sandbox.remote_sandbox import RemoteSandboxRuntime
from rock.actions.sandbox.config import RemoteSandboxRuntimeConfig
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.actions.sandbox.response import State
from rock.logger import init_logger


logger = init_logger(__name__)


class K8sProvider(Protocol):
    """Base K8s provider interface.
    
    This protocol defines the standard interface for K8s sandbox providers.
    All provider implementations must support these three core operations:
    - submit: Create and initialize a new sandbox
    - get_status: Retrieve current sandbox status and check if alive
    - stop: Stop and delete a sandbox
    """
    
    def __init__(self, k8s_config: K8sConfig):
        """Initialize provider with K8s configuration.
        
        Args:
            k8s_config: K8sConfig object containing kubeconfig and templates
        """
        ...
    
    async def _ensure_initialized(self):
        """Ensure K8s client is initialized."""
        ...
    
    @abstractmethod
    async def submit(self, config: DeploymentConfig, user_info: dict = {}) -> SandboxInfo:
        """Submit a sandbox deployment.
        
        Args:
            config: Deployment configuration
            user_info: User metadata
            
        Returns:
            SandboxInfo with sandbox metadata
        """
        pass
    
    @abstractmethod
    async def get_status(self, sandbox_id: str) -> SandboxInfo:
        """Get sandbox status.
        
        Args:
            sandbox_id: ID of the sandbox
            
        Returns:
            SandboxInfo with current status
        """
        pass
    
    @abstractmethod
    async def stop(self, sandbox_id: str) -> bool:
        """Stop and delete a sandbox.
        
        Args:
            sandbox_id: ID of the sandbox to delete
            
        Returns:
            True if successful, False otherwise
        """
        pass


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
        
        # Initialize template loader with config templates
        self._template_loader = K8sTemplateLoader(
            templates=k8s_config.templates,
            default_namespace=k8s_config.namespace,
        )
        logger.info(f"Available K8S templates: {', '.join(self._template_loader.available_templates)}")

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
            created_sandbox_id = await self._create(config)

            # Extract and set user info
            user_id = user_info.get("user_id", "default")
            experiment_id = user_info.get("experiment_id", "default")
            namespace = user_info.get("namespace", "default")
            rock_authorization = user_info.get("rock_authorization", "default")

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
            )

            logger.info(f"sandbox {sandbox_id} is submitted")
            return sandbox_info

        except Exception as e:
            logger.error(f"Failed to create sandbox {sandbox_id}: {e}", exc_info=True)
            # Clean up on failure
            try:
                if 'created_sandbox_id' in locals():
                    await self.stop(created_sandbox_id)
                    logger.info(f"Cleaned up failed sandbox {created_sandbox_id}")
            except:
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
            SandboxInfo with current status (without user_info fields)
        """
        from rock.actions.sandbox.response import State

        # Get host_ip and port_mapping
        host_ip, port_mapping = await self._get_sandbox_runtime_info(sandbox_id)

        # Check is_alive through runtime
        is_alive = False
        if host_ip:
            runtime = self._build_runtime(host_ip, port_mapping)
            try:
                is_alive_response = await runtime.is_alive()
                is_alive = is_alive_response.is_alive
            except Exception as e:
                logger.debug(f"Failed to check is_alive for {sandbox_id}: {e}")

        # Build sandbox info with current state
        sandbox_info = SandboxInfo(
            sandbox_id=sandbox_id,
            host_name=sandbox_id,
            host_ip=host_ip,
            port_mapping=port_mapping,
            state=State.RUNNING if is_alive else State.PENDING,
            phases={},
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
                watch_timeout_seconds=self._k8s_config.watch_timeout_seconds,
                watch_reconnect_delay_seconds=self._k8s_config.watch_reconnect_delay_seconds,
            )
            await self._k8s_api.start()
            self._initialized = True
            
            logger.info("Initialized K8s provider with informer")
        except Exception as e:
            logger.error(f"Failed to initialize K8s client: {e}", exc_info=True)
            raise
    
    def _normalize_memory(self, memory: str) -> str:
        """Normalize memory format to Kubernetes standard.
        
        Convert formats like '2g', '2G', '2048m' to K8s format like '2Gi', '2048Mi'.
        """
        import re
        
        # Already in K8s format
        if re.match(r'^\d+(\.\d+)?(Ei|Pi|Ti|Gi|Mi|Ki)$', memory):
            return memory
        
        # Parse value and unit
        match = re.match(r'^(\d+(\.\d+)?)([a-zA-Z]*)$', memory)
        if not match:
            # Fallback: assume it's bytes and convert to Mi
            try:
                bytes_val = int(memory)
                return f"{bytes_val // (1024 * 1024)}Mi"
            except:
                return memory  # Return as-is if can't parse

        value = float(match.group(1))
        unit = match.group(3).lower()

        # Convert to K8s format - use int() for whole numbers, preserve decimals otherwise
        if unit in ('', 'b'):
            mi_value = value / (1024 * 1024)
            return f"{int(mi_value) if mi_value == int(mi_value) else mi_value:.2f}Mi"
        elif unit in ('k', 'kb'):
            return f"{int(value) if value == int(value) else value:.2f}Ki"
        elif unit in ('m', 'mb'):
            return f"{int(value) if value == int(value) else value:.2f}Mi"
        elif unit in ('g', 'gb'):
            return f"{int(value) if value == int(value) else value:.2f}Gi"
        elif unit in ('t', 'tb'):
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
            'apiVersion': K8sConstants.CRD_API_VERSION,
            'kind': K8sConstants.CRD_KIND,
            'metadata': {
                'name': sandbox_id,
                'namespace': self.namespace,
                'labels': {
                    K8sConstants.LABEL_SANDBOX_ID: sandbox_id,
                },
                'annotations': {
                    K8sConstants.ANNOTATION_PORTS: json.dumps(ports_config),
                },
            },
            'spec': {
                'poolRef': pool_name,
                'replicas': 1,
            }
        }
        
        return manifest

    def _build_batchsandbox_manifest(self, config: DockerDeploymentConfig) -> dict[str, Any]:
        """Build BatchSandbox manifest from template and deployment config.
        
        This method uses the template loader to get a base template and only sets
        the user-configurable parameters from SandboxStartRequest:
        - image
        - cpus
        - memory
        
        All other fields (command, volumes, tolerations, etc.) come from the template.
        
        Returns:
            Manifest dictionary
        """
        sandbox_id = config.container_name
        
        # Check if using pool mode
        pool_name = config.extensions.get(K8sConstants.EXT_POOL_NAME)
        if pool_name:
            # Hardcode ports for pool mode
            ports_config = {'proxy': 8000, 'server': 8080, 'ssh': 22}
            manifest = self._build_pool_manifest(sandbox_id, pool_name, ports_config)
            
            logger.debug(f"Built BatchSandbox manifest for {sandbox_id} using pool '{pool_name}' in namespace '{self.namespace}'")
            return manifest
        
        # Template mode: build from template
        # Get template name from extensions or use 'default'
        template_name = config.extensions.get(K8sConstants.EXT_TEMPLATE_NAME, 'default')
        
        # Build manifest using template loader
        manifest = self._template_loader.build_manifest(
            template_name=template_name,
            sandbox_id=sandbox_id,
            image=config.image,
            cpus=config.cpus,
            memory=self._normalize_memory(config.memory),
        )
        
        logger.debug(f"Built BatchSandbox manifest for {sandbox_id} in namespace '{self.namespace}' using template '{template_name}'")
        return manifest

    async def _create(self, config: DockerDeploymentConfig) -> str:
        """Create a BatchSandbox resource without waiting for IP allocation.
        
        Args:
            config: Docker deployment configuration
        
        Returns:
            sandbox_id (same as config.container_name)
            
        Raises:
            Exception: If creation fails or sandbox already exists
        """
        await self._ensure_initialized()
        
        sandbox_id = config.container_name
        
        try:
            manifest = self._build_batchsandbox_manifest(config)
            
            # Create BatchSandbox resource
            await self._k8s_api.create_custom_object(body=manifest)
            
            logger.info(f"Created BatchSandbox: {sandbox_id} in namespace: {self.namespace}")
            return sandbox_id
            
        except client.exceptions.ApiException as e:
            if e.status == 409:
                logger.warning(f"BatchSandbox {sandbox_id} already exists")
                raise Exception(f"Sandbox {sandbox_id} already exists")
            logger.error(f"Failed to create BatchSandbox: {e}", exc_info=True)
            raise Exception(f"Failed to create sandbox: {e.reason}")
        except Exception as e:
            logger.error(f"Unexpected error creating sandbox: {e}", exc_info=True)
            raise

    async def _get_sandbox_runtime_info(self, sandbox_id: str) -> tuple[str, dict[int, int]]:
        """Get sandbox runtime info (host_ip and port_mapping).
        
        Args:
            sandbox_id: ID of the sandbox
            
        Returns:
            tuple: (host_ip, port_mapping)
            - host_ip: Pod IP from endpoints annotation (empty string if not allocated)
            - port_mapping: Port configuration from annotations
            
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
            annotations = metadata.get("annotations", {})
            
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
                Port.PROXY: ports_config['proxy'],
                Port.SERVER: ports_config['server'],
                Port.SSH: ports_config['ssh'],
            }
            
            return host_ip, port_mapping
            
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

