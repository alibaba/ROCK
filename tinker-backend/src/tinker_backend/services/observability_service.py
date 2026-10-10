"""Read-only metric aggregation helpers for the Prometheus endpoint."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

STALE_CLAIMED_SECONDS = 1800


async def _rows(session: AsyncSession, sql: str) -> list[dict[str, Any]]:
    result = await session.execute(text(sql))
    return [dict(row._mapping) for row in result]


async def _count(session: AsyncSession, table: str) -> int:
    result = await session.execute(text(f"select count(*) as count from {table}"))
    row = result.one()
    return int(row._mapping["count"] or 0)


def _dialect_name(session: AsyncSession) -> str:
    bind = session.get_bind()
    return bind.dialect.name if bind is not None else ""


async def collect_metrics_snapshot(session: AsyncSession) -> dict[str, Any]:
    sqlite = _dialect_name(session) == "sqlite"
    runtime_ready_latency_sql = (
        """
        select runtime_type,
               avg(strftime('%s', ready_at) - strftime('%s', created_at)) as avg_seconds,
               max(strftime('%s', ready_at) - strftime('%s', created_at)) as max_seconds
        from runtime_instances
        where ready_at is not null
        group by runtime_type
        """
        if sqlite
        else """
        select runtime_type,
               avg(extract(epoch from ready_at - created_at)) as avg_seconds,
               max(extract(epoch from ready_at - created_at)) as max_seconds
        from runtime_instances
        where ready_at is not null
        group by runtime_type
        """
    )
    action_latency_sql = (
        """
        select action_type, status,
               avg(strftime('%s', completed_at) - strftime('%s', created_at)) as avg_seconds,
               max(strftime('%s', completed_at) - strftime('%s', created_at)) as max_seconds
        from runtime_actions
        where completed_at is not null
        group by action_type, status
        """
        if sqlite
        else """
        select action_type, status,
               avg(extract(epoch from completed_at - created_at)) as avg_seconds,
               max(extract(epoch from completed_at - created_at)) as max_seconds
        from runtime_actions
        where completed_at is not null
        group by action_type, status
        """
    )
    stale_claimed_sql = (
        f"""
        select action_type, count(*) as count
        from runtime_actions
        where status = 'claimed'
          and claimed_at is not null
          and claimed_at < datetime('now', '-{STALE_CLAIMED_SECONDS} seconds')
        group by action_type
        """
        if sqlite
        else f"""
        select action_type, count(*) as count
        from runtime_actions
        where status = 'claimed'
          and claimed_at is not null
          and claimed_at < now() - interval '{STALE_CLAIMED_SECONDS} seconds'
        group by action_type
        """
    )
    future_latency_sql = (
        """
        select request_type, status,
               avg(strftime('%s', completed_at) - strftime('%s', created_at)) as avg_seconds,
               max(strftime('%s', completed_at) - strftime('%s', created_at)) as max_seconds
        from operation_futures
        where completed_at is not null
        group by request_type, status
        """
        if sqlite
        else """
        select request_type, status,
               avg(extract(epoch from completed_at - created_at)) as avg_seconds,
               max(extract(epoch from completed_at - created_at)) as max_seconds
        from operation_futures
        where completed_at is not null
        group by request_type, status
        """
    )
    totals = {
        "runtimes": await _count(session, "runtime_instances"),
        "actions": await _count(session, "runtime_actions"),
        "futures": await _count(session, "operation_futures"),
        "envs": await _count(session, "task_environments"),
        "steps": await _count(session, "environment_steps"),
        "training_models": await _count(session, "training_models"),
        "sampling_sessions": await _count(session, "sampling_sessions"),
        "checkpoints": await _count(session, "checkpoints"),
    }
    return {
        "totals": totals,
        "runtime_counts": await _rows(
            session,
            """
            select runtime_type, status, ready, count(*) as count
            from runtime_instances
            group by runtime_type, status, ready
            """,
        ),
        "runtime_ready_latency": await _rows(session, runtime_ready_latency_sql),
        "action_counts": await _rows(
            session,
            """
            select action_type, status, count(*) as count
            from runtime_actions
            group by action_type, status
            """,
        ),
        "action_latency": await _rows(session, action_latency_sql),
        "stale_claimed_actions": await _rows(session, stale_claimed_sql),
        "future_counts": await _rows(
            session,
            """
            select request_type, status, count(*) as count
            from operation_futures
            group by request_type, status
            """,
        ),
        "future_latency": await _rows(session, future_latency_sql),
        "env_counts": await _rows(
            session,
            """
            select dataset, split, status, count(*) as count
            from task_environments
            group by dataset, split, status
            """,
        ),
        "terminal_steps": await _rows(
            session,
            """
            select finish_reason, reward, count(*) as count
            from environment_steps
            where finish_reason is not null or reward is not null
            group by finish_reason, reward
            """,
        ),
        "training_model_counts": await _rows(
            session,
            """
            select base_model, status, count(*) as count
            from training_models
            group by base_model, status
            """,
        ),
        "checkpoint_counts": await _rows(
            session,
            """
            select checkpoint_type, status, count(*) as count
            from checkpoints
            group by checkpoint_type, status
            """,
        ),
    }


def _label_value(value: Any) -> str:
    if value is None:
        value = "none"
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**labels: Any) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{key}="{_label_value(value)}"' for key, value in labels.items()) + "}"


def _emit(lines: list[str], name: str, value: Any, **labels: Any) -> None:
    lines.append(f"{name}{_labels(**labels)} {float(value or 0.0)}")


def _header(lines: list[str], name: str, help_text: str) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} gauge")


def render_prometheus_metrics(snapshot: dict[str, Any]) -> str:
    lines: list[str] = []
    _header(lines, "tinker_backend_info", "Tinker backend scrape information")
    _emit(lines, "tinker_backend_info", 1)
    _header(lines, "tinker_total", "Total stored backend records by resource")
    for resource, count in snapshot["totals"].items():
        _emit(lines, "tinker_total", count, resource=resource)
    _header(lines, "tinker_runtime_instances", "Runtime instances by type status and ready flag")
    for item in snapshot["runtime_counts"]:
        _emit(lines, "tinker_runtime_instances", item["count"], runtime_type=item["runtime_type"], status=item["status"], ready=str(item["ready"]).lower())
    _header(lines, "tinker_runtime_ready_latency_seconds", "Runtime create-to-ready latency")
    for item in snapshot["runtime_ready_latency"]:
        _emit(lines, "tinker_runtime_ready_latency_seconds", item["avg_seconds"], runtime_type=item["runtime_type"], statistic="avg")
        _emit(lines, "tinker_runtime_ready_latency_seconds", item["max_seconds"], runtime_type=item["runtime_type"], statistic="max")
    _header(lines, "tinker_runtime_actions", "Runtime actions by type and status")
    for item in snapshot["action_counts"]:
        _emit(lines, "tinker_runtime_actions", item["count"], action_type=item["action_type"], status=item["status"])
    _header(lines, "tinker_runtime_action_latency_seconds", "Completed action latency")
    for item in snapshot["action_latency"]:
        _emit(lines, "tinker_runtime_action_latency_seconds", item["avg_seconds"], action_type=item["action_type"], status=item["status"], statistic="avg")
        _emit(lines, "tinker_runtime_action_latency_seconds", item["max_seconds"], action_type=item["action_type"], status=item["status"], statistic="max")
    _header(lines, "tinker_runtime_stale_claimed_actions", "Claimed actions older than threshold")
    for item in snapshot["stale_claimed_actions"]:
        _emit(lines, "tinker_runtime_stale_claimed_actions", item["count"], action_type=item["action_type"], threshold_seconds=STALE_CLAIMED_SECONDS)
    _header(lines, "tinker_operation_futures", "Operation futures by type and status")
    for item in snapshot["future_counts"]:
        _emit(lines, "tinker_operation_futures", item["count"], request_type=item["request_type"], status=item["status"])
    _header(lines, "tinker_operation_future_latency_seconds", "Completed future latency")
    for item in snapshot["future_latency"]:
        _emit(lines, "tinker_operation_future_latency_seconds", item["avg_seconds"], request_type=item["request_type"], status=item["status"], statistic="avg")
        _emit(lines, "tinker_operation_future_latency_seconds", item["max_seconds"], request_type=item["request_type"], status=item["status"], statistic="max")
    _header(lines, "tinker_task_environments", "Task environments by dataset and status")
    for item in snapshot["env_counts"]:
        _emit(lines, "tinker_task_environments", item["count"], dataset=item["dataset"], split=item["split"], status=item["status"])
    _header(lines, "tinker_environment_terminal_steps", "Terminal environment steps by finish reason and reward")
    for item in snapshot["terminal_steps"]:
        _emit(lines, "tinker_environment_terminal_steps", item["count"], finish_reason=item["finish_reason"], reward=item["reward"])
    _header(lines, "tinker_training_models", "Training models by base model and status")
    for item in snapshot["training_model_counts"]:
        _emit(lines, "tinker_training_models", item["count"], base_model=item["base_model"], status=item["status"])
    _header(lines, "tinker_checkpoints", "Checkpoints by type and status")
    for item in snapshot["checkpoint_counts"]:
        _emit(lines, "tinker_checkpoints", item["count"], checkpoint_type=item["checkpoint_type"], status=item["status"])
    return "\n".join(lines) + "\n"
