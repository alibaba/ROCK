---
sidebar_position: 4
---

# Rock Agent Quick Start

ROCK provides two parallel ways to use agents, each suited to a different scenario:

- **Job**: Run an agent evaluation/task in a sandbox via BashJob / HarborJob (typical benchmarks: SWE-bench, Terminal Bench) — the primary entry point.
- **install-agent**: Install and run an agent directly inside a single sandbox — for local development and one-off debugging.

Job is covered first. The install-agent section follows at the end, with full reference at [Install Agent in Sandbox (Experimental)](../References/Python%20SDK%20References/rock-agent.md).

## Prerequisites

- Make sure you have a working ROCK service. If you need to start the service locally, refer to [Quick Start](quickstart.md).

---

## 1. Use Job to Run Agent

Job has two backends: **Harbor Job** runs an AI agent benchmark task (SWE-bench, Terminal Bench, etc.); **Bash Job** runs a custom shell script inside a sandbox.

### 1.1 Prepare a yaml

Pick a starting point and copy the matching template:

- Harbor Job (Terminal Bench): [`examples/job/harbor/tb_job_config.yaml.template`](https://github.com/alibaba/ROCK/tree/master/examples/job/harbor/tb_job_config.yaml.template)
- Bash Job (claw-eval): [`examples/job/bash/claw_eval/claw_eval_bashjob.yaml.template`](https://github.com/alibaba/ROCK/tree/master/examples/job/bash/claw_eval/claw_eval_bashjob.yaml.template)

Fill in the fields per the template. See [Use Job to Run Agent](../References/Python%20SDK%20References/job.md) for the full field reference of both backends.

### 1.2 Launch via Python SDK

```python
import asyncio
from rock.sdk.job import Job, JobConfig

async def main():
    config = JobConfig.from_yaml("swe_job_config.yaml")
    result = await Job(config).run()

    print(f"status={result.status}, score={result.score}")
    for trial in result.trial_results:
        print(f"  {trial.task_name}: score={trial.score} ({trial.status})")

asyncio.run(main())
```

For BashJob usage, full field references, and result-handling details, see [Use Job to Run Agent](../References/Python%20SDK%20References/job.md).

---

## 2. install-agent: Install and Run an Agent in a Sandbox

For local development or debugging a single agent run, the core API is:

```python
await sandbox.agent.install(config="rock_agent_config.yaml")
result = await sandbox.agent.run(prompt="hello")
```

The `examples/install-agents/` directory ships ready-to-run examples:

- `examples/install-agents/iflow_cli/` — IFlowCli
- `examples/install-agents/claude_code/` — Claude Code
- `examples/install-agents/cursor_cli/`, `qwen_code/`, `swe_agent/`, `openclaw/` — others

Run the Claude Code example:

```bash
cd examples/install-agents/claude_code
python claude_code_demo.py
```

For full RockAgentConfig field details, placeholder semantics, and API reference, see [Install Agent in Sandbox (Experimental)](../References/Python%20SDK%20References/rock-agent.md).
