---
sidebar_position: 9
---

# Commit a Sandbox to an Image

ROCK can commit a running sandbox container to a Docker image and push the image to a registry. Because `docker commit` and `docker push` can take a long time for a large sandbox, the asynchronous API returns immediately. Poll the status with the sandbox ID until the task reaches a terminal phase.

The asynchronous workflow is:

```text
POST /commit          -> RUNNING
GET /commit/{id}      -> RUNNING
GET /commit/{id}      -> SUCCEEDED or FAILED
```

```bash
export ROCK_API_URL="https://xrl.alibaba-inc.com/apis/envs/sandbox/v1"
```

## Prerequisites

- The registry account must have permission to push the target image, and the sandbox network must be able to reach the registry.

## HTTP API

### Start a commit

```text
POST $ROCK_API_URL/commit
Content-Type: application/json
```

Request body:

| Field | Type | Required | Description |
|---|---|---|---|
| `sandbox_id` | string | Yes | Sandbox ID and Docker container name. |
| `image_tag` | string | Yes | Full target image name, for example `registry.example.com/team/app:v1`. |
| `username` | string | Yes | Registry username. |
| `password` | string | Yes | Registry password. Send the original password; do not Base64-encode it. |

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

A successfully accepted task returns `RUNNING`:

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

### Query commit status

```text
GET $ROCK_API_URL/commit/{sandbox_id}
```

```bash
curl -sS "$ROCK_API_URL/commit/sandbox-123"
```

Poll every few seconds until `result.phase` is either `SUCCEEDED` or `FAILED`. A successful result looks like:

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

If the background operation fails, the request itself is still successful, but the task phase is `FAILED`:

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

### Status fields

| Field | Description |
|---|---|
| `phase` | `RUNNING`, `SUCCEEDED`, or `FAILED`. |
| `started_at` | Time at which the worker accepted the task. |
| `completed_at` | Completion time; present only for a terminal task. |
| `exit_code` | `0` on success; a nonzero value or `-1` on failure. |
| `failed_stage` | Failed step, such as `login`, `commit`, `push`, or `supervisor`. |
| `error_code` | Machine-readable failure code. |
| `error_message` | Human-readable diagnostic log tail. |

Common task errors include `LOGIN_FAILED`, `COMMIT_FAILED`, `PUSH_FAILED`, `TIMEOUT`, and `PROCESS_LOST`. Errors such as `SANDBOX_CONTAINER_NOT_FOUND`, `COMMIT_CONFLICT`, `STATUS_NOT_FOUND`, and `WORKER_UNREACHABLE` are request-level failures and return a top-level `status` of `Failed`.

### Repeated requests

- If the same sandbox is already committing the same image tag, another `POST /commit` returns the existing `RUNNING` task instead of starting a second process.
- If that sandbox is already committing a different image tag, the request fails with `COMMIT_CONFLICT`.
- After a task reaches `SUCCEEDED` or `FAILED`, a new `POST /commit` starts a new task and replaces the previous status for that sandbox.

## Python SDK

Use `commit()` when the caller should wait for the final result. It calls `commit_async()` internally and polls `get_commit_status()` until the task reaches `SUCCEEDED` or `FAILED`.

| Parameter | Default | Description |
|---|---|---|
| `timeout` | `180` seconds | Maximum total time for starting and polling the task. A `TimeoutError` is raised when it expires. |
| `interval` | `2` seconds | Delay between status queries. |

The timeout only stops the SDK from waiting; it does not cancel a commit task that is already running on the worker. Call `get_commit_status()` later to query that task.

The `Sandbox` object must already be started.

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
        raise RuntimeError("sandbox_id is not set")

    if status.phase == CommitPhase.FAILED:
        raise RuntimeError(
            f"commit failed: code={status.error_code}, "
            f"stage={status.failed_stage}, message={status.error_message}"
        )

    print(f"image pushed: {status.image_tag}")
```

Use `commit_async()` directly when the caller must not wait or needs custom polling behavior. Its return value is the initial task status; call `get_commit_status()` to query subsequent status.

## TypeScript SDK

Use `commit()` to start the task and wait for its final status. It calls `commitAsync()` internally and polls `getCommitStatus()`. The `timeout` and `interval` arguments use seconds and default to `180` and `2`.

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
    throw new Error('sandbox_id is not set');
  }

  if (status.phase === CommitPhase.FAILED) {
    throw new Error(
      `commit failed: code=${status.errorCode}, ` +
      `stage=${status.failedStage}, message=${status.errorMessage}`
    );
  }

  console.log(`image pushed: ${status.imageTag}`);
}
```

A timeout only stops the SDK from waiting; it does not cancel the worker task. Use `commitAsync()` when the caller needs to control polling, and call `getCommitStatus()` to query the task later.
