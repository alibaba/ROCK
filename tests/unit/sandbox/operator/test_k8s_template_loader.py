"""Unit tests for K8sTemplateLoader."""

import pytest

from rock.sandbox.operator.k8s.constants import K8sConstants
from rock.sandbox.operator.k8s.template_loader import K8sTemplateLoader


class TestK8sTemplateLoader:
    """Test cases for K8sTemplateLoader."""

    def test_initialization_success(self, basic_templates):
        """Test successful template loader initialization."""
        loader = K8sTemplateLoader(
            templates=basic_templates,
            default_namespace="rock-test"
        )
        
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
            template_name="default",
            sandbox_id="test-sandbox",
            image="python:3.11",
            cpus=2.0,
            memory="4Gi"
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
            template_name="default",
            sandbox_id="test-sandbox",
            cpus=4.0,
            memory="8Gi"
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
        
        # Should not have resources section if not specified
        assert "resources" not in container or not container.get("resources")

    def test_build_manifest_with_custom_image(self, template_loader):
        """Test building manifest with custom image."""
        manifest = template_loader.build_manifest(
            template_name="default",
            sandbox_id="test-sandbox",
            image="ubuntu:22.04"
        )
        
        container = manifest["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == "ubuntu:22.04"

    def test_build_manifest_missing_ports_in_template(self):
        """Test building manifest fails when template lacks ports config."""
        templates = {
            "no-ports": {
                "template": {
                    "spec": {"containers": [{"name": "main"}]}
                }
            }
        }
        
        loader = K8sTemplateLoader(templates=templates, default_namespace="rock-test")
        
        with pytest.raises(ValueError, match="missing required 'ports' configuration"):
            loader.build_manifest(template_name="no-ports", sandbox_id="test")

    def test_build_manifest_with_resource_speedup(self):
        """Test building manifest with resource speedup label."""
        templates = {
            "speedup": {
                "enable_resource_speedup": True,
                "ports": {"proxy": 8000, "server": 8080, "ssh": 22},
                "template": {
                    "spec": {"containers": [{"name": "main"}]}
                }
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
        manifest = template_loader.build_manifest(
            template_name="default",
            sandbox_id="test-sandbox"
        )
        
        pod_labels = manifest["spec"]["template"]["metadata"]["labels"]
        assert pod_labels[K8sConstants.LABEL_SANDBOX_ID] == "test-sandbox"
