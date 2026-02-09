# FileSystem

文件系统操作接口，提供沙箱环境中的权限管理和目录上传功能。

## chown - 修改所有者

```python
from rock.actions.sandbox.request import ChownRequest

# 创建远程用户后修改所有者
await sandbox.remote_user.create_remote_user("deploy")

# 获取当前目录
pwd_response = await sandbox.execute(Command(command=["pwd"]))
pwd = pwd_response.stdout.strip()

# 修改目录所有者
await sandbox.fs.chown(
    ChownRequest(
        paths=[pwd],
        remote_user="deploy",
        recursive=False,
    )
)

# 递归修改目录及其内容所有者
await sandbox.fs.chown(
    ChownRequest(
        paths=["/home/user/project"],
        remote_user="deploy",
        recursive=True,
    )
)
```

## chmod - 修改权限

```python
from rock.actions.sandbox.request import ChmodRequest

# 创建测试目录
await sandbox.execute(Command(command=["mkdir", "-p", "/tmp/app"]))

# 修改目录权限
await sandbox.fs.chmod(
    ChmodRequest(
        paths=["/tmp/app"],
        mode="755",
        recursive=False,
    )
)

# 递归修改权限（包括子目录和文件）
await sandbox.fs.chmod(
    ChmodRequest(
        paths=["/tmp/app"],
        mode="644",
        recursive=True,
    )
)

# 设置最高权限
await sandbox.fs.chmod(
    ChmodRequest(
        paths=["/tmp/shared"],
        mode="777",
        recursive=True,
    )
)
```

## upload_dir - 上传目录

```python
import os
from pathlib import Path

# 准备本地目录
local_dir = Path("/Users/foo/my-project")
(local_dir / "config.json").write_text('{"key": "value"}')
(local_dir / "app.py").write_text("print('hello')")

# 上传到沙箱
result = await sandbox.fs.upload_dir(
    source_dir=str(local_dir),
    target_dir="/root/project",
    extract_timeout=600,
)

if result.exit_code == 0:
    print(f"上传成功: {result.output}")
else:
    print(f"上传失败: {result.failure_reason}")
```
