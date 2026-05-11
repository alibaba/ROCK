# job/bash

`BashJob` examples: run an arbitrary shell script inside a sandbox.

## Layout

| File / dir | Form | Description |
|------------|------|-------------|
| [`simple_bash_job_demo.sh`](./simple_bash_job_demo.sh) | CLI | Minimal `rock job run --script-content ...` demo |
| [`claw_eval/`](./claw_eval/) | Python SDK | `claw-eval` benchmark wrapped as a BashJob — uses `JobConfig.from_yaml()` + `Job(config).run()` |

Both forms use the same underlying `BashJobConfig` schema; the CLI just wraps it.

## Quick run

```bash
# CLI form
bash simple_bash_job_demo.sh

# SDK form
cd claw_eval
cp claw_eval_bashjob.yaml.template claw_eval_bashjob.yaml  # fill in real values
python run_claw_eval.py
```
