---
sidebar_position: 9
---

# 将沙箱提交为镜像

ROCK 可以将运行中的沙箱容器提交为 Docker 镜像，并将镜像推送到 Registry。对于体积较大的沙箱，`docker commit` 和 `docker push` 可能耗时较长，因此异步接口会立即返回。调用方使用 sandbox ID 轮询状态，直到任务进入终态。

异步流程如下：

```text
POST /commit          -> RUNNING
GET /commit/{id}      -> RUNNING
GET /commit/{id}      -> SUCCEEDED 或 FAILED
```

```bash
export ROCK_API_URL="https://xrl.alibaba-inc.com/apis/envs/sandbox/v1"
```

## 使用前提

- Registry 账号必须具有目标镜像的推送权限，且沙箱网络环境能够访问。

## HTTP API

### 发起 commit

```text
POST $ROCK_API_URL/commit
Content-Type: application/json
```

请求参数：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `sandbox_id` | string | 是 | sandbox ID，同时也是 Docker 容器名。 |
| `image_tag` | string | 是 | 完整的目标镜像名称，例如 `registry.example.com/team/app:v1`。 |
| `username` | string | 是 | Registry 用户名。 |
| `password` | string | 是 | Registry 密码。直接传原始密码，无需进行 Base64 编码。 |

```bash
curl -sS -X POST "$ROCK_API_URL/commit" \
  -H "Content-Type: application/json" \
  -d '{
    "sandbox_id": "sandbox-123",
    "image_tag": "registry.example.com/team/app:v1",
    "username": "registry-user",
    "password": "registry-password"
  }'
```

任务成功受理后返回 `RUNNING`：

```json
{
  "status": "Success",
  "message": null,
  "error": null,
  "result": {
    "sandbox_id": "sandbox-123",
    "image_tag": "registry.example.com/team/app:v1",
    "phase": "RUNNING",
    "started_at": "2026-07-22T10:30:00+08:00",
    "completed_at": null,
    "exit_code": null,
    "failed_stage": null,
    "error_code": null,
    "error_message": null
  }
}
```

### 查询 commit 状态

```text
GET $ROCK_API_URL/commit/{sandbox_id}
```

```bash
curl -sS "$ROCK_API_URL/commit/sandbox-123"
```

建议每隔几秒轮询一次，直到 `result.phase` 变为 `SUCCEEDED` 或 `FAILED`。成功结果示例：

```json
{
  "status": "Success",
  "result": {
    "sandbox_id": "sandbox-123",
    "image_tag": "registry.example.com/team/app:v1",
    "phase": "SUCCEEDED",
    "started_at": "2026-07-22T10:30:00+08:00",
    "completed_at": "2026-07-22T10:36:42+08:00",
    "exit_code": 0,
    "failed_stage": null,
    "error_code": null,
    "error_message": null
  }
}
```

如果后台操作失败，请求本身仍然成功，但任务状态为 `FAILED`：

```json
{
  "status": "Success",
  "result": {
    "sandbox_id": "sandbox-123",
    "image_tag": "registry.example.com/team/app:v1",
    "phase": "FAILED",
    "started_at": "2026-07-22T10:30:00+08:00",
    "completed_at": "2026-07-22T10:30:03+08:00",
    "exit_code": 11,
    "failed_stage": "login",
    "error_code": "LOGIN_FAILED",
    "error_message": "Error response from daemon: ..."
  }
}
```

### 状态字段

| 字段 | 说明 |
|---|---|
| `phase` | `RUNNING`、`SUCCEEDED` 或 `FAILED`。 |
| `started_at` | worker 接受任务的时间。 |
| `completed_at` | 任务结束时间，仅终态任务存在。 |
| `exit_code` | 成功时为 `0`，失败时为非零值或 `-1`。 |
| `failed_stage` | 失败步骤，例如 `login`、`commit`、`push` 或 `supervisor`。 |
| `error_code` | 供程序判断的错误码。 |
| `error_message` | 供排障使用的可读日志末尾内容。 |

常见任务错误包括 `LOGIN_FAILED`、`COMMIT_FAILED`、`PUSH_FAILED`、`TIMEOUT` 和 `PROCESS_LOST`。`SANDBOX_CONTAINER_NOT_FOUND`、`COMMIT_CONFLICT`、`STATUS_NOT_FOUND`、`WORKER_UNREACHABLE` 等错误属于请求级失败，顶层 `status` 会返回 `Failed`。

### 重复请求

- 同一个 sandbox 正在提交相同的 `image_tag` 时，再次调用 `POST /commit` 会返回已有的 `RUNNING` 任务，不会启动第二个进程。
- 同一个 sandbox 正在提交不同的 `image_tag` 时，请求会以 `COMMIT_CONFLICT` 失败。
- 任务进入 `SUCCEEDED` 或 `FAILED` 后，再次调用 `POST /commit` 会启动新任务，并覆盖该 sandbox 的上一次状态。

## Python SDK

当调用方需要等待最终结果时，使用 `commit()`。该方法内部调用 `commit_async()`，并轮询 `get_commit_status()`，直到任务进入 `SUCCEEDED` 或 `FAILED`。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `timeout` | `180` 秒 | 发起任务和轮询状态的总超时时间，超时后抛出 `TimeoutError`。 |
| `interval` | `2` 秒 | 两次状态查询之间的等待时间。 |

超时只会停止 SDK 端的等待，不会取消 worker 上已经运行的 commit 任务。之后仍可调用 `get_commit_status()` 查询该任务。

调用前，`Sandbox` 对象应已启动。

```python
from rock.actions import CommitPhase


async def commit_and_wait(sandbox):
    status = await sandbox.commit(
        image_tag="registry.example.com/team/app:v1",
        username="registry-user",
        password="registry-password",
        timeout=180,
        interval=2,
    )
    if status is None:
        raise RuntimeError("sandbox_id 未设置")

    if status.phase == CommitPhase.FAILED:
        raise RuntimeError(
            f"commit 失败: code={status.error_code}, "
            f"stage={status.failed_stage}, message={status.error_message}"
        )

    print(f"镜像推送成功: {status.image_tag}")
```

当调用方不希望等待，或需要自行控制轮询逻辑时，可直接使用 `commit_async()`。该方法返回任务的初始状态，后续状态通过 `get_commit_status()` 查询。

## TypeScript SDK

使用 `commit()` 发起任务并等待最终状态。该方法内部调用 `commitAsync()`，并轮询 `getCommitStatus()`。`timeout` 和 `interval` 参数的单位为秒，默认值分别为 `180` 和 `2`。

```typescript
import { CommitPhase } from 'rl-rock';

async function commitAndWait(sandbox) {
  const status = await sandbox.commit(
    'registry.example.com/team/app:v1',
    'registry-user',
    'registry-password',
    180,
    2
  );
  if (!status) {
    throw new Error('sandbox_id 未设置');
  }

  if (status.phase === CommitPhase.FAILED) {
    throw new Error(
      `commit 失败: code=${status.errorCode}, ` +
      `stage=${status.failedStage}, message=${status.errorMessage}`
    );
  }

  console.log(`镜像推送成功: ${status.imageTag}`);
}
```

超时只会停止 SDK 端的等待，不会取消 worker 上已经运行的任务。调用方需要自行控制轮询时可使用 `commitAsync()`，之后通过 `getCommitStatus()` 查询任务状态。
