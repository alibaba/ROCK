"""Unit tests for BatchSandboxProvider helper methods."""

import pytest

from rock.config import K8sConfig, PoolConfig, TemplateSelectorRule
from rock.deployments.config import DockerDeploymentConfig
from rock.deployments.constants import Port
from rock.sandbox.operator.k8s.constants import K8sConstants
from rock.sandbox.operator.k8s.provider import (
    BatchSandboxProvider,
    DefaultTemplateSelector,
    ResourceMatchingPoolSelector,
)

BASIC_TEMPLATES = {
    "default": {
        "namespace": "rock-test",
        "ports": {"proxy": 8000, "server": 8080, "ssh": 22},
        "template": {
            "metadata": {"labels": {"app": "test"}},
            "spec": {"containers": [{"name": "main", "image": "python:3.11"}]},
        },
    }
}


def make_provider() -> BatchSandboxProvider:
    return BatchSandboxProvider(
        k8s_config=K8sConfig(
            kubeconfig_path=None,
            templates=BASIC_TEMPLATES,
        )
    )


def make_config(
    image: str = "python:3.11",
    cpus: float = 2,
    memory: str = "4Gi",
    extended_params: dict = None,
    image_os: str = "linux",
    num_gpus: float | None = None,
    accelerator_type: str | None = None,
    disk: str | None = None,
    limit_cpus: float | None = None,
) -> DockerDeploymentConfig:
    return DockerDeploymentConfig(
        image=image,
        cpus=cpus,
        memory=memory,
        container_name="test-sandbox",
        extended_params=extended_params or {},
        image_os=image_os,
        num_gpus=num_gpus,
        accelerator_type=accelerator_type,
        disk=disk,
        limit_cpus=limit_cpus,
    )


# ========== ResourceMatchingPoolSelector ==========


