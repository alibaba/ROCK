"""ROCK adapter for Tinker backend pull-mode task environments."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import json
import logging
import os
import re
import shlex
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml
from tinker_backend.rock_config import ROCKJobDefaults
from tinker_backend.rock_config import load_job_config
from rock.sdk.bench import AgentConfig
from rock.sdk.job import Job, JobConfig
from rock.sdk.job.operator import Operator
from rock.sdk.job.trial.harbor import HarborTrial
from rock.sdk.sandbox.model_service.base import ModelService, ModelServiceConfig

from tinker_backend.config import get_settings
from tinker_backend.protocol.schemas import OpenAIChatPrompt
from tinker_backend.services.task_catalog import list_task_ids

logger = logging.getLogger(__name__)

_DEFAULT_MODEL_SERVICE_PORT = 28080
_CLAIM_ACTIVE_INTERVAL_SECONDS = 0.5
_CLAIM_EMPTY_BACKOFF_SECONDS = (1.0, 5.0, 10.0)
_HEARTBEAT_INTERVAL_SECONDS = 30.0
_DEFAULT_MODEL_SERVICE_INSTALL_TIMEOUT = 900
_DEFAULT_MODEL_SERVICE_INSTALL_CMD = (
    "pip install 'rl-rock[model-service]'"
    " -i https://mirrors.aliyun.com/pypi/simple/"
    " --no-extra-index-url"
    " --trusted-host mirrors.aliyun.com"
    " --timeout 600"
)
_MODEL_SERVICE_HEALTH_INTERVAL_SECONDS = 10.0
_MODEL_SERVICE_HEALTH_FAILURES = 2
_MODEL_SERVICE_PROBE_TIMEOUT_SECONDS = 20
_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
_OSS_ENV_TO_MIRROR_FIELD = {
    "OSS_BUCKET": "oss_bucket",
    "OSS_ACCESS_KEY_ID": "oss_access_key_id",
    "OSS_ACCESS_KEY_SECRET": "oss_access_key_secret",
    "OSS_REGION": "oss_region",
    "OSS_ENDPOINT": "oss_endpoint",
}


class _SuppressClaimHttpLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return "/api/v1/runtimes/" not in message or "/actions/claim" not in message


def _install_httpx_claim_log_filter() -> None:
    httpx_logger = logging.getLogger("httpx")
    if any(isinstance(item, _SuppressClaimHttpLogFilter) for item in httpx_logger.filters):
        return
    httpx_logger.addFilter(_SuppressClaimHttpLogFilter())


def _read_yaml(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Adapter config must be a YAML mapping: {path}")
    return loaded


def _float_sequence(value: Any, default: tuple[float, ...]) -> tuple[float, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = [value]
    parsed: list[float] = []
    for item in items:
        try:
            parsed.append(max(0.0, float(item)))
        except (TypeError, ValueError):
            continue
    return tuple(parsed) or default


def _load_job_config_with_env_expansion(job_config_path: str) -> JobConfig:
    """Match ROLL's Harbor config loading: expand ${ENV_VAR} before parsing."""
    raw_config = Path(job_config_path).read_text(encoding="utf-8")
    expanded_config = _ENV_VAR_PATTERN.sub(
        lambda match: os.environ.get(match.group(1), match.group(0)),
        raw_config,
    )
    if expanded_config == raw_config:
        return JobConfig.from_yaml(job_config_path)

    suffix = Path(job_config_path).suffix or ".yaml"
    with tempfile.NamedTemporaryFile("w", suffix=suffix, encoding="utf-8") as config_file:
        config_file.write(expanded_config)
        config_file.flush()
        return JobConfig.from_yaml(config_file.name)


def _configured_oss_env() -> dict[str, str]:
    return {
        name: value
        for name in [
            "OSS_ACCESS_KEY_ID",
            "OSS_ACCESS_KEY_SECRET",
            "OSS_REGION",
            "OSS_ENDPOINT",
            "OSS_BUCKET",
            "OSS_DATASET_PATH",
        ]
        if (value := os.environ.get(name))
    }


def _apply_oss_env_to_config(config: JobConfig) -> None:
    oss_env = _configured_oss_env()
    if not oss_env:
        return

    environment = getattr(config, "environment", None)
    if environment is None:
        return

    if getattr(environment, "env", None) is None:
        environment.env = {}
    environment.env.update(oss_env)

    mirror = getattr(environment, "oss_mirror", None)
    if mirror is None:
        return
    mirror.enabled = True
    for env_name, field_name in _OSS_ENV_TO_MIRROR_FIELD.items():
        value = oss_env.get(env_name)
        if value:
            setattr(mirror, field_name, value)


def _shell_export_lines(env: dict[str, str]) -> list[str]:
    return [f"export {key}={shlex.quote(value)}" for key, value in env.items()]


def _model_service_health_command(port: int) -> str:
    code = (
        "import urllib.request; "
        f"resp = urllib.request.urlopen('http://127.0.0.1:{port}/health', timeout=3); "
        "body = resp.read(200).decode(errors='replace'); "
        "print(f'health_status={resp.status}'); "
        "print(f'health_body={body}'); "
        "raise SystemExit(0 if resp.status == 200 else 1)"
    )
    return f"python -c {shlex.quote(code)}"


