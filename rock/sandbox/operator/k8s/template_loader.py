"""K8S template loader for BatchSandbox manifests."""

import copy
import json
from pathlib import Path
from typing import Any, Dict

import yaml

from rock.logger import init_logger
from rock.sandbox.operator.k8s.constants import K8sConstants

logger = init_logger(__name__)


class K8sTemplateLoader:
    """Loader for K8S BatchSandbox templates."""
    
    def __init__(self, templates: Dict[str, Dict[str, Any]], default_namespace: str = 'rock'):
        """Initialize template loader.
        
        Args:
            templates: Dictionary of template configurations from K8sConfig
            default_namespace: Default namespace if template doesn't specify one
        """
        self._templates: Dict[str, Dict[str, Any]] = templates
        self._default_namespace = default_namespace
        
        if not self._templates:
            raise ValueError(
                "No templates provided. At least one template must be defined in K8sConfig.templates."
            )
        
        logger.info(f"Loaded {len(self._templates)} K8S templates from config")
        logger.debug(f"Available templates: {', '.join(self._templates.keys())}")
    
    def get_template(self, template_name: str = 'default') -> Dict[str, Any]:
        """Get a template by name.
        
        Args:
            template_name: Name of the template
            
        Returns:
            Deep copy of the template dictionary
            
        Raises:
            ValueError: If template not found
        """
        if template_name not in self._templates:
            available = ', '.join(self._templates.keys())
            raise ValueError(f"Template '{template_name}' not found. Available: {available}")
        
        return copy.deepcopy(self._templates[template_name])
    
    def build_manifest(
        self,
        template_name: str = 'default',
        sandbox_id: str = None,
        image: str = None,
        cpus: float = None,
        memory: str = None,
    ) -> Dict[str, Any]:
        """Build a complete BatchSandbox manifest from template.
        
        Template structure:
        - namespace: K8S namespace for the sandbox (REQUIRED)
        - ports: custom port configuration (not part of K8S manifest)
        - template: corresponds to spec.template in BatchSandbox CRD
          - template.metadata -> spec.template.metadata
          - template.spec -> spec.template.spec (Pod spec)
        
        Top-level fields are hardcoded:
        - apiVersion: sandbox.opensandbox.io/v1alpha1
        - kind: BatchSandbox
        - metadata: constructed from parameters
        - spec.replicas: always 1
        
        Args:
            template_name: Name of the template to use
            sandbox_id: Sandbox identifier
            image: Container image
            cpus: CPU resource limit
            memory: Memory resource limit (normalized format like '2Gi')
            
        Returns:
            Complete BatchSandbox manifest
        """
        import uuid
        
        # Get template configuration
        config = self.get_template(template_name)
        
        # Use default namespace (configured at startup)
        namespace = self._default_namespace
        
        # Get enable_resource_speedup from template (default to True)
        enable_resource_speedup = config.get('enable_resource_speedup', True)
        
        # Get port configuration from template (required)
        ports_config = config.get('ports')
        if not ports_config:
            raise ValueError(
                f"Template '{template_name}' is missing required 'ports' configuration. "
                f"Each template must define ports (proxy, server, ssh)."
            )
        
        # Extract template (corresponds to spec.template in BatchSandbox)
        pod_template = config.get('template', {})
        template_metadata = copy.deepcopy(pod_template.get('metadata', {}))
        pod_spec = copy.deepcopy(pod_template.get('spec', {}))
        
        # Generate sandbox_id if not provided
        if not sandbox_id:
            sandbox_id = f"sandbox-{uuid.uuid4().hex[:8]}"
        
        # Build top-level BatchSandbox manifest (hardcoded structure)
        manifest = {
            'apiVersion': K8sConstants.CRD_API_VERSION,
            'kind': K8sConstants.CRD_KIND,
            'metadata': {
                'name': sandbox_id,
                'namespace': namespace,
                'labels': {
                    K8sConstants.LABEL_SANDBOX_ID: sandbox_id,
                    K8sConstants.LABEL_TEMPLATE: template_name,
                },
                'annotations': {
                    K8sConstants.ANNOTATION_PORTS: json.dumps(ports_config),
                }
            },
            'spec': {
                'replicas': 1,  # Always 1 for sandbox
                'template': {
                    'metadata': template_metadata,
                    'spec': pod_spec
                }
            }
        }
        
        # Add resource speedup label if enabled
        if enable_resource_speedup:
            manifest['metadata']['labels'][K8sConstants.LABEL_RESOURCE_SPEEDUP] = 'true'
        
        # Add sandbox-id label to template metadata
        if 'labels' not in manifest['spec']['template']['metadata']:
            manifest['spec']['template']['metadata']['labels'] = {}
        manifest['spec']['template']['metadata']['labels'][K8sConstants.LABEL_SANDBOX_ID] = sandbox_id
        
        # Set container image
        if image:
            containers = pod_spec.get('containers', [])
            if containers and len(containers) > 0:
                containers[0]['image'] = image
        
        # Set resources if provided
        if cpus is not None or memory is not None:
            containers = pod_spec.get('containers', [])
            if containers and len(containers) > 0:
                if 'resources' not in containers[0]:
                    containers[0]['resources'] = {}
                
                if cpus is not None or memory is not None:
                    containers[0]['resources']['requests'] = {}
                    containers[0]['resources']['limits'] = {}
                    
                    if cpus is not None:
                        containers[0]['resources']['requests']['cpu'] = str(cpus)
                        containers[0]['resources']['limits']['cpu'] = str(cpus)
                    
                    if memory is not None:
                        containers[0]['resources']['requests']['memory'] = memory
                        containers[0]['resources']['limits']['memory'] = memory
        
        return manifest
    
    @property
    def available_templates(self) -> list[str]:
        """Get list of available template names."""
        return list(self._templates.keys())
    

