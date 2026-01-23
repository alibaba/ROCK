---
sidebar_position: 2
---

# Model Service（Experimental）

The Model Service provided by ROCK is responsible for handling AI model call communications, serving as a communication bridge between agents and training frameworks (such as Roll) or actual LLM inference services.

## Architecture Overview

The model service uses the file system as a communication medium, implementing a request-response mechanism between agents and models. When an agent needs to call a model, the request is first written to a log file, then processed by the listening component. When the model generates a response, the result is written back to the log file and read by the waiting agent.

## CLI Commands

To use the model service via CLI, ROCK provides a set of CLI commands that can be accessed via `rock model-service` after installing ROCK in the sandbox:

### start command
Start the model service process
```bash
rock model-service start --type [local|proxy]
```

Parameters:
- `--type`: Model service type, optional `local` or `proxy`, defaults to `local`

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

## Sandbox Integration

### ModelServiceConfig
Located in `rock/sdk/sandbox/model_service/base.py`, defines model service configuration in the sandbox:
- Working directory
- Python and model service installation commands
- Session environment variables
- Various command templates

### ModelService Class
Handles the lifecycle of model services within the sandbox:
- `install()`: Install model service dependencies in the sandbox
- `start()`: Start the model service
- `stop()`: Stop the model service
- `watch_agent()`: Monitor the agent process
- `anti_call_llm()`: Perform anti-call LLM operations

## Workflow

1. Agent initiates a model call request
2. Request is formatted and written to a log file
3. Model service listens to the log file and captures new requests
4. Runtime (Roll) processes the request and generates a response
5. Response is written to the log file
6. Model service returns the response to the agent

## Configuration Options

### Service Configuration
- `SERVICE_HOST`: Service host address, defaults to "0.0.0.0"
- `SERVICE_PORT`: Service port, defaults to 8080

### Log Configuration
- `LOG_FILE`: Log file path used for communication, containing request and response data

### Polling Configuration
- `POLLING_INTERVAL_SECONDS`: Polling interval, defaults to 0.1 seconds
- `REQUEST_TIMEOUT`: Request timeout, defaults to unlimited

### Marker Configuration
Defines markers used to distinguish different types of messages in the log file:
- `REQUEST_START_MARKER` / `REQUEST_END_MARKER`
- `RESPONSE_START_MARKER` / `RESPONSE_END_MARKER`
- `SESSION_END_MARKER`