class TestResourceMatchingPoolSelector:
    def test_select_pool_by_image_and_resource_match(self):
        """Select pool when image and resources match, choose smallest when multiple satisfy."""
        selector = ResourceMatchingPoolSelector()
        pools = {
            "pool_large": PoolConfig(image="python:3.11", cpus=8, memory="16Gi"),
            "pool_small": PoolConfig(image="python:3.11", cpus=4, memory="8Gi"),
            "pool_tiny": PoolConfig(image="python:3.11", cpus=2, memory="4Gi"),
        }
        config = make_config(image="python:3.11", cpus=2, memory="4Gi")
        assert selector.select_pool(config, pools) == "pool_tiny"

    def test_returns_none_when_image_not_match(self):
        """Return None when image does not match."""
        selector = ResourceMatchingPoolSelector()
        pools = {
            "pool_win": PoolConfig(image="windows:latest", cpus=4, memory="8Gi"),
        }
        config = make_config(image="python:3.11", cpus=2, memory="4Gi")
        assert selector.select_pool(config, pools) is None

    def test_returns_none_when_cpus_not_enough(self):
        """Return None when pool cpus are insufficient."""
        selector = ResourceMatchingPoolSelector()
        pools = {
            "pool_small": PoolConfig(image="python:3.11", cpus=2, memory="8Gi"),
        }
        config = make_config(image="python:3.11", cpus=4, memory="4Gi")
        assert selector.select_pool(config, pools) is None

    def test_returns_none_when_memory_not_enough(self):
        """Return None when pool memory is insufficient."""
        selector = ResourceMatchingPoolSelector()
        pools = {
            "pool_small": PoolConfig(image="python:3.11", cpus=8, memory="4Gi"),
        }
        config = make_config(image="python:3.11", cpus=2, memory="8Gi")
        assert selector.select_pool(config, pools) is None

    def test_returns_none_when_pools_empty(self):
        """Return None when pools is empty."""
        selector = ResourceMatchingPoolSelector()
        config = make_config()
        assert selector.select_pool(config, {}) is None

    def test_pool_exact_resource_match(self):
        """Pool can be selected when resources exactly match requirements."""
        selector = ResourceMatchingPoolSelector()
        pools = {
            "pool_exact": PoolConfig(image="python:3.11", cpus=2, memory="4Gi"),
        }
        config = make_config(image="python:3.11", cpus=2, memory="4Gi")
        assert selector.select_pool(config, pools) == "pool_exact"

    def test_memory_unit_conversion(self):
        """Different memory units can be compared correctly (4096Mi >= 4Gi)."""
        selector = ResourceMatchingPoolSelector()
        pools = {
            "pool_mi": PoolConfig(image="python:3.11", cpus=2, memory="4096Mi"),
        }
        config = make_config(image="python:3.11", cpus=2, memory="4Gi")
        assert selector.select_pool(config, pools) == "pool_mi"

    def test_skip_pool_when_disk_not_enough(self):
        """Return None when pool disk is smaller than required."""
        selector = ResourceMatchingPoolSelector()
        pools = {
            "pool_small_disk": PoolConfig(image="python:3.11", cpus=2, memory="4Gi", disk="20Gi"),
        }
        config = make_config(image="python:3.11", cpus=2, memory="4Gi", disk="50Gi")
        assert selector.select_pool(config, pools) is None

    def test_skip_pool_without_disk_when_disk_required(self):
        """Return None when config requires disk but pool has no disk."""
        selector = ResourceMatchingPoolSelector()
        pools = {
            "pool_no_disk": PoolConfig(image="python:3.11", cpus=2, memory="4Gi"),
        }
        config = make_config(image="python:3.11", cpus=2, memory="4Gi", disk="50Gi")
        assert selector.select_pool(config, pools) is None

    def test_select_pool_with_sufficient_disk(self):
        """Select pool when disk capacity meets requirement."""
        selector = ResourceMatchingPoolSelector()
        pools = {
            "pool_disk": PoolConfig(image="python:3.11", cpus=2, memory="4Gi", disk="100Gi"),
        }
        config = make_config(image="python:3.11", cpus=2, memory="4Gi", disk="50Gi")
        assert selector.select_pool(config, pools) == "pool_disk"

    def test_select_best_fit_pool_with_disk(self):
        """Select pool with smallest cpu+mem+disk when multiple pools match."""
        selector = ResourceMatchingPoolSelector()
        pools = {
            "pool_large": PoolConfig(image="python:3.11", cpus=8, memory="16Gi", disk="200Gi"),
            "pool_exact": PoolConfig(image="python:3.11", cpus=2, memory="4Gi", disk="50Gi"),
        }
        config = make_config(image="python:3.11", cpus=2, memory="4Gi", disk="50Gi")
        assert selector.select_pool(config, pools) == "pool_exact"

    def test_no_disk_filter_when_config_has_no_disk(self):
        """Pools without disk field are still selectable when config has no disk requirement."""
        selector = ResourceMatchingPoolSelector()
        pools = {
            "pool_no_disk": PoolConfig(image="python:3.11", cpus=2, memory="4Gi"),
        }
        config = make_config(image="python:3.11", cpus=2, memory="4Gi")
        assert selector.select_pool(config, pools) == "pool_no_disk"


# ========== _get_pool_name ==========


class TestGetPoolName:
    async def test_returns_pool_from_extended_params(self):
        """Return pool directly from extended_params without using selector."""
        provider = make_provider()
        provider.set_nacos_provider(
            MockNacosProvider(
                {K8sConstants.NACOS_POOLS_KEY: {"pool_nacos": {"image": "python:3.11", "cpus": 4, "memory": "8Gi"}}}
            )
        )
        config = make_config(extended_params={"pool_name": "my_pool"})
        assert await provider._get_pool_name(config) == "my_pool"

    async def test_extended_params_takes_priority_over_selector(self):
        """extended_params takes priority over selector."""
        provider = make_provider()
        provider.set_nacos_provider(
            MockNacosProvider(
                {K8sConstants.NACOS_POOLS_KEY: {"pool_auto": {"image": "python:3.11", "cpus": 4, "memory": "8Gi"}}}
            )
        )
        config = make_config(extended_params={"pool_name": "explicit_pool"})
        assert await provider._get_pool_name(config) == "explicit_pool"

    async def test_uses_selector_when_no_extended_params(self):
        """Use selector to choose pool when no extended_params."""
        provider = make_provider()
        provider.set_nacos_provider(
            MockNacosProvider(
                {
                    K8sConstants.NACOS_POOLS_KEY: {
                        "pool_small": {"image": "python:3.11", "cpus": 2, "memory": "4Gi"},
                        "pool_large": {"image": "python:3.11", "cpus": 8, "memory": "16Gi"},
                    }
                }
            )
        )
        config = make_config(image="python:3.11", cpus=2, memory="4Gi")
        assert await provider._get_pool_name(config) == "pool_small"

    async def test_returns_none_when_no_matching_pool(self):
        """Return None when no matching pool."""
        provider = make_provider()
        # No nacos provider set, so pools is empty
        config = make_config()
        assert await provider._get_pool_name(config) is None


