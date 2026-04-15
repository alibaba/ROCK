# `rock job run` CLI 参数设计方案

## 1. 背景

当前 `rock/cli/command/job.py` 对 bash 和 harbor 两种 job 类型使用不同的输入方式：

| job_type | 必填参数 | 其他参数 |
|----------|----------|----------|
| `bash`   | `--script` 或 `--script-content`（二选一） | `--image / --memory / --cpus / --env / --local-path / --timeout / ...` |
| `harbor` | `--job_config`（YAML 文件） | `--image`（覆盖 YAML 中的字段） |

存在的问题：

1. **Bash 不支持 YAML 配置** — 真实业务里 bash 也会携带大量 env、setup_commands、uploads、image 等字段，挤在一行 CLI flag 里既难写又难复用。
2. **`--type` 与入参强耦合** — `--type bash` 强制走 CLI flags 路径，`--type harbor` 强制走 YAML 路径，入参形态随类型跳变。实际上，"从 YAML 载入 config" 和 "类型"是正交的两个维度。
3. **错误提示不友好** — 现有实现只打印一行 `logger.error`，不显示 help / 示例 / 合法组合，用户无法自助排查。
4. **参数互斥关系隐式** — `--script` vs `--script-content`、`--job_config` vs `--script*`、`--type` vs YAML 里的类型等互斥/冲突关系散落在校验代码里，没有集中说明。

已具备的能力（参考 `rock/sdk/job/config.py`）：

- `JobConfig.from_yaml(path)` 已支持类型自动识别 —— 通过 `HarborJobConfig → BashJobConfig` 顺序的严格模型校验（`extra="forbid"`）定位子类。
- `BashJobConfig` / `HarborJobConfig` 都是 `from_yaml` 的可用入口。

所以 CLI 层只需要把"YAML 路径"升格为一等公民，即可统一两种类型的入参形态。

---

## 2. 设计目标

1. **YAML 优先** — `--job_config` 是推荐路径，适用于所有 job 类型；CLI flags 是简化入口，仅适用于 bash。
2. **单一输入模式** — `--job_config` 模式与 flags 模式互斥，不允许混用以避免配置优先级歧义。
3. **类型自动识别** — 使用 `--job_config` 时不需要 `--type`；`--type` 仅作为 flags 模式下的显式声明（默认 `bash`）。
4. **错误提示优雅** — 任何参数校验失败都：
   - 清晰指出错了什么；
   - 给出合法组合示例；
   - 最后附上 `rock job run --help` 提示。
5. **argparse 原生风格** — 使用 `parser.error()`，退出码为 2，日志/错误都写 stderr，与其他 CLI 子命令一致。

---

## 3. 入参规则

### 3.1 两种互斥的输入模式

```
┌──────────────────────────────────────────────┬───────────────────────────────────────────────┐
│  Mode A: YAML config（推荐，通用）              │  Mode B: CLI flags（简化，仅 bash）           │
├──────────────────────────────────────────────┼───────────────────────────────────────────────┤
│  rock job run --job_config job.yaml                │  rock job run [--type bash]                   │
│                [--image ...] [--cluster ...]   │                --script path/to/script.sh     │
│                [--base-url ...] [--timeout N]  │               (或 --script-content "...")     │
│                                                │                [--image / --memory / --cpus] │
│                                                │                [--env / --local-path / ...]  │
└──────────────────────────────────────────────┴───────────────────────────────────────────────┘
```

### 3.2 参数分类

