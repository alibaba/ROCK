---
sidebar_position: 5
---

# Trajectory Distillation with Job

Use the ROCK Job system to collect Agent trajectories, filter high-quality data, and convert it for model distillation training.

## 1. Quick Start

A complete end-to-end example. Prepare the config and script, then run.

### Step 1: Prepare the Config

Create `distill_job_config.yaml`:

```yaml
experiment_id: "distill-exp-001"

environment:
  base_url: "http://your-rock-service:8080"      # ROCK service address
  extra_headers:
    XRL-Authorization: "Bearer <your-token>"     # Auth token
  user_id: "<your-user-id>"
  cluster: "<your-cluster>"
  image: "your-harbor-image:latest"              # Image with Harbor and Agent
  cpus: 8
  memory: "32g"
  startup_timeout: 1800
  env:
    OPENAI_API_KEY: "<your-api-key>"             # Teacher model API key
    OPENAI_API_BASE: "<your-api-base-url>"       # Teacher model API base URL

agents:
  - name: "swe-agent"
    model_name: "openai/<your-teacher-model>"
    max_timeout_sec: 1800
    kwargs:
      per_instance_cost_limit: 0                 # Disable cost check (required for custom models)
      total_cost_limit: 0

datasets:
  - name: "princeton-nlp/SWE-bench_Verified"
    registry:
      split: "test"
      oss_access_key_id: "<your-oss-key>"
      oss_access_key_secret: "<your-oss-secret>"
      oss_bucket: "<your-oss-bucket>"
      oss_dataset_path: "<your-oss-path>"
      oss_region: "cn-shanghai"
      oss_endpoint: "oss-cn-shanghai.aliyuncs.com"
    task_names:
      - "astropy__astropy-7606"                  # Start with one task to verify the flow

orchestrator:
  n_concurrent_trials: 4
```

Replace `<...>` placeholders with actual values.

:::caution
When using non-standard models (not official OpenAI models), you **must set** `per_instance_cost_limit: 0` and `total_cost_limit: 0`. Otherwise swe-agent will exit immediately because litellm cannot calculate the cost, producing no trajectories.
:::

### Step 2: Write the Script

Create `collect_trajectories.py`:

```python
"""End-to-end trajectory collection example"""

import asyncio
import json

from rock.sdk.job import Job, JobConfig
from rock.sdk.job.operator import ScatterOperator


async def main():
    # 1. Load config
    config = JobConfig.from_yaml("distill_job_config.yaml")

    # 2. Create Job — ScatterOperator(size=N) runs N parallel sandboxes
    job = Job(config, operator=ScatterOperator(size=1))

    # 3. Run and wait for results
    result = await job.run()

    print(f"Job completed: exit_code={result.exit_code}, score={result.score}")
    print(f"Total trials: {len(result.trial_results)}\n")

    # 4. Print each trial's result
    for trial in result.trial_results:
        print(f"  {trial.task_name}: score={trial.score}, status={trial.status}")
        if trial.exception_info:
            print(f"    error: {trial.exception_info.exception_message}")


asyncio.run(main())
```

### Step 3: Run

```bash
python collect_trajectories.py
```

On completion, each trial produces:
- `result.json` — automatically parsed into `HarborTrialResult`, containing reward (`trial.score`) and execution status
- `trajectory.json` — full Agent interaction trace in ATIF format, with each step's tool_calls and observations

Once verified, change `ScatterOperator(size=1)` to `ScatterOperator(size=8)` to scale to 8 parallel sandboxes, and remove `task_names` to run the full dataset.

---

## 2. Prerequisites

- A running ROCK service — see [Quick Start](../Getting%20Started/quickstart.md)
- Teacher model API accessible (OpenAI-compatible endpoint)
- A Harbor image built and available
- (Optional) OSS storage for dataset download and artifact persistence

## 3. Configuration Details

### 3.1 Key Configuration Fields

