# Sandbox SDK Reference

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

**Description**: Asynchronously reads file content by line range, with built-in support for automatic chunking and session management. Key features include:
- Automatic chunked reading for large files  
- Automatic total line count estimation  
- Built-in retry mechanism (3 retries by default)  
- Input parameter validation  

### Usage Examples:

```python
# Read the entire file
response = await read_file_by_line_range("example.txt")

# Read a specific line range (1-based, inclusive)
response = await read_file_by_line_range("example.txt", start_line=1, end_line=2000)
```