# Tinker Backend Cookbook

This cookbook is the SDK-side entrypoint for the Tinker backend runtime path.
Tasks are resolved in the local SDK process and passed to the backend as
`tinker.TaskDescriptor` objects. Task selection completes before the local backend starts.

## Current Scope

- An explicit `--task-id` is converted locally into a `TaskDescriptor` without a catalog lookup.
- Regex `--task-filter` selection uses `PublicTaskCatalog` locally before `ServerJob.open(...)` allocates backend resources.
- `TinkerClient.create_runtime(...)` starts a backend-managed runtime and waits for its ready future.
- Rollout uses `runtime.init_task_env(task)`, `runtime.get_step(...)`, and runtime-scoped sampling.
- Training uses runtime-scoped model, sampler, reference, forward/backward, optimizer, and publish APIs.
- `async with runtime` closes ROLL, the ROCK adapter, sandbox resources, and residual processes.


## Backend Platform

The selected `--config-path` YAML also owns SDK-side backend startup:

```yaml
tinker_backend:
  platform: local
  backend_config:
    repo_path: /root/tinker-backend
    port: 9000
    host: 0.0.0.0
    client_host: 127.0.0.1
```

Cookbook scripts call `tinker.ServerJob.open(config_path=...)`; they do not expose `--base-url`, `--platform`, or backend launch flags. Each `ServerJob` owns a fresh backend instance. If the configured port is already occupied or a backend is already responding, startup fails instead of reusing it; use another `tinker_backend.backend_config.port` such as 9001 or 9002 for concurrent runs.

## Task Selection

Default task arguments are defined in `tasks.py`:

```text
dataset    = princeton-nlp/SWE-bench_Verified
split      = test
bench_name = SWE-bench
task_id    = sympy__sympy-19637
```

Every runnable script accepts the same task selector:

```bash
--dataset princeton-nlp/SWE-bench_Verified \
  --split test \
  --bench-name SWE-bench \
  --task-id sympy__sympy-19637
```

`--task-filter` can be used instead of `--task-id` when a regex selection is needed.
The local control environment includes the public dataset reader. For a standalone
SDK on Python 3.12–3.13, install the task-catalog extra. Task filtering streams
instance IDs from the public Hugging Face dataset; it does not prepare task
images. The default explicit task ID does not access the catalog.

Use `--task-seed` for deterministic selection order. The selected descriptor
and selection inputs are written to `task_manifest.json` under the run results
directory before the ServerJob starts.

## Rollout

Run one task through ROLL + ROCK via a backend listening on port 9000:

```bash
python3 tinker_cookbook/tinker_backend_cookbook/eval_swe_bench.py \
  --runtime-type ROLL \
  --config-path tinker_cookbook/config/roll_runtime.yaml \
  --dataset princeton-nlp/SWE-bench_Verified \
  --split test \
  --bench-name SWE-bench \
  --task-id sympy__sympy-19637 \
  --temperature 0.8
```

## Training

Use the train runtime config:

```bash
python3 tinker_cookbook/tinker_backend_cookbook/train_swe_bench.py \
  --runtime-type ROLL \
  --config-path tinker_cookbook/config/roll_train_runtime.yaml \
  --dataset princeton-nlp/SWE-bench_Verified \
  --split test \
  --bench-name SWE-bench \
  --task-id sympy__sympy-19637 \
  --max-turns 2 \
  --max-tokens 64 \
  --temperature 0.8 \
  --max-train-transitions 1
```

Use `train_swe_bench_kl.py` for PPO plus reference KL and `train_swe_bench_grpo.py` for GRPO plus reference KL.
Training cookbooks do not load a local tokenizer. They retain runtime text for
readable artifacts and runtime token IDs/logprobs for training.

## Files

- `tasks.py`: public task selection, and manifest writing.
- `env.py`: `RemoteSandboxEnv`, which adapts a `TaskDescriptor` to rollout `Env` semantics.
- `trajectory_format.py`: structured prompt/response trajectory formatting without a local tokenizer.
- `eval_swe_bench.py`: single-task rollout smoke.
- `train_swe_bench.py`: PPO smoke.
- `train_swe_bench_kl.py`: PPO with frozen reference KL.
- `train_swe_bench_grpo.py`: GRPO with group-relative advantages and frozen reference KL.
