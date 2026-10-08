---
sidebar_position: 8
title: Tinker Quick Start
---

Tinker provides a Python SDK and a local backend for running model sampling and reinforcement learning through a client API. This repository includes the Tinker client, backend, and cookbook examples; ROLL supplies the GPU runtime for inference and training. Local ROCK manages Harbor sandboxes, where SWE-agent executes tools and obtains verifier rewards. Model inference runs separately on the GPU backend, with requests routed through ROCK ModelService. The examples support rollout, PPO, PPO with KL regularization, and GRPO.

The integration supports all SWE-bench Verified tasks distributed in the public Harbor format: each task retains its own environment build and original verifier, with a shared SWE-agent and ModelService integration. This guide uses Qwen3 4B and one task to demonstrate setup and rollout before optional training.

Execution flow: Tinker client → local backend → ROLL model runtime → ROCK ModelService → SWE-agent in Harbor → SWE verifier. Models run on GPUs; tools run inside the sandbox.

## 1. Prepare the environment and source code

Use Linux x86_64 and Python 3.12, with working Docker, GPUs, and public network access. Docker must permit privileged containers. The example assumes eight GPUs: rollout uses GPUs 4–7, while training uses all eight. Adjust the generated YAML before running if your device count or allocation differs. This guide uses the following GPU base image, which includes Torch 2.11, vLLM 0.23, and CUDA 13:

```text
roll-registry.cn-hangzhou.cr.aliyuncs.com/roll/pytorch:nvcr-26.03-py3-torch2110-vllm0230
```

```bash
nvidia-smi
docker info
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

git clone https://github.com/alibaba/ROCK.git
git clone --branch feat/tinker-runtime --single-branch https://github.com/alibaba/ROLL.git
export ROCK="$(realpath ROCK)"
export ROLL="$(realpath ROLL)"
export TASK="$HOME/tinker-local"
export QS="$ROCK/examples/tinker_quick_start"
mkdir -p "$TASK/logs"
```

Check that `$QS/setup.sh` and `$ROLL/requirements_tinker.txt` exist. In each new terminal, set the same `ROCK`, `ROLL`, `TASK`, `QS`, and (after section 4) `TASK_ID` variables, then run `export PY="$TASK/control-venv/bin/python"`. Use a new absolute path for `TASK`.

## 2. Install the ROLL environment

ROLL's `requirements_tinker.txt` contains the additional Tinker dependencies and is maintained against the official `requirements_common.txt` and `requirements_torch2100_vllm.txt`. The specified image already provides the GPU software stack. The setup script uses uv to add dependencies in a separate environment that inherits the image's packages, and checks that the versions and source paths of Torch, vLLM, CUDA, Transformer Engine, and other protected packages have not changed.

```bash
bash "$QS/setup.sh" runtime --gpu-check > "$TASK/logs/setup-runtime.log" 2>&1
cat "$TASK/runtime-venv/setup-public-runtime.json"
```

`--gpu-check` runs a small check on visible GPU 0. If other jobs are running, prefix the command with `CUDA_VISIBLE_DEVICES=<idle GPU index>`.

## 3. Install ROCK and Tinker

```bash
bash "$QS/setup.sh" control > "$TASK/logs/setup-control.log" 2>&1
export PY="$TASK/control-venv/bin/python"
cat "$TASK/control-venv/import-check.json"
```

This runs `uv sync --project "$ROCK/rock-tinker/local" --frozen --group control --no-dev`, using `rock-tinker/local/pyproject.toml` and its `uv.lock` to install a separate Tinker control environment, then builds the combined `rl-rock` wheel. ROCK's root dependencies, Python version range, and package sources remain unchanged. The control environment uses CPU PyTorch, while the GPU environment reuses the image's software stack. The wheel is written to `$TASK/dist/`.

## 4. Prepare the public task and Harbor sandbox

```bash
export TASK_ID=sympy__sympy-19637
WHEELS=("$TASK"/dist/rl_rock-*.whl)
test "${#WHEELS[@]}" -eq 1 && test -f "${WHEELS[0]}" || exit 1
"$PY" "$QS/prepare_sandbox.py" --rock-root "$ROCK" --task "$TASK" --task-id "$TASK_ID" \
  --wheel "${WHEELS[0]}" \
  > "$TASK/logs/prepare-sandbox.log" 2>&1
```

