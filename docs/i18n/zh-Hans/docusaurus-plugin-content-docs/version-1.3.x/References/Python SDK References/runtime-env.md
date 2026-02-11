# RuntimeEnv

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
wrapped = env.wrapped_cmd("node script.js")
# 返回: bash -c 'export PATH=/tmp/rock-runtime-envs/node/22.18.0/xxx/runtime-env/bin:$PATH && node script.js'
```

## run

在运行时环境中执行命令。内部基于 `wrapped_cmd` 实现

```python
await env.run("node script.js")
await env.run("npm install express")
```

## PythonRuntimeEnvConfig

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `type` | `Literal["python"]` | `"python"` | 类型标识 |
| `version` | `"3.11" \| "3.12" \| "default"` | `"default"` | Python 版本，默认 3.11 |
| `pip` | `list[str] \| str \| None` | `None` | pip 包列表或 requirements.txt 路径 |
| `pip_index_url` | `str \| None` | 环境变量 | pip 镜像源 |
| `extra_symlink_dir` | `str \| None` | `None` | 符号链接的目标目录 |
| `extra_symlink_executables` | `list[str]` | `["python", "python3", "pip", "pip3"]` | 要创建符号链接的可执行文件列表 |

## NodeRuntimeEnvConfig

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `type` | `Literal["node"]` | `"node"` | 类型标识 |
| `version` | `"22.18.0" \| "default"` | `"default"` | Node 版本，默认 22.18.0 |
| `npm_registry` | `str \| None` | `None` | npm 镜像源 |
| `extra_symlink_dir` | `str \| None` | `None` | 符号链接的目标目录 |
| `extra_symlink_executables` | `list[str]` | `["node", "npm", "npx"]` | 要创建符号链接的可执行文件列表 |

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

# Config 类：定义配置类型，用于 RuntimeEnv.create() 路由到对应实现
class NodeRuntimeEnvConfig(RuntimeEnvConfig):
    type: Literal["node"] = "node"  # 必须与 runtime_env_type 一致

# RuntimeEnv 实现类：定义如何安装和运行该运行时环境
class NodeRuntimeEnv(RuntimeEnv):
    runtime_env_type = "node"  # 自动注册到 RuntimeEnv._REGISTRY

    @override
    def _get_install_cmd(self) -> str:
        # 下载 Node 二进制包并解压，最后重命名为 runtime-env
        return (
            "wget -q -O node.tar.xz https://npmmirror.com/mirrors/node/v22.18.0/node-v22.18.0-linux-x64.tar.xz && "
            "tar -xf node.tar.xz && "
            "mv node-v22.18.0-linux-x64 runtime-env"
        )
```

## 加速基础环境安装

`PythonRuntimeEnv` 默认从 https://github.com/astral-sh/python-build-standalone/releases/ 下载 Python 安装包。若网络不可达或下载较慢，可通过环境变量 `ROCK_RTENV_PYTHON_V31114_INSTALL_CMD` 或 `ROCK_RTENV_PYTHON_V31212_INSTALL_CMD` 覆盖默认安装命令（例如切换到内网源/镜像源）。

默认值示例：

```python
"ROCK_RTENV_PYTHON_V31114_INSTALL_CMD": lambda: os.getenv(
    "ROCK_RTENV_PYTHON_V31114_INSTALL_CMD",
    "[ -f cpython31114.tar.gz ] && rm cpython31114.tar.gz; [ -d python ] && rm -rf python; "
    "wget -q -O cpython31114.tar.gz https://github.com/astral-sh/python-build-standalone/releases/download/20251120/cpython-3.11.14+20251120-x86_64-unknown-linux-gnu-install_only.tar.gz "
    "&& tar -xzf cpython31114.tar.gz && mv python runtime-env",
),
```

例如，替换为镜像源下载：

```bash
export ROCK_RTENV_PYTHON_V31114_INSTALL_CMD='[ -f cpython31114.tar.gz ] && rm cpython31114.tar.gz; [ -d python ] && rm -rf python; wget -q -O cpython31114.tar.gz https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone/20251209/cpython-3.11.14+20251209-x86_64-unknown-linux-gnu-install_only.tar.gz && tar -xzf cpython31114.tar.gz && mv python runtime-env'
```

请确保该命令执行完成后，会在 `runtime_env` 的默认工作目录下生成 `runtime-env` 目录，并且 `${workdir}/runtime-env/bin/` 下包含对应可执行文件，例如：

- `${workdir}/runtime-env/bin/python`

Node 环境同理，可通过修改环境变量 `ROCK_RTENV_NODE_V22180_INSTALL_CMD` 来指定更快的下载/安装命令。