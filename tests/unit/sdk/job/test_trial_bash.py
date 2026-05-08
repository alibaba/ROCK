"""Tests for rock.sdk.job.trial.bash — BashTrial."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.sdk.envhub import EnvironmentConfig
from rock.sdk.envhub.config import OssMirrorConfig
from rock.sdk.job.config import BashJobConfig
from rock.sdk.job.trial.bash import BashTrial
from rock.sdk.job.trial.registry import _create_trial


def _success_obs():
    obs = MagicMock()
    obs.exit_code = 0
    return obs


# ---------------------------------------------------------------------------
# BashTrial.build()
# ---------------------------------------------------------------------------


class TestBashTrialBuild:
    def test_build_basic_script(self):
        cfg = BashJobConfig(script="echo hello")
        trial = BashTrial(cfg)
        assert trial.build() == "echo hello"

    def test_build_empty_script(self):
        cfg = BashJobConfig(script=None)
        trial = BashTrial(cfg)
        assert trial.build() == ""

    def test_build_without_oss_mirror_returns_raw(self):
        """No oss_mirror -> user script is returned as-is."""
        cfg = BashJobConfig(script="echo hi")
        trial = BashTrial(cfg)
        assert trial.build() == "echo hi"

    def test_build_oss_disabled_returns_raw(self):
        """oss_mirror.enabled=False -> raw script."""
        cfg = BashJobConfig(
            script="echo hi",
            environment=EnvironmentConfig(oss_mirror=OssMirrorConfig(enabled=False)),
        )
        trial = BashTrial(cfg)
        assert trial.build() == "echo hi"

    def test_build_oss_enabled_and_ready_returns_wrapper(self):
        """oss_mirror.enabled=True and ossutil_ready -> wrapper script."""
        cfg = BashJobConfig(
            script="echo hi",
            environment=EnvironmentConfig(
                oss_mirror=OssMirrorConfig(enabled=True, oss_bucket="b"),
            ),
        )
        trial = BashTrial(cfg)
        trial._ossutil_ready = True  # simulate successful setup

        wrapper = trial.build()

        assert wrapper.startswith("#!/bin/bash")
        assert "echo hi" in wrapper
        assert "ossutil cp" in wrapper
        assert "__ROCK_USER_SCRIPT_EOF_" in wrapper

    def test_build_oss_enabled_but_not_ready_falls_back(self):
        """oss_mirror.enabled=True but ossutil install failed -> raw script."""
        cfg = BashJobConfig(
            script="echo hi",
            environment=EnvironmentConfig(
                oss_mirror=OssMirrorConfig(enabled=True, oss_bucket="b"),
            ),
        )
        trial = BashTrial(cfg)
        trial._ossutil_ready = False  # ensure_ossutil failed

        assert trial.build() == "echo hi"


# ---------------------------------------------------------------------------
# BashTrial.setup()
# ---------------------------------------------------------------------------


class TestBashTrialSetup:
    async def test_setup_uploads_dirs(self, tmp_path):
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        cfg = BashJobConfig(
            script="echo hi",
            environment=EnvironmentConfig(
                uploads=[(str(dir_a), "/sandbox/a"), (str(dir_b), "/sandbox/b")],
            ),
        )
        trial = BashTrial(cfg)
        mock_sandbox = AsyncMock()
        mock_sandbox.fs.upload_dir = AsyncMock(return_value=_success_obs())

        await trial.setup(mock_sandbox)

        assert mock_sandbox.fs.upload_dir.call_count == 2

    async def test_setup_reads_script_path(self):
        expected = "expected content"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
            f.write(expected)
            tmp_path = f.name
        try:
            cfg = BashJobConfig(script_path=tmp_path)
            trial = BashTrial(cfg)
            mock_sandbox = AsyncMock()
            mock_sandbox.fs.upload_dir = AsyncMock(return_value=_success_obs())

            await trial.setup(mock_sandbox)

            assert trial._config.script == expected
        finally:
            Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# BashTrial.collect()
# ---------------------------------------------------------------------------


class TestBashTrialCollect:
    async def test_collect_exit_code_zero(self):
        cfg = BashJobConfig(script="echo hi", job_name="myjob")
        trial = BashTrial(cfg)
        mock_sandbox = AsyncMock()

        result = await trial.collect(mock_sandbox, output="hi\n", exit_code=0)

        assert result.exception_info is None
        assert result.task_name == "myjob"
        assert result.status == "completed"

    async def test_collect_exit_code_nonzero(self):
        cfg = BashJobConfig(script="false", job_name="myjob")
        trial = BashTrial(cfg)
        mock_sandbox = AsyncMock()

        result = await trial.collect(mock_sandbox, output="", exit_code=1)

        assert result.exception_info is not None
        assert result.exception_info.exception_type == "BashExitCode"
        assert result.status == "failed"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestBashTrialRegistration:
    def test_bash_config_creates_bash_trial(self):
        cfg = BashJobConfig(script="echo hi")
        trial = _create_trial(cfg)
        assert isinstance(trial, BashTrial)


# ---------------------------------------------------------------------------
# G4: on_sandbox_ready hook — backfill namespace / experiment_id
# Behavior is inherited from AbstractTrial: BashTrial must also backfill.
# ---------------------------------------------------------------------------


class TestBashTrialOnSandboxReady:
    """G4: BashTrial inherits on_sandbox_ready from AbstractTrial and must backfill namespace/experiment_id."""

    async def test_namespace_backfilled_when_config_unset(self):
        cfg = BashJobConfig(script="echo hi")
        trial = BashTrial(cfg)
        sandbox = MagicMock()
        sandbox._namespace = "sb-ns"
        sandbox._experiment_id = "exp-1"

        await trial.on_sandbox_ready(sandbox)

        assert cfg.namespace == "sb-ns"
        assert cfg.experiment_id == "exp-1"

    async def test_experiment_id_config_takes_priority_over_sandbox(self):
        """Config experiment_id overrides sandbox's different value — no error raised."""
        cfg = BashJobConfig(script="echo hi", experiment_id="claw-eval")
        trial = BashTrial(cfg)
        sandbox = MagicMock()
        sandbox._namespace = None
        sandbox._experiment_id = "default"

        await trial.on_sandbox_ready(sandbox)

        assert cfg.experiment_id == "claw-eval"

    async def test_namespace_mismatch_raises(self):
        cfg = BashJobConfig(script="echo hi", namespace="cfg-ns")
        trial = BashTrial(cfg)
        sandbox = MagicMock()
        sandbox._namespace = "sb-ns"
        sandbox._experiment_id = None

        with pytest.raises(ValueError, match="namespace mismatch"):
            await trial.on_sandbox_ready(sandbox)