This downloads only the selected public Harbor task and prepares the Harbor controller image `tinker-harbor:local` and a reusable, pinned SWE-agent runtime bundled in that controller image. It preserves the task's `instruction.md`, `task.toml`, complete `environment/Dockerfile`, and original verifier. Harbor builds and executes the task's own environment; SWE-agent runs in a separate Python environment, so its dependencies do not replace the task's dependencies. Runtime preparation is automatic and does not require manually entering the task container.

The task is stored in `$TASK/assets/harbor-tasks/$TASK_ID/`. Source and integrity information is saved in `$TASK/sandbox-preparation/manifest.json`. Images and the agent runtime can be reused after preparation. To select another task, such as `pallets__flask-5014`, change `TASK_ID` and use the same ID in preparation, configuration, and execution. A fresh `TASK` directory keeps each example's configuration and results separate. Each task's image and verifier dependencies must be accessible from the sandbox network.

## 5. Configure the local services

```bash
export NODE_IP="$(ip -4 route get 1.1.1.1 | sed -n 's/.* src \([^ ]*\).*/\1/p' | head -1)"
"$PY" "$QS/configure.py" \
  --roll-root "$ROLL" --task-root "$TASK" --task-id "$TASK_ID" \
  --model Qwen/Qwen3-4B-Instruct-2507 \
  --outer-image tinker-harbor:local --node-ip "$NODE_IP" --rollout-only
```

ROLL downloads and caches weights automatically from the model ID. You can also use `--model /absolute/path/to/model`. Hugging Face is the default source; add `--model-download-type MODELSCOPE` to use ModelScope.

Check these generated settings for your machine:

| File | Common settings |
|---|---|
| `config/engine.yaml` | `pretrain`, `actor_infer.device_mapping`, inference parallelism, and token budgets |
| `config/harbor.yaml` | ROCK address, `cluster: local`, sandbox resources, and task path |
| `config/runtime.yaml` | Backend address, GPU Python path, and checkpoint directory |
| `config/admin.yaml` | Local ROCK service configuration |

The default ports are 18080 for Admin, 19210 for the backend, 22555 for the host ROCKlet, and 28080 for ModelService. Inference uses GPUs 4–7. Keep parallelism consistent with the device count when changing GPU allocation. `--rollout-only` disables training and the reference model, leaving the training GPUs unused. The generator refuses to overwrite existing configuration; edit the YAML directly for later changes.

## 6. Start local ROCK

First check that ports 18080, 19210, 22555, and 28080 are not in use by other jobs. Run the following commands in two terminals, keeping both processes in the foreground:

```bash
# Terminal A
bash "$QS/serve.sh" worker
```

```bash
# Terminal B
bash "$QS/serve.sh" admin
```

In another terminal, wait for the services to become ready:

```bash
curl -fsS http://127.0.0.1:18080/
ss -lntp | grep -E ':18080|:22555'
```

Local ROCK starts the required Ray components; you do not need to start a separate Ray head. Reuse services only when they use the same `TASK/config/admin.yaml` and key. When switching `TASK`, first stop the two services you started with Ctrl-C, then start them again with the new `TASK` as described above. Do not stop other users' services.

To change the local Admin port, add `--admin-port 18081` to the configuration command in section 5. Both the generated ROCK base URL and the `serve.sh admin` listener use `http://127.0.0.1:18081`; update the health-check URL above accordingly.

### Connect to your own ROCK cluster

The default ROCK endpoint is `http://127.0.0.1:18080`, with `cluster: local`. To use an existing ROCK deployment, pass its Admin API URL and configured cluster name when generating a fresh configuration:

```bash
export ROCK_BASE_URL=https://rock.example.com
export ROCK_CLUSTER=my-cluster
"$PY" "$QS/configure.py" \
  --roll-root "$ROLL" --task-root "$TASK" --task-id "$TASK_ID" \
  --model Qwen/Qwen3-4B-Instruct-2507 \
  --outer-image registry.example.com/team/tinker-harbor:local \
  --node-ip "$NODE_IP" --rollout-only \
  --rock-base-url "$ROCK_BASE_URL" --rock-cluster "$ROCK_CLUSTER"
```

Use this instead of the configuration command in section 5. Publish the prepared Harbor controller image to a registry accessible to your cluster and substitute its real image name. With an existing deployment, skip the local worker/Admin startup above; Tinker and ROLL still run locally.

