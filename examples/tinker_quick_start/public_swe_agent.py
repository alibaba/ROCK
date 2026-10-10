"""Offline installation check for the unchanged public Harbor SWE-agent adapter.

Install this small module in the outer Harbor controller's site-packages. Its
``run`` and trajectory conversion remain the official Harbor implementation.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import inspect
import json
from pathlib import Path
import shlex
import tarfile
import zipfile
from typing import Annotated

from pydantic import Field

from harbor.agents.installed.swe_agent import SweAgent, SweAgentOptions
from harbor.agents.options import Cli

SWE_AGENT_COMMIT = "3ea751c087f32b16e039a2233dd6eefecef325d5"
SWE_AGENT_SOURCE_SHA256 = "a02724577cde3c7033343efff74184ad33ca783b9f3b81bea90c13f4c47219a4"
ARTIFACT_DIR = "/opt/sweagent-artifacts"
PUBLIC_TOOL_STARTUP_COMMANDS = [
    "if [ -f /opt/miniconda3/etc/profile.d/conda.sh ] && [ -d /opt/miniconda3/envs/testbed ]; then export CONDA_CHANGEPS1=false; source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed; fi",
]
RUNTIME_PAYLOAD = "/opt/public/sweagent-runtime.tar.gz"
RUNTIME_ROOTS = ("opt/python312", "opt/sweagent-venv", "opt/SWE-agent", "opt/sweagent-configs", "opt/sweagent-artifacts", "opt/tiktoken-cache", "opt/sweagent-tools", "usr/local/bin/sweagent")


def validate_runtime_payload(path):
    """Reject payloads that could write outside the isolated agent installation."""
    import posixpath
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        for entry in members:
            name = entry.name.removeprefix("./").rstrip("/")
            if name.startswith("/") or ".." in Path(name).parts:
                raise RuntimeError("Unsafe runtime archive path")
            if not any(name == root or name.startswith(root + "/") for root in RUNTIME_ROOTS):
                raise RuntimeError("Unexpected runtime archive member: " + name)
            if not (entry.isfile() or entry.isdir() or entry.issym() or entry.islnk()):
                raise RuntimeError("Unsupported runtime archive member: " + name)
            if entry.issym() or entry.islnk():
                target = entry.linkname
                resolved = posixpath.normpath(target.lstrip("/") if target.startswith("/") or entry.islnk() else posixpath.join(posixpath.dirname(name), target))
                if not any(resolved == root or resolved.startswith(root + "/") for root in RUNTIME_ROOTS):
                    raise RuntimeError("Runtime symlink escapes its installation: " + name)
        if not members:
            raise RuntimeError("Empty runtime archive")


def verify_installation(wheel_path, manifest_path, venv_dir, config_path):
    """Bind this build to the pinned source, then compare every installed Python file."""
    wheel = Path(wheel_path)
    manifest = json.loads(Path(manifest_path).read_text())
    if manifest.get("revision") != SWE_AGENT_COMMIT:
        raise RuntimeError("Preinstalled SWE-agent source revision does not match the public pin")
    wheel_info = manifest.get("wheel") or {}
    expected_wheel = wheel_info.get("sha256")
    if not isinstance(expected_wheel, str) or len(expected_wheel) != 64:
        raise RuntimeError("Preinstalled SWE-agent manifest must record this build's wheel checksum")
    actual_wheel = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if actual_wheel != expected_wheel:
        raise RuntimeError("Preinstalled SWE-agent wheel checksum mismatch")
    source_archive = Path(manifest_path).parent / "swe-agent-source.tar.gz"
    if manifest.get("sha256") != SWE_AGENT_SOURCE_SHA256 or hashlib.sha256(source_archive.read_bytes()).hexdigest() != SWE_AGENT_SOURCE_SHA256:
        raise RuntimeError("Preinstalled SWE-agent public source archive checksum mismatch")
    with tarfile.open(source_archive, "r:gz") as source:
        source_files = {
            item.name.split("/", 1)[1]: source.extractfile(item).read()
            for item in source.getmembers()
            if item.isfile() and "/" in item.name
            and item.name.split("/", 1)[1].startswith("sweagent/") and item.name.endswith(".py")
        }
    with zipfile.ZipFile(wheel) as archive:
        wheel_files = {name: archive.read(name) for name in archive.namelist()
                       if name.startswith("sweagent/") and name.endswith(".py")}
    # The pinned setuptools package omits this non-package experimental module.
    source_files.pop("sweagent/agent/extra/shell_agent.py", None)
    if not source_files or wheel_files != source_files:
        raise RuntimeError("Preinstalled SWE-agent wheel Python source differs from pinned public archive")
    distribution = importlib.metadata.distribution("sweagent")
    if distribution.version != "1.1.0":
        raise RuntimeError("Unexpected public SWE-agent distribution version")
    prefix = Path(venv_dir).resolve()
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.startswith("sweagent/") and name.endswith(".py"):
                installed = Path(distribution.locate_file(name)).resolve()
                if not installed.is_relative_to(prefix) or installed.read_bytes() != archive.read(name):
                    raise RuntimeError(f"Installed SWE-agent source differs from public wheel: {name}")
    for forbidden in ("/opt/swe/task.jsonl", "/opt/swe/bridge.py", "/tests/test.sh"):
        if Path(forbidden).exists():
            raise RuntimeError(f"Grading/diagnostic payload present before agent execution: {forbidden}")
    import yaml
    from sweagent.run.run_single import RunSingleConfig
    config = yaml.safe_load(Path(config_path).read_text())
    validated = RunSingleConfig.model_validate(config)
    if validated.env.post_startup_commands[-len(PUBLIC_TOOL_STARTUP_COMMANDS):] != PUBLIC_TOOL_STARTUP_COMMANDS:
        raise RuntimeError("Public tool shell must preserve the task runtime without a fixed Python version")
    return {"revision": SWE_AGENT_COMMIT, "wheel_sha256": actual_wheel,
            "installed_source": "matches pinned public wheel", "offline": True}


def build_public_config(default_path, output_path, *, max_turns=8, max_tokens=512):
    """Copy all official templates/tools; change only public model options.

    Public SWE-agent checks call limits after a response using ``calls > limit``.
    ``max_turns - 1`` permits at most max_turns successful sampled responses; the
    last response may trigger the official autosubmit without executing its tool.
    """
    import yaml
    if type(max_turns) is not int or max_turns < 2 or type(max_tokens) is not int or not 1 <= max_tokens < 8192:
        raise ValueError("Public call-limit configuration requires max_turns >= 2")
    config = yaml.safe_load(Path(default_path).read_text())
    model = config["agent"].setdefault("model", {})
    model.update(name="openai/local-qwen", per_instance_cost_limit=0,
                 total_cost_limit=0, per_instance_call_limit=max_turns - 1,
                 temperature=1.0, top_p=1.0, max_input_tokens=min(7600, 8192 - max_tokens),
                 max_output_tokens=max_tokens)
    model.setdefault("completion_kwargs", {}).update(max_tokens=max_tokens)
    for bundle in config.get("agent", {}).get("tools", {}).get("bundles", []):
        name = Path(bundle["path"]).name
        if name in ("registry", "edit_anthropic", "review_on_submit_m"):
            bundle["path"] = "/opt/sweagent-tools/" + name
    environment = config.setdefault("env", {})
    startup = environment.get("post_startup_commands", [])
    environment["post_startup_commands"] = (
        [command for command in startup if command not in PUBLIC_TOOL_STARTUP_COMMANDS]
        + list(PUBLIC_TOOL_STARTUP_COMMANDS)
    )
    Path(output_path).write_text(yaml.safe_dump(config, sort_keys=False))
    return config


class PublicSweAgentOptions(SweAgentOptions):
    max_observation_length: Annotated[
        str | None, Cli("--agent.templates.max_observation_length")
    ] = Field(default=None, description="Maximum observation characters in official agent history.")
    next_step_truncated_observation_template: Annotated[
        str | None, Cli("--agent.templates.next_step_truncated_observation_template")
    ] = Field(default=None, description="Official Jinja template for clipped observations.")
    history_processors: Annotated[
        str | None, Cli("--agent.history_processors")
    ] = Field(default=None, description="JSON list of official history processor configurations.")
    per_instance_call_limit: Annotated[
        str | None, Cli("--agent.model.per_instance_call_limit")
    ] = Field(default=None, description="Successful completion limit checked after sampling.")
    max_output_tokens: Annotated[
        str | None, Cli("--agent.model.max_output_tokens")
    ] = Field(default=None, description="Maximum tokens in each public model response.")


class PreinstalledSweAgent(SweAgent):
    options_model = PublicSweAgentOptions
    options: PublicSweAgentOptions

    async def install(self, environment):
        if self._version != SWE_AGENT_COMMIT:
            raise RuntimeError("Offline SWE-agent requires the fixed public source commit")
        config_path = self._get_env("SWEAGENT_CONFIG") or "/opt/sweagent-configs/public.yaml"
        if not config_path.startswith("/opt/sweagent-configs/") or ".." in Path(config_path).parts:
            raise RuntimeError("Offline public scaffold must be preinstalled under /opt/sweagent-configs")
        validate_runtime_payload(RUNTIME_PAYLOAD)
        await environment.upload_file(RUNTIME_PAYLOAD, "/tmp/sweagent-runtime.tar.gz")
        unpacked = await self.exec_as_root(environment, command=(
            "tar --keep-old-files -xzf /tmp/sweagent-runtime.tar.gz -C / && "
            "rm /tmp/sweagent-runtime.tar.gz"
        ))
        if unpacked.return_code != 0:
            raise RuntimeError(f"Offline SWE-agent runtime extraction failed: {unpacked.stderr}")
        # The inner task container need not import this outer-controller module.
        source = ("import hashlib, importlib.metadata, json, zipfile, tarfile\nfrom pathlib import Path\n"
                  f"SWE_AGENT_COMMIT={SWE_AGENT_COMMIT!r}\n"
                  f"SWE_AGENT_SOURCE_SHA256={SWE_AGENT_SOURCE_SHA256!r}\n"
                  f"PUBLIC_TOOL_STARTUP_COMMANDS={PUBLIC_TOOL_STARTUP_COMMANDS!r}\n"
                  + inspect.getsource(verify_installation) + "\n"
                  + "print(json.dumps(verify_installation("
                  + repr(f"{ARTIFACT_DIR}/sweagent-1.1.0-py3-none-any.whl") + ","
                  + repr(f"{ARTIFACT_DIR}/swe-agent-source.json") + ","
                  + repr("/opt/sweagent-venv") + "," + repr(config_path) + ")))\n")
        result = await self.exec_as_root(environment, command=(
            "test -x /usr/local/bin/sweagent && "
            "env PYTHONNOUSERSITE=1 LITELLM_LOCAL_MODEL_COST_MAP=True LITELLM_TELEMETRY=False "
            "/opt/sweagent-venv/bin/python -c " + shlex.quote(source)
        ))
        if result.return_code != 0:
            raise RuntimeError(f"Preinstalled public SWE-agent verification failed: {result.stderr}")
