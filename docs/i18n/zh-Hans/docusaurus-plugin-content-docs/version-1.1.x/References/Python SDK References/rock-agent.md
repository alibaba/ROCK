# Rock Agent（实验性）

RockAgent 是 ROCK 框架中的核心 Agent 实现，直接继承自 `Agent` 抽象基类。它提供了完整的 Agent 生命周期管理，包括环境初始化、ModelService 集成、命令执行等功能。
使用sandbox.agent.install()以及sandbox.agent.run(prompt)就可以在Rock提供的Sandbox环境中安装和运行Agent

## 核心概念

RockAgent 通过 `sandbox.agent.install()` 和 `sandbox.agent.run(prompt)` 提供简洁的 API，用于在 ROCK Sandbox 环境中安装和运行 Agent。

## IFlowCli 示例配置文件

```yaml
run_cmd: "iflow -p ${prompt} --yolo"           # ${prompt} 必须

runtime_env_config:
  type: node                                    # IFlowCli 使用 Node 运行时
  npm_registry: "https://registry.npmmirror.com"
  custom_install_cmd: "npm i -g @iflow-ai/iflow-cli@latest"

env:                                            # 环境变量
  IFLOW_API_KEY: "xxxxxxx"
  IFLOW_BASE_URL: "xxxxxxx"
  IFLOW_MODEL_NAME: "Qwen3-Coder-Plus"

```

## LangGraph Agent 启动示例

```yaml
working_dir: "."                                # 上传包含 langgraph_agent.py的本地当前目录到 sandbox里

run_cmd: "python langgraph_agent.py ${prompt}"  # 运行本地脚本

runtime_env_config:
  type: python
  pip:                                           # 安装 Python 依赖
    - langchain==1.2.3
    - langchain-openai==1.1.7
    - langgraph==1.0.6

env:
  OPENAI_API_KEY: xxxxxxx

```

## 详细配置示例与解析

```yaml
# ========== 基础配置 ==========
agent_type: "default"                           # Agent 类型标识 (默认: "default")
agent_name: "demo-agent"                        # Agent 实例名称 (默认: 随机 uuid)
version: "1.0.0"                                # 版本标识 (默认: "default")
instance_id: "instance-001"                     # 实例 ID (默认: "instance-id-<随机uuid>")
agent_installed_dir: "/tmp/installed_agent"     # Agent 安装目录 (默认: "/tmp/installed_agent")
agent_session: "my-session"                     # bash 会话标识 (默认: "agent-session-<随机uuid>")
env:                                             # 环境变量 (默认: {})
  OPENAI_API_KEY: "xxxxxxx"

# ========== 工作目录配置 ==========
working_dir: "./my_project"                     # 本地目录，上传到 sandbox (默认: None 不上传)
project_path: "/testbed"                        # sandbox 中工作目录，用于 cd (默认: None)
use_deploy_working_dir_as_fallback: true        # project_path 为空时是否回退到 deploy.working_dir (默认: true)

# ========== 运行配置 ==========
run_cmd: "python main.py --prompt {prompt}"     # Agent 执行命令，必须包含 {prompt} (默认: None)
# run_cmd: "python ${working_dir}/main.py --prompt {prompt}"  # ${working_dir} 会被替换为实际路径

# 超时配置
agent_install_timeout: 600                      # 安装超时，单位秒 (默认: 600)
agent_run_timeout: 1800                         # 运行超时，单位秒 (默认: 1800)
agent_run_check_interval: 30                    # 检查间隔，单位秒 (默认: 30)


# ========== 安装前/后执行命令 ==========
pre_init_cmds:                                   # 初始化前执行的命令 (默认: 从 env_vars 读取)
  - command: "apt update && apt install -y git"
    timeout_seconds: 300                         # 命令超时，单位秒 (默认: 300)
  - command: "cp ${working_dir}/config.json /root/.config/config.json"
    timeout_seconds: 60

post_init_cmds:                                  # 初始化后执行的命令 (默认: [])
  - command: "echo 'Installation complete'"
    timeout_seconds: 30

# ========== 运行时环境配置 ==========
runtime_env_config:  # 具体参考 RuntimeEnv 有关文档
  type: "python"                                # 运行时类型: python / node (默认: "python")

# ========== ModelService 集成 ==========
model_service_config:                            # 具体参考 ModelService 有关文档
  enabled: true                                  # 启用 ModelService (默认: false)
```

