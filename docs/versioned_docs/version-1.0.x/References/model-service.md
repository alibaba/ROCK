---
sidebar_position: 2
---

# Model Service

Model Service is an important component in the Self-ROCK framework that handles communication for AI model calls, providing a communication bridge between the Agent and Runtime (Roll).

## Architecture Overview

The model service uses the file system as a communication medium to implement the request-response mechanism between the agent and the model. When an agent needs to call a model, the request is first written to the log file, then processed by the listening component. When the model generates a response, the result is written back to the log file and read by the waiting agent.

## Core Components

### 1. ModelService
Located in `rock/sdk/model/service.py`, responsible for service lifecycle management:
- Start model service (local or proxy mode)
- Monitor agent processes
- Stop model service

### 2. ModelClient
Located in `rock/sdk/model/client.py`, provides client functionality for communicating with the model service:
- Push model responses to the system
- Retrieve new requests from the system
- Manage request/response indexing to maintain order

### 3. Configuration
Located in `rock/sdk/model/server/config.py`, defines the service configuration parameters:
- Service host and port (default 8080)
- Log file path
- Request and response marker strings
- Polling interval and other parameters

## API Interface

### Local API (`/v1/chat/completions`)
- **Method**: POST
- **Description**: OpenAI-compatible chat completion endpoint
- **Functionality**:
  - Process streaming and non-streaming requests
  - Write requests to log file
  - Listen for and return responses from runtime
  - Throw exception if no response is found

### Health Check (`/health`)
- **Method**: GET
- **Description**: Health check endpoint for verifying service status

### Proxy API
The current proxy API (`/v1/chat/completions`) is not yet implemented

## CLI Commands

The model service provides a set of CLI commands accessible through `rock model-service`:

### start command
Start the model service process
```bash
rock model-service start --type [local|proxy]
```

Parameters:
- `--type`: Model service type, can be `local` or `proxy`, default is `local`

### watch-agent command
Monitor the agent process, send SESSION_END message when the process exits
```bash
rock model-service watch-agent --pid <process ID>
```

Parameters:
- `--pid`: Agent process ID to monitor

### stop command
Stop the model service
```bash
rock model-service stop
```

### anti-call-llm command
Anti-call LLM interface
```bash
rock model-service anti-call-llm --index <index> [--response <response>]
```

Parameters:
- `--index`: Index of the last LLM call, starting from 0
- `--response`: Response from the last LLM call (optional)

## File Communication Protocol

The model service uses files for inter-process communication and defines specific marker formats for distinguishing between requests and responses:

### Request Format
```
LLM_REQUEST_START{JSON request data}LLM_REQUEST_END{Metadata JSON}
```

### Response Format
```
LLM_RESPONSE_START{JSON response data}LLM_RESPONSE_END{Metadata JSON}
```

### Session End Marker
```
SESSION_END
```

Metadata includes timestamp and index information to ensure message order and processing.

## Sandbox Integration

### ModelServiceConfig
Located in `rock/sdk/sandbox/model_service/base.py`, defines the model service configuration in the sandbox:
- Working directory
- Python and model service installation commands
- Session environment variables
- Various command templates

### ModelService Class
Manages the model service lifecycle within the sandbox:
- `install()`: Install model service dependencies in the sandbox
- `start()`: Start the model service
- `stop()`: Stop the model service
- `watch_agent()`: Monitor agent processes
- `anti_call_llm()`: Execute anti-call LLM operation

## Workflow

1. Agent initiates model call request
2. Request is formatted and written to log file
3. Model service listens to log file and captures new requests
4. Runtime (Roll) processes the request and generates a response
5. Response is written to log file
6. Model service returns the response to the agent

## Configuration Options

### Service Configuration
- `SERVICE_HOST`: Service host address, default is "0.0.0.0"
- `SERVICE_PORT`: Service port, default is 8080

### Log Configuration
- `LOG_FILE`: Log file path containing request and response data

### Polling Configuration
- `POLLING_INTERVAL_SECONDS`: Polling interval, default is 0.1 seconds
- `REQUEST_TIMEOUT`: Request timeout, default is infinite

### Marker Configuration
Defines markers for distinguishing different message types in the log file:
- `REQUEST_START_MARKER` / `REQUEST_END_MARKER`
- `RESPONSE_START_MARKER` / `RESPONSE_END_MARKER`
- `SESSION_END_MARKER`