For an existing configuration, change these fields in `$TASK/config/harbor.yaml`:

```yaml
environment:
  base_url: https://rock.example.com
  cluster: my-cluster
  image: registry.example.com/team/tinker-harbor:local
  extra_headers: {}
```

Keep the other fields. If your deployment requires authentication headers, add the header names and values provided by its administrator under `extra_headers`; this mapping is passed to the ROCK SDK. Do not copy the local Admin encryption key to the remote cluster or commit credentials. The Tinker host must be able to reach the Admin API and sandbox endpoints used by the ROCK SDK. ModelService runs inside the ROCK sandbox: the inner SWE-agent connects to that service (port 28080 by default), while the pull runner exchanges inference requests and responses through ROCK commands. The sandbox does not need a direct HTTP connection to the GPU backend. Your cluster must allow the nested Harbor task container to reach ModelService in its outer sandbox.

## 7. Start Tinker and run rollout

```bash
bash "$QS/run.sh" rollout --task-id "$TASK_ID"
```

This invokes the cookbook's `eval_swe_bench.py`, starts the local backend, waits for the GPU runtime, creates the client, and executes the task. It shuts down this run's backend and runtime when finished. You do not need to start another backend on the same port.

By default, the same task runs for at most 32 turns, with up to 8192 generated tokens per call and temperature 1.0. The configuration generator uses the same budget from the last successful scored rollout for evaluation, PPO, PPO+KL, and GRPO; no configuration patch is needed:

| Setting | Default |
|---|---:|
| Agent input tokens | 32768 |
| Agent output tokens | 8192 |
| Inference context length | 65536 |
| Characters per tool observation | 12000 |
| Recent tool observations retained | 8 |

For later changes, edit `config/harbor.yaml` and `config/engine.yaml` directly. Input plus output must fit within the model service's context limit. When changing the output budget, update the agent's `max_output_tokens`, `completion_kwargs.max_tokens`, and the client's `--max-tokens` together. Save the changes and rerun rollout. Neither a larger budget nor identical settings guarantee that every attempt solves the task; use the verifier reward to determine the result.

## 8. Inspect cookbook client results and logs

Rollout, PPO, PPO+KL, and GRPO all run through the cookbook in `rock-tinker`, which communicates with the backend through the Tinker SDK. Start with the cookbook client's output and saved results; no separate SDK query program is required.

`run.sh` prints the current `Run directory`, redirects cookbook output to `client.log` there, and prints the exit code and log path when it finishes. In another terminal, set the same `TASK` and `PY`, then follow the client log:

```bash
MODE=rollout  # Or ppo, ppo-kl, or grpo
RUN_DIR="$(ls -td "$TASK/runs/$MODE-"* | head -1)"
tail -f "$RUN_DIR/client.log"
```

Ctrl-C stops following the log without stopping the job in the other terminal. After the job finishes, inspect its exit status and result file:

```bash
cat "$RUN_DIR/exit-status.json"
RESULT_DIR="$(find "$RUN_DIR/results" -mindepth 1 -maxdepth 1 -type d | head -1)"
if [ "$MODE" = rollout ]; then
  "$PY" -m json.tool "$RESULT_DIR/summary.json"
else
  "$PY" -m json.tool "$RESULT_DIR/result.json"
fi
```

You can also use the exact directory printed after `Results dir:` in `client.log`. Files are written when their corresponding stage completes; if the run fails early, check the error in `client.log` first.

| Mode | Files saved by the cookbook (relative to `RESULT_DIR`) | What to inspect |
|---|---|---|
| Rollout | `summary.json` | `task_id`, `reward`, `episode_done`, `turns_used`, and `time_seconds` |
| PPO / PPO+KL | `result.json`, `trajectory.txt` | Rollout `reward`, training `fwdbwd_metrics`, `optim_metrics`, `train_elapsed`, and `published_sampling_client` |
| GRPO | `result.json`, `step_000/step_result.json`, etc. | Run-level `steps`; each step's `group_returns`, `group_advantages`, `fwdbwd_metrics`, and `optim_metrics` |
| Individual GRPO trajectories | `step_000/rollout_00.txt`, `step_000/rollout_00.json`, etc. | Model/tool interactions and the trajectory's `return`, `advantage`, and `turns` |
| Saved training state | `checkpoint.json` (only with `--save-state NAME`) | Checkpoint `name` and `path` returned by the backend; this JSON records the location, not the model weights |

