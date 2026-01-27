---
sidebar_position: 3
---

# Sandbox Agent（Experimental）

Sandbox Agent is a component in the ROCK framework used to run AI agent tasks in an isolated environment, providing a secure and controllable execution environment for agents. Agents can be integrated with Model Service to achieve elegant handling of LLM model calls.

## Architecture Overview

Sandbox Agent initializes and executes agent tasks in an isolated sandbox environment, including steps such as installing necessary dependencies and configuring the runtime environment. The agent system supports multiple types of agents, such as SWE-agent and IFlow CLI, each with their specific configurations and implementation approaches.

Agents can be integrated with Model Service to handle AI model calls during task execution. When an agent needs to query or call an AI model, the Model Service handles model communication in the background, allowing the agent to focus on task execution.

## Sandbox Agent Initialization Workflow

1. Create sandbox environment and agent instance
2. Call `init()` method to initialize the agent
3. Create a dedicated bash session in the sandbox
4. Execute pre-startup commands (such as environment configuration)
5. Install necessary dependencies (Python, Node.js, etc.)
6. Install specific agent tools (such as SWE-agent or IFlow CLI)
7. If model service is configured, synchronously initialize the model service

## Sandbox Agent Execution Workflow

1. Prepare task running parameters (problem statement, project path, etc.)
2. Call `run()` method to execute the task
3. Prepare configuration files required for running and upload them to the sandbox environment (if needed)
4. Execute Agent
5. If model service is configured, synchronously monitor the Agent process
6. Wait for task completion and return results

## Model Service Integration Capabilities

Sandbox Agent supports seamless integration with Model Service, providing a communication bridge for AI model calls. This integration allows agents to interact with Model Service through the file system communication protocol, implementing an asynchronous request-response mechanism. During Agent initialization, the LLM address requested by the Agent can be configured to the address of the model-service server (such as the default http://localhost:8080/v1/chat/completions), thereby leveraging model-service to access the actual inference service.

The workflow when an agent is integrated with model service:

### Before Task Starts
1. Call `start_model_service()` to start the model service
2. Call `watch_agent()` to set up monitoring of the agent process

### When Agent Calls LLM
1. Agent initiates a model call request
2. Request is written to the log file through the file communication protocol
3. Model service listener captures the request
4. Actual AI model returns a response
5. Response is written back through the file communication protocol
6. Agent reads the response and continues execution
