# Configurable Exception Tracebacks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 ROCK 的自定义日志 formatter 默认保留异常类型与完整 traceback，并允许通过环境变量或 admin/proxy YAML 配置恢复旧输出模式。

**Architecture:** 在 `rock.logger` 中维护进程级 YAML 配置值，并在格式化每条日志时按“显式环境变量 > YAML 注入值 > 默认开启”解析有效开关。`StandardFormatter` 只对带有效 `exc_info` 的记录追加全限定异常类型和标准 traceback；admin 与 proxy 在公共 lifespan 中注入 YAML 值，rocklet 继续只使用环境变量或默认值。

**Tech Stack:** Python 3.10–3.12、标准库 `logging`、dataclasses、PyYAML、FastAPI lifespan、pytest、httpx、ruff。

**References:**

- Issue: [alibaba/ROCK#1260](https://github.com/alibaba/ROCK/issues/1260)
- Design: `docs/superpowers/specs/2026-07-19-exception-traceback-logging-design.md`

## Global Constraints

- 新能力默认开启；未设置环境变量且未注入 YAML 时必须输出异常类型和 traceback。
- 配置优先级固定为：显式 `ROCK_LOGGING_EXCEPTION_TRACEBACK_ENABLE` > YAML `logging.exception_traceback_enabled` > 默认 `true`。
- 开关关闭时必须保持 `origin/master` 的日志文本，不附加异常类型，不输出 traceback，也不清理原消息末尾空白。
- 普通日志、日志头字段顺序、时区、颜色、文件/stdout 路由不得改变。
- 只使用标准 `logging.Formatter.formatException()`；不得记录局部变量或额外序列化异常对象。
- 不修改 `rock/common/exception.py`、API 响应、HTTP 状态码和异常处理流程。
- 不修改 `rock/rocklet/server.py`，也不为 rocklet 引入 `RockConfig`。
- 不支持通过 Nacos 动态更新该开关。
- commit message 使用英文 Conventional Commits，且不得包含 `Co-Authored-By`。

---

## File Structure

- `rock/env_vars.py`：声明并解析异常堆栈环境变量。
- `rock/config.py`：定义 `LoggingConfig`，并从 YAML 构造 `RockConfig.logging`。
- `rock/logger.py`：持有进程级配置、解析最终开关并格式化异常类型与 traceback。
- `rock/admin/main.py`：在 admin/proxy 共用 lifespan 中注入 YAML 日志配置。
- `rock-conf/rock-local.yml`：展示本地 admin 的日志开关。
- `rock-conf/rock-dev.yml`：展示开发环境 admin/proxy 的日志开关。
- `rock-conf/rock-test.yml`：展示测试环境 admin 的日志开关。
- `tests/unit/test_config.py`：覆盖 YAML 默认值和 true/false 解析。
- `tests/unit/test_logger.py`：覆盖配置优先级、异常格式、回滚模式、颜色和异常链。
- `tests/unit/admin/test_logging_config.py`：覆盖 admin/proxy 公共启动链路向 logger 传递 YAML 值。

---

### Task 1: Add logging configuration and precedence resolution

**Files:**

- Modify: `rock/env_vars.py:8-13,78-83`
- Modify: `rock/config.py:42-56,519-612`
- Modify: `rock/logger.py:8-13`
- Modify: `tests/unit/test_config.py:1-18`
- Modify: `tests/unit/test_logger.py:1-12`

**Interfaces:**

- Produces: `LoggingConfig(exception_traceback_enabled: bool = True)`.
- Produces: `RockConfig.logging: LoggingConfig`.
- Produces: `configure_logging(*, exception_traceback_enabled: bool) -> None`.
- Produces: `is_exception_traceback_enabled() -> bool`.
- Consumes: existing `env_vars.is_set(name: str)` to distinguish an unset environment variable from its default value.

- [ ] **Step 1: Write failing YAML configuration tests**

In `tests/unit/test_config.py`, extend the existing import and add these tests after `test_rock_config`:

```python
from rock.config import ImageRegistryMirror, LoggingConfig, RockConfig, RuntimeConfig, _resolve_k8s_template_includes


def test_logging_config_defaults_to_exception_tracebacks_enabled():
    config = LoggingConfig()

    assert config.exception_traceback_enabled is True
    assert RockConfig().logging.exception_traceback_enabled is True


@pytest.mark.parametrize("enabled", [True, False])
def test_logging_config_from_yaml(tmp_path, monkeypatch, enabled):
    yaml_path = tmp_path / "rock-test.yml"
    yaml_path.write_text(yaml.safe_dump({"logging": {"exception_traceback_enabled": enabled}}))
    monkeypatch.setenv("ROCK_PYTHON_ENV_PATH", "/usr")
    monkeypatch.setenv("ROCK_ENVHUB_DB_URL", "sqlite:////tmp/test.db")

    rock_config = RockConfig.from_env(str(yaml_path))

    assert rock_config.logging.exception_traceback_enabled is enabled
```

- [ ] **Step 2: Write failing precedence tests**

In `tests/unit/test_logger.py`, add `pytest` and import the new logger interfaces:

```python
import pytest

from rock.logger import configure_logging, init_logger, is_exception_traceback_enabled
```

Add an autouse fixture and precedence tests before the existing logger tests:

```python
@pytest.fixture(autouse=True)
def reset_exception_traceback_config(monkeypatch):
    monkeypatch.delenv("ROCK_LOGGING_EXCEPTION_TRACEBACK_ENABLE", raising=False)
    configure_logging(exception_traceback_enabled=True)
    yield
    configure_logging(exception_traceback_enabled=True)


@pytest.mark.parametrize("enabled", [True, False])
def test_runtime_logging_config_used_when_environment_is_unset(enabled):
    configure_logging(exception_traceback_enabled=enabled)

    assert is_exception_traceback_enabled() is enabled


@pytest.mark.parametrize(
    ("configured", "environment_value", "expected"),
    [
        (False, "true", True),
        (True, "false", False),
        (False, "TRUE", True),
        (True, "FALSE", False),
    ],
)
def test_environment_overrides_runtime_logging_config(monkeypatch, configured, environment_value, expected):
    configure_logging(exception_traceback_enabled=configured)
    monkeypatch.setenv("ROCK_LOGGING_EXCEPTION_TRACEBACK_ENABLE", environment_value)

    assert is_exception_traceback_enabled() is expected
```

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```bash
uv run pytest \
  tests/unit/test_config.py::test_logging_config_defaults_to_exception_tracebacks_enabled \
  tests/unit/test_config.py::test_logging_config_from_yaml \
  tests/unit/test_logger.py::test_runtime_logging_config_used_when_environment_is_unset \
  tests/unit/test_logger.py::test_environment_overrides_runtime_logging_config -v
```

Expected: collection fails because `LoggingConfig`, `configure_logging`, and `is_exception_traceback_enabled` do not exist yet.

- [ ] **Step 4: Add the environment variable**

In the `TYPE_CHECKING` block of `rock/env_vars.py`, place this declaration beside the existing logging variables:

```python
ROCK_LOGGING_EXCEPTION_TRACEBACK_ENABLE: bool = True
```

In `environment_variables`, place this entry after `ROCK_LOGGING_APPEND`:

```python
"ROCK_LOGGING_EXCEPTION_TRACEBACK_ENABLE": lambda: os.getenv(
    "ROCK_LOGGING_EXCEPTION_TRACEBACK_ENABLE", "true"
).lower()
== "true",
```

Run `uv run ruff format rock/env_vars.py` immediately after editing so the multiline lambda is normalized by the repository formatter.

- [ ] **Step 5: Add `LoggingConfig` and YAML parsing**

In `rock/config.py`, add this dataclass after `RedisConfig`:

```python
@dataclass
class LoggingConfig:
    exception_traceback_enabled: bool = True
```

Add the field beside the other top-level `RockConfig` sections:

```python
logging: LoggingConfig = field(default_factory=LoggingConfig)
```

In `RockConfig.from_env()`, add this conversion after the `redis` section:

```python
if "logging" in config:
    kwargs["logging"] = LoggingConfig(**config["logging"])
```

- [ ] **Step 6: Add the process-level resolver**

In `rock/logger.py`, immediately after imports, add:

```python
_exception_traceback_enabled = True


def configure_logging(*, exception_traceback_enabled: bool) -> None:
    global _exception_traceback_enabled
    _exception_traceback_enabled = exception_traceback_enabled


def is_exception_traceback_enabled() -> bool:
    if env_vars.is_set("ROCK_LOGGING_EXCEPTION_TRACEBACK_ENABLE"):
        return env_vars.ROCK_LOGGING_EXCEPTION_TRACEBACK_ENABLE
    return _exception_traceback_enabled
```

The formatter must call `is_exception_traceback_enabled()` for each record; no handler or formatter may cache the result.

- [ ] **Step 7: Run the focused tests and verify GREEN**

Run the same command from Step 3.

Expected: all parameterized cases pass; YAML defaults to `true`, YAML accepts both booleans, and a case-insensitive explicit environment value overrides the configured value in both directions.

- [ ] **Step 8: Run formatting and lint for Task 1**

Run:

```bash
uv run ruff format rock/env_vars.py rock/config.py rock/logger.py tests/unit/test_config.py tests/unit/test_logger.py
uv run ruff check rock/env_vars.py rock/config.py rock/logger.py tests/unit/test_config.py tests/unit/test_logger.py
```

Expected: both commands exit 0.

- [ ] **Step 9: Commit Task 1**

```bash
git add rock/env_vars.py rock/config.py rock/logger.py tests/unit/test_config.py tests/unit/test_logger.py
git commit -m "feat: add configurable traceback logging"
```

---

### Task 2: Preserve exception type and traceback in `StandardFormatter`

**Files:**

- Modify: `rock/logger.py:13-52`
- Modify: `tests/unit/test_logger.py`

**Interfaces:**

- Consumes: `is_exception_traceback_enabled() -> bool` from Task 1.
- Consumes: `record.exc_info`, requiring a non-`None` exception class at index 0.
- Produces: enabled first-line suffix `[exception_type=<module>.<qualname>]` followed by `formatException(record.exc_info)`.
- Produces: disabled output identical to the baseline `StandardFormatter` result.

- [ ] **Step 1: Add a deterministic exception-record helper**

Add `sys` and `httpx` imports to `tests/unit/test_logger.py`, then add:

```python
import sys

import httpx


def _make_exception_record(exc: Exception) -> logging.LogRecord:
    try:
        raise exc
    except Exception:
        return logging.LogRecord(
            name="rock.common.exception",
            level=logging.ERROR,
            pathname="/tmp/exception.py",
            lineno=61,
            msg="Error in http_proxy: %s",
            args=(str(exc),),
            exc_info=sys.exc_info(),
        )
```

Extend the logger import to include `TimezoneFormatter`:

```python
from rock.logger import TimezoneFormatter, configure_logging, init_logger, is_exception_traceback_enabled
```

- [ ] **Step 2: Write failing enabled and rollback-mode tests**

Add:

```python
@pytest.mark.parametrize("log_color_enable", [True, False])
def test_formatter_includes_empty_exception_type_and_traceback_once(log_color_enable):
    formatter = TimezoneFormatter(log_color_enable=log_color_enable, tz_string="Asia/Shanghai")
    record = _make_exception_record(httpx.PoolTimeout(""))

    output = formatter.format(record)

    assert output.count("[exception_type=httpx.PoolTimeout]") == 1
    assert output.count("Traceback (most recent call last):") == 1
    assert "Error in http_proxy: [exception_type=httpx.PoolTimeout]\nTraceback" in output
    assert output.rstrip().endswith("httpx.PoolTimeout")


def test_formatter_disabled_preserves_current_single_line_output():
    configure_logging(exception_traceback_enabled=False)
    formatter = TimezoneFormatter(log_color_enable=False, tz_string="Asia/Shanghai")
    record = _make_exception_record(httpx.PoolTimeout(""))

    output = formatter.format(record)

    assert output.endswith("-- Error in http_proxy: ")
    assert "exception_type=" not in output
    assert "Traceback (most recent call last):" not in output
```

- [ ] **Step 3: Write failing ordinary-log and exception-chain tests**

Add:

```python
def test_formatter_does_not_change_records_without_exc_info():
    formatter = TimezoneFormatter(log_color_enable=False, tz_string="Asia/Shanghai")
    record = logging.LogRecord(
        name="rock.test",
        level=logging.ERROR,
        pathname="/tmp/test.py",
        lineno=10,
        msg="ordinary error",
        args=(),
        exc_info=None,
    )

    configure_logging(exception_traceback_enabled=True)
    enabled_output = formatter.format(record)
    configure_logging(exception_traceback_enabled=False)
    disabled_output = formatter.format(record)

    assert enabled_output == disabled_output
    assert enabled_output.endswith("-- ordinary error")


def test_formatter_preserves_standard_exception_chain():
    formatter = TimezoneFormatter(log_color_enable=False, tz_string="Asia/Shanghai")
    try:
        try:
            raise ValueError("inner")
        except ValueError as exc:
            raise RuntimeError("outer") from exc
    except RuntimeError:
        record = logging.LogRecord(
            name="rock.common.exception",
            level=logging.ERROR,
            pathname="/tmp/exception.py",
            lineno=61,
            msg="chained failure",
            args=(),
            exc_info=sys.exc_info(),
        )

    output = formatter.format(record)

    assert "[exception_type=builtins.RuntimeError]" in output
    assert "ValueError: inner" in output
    assert "The above exception was the direct cause" in output
    assert output.rstrip().endswith("RuntimeError: outer")
```

- [ ] **Step 4: Run formatter tests and verify RED**

Run:

```bash
uv run pytest \
  tests/unit/test_logger.py::test_formatter_includes_empty_exception_type_and_traceback_once \
  tests/unit/test_logger.py::test_formatter_disabled_preserves_current_single_line_output \
  tests/unit/test_logger.py::test_formatter_does_not_change_records_without_exc_info \
  tests/unit/test_logger.py::test_formatter_preserves_standard_exception_chain -v
```

Expected: enabled-mode and exception-chain assertions fail because the current formatter discards `record.exc_info`; rollback-mode and ordinary-log tests may already pass.

- [ ] **Step 5: Implement exception formatting without mutating the record**

In `StandardFormatter.format()`, replace the two final return branches with this exact structure:

```python
message = record.getMessage()
if is_exception_traceback_enabled() and record.exc_info and record.exc_info[0] is not None:
    exception_class = record.exc_info[0]
    exception_type = f"{exception_class.__module__}.{exception_class.__qualname__}"
    message = f"{message.rstrip()} [exception_type={exception_type}]\n{self.formatException(record.exc_info)}"

# Color the header part and keep message in default color
if self.log_color_enable:
    return f"{log_color}{header_str}{RESET} {message}"
return f"{header_str} {message}"
```

Do not assign to `record.exc_text`, `record.msg`, `record.args`, or `record.exc_info`. This lets stdout and file handlers independently render one traceback without sharing cached text or changing downstream handlers.

- [ ] **Step 6: Run formatter tests and verify GREEN**

Run the same command from Step 4.

Expected: all five parameterized formatter cases pass; enabled color and non-color output contain one traceback, disabled output remains a single line, ordinary logs are byte-for-byte equal, and the standard exception chain is present.

- [ ] **Step 7: Run the complete logger unit test file**

Run:

```bash
uv run pytest tests/unit/test_logger.py -v
```

Expected: all existing timestamp and billing tests plus the new configuration and formatter tests pass.

- [ ] **Step 8: Format, lint, and commit Task 2**

```bash
uv run ruff format rock/logger.py tests/unit/test_logger.py
uv run ruff check rock/logger.py tests/unit/test_logger.py
git add rock/logger.py tests/unit/test_logger.py
git commit -m "fix: preserve exception tracebacks in logs"
```

Expected: ruff commands exit 0 and the commit contains only formatter behavior and its tests.

---

### Task 3: Apply YAML configuration to admin and proxy startup

**Files:**

- Modify: `rock/admin/main.py:43-47,68-74,109-118`
- Create: `tests/unit/admin/test_logging_config.py`
- Modify: `rock-conf/rock-local.yml:1`
- Modify: `rock-conf/rock-dev.yml:1`
- Modify: `rock-conf/rock-test.yml:1`

**Interfaces:**

- Consumes: `RockConfig.logging.exception_traceback_enabled` from Task 1.
- Consumes: `configure_logging(*, exception_traceback_enabled: bool) -> None` from Task 1.
- Produces: `_apply_logging_config(rock_config: RockConfig) -> None` as the small startup seam shared by admin and proxy roles.
- Leaves: rocklet behavior unchanged; it receives no YAML injection and resolves environment/default state in the formatter.

- [ ] **Step 1: Write the failing admin configuration forwarding test**

Create `tests/unit/admin/test_logging_config.py` with:

```python
import pytest

from rock.admin import main as admin_main
from rock.config import LoggingConfig, RockConfig


@pytest.mark.parametrize("enabled", [True, False])
def test_apply_logging_config_forwards_yaml_value(monkeypatch, enabled):
    calls = []

    def capture_config(*, exception_traceback_enabled):
        calls.append(exception_traceback_enabled)

    monkeypatch.setattr(admin_main, "configure_logging", capture_config)
    rock_config = RockConfig(logging=LoggingConfig(exception_traceback_enabled=enabled))

    admin_main._apply_logging_config(rock_config)

    assert calls == [enabled]
```

- [ ] **Step 2: Run the admin test and verify RED**

Run:

```bash
uv run pytest tests/unit/admin/test_logging_config.py -v
```

Expected: both parameter cases fail because `rock.admin.main` has neither `configure_logging` nor `_apply_logging_config`.

- [ ] **Step 3: Wire the common admin/proxy lifespan**

Replace the logger import in `rock/admin/main.py` with:

```python
from rock.logger import configure_logging, init_logger, reset_log_file
```

Add this function after the module logger and constants:

```python
def _apply_logging_config(rock_config: RockConfig) -> None:
    configure_logging(exception_traceback_enabled=rock_config.logging.exception_traceback_enabled)
```

In `lifespan()`, call it immediately after YAML loading and before Nacos or service initialization:

```python
rock_config = RockConfig.from_env(config_file_path)
_apply_logging_config(rock_config)
```

Both `--role admin` and `--role proxy` use `create_app()` with this same lifespan, so do not add role-specific branches.

- [ ] **Step 4: Run the admin test and verify GREEN**

Run:

```bash
uv run pytest tests/unit/admin/test_logging_config.py -v
```

Expected: 2 passed and each YAML boolean is forwarded exactly once.

- [ ] **Step 5: Expose the YAML option in shipped configurations**

Add this block at the top of each of `rock-conf/rock-local.yml`, `rock-conf/rock-dev.yml`, and `rock-conf/rock-test.yml`:

```yaml
logging:
  exception_traceback_enabled: true

```

Do not add the section to rocklet configuration because rocklet does not load `RockConfig`.

- [ ] **Step 6: Validate all three YAML files parse and expose the boolean**

Run:

```bash
uv run python -c 'from pathlib import Path; import yaml; paths = [Path("rock-conf/rock-local.yml"), Path("rock-conf/rock-dev.yml"), Path("rock-conf/rock-test.yml")]; values = [yaml.safe_load(path.read_text())["logging"]["exception_traceback_enabled"] for path in paths]; assert values == [True, True, True], values'
```

Expected: command exits 0 with no output.

- [ ] **Step 7: Run focused integration regression**

Run:

```bash
uv run pytest tests/unit/test_logger.py tests/unit/test_config.py tests/unit/admin/test_logging_config.py -v
```

Expected: all tests pass, including the original 67 logger/config tests and every new parameterized case.

- [ ] **Step 8: Format, lint, and commit Task 3**

```bash
uv run ruff format rock/admin/main.py tests/unit/admin/test_logging_config.py
uv run ruff check \
  rock/logger.py \
  rock/env_vars.py \
  rock/config.py \
  rock/admin/main.py \
  tests/unit/test_logger.py \
  tests/unit/test_config.py \
  tests/unit/admin/test_logging_config.py
git add \
  rock/admin/main.py \
  tests/unit/admin/test_logging_config.py \
  rock-conf/rock-local.yml \
  rock-conf/rock-dev.yml \
  rock-conf/rock-test.yml
git commit -m "feat: apply traceback config to admin"
```

Expected: ruff exits 0 and the commit contains only admin/proxy startup wiring, configuration examples, and the startup unit test.

---

## Final Verification

- [ ] Run the complete focused suite:

```bash
uv run pytest tests/unit/test_logger.py tests/unit/test_config.py tests/unit/admin/test_logging_config.py -v
```

- [ ] Run the repository fast-test profile:

```bash
uv run pytest -m "not need_ray and not need_admin and not need_admin_and_network" --reruns 1
```

- [ ] Run final formatting verification without modifying files:

```bash
uv run ruff format --check \
  rock/logger.py \
  rock/env_vars.py \
  rock/config.py \
  rock/admin/main.py \
  tests/unit/test_logger.py \
  tests/unit/test_config.py \
  tests/unit/admin/test_logging_config.py
```

- [ ] Run final lint verification:

```bash
uv run ruff check \
  rock/logger.py \
  rock/env_vars.py \
  rock/config.py \
  rock/admin/main.py \
  tests/unit/test_logger.py \
  tests/unit/test_config.py \
  tests/unit/admin/test_logging_config.py
```

- [ ] Confirm scope and history:

```bash
git status --short
git diff origin/master...HEAD --stat
git log --oneline origin/master..HEAD
```

Expected final state: worktree clean; no changes under `rock/rocklet/` or `rock/common/exception.py`; Issue #1260 design, configuration, formatter, startup integration, and tests appear as intentional Conventional Commits with no `Co-Authored-By` trailers.
