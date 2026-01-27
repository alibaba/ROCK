# RuntimeEnv SDK Reference

RuntimeEnv 模块用于在沙箱中管理语言运行时环境（目前提供了 Python / Node.js）。

## 快速开始(使用示例)

```python
from rock.sdk.sandbox import Sandbox
from rock.sdk.sandbox.config import SandboxConfig
from rock.sdk.sandbox.runtime_env import RuntimeEnv, NodeRuntimeEnvConfig

sandbox_config = SandboxConfig()
sandbox = Sandbox()
await sandbox.start()

node_runtime_env_config = NodeRuntimeEnvConfig(version="default")
env = await RuntimeEnv.create(sandbox, node_runtime_env_config)

await env.run("node --version")
```

## RuntimeEnv.create

异步工厂方法，根据配置创建 RuntimeEnv 实例并初始化，自动注册到 `sandbox.runtime_envs`。

```python
from rock.sdk.sandbox.runtime_env import RuntimeEnv, NodeRuntimeEnvConfig

env = await RuntimeEnv.create(
    sandbox,
    NodeRuntimeEnvConfig(version="22.18.0"),
)

# 自动注册，可通过 sandbox.runtime_envs[env.runtime_env_id] 访问
print(env.runtime_env_id in sandbox.runtime_envs)  # True
```

## wrapped_cmd

包装命令，将 `bin_dir` 加入 PATH，确保优先使用运行时环境中的可执行文件。

```python
# 默认 prepend=True，bin_dir 优先于系统 PATH
wrapped = env.wrapped_cmd("node script.js")
# 返回: bash -c 'export PATH=/tmp/rock-runtime-envs/node/22.18.0/xxx/runtime-env/bin:$PATH && node script.js'

```

## run

在运行时环境中执行命令。内部基于 `wrapped_cmd` 实现，自动将 `bin_dir` 加入 PATH。

```python
await env.run("node script.js")
await env.run("npm install express")

# 指定超时时间
await env.run("npm install some-big-package", wait_timeout=1200)
```

## PythonRuntimeEnvConfig

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `type` | `Literal["python"]` | `"python"` | 类型标识 |
| `version` | `"3.11" \| "3.12" \| "default"` | `"default"` | Python 版本，默认 3.11 |
| `pip` | `list[str] \| str \| None` | `None` | pip 包列表或 requirements.txt 路径 |
| `pip_index_url` | `str \| None` | 环境变量 | pip 镜像源 |

## NodeRuntimeEnvConfig

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `type` | `Literal["node"]` | `"node"` | 类型标识 |
| `version` | `"22.18.0" \| "default"` | `"default"` | Node 版本，默认 22.18.0 |
| `npm_registry` | `str \| None` | `None` | npm 镜像源 |

## 自定义 RuntimeEnv 实现约束

自定义 RuntimeEnv 需遵循以下规则：

1. **定义 `runtime_env_type` 类属性**：作为类型标识符，用于自动注册到 RuntimeEnv 工厂
2. **重写 `_get_install_cmd()`**：返回安装命令
3. **安装命令最后必须**：将目录重命名为 `runtime-env`


## NodeRuntimeEnv 简化版实现示例

```python
from rock.sdk.sandbox.runtime_env import RuntimeEnv, RuntimeEnvConfig
from typing import Literal
from pydantic import Field
from typing_extensions import override

# Config class: defines configuration type, used by RuntimeEnv.create() for routing
class NodeRuntimeEnvConfig(RuntimeEnvConfig):
    type: Literal["node"] = "node"  # Must match runtime_env_type

# RuntimeEnv implementation: defines how to install and run this runtime
class NodeRuntimeEnv(RuntimeEnv):
    runtime_env_type = "node"  # Auto-registered to RuntimeEnv._REGISTRY

    @override
    def _get_install_cmd(self) -> str:
        # Download Node binary and extract, rename to runtime-env
        return (
            "wget -q -O node.tar.xz https://npmmirror.com/mirrors/node/v22.18.0/node-v22.18.0-linux-x64.tar.xz && "
            "tar -xf node.tar.xz && "
            "mv node-v22.18.0-linux-x64 runtime-env"
        )
```
