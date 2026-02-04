# Error Codes

错误码定义和分类，用于错误处理和重试策略。

## 使用示例

```python
import rock

def test_codes_values():
    """测试基本状态码值"""
    assert rock.codes.OK == 2000
    assert rock.codes.BAD_REQUEST == 4000
    assert rock.codes.INTERNAL_SERVER_ERROR == 5000
    assert rock.codes.COMMAND_ERROR == 6000
```

## Codes 分类

```python
OK = 2000, "OK"
"""
成功状态码 (2xxx)
"""

BAD_REQUEST = 4000, "Bad Request"
"""
客户端错误码 (4xxx):

这些错误表示客户端请求有问题，
SDK 会抛出异常。
"""

INTERNAL_SERVER_ERROR = 5000, "Internal Server Error"
"""
服务端错误码 (5xxx):

这些错误表示服务端出现问题，
SDK 会抛出异常。
"""

COMMAND_ERROR = 6000, "Command Error"
"""
命令/执行错误码 (6xxx):

这些错误与命令执行相关，由模型处理，
SDK 不会抛出异常。
"""
```

## 重试策略建议

- **重试触发条件**: 只有当 `INTERNAL_SERVER_ERROR` 时才需要重试
- **其他情况的处理策略**:
  - `BAD_REQUEST`: 需要检查 arun 调用逻辑是否有异常
  - `COMMAND_ERROR`: stdout 输出到 `observation.output`，stderr 输出到 `observation.failure_reason`
- `COMMAND_ERROR` 说明: 由于 bash 执行失败时，stdout/stderr 可能全部非空，建议将 observation 中 output 和 failure_reason 全部 prompt 给模型进行推理

## 重试示例

```python
# Background execution with nohup
while retry_times < retry_limit:
    try:
        observation: Observation = await sandbox.arun(
            "python long_running_script.py",
            mode="nohup"
        )
        if observation.exit_code != 0:
            logging.warning(
                f"Command failed with exit code {observation.exit_code}, "
                f"output: {observation.output}, failure_reason: {observation.failure_reason}"
            )
        return observation
    except RockException as e:
        if rock.codes.is_server_error(e.code):
            if retry_times >= retry_limit:
                logging.error(f"All {retry_limit} attempts failed")
                raise e
            else:
                retry_times += 1
                logging.error(
                    f"Server error occurred, code: {e.code}, message: {e.code.get_reason_phrase()}, "
                    f"exception: {str(e)}, will retry, times: {retry_times}."
                )
                await asyncio.sleep(2)
                continue
        else:
            logging.error(
                f"Non-retriable error occurred, code: {e.code}, message: {e.code.get_reason_phrase()}, exception: {str(e)}."
            )
            raise e
```
