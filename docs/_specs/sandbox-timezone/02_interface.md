# Sandbox Timezone — Interface Contract

## 1. Runtime Environment Variables

### 使用的变量

| 变量 | 来源 | 默认值 | 用途 |
|------|------|------|------|
| `ROCK_TIME_ZONE` | ROCK env vars | `Asia/Shanghai` | 1) ROCK 自身日志、调度等应用层逻辑 2) 确定挂载到容器的 zoneinfo 文件路径 |

### 行为规则

- `ROCK_TIME_ZONE` 继续按原逻辑传入容器（`-e ROCK_TIME_ZONE=...`），默认 `Asia/Shanghai`
- 同时根据 `ROCK_TIME_ZONE` 的值定位 `/usr/share/zoneinfo/{ROCK_TIME_ZONE}`，挂载到容器 `/etc/localtime`
- 不再向容器传递 `TZ` 环境变量

---

## 2. Docker Run Contract

### Volume 挂载

Docker sandbox 启动时，volume 参数至少包含：

```bash
-v /usr/share/zoneinfo/<ROCK_TIME_ZONE>:/etc/localtime:ro
```

在默认情况下等价于：

```bash
-v /usr/share/zoneinfo/Asia/Shanghai:/etc/localtime:ro
```

### 环境变量注入

```bash
-e ROCK_TIME_ZONE=Asia/Shanghai
```

### 示例

#### 示例 1：默认配置

```bash
docker run ... \
  -v /usr/share/zoneinfo/Asia/Shanghai:/etc/localtime:ro \
  -e ROCK_TIME_ZONE=Asia/Shanghai \
  ...
```

#### 示例 2：`ROCK_TIME_ZONE=America/New_York`

```bash
docker run ... \
  -v /usr/share/zoneinfo/America/New_York:/etc/localtime:ro \
  -e ROCK_TIME_ZONE=America/New_York \
  ...
```

#### 示例 3：zoneinfo 文件不存在

```bash
# /usr/share/zoneinfo/Invalid/Zone 不存在
# → 打印 warning 日志，跳过挂载
# → 容器时区回退为 UTC（镜像默认行为）
docker run ... \
  -e ROCK_TIME_ZONE=Invalid/Zone \
  ...
```

---

## 3. Container-side Observable Behavior

### 可观测方式

| 检查方式 | 预期 |
|------|------|
| `date` | 按挂载的时区显示当前时间 |
| `ls -l` | 文件修改时间按挂载的时区显示 |
| `cat /etc/localtime` | 返回有效的 TZif 二进制数据 |

### 边界说明

- 挂载的是完整的 IANA zoneinfo 文件，包含历史规则和夏令时信息
- 容器内无需安装 `tzdata` 即可正确使用
- 如果容器内程序同时设置了 `TZ` 环境变量，`TZ` 优先级高于 `/etc/localtime`

---

## 4. Backward Compatibility

- `ROCK_TIME_ZONE` 保持不变
- 不新增对外 API 字段
- 不修改 sandbox 启动请求模型
- 新增 volume 挂载为纯新增行为，不影响已有挂载
