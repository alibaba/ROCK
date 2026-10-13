from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tinker_backend.protocol.enums import ActionState, FutureState, RuntimeState
from tinker_backend.storage.models import (
    OperationFutureRecord,
    RuntimeActionRecord,
    RuntimeInstanceRecord,
    utcnow,
)
from tinker_backend.protocol.schemas import CloseRuntimeOutput, CreateRuntimeOutput, CreateRuntimeRequest, RuntimeHeartbeatRequest
from tinker_backend.config import Settings


class RuntimeLaunchError(RuntimeError):
    pass


CLOSE_ACTION_TYPES = ("close_runtime", "close_rock_adapter")


def make_runtime_id() -> str:
    return "rt_" + uuid4().hex[:12]


def _config_suffix(config_type: str) -> str:
    return "yaml" if config_type == "yaml" else config_type


def runtime_config_path(settings: Settings, runtime_id: str, config_type: str) -> Path:
    return Path(settings.runtime_state_dir) / runtime_id / f"config.{_config_suffix(config_type)}"


def runtime_original_config_path(settings: Settings, runtime_id: str, config_type: str) -> Path:
    return Path(settings.runtime_state_dir) / runtime_id / f"config.original.{_config_suffix(config_type)}"


def resolve_runtime_log_path(runtime_id: str, *, state_dir: str | Path | None = None) -> Path:
    log_dir = os.environ.get("TINKER_LOG_DIR")
    if log_dir:
        path = Path(log_dir).expanduser() / f"runtime_{runtime_id}.log"
    elif state_dir is not None:
        path = Path(state_dir) / runtime_id / "runtime.log"
    else:
        path = Path("/var/lib/tinker-backend/runtimes") / runtime_id / "runtime.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def roll_config_path(settings: Settings, runtime_id: str) -> Path:
    return Path(settings.runtime_state_dir) / runtime_id / "roll" / "config.yaml"


def _dump_yaml_mapping(value: dict[str, Any]) -> str:
    return yaml.safe_dump(value, sort_keys=False, allow_unicode=True)


def materialize_runtime_config(settings: Settings, runtime_id: str, config_type: str, config_content: str) -> str:
    """Write backend-owned runtime config files and return the runtime wrapper path.

    The SDK sends one combined YAML. The backend keeps that original YAML for
    audit/debug, but the runtime process receives a generated wrapper YAML. If
    the combined config contains a top-level ``roll:`` mapping, that
    section is materialized as a standalone ROLL config under the
    runtime state directory and the wrapper's ``tinker_runtime`` section is
    updated with the generated Hydra config location.
    """

    state_dir = Path(settings.runtime_state_dir) / runtime_id
    state_dir.mkdir(parents=True, exist_ok=True)

    original_path = runtime_original_config_path(settings, runtime_id, config_type)
    original_path.write_text(config_content, encoding="utf-8")

    runtime_config = parse_runtime_config(config_content)
    generated_config = dict(runtime_config)
    metadata = dict(generated_config.get("tinker_backend") or {})
    metadata["original_config_path"] = str(original_path)

    if "roll" in runtime_config:
        roll_config = runtime_config.get("roll")
        if not isinstance(roll_config, dict):
            raise RuntimeLaunchError("runtime config roll section must be a YAML mapping")

        sa_path = roll_config_path(settings, runtime_id)
        sa_path.parent.mkdir(parents=True, exist_ok=True)
        sa_path.write_text(_dump_yaml_mapping(roll_config), encoding="utf-8")

        tinker_runtime = dict(generated_config.get("tinker_runtime") or {})
        backend_config = dict(tinker_runtime.get("backend_config") or {})
        backend_config["config_path"] = str(sa_path.parent)
        backend_config["config_name"] = sa_path.stem
        backend_config["generated_config_path"] = str(sa_path)
        tinker_runtime["backend_config"] = backend_config
        generated_config["tinker_runtime"] = tinker_runtime
        metadata["roll_config_path"] = str(sa_path)

    generated_config["tinker_backend"] = metadata

    path = runtime_config_path(settings, runtime_id, config_type)
    path.write_text(_dump_yaml_mapping(generated_config), encoding="utf-8")
    return str(path)


