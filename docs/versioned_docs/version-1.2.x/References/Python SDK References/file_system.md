# FileSystem

File system interface for sandbox environment operations including permission and ownership management.

## chown - Change Owner

```python
from rock.actions.sandbox.request import ChownRequest

# Create remote user before changing ownership
await sandbox.remote_user.create_remote_user("deploy")

# Get current working directory
pwd_response = await sandbox.execute(Command(command=["pwd"]))
pwd = pwd_response.stdout.strip()

# Change directory owner
await sandbox.fs.chown(
    ChownRequest(
        paths=[pwd],
        remote_user="deploy",
        recursive=False,
    )
)

# Recursively change owner for directory and contents
await sandbox.fs.chown(
    ChownRequest(
        paths=["/home/user/project"],
        remote_user="deploy",
        recursive=True,
    )
)
```

## chmod - Change Permissions

```python
from rock.actions.sandbox.request import ChmodRequest

# Create test directory
await sandbox.execute(Command(command=["mkdir", "-p", "/tmp/app"]))

# Change directory permissions
await sandbox.fs.chmod(
    ChmodRequest(
        paths=["/tmp/app"],
        mode="755",
        recursive=False,
    )
)

# Recursively change permissions (includes subdirectories and files)
await sandbox.fs.chmod(
    ChmodRequest(
        paths=["/tmp/app"],
        mode="644",
        recursive=True,
    )
)

# Set maximum permissions
await sandbox.fs.chmod(
    ChmodRequest(
        paths=["/tmp/shared"],
        mode="777",
        recursive=True,
    )
)
```

## upload_dir - Upload Directory

```python
import os
from pathlib import Path

# Prepare local directory
local_dir = Path("/Users/foo/my-project")
(local_dir / "config.json").write_text('{"key": "value"}')
(local_dir / "app.py").write_text("print('hello')")

# Upload to sandbox
result = await sandbox.fs.upload_dir(
    source_dir=str(local_dir),
    target_dir="/root/project",
    extract_timeout=600,
)

if result.exit_code == 0:
    print(f"Upload success: {result.output}")
else:
    print(f"Upload failed: {result.failure_reason}")
```
