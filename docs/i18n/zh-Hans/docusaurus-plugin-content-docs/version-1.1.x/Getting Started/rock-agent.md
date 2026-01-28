---
sidebar_position: 4
---

# Rock Agent 快速启动

Rock Agent 是 ROCK 提供的 AI Agent 运行框架，支持在沙箱环境中运行各种类型的 Agent。

## 前置条件

- 确保有可用的ROCK服务, 如果需要本地拉起服务端, 参考[快速启动](quickstart.md)

## 使用示例

ROCK 提供了两个Hello World Agent 示例，位于 `examples/agents/` 目录下：

```
examples/agents/
├── claude_code/      # ClaudeCode Agent 示例
└── iflow_cli/        # IFlowCli Agent 示例
```

### 运行 IFlowCli 示例

```bash
cd examples/agents/iflow_cli
python iflow_cli_demo.py
```

### 运行 ClaudeCode 示例

```bash
cd examples/agents/claude_code
python claude_code_demo.py
```

## IFlowCli 配置文件

配置文件位于 `examples/agents/iflow_cli/rock_agent_config.yaml`：

```yaml
run_cmd: "iflow -p ${prompt} --yolo"

runtime_env_config:
  type: node
  npm_registry: "https://registry.npmmirror.com"
  custom_install_cmd: "npm i -g @iflow-ai/iflow-cli@latest"

env:
  IFLOW_API_KEY: "" # 填入你的 API Key
  IFLOW_BASE_URL: "" # 填入你的 Base URL
  IFLOW_MODEL_NAME: "" # 填入你的模型名称
```

## ClaudeCode 配置文件

配置文件位于 `examples/agents/claude_code/rock_agent_config.yaml`：

```yaml
run_cmd: "claude -p ${prompt}"

runtime_env_config:
  type: node
  custom_install_cmd: "npm install -g @anthropic-ai/claude-code"

env:
  ANTHROPIC_BASE_URL: "" # 填入你的anthropic base url
  ANTHROPIC_API_KEY: "" # 填入你的anthropic api key
```

## 相关文档

- [RockAgent 参考](../References/Python%20SDK%20References/rock-agent.md)