## 使用示例

### 使用 YAML 配置文件 (推荐)

```python
# prepare a rock_agent_config.yaml
await sandbox.agent.install()
await sandbox.agent.run(prompt="hello")
```

## API 参考

### install(config)

初始化 Agent 环境。

**流程：**
1. 如果配置了 `working_dir`，部署到 sandbox
2. 设置 bash session, 以及配置env环境变量
3. 执行 `pre_init_cmds`
4. 并行初始化 RuntimeEnv 和 ModelService（如果启用）
5. 执行 `post_init_cmds`

**参数：**
- `config`: Agent 配置文件，支持两种传入方式：
  - **字符串路径**: YAML 配置文件路径，默认值为 `"rock_agent_config.yaml"`
  - **RockAgentConfig 对象**: 直接传入 `RockAgentConfig` 实例

### run(prompt)

执行 Agent 任务。

**流程：**
1. 替换命令中的 `${prompt}` 占位符
2. 替换 `${working_dir}` 占位符
3. 如果配置了 `project_path`，执行 `cd project_path`
4. 启动agent进程
5. 如果启用 ModelService，启动 `watch_agent`
6. 等待任务完成并返回结果

## 与 ModelService 集成

RockAgent 自动管理 ModelService 生命周期：

```yaml
model_service_config:
  enabled: true                                  # 启用 ModelService
```

**自动执行：**
- 安装阶段：安装 ModelService（仅安装，不启动）
- 运行阶段：启动 ModelService + `watch_agent` 监控进程

## 与 RuntimeEnv 集成

RockAgent 自动管理运行时环境，在 `install()` 阶段并行初始化 RuntimeEnv：

```yaml
runtime_env_config:  # 具体参考 RuntimeEnv 有关文档
  type: "python"        # 运行时类型：python / node
  version: "3.11"       # 版本号
```

**自动执行：**
- 根据 `type` 安装对应的运行时（Python 或 Node.js）
- 安装 `pip` 依赖（如果配置了）
- 执行 `custom_install_cmd` 自定义安装命令（如果配置了）
- 支持 `npm_registry` 配置 Node.js 的 npm 镜像源

**示例：配置 Python 运行时**
```yaml
runtime_env_config:
  type: "python"
  version: "3.11"
  pip:
    - package1==1.0.0
    - package2==2.0.0
  custom_install_cmd: "git clone https://github.com/SWE-agent/SWE-agent.git && cd SWE-agent && pip install -e ."
```

**示例：配置 Node 运行时**
```yaml
runtime_env_config:
  type: "node"
  version: "20"
  npm_registry: "https://registry.npmmirror.com"
  custom_install_cmd: "npm i -g some-package"
```


## 注意事项

### working_dir 与 project_path

| 配置项 | 作用 | 联动方式 |
|--------|------|----------|
| `working_dir` | 本地目录，上传到 sandbox | 调用 `deploy.deploy_working_dir()` 上传，上传后 `deploy.working_dir` 变为 sandbox 中的路径 |
| `${working_dir}` | 命令中的占位符 | 被 `deploy.format()` 替换为 `deploy.working_dir` 的值, 会在配置中的init_cmds和run_cmd中替换 |
| `project_path` | sandbox 中的工作目录 | 用于运行前 `cd project_path`，不设置时会进入到 `deploy.working_dir` 工作|
| `use_deploy_working_dir_as_fallback` | run_cmd时, project_path 未设置时是否回退到 deploy.working_dir |
