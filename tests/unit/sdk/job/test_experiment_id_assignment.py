"""Regression tests for experiment ID synchronization after JobConfig assignment."""

from unittest.mock import patch

import pytest

from rock.sdk.bench.models.job.config import HarborJobConfig
from rock.sdk.bench.models.trial.config import RockEnvironmentConfig
from rock.sdk.envhub import EnvironmentConfig
from rock.sdk.envhub.config import OssMirrorConfig
from rock.sdk.job.config import BashJobConfig, JobConfig

# ---------------------------------------------------------------------------
# Experiment ID assignment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("config_cls", [JobConfig, BashJobConfig, HarborJobConfig])
class TestExperimentIdAssignment:
    def test_experiment_id_assignment_syncs_environment(self, config_cls):
        cfg = config_cls(experiment_id="exp-before")

        cfg.experiment_id = "exp-after"

        assert cfg.environment.experiment_id == "exp-after"

    @pytest.mark.parametrize("environment_experiment_id", [None, "exp-job", "exp-other"])
    def test_environment_replacement_syncs_experiment_id(self, config_cls, environment_experiment_id):
        cfg = config_cls(experiment_id="exp-job")
        environment = type(cfg.environment)(experiment_id=environment_experiment_id)

        cfg.environment = environment

        assert cfg.environment is environment
        assert cfg.environment.experiment_id == "exp-job"

    def test_unrelated_assignment_preserves_environment(self, config_cls):
        cfg = config_cls(experiment_id="exp-job")
        environment = cfg.environment

        cfg.job_name = "renamed-job"

        assert cfg.job_name == "renamed-job"
        assert cfg.environment is environment
        assert cfg.environment.experiment_id == "exp-job"

    def test_assignment_logs_conflicting_environment_id(self, config_cls):
        cfg = config_cls(experiment_id="exp-before")

        with patch("rock.sdk.job.config.logger.warning") as warning:
            cfg.experiment_id = "exp-after"

        warning.assert_called_once_with(
            "experiment_id conflict: JobConfig has '%s', environment has '%s'. "
            "Using JobConfig.experiment_id and overriding environment.experiment_id.",
            "exp-after",
            "exp-before",
        )


@pytest.mark.parametrize("config_cls", [JobConfig, BashJobConfig])
def test_assigning_initially_unset_experiment_id_syncs_environment(config_cls):
    cfg = config_cls()

    cfg.experiment_id = "exp-assigned"

    assert cfg.environment.experiment_id == "exp-assigned"


@pytest.mark.parametrize("config_cls", [JobConfig, BashJobConfig])
def test_unset_experiment_id_preserves_replacement_environment(config_cls):
    cfg = config_cls()

    cfg.environment = EnvironmentConfig(experiment_id="exp-environment")

    assert cfg.experiment_id is None
    assert cfg.environment.experiment_id == "exp-environment"


@pytest.mark.parametrize("config_cls", [JobConfig, BashJobConfig])
def test_clearing_experiment_id_preserves_environment_id(config_cls):
    cfg = config_cls(experiment_id="exp-environment")

    cfg.experiment_id = None

    assert cfg.experiment_id is None
    assert cfg.environment.experiment_id == "exp-environment"


def test_harbor_experiment_id_assignment_syncs_oss_mirror():
    cfg = HarborJobConfig(
        experiment_id="exp-before",
        environment=RockEnvironmentConfig(oss_mirror=OssMirrorConfig(enabled=True)),
    )

    cfg.experiment_id = "exp-after"

    assert cfg.environment.experiment_id == "exp-after"
    assert cfg.environment.oss_mirror.experiment_id == "exp-after"


def test_harbor_environment_replacement_syncs_oss_mirror():
    cfg = HarborJobConfig(experiment_id="exp-job")

    cfg.environment = RockEnvironmentConfig(oss_mirror=OssMirrorConfig(enabled=True, experiment_id="exp-old"))

    assert cfg.environment.experiment_id == "exp-job"
    assert cfg.environment.oss_mirror.experiment_id == "exp-job"
