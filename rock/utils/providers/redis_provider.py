from typing import Any

import redis
from redis.commands.json import JSON

from rock.logger import init_logger

logger = init_logger(__name__)


class RedisProvider:
    def __init__(
        self,
        host: str,
        port: int,
        password: str,
    ):
        self.host = host
        self.port = port
        self.password = password
        self.client: redis.Redis | None = None

    async def init_pool(self):
        """
        Called when the application starts, initializes the Redis connection pool and client.
        """
        logger.info("Initializing Redis connection pool...")
        # Create asynchronous connection pool
        pool = redis.asyncio.ConnectionPool(
            host=self.host,
            port=self.port,
            password=self.password,
            decode_responses=True,  # Automatically decode Redis bytes responses to str
            max_connections=500,  # Set maximum connections according to your needs
        )
        # Create Redis client based on connection pool
        self.client = redis.asyncio.Redis.from_pool(pool)
        await self.client.ping()
        logger.info("Redis connection pool initialized and connection successful.")

    async def close_pool(self):
        """
        Called when the application closes, shuts down the Redis client and connection pool.
        """
        if self.client:
            logger.info("Closing Redis connection pool...")
            await self.client.close()
            logger.info("Redis connection pool closed.")
            self.client = None

    def _ensure_client(self) -> redis.Redis:
        """Internal helper method to ensure the client is initialized."""
        if self.client is None:
            raise RuntimeError("RedisManager is not initialized. Please call 'init_pool()' first.")
        return self.client

    # --- RedisJSON functionality encapsulation ---

    @property
    def json_client(self) -> JSON:
        """
        Provides direct access to the native RedisJSON client for advanced operations.
        Usage: await provider.json_client.arrappend(...)
        """
        return self._ensure_client().json()

    async def json_set(self, key: str, path: str, obj: Any):
        """
        Set a JSON object at the specified key and path.

        :param key: Redis key.
        :param path: JSONPath expression (e.g., '$' represents the root).
        :param obj: A Python object that can be serialized to JSON.
        """
        logger.debug(f"JSON SET on key '{key}' at path '{path}'")
        try:
            await self.json_client.set(key, path, obj)
        except Exception as e:
            logger.error(f"Error on JSON SET for key '{key}': {e}", exc_info=True)
            raise

    async def json_set_with_ttl(self, key: str, path: str, obj: Any, ttl_seconds: int):
        """
        Set a JSON document and specify an expiration time (TTL) for it.

        :param key: Redis key.
        :param data: The Python dictionary to store.
        :param ttl_seconds: Time to live in seconds.
        """
        client = self._ensure_client()

        # Step 1: Use .json().set() to set JSON data
        # Use aio-redis pipeline to ensure atomicity of the two commands (sent as a transaction)
        async with client.pipeline() as pipe:
            # Commands will be queued but not executed immediately
            pipe.json().set(key, path, obj)
            # Step 2: Use .expire() to set expiration time for the same key
            pipe.expire(key, ttl_seconds)

            # Execute all commands at once
            results = await pipe.execute()

        logger.info(f"Set JSON for key '{key}' with TTL {ttl_seconds}s. Results: {results}")
        return results

    async def json_get(self, key: str, path: str = "$") -> Any | None:
        """
        Get JSON data from the specified key and path.

        :param key: Redis key.
        :param path: JSONPath expression (defaults to '$' to get the entire document).
        :return: Parsed Python object, or None if it doesn't exist.
        """
        logger.debug(f"JSON GET from key '{key}' at path '{path}'")
        try:
            # RedisJSON's GET can return a list even for a single match
            result = await self.json_client.get(key, path)
            # Unwrap if it's a single-element list from a specific path query
            if isinstance(result, list) and len(result) == 1 and path != "$":
                return result[0]
            return result
        except Exception as e:
            logger.error(f"Error on JSON GET for key '{key}': {e}", exc_info=True)
            return None

    async def get_ttl(self, key: str) -> int | None:
        """
        Get the remaining expiration time (TTL) for the specified key.

        :param key: Redis key.
        :return: Remaining expiration time (seconds).
                 If the key does not exist, returns -2.
                 If the key exists but has no expiration time, returns -1.
                 If an error occurs, returns None.
        """
        logger.debug(f"Getting TTL for key '{key}'")
        try:
            # Use the standard redis client to execute the TTL command
            ttl_in_seconds = await self.client.ttl(key)
            return ttl_in_seconds
        except Exception as e:
            logger.error(f"Error on TTL for key '{key}': {e}", exc_info=True)
            return None

    async def json_delete(self, key: str, path: str = "$") -> int:
        """
        Delete JSON data at the specified key and path.

        :param key: Redis key.
        :param path: JSONPath expression (defaults to '$' to delete the entire document).
        :return: Number of paths successfully deleted.
        """
        logger.debug(f"JSON DELETE on key '{key}' at path '{path}'")
        try:
            return await self.json_client.delete(key, path)
        except Exception as e:
            logger.error(f"Error on JSON DELETE for key '{key}': {e}", exc_info=True)
            raise

    async def pattern_exists(self, pattern: str) -> bool:
        """
        Asynchronously, efficiently, and safely check if there are keys matching the specified pattern.
        """
        try:
            async for _ in self.client.scan_iter(match=pattern, count=1):  # type: ignore
                # As long as we enter the loop body, it means we found at least one key
                return True
        except redis.exceptions.RedisError as e:
            logger.error(f"Connect Redis error: {e}")
            return False

        return False

    async def json_mget(self, keys: list[str], path: str = "$") -> list[Any | None]:
        """
        Get JSON data from the specified keys and path.
        :param keys: Redis key list
        :param path: JSONPath expression (defaults to '$' to get the entire document).
        :return: Parsed Python object list
        """
        logger.debug(f"JSON MGET for keys '{keys}' at path '{path}'")
        try:
            results = await self.json_client.mget(keys, path)
            return results
        except Exception as e:
            logger.error(f"Error on JSON MGET for keys '{keys}': {e}", exc_info=True)
            raise
