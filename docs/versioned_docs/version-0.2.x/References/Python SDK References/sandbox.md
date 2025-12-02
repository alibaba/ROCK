# Sandbox SDK Reference

## `arun`

The `arun()` method now supports a new parameter: `response_limited_bytes_in_nohup` (integer type), which resolves request timeout issues caused by excessively long responses.  
This parameter limits the number of characters returned in the output when running in `nohup` mode. The default value is `None`, meaning no limit is applied.

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

# The returned response will contain at most 1024 characters
resp = asyncio.run(
    sandbox.arun(
        cmd="cat /tmp/test.txt",
        mode="nohup",
        session="bash-1",
    )
)
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