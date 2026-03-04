# Rock Agent (Experimental)

RockAgent is the core Agent implementation in the ROCK framework, directly inheriting from the `Agent` abstract base class. It provides complete Agent lifecycle management, including environment initialization, ModelService integration, command execution, and more.

Using `sandbox.agent.install()` and `sandbox.agent.run(prompt)`, you can install and run Agents in the Sandbox environment provided by Rock.

## Core Concepts

The core workflow of RockAgent is divided into two phases:

1. **install(config)**: Initialize the Agent environment, including deploying the working directory, setting environment variables, initializing the runtime environment, etc.
2. **run(prompt)**: Execute the Agent task, replace placeholders, and start the Agent process

## Quick Start

### Claude Code Example

```yaml
run_cmd: "claude -p ${prompt}"

runtime_env_config:
  type: node
  custom_install_cmd: "npm install -g @anthropic-ai/claude-code"

env:
  ANTHROPIC_BASE_URL: ""
  ANTHROPIC_API_KEY: ""
```

### IFlowCli Example

```yaml
run_cmd: "iflow -p ${prompt} --yolo"           # ${prompt} is required

runtime_env_config:
  type: node
  custom_install_cmd: "npm i -g @iflow-ai/iflow-cli@latest"

env:                                            # Environment variables
  IFLOW_API_KEY: "xxxxxxx"
  IFLOW_BASE_URL: "xxxxxxx"
  IFLOW_MODEL_NAME: "xxxxxxx"
```

### LangGraph Agent Example

```yaml
working_dir: "."                                # Upload local current directory containing langgraph_agent.py to sandbox

run_cmd: "python langgraph_agent.py ${prompt}"  # Run local script

runtime_env_config:
  type: python
  pip:                                          # Install pip dependencies
    - langchain==1.2.3
    - langchain-openai==1.1.7
    - langgraph==1.0.6

env:
  OPENAI_API_KEY: xxxxxxx
```

## Configuration Details

### Basic Configuration

```yaml
agent_type: "default"                           # Agent type identifier (default: "default")
agent_name: "demo-agent"                        # Agent instance name (default: random uuid)
version: "1.0.0"                                # Version identifier (default: "default")
instance_id: "instance-001"                     # Instance ID (default: "instance-id-<random-uuid>")
agent_installed_dir: "/tmp/installed_agent"     # Agent installation directory (default: "/tmp/installed_agent")
agent_session: "my-session"                     # Bash session identifier (default: "agent-session-<random-uuid>")
env:                                            # Environment variables (default: {})
  OPENAI_API_KEY: "xxxxxxx"
```

### Working Directory Configuration

```yaml
working_dir: "./my_project"                     # Local directory to upload to sandbox (default: None, no upload)
project_path: "/testbed"                        # Working directory in sandbox for cd (default: None)
use_deploy_working_dir_as_fallback: true        # Whether to fall back to deploy.working_dir when project_path is empty (default: true)
```

### Execution Configuration

```yaml
run_cmd: "python main.py --prompt ${prompt}"    # Agent execution command, must contain ${prompt} (default: None)

skip_wrap_run_cmd: false                       # Skip wrapping run_cmd with PATH (default: false)

# Timeout configuration
agent_install_timeout: 600                      # Installation timeout in seconds (default: 600)
agent_run_timeout: 1800                         # Run timeout in seconds (default: 1800)
agent_run_check_interval: 30                    # Check interval in seconds (default: 30)
```

**`skip_wrap_run_cmd`**:
- `false` (default): Wraps the command with `export PATH=<bin_dir>:$PATH &&` to ensure runtime environment executables are used
- `true`: Skips PATH wrapping, runs the command directly with `bash -c`

### Initialization Hooks

```yaml
pre_init_cmds:                                  # Commands executed before initialization (default: read from env_vars)
  - command: "apt update && apt install -y git"
    timeout_seconds: 300                        # Command timeout in seconds (default: 300)
  - command: "cp ${working_dir}/config.json /root/.config/config.json"
    timeout_seconds: 60

post_init_cmds:                                 # Commands executed after initialization (default: [])
  - command: "echo 'Installation complete'"
    timeout_seconds: 30
```

**Notes**:
- `pre_init_cmds` and `post_init_cmds` do not inherit the Agent's `env` environment variables
- Typically used for installation operations and configuration file movement
- Common command examples:
  - `apt update && apt install -y git wget tar`
  - `cp ${working_dir}/config.json /root/.config/config.json`

### RuntimeEnv Configuration

```yaml
runtime_env_config:                             # Refer to RuntimeEnv documentation for details
  type: "python"                                # Runtime type: python / node (default: "python")
  version: "3.11"                               # Version number
  pip:                                          # Python dependency package list
    - package1==1.0.0
    - package2==2.0.0
  custom_install_cmd: "git clone https://github.com/SWE-agent/SWE-agent.git && cd SWE-agent && pip install -e ."
```

**Node Runtime Example**:

```yaml
runtime_env_config:
  type: "node"
  version: "22.18.0"
  npm_registry: "https://registry.npmmirror.com"
  custom_install_cmd: "npm i -g some-package"
```