def parse_runtime_config(config_content: str) -> dict[str, Any]:
    loaded = yaml.safe_load(config_content) if config_content.strip() else {}
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise RuntimeLaunchError("runtime config content must be a YAML mapping")
    return loaded


async def create_runtime_future(
    session: AsyncSession,
    settings: Settings,
    request: CreateRuntimeRequest,
) -> tuple[RuntimeInstanceRecord, OperationFutureRecord]:
    runtime_id = make_runtime_id()
    config_path = materialize_runtime_config(
        settings,
        runtime_id=runtime_id,
        config_type=request.config_type,
        config_content=request.config_content,
    )

    runtime = RuntimeInstanceRecord(
        runtime_id=runtime_id,
        runtime_type=request.runtime_type,
        config_type=request.config_type,
        config_content=request.config_content,
        config_path=config_path,
        status=RuntimeState.CREATING.value,
        ready=False,
        session_id=request.session_id,
        updated_at=utcnow(),
    )
    session.add(runtime)
    await session.flush()

    future = OperationFutureRecord(
        request_type="create_runtime",
        runtime_id=runtime.runtime_id,
        request_data=request.model_dump(mode="json"),
        status=FutureState.PENDING.value,
    )
    session.add(future)
    await session.flush()
    return runtime, future


def _runtime_output(runtime: RuntimeInstanceRecord) -> dict[str, Any]:
    return CreateRuntimeOutput(
        runtime_id=runtime.runtime_id,
        runtime_type=runtime.runtime_type,
        status=runtime.status,
        ready=runtime.ready,
        config_type=runtime.config_type,
        config_path=runtime.config_path,
        adapter_base_url=runtime.adapter_base_url,
        error_message=runtime.error_message,
    ).model_dump(mode="json")


async def _complete_create_runtime_future(session: AsyncSession, runtime: RuntimeInstanceRecord) -> None:
    result = await session.execute(
        select(OperationFutureRecord)
        .where(OperationFutureRecord.runtime_id == runtime.runtime_id)
        .where(OperationFutureRecord.request_type == "create_runtime")
        .where(OperationFutureRecord.status == FutureState.PENDING.value)
        .order_by(OperationFutureRecord.request_id)
    )
    for future in result.scalars().all():
        future.status = FutureState.COMPLETED.value
        future.result_data = _runtime_output(runtime)
        future.completed_at = utcnow()


async def _fail_create_runtime_future(session: AsyncSession, runtime: RuntimeInstanceRecord, error_message: str) -> None:
    runtime.status = RuntimeState.FAILED.value
    runtime.ready = False
    runtime.error_message = error_message
    runtime.updated_at = utcnow()
    result = await session.execute(
        select(OperationFutureRecord)
        .where(OperationFutureRecord.runtime_id == runtime.runtime_id)
        .where(OperationFutureRecord.request_type == "create_runtime")
        .where(OperationFutureRecord.status == FutureState.PENDING.value)
        .order_by(OperationFutureRecord.request_id)
    )
    for future in result.scalars().all():
        future.status = FutureState.FAILED.value
        future.error_message = error_message
        future.completed_at = utcnow()


def _runtime_section(config: dict[str, Any]) -> dict[str, Any]:
    runtime = config.get("runtime")
    return runtime if isinstance(runtime, dict) else {}


def _rock_section(config: dict[str, Any]) -> dict[str, Any]:
    rock = config.get("rock")
    return rock if isinstance(rock, dict) else {}


def _rock_enabled(config: dict[str, Any]) -> bool:
    rock_cfg = _rock_section(config)
    return bool(rock_cfg) and bool(rock_cfg.get("enabled", True))