| 组别 | 参数 | Mode A 允许 | Mode B 允许 |
|------|------|:-----------:|:-----------:|
| **模式开关** | `--job_config PATH` | ✅（必填） | ❌ |
| **类型声明** | `--type {bash,harbor}` | ❌（类型由 YAML 决定） | ✅（可选，默认 `bash`；若指定 `harbor` 必须配 `--job_config`） |
| **bash 脚本** | `--script / --script-content` | ❌ | ✅（二选一，必填） |
| **环境覆盖** | `--image / --memory / --cpus` | ✅（覆盖 YAML 中对应字段） | ✅ |
| **运行位置** | `--base-url / --cluster / --extra-headers / --xrl-authorization` | ✅ | ✅ |
| **环境变量** | `--env KEY=VALUE`（可重复） | ✅（追加/覆盖 YAML 的 `environment.env`） | ✅ |
| **文件上传** | `--local-path / --target-path` | ✅（追加到 YAML 的 `environment.uploads`） | ✅ |
| **超时** | `--timeout N` | ✅（覆盖 YAML 的 `timeout`） | ✅ |

### 3.3 校验矩阵

| 情况 | 判定 | 行为 |
|------|------|------|
| 两种 mode 都没给（既没 `--job_config` 也没 `--script*`） | 错误 | 打印缺参提示 + usage + help 指引 |
| 同时给 `--job_config` 和 `--script` / `--script-content` | 错误 | 明确告诉用户两种模式互斥，二选一 |
| 同时给 `--script` 和 `--script-content` | 错误 | 告知"inline 内容和文件路径只能二选一" |
| `--type harbor` 但没 `--job_config` | 错误 | 告知 harbor 类型必须走 YAML |
| `--type bash` + `--job_config` 指向 HarborJobConfig YAML | 错误 | 告知类型冲突（YAML 自动识别为 harbor） |
| `--job_config` 文件不存在 / 解析失败 | 错误 | 转发 `from_yaml` 的 `ValueError` 细节 |

---

## 4. 错误提示设计

所有入参校验失败都走一个统一的 `_fail(parser, msg, hint=None)` 辅助函数，保持风格一致。

### 4.1 辅助函数示意

```python
def _fail(parser: argparse.ArgumentParser, msg: str, *, hint: str | None = None) -> None:
    """Emit a consistent CLI error: message + optional hint + help指引，然后退出 2。"""
    lines = [msg]
    if hint:
        lines.append("")
        lines.append(hint)
    lines.append("")
    lines.append("Run `rock job run --help` for full usage.")
    parser.error("\n".join(lines))
```

`parser.error()` 会：先打印 usage（来自 argparse），再把我们给的消息写到 stderr，最后以 exit code 2 退出 —— 这正是 argparse 原生校验失败时的行为，一致性好。

### 4.2 错误样例（对用户实际看到的输出）

**Case 1: 什么都没给**

```
usage: rock job run [-h] [--type {bash,harbor}] [--job_config CONFIG] [--script SCRIPT] ...
rock job run: error: Missing job definition. Provide either a YAML config or inline script.

Examples:
  rock job run --job_config job.yaml                     # any job type, auto-detected
  rock job run --script path/to/run.sh               # bash, script file
  rock job run --script-content "echo hi"            # bash, inline snippet

Run `rock job run --help` for full usage.
```

**Case 2: 同时给了 `--job_config` 和 `--script`**

```
rock job run: error: --job_config is mutually exclusive with --script / --script-content.

Pick one mode:
  • YAML mode:  rock job run --job_config job.yaml
  • flags mode: rock job run --script run.sh

Run `rock job run --help` for full usage.
```

**Case 3: `--type harbor` 没带 `--job_config`**

```
rock job run: error: --type harbor requires --job_config <yaml>.

Harbor jobs cannot be expressed purely via CLI flags.
Example:
  rock job run --job_config harbor.yaml

Run `rock job run --help` for full usage.
```

**Case 4: `--script` 和 `--script-content` 同时给**

```
rock job run: error: --script and --script-content are mutually exclusive (pick a file path OR an inline snippet).

Run `rock job run --help` for full usage.
```

**Case 5: YAML 解析失败（来自 `JobConfig.from_yaml`）**

```
rock job run: error: Failed to load --job_config 'job.yaml':
YAML does not match any known job type.
  As HarborJobConfig: 1 validation error for HarborJobConfig
    experiment_id: Field required [type=missing, ...]
  As BashJobConfig:   1 validation error for BashJobConfig
    agents: Extra inputs are not permitted [type=extra_forbidden, ...]

Run `rock job run --help` for full usage.
```

