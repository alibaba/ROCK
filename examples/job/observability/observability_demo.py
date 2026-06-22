"""Observability demo for the ROCK Job SDK.

Shows how the exception observability added in ``rock.sdk.job.observability``
behaves end-to-end. The observability layer is **woven automatically** into
every ``JobExecutor`` phase (start / setup / launch / wait / collect) — you do
NOT call it directly. Your only knobs are two environment variables:

    ROCK_JOB_METRICS_OTLP_ENDPOINT          # unset -> log-only; set -> also emit OTLP counter
    ROCK_JOB_METRICS_HIGH_CARDINALITY_LABELS # "false" -> drop job_name/sandbox_id from the metric

On every job exception the SDK dual-writes:
  1. a structured ``key=value`` ERROR log (always, with full labels), and
  2. a counter ``rock_job.exception.total`` (only when an OTLP endpoint is set).

Two failure semantics are surfaced (see docs/dev/job/exception-handling.md):
  - soft fail: carried back as data in ``TrialResult.exception_info`` — run() does NOT raise
  - hard fail: re-raised out of run()/wait() (and fail-fast across the whole batch)

Modes
-----
  --mode self-test   No sandbox needed. Drives the reporter + monitor_job_phase
                     decorator with stubs so you can SEE the exact log line and
                     counter increment for a soft fail and a hard fail.
  --mode run         The real path: builds a BashJobConfig and runs it in a
                     sandbox via Job(config).run(). Use --scenario to pick a
                     script that succeeds or soft-fails.

Real-run env vars (only for --mode run):
    ROCK_BASE_URL            admin URL, e.g. http://localhost:8080
    YOUR_API_KEY             bearer token (sent as XRL-Authorization header)
    ROCK_IMAGE               sandbox image (default: python:3.11)
    ROCK_CLUSTER             cluster name (optional)
    YOUR_EXPERIMENT_ID       experiment id (default: job_observability_demo)

Examples
--------
    # See the log + metric pipeline locally, no infra required:
    python examples/job/observability/observability_demo.py --mode self-test

    # Real run that produces a soft failure (script exits non-zero):
    export ROCK_JOB_METRICS_OTLP_ENDPOINT=http://localhost:4318/v1/metrics  # optional
    python examples/job/observability/observability_demo.py --mode run --scenario soft-fail --scatter 2
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from rock import env_vars
from rock.sdk.job import BashJobConfig, Job, observability
from rock.sdk.job.operator import ScatterOperator
from rock.sdk.job.result import ExceptionInfo, JobResult, TrialResult

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("observability_demo")

# Scripts chosen to exercise distinct exception_types attributed to the
# "collect" phase (the JobExecutor scans collect()'s return value for soft fails).
SCENARIO_SCRIPTS = {
    # exit 0 -> clean trial, no exception event emitted
    "success": 'echo "[trial] doing work..."; sleep 1; echo "[trial] done"; exit 0',
    # non-zero exit -> BashExitCode soft fail
    "soft-fail": 'echo "[trial] doing work..."; sleep 1; echo "[trial] boom"; exit 7',
    # never finishes within --timeout -> ProcessTimeout soft fail
    "timeout": 'echo "[trial] sleeping past the timeout..."; sleep 100000',
}


def print_observability_status() -> None:
    """Echo the two knobs so it's obvious whether metrics are on."""
    endpoint = env_vars.ROCK_JOB_METRICS_OTLP_ENDPOINT
    high_card = env_vars.ROCK_JOB_METRICS_HIGH_CARDINALITY_LABELS
    logger.info("─" * 60)
    logger.info("Job observability config:")
    if endpoint:
        logger.info("  metrics:        ON  -> OTLP counter to %s", endpoint)
    else:
        logger.info("  metrics:        OFF -> log-only (set ROCK_JOB_METRICS_OTLP_ENDPOINT to enable)")
    logger.info(
        "  high-card labels: %s (job_name/sandbox_id %s on the metric)", high_card, "kept" if high_card else "dropped"
    )
    logger.info("  counter name:    rock_job.exception.total")
    logger.info("─" * 60)


# ─────────────────────────────────────────────────────────────────────────────
# self-test mode: no sandbox, drive the observability layer directly
# ─────────────────────────────────────────────────────────────────────────────


class _PrintingCounter:
    """Stand-in OTLP counter that prints each increment instead of exporting."""

    def add(self, value: int, attributes: dict) -> None:
        logger.info("  >> counter rock_job.exception.total += %d  attrs=%s", value, attributes)


class _Cfg:
    job_name = "selftest-job"
    experiment_id = "selftest-exp"
    namespace = "selftest-ns"


class _StubTrial:
    """Duck-typed AbstractTrial — observability only needs ._config."""

    _config = _Cfg()


class _StubExecutor:
    """A tiny JobExecutor-like object whose phases are decorated, so we exercise
    the exact same monitor_job_phase code path the real executor uses."""

    @observability.monitor_job_phase("setup")
    async def hard_fail_phase(self, trial):
        raise RuntimeError("simulated hard failure in setup")

    @observability.monitor_job_phase("collect")
    async def soft_fail_phase(self, trial):
        # Returning a TrialResult carrying exception_info == a soft fail.
        return TrialResult(
            task_name="selftest-task",
            exception_info=ExceptionInfo(exception_type="BashExitCode", exception_message="exit code 7"),
        )


