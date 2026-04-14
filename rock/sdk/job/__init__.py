# Pre-import rock.sdk.bench to resolve a known circular-import issue between
# rock.sdk.job.config (base JobConfig) and rock.sdk.bench.models.job.config
# (Harbor JobConfig, which inherits from the base). Doing this import first
# ensures bench is fully loaded before any rock.sdk.job submodule pulls it in.
import rock.sdk.bench  # noqa: F401, I001

from rock.sdk.job.config import BashJobConfig, JobConfig
from rock.sdk.job.executor import JobClient, JobExecutor, TrialClient
from rock.sdk.job.api import Job
from rock.sdk.job.operator import Operator, ScatterOperator
from rock.sdk.job.result import ExceptionInfo, JobResult, JobStatus, TrialResult
from rock.sdk.job.trial import AbstractTrial, register_trial

# Auto-register BashTrial (safe: no bench dependency).
# HarborTrial is registered by rock.sdk.bench.__init__ to avoid a circular
# import when rock.sdk.job is triggered mid-bench-load.
import rock.sdk.job.trial.bash  # noqa: F401

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