# ========== _get_template_name ==========


class TestGetTemplateName:
    async def test_returns_template_from_extended_params(self):
        """Return template directly from extended_params without using selector."""
        provider = make_provider()
        config = make_config(extended_params={"template_name": "gpu_template"})
        assert await provider._get_template_name(config) == "gpu_template"

    async def test_returns_default_when_no_params_and_no_rules(self):
        """Return 'default' when extended_params and Nacos rules are empty."""
        provider = make_provider()
        config = make_config()
        assert await provider._get_template_name(config) == "default"

    async def test_returns_template_from_nacos_rules(self):
        """Return template selected by Nacos template_rules."""
        provider = make_provider()
        provider.set_nacos_provider(
            MockNacosProvider(
                {
                    K8sConstants.NACOS_TEMPLATE_RULES_KEY: {
                        "gpu-a100-single": {
                            "min_num_gpus": 1,
                            "max_num_gpus": 1,
                            "accelerator_types": ["A100"],
                        }
                    }
                }
            )
        )
        config = make_config(num_gpus=1, accelerator_type="A100")
        assert await provider._get_template_name(config) == "gpu-a100-single"

    async def test_extended_params_takes_priority_over_nacos_rules(self):
        """extended_params template_name beats Nacos template_rules."""
        provider = make_provider()
        provider.set_nacos_provider(
            MockNacosProvider(
                {
                    K8sConstants.NACOS_TEMPLATE_RULES_KEY: {
                        "gpu-a100-single": {
                            "min_num_gpus": 1,
                            "max_num_gpus": 1,
                            "accelerator_types": ["A100"],
                        }
                    }
                }
            )
        )
        config = make_config(
            extended_params={"template_name": "custom"},
            num_gpus=1,
            accelerator_type="A100",
        )
        assert await provider._get_template_name(config) == "custom"

    async def test_returns_default_when_nacos_rules_do_not_match(self):
        """Return 'default' when Nacos rules do not match the config."""
        provider = make_provider()
        provider.set_nacos_provider(
            MockNacosProvider(
                {
                    K8sConstants.NACOS_TEMPLATE_RULES_KEY: {
                        "gpu-a100-single": {
                            "min_num_gpus": 1,
                            "max_num_gpus": 1,
                            "accelerator_types": ["A100"],
                        }
                    }
                }
            )
        )
        config = make_config(num_gpus=1)
        # No accelerator_type, so A100-only rule does not match
        assert await provider._get_template_name(config) == "default"


# ========== DefaultTemplateSelector ==========