---

## 5. 实现草图

```python
# rock/cli/command/job.py

import argparse
from pathlib import Path

from rock.cli.command.command import Command
from rock.logger import init_logger

logger = init_logger(__name__)


def _fail(parser: argparse.ArgumentParser, msg: str, *, hint: str | None = None) -> None:
    """Emit a consistent CLI error + help pointer, then exit 2."""
    parts = [msg]
    if hint:
        parts.extend(["", hint])
    parts.extend(["", "Run `rock job run --help` for full usage."])
    parser.error("\n".join(parts))


class JobCommand(Command):
    name = "job"

    # The run sub-parser is stashed here so arun() can call parser.error().
    _run_parser: argparse.ArgumentParser | None = None

    async def arun(self, args: argparse.Namespace):
        if args.job_command == "run":
            await self._job_run(args)
        else:
            logger.error(f"Unknown job subcommand: {args.job_command}")

    async def _job_run(self, args: argparse.Namespace):
        from rock.sdk.job import Job
        from rock.sdk.job.config import BashJobConfig, JobConfig

        parser = self._run_parser  # set in add_parser_to

        # ── 1. 模式互斥校验 ──────────────────────────────────────────
        has_config = bool(args.config)
        has_script = bool(args.script or args.script_content)

        if not has_config and not has_script:
            _fail(
                parser,
                "Missing job definition. Provide either a YAML config or inline script.",
                hint=(
                    "Examples:\n"
                    "  rock job run --job_config job.yaml                     # any job type, auto-detected\n"
                    "  rock job run --script path/to/run.sh               # bash, script file\n"
                    "  rock job run --script-content \"echo hi\"            # bash, inline snippet"
                ),
            )

        if has_config and has_script:
            _fail(
                parser,
                "--job_config is mutually exclusive with --script / --script-content.",
                hint=(
                    "Pick one mode:\n"
                    "  • YAML mode:  rock job run --job_config job.yaml\n"
                    "  • flags mode: rock job run --script run.sh"
                ),
            )

        if args.script and args.script_content:
            _fail(
                parser,
                "--script and --script-content are mutually exclusive "
                "(pick a file path OR an inline snippet).",
            )

        if args.type == "harbor" and not has_config:
            _fail(
                parser,
                "--type harbor requires --job_config <yaml>.",
                hint=(
                    "Harbor jobs cannot be expressed purely via CLI flags.\n"
                    "Example:\n"
                    "  rock job run --job_config harbor.yaml"
                ),
            )

        # ── 2. 组装 config ──────────────────────────────────────────
        if has_config:
            config = self._config_from_yaml(parser, args)
        else:
            config = self._config_from_flags(args)

        # ── 3. 用 CLI overrides 打补丁（两种模式共用）────────────────
        self._apply_overrides(config, args)

        # ── 4. 运行 ────────────────────────────────────────────────
        try:
            result = await Job(config).run()
            if result.trial_results:
                for tr in result.trial_results:
                    output = getattr(tr, "raw_output", None) or ""
                    if output:
                        print(output)
            logger.info(f"Job completed: status={result.status}")
        except Exception as e:
            logger.error(f"Job failed: {e}")

    # ── helpers ───────────────────────────────────────────────────

    def _config_from_yaml(self, parser, args):
        from rock.sdk.job.config import BashJobConfig, JobConfig

        path = args.config
        if not Path(path).is_file():
            _fail(parser, f"--job_config path does not exist: {path}")

        try:
            config = JobConfig.from_yaml(path)  # 自动识别 Bash/Harbor
        except (ValueError, Exception) as exc:
            _fail(parser, f"Failed to load --job_config {path!r}:\n{exc}")

        # --type 显式声明时做一次一致性检查
        if args.type:
            expected_type = args.type
            actual_type = "bash" if isinstance(config, BashJobConfig) else "harbor"
            if expected_type != actual_type:
                _fail(
                    parser,
                    f"--type {expected_type} does not match YAML (detected as {actual_type}).",
                    hint="Remove --type and let the YAML decide, or pass a matching config file.",
                )
        return config

    def _config_from_flags(self, args):
        from rock.sdk.bench.models.trial.config import RockEnvironmentConfig
        from rock.sdk.job.config import BashJobConfig

        env = {}
        for item in args.env or []:
            key, _, value = item.partition("=")
            env[key] = value

        uploads = [(args.local_path, args.target_path)] if args.local_path else []

        return BashJobConfig(
            script=args.script_content,
            script_path=args.script,
            environment=RockEnvironmentConfig(
                image=args.image,
                memory=args.memory,
                cpus=args.cpus,
                base_url=args.base_url,
                cluster=args.cluster,
                extra_headers=args.extra_headers,
                xrl_authorization=args.xrl_authorization,
                uploads=uploads,
                auto_stop=True,
                env=env,
            ),
            timeout=args.timeout,
        )

    def _apply_overrides(self, config, args):
        """Apply CLI overrides that are valid in both modes (e.g. --image)."""
        env = config.environment
        if args.image:
            env.image = args.image
        if args.memory:
            env.memory = args.memory
        if args.cpus:
            env.cpus = args.cpus
        if args.base_url:
            env.base_url = args.base_url
        if args.cluster:
            env.cluster = args.cluster
        if args.xrl_authorization:
            env.xrl_authorization = args.xrl_authorization
        # --env / --local-path / --timeout 在 YAML 模式下也追加/覆盖
        for item in args.env or []:
            key, _, value = item.partition("=")
            env.env[key] = value
        if args.local_path:
            env.uploads = list(env.uploads) + [(args.local_path, args.target_path)]
        if args.timeout is not None:
            config.timeout = args.timeout
        env.auto_stop = True

    # ── parser ────────────────────────────────────────────────────

    @staticmethod
    async def add_parser_to(subparsers: argparse._SubParsersAction):
        job_parser = subparsers.add_parser("job", help="Manage sandbox jobs")
        job_subparsers = job_parser.add_subparsers(dest="job_command")

        run_parser = job_subparsers.add_parser(
            "run",
            help="Run a job in a sandbox",
            description=(
                "Run a sandbox job in one of two modes:\n"
                "  (1) YAML mode  : --job_config <file>         (type auto-detected)\n"
                "  (2) flags mode : --script / --script-content  (bash only)\n"
                "The two modes are mutually exclusive."
            ),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )

        # mode switches
        run_parser.add_argument(
            "--type",
            choices=["bash", "harbor"],
            default=None,
            help="Explicit job type (flags mode only; YAML mode auto-detects).",
        )
        run_parser.add_argument("--job_config", default=None, help="YAML config path (any job type).")
        run_parser.add_argument("--script", default=None, help="Bash script file path (flags mode).")
        run_parser.add_argument("--script-content", default=None, help="Inline bash snippet (flags mode).")

        # shared overrides
        run_parser.add_argument("--image", default=None, help="Sandbox image (overrides YAML).")
        run_parser.add_argument("--memory", default=None, help="Memory (e.g. 8g). Overrides YAML.")
        run_parser.add_argument("--cpus", default=None, type=float, help="CPU count. Overrides YAML.")
        run_parser.add_argument("--timeout", type=int, default=None, help="Timeout in seconds.")
        run_parser.add_argument("--local-path", default=None, help="Local dir to upload.")
        run_parser.add_argument("--target-path", default="/root/job", help="Target dir in sandbox.")
        run_parser.add_argument("--base-url", default=None, help="Admin service base URL.")
        run_parser.add_argument("--cluster", default=None, help="Cluster name (e.g. vpc-sg-sl-a).")
        run_parser.add_argument("--extra-headers", default=None, help="Extra HTTP headers (JSON).")
        run_parser.add_argument(
            "--env",
            action="append",
            default=None,
            metavar="KEY=VALUE",
            help="Environment variable, repeatable (e.g. --env FOO=bar --env BAZ=qux).",
        )
        run_parser.add_argument("--xrl-authorization", default=None, help="XRL authorization token.")

        # stash on command class so arun() can call parser.error() consistently
        JobCommand._run_parser = run_parser
```

