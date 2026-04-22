# Sandbox Timezone — Implementation Plan

## 背景

Docker sandbox 此前未设置容器内系统时区，导致容器内系统时区为 UTC。具体表现为：文件修改时间按 UTC 记录，前端展示时与用户本地时间存在偏差；`date`、`ls -l` 等系统命令输出 UTC 时间，与用户预期不符。

本次实现通过将宿主机的 zoneinfo 文件挂载到容器 `/etc/localtime`，让容器内系统时区与宿主机配置一致。

---

## File Changes

| 文件 | 修改类型 | 说明 |
|------|------|------|
| `rock/deployments/docker.py` | 修改 | 在 `_start` 方法中根据 `ROCK_TIME_ZONE` 挂载 zoneinfo 文件到 `/etc/localtime:ro` |
| `tests/unit/rocklet/test_docker_deployment.py` | 修改 | 验证挂载参数生成正确；集成测试验证容器内时区生效 |

---

## Core Logic

### 变更：Docker sandbox 挂载 `/etc/localtime`

文件：`rock/deployments/docker.py`，`_start` 方法

在构建 `docker run` 命令时，根据 `ROCK_TIME_ZONE` 确定 zoneinfo 文件路径，若文件存在则追加 volume 挂载参数：

```python
# 在 env_arg 和 volume_args 构建区域之后
tz = env_vars.ROCK_TIME_ZONE  # 默认 Asia/Shanghai
localtime_src = f"/usr/share/zoneinfo/{tz}"
if os.path.isfile(localtime_src):
    volume_args.extend(["-v", f"{localtime_src}:/etc/localtime:ro"])
else:
    logger.warning(f"Zoneinfo file not found: {localtime_src}, skipping /etc/localtime mount")
```

### 设计要点

1. **用 `ROCK_TIME_ZONE` 而非 `TZ`**：`ROCK_TIME_ZONE` 始终是 IANA 格式（如 `Asia/Shanghai`），可直接映射到 `/usr/share/zoneinfo/` 下的文件路径。`TZ` 可能是 POSIX 格式（如 `CST-8`），无法映射到文件。

2. **文件存在性校验**：启动前用 `os.path.isfile()` 检查 zoneinfo 文件是否存在。不存在时打 warning 并跳过，不阻断容器启动。

3. **只读挂载**：使用 `:ro` 防止容器内进程修改宿主机时区文件。

4. **不传 `TZ` 环境变量**：挂载 `/etc/localtime` 已足够让 glibc/musl 正确解析时区，无需额外设置 `TZ`。避免 `TZ` 与 `/etc/localtime` 不一致导致的混乱。

---

## Validation Plan

### 用例 1：单元测试 — 挂载参数生成

- mock `os.path.isfile` 返回 `True`
- 验证 `_start` 生成的 `docker run` 命令中包含 `-v /usr/share/zoneinfo/Asia/Shanghai:/etc/localtime:ro`

### 用例 2：单元测试 — zoneinfo 文件不存在时跳过

- mock `os.path.isfile` 返回 `False`
- 验证 `docker run` 命令中不包含 `/etc/localtime` 相关挂载
- 验证打印了 warning 日志

### 用例 3：集成测试 — 真实 Docker 容器验证

- 前提：宿主机有 `/usr/share/zoneinfo/Asia/Shanghai`
- 启动 Docker 容器，挂载 `/etc/localtime`
- 在容器内执行 `date +%Z` 或 `ls -l /etc/localtime`
- 验证时区显示正确

---

## Rollback

- 回滚仅需恢复 `rock/deployments/docker.py` 中的挂载逻辑
- 对现有对外接口无兼容性影响
