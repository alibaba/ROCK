---
sidebar_position: 4
---

# Rock Agent 快速启动

ROCK 提供两种并列的 agent 使用能力,各自适用不同场景:

- **Job**:通过 BashJob / HarborJob 在 sandbox 里跑一次 agent 评测/任务(典型基准:SWE-bench、Terminal Bench),是入门主要场景。
- **install-agent**:直接在单个沙箱里安装并运行 agent,适合本地开发、单次调试。

下面优先介绍 Job 用法,install-agent 用法见末尾或 [Install Agent in Sandbox (Experimental)](../References/Python%20SDK%20References/rock-agent.md)。

## 前置条件

- 确保有可用的 ROCK 服务,如果需要本地拉起服务端,参考[快速启动](quickstart.md)

---

## 一、用 Job 运行 Agent

Job 有两种 backend:**Harbor Job** 用于运行 AI agent 基准评测任务(SWE-bench、Terminal Bench 等);**Bash Job** 用于在沙箱里跑自定义 shell 脚本。

### 1.1 准备 yaml

挑一类作为起点,直接复制对应模板:

- Harbor Job(Terminal Bench):[`examples/job/harbor/tb_job_config.yaml.template`](https://github.com/alibaba/ROCK/tree/master/examples/job/harbor/tb_job_config.yaml.template)
- Bash Job(claw-eval):[`examples/job/bash/claw_eval/claw_eval_bashjob.yaml.template`](https://github.com/alibaba/ROCK/tree/master/examples/job/bash/claw_eval/claw_eval_bashjob.yaml.template)

按模板填好对应字段即可。两类 Job 的完整字段说明见 [Use Job to Run Agent](../References/Python%20SDK%20References/job.md)。

### 1.2 通过 Python SDK 启动

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

BashJob 的用法、完整字段说明、结果处理详见 [Use Job to Run Agent](../References/Python%20SDK%20References/job.md)。

---

## 二、install-agent:在沙箱里安装并运行 Agent

适合本地开发或单次调试 agent 的场景,核心 API:

```python
await sandbox.agent.install(config="rock_agent_config.yaml")
result = await sandbox.agent.run(prompt="hello")
```

`examples/install-agents/` 下提供了多个开箱即用的示例:

- `examples/install-agents/iflow_cli/` — IFlowCli
- `examples/install-agents/claude_code/` — Claude Code
- `examples/install-agents/cursor_cli/`、`qwen_code/`、`swe_agent/`、`openclaw/` — 其他

运行 Claude Code 示例:

```bash
cd examples/install-agents/claude_code
python claude_code_demo.py
```

完整 RockAgentConfig 字段说明、占位符语义、API 参考详见 [Install Agent in Sandbox (Experimental)](../References/Python%20SDK%20References/rock-agent.md)。
