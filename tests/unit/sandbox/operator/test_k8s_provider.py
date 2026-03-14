"""Unit tests for BatchSandboxProvider helper methods."""

import pytest

from rock.config import K8sConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.operator.k8s.provider import BatchSandboxProvider


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


def make_provider(pool_map: dict = None, template_map: dict = None, pool_ports: dict = None) -> BatchSandboxProvider:
    return BatchSandboxProvider(
        k8s_config=K8sConfig(
            kubeconfig_path=None,
            templates=BASIC_TEMPLATES,
            pool_map=pool_map or {},
            template_map=template_map or {},
            pool_ports=pool_ports or {},
        )
    )


def make_config(extended_params: dict = None, image_os: str = "linux") -> DockerDeploymentConfig:
    return DockerDeploymentConfig(
        image="python:3.11",
        container_name="test-sandbox",
        extended_params=extended_params or {},
        image_os=image_os,
    )


# ========== _get_pool_name ==========


class TestGetPoolName:
    def test_returns_pool_from_extended_params(self):
        """Priority 1: extended_params 中有 pool_name 时直接返回。"""
        provider = make_provider()
        config = make_config(extended_params={"pool_name": "my_pool"})
        assert provider._get_pool_name(config) == "my_pool"

    def test_extended_params_takes_priority_over_pool_map(self):
        """extended_params 优先级高于 pool_map。"""
        provider = make_provider(pool_map={"linux": "map_pool"})
        config = make_config(extended_params={"pool_name": "ext_pool"}, image_os="linux")
        assert provider._get_pool_name(config) == "ext_pool"

    def test_returns_pool_from_pool_map_by_image_os(self):
        """Priority 2: extended_params 无值时，根据 image_os 从 pool_map 查找。"""
        provider = make_provider(pool_map={"windows": "pool_windows"})
        config = make_config(image_os="windows")
        assert provider._get_pool_name(config) == "pool_windows"

    def test_returns_none_when_image_os_not_in_pool_map(self):
        """image_os 不在 pool_map 中时返回 None。"""
        provider = make_provider(pool_map={"windows": "pool_windows"})
        config = make_config(image_os="linux")
        assert provider._get_pool_name(config) is None

    def test_returns_none_when_no_image_os(self):
        """image_os 为空字符串时跳过 pool_map 查找，返回 None。"""
        provider = make_provider(pool_map={"windows": "pool_windows"})
        config = make_config(image_os="")
        assert provider._get_pool_name(config) is None

    def test_returns_none_when_pool_map_empty(self):
        """pool_map 为空时返回 None。"""
        provider = make_provider(pool_map={})
        config = make_config(image_os="windows")
        assert provider._get_pool_name(config) is None

    def test_returns_none_when_no_params_and_no_pool_map(self):
        """extended_params 和 pool_map 均无值时返回 None。"""
        provider = make_provider()
        config = make_config()
        assert provider._get_pool_name(config) is None


# ========== _get_template_name ==========


class TestGetTemplateName:
    def test_returns_template_from_extended_params(self):
        """Priority 1: extended_params 中有 template_name 时直接返回。"""
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
    def test_returns_ports_from_config(self):
        """从 pool_ports 配置中获取指定 pool 的端口。"""
        provider = make_provider(pool_ports={"pool_windows": {"proxy": 9000, "server": 9090, "ssh": 2222}})
        ports = provider._get_pool_ports("pool_windows")
        assert ports == {"proxy": 9000, "server": 9090, "ssh": 2222}

    def test_returns_defaults_when_pool_not_in_config(self):
        """pool 不在配置中时返回默认端口。"""
        provider = make_provider(pool_ports={})
        ports = provider._get_pool_ports("unknown_pool")
        assert ports == {"proxy": 8000, "server": 8080, "ssh": 22}

    def test_returns_defaults_when_pool_ports_empty(self):
        """pool_ports 配置为空时返回默认端口。"""
        provider = make_provider()
        ports = provider._get_pool_ports("any_pool")
        assert ports == {"proxy": 8000, "server": 8080, "ssh": 22}

    def test_returns_partial_defaults_when_some_ports_missing(self):
        """配置中部分端口缺失时，缺失的使用默认值。"""
        provider = make_provider(pool_ports={"pool_partial": {"proxy": 7000}})
        ports = provider._get_pool_ports("pool_partial")
        assert ports == {"proxy": 7000, "server": 8080, "ssh": 22}
