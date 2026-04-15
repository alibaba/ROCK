from pathlib import Path

from rock.sdk.bench.models.environment_type import EnvironmentType
from rock.sdk.bench.models.job.config import (
    HarborJobConfig,
    LocalDatasetConfig,
    OrchestratorConfig,
    RegistryDatasetConfig,
    RemoteRegistryInfo,
    RetryConfig,
    RockEnvironmentConfig,
)
from rock.sdk.bench.models.metric.config import MetricConfig
from rock.sdk.bench.models.metric.type import MetricType
from rock.sdk.bench.models.orchestrator_type import OrchestratorType
from rock.sdk.bench.models.trial.config import (
    AgentConfig,
    ArtifactConfig,
    TaskConfig,
    VerifierConfig,
)
from rock.sdk.bench.models.trial.config import (
    EnvironmentConfig as HarborEnvironmentConfig,
)


class TestOrchestratorType:
    def test_values(self):
        assert OrchestratorType.LOCAL == "local"
        assert OrchestratorType.QUEUE == "queue"

    def test_from_string(self):
        assert OrchestratorType("local") == OrchestratorType.LOCAL
        assert OrchestratorType("queue") == OrchestratorType.QUEUE


class TestEnvironmentType:
    def test_values(self):
        assert EnvironmentType.DOCKER == "docker"
        assert EnvironmentType.ROCK == "rock"
        assert EnvironmentType.E2B == "e2b"

    def test_all_types_exist(self):
        expected = {"docker", "daytona", "e2b", "modal", "runloop", "gke", "rock"}
        actual = {e.value for e in EnvironmentType}
        assert actual == expected


class TestMetricType:
    def test_values(self):
        assert MetricType.MEAN == "mean"
        assert MetricType.SUM == "sum"
        assert MetricType.UV_SCRIPT == "uv-script"

    def test_all_types_exist(self):
        expected = {"sum", "min", "max", "mean", "uv-script"}
        actual = {e.value for e in MetricType}
        assert actual == expected


class TestAgentConfig:
    def test_defaults(self):
        agent = AgentConfig()
        assert agent.name is None
        assert agent.import_path is None
        assert agent.model_name is None
        assert agent.override_timeout_sec is None
        assert agent.override_setup_timeout_sec is None
        assert agent.max_timeout_sec is None
        assert agent.kwargs == {}
        assert agent.env == {}

    def test_with_values(self):
        agent = AgentConfig(
            name="terminus-2",
            model_name="hosted_vllm/my-model",
            kwargs={"max_iterations": 30},
            env={"LLM_API_KEY": "sk-xxx"},
        )
        assert agent.name == "terminus-2"
        assert agent.model_name == "hosted_vllm/my-model"
        assert agent.kwargs["max_iterations"] == 30
        assert agent.env["LLM_API_KEY"] == "sk-xxx"


class TestEnvironmentConfig:
    def test_defaults(self):
        env = HarborEnvironmentConfig()
        assert env.type is None
        assert env.force_build is False
        assert env.delete is True
        assert env.env == {}
        assert env.kwargs == {}

    def test_with_type(self):
        env = HarborEnvironmentConfig(type=EnvironmentType.DOCKER, force_build=True)
        assert env.type == EnvironmentType.DOCKER
        assert env.force_build is True

    def test_with_string_type(self):
        env = HarborEnvironmentConfig(type="docker")
        assert env.type == EnvironmentType.DOCKER


class TestVerifierConfig:
    def test_defaults(self):
        v = VerifierConfig()
        assert v.override_timeout_sec is None
        assert v.max_timeout_sec is None
        assert v.disable is False
        assert v.mode is None

    def test_mode_harbor(self):
        v = VerifierConfig(mode="harbor")
        assert v.mode == "harbor"

    def test_mode_native(self):
        v = VerifierConfig(mode="native")
        assert v.mode == "native"

    def test_mode_invalid_raises_validation_error(self):
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            VerifierConfig(mode="invalid")

    def test_mode_none_explicit(self):
        v = VerifierConfig(mode=None)
        assert v.mode is None


class TestTaskConfig:
    def test_required_path(self):
        task = TaskConfig(path="/workspace/tasks/fix-bug")
        assert task.path == Path("/workspace/tasks/fix-bug")

    def test_optional_fields(self):
        task = TaskConfig(path="/workspace/tasks/t1", git_url="https://github.com/repo", overwrite=True)
        assert task.git_url == "https://github.com/repo"
        assert task.overwrite is True


