---
sidebar_position: 7
---

# ROCK & ROLL Quick Start Guide

This guide will walk you through running a reinforcement learning training example based on the Sokoban game, using ROLL (the training framework) and ROCK (the environment management tool).

## 1. Prerequisites

Before you begin, please ensure your system has the following dependencies installed.

### 1.1 System Requirements

- **OS**: A Linux-based system is recommended (e.g., Ubuntu 20.04+).
- **Hardware**: An NVIDIA GPU with the corresponding drivers is recommended.
- **Docker**: ROCK uses Docker for containerized environment management.
- **uv**: ROCK uses uv for dependency management and virtual environment creation.

### 1.2 Verify Dependencies & Pre-pull Image

```bash
# Verify Docker installation
docker --version

# Verify Docker is running and pre-pull the Sokoban environment image
# This will save time when the training starts.
docker pull rock-n-roll-registry.cn-hangzhou.cr.aliyuncs.com/rock/sokoban-sandbox:latest

# Verify uv installation
uv --version

```

### 1.3 Initialize the Project

```bash
# Clone the project repositories
git clone https://github.com/alibaba/ROCK.git
git clone https://github.com/alibaba/ROLL.git

# Ensure both repositories are in the same parent directory, like this:
# your-workspace/
# ├── ROCK/
# └── ROLL/
```


## 2. Launch the Training Process

> Note: The following instructions use torch==2.6.0 and vLLM==0.8.4 as an example.


### Option 1: Using a Virtual Environment (Recommended)

#### Why is this method recommended?
- Isolation: A uv virtual environment ensures that project dependencies are isolated from your system, preventing conflicts.
- Fast Startup: ROCK can reuse this virtual environment, significantly speeding up subsequent task initializations.
- Stability & Reproducibility: Dependency management is cleaner and more reliable.


```bash  
# Navigate to the ROCK directory
cd ROCK

# Create and activate a Python 3.10 virtual environment (ROLL recommends Python 3.10)
uv venv --python 3.10 --python-preference only-managed
source .venv/bin/activate

# Install all of ROCK's dependencies using uv
uv sync --all-extras

# If using Python 3.10, starting Ray may raise a `ValueError: <object object at 0x...> is not a valid Sentinel`.
# This is due to an incompatibility between `ray` and `click` versions 8.3+.
# To fix this, downgrade `click` to a version below 8.3. This issue does not affect Python 3.11.
uv pip install 'click>=8.2,click<8.3'

# Navigate to the ROLL directory to install its dependencies
cd ../ROLL

# Install core PyTorch components
uv pip install torch==2.6.0 torchvision==0.21.0 torchaudio==2.6.0 

# Install transformer-engine. The --no-build-isolation flag prevents errors where torch cannot be found.
uv pip install transformer-engine[pytorch]==2.2.0 --no-build-isolation

# Install a pre-compiled version of flash-attention matching the specific CUDA and PyTorch versions
uv pip install https://github.com/Dao-AILab/flash-attention/releases/download/v2.7.2.post1/flash_attn-2.7.2.post1+cu12torch2.6cxx11abiFALSE-cp310-cp310-linux_x86_64.whl  

# Install the remaining dependencies
uv pip install -r requirements_torch260_vllm.txt

# (Optional) Install Tensorboard to check training metrics
uv pip install tensorboard -i $PYPI_MIRROR

# All set! Launch the training script.
bash examples/agentic_demo/run_agentic_pipeline_sokoban_sandbox_single_node.sh
```

### Option 2: Using the System Environment (Alternative)

For optimal compatibility with this method, we recommend running these commands inside one of ROLL's official base Docker images. These images come pre-installed with matching CUDA, cuDNN, and other foundational libraries.

> [ROLL's Official Docker Image List](https://alibaba.github.io/ROLL/docs/English/QuickStart/image_address)  


#### Warning
This method will install all Python packages directly into your current environment (e.g., the container's base system), which may cause conflicts with system packages or other projects.

Since ROCK cannot reuse the environment, it may need to reinstall some dependencies each time a task starts, leading to slower startup times that are dependent on network speed.


```bash  
# Install ROCK's dependencies
cd ROCK
pip install .
pip install ".[admin]"

# Install ROLL's dependencies
cd ../ROLL
pip install -r requirements_torch260_vllm.txt

# Crucial: Configure ROCK to use uv as its worker environment manager
export ROCK_WORKER_ENV_TYPE=uv

# Launch the training script
bash examples/agentic_demo/run_agentic_pipeline_sokoban_sandbox_single_node.sh
```

You have now successfully launched the Sokoban reinforcement learning training process. Happy Rock & Roll！


## 3. Multi-Node Deployment

Instead of running everything on a single machine, you can deploy the  **ROCK Service** and **ROLL job** on separate machines. This is a common client-server setup where they communicate over the network.

### 3.1  Deploy the ROCK Service on Machine A

On a dedicated machine (or container), follow the [ROCK Quick Start Guide](https://rock.io.alibaba-inc.com/docs/rock/quickstart) to deploy and start the ROCK service.

> **Important**  
> After starting the service, take note of its IP address and port (e.g., `http://192.168.1.10:8000`). You will need this address for the subsequent steps.

### 3.2 Prepare the ROLL Client on Machine B

On the other machine where you will run the training task, perform the following steps.

1. Verify Network Connectivity

First, use the curl command to check if you can reach the ROCK service on Machine A from Machine B.
```bash  
# Replace <ip_address>:<port> with the actual address of your ROCK service
# If successful, you should receive a response like {"message":"hello, ROCK!"}
curl http://<ip_address>:<port>
```

2. Prepare the ROLL Environment

```bash  
# Clone the ROLL repository
git clone https://github.com/alibaba/ROLL.git
cd ROLL

# Install dependencies
pip install -r requirements_torch260_vllm.txt
```

3. Configure the ROLL Connection Address

Modify ROLL's configuration file to point to the remote ROCK service.
- Open the configuration file: examples/agentic_demo/agentic_val_sokoban_sandbox.yaml.
- Find the "SokobanSandbox" section under "env_config".
- Update the base_url value to your ROCK service's address.
```yaml
custom_envs:
  SokobanSandbox:
    env_config:
      # Change the address here to your ROCK service's address
      # Example: base_url: 'http://192.168.1.10:8000'
      base_url: 'http://<ip_address>:<port>'
```

4. Start Training
Once configured, you can start the ROLL training script on Machine B.

```bash
# This script will now request environments from the ROCK service on Machine A over the network.
bash examples/agentic_demo/run_agentic_pipeline_sokoban_sandbox_multi_nodes.sh
```

### Advanced: Distributed ROLL Training

If you wish to deploy the ROLL training task itself in a distributed manner, you can refer to ROLL's official documentation for distributed deployment.
> [Quick Start: Multi-Node Deployment Guide](https://alibaba.github.io/ROLL/docs/QuickStart/multi_nodes_quick_start/)