**Automatic Operations**:
- Install corresponding runtime based on `type` (Python or Node.js)
- Install `pip` dependencies (if configured)
- Execute `custom_install_cmd` custom installation command (if configured)
- Support `npm_registry` configuration for Node.js npm mirror source

### ModelService Configuration

```yaml
model_service_config:                           # Refer to ModelService documentation for details
  enabled: true                                 # Enable ModelService (default: false)
```

**Automatic Operations**:
- Installation phase: Install ModelService (install only, do not start)
- Run phase: Start ModelService + `watch_agent` monitoring process

**Notes**: You need to set the model request URL to the ModelService URL. For example, if the ModelService provides an OpenAI-compatible URL at `http://127.0.0.1:8080/v1/chat/completions`, you typically need to set the Agent's LLM request URL to `http://127.0.0.1:8080/v1/`.

## API Reference

### install(config)

Initialize the Agent environment.

**Execution Flow**:
1. If `working_dir` is configured, deploy to sandbox
2. Set up bash session and configure env environment variables
3. Execute `pre_init_cmds`
4. Initialize RuntimeEnv and ModelService in parallel (if enabled)
5. Execute `post_init_cmds`

**Parameters**:
- `config`: Agent configuration file, supports two input methods:
  - **String path**: YAML configuration file path, default value is `"rock_agent_config.yaml"`
  - **RockAgentConfig object**: Directly pass a `RockAgentConfig` instance

### run(prompt)

Execute the Agent task.

**Execution Flow**:
1. Replace placeholders and prepare Agent run command
2. Start the agent process
3. If ModelService is enabled, start `watch_agent`
4. Wait for task completion and return results

## Advanced Usage

### Difference and Interaction between working_dir and project_path

| Configuration | Function | Interaction Method |
|--------------|----------|-------------------|
| `working_dir` | Local directory uploaded to sandbox | Calls `deploy.deploy_working_dir()` to upload, after upload `deploy.working_dir` becomes the path in sandbox |
| `${working_dir}` | Placeholder in commands | Replaced by `deploy.format()` with the value of `deploy.working_dir`, replaced in init_cmds and run_cmd in the configuration |
| `project_path` | Working directory in sandbox | Used for `cd project_path` before running, when not set it enters the `deploy.working_dir` working directory |
| `use_deploy_working_dir_as_fallback` | Whether to fall back to deploy.working_dir when project_path is not set at runtime | Default is `true`, when set to `false` it will not enter working_dir even if project_path is not set |

**Usage Recommendations**:
- Use `working_dir` to upload local project code to sandbox
- Use `project_path` to specify the working directory in sandbox (e.g., `/testbed`)
- Set `use_deploy_working_dir_as_fallback: false` scenario: Need to perform local file mounting, but want to run Agent in the image's default working directory

### Placeholder Usage

Rock Agent supports replacing the following placeholders in the configuration file:

- `${prompt}`: Required in run_cmd, will be replaced with the prompt passed to `run(prompt)`
- `${working_dir}`: Optional, will be replaced with the actual working directory path in sandbox, also supported in init_cmds and run_cmd
- `${bin_dir}`: Optional, will be replaced with the runtime environment's bin directory path

**Example**:
```yaml
run_cmd: "python ${working_dir}/main.py --prompt ${prompt}"
```

### use_deploy_working_dir_as_fallback Explanation

When `project_path` is not set:
- `true` (default): Before running Agent, it will automatically `cd` to `deploy.working_dir`
- `false`: Before running Agent, it will not automatically switch directories, staying in the current directory

Applicable Scenarios:
- `true`: Most scenarios, where you want Agent to run in the uploaded code directory
- `false`: Need to mount local files, but want to run Agent in the image's default working directory (e.g., `/app`, `/testbed`)

## Complete Configuration Example

```yaml
# ========== Basic Configuration ==========
agent_type: "default"
agent_name: "demo-agent"
version: "1.0.0"
instance_id: "instance-001"
agent_installed_dir: "/tmp/installed_agent"
agent_session: "my-session"
env:
  OPENAI_API_KEY: "xxxxxxx"

# ========== Working Directory Configuration ==========
working_dir: "./my_project"
project_path: "/testbed"
use_deploy_working_dir_as_fallback: true

# ========== Run Configuration ==========
run_cmd: "python ${working_dir}/main.py --prompt ${prompt}"

# Timeout configuration
agent_install_timeout: 600
agent_run_timeout: 1800
agent_run_check_interval: 30

# ========== Initialization Commands ==========
pre_init_cmds:
  - command: "apt update && apt install -y git"
    timeout_seconds: 300
  - command: "cp ${working_dir}/config.json /root/.config/config.json"
    timeout_seconds: 60

post_init_cmds:
  - command: "echo 'Installation complete'"
    timeout_seconds: 30

# ========== Runtime Environment Configuration ==========
runtime_env_config:
  type: "python"
  version: "3.11"
  pip:
    - langchain==1.2.3
    - langchain-openai==1.1.7

# ========== ModelService Integration ==========
model_service_config:
  enabled: true
```

## Usage Examples

### Using YAML Configuration File (Recommended)

```python
# prepare a rock_agent_config.yaml
await sandbox.agent.install(config="rock_agent_config.yaml")
await sandbox.agent.run(prompt="hello")
```
