# job

Examples for the **Job** way of using ROCK: run an agent evaluation/task in a sandbox via `rock.sdk.job.Job` + `JobConfig`.

For installing and running an agent inside a single sandbox, see [`../install-agents/`](../install-agents/) instead.

## Layout

| Subdir | Backend | Use it for |
|--------|---------|-----------|
| [`bash/`](./bash/) | `BashJobConfig` | Run an arbitrary shell script inside a sandbox (data processing, external evaluation tools) |
| [`harbor/`](./harbor/) | `HarborJobConfig` | Run an AI agent benchmark task (SWE-bench, Terminal Bench, …) via the Harbor framework |

Both backends share a single `Job(config).run()` entrypoint — pick the config type based on your scenario.

See the [Use Job to Run Agent](../../docs/versioned_docs/version-1.7.x/References/Python%20SDK%20References/job.md) reference for the full schema.
