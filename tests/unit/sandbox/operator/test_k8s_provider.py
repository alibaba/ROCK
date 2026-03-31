"""Unit tests for BatchSandboxProvider helper methods."""

from rock.config import K8sConfig, PoolConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.operator.k8s.constants import K8sConstants
from rock.sandbox.operator.k8s.provider import BatchSandboxProvider, ResourceMatchingPoolSelector

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


def make_provider(template_map: dict = None) -> BatchSandboxProvider:
    return BatchSandboxProvider(
        k8s_config=K8sConfig(
            kubeconfig_path=None,
            templates=BASIC_TEMPLATES,
            template_map=template_map or {},
        )
    )


def make_config(
    image: str = "python:3.11",
    cpus: float = 2,
    memory: str = "4Gi",
    extended_params: dict = None,
    image_os: str = "linux",
) -> DockerDeploymentConfig:
    return DockerDeploymentConfig(
        image=image,
        cpus=cpus,
        memory=memory,
        container_name="test-sandbox",
        extended_params=extended_params or {},
        image_os=image_os,
    )


# ========== ResourceMatchingPoolSelector ==========


class TestResourceMatchingPoolSelector:
    def test_select_pool_by_image_and_resource_match(self):
        """image 和资源均匹配时选中，多个满足时选资源最小的。"""
        selector = ResourceMatchingPoolSelector()
        pools = {
            "pool_large": PoolConfig(image="python:3.11", cpus=8, memory="16Gi"),
            "pool_small": PoolConfig(image="python:3.11", cpus=4, memory="8Gi"),
            "pool_tiny": PoolConfig(image="python:3.11", cpus=2, memory="4Gi"),
        }
        config = make_config(image="python:3.11", cpus=2, memory="4Gi")
        assert selector.select_pool(config, pools) == "pool_tiny"

    def test_returns_none_when_image_not_match(self):
        """image 不匹配时返回 None。"""
        selector = ResourceMatchingPoolSelector()
        pools = {
            "pool_win": PoolConfig(image="windows:latest", cpus=4, memory="8Gi"),
        }
        config = make_config(image="python:3.11", cpus=2, memory="4Gi")
        assert selector.select_pool(config, pools) is None

    def test_returns_none_when_cpus_not_enough(self):
        """pool cpus 不足时返回 None。"""
        selector = ResourceMatchingPoolSelector()
        pools = {
            "pool_small": PoolConfig(image="python:3.11", cpus=2, memory="8Gi"),
        }
        config = make_config(image="python:3.11", cpus=4, memory="4Gi")
        assert selector.select_pool(config, pools) is None

    def test_returns_none_when_memory_not_enough(self):
        """pool memory 不足时返回 None。"""
        selector = ResourceMatchingPoolSelector()
        pools = {
            "pool_small": PoolConfig(image="python:3.11", cpus=8, memory="4Gi"),
        }
        config = make_config(image="python:3.11", cpus=2, memory="8Gi")
        assert selector.select_pool(config, pools) is None

    def test_returns_none_when_pools_empty(self):
        """pools 为空时返回 None。"""
        selector = ResourceMatchingPoolSelector()
        config = make_config()
        assert selector.select_pool(config, {}) is None

    def test_pool_exact_resource_match(self):
        """pool 资源与需求完全相等时可被选中。"""
        selector = ResourceMatchingPoolSelector()
        pools = {
            "pool_exact": PoolConfig(image="python:3.11", cpus=2, memory="4Gi"),
        }
        config = make_config(image="python:3.11", cpus=2, memory="4Gi")
        assert selector.select_pool(config, pools) == "pool_exact"

    def test_memory_unit_conversion(self):
        """不同内存单位可正确比较（4096Mi >= 4Gi）。"""
        selector = ResourceMatchingPoolSelector()
        pools = {
            "pool_mi": PoolConfig(image="python:3.11", cpus=2, memory="4096Mi"),
        }
        config = make_config(image="python:3.11", cpus=2, memory="4Gi")
        assert selector.select_pool(config, pools) == "pool_mi"


# ========== _get_pool_name ==========