class TestDefaultTemplateSelector:
    def test_select_by_gpu_count_range_and_type(self):
        """Select template by num_gpus range and accelerator_type."""
        selector = DefaultTemplateSelector()
        rules = {
            "gpu-a100-single": TemplateSelectorRule(
                min_num_gpus=1,
                max_num_gpus=1,
                accelerator_types=["A100"],
            ),
            "gpu-a100-multi": TemplateSelectorRule(
                min_num_gpus=2,
                accelerator_types=["A100"],
            ),
        }
        config = make_config(num_gpus=1, accelerator_type="A100")
        assert selector.select_template(config, rules) == "gpu-a100-single"

    def test_select_multi_gpu_by_type(self):
        """Select multi-GPU template by accelerator_type."""
        selector = DefaultTemplateSelector()
        rules = {
            "gpu-a100-single": TemplateSelectorRule(
                min_num_gpus=1,
                max_num_gpus=1,
                accelerator_types=["A100"],
            ),
            "gpu-a100-multi": TemplateSelectorRule(
                min_num_gpus=2,
                accelerator_types=["A100"],
            ),
        }
        config = make_config(num_gpus=4, accelerator_type="A100")
        assert selector.select_template(config, rules) == "gpu-a100-multi"

    def test_select_cpu_when_no_gpu(self):
        """Select CPU template when num_gpus is 0/None."""
        selector = DefaultTemplateSelector()
        rules = {
            "default": TemplateSelectorRule(image_os=["linux"], max_num_gpus=0),
            "gpu-a100-single": TemplateSelectorRule(
                min_num_gpus=1,
                max_num_gpus=1,
                accelerator_types=["A100"],
            ),
        }
        config = make_config(num_gpus=None, image_os="linux")
        assert selector.select_template(config, rules) == "default"

    def test_first_matching_rule_wins(self):
        """Rules are evaluated in declaration order; first match wins."""
        selector = DefaultTemplateSelector()
        rules = {
            "template-first": TemplateSelectorRule(image_os=["linux"]),
            "template-second": TemplateSelectorRule(image_os=["linux"]),
        }
        config = make_config(image_os="linux")
        assert selector.select_template(config, rules) == "template-first"

    def test_image_filter(self):
        """image condition filters out non-matching rules."""
        selector = DefaultTemplateSelector()
        rules = {
            "py311-template": TemplateSelectorRule(image="python:3.11"),
            "py312-template": TemplateSelectorRule(image="python:3.12"),
        }
        config = make_config(image="python:3.12")
        assert selector.select_template(config, rules) == "py312-template"

    def test_image_wildcard_match(self):
        """image supports shell-style wildcards."""
        selector = DefaultTemplateSelector()
        rules = {
            "py-template": TemplateSelectorRule(image="python:*"),
            "other-template": TemplateSelectorRule(image="*"),
        }
        config = make_config(image="python:3.12")
        assert selector.select_template(config, rules) == "py-template"

    def test_image_list_match(self):
        """image supports a list of patterns."""
        selector = DefaultTemplateSelector()
        rules = {
            "py-template": TemplateSelectorRule(image=["python:3.11", "python:3.12"]),
        }
        config = make_config(image="python:3.11")
        assert selector.select_template(config, rules) == "py-template"

    def test_image_os_list_match(self):
        """image_os supports a list of values."""
        selector = DefaultTemplateSelector()
        rules = {
            "non-linux-template": TemplateSelectorRule(image_os=["windows", "android"]),
        }
        config = make_config(image_os="android")
        assert selector.select_template(config, rules) == "non-linux-template"

    def test_accelerator_types_list_match(self):
        """accelerator_types supports multiple accelerator types."""
        selector = DefaultTemplateSelector()
        rules = {
            "gpu-template": TemplateSelectorRule(accelerator_types=["A100", "H20"]),
        }
        config = make_config(num_gpus=1, accelerator_type="H20")
        assert selector.select_template(config, rules) == "gpu-template"

    def test_fractional_gpu_in_range(self):
        """Fractional num_gpus falls within min/max range."""
        selector = DefaultTemplateSelector()
        rules = {
            "gpu-shared": TemplateSelectorRule(
                min_num_gpus=0.1,
                max_num_gpus=0.9,
                accelerator_types=["A100"],
            ),
        }
        config = make_config(num_gpus=0.5, accelerator_type="A100")
        assert selector.select_template(config, rules) == "gpu-shared"

    def test_returns_none_when_no_match(self):
        """Return None when no rule matches."""
        selector = DefaultTemplateSelector()
        rules = {
            "gpu-a100": TemplateSelectorRule(accelerator_types=["A100"]),
        }
        config = make_config(num_gpus=1, accelerator_type="H20")
        assert selector.select_template(config, rules) is None

    def test_returns_none_when_rules_empty(self):
        """Return None when rules dict is empty."""
        selector = DefaultTemplateSelector()
        config = make_config()
        assert selector.select_template(config, {}) is None


# ========== _get_template_rules from nacos ==========


