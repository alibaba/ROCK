# job/harbor

`HarborJob` examples: run an AI agent benchmark task via the Harbor framework.

## Files

| File | Purpose |
|------|---------|
| [`harbor_demo.py`](./harbor_demo.py) | Entry point — loads `JobConfig.from_yaml()`, runs `Job(config).run()`, iterates trial results |
| [`swe_job_config.yaml.template`](./swe_job_config.yaml.template) | SWE-bench task config template |
| [`swe_job_config-verifier.yaml.template`](./swe_job_config-verifier.yaml.template) | SWE-bench variant with `verifier.mode: native` |
| [`tb_job_config.yaml.template`](./tb_job_config.yaml.template) | Terminal Bench task config template |

## Quick run

```bash
# 1. copy a template and fill in real values
cp swe_job_config.yaml.template swe_job_config.yaml

# 2. set required env vars (OSS credentials, etc.) — see harbor_demo.py docstring
source .env

# 3. run
python harbor_demo.py -c swe_job_config.yaml
```

The `agents:` block uses Harbor's own minimal schema (typical fields: `name`, `model_name`) — see the templates above for the canonical shape.
