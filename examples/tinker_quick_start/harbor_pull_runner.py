"""Public Harbor operator for the original Tinker ModelService pull adapter.

Trusted tests are uploaded after the inner SWE-agent finishes.
"""
from __future__ import annotations

import yaml
from rock.sdk.job.operator import Operator
from rock.sdk.job.trial.abstract import AbstractTrial
from rock.sdk.job.trial.harbor import HarborTrial
from rock.sdk.sandbox.model_service.base import ModelService, ModelServiceConfig
from rock.sdk.sandbox.runtime_env import PythonRuntimeEnv, PythonRuntimeEnvConfig
from roll.pipeline.agentic.agent_runner.rock.pull_runner import ModelServiceHarborTrial

class LocalOnlyOssClient:
    """Disable SDK's optional STS/persistence probing for this sandbox only."""
    is_available = False
    _bucket = None
    _token_expire_time = None

    async def ensure_setup(self):
        return False

    async def close(self):
        pass

class PreinstalledPythonRuntimeEnv(PythonRuntimeEnv):
    # Do not register a new runtime type or change global SDK behavior.
    runtime_env_type = None

    def _get_install_cmd(self):
        return "test -x /opt/rocklet/bin/python && ln -s /opt/rocklet runtime-env"

    async def _post_init(self):
        await self.run("/opt/rocklet/bin/python -c 'import rock, fastapi, uvicorn, psutil, openai, httpx'")

class PreinstalledModelService(ModelService):
    async def install(self):
        self.runtime_env = PreinstalledPythonRuntimeEnv(
            self._sandbox, self.config.runtime_env_config,
        )
        self._sandbox.runtime_envs[self.runtime_env.runtime_env_id] = self.runtime_env
        await self.runtime_env.init()
        await self._create_rock_config()
        await self._install_model_service()
        self.is_installed = True

def public_harbor_config(config):
    """Explicit public schema, excluding SDK fork/OSS/sandbox extensions."""
    return {
        "job_name": config.job_name, "jobs_dir": str(config.jobs_dir),
        "n_attempts": 1, "n_concurrent_trials": 1,
        "environment": {"type": "docker", "delete": True},
        "agents": [agent.model_dump(mode="json", exclude_none=True)
                   for agent in config.agents],
        "datasets": [{"path": str(dataset.path),
                      "task_names": dataset.task_names, "n_tasks": 1}
                     for dataset in config.datasets],
        "verifier": {"disable": False, "env": {
            "UV_PYTHON": "/opt/python312/bin/python3.12", **config.verifier.env}},
    }

class PublicModelServiceHarborTrial(ModelServiceHarborTrial):
    async def on_sandbox_ready(self, sandbox):
        # SDK uploads otherwise probe /get_token even after DIRECT transfer.
        sandbox._oss = LocalOnlyOssClient()
        # Skip the original trial's online pip/runtime installation.
        await HarborTrial.on_sandbox_ready(self, sandbox)
        observation = await sandbox.arun("hostname -I 2>/dev/null | awk '{print $1}'")
        self._sandbox_host_ip = observation.output.strip()
        if not self._sandbox_host_ip:
            raise RuntimeError("Outer ROCK sandbox has no address for the inner agent")
        port = self._model_service_port
        agent = self._config.agents[0]
        # The original backend injects internal-agent connection kwargs. Public
        # SWE-agent accepts these through its environment, not its CLI schema.
        agent.kwargs.pop("api_base", None)
        agent.kwargs.pop("api_key", None)
        agent.env["OPENAI_BASE_URL"] = f"http://{self._sandbox_host_ip}:{port}/v1"
        agent.env["OPENAI_API_KEY"] = "public-local"
        cli = "/opt/rocklet/bin/rock model-service"
        config = ModelServiceConfig(
            enabled=True, type="local", install_cmd="true",
            runtime_env_config=PythonRuntimeEnvConfig(
                version="3.12", pip_index_url=None, pip=None,
                extra_symlink_executables=[],
            ),
            start_cmd=f"{cli} start --type local --host 0.0.0.0 --port {port}",
            stop_cmd=f"{cli} stop",
            watch_agent_cmd=f"{cli} watch-agent --pid ${{pid}} --host 127.0.0.1 --port {port}",
            anti_call_llm_cmd=f"{cli} anti-call-llm --index ${{index}} --response ${{response_payload}}",
            anti_call_llm_cmd_file=f"{cli} anti-call-llm --index ${{index}} --response-file ${{response_file}}",
            anti_call_llm_cmd_no_response=f"{cli} anti-call-llm --index ${{index}}",
        )
        self._model_service = PreinstalledModelService(sandbox, config)
        sandbox.model_service = self._model_service
        await self._model_service.install()
        await self._model_service.start()

    async def setup(self, sandbox):
        await AbstractTrial.setup(self, sandbox)
        self._inject_runtime_labels(sandbox)
        content = yaml.safe_dump(public_harbor_config(self._config), sort_keys=False)
        path = f"/data/logs/user-defined/rock_job_{self._config.job_name}.yaml"
        await sandbox.write_file_by_path(content, path)

class PublicModelServiceOperator(Operator):
    def __init__(self, port=28080):
        self.port = port

    def apply(self, config):
        return [PublicModelServiceHarborTrial(config, model_service_port=self.port)]
