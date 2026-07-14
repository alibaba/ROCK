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
| [`observability_demo.py`](./observability_demo.py) | Entry point — echoes the two knobs, loads `JobConfig.from_yaml()`, runs `Job(config).run()`, prints per-trial `exception_info` |
| [`observability_job_config.yaml.template`](./observability_job_config.yaml.template) | `BashJobConfig` whose script exits non-zero, producing one soft-fail event |

## Quick run

```bash
# 1. copy the template and fill in real values (<placeholders>)
cp observability_job_config.yaml.template observability_job_config.yaml

# 2. (optional) turn metrics ON — otherwise the demo is log-only
export ROCK_JOB_METRICS_OTLP_ENDPOINT="http://localhost:4318/v1/metrics"

# 3. run
python observability_demo.py -c observability_job_config.yaml
```

The template's script does `exit 7`, so a clean run produces exactly one
soft-fail event. You'll see it twice in the output:

```
ERROR ... job exception phase=collect severity=soft exception_type=BashExitCode ... job_name=observability_demo ...
...
JobResult: status=failed  completed=0  failed=1  exit_code=...
  trial[0] ...: SOFT FAIL -> BashExitCode: ...
```

The ERROR log line is **always** emitted. The matching counter increment on
`rock_job.exception.total` is exported **only** when `ROCK_JOB_METRICS_OTLP_ENDPOINT`
is set. The demo calls `get_reporter().shutdown()` at the end to force a final
metric flush before this short-lived process exits (also registered via `atexit`,
so it's safe and idempotent).

To see a **hard fail** (`run()` raises, `severity=hard`) instead, point
`base_url` at an unreachable admin so the sandbox can't start.

## Wiring metrics to a collector

`ROCK_JOB_METRICS_OTLP_ENDPOINT` is a standard OTLP/HTTP metrics endpoint. Point
it at any OpenTelemetry Collector (or a backend that accepts OTLP/HTTP). A quick
local collector that logs received metrics to stdout is enough to eyeball the
counter; then scrape/forward to Prometheus, etc., in your real deployment.
