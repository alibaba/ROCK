from rock.sdk.sandbox.config import SandboxConfig, SandboxGroupConfig


def test_sandbox_config_operator_type_default_is_ray():
    """operator_type should default to 'ray' when not specified."""
    config = SandboxConfig()
    assert config.operator_type == "ray"


def test_sandbox_config_operator_type_set_explicitly():
    """operator_type should be stored when explicitly set."""
    config = SandboxConfig(operator_type="k8s")
    assert config.operator_type == "k8s"


def test_sandbox_config_operator_type_ray():
    """operator_type should accept 'ray' value."""
    config = SandboxConfig(operator_type="ray")
    assert config.operator_type == "ray"


def test_sandbox_config_operator_type_with_other_fields():
    """operator_type should coexist with other config fields."""
    config = SandboxConfig(
        image="ubuntu:22.04",
        memory="16g",
        cpus=4,
        cluster="us-east",
        operator_type="k8s",
    )
    assert config.operator_type == "k8s"
    assert config.image == "ubuntu:22.04"
    assert config.memory == "16g"
    assert config.cpus == 4
    assert config.cluster == "us-east"


def test_sandbox_config_operator_type_serialization():
    """operator_type should appear in model_dump output."""
    config = SandboxConfig(operator_type="ray")
    dumped = config.model_dump()
    assert "operator_type" in dumped
    assert dumped["operator_type"] == "ray"


def test_sandbox_config_operator_type_default_serialization():
    """Default operator_type='ray' should appear in model_dump output."""
    config = SandboxConfig()
    dumped = config.model_dump()
    assert "operator_type" in dumped
    assert dumped["operator_type"] == "ray"


def test_sandbox_group_config_has_operator_type():
    """SandboxGroupConfig should support operator_type field."""
    config = SandboxGroupConfig(operator_type="ray", size=2)
    assert config.operator_type == "ray"


def test_sandbox_group_config_operator_type_default_ray():
    """SandboxGroupConfig.operator_type should default to 'ray'."""
    config = SandboxGroupConfig(size=2)
    assert config.operator_type == "ray"
