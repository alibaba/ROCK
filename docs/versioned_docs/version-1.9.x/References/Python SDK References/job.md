# Use Job to Run Agent

> This is the reference for **Job**, one of ROCK's two parallel ways to use agents. Its core API is `rock.sdk.job.Job` with `JobConfig`, used to run an agent evaluation/task in a sandbox. Two backends are supported: **Bash Job** and **Harbor Bench Job**.
>
> The other way is to install and run an agent inside a single sandbox — see [Install Agent in Sandbox](./rock-agent.md). The two ways use distinct config schemas — **do not mix them**.

`rock.sdk.job` exposes a single `Job` API that supports two modes, distinguished by the config type:

- **Bash Job**: Runs an arbitrary shell script inside a sandbox — useful for data processing, external evaluation tools, etc.
- **Harbor Bench Job**: Runs an AI agent benchmark task via the Harbor framework (SWE-bench, Terminal Bench, etc.).

## End-to-End Example

A minimal runnable Python snippet:

```python
import asyncio
from rock.sdk.job import Job, JobConfig

async def main():
    config = JobConfig.from_yaml("swe_job_config.yaml")  # contains agents: and datasets:
    result = await Job(config).run()

    print(f"status={result.status}, score={result.score}")
    for trial in result.trial_results:
        print(f"  {trial.task_name}: score={trial.score} ({trial.status})")

asyncio.run(main())
```

The full yaml template is in `examples/job/harbor/swe_job_config.yaml.template`.

---

## Bash Job

Bash Job is for running arbitrary shell scripts inside a sandbox — running an external evaluation tool, processing data, etc.

Full example: [`examples/job/bash/claw_eval/`](https://github.com/alibaba/ROCK/tree/master/examples/job/bash/claw_eval)

- `run_claw_eval.py` — Entry point demonstrating `JobConfig.from_yaml()` + `Job(config).run()`
- `claw_eval_bashjob.yaml.template` — YAML template with `script_path`, `environment`, `uploads`, `env`, etc.
- `run_claw_eval.sh` — The script that actually runs in the sandbox (DinD startup, log writing, score output)

### BashJobConfig Fields

| Field | Type | Default | Description |
|------|------|---------|-------------|
| `script` | `str \| None` | `None` | Inline script content (mutually exclusive with `script_path`) |
| `script_path` | `str \| None` | `None` | Local script path; the file is read and uploaded at runtime |
| `job_name` | `str` | current timestamp | Name used for log and artifact paths |
| `environment` | `EnvironmentConfig` | — | Sandbox connection and resource config (see below) |
| `namespace` | `str \| None` | `None` | Namespace |
| `experiment_id` | `str \| None` | `None` | Experiment ID |
| `timeout` | `int` | `7200` | Overall timeout in seconds (2 hours) |

**Common `environment` fields:**

| Field | Type | Description |
|------|------|-------------|
| `image` | `str` | Sandbox Docker image |
| `base_url` | `str` | ROCK platform URL |
| `xrl_authorization` | `str` | Auth token |
| `cluster` | `str` | Target cluster |
| `memory` | `str` | Memory size (e.g. `"64g"`) |
| `cpus` | `int` | Number of CPUs |
| `auto_stop` | `bool` | Whether to stop the sandbox after the job |
| `uploads` | `list` | Local-to-sandbox file/dir uploads, format: `[local_path, sandbox_path]` |
| `env` | `dict[str, str]` | Environment variables injected into the sandbox session |

---

## Harbor Bench Job

Harbor Bench Job runs AI agent benchmark tasks like SWE-bench and Terminal Bench via the Harbor framework.

> **Note**: `rock.sdk.bench.Job` is deprecated and will be removed in a future release. Use `rock.sdk.job.Job` + `HarborJobConfig` instead.

Full example: [`examples/job/harbor/`](https://github.com/alibaba/ROCK/tree/master/examples/job/harbor)

- `harbor_demo.py` — Entry point demonstrating `JobConfig.from_yaml()` + `Job(config).run()` + result iteration
- `swe_job_config.yaml.template` — SWE-bench task config template
- `swe_job_config-verifier.yaml.template` — Variant with `verifier.mode: native`
- `tb_job_config.yaml.template` — Terminal Bench task config template

### HarborJobConfig Core Fields

**Basic fields:**

| Field | Type | Default | Description |
|------|------|---------|-------------|
| `experiment_id` | `str` | required | Experiment ID — required by Harbor |
| `job_name` | `str \| None` | auto-generated | Format: `{dataset}_{task}_{uuid[:8]}` |
| `namespace` | `str \| None` | `None` | Namespace, auto-filled from the sandbox |
| `environment` | `RockEnvironmentConfig` | — | Sandbox connection and resource config |

**Execution control:**

| Field | Type | Default | Description |
|------|------|---------|-------------|
| `n_attempts` | `int` | `1` | Attempts per Trial |
| `timeout` | `int` | `7200` | Overall timeout (auto-derived from agent_timeout) |
| `debug` | `bool` | `False` | Debug mode — keeps more intermediate artifacts |

**Components:**

| Field | Type | Description |
|------|------|-------------|
| `agents` | `list[AgentConfig]` | Harbor's own agent config (typical fields: `name`, `model_name`) — see `examples/job/harbor/swe_job_config.yaml.template` for the canonical shape |
| `datasets` | `list[DatasetConfig]` | Dataset configs |
| `verifier` | `VerifierConfig` | Verifier evaluation config |
| `orchestrator` | `OrchestratorConfig` | Concurrency / scheduling config |

---

## Result Handling

Both Job modes return a `JobResult`:

```python
result = await Job(config).run()

print(f"status={result.status}, score={result.score}")
for trial in result.trial_results:
    print(f"  {trial.task_name}: score={trial.score} ({trial.status})")
    if trial.exception_info:
        print(f"    {trial.exception_info.exception_type}: {trial.exception_info.exception_message}")
```

### JobResult Fields

| Field / Property | Type | Description |
|------------------|------|-------------|
| `status` | `JobStatus` | Overall task status |
| `trial_results` | `list[TrialResult]` | List of all Trial results |
| `score` | `float` (property) | Average `score` across all Trials |
| `n_completed` | `int` (property) | Number of Trials with status `completed` |
| `n_failed` | `int` (property) | Number of Trials with status `failed` |

### TrialResult Fields

| Field / Property | Type | Description |
|------------------|------|-------------|
| `task_name` | `str` | Task name |
| `exit_code` | `int` | Process exit code |
| `raw_output` | `str` | Raw process output |
| `exception_info` | `ExceptionInfo \| None` | Populated if an exception occurred |
| `status` | `str` (property) | `"completed"` or `"failed"` |
| `duration_sec` | `float` (property) | Execution time in seconds |
| `score` | `float` (property) | Score (Bash Job defaults to `0.0`; Harbor mode comes from the verifier) |