def _backend_base_url(settings: Settings, config: dict[str, Any]) -> str:
    backend = config.get("backend") if isinstance(config.get("backend"), dict) else {}
    configured = backend.get("base_url") or settings.public_base_url
    return str(configured or f"http://127.0.0.1:{settings.port}").rstrip("/")


def _command_from_script(script: str) -> list[str]:
    if script.endswith(".py"):
        return [sys.executable, script]
    return [script]



def _clean_subprocess_env(env: dict[str, str]) -> dict[str, str]:
    cleaned = dict(env)
    for key in list(cleaned):
        if key == "UV" or key.startswith("UV_"):
            cleaned.pop(key, None)
    for key in [
        "VIRTUAL_ENV",
        "RAY_JOB_CONFIG_JSON_ENV_VAR",
        "RAY_RUNTIME_ENV_HOOK",
    ]:
        cleaned.pop(key, None)
    # The backend is often launched with `uv run`; Ray 2.48 otherwise
    # propagates that to runtime workers as `py_executable: uv run`, which is
    # wrong for ROLL repos whose pyproject.toml is tool-only.
    cleaned["RAY_ENABLE_UV_RUN_RUNTIME_ENV"] = "0"
    return cleaned


def _runtime_env(runtime: RuntimeInstanceRecord, settings: Settings, config: dict[str, Any]) -> dict[str, str]:
    runtime_cfg = _runtime_section(config)
    rock_cfg = _rock_section(config)
    backend_url = _backend_base_url(settings, config)

    env = _clean_subprocess_env(os.environ.copy())
    env.update(settings.oss_env())
    env.update({str(k): str(v) for k, v in (runtime_cfg.get("env") or {}).items()})
    env.update({str(k): str(v) for k, v in (rock_cfg.get("env") or {}).items()})
    env.update(
        {
            "TINKER_RUNTIME_ID": runtime.runtime_id,
            "TINKER_BACKEND_BASE_URL": backend_url,
            "TINKER_CONFIG_PATH": runtime.config_path or "",
        }
    )
    return env


def build_runtime_command(runtime: RuntimeInstanceRecord, settings: Settings, config: dict[str, Any]) -> tuple[list[str] | str, str, dict[str, str], bool]:
    runtime_cfg = _runtime_section(config)
    rock_cfg = _rock_section(config)
    backend_url = _backend_base_url(settings, config)

    workdir = str(runtime_cfg.get("workdir") or Path.cwd())
    env = _runtime_env(runtime, settings, config)

    command: list[str] | str
    shell = False
    launch_command = runtime_cfg.get("launch_command") or runtime_cfg.get("command")
    if launch_command:
        command = launch_command if isinstance(launch_command, str) else [str(x) for x in launch_command]
        shell = isinstance(command, str)
    elif runtime_cfg.get("launch_module"):
        command = [sys.executable, "-m", str(runtime_cfg["launch_module"])]
    elif runtime_cfg.get("launch_script") or runtime_cfg.get("script"):
        script = str(runtime_cfg.get("launch_script") or runtime_cfg.get("script"))
        command = _command_from_script(script)
        if not runtime_cfg.get("workdir"):
            workdir = str(Path(script).resolve().parent)
    elif runtime.runtime_type == "dummy":
        command = [sys.executable, "/root/tinker-dummy/run_dummy.py"]
        workdir = "/root/tinker-dummy"
    elif runtime_cfg.get("adapter") == "rock" or rock_cfg:
        command = [sys.executable, "-m", "tinker_backend.rock_adapter"]
        workdir = str(runtime_cfg.get("workdir") or Path.cwd())
    else:
        raise RuntimeLaunchError(
            f"runtime_type={runtime.runtime_type!r} requires runtime.launch_script, "
            "runtime.launch_module, runtime.launch_command, or rock config"
        )

    if isinstance(command, list):
        module_name = ""
        if len(command) >= 3 and command[0] == sys.executable and command[1] == "-m":
            module_name = command[2]
        if module_name == "tinker_backend.rock_adapter":
            command.extend(["--backend-url", backend_url, "--runtime-id", runtime.runtime_id])
            job_config_path = rock_cfg.get("job_config_path") or runtime_cfg.get("job_config_path")
            if job_config_path:
                command.extend(["--job-config", str(job_config_path)])
        else:
            command.extend([
                "--runtime-id",
                runtime.runtime_id,
                "--backend-base-url",
                backend_url,
                "--config-path",
                runtime.config_path or "",
            ])
        extra_args = runtime_cfg.get("args") or []
        if isinstance(extra_args, str):
            command.extend(shlex.split(extra_args))
        else:
            command.extend([str(x) for x in extra_args])

    return command, workdir, env, shell


