"""K8S template loader for BatchSandbox manifests."""

import copy
import json
from typing import Any

import jinja2

from rock.logger import init_logger
from rock.sandbox.operator.k8s.constants import K8sConstants
from rock.utils.jinja_render import render_node

logger = init_logger(__name__)


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
        sandbox_id: str | None = None,
        image: str | None = None,
        cpus: float | None = None,
        memory: str | None = None,
        disk: str | None = None,
        num_gpus: int | None = None,
        accelerator_type: str | None = None,
        limit_cpus: float | None = None,
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

        CPU overcommit: ``cpus`` is the scheduler reservation (rendered into
        ``requests.cpu``), ``limit_cpus`` is the cgroup hard cap (rendered into
        ``limits.cpu``). When ``limit_cpus`` is None we fall back to ``cpus`` so
        that ``requests.cpu == limits.cpu`` and behaviour matches the pre-
        overcommit baseline. A ``limit_cpus > cpus`` value lets the container
        burst above its reservation, mirroring the Ray path's ``--cpus`` flag.

        Args:
            template_name: Name of the template to use.
            sandbox_id: Sandbox identifier (auto-generated if missing).
            image: Container image (rendered into the template via {{ image }}).
            cpus: CPU resource value (rendered via {{ cpus }}).
            memory: Memory resource value (rendered via {{ memory }}).
            disk: Disk resource value (rendered via {{ disk }}).
            num_gpus: GPU count (rendered via {{ num_gpus }}).
            accelerator_type: GPU model (rendered via {{ accelerator_type }}).
            limit_cpus: CPU hard cap for overcommit (rendered via
                {{ limit_cpus }}). When None, falls back to ``cpus`` to keep
                requests.cpu == limits.cpu.

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

        # limit_cpus falls back to cpus so templates that always reference
        # {{ limit_cpus }} keep working when overcommit is not set.
        effective_limit_cpus = limit_cpus if limit_cpus is not None else cpus

        # num_gpus stays numeric so templates can do arithmetic; cpus str-coerced to pin float->"4.0" formatting.
        ctx = {
            "sandbox_id": sandbox_id,
            "template_name": template_name,
            "image": image if image is not None else "",
            "cpus": str(cpus) if cpus is not None else "",
            "memory": memory if memory is not None else "",
            "disk": disk if disk is not None else "",
            "num_gpus": num_gpus if num_gpus is not None else "",
            "accelerator_type": accelerator_type if accelerator_type is not None else "",
            "limit_cpus": str(effective_limit_cpus) if effective_limit_cpus is not None else "",
        }

        rendered = render_node(config, self._jinja_env, ctx)

        enable_resource_speedup = rendered.get("enable_resource_speedup", False)
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
