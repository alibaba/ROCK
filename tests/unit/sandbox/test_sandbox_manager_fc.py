"""Unit tests for SandboxManager FC integration paths.

Verifies review findings:
- C7: _build_sandbox_info_metadata crashes when memory is int (FCOperator output)
- W12: timeout_info uses unmerged session_ttl, falls back to 10 minutes
- W13: FC config skips duplicate sandbox detection
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from rock import env_vars
from rock.actions.sandbox.response import State
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.sandbox.operator.fc.config import FCOperatorConfig
from rock.sandbox.sandbox_manager import SandboxManager
from rock.sdk.common.exceptions import BadRequestRockError


def _make_manager(**attrs) -> SandboxManager:
    """Create a SandboxManager without running __init__."""
    manager = SandboxManager.__new__(SandboxManager)
    for key, value in attrs.items():
        setattr(manager, key, value)
    return manager


class TestDuplicateDetection:
    @pytest.mark.xfail(reason="W13: FCOperatorConfig skips duplicate sandbox detection")
    async def test_fc_duplicate_session_raises(self, fc_config):
        manager = _make_manager(_meta_store=MagicMock(exists=AsyncMock(return_value=True)))
        config = FCOperatorConfig(session_id="fc-dup", image="img")
        with pytest.raises(BadRequestRockError):
            await manager._check_sandbox_exists_in_redis(config)


class TestBuildSandboxInfoMetadata:
    async def test_int_memory_crashes_convert_to_gb(self):
        """C7: FCOperator.submit returns memory as int; manager expects str and crashes."""
        manager = _make_manager(
            refresh_aes_key=AsyncMock(),
            _aes_encrypter=MagicMock(),
        )
        info: SandboxInfo = {"sandbox_id": "s", "memory": 4096}  # type: ignore[typeddict-item]
        with pytest.raises(AttributeError):
            await manager._build_sandbox_info_metadata(info, {}, {})


class TestTimeoutInfo:
    async def _run_start_async(self, config: FCOperatorConfig) -> dict:
        sandbox_info: SandboxInfo = {
            "sandbox_id": config.session_id,
            "memory": "4g",
            "state": State.RUNNING,
        }
        manager = _make_manager(
            _check_sandbox_exists_in_redis=AsyncMock(),
            deployment_manager=MagicMock(init_config=AsyncMock(return_value=config)),
            _operator=MagicMock(submit=AsyncMock(return_value=sandbox_info)),
            refresh_aes_key=AsyncMock(),
            _aes_encrypter=MagicMock(),
            rock_config=MagicMock(runtime=MagicMock(use_standard_spec_only=False)),
            _meta_store=MagicMock(create=AsyncMock()),
        )
        await manager.start_async(config, {}, {})
        return manager._meta_store.create.call_args.kwargs["timeout_info"]

    async def test_unmerged_session_ttl_falls_back_to_10_minutes(self):
        """W12: session_ttl is None before merge, timeout falls back to 10 minutes."""
        config = FCOperatorConfig(session_id="fc-ttl-none", image="img", session_ttl=None)
        timeout_info = await self._run_start_async(config)
        assert timeout_info[env_vars.ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY] == "10"

    async def test_explicit_session_ttl_used_as_minutes(self):
        config = FCOperatorConfig(session_id="fc-ttl-7200", image="img", session_ttl=7200)
        timeout_info = await self._run_start_async(config)
        assert timeout_info[env_vars.ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY] == "120"
