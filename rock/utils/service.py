
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.admin.core.redis_key import alive_sandbox_key
from rock.utils.providers.redis_provider import RedisProvider


async def build_sandbox_from_redis(redis_provider: RedisProvider, sandbox_id: str) -> SandboxInfo | None:
    if redis_provider:
        sandbox_status = await redis_provider.json_get(alive_sandbox_key(sandbox_id), "$")
        if sandbox_status and len(sandbox_status) > 0:
            return sandbox_status[0]
    return None