| Field | Purpose | Notes |
|-------|---------|-------|
| `experiment_id` | Experiment identifier | Required for HarborJobConfig |
| `environment.extra_headers` | Authentication | Set `XRL-Authorization: "Bearer <token>"` for service auth |
| `environment.user_id` | User identifier | ROCK platform user ID |
| `environment.cluster` | Cluster identifier | Target cluster for sandbox execution |
| `agents[].model_name` | Teacher model identifier | Format: `provider/model`, e.g., `openai/gpt-4o` |
| `agents[].max_timeout_sec` | Per-task Agent timeout | Influences overall Job wait timeout |
| `agents[].kwargs` | Agent parameter passthrough | Passed through to Harbor Agent (not a ROCK field), see below |
| `orchestrator.n_concurrent_trials` | Harbor internal concurrency | Controls parallel trials within a single sandbox |
| `timeout_multiplier` | Timeout multiplier | Applied to Agent timeout to compute final Job wait timeout |

#### agents[].kwargs Common Parameters

These parameters are passed through to Harbor Agent via `kwargs` — they are not ROCK Job fields:

| Parameter | Purpose | Notes |
|-----------|---------|-------|
| `per_instance_cost_limit` | Per-run cost limit | **Must be set to 0** when using custom models, otherwise litellm cannot calculate cost and the Agent exits |
| `total_cost_limit` | Total cost limit | Same as above, **must be set to 0** |
| `collect_rollout_details` | Collect rollout details | When `true`, `result.json` may include `rollout_details` (whether it's populated depends on the specific Agent implementation) |

### 3.2 Parallelism

```
Total parallelism = ScatterOperator(size) × orchestrator.n_concurrent_trials
```

- `ScatterOperator(size=N)` — creates N independent sandboxes (ROCK-level parallelism)
- `orchestrator.n_concurrent_trials` — concurrent trials within each sandbox (Harbor-level)

For example, `size=4, n_concurrent_trials=4` means 4 sandboxes each running 4 trials = 16-way parallelism.

## 4. Trajectory Data Reference

### 4.1 Output Summary

Each completed trial produces:

| Data | Format | Access Method | Description |
|------|--------|---------------|-------------|
| `result.json` | JSON | Automatically parsed into `HarborTrialResult`, accessed via `JobResult.trial_results` | Contains reward, execution status, exception info |
| `trajectory.json` | ATIF JSON | Stored in sandbox filesystem, requires manual read | **Primary trajectory data source** — full Agent interaction history |

:::info
Whether `agent_result.rollout_details` and `token_ids` in `result.json` contain data **depends on the specific Agent implementation**. For example, swe-agent currently does not populate these fields. The full interaction data lives in `trajectory.json`.
:::

### 4.2 HarborTrialResult Fields

`result.json` is automatically parsed into `HarborTrialResult`, accessible via `JobResult.trial_results`:

```python
trial: HarborTrialResult

# Basic info
trial.task_name          # str   — task name
trial.trial_name         # str   — trial name
trial.status             # str   — "completed" or "failed"

# Reward (for filtering)
trial.score              # float — verifier_result.rewards["reward"]

# Exception info
trial.exception_info     # ExceptionInfo | None
  .exception_type        # str
  .exception_message     # str

# Agent execution results (whether populated depends on Agent implementation)
trial.agent_result       # AgentResult | None
  .n_input_tokens        # int
  .n_output_tokens       # int
  .cost_usd              # float
  .rollout_details       # list[dict] | None
trial.token_ids          # list[int] — concatenation of completion_token_ids from rollout_details
```

### 4.3 trajectory.json — Agent Interaction Trace

`trajectory.json` is an ATIF (Agent Trajectory Interchange Format) file produced by Harbor. It is the **primary data source for trajectory distillation**, containing each Agent interaction step:

```json
{
  "schema_version": "ATIF-v1.5",
  "session_id": "...",
  "agent": {"name": "swe-agent", "version": "1.1.0", "model_name": "..."},
  "steps": [
    {
      "step_id": 1,
      "source": "system",
      "message": "You are a helpful assistant..."
    },
    {
      "step_id": 2,
      "source": "agent",
      "message": "I'll help you fix this issue...",
      "tool_calls": [
        {
          "function_name": "swe_agent_action",
          "arguments": {"raw_action": "find /testbed -type f -name '*.py' | head -20"}
        }
      ],
      "observation": {
        "results": [{"content": "/testbed/astropy/..."}]
      }
    }
  ]
}
```

Each step contains:
- `source` — `"system"` / `"agent"` / `"tool"`
- `message` — Agent's reasoning or system prompt
- `tool_calls` — tools invoked by the Agent with arguments
- `observation` — tool execution results

### 4.4 Sandbox File Layout

```
/data/logs/user-defined/jobs/{job_name}/
├── result.json                              # Top-level summary
├── config.json                              # Run configuration
└── {task_name}__{trial_id}/                 # Per-trial directory
    ├── result.json                          # Trial-level result (parsed as HarborTrialResult)
    ├── config.json
    ├── agent/
    │   ├── trajectory.json                  # ATIF format trajectory (primary data)
    │   └── swe-agent.trajectory.json        # Agent's native format trajectory
    ├── verifier/
    │   └── report.json                      # Verification results
    └── artifacts/
        └── manifest.json
```

## 5. Advanced Usage

### 5.1 Async Mode (RL Training Integration)

Use `submit()` / `wait()` for non-blocking submission in RL training loops:

```python
async def rl_training_loop():
    config = JobConfig.from_yaml("distill_job_config.yaml")
    job = Job(config, operator=ScatterOperator(size=4))

    # Non-blocking submit
    await job.submit()

    # ... perform other work here (e.g., model parameter updates) ...

    # Block until results are ready
    result = await job.wait()

    # Filter by score; trajectory data is in trajectory.json
    for trial in result.trial_results:
        if trial.score > 0:
            # Use trial.task_name and trial.trial_name to locate trajectory.json
            pass
```

### 5.2 Rejection Sampling

Sample the same task multiple times, keep only the best trajectories.

`ScatterOperator(size=8)` creates 8 independent sandboxes, each running the **same full config**. If the config has 1 task, this produces 8 results (8 independent attempts of the same task); if it has 10 tasks, it produces 80 results (8 attempts per task).

```python
from collections import defaultdict

async def rejection_sampling_collect():
    config = JobConfig.from_yaml("distill_job_config.yaml")

    # 8 parallel sandboxes, each running the full config
    # Same task gets 8 independent attempts — pick the best
    result = await Job(config, operator=ScatterOperator(size=8)).run()

    # Group by task, take top-1 per task
    by_task = defaultdict(list)
    for trial in result.trial_results:
        if trial.exception_info is None:
            by_task[trial.task_name].append(trial)

    best_trials = []
    for task_name, trials in by_task.items():
        best = max(trials, key=lambda t: t.score)
        if best.score > 0:
            best_trials.append(best)

    print(f"Rejection sampling: {len(best_trials)} best trajectories from {len(by_task)} tasks")
    return best_trials
```

### 5.3 DPO Preference Pairs

Construct (chosen, rejected) pairs from multiple attempts on the same task:

```python
def to_dpo_pairs(trials):
    by_task = defaultdict(list)
    for trial in trials:
        by_task[trial.task_name].append(trial)

    pairs = []
    for task_name, task_trials in by_task.items():
        sorted_trials = sorted(task_trials, key=lambda t: t.score, reverse=True)
        if len(sorted_trials) < 2:
            continue
        chosen, rejected = sorted_trials[0], sorted_trials[-1]
        if chosen.score > rejected.score:
            pairs.append({
                "task": task_name,
                "chosen_trial": chosen.trial_name,
                "rejected_trial": rejected.trial_name,
                "chosen_score": chosen.score,
                "rejected_score": rejected.score,
            })
    return pairs
```

## Related Documentation

- [Quick Start](../Getting%20Started/quickstart.md) — Deploy ROCK service
- [Configuration Guide](./configuration.md) — Runtime environment configuration
- [API Reference](../References/api.md) — Sandbox API endpoints
- [Python SDK Reference](../References/Python%20SDK%20References/python_sdk.md) — Full SDK reference