class TestGetTemplateRulesFromNacos:
    async def test_get_template_rules_from_nacos(self):
        """Get template rules from Nacos."""
        nacos_config = {
            K8sConstants.NACOS_TEMPLATE_RULES_KEY: {
                "gpu-a100-single": {
                    "min_num_gpus": 1,
                    "max_num_gpus": 1,
                    "accelerator_types": ["A100"],
                }
            }
        }
        provider = make_provider()
        provider.set_nacos_provider(MockNacosProvider(nacos_config))

        rules = await provider._get_template_rules()
        assert len(rules) == 1
        assert "gpu-a100-single" in rules
        assert rules["gpu-a100-single"].min_num_gpus == 1
        assert rules["gpu-a100-single"].max_num_gpus == 1
        assert rules["gpu-a100-single"].accelerator_types == ["A100"]

    async def test_returns_empty_when_no_nacos_provider(self):
        """Return empty dict when no nacos provider."""
        provider = make_provider()
        rules = await provider._get_template_rules()
        assert rules == {}

    async def test_returns_empty_when_nacos_has_no_rules(self):
        """Return empty dict when Nacos has no template_rules config."""
        provider = make_provider()
        provider.set_nacos_provider(MockNacosProvider({"other_key": "value"}))

        rules = await provider._get_template_rules()
        assert rules == {}

    async def test_returns_empty_when_template_rules_is_not_dict(self):
        """Return empty dict when template_rules value is not a dict."""
        provider = make_provider()
        provider.set_nacos_provider(
            MockNacosProvider({K8sConstants.NACOS_TEMPLATE_RULES_KEY: ["not-a-dict"]})
        )

        rules = await provider._get_template_rules()
        assert rules == {}

    async def test_skips_invalid_rule_entries(self):
        """Skip rule entries that are not dicts and keep valid ones."""
        nacos_config = {
            K8sConstants.NACOS_TEMPLATE_RULES_KEY: {
                "valid-rule": {"image_os": ["linux"]},
                "invalid-rule": "not-a-dict",
            }
        }
        provider = make_provider()
        provider.set_nacos_provider(MockNacosProvider(nacos_config))

        rules = await provider._get_template_rules()
        assert list(rules.keys()) == ["valid-rule"]
        assert rules["valid-rule"].image_os == ["linux"]


# ========== _get_pool_ports ==========


class TestGetPoolPorts:
    async def test_returns_ports_from_pool_config(self):
        """Get port configuration from PoolConfig."""
        provider = make_provider()
        provider.set_nacos_provider(
            MockNacosProvider(
                {
                    K8sConstants.NACOS_POOLS_KEY: {
                        "pool_custom": {
                            "image": "python:3.11",
                            "cpus": 4,
                            "memory": "8Gi",
                            "ports": {"proxy": 9000, "server": 9090, "ssh": 2222},
                        }
                    }
                }
            )
        )
        ports = await provider._get_pool_ports("pool_custom")
        assert ports == {"proxy": 9000, "server": 9090, "ssh": 2222}

    async def test_returns_default_ports_when_pool_not_found(self):
        """Return default ports when pool does not exist."""
        provider = make_provider()
        # No nacos provider set, so pools is empty
        ports = await provider._get_pool_ports("unknown_pool")
        assert ports == {"proxy": 8000, "server": 8080, "ssh": 22}

    async def test_returns_default_ports_for_pool_without_ports(self):
        """PoolConfig without ports config gets default values via __post_init__."""
        provider = make_provider()
        provider.set_nacos_provider(
            MockNacosProvider(
                {K8sConstants.NACOS_POOLS_KEY: {"pool_no_ports": {"image": "python:3.11", "cpus": 4, "memory": "8Gi"}}}
            )
        )
        ports = await provider._get_pool_ports("pool_no_ports")
        # PoolConfig.__post_init__ fills in default ports, so provider returns them
        assert ports == {"proxy": 8000, "server": 8080, "ssh": 22}


# ========== _get_pools from nacos ==========


class MockNacosProvider:
    """Mock Nacos provider for testing."""

    def __init__(self, config: dict = None):
        self._config = config

    async def get_config(self):
        return self._config


class MockK8sApiClient:
    """Mock K8s API client for testing."""

    def __init__(self, custom_object: dict = None):
        self._custom_object = custom_object

    async def get_custom_object(self, name: str) -> dict:
        if self._custom_object is None:
            raise Exception(f"Sandbox '{name}' not found")
        return self._custom_object


