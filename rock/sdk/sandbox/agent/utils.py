import json
from pathlib import Path

import yaml

from rock.sdk.sandbox.agent.rock_agent import RockAgentConfig


def load_agent_config(config_source: RockAgentConfig | dict | str) -> RockAgentConfig:
    """加载 agent 配置。

    Args:
        config_source: RockAgentConfig、dict 或 yaml/json 文件路径

    Returns:
        RockAgentConfig 实例

    Raises:
        ValueError: 当配置格式无效时
        FileNotFoundError: 当文件路径不存在时
    """
    if isinstance(config_source, RockAgentConfig):
        return config_source

    if isinstance(config_source, dict):
        return RockAgentConfig(**config_source)

    if isinstance(config_source, str):
        path = Path(config_source)
        if not path.exists():
            raise FileNotFoundError(f"Agent config file not found: {config_source}")

        suffix = path.suffix.lower()
        if suffix in (".yaml", ".yml"):
            with open(path, encoding="utf-8") as f:
                config_dict = yaml.safe_load(f)
        elif suffix == ".json":
            with open(path, encoding="utf-8") as f:
                config_dict = json.load(f)
        else:
            raise ValueError(f"Unsupported config file format: {suffix}. Supported formats: .yaml, .yml, .json")

        return RockAgentConfig(**config_dict)

    raise ValueError(
        f"Invalid config source type: {type(config_source).__name__}. "
        "Expected RockAgentConfig, dict, or file path (str)."
    )
