# nohup 模式 exit_code 修复 — Requirement Spec

## Background

`Sandbox.arun()` 支持 `mode="nohup"` 参数，用于在沙箱内以后台方式运行长时命令。nohup 模式的执行流程如下：

1. 通过 `nohup {cmd} < /dev/null > {tmp_file} 2>&1 & echo PIDSTART${!}PIDEND;disown` 将命令提交到后台
2. 轮询 `kill -0 {pid}` 检测进程是否存活
3. 进程结束后，读取输出文件内容作为 `output`
4. 返回 `Observation(output=..., exit_code=...)`

### 当前问题

`exit_code` 的值不反映 cmd 的真实退出码：

- `success=True`（进程在 `wait_timeout` 内完成）→ 固定返回 `exit_code=0`
- `success=False`（超时）→ 固定返回 `exit_code=1`

这意味着即使 cmd 本身执行失败（如命令不存在、脚本报错），只要进程在超时前结束，`exit_code` 就是 `0`，调用方无法通过 `exit_code` 判断命令是否成功执行。

**示例**：

```python
result = await sandbox.arun(cmd="nonexistent_command_xyz", session="s", mode="nohup")
# 当前行为：result.exit_code == 0  ← 错误，命令不存在应返回 127
# 期望行为：result.exit_code == 127
```

---

## In / Out

### In（本次要做的）

1. **捕获 cmd 的真实退出码**
   - 通过子 Shell 包裹，将 cmd 的退出码写入独立的 `.rc` 文件
   - 进程结束后读取 `.rc` 文件，将其作为 `Observation.exit_code` 返回
   - 适用于 `ignore_output=True` 和 `ignore_output=False` 两种路径

2. **保持超时语义不变**
   - `success=False`（超时）时，`exit_code` 仍返回 `1`，`failure_reason` 包含超时信息

3. **向后兼容**
   - `.rc` 文件读取失败时（如命令在 bash -c 之前就失败），`exit_code` 回退为原有逻辑（`0` 或 `1`）

### Out（本次不做的）

- 修改 `wait_for_process_completion` 的轮询策略或超时机制
- 修改 `ignore_output=True` 时的 detached 消息格式
- 对 `mode="normal"` 的任何修改（normal 模式天然返回真实 exit_code）
- SDK 客户端侧的行为变更文档更新

---

## Acceptance Criteria

- **AC1**：`arun(cmd="nonexistent_command", mode="nohup")` 返回 `exit_code=127`，`output` 包含 bash 的 "command not found" 信息
- **AC2**：`arun(cmd="exit 42", mode="nohup")` 返回 `exit_code=42`
- **AC3**：`arun(cmd="echo hello", mode="nohup")` 返回 `exit_code=0`，行为不变
- **AC4**：`arun(cmd="...", mode="nohup", ignore_output=True)` 同样返回真实 `exit_code`
- **AC5**：进程超时（`wait_timeout` 到期）时，`exit_code=1`，`failure_reason` 包含超时信息，行为不变
- **AC6**：`.rc` 文件不存在或内容非数字时，`exit_code` 回退为 `0`（success）或 `1`（timeout），不抛出异常

---

## Constraints

- 不引入新的外部依赖（不 `import shlex`）
- 不改变 nohup 命令中 cmd 的执行方式（不对 cmd 做额外 shell 转义）
- 不修改 `wait_for_process_completion` 的返回签名（保持 `tuple[bool, str]`）
- `.rc` 文件与 `.out` 文件使用相同的时间戳前缀，放在 `/tmp/` 下

---

## Risks

- **风险**：子 Shell `( ... )` 增加了一层进程，`${!}` 捕获的是子 Shell 的 PID 而非 nohup 进程的 PID，但监控语义不变（子 Shell 在 cmd 结束后才退出）
- **风险**：`.rc` 文件读取失败（磁盘满、权限问题等）时静默回退，不暴露给调用方
- **回滚**：仅修改 `rock/sdk/sandbox/client.py`，还原该文件即可
