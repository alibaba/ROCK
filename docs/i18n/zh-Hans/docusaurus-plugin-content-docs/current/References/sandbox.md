# Sandbox SDK 参考

## `arun`
`arun()` 方法新增 `response_limited_bytes_in_nohup` 参数(int型)，解决response过长导致的请求超时问题。
该参数用于限制 nohup 模式下返回的 output 字符数，默认值为 `None`，表示不限制。

```python
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig
from rock.sdk.sandbox.request import CreateBashSessionRequest

config = SandboxConfig(
            image=f"{image}",
            xrl_authorization=f"{xrl_authorization}",
            user_id=f"{user_id}",
            cluster=f"{cluster}",
        )
sandbox = Sandbox(config)

session = sandbox.create_session(
    CreateBashSessionRequest(
        session="bash-1",
        response_limited_bytes_in_nohup=1024
    )
)

# 返回的数据最多只有1024个字符
resp = asyncio.run(
    sandbox.arun(
        cmd="cat /tmp/test.txt",
        mode="nohup",
        session="bash-1",
    )
)
```

## `read_file_by_line_range`
功能说明: 按行范围异步读取文件内容，支持自动分块读取和会话管理。主要特性包括大文件自动分块读取、自动统计文件总行数、内置重试机制（3次重试）、参数验证。以下是使用示例:
```python
# 读取整个文件
response = await read_file_by_line_range("example.txt")

# 读取指定行范围
response = await read_file_by_line_range("example.txt", start_line=1, end_line=2000)
```