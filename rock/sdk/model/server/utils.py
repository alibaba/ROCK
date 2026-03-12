import json
import os
import time
from collections.abc import Callable
from functools import wraps

from fastapi.responses import JSONResponse

from rock.admin.metrics.monitor import MetricsMonitor
from rock.sdk.model.server.config import TRAJ_FILE

MODEL_SERVICE_REQUEST_RT = "model_service.request.rt"
MODEL_SERVICE_REQUEST_COUNT = "model_service.request.count"

_metrics_monitor: MetricsMonitor | None = None


def _get_or_create_metrics_monitor() -> MetricsMonitor:
    global _metrics_monitor
    if _metrics_monitor is None:
        endpoint = os.getenv("ROCK_METRICS_ENDPOINT", "localhost:4318/v1/metrics")
        _metrics_monitor = MetricsMonitor.create(metrics_endpoint=endpoint)
        _metrics_monitor._register_gauge(MODEL_SERVICE_REQUEST_RT, "total execution time for request", "ms")
        _metrics_monitor._register_counter(MODEL_SERVICE_REQUEST_COUNT, "total request count", "count")
    return _metrics_monitor


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

        start_time = time.perf_counter()
        result = await func(*args, **kwargs)
        rt = time.perf_counter() - start_time

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
        monitor = _get_or_create_metrics_monitor()
        attr = {"type": "chat_completions"}
        attr["sandbox_id"] = os.getenv("ROCK_SANDBOX_ID", "unknown")
        monitor.record_gauge_by_name(MODEL_SERVICE_REQUEST_RT, rt, attributes=attr)
        monitor.record_counter_by_name(MODEL_SERVICE_REQUEST_COUNT, 1, attributes=attr)
        return result

    return wrapper
