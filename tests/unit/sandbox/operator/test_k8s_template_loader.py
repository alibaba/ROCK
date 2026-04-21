"""Unit tests for K8sTemplateLoader."""

import pytest

from rock.sandbox.operator.k8s.constants import K8sConstants
from rock.sandbox.operator.k8s.template_loader import K8sTemplateLoader


class TestK8sTemplateLoader:
    """Test cases for K8sTemplateLoader."""

    def test_initialization_success(self, basic_templates):
        """Test successful template loader initialization."""
        loader = K8sTemplateLoader(templates=basic_templates, default_namespace="rock-test")

        assert loader._default_namespace == "rock-test"
        assert len(loader._templates) == 1
        assert "default" in loader.available_templates

    def test_initialization_without_templates(self):
        """Test initialization fails without templates."""
        with pytest.raises(ValueError, match="No templates provided"):
            K8sTemplateLoader(templates={}, default_namespace="rock-test")

    def test_get_template_success(self, template_loader):
        """Test getting template by name."""
        template = template_loader.get_template("default")

        assert template is not None
        assert "ports" in template
        assert "template" in template
        assert template["ports"]["proxy"] == 8000

    def test_get_template_not_found(self, template_loader):
        """Test getting non-existent template."""
        with pytest.raises(ValueError, match="Template 'nonexistent' not found"):
            template_loader.get_template("nonexistent")

    def test_get_template_returns_copy(self, template_loader):
        """Test that get_template returns a deep copy."""
        template1 = template_loader.get_template("default")
        template2 = template_loader.get_template("default")

        # Modify first template
        template1["ports"]["proxy"] = 9999

        # Second template should not be affected
        assert template2["ports"]["proxy"] == 8000

    def test_build_manifest_basic(self, template_loader):
        """Test building basic manifest."""
        manifest = template_loader.build_manifest(
            template_name="default", sandbox_id="test-sandbox", image="python:3.11", cpus=2.0, memory="4Gi"
        )

        # Verify top-level structure
        assert manifest["apiVersion"] == K8sConstants.CRD_API_VERSION
        assert manifest["kind"] == K8sConstants.CRD_KIND
        assert manifest["metadata"]["name"] == "test-sandbox"
        assert manifest["metadata"]["namespace"] == "rock-test"

        # Verify labels
        assert manifest["metadata"]["labels"][K8sConstants.LABEL_SANDBOX_ID] == "test-sandbox"
        assert manifest["metadata"]["labels"][K8sConstants.LABEL_TEMPLATE] == "default"

        # Verify annotations (ports stored as JSON)
        assert K8sConstants.ANNOTATION_PORTS in manifest["metadata"]["annotations"]

        # Verify spec
        assert manifest["spec"]["replicas"] == 1
        assert "template" in manifest["spec"]

    def test_build_manifest_with_resources(self, template_loader):
        """Test building manifest with CPU and memory resources."""
        manifest = template_loader.build_manifest(
            template_name="default", sandbox_id="test-sandbox", cpus=4.0, memory="8Gi"
        )

        container = manifest["spec"]["template"]["spec"]["containers"][0]

        # Verify resource requests and limits
        assert container["resources"]["requests"]["cpu"] == "4.0"
        assert container["resources"]["limits"]["cpu"] == "4.0"
        assert container["resources"]["requests"]["memory"] == "8Gi"
        assert container["resources"]["limits"]["memory"] == "8Gi"

    def test_build_manifest_without_resources(self, template_loader):
        """Test building manifest without specifying resources."""
        manifest = template_loader.build_manifest(
            template_name="default",
            sandbox_id="test-sandbox",
        )

        container = manifest["spec"]["template"]["spec"]["containers"][0]

        # Should not have any concrete resource values when nothing is specified.
        # The Jinja2-based render keeps the template's resources skeleton but
        # drops any keys whose placeholder rendered to empty (cpus, memory).
        assert "resources" in container
        resources = container["resources"]
        assert resources.get("requests", {}) == {}
        assert resources.get("limits", {}) == {}

    def test_build_manifest_with_custom_image(self, template_loader):
        """Test building manifest with custom image."""
        manifest = template_loader.build_manifest(
            template_name="default", sandbox_id="test-sandbox", image="ubuntu:22.04"
        )

        container = manifest["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == "ubuntu:22.04"

    def test_build_manifest_missing_ports_in_template(self):
        """Test building manifest fails when template lacks ports config."""
        templates = {"no-ports": {"template": {"spec": {"containers": [{"name": "main"}]}}}}

        loader = K8sTemplateLoader(templates=templates, default_namespace="rock-test")

        with pytest.raises(ValueError, match="missing required 'ports' configuration"):
            loader.build_manifest(template_name="no-ports", sandbox_id="test")

    def test_build_manifest_with_resource_speedup(self):
        """Test building manifest with resource speedup label."""
        templates = {
            "speedup": {
                "enable_resource_speedup": True,
                "ports": {"proxy": 8000, "server": 8080, "ssh": 22},
                "template": {"spec": {"containers": [{"name": "main"}]}},
            }
        }

        loader = K8sTemplateLoader(templates=templates, default_namespace="rock-test")
        manifest = loader.build_manifest(template_name="speedup", sandbox_id="test")

        assert manifest["metadata"]["labels"][K8sConstants.LABEL_RESOURCE_SPEEDUP] == "true"

    def test_build_manifest_auto_generate_sandbox_id(self, template_loader):
        """Test building manifest auto-generates sandbox_id if not provided."""
        manifest = template_loader.build_manifest(template_name="default")

        sandbox_id = manifest["metadata"]["name"]
        assert sandbox_id.startswith("sandbox-")
        assert len(sandbox_id) > 8  # Should have UUID suffix

    def test_available_templates_property(self, template_loader):
        """Test available_templates property."""
        templates = template_loader.available_templates

        assert isinstance(templates, list)
        assert "default" in templates

    def test_build_manifest_adds_sandbox_id_to_pod_labels(self, template_loader):
        """Test that sandbox-id label is added to pod template."""
        manifest = template_loader.build_manifest(template_name="default", sandbox_id="test-sandbox")

        pod_labels = manifest["spec"]["template"]["metadata"]["labels"]
        assert pod_labels[K8sConstants.LABEL_SANDBOX_ID] == "test-sandbox"

    def test_build_manifest_gpu_template(self):
        """GPU placeholders fill correctly when num_gpus and accelerator_type provided."""
        templates = {
            "gpu": {
                "ports": {"proxy": 8000, "server": 8080, "ssh": 22},
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "main",
                                "image": "{{ image | default('cuda:12', true) }}",
                                "resources": {
                                    "limits": {
                                        "alibabacloud.com/gpu": ("{{ (num_gpus * 100) if num_gpus else '' }}"),
                                        "alibabacloud.com/gpu-mem-ratio": (
                                            "{{ (num_gpus * 100) if num_gpus else '' }}"
                                        ),
                                    }
                                },
                            }
                        ],
                        "affinity": {
                            "nodeAffinity": {
                                "requiredDuringSchedulingIgnoredDuringExecution": {
                                    "nodeSelectorTerms": [
                                        {
                                            "matchExpressions": [
                                                {
                                                    "key": "alibabacloud.com/gpu-card-model-detail",
                                                    "operator": "In",
                                                    "values": ["{{ accelerator_type }}"],
                                                }
                                            ]
                                        }
                                    ]
                                }
                            }
                        },
                    }
                },
            }
        }
        loader = K8sTemplateLoader(templates=templates, default_namespace="rock-test")

        manifest = loader.build_manifest(
            template_name="gpu",
            sandbox_id="test-gpu",
            num_gpus=4,
            accelerator_type="A100",
        )

        container = manifest["spec"]["template"]["spec"]["containers"][0]
        assert container["resources"]["limits"]["alibabacloud.com/gpu"] == "400"
        assert container["resources"]["limits"]["alibabacloud.com/gpu-mem-ratio"] == "400"

        affinity = manifest["spec"]["template"]["spec"]["affinity"]
        terms = affinity["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
        assert terms[0]["matchExpressions"][0]["values"] == ["A100"]

    def test_build_manifest_drops_gpu_when_no_gpu(self):
        """When num_gpus omitted, GPU keys collapse out of resources.limits."""
        templates = {
            "gpu": {
                "ports": {"proxy": 8000, "server": 8080, "ssh": 22},
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "main",
                                "image": "{{ image | default('cuda:12', true) }}",
                                "resources": {
                                    "limits": {
                                        "cpu": "{{ cpus | default('2', true) }}",
                                        "alibabacloud.com/gpu": ("{{ (num_gpus * 100) if num_gpus else '' }}"),
                                        "alibabacloud.com/gpu-mem-ratio": (
                                            "{{ (num_gpus * 100) if num_gpus else '' }}"
                                        ),
                                    }
                                },
                            }
                        ],
                    }
                },
            }
        }
        loader = K8sTemplateLoader(templates=templates, default_namespace="rock-test")

        manifest = loader.build_manifest(template_name="gpu", sandbox_id="test-cpu")

        limits = manifest["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]
        # cpu has a default → present
        assert limits["cpu"] == "2"
        # GPU placeholders rendered to empty → keys dropped
        assert "alibabacloud.com/gpu" not in limits
        assert "alibabacloud.com/gpu-mem-ratio" not in limits


class TestRenderNode:
    """Unit tests for the private _render_node helper."""

    def _make_env(self):
        from jinja2 import Environment, StrictUndefined

        return Environment(undefined=StrictUndefined, autoescape=False)

    def test_string_without_placeholder_returned_as_is(self):
        from rock.sandbox.operator.k8s.template_loader import _render_node

        env = self._make_env()
        assert _render_node("hello world", env, {}) == "hello world"
        assert _render_node("/bin/sh -c 'echo'", env, {"x": "y"}) == "/bin/sh -c 'echo'"

    def test_simple_variable_substitution(self):
        from rock.sandbox.operator.k8s.template_loader import _render_node

        env = self._make_env()
        assert _render_node("{{ a }}", env, {"a": "x"}) == "x"
        assert _render_node("prefix-{{ a }}-suffix", env, {"a": "v"}) == "prefix-v-suffix"

    def test_arithmetic_expression(self):
        from rock.sandbox.operator.k8s.template_loader import _render_node

        env = self._make_env()
        assert _render_node("{{ n * 100 }}", env, {"n": 4}) == "400"

    def test_default_filter_with_boolean_true(self):
        from rock.sandbox.operator.k8s.template_loader import _render_node

        env = self._make_env()
        # boolean=true makes default trigger on falsy values (incl. "" / None)
        assert _render_node("{{ a | default('d', true) }}", env, {"a": ""}) == "d"
        assert _render_node("{{ a | default('d', true) }}", env, {"a": "real"}) == "real"

    def test_dict_renders_each_value(self):
        from rock.sandbox.operator.k8s.template_loader import _render_node

        env = self._make_env()
        node = {"image": "{{ image }}", "name": "main"}
        assert _render_node(node, env, {"image": "ubuntu:22.04"}) == {
            "image": "ubuntu:22.04",
            "name": "main",
        }

    def test_dict_drops_keys_rendered_to_empty(self):
        from rock.sandbox.operator.k8s.template_loader import _render_node

        env = self._make_env()
        node = {"cpu": "{{ cpus }}", "memory": "{{ memory }}", "static": "keep"}
        result = _render_node(node, env, {"cpus": "", "memory": "8Gi"})
        assert result == {"memory": "8Gi", "static": "keep"}

    def test_list_renders_each_element(self):
        from rock.sandbox.operator.k8s.template_loader import _render_node

        env = self._make_env()
        node = ["a", "{{ x }}", "c"]
        assert _render_node(node, env, {"x": "b"}) == ["a", "b", "c"]

    def test_list_drops_elements_rendered_to_empty(self):
        from rock.sandbox.operator.k8s.template_loader import _render_node

        env = self._make_env()
        node = ["{{ a }}", "keep", "{{ b }}"]
        assert _render_node(node, env, {"a": "", "b": "ok"}) == ["keep", "ok"]

    def test_nested_dict_and_list_render_recursively(self):
        from rock.sandbox.operator.k8s.template_loader import _render_node

        env = self._make_env()
        node = {"spec": {"containers": [{"name": "main", "image": "{{ image }}", "args": ["--port", "{{ port }}"]}]}}
        ctx = {"image": "alpine:3.18", "port": "8080"}
        assert _render_node(node, env, ctx) == {
            "spec": {"containers": [{"name": "main", "image": "alpine:3.18", "args": ["--port", "8080"]}]}
        }

    def test_non_string_scalars_pass_through(self):
        from rock.sandbox.operator.k8s.template_loader import _render_node

        env = self._make_env()
        assert _render_node(42, env, {}) == 42
        assert _render_node(3.14, env, {}) == 3.14
        assert _render_node(True, env, {}) is True
        assert _render_node(None, env, {}) is None

    def test_strict_undefined_raises_on_unknown_variable(self):
        import jinja2

        from rock.sandbox.operator.k8s.template_loader import _render_node

        env = self._make_env()
        with pytest.raises(jinja2.UndefinedError):
            _render_node("{{ typo_var }}", env, {"correct_var": "x"})