def build_rock_adapter_command(runtime: RuntimeInstanceRecord, settings: Settings, config: dict[str, Any]) -> tuple[list[str], str, dict[str, str]]:
    runtime_cfg = _runtime_section(config)
    rock_cfg = _rock_section(config)
    backend_url = _backend_base_url(settings, config)
    workdir = str(rock_cfg.get("workdir") or Path.cwd())
    env = _runtime_env(runtime, settings, config)
    command = [
        sys.executable,
        "-m",
        "tinker_backend.rock_adapter",
        "--backend-url",
        backend_url,
        "--runtime-id",
        runtime.runtime_id,
        "--config-path",
        runtime.config_path or "",
    ]
    job_config_path = rock_cfg.get("job_config_path") or runtime_cfg.get("job_config_path")
    if job_config_path:
        command.extend(["--job-config", str(job_config_path)])
    if rock_cfg.get("model_service_port"):
        command.extend(["--model-service-port", str(rock_cfg["model_service_port"])])
    return command, workdir, env


def _command_is_rock_adapter(command: list[str] | str) -> bool:
    if isinstance(command, str):
        return "tinker_backend.rock_adapter" in command
    return len(command) >= 3 and command[1] == "-m" and command[2] == "tinker_backend.rock_adapter"


async def launch_runtime_process(session: AsyncSession, settings: Settings, runtime: RuntimeInstanceRecord) -> None:
    try:
        config = parse_runtime_config(runtime.config_content)
        command, workdir, env, shell = build_runtime_command(runtime, settings, config)
        state_dir = Path(settings.runtime_state_dir) / runtime.runtime_id
        state_dir.mkdir(parents=True, exist_ok=True)
        log_path = resolve_runtime_log_path(runtime.runtime_id, state_dir=settings.runtime_state_dir)
        with log_path.open("ab") as log_file:
            rendered_command = command if isinstance(command, str) else " ".join(shlex.quote(x) for x in command)
            log_file.write(f"Launching runtime {runtime.runtime_id}: {rendered_command}\n".encode("utf-8"))
            log_file.flush()
            process = subprocess.Popen(
                command,
                cwd=workdir,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                shell=shell,
            )
            if _rock_enabled(config) and not _command_is_rock_adapter(command):
                adapter_command, adapter_workdir, adapter_env = build_rock_adapter_command(runtime, settings, config)
                rendered_adapter_command = " ".join(shlex.quote(x) for x in adapter_command)
                log_file.write(
                    f"Launching ROCK adapter {runtime.runtime_id}: {rendered_adapter_command}\n".encode("utf-8")
                )
                log_file.flush()
                subprocess.Popen(
                    adapter_command,
                    cwd=adapter_workdir,
                    env=adapter_env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
    except Exception as exc:
        await _fail_create_runtime_future(session, runtime, str(exc))
        await session.flush()
        return

    runtime.status = RuntimeState.STARTING.value
    runtime.ready = False
    runtime.process_pid = process.pid
    runtime.launcher_owner = "tinker-backend"
    runtime.updated_at = utcnow()
    await session.flush()




def _close_runtime_output(
    runtime: RuntimeInstanceRecord,
    *,
    graceful: bool,
    killed_pids: list[int] | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    return CloseRuntimeOutput(
        runtime_id=runtime.runtime_id,
        status=runtime.status,
        graceful=graceful,
        killed_pids=killed_pids or [],
        message=message,
    ).model_dump(mode="json")


async def create_close_runtime_future(
    session: AsyncSession,
    runtime: RuntimeInstanceRecord,
    *,
    force_after_seconds: float | None = None,
) -> tuple[RuntimeActionRecord, OperationFutureRecord]:
    payload = {
        "runtime_id": runtime.runtime_id,
        "force_after_seconds": force_after_seconds,
    }
    runtime.status = RuntimeState.STOPPING.value
    runtime.ready = False
    runtime.updated_at = utcnow()

    future = OperationFutureRecord(
        request_type="close_runtime",
        runtime_id=runtime.runtime_id,
        request_data=payload,
        status=FutureState.PENDING.value,
    )
    session.add(future)
    await session.flush()


    action = RuntimeActionRecord(
        runtime_id=runtime.runtime_id,
        action_type="close_runtime",
        env_id=None,
        future_id=future.request_id,
        payload=payload,
    )
    session.add(action)
    await session.flush()

    try:
        config = parse_runtime_config(runtime.config_content)
    except Exception:
        config = {}
    if _rock_enabled(config):
        session.add(
            RuntimeActionRecord(
                runtime_id=runtime.runtime_id,
                action_type="close_rock_adapter",
                env_id=None,
                future_id=future.request_id,
                payload=payload,
            )
        )
        await session.flush()
    return action, future


async def create_completed_close_runtime_future(
    session: AsyncSession,
    runtime: RuntimeInstanceRecord,
    *,
    message: str,
) -> OperationFutureRecord:
    runtime.status = RuntimeState.STOPPED.value
    runtime.ready = False
    runtime.process_pid = None
    runtime.updated_at = utcnow()


    future = OperationFutureRecord(
        request_type="close_runtime",
        runtime_id=runtime.runtime_id,
        request_data={"runtime_id": runtime.runtime_id},
        result_data=_close_runtime_output(runtime, graceful=True, message=message),
        status=FutureState.COMPLETED.value,
        completed_at=utcnow(),
    )
    session.add(future)
    await session.flush()
    return future


async def complete_close_runtime_future(
    session: AsyncSession,
    runtime: RuntimeInstanceRecord,
    future_id: int,
    *,
    graceful: bool,
    killed_pids: list[int] | None = None,
    message: str | None = None,
) -> OperationFutureRecord | None:
    runtime.status = RuntimeState.STOPPED.value
    runtime.ready = False
    runtime.process_pid = None
    runtime.updated_at = utcnow()


    result = await session.execute(
        select(RuntimeActionRecord)
        .where(RuntimeActionRecord.runtime_id == runtime.runtime_id)
        .where(RuntimeActionRecord.future_id == future_id)
        .where(RuntimeActionRecord.action_type.in_(CLOSE_ACTION_TYPES))
        .order_by(RuntimeActionRecord.action_id.desc())
    )
    actions = result.scalars().all()
    for action in actions:
        if action.status in {ActionState.COMPLETED.value, ActionState.FAILED.value}:
            continue
        action.status = ActionState.FAILED.value
        action.error_message = message or "runtime close action did not complete before forced cleanup"
        action.completed_at = utcnow()

    future = await session.get(OperationFutureRecord, future_id)
    if future is None:
        return None
    if future.status == FutureState.PENDING.value:
        future.status = FutureState.COMPLETED.value
        future.result_data = _close_runtime_output(
            runtime,
            graceful=graceful,
            killed_pids=killed_pids,
            message=message,
        )
        future.completed_at = utcnow()
    return future


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_process_table() -> list[tuple[int, int, str]]:
    try:
        output = subprocess.check_output(["ps", "-eo", "pid=,pgid=,args="], text=True)
    except Exception:
        return []
    rows: list[tuple[int, int, str]] = []
    for line in output.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            pgid = int(parts[1])
        except ValueError:
            continue
        rows.append((pid, pgid, parts[2]))
    return rows


def _runtime_process_ids(runtime: RuntimeInstanceRecord) -> tuple[set[int], set[int]]:
    rows = _read_process_table()
    pids: set[int] = set()
    pgids: set[int] = set()

    if runtime.process_pid and _pid_alive(runtime.process_pid):
        pids.add(runtime.process_pid)
        for pid, pgid, _args in rows:
            if pid == runtime.process_pid:
                pgids.add(pgid)
                break
        else:
            try:
                pgids.add(os.getpgid(runtime.process_pid))
            except ProcessLookupError:
                pass

    runtime_markers = (
        "runtime_pipeline.py",
        "tinker_backend.rock_adapter",
        "run_dummy.py",
    )
    for pid, pgid, args in rows:
        if runtime.runtime_id not in args:
            continue
        if not any(marker in args for marker in runtime_markers):
            continue
        pids.add(pid)
        pgids.add(pgid)

    # Include children in the process groups we are about to terminate, such as
    # vLLM EngineCore processes spawned by the ROLL runtime.
    if pgids:
        for pid, pgid, _args in rows:
            if pgid in pgids:
                pids.add(pid)
    return pids, pgids


def terminate_runtime_processes(runtime: RuntimeInstanceRecord, *, grace_seconds: float = 5.0) -> list[int]:
    pids, pgids = _runtime_process_ids(runtime)
    if not pids and not pgids:
        return []

    current_pid = os.getpid()
    pids.discard(current_pid)
    signaled: set[int] = set(pids)

    for pgid in sorted(pgids):
        if pgid <= 0 or pgid == os.getpgrp():
            continue
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass

    for pid in sorted(pids):
        if pid <= 0:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass

    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        if not any(_pid_alive(pid) for pid in pids):
            return sorted(signaled)
        time.sleep(0.2)

    for pgid in sorted(pgids):
        if pgid <= 0 or pgid == os.getpgrp():
            continue
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass

    for pid in sorted(pids):
        if pid <= 0 or not _pid_alive(pid):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
    return sorted(signaled)

async def record_runtime_heartbeat(
    session: AsyncSession,
    runtime_id: str,
    body: RuntimeHeartbeatRequest,
) -> RuntimeInstanceRecord:
    runtime = await session.get(RuntimeInstanceRecord, runtime_id)
    if runtime is None:
        raise ValueError(f"Runtime {runtime_id} not found")
    if body.runtime_id is not None and body.runtime_id != runtime_id:
        raise ValueError("runtime_id in path and body do not match")

    runtime.last_heartbeat_at = utcnow()
    runtime.updated_at = runtime.last_heartbeat_at
    if body.adapter_base_url is not None:
        runtime.adapter_base_url = body.adapter_base_url
    if body.process_pid is not None:
        runtime.process_pid = body.process_pid

    if body.error_message or body.status == RuntimeState.FAILED.value:
        await _fail_create_runtime_future(session, runtime, body.error_message or "runtime heartbeat reported failure")
        await session.flush()
        return runtime

    if runtime.status in {RuntimeState.STOPPING.value, RuntimeState.STOPPED.value}:
        if body.status == RuntimeState.STOPPED.value:
            runtime.status = RuntimeState.STOPPED.value
            runtime.ready = False
            runtime.error_message = None
        await session.flush()
        return runtime

    runtime.status = body.status or (RuntimeState.READY.value if body.ready else RuntimeState.STARTING.value)
    runtime.ready = bool(body.ready)
    runtime.error_message = None
    if runtime.ready and runtime.ready_at is None:
        runtime.ready_at = utcnow()
    if runtime.ready:
        runtime.status = RuntimeState.READY.value
        await _complete_create_runtime_future(session, runtime)

    await session.flush()
    return runtime
