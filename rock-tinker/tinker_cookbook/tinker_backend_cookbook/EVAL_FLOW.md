# Tinker Backend Eval Flow

This document describes the current public-dataset rollout path in
`tinker_cookbook/tinker_backend_cookbook/eval_swe_bench.py`.

## Overview

```text
Public task selection (runs before ServerJob.open)
  -> explicit task_id or PublicTaskCatalog.select_tasks(...)
      -> TaskDescriptor(task_id, dataset, split, bench_name)
ServerJob.open(config_path)
  -> resolves/starts backend from tinker_backend.platform/backend_config
  -> create_runtime(runtime_type, config_path)
      -> Runtime
  -> RemoteSandboxEnv(task=TaskDescriptor, runtime=Runtime)
      -> runtime.init_task_env(task)
      -> runtime.get_step(env_id, step_id=0)
      -> loop: runtime.sample(prompt, env_id) -> runtime.get_step(...)
  -> runtime.close()
```

The task contract is `task_id`, `dataset`, `split`, `bench_name`, and optional
`metadata`.

## Entry Point

```bash
python3 tinker_cookbook/tinker_backend_cookbook/eval_swe_bench.py \
  --runtime-type ROLL \
  --config-path tinker_cookbook/config/roll_runtime.yaml \
  --dataset princeton-nlp/SWE-bench_Verified \
  --split test \
  --bench-name SWE-bench \
  --task-id sympy__sympy-19637 \
  --max-turns 2 \
  --max-tokens 64 \
  --temperature 0.8
```

## Task Loading

`tasks.py` centralizes task selection:

```python
task = await load_one_task(args)

async with tinker.ServerJob.open(config_path=args.config_path, api_key=args.api_key) as job:
    client = await job.get_client()
```

When `--task-id` is explicit, `load_one_task(...)` constructs the
descriptor directly. When `--task-filter` is used, it calls the local catalog:

```python
await catalog.select_tasks(
    dataset=args.dataset,
    split=args.split,
    bench_name=args.bench_name,
    task_filter=task_filter_from_args(args),
    seed=args.task_seed,
)
```

Selection completes before `ServerJob.open(...)`, so an invalid or empty filter
does not start a backend job. The selected descriptor is written to the local
run's `task_manifest.json` and then passed unchanged to `runtime.init_task_env`.

## Runtime Creation

```python
runtime = await client.create_runtime(
    runtime_type=args.runtime_type,
    config_path=args.config_path,
)
```

The SDK reads the config file locally and sends opaque config content to the
backend. The backend materializes the runtime config, starts ROLL and the
ROCK adapter, and completes the create-runtime future only after the runtime
reports ready.

## Environment Initialization

```python
env = RemoteSandboxEnv(task=task, runtime=runtime, max_turns=args.max_turns)
trajectory = await do_single_rollout(policy, env)
```

`RemoteSandboxEnv.get_observation(init=True)` calls:

```python
env_id = await runtime.init_task_env(task)
step0 = await runtime.get_step(env_id=env_id, step_id=0)
```

`init_task_env` is future-backed. It completes only after the ROCK adapter has
started the sandbox, started ModelService, launched the Harbor agent, intercepted
the first LLM request, and written step 0 into the backend.

## Rollout Loop

`do_single_rollout(...)` drives the episode:

1. Get step 0 prompt.
2. Call `RuntimeTokenCompleter`, which sends `runtime.sample(..., env_id=env_id)`.
3. Backend routes the sample action to ROLL.
4. When sampling completes, backend enqueues delivery to the ROCK adapter.
5. ROCK adapter sends the model response back into ModelService/Harbor.
6. The agent either emits the next LLM request, producing another step, or exits.
7. Terminal step carries `finish_reason` and `reward`.

## Output

The script writes one timestamped result directory under:

```text
tinker_cookbook/tinker_backend_cookbook/results/runtime_rollout_*
```

The main artifact is `summary.json`, including runtime id, env id, task id,
dataset, split, number of turns, reward, terminal status, and elapsed time.

## Batch Evaluation

The supported rollout entrypoint is `eval_swe_bench.py`. Add a new public-dataset
batch runner if multi-task evaluation is needed again.
