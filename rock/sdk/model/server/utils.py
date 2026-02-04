import json
import os
from collections.abc import Callable
from functools import wraps

from fastapi.responses import JSONResponse

from rock.sdk.model.server.config import TRAJ_FILE


def _write_traj(data: dict):
    """Write traj data to file in JSONL format."""
    from rock import env_vars

    append = env_vars.ROCK_MODEL_SERVICE_TRAJ_APPEND_MODE
    if TRAJ_FILE:
        os.makedirs(os.path.dirname(TRAJ_FILE), exist_ok=True)
        mode = "a" if append else "w"
        with open(TRAJ_FILE, mode, encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")


def record_traj(func: Callable):
    """Decorator to record chat completions input/output as traj."""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        # Extract body from args/kwargs for logging
        body = args[0] if args else kwargs.get("body")
        result = await func(*args, **kwargs)
        # JSONResponse.body is bytes, dict is returned directly
        if isinstance(result, JSONResponse):
            response_data = json.loads(result.body)
        else:
            response_data = result
        _write_traj(
            {
                "request": body,
                "response": response_data,
            }
        )
        return result

    return wrapper