> 注：`parser.error()` 在 argparse 内部 `sys.exit(2)`，因此 `arun()` 里不需要 return；后续代码永远不会执行到。

---

## 6. 使用示例

### 6.1 Bash — flags 模式（原行为保持）

```bash
rock job run --script ./train.sh \
    --image python:3.11 --memory 8g --cpus 4 \
    --env FOO=bar --env BAZ=qux \
    --local-path ./workspace --target-path /root/job
```

### 6.2 Bash — YAML 模式（新能力）

`bash_job.yaml`：

```yaml
script_path: ./train.sh
timeout: 7200
environment:
  image: python:3.11
  memory: 8g
  cpus: 4
  env:
    FOO: bar
    BAZ: qux
  uploads:
    - ["./workspace", "/root/job"]
```

```bash
rock job run --job_config bash_job.yaml

# 仍可以用 CLI override 局部字段
rock job run --job_config bash_job.yaml --image python:3.12 --timeout 1800
```

### 6.3 Harbor — YAML 模式（与之前一致）

```bash
rock job run --job_config harbor.yaml
rock job run --job_config harbor.yaml --image harbor-runner:v2   # 覆盖 image
```

---

## 7. 向后兼容

| 用法 | 是否仍然可用 | 说明 |
|------|:------------:|------|
| `rock job run --type bash --script foo.sh` | ✅ | 等价于 flags 模式，`--type bash` 为显式声明。 |
| `rock job run --script foo.sh` | ✅ | `--type` 默认视为 `bash`。 |
| `rock job run --type harbor --job_config harbor.yaml` | ✅ | 显式声明 + YAML 校验一致性。 |
| `rock job run --job_config harbor.yaml` | ✅ | 推荐新用法，自动识别。 |
| `rock job run --job_config ... --script ...` | ❌（新增校验） | 之前不明确，现在明确报错。 |
| `rock job run`（无参） | ❌（错误消息更友好） | 之前报错含糊，现在打印 usage + 示例。 |

