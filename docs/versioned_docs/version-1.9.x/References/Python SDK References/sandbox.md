# Handling Large Files and Long Command Outputs

## `arun`

`arun()` provides two knobs to control how `nohup` output is handled:

1. **`response_limited_bytes_in_nohup`** *(integer type)*
   Caps the number of characters returned from the nohup output file. Useful when you still need to stream some logs back but want an upper bound (default `None` = no cap).

2. **`ignore_output`** *(bool, default `False`)*
   When set to `True`, `arun()` skips reading the nohup output file entirely. The command still runs to completion and writes logs to `/tmp/tmp_<timestamp>.out`, but the SDK immediately returns a lightweight hint telling agents where to fetch the logs later (via `read_file`, download APIs, or custom commands). This fully decouples "execute command" from "inspect logs". The response also includes the **file size** to help users decide whether to download directly or read in chunks.

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

# Example 1: limit the returned logs to 1024 characters
resp_limited = asyncio.run(
    sandbox.arun(
        cmd="cat /tmp/test.txt",
        mode="nohup",
        session="bash-1",
        response_limited_bytes_in_nohup=1024,
    )
)

# Example 2: skip collecting logs; agent will download/read them later
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
# Use Sandbox.read_file(...), download APIs, or run 'cat /tmp/tmp_xxx.out' ...
```

## `read_file_by_line_range`

Asynchronously reads file content by line range, with built-in support for automatic chunking and session management. Supports large file reading.

### Key Features
- **Chunked reading for large files**: Automatically splits large files into chunks
- **Automatic line count**: Estimates total lines when end_line is not specified
- **Built-in retry mechanism**: Up to 3 retries for critical operations
- **Input validation**: Validates input parameters automatically
- **Session management**: Supports custom session or auto-created temporary session

### Parameters
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `file_path` | str | - | File path to read (absolute or relative path in sandbox) |
| `start_line` | int \| None | 1 | Starting line number (1-based) |
| `end_line` | int \| None | None | Ending line number (inclusive), defaults to file end |
| `lines_per_request` | int | 1000 | Lines per request, range 1-10000 |

### Return Value
- `ReadFileResponse`: Response object containing file content
  - `content` (str): The file content read

### Exception Handling
- `Exception`: Raised when `start_line < 1`
- `Exception`: Raised when `end_line < start_line`
- `Exception`: Raised when `lines_per_request` is not in range 1-10000
- `Exception`: Raised when file reading fails

### Usage Examples

```python
# Read the entire file
response = await sandbox.read_file_by_line_range("/path/to/file.txt")

# Read a specific line range (lines 100 to 500)
response = await sandbox.read_file_by_line_range(
    "/path/to/file.txt",
    start_line=100,
    end_line=500
)

# Read from line 1990 to the end of file
response = await sandbox.read_file_by_line_range(
    "/path/to/file.txt",
    start_line=1990
)

# Use custom chunk size
response = await sandbox.read_file_by_line_range(
    "/path/to/file.txt",
    lines_per_request=5000
)
```

### Notes
- Line numbers are 1-based, not 0-based
- For large files, consider increasing `lines_per_request` for better efficiency
- File path must be a valid path within the sandbox
- Uses `sed` command for file reading; ensure the sandbox image supports this command
