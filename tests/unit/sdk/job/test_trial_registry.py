"""Tests for rock.sdk.job.trial — AbstractTrial and registry."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

# Import bench first to avoid circular-import pitfall in rock.sdk.job.config
import rock.sdk.bench  # noqa: F401
from rock.sdk.envhub import EnvironmentConfig
from rock.sdk.job.config import JobConfig
from rock.sdk.job.result import TrialResult
from rock.sdk.job.trial.abstract import AbstractTrial
from rock.sdk.job.trial.registry import _TRIAL_REGISTRY, _create_trial, register_trial

# ---------------------------------------------------------------------------
# Stubs used across tests
# ---------------------------------------------------------------------------


class _StubConfig(JobConfig):
    stub_field: str = "test"


class _StubTrial(AbstractTrial):
    async def setup(self, sandbox) -> None:
        pass

    def build(self) -> str:
        return "echo stub"

    async def collect(self, sandbox, output, exit_code) -> TrialResult:
        return TrialResult(task_name="stub")


@pytest.fixture(autouse=True)
def _reset_registry():
    """Preserve and restore the registry between tests to avoid cross-test pollution."""
    saved = dict(_TRIAL_REGISTRY)
    _TRIAL_REGISTRY.clear()
    yield
    _TRIAL_REGISTRY.clear()
    _TRIAL_REGISTRY.update(saved)


# ---------------------------------------------------------------------------
# AbstractTrial
# ---------------------------------------------------------------------------


class TestAbstractTrial:
    def test_cannot_instantiate_directly(self):
        """AbstractTrial is abstract and cannot be instantiated."""
        with pytest.raises(TypeError):
            AbstractTrial(JobConfig())  # type: ignore[abstract]

    def test_subclass_can_be_instantiated(self):
        cfg = _StubConfig()
        trial = _StubTrial(cfg)
        assert isinstance(trial, AbstractTrial)

    def test_config_reference_held(self):
        cfg = _StubConfig(stub_field="hello")
        trial = _StubTrial(cfg)
        assert trial._config is cfg

    async def test_upload_dirs_dispatches_to_upload_dir(self, tmp_path):
        """Directory entries should call sandbox.fs.upload_dir()."""
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()

        mock_sandbox = AsyncMock()
        success_obs = MagicMock()
        success_obs.exit_code = 0
        mock_sandbox.fs.upload_dir = AsyncMock(return_value=success_obs)
        cfg = _StubConfig(environment=EnvironmentConfig(uploads=[(str(dir_a), "/b"), (str(dir_b), "/d")]))
        trial = _StubTrial(cfg)

        await trial._upload_files(mock_sandbox)

        assert mock_sandbox.fs.upload_dir.call_count == 2
        mock_sandbox.upload_by_path.assert_not_called()

    async def test_upload_files_dispatches_to_upload_by_path(self, tmp_path):
        """File entries should call sandbox.upload_by_path()."""
        file_a = tmp_path / "a.txt"
        file_a.write_text("content")

        mock_sandbox = AsyncMock()
        upload_resp = MagicMock()
        upload_resp.success = True
        mock_sandbox.upload_by_path = AsyncMock(return_value=upload_resp)
        cfg = _StubConfig(environment=EnvironmentConfig(uploads=[(str(file_a), "/sandbox/a.txt")]))
        trial = _StubTrial(cfg)

        await trial._upload_files(mock_sandbox)

        mock_sandbox.upload_by_path.assert_called_once_with(file_path=str(file_a), target_path="/sandbox/a.txt")
        mock_sandbox.fs.upload_dir.assert_not_called()

    async def test_upload_mixed_files_and_dirs(self, tmp_path):
        """Mixed entries dispatch to the correct method."""
        dir_a = tmp_path / "mydir"
        dir_a.mkdir()
        file_b = tmp_path / "myfile.txt"
        file_b.write_text("data")

        mock_sandbox = AsyncMock()
        success_obs = MagicMock()
        success_obs.exit_code = 0
        mock_sandbox.fs.upload_dir = AsyncMock(return_value=success_obs)
        upload_resp = MagicMock()
        upload_resp.success = True
        mock_sandbox.upload_by_path = AsyncMock(return_value=upload_resp)
        cfg = _StubConfig(
            environment=EnvironmentConfig(uploads=[(str(dir_a), "/sandbox/dir"), (str(file_b), "/sandbox/file.txt")])
        )
        trial = _StubTrial(cfg)

        await trial._upload_files(mock_sandbox)

        mock_sandbox.fs.upload_dir.assert_called_once()
        mock_sandbox.upload_by_path.assert_called_once()

    async def test_upload_files_noop_when_empty(self):
        mock_sandbox = AsyncMock()
        cfg = _StubConfig(uploads=[])
        trial = _StubTrial(cfg)

        await trial._upload_files(mock_sandbox)

        mock_sandbox.fs.upload_dir.assert_not_called()
        mock_sandbox.upload_by_path.assert_not_called()

    async def test_upload_dir_raises_on_failure(self, tmp_path):
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        cfg = _StubConfig(environment=EnvironmentConfig(uploads=[(str(dir_a), "/b")]))
        trial = _StubTrial(cfg)
        mock_sandbox = AsyncMock()
        failure_obs = MagicMock()
        failure_obs.exit_code = 1
        failure_obs.failure_reason = "disk full"
        mock_sandbox.fs.upload_dir = AsyncMock(return_value=failure_obs)

        with pytest.raises(RuntimeError, match="disk full"):
            await trial._upload_files(mock_sandbox)

    async def test_upload_file_raises_on_failure(self, tmp_path):
        file_a = tmp_path / "a.txt"
        file_a.write_text("content")
        cfg = _StubConfig(environment=EnvironmentConfig(uploads=[(str(file_a), "/b")]))
        trial = _StubTrial(cfg)
        mock_sandbox = AsyncMock()
        upload_resp = MagicMock()
        upload_resp.success = False
        upload_resp.message = "upload failed"
        mock_sandbox.upload_by_path = AsyncMock(return_value=upload_resp)

        with pytest.raises(RuntimeError, match="upload failed"):
            await trial._upload_files(mock_sandbox)

    async def test_upload_nonexistent_path_raises(self):
        cfg = _StubConfig(environment=EnvironmentConfig(uploads=[("/nonexistent/path", "/b")]))
        trial = _StubTrial(cfg)
        mock_sandbox = AsyncMock()

        with pytest.raises(RuntimeError, match="not found or unsupported"):
            await trial._upload_files(mock_sandbox)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestTrialRegistry:
    def test_create_trial_unregistered_raises_type_error(self):
        cfg = _StubConfig()
        with pytest.raises(TypeError, match="No trial registered"):
            _create_trial(cfg)

    def test_register_and_create_trial(self):
        register_trial(_StubConfig, _StubTrial)
        cfg = _StubConfig()
        trial = _create_trial(cfg)
        assert isinstance(trial, _StubTrial)
        assert trial._config is cfg

    def test_error_lists_supported_configs(self):
        register_trial(_StubConfig, _StubTrial)

        class _OtherConfig(JobConfig):
            pass

        with pytest.raises(TypeError) as exc_info:
            _create_trial(_OtherConfig())
        assert "_StubConfig" in str(exc_info.value)