A complete rollout requires `exit_code=0` in `exit-status.json` and `episode_done=true` in the generated `summary.json`. Then inspect `reward`: `reward=1` means the task was solved, while a validly scored `reward=0` means it was not solved in this run. If the client fails, is interrupted, or does not produce a summary, a scoring file inside the sandbox alone does not establish client completion. `SESSION_END` indicates that the agent process ended; it is not itself a timeout error. The standalone rollout cookbook currently saves a summary and tokens from the first action, not a complete agent trajectory.

During training, `client.log` prints `Rollout done` (group rollout progress for GRPO), `forward_backward done`, `optim_step done`, and `Weights published to sampler`. Check these together with the saved training metrics to confirm training and weight publication completed. Messages such as `skipping step`, `skipping train step`, or `nothing to train on` mean that a training step was skipped; exit code 0 alone does not establish that training occurred. GRPO's `rollouts_succeeded` counts successfully executed trajectories, not trajectories with reward 1; inspect `group_returns` for scores. Client result files remain available locally after the backend/runtime shuts down.

For errors reported by the client, inspect the underlying component logs:

| Component | Log location |
|---|---|
| Tinker backend and ROLL GPU runtime | `$TASK/jobs/<job>/`; the `TINKER_SERVER_JOB` line in `client.log` contains this job's `log_dir` |
| Local ROCK Admin / ROCKlet | `$TASK/logs/rock-admin.log`, `$TASK/logs/rock-worker.log` |
| Harbor, SWE-agent, and verifier | `/data/logs/user-defined/jobs/` inside the running sandbox |

The original scoring files in each Harbor trial are `verifier/report.json` and `verifier/reward.txt`. Check logs for dependency download or execution failures rather than treating the reward file alone as evidence of valid grading. Sandboxes are deleted after completion, so export complete agent/verifier traces while the sandbox is running. The cookbook's local output remains the normal entry point for inspecting results.

## 9. Run training when needed

Training requires eight idle GPUs. PPO, PPO+KL, and GRPO share the rollout budget in section 7: 32768 input tokens, 8192 output tokens, a 65536-token context, and eight recent tool observations of up to 12000 characters each. The generator enables training, the reference model, and inference together. These larger budgets also increase training memory requirements. Existing generated configurations are not updated automatically; generate a new training directory as below.

To switch from rollout to training, wait for the client to exit, then stop any local worker and Admin that you started with Ctrl-C. Keep a shared ROCK deployment running. Keep the original task directory, and create a new training directory in the same terminal:

```bash
export ROLLOUT_TASK="$TASK"
export TASK="$HOME/tinker-train"
mkdir -p "$TASK/logs"
ln -s "$ROLLOUT_TASK/runtime-venv" "$TASK/runtime-venv"
ln -s "$ROLLOUT_TASK/control-venv" "$TASK/control-venv"
ln -s "$ROLLOUT_TASK/assets" "$TASK/assets"
export PY="$TASK/control-venv/bin/python"
"$PY" "$QS/configure.py" \
  --roll-root "$ROLL" --task-root "$TASK" --task-id "$TASK_ID" \
  --model Qwen/Qwen3-4B-Instruct-2507 \
  --outer-image tinker-harbor:local --node-ip "$NODE_IP"
```

This reuses the installed environments, public task, and sandbox images without downloading them again. The new directory must not have been configured before. For an existing ROCK cluster, also pass the same `--rock-base-url`, `--rock-cluster`, and registry image as in section 6. Otherwise restart the local worker and Admin for this directory as described in section 6. Then run these commands:

```bash
bash "$QS/run.sh" ppo --learning-rate 1e-6 --lora-rank 0 --max-train-transitions 1
bash "$QS/run.sh" ppo-kl --learning-rate 1e-6 --lora-rank 0 --kl-beta 0.05
bash "$QS/run.sh" grpo --learning-rate 1e-6 --lora-rank 0 \
  --num-steps 1 --group-size 2 --rollout-concurrency 1
```

Run the three commands sequentially, waiting for each to exit before starting the next. The PPO example trains on only one transition to check the training call; remove `--max-train-transitions 1` to train on the full trajectory. Add `--save-state quick-start-final` to save state. Completing a training command does not establish an improvement in model quality; measure that with a separate before-and-after evaluation.