没有被移除的 flag，没有破坏性变更。

---

## 8. 变更清单

- `rock/cli/command/job.py`
  - 新增 `_fail()` 辅助函数
  - 拆分 `_config_from_yaml` / `_config_from_flags` / `_apply_overrides`
  - `--type` 默认改为 `None`（由模式推导），仅做一致性校验
  - `--timeout` 默认改为 `None`（不覆盖 YAML 中的 timeout，除非显式给出）
  - `--job_config` 支持 bash 类型
  - 所有入参错误走 `parser.error()`，统一格式
  - `description` 里写清楚两种模式（`RawDescriptionHelpFormatter`）
- `tests/unit/cli/command/test_job.py`（新增 / 扩展）
  - Case: 两种模式互斥
  - Case: `--type harbor` 无 `--job_config`
  - Case: YAML 模式下 `--type` 与 YAML 一致 / 冲突
  - Case: bash YAML 正常加载 + CLI override 生效
  - Case: `parser.error()` 以 exit code 2 退出、stderr 包含 usage

---

## 9. 未来扩展

1. **`rock job validate --job_config foo.yaml`** — 只做 YAML 校验 + 类型识别，便于 CI 中 lint。
2. **`rock job run --job_config foo.yaml --dry-run`** — 打印最终合并后的 config（含 CLI overrides）但不实际提交。
3. **多个 `--job_config`** — 支持 config 合并（base + override），对应 `JobConfig.model_validate` 的深合并。
4. **`ROCK_JOB_CONFIG` 环境变量** — 作为 `--job_config` 的默认值，与 `ROCK_CONFIG` 风格一致。
