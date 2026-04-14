"""Tests for HarborJobConfig._sync_experiment_id model_validator and namespace consistency."""

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from rock.sdk.bench.job import Job
from rock.sdk.bench.models.job.config import HarborJobConfig
from rock.sdk.bench.models.trial.config import RockEnvironmentConfig


class TestExperimentIdNotEmpty:
    def test_none_experiment_id_raises(self):
        """experiment_id=None (default) must raise ValidationError."""
        with pytest.raises(ValidationError, match="experiment_id"):
            HarborJobConfig(job_name="test")

    def test_empty_string_experiment_id_raises(self):
        """experiment_id='' must raise ValidationError."""
        with pytest.raises(ValidationError, match="experiment_id must not be empty"):
            HarborJobConfig(job_name="test", experiment_id="")


class TestExperimentIdConsistency:
    def test_env_none_syncs_from_jobconfig(self):
        """When environment.experiment_id is None, it gets set from HarborJobConfig."""
        cfg = HarborJobConfig(job_name="test", experiment_id="exp-1")
        assert cfg.environment.experiment_id == "exp-1"

    def test_env_matches_jobconfig_passes(self):
        """When both are set and equal, no error."""
        cfg = HarborJobConfig(
            job_name="test",
            experiment_id="exp-1",
            environment=RockEnvironmentConfig(experiment_id="exp-1"),
        )
        assert cfg.experiment_id == "exp-1"
        assert cfg.environment.experiment_id == "exp-1"

    def test_env_mismatch_raises(self):
        """When environment.experiment_id differs from HarborJobConfig.experiment_id, raise."""
        with pytest.raises(ValidationError, match="experiment_id mismatch"):
            HarborJobConfig(
                job_name="test",
                experiment_id="exp-1",
                environment=RockEnvironmentConfig(experiment_id="exp-OTHER"),
            )


class TestAutofillNamespaceConsistency:
    """Tests for namespace consistency check in Job._autofill_sandbox_info."""

    async def test_namespace_autofilled_from_sandbox(self):
        """When user sets no namespace, sandbox value is used."""
        config = HarborJobConfig(job_name="test", experiment_id="exp-1")
        assert config.namespace is None

        job = Job(config)
        sandbox = AsyncMock()
        sandbox._namespace = "sandbox-ns"
        sandbox._experiment_id = "exp-1"
        job._sandbox = sandbox

        await job._autofill_sandbox_info()
        assert config.namespace == "sandbox-ns"

    async def test_namespace_user_matches_sandbox(self):
        """When user namespace matches sandbox, no error."""
        config = HarborJobConfig(job_name="test", experiment_id="exp-1", namespace="same-ns")
        job = Job(config)
        sandbox = AsyncMock()
        sandbox._namespace = "same-ns"
        sandbox._experiment_id = "exp-1"
        job._sandbox = sandbox

        await job._autofill_sandbox_info()
        assert config.namespace == "same-ns"

    async def test_namespace_mismatch_raises(self):
        """When user namespace differs from sandbox, raise ValueError."""
        config = HarborJobConfig(job_name="test", experiment_id="exp-1", namespace="user-ns")
        job = Job(config)
        sandbox = AsyncMock()
        sandbox._namespace = "different-ns"
        sandbox._experiment_id = "exp-1"
        job._sandbox = sandbox

        with pytest.raises(ValueError, match="namespace mismatch"):
            await job._autofill_sandbox_info()

    async def test_namespace_user_set_sandbox_none(self):
        """When user sets namespace but sandbox returns None, keep user value."""
        config = HarborJobConfig(job_name="test", experiment_id="exp-1", namespace="user-ns")
        job = Job(config)
        sandbox = AsyncMock()
        sandbox._namespace = None
        sandbox._experiment_id = "exp-1"
        job._sandbox = sandbox

        await job._autofill_sandbox_info()
        assert config.namespace == "user-ns"
