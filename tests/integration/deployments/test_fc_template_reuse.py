"""
FC Template Reuse Integration Tests (IT-FC-11)

Tests for the two-layer architecture:
- Layer 1: Function templates (created/reused by config hash)
- Layer 2: Sandbox instances (sessions on functions, with reference counting)
"""

import json
import sys
from unittest.mock import MagicMock

import pytest

from rock.sandbox.operator.fc import FCOperatorConfig


def _make_fc_client(response_body: dict | None = None):
    """Create a mock FC SDK client with configurable response."""
    client = MagicMock()
    response = MagicMock()
    response.body = json.dumps(response_body or {"output": "root@fc:~$ "})
    client.invoke_function_with_options = MagicMock(return_value=response)
    # By default, function does not exist (get_function raises)
    client.get_function_with_options = MagicMock(side_effect=Exception("FunctionNotFound"))
    # Create/delete function return mock responses
    create_response = MagicMock()
    create_response.body = MagicMock(function_name="mock-function")
    client.create_function_with_options = MagicMock(return_value=create_response)
    client.delete_function_with_options = MagicMock(return_value=MagicMock())
    return client


@pytest.fixture(autouse=True)
def _fake_fc_sdk(monkeypatch):
    """Inject a fake alibabacloud FC SDK into sys.modules for all tests."""
    fake_client_module = MagicMock()
    fake_client_module.Client = MagicMock(return_value=MagicMock())

    fake_models_module = MagicMock()

    def _make_request_class(name):
        class _Req:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)
            def to_map(self):
                return dict(self.__dict__)
        _Req.__name__ = name
        return _Req

    for cls_name in [
        "CreateFunctionInput",
        "CreateFunctionRequest",
        "CustomContainerConfig",
        "HeaderFieldSessionAffinityConfig",
        "GetFunctionRequest",
        "ListFunctionsRequest",
        "InvokeFunctionRequest",
        "InvokeFunctionHeaders",
    ]:
        setattr(fake_models_module, cls_name, _make_request_class(cls_name))

    fake_tea_openapi_module = MagicMock()
    fake_tea_openapi_module.models = MagicMock()
    fake_tea_openapi_module.models.Config = MagicMock()

    class _RuntimeOptions:
        def __init__(self, **kwargs):
            self.common_headers = None
            self.__dict__.update(kwargs)

    fake_tea_util_module = MagicMock()
    fake_tea_util_module.models = MagicMock()
    fake_tea_util_module.models.RuntimeOptions = _RuntimeOptions

    monkeypatch.setitem(sys.modules, "alibabacloud_fc20230330", MagicMock())
    monkeypatch.setitem(sys.modules, "alibabacloud_fc20230330.client", fake_client_module)
    monkeypatch.setitem(sys.modules, "alibabacloud_fc20230330.models", fake_models_module)
    monkeypatch.setitem(sys.modules, "alibabacloud_tea_openapi", fake_tea_openapi_module)
    monkeypatch.setitem(sys.modules, "alibabacloud_tea_openapi.models", fake_tea_openapi_module.models)
    monkeypatch.setitem(sys.modules, "alibabacloud_tea_util", fake_tea_util_module)
    monkeypatch.setitem(sys.modules, "alibabacloud_tea_util.models", fake_tea_util_module.models)


