# Sandbox SDK 参考

## `arun`
`arun()` 在 `nohup` 模式下提供了两个关键参数，帮助 Agent / 调用方在“执行”与“查看”之间按需解耦：

1. **`response_limited_bytes_in_nohup`**（int 型）  
   限制返回内容的最大字符数（例如 `64 * 1024`），适合仍需立刻查看部分日志、但必须控制带宽的场景。默认值 `None` 表示不加限制。

2. **`ignore_output`**（bool，默认 `False`）  
   当设为 `True` 时，`arun()` 不再读取 nohup 输出文件，而是在命令执行完毕后立即返回一段提示信息（包含输出文件路径、**文件大小**及查看方式）。日志仍写入 `/tmp/tmp_<timestamp>.out`，后续可通过 `read_file`、下载接口或自定义命令按需读取，实现"执行"与"查看"彻底解耦。返回的文件大小信息可帮助用户决定是直接下载还是分块读取。

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

session = sandbox.create_session(CreateBashSessionRequest(session="bash-1"))

# 示例 1：限制最多 1024 个字符
resp_limit = asyncio.run(
    sandbox.arun(
        cmd="cat /tmp/test.txt",
        mode="nohup",
        session="bash-1",
        response_limited_bytes_in_nohup=1024,
    )
)

# 示例 2：完全跳过日志读取，后续再通过 read_file / 下载获取
resp_detached = asyncio.run(
    sandbox.arun(
        cmd="bash run_long_job.sh",
        mode="nohup",
        session="bash-1",
        ignore_output=True,
    )
)
print(resp_detached.output)
# Command executed in nohup mode without streaming the log content.
# Status: completed
# Output file: /tmp/tmp_xxx.out
# File size: 15.23 MB
# 可通过 Sandbox.read_file(...) / 下载接口 / cat /tmp/tmp_xxx.out 查看日志
```

## `read_file_by_line_range`
功能说明: 按行范围异步读取文件内容，支持自动分块读取和会话管理。主要特性包括大文件自动分块读取、自动统计文件总行数、内置重试机制（3次重试）、参数验证。以下是使用示例:
```python
# 读取整个文件
response = await read_file_by_line_range("example.txt")

# 读取指定行范围
response = await read_file_by_line_range("example.txt", start_line=1, end_line=2000)
```