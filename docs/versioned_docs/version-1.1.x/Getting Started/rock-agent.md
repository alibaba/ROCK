---
sidebar_position: 4
---

# Rock Agent Quick Start

Rock Agent is an AI Agent runtime framework provided by ROCK, supporting various types of Agents running in sandbox environments.

## Prerequisites
- Make sure you have a working ROCK service, if you need to locally start the service side, refer to [Quick Start](quickstart.md).

## Examples

ROCK provides two Hello World Agent examples in the `examples/agents/` directory:

```
examples/agents/
├── claude_code/      # ClaudeCode Agent example
└── iflow_cli/        # IFlowCli Agent example
```

### Run IFlowCli Example

```bash
cd examples/agents/iflow_cli
python iflow_cli_demo.py
```

### Run ClaudeCode Example

```bash
cd examples/agents/claude_code
python claude_code_demo.py
```

## IFlowCli Configuration File

The configuration file is located at `examples/agents/iflow_cli/rock_agent_config.yaml`:

```yaml
run_cmd: "iflow -p ${prompt} --yolo"

runtime_env_config:
  type: node
  npm_registry: "https://registry.npmmirror.com"
  custom_install_cmd: "npm i -g @iflow-ai/iflow-cli@latest"

env:
  IFLOW_API_KEY: "" # Enter your API key
  IFLOW_BASE_URL: "" # Enter your base URL
  IFLOW_MODEL_NAME: "" # Enter your model name
```

## ClaudeCode Configuration File

The configuration file is located at `examples/agents/claude_code/rock_agent_config.yaml`:

```yaml
run_cmd: "claude -p ${prompt}"

runtime_env_config:
  type: node
  custom_install_cmd: "npm install -g @anthropic-ai/claude-code"

env:
  ANTHROPIC_BASE_URL: "" # Enter your anthropic base url
  ANTHROPIC_API_KEY: "" # Enter your anthropic api key
```

## Related Documentation

- [RockAgent Reference](../References/Python%20SDK%20References/rock-agent.md)
