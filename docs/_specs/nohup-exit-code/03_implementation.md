# nohup 模式 exit_code 修复 — Implementation Plan

## 核心设计

### 为什么用子 Shell `( ... )` 而非 `bash -c '...'`

`bash -c 'cmd; echo $? > rc'` 方案需要对 cmd 做 shell 转义（shlex.quote），否则 cmd 内含单引号、heredoc 等复杂结构时会产生引号冲突。

子 Shell 方案完全不改变 cmd 的嵌入方式：

```bash
# 改前
nohup {cmd} < /dev/null > {tmp_file} 2>&1 & echo PIDSTART${!}PIDEND;disown

# 改后
( nohup {cmd} < /dev/null > {tmp_file} 2>&1; echo $? > {rc_file} ) & echo PIDSTART${!}PIDEND;disown
```

**关键点**：

- cmd 原样嵌入，不做任何额外转义，与改前完全一致
- `echo $? > {rc_file}` 在子 Shell 内，紧跟 nohup 退出后执行
- nohup 的退出码 = cmd 的退出码（POSIX 标准：nohup 以所运行命令的退出码退出）
- `${!}` 捕获子 Shell 的 PID，`kill -0` 监控子 Shell 的生命周期
- 子 Shell 在 cmd + echo 都完成后才退出，`.rc` 文件写入与进程退出是原子的

### 小括号 `( ... )` 的作用

`( ... )` 在 bash 中创建一个**子 Shell**（subshell），其作用有三：

**① 将多条命令组合成一个后台作业**

```bash
# 没有括号：& 只作用于 nohup，echo $? 在前台执行，无法捕获后台进程的退出码
nohup {cmd} ... 2>&1; echo $? > rc &   # ← 错误：echo 在前台，$? 是 & 本身的退出码（永远为 0）

# 有括号：& 作用于整个子 Shell，nohup 和 echo $? 顺序执行于同一后台作业中
( nohup {cmd} ... 2>&1; echo $? > rc ) &   # ← 正确
```

**② 保证 `echo $?` 在后台、紧跟 nohup 执行**

子 Shell 内部的命令按顺序串行执行：先 `nohup {cmd}`，等 nohup 退出后立即执行 `echo $? > rc_file`。
外部的 `echo PIDSTART${!}PIDEND;disown` 则在父 Shell 中立即返回，不等待子 Shell 完成。

**③ 隔离子 Shell 的退出码，不干扰 `${!}` 的捕获**

```bash
( nohup {cmd} ... 2>&1; echo $? > rc ) & echo PIDSTART${!}PIDEND; disown
#                                      ^
#                                      此处 $? 是 & 的退出码（总为 0），${!} 是子 Shell 的 PID
```

`${!}` 是父 Shell 最近启动的后台作业的 PID，即子 Shell 的 PID。`kill -0 ${!}` 监控的是子 Shell 是否存活，子 Shell 在 cmd 执行完 + rc 文件写入完后才退出，因此 `kill -0` 失效的时刻等价于"cmd 已执行完且 rc 已写入"。

### 退出码读取时机

`wait_for_process_completion` 检测到进程结束（`kill -0` 抛异常）后，`handle_nohup_output` 被调用。此时：

1. 子 Shell 已退出，`.rc` 文件已写入完成
2. 读取 `.rc` 文件内容，解析为整数作为 `actual_exit_code`
3. `success=True` 时使用 `actual_exit_code`；`success=False`（超时）时忽略 `.rc`，固定返回 `1`

---

## File Changes

| 文件 | 修改类型 | 说明 |
|------|----------|------|
| `rock/sdk/sandbox/client.py` | 修改 | 核心逻辑，涉及 3 个方法 |
| `tests/unit/sdk/test_arun_nohup.py` | 修改 | 更新 2 个测试的 `len(executed_commands)` 断言 |
| `tests/integration/sdk/sandbox/test_sdk_client.py` | 修改 | 更新集成测试的 `exit_code` 断言 |

---

## 实现细节

### 变更 1：`_arun_with_nohup` — 生成 `.rc` 文件路径并透传

```python
# 现有
tmp_file = output_file if output_file else f"/tmp/tmp_{timestamp}.out"
pid, error_response = await self.start_nohup_process(cmd=cmd, tmp_file=tmp_file, session=session)
...
return await self.handle_nohup_output(
    tmp_file=tmp_file, session=session, success=success, message=message,
    ignore_output=ignore_output, response_limited_bytes_in_nohup=response_limited_bytes_in_nohup,
)

# 修改后：新增 exit_code_file
tmp_file = output_file if output_file else f"/tmp/tmp_{timestamp}.out"
exit_code_file = f"/tmp/tmp_{timestamp}.rc"
pid, error_response = await self.start_nohup_process(
    cmd=cmd, tmp_file=tmp_file, session=session, exit_code_file=exit_code_file
)
...
return await self.handle_nohup_output(
    tmp_file=tmp_file, session=session, success=success, message=message,
    ignore_output=ignore_output, response_limited_bytes_in_nohup=response_limited_bytes_in_nohup,
    exit_code_file=exit_code_file,
)
```