# ---------------------------------------------------------------------------
# OSS mirror integration
# ---------------------------------------------------------------------------


def _oss_sandbox(ns="ns", exp="exp"):
    """Minimal sandbox mock with oss_mirror support."""
    sb = AsyncMock()
    sb._namespace = ns
    sb._experiment_id = exp
    sb.arun = AsyncMock(return_value=MagicMock(exit_code=0, output=""))
    sb.fs.ensure_ossutil = AsyncMock(return_value=True)
    sb.fs.upload_dir = AsyncMock(return_value=MagicMock(exit_code=0))
    return sb


_MIRROR = OssMirrorConfig(enabled=True, oss_bucket="b", oss_endpoint="ep", oss_region="rg")


class TestBashTrialOssMirror:
    """OSS mirror integration (spec 2026-04-27) — setup only installs ossutil, collect does not upload."""

    async def test_setup_installs_ossutil_when_enabled(self):
        cfg = BashJobConfig(
            script="echo",
            job_name="j",
            namespace="ns",
            experiment_id="exp",
            environment=EnvironmentConfig(oss_mirror=_MIRROR),
        )
        trial = BashTrial(cfg)
        sb = _oss_sandbox()

        await trial.setup(sb)

        sb.fs.ensure_ossutil.assert_called_once()
        assert trial._ossutil_ready is True
        # setup must not trigger any ossutil cp — uploads happen inside the wrapper
        cp_calls = [c for c in sb.arun.call_args_list if "ossutil cp" in str(c)]
        assert cp_calls == []

    async def test_setup_marks_ossutil_not_ready_on_install_failure(self):
        cfg = BashJobConfig(
            script="echo",
            job_name="j",
            namespace="ns",
            experiment_id="exp",
            environment=EnvironmentConfig(oss_mirror=_MIRROR),
        )
        trial = BashTrial(cfg)
        sb = _oss_sandbox()
        sb.fs.ensure_ossutil = AsyncMock(return_value=False)

        await trial.setup(sb)

        assert trial._ossutil_ready is False

    async def test_setup_skips_when_no_mirror(self):
        trial = BashTrial(BashJobConfig(script="echo"))
        sb = _oss_sandbox()
        await trial.setup(sb)
        sb.fs.ensure_ossutil.assert_not_called()

    async def test_collect_does_not_upload(self):
        """collect no longer calls ossutil cp — uploads are wrapper-driven."""
        cfg = BashJobConfig(
            script="echo",
            job_name="j",
            namespace="ns",
            experiment_id="exp",
            environment=EnvironmentConfig(oss_mirror=_MIRROR),
        )
        trial = BashTrial(cfg)
        sb = _oss_sandbox()

        await trial.setup(sb)
        await trial.collect(sb, "ok", 0)

        cp_calls = [c for c in sb.arun.call_args_list if "ossutil cp" in str(c)]
        assert cp_calls == []


