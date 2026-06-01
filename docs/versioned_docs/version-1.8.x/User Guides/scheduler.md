---
sidebar_position: 5
---

# Scheduler

The ROCK scheduler is a periodic task framework embedded in the `admin` service. It dispatches background maintenance tasks (image cleanup, file cleanup, container cleanup, image pre-pull, custom tasks, ...) to every alive Ray worker on a configurable interval, so worker nodes stay healthy without manual intervention.

This guide covers how to enable the scheduler, configure built-in tasks, write your own task, and inspect runtime status.

## 1. How It Works

- The scheduler runs inside the `admin` process as a dedicated daemon thread (`SchedulerThread`) with its own `asyncio` event loop.
- Tasks are scheduled by [APScheduler](https://apscheduler.readthedocs.io/) using fixed intervals (`interval_seconds`).
- For each tick, the scheduler resolves the list of alive Ray workers (cached via `worker_cache_ttl` seconds) and dispatches the task to every worker concurrently (default concurrency: 50).
- Dispatch is done over HTTP through the worker's **rocklet** service: the admin builds a `RemoteSandboxRuntime(host=worker_ip, port=Port.PROXY)` (see `rock.deployments.constants.Port`) and calls `runtime.execute / read_file / write_file` against it. **Every worker must therefore have the `rocklet` server running and reachable on `Port.PROXY`** — otherwise the scheduler cannot push commands or read/write status files on that worker.
- Each task subclasses `rock.admin.scheduler.task_base.BaseTask` and must implement `run_action(runtime: RemoteSandboxRuntime)` — the work performed on a single worker.
- Per-worker execution status is persisted to the worker filesystem under `ROCK_SCHEDULER_STATUS_DIR` (default `/data/scheduler_status`), and an aggregated execution report is written to `<status_dir>/<task_type>_run_report.json` after every run.
- If a Nacos config provider is enabled, the scheduler subscribes to config changes and applies a diff: only tasks whose hash changed are re-installed; removed tasks are cleaned up from all workers.

### Prerequisites

Before enabling the scheduler, make sure each Ray worker meets the following requirements:

| Requirement | Why |
|-------------|-----|
| `rocklet` process is running on the worker | The scheduler dispatches every task through the rocklet HTTP API; without it, `runtime.execute` calls time out. |
| Rocklet's listening port is reachable from the admin | The scheduler uses `Port.PROXY` (defined in `rock.deployments.constants.Port`) as the dispatch target. Make sure no firewall / security group blocks it. |
| `ROCK_SCHEDULER_STATUS_DIR` is writable inside the worker | Tasks read and write `<task>_status.json` here for idempotency / PID tracking. |
| Tools required by the task are available on the worker | e.g. `docker` for the cleanup / pull tasks, `curl` and outbound network for `ImageCleanupTask` to install `docuum` on first run. |

The rocklet server is started automatically by the standard worker bootstrap scripts (`docker_run.sh`, `docker_run_with_uv.sh`, `docker_run_with_pip.sh`) — typically `rocklet --port <Port.PROXY>`. If you bring up workers with a custom entrypoint, ensure the equivalent command is invoked. See the [Configuration](./configuration.md) guide for the runtime-environment options that govern how rocklet is started.

### Idempotency

Each task declares an idempotency mode that affects how it is re-run:

| Mode | Behavior |
|------|----------|
| `IDEMPOTENT` | Always run on every tick. Safe to repeat (e.g. `docker pull`, `find -exec rm`). |
| `NON_IDEMPOTENT` | Spawns a background daemon (e.g. `docuum`). The scheduler reads the previous status file, checks whether the recorded PID is still alive, and skips re-launch if the daemon is still running. On task removal the daemon is killed via `pkill`. |

## 2. Enabling the Scheduler

The scheduler is configured under the top-level `scheduler:` key of the ROCK admin YAML (e.g. `rock-conf/rock-local.yml`, `rock-conf/rock-dev.yml`).

```yaml
scheduler:
  enabled: true              # Master switch
  worker_cache_ttl: 43200    # Worker IP cache TTL in seconds
  tasks:
    # ... task list, see below
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Master switch. When `false`, all tasks are removed and no new ticks fire. |
| `worker_cache_ttl` | int | `3600` | Seconds the alive-worker IP list is cached before refreshing from `ray.nodes()`. |
| `tasks` | list | `[]` | List of `TaskConfig` entries (see [Section 4](#4-task-config-schema)). |

### Related Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `ROCK_SCHEDULER_STATUS_DIR` | `/data/scheduler_status` | Directory on workers where per-task status JSON and run reports are written. |
| `ROCK_LOGGING_PATH` | (unset) | When set, scheduler-spawned daemons (docuum, container_cleanup, image_pull) redirect their stdout/stderr to `<ROCK_LOGGING_PATH>/<task>.log`. |
| `ROCK_DOCUUM_INSTALL_URL` | `https://raw.githubusercontent.com/stepchowfun/docuum/main/install.sh` | Install script URL for `docuum`, fetched on demand by `ImageCleanupTask`. |

## 3. Built-in Tasks

ROCK ships with four built-in tasks under `rock.admin.scheduler.tasks`. Each task is registered by setting `task_class` to its fully qualified class path.

### 3.1 ImageCleanupTask

Runs [`docuum`](https://github.com/stepchowfun/docuum) on every worker to evict the least-recently-used Docker images once disk usage crosses a threshold. **Non-idempotent** — `docuum` runs as a long-lived daemon; the scheduler tracks its PID and skips re-launch while the daemon is alive.

```yaml
- task_class: rock.admin.scheduler.tasks.image_cleanup_task.ImageCleanupTask
  enabled: true
  interval_seconds: 43200       # Re-check daemon every 12 hours
  params:
    disk_threshold: "70%"       # Trigger eviction when disk usage exceeds 70%
    image_whitelist:            # Glob patterns matching repository:tag — never evicted
      - "python:3.11"
      - "my-registry.example.com/base/*"
```

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `disk_threshold` | str | `"1T"` | Disk usage threshold passed to `docuum --threshold`. Accepts size (`100G`, `1T`) or percentage (`70%`). |
| `image_whitelist` | list[str] | `[]` | Glob patterns forwarded to `docuum --keep`. |

### 3.2 FileCleanupTask

Walks each configured directory and removes files that are either older than `max_age_mins` or larger than `max_file_size`, then prunes empty subdirectories. **Idempotent**.

```yaml
- task_class: rock.admin.scheduler.tasks.file_cleanup_task.FileCleanupTask
  enabled: true
  interval_seconds: 86400       # Run daily
  params:
    target_dirs:
      # Plain string form — no exclusions
      - "/data/service_status"
      # Object form — per-directory exclusions
      - path: "/data/logs"
        exclude_files:           # Plain name | relative path | absolute path
          - "docuum.log"
          - "./rocklet.log"
          - "./access.log"
        exclude_dirs:
          - ".cache"
    max_age_mins: 10080          # 7 days; older files are removed
    max_file_size: "1G"          # Files larger than this are removed (supports K/M/G/T)
```

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `target_dirs` | list | `[]` | Each entry is either a string (path only) or `{path, exclude_files, exclude_dirs}`. |
| `max_age_mins` | int | `10080` | Files whose mtime is older than this many minutes are deleted. |
| `max_file_size` | str | `"1G"` | Files larger than this are deleted. Suffixes `K/M/G/T` accepted. |

The deletion condition is `(-mmin +max_age_mins) OR (-size +max_file_size)`. After file removal, a second `find -depth -type d -empty -delete` pass removes empty directories left behind (also honoring `exclude_dirs`).

### 3.3 ContainerCleanupTask

Removes stopped Docker containers older than a configurable age. Helps prevent the worker's container list from growing unbounded between sandbox runs. **Idempotent**.

```yaml
- task_class: rock.admin.scheduler.tasks.container_cleanup_task.ContainerCleanupTask
  enabled: true
  interval_seconds: 86400
  params:
    max_age_hours: 72            # Remove exited containers older than 72 hours
```

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `max_age_hours` | int | `24` | Maximum age (hours since `FinishedAt`) for kept exited containers. Older ones are `docker rm`'d. |

The task also removes any container in the `created` state (never started) on every run.

### 3.4 ImagePullTask

Pre-pulls a list of Docker images on every worker, optionally logging in to private registries first. Reduces sandbox cold-start latency. **Idempotent** (`docker pull` is a no-op when the image is already up-to-date).

```yaml
- task_class: rock.admin.scheduler.tasks.image_pull_task.ImagePullTask
  enabled: true
  interval_seconds: 21600       # Refresh every 6 hours
  params:
    images:
      # Plain string form — public image, no auth
      - "python:3.11"
      # Object form — private image with registry login
      - image: "my-registry.example.com/chatos/python:313"
        registry_username: "myuser"
        registry_password: "bXlwYXNzd29yZA=="   # base64-encoded
```

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `images` | list | `[]` | Each entry is either an image string or `{image, registry_username, registry_password}`. |

`registry_password` must be base64-encoded; the worker decodes it and pipes it to `docker login --password-stdin`. The registry host is parsed from the image name, so each image can target a different registry.

## 4. Task Config Schema

Every entry under `scheduler.tasks` is loaded as a `rock.config.TaskConfig`:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `task_class` | str | `""` | Fully qualified Python class path. Required. |
| `enabled` | bool | `true` | Disabled tasks are skipped at install time and torn down on reload. |
| `interval_seconds` | int | `3600` | APScheduler `interval` in seconds. |
| `params` | dict | `{}` | Task-specific kwargs forwarded to `from_config()`. |

A change in any field of an existing task entry causes the scheduler to uninstall the old task (cleaning up its worker-side state when non-idempotent) and install the new one — without restarting the admin process.

## 5. Writing a Custom Task

Any class under your Python path that subclasses `BaseTask` can be registered. The minimum contract is:

```python
# my_pkg/my_tasks/disk_report_task.py
from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.scheduler.task_base import BaseTask, IdempotencyType, TaskStatusEnum
from rock.sandbox.remote_sandbox import RemoteSandboxRuntime


class DiskReportTask(BaseTask):
    """Log `df -h` output from every worker."""

    def __init__(self, interval_seconds: int = 3600, mount_point: str = "/"):
        super().__init__(
            type="disk_report",                   # Used as job id and status filename prefix
            interval_seconds=interval_seconds,
            idempotency=IdempotencyType.IDEMPOTENT,
        )
        self.mount_point = mount_point

    @classmethod
    def from_config(cls, task_config) -> "DiskReportTask":
        return cls(
            interval_seconds=task_config.interval_seconds,
            mount_point=task_config.params.get("mount_point", "/"),
        )

    async def run_action(self, runtime: RemoteSandboxRuntime) -> dict:
        result = await runtime.execute(
            Command(command=f"df -h {self.mount_point}", shell=True),
        )
        return {
            "status": TaskStatusEnum.SUCCESS,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
        }
```

Then register it from YAML:

```yaml
scheduler:
  enabled: true
  tasks:
    - task_class: my_pkg.my_tasks.disk_report_task.DiskReportTask
      enabled: true
      interval_seconds: 600
      params:
        mount_point: "/data"
```

### Authoring Checklist

- **Always set a unique `type` string** in `super().__init__()`. It is used as the APScheduler job id, the status filename (`<type>_status.json`), and the run-report filename (`<type>_run_report.json`). Two tasks must not share a `type`.
- **Pick the right `IdempotencyType`**:
  - Use `IDEMPOTENT` when `run_action` finishes synchronously and is safe to re-execute.
  - Use `NON_IDEMPOTENT` when you `nohup` a long-running daemon and return its PID. The scheduler will then track the PID, skip re-launch while it is alive, and `pkill` it on uninstall.
- **Return a dict from `run_action`**. Recommended keys:
  - `status` — a `TaskStatusEnum` value, persisted into the status file.
  - `pid` — required for `NON_IDEMPOTENT` daemons (use `rock.utils.system.extract_nohup_pid` on the `nohup ... & echo PID_PREFIX${{!}}PID_SUFFIX` output).
  - Any other diagnostic fields are written to the status file's `extra` section.
- **Override `from_config(cls, task_config)`** to translate `task_config.params` into your `__init__` kwargs.
- **Use `runtime.execute / read_file / write_file`** rather than running shell commands locally — the scheduler is dispatching to remote workers via `RemoteSandboxRuntime`.

## 6. Observability

For each task, the scheduler writes two artifacts on every worker:

| Path | Written By | Contents |
|------|------------|----------|
| `<ROCK_SCHEDULER_STATUS_DIR>/<task_type>_status.json` | `BaseTask.save_task_status` | Latest per-worker status: `task_name`, `worker_ip`, `pid`, `status` (`pending`/`running`/`success`/`failed`), `last_run`, `error`, plus task-specific `extra` fields. |
| `<ROCK_SCHEDULER_STATUS_DIR>/<task_type>_run_report.json` | `BaseTask.run` (admin side, after the tick completes) | Aggregated report: total/success/failed counts, list of `success_ips`, and `failed_details` (`ip` + traceback). |

Scheduler-internal logs are written under the standard ROCK admin log path, with `name="scheduler"`, `name="task_base"`, `name="image_clean"`, etc. Scheduler-spawned daemons additionally write their own logs to `<ROCK_LOGGING_PATH>/<task>.log` when `ROCK_LOGGING_PATH` is set (e.g. `docuum.log`, `container_cleanup.log`, `image_pull.log`).

## 7. Dynamic Reload via Nacos (Optional)

When the admin service is configured with a Nacos provider, the scheduler installs a YAML listener and reacts to config pushes:

- Only the `scheduler:` section is inspected; other sections are ignored.
- The new section is hashed and compared against the previous one — duplicate notifications are skipped.
- A diff between old and new task lists determines which tasks to install, uninstall, or reinstall (changed `params` / `interval_seconds` / `enabled`).
- Non-idempotent tasks that are removed or re-installed are first cleaned up: their daemon PID is killed and the status file is removed.

This means task interval changes, parameter tweaks, and adding/removing tasks can be applied without restarting the admin process.

## 8. Troubleshooting

| Symptom | Likely Cause | What to Check |
|---------|--------------|----------------|
| `Scheduler disabled, all tasks removed` in admin log | `scheduler.enabled` is `false` | Set `enabled: true` in YAML. |
| `No alive workers found for task '<type>'` | Ray cluster has no live worker nodes | Verify `ray.nodes()` reports alive CPU workers; consider lowering `worker_cache_ttl` if workers were just added. |
| Task ticks fire but every worker shows up in `failed_details` with connection errors | `rocklet` is not running on the workers, or `Port.PROXY` is blocked | On the worker host, hit the rocklet liveness endpoint `GET /is_alive` on `Port.PROXY` (e.g. `curl http://<worker_ip>:<Port.PROXY>/is_alive`); if it does not respond, restart rocklet (`rocklet --port <Port.PROXY>`) or open the port in the firewall. |
| Task runs but never repeats on a non-idempotent worker | Recorded PID still alive | Inspect `<status_dir>/<task>_status.json`; if `status: running` and the PID is alive, `should_run` returns `False`. |
| `Failed to create task '<class>'` | `task_class` import failed | Ensure the module is importable inside the admin process (installed in the same venv, on `PYTHONPATH`). |
| `docker login` failing in `ImagePullTask` | `registry_password` not base64-encoded, or wrong registry parsed from image | Re-encode the password with `echo -n '<pwd>' \| base64`; double-check the image's registry host. |

## Related Documents

- [Configuration](./configuration.md) — Environment variables and runtime layout
- [API Documentation](../References/api.md) — Admin HTTP API
- [Python SDK Documentation](../References/Python%20SDK%20References/python_sdk.md) — Programmatic sandbox usage
