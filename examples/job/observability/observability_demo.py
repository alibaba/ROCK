"""Observability demo for the ROCK Job SDK.

Same shape as ``examples/job/harbor/harbor_demo.py`` — load a YAML config, run
``Job(config).run()``, print the per-trial results — but pointed at the
**exception observability** layer in ``rock.sdk.job.observability``.

You do NOT call the observability layer yourself. It is woven automatically into
every ``JobExecutor`` phase (start / setup / launch / wait / collect). Your only
knobs are two environment variables:

    ROCK_JOB_METRICS_OTLP_ENDPOINT           # unset -> log-only; set -> also emit OTLP counter
    ROCK_JOB_METRICS_HIGH_CARDINALITY_LABELS # "false" -> drop job_name/sandbox_id from the metric

On every job exception the SDK dual-writes:
  1. a structured ``key=value`` ERROR log (always, with full labels), and
  2. a counter ``rock_job.exception.total`` (only when an OTLP endpoint is set).

Two failure semantics are surfaced (see docs/dev/job/exception-handling.md):
  - soft fail (script exit != 0, timeout): carried back as data in
    ``TrialResult.exception_info`` — run() does NOT raise.
  - hard fail (sandbox can't start): re-raised out of run()/wait().

The bundled ``observability_job_config.yaml.template`` runs a script that exits
non-zero, so a clean run produces exactly one soft-fail event you can see in the
logs (and as a metric, if you set the OTLP endpoint).

Usage:
    cp observability_job_config.yaml.template observability_job_config.yaml
    # fill in <placeholders>, then optionally turn metrics on:
    export ROCK_JOB_METRICS_OTLP_ENDPOINT=http://localhost:4318/v1/metrics
    python examples/job/observability/observability_demo.py -c observability_job_config.yaml
"""

import argparse
import asyncio
import logging

from rock import env_vars
from rock.sdk.job import Job, JobConfig, observability
from rock.sdk.job.result import JobResult

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
# disable httpx
logging.getLogger("httpx").setLevel(logging.WARNING)


def print_observability_status() -> None:
    """Echo the two knobs so it's obvious whether metrics are on."""
    endpoint = env_vars.ROCK_JOB_METRICS_OTLP_ENDPOINT
    high_card = env_vars.ROCK_JOB_METRICS_HIGH_CARDINALITY_LABELS
    logger.info("─" * 60)
    logger.info("Job observability config:")
    if endpoint:
        logger.info("  metrics:          ON  -> OTLP counter to %s", endpoint)
    else:
        logger.info("  metrics:          OFF -> log-only (set ROCK_JOB_METRICS_OTLP_ENDPOINT to enable)")
    logger.info(
        "  high-card labels: %s (job_name/sandbox_id %s on the metric)", high_card, "kept" if high_card else "dropped"
    )
    logger.info("  counter name:     rock_job.exception.total")
    logger.info("─" * 60)


def summarize(result: JobResult) -> None:
    logger.info("─" * 60)
    logger.info(
        "JobResult: status=%s  completed=%d  failed=%d  exit_code=%d",
        result.status,
        result.n_completed,
        result.n_failed,
        result.exit_code,
    )
    for i, trial in enumerate(result.trial_results):
        if trial.exception_info is not None:
            # SOFT fail: surfaced as data, run() did not raise.
            logger.info(
                "  trial[%d] %s: SOFT FAIL -> %s: %s",
                i,
                trial.task_name or "?",
                trial.exception_info.exception_type,
                trial.exception_info.exception_message,
            )
        else:
            logger.info("  trial[%d] %s: ok", i, trial.task_name or "?")
    logger.info("─" * 60)
    if result.n_failed:
        logger.info("Each soft failure above was ALSO emitted as one ERROR log + one metric")
        logger.info("increment, attributed to the 'collect' phase. Check the logs above.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Bash job inside a ROCK sandbox and observe its exceptions")
    parser.add_argument("-c", "--config", required=True, help="Path to BashJobConfig YAML file")
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> None:
    print_observability_status()
    config = JobConfig.from_yaml(args.config)
    try:
        result = await Job(config).run()
    except Exception:
        # A HARD fail (e.g. sandbox can't start) propagates here and was already
        # recorded by the observability layer (severity=hard) before re-raising.
        logger.exception("Job hard-failed — observability recorded a severity=hard event for the failing phase")
        raise
    else:
        summarize(result)
    finally:
        # Short-lived process: force a final metrics flush so the last buffered
        # exceptions are exported before exit. atexit also calls this (idempotent),
        # but doing it explicitly makes the flush deterministic in a demo.
        observability.get_reporter().shutdown()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(async_main(args))