# ---------------------------------------------------------------------------
# Wrapper renderer (spec 2026-04-27)
# ---------------------------------------------------------------------------


class TestRenderWrapper:
    def test_wrapper_contains_prologue_and_epilogue(self):
        wrapper = BashTrial._render_wrapper("echo hi", token="deadbeef")

        # prologue
        assert 'mkdir -p "$ROCK_ARTIFACT_DIR"' in wrapper
        assert 'touch "$ROCK_ARTIFACT_DIR/.placeholder"' in wrapper
        # initial upload (silent on failure)
        assert "|| true" in wrapper
        # heredoc terminator appears twice (open + close)
        assert wrapper.count("__ROCK_USER_SCRIPT_EOF_deadbeef__") == 2
        # single-quoted terminator (disables parameter expansion)
        assert "bash <<'__ROCK_USER_SCRIPT_EOF_deadbeef__'" in wrapper
        # user script body
        assert "echo hi" in wrapper
        # capture user exit code
        assert "_rock_user_rc=$?" in wrapper
        # epilogue: final upload with -f
        assert "ossutil cp" in wrapper
        assert "--recursive -f" in wrapper
        assert "exit $_rock_user_rc" in wrapper

    def test_wrapper_uses_oss_env_variables(self):
        """Wrapper reads paths/bucket from env only; no plaintext credentials."""
        wrapper = BashTrial._render_wrapper("echo hi", token="deadbeef")

        # env-var references present
        assert '"oss://$OSS_BUCKET/$ROCK_OSS_PREFIX/"' in wrapper
        # credential flags must not appear (no command-line leakage)
        assert "--access-key-id" not in wrapper
        assert "--access-key-secret" not in wrapper

    def test_wrapper_empty_user_script(self):
        wrapper = BashTrial._render_wrapper("", token="deadbeef")
        assert "__ROCK_USER_SCRIPT_EOF_deadbeef__" in wrapper
        # prologue/epilogue still present
        assert "ossutil cp" in wrapper

    def test_wrapper_preserves_user_script_verbatim(self):
        """Single-quoted heredoc keeps $VAR, backticks, $() unexpanded."""
        from rock.sdk.job.trial.bash import BashTrial

        user = 'echo "$HOME $(date) `whoami`"'
        wrapper = BashTrial._render_wrapper(user, token="deadbeef")
        assert user in wrapper

    def test_wrapper_auto_generates_token_when_omitted(self):
        """Without an explicit token, secrets.token_hex(4) is used."""
        import re as _re

        wrapper = BashTrial._render_wrapper("echo hi")
        match = _re.search(r"__ROCK_USER_SCRIPT_EOF_([0-9a-f]{8})__", wrapper)
        assert match is not None, "wrapper should contain auto-generated 8-char hex token"


