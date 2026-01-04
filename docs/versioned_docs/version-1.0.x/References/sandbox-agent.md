---
sidebar_position: 3
---

# Sandbox Agent

The Sandbox Agent is a component in the Self-ROCK framework for running AI agent tasks in an isolated environment, providing a secure and controllable agent execution environment. Agents can be integrated with Model Service to handle AI model calls.

## Architecture Overview

The sandbox agent initializes and executes agent tasks in an isolated sandbox environment, including installing necessary dependencies, configuring the runtime environment, and other steps. The agent system supports multiple different agent types, such as SWE-agent and IFlow CLI, each with specific configurations and implementations.

Agents can be integrated with Model Service to handle AI model calls during task execution. When agents need to query or call AI models, the model service processes model communications in the background, allowing agents to focus on task execution.

## Model Service Integration Capability

Sandbox agents support seamless integration with Model Service, providing a communication bridge for AI model calls. This integration allows agents to interact with Model Service through file system communication protocol, enabling asynchronous request-response mechanisms.

### Integration Methods
- **Configuration-driven**: Specify Model Service through `model_service_config` parameter in the agent configuration
- **Parallel Initialization**: Model Service installation process executes in parallel with agent installation
- **Dynamic Start**: Model Service can be started on-demand during task execution
- **Process Monitoring**: Monitor agent processes through `watch-agent` command

### ModelServiceConfig Properties
- `workdir`: Model Service working directory
- `python_install_cmd`: Python environment installation command
- `model_service_install_cmd`: Model Service installation command
- `python_install_timeout`: Python installation timeout
- `model_service_install_timeout`: Model Service installation timeout
- `model_service_type`: Model Service type (local/proxy)
- `model_service_session`: Service session name
- `start_cmd`, `stop_cmd`, `watch_agent_cmd`: Control command templates
- `anti_call_llm_cmd`: Anti-call LLM command
- `logging_path`: Log path

## Core Components

### 1. Agent Abstract Base Class
Located in `rock/sdk/sandbox/agent/base.py`, defines the abstract base class for all sandbox agents:
- `__init__()`: Initialize the agent with sandbox instance and optional model service
- `init()`: Asynchronously initialize the agent environment
- `run()`: Asynchronously execute agent tasks

### 2. AgentConfig Abstract Configuration Class
Located in `rock/sdk/sandbox/agent/config.py`, defines the basic configuration for agents:
- `agent_type`: Agent type identifier
- `version`: Version information

## Specific Agent Implementations

### SWE-agent
Implemented based on the `Agent` base class, specifically designed to handle software engineering tasks.

#### SweAgentConfig Configuration
- `agent_type`: Fixed to "swe-agent"
- `default_run_single_config`: Default runtime configuration
- `agent_session`: Name of the bash session for agent execution
- `pre_startup_bash_cmd_list`: List of commands to execute before startup
- `post_startup_bash_cmd_list`: List of commands to execute after startup
- `swe_agent_workdir`: Agent working directory
- `python_install_cmd`: Python environment installation command
- `swe_agent_install_cmd`: SWE-agent installation command
- `python_install_timeout`: Python installation timeout duration
- `swe_agent_install_timeout`: SWE-agent installation timeout duration
- `agent_run_timeout`: Agent runtime timeout duration
- `agent_run_check_interval`: Agent runtime check interval
- `model_service_config`: Optional model service configuration

#### SweAgent Implementation
- `init()`: Initialize SWE-agent environment, including installing Python environment, SWE-agent dependencies, and optional model service
- `run()`: Execute specified software engineering tasks
- `start_model_service()`: Start the associated model service
- `_install_swe_agent()`: Install the SWE-agent environment
- `_init_model_service()`: Initialize the model service
- `_agent_run()`: Execute agent commands

### IFlow CLI Agent
Implemented based on the `Agent` base class, handles command-line-based agent tasks.

#### IFlowCliConfig Configuration
- `agent_type`: Fixed to "iflow-cli"
- `agent_session`: Name of the bash session for agent execution
- `pre_startup_bash_cmd_list`: List of commands to execute before startup
- `npm_install_cmd`: NPM installation command
- `npm_install_timeout`: NPM installation timeout duration
- `iflow_cli_install_cmd`: IFlow CLI installation command
- `iflow_settings`: IFlow configuration settings
- `iflow_run_cmd`: Execution command template
- `iflow_log_file`: IFlow log file path
- `session_envs`: Session environment variables
- `model_service_config`: Optional model service configuration

#### IFlowCli Implementation
- `init()`: Initialize IFlow CLI environment, including installing Node.js, IFlow CLI, and optional model service
- `run()`: Run the IFlow CLI with a specified problem statement
- `start_model_service()`: Start the associated model service
- `_install_iflow_cli()`: Install the IFlow CLI environment
- `_init_model_service()`: Initialize the model service
- `_extract_session_id_from_log()`: Extract session ID from log
- `_get_session_id_from_sandbox()`: Retrieve session ID from sandbox
- `_agent_run()`: Execute agent commands

## Initialization Process

1. Create sandbox environment and agent instance
2. Call `init()` method to initialize the agent
3. Create dedicated bash session in the sandbox
4. Execute pre-startup commands (such as environment configuration)
5. Install necessary dependencies (Python, Node.js, etc.)
6. Install specific agent tools (such as SWE-agent or IFlow CLI)
7. If model service is configured, initialize model service in parallel

## Execution Process

1. Prepare task execution parameters (problem statement, project path, etc.)
2. Call `run()` method to execute the task
3. Prepare configuration files required for the task
4. Upload configuration files to the sandbox environment
5. Execute agent command
6. If model service is configured, monitor the agent process
7. Wait for task completion and return results

## Model Service Interaction Flow

When agents are integrated with Model Service, the workflow is:

### Before Task Start
1. Call `start_model_service()` to start Model Service
2. Call `watch_agent()` to set up monitoring for the agent process

### When Agent Calls LLM
1. Agent initiates model call request
2. Request is written to log file via file communication protocol
3. Model Service listener captures the request
4. Actual AI model returns response
5. Response is written back via file communication protocol
6. Agent reads response and continues execution

### After Task Completion
1. If needed, stop Model Service process
2. Clean up related resources

## Workflow

### Initialization Phase
1. Create sandbox dedicated session
2. Execute environment pre-configuration commands
3. Create working directory
4. Install runtime environment (Python/Node.js)
5. Install specific agent tools
6. Optional: Initialize model service

### Task Execution Phase
1. Receive task parameters: problem description, codebase path, etc.
2. Generate and upload task-specific configuration
3. Execute agent command
4. Optional: Start agent monitoring (if model service is configured)
5. Wait for processing completion
6. Collect and return execution results

## Configuration Options

### General Agent Configuration
- `agent_session`: Name of the bash session for agent execution
- `pre_startup_bash_cmd_list`: List of commands to execute before startup
- `session_envs`: Session environment variables configuration
- `model_service_config`: Model service configuration (optional)

### SWE-agent Specific Configuration
- `swe_agent_workdir`: SWE-agent working directory
- `python_install_cmd`: Python installation command
- `swe_agent_install_cmd`: SWE-agent installation command
- `default_run_single_config`: Default runtime configuration

### IFlow CLI Specific Configuration
- `npm_install_cmd`: NPM installation command
- `iflow_cli_install_cmd`: IFlow CLI installation command
- `iflow_settings`: IFlow settings
- `iflow_run_cmd`: Execution command template
- `iflow_log_file`: Log file path