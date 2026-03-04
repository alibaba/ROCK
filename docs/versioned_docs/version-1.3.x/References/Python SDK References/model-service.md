# Model Service (Experimental)

The Model Service provided by ROCK is responsible for handling AI model call communications, serving as a communication bridge between agents and training frameworks (such as Roll) or actual LLM inference services.

## RockAgent Integration

ModelService is typically **automatically managed by RockAgent** - no manual lifecycle management is required. Simply enable it in the configuration:

```python
from rock.sdk.sandbox.model_service.base import ModelServiceConfig

config = ModelServiceConfig(
    enabled=True,  # Enable ModelService, RockAgent manages its lifecycle
)
```

RockAgent will automatically:
- Install ModelService (install Python runtime, install model service package)
- Start/stop ModelService
- Monitor Agent process

## Architecture Overview (Local Mode)

In local mode, the model service uses the **file system** as the communication medium, implementing a request-response mechanism between agents and models.

When an agent needs to call a model, the request is first written to a log file, then processed by the listening component. When the model generates a response, the result is written back to the log file and read by the waiting agent.

## anti_call_llm - Core API

`anti_call_llm()` is the **most important API in Local mode**, used to manually trigger LLM anti-calls for fine-grained control over model calls:

```python
result = await model_service.anti_call_llm(
    index=0,                                   # LLM call index
    response_payload='OpenAI type response',       # Response data (optional)
    call_timeout=600,                          # Operation timeout (seconds)
    check_interval=3,                          # Status check interval (seconds)
)
```

**Use cases:**
- After Agent captures LLM response, call this method to notify Roll runtime
- Supports carrying response data for error handling or retry
- Configurable timeout and check interval for different network environments

## CLI Commands

To use the model service via CLI, ROCK provides a set of CLI commands that can be accessed via `rock model-service` after installing ROCK in the sandbox:

### start command
Start the model service process
```bash
rock model-service start --type [local|proxy] [options]
```

Parameters:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--type` | str | `local` | Service type: `local` or `proxy` |
| `--config-file` | str | None | Path to configuration file |
| `--host` | str | None | Server host address (overrides config) |
| `--port` | int | None | Server port (overrides config) |
| `--proxy-base-url` | str | None | Proxy base URL |
| `--retryable-status-codes` | str | None | Comma-separated list of retryable status codes |
| `--request-timeout` | int | None | Request timeout in seconds |

### watch-agent command
Monitor the agent process and send a SESSION_END message when the process exits
```bash
rock model-service watch-agent --pid <process_id>
```

Parameters:
- `--pid`: The ID of the agent process to monitor

### stop command
Stop the model service
```bash
rock model-service stop
```

### anti-call-llm command
Anti-call the LLM interface
```bash
rock model-service anti-call-llm --index <index> [--response <response>]
```

Parameters:
- `--index`: Index of the previous LLM call, starting from 0
- `--response`: Response from the previous LLM call (optional)

## File Communication Protocol

The model service uses files for inter-process communication, defining specific marker formats to distinguish requests and responses:

### Request Format
```
LLM_REQUEST_START{JSON request data}LLM_REQUEST_END{metadata JSON}
```

### Response Format
```
LLM_RESPONSE_START{JSON response data}LLM_RESPONSE_END{metadata JSON}
```

### Session End Marker
```
SESSION_END
```

Metadata contains timestamp and index information to ensure message order and processing.

## SDK Usage

### ModelServiceConfig

Model service configuration class, located in `rock/sdk/sandbox/model_service/base.py`:

```python
from rock.sdk.sandbox.model_service.base import ModelServiceConfig

config = ModelServiceConfig(
    enabled=True,
    type="local",                                      # Service type
    install_cmd="pip install rock-model-service",      # Install command
    install_timeout=300,                               # Install timeout (seconds)
    start_cmd="rock model-service start --type ${type}",  # Start command
    stop_cmd="rock model-service stop",                # Stop command
    logging_path="/data/logs",                         # Log path
    logging_file_name="model_service.log",             # Log filename
)
```

| Config | Default | Description |
|--------|---------|-------------|
| `enabled` | `False` | Whether to enable model service (RockAgent manages) |
| `type` | `"local"` | Service type: `local` or `proxy` |
| `install_cmd` | - | Model service package install command |
| `install_timeout` | `300` | Install timeout in seconds |
| `start_cmd` | - | Start command template |
| `stop_cmd` | - | Stop command |
| `logging_path` | `/data/logs` | Log directory path |
| `logging_file_name` | `model_service.log` | Log filename |

### ModelService

Model service management class, handles the lifecycle of model services within the sandbox:

```python
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.model_service.base import ModelServiceConfig, ModelService

