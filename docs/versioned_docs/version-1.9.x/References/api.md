---
sidebar_position: 1
---

# API Reference

This guide provides detailed information about the core API services provided by the ROCK platform, including sandbox environment management and GEM environment interaction.

## 1. Overview

The ROCK platform provides two core API services:
- Sandbox API: Sandbox environment management
- GEM API: GEM environment interaction

All API interfaces follow RESTful design principles and support JSON format data exchange.

## 2. Sandbox API

Full lifecycle management functions for sandbox environments:

### Sandbox Management Interfaces

1. **Start Sandbox** - Start a sandbox environment
   - Create a new sandbox instance
   - Support specifying image, resource configuration and other parameters

2. **Start Sandbox Async** - Asynchronously start a sandbox environment
   - Asynchronously create a sandbox instance
   - Suitable for scenarios requiring quick response

3. **Check Sandbox Alive Status** - Check sandbox alive status
   - Verify if the sandbox is running normally

4. **Get Sandbox Statistics** - Get sandbox statistics
   - Get resource usage statistics of the sandbox

5. **Get Sandbox Status** - Get detailed sandbox status
   - Get complete status information of the sandbox

6. **Stop Sandbox** - Stop sandbox environment
   - Safely shut down the sandbox instance

7. **Commit Sandbox** - Commit sandbox as image
   - Save current sandbox state as a new image

### Command Execution Interfaces

8. **Execute Command** - Execute command in sandbox
   - Run specified command directly in the sandbox

9. **Create Bash Session** - Create Bash session
   - Create a persistent Bash session environment

10. **Run Command in Session** - Run command in session
    - Execute command in a created session

11. **Close Session** - Close session
    - Release session resources

### File Operation Interfaces

12. **Read File** - Read sandbox file
    - Read specified file content from the sandbox

13. **Write File** - Write sandbox file
    - Write file to the sandbox

14. **Upload File** - Upload file to sandbox
    - Upload local file to the sandbox

## 3. GEM API

GEM environment interaction functions:

1. **Make Environment** - Create GEM environment
   - Initialize a new GEM environment instance

2. **Reset Environment** - Reset GEM environment
   - Reset GEM environment to initial state

3. **Step Environment** - Execute GEM environment step
   - Execute an action step in the GEM environment

4. **Close Environment** - Close GEM environment
   - Release GEM environment resources


## 4. HTTP API Usage Examples

### 4.1 Sandbox API Examples

#### Start Sandbox
```bash
curl -X POST 'http://localhost:8080/apis/envs/sandbox/v1/start' \
-H 'Content-Type: application/json' \
-d '{
  "image": "python:3.11",
  "resources": {
    "cpu": "2",
    "memory": "8g"
  }
}'
```

#### Asynchronously Start Sandbox
```bash
curl -X POST 'http://localhost:8080/apis/envs/sandbox/v1/start_async' \
-H 'Content-Type: application/json' \
-d '{
  "image": "python:3.11",
  "resources": {
    "cpu": "2",
    "memory": "8g"
  }
}'
```

#### Execute Command
```bash
curl -X POST 'http://localhost:8080/apis/envs/sandbox/v1/execute' \
-H 'Content-Type: application/json' \
-d '{
  "sandbox_id": "sandbox-12345",
  "command": "ls -la"
}'
```

#### Create Session
```bash
curl -X POST 'http://localhost:8080/apis/envs/sandbox/v1/create_session' \
-H 'Content-Type: application/json' \
-d '{
  "sandbox_id": "sandbox-12345",
  "session": "my_session"
}'
```

#### Run Command in Session
```bash
curl -X POST 'http://localhost:8080/apis/envs/sandbox/v1/run_in_session' \
-H 'Content-Type: application/json' \
-d '{
  "sandbox_id": "sandbox-12345",
  "session": "my_session",
  "command": "python script.py"
}'
```

#### Upload File
```bash
curl -X POST 'http://localhost:8080/apis/envs/sandbox/v1/upload' \
-F 'file=@./local_file.txt' \
-F 'target_path=./remote_file.txt' \
-F 'sandbox_id=sandbox-12345'
```

#### Stop Sandbox
```bash
curl -X POST 'http://localhost:8080/apis/envs/sandbox/v1/stop' \
-H 'Content-Type: application/json' \
-d '{
  "sandbox_id": "sandbox-12345"
}'
```

### 4.2 GEM API Examples

```bash
# Create GEM environment
curl -X POST 'http://localhost:8080/apis/v1/envs/gem/make' \
-H 'Content-Type: application/json' \
-d '{"env_id": "game:Sokoban-v0-easy"}'

# Reset environment
curl -X POST 'http://localhost:8080/apis/v1/envs/gem/reset' \
-H 'Content-Type: application/json' \
-d '{"sandbox_id": "sandbox-12345", "seed": 42}'

# Execute step
curl -X POST 'http://localhost:8080/apis/v1/envs/gem/step' \
-H 'Content-Type: application/json' \
-d '{"sandbox_id": "sandbox-12345", "action": "random_action"}'

# Close environment
curl -X POST 'http://localhost:8080/apis/v1/envs/gem/close' \
-H 'Content-Type: application/json' \
-d '{"sandbox_id": "sandbox-12345"}'
```

## Related Documents

- [Quick Start Guide](../Getting%20Started/quickstart.md) - Learn how to quickly get started with ROCK API
- [Python SDK Documentation](./Python%20SDK%20References/python_sdk.md) - Learn how to use the SDK to call APIs
- [Configuration Guide](../User%20Guides/configuration.md) - Learn about API-related configuration options
- [Installation Guide](../Getting%20Started/installation.md) - Detailed information about ROCK installation and setup