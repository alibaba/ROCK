import configparser
from dataclasses import dataclass, field
from pathlib import Path

from rock import env_vars
from rock.logger import init_logger

logger = init_logger(__name__)


@dataclass
class DatasetConfig:
    oss_bucket: str | None = None
    oss_endpoint: str | None = None
    oss_access_key_id: str | None = None
    oss_access_key_secret: str | None = None
    oss_region: str | None = None


@dataclass
class CLIConfig:
    """CLI configuration class"""

    base_url: str = env_vars.ROCK_BASE_URL
    extra_headers: dict[str, str] = field(default_factory=dict)
    dataset_config: DatasetConfig = field(default_factory=DatasetConfig)


class ConfigManager:
    """Configuration manager"""

    DEFAULT_CONFIG_PATH = env_vars.ROCK_CLI_DEFAULT_CONFIG_PATH

    def __init__(self, config_path: Path | None = None):
        """
        Initialize configuration manager

        Args:
            config_path: Configuration file path, default is ~/.rock/config.ini
        """
        self.config_path = config_path or self.DEFAULT_CONFIG_PATH
        self.config = CLIConfig()
        self._load_config()

    def _load_config(self):
        """Load configuration file"""
        if not self.config_path.exists():
            logger.warning(f"Config file {self.config_path} does not exist. Using default configuration.")
            return

        try:
            parser = configparser.ConfigParser()
            parser.read(self.config_path, encoding="utf-8")

            # Read configuration from [rock] section
            if "rock" in parser:
                rock_section = parser["rock"]

                if "base_url" in rock_section:
                    self.config.base_url = rock_section["base_url"]

            # Read additional header configuration - Solution 2
            if "rock.extra_headers" in parser:
                headers_section = parser["rock.extra_headers"]
                for key, value in headers_section.items():
                    if value.strip():
                        self.config.extra_headers[key] = value.strip()

            if "dataset" in parser:
                ds = parser["dataset"]
                self.config.dataset_config = DatasetConfig(
                    oss_bucket=ds.get("oss_bucket") or None,
                    oss_endpoint=ds.get("oss_endpoint") or None,
                    oss_access_key_id=ds.get("oss_access_key_id") or None,
                    oss_access_key_secret=ds.get("oss_access_key_secret") or None,
                    oss_region=ds.get("oss_region") or None,
                )

        except Exception as e:
            logger.warning(f"Failed to load config file {self.config_path}: {e}", exc_info=True)
            raise e

    def get_config(self) -> CLIConfig:
        """Get configuration"""
        return self.config