def _model_service_diagnostics_command(port: int, label: str) -> str:
    python_probe = (
        "import importlib.util, sys; "
        "print('python_executable=' + sys.executable); "
        "print('psutil_spec=' + str(importlib.util.find_spec('psutil'))); "
        "print('rock_spec=' + str(importlib.util.find_spec('rock')))"
    )
    return " ; ".join(
        [
            "set +e",
            f"echo probe_label={shlex.quote(label)}",
            "printf 'which_rock='; command -v rock || true",
            "printf 'which_python='; command -v python || true",
            f"python -c {shlex.quote(python_probe)} || true",
            "printf 'pid_file='; cat data/cli/model/pid.txt 2>/dev/null || true; echo",
            "echo model_service_processes_begin",
            "ps -ef | grep -E 'rock.sdk.model.server|model-service|uvicorn|python.* -m main' | grep -v grep || true",
            "echo model_service_processes_end",
            f"{_model_service_health_command(port)} || true",
            "for f in /data/logs/model_service.log /data/logs/LLMService.log; do "
            "echo log_tail_begin:$f; "
            "tail -80 $f 2>/dev/null || true; "
            "echo log_tail_end:$f; "
            "done",
        ]
    )


async def _run_model_service_probe(
    model_service: ModelService,
    command: str,
    *,
    wait_timeout: int = _MODEL_SERVICE_PROBE_TIMEOUT_SECONDS,
) -> tuple[int | None, str]:
    runtime_env = getattr(model_service, "runtime_env", None)
    sandbox = getattr(model_service, "_sandbox", None)
    if runtime_env is None or sandbox is None:
        return 1, "ModelService runtime_env or sandbox is unavailable"
    try:
        result = await sandbox.arun(
            cmd=runtime_env.wrapped_cmd(command),
            session=None,
            wait_timeout=wait_timeout,
        )
    except Exception as exc:
        return 1, f"probe command failed: {type(exc).__name__}: {exc}"
    return result.exit_code, result.output or ""



class ModelServiceHarborTrial(HarborTrial):
    """HarborTrial that installs and starts ModelService inside the sandbox."""

    def __init__(self, config, model_service_port: int = _DEFAULT_MODEL_SERVICE_PORT):
        super().__init__(config)
        self._model_service: Optional[ModelService] = None
        self._model_service_port = model_service_port
        self._model_service_install_cmd = _DEFAULT_MODEL_SERVICE_INSTALL_CMD
        self._model_service_install_timeout = _DEFAULT_MODEL_SERVICE_INSTALL_TIMEOUT
        self._sandbox_host_ip: Optional[str] = None

    def configure_model_service(self, install_cmd: str, install_timeout: int) -> None:
        self._model_service_install_cmd = install_cmd
        self._model_service_install_timeout = install_timeout

    def build(self) -> str:
        script = super().build()
        export_lines = _shell_export_lines(_configured_oss_env())
        if not export_lines:
            return script
        return "\n".join([script.split("\n", 1)[0], *export_lines, script.split("\n", 1)[1]])

    async def on_sandbox_ready(self, sandbox) -> None:
        await super().on_sandbox_ready(sandbox)

        obs = await sandbox.arun("hostname -I 2>/dev/null | awk '{print $1}'")
        self._sandbox_host_ip = obs.output.strip() or getattr(sandbox, "host_ip", None)

        if self._sandbox_host_ip and getattr(self, "_config", None) and self._config.agents:
            self._config.agents[0].kwargs["api_base"] = (
                f"http://{self._sandbox_host_ip}:{self._model_service_port}/v1"
            )

        ms_config = ModelServiceConfig(
            enabled=True,
            type="local",
            install_timeout=self._model_service_install_timeout,
            install_cmd=self._model_service_install_cmd,
            start_cmd=(
                f"rock model-service start --type local"
                f" --host 0.0.0.0 --port {self._model_service_port}"
            ),
            stop_cmd="rock model-service stop",
            watch_agent_cmd=(
                f"rock model-service watch-agent --pid ${{pid}}"
                f" --host 127.0.0.1 --port {self._model_service_port}"
            ),
        )
        self._model_service = ModelService(sandbox=sandbox, config=ms_config)
        await self._model_service.install()
        await self._model_service.start()
        await self._verify_model_service_after_start()

    async def _verify_model_service_after_start(self) -> None:
        if self._model_service is None:
            return
        sandbox = getattr(self._model_service, "_sandbox", None)
        sandbox_id = getattr(sandbox, "sandbox_id", "unknown")
        exit_code, diagnostics = await _run_model_service_probe(
            self._model_service,
            _model_service_diagnostics_command(self._model_service_port, "post_start"),
            wait_timeout=30,
        )
        logger.info(
            "[%s] ModelService post-start diagnostics exit_code=%s\n%s",
            sandbox_id,
            exit_code,
            diagnostics,
        )
        health_code, health_output = await _run_model_service_probe(
            self._model_service,
            _model_service_health_command(self._model_service_port),
            wait_timeout=10,
        )
        if health_code != 0:
            raise RuntimeError(
                f"ModelService health check failed after start in sandbox {sandbox_id}: {health_output}"
            )

    @property
    def model_service(self) -> Optional[ModelService]:
        return self._model_service


class ModelServiceOperator(Operator):
    def __init__(
        self,
        model_service_port: int = _DEFAULT_MODEL_SERVICE_PORT,
        model_service_install_cmd: str = _DEFAULT_MODEL_SERVICE_INSTALL_CMD,
        model_service_install_timeout: int = _DEFAULT_MODEL_SERVICE_INSTALL_TIMEOUT,
    ):
        self._model_service_port = model_service_port
        self._model_service_install_cmd = model_service_install_cmd
        self._model_service_install_timeout = model_service_install_timeout

    def apply(self, config) -> list:
        trial = ModelServiceHarborTrial(config, model_service_port=self._model_service_port)
        trial.configure_model_service(
            install_cmd=self._model_service_install_cmd,
            install_timeout=self._model_service_install_timeout,
        )
        return [trial]


class LiveEnv:
    def __init__(
        self,
        env_id: str,
        job: Job,
        trial: ModelServiceHarborTrial,
        model_service: ModelService,
        harbor_pid: str | None = None,
        job_name: str | None = None,
    ):
        self.env_id = env_id
        self.job = job
        self.trial = trial
        self.model_service = model_service
        self.harbor_pid = harbor_pid
        self.job_name = job_name
        self.current_index = 0


