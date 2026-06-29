"""Unit tests for SandboxManager FC integration paths.

Verifies review fixes:
- C2: _build_sandbox_info_metadata works with string memory (FCOperator now returns "4096m")
- W3: timeout_info uses FCConfig.default_session_ttl (24h) when session_ttl is None
- W4: FCOperatorConfig is checked for duplicate sessions
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
    async def test_fc_duplicate_session_raises(self):
        """W4: FCOperatorConfig should be checked for duplicate sessions."""
        manager = _make_manager(_meta_store=MagicMock(exists=AsyncMock(return_value=True)))
        config = FCOperatorConfig(session_id="fc-dup", image="img")
        with pytest.raises(BadRequestRockError):
            await manager._check_sandbox_exists_in_redis(config)

    async def test_fc_no_duplicate_when_not_exists(self):
        """W4: No error when session_id doesn't exist in meta store."""
        manager = _make_manager(_meta_store=MagicMock(exists=AsyncMock(return_value=False)))
        config = FCOperatorConfig(session_id="fc-new", image="img")
        await manager._check_sandbox_exists_in_redis(config)


class TestBuildSandboxInfoMetadata:
    async def test_memory_string_works_with_convert_to_gb(self):
        """C2: FCOperator.submit returns memory as string like '4096m'."""
        manager = _make_manager(
            refresh_aes_key=AsyncMock(),
            _aes_encrypter=MagicMock(),
        )
        info: SandboxInfo = {"sandbox_id": "s", "memory": "4096m"}  # type: ignore[typeddict-item]
        await manager._build_sandbox_info_metadata(info, {}, {})
        assert info["memory"] is not None


class TestTimeoutInfo:
    async def _run_start_async(self, config: FCOperatorConfig) -> dict:
        sandbox_info: SandboxInfo = {
            "sandbox_id": config.session_id,
            "memory": "4g",
            "state": State.RUNNING,
        }
        fc_config_mock = MagicMock()
        fc_config_mock.default_session_ttl = 86400
        manager = _make_manager(
            _check_sandbox_exists_in_redis=AsyncMock(),
            deployment_manager=MagicMock(init_config=AsyncMock(return_value=config)),
            _operator=MagicMock(submit=AsyncMock(return_value=sandbox_info)),
            refresh_aes_key=AsyncMock(),
            _aes_encrypter=MagicMock(),
            rock_config=MagicMock(
                runtime=MagicMock(use_standard_spec_only=False),
                fc=fc_config_mock,
            ),
            _meta_store=MagicMock(create=AsyncMock()),
        )
        await manager.start_async(config, {}, {})
        return manager._meta_store.create.call_args.kwargs["timeout_info"]

    async def test_default_session_ttl_used_as_timeout(self):
        """W3: When session_ttl is None, should use FCConfig.default_session_ttl (24h = 1440 min)."""
        config = FCOperatorConfig(session_id="fc-ttl-none", image="img", session_ttl=None)
        timeout_info = await self._run_start_async(config)
        assert timeout_info[env_vars.ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY] == "1440"

    async def test_explicit_session_ttl_used_as_minutes(self):
        config = FCOperatorConfig(session_id="fc-ttl-7200", image="img", session_ttl=7200)
        timeout_info = await self._run_start_async(config)
        assert timeout_info[env_vars.ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY] == "120"
