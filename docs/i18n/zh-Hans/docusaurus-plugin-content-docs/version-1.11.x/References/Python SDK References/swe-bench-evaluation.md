# SWE-Bench 评测

本文档介绍如何使用 ROCK SDK 运行 SWE-Bench Verified 评测，包括沙箱启动、Agent 集成、测试环境准备和结果解析。

### 快速开始
SWE-Bench-Verified 是一个用于评估 AI 编程 Agent 在真实软件工程任务上表现的基准测试。

在ROCK上运行一个SWE-Bench任务包含以下步骤：

1. **load_task_config** — 加载 `task.yaml` 获取任务指令
2. **start_sandbox** — 使用任务专属的 Docker 镜像启动沙箱
3. **agent.install / agent.run** — 安装并运行 Agent 来解决任务
4. **setup_test_env** — 上传测试文件和运行测试脚本到沙箱
5. **运行测试** — 通过 `sandbox.arun()` 执行测试脚本，支持超时控制
6. **parse_swebench_result** — 解析测试输出，判断 PASSED / FAILED
7. **sandbox.stop** — 清理沙箱资源

**下面是示例代码** 

```python
import asyncio
from pathlib import Path

from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig

async def main():
    task_name = "django__django-14539"
    task_dir = Path("/root/terminal-bench-datasets/datasets/swebench-verified") / task_name
    agent_config_path = "/path/to/iflow_config.yaml"

    # 1. 加载任务指令
    task_config = await load_task_config(task_dir)           # 参见 load_task_config 章节
    instruction = task_config["instruction"]

    # 2. 启动沙箱
    sandbox = await start_sandbox(task_name)                 # 参见 start_sandbox 章节

    try:
        # 3. 安装并运行 Agent
        await sandbox.agent.install(config=agent_config_path)
        result = await sandbox.agent.run(instruction)

        # 4. 准备测试环境
        await setup_test_env(sandbox, task_dir)              # 参见 setup_test_env 章节

        # 5. 运行测试
        resp = await run_tests(sandbox)                      # 参见"运行测试"章节

        # 6. 解析结果
        is_resolved = parse_swebench_result(resp.output)     # 参见 parse_swebench_result 章节
        print(f"Task {task_name} resolved: {is_resolved}")
    finally:
        await sandbox.stop()

asyncio.run(main())
```

以下章节详细介绍评测流程中使用的各个函数。

---

## start_sandbox

使用任务专属的 SWE-Bench Docker 镜像启动沙箱实例。每个任务都有一个预构建的镜像，包含目标仓库和运行环境。

`image` 参数格式如下：

```
slimshetty/swebench-verified:sweb.eval.x86_64.{task_name}
```

例如，任务 `django__django-14539` 对应的镜像为：

```
slimshetty/swebench-verified:sweb.eval.x86_64.django__django-14539
```

```python
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig

async def start_sandbox(task_name: str) -> Sandbox:
    image = f"slimshetty/swebench-verified:sweb.eval.x86_64.{task_name}"
    config = SandboxConfig(image=image)
    sandbox = Sandbox(config)
    await sandbox.start()
    return sandbox
```

## load_task_config

从任务目录中加载 `task.yaml` 配置文件。YAML 文件包含 `instruction` 字段，用于描述 Agent 需要完成的编程任务。

```python
import yaml
from pathlib import Path

async def load_task_config(task_dir: Path) -> dict:
    task_yaml_path = task_dir / "task.yaml"
    if not task_yaml_path.exists():
        raise FileNotFoundError(f"task.yaml not found in {task_dir}")

    with open(task_yaml_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config

# 使用示例
task_config = await load_task_config(task_dir)
instruction = task_config["instruction"]
```

## agent.install / agent.run

使用 `sandbox.agent.install()` 和 `sandbox.agent.run()` 在沙箱中部署和执行 Agent。详细的 Agent 配置请参考 [Rock Agent](./rock-agent.md)。

