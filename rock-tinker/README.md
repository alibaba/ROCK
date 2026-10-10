# ROCK Tinker Python SDK

Tinker provides a Python client and a local backend for running agent rollouts and reinforcement-learning training. The backend delegates GPU inference and training to ROLL; local ROCK manages Harbor sandboxes where SWE-agent executes tools. Agent model requests return through ROCK ModelService to the local GPU runtime.

The SDK, backend and cookbook are bundled in the `rl-rock` wheel. Their local environment is installed from `rock-tinker/local/pyproject.toml` and its frozen `uv.lock`, independently of ROCK's default dependencies. The supported backend platform is `local`.

API documentation and generation instructions are indexed in [docs/README.md](docs/README.md).

Upstream origins, licenses and local changes are documented in [UPSTREAM.md](UPSTREAM.md).

## Repository layout

- `src/tinker/`: Python SDK.
- `local/`: isolated control-environment dependencies and lockfile.
- `tinker_cookbook/tinker_backend_cookbook/`: rollout and training entrypoints.
- `tinker_cookbook/config/`: runtime YAML templates, included in the wheel.
- `tests/`: SDK tests; ROCK integration regressions live in `../tests/unit/tinker/`.

## Install and prepare

Follow the complete [English quick start](../docs/versioned_docs/version-1.11.x/User%20Guides/tinker-quick-start.md) or [中文快速开始](../docs/i18n/zh-Hans/docusaurus-plugin-content-docs/version-1.11.x/User%20Guides/tinker-quick-start.md) for the GPU base image, repository checkout, public dataset, sandbox images and service configuration.

Complete environment preparation in the quick start first, then set these variables in each terminal:

```bash
export ROCK=/absolute/path/to/ROCK
export ROLL=/absolute/path/to/ROLL
export TASK="$HOME/tinker-local"
export QS="$ROCK/examples/tinker_quick_start"
export PY="$TASK/control-venv/bin/python"
```

| Variable / command | Meaning |
|---|---|
| `ROCK`, `ROLL` | Absolute paths to the two source checkouts. |
| `TASK` | Working directory for environments, YAML, logs, checkpoints and results. Use a fresh directory for initial setup. |
| `QS` | The single local quick-start example directory. |
| `PY` | Python interpreter in the isolated Tinker control environment. |

Before running the commands below, complete the quick start's public task preparation, YAML generation and ROCK connection (local services or an existing cluster). `--rollout-only` configuration disables training and reference workers; follow the guide's training configuration step before running a training command.

## Run rollout or training

The wrapper selects the original cookbook entrypoint and automatically owns the local backend/runtime lifecycle. Additional arguments are forwarded to that entrypoint.

```bash
# Real SWE-agent rollout followed by the SWE-bench verifier.
bash "$QS/run.sh" rollout --max-turns 32 --max-tokens 8192 --temperature 1.0

# PPO: one transition to check the training call; omit the limit for the full trajectory.
bash "$QS/run.sh" ppo --learning-rate 1e-6 --lora-rank 0 --max-train-transitions 1

# PPO with a frozen-reference KL penalty.
bash "$QS/run.sh" ppo-kl --learning-rate 1e-6 --lora-rank 0 --kl-beta 0.05

# GRPO: two trajectories per group, generated sequentially.
bash "$QS/run.sh" grpo --learning-rate 1e-6 --lora-rank 0 \
  --num-steps 1 --group-size 2 --rollout-concurrency 1
```

Run one command at a time. Training requires the training YAML and the configured free GPUs (the example uses all eight). Do not run training against the `--rollout-only` configuration.

| Mode | Cookbook entrypoint |
|---|---|
| `rollout` | `eval_swe_bench.py` |
| `ppo` | `train_swe_bench.py` |
| `ppo-kl` | `train_swe_bench_kl.py` |
| `grpo` | `train_swe_bench_grpo.py` |

The entrypoints live in `tinker_cookbook/tinker_backend_cookbook/`. To inspect their complete argument lists without starting a backend:

