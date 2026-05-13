"""Unit tests for in-sandbox model-service proxy integration on Job layer.

Covers:
- ProxyConfig mutex validator (recording_file vs replay_file)
- _build_proxy_start_cmd argument assembly (record / replay / default recording path)
- _setup_proxy behaviors (no-op / OPENAI_BASE_URL check / replay upload ordering)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from rock import env_vars
from rock.sdk.envhub import EnvironmentConfig
from rock.sdk.envhub.config import ProxyConfig
from rock.sdk.job.config import BashJobConfig
from rock.sdk.job.trial.bash import BashTrial

# ---------------------------------------------------------------------------
# ProxyConfig validators
# ---------------------------------------------------------------------------


class TestProxyConfigValidators:
    def test_record_replay_mutually_exclusive(self):
        with pytest.raises(ValidationError, match="mutually exclusive"):
            ProxyConfig(enabled=True, recording_file="a.jsonl", replay_file="b.jsonl")

    def test_only_recording_file_ok(self):
        c = ProxyConfig(enabled=True, recording_file="a.jsonl")
        assert c.recording_file == "a.jsonl"
        assert c.replay_file is None

    def test_only_replay_file_ok(self):
        c = ProxyConfig(enabled=True, replay_file="b.jsonl")
        assert c.replay_file == "b.jsonl"
        assert c.recording_file is None

    def test_both_unset_ok(self):
        """Recording mode default: leaving both recording_file and replay_file
        unset is valid (model-service uses its own default path)."""
        c = ProxyConfig(enabled=True)
        assert c.recording_file is None
        assert c.replay_file is None

    def test_defaults(self):
        c = ProxyConfig()
        assert c.enabled is False
        assert c.host == "0.0.0.0"
        assert c.port == 28080
        assert c.model_service_package == "rl-rock[model-service]"


class TestEnvironmentConfigProxyField:
    def test_proxy_field_default_none(self):
        cfg = EnvironmentConfig()
        assert cfg.proxy is None

    def test_proxy_field_accepts_proxy_config(self):
        cfg = EnvironmentConfig(proxy=ProxyConfig(enabled=True))
        assert cfg.proxy is not None
        assert cfg.proxy.enabled is True

    def test_proxy_enabled_without_openai_base_url_does_not_raise(self):
        """EnvironmentConfig stays pure: even when proxy.enabled=True with no
        OPENAI_BASE_URL in env, no error is raised here. The check belongs to
        AbstractTrial._setup_proxy."""
        EnvironmentConfig(proxy=ProxyConfig(enabled=True))


def _make_trial(*, proxy=None, env=None):
    """Create a BashTrial wired with the given proxy and env for testing helpers."""
    environment = EnvironmentConfig(proxy=proxy, env=env or {})
    cfg = BashJobConfig(script="echo", environment=environment)
    return BashTrial(cfg)


class TestBuildProxyStartCmd:
    def test_record_mode_with_explicit_path(self):
        trial = _make_trial(
            proxy=ProxyConfig(enabled=True, recording_file="/data/logs/x.jsonl", port=28080),
            env={"OPENAI_BASE_URL": "https://upstream.example.com/v1"},
        )
        cmd = trial._build_proxy_start_cmd()
        assert "rock model-service start --type proxy" in cmd
        assert "--host 0.0.0.0" in cmd
        assert "--port 28080" in cmd
        assert "--proxy-base-url https://upstream.example.com/v1" in cmd
        assert "--recording-file /data/logs/x.jsonl" in cmd
        assert "--replay-file" not in cmd

    def test_replay_mode_uses_sandbox_replay_path(self):
        trial = _make_trial(
            proxy=ProxyConfig(enabled=True, replay_file="/local/r.jsonl"),
            env={"OPENAI_BASE_URL": "https://upstream.example.com/v1"},
        )
        cmd = trial._build_proxy_start_cmd()
        assert "--replay-file" in cmd
        assert env_vars.ROCK_JOB_PROXY_REPLAY_FILE in cmd
        assert "--recording-file" not in cmd

    def test_record_mode_omits_recording_flag_when_unset(self):
        trial = _make_trial(
            proxy=ProxyConfig(enabled=True),
            env={"OPENAI_BASE_URL": "https://x/v1"},
        )
        cmd = trial._build_proxy_start_cmd()
        assert "--type proxy" in cmd
        assert "--recording-file" not in cmd
        assert "--replay-file" not in cmd

    def test_sandbox_replay_file_constant(self):
        assert env_vars.ROCK_JOB_PROXY_REPLAY_FILE == "/data/logs/user-defined/rock-job-proxy-replay.jsonl"


class TestSetupProxy:
    """Covers the four behavior branches of _setup_proxy:
    - disabled / proxy is None -> no-op
    - enabled but env missing OPENAI_BASE_URL -> ValueError (before install/start)
    - replay mode -> upload_by_path runs before install/start
    - record mode -> upload_by_path is not called
    """

    async def test_noop_when_proxy_is_none(self):
        cfg = BashJobConfig(script="echo")
        trial = BashTrial(cfg)
        sandbox = AsyncMock()
        await trial._setup_proxy(sandbox)
        sandbox.upload_by_path.assert_not_called()

    async def test_noop_when_disabled(self):
        cfg = BashJobConfig(
            script="echo",
            environment=EnvironmentConfig(proxy=ProxyConfig(enabled=False)),
        )
        trial = BashTrial(cfg)
        sandbox = AsyncMock()
        await trial._setup_proxy(sandbox)
        sandbox.upload_by_path.assert_not_called()

    async def test_raises_when_openai_base_url_missing(self, monkeypatch):
        """proxy.enabled=True but env missing OPENAI_BASE_URL -> ValueError,
        and it must happen before ModelService.install/start is called."""
        cfg = BashJobConfig(
            script="echo",
            environment=EnvironmentConfig(proxy=ProxyConfig(enabled=True)),
        )
        trial = BashTrial(cfg)
        sandbox = AsyncMock()

        # Patch ModelService to ensure it is never constructed.
        ms_class = MagicMock()
        monkeypatch.setattr("rock.sdk.job.trial.abstract.ModelService", ms_class)

        with pytest.raises(ValueError, match="OPENAI_BASE_URL"):
            await trial._setup_proxy(sandbox)

        ms_class.assert_not_called()
        sandbox.upload_by_path.assert_not_called()

    async def test_replay_uploads_before_start(self, monkeypatch):
        cfg = BashJobConfig(
            script="echo",
            environment=EnvironmentConfig(
                env={"OPENAI_BASE_URL": "https://upstream/v1"},
                proxy=ProxyConfig(enabled=True, replay_file="/local/r.jsonl"),
            ),
        )
        trial = BashTrial(cfg)

        sandbox = AsyncMock()
        sandbox.upload_by_path.return_value = MagicMock(success=True, message="")
        arun_obs = MagicMock()
        arun_obs.output = "10.0.0.1"
        sandbox.arun = AsyncMock(return_value=arun_obs)

        fake_ms_instance = AsyncMock()
        ms_class = MagicMock(return_value=fake_ms_instance)
        monkeypatch.setattr("rock.sdk.job.trial.abstract.ModelService", ms_class)

        await trial._setup_proxy(sandbox)

        # Upload must happen.
        sandbox.upload_by_path.assert_awaited_once_with(
            file_path="/local/r.jsonl",
            target_path=env_vars.ROCK_JOB_PROXY_REPLAY_FILE,
        )
        # ModelService is installed and started.
        fake_ms_instance.install.assert_awaited_once()
        fake_ms_instance.start.assert_awaited_once()

        # Ordering: upload must precede install.
        all_calls = sandbox.method_calls + fake_ms_instance.method_calls
        upload_idx = next(i for i, c in enumerate(all_calls) if c[0] == "upload_by_path")
        install_idx = next(i for i, c in enumerate(all_calls) if c[0] == "install")
        assert upload_idx < install_idx, (
            f"replay file must be uploaded before ModelService.install "
            f"(upload at idx {upload_idx}, install at idx {install_idx})"
        )

    async def test_replay_upload_failure_raises(self, monkeypatch):
        cfg = BashJobConfig(
            script="echo",
            environment=EnvironmentConfig(
                env={"OPENAI_BASE_URL": "https://upstream/v1"},
                proxy=ProxyConfig(enabled=True, replay_file="/local/r.jsonl"),
            ),
        )
        trial = BashTrial(cfg)
        sandbox = AsyncMock()
        sandbox.upload_by_path.return_value = MagicMock(success=False, message="boom")

        fake_ms_instance = AsyncMock()
        ms_class = MagicMock(return_value=fake_ms_instance)
        monkeypatch.setattr("rock.sdk.job.trial.abstract.ModelService", ms_class)

        with pytest.raises(RuntimeError, match="boom"):
            await trial._setup_proxy(sandbox)
        fake_ms_instance.install.assert_not_called()

    async def test_record_mode_no_upload(self, monkeypatch):
        cfg = BashJobConfig(
            script="echo",
            environment=EnvironmentConfig(
                env={"OPENAI_BASE_URL": "https://upstream/v1"},
                proxy=ProxyConfig(enabled=True, recording_file="/data/logs/x.jsonl"),
            ),
        )
        trial = BashTrial(cfg)

        sandbox = AsyncMock()
        arun_obs = MagicMock()
        arun_obs.output = "10.0.0.1"
        sandbox.arun = AsyncMock(return_value=arun_obs)
        fake_ms_instance = AsyncMock()
        ms_class = MagicMock(return_value=fake_ms_instance)
        monkeypatch.setattr("rock.sdk.job.trial.abstract.ModelService", ms_class)

        await trial._setup_proxy(sandbox)

        sandbox.upload_by_path.assert_not_called()
        fake_ms_instance.install.assert_awaited_once()
        fake_ms_instance.start.assert_awaited_once()
        # env['OPENAI_BASE_URL'] must be rewritten to the proxy URL.
        assert cfg.environment.env["OPENAI_BASE_URL"] == "http://10.0.0.1:28080/v1"


class TestHarborTrialSetupCallsProxy:
    async def test_setup_calls_setup_proxy_first(self, monkeypatch, tmp_path):
        """HarborTrial.setup() must await self._setup_proxy(sandbox) first."""
        from rock.sdk.bench.models.job.config import HarborJobConfig
        from rock.sdk.job.trial.harbor import HarborTrial

        cfg = HarborJobConfig(experiment_id="exp-test")
        trial = HarborTrial(cfg)

        sandbox = AsyncMock()
        sandbox.write_file_by_path = AsyncMock()

        order: list[str] = []

        async def track_setup_proxy(sb):
            order.append("setup_proxy")

        async def track_upload(sb):
            order.append("upload_files")

        monkeypatch.setattr(trial, "_setup_proxy", track_setup_proxy)
        monkeypatch.setattr(trial, "_upload_files", track_upload)

        await trial.setup(sandbox)
        assert order[:2] == [
            "setup_proxy",
            "upload_files",
        ], f"setup_proxy must be called before _upload_files, actual order: {order}"


class TestBashTrialSetupCallsProxy:
    async def test_setup_calls_setup_proxy_first(self, monkeypatch):
        """BashTrial.setup() must await self._setup_proxy(sandbox) first."""
        cfg = BashJobConfig(script="echo hi")
        trial = BashTrial(cfg)

        sandbox = AsyncMock()
        order: list[str] = []

        async def track_setup_proxy(sb):
            order.append("setup_proxy")

        async def track_upload(sb):
            order.append("upload_files")

        monkeypatch.setattr(trial, "_setup_proxy", track_setup_proxy)
        monkeypatch.setattr(trial, "_upload_files", track_upload)

        await trial.setup(sandbox)
        assert order[:2] == [
            "setup_proxy",
            "upload_files",
        ], f"setup_proxy must be called before _upload_files, actual order: {order}"
