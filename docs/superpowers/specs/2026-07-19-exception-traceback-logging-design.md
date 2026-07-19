# 可配置的异常堆栈日志设计

## 状态

- 关联 Issue：[alibaba/ROCK#1260](https://github.com/alibaba/ROCK/issues/1260)
- 基线：`origin/master`
- 日期：2026-07-19

## 背景

`rock.common.exception.handle_exceptions` 捕获普通异常时已经使用 `exc_info=True`：

```python
logger.error(f"Error in {func.__name__}: {str(e)}", exc_info=True)
```

但 `rock.logger.StandardFormatter.format()` 完全手工拼接日志，只读取 `record.getMessage()`，没有处理 `record.exc_info`。因此异常对象仍在 `LogRecord` 中，最终输出却没有异常类型和 traceback。对于 `str(e)` 为空的异常，日志只剩下类似：

```text
Error in http_proxy:
```

这会丢失定位问题所需的异常类型、调用路径和异常链。

## 目标

1. 自定义 formatter 在存在有效 `exc_info` 时输出完整 Python traceback。
2. 在首行附加异常的全限定类型；即使日志平台按行切分，仍能看到异常种类。
3. 默认启用新能力，同时提供环境变量和 YAML 开关，可恢复当前的单行输出模式。
4. 保持现有日志头、时区、颜色、文件/stdout 选择和普通日志格式不变。
5. 明确 admin、proxy 与 rocklet 的不同配置加载方式。

## 非目标

- 不判断这次线上错误的具体异常类型或根因。
- 不改变异常捕获、HTTP 状态码、`RockResponse` 或 API 返回内容。
- 不在 traceback 中增加局部变量、请求体或其他可能敏感的数据。
- 不为 rocklet 引入 `RockConfig`/YAML 加载链路。
- 不支持通过 Nacos 动态更新该开关。

## 方案选择

采用“公共 formatter + 进程级运行时配置”的方案。

不直接修改每个 `logger.error(...)` 调用点，因为这会遗漏其他使用 `exc_info=True` 的日志，也会让输出行为散落到业务代码中。也不只依赖环境变量，因为 admin/proxy 已有统一 YAML 配置体系，运维需要通过现有配置文件干预。

formatter 在每次格式化时查询当前有效开关，而不是在 logger/handler 创建时固化值。这一点很重要：模块级 logger 通常早于 FastAPI lifespan 创建，而 YAML 是在 lifespan 中才加载的。

## 配置模型与优先级

新增 YAML 配置：

```yaml
logging:
  exception_traceback_enabled: true
```

新增环境变量：

```text
ROCK_LOGGING_EXCEPTION_TRACEBACK_ENABLE=true|false
```

有效值按下列优先级解析：

| 优先级 | 来源 | 适用范围 |
|---|---|---|
| 1 | 显式设置的 `ROCK_LOGGING_EXCEPTION_TRACEBACK_ENABLE` | 所有进程，包括 admin、proxy、rocklet 和 CLI |
| 2 | YAML `logging.exception_traceback_enabled` | 加载 `RockConfig` 的 admin 与 proxy |
| 3 | 默认值 `true` | 未提供上述配置的所有进程 |

环境变量是否“显式设置”通过 `env_vars.is_set()` 判断，避免环境变量的默认值无条件覆盖 YAML。环境变量和 YAML 任一处设置为 `false`（且未被更高优先级覆盖）时，恢复当前行为：只输出原始日志消息，不附加异常类型和 traceback。

`RockConfig` 新增 `LoggingConfig`，其中 `exception_traceback_enabled` 默认为 `true`。旧 YAML 不包含 `logging` 时保持向后兼容，不会解析失败。

## 日志输出契约

### 开关启用

当 `record.exc_info` 有效时，首行在原消息后附加全限定异常类型，随后换行输出 `logging.Formatter.formatException()` 生成的标准 traceback：

```text
2026-07-18T23:42:40.329+08:00 ERROR:exception.py:61 [rock.common.exception] [] [0b5128ae17843892402452213e0cb1] -- Error in http_proxy: [exception_type=httpx.PoolTimeout]
Traceback (most recent call last):
  ...
httpx.PoolTimeout
```

若异常消息非空，原消息完整保留，异常类型追加在其后。若原消息以空白结尾，只在启用模式下清理末尾空白，避免类型标签前出现多余空格。

异常类型取实际异常类的 `module + qualname`，不使用 `repr(exception)`，以免额外输出异常对象中可能包含的敏感信息。标准 `formatException()` 会保留 Python 异常链，但不会主动输出局部变量。

### 开关关闭

输出与当前 `origin/master` 完全一致：

```text
2026-07-18T23:42:40.329+08:00 ERROR:exception.py:61 [rock.common.exception] [] [0b5128ae17843892402452213e0cb1] -- Error in http_proxy:
```

不附加异常类型，也不输出 traceback。没有有效 `exc_info` 的普通日志无论开关为何都保持不变。

## 组件改动

### `rock/logger.py`

- 增加进程级 YAML 配置值及设置函数，参数只接收布尔值，避免 logger 反向依赖 `RockConfig`。
- 增加有效开关解析函数：显式环境变量优先，其次进程级 YAML 值，最后默认 `true`。
- `StandardFormatter.format()` 在完成现有日志头和消息拼接后，按需追加异常类型和 `formatException(record.exc_info)` 的结果。
- 仅当 `record.exc_info` 存在且异常类型不为 `None` 时视为有效，避免在异常上下文之外传入 `exc_info=True` 产生无意义输出。
- stdout 的彩色 formatter 与文件的非彩色 formatter 共用同一逻辑，每个 handler 各输出一次，不修改或复用 `record.exc_text`，避免多 handler 重复堆栈。

### `rock/env_vars.py`

- 声明并解析 `ROCK_LOGGING_EXCEPTION_TRACEBACK_ENABLE`。
- 继续使用现有布尔环境变量风格，对 `true`/`false` 做大小写无关解析。
- 使用现有 `is_set()` 区分“未设置”和“显式设置”。

### `rock/config.py`

- 新增 `LoggingConfig` dataclass。
- 在 `RockConfig` 中增加 `logging` 字段。
- 在 `RockConfig.from_env()` 中解析 YAML 的 `logging` 节。

### `rock/admin/main.py`

- `RockConfig.from_env()` 返回后，立即把 `rock_config.logging.exception_traceback_enabled` 注入公共 logger。
- admin 角色与 proxy 角色由同一个 `create_app()`/`lifespan()` 启动链路创建，所以两者使用同一处改动。
- 在 YAML 加载完成之前的极早期启动日志使用环境变量或默认值；加载完成后的业务日志使用完整优先级。

### `rock/rocklet/server.py`

不修改。rocklet 当前不调用 `RockConfig.from_env()`，因此不读取 admin 的 YAML 配置。它通过公共 formatter 使用环境变量；环境变量未设置时使用默认值 `true`。这样不会为轻量运行时引入额外配置依赖。

### 配置示例

在仓库的 admin 配置示例中展示 `logging.exception_traceback_enabled`，使 YAML 能力可发现。字段缺失仍使用默认值，不要求现有部署立刻修改 YAML。

## 初始化与并发

模块导入阶段创建的 formatter 不缓存开关，格式化每条异常日志时读取有效值。因此 lifespan 中的配置注入会作用于已经创建的 logger/handler。

YAML 值只在服务启动阶段设置一次。Python 模块级布尔引用的读写对该场景足够，不引入锁；多 worker 进程分别执行各自的 lifespan 并持有各自的配置值。

## 兼容性与回滚

- 默认值从“formatter 丢弃 traceback”改变为“输出 traceback”，这是有意的可观测性增强。
- 需要立即回滚日志形态时，将环境变量设为 `false`；admin/proxy 也可在 YAML 中设为 `false`。
- 环境变量优先级最高，可在不修改/重新生成 YAML 的情况下统一覆盖。
- 现有日志头字段顺序不变，普通日志不变；只影响带有效 `exc_info` 的日志。

## 测试策略

### Formatter 单元测试

1. 默认启用时，普通异常同时输出首行全限定类型与完整 traceback。
2. 使用 `httpx.PoolTimeout("")` 验证空异常消息仍在首行输出 `httpx.PoolTimeout`。
3. 开关关闭时，输出与当前格式完全一致，既无类型标签也无 traceback。
4. 普通 INFO/WARNING/ERROR（无 `exc_info`）在开关启用和关闭时均保持原格式。
5. stdout 彩色格式与文件非彩色格式均输出一次 traceback，不重复。
6. 异常链通过标准 formatter 正确输出。

### 配置单元测试

1. YAML `true`/`false` 均能解析到 `RockConfig.logging`。
2. 环境变量未设置时使用 YAML 值。
3. 环境变量显式 `true` 覆盖 YAML `false`。
4. 环境变量显式 `false` 覆盖 YAML `true`。
5. YAML 与环境变量均未设置时默认为 `true`。

### 回归验证

至少执行：

```bash
uv run pytest tests/unit/test_logger.py tests/unit/test_config.py -v
uv run ruff check rock/logger.py rock/env_vars.py rock/config.py rock/admin/main.py tests/unit/test_logger.py tests/unit/test_config.py
uv run ruff format --check rock/logger.py rock/env_vars.py rock/config.py rock/admin/main.py tests/unit/test_logger.py tests/unit/test_config.py
```

现有时间戳和 billing 日志格式测试必须继续通过。

## 实施顺序

1. 先补 formatter 与配置优先级的失败测试。
2. 实现环境变量、YAML dataclass 与解析。
3. 实现 formatter 的异常类型和 traceback 输出。
4. 在 admin/proxy lifespan 注入 YAML 配置。
5. 更新配置示例并执行回归测试。
