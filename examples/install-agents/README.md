# install-agents

Examples for the **install-agent** way of using ROCK: install and run an agent inside a single sandbox via `sandbox.agent.install()` + `sandbox.agent.run(prompt)`.

To run an agent evaluation/benchmark task via Job, see [`../job/`](../job/) instead.

## Layout

| Subdir | Agent runtime |
|--------|---------------|
| [`claude_code/`](./claude_code/) | Anthropic Claude Code CLI (`@anthropic-ai/claude-code`) |
| [`cursor_cli/`](./cursor_cli/) | Cursor CLI |
| [`iflow_cli/`](./iflow_cli/) | iFlow CLI (`@iflow-ai/iflow-cli`) |
| [`openclaw/`](./openclaw/) | OpenClaw — admin/proxy split-mode demo, has its own README |
| [`qwen_code/`](./qwen_code/) | qwen-code (`@qwen-code/qwen-code`) |
| [`swe_agent/`](./swe_agent/) | SWE-agent (`pip install -e` from GitHub) |

Each subdir contains a `*_demo.py` entry point and a `rock_agent_config.yaml` driving the install/run.

## Run

```bash
# pick any subdir
cd iflow_cli
python iflow_cli_demo.py
```

See the [Install Agent in Sandbox (Experimental)](../../docs/versioned_docs/version-1.7.x/References/Python%20SDK%20References/rock-agent.md) reference for the full RockAgentConfig schema.
