# nohup 模式 exit_code 修复 — Interface Contract

本次修复不新增任何 API 端点，通过在 `Sandbox.arun()` 新增 `capture_exit_code` 开关参数，让调用方显式选择启用真实退出码捕获。

---

## 1. `Sandbox.arun()` — 签名变更

### 方法签名

```python
# 改前
async def arun(
    self,
    cmd: str,
    session: str = None,
    wait_timeout: int = 300,
    wait_interval: int = 10,
    mode: RunModeType = RunMode.NORMAL,
    response_limited_bytes_in_nohup: int | None = None,
    ignore_output: bool = False,
    output_file: str | None = None,
) -> Observation:

# 改后
async def arun(
    self,
    cmd: str,
    session: str = None,
    wait_timeout: int = 300,
    wait_interval: int = 10,
    mode: RunModeType = RunMode.NORMAL,
    response_limited_bytes_in_nohup: int | None = None,
    ignore_output: bool = False,
    output_file: str | None = None,
    capture_exit_code: bool = False,   # ← 新增
) -> Observation:
```

### `capture_exit_code` 参数说明

| 值 | 行为 |
|----|------|
| `False`（默认） | 原有逻辑不变：`success=True` → `exit_code=0`，`success=False` → `exit_code=1` |
| `True` | 启用子 Shell 包裹，捕获 cmd 真实退出码写入 `.rc` 文件，进程结束后读取 |

- 仅在 `mode="nohup"` 时生效；`mode="normal"` 下忽略该参数（normal 模式天然返回真实 exit_code）
- 默认 `False` 保证向后兼容，不改变任何现有调用的行为

### 返回值 `Observation.exit_code` 语义

| 场景 | `capture_exit_code=False`（原有） | `capture_exit_code=True`（新） |
|------|-----------------------------------|-------------------------------|
| cmd 成功执行（exit 0） | `0` | `0` |
| cmd 失败（exit N，N≠0） | `0` | `N` ← **修复** |
| cmd 不存在（command not found） | `0` | `127` ← **修复** |
| 进程完成，`.rc` 读取失败 | `0` | `0`（静默回退） |
| 进程超时（wait_timeout 到期） | `1` | `1`（不变，超时不读 `.rc`） |
| nohup 提交失败 | `1` | `1`（不变） |

### `failure_reason` 语义（不变）

`failure_reason` 只在以下情况非空：
- 进程超时：包含超时消息
- nohup 提交失败：包含错误信息

cmd 本身失败（exit_code ≠ 0）时，`failure_reason` 保持为空字符串，错误信息通过 `output` 字段（即 nohup 输出文件内容）传递。

---

## 2. 临时文件约定

`capture_exit_code=True` 时，nohup 模式每次调用会在沙箱内 `/tmp/` 目录额外生成 `.rc` 文件：

| 文件 | 路径模式 | 用途 | 条件 |
|------|----------|------|------|
| 输出文件 | `/tmp/tmp_{timestamp_ns}.out` | cmd 的 stdout + stderr | 始终生成 |
| 退出码文件 | `/tmp/tmp_{timestamp_ns}.rc` | cmd 的退出码（纯数字，一行） | 仅 `capture_exit_code=True` |

> 临时文件不会被自动清理，与现有 `.out` 文件行为一致。

---

## 3. `_arun_with_nohup()` — 签名变更

内部方法，新增 `capture_exit_code` 透传参数：

```python
# 改前
async def _arun_with_nohup(
    self, cmd, session, wait_timeout, wait_interval,
    response_limited_bytes_in_nohup, ignore_output, output_file,
) -> Observation:

# 改后
async def _arun_with_nohup(
    self, cmd, session, wait_timeout, wait_interval,
    response_limited_bytes_in_nohup, ignore_output, output_file,
    capture_exit_code: bool = False,   # ← 新增
) -> Observation:
```

`capture_exit_code=True` 时：生成 `exit_code_file` 路径并传给 `start_nohup_process` 和 `handle_nohup_output`。
`capture_exit_code=False` 时：`exit_code_file` 不生成，两个方法收到 `exit_code_file=None`，走原有逻辑。

---

## 4. `start_nohup_process()` — 签名变更

此方法为内部方法，新增可选参数 `exit_code_file`：

```python
# 改前
async def start_nohup_process(
    self, cmd: str, tmp_file: str, session: str
) -> tuple[int | None, Observation | None]:

# 改后
async def start_nohup_process(
    self, cmd: str, tmp_file: str, session: str,
    exit_code_file: str | None = None,   # ← 新增
) -> tuple[int | None, Observation | None]:
```

- `exit_code_file=None`：生成原始 nohup 命令（行为不变）
- `exit_code_file` 有值：生成子 Shell 包裹命令，将退出码写入该文件

---

## 5. `handle_nohup_output()` — 签名变更

新增可选参数 `exit_code_file`：

```python
# 改前
async def handle_nohup_output(
    self, tmp_file, session, success, message, ignore_output, response_limited_bytes_in_nohup
) -> Observation:

# 改后
async def handle_nohup_output(
    self, tmp_file, session, success, message, ignore_output, response_limited_bytes_in_nohup,
    exit_code_file: str | None = None,   # ← 新增
) -> Observation:
```

`exit_code_file=None` 时行为退化为改前逻辑，保证向后兼容（如有外部调用）。