### 变更 2：`start_nohup_process` — 子 Shell 包裹

```python
# 现有
nohup_command = f"nohup {cmd} < /dev/null > {tmp_file} 2>&1 & echo {PID_PREFIX}${{!}}{PID_SUFFIX};disown"

# 修改后：子 Shell 包裹，捕获退出码
nohup_command = (
    f"( nohup {cmd} < /dev/null > {tmp_file} 2>&1; echo $? > {exit_code_file} )"
    f" & echo {PID_PREFIX}${{!}}{PID_SUFFIX};disown"
)
```

方法签名新增 `exit_code_file: str` 参数。

### 变更 3：`handle_nohup_output` — 读取真实退出码

在现有逻辑前插入退出码读取：

```python
async def handle_nohup_output(
    self, tmp_file, session, success, message, ignore_output,
    response_limited_bytes_in_nohup, exit_code_file=None,
) -> Observation:
    # 仅在 success=True 时读取 .rc 文件（超时场景下子 Shell 可能未写完）
    actual_exit_code = None
    if success and exit_code_file:
        try:
            rc_result = await self._run_in_session(
                BashAction(session=session, command=f"cat {exit_code_file} 2>/dev/null", check="ignore")
            )
            raw = rc_result.output.strip()
            if raw.isdigit():
                actual_exit_code = int(raw)
        except Exception:
            pass  # 静默回退，不影响主流程

    # 计算最终 exit_code
    exit_code = actual_exit_code if actual_exit_code is not None else (0 if success else 1)
    failure_reason = "" if success else message

    # ignore_output 路径（返回摘要信息）
    if ignore_output:
        ...  # 获取 file_size 逻辑不变
        detached_msg = self._build_nohup_detached_message(tmp_file, success, message, file_size)
        return Observation(output=detached_msg, exit_code=exit_code, failure_reason=failure_reason)

    # 默认路径（读取并返回输出文件内容）
    check_res_command = f"cat {tmp_file}"
    if response_limited_bytes_in_nohup:
        check_res_command = f"head -c {response_limited_bytes_in_nohup} {tmp_file}"
    exec_result = await self._run_in_session(BashAction(session=session, command=check_res_command))
    return Observation(output=exec_result.output, exit_code=exit_code, failure_reason=failure_reason)
```

---

## 单元测试更新

单元测试中的 mock `fake_run_in_session` 对"cat"命令会抛出 `AssertionError`（或直接 return），该异常被 `handle_nohup_output` 的 `except Exception: pass` 捕获，`actual_exit_code=None`，退出码回退为原逻辑，**断言不受影响**。

唯一受影响的是 `len(executed_commands)` 断言：新增了一次 `cat {exit_code_file}` 调用。

| 测试 | 受影响原因 | 修改 |
|------|-----------|------|
| `test_arun_nohup_ignore_output_true_returns_hint` | `success=True` → 读取 `.rc` 文件 | `len == 2` → `len == 3`，新增 `cat` 命令断言 |
| `test_arun_nohup_ignore_output_stat_fails` | `success=True` → 读取 `.rc` 文件 | `len == 2` → `len == 3` |
| `test_arun_nohup_ignore_output_true_propagates_failure` | `success=False` → 不读取 `.rc` | 不变 |
| 其余测试 | `.rc` 读取异常被静默捕获 | 不变 |

单元测试中 mock 的命令前缀检查 `startswith("nohup ")` 将不再匹配（子 Shell 命令以 `(` 开头），需改为 `"nohup " in action.command`。

---

## 集成测试更新

`test_arun_nohup_nonexistent_command_exit_code` 断言从 `exit_code == 0` 改为 `exit_code == 127`：

```python
# 改前（错误语义）
assert result.exit_code == 0

# 改后（正确语义）
assert result.exit_code == 127
assert result.output  # bash 输出 "command not found" 错误信息
```

---

## Execution Plan

### Step 1：修改 `rock/sdk/sandbox/client.py`

按变更 1、2、3 顺序修改。

### Step 2：更新单元测试

修改 `tests/unit/sdk/test_arun_nohup.py` 中两个测试的 `len(executed_commands)` 断言，以及 mock 中命令匹配逻辑。

### Step 3：更新集成测试

修改 `tests/integration/sdk/sandbox/test_sdk_client.py::test_arun_nohup_nonexistent_command_exit_code` 的 `exit_code` 断言。

### Step 4：验证

```bash
# 单元测试
uv run pytest tests/unit/sdk/test_arun_nohup.py -v

# 集成测试（需要 admin + docker）
uv run pytest tests/integration/sdk/sandbox/test_sdk_client.py -v -m need_admin
```

---

## Bad Cases & Limitations

### Bad Case 1：cmd 含裸 `)` 导致子 Shell 提前结束（新引入）

**场景**：cmd 中包含未配对的裸 `)`，如：

