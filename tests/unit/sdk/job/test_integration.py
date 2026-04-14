"""Integration tests: public exports + end-to-end behavior + backward compat."""

from __future__ import annotations


class TestPublicImports:
    def test_import_job(self):
        from rock.sdk.job import Job

        assert Job is not None

    def test_import_configs(self):
        from rock.sdk.job import BashJobConfig, JobConfig

        assert issubclass(BashJobConfig, JobConfig)

    def test_import_results(self):
        from rock.sdk.job import JobStatus, TrialResult

        assert JobStatus.COMPLETED == "completed"
        assert TrialResult is not None

    def test_import_operator(self):
        from rock.sdk.job import Operator, ScatterOperator

        assert issubclass(ScatterOperator, Operator)

    def test_import_trial(self):
        from rock.sdk.job import AbstractTrial, register_trial

        assert AbstractTrial is not None
        assert callable(register_trial)

    def test_import_executor(self):
        from rock.sdk.job import JobClient, JobExecutor, TrialClient

        assert JobExecutor is not None
        assert TrialClient is not None
        assert JobClient is not None


class TestTrialRegistryAutoRegistration:
    def test_bash_registered(self):
        from rock.sdk.job import BashJobConfig
        from rock.sdk.job.trial.registry import _TRIAL_REGISTRY

        assert BashJobConfig in _TRIAL_REGISTRY

    def test_harbor_registered(self):
        # Importing rock.sdk.job triggers auto-registration
        import rock.sdk.job  # noqa: F401
        from rock.sdk.bench.models.job.config import HarborJobConfig
        from rock.sdk.job.trial.registry import _TRIAL_REGISTRY

        assert HarborJobConfig in _TRIAL_REGISTRY


class TestBackwardCompat:
    def test_old_agent_imports_still_work(self):
        """rock.sdk.bench (formerly rock.sdk.agent) must still export Job, HarborJobConfig, JobResult, JobStatus."""
        from rock.sdk.bench import HarborJobConfig, Job, JobResult, JobStatus

        assert Job is not None
        assert HarborJobConfig is not None
        assert JobResult is not None
        assert JobStatus is not None