class RockAdapter:
    def __init__(
        self,
        backend_url: str,
        runtime_id: str,
        config_path: str | None = None,
        job_config_path: str | None = None,
        model_service_port: int | None = None,
    ):
        self.backend_url = backend_url.rstrip("/")
        self.runtime_id = runtime_id
        self.config_path = config_path or os.environ.get("TINKER_CONFIG_PATH")
        self.adapter_config = _read_yaml(self.config_path)
        rock_cfg = self._rock_config()
        self.job_config_path = job_config_path or rock_cfg.get("job_config_path")
        self.model_service_port = int(model_service_port or rock_cfg.get("model_service_port") or _DEFAULT_MODEL_SERVICE_PORT)
        self.model_service_install_cmd = str(
            rock_cfg.get("model_service_install_cmd") or _DEFAULT_MODEL_SERVICE_INSTALL_CMD
        )
        self.model_service_install_timeout = int(
            rock_cfg.get("model_service_install_timeout") or _DEFAULT_MODEL_SERVICE_INSTALL_TIMEOUT
        )
        self.claim_active_interval = float(
            rock_cfg.get("claim_active_interval_seconds") or _CLAIM_ACTIVE_INTERVAL_SECONDS
        )
        self.claim_empty_backoff_seconds = _float_sequence(
            rock_cfg.get("empty_claim_backoff_seconds"),
            _CLAIM_EMPTY_BACKOFF_SECONDS,
        )
        self.bench_name: str | None = rock_cfg.get("bench_name")
        self._job_config_overrides: dict[str, Any] = dict(rock_cfg.get("job_config_overrides", {}))
        agent_cfg = rock_cfg.get("agent", {}) if isinstance(rock_cfg.get("agent"), dict) else {}
        raw_max_iterations = agent_cfg.get("max_iterations")
        self._agent_max_iterations = int(raw_max_iterations) if raw_max_iterations is not None else None
        if self._agent_max_iterations is not None and self._agent_max_iterations <= 0:
            raise ValueError("rock.agent.max_iterations must be greater than zero")
        defaults = ROCKJobDefaults(
            rock_key=os.environ.get("ROCK_KEY", ""),
            openai_api_key="placeholder",
            openai_base_url="placeholder",
            openai_model="placeholder",
        )
        self._job_defaults = ROCKJobDefaults.from_env(defaults=defaults)
        self.live_envs: dict[str, LiveEnv] = {}
        self.pending_jobs: dict[str, Job] = {}
        self._http: httpx.AsyncClient | None = None
        self._last_heartbeat_at = 0.0
        self._tasks: set[asyncio.Task] = set()
        self.should_stop = False

    def _rock_config(self) -> dict[str, Any]:
        rock = self.adapter_config.get("rock")
        return rock if isinstance(rock, dict) else {}

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=60.0)
        return self._http

    async def _post_heartbeat(self, *, ready: bool, status: str = "ready", error_message: str | None = None) -> None:
        client = await self._client()
        resp = await client.post(
            f"{self.backend_url}/api/v1/runtimes/{self.runtime_id}/heartbeat",
            json={
                "runtime_id": self.runtime_id,
                "status": status,
                "ready": ready,
                "process_pid": os.getpid(),
                "error_message": error_message,
                "metadata": {"adapter": "rock", "config_path": self.config_path},
            },
        )
        resp.raise_for_status()
        self._last_heartbeat_at = asyncio.get_running_loop().time()

    async def _claim_actions(self, action_types: list[str] | None = None, limit: int = 5) -> list[dict]:
        client = await self._client()
        resp = await client.post(
            f"{self.backend_url}/api/v1/runtimes/{self.runtime_id}/actions/claim",
            json={"action_types": action_types, "limit": limit},
        )
        resp.raise_for_status()
        return resp.json().get("actions", [])

    async def _post_result(
        self,
        action_id: int,
        status: str = "completed",
        result_data: dict | None = None,
        error_message: str | None = None,
    ) -> None:
        client = await self._client()
        resp = await client.post(
            f"{self.backend_url}/api/v1/runtimes/{self.runtime_id}/actions/{action_id}/result",
            json={"status": status, "result_data": result_data, "error_message": error_message},
        )
        resp.raise_for_status()

    async def _post_step(
        self,
        env_id: str,
        step_id: int,
        prompt: dict | None = None,
        finish_reason: str | None = None,
        reward: float | None = None,
    ) -> None:
        client = await self._client()
        resp = await client.post(
            f"{self.backend_url}/api/v1/runtimes/{self.runtime_id}/envs/{env_id}/steps",
            json={"step_id": step_id, "prompt": prompt, "finish_reason": finish_reason, "reward": reward},
        )
        resp.raise_for_status()

    async def run_loop(self) -> None:
        logger.info("RockAdapter starting for runtime=%s backend=%s", self.runtime_id, self.backend_url)
        empty_claim_count = 0
        while not self.should_stop:
            try:
                actions = await self._claim_actions(["init_task_env", "deliver_sample", "close_rock_adapter"])
            except Exception as e:
                logger.warning("Failed to claim actions: %s", e)
                await asyncio.sleep(self.claim_empty_backoff_seconds[-1])
                continue

            if not actions:
                backoff = self.claim_empty_backoff_seconds[
                    min(empty_claim_count, len(self.claim_empty_backoff_seconds) - 1)
                ]
                empty_claim_count += 1
                await asyncio.sleep(backoff)
                continue

            empty_claim_count = 0
            for action in actions:
                if action.get("action_type") == "close_rock_adapter":
                    await self._dispatch_action(action)
                    break
                task = asyncio.create_task(self._run_action(action))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            if not self.should_stop and self.claim_active_interval > 0:
                await asyncio.sleep(self.claim_active_interval)

        if self._tasks:
            await asyncio.wait(self._tasks, timeout=5)

    async def _run_action(self, action: dict) -> None:
        try:
            await self._dispatch_action(action)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Error processing action %s: %s", action.get("action_id"), e, exc_info=True)
            error_message = self._truncate_message(f"{type(e).__name__}: {e}")
            cleanup_errors = await self._cleanup_action_env_after_failure(action)
            if cleanup_errors:
                error_message = self._truncate_message(
                    f"{error_message}; cleanup_errors={'; '.join(cleanup_errors)}"
                )
            try:
                await self._post_result(action["action_id"], status="failed", error_message=error_message)
            except Exception:
                logger.error("Failed to post error result", exc_info=True)

    async def _dispatch_action(self, action: dict) -> None:
        action_type = action["action_type"]
        action_id = action["action_id"]
        env_id = action.get("env_id")
        payload = action.get("payload", {})
        logger.info("Dispatching action %s type=%s env_id=%s", action_id, action_type, env_id)
        if action_type == "init_task_env":
            await self._handle_init_task_env(action_id, env_id, payload)
        elif action_type == "deliver_sample":
            await self._handle_deliver_sample(action_id, env_id, payload)
        elif action_type == "close_rock_adapter":
            await self._handle_close_rock_adapter(action_id, payload)
        else:
            await self._post_result(action_id, status="completed", result_data={"stub": True})

    async def _handle_init_task_env(self, action_id: int, env_id: str, payload: dict) -> None:
        if not env_id:
            raise RuntimeError("init_task_env action is missing env_id")
        task_id = payload.get("task_id", "unknown")
        dataset = payload.get("dataset", "")
        split = payload.get("split", "")
        metadata = payload.get("metadata") or {}
        bench_name = metadata.get("bench_name")
        job_id = f"tinker_{env_id}_{uuid.uuid4().hex[:8]}"
        logger.info("[init_task_env] env=%s job=%s bench=%s starting ROCK sandbox", env_id, job_id, bench_name)

        operator = self._make_operator()
        config = self._build_job_config(task_id, dataset, split, env_id, job_id, bench_name=bench_name)
        model_service_url = f"http://127.0.0.1:{self.model_service_port}/v1"
        config.agents[0].kwargs["api_base"] = model_service_url
        if getattr(config, "environment", None) and isinstance(getattr(config.environment, "env", None), dict):
            config.environment.env["OPENAI_BASE_URL"] = model_service_url
        job = Job(config=config, operator=operator)
        self.pending_jobs[env_id] = job
        submit_completed = False
        try:
            await job.submit()
            submit_completed = True
        finally:
            if submit_completed:
                self.pending_jobs.pop(env_id, None)

        trial_client = job._job_client.trials[0]
        trial: ModelServiceHarborTrial = trial_client.trial
        model_service = trial.model_service
        if model_service is None:
            raise RuntimeError(f"ModelService not initialized for env {env_id}")
        harbor_pid = str(trial_client.pid)
        live_env = LiveEnv(
            env_id=env_id,
            job=job,
            trial=trial,
            model_service=model_service,
            harbor_pid=harbor_pid,
            job_name=job_id,
        )
        self.live_envs[env_id] = live_env

        logger.info("[init_task_env] env=%s starting watch_agent pid=%s", env_id, harbor_pid)
        asyncio.ensure_future(model_service.watch_agent(pid=harbor_pid))

        logger.info("[init_task_env] env=%s waiting for first LLM request", env_id)
        raw_output = await self._anti_call_llm_with_health_monitor(
            live_env,
            index=0,
            response_payload=None,
            phase="init_task_env",
        )
        raw_stripped = raw_output.strip() if raw_output else ""

        if "SESSION_END" in raw_stripped:
            finish_reason, reward, error_message = await self._finish_env(live_env)
            await self._close_finished_env(env_id, live_env)
            await self._post_step(env_id, step_id=0, finish_reason=finish_reason, reward=reward)
            error_message = error_message or (
                f"ROCK env {env_id} ended before the first LLM request "
                f"(finish_reason={finish_reason}, reward={reward})"
            )
            await self._post_result(
                action_id,
                status="failed",
                result_data={
                    "env_id": env_id,
                    "finished": True,
                    "finish_reason": finish_reason,
                    "reward": reward,
                },
                error_message=error_message,
            )
            logger.error("[init_task_env] env=%s failed before first LLM request: %s", env_id, error_message)
            return
        else:
            prompt = self._parse_llm_request_to_prompt(raw_stripped)
            await self._post_step(env_id, step_id=0, prompt=prompt)

        await self._post_result(action_id, status="completed", result_data={"env_id": env_id})
        logger.info("[init_task_env] env=%s completed", env_id)

    def _make_operator(self) -> Operator:
        rock_cfg = self._rock_config()
        factory = rock_cfg.get("operator_factory")
        if not factory:
            return ModelServiceOperator(
                model_service_port=self.model_service_port,
                model_service_install_cmd=self.model_service_install_cmd,
                model_service_install_timeout=self.model_service_install_timeout,
            )
        module, separator, name = str(factory).partition(":")
        if not separator or not module or not name or ":" in name:
            raise ValueError("rock.operator_factory must be module:Class")
        operator_type = getattr(importlib.import_module(module), name)
        operator = operator_type(**dict(rock_cfg.get("operator_kwargs") or {}))
        if not isinstance(operator, Operator):
            raise TypeError("rock.operator_factory must construct a ROCK Operator")
        return operator

    def _anti_call_timeout(self) -> int:
        rock_cfg = self._rock_config()
        runtime_cfg = rock_cfg.get("runtime", {}) if isinstance(rock_cfg.get("runtime"), dict) else {}
        return int(rock_cfg.get("anti_call_timeout_sec") or runtime_cfg.get("anti_call_timeout_sec") or 1800)

    async def _handle_deliver_sample(self, action_id: int, env_id: str | None, payload: dict) -> None:
        if env_id is None or env_id not in self.live_envs:
            await self._post_result(action_id, status="failed", error_message=f"Unknown env: {env_id}")
            return

        live_env = self.live_envs[env_id]
        live_env.current_index += 1
        sample_response = payload.get("sample_response")
        if not isinstance(sample_response, dict):
            await self._post_result(action_id, status="failed", error_message="deliver_sample missing sample_response")
            return
        response_payload = json.dumps(self._make_openai_response(sample_response), ensure_ascii=False)

        raw_output = await self._anti_call_llm_with_health_monitor(
            live_env,
            index=live_env.current_index,
            response_payload=response_payload,
            phase="deliver_sample",
        )
        raw_stripped = raw_output.strip() if raw_output else ""

        if "SESSION_END" in raw_stripped:
            finish_reason, reward, error_message = await self._finish_env(live_env)
            # Complete terminal cleanup before the caller can close the runtime.
            await self._close_finished_env(env_id, live_env)
            await self._post_step(env_id, step_id=live_env.current_index, finish_reason=finish_reason, reward=reward)
            result_data = {
                "env_id": env_id,
                "finished": True,
                "finish_reason": finish_reason,
                "reward": reward,
            }
            if error_message:
                result_data["error_message"] = error_message
                result_data["warning_message"] = error_message
            action_status = "completed"
            if error_message and reward <= 0:
                action_status = "failed"
            await self._post_result(
                action_id,
                status=action_status,
                result_data=result_data,
                error_message=error_message if action_status == "failed" else None,
            )
            return

        prompt = self._parse_llm_request_to_prompt(raw_stripped)
        await self._post_step(env_id, step_id=live_env.current_index, prompt=prompt)
        await self._post_result(action_id, status="completed", result_data={"env_id": env_id, "finished": False})

    async def _anti_call_llm_with_health_monitor(
        self,
        live_env: LiveEnv,
        *,
        index: int,
        response_payload: str | None,
        phase: str,
    ) -> str:
        anti_call_task = asyncio.create_task(
            live_env.model_service.anti_call_llm(
                index=index,
                response_payload=response_payload,
                call_timeout=self._anti_call_timeout(),
            )
        )
        monitor_task = asyncio.create_task(self._monitor_model_service(live_env, phase=phase, index=index))
        try:
            done, _pending = await asyncio.wait(
                {anti_call_task, monitor_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if anti_call_task in done:
                monitor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor_task
                return anti_call_task.result()

            error_message = monitor_task.result()
            anti_call_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await anti_call_task
            raise RuntimeError(error_message)
        finally:
            for task in (anti_call_task, monitor_task):
                if not task.done():
                    task.cancel()

    async def _monitor_model_service(self, live_env: LiveEnv, *, phase: str, index: int) -> str:
        failures = 0
        while True:
            await asyncio.sleep(_MODEL_SERVICE_HEALTH_INTERVAL_SECONDS)
            ok, detail = await self._check_model_service_health(live_env)
            if ok:
                if failures:
                    logger.info(
                        "ModelService health recovered env=%s phase=%s index=%s",
                        live_env.env_id,
                        phase,
                        index,
                    )
                failures = 0
                continue

            failures += 1
            logger.warning(
                "ModelService health check failed env=%s phase=%s index=%s failures=%s/%s detail=%s",
                live_env.env_id,
                phase,
                index,
                failures,
                _MODEL_SERVICE_HEALTH_FAILURES,
                detail,
            )
            if failures >= _MODEL_SERVICE_HEALTH_FAILURES:
                diagnostics = await self._collect_model_service_diagnostics(
                    live_env,
                    label=f"{phase}_index_{index}_health_failed",
                )
                return self._truncate_message(
                    f"ModelService became unhealthy while waiting for {phase} "
                    f"(env={live_env.env_id}, index={index}, job={live_env.job_name}, "
                    f"harbor_pid={live_env.harbor_pid}): {detail}\n{diagnostics}"
                )

    async def _check_model_service_health(self, live_env: LiveEnv) -> tuple[bool, str]:
        exit_code, output = await _run_model_service_probe(
            live_env.model_service,
            _model_service_health_command(self.model_service_port),
            wait_timeout=10,
        )
        detail = self._truncate_message(output.strip() or f"health exit_code={exit_code}", limit=1000)
        return exit_code == 0, detail

    async def _collect_model_service_diagnostics(self, live_env: LiveEnv, *, label: str) -> str:
        exit_code, output = await _run_model_service_probe(
            live_env.model_service,
            _model_service_diagnostics_command(self.model_service_port, label),
            wait_timeout=30,
        )
        return self._truncate_message(f"diagnostics_exit_code={exit_code}\n{output}", limit=3000)

    async def _handle_close_rock_adapter(self, action_id: int, payload: dict) -> None:
        del payload
        logger.info("Closing ROCK adapter runtime=%s live_envs=%s", self.runtime_id, list(self.live_envs))
        shutdown_result = await self.shutdown(close_http=False, cancel_tasks=True)
        errors = shutdown_result.get("errors", [])
        result_data = {
            "runtime_id": self.runtime_id,
            "status": "stopping",
            "graceful": not errors,
            "closed_env_ids": shutdown_result.get("closed_env_ids", []),
            "errors": errors,
            "process_pid": os.getpid(),
        }
        await self._post_result(
            action_id,
            status="completed" if not errors else "failed",
            result_data=result_data,
            error_message="; ".join(errors) if errors else None,
        )
        try:
            await self._post_heartbeat(ready=False, status="stopped")
        except Exception:
            logger.warning("Failed to post stopped heartbeat", exc_info=True)
        self.should_stop = True

    async def _finish_env(self, live_env: LiveEnv) -> tuple[str, float, str | None]:
        rock_cfg = self._rock_config()
        timeout = float(rock_cfg.get("job_wait_timeout_sec", 300))
        try:
            result = await asyncio.wait_for(live_env.job.wait(), timeout=timeout)
        except Exception as exc:
            error_message = f"Failed to wait for ROCK job env={live_env.env_id}: {exc}"
            logger.warning(error_message, exc_info=True)
            return "exit", 0.0, self._truncate_message(error_message)

        finish_reason, reward = self._extract_finish(result)
        error_message = self._extract_error_message(result)
        if error_message:
            logger.error("ROCK job finished with exception env=%s: %s", live_env.env_id, error_message)
        else:
            logger.info(
                "ROCK job finished env=%s finish_reason=%s reward=%s",
                live_env.env_id,
                finish_reason,
                reward,
            )
        return finish_reason, reward, error_message

    @staticmethod
    def _extract_finish(result: Any) -> tuple[str, float]:
        status = str(getattr(getattr(result, "status", None), "value", getattr(result, "status", "")))
        reward = 0.0
        trial_results = getattr(result, "trial_results", None) or []
        if trial_results:
            verifier_result = getattr(trial_results[0], "verifier_result", None)
            rewards = getattr(verifier_result, "rewards", None) if verifier_result is not None else None
            if isinstance(rewards, dict):
                try:
                    reward = float(rewards.get("reward", 0.0) or 0.0)
                except (TypeError, ValueError):
                    reward = 0.0
        normalized_status = status.upper()
        finish_reason = "FINISHED" if "FINISHED" in normalized_status else "exit"
        return finish_reason, reward

    @staticmethod
    def _extract_error_message(result: Any) -> str | None:
        trial_results = getattr(result, "trial_results", None) or []
        for index, trial_result in enumerate(trial_results):
            exception_info = getattr(trial_result, "exception_info", None)
            if exception_info is None:
                continue
            exception_type = getattr(exception_info, "exception_type", None) or type(exception_info).__name__
            exception_message = getattr(exception_info, "exception_message", None) or str(exception_info)
            occurred_at = getattr(exception_info, "occurred_at", None)
            task_name = getattr(trial_result, "task_name", None) or f"trial[{index}]"
            parts = [f"{task_name}: {exception_type}: {exception_message}"]
            if occurred_at:
                parts.append(f"occurred_at={occurred_at}")
            return RockAdapter._truncate_message("; ".join(parts))

        status = str(getattr(getattr(result, "status", None), "value", getattr(result, "status", "")))
        normalized_status = status.lower()
        if normalized_status and not any(token in normalized_status for token in ("completed", "finished", "success")):
            raw_output = str(getattr(result, "raw_output", "") or "").strip()
            message = f"ROCK job status={status}"
            if raw_output:
                message = f"{message}: {raw_output[-1000:]}"
            return RockAdapter._truncate_message(message)
        return None

    @staticmethod
    def _truncate_message(message: str, limit: int = 4000) -> str:
        if len(message) <= limit:
            return message
        return message[: limit - 3] + "..."

    def _build_job_config(
        self,
        task_id: str,
        dataset: str,
        split: str,
        env_id: str,
        job_id: str,
        bench_name: str | None = None,
    ) -> JobConfig:
        effective_bench = bench_name or self.bench_name
        if not effective_bench:
            return self._build_job_config_legacy(
                task_id, dataset, split, env_id, job_id,
            )

        rock_cfg = self._rock_config()
        llm_cfg = rock_cfg.get("llm", {}) if isinstance(rock_cfg.get("llm"), dict) else {}
        raw_model = llm_cfg.get("model_name")
        model_name = "openai/{}".format(raw_model) if raw_model else None

        job_overrides: dict[str, Any] = {
            "environment.env.TASK_ID": task_id,
            "environment.env.DATASET": dataset,
            "environment.env.SPLIT": split,
            "datasets.0.name": dataset,
            "datasets.0.version": split,
            "datasets.0.registry.split": split,
            **self._job_config_overrides,
        }
        if self.job_config_path:
            datasets = _read_yaml(self.job_config_path).get("datasets") or []
            if datasets and datasets[0].get("path") is not None:
                # Registry fields can make the union parser discard a local path.
                for key in ("datasets.0.name", "datasets.0.version", "datasets.0.registry.split"):
                    if key not in self._job_config_overrides:
                        job_overrides.pop(key, None)
        if self._agent_max_iterations is not None:
            job_overrides["agents.0.kwargs.max_iterations"] = self._agent_max_iterations

        runtime_cfg = rock_cfg.get("runtime", {}) if isinstance(rock_cfg.get("runtime"), dict) else {}
        task_timeout_sec = runtime_cfg.get("task_timeout_sec")
        if task_timeout_sec is not None:
            job_overrides["agents.0.max_timeout_sec"] = int(task_timeout_sec)

        config = load_job_config(
            effective_bench,
            default_config=self._job_defaults,
            job_config_path=self.job_config_path,
            template_root=rock_cfg.get("template_root"),
            task_names=[task_id],
            model_name=model_name,
            env={"OPENAI_BASE_URL": "", "OPENAI_API_KEY": str(env_id)},
            **job_overrides,
        )

        experiment_id = os.environ.get("TASK_ID", self.runtime_id)
        config.job_name = job_id
        config.experiment_id = experiment_id
        if config.environment:
            config.environment.experiment_id = experiment_id

        return config

    def _build_job_config_legacy(
        self,
        task_id: str,
        dataset: str,
        split: str,
        env_id: str,
        job_id: str,
    ) -> JobConfig:
        if not self.job_config_path:
            raise RuntimeError("rock.job_config_path or rock.bench_name is required to start a ROCK Harbor job")
        base_config = _load_job_config_with_env_expansion(self.job_config_path)
        config = base_config.model_copy(deep=True)
        _apply_oss_env_to_config(config)
        config.job_name = job_id

        rock_cfg = self._rock_config()
        llm_cfg = rock_cfg.get("llm", {}) if isinstance(rock_cfg.get("llm"), dict) else {}
        agent_cfg = rock_cfg.get("agent", {}) if isinstance(rock_cfg.get("agent"), dict) else {}
        runtime_cfg = rock_cfg.get("runtime", {}) if isinstance(rock_cfg.get("runtime"), dict) else {}

        config.agents = [
            AgentConfig(
                name=str(agent_cfg.get("name", "swe-agent-internal")),
                model_name="openai/{}".format(llm_cfg.get("model_name", "default_model")),
                max_timeout_sec=int(runtime_cfg.get("task_timeout_sec", 1800)),
                kwargs={
                    "api_key": str(env_id),
                    "api_base": "",
                    "sweagent_config": agent_cfg.get("scaffold_config", "anthropic"),
                    "max_iterations": int(agent_cfg.get("max_iterations", 15)),
                    "temperature": float(llm_cfg.get("temperature", 0.99)),
                    "max_tokens": int(llm_cfg.get("max_tokens", 2048)),
                    "num_retries": int(agent_cfg.get("num_retries", 4)),
                    "tools_parse_function": agent_cfg.get("tools_parse_function", "function_calling"),
                    "full_history": bool(agent_cfg.get("full_history", True)),
                    "max_observation_length": int(agent_cfg.get("max_observation_length", 10000)),
                },
            )
        ]

        if getattr(config, "datasets", None):
            config.datasets[0].task_names = [task_id]
            config.datasets[0].registry.split = split
            config.datasets[0].name = dataset
            config.datasets[0].version = split

        if getattr(config, "environment", None):
            if getattr(config.environment, "env", None) is None:
                config.environment.env = {}
            config.environment.env["TASK_ID"] = task_id
            config.environment.env["DATASET"] = dataset
            config.environment.env["SPLIT"] = split
            config.environment.experiment_id = os.environ.get("TASK_ID", self.runtime_id)

        config.experiment_id = os.environ.get("TASK_ID", self.runtime_id)
        try:
            config.verifier.native_config.template.name = f"swe-agent-internal/{dataset}"
        except Exception:
            pass
        return config

    def _parse_llm_request_to_prompt(self, raw_output: str | None) -> dict[str, Any]:
        if not raw_output or not raw_output.strip():
            raise ValueError("intercepted LLM request is empty")
        text = raw_output.strip()
        json_start = text.find("{")
        if json_start < 0:
            raise ValueError("intercepted LLM request does not contain a JSON object")
        try:
            request_dict, json_end = json.JSONDecoder().raw_decode(text, idx=json_start)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("intercepted LLM request is not valid JSON") from exc
        if text[json_end:].strip():
            raise ValueError("intercepted LLM request has unexpected content after the JSON object")
        if json_start:
            logger.debug(
                "_parse_llm_request_to_prompt: skipped %d chars of non-JSON prefix",
                json_start,
            )
        if not isinstance(request_dict, dict):
            raise ValueError("intercepted LLM request JSON must be an object")
        try:
            prompt = OpenAIChatPrompt(request=request_dict)
        except ValueError as exc:
            raise ValueError(
                f"intercepted LLM request is not an OpenAI chat request: {exc}"
            ) from exc
        return prompt.model_dump(mode="json")

    @staticmethod
    def _make_openai_response(sample_response: dict[str, Any]) -> dict[str, Any]:
        sequence = sample_response["sequences"][0]
        content = sequence.get("text") if sequence.get("text") is not None else sample_response.get("text")
        if content is None:
            content = sequence.get("content") if sequence.get("content") is not None else sample_response.get("content")
        if content is None:
            raise ValueError("sample_response sequence must include text or content for deliver_sample")
        tool_calls = sequence.get("tool_calls") or sample_response.get("tool_calls")
        finish_reason = sequence.get("finish_reason") or sequence.get("stop_reason") or sample_response.get("finish_reason") or "stop"
        message = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
            finish_reason = "tool_calls"
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
        }

    async def _cleanup_action_env_after_failure(self, action: dict) -> list[str]:
        env_id = action.get("env_id")
        if not env_id:
            return []
        errors: list[str] = []
        live_env = self.live_envs.pop(env_id, None)
        if live_env is not None:
            errors.extend(await self._close_live_env(env_id, live_env))
        pending_job = self.pending_jobs.pop(env_id, None)
        if pending_job is not None:
            errors.extend(await self._close_job_sandboxes(env_id, pending_job))
        return errors

    async def _close_job_sandboxes(self, env_id: str, job: Job, *, cancel_job: bool = True) -> list[str]:
        errors: list[str] = []
        if cancel_job:
            try:
                await job.cancel()
            except Exception as exc:
                errors.append(f"{env_id}: job.cancel failed: {exc}")

        job_client = getattr(job, "_job_client", None)
        for trial_client in getattr(job_client, "trials", []) or []:
            sandbox = getattr(trial_client, "sandbox", None)
            if sandbox is None:
                continue
            try:
                close = getattr(sandbox, "close", None)
                if callable(close):
                    await close()
                else:
                    stop = getattr(sandbox, "stop", None)
                    if callable(stop):
                        await stop()
            except Exception as exc:
                sandbox_id = getattr(sandbox, "sandbox_id", "unknown")
                errors.append(f"{env_id}: sandbox {sandbox_id} close failed: {exc}")
        return errors

    async def _close_finished_env(self, env_id: str, live_env: LiveEnv) -> None:
        # Keep failed cleanup registered so runtime shutdown can retry it.
        try:
            errors = await asyncio.wait_for(self._close_live_env(env_id, live_env, cancel_job=False), timeout=90)
        except Exception as exc:
            errors = [f"{env_id}: terminal cleanup failed: {exc}"]
        if errors:
            logger.warning("ROCK terminal cleanup: %s", errors)
        else:
            self.live_envs.pop(env_id, None)

    async def _close_live_env(self, env_id: str, live_env: LiveEnv, *, cancel_job: bool = True) -> list[str]:
        errors: list[str] = []
        try:
            await live_env.model_service.stop()
        except Exception as exc:
            errors.append(f"{env_id}: model_service.stop failed: {exc}")
        errors.extend(await self._close_job_sandboxes(env_id, live_env.job, cancel_job=cancel_job))
        return errors

    async def shutdown(self, *, close_http: bool = True, cancel_tasks: bool = False) -> dict[str, list[str]]:
        errors: list[str] = []
        if cancel_tasks:
            current_task = asyncio.current_task()
            for task in list(self._tasks):
                if task is current_task or task.done():
                    continue
                task.cancel()
            if self._tasks:
                _done, pending = await asyncio.wait(self._tasks, timeout=5)
                for task in pending:
                    errors.append("adapter task did not cancel within 5s")

        closed_env_ids: list[str] = []
        for env_id, job in list(self.pending_jobs.items()):
            logger.info("Shutting down pending ROCK job for env %s", env_id)
            errors.extend(await self._close_job_sandboxes(env_id, job))
            closed_env_ids.append(env_id)
            self.pending_jobs.pop(env_id, None)

        for env_id, live_env in list(self.live_envs.items()):
            logger.info("Shutting down live env %s", env_id)
            errors.extend(await self._close_live_env(env_id, live_env))
            closed_env_ids.append(env_id)
            self.live_envs.pop(env_id, None)
        if close_http and self._http and not self._http.is_closed:
            await self._http.aclose()
        result: dict[str, list[str]] = {}
        if closed_env_ids:
            result["closed_env_ids"] = closed_env_ids
        if errors:
            result["errors"] = errors
        return result

    # ------------------------------------------------------------------
    # ROCK task listing + filtering
    # ------------------------------------------------------------------

    def _list_tasks_from_catalog(self, dataset: str, split: str) -> list[str]:
        settings = get_settings()
        task_ids = list_task_ids(
            dataset,
            split,
        )
        logger.info(
            "[rock] Listed %d tasks from %s/%s via %s",
            len(task_ids),
            dataset,
            split,
            settings.tinker_platform,
        )
        return task_ids

    def list_dataset_tasks(
        self,
        datasets: list[dict[str, str]] | None = None,
    ) -> list[dict[str, str]]:
        """List task IDs from configured or provided datasets, with optional regex filtering.

        Each dataset entry: {"dataset": "org/name", "split": "test", "bench_name": "SWE-bench", "task_filter": "^django__"}
        Returns: [{"task_id": "...", "dataset": "...", "split": "...", "bench_name": "..."}]
        """
        datasets = datasets or self._rock_config().get("datasets", [])
        if not datasets:
            return []

        results: list[dict[str, str]] = []
        for ds in datasets:
            dataset = ds["dataset"]
            split = ds["split"]
            bench_name = ds.get("bench_name") or self.bench_name
            task_filter = ds.get("task_filter")

            task_ids = self._list_tasks_from_catalog(dataset, split)
            total = len(task_ids)

            if task_filter:
                pattern = re.compile(task_filter)
                task_ids = [tid for tid in task_ids if pattern.search(tid)]
                logger.info(
                    "[rock] %s: %d/%d tasks after filter '%s' from %s/%s",
                    bench_name, len(task_ids), total, task_filter, dataset, split,
                )
            else:
                logger.info(
                    "[rock] %s: %d tasks from %s/%s",
                    bench_name, total, dataset, split,
                )

            for tid in task_ids:
                results.append({
                    "task_id": tid,
                    "dataset": dataset,
                    "split": split,
                    "bench_name": bench_name,
                })
        return results


async def run_adapter(
    backend_url: str = "http://localhost:9000",
    runtime_id: str = "",
    config_path: str | None = None,
    job_config_path: str | None = None,
    model_service_port: int | None = None,
) -> None:
    adapter = RockAdapter(
        backend_url=backend_url,
        runtime_id=runtime_id,
        config_path=config_path,
        job_config_path=job_config_path,
        model_service_port=model_service_port,
    )
    try:
        await adapter.run_loop()
    finally:
        await adapter.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="Tinker ROCK adapter")
    parser.add_argument("--backend-url", default=os.environ.get("TINKER_BACKEND_BASE_URL", "http://localhost:9000"))
    parser.add_argument("--runtime-id", default=os.environ.get("TINKER_RUNTIME_ID"), required=False)
    parser.add_argument("--config-path", default=os.environ.get("TINKER_CONFIG_PATH"))
    parser.add_argument("--job-config", default=None)
    parser.add_argument("--model-service-port", type=int, default=None)
    args = parser.parse_args()
    if not args.runtime_id:
        parser.error("--runtime-id or TINKER_RUNTIME_ID is required")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    _install_httpx_claim_log_filter()
    asyncio.run(run_adapter(
        backend_url=args.backend_url,
        runtime_id=args.runtime_id,
        config_path=args.config_path,
        job_config_path=args.job_config,
        model_service_port=args.model_service_port,
    ))


if __name__ == "__main__":
    main()
