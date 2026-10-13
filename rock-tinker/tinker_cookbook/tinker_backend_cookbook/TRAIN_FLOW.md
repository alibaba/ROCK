# Tinker Backend Train Flow

This document describes the current public-dataset training cookbook. All task
envs are created from `TaskDescriptor` values returned by backend `list_tasks`.
The scripts do not read local task files. They create the SDK client with `tinker.ServerJob.open(config_path=...)`, so backend platform startup comes from the selected YAML top-level `tinker_backend` section.

## Scripts

- `train_swe_bench.py`: one task, one rollout, one PPO update, one publish.
- `train_swe_bench_kl.py`: PPO plus frozen reference KL.
- `train_swe_bench_grpo.py`: multiple envs for one task, GRPO group-relative advantages, frozen reference KL.

All three scripts share the task selector from `tasks.py`:

```bash
--dataset princeton-nlp/SWE-bench_Verified \
  --split test \
  --bench-name SWE-bench \
  --task-id sympy__sympy-19637
```

## PPO Smoke

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

The cookbook does not load a local tokenizer. Readable trajectory artifacts use
the original OpenAI prompt and generated text returned by the runtime, while
training continues to use the runtime's exact token IDs and logprobs.

## PPO + KL

```bash
python3 tinker_cookbook/tinker_backend_cookbook/train_swe_bench_kl.py \
  --runtime-type ROLL \
  --config-path tinker_cookbook/config/roll_train_runtime.yaml \
  --dataset princeton-nlp/SWE-bench_Verified \
  --split test \
  --bench-name SWE-bench \
  --task-id sympy__sympy-19637 \
  --kl-beta 0.05
```

The reference client is created with:

```python
ref_sampling_client = await runtime.create_sampling_client(base_model=ref_model)
```

Its `compute_logprobs()` path is score-only and is expected to route to the
reference role in ROLL.

## GRPO + KL

```bash
python3 tinker_cookbook/tinker_backend_cookbook/train_swe_bench_grpo.py \
  --runtime-type ROLL \
  --config-path tinker_cookbook/config/roll_train_runtime.yaml \
  --dataset princeton-nlp/SWE-bench_Verified \
  --split test \
  --bench-name SWE-bench \
  --task-id sympy__sympy-19637 \
  --group-size 3 \
  --rollout-concurrency 3 \
  --num-steps 1 \
  --temperature 0.8
```

Each group member creates its own backend task env and therefore its own ROCK
sandbox. Sampling, delivery, and terminal reward are demultiplexed by `env_id`.

## Lifecycle

1. An explicit task ID or local `PublicTaskCatalog` selection returns a `TaskDescriptor` before backend allocation.
2. `TinkerClient.create_runtime(...)` starts ROLL and the ROCK adapter.
3. `runtime.create_lora_training(...)` creates a trainable model through a future-backed `create_model` action.
4. `training.publish_to_sampler()` publishes current training weights to the runtime sampler.
5. `RemoteSandboxEnv(task=task, runtime=runtime)` calls `runtime.init_task_env(task)`.
6. Rollout calls sampler-backed `sample(..., env_id=env_id)` and waits for backend futures.
7. PPO/GRPO code builds `Datum` records from rollout transitions.
8. `training.forward_backward_custom(...)` first gets logprobs, computes custom loss gradients in the SDK process, then sends weighted CE data through `forward_backward`.
9. `training.optim_step(...)` updates actor_train.
10. `training.publish_to_sampler()` publishes updated weights to actor_infer for the next rollout.
11. `async with runtime` closes ROLL, ROCK adapter, sandbox resources, and residual processes.

## Naming

- `publish_to_sampler` means transiently publishing current training weights to a sampling session.
- `save_state(name)` means a durable training checkpoint.
- `load_state(path)` and `load_state_with_optimizer(path)` restore durable checkpoints.

Do not use `save_weights_and_get_sampling_client*` names in this runtime
cookbook; those names conflate transient sampler publish with durable checkpoint
save.

## Notes

`forward_backward_custom()` does not send arbitrary Python loss code to the
backend. The custom loss is executed in the SDK process with PyTorch. The backend
receives the resulting per-token training weights and runs its own train worker
operators.