class TestArtifactConfig:
    def test_fields(self):
        a = ArtifactConfig(source="logs/*.log")
        assert a.source == "logs/*.log"
        assert a.destination is None

    def test_with_destination(self):
        a = ArtifactConfig(source="result.json", destination="/output/result.json")
        assert a.destination == "/output/result.json"


class TestMetricConfig:
    def test_defaults(self):
        m = MetricConfig()
        assert m.type == MetricType.MEAN
        assert m.kwargs == {}

    def test_with_type(self):
        m = MetricConfig(type="sum")
        assert m.type == MetricType.SUM


class TestRetryConfig:
    def test_defaults(self):
        r = RetryConfig()
        assert r.max_retries == 0
        assert r.wait_multiplier == 1.0
        assert r.min_wait_sec == 1.0
        assert r.max_wait_sec == 60.0


class TestOrchestratorConfig:
    def test_defaults(self):
        o = OrchestratorConfig()
        assert o.type == OrchestratorType.LOCAL
        assert o.n_concurrent_trials == 4
        assert o.quiet is False

    def test_with_values(self):
        o = OrchestratorConfig(type="queue", n_concurrent_trials=8, quiet=True)
        assert o.type == OrchestratorType.QUEUE
        assert o.n_concurrent_trials == 8


class TestLocalDatasetConfig:
    def test_minimal(self):
        d = LocalDatasetConfig(path="/data/tasks")
        assert d.path == Path("/data/tasks")
        assert d.task_names is None
        assert d.n_tasks is None

    def test_with_task_names(self):
        d = LocalDatasetConfig(path="/data/tasks", task_names=["task-1", "task-2"])
        assert d.task_names == ["task-1", "task-2"]


class TestRegistryDatasetConfig:
    def test_minimal(self):
        d = RegistryDatasetConfig(
            registry=RemoteRegistryInfo(),
            name="terminal-bench",
        )
        assert d.name == "terminal-bench"
        assert d.version is None

    def test_with_version(self):
        d = RegistryDatasetConfig(
            registry=RemoteRegistryInfo(),
            name="terminal-bench",
            version="2.0",
            n_tasks=50,
        )
        assert d.name == "terminal-bench"
        assert d.version == "2.0"
        assert d.n_tasks == 50


class TestHarborJobConfig:
    def test_defaults(self):
        cfg = HarborJobConfig(experiment_id="test-exp")
        assert cfg.n_attempts == 1
        assert cfg.timeout_multiplier == 1.0
        assert cfg.debug is False
        assert isinstance(cfg.orchestrator, OrchestratorConfig)
        assert isinstance(cfg.environment, RockEnvironmentConfig)
        assert cfg.agents == [AgentConfig()]
        assert cfg.datasets == []
        assert cfg.tasks == []
        assert cfg.metrics == []
        assert cfg.artifacts == []

    def test_environment_defaults(self):
        cfg = HarborJobConfig(experiment_id="test-exp")
        assert cfg.environment.uploads == []
        assert cfg.environment.env == {}
        assert cfg.environment.auto_stop is False

    def test_with_full_config(self):
        cfg = HarborJobConfig(
            job_name="test-job",
            experiment_id="test-exp",
            n_attempts=2,
            agents=[AgentConfig(name="terminus-2", model_name="hosted_vllm/m")],
            datasets=[RegistryDatasetConfig(registry=RemoteRegistryInfo(), name="terminal-bench", version="2.0")],
        )
        assert cfg.job_name == "test-job"
        assert cfg.n_attempts == 2
        assert len(cfg.agents) == 1
        assert cfg.agents[0].name == "terminus-2"


class TestPublicAPI:
    def test_import_from_agent_package(self):
        from rock.sdk.bench import HarborTrialResult, Job, JobResult, JobStatus

        assert Job is not None
        assert JobResult is not None
        assert JobStatus is not None
        assert HarborTrialResult is not None

    def test_import_from_models_package(self):
        from rock.sdk.bench.models import (
            AgentConfig,
            EnvironmentType,
            HarborJobConfig,
        )

        assert HarborJobConfig is not None
        assert AgentConfig is not None
        assert EnvironmentType is not None
