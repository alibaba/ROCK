# Rock Agent (Experimental)

RockAgent is the core Agent implementation in the ROCK framework, inheriting directly from the `Agent` abstract base class. It provides complete Agent lifecycle management including environment initialization, ModelService integration, and command execution.
Use `sandbox.agent.install()` and `sandbox.agent.run(prompt)` to install and run Agent in the ROCK Sandbox environment.

## Core Concepts

RockAgent provides simple APIs via `sandbox.agent.install()` and `sandbox.agent.run(prompt)` for installing and running Agent in the ROCK Sandbox environment.

## IFlowCli Config Example

```yaml
run_cmd: "iflow -p ${prompt} --yolo"           # ${prompt} is required

runtime_env_config:
  type: node                                    # IFlowCli uses Node runtime
  npm_registry: "https://registry.npmmirror.com"
  custom_install_cmd: "npm i -g @iflow-ai/iflow-cli@latest"

env:                                            # Environment variables
  IFLOW_API_KEY: "xxxxxxx"
  IFLOW_BASE_URL: "xxxxxxx"
  IFLOW_MODEL_NAME: "Qwen3-Coder-Plus"

```

## LangGraph Agent Example

```yaml
working_dir: "."                                # Upload current directory containing langgraph_agent.py to sandbox

run_cmd: "python langgraph_agent.py ${prompt}"  # Run local script

runtime_env_config:
  type: python
  pip:                                           # Install Python dependencies
    - langchain==1.2.3
    - langchain-openai==1.1.7
    - langgraph==1.0.6

env:
  OPENAI_API_KEY: xxxxxxx

```

## Detailed Config Example

```yaml
# ========== Basic Config ==========
agent_type: "default"                           # Agent type identifier (default: "default")
agent_name: "demo-agent"                        # Agent instance name (default: random uuid)
version: "1.0.0"                                # Version identifier (default: "default")
instance_id: "instance-001"                     # Instance ID (default: "instance-id-<random_uuid>")
agent_installed_dir: "/tmp/installed_agent"     # Agent installation directory (default: "/tmp/installed_agent")
agent_session: "my-session"                     # Bash session identifier (default: "agent-session-<random_uuid>")
env:                                             # Environment variables (default: {})
  OPENAI_API_KEY: "xxxxxxx"

# ========== Working Directory Config ==========
working_dir: "./my_project"                     # Local directory, upload to sandbox (default: None, no upload)
project_path: "/testbed"                        # Working directory in sandbox, used for cd (default: None)
use_deploy_working_dir_as_fallback: true        # Fall back to deploy.working_dir when project_path is empty (default: true)

# ========== Runtime Config ==========
run_cmd: "python main.py --prompt {prompt}"     # Agent execution command, must contain {prompt} (default: None)
# run_cmd: "python ${working_dir}/main.py --prompt {prompt}"  # ${working_dir} will be replaced with actual path

# Timeout Config
agent_install_timeout: 600                      # Install timeout in seconds (default: 600)
agent_run_timeout: 1800                         # Run timeout in seconds (default: 1800)
agent_run_check_interval: 30                    # Check interval in seconds (default: 30)


# ========== Pre/Post Init Commands ==========
pre_init_cmds:                                   # Commands to execute before initialization (default: from env_vars)
  - command: "apt update && apt install -y git"
    timeout_seconds: 300                         # Command timeout in seconds (default: 300)
  - command: "cp ${working_dir}/config.json /root/.config/config.json"
    timeout_seconds: 60

post_init_cmds:                                  # Commands to execute after initialization (default: [])
  - command: "echo 'Installation complete'"
    timeout_seconds: 30

# ========== Runtime Environment Config ==========
runtime_env_config:  # See RuntimeEnv documentation for details
  type: "python"                                # Runtime type: python / node (default: "python")

# ========== ModelService Integration ==========
model_service_config:                            # See ModelService documentation for details
  enabled: true                                  # Enable ModelService (default: false)
```

## Usage Example

### Using YAML Config File (Recommended)

```python
# prepare a rock_agent_config.yaml
await sandbox.agent.install()
await sandbox.agent.run(prompt="hello")
```

## API Reference

### install(config)

Initialize Agent environment.

**Flow:**
1. If `working_dir` is configured, deploy to sandbox
2. Setup bash session and configure env variables
3. Execute `pre_init_cmds`
4. Parallel initialize RuntimeEnv and ModelService (if enabled)
5. Execute `post_init_cmds`

**Parameters:**
- `config`: Agent configuration file, supports two input methods:
  - **String path**: YAML config file path, default value like `"rock_agent_config.yaml"`
  - **RockAgentConfig object**: Directly pass a `RockAgentConfig` instance

### run(prompt)

Execute Agent task.

**Flow:**
1. Replace `${prompt}` placeholder in command
2. Replace `${working_dir}` placeholder
3. If `project_path` is configured, execute `cd project_path`
4. Start agent process
5. If ModelService is enabled, start `watch_agent`
6. Wait for task completion and return result

## ModelService Integration

RockAgent automatically manages ModelService lifecycle:

```yaml
model_service_config:
  enabled: true                                  # Enable ModelService
```

**Auto-executed:**
- Install phase: Install ModelService (install only, not start)
- Run phase: Start ModelService + `watch_agent` to monitor process

## RuntimeEnv Integration

RockAgent automatically manages runtime environment, initializing RuntimeEnv in parallel during `install()`:

```yaml
runtime_env_config:  # See RuntimeEnv documentation for details
  type: "python"        # Runtime type: python / node
  version: "3.11"       # Version
```

**Auto-executed:**
- Install corresponding runtime based on `type` (Python or Node.js)
- Install `pip` dependencies (if configured)
- Execute `custom_install_cmd` custom install command (if configured)
- Support `npm_registry` for Node.js npm mirror

**Example: Configure Python Runtime**
```yaml
runtime_env_config:
  type: "python"
  version: "3.11"
  pip:
    - package1==1.0.0
    - package2==2.0.0
  custom_install_cmd: "git clone https://github.com/SWE-agent/SWE-agent.git && cd SWE-agent && pip install -e ."
```

**Example: Configure Node Runtime**
```yaml
runtime_env_config:
  type: "node"
  version: "20"
  npm_registry: "https://registry.npmmirror.com"
  custom_install_cmd: "npm i -g some-package"
```


## Notes

### working_dir vs project_path

| Config | Purpose | How it Works |
|--------|---------|--------------|
| `working_dir` | Local directory, upload to sandbox | Call `deploy.deploy_working_dir()` to upload, after upload `deploy.working_dir` becomes sandbox path |
| `${working_dir}` | Placeholder in commands | Replaced by `deploy.format()` with `deploy.working_dir` value, replaced in init_cmds and run_cmd |
| `project_path` | Working directory in sandbox | Used for `cd project_path` before running, if not set, work in `deploy.working_dir` |
| `use_deploy_working_dir_as_fallback` | When running command, whether to fall back to deploy.working_dir if project_path is not set |