```bash
cmd = "echo hello ) world"
```

子 Shell 命令展开后：

```bash
( nohup echo hello ) world < /dev/null > /tmp/tmp_xxx.out 2>&1; echo $? > /tmp/tmp_xxx.rc ) & ...
#                  ^
#                  bash 将这个 ) 解析为子 Shell 的结束符
```

**后果**：
- bash 将在 `echo hello` 后提前关闭子 Shell
- `world ...` 作为后续命令在父 Shell 中执行
- `.rc` 文件捕获的是提前结束的子 Shell 的退出码，而非完整 cmd 的退出码
- 输出内容也不完整

**对比原有行为**：原命令 `nohup {cmd} ...` 遇到裸 `)` 同样是 bash 语法解析问题，行为相同（语法错误、部分执行），**不是新引入的退化**。

**结论**：此问题本质上是调用方传入语法不合法的 cmd，在原方案中同样存在，子 Shell 方案不引入新风险。

---

### Bad Case 2：`$?` 捕获的是 nohup 的退出码，而非 cmd 的直接退出码

**场景**：子 Shell 内执行的是 `nohup {cmd}`，`echo $?` 捕获的是 `nohup` 进程的退出码。

**分析**：

POSIX 标准规定：nohup 以所运行命令的退出码退出（即 `nohup` 进程 exit status = cmd exit status）。因此，`echo $?` 得到的值在语义上等价于 cmd 的退出码。

**例外情况**：
| 场景 | nohup 退出码 |
|------|-------------|
| cmd 正常执行，exit 0 | 0（等于 cmd） |
| cmd 失败，exit N | N（等于 cmd） |
| cmd 不存在（bash: command not found） | 127（bash 给出，nohup 透传）|
| nohup 自身无法执行 cmd（权限拒绝等） | 126（POSIX 规定） |
| nohup 命令本身无法启动（如 nohup 不存在） | shell 报错，子 Shell 语法失败，`.rc` 不写入 |

**结论**：在正常情况下（nohup 可执行、cmd 路径存在），`$?` 即为 cmd 退出码。仅当 nohup 本身无法执行时才出现语义差异，此时 `.rc` 文件不写入，回退为 `exit_code=0`（AC6 覆盖）。

---

### Bad Case 3：子 Shell zombie 导致额外等待一个 `wait_interval`

**场景**：子 Shell 进程退出后，父 Shell（bash）尚未调用 `wait()` 回收它，子 Shell 处于 **zombie（僵尸）** 状态。

**`kill -0` 对 zombie 的行为**：`kill -0 <zombie_pid>` 返回 **0**（进程条目仍在进程表中），不抛出异常。

**后果**：
- `wait_for_process_completion` 在该轮次检测到"进程存活"
- 实际子 Shell 已经执行完（cmd + rc 写入都已完成）
- 多等一个 `wait_interval`（默认 10 秒）后，下一轮 `kill -0` 才抛出异常

**量化影响**：额外延迟最多 `wait_interval` 秒（默认 10s），不影响正确性，仅轻微影响性能。

**对比原有行为**：原方案的 nohup 进程同样会经历 zombie 状态（bash 是父进程），**行为相同，不是新引入的退化**。

> 注：`disown` 使 bash 不再追踪该作业，但不影响 zombie 回收机制。zombie 的回收由 bash 的信号处理（SIGCHLD）触发，而不受 `disown` 影响。

---

### Bad Case 4：`.rc` 文件写入失败时静默回退为 `exit_code=0`

**场景**：沙箱内 `/tmp` 磁盘满、权限问题，或 `.rc` 文件路径不可写，导致 `echo $? > {rc_file}` 失败。

**后果**：
- `.rc` 文件不存在或内容为空
- `handle_nohup_output` 读取 `.rc` 后 `raw.isdigit()` 为 `False`
- `actual_exit_code = None`
- 回退为原逻辑：`exit_code = 0 if success else 1`
- **即使 cmd 实际失败（exit N），也错误地返回 `exit_code=0`**

**影响**：调用方收到语义错误的 exit_code（AC6 接受此回退行为）。

**可观察性**：此失败静默，无日志、无异常。调用方无法区分"cmd 成功退出 0"和"rc 文件写入失败导致回退为 0"。

**缓解**：
- `/tmp` 磁盘满通常是环境问题，超出本次修复范围
- 如需可观察性，可在未来迭代中增加 warning 日志（`actual_exit_code=None` 时）

---

### Bad Case 5：output_file 参数指定自定义路径时，`.rc` 文件仍在 `/tmp`

**场景**：调用方指定 `output_file="/custom/path/output.log"`，则 `.out` 文件在自定义路径，但 `.rc` 文件始终生成在 `/tmp/tmp_{timestamp}.rc`。

**分析**：两个文件用途不同，不要求放在同一目录。`output_file` 是调用方关心的输出内容；`.rc` 文件是实现细节，放在 `/tmp` 统一管理。

**结论**：行为合理，无风险。临时约定已在 `02_interface.md` 中明确。