```python
# 使用 YAML 配置文件安装 Agent（以 iflow_config.yaml 为例）
await sandbox.agent.install(config="iflow_config.yaml")

# 使用任务指令运行 Agent
result = await sandbox.agent.run(instruction)
```

## setup_test_env

在沙箱中准备测试环境：安装 [uv](https://github.com/astral-sh/uv) 包管理器，并上传测试文件和运行测试脚本。

```python
from pathlib import Path

from rock.actions.sandbox.request import CreateBashSessionRequest
from rock.sdk.sandbox.client import RunMode, Sandbox

async def setup_test_env(sandbox: Sandbox, task_dir: Path) -> str:
    """准备测试环境并返回会话名称。"""
    # 1. 创建带有自定义环境变量的会话
    session_name = "swe-evaluation"
    await sandbox.create_session(
        CreateBashSessionRequest(
            session=session_name,
            env_enable=True,
            env={
                "UV_PYTHON_INSTALL_MIRROR": "https://registry.npmmirror.com/-/binary/python-build-standalone"
            },
        )
    )

    # 2. 安装 uv
    for cmd in [
        "wget https://github.com/astral-sh/uv/releases/download/0.10.5/uv-x86_64-unknown-linux-gnu.tar.gz",
        "tar -xzf uv-x86_64-unknown-linux-gnu.tar.gz --strip-components=1 -C /usr/local/bin",
    ]:
        await sandbox.arun(cmd, session=session_name, mode=RunMode.NOHUP)

    # 3. 上传测试文件
    sandbox_test_dir = "/tests"
    result = await sandbox.fs.upload_dir(task_dir / "tests", sandbox_test_dir)
    if result.exit_code != 0:
        raise RuntimeError("Failed to upload test files")

    # 4. 上传运行测试脚本
    run_tests_script = task_dir / "run-tests.sh"
    result = await sandbox.upload_by_path(
        run_tests_script,
        f"{sandbox_test_dir}/{run_tests_script.name}",
    )
    if not result.success:
        raise RuntimeError("Failed to upload run-tests script")

    return session_name
```

## 运行测试

使用 `RunMode.NOHUP` 模式执行测试脚本，支持可配置的超时时间。

```python
import shlex
from rock.actions.sandbox.response import Observation
from rock.sdk.sandbox.client import RunMode

test_timeout_sec = 3600
sandbox_test_dir = "/tests"

session_name = "swe-evaluation"

run_tests_command = f"sh -c 'bash {sandbox_test_dir}/run-tests.sh'"
resp: Observation = await sandbox.arun(
    run_tests_command,
    session=session_name,
    mode=RunMode.NOHUP,
    wait_timeout=test_timeout_sec,
)
```

## parse_swebench_result

解析测试输出以判断 SWE-Bench 任务是否通过。解析器会查找由标记行分隔的结果块，并检查是否包含 `PASSED`。

```python
import re

def parse_swebench_result(output: str) -> bool:
    """解析 SWE-Bench 测试输出，判断任务是否通过。

    匹配 'SWEBench results starts here' 和
    'SWEBench results ends here' 之间的内容块，
    然后检查其中是否包含 'PASSED'。
    """
    match = re.search(
        r"SWEBench results starts here\s*(.*?)\s*SWEBench results ends here",
        output,
        re.DOTALL,
    )
    if not match:
        return False
    return match.group(1).strip() == "PASSED"

# 使用示例
is_resolved = parse_swebench_result(resp.output)
```

## 注意事项

- **任务数据集**：任务目录（包含 `task.yaml`、`tests/` 和 `run-tests.sh`）可从 [terminal-bench-datasets](https://github.com/laude-institute/terminal-bench-datasets) 仓库获取。
- **任务镜像**：每个 SWE-Bench 任务需要特定的 Docker 镜像（如 `sweb.eval.x86_64.<task_name>`）。请确保镜像在对应的环境中可用。
- **Agent 配置**：Agent 配置 YAML 定义了运行时、依赖和执行命令。详情请参考 [Rock Agent](./rock-agent.md)。
