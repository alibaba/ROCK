from typing import Any

import nacos
import yaml

from rock.logger import init_logger

logger = init_logger(__name__)


class NacosConfigProvider:
    """
    A class for managing Nacos configurations, including initialization, retrieval, and dynamic updates.
    """

    def __init__(
        self,
        server_addresses: str,
        endpoint: str,
        data_id: str,
        group: str,
        namespace: str = "",
    ):
        self.server_addresses = server_addresses
        self.endpoint = endpoint
        self.namespace = namespace
        self.data_id = data_id
        self.group = group
        self.config_cache: Any | None = None

        self.client = nacos.NacosClient(server_addresses=self.server_addresses, endpoint=self.endpoint, namespace=self.namespace)

    async def get_config(self) -> Any:
        """
        Retrieve and parse configuration from Nacos.
        """
        if self.config_cache:
            return self.config_cache

        try:
            config_str = self.client.get_config(self.data_id, self.group)
            if config_str:
                logger.info("Successfully retrieved config from Nacos.")
                self.config_cache = yaml.safe_load(config_str)
            return self.config_cache
        except Exception as e:
            logger.error(f"Error getting config from Nacos: {e}")
            return None

    def _update_callback(self, new_config: dict):
        """
        This callback function is called when the Nacos configuration changes.
        """
        logger.info("Nacos config changed! Reloading configuration...")
        try:
            self.config_cache = yaml.safe_load(new_config["content"])
            logger.info("Configuration reloaded successfully.")
            # In actual applications, you may need to trigger some update logic here
            # For example, reinitialize components that depend on the configuration
        except yaml.YAMLError as e:
            logger.error(f"Failed to parse updated YAML config: {e}")

    def add_listener(self):
        """
        Add a listener for the configuration to implement hot reloading.
        """
        try:
            self.client.add_config_watcher(
                data_id=self.data_id,
                group=self.group,
                cb=self._update_callback,
            )
            logger.info(f"Added config watcher for data_id='{self.data_id}' group='{self.group}'.")
        except Exception as e:
            logger.error(f"Failed to add Nacos config watcher: {e}")
            raise

    async def get_switch_status(self, switch_name: str, not_found_default: bool = False) -> bool:
        config = await self.get_config() or {}
        return bool((config.get("switch") or {}).get(switch_name, not_found_default))
