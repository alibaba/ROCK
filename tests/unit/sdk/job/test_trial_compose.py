"""Tests for rock.sdk.job.compose.trial — ComposeTrial v2 (docker compose up)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from rock.sdk.envhub import EnvironmentConfig
from rock.sdk.envhub.config import OssMirrorConfig
from rock.sdk.job.result import TrialResult
from rock.sdk.job.trial.registry import _create_trial


def _make_config(
    compose_file: str = "./docker-compose.yaml",
    abort_on_container_exit: bool = True,
    job_name: str = "test-job",
    oss_mirror: OssMirrorConfig | None = None,
):
    """Build a minimal v2 ComposeJobConfig."""
    # Import here to allow config-dev to have their impl ready first
    from rock.sdk.job.compose.config import ComposeJobConfig

    env_kwargs = {}
    if oss_mirror is not None:
        env_kwargs["oss_mirror"] = oss_mirror

    return ComposeJobConfig(
        job_name=job_name,
        compose_file=compose_file,
        abort_on_container_exit=abort_on_container_exit,
        environment=EnvironmentConfig(**env_kwargs),
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
    sb.fs.upload_dir = AsyncMock(return_value=MagicMock(exit_code=0))
    sb.write_file_by_path = AsyncMock(return_value=MagicMock(success=True))
    return sb


# ── Registration ──────────────────────────────────────────────────────────────


class TestRegistration:
    def test_compose_config_creates_compose_trial(self):
        from rock.sdk.job.compose.trial import ComposeTrial

        cfg = _make_config()
        trial = _create_trial(cfg)
        assert isinstance(trial, ComposeTrial)


# ── build() ───────────────────────────────────────────────────────────────────


class TestBuild:
    def test_build_returns_bash_runner(self):
        from rock.sdk.job.compose.trial import ComposeTrial

        cfg = _make_config()
        trial = ComposeTrial(cfg)
        assert trial.build() == "bash /rock/runner.sh"


# ── runner.sh rendering ───────────────────────────────────────────────────────


class TestRunnerRendering:
    def _get_runner(self, **kwargs) -> str:
        from rock.sdk.job.compose.trial import ComposeTrial

        cfg = _make_config(**kwargs)
        trial = ComposeTrial(cfg)
        return trial._render_runner_sh()

    def test_runner_contains_docker_compose_up(self):
        runner = self._get_runner()
        assert "docker compose" in runner
        assert "up" in runner

    def test_runner_contains_exit_code_from_main(self):
        runner = self._get_runner()
        assert "--exit-code-from main" in runner

    def test_abort_flag_when_abort_on_container_exit_true(self):
        runner = self._get_runner(abort_on_container_exit=True)
        assert "--abort-on-container-exit" in runner

    def test_no_abort_flag_when_abort_on_container_exit_false(self):
        runner = self._get_runner(abort_on_container_exit=False)
        assert "--abort-on-container-exit" not in runner

    def test_compose_file_path_in_runner(self):
        """Runner must reference the fixed in-sandbox path for compose file."""
        runner = self._get_runner()
        assert "/rock/compose/docker-compose.yaml" in runner

    def test_p0_starts_dockerd_with_kata_fixes(self):
        """P0 must start dockerd with kata-environment fixes:
        explicit PATH (so nohup'd dockerd finds containerd) and
        DOCKER_IGNORE_BR_NETFILTER_ERROR=1 (kata guest lacks br_netfilter)."""
        runner = self._get_runner()
        assert "nohup dockerd" in runner
        assert "DOCKER_IGNORE_BR_NETFILTER_ERROR=1" in runner
        assert "PATH=/usr/local/bin" in runner

    def test_pipestatus_preserved(self):
        """str.replace approach must keep ${PIPESTATUS[0]} intact (not broken by str.format)."""
        runner = self._get_runner()
        assert "${PIPESTATUS[0]}" in runner

    def test_cleanup_trap_runs_compose_logs(self):
        """trap EXIT must capture docker compose logs."""
        runner = self._get_runner()
        assert "docker compose" in runner
        assert "logs" in runner
        assert "compose.log" in runner

    def test_cleanup_trap_runs_compose_down(self):
        """trap EXIT must tear down compose stack."""
        runner = self._get_runner()
        assert "docker compose" in runner
        assert "down" in runner

    def test_no_oss_upload_when_mirror_disabled(self):
        runner = self._get_runner()
        # Should not contain ossutil commands when oss_mirror not enabled
        assert "ossutil" not in runner

    def test_oss_upload_when_mirror_enabled(self):
        mirror = OssMirrorConfig(
            enabled=True,
            oss_bucket="my-bucket",
            oss_endpoint="oss-cn-hangzhou-internal.aliyuncs.com",
            oss_region="cn-hangzhou",
        )
        runner = self._get_runner(oss_mirror=mirror)
        assert "ossutil" in runner

    def test_optional_registry_login_rendered(self):
        """Registry login block must be present (even if conditional at runtime)."""
        runner = self._get_runner()
        # Must contain conditional registry login
        assert "REGISTRY_USERNAME" in runner
        assert "docker login" in runner

    def test_runner_shebang(self):
        runner = self._get_runner()
        assert runner.startswith("#!/bin/bash")


# ── setup() ───────────────────────────────────────────────────────────────────


class TestSetup:
    async def test_setup_writes_runner_sh(self):
        from rock.sdk.job.compose.trial import ComposeTrial

        cfg = _make_config()
        trial = ComposeTrial(cfg)
        sb = _mock_sandbox()

        await trial.setup(sb)

        paths = [call.args[1] for call in sb.write_file_by_path.call_args_list]
        assert "/rock/runner.sh" in paths

    async def test_setup_calls_upload_files(self):
        """setup() must call _upload_files to handle environment.uploads."""
        from rock.sdk.job.compose.trial import ComposeTrial

        cfg = _make_config()
        trial = ComposeTrial(cfg)
        sb = _mock_sandbox()

        # Patch _upload_files to track calls
        upload_called = []

        async def fake_upload(sandbox):
            upload_called.append(True)

        trial._upload_files = fake_upload

        await trial.setup(sb)

        assert len(upload_called) == 1


# ── collect() ────────────────────────────────────────────────────────────────


class TestCollect:
    async def test_collect_success_returns_trial_result(self):
        from rock.sdk.job.compose.trial import ComposeTrial

        cfg = _make_config(job_name="my-job")
        trial = ComposeTrial(cfg)
        sb = _mock_sandbox()

        result = await trial.collect(sb, output="ok\n", exit_code=0)

        assert isinstance(result, TrialResult)
        assert result.exception_info is None
        assert result.task_name == "my-job"
        assert result.status == "completed"
        assert result.exit_code == 0

    async def test_collect_failure_sets_compose_main_service_failed(self):
        from rock.sdk.job.compose.trial import ComposeTrial

        cfg = _make_config(job_name="fail-job")
        trial = ComposeTrial(cfg)
        sb = _mock_sandbox()

        result = await trial.collect(sb, output="", exit_code=2)

        assert result.exception_info is not None
        assert result.exception_info.exception_type == "ComposeMainServiceFailed"
        assert "2" in result.exception_info.exception_message
        assert result.status == "failed"
        assert result.exit_code == 2

    async def test_collect_reads_compose_log(self):
        from rock.sdk.job.compose.trial import ComposeTrial

        cfg = _make_config()
        trial = ComposeTrial(cfg)
        sb = _mock_sandbox()
        obs = MagicMock()
        obs.output = "compose log content"
        sb.arun = AsyncMock(return_value=obs)

        result = await trial.collect(sb, output="", exit_code=0)

        calls = [str(call) for call in sb.arun.call_args_list]
        assert any("compose.log" in c for c in calls)
        assert result is not None

    async def test_collect_exit_code_1_is_failure(self):
        from rock.sdk.job.compose.trial import ComposeTrial

        cfg = _make_config()
        trial = ComposeTrial(cfg)
        sb = _mock_sandbox()

        result = await trial.collect(sb, output="", exit_code=1)

        assert result.exception_info is not None
        assert result.exception_info.exception_type == "ComposeMainServiceFailed"

    async def test_collect_returns_single_trial_result_not_list(self):
        from rock.sdk.job.compose.trial import ComposeTrial

        cfg = _make_config()
        trial = ComposeTrial(cfg)
        sb = _mock_sandbox()

        result = await trial.collect(sb, output="ok", exit_code=0)

        assert isinstance(result, TrialResult)
        assert not isinstance(result, list)


# ── on_sandbox_ready hook ─────────────────────────────────────────────────────


class TestOnSandboxReady:
    async def test_backfills_namespace_and_experiment_id(self):
        from rock.sdk.job.compose.trial import ComposeTrial

        cfg = _make_config()
        trial = ComposeTrial(cfg)
        sb = MagicMock()
        sb._namespace = "ns-123"
        sb._experiment_id = "exp-456"

        await trial.on_sandbox_ready(sb)

        assert cfg.namespace == "ns-123"
        assert cfg.experiment_id == "exp-456"

    async def test_oss_mirror_env_prepared_when_enabled(self, monkeypatch):
        from rock.sdk.job.compose.trial import ComposeTrial

        # Clear any OSS env vars to test pure config path
        for k in list(__import__("os").environ):
            if k.startswith("OSS"):
                monkeypatch.delenv(k, raising=False)

        cfg = _make_config(
            job_name="oss-job",
            oss_mirror=OssMirrorConfig(
                enabled=True,
                oss_bucket="my-bucket",
                oss_endpoint="oss-cn-hangzhou.aliyuncs.com",
                oss_region="cn-hangzhou",
            ),
        )
        trial = ComposeTrial(cfg)
        sb = MagicMock()
        sb._namespace = "ns1"
        sb._experiment_id = "exp1"

        await trial.on_sandbox_ready(sb)

        assert cfg.environment.env.get("OSS_BUCKET") == "my-bucket"
        assert "ROCK_ARTIFACT_DIR" in cfg.environment.env
        assert cfg.environment.env["ROCK_OSS_PREFIX"] == "artifacts/ns1/exp1/oss-job"
