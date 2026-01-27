from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from rock import env_vars

"""Configuration for LLM Service."""

# Service configuration
SERVICE_HOST = "0.0.0.0"
SERVICE_PORT = 8080

# Log file configuration
LOG_DIR = env_vars.ROCK_MODEL_SERVICE_DATA_DIR
LOG_FILE = LOG_DIR + "/LLMService.log"

# Polling configuration
POLLING_INTERVAL_SECONDS = 0.1  # seconds
REQUEST_TIMEOUT = None  # Infinite timeout as requested

# Request markers
REQUEST_START_MARKER = "LLM_REQUEST_START"
REQUEST_END_MARKER = "LLM_REQUEST_END"
RESPONSE_START_MARKER = "LLM_RESPONSE_START"
RESPONSE_END_MARKER = "LLM_RESPONSE_END"
SESSION_END_MARKER = "SESSION_END"


class ModelServiceConfig(BaseModel):
    proxy_rules: dict[str, str] = Field(
        default_factory=lambda: {
            "gpt-3.5-turbo": "https://api.openai.com/v1",
            "default": "https://api-inference.modelscope.cn/v1"
        }
    )

    # Only these codes will trigger a retry.
    # Codes not in this list (e.g., 400, 401, 403, or certain 5xx/6xx) will fail immediately.
    retryable_status_codes: list[int] = Field(
        default_factory=lambda: [429, 500]
    )

    request_timeout: int = 120

    @classmethod
    def from_file(cls, config_path: str | None = None):
        """
        Factory method to create a config instance.

        Args:
            config_path: Path to the YAML file. If None, returns default config.
        """
        if not config_path:
            return cls()

        config_file = Path(config_path)

        if not config_file.exists():
            raise FileNotFoundError(f"Config file {config_file} not found")

        with open(config_file, encoding="utf-8") as f:
            config_data = yaml.safe_load(f)

        if config_data is None:
            return cls()

        return cls(**config_data)
