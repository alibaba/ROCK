# Rock Agent（实验性）

RockAgent 是 ROCK 框架中的核心 Agent 实现，直接继承自 `Agent` 抽象基类。它提供了完整的 Agent 生命周期管理，包括环境初始化、ModelService 集成、命令执行等功能。

使用 `sandbox.agent.install()` 以及 `sandbox.agent.run(prompt)` 就可以在 Rock 提供的 Sandbox 环境中安装和运行 Agent。

## 核心概念

RockAgent 的核心工作流程分为两个阶段：

1. **install(config)**: 初始化 Agent 环境，包括部署工作目录、设置环境变量、初始化运行时环境等
2. **run(prompt)**: 执行 Agent 任务，替换占位符并启动 Agent 进程

## 快速开始

### Claude Code 示例

```yaml
run_cmd: "claude -p ${prompt}"

runtime_env_config:
  type: node
  custom_install_cmd: "npm install -g @anthropic-ai/claude-code"

env:
  ANTHROPIC_BASE_URL: ""
  ANTHROPIC_API_KEY: ""
```

### IFlowCli 示例

```yaml
run_cmd: "iflow -p ${prompt} --yolo"           # ${prompt} 必须

runtime_env_config:
  type: node                                    
  custom_install_cmd: "npm i -g @iflow-ai/iflow-cli@latest"

env:                                            # 环境变量
  IFLOW_API_KEY: "xxxxxxx"
  IFLOW_BASE_URL: "xxxxxxx"
  IFLOW_MODEL_NAME: "xxxxxxx"
```

### LangGraph Agent 示例

```yaml
working_dir: "."                                # 上传包含 langgraph_agent.py 的本地当前目录到 sandbox

run_cmd: "python langgraph_agent.py ${prompt}"  # 运行本地脚本

runtime_env_config:
  type: python
  pip:                                          # 安装 pip 依赖
    - langchain==1.2.3
    - langchain-openai==1.1.7
    - langgraph==1.0.6

env:
  OPENAI_API_KEY: xxxxxxx
```

## 配置详解

### 基础配置

```yaml
agent_type: "default"                           # Agent 类型标识（默认: "default"）
agent_name: "demo-agent"                        # Agent 实例名称（默认: 随机 uuid）
version: "1.0.0"                                # 版本标识（默认: "default"）
instance_id: "instance-001"                     # 实例 ID（默认: "instance-id-<随机uuid>"）
agent_installed_dir: "/tmp/installed_agent"     # Agent 安装目录（默认: "/tmp/installed_agent"）
agent_session: "my-session"                     # bash 会话标识（默认: "agent-session-<随机uuid>"）
env:                                            # 环境变量（默认: {}）
  OPENAI_API_KEY: "xxxxxxx"
```

### 工作目录配置

```yaml
working_dir: "./my_project"                     # 本地目录，上传到 sandbox（默认: None 不上传）
project_path: "/testbed"                        # sandbox 中工作目录，用于 cd（默认: None）
use_deploy_working_dir_as_fallback: true        # project_path 为空时是否回退到 deploy.working_dir（默认: true）
```

### 执行配置

```yaml
run_cmd: "python main.py --prompt ${prompt}"    # Agent 执行命令，必须包含 ${prompt}（默认: None）

# 超时配置
agent_install_timeout: 600                      # 安装超时，单位秒（默认: 600）
agent_run_timeout: 1800                         # 运行超时，单位秒（默认: 1800）
agent_run_check_interval: 30                    # 检查间隔，单位秒（默认: 30）
```

### 初始化钩子

```yaml
pre_init_cmds:                                  # 初始化前执行的命令（默认: 从 env_vars 读取）
  - command: "apt update && apt install -y git"
    timeout_seconds: 300                        # 命令超时，单位秒（默认: 300）
  - command: "cp ${working_dir}/config.json /root/.config/config.json"
    timeout_seconds: 60

post_init_cmds:                                 # 初始化后执行的命令（默认: []）
  - command: "echo 'Installation complete'"
    timeout_seconds: 30
```

**注意事项**：
- `pre_init_cmds` 和 `post_init_cmds` 不继承 Agent 的 `env` 环境变量
- 通常用于执行安装操作和配置文件移动操作
- 常用命令示例：
  - `apt update && apt install -y git wget tar`
  - `cp ${working_dir}/config.json /root/.config/config.json`

### RuntimeEnv 配置

```yaml
runtime_env_config:                             # 具体参考 RuntimeEnv 有关文档
  type: "python"                                # 运行时类型: python / node（默认: "python"）
  version: "3.11"                               # 版本号
  pip:                                          # Python 依赖包列表
    - package1==1.0.0
    - package2==2.0.0
  custom_install_cmd: "git clone https://github.com/SWE-agent/SWE-agent.git && cd SWE-agent && pip install -e ."
```

**Node 运行时示例**：

```yaml
runtime_env_config:
  type: "node"
  version: "22.18.0"
  npm_registry: "https://registry.npmmirror.com"
  custom_install_cmd: "npm i -g some-package"
```

**自动执行的操作**：
- 根据 `type` 安装对应的运行时（Python 或 Node.js）
- 安装 `pip` 依赖（如果配置了）
- 执行 `custom_install_cmd` 自定义安装命令（如果配置了）
- 支持 `npm_registry` 配置 Node.js 的 npm 镜像源

