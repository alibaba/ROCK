# 处理大文件和长命令输出

## `arun`
`arun()` 在 `nohup` 模式下提供了两个关键参数，帮助 Agent / 调用方在"执行"与"查看"之间按需解耦：

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

按行范围异步读取文件内容，支持自动分块读取和会话管理，支持大文件读取。

### 重要特性
- **大文件分块读取**: 自动将大文件分成多个小块进行读取
- **自动统计行数**: 未指定结束行时，自动计算文件总行数
- **内置重试机制**: 关键操作支持最多 3 次重试，提高可靠性
- **参数验证**: 自动验证输入参数的合法性
- **会话管理**: 支持指定会话或自动创建临时会话

### 参数说明
| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `file_path` | str | - | 要读取的文件路径（沙箱中的绝对路径或相对路径） |
| `start_line` | int \| None | 1 | 起始行号（从 1 开始） |
| `end_line` | int \| None | None | 结束行号（包含），默认为文件末尾 |
| `lines_per_request` | int | 1000 | 每次请求读取的行数，范围 1-10000 |

### 返回值
- `ReadFileResponse`: 包含文件内容的响应对象
  - `content` (str): 读取的文件内容

### 异常说明
- `Exception`: 当 `start_line < 1` 时抛出
- `Exception`: 当 `end_line < start_line` 时抛出
- `Exception`: 当 `lines_per_request` 不在 1-10000 范围内时抛出
- `Exception`: 当文件读取失败时抛出

### 使用示例

```python
# 读取整个文件
response = await sandbox.read_file_by_line_range("/path/to/file.txt")

# 读取指定行范围（第 100 到 500 行）
response = await sandbox.read_file_by_line_range(
    "/path/to/file.txt",
    start_line=100,
    end_line=500
)

# 从第 1990 行读取到文件末尾
response = await sandbox.read_file_by_line_range(
    "/path/to/file.txt",
    start_line=1990
)

# 使用自定义分块大小
response = await sandbox.read_file_by_line_range(
    "/path/to/file.txt",
    lines_per_request=5000
)
```

### 注意事项
- 行号从 1 开始计数，而非 0
- 对于大文件建议适当增加 `lines_per_request` 以提高效率
- 文件路径必须是沙箱内的有效路径
- 使用 `sed` 命令进行文件读取，确保沙箱镜像支持该命令