class TestGetPoolName:
    async def test_returns_pool_from_extended_params(self):
        """extended_params 中有 pool_name 时直接返回，不走 selector。"""
        provider = make_provider()
        provider.set_nacos_provider(
            MockNacosProvider(
                {K8sConstants.NACOS_POOLS_KEY: {"pool_nacos": {"image": "python:3.11", "cpus": 4, "memory": "8Gi"}}}
            )
        )
        config = make_config(extended_params={"pool_name": "my_pool"})
        assert await provider._get_pool_name(config) == "my_pool"

    async def test_extended_params_takes_priority_over_selector(self):
        """extended_params 优先级高于 selector。"""
        provider = make_provider()
        provider.set_nacos_provider(
            MockNacosProvider(
                {K8sConstants.NACOS_POOLS_KEY: {"pool_auto": {"image": "python:3.11", "cpus": 4, "memory": "8Gi"}}}
            )
        )
        config = make_config(extended_params={"pool_name": "explicit_pool"})
        assert await provider._get_pool_name(config) == "explicit_pool"

    async def test_uses_selector_when_no_extended_params(self):
        """无 extended_params 时使用 selector 选择 pool。"""
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
        """无匹配 pool 时返回 None。"""
        provider = make_provider()
        # No nacos provider set, so pools is empty
        config = make_config()
        assert await provider._get_pool_name(config) is None


# ========== _get_template_name ==========


class TestGetTemplateName:
    def test_returns_template_from_extended_params(self):
        """extended_params 中有 template_name 时直接返回，不走 template_map。"""
        provider = make_provider()
        config = make_config(extended_params={"template_name": "gpu_template"})
        assert provider._get_template_name(config) == "gpu_template"

    def test_extended_params_takes_priority_over_template_map(self):
        """extended_params 优先级高于 template_map。"""
        provider = make_provider(template_map={"linux": "map_template"})
        config = make_config(extended_params={"template_name": "ext_template"}, image_os="linux")
        assert provider._get_template_name(config) == "ext_template"

    def test_returns_template_from_template_map_by_image_os(self):
        """Priority 2: extended_params 无值时，根据 image_os 从 template_map 查找。"""
        provider = make_provider(template_map={"windows": "windows_template"})
        config = make_config(image_os="windows")
        assert provider._get_template_name(config) == "windows_template"

    def test_returns_default_when_image_os_not_in_template_map(self):
        """image_os 不在 template_map 中时返回 'default'。"""
        provider = make_provider(template_map={"windows": "windows_template"})
        config = make_config(image_os="linux")
        assert provider._get_template_name(config) == "default"

    def test_returns_default_when_no_image_os(self):
        """image_os 为空字符串时跳过 template_map 查找，返回 'default'。"""
        provider = make_provider(template_map={"windows": "windows_template"})
        config = make_config(image_os="")
        assert provider._get_template_name(config) == "default"

    def test_returns_default_when_template_map_empty(self):
        """template_map 为空时返回 'default'。"""
        provider = make_provider(template_map={})
        config = make_config(image_os="windows")
        assert provider._get_template_name(config) == "default"

    def test_returns_default_when_no_params_and_no_template_map(self):
        """extended_params 和 template_map 均无值时返回 'default'。"""
        provider = make_provider()
        config = make_config()
        assert provider._get_template_name(config) == "default"


# ========== _get_pool_ports ==========


class TestGetPoolPorts:
    async def test_returns_ports_from_pool_config(self):
        """从 PoolConfig 中获取端口配置。"""
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
        """pool 不存在时返回默认端口。"""
        provider = make_provider()
        # No nacos provider set, so pools is empty
        ports = await provider._get_pool_ports("unknown_pool")
        assert ports == {"proxy": 8000, "server": 8080, "ssh": 22}

    async def test_returns_default_ports_for_pool_without_ports(self):
        """PoolConfig 未配置 ports 时由 __post_init__ 自动补全默认值。"""
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


class TestGetPoolsFromNacos:
    async def test_get_pools_from_nacos(self):
        """从 Nacos 获取 pools 配置。"""
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
        """无 nacos provider 时返回空字典。"""
        provider = make_provider()
        # No nacos provider set

        pools = await provider._get_pools()
        assert pools == {}

    async def test_returns_empty_when_nacos_has_no_pools(self):
        """Nacos 无 pools 配置时返回空字典。"""
        provider = make_provider()
        provider.set_nacos_provider(MockNacosProvider({"other_key": "value"}))

        pools = await provider._get_pools()
        assert pools == {}

    async def test_pool_selection_uses_nacos_pools(self):
        """Pool 选择使用 Nacos 中的 pools。"""
        nacos_config = {
            K8sConstants.NACOS_POOLS_KEY: {"pool_nacos": {"image": "python:3.11", "cpus": 2, "memory": "4Gi"}}
        }
        provider = make_provider()
        provider.set_nacos_provider(MockNacosProvider(nacos_config))

        config = make_config(image="python:3.11", cpus=2, memory="4Gi")
        pool_name = await provider._get_pool_name(config)
        assert pool_name == "pool_nacos"
