# Auto-register BashTrial (safe: no bench dependency).
# HarborTrial is registered by rock.sdk.bench.__init__ to avoid circular imports.
import rock.sdk.job.trial.bash  # noqa: F401

from rock.sdk.job.api import Job
from rock.sdk.job.config import BashJobConfig, JobConfig
from rock.sdk.job.executor import JobClient, JobExecutor, TrialClient
from rock.sdk.job.operator import Operator, ScatterOperator
from rock.sdk.job.result import ExceptionInfo, JobResult, JobStatus, TrialResult
from rock.sdk.job.trial import AbstractTrial, register_trial

__all__ = [
    "Job",
    "JobConfig",
    "BashJobConfig",
    "JobResult",
    "JobStatus",
    "TrialResult",
    "ExceptionInfo",
    "JobExecutor",
    "JobClient",
    "TrialClient",
    "Operator",
    "ScatterOperator",
    "SingleTaskPlanner",
    "ResolvedTask",
    "PlannedJob",
    "JobMeta",
    "JobMetaRepository",
    "RunMeta",
    "RunJobRef",
    "RunJobStatus",
    "RunScoreSummary",
    "RunMetaRepository",
    "AbstractTrial",
    "register_trial",
    "JobViewer",
]


def __getattr__(name: str):
    if name == "JobViewer":
        from rock.sdk.job.viewer import JobViewer

        return JobViewer
    if name in {"SingleTaskPlanner", "ResolvedTask", "PlannedJob"}:
        from rock.sdk.job.planner import PlannedJob, ResolvedTask, SingleTaskPlanner

        return {
            "SingleTaskPlanner": SingleTaskPlanner,
            "ResolvedTask": ResolvedTask,
            "PlannedJob": PlannedJob,
        }[name]
    if name in {"JobMeta", "RunMeta", "RunJobRef", "RunJobStatus", "RunScoreSummary"}:
        from rock.sdk.job.meta import JobMeta, RunJobRef, RunJobStatus, RunMeta, RunScoreSummary

        return {
            "JobMeta": JobMeta,
            "RunMeta": RunMeta,
            "RunJobRef": RunJobRef,
            "RunJobStatus": RunJobStatus,
            "RunScoreSummary": RunScoreSummary,
        }[name]
    if name == "JobMetaRepository":
        from rock.sdk.job.job_meta import JobMetaRepository

        return JobMetaRepository
    if name == "RunMetaRepository":
        from rock.sdk.job.run_meta import RunMetaRepository

        return RunMetaRepository
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