### ModelService 配置

```yaml
model_service_config:                           # 具体参考 ModelService 有关文档
  enabled: true                                 # 启用 ModelService（默认: false）
```

**自动执行的操作**：
- 安装阶段：安装 ModelService（仅安装，不启动）
- 运行阶段：启动 ModelService + `watch_agent` 监控进程

**注意事项**：需要将模型请求的 URL 设置为 ModelService 的 URL。例如 ModelService 提供的 OpenAI-compatible 的 URL 为 `http://127.0.0.1:8080/v1/chat/completions`，则通常需要将 Agent 向 LLM 请求的 URL 设置为 `http://127.0.0.1:8080/v1/`。

## API 参考

### install(config)

初始化 Agent 环境。

**执行流程**：
1. 如果配置了 `working_dir`，部署到 sandbox
2. 设置 bash session，以及配置 env 环境变量
3. 执行 `pre_init_cmds`
4. 并行初始化 RuntimeEnv 和 ModelService（如果启用）
5. 执行 `post_init_cmds`

**参数**：
- `config`: Agent 配置文件，支持两种传入方式：
  - **字符串路径**: YAML 配置文件路径，默认值为 `"rock_agent_config.yaml"`
  - **RockAgentConfig 对象**: 直接传入 `RockAgentConfig` 实例

### run(prompt)

执行 Agent 任务。

**执行流程**：
1. 替换占位符, 准备Agent 运行命令
4. 启动 agent 进程
5. 如果启用 ModelService，启动 `watch_agent`
6. 等待任务完成并返回结果

## 高级用法

### working_dir 与 project_path 的区别与联动

| 配置项 | 作用 | 联动方式 |
|--------|------|----------|
| `working_dir` | 本地目录，上传到 sandbox | 调用 `deploy.deploy_working_dir()` 上传，上传后 `deploy.working_dir` 变为 sandbox 中的路径 |
| `${working_dir}` | 命令中的占位符 | 被 `deploy.format()` 替换为 `deploy.working_dir` 的值，会在配置中的 init_cmds 和 run_cmd 中替换 |
| `project_path` | sandbox 中的工作目录 | 用于运行前 `cd project_path`，不设置时会进入到 `deploy.working_dir` 工作目录 |
| `use_deploy_working_dir_as_fallback` | run 时 project_path 未设置时是否回退到 deploy.working_dir | 默认为 `true`，设为 `false` 时即使未设置 project_path 也不会进入 working_dir |

**使用建议**：
- 使用 `working_dir` 上传本地项目代码到 sandbox
- 使用 `project_path` 指定 sandbox 中的工作目录（如 `/testbed`）
- 设置 `use_deploy_working_dir_as_fallback: false` 的场景：需要进行本地文件挂载，但希望在镜像默认工作目录下运行 Agent

### 占位符使用

Rock Agent 在支持在配置文件中替换以下占位符：

- `${prompt}`: 在run_cmd 中必需，会被替换为 `run(prompt)` 传入的提示词
- `${working_dir}`: 可选，会被替换为 sandbox 中实际的工作目录路径, 同时支持在 init_cmds和 run_cmd 中使用

**示例**：
```yaml
run_cmd: "python ${working_dir}/main.py --prompt ${prompt}"
```

### use_deploy_working_dir_as_fallback 说明

当 `project_path` 未设置时：
- `true`（默认）：运行 Agent 前会自动 `cd` 到 `deploy.working_dir`
- `false`：运行 Agent 前不会自动切换目录，保持在当前目录

适用场景：
- `true`: 大多数场景，希望 Agent 在上传的代码目录中运行
- `false`: 需要挂载本地文件，但希望在镜像默认工作目录（如 `/app, /testbed`）下运行 Agent

## 完整配置示例

```yaml
# ========== 基础配置 ==========
agent_type: "default"
agent_name: "demo-agent"
version: "1.0.0"
instance_id: "instance-001"
agent_installed_dir: "/tmp/installed_agent"
agent_session: "my-session"
env:
  OPENAI_API_KEY: "xxxxxxx"

# ========== 工作目录配置 ==========
working_dir: "./my_project"
project_path: "/testbed"
use_deploy_working_dir_as_fallback: true

# ========== 运行配置 ==========
run_cmd: "python ${working_dir}/main.py --prompt ${prompt}"

# 超时配置
agent_install_timeout: 600
agent_run_timeout: 1800
agent_run_check_interval: 30

# ========== 初始化命令 ==========
pre_init_cmds:
  - command: "apt update && apt install -y git"
    timeout_seconds: 300
  - command: "cp ${working_dir}/config.json /root/.config/config.json"
    timeout_seconds: 60

post_init_cmds:
  - command: "echo 'Installation complete'"
    timeout_seconds: 30

# ========== 运行时环境配置 ==========
runtime_env_config:
  type: "python"
  version: "3.11"
  pip:
    - langchain==1.2.3
    - langchain-openai==1.1.7

# ========== ModelService 集成 ==========
model_service_config:
  enabled: true
```

## 使用示例

### 使用 YAML 配置文件（推荐）

```python
# prepare a rock_agent_config.yaml
await sandbox.agent.install(config="rock_agent_config.yaml")
await sandbox.agent.run(prompt="hello")
```