async def run_self_test() -> None:
    logger.info("=== self-test mode: exercising the observability layer with stubs ===")
    print_observability_status()

    # Force-enable the metric side with a printing counter so you can see BOTH
    # the structured log AND the counter increment, regardless of env config.
    reporter = observability.get_reporter()
    reporter._enabled = True
    reporter._counter = _PrintingCounter()

    executor = _StubExecutor()
    trial = _StubTrial()

    logger.info("")
    logger.info("[1] soft fail — phase returns a TrialResult with exception_info:")
    result = await executor.soft_fail_phase(trial)
    logger.info("    phase returned normally (NOT raised); status=%s", result.status)

    logger.info("")
    logger.info("[2] hard fail — phase raises; observability records then re-raises:")
    try:
        await executor.hard_fail_phase(trial)
    except RuntimeError as e:
        logger.info("    original exception propagated unchanged: %r", e)

    logger.info("")
    logger.info("Note: each case logged one ERROR 'job exception ...' line above and")
    logger.info("incremented the counter once. In a real run these come for free —")
    logger.info("you only read JobResult.status / TrialResult.exception_info.")


# ─────────────────────────────────────────────────────────────────────────────
# run mode: the real Job(config).run() path
# ─────────────────────────────────────────────────────────────────────────────


def build_config(args: argparse.Namespace) -> BashJobConfig:
    api_key = os.environ.get("YOUR_API_KEY", "")
    environment = {
        "base_url": os.environ.get("ROCK_BASE_URL", "http://localhost:8080"),
        "image": os.environ.get("ROCK_IMAGE", "python:3.11"),
    }
    # Only set optional fields when provided — cluster/extra_headers reject None.
    if os.environ.get("ROCK_CLUSTER"):
        environment["cluster"] = os.environ["ROCK_CLUSTER"]
    if api_key:
        environment["extra_headers"] = {"XRL-Authorization": f"Bearer {api_key}"}
    return BashJobConfig(
        job_name="observability_demo",
        experiment_id=os.environ.get("YOUR_EXPERIMENT_ID", "job_observability_demo"),
        timeout=args.timeout,
        labels={"demo": "observability"},
        script=SCENARIO_SCRIPTS[args.scenario],
        environment=environment,
    )


def summarize(result: JobResult) -> None:
    logger.info("─" * 60)
    logger.info(
        "JobResult: status=%s  completed=%d  failed=%d  exit_code=%d",
        result.status,
        result.n_completed,
        result.n_failed,
        result.exit_code,
    )
    for i, t in enumerate(result.trial_results):
        if t.exception_info is not None:
            # This is a SOFT fail: surfaced as data, run() did not raise.
            logger.info(
                "  trial[%d] %s: SOFT FAIL -> %s: %s",
                i,
                t.task_name or "?",
                t.exception_info.exception_type,
                t.exception_info.exception_message,
            )
        else:
            logger.info("  trial[%d] %s: ok", i, t.task_name or "?")
    logger.info("─" * 60)
    if result.n_failed:
        logger.info("Soft failures above were ALSO emitted as one ERROR log + one metric")
        logger.info("increment each, attributed to the 'collect' phase. Check the logs.")


async def run_real(args: argparse.Namespace) -> None:
    logger.info("=== run mode: real Job(config).run() with scenario=%s scatter=%d ===", args.scenario, args.scatter)
    print_observability_status()

    config = build_config(args)
    job = Job(config, operator=ScatterOperator(size=args.scatter))
    try:
        result = await job.run()
    except Exception:
        # A HARD fail (e.g. sandbox can't start) propagates here and was already
        # recorded by the observability layer (severity=hard) before re-raising.
        logger.exception("Job hard-failed — observability recorded a severity=hard event for the failing phase")
        raise
    summarize(result)

    # Short-lived process: force a final metrics flush so the last buffered
    # exceptions are exported before exit. atexit also calls this (idempotent),
    # but doing it explicitly makes the flush deterministic in a demo.
    observability.get_reporter().shutdown()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ROCK Job SDK observability demo")
    p.add_argument(
        "--mode",
        choices=["self-test", "run"],
        default="self-test",
        help="self-test: no infra; run: real Job(config).run()",
    )
    p.add_argument(
        "--scenario",
        choices=list(SCENARIO_SCRIPTS),
        default="soft-fail",
        help="(run mode) which bash script the trial executes",
    )
    p.add_argument("--scatter", type=int, default=1, help="(run mode) number of parallel trials")
    p.add_argument("--timeout", type=int, default=120, help="(run mode) per-trial timeout in seconds")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.mode == "self-test":
        asyncio.run(run_self_test())
        return
    if not os.environ.get("ROCK_BASE_URL"):
        logger.error(
            "--mode run needs ROCK_BASE_URL (and usually YOUR_API_KEY). "
            "Try --mode self-test for an infra-free walkthrough."
        )
        sys.exit(1)
    asyncio.run(run_real(args))


if __name__ == "__main__":
    main()