# ---------------------------------------------------------------------------
# on_sandbox_ready / _prepare_oss_session_env (spec 2026-04-27)
# ---------------------------------------------------------------------------


def _ready_sandbox(ns="ns", exp="exp"):
    """Minimal sandbox mock for on_sandbox_ready: only namespace/experiment_id."""
    sb = MagicMock()
    sb._namespace = ns
    sb._experiment_id = exp
    return sb


class TestBashTrialPrepareOssSessionEnv:
    """BashTrial.on_sandbox_ready resolves OSS credentials, validates, and
    writes derived ROCK_* keys into environment.env. JobExecutor._build_session_env
    stays trial-agnostic."""

    def _clear_oss(self, monkeypatch):
        for k in list(__import__("os").environ):
            if k.startswith("OSS"):
                monkeypatch.delenv(k, raising=False)

    async def test_oss_mirror_config_field_wins_over_env_and_host(self, monkeypatch):
        """Priority: OssMirrorConfig field > environment.env > host os.environ."""
        self._clear_oss(monkeypatch)
        monkeypatch.setenv("OSS_ACCESS_KEY_ID", "host_id")

        cfg = BashJobConfig(
            script="echo",
            job_name="j",
            environment=EnvironmentConfig(
                env={"OSS_ACCESS_KEY_ID": "env_id", "OSS_ENDPOINT": "env_ep", "OSS_REGION": "env_rg"},
                oss_mirror=OssMirrorConfig(
                    enabled=True,
                    oss_bucket="cfg_bucket",
                    oss_access_key_id="cfg_id",
                ),
            ),
        )
        trial = BashTrial(cfg)

        await trial.on_sandbox_ready(_ready_sandbox())

        assert cfg.environment.env["OSS_ACCESS_KEY_ID"] == "cfg_id"
        assert cfg.environment.env["OSS_BUCKET"] == "cfg_bucket"
        # environment.env fills slots the config did not supply
        assert cfg.environment.env["OSS_ENDPOINT"] == "env_ep"
        assert cfg.environment.env["OSS_REGION"] == "env_rg"

    async def test_environment_env_can_supply_oss_credentials(self, monkeypatch):
        """Issue-2 fix: OSS_* inside environment.env are usable as credentials."""
        self._clear_oss(monkeypatch)

        cfg = BashJobConfig(
            script="echo",
            job_name="j",
            environment=EnvironmentConfig(
                env={
                    "OSS_ACCESS_KEY_ID": "ak",
                    "OSS_ACCESS_KEY_SECRET": "sk",
                    "OSS_ENDPOINT": "ep",
                    "OSS_REGION": "rg",
                    "OSS_BUCKET": "b",
                },
                oss_mirror=OssMirrorConfig(enabled=True),
            ),
        )
        trial = BashTrial(cfg)

        await trial.on_sandbox_ready(_ready_sandbox())

        assert cfg.environment.env["OSS_ACCESS_KEY_ID"] == "ak"
        assert cfg.environment.env["OSS_BUCKET"] == "b"

    async def test_derived_rock_env_keys_present_when_enabled(self, monkeypatch):
        self._clear_oss(monkeypatch)

        cfg = BashJobConfig(
            script="echo",
            job_name="myjob",
            environment=EnvironmentConfig(
                oss_mirror=OssMirrorConfig(enabled=True, oss_bucket="b", oss_endpoint="ep", oss_region="rg"),
            ),
        )
        trial = BashTrial(cfg)

        await trial.on_sandbox_ready(_ready_sandbox(ns="ns1", exp="exp1"))

        assert cfg.environment.env["ROCK_ARTIFACT_DIR"] == "/data/logs/user-defined"
        assert cfg.environment.env["ROCK_OSS_PREFIX"] == "artifacts/ns1/exp1/myjob"

    async def test_no_action_when_mirror_disabled(self, monkeypatch):
        self._clear_oss(monkeypatch)

        cfg = BashJobConfig(
            script="echo",
            environment=EnvironmentConfig(
                oss_mirror=OssMirrorConfig(enabled=False, oss_bucket="b"),
            ),
        )
        trial = BashTrial(cfg)

        await trial.on_sandbox_ready(_ready_sandbox())

        # No OSS_* / ROCK_* keys injected when mirror disabled
        assert "OSS_BUCKET" not in cfg.environment.env
        assert "ROCK_ARTIFACT_DIR" not in cfg.environment.env
        assert "ROCK_OSS_PREFIX" not in cfg.environment.env

    async def test_no_action_when_no_mirror(self, monkeypatch):
        self._clear_oss(monkeypatch)

        cfg = BashJobConfig(script="echo", environment=EnvironmentConfig())
        trial = BashTrial(cfg)

        await trial.on_sandbox_ready(_ready_sandbox())

        assert "ROCK_ARTIFACT_DIR" not in cfg.environment.env

    async def test_missing_namespace_raises(self, monkeypatch):
        self._clear_oss(monkeypatch)

        cfg = BashJobConfig(
            script="echo",
            job_name="j",
            environment=EnvironmentConfig(
                oss_mirror=OssMirrorConfig(enabled=True, oss_bucket="b", oss_endpoint="ep", oss_region="rg"),
            ),
        )
        trial = BashTrial(cfg)

        with pytest.raises(ValueError, match="namespace"):
            await trial.on_sandbox_ready(_ready_sandbox(ns=None, exp="exp"))

    async def test_missing_experiment_id_raises(self, monkeypatch):
        self._clear_oss(monkeypatch)

        cfg = BashJobConfig(
            script="echo",
            job_name="j",
            environment=EnvironmentConfig(
                oss_mirror=OssMirrorConfig(enabled=True, oss_bucket="b", oss_endpoint="ep", oss_region="rg"),
            ),
        )
        trial = BashTrial(cfg)

        with pytest.raises(ValueError, match="experiment_id"):
            await trial.on_sandbox_ready(_ready_sandbox(ns="ns", exp=None))

    async def test_missing_bucket_raises(self, monkeypatch):
        self._clear_oss(monkeypatch)

        cfg = BashJobConfig(
            script="echo",
            job_name="j",
            environment=EnvironmentConfig(
                oss_mirror=OssMirrorConfig(enabled=True, oss_endpoint="ep", oss_region="rg"),
            ),
        )
        trial = BashTrial(cfg)

        with pytest.raises(ValueError, match="OSS_BUCKET"):
            await trial.on_sandbox_ready(_ready_sandbox())

    async def test_missing_endpoint_raises(self, monkeypatch):
        self._clear_oss(monkeypatch)

        cfg = BashJobConfig(
            script="echo",
            job_name="j",
            environment=EnvironmentConfig(
                oss_mirror=OssMirrorConfig(enabled=True, oss_bucket="b", oss_region="rg"),
            ),
        )
        trial = BashTrial(cfg)

        with pytest.raises(ValueError, match="OSS_ENDPOINT"):
            await trial.on_sandbox_ready(_ready_sandbox())

    async def test_missing_region_raises(self, monkeypatch):
        self._clear_oss(monkeypatch)

        cfg = BashJobConfig(
            script="echo",
            job_name="j",
            environment=EnvironmentConfig(
                oss_mirror=OssMirrorConfig(enabled=True, oss_bucket="b", oss_endpoint="ep"),
            ),
        )
        trial = BashTrial(cfg)

        with pytest.raises(ValueError, match="OSS_REGION"):
            await trial.on_sandbox_ready(_ready_sandbox())