sandbox = Sandbox(config)
model_service = ModelService(sandbox, ModelServiceConfig())

# Typically auto-managed by RockAgent, no manual calls needed
# The following methods are only for manual control when needed

# Install model service
await model_service.install()

# Start model service
await model_service.start()

# Monitor agent process
await model_service.watch_agent(pid="12345")

# Execute anti-call LLM (Core API for Local mode)
result = await model_service.anti_call_llm(
    index=0,
    response_payload='{"content": "response"}',
    call_timeout=600,
    check_interval=3,
)

# Stop model service
await model_service.stop()
```

## API Reference

### install()

Install model service dependencies in the sandbox.

```python
await model_service.install()
```

Execution steps:
1. Create and initialize Python runtime environment
2. Create Rock config file
3. Install model service package

**Note:** Typically auto-called by RockAgent.

### start()

Start the model service.

```python
await model_service.start()
```

Prerequisite: Must call `install()` first.

**Note:** Typically auto-called by RockAgent.

### stop()

Stop the model service.

```python
await model_service.stop()
```

If the service is not running, this operation will be skipped.

**Note:** Typically auto-called by RockAgent.

### watch_agent(pid)

Monitor the agent process.

```python
await model_service.watch_agent(pid="12345")
```

Sends `SESSION_END` message when the process exits.

### anti_call_llm(index, response_payload, call_timeout, check_interval)

Execute anti-call LLM operation. **This is the most important API in Local mode.**

```python
result = await model_service.anti_call_llm(
    index=0,                                   # LLM call index
    response_payload='{"result": "..."}',       # Response data (optional)
    call_timeout=600,                          # Operation timeout (seconds)
    check_interval=3,                          # Status check interval (seconds)
)
```

## Configuration Options

### Service Configuration
- `SERVICE_HOST`: Service host address, defaults to `"0.0.0.0"`
- `SERVICE_PORT`: Service port, defaults to `8080`

### Log Configuration
- `LOG_FILE`: Log file path used for communication, containing request and response data

### Trajectory (Traj) Logging
The model service records LLM call trajectories (traj) to a JSONL file for debugging and analysis.

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `ROCK_MODEL_SERVICE_DATA_DIR` | `/data/logs` | Directory for traj log files |
| `ROCK_MODEL_SERVICE_TRAJ_APPEND_MODE` | `false` | Append mode (true/false) |

**Traj file location**: `{DATA_DIR}/LLMTraj.jsonl`

**Traj file format** (JSONL - one JSON object per line):
```json
{"request": {...}, "response": {...}}
```

### Polling Configuration
- `POLLING_INTERVAL_SECONDS`: Polling interval, defaults to `0.1` seconds
- `REQUEST_TIMEOUT`: Request timeout, defaults to unlimited

### Marker Configuration
Defines markers used to distinguish different types of messages in the log file:
- `REQUEST_START_MARKER` / `REQUEST_END_MARKER`
- `RESPONSE_START_MARKER` / `RESPONSE_END_MARKER`
- `SESSION_END_MARKER`

### ModelServiceConfig (Server-side)

The server-side configuration class defines how the model service handles requests:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | str | `"0.0.0.0"` | Server host address |
| `port` | int | `8080` | Server port |
| `proxy_base_url` | str \| None | `None` | Direct proxy URL |
| `proxy_rules` | dict | See below | Model name to URL mapping |
| `retryable_status_codes` | list[int] | `[429, 500]` | Retryable HTTP status codes |
| `request_timeout` | int | `120` | Request timeout in seconds |

**Default proxy_rules**:
```python
{
    "gpt-3.5-turbo": "https://api.openai.com/v1",
    "default": "https://api-inference.modelscope.cn/v1",
}
```
