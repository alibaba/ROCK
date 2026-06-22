# job/observability

Demo for the **exception observability** layer in the Job SDK
(`rock.sdk.job.observability`): structured exception logs + an optional OTLP
metric, emitted automatically from every `JobExecutor` phase.

You don't call the observability layer yourself — it's woven into the executor
via the `monitor_job_phase` decorator. Your only knobs are two env vars:

| Env var | Default | Effect |
|---------|---------|--------|
| `ROCK_JOB_METRICS_OTLP_ENDPOINT` | unset | Unset → **log-only**. Set → also emit the OTLP counter `rock_job.exception.total`. |
| `ROCK_JOB_METRICS_HIGH_CARDINALITY_LABELS` | `true` | `false` → drop `job_name` / `sandbox_id` from the **metric** (they stay on the log). |

On every job exception the SDK dual-writes:

1. a structured `key=value` **ERROR log** — always, with the full label set, and
2. a counter increment on `rock_job.exception.total` — only when an OTLP endpoint is configured.

Labels: `phase`, `severity`, `exception_type`, `trial_type`, `job_name`,
`experiment_id`, `namespace`, `sandbox_id`.

## Two failure semantics

| | What the SDK does | How you observe it |
|---|---|---|
| **soft fail** (script `exit != 0`, timeout, no output) | recorded as `severity=soft`, carried back in `TrialResult.exception_info` — `run()` does **not** raise | `JobResult.status == FAILED`, `TrialResult.exception_info` populated |
| **hard fail** (sandbox can't start, launch fails) | recorded as `severity=hard`, then **re-raised** (fail-fast across the batch) | `run()`/`wait()` raises |

See [`docs/dev/job/exception-handling.md`](../../../docs/dev/job/exception-handling.md) for the full taxonomy.

## Files

| File | Purpose |
|------|---------|
| [`observability_demo.py`](./observability_demo.py) | Entry point with two modes: `self-test` (no infra) and `run` (real `Job(config).run()`) |

## Quick run

### 1. Infra-free walkthrough (`--mode self-test`)

Drives the reporter + `monitor_job_phase` decorator with stubs so you can see
the exact ERROR log line and counter increment for both a soft and a hard fail.
No sandbox, no admin, no network:

```bash
python examples/job/observability/observability_demo.py --mode self-test
```

### 2. Real run (`--mode run`)

Runs a `BashJobConfig` in a real sandbox. The script is chosen by `--scenario`:

| `--scenario` | Script behavior | Resulting event |
|--------------|-----------------|-----------------|
| `success` | `exit 0` | none |
| `soft-fail` (default) | `exit 7` | soft fail `BashExitCode`, phase `collect` |
| `timeout` | sleeps past `--timeout` | soft fail `ProcessTimeout`, phase `collect` |

```bash
export ROCK_BASE_URL="http://localhost:8080"   # required
export YOUR_API_KEY="<your-token>"             # sent as XRL-Authorization
export ROCK_IMAGE="python:3.11"                # optional
export ROCK_CLUSTER="<your-cluster>"           # optional

# optional: turn metrics ON (otherwise log-only)
export ROCK_JOB_METRICS_OTLP_ENDPOINT="http://localhost:4318/v1/metrics"

python examples/job/observability/observability_demo.py --mode run --scenario soft-fail --scatter 2
```

The demo prints whether metrics are on, runs the job, then summarizes
`JobResult.status` / per-trial `exception_info`. Because this is a short-lived
process, it calls `get_reporter().shutdown()` at the end to force a final metric
flush before exit (the same flush is registered via `atexit`, so it's safe and
idempotent).

## Wiring metrics to a collector

`ROCK_JOB_METRICS_OTLP_ENDPOINT` is a standard OTLP/HTTP metrics endpoint. Point
it at any OpenTelemetry Collector (or a backend that accepts OTLP/HTTP). A quick
local collector that logs received metrics to stdout is enough to eyeball the
counter; then scrape/forward to Prometheus, etc., in your real deployment.
