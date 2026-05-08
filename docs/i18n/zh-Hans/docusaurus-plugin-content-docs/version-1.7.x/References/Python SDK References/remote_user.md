# Remote User

远程用户管理，用于在沙箱中创建和管理用户。

## 使用示例

```python
import asyncio
from rock.sdk.sandbox.config import SandboxConfig
from rock.sdk.sandbox.client import Sandbox

from rock.actions import Action, CreateBashSessionRequest, Observation

async def test_remote_user():
    config = SandboxConfig(
        image='hub.docker.alibaba-inc.com/chatos/python:3.11',
        xrl_authorization='xxx',
        cluster='nt-c'
    )
    sandbox = Sandbox(config)
    await sandbox.start()

    await sandbox.remote_user.create_remote_user('rock')
    assert await sandbox.remote_user.is_user_exist('rock')
    print('test remote user success')

async def test_create_session_with_remote_user():
    config = SandboxConfig(
        image='hub.docker.alibaba-inc.com/chatos/python:3.11',
        xrl_authorization='xxx',
        cluster='nt-c'
    )
    sandbox = Sandbox(config)
    await sandbox.start()

    await sandbox.remote_user.create_remote_user('rock')
    assert await sandbox.remote_user.is_user_exist('rock')

    await sandbox.create_session(CreateBashSessionRequest(remote_user="rock", session="bash"))

    observation: Observation = await sandbox.run_in_session(
        action=Action(session="bash", command="whoami")
    )
    print(observation)
    assert observation.output.strip() == "rock"
    print('test create session with remote user success')

if __name__ == '__main__':
    asyncio.run(test_remote_user())
    asyncio.run(test_create_session_with_remote_user())
```

## API

### create_remote_user(username)

创建远程用户。

```python
await sandbox.remote_user.create_remote_user('username')
```

### is_user_exist(username)

检查用户是否存在。

```python
exists = await sandbox.remote_user.is_user_exist('username')
```