```bash
bash "$QS/run.sh" rollout --help
bash "$QS/run.sh" ppo --help
bash "$QS/run.sh" ppo-kl --help
bash "$QS/run.sh" grpo --help
```

The wrapper captures help text in the printed run directory's `client.log`.

## Sampling and task parameters

Values below are the **wrapper defaults**, which differ from direct cookbook invocation defaults.

| Parameter | Default via `run.sh` | Meaning |
|---|---|---|
| `--config-path` | `$TASK/config/runtime.yaml` | Local backend and ROLL runtime configuration. |
| `--output-path` | A new `$TASK/runs/<mode>-<timestamp>-<id>/results` directory | Cookbook results directory. |
| `--task-id` | `TASK_ID`, then the configured task, then `sympy__sympy-19637` | Public SWE-bench Verified task; an explicit ID avoids catalog discovery. Use the same ID for task preparation, YAML generation and execution. All public Harbor-format SWE-bench Verified tasks use their original environment and verifier; the quick start selects one example. |
| `--dataset` / `--split` | `princeton-nlp/SWE-bench_Verified` / `test` | Task dataset identity. |
| `--max-turns` | `32` | Maximum agent interaction turns in an episode. |
| `--max-tokens` | `8192` (all modes) | Maximum generated tokens per model call, not the entire episode. |
| `--temperature` | `1.0` | Sampling temperature. |

Extra arguments override the wrapper's earlier values. When changing output length, also align `max_output_tokens` and `completion_kwargs.max_tokens` in the agent YAML and the inference engine's context budget. Input plus output tokens must fit within that budget. Configure GPU allocation and tensor parallelism in `config/engine.yaml`; the quick start explains the generated files.

## Training parameters

These are cookbook parser defaults unless an example command above overrides them.

| Parameter | Applies to | Default | Meaning |
|---|---|---|---|
| `--learning-rate` | All training modes | `3e-6` | Optimizer learning rate. Examples use `1e-6`. |
| `--weight-decay` | All training modes | `0.0` | Optimizer weight decay. |
| `--clip-epsilon` | All training modes | `0.2` | Policy probability-ratio clipping range. |
| `--lora-rank` | All training modes | `32` | Requested LoRA rank; the local full-parameter examples use `0`. Keep the runtime training strategy compatible. |
| `--max-train-transitions` | PPO | Unset | Limit the transitions used for an update; `1` is a smoke check. |
| `--max-train-total-tokens` | PPO | Unset | Cap tokens selected for training. |
| `--kl-beta` | PPO+KL, GRPO | `0.05` | Frozen-reference KL penalty coefficient. |
| `--num-steps` | GRPO | `1` | Number of training iterations. |
| `--group-size` | GRPO | `8` | Trajectories per group; must be at least `2`. |
| `--rollout-concurrency` | GRPO | Group size | Maximum simultaneous rollouts. Use `1` for the sequential example. |
| `--save-state NAME` | All training modes | Unset | Save training state before optional post-training evaluation. |
| `--eval-after` | All training modes | Disabled | Run one fresh same-task episode after training; this alone does not establish a statistical improvement. |

For ROLL, model weights come from the engine YAML's `pretrain`. Keep the CLI `--model-name` (and `--ref-model-name` for reference-based modes) consistent with it; changing those names alone does not select different ROLL weights.

## Results and logs

```bash
RUN_DIR="$(ls -td "$TASK"/runs/rollout-* | head -1)"
tail -n 80 "$RUN_DIR/client.log"
cat "$RUN_DIR/exit-status.json"
find "$RUN_DIR/results" -name summary.json
```

`exit_code=0` and a completed verifier result indicate a completed rollout. `reward=1` means the task was solved; a valid `reward=0` means it was not solved. Infrastructure failures or missing verifier results are not valid zero scores. Training completion is separate from measured improvement.

Backend/runtime logs are under `$TASK/jobs/`; ROCK service logs are under `$TASK/logs/`. See the quick start for service startup and Harbor log retention.
