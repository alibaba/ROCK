# Error Codes

Error code definitions and categories for error handling and retry strategies.

## Usage Example

```python
import rock

def test_codes_values():
    """Test basic status code values"""
    assert rock.codes.OK == 2000
    assert rock.codes.BAD_REQUEST == 4000
    assert rock.codes.INTERNAL_SERVER_ERROR == 5000
    assert rock.codes.COMMAND_ERROR == 6000
```

## Codes Categories

```python
OK = 2000, "OK"
"""
Success codes (2xxx)
"""

BAD_REQUEST = 4000, "Bad Request"
"""
Client error codes (4xxx):

These errors indicate issues with the client request,
SDK will raise Exceptions for these errors.
"""

INTERNAL_SERVER_ERROR = 5000, "Internal Server Error"
"""
Server error codes (5xxx):

These errors indicate issues on the server side,
SDK will raise Exceptions for these errors.
"""

COMMAND_ERROR = 6000, "Command Error"
"""
Command/execution error codes (6xxx):

These errors are related to command execution and should be handled by the model,
SDK will NOT raise Exceptions for these errors.
"""
```

## Retry Strategy Recommendations

- **Retry trigger**: Only retry when `INTERNAL_SERVER_ERROR` occurs
- **Other error handling**:
  - `BAD_REQUEST`: Check if there are issues with the arun call logic
  - `COMMAND_ERROR`: stdout goes to `observation.output`, stderr goes to `observation.failure_reason`
- `COMMAND_ERROR` note: When bash execution fails, both stdout and stderr may be non-empty. It is recommended to prompt the model with both output and failure_reason from the observation.

## Retry Example

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