class TestGetPoolsFromNacos:
    async def test_get_pools_from_nacos(self):
        """Get pools configuration from Nacos."""
        nacos_config = {
            K8sConstants.NACOS_POOLS_KEY: {
                "pool_nacos": {
                    "image": "python:3.11",
                    "cpus": 4,
                    "memory": "8Gi",
                    "ports": {"proxy": 9000, "server": 9090, "ssh": 2222},
                }
            }
        }
        provider = make_provider()
        provider.set_nacos_provider(MockNacosProvider(nacos_config))

        pools = await provider._get_pools()
        assert "pool_nacos" in pools
        assert pools["pool_nacos"].image == "python:3.11"
        assert pools["pool_nacos"].cpus == 4
        assert pools["pool_nacos"].ports == {"proxy": 9000, "server": 9090, "ssh": 2222}

    async def test_returns_empty_when_no_nacos_provider(self):
        """Return empty dict when no nacos provider."""
        provider = make_provider()
        # No nacos provider set

        pools = await provider._get_pools()
        assert pools == {}

    async def test_returns_empty_when_nacos_has_no_pools(self):
        """Return empty dict when Nacos has no pools config."""
        provider = make_provider()
        provider.set_nacos_provider(MockNacosProvider({"other_key": "value"}))

        pools = await provider._get_pools()
        assert pools == {}

    async def test_pool_selection_uses_nacos_pools(self):
        """Pool selection uses pools from Nacos."""
        nacos_config = {
            K8sConstants.NACOS_POOLS_KEY: {"pool_nacos": {"image": "python:3.11", "cpus": 2, "memory": "4Gi"}}
        }
        provider = make_provider()
        provider.set_nacos_provider(MockNacosProvider(nacos_config))

        config = make_config(image="python:3.11", cpus=2, memory="4Gi")
        pool_name = await provider._get_pool_name(config)
        assert pool_name == "pool_nacos"


# ========== _get_sandbox_runtime_info ==========


class TestGetSandboxRuntimeInfo:
    async def test_raises_when_sandbox_being_deleted(self):
        """Raise exception when sandbox is being deleted."""
        provider = make_provider()
        provider._initialized = True

        # Mock K8s API to return a resource with deletionTimestamp
        provider._k8s_api = MockK8sApiClient(
            {"metadata": {"name": "test-sandbox", "deletionTimestamp": "2024-01-01T00:00:00Z", "annotations": {}}}
        )

        with pytest.raises(Exception, match="is being deleted"):
            await provider._get_sandbox_runtime_info("test-sandbox")

    async def test_returns_runtime_info_when_sandbox_active(self):
        """Return runtime info when sandbox is active."""
        provider = make_provider()
        provider._initialized = True

        # Mock K8s API to return a normal resource
        provider._k8s_api = MockK8sApiClient(
            {
                "metadata": {
                    "name": "test-sandbox",
                    "annotations": {
                        K8sConstants.ANNOTATION_ENDPOINTS: '["10.0.0.1"]',
                        K8sConstants.ANNOTATION_PORTS: '{"proxy": 8000, "server": 8080, "ssh": 22}',
                    },
                }
            }
        )

        host_ip, port_mapping, resource_version = await provider._get_sandbox_runtime_info("test-sandbox")
        assert host_ip == "10.0.0.1"
        assert port_mapping[Port.PROXY] == 8000
        assert port_mapping[Port.SERVER] == 8080
        assert port_mapping[Port.SSH] == 22
        assert resource_version == ""

    async def test_returns_resource_version_when_present(self):
        """Return resourceVersion correctly when present in resource."""
        provider = make_provider()
        provider._initialized = True

        # Mock K8s API to return a resource with resourceVersion
        provider._k8s_api = MockK8sApiClient(
            {
                "metadata": {
                    "name": "test-sandbox",
                    "resourceVersion": "12345",
                    "annotations": {
                        K8sConstants.ANNOTATION_ENDPOINTS: '["10.0.0.1"]',
                        K8sConstants.ANNOTATION_PORTS: '{"proxy": 8000, "server": 8080, "ssh": 22}',
                    },
                }
            }
        )

        host_ip, port_mapping, resource_version = await provider._get_sandbox_runtime_info("test-sandbox")
        assert host_ip == "10.0.0.1"
        assert port_mapping[Port.PROXY] == 8000
        assert resource_version == "12345"


# Template that mirrors the real prod K8s template (requests.cpu uses {{ cpus }},
# limits.cpu uses {{ limit_cpus }}) so we can verify the overcommit plumbing.
RESOURCE_TEMPLATES = {
    "default": {
        "namespace": "rock-test",
        "ports": {"proxy": 8000, "server": 8080, "ssh": 22},
        "template": {
            "spec": {
                "containers": [
                    {
                        "name": "main",
                        "image": "{{ image | default('python:3.11', true) }}",
                        "resources": {
                            "requests": {"cpu": "{{ cpus }}", "memory": "{{ memory }}"},
                            "limits": {"cpu": "{{ limit_cpus }}", "memory": "{{ memory }}"},
                        },
                    }
                ],
            },
        },
    }
}


