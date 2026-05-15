# evaluation

End-to-end evaluation demos that combine sandbox lifecycle, agent install/run, and a test suite — useful for understanding how individual pieces fit together at the script level.

## Layout

| Subdir | Path | Description |
|--------|------|-------------|
| [`swe_bench/`](./swe_bench/) | install-agent | Single-task SWE-bench Verified demo: starts a sandbox, installs an agent via `sandbox.agent.install()`, runs the agent on the task, runs the test suite, parses the result |

## When to use this vs `job/harbor/`

| | `evaluation/swe_bench/` | [`job/harbor/`](../job/harbor/) |
|--|------------------------|-------------------------------|
| Path | install-agent | Job (Harbor) |
| When | Debugging task setup or test parsing — full pipeline visible in script form | Production benchmark runs through the Harbor framework |
| API | `Sandbox` + `sandbox.agent.install()` | `Job(JobConfig.from_yaml(...)).run()` |

If you're running SWE-bench through the standard pipeline, prefer [`job/harbor/`](../job/harbor/).
