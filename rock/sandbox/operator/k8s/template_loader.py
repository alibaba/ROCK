"""K8S template loader for BatchSandbox manifests."""

import copy
import json
from collections.abc import Mapping
from typing import Any

import jinja2

from rock.logger import init_logger
from rock.sandbox.operator.k8s.constants import K8sConstants

logger = init_logger(__name__)

_DROP = object()  # sentinel: rendered placeholder collapsed to empty


def _render_node(node: Any, env: jinja2.Environment, ctx: Mapping[str, Any]) -> Any:
    """Recursively render a template node with Jinja2.

    Strings containing ``{{`` are rendered against ``ctx``; an empty
    rendered result yields the ``_DROP`` sentinel so the caller can
    remove the key (in a dict) or skip the element (in a list).
    """
    if isinstance(node, str):
        if "{{" not in node:
            return node
        rendered = env.from_string(node).render(**ctx).strip()
        if rendered == "":
            return _DROP
        return rendered
    if isinstance(node, dict):
        result: dict[Any, Any] = {}
        for k, v in node.items():
            rendered = _render_node(v, env, ctx)
            if rendered is _DROP:
                continue
            result[k] = rendered
        return result
    if isinstance(node, list):
        result_list: list[Any] = []
        for item in node:
            rendered = _render_node(item, env, ctx)
            if rendered is _DROP:
                continue
            result_list.append(rendered)
        return result_list
    return node


class K8sTemplateLoader:
    """Loader for K8S BatchSandbox templates."""

    def __init__(self, templates: dict[str, dict[str, Any]], default_namespace: str = "rock"):
        """Initialize template loader.

        Args:
            templates: Dictionary of template configurations from K8sConfig
            default_namespace: Default namespace if template doesn't specify one
        """
        self._templates: dict[str, dict[str, Any]] = templates
        self._default_namespace = default_namespace

        if not self._templates:
            raise ValueError("No templates provided. At least one template must be defined in K8sConfig.templates.")

        self._jinja_env = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)

        logger.info(f"Loaded {len(self._templates)} K8S templates from config")
        logger.debug(f"Available templates: {', '.join(self._templates.keys())}")

    def get_template(self, template_name: str = "default") -> dict[str, Any]:
        """Get a template by name.

        Args:
            template_name: Name of the template

        Returns:
            Deep copy of the template dictionary

        Raises:
            ValueError: If template not found
        """
        if template_name not in self._templates:
            available = ", ".join(self._templates.keys())
            raise ValueError(f"Template '{template_name}' not found. Available: {available}")

        return copy.deepcopy(self._templates[template_name])

    def build_manifest(
        self,
        template_name: str = "default",
        sandbox_id: str = None,
        image: str = None,
        cpus: float = None,
        memory: str = None,
        num_gpus: int = None,
        accelerator_type: str = None,
    ) -> dict[str, Any]:
        """Build a complete BatchSandbox manifest from template.

        The template is rendered with Jinja2: every string value is treated as
        a Jinja2 template against a ``ctx`` built from the call arguments.
        ``None`` arguments enter ``ctx`` as ``""`` so that:

        * plain ``{{ var }}`` placeholders collapse to empty strings and the
          drop-empty rule removes the surrounding dict key / list element;
        * ``{{ var | default('x', true) }}`` placeholders fall back to the
          template-supplied default.

        The CRD wrapper (apiVersion/kind/metadata/spec.replicas) and the
        sandbox-id / template / resource-speedup labels and ports annotation
        are still assembled in code, since they are structural rather than
        configurable.

        Args:
            template_name: Name of the template to use.
            sandbox_id: Sandbox identifier (auto-generated if missing).
            image: Container image (rendered into the template via {{ image }}).
            cpus: CPU resource value (rendered via {{ cpus }}).
            memory: Memory resource value (rendered via {{ memory }}).
            num_gpus: GPU count (rendered via {{ num_gpus }}).
            accelerator_type: GPU model (rendered via {{ accelerator_type }}).

        Returns:
            Complete BatchSandbox manifest.
        """
        import uuid

        config = self.get_template(template_name)

        ports_config = config.get("ports")
        if not ports_config:
            raise ValueError(
                f"Template '{template_name}' is missing required 'ports' configuration. "
                f"Each template must define ports (proxy, server, ssh)."
            )

        if not sandbox_id:
            sandbox_id = f"sandbox-{uuid.uuid4().hex[:8]}"

        ctx = {
            "sandbox_id": sandbox_id,
            "template_name": template_name,
            "image": image if image is not None else "",
            "cpus": str(cpus) if cpus is not None else "",
            "memory": memory if memory is not None else "",
            "num_gpus": num_gpus if num_gpus is not None else "",
            "accelerator_type": accelerator_type if accelerator_type is not None else "",
        }

        rendered = _render_node(config, self._jinja_env, ctx)

        enable_resource_speedup = rendered.get("enable_resource_speedup", True)
        pod_template = rendered.get("template", {})
        template_metadata = pod_template.get("metadata", {})
        pod_spec = pod_template.get("spec", {})

        manifest = {
            "apiVersion": K8sConstants.CRD_API_VERSION,
            "kind": K8sConstants.CRD_KIND,
            "metadata": {
                "name": sandbox_id,
                "namespace": self._default_namespace,
                "labels": {
                    K8sConstants.LABEL_SANDBOX_ID: sandbox_id,
                    K8sConstants.LABEL_TEMPLATE: template_name,
                },
                "annotations": {
                    K8sConstants.ANNOTATION_PORTS: json.dumps(ports_config),
                },
            },
            "spec": {
                "replicas": 1,
                "template": {"metadata": template_metadata, "spec": pod_spec},
            },
        }

        if enable_resource_speedup:
            manifest["metadata"]["labels"][K8sConstants.LABEL_RESOURCE_SPEEDUP] = "true"

        if "labels" not in manifest["spec"]["template"]["metadata"]:
            manifest["spec"]["template"]["metadata"]["labels"] = {}
        manifest["spec"]["template"]["metadata"]["labels"][K8sConstants.LABEL_SANDBOX_ID] = sandbox_id

        return manifest

    @property
    def available_templates(self) -> list[str]:
        """Get list of available template names."""
        return list(self._templates.keys())
