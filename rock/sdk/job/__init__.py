# Auto-register BashTrial (safe: no bench dependency).
# HarborTrial is registered by rock.sdk.bench.__init__ to avoid a circular
# import when rock.sdk.job is triggered mid-bench-load.
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
    "AbstractTrial",
    "register_trial",
]