def make_resource_provider() -> BatchSandboxProvider:
    return BatchSandboxProvider(
        k8s_config=K8sConfig(
            kubeconfig_path=None,
            templates=RESOURCE_TEMPLATES,
        )
    )


class TestBuildBatchSandboxManifestCpuOvercommit:
    """`_build_batchsandbox_manifest` must forward `config.limit_cpus` so K8s
    sandboxes can request `cpus` cores while bursting up to `limit_cpus` — the
    K8s analogue of the Ray path's `docker run --cpu-shares ... --cpus ...`."""

    async def test_limit_cpus_propagated_to_manifest(self):
        """limit_cpus > cpus: requests.cpu stays at cpus, limits.cpu = limit_cpus."""
        provider = make_resource_provider()
        config = make_config(cpus=2.0, memory="4Gi", limit_cpus=6.0)

        manifest = await provider._build_batchsandbox_manifest(config)

        container = manifest["spec"]["template"]["spec"]["containers"][0]
        assert container["resources"]["requests"]["cpu"] == "2.0"
        assert container["resources"]["limits"]["cpu"] == "6.0"

    async def test_limit_cpus_defaults_to_cpus_when_none(self):
        """limit_cpus omitted: loader falls back to cpus so requests.cpu == limits.cpu."""
        provider = make_resource_provider()
        config = make_config(cpus=4.0, memory="8Gi")  # limit_cpus left as None

        manifest = await provider._build_batchsandbox_manifest(config)

        container = manifest["spec"]["template"]["spec"]["containers"][0]
        assert container["resources"]["requests"]["cpu"] == "4.0"
        assert container["resources"]["limits"]["cpu"] == "4.0"


IMAGE_AUTH_ANNOTATION = "example.com/encrypted-image-auth"

IMAGE_AUTH_TEMPLATE = {
    "default": {
        "namespace": "rock-test",
        "ports": {"proxy": 8000, "server": 8080, "ssh": 22},
        "template": {
            "metadata": {
                "labels": {"app": "test"},
                "annotations": {
                    IMAGE_AUTH_ANNOTATION: "{{ encrypted_image_auth | default('public', true) }}"
                },
            },
            "spec": {"containers": [{"name": "main", "image": "python:3.11"}]},
        },
    }
}


def _make_provider_with_templates(templates: dict) -> BatchSandboxProvider:
    return BatchSandboxProvider(
        k8s_config=K8sConfig(
            kubeconfig_path=None,
            templates=templates,
        )
    )


class TestBuildBatchSandboxManifestImageAuth:
    """build_manifest must pass encrypted image auth to the template so the
    template can render it into the desired annotation."""

    async def test_renders_encrypted_image_auth_from_template(self, monkeypatch):
        monkeypatch.setenv("ROCK_IMAGE_AUTH_KEY", "0" * 32)
        provider = _make_provider_with_templates(IMAGE_AUTH_TEMPLATE)
        config = make_config()
        config.registry_username = "user"
        config.registry_password = "pass"

        manifest = await provider._build_batchsandbox_manifest(config)

        annotations = manifest["spec"]["template"]["metadata"]["annotations"]
        assert IMAGE_AUTH_ANNOTATION in annotations
        encrypted = annotations[IMAGE_AUTH_ANNOTATION]
        assert len(encrypted) > 0  # base64 encoded ciphertext

    async def test_public_auth_when_key_missing(self, monkeypatch):
        monkeypatch.delenv("ROCK_IMAGE_AUTH_KEY", raising=False)
        provider = _make_provider_with_templates(IMAGE_AUTH_TEMPLATE)
        config = make_config()
        config.registry_username = "user"
        config.registry_password = "pass"

        manifest = await provider._build_batchsandbox_manifest(config)

        annotations = manifest["spec"]["template"]["metadata"]["annotations"]
        assert annotations[IMAGE_AUTH_ANNOTATION] == "public"

    async def test_public_auth_when_credentials_missing(self, monkeypatch):
        monkeypatch.setenv("ROCK_IMAGE_AUTH_KEY", "0" * 32)
        provider = _make_provider_with_templates(IMAGE_AUTH_TEMPLATE)
        config = make_config()

        manifest = await provider._build_batchsandbox_manifest(config)

        annotations = manifest["spec"]["template"]["metadata"]["annotations"]
        assert annotations[IMAGE_AUTH_ANNOTATION] == "public"