class TestTemplateReuse:
    """Integration tests for FC function template reuse.

    Verifies the two-layer architecture:
    - Layer 1: Function templates are reused when configs match (by hash)
    - Layer 2: Sandbox instances (sessions) are created on templates
    """

    def _make_operator_config(self, image="img:latest", memory=4096, cpus=2.0, env=None):
        """Create an FCOperatorConfig for template testing."""
        return FCOperatorConfig(
            image=image,
            memory=memory,
            cpus=cpus,
            env=env,
            session_ttl=3600,
            session_idle_timeout=300,
            function_timeout=60.0,
            region="cn-hangzhou",
            account_id="1234567890",
            access_key_id="AKIDTEST",
            access_key_secret="SKTEST",
        )

    def _make_fc_config(self):
        from rock.config import FCConfig

        return FCConfig(
            region="cn-hangzhou",
            account_id="1234567890",
            function_name="default-func",
            access_key_id="AKIDTEST",
            access_key_secret="SKTEST",
            default_memory=4096,
            default_cpus=2.0,
            default_session_ttl=86400,
            default_function_timeout=3600.0,
            default_session_idle_timeout=1800,
        )

    @pytest.mark.asyncio
    async def test_same_config_reuses_function(self):
        """IT-FC-11a: Two sandboxes with same config share one function."""
        from rock.sandbox.operator.fc import FCOperator

        operator = FCOperator(self._make_fc_config())
        mock_client = _make_fc_client()
        operator._fc_client = mock_client

        config = self._make_operator_config()
        await operator.submit(config, user_info={})
        await operator.submit(config, user_info={})

        # create_function should only be called once (template reused)
        assert mock_client.create_function_with_options.call_count == 1

        # Both sandboxes should reference the same function
        functions = list(operator._sandbox_functions.values())
        assert len(functions) == 2
        assert functions[0] == functions[1]

        # Ref count should be 2
        ref_count = list(operator._function_refs.values())[0]
        assert ref_count == 2

    @pytest.mark.asyncio
    async def test_different_config_creates_separate_function(self):
        """IT-FC-11b: Sandboxes with different configs use different functions."""
        from rock.sandbox.operator.fc import FCOperator

        operator = FCOperator(self._make_fc_config())
        mock_client = _make_fc_client()
        operator._fc_client = mock_client

        await operator.submit(self._make_operator_config(memory=4096), user_info={})
        await operator.submit(self._make_operator_config(memory=8192), user_info={})

        assert mock_client.create_function_with_options.call_count == 2

        functions = list(operator._sandbox_functions.values())
        assert functions[0] != functions[1]

    @pytest.mark.asyncio
    async def test_stop_keeps_function_when_other_active(self):
        """IT-FC-11c: Function not deleted when other instances still use it."""
        from rock.sandbox.operator.fc import FCOperator

        operator = FCOperator(self._make_fc_config())
        mock_client = _make_fc_client()
        operator._fc_client = mock_client

        config = self._make_operator_config()
        sb1 = await operator.submit(config, user_info={})
        sb2 = await operator.submit(config, user_info={})

        # Stop first sandbox - function should be kept
        await operator.stop(sb1["sandbox_id"])
        assert mock_client.delete_function_with_options.call_count == 0
        assert list(operator._function_refs.values())[0] == 1

        # Stop second sandbox - now function should be deleted
        await operator.stop(sb2["sandbox_id"])
        assert mock_client.delete_function_with_options.call_count == 1

    @pytest.mark.asyncio
    async def test_stop_deletes_function_when_last_instance(self):
        """IT-FC-11d: Function deleted when last instance stops."""
        from rock.sandbox.operator.fc import FCOperator

        operator = FCOperator(self._make_fc_config())
        mock_client = _make_fc_client()
        operator._fc_client = mock_client

        config = self._make_operator_config()
        sb = await operator.submit(config, user_info={})
        await operator.stop(sb["sandbox_id"])

        assert mock_client.delete_function_with_options.call_count == 1

    @pytest.mark.asyncio
    async def test_different_env_creates_separate_function(self):
        """IT-FC-11e: Different env vars create different function templates."""
        from rock.sandbox.operator.fc import FCOperator

        operator = FCOperator(self._make_fc_config())
        mock_client = _make_fc_client()
        operator._fc_client = mock_client

        await operator.submit(self._make_operator_config(env={"VAR": "1"}), user_info={})
        await operator.submit(self._make_operator_config(env={"VAR": "2"}), user_info={})

        assert mock_client.create_function_with_options.call_count == 2

    @pytest.mark.asyncio
    async def test_function_name_uses_template_hash(self):
        """IT-FC-11f: Function name should contain template hash prefix."""
        from rock.sandbox.operator.fc import FCOperator

        operator = FCOperator(self._make_fc_config())
        mock_client = _make_fc_client()
        operator._fc_client = mock_client

        config = self._make_operator_config()
        sb = await operator.submit(config, user_info={})

        assert sb["function_name"].startswith("rock-tpl-")
        merged = config.merge_with_fc_config(self._make_fc_config())
        expected_hash = merged.template_hash()
        assert expected_hash in sb["function_name"]

    @pytest.mark.asyncio
    async def test_different_session_ttl_creates_separate_function(self):
        """IT-FC-11g: Different session_ttl creates different function template."""
        from rock.sandbox.operator.fc import FCOperator

        operator = FCOperator(self._make_fc_config())
        mock_client = _make_fc_client()
        operator._fc_client = mock_client

        c1 = self._make_operator_config()
        c1.session_ttl = 3600
        c2 = self._make_operator_config()
        c2.session_ttl = 7200

        await operator.submit(c1, user_info={})
        await operator.submit(c2, user_info={})

        assert mock_client.create_function_with_options.call_count == 2

    @pytest.mark.asyncio
    async def test_concurrent_submit_does_not_duplicate_function(self):
        """IT-FC-11h: Concurrent submit with same config creates only one function."""
        from rock.sandbox.operator.fc import FCOperator

        operator = FCOperator(self._make_fc_config())
        mock_client = _make_fc_client()
        operator._fc_client = mock_client

        config = self._make_operator_config()

        # Submit two sandboxes concurrently with the same config
        import asyncio
        results = await asyncio.gather(
            operator.submit(config, user_info={}),
            operator.submit(config, user_info={}),
        )

        # create_function should only be called once (double-checked locking)
        assert mock_client.create_function_with_options.call_count == 1

        # Both sandboxes should reference the same function
        assert results[0]["function_name"] == results[1]["function_name"]

    @pytest.mark.asyncio
    async def test_function_already_exists_handled_gracefully(self):
        """IT-FC-11i: FunctionAlreadyExists error during create is handled."""
        from rock.sandbox.operator.fc import FCOperator

        operator = FCOperator(self._make_fc_config())
        mock_client = _make_fc_client()
        operator._fc_client = mock_client

        # Simulate: _function_exists returns False, but create raises AlreadyExists
        mock_client.get_function_with_options = MagicMock(side_effect=Exception("NotFound"))

        call_count = [0]
        original_create = mock_client.create_function_with_options

        def create_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("FunctionAlreadyExists")
            # Second call (shouldn't happen) returns normally
            return original_create.return_value

        mock_client.create_function_with_options = MagicMock(side_effect=create_side_effect)

        config = self._make_operator_config()
        # Should not raise - FunctionAlreadyExists is handled gracefully
        sb = await operator.submit(config, user_info={})
        assert sb["function_name"].startswith("rock-tpl-")

    @pytest.mark.asyncio
    async def test_cleanup_orphaned_functions_deletes_unknown(self):
        """IT-FC-11j: cleanup_orphaned_functions deletes functions not in cache."""
        from rock.sandbox.operator.fc import FCOperator

        operator = FCOperator(self._make_fc_config())
        mock_client = _make_fc_client()
        operator._fc_client = mock_client

        # Mock list_functions to return some functions
        list_response = MagicMock()
        list_response.body = MagicMock()
        func1 = MagicMock(function_name="rock-tpl-aaa111")
        func2 = MagicMock(function_name="rock-tpl-bbb222")
        func3 = MagicMock(function_name="rock-tpl-ccc333")
        list_response.body.functions = [func1, func2, func3]
        mock_client.list_functions_with_options = MagicMock(return_value=list_response)

        # Add func1 to cache (active), func2 and func3 are orphaned
        operator._function_cache["hash1"] = "rock-tpl-aaa111"

        deleted = await operator.cleanup_orphaned_functions()

        # Should delete func2 and func3 (2 orphaned)
        assert deleted == 2
        assert mock_client.delete_function_with_options.call_count == 2

    @pytest.mark.asyncio
    async def test_cleanup_orphaned_functions_keeps_active(self):
        """IT-FC-11k: cleanup_orphaned_functions keeps functions in cache."""
        from rock.sandbox.operator.fc import FCOperator

        operator = FCOperator(self._make_fc_config())
        mock_client = _make_fc_client()
        operator._fc_client = mock_client

        # Mock list_functions
        list_response = MagicMock()
        list_response.body = MagicMock()
        func1 = MagicMock(function_name="rock-tpl-aaa111")
        list_response.body.functions = [func1]
        mock_client.list_functions_with_options = MagicMock(return_value=list_response)

        # func1 is active
        operator._function_cache["hash1"] = "rock-tpl-aaa111"

        deleted = await operator.cleanup_orphaned_functions()

        assert deleted == 0
        assert mock_client.delete_function_with_options.call_count == 0

    @pytest.mark.asyncio
    async def test_session_idle_timeout_passed_as_env(self):
        """IT-FC-11l: session_idle_timeout is passed as ROCK_SESSION_IDLE_TIMEOUT env var."""
        from rock.sandbox.operator.fc import FCOperator

        operator = FCOperator(self._make_fc_config())
        mock_client = _make_fc_client()
        operator._fc_client = mock_client

        config = self._make_operator_config()
        config.session_idle_timeout = 600

        await operator.submit(config, user_info={})

        # Check that create_function was called with env vars including ROCK_SESSION_IDLE_TIMEOUT
        call_args = mock_client.create_function_with_options.call_args
        request = call_args.args[0]
        body = request.body
        assert body.environment_variables is not None
        assert body.environment_variables.get("ROCK_SESSION_IDLE_TIMEOUT") == "600"
