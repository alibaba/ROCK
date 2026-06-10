"""Tests for rock.sdk.job.compose.trial — ComposeTrial."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from rock.sdk.envhub import EnvironmentConfig
from rock.sdk.envhub.config import OssMirrorConfig
from rock.sdk.job.compose.config import (
    ComposeJobConfig,
    ComposeSpec,
    HealthSpec,
    InitContainerSpec,
    MainContainerSpec,
    OssDep,
    ResourceSpec,
    SecretEnvEntry,
    SidecarSpec,
)
from rock.sdk.job.compose.trial import (
    ComposeTrial,
    _entrypoint_args,
    _env_args,
    _render_oss_deps,
    _resource_args,
)
from rock.sdk.job.trial.registry import _create_trial

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _minimal_main(image="ubuntu:22.04") -> MainContainerSpec:
    return MainContainerSpec(image=image)


def _minimal_compose(script="echo hello") -> ComposeJobConfig:
    return ComposeJobConfig(
        script=script,
        compose=ComposeSpec(main=_minimal_main()),
    )


def _mock_sandbox() -> AsyncMock:
    sb = AsyncMock()
    sb._namespace = "test-ns"
    sb._experiment_id = "test-exp"
    obs = MagicMock()
    obs.exit_code = 0
    obs.output = ""
    sb.arun = AsyncMock(return_value=obs)
    sb.fs = AsyncMock()
    sb.fs.ensure_ossutil = AsyncMock(return_value=True)
    sb.fs.upload_dir = AsyncMock(return_value=MagicMock(exit_code=0))
    sb.write_file_by_path = AsyncMock(return_value=MagicMock(success=True))
    return sb


# ── Registration ──────────────────────────────────────────────────────────────


class TestRegistration:
    def test_compose_config_creates_compose_trial(self):
        cfg = _minimal_compose()
        trial = _create_trial(cfg)
        assert isinstance(trial, ComposeTrial)


# ── build() ───────────────────────────────────────────────────────────────────


class TestBuild:
    def test_build_returns_bash_runner(self):
        cfg = _minimal_compose()
        trial = ComposeTrial(cfg)
        assert trial.build() == "bash /rock/runner.sh"


# ── setup() ───────────────────────────────────────────────────────────────────


class TestSetup:
    async def test_setup_writes_runner_sh(self):
        cfg = _minimal_compose()
        trial = ComposeTrial(cfg)
        sb = _mock_sandbox()

        await trial.setup(sb)

        # write_file_by_path should have been called with /rock/runner.sh
        paths = [call.args[1] for call in sb.write_file_by_path.call_args_list]
        assert "/rock/runner.sh" in paths

    async def test_setup_writes_main_script(self):
        cfg = _minimal_compose(script="echo main-script")
        trial = ComposeTrial(cfg)
        sb = _mock_sandbox()

        await trial.setup(sb)

        # main.sh should be written
        paths = [call.args[1] for call in sb.write_file_by_path.call_args_list]
        assert "/rock/scripts/main.sh" in paths

        # Find main.sh content
        for call in sb.write_file_by_path.call_args_list:
            if call.args[1] == "/rock/scripts/main.sh":
                assert "echo main-script" in call.args[0]
                break

    async def test_setup_writes_inline_init_script(self):
        cfg = ComposeJobConfig(
            script="echo main",
            compose=ComposeSpec(
                main=_minimal_main(),
                init_containers=[InitContainerSpec(name="setup", image="alpine", script="echo setup-init")],
            ),
        )
        trial = ComposeTrial(cfg)
        sb = _mock_sandbox()

        await trial.setup(sb)

        paths = [call.args[1] for call in sb.write_file_by_path.call_args_list]
        assert "/rock/scripts/setup.sh" in paths

    async def test_setup_runner_contains_docker_network_create(self):
        cfg = _minimal_compose()
        trial = ComposeTrial(cfg)
        sb = _mock_sandbox()

        await trial.setup(sb)

        runner_content = None
        for call in sb.write_file_by_path.call_args_list:
            if call.args[1] == "/rock/runner.sh":
                runner_content = call.args[0]
                break

        assert runner_content is not None
        assert "docker network create rock_compose_$$" in runner_content
        assert "main.sh" in runner_content

    async def test_setup_runner_contains_pipestatus(self):
        """${PIPESTATUS[0]} must be present verbatim (not broken by str.format)."""
        cfg = _minimal_compose()
        trial = ComposeTrial(cfg)
        sb = _mock_sandbox()

        await trial.setup(sb)

        runner_content = None
        for call in sb.write_file_by_path.call_args_list:
            if call.args[1] == "/rock/runner.sh":
                runner_content = call.args[0]
                break

        assert runner_content is not None
        assert "${PIPESTATUS[0]}" in runner_content

    async def test_setup_ensures_ossutil_when_oss_deps(self):
        cfg = ComposeJobConfig(
            script="echo hi",
            compose=ComposeSpec(
                main=MainContainerSpec(
                    image="ubuntu:22.04",
                    oss_deps=[OssDep(key="oss://bucket/data.tar.gz", target_path="/data")],
                )
            ),
        )
        trial = ComposeTrial(cfg)
        sb = _mock_sandbox()

        await trial.setup(sb)

        sb.fs.ensure_ossutil.assert_called_once()

    async def test_setup_no_ossutil_when_no_oss_deps(self):
        cfg = _minimal_compose()
        trial = ComposeTrial(cfg)
        sb = _mock_sandbox()

        await trial.setup(sb)

        sb.fs.ensure_ossutil.assert_not_called()

    async def test_setup_reads_script_path(self, tmp_path):
        script_file = tmp_path / "main.sh"
        script_file.write_text("echo from-file")

        cfg = ComposeJobConfig(
            script_path=str(script_file),
            compose=ComposeSpec(main=_minimal_main()),
        )
        trial = ComposeTrial(cfg)
        sb = _mock_sandbox()

        await trial.setup(sb)

        # Ensure content from file was written to /rock/scripts/main.sh
        for call in sb.write_file_by_path.call_args_list:
            if call.args[1] == "/rock/scripts/main.sh":
                assert "echo from-file" in call.args[0]
                break


# ── collect() ────────────────────────────────────────────────────────────────


class TestCollect:
    async def test_collect_success(self):
        cfg = ComposeJobConfig(
            script="echo hi",
            job_name="myjob",
            compose=ComposeSpec(main=_minimal_main()),
        )
        trial = ComposeTrial(cfg)
        sb = _mock_sandbox()

        result = await trial.collect(sb, output="ok\n", exit_code=0)

        assert result.exception_info is None
        assert result.task_name == "myjob"
        assert result.status == "completed"
        assert result.exit_code == 0

    async def test_collect_failure_sets_compose_exception(self):
        cfg = ComposeJobConfig(
            script="exit 1",
            job_name="myjob",
            compose=ComposeSpec(main=_minimal_main()),
        )
        trial = ComposeTrial(cfg)
        sb = _mock_sandbox()

        result = await trial.collect(sb, output="", exit_code=1)

        assert result.exception_info is not None
        assert result.exception_info.exception_type == "ComposeMainContainerFailed"
        assert result.status == "failed"
        assert result.exit_code == 1

    async def test_collect_reads_main_log(self):
        cfg = _minimal_compose()
        trial = ComposeTrial(cfg)
        sb = _mock_sandbox()
        obs = MagicMock()
        obs.output = "some log output"
        sb.arun = AsyncMock(return_value=obs)

        result = await trial.collect(sb, output="", exit_code=0)

        # arun should have been called with cat for main log
        calls = [str(call) for call in sb.arun.call_args_list]
        assert any("main.log" in c for c in calls)
        assert result is not None

    async def test_collect_reads_sidecar_log(self):
        cfg = ComposeJobConfig(
            script="echo hi",
            compose=ComposeSpec(
                main=_minimal_main(),
                sidecars=[SidecarSpec(name="proxy", image="nginx:latest")],
            ),
        )
        trial = ComposeTrial(cfg)
        sb = _mock_sandbox()
        obs = MagicMock()
        obs.output = "proxy log"
        sb.arun = AsyncMock(return_value=obs)

        await trial.collect(sb, output="", exit_code=0)

        calls = [str(call) for call in sb.arun.call_args_list]
        assert any("proxy.log" in c for c in calls)


# ── runner.sh rendering ───────────────────────────────────────────────────────


class TestRunnerRendering:
    def _get_runner(self, cfg: ComposeJobConfig) -> str:
        trial = ComposeTrial(cfg)
        return trial._render_runner_sh()

    def test_p0_starts_dockerd_with_kata_fixes(self):
        """P0 must actively start dockerd with the fixes learned from real kata runs:
        explicit PATH (so nohup'd dockerd finds containerd) and
        DOCKER_IGNORE_BR_NETFILTER_ERROR=1 (kata guest lacks br_netfilter)."""
        cfg = ComposeJobConfig(script="echo hi", compose=ComposeSpec(main=_minimal_main()))
        runner = self._get_runner(cfg)
        assert "nohup dockerd" in runner
        assert "DOCKER_IGNORE_BR_NETFILTER_ERROR=1" in runner
        assert "PATH=/usr/local/bin" in runner

    def test_main_mounts_scripts_dir(self):
        """Inner main container must bind-mount the outer /rock/scripts so main.sh exists."""
        cfg = ComposeJobConfig(script="echo hi", compose=ComposeSpec(main=_minimal_main()))
        runner = self._get_runner(cfg)
        assert "-v /rock/scripts:/rock/scripts:ro" in runner
        assert "bash /rock/scripts/main.sh" in runner

    def test_init_container_has_rm_flag(self):
        cfg = ComposeJobConfig(
            script="echo hi",
            compose=ComposeSpec(
                main=_minimal_main(),
                init_containers=[InitContainerSpec(name="init1", image="alpine", script="echo init")],
            ),
        )
        runner = self._get_runner(cfg)
        assert "docker run --rm" in runner
        assert "init1" in runner
        # init containers also need the scripts dir mounted
        assert "-v /rock/scripts:/rock/scripts:ro" in runner

    def test_sidecar_has_d_flag_and_network_alias(self):
        cfg = ComposeJobConfig(
            script="echo hi",
            compose=ComposeSpec(
                main=_minimal_main(),
                sidecars=[SidecarSpec(name="proxy", image="nginx:latest")],
            ),
        )
        runner = self._get_runner(cfg)
        assert "docker run -d" in runner
        assert "--network-alias proxy" in runner

    def test_health_probe_triggers_nc(self):
        cfg = ComposeJobConfig(
            script="echo hi",
            compose=ComposeSpec(
                main=_minimal_main(),
                sidecars=[SidecarSpec(name="proxy", image="nginx:latest", health=HealthSpec(port=8080))],
            ),
        )
        runner = self._get_runner(cfg)
        assert "nc -z" in runner
        assert "proxy" in runner
        assert "8080" in runner

    def test_no_health_probe_when_no_health(self):
        cfg = ComposeJobConfig(
            script="echo hi",
            compose=ComposeSpec(
                main=_minimal_main(),
                sidecars=[SidecarSpec(name="proxy", image="nginx:latest")],
            ),
        )
        runner = self._get_runner(cfg)
        assert "nc -z" not in runner

    def test_privileged_flag(self):
        cfg = ComposeJobConfig(
            script="echo hi",
            compose=ComposeSpec(
                main=MainContainerSpec(image="ubuntu:22.04", privileged=True),
            ),
        )
        runner = self._get_runner(cfg)
        # main container section should have --privileged
        assert "--privileged" in runner

    def test_init_privileged(self):
        cfg = ComposeJobConfig(
            script="echo hi",
            compose=ComposeSpec(
                main=_minimal_main(),
                init_containers=[InitContainerSpec(name="priv-init", image="alpine", privileged=True)],
            ),
        )
        runner = self._get_runner(cfg)
        assert "--privileged" in runner

    def test_command_produces_entrypoint(self):
        cfg = ComposeJobConfig(
            script="echo hi",
            compose=ComposeSpec(
                main=_minimal_main(),
                init_containers=[
                    InitContainerSpec(name="custom", image="alpine", command=["sh"], args=["-c", "echo custom"])
                ],
            ),
        )
        runner = self._get_runner(cfg)
        assert "--entrypoint" in runner

    def test_secret_env_rendered_as_shell_var_reference(self):
        cfg = ComposeJobConfig(
            script="echo hi",
            compose=ComposeSpec(
                main=MainContainerSpec(
                    image="ubuntu:22.04",
                    secret_env={"MY_SECRET": SecretEnvEntry(secret_name="my-secret", secret_key="key")},
                )
            ),
        )
        runner = self._get_runner(cfg)
        # Secret should be a shell variable reference, not the actual secret value
        assert "${MY_SECRET}" in runner
        assert "secret_key" not in runner
        assert "my-secret" not in runner

    def test_oss_deps_extract_branch_generates_tar(self):
        cfg = ComposeJobConfig(
            script="echo hi",
            compose=ComposeSpec(
                main=MainContainerSpec(
                    image="ubuntu:22.04",
                    oss_deps=[OssDep(key="oss://bucket/data.tar.gz", target_path="/data", extract=True)],
                )
            ),
        )
        runner = self._get_runner(cfg)
        assert "tar -xf" in runner
        assert "ossutil cp" in runner

    def test_oss_deps_no_extract_no_tar(self):
        cfg = ComposeJobConfig(
            script="echo hi",
            compose=ComposeSpec(
                main=MainContainerSpec(
                    image="ubuntu:22.04",
                    oss_deps=[OssDep(key="oss://bucket/model.bin", target_path="/model/model.bin")],
                )
            ),
        )
        runner = self._get_runner(cfg)
        assert "ossutil cp" in runner
        assert "tar -xf" not in runner

    def test_pipestatus_preserved(self):
        """str.replace approach must keep ${PIPESTATUS[0]} intact."""
        runner = self._get_runner(_minimal_compose())
        assert "${PIPESTATUS[0]}" in runner

    def test_main_script_path_in_runner(self):
        runner = self._get_runner(_minimal_compose())
        assert "bash /rock/scripts/main.sh" in runner

    def test_resource_spec_cpus_in_runner(self):
        cfg = ComposeJobConfig(
            script="echo hi",
            compose=ComposeSpec(main=MainContainerSpec(image="ubuntu:22.04", resources=ResourceSpec(cpus=4.0))),
        )
        runner = self._get_runner(cfg)
        assert "--cpus" in runner
        assert "4.0" in runner

    def test_resource_spec_memory_in_runner(self):
        cfg = ComposeJobConfig(
            script="echo hi",
            compose=ComposeSpec(
                main=MainContainerSpec(
                    image="ubuntu:22.04",
                    resources=ResourceSpec(memory="8g", memory_limit="16g"),
                )
            ),
        )
        runner = self._get_runner(cfg)
        assert "--memory-reservation" in runner
        assert "--memory" in runner


# ── Helper function unit tests ────────────────────────────────────────────────


class TestResourceArgs:
    def test_none_returns_empty(self):
        assert _resource_args(None) == []

    def test_cpus(self):
        r = ResourceSpec(cpus=2.0)
        args = _resource_args(r)
        assert "--cpus 2.0" in args

    def test_cpu_limit_overrides_cpus(self):
        r = ResourceSpec(cpus=2.0, cpu_limit=4.0)
        args = _resource_args(r)
        assert "--cpus 4.0" in args
        assert "--cpus 2.0" not in args

    def test_memory(self):
        r = ResourceSpec(memory="4g")
        args = _resource_args(r)
        assert "--memory-reservation 4g" in args

    def test_memory_limit(self):
        r = ResourceSpec(memory_limit="8g")
        args = _resource_args(r)
        assert "--memory 8g" in args


class TestEnvArgs:
    def test_plain_env(self):
        args = _env_args({"FOO": "bar"}, {})
        assert "-e FOO=bar" in args

    def test_secret_env_shell_var(self):
        secret = SecretEnvEntry(secret_name="my-secret", secret_key="my-key")
        args = _env_args({}, {"MY_SECRET": secret})
        # Must reference ${MY_SECRET} not the literal key value
        env_arg = next(a for a in args if "MY_SECRET" in a)
        assert "${MY_SECRET}" in env_arg
        assert "my-key" not in env_arg
        assert "my-secret" not in env_arg


class TestEntrypointArgs:
    def test_command_sets_entrypoint(self):
        spec = MagicMock()
        spec.command = ["dockerd"]
        spec.args = ["--tls=false"]
        spec.script_path = None
        spec.script = None
        flag_args, pos = _entrypoint_args(spec)
        assert any("--entrypoint" in f for f in flag_args)
        assert "--tls=false" in pos

    def test_script_path(self):
        spec = MagicMock()
        spec.command = None
        spec.script_path = "/my/script.sh"
        spec.script = None
        flag_args, pos = _entrypoint_args(spec)
        assert flag_args == []
        assert "bash" in pos
        assert "/my/script.sh" in pos

    def test_inline_script_uses_name(self):
        spec = MagicMock()
        spec.command = None
        spec.script_path = None
        spec.script = "echo hi"
        spec.name = "mycontainer"
        flag_args, pos = _entrypoint_args(spec)
        assert flag_args == []
        assert "bash /rock/scripts/mycontainer.sh" in pos

    def test_no_entrypoint_options(self):
        spec = MagicMock()
        spec.command = None
        spec.script_path = None
        spec.script = None
        flag_args, pos = _entrypoint_args(spec)
        assert flag_args == []
        assert pos == ""


class TestRenderOssDeps:
    def test_empty(self):
        result = _render_oss_deps([])
        assert "no oss_deps" in result

    def test_plain_dep(self):
        dep = OssDep(key="oss://b/file.bin", target_path="/data/file.bin")
        result = _render_oss_deps([dep])
        assert "ossutil cp" in result
        assert "tar" not in result

    def test_extract_dep(self):
        dep = OssDep(key="oss://b/data.tar.gz", target_path="/data", extract=True)
        result = _render_oss_deps([dep])
        assert "tar -xf" in result
        assert "ossutil cp" in result


# ── on_sandbox_ready hook ─────────────────────────────────────────────────────


class TestOnSandboxReady:
    async def test_backfills_namespace(self):
        cfg = _minimal_compose()
        trial = ComposeTrial(cfg)
        sb = MagicMock()
        sb._namespace = "test-ns"
        sb._experiment_id = "test-exp"

        await trial.on_sandbox_ready(sb)

        assert cfg.namespace == "test-ns"
        assert cfg.experiment_id == "test-exp"

    async def test_oss_mirror_env_prepared_when_enabled(self, monkeypatch):
        for k in list(__import__("os").environ):
            if k.startswith("OSS"):
                monkeypatch.delenv(k, raising=False)

        cfg = ComposeJobConfig(
            script="echo hi",
            job_name="myjob",
            compose=ComposeSpec(main=_minimal_main()),
            environment=EnvironmentConfig(
                oss_mirror=OssMirrorConfig(
                    enabled=True,
                    oss_bucket="b",
                    oss_endpoint="ep",
                    oss_region="rg",
                ),
            ),
        )
        trial = ComposeTrial(cfg)
        sb = MagicMock()
        sb._namespace = "ns1"
        sb._experiment_id = "exp1"

        await trial.on_sandbox_ready(sb)

        assert cfg.environment.env.get("OSS_BUCKET") == "b"
        assert "ROCK_ARTIFACT_DIR" in cfg.environment.env
        assert cfg.environment.env["ROCK_OSS_PREFIX"] == "artifacts/ns1/exp1/myjob"
