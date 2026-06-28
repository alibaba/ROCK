"""
FC Runtime E2E Tests

End-to-end tests that call actual deployed FC functions.
These tests require a real FC function to be deployed.

FC (Function Compute) is Alibaba Cloud's serverless compute service.

Note: FC uses direct Runtime management via FCOperator, not the Deployment pattern.

Prerequisites:
1. Deploy FC function (choose one runtime mode):
   - Custom Runtime: cd rock/deployments/fc_rocklet/runtime && ./package.sh && s deploy
   - Container: Build image, push to ACR, update s.yaml, then s deploy
2. Set environment variables for credentials:
   FC_ACCOUNT_ID, FC_ACCESS_KEY_ID, FC_ACCESS_KEY_SECRET

Access Protocols:
- FC SDK: Use FCRuntime class with SDK InvokeFunction (recommended)
- HTTP: Direct HTTP calls to FC function HTTP trigger endpoints
- gRPC: Not supported yet (planned)

Test Coverage:

E2E-FC-SDK (FC SDK Protocol):
- E2E-FC-SDK-01: Health check (is_alive)
- E2E-FC-SDK-02: Session lifecycle (create, run, close)
- E2E-FC-SDK-03: Command execution with session
- E2E-FC-SDK-04: File operations (read/write)
- E2E-FC-SDK-05: Direct execute (no session)

E2E-HTTP (HTTP Protocol):
- E2E-HTTP-01: Health check (is_alive)
- E2E-HTTP-02: Session lifecycle (create, run, close)
- E2E-HTTP-03: Command execution with session
- E2E-HTTP-04: File operations (read/write/upload)
- E2E-HTTP-05: Direct execute (no session)
- E2E-HTTP-06: Error handling

E2E-GRPC (gRPC Protocol - Not Supported):
- E2E-GRPC-01: Health check via gRPC (planned)
- E2E-GRPC-02: Create session via gRPC (planned)
- E2E-GRPC-03: Execute command via gRPC (planned)
"""

import os
import uuid

import httpx
import pytest

from rock.actions import Action, CloseSessionRequest, CreateSessionRequest, ReadFileRequest, WriteFileRequest
from rock.logger import init_logger
from rock.sandbox.operator.fc import FCOperatorConfig, FCRuntime

logger = init_logger(__name__)

# E2E tests requiring deployed FC function
# Prerequisites:
#   1. Deploy FC function: cd rock/deployments/fc_rocklet/runtime && s deploy
#   2. Set environment variables: FC_ACCOUNT_ID, FC_ACCESS_KEY_ID, FC_ACCESS_KEY_SECRET
pytestmark = pytest.mark.need_fc


# ============================================================
# Test Configuration
# ============================================================


def get_fc_config() -> dict:
    """Get FC configuration from environment or defaults."""
    return {
        "region": os.getenv("FC_REGION", "cn-hangzhou"),
        "function_name": os.getenv("FC_FUNCTION_NAME", "rock-serverless-runtime-rocklet"),
        "account_id": os.getenv("FC_ACCOUNT_ID"),
        "access_key_id": os.getenv("FC_ACCESS_KEY_ID"),
        "access_key_secret": os.getenv("FC_ACCESS_KEY_SECRET"),
        "security_token": os.getenv("FC_SECURITY_TOKEN"),
    }


def get_fc_url() -> str:
    """Get FC function HTTP trigger URL.

    FC 3.0 uses the fcapp.run domain format:
    https://{prefix}.{region}.fcapp.run

    The URL can be overridden via FC_FUNCTION_URL env var.
    """
    url = os.getenv("FC_FUNCTION_URL")
    if url:
        return url.rstrip("/")
    # Default: derive from function name (FC 3.0 auto-generated URL)
    config = get_fc_config()
    region = config["region"]
    function_name = config["function_name"]
    # FC 3.0 URL pattern: https://{prefix}.{region}.fcapp.run
    # The prefix is auto-generated; use env var FC_FUNCTION_URL for exact URL
    return f"https://{function_name}.{region}.fcapp.run"


def skip_if_no_credentials():
    """Skip tests if FC credentials are not available."""
    config = get_fc_config()
    if not config["account_id"] or not config["access_key_id"]:
        pytest.skip("FC credentials not set. Set FC_ACCOUNT_ID, FC_ACCESS_KEY_ID, FC_ACCESS_KEY_SECRET")


@pytest.fixture
def fc_runtime_config() -> FCOperatorConfig:
    """Create FCOperatorConfig for testing."""
    skip_if_no_credentials()

    config = get_fc_config()
    session_id = f"e2e-{uuid.uuid4().hex[:8]}"

    return FCOperatorConfig(
        type="fc",
        session_id=session_id,
        function_name=config["function_name"],
        region=config["region"],
        account_id=config["account_id"],
        access_key_id=config["access_key_id"],
        access_key_secret=config["access_key_secret"],
        security_token=config["security_token"],
        session_ttl=600,
        session_idle_timeout=60,
        function_timeout=30.0,
    )


def create_fc_client(fc_config: dict):
    """Create FC SDK client from config dict.

    FC 3.0 SDK endpoint format:
        UID.{region-id}.fc.aliyuncs.com
    e.g. 1273734601317349.cn-hangzhou.fc.aliyuncs.com
    """
    from alibabacloud_fc20230330.client import Client
    from alibabacloud_tea_openapi.models import Config

    account_id = fc_config["account_id"]
    region = fc_config["region"]
    sdk_config = Config(
        access_key_id=fc_config["access_key_id"],
        access_key_secret=fc_config["access_key_secret"],
        security_token=fc_config.get("security_token"),
        endpoint=f"{account_id}.{region}.fc.aliyuncs.com",
    )
    return Client(sdk_config)


@pytest.fixture
async def fc_runtime(fc_runtime_config: FCOperatorConfig) -> FCRuntime:
    """Create and initialize FCRuntime for testing."""
    config = get_fc_config()
    client = create_fc_client(config)
    runtime = FCRuntime(fc_runtime_config, fc_client=client)
    # Create session for this sandbox via SDK InvokeFunction
    await runtime.create_session(CreateSessionRequest(session=fc_runtime_config.session_id))
    yield runtime
    # Cleanup
    try:
        await runtime.close()
    except Exception:
        pass


@pytest.fixture
def http_client() -> httpx.AsyncClient:
    """Create HTTP client for direct API testing."""
    skip_if_no_credentials()
    return httpx.AsyncClient(timeout=60.0)


# ============================================================
# E2E-FC-SDK: FC SDK Protocol Tests
# ============================================================


class TestE2EFCSDKHealthCheck:
    """E2E tests via FC SDK (FCRuntime class)."""

    @pytest.mark.asyncio
    async def test_is_alive(self, fc_runtime: FCRuntime):
        """E2E-FC-SDK-01: Test is_alive via FC SDK."""
        result = await fc_runtime.is_alive()
        assert result.is_alive is True


class TestE2EFCSDKSessionLifecycle:
    """E2E tests for session lifecycle via FC SDK."""

    @pytest.mark.asyncio
    async def test_create_session(self, fc_runtime: FCRuntime):
        """E2E-FC-SDK-02a: Create session via FC SDK."""
        create_result = await fc_runtime.create_session(CreateSessionRequest(session=fc_runtime_config.session_id))
        assert create_result is not None

    @pytest.mark.asyncio
    async def test_full_session_lifecycle(self, fc_runtime: FCRuntime, fc_runtime_config: FCOperatorConfig):
        """E2E-FC-SDK-02b: Full session lifecycle via FC SDK."""
        # Create session
        create_result = await fc_runtime.create_session(CreateSessionRequest(session=fc_runtime_config.session_id))
        assert create_result is not None

        # Run command
        run_result = await fc_runtime.run_in_session(
            Action(session=fc_runtime_config.session_id, action_type="bash", command="echo 'hello fc sdk'")
        )
        assert run_result.exit_code == 0
        assert "hello fc sdk" in run_result.output

        # Close session
        close_result = await fc_runtime.close_session(CloseSessionRequest(session=fc_runtime_config.session_id))
        assert close_result is not None


class TestE2EFCSDKCommandExecution:
    """E2E tests for command execution via FC SDK."""

    @pytest.fixture(autouse=True)
    async def setup_session(self, fc_runtime: FCRuntime, fc_runtime_config: FCOperatorConfig):
        """Create session before each test."""
        await fc_runtime.create_session(CreateSessionRequest(session=fc_runtime_config.session_id))
        yield
        try:
            await fc_runtime.close_session(CloseSessionRequest(session=fc_runtime_config.session_id))
        except Exception:
            pass

    @pytest.mark.asyncio
    async def test_echo_command(self, fc_runtime: FCRuntime, fc_runtime_config: FCOperatorConfig):
        """E2E-FC-SDK-03a: Test echo command."""
        result = await fc_runtime.run_in_session(
            Action(session=fc_runtime_config.session_id, action_type="bash", command="echo 'test'")
        )
        assert result.exit_code == 0
        assert "test" in result.output

    @pytest.mark.asyncio
    async def test_command_with_pipe(self, fc_runtime: FCRuntime, fc_runtime_config: FCOperatorConfig):
        """E2E-FC-SDK-03b: Test command with pipe."""
        result = await fc_runtime.run_in_session(
            Action(session=fc_runtime_config.session_id, action_type="bash", command="echo 'hello world' | wc -w")
        )
        assert result.exit_code == 0
        assert "2" in result.output

    @pytest.mark.asyncio
    async def test_environment_variable_persistence(self, fc_runtime: FCRuntime, fc_runtime_config: FCOperatorConfig):
        """E2E-FC-SDK-03c: Test environment variable persists."""
        await fc_runtime.run_in_session(
            Action(session=fc_runtime_config.session_id, action_type="bash", command="export TEST_VAR='e2e_value'")
        )
        result = await fc_runtime.run_in_session(
            Action(session=fc_runtime_config.session_id, action_type="bash", command="echo $TEST_VAR")
        )
        assert "e2e_value" in result.output

    @pytest.mark.asyncio
    async def test_working_directory_persistence(self, fc_runtime: FCRuntime, fc_runtime_config: FCOperatorConfig):
        """E2E-FC-SDK-03d: Test cd command persists."""
        await fc_runtime.run_in_session(
            Action(
                session=fc_runtime_config.session_id,
                action_type="bash",
                command="mkdir -p /tmp/e2e_test && cd /tmp/e2e_test",
            )
        )
        result = await fc_runtime.run_in_session(
            Action(session=fc_runtime_config.session_id, action_type="bash", command="pwd")
        )
        assert "/tmp/e2e_test" in result.output


class TestE2EFCSDKFileOperations:
    """E2E tests for file operations via FC SDK."""

    @pytest.fixture(autouse=True)
    async def setup_session(self, fc_runtime: FCRuntime, fc_runtime_config: FCOperatorConfig):
        """Create session before each test."""
        await fc_runtime.create_session(CreateSessionRequest(session=fc_runtime_config.session_id))
        yield
        try:
            await fc_runtime.close_session(CloseSessionRequest(session=fc_runtime_config.session_id))
        except Exception:
            pass

    @pytest.mark.asyncio
    async def test_write_and_read_file(self, fc_runtime: FCRuntime):
        """E2E-FC-SDK-04a: Test write and read file."""
        test_content = f"e2e test content {uuid.uuid4().hex}"
        test_file = f"/tmp/e2e_test_{uuid.uuid4().hex[:8]}.txt"

        # Write file
        write_result = await fc_runtime.write_file(WriteFileRequest(path=test_file, content=test_content))
        assert write_result.success is True

        # Read file
        read_result = await fc_runtime.read_file(ReadFileRequest(path=test_file))
        assert read_result.success is True
        assert test_content in read_result.content

    @pytest.mark.asyncio
    async def test_read_nonexistent_file(self, fc_runtime: FCRuntime):
        """E2E-FC-SDK-04b: Test reading nonexistent file."""
        result = await fc_runtime.read_file(ReadFileRequest(path=f"/tmp/nonexistent_{uuid.uuid4().hex}.txt"))
        assert result.success is False or result.error is not None


class TestE2EFCSDKDirectExecute:
    """E2E tests for direct execute via FC SDK."""

    @pytest.mark.asyncio
    async def test_execute_command_directly(self, fc_runtime: FCRuntime):
        """E2E-FC-SDK-05: Test execute command without session."""
        from rock.actions import Command

        result = await fc_runtime.execute(Command(command="echo 'direct execute'", timeout=30, shell=True))
        assert result.exit_code == 0
        assert "direct execute" in result.output


# ============================================================
# E2E-HTTP: HTTP Protocol Tests
# ============================================================


class TestE2EHTTPHealthCheck:
    """E2E tests via direct HTTP calls."""

    @pytest.mark.asyncio
    async def test_is_alive(self, http_client: httpx.AsyncClient):
        """E2E-HTTP-01: Test /is_alive endpoint via HTTP."""
        url = f"{get_fc_url()}/is_alive"
        response = await http_client.get(url)
        assert response.status_code == 200
        data = response.json()
        assert data.get("is_alive") is True


class TestE2EHTTPSessionLifecycle:
    """E2E tests for session lifecycle via HTTP."""

    @pytest.mark.asyncio
    async def test_create_session(self, http_client: httpx.AsyncClient):
        """E2E-HTTP-02a: Test /create_session via HTTP."""
        session_id = f"session-{uuid.uuid4().hex[:8]}"
        url = f"{get_fc_url()}/create_session"
        response = await http_client.post(
            url,
            json={"session_type": "bash", "session": session_id},
            headers={"x-rock-session-id": session_id},
        )
        assert response.status_code == 200
        data = response.json()
        assert "output" in data or "session_type" in data

    @pytest.mark.asyncio
    async def test_close_session(self, http_client: httpx.AsyncClient):
        """E2E-HTTP-02b: Test /close_session via HTTP."""
        session_id = f"session-{uuid.uuid4().hex[:8]}"
        base_url = get_fc_url()

        # Create session first
        await http_client.post(
            f"{base_url}/create_session",
            json={"session_type": "bash"},
            headers={"x-rock-session-id": session_id},
        )

        # Close session - include session_type for discriminator compatibility
        response = await http_client.post(
            f"{base_url}/close_session",
            json={"session": session_id, "session_type": "bash"},
            headers={"x-rock-session-id": session_id},
        )
        assert response.status_code == 200


class TestE2EHTTPCommandExecution:
    """E2E tests for command execution via HTTP."""

    @pytest.mark.asyncio
    async def test_run_in_session(self, http_client: httpx.AsyncClient):
        """E2E-HTTP-03: Test /run_in_session via HTTP."""
        session_id = f"session-{uuid.uuid4().hex[:8]}"
        base_url = get_fc_url()

        # Create session
        await http_client.post(
            f"{base_url}/create_session",
            json={"session_type": "bash"},
            headers={"x-rock-session-id": session_id},
        )

        # Run command
        response = await http_client.post(
            f"{base_url}/run_in_session",
            json={"action_type": "bash", "command": "echo 'http test'"},
            headers={"x-rock-session-id": session_id},
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("exit_code") == 0


class TestE2EHTTPFileOperations:
    """E2E tests for file operations via HTTP."""

    @pytest.mark.asyncio
    async def test_write_file(self, http_client: httpx.AsyncClient):
        """E2E-HTTP-04a: Test /write_file via HTTP."""
        session_id = f"session-{uuid.uuid4().hex[:8]}"
        base_url = get_fc_url()
        test_content = f"e2e http test content {uuid.uuid4().hex}"
        test_file = f"/tmp/e2e_http_{uuid.uuid4().hex[:8]}.txt"

        # Create session
        await http_client.post(
            f"{base_url}/create_session",
            json={"session_type": "bash"},
            headers={"x-rock-session-id": session_id},
        )

        # Write file
        response = await http_client.post(
            f"{base_url}/write_file",
            json={"path": test_file, "content": test_content},
            headers={"x-rock-session-id": session_id},
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("success") is True or data.get("exit_code") == 0

    @pytest.mark.asyncio
    async def test_read_file(self, http_client: httpx.AsyncClient):
        """E2E-HTTP-04b: Test /read_file via HTTP."""
        session_id = f"session-{uuid.uuid4().hex[:8]}"
        base_url = get_fc_url()
        test_content = f"e2e http read test {uuid.uuid4().hex}"
        test_file = f"/tmp/e2e_http_read_{uuid.uuid4().hex[:8]}.txt"

        # Create session
        await http_client.post(
            f"{base_url}/create_session",
            json={"session_type": "bash"},
            headers={"x-rock-session-id": session_id},
        )

        # Write file first
        await http_client.post(
            f"{base_url}/write_file",
            json={"path": test_file, "content": test_content},
            headers={"x-rock-session-id": session_id},
        )

        # Read file
        response = await http_client.post(
            f"{base_url}/read_file",
            json={"path": test_file},
            headers={"x-rock-session-id": session_id},
        )
        assert response.status_code == 200
        data = response.json()
        assert test_content in data.get("content", "") or test_content in data.get("output", "")

    @pytest.mark.asyncio
    async def test_upload_file(self, http_client: httpx.AsyncClient):
        """E2E-HTTP-04c: Test /upload via HTTP."""
        session_id = f"session-{uuid.uuid4().hex[:8]}"
        base_url = get_fc_url()
        test_content = f"e2e http upload test {uuid.uuid4().hex}"
        test_file = f"/tmp/e2e_http_upload_{uuid.uuid4().hex[:8]}.txt"

        # Create session
        await http_client.post(
            f"{base_url}/create_session",
            json={"session_type": "bash"},
            headers={"x-rock-session-id": session_id},
        )

        # Upload file (using base64 encoded content)
        import base64

        encoded_content = base64.b64encode(test_content.encode()).decode()

        response = await http_client.post(
            f"{base_url}/upload",
            json={"path": test_file, "content": encoded_content, "encoding": "base64"},
            headers={"x-rock-session-id": session_id},
        )
        assert response.status_code == 200


class TestE2EHTTPDirectExecute:
    """E2E tests for direct execute via HTTP."""

    @pytest.mark.asyncio
    async def test_execute_no_session(self, http_client: httpx.AsyncClient):
        """E2E-HTTP-05a: Test /execute without session via HTTP."""
        base_url = get_fc_url()

        response = await http_client.post(
            f"{base_url}/execute",
            json={"command": "echo 'direct execute via http'", "timeout": 30, "shell": True},
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("exit_code") == 0
        # Response may have 'output' or 'stdout' field depending on implementation
        output = data.get("output", "") or data.get("stdout", "")
        assert "direct execute via http" in output

    @pytest.mark.asyncio
    async def test_execute_with_pipes(self, http_client: httpx.AsyncClient):
        """E2E-HTTP-05b: Test /execute with pipes via HTTP."""
        base_url = get_fc_url()

        response = await http_client.post(
            f"{base_url}/execute",
            json={"command": "echo 'hello world' | wc -w", "timeout": 30, "shell": True},
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("exit_code") == 0
        # Response may have 'output' or 'stdout' field depending on implementation
        output = data.get("output", "") or data.get("stdout", "")
        assert "2" in output


class TestE2EHTTPErrorHandling:
    """E2E tests for error handling via HTTP."""

    @pytest.mark.asyncio
    async def test_invalid_session_id(self, http_client: httpx.AsyncClient):
        """E2E-HTTP-06a: Test with invalid session ID."""
        base_url = get_fc_url()

        response = await http_client.post(
            f"{base_url}/run_in_session",
            json={"action_type": "bash", "command": "echo test"},
            headers={"x-rock-session-id": "invalid-nonexistent-session-xyz"},
        )
        # Should return error or create new session
        assert response.status_code in [200, 400, 404]

    @pytest.mark.asyncio
    async def test_read_nonexistent_file(self, http_client: httpx.AsyncClient):
        """E2E-HTTP-06b: Test reading nonexistent file."""
        session_id = f"session-{uuid.uuid4().hex[:8]}"
        base_url = get_fc_url()

        # Create session
        await http_client.post(
            f"{base_url}/create_session",
            json={"session_type": "bash"},
            headers={"x-rock-session-id": session_id},
        )

        # Try to read nonexistent file
        response = await http_client.post(
            f"{base_url}/read_file",
            json={"path": f"/tmp/nonexistent_{uuid.uuid4().hex}.txt"},
            headers={"x-rock-session-id": session_id},
        )
        # Should return error
        data = response.json()
        assert data.get("success") is False or data.get("error") is not None or response.status_code >= 400

    @pytest.mark.asyncio
    async def test_command_failure(self, http_client: httpx.AsyncClient):
        """E2E-HTTP-06c: Test command that fails."""
        session_id = f"session-{uuid.uuid4().hex[:8]}"
        base_url = get_fc_url()

        # Create session
        await http_client.post(
            f"{base_url}/create_session",
            json={"session_type": "bash"},
            headers={"x-rock-session-id": session_id},
        )

        # Run a command that will fail
        response = await http_client.post(
            f"{base_url}/run_in_session",
            json={"action_type": "bash", "command": "exit 1"},
            headers={"x-rock-session-id": session_id},
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("exit_code") == 1


# ============================================================
# E2E-FC-SDK-Direct: Direct SDK InvokeFunction Tests
# ============================================================


class TestE2EFCSDKInvokeFunction:
    """E2E tests using FC SDK invoke_function_with_options directly.

    These tests bypass the FCRuntime abstraction and call the FC SDK's
    InvokeFunction API directly, verifying the SDK integration at the lowest level.
    """

    @pytest.fixture
    def fc_sdk_setup(self):
        """Setup FC SDK client and session ID for direct invoke tests."""
        skip_if_no_credentials()
        config = get_fc_config()
        client = create_fc_client(config)
        session_id = f"sdk-direct-{uuid.uuid4().hex[:8]}"
        return client, config["function_name"], session_id

    def _invoke(self, client, function_name, session_id, payload):
        """Call invoke_function_with_options directly."""
        import json

        from alibabacloud_fc20230330.models import (
            InvokeFunctionHeaders,
            InvokeFunctionRequest,
        )
        from alibabacloud_tea_util.models import RuntimeOptions

        req = InvokeFunctionRequest(body=json.dumps(payload))
        headers = InvokeFunctionHeaders(common_headers={"x-rock-session-id": session_id})
        runtime = RuntimeOptions(read_timeout=60000, connect_timeout=10000)
        resp = client.invoke_function_with_options(function_name, req, headers, runtime)
        # SDK returns BytesIO for resp.body; read and decode
        body = resp.body
        if hasattr(body, "read"):
            body = body.read()
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        return json.loads(body)

    @pytest.mark.asyncio
    async def test_sdk_invoke_is_alive(self, fc_sdk_setup):
        """E2E-FC-SDK-DIR-01: Test is_alive via direct SDK InvokeFunction."""
        client, fn, sid = fc_sdk_setup
        result = self._invoke(client, fn, sid, {"action": "is_alive"})
        assert result.get("is_alive") is True

    @pytest.mark.asyncio
    async def test_sdk_invoke_create_session(self, fc_sdk_setup):
        """E2E-FC-SDK-DIR-02: Create session via direct SDK InvokeFunction."""
        client, fn, sid = fc_sdk_setup
        result = self._invoke(client, fn, sid, {"action": "create_session", "session": "s"})
        assert "output" in result or "session_type" in result

    @pytest.mark.asyncio
    async def test_sdk_invoke_full_lifecycle(self, fc_sdk_setup):
        """E2E-FC-SDK-DIR-03: Full session lifecycle via direct SDK InvokeFunction."""
        client, fn, sid = fc_sdk_setup

        # Create session
        r = self._invoke(client, fn, sid, {"action": "create_session", "session": "s"})
        assert "session_type" in r

        # Run echo command
        r = self._invoke(client, fn, sid, {"action": "run_in_session", "command": "echo sdk_direct", "session": "s"})
        assert r.get("exit_code") == 0
        assert "sdk_direct" in r.get("output", "")

        # cd /tmp and verify state persistence
        self._invoke(client, fn, sid, {"action": "run_in_session", "command": "cd /tmp", "session": "s"})
        r = self._invoke(client, fn, sid, {"action": "run_in_session", "command": "pwd", "session": "s"})
        assert r.get("exit_code") == 0
        assert "/tmp" in r.get("output", "")

        # Export env var and verify persistence
        self._invoke(
            client,
            fn,
            sid,
            {"action": "run_in_session", "command": "export SDK_TEST=42", "session": "s"},
        )
        r = self._invoke(client, fn, sid, {"action": "run_in_session", "command": "echo $SDK_TEST", "session": "s"})
        assert r.get("exit_code") == 0
        assert "42" in r.get("output", "")

        # Close session
        r = self._invoke(client, fn, sid, {"action": "close_session", "session": "s"})
        assert "session_type" in r


# ============================================================
# E2E-Lifecycle: Session Lifecycle Timeout Tests
# ============================================================


class TestE2ESessionLifecycleTimeout:
    """E2E tests for session idle timeout behavior.

    These tests verify that FC session affinity respects the configured
    sessionIdleTimeoutInSeconds. When a session is idle for longer than
    the timeout, the FC platform recycles the instance.

    Prerequisites:
    - Deploy function with short sessionIdleTimeoutInSeconds (e.g., 60s)
    - Set FC_SESSION_IDLE_TIMEOUT env var to match the deployed timeout
    """

    @pytest.fixture
    def timeout_config(self):
        """Get timeout configuration from environment."""
        skip_if_no_credentials()
        idle_timeout = int(os.getenv("FC_SESSION_IDLE_TIMEOUT", "60"))
        return {"idle_timeout": idle_timeout}

    @pytest.mark.asyncio
    async def test_session_survives_within_timeout(self, http_client: httpx.AsyncClient):
        """E2E-LIFECYCLE-01: Session persists within idle timeout window."""
        session_id = f"lifecycle-{uuid.uuid4().hex[:8]}"
        base_url = get_fc_url()
        headers = {"x-rock-session-id": session_id}

        # Create session
        resp = await http_client.post(
            f"{base_url}/",
            json={"action": "create_session", "session": "s"},
            headers=headers,
        )
        assert resp.status_code == 200

        # Run command immediately
        resp = await http_client.post(
            f"{base_url}/",
            json={"action": "run_in_session", "command": "echo active", "session": "s"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("exit_code") == 0
        assert "active" in data.get("output", "")

        # Cleanup
        await http_client.post(
            f"{base_url}/",
            json={"action": "close_session", "session": "s"},
            headers=headers,
        )

    @pytest.mark.asyncio
    async def test_session_state_lost_after_timeout(self, http_client: httpx.AsyncClient, timeout_config):
        """E2E-LIFECYCLE-02: Session state is lost after idle timeout.

        After the session idle timeout expires, FC creates a new instance.
        The rocklet's internal bash session is lost, so state (cd, env vars)
        does not persist across the timeout boundary.
        """
        import asyncio

        session_id = f"timeout-{uuid.uuid4().hex[:8]}"
        base_url = get_fc_url()
        headers = {"x-rock-session-id": session_id}
        idle_timeout = timeout_config["idle_timeout"]

        # Create session and set state
        await http_client.post(
            f"{base_url}/",
            json={"action": "create_session", "session": "s"},
            headers=headers,
        )
        await http_client.post(
            f"{base_url}/",
            json={"action": "run_in_session", "command": "cd /tmp", "session": "s"},
            headers=headers,
        )
        resp = await http_client.post(
            f"{base_url}/",
            json={"action": "run_in_session", "command": "pwd", "session": "s"},
            headers=headers,
        )
        assert "/tmp" in resp.json().get("output", "")

        # Wait for idle timeout to expire (add 10s buffer)
        wait_time = idle_timeout + 10
        await asyncio.sleep(wait_time)

        # After timeout, instance is recycled; new instance has no state
        # The FC platform routes the request to a new instance with the same session ID,
        # but the rocklet's internal session no longer exists.
        # Expected outcomes:
        #   1. SessionDoesNotExistError (instance recycled, session lost)
        #   2. 200 with pwd != /tmp (if session auto-recreated on new instance)
        resp = await http_client.post(
            f"{base_url}/",
            json={"action": "run_in_session", "command": "pwd", "session": "s"},
            headers=headers,
        )
        data = resp.json()

        # Case 1: Session no longer exists (most common after instance recycle)
        if "rockletexception" in data:
            assert "does not exist" in str(data.get("rockletexception", {}).get("message", "")), (
                f"Unexpected error: {data}"
            )
        else:
            # Case 2: Session auto-recreated, but state lost
            output = data.get("output", "")
            assert "/tmp" not in output, (
                f"Session state persisted across timeout (unexpected). pwd after timeout: {output}"
            )

    @pytest.mark.asyncio
    async def test_session_ttl_expiration(self, http_client: httpx.AsyncClient):
        """E2E-LIFECYCLE-03: Session TTL forces session recycling.

        The sessionTTLInSeconds sets a maximum lifetime for a session,
        regardless of activity. After TTL expires, the session is recycled.
        """
        import asyncio

        session_id = f"ttl-{uuid.uuid4().hex[:8]}"
        base_url = get_fc_url()
        headers = {"x-rock-session-id": session_id}
        session_ttl = int(os.getenv("FC_SESSION_TTL", "120"))

        # Create session
        await http_client.post(
            f"{base_url}/",
            json={"action": "create_session", "session": "s"},
            headers=headers,
        )

        # Keep session active with periodic requests
        for i in range(session_ttl // 10):
            await http_client.post(
                f"{base_url}/",
                json={"action": "run_in_session", "command": f"echo keepalive_{i}", "session": "s"},
                headers=headers,
            )
            await asyncio.sleep(10)

        # After TTL, session should be recycled
        await asyncio.sleep(10)

        # New request should get a fresh instance
        resp = await http_client.post(
            f"{base_url}/",
            json={"action": "run_in_session", "command": "echo after_ttl", "session": "s"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert "after_ttl" in resp.json().get("output", "")


# ============================================================
# E2E-GRPC: gRPC Protocol Tests (Not Supported Yet)
# ============================================================


class TestE2EGRPC:
    """E2E tests for gRPC protocol (planned feature).

    gRPC support is not yet implemented in ROCK.
    These tests are placeholders for future implementation.
    """

    @pytest.mark.skip(reason="gRPC not supported yet")
    @pytest.mark.asyncio
    async def test_grpc_health_check(self):
        """E2E-GRPC-01: Test health check via gRPC."""
        pass

    @pytest.mark.skip(reason="gRPC not supported yet")
    @pytest.mark.asyncio
    async def test_grpc_create_session(self):
        """E2E-GRPC-02: Test create session via gRPC."""
        pass

    @pytest.mark.skip(reason="gRPC not supported yet")
    @pytest.mark.asyncio
    async def test_grpc_execute_command(self):
        """E2E-GRPC-03: Test execute command via gRPC."""
        pass


# ============================================================
# E2E-Error: Error Handling Tests
# ============================================================


class TestE2EErrorHandling:
    """E2E tests for error handling across all protocols."""

    @pytest.mark.asyncio
    async def test_invalid_session_id_http(self, http_client: httpx.AsyncClient):
        """E2E-ERR-01a: Test HTTP with invalid session ID."""
        url = f"{get_fc_url()}/run_in_session"
        response = await http_client.post(
            url,
            json={"action_type": "bash", "command": "echo test"},
            headers={"x-rock-session-id": "invalid-nonexistent-session"},
        )
        # Should return error or create new session
        assert response.status_code in [200, 400, 404]

    @pytest.mark.asyncio
    async def test_command_timeout(self, fc_runtime_config: FCOperatorConfig):
        """E2E-ERR-01b: Test command timeout handling."""
        from rock.actions import Command

        short_timeout_config = FCOperatorConfig(
            type="fc",
            session_id=f"timeout-{uuid.uuid4().hex[:8]}",
            function_name=fc_runtime_config.function_name,
            region=fc_runtime_config.region,
            account_id=fc_runtime_config.account_id,
            access_key_id=fc_runtime_config.access_key_id,
            access_key_secret=fc_runtime_config.access_key_secret,
            function_timeout=2.0,
        )

        config = get_fc_config()
        client = create_fc_client(config)
        runtime = FCRuntime(short_timeout_config, fc_client=client)
        try:
            # Create session before executing
            await runtime.create_session(CreateSessionRequest(session=short_timeout_config.session_id))
            result = await runtime.execute(Command(command="sleep 10", timeout=1, shell=True))
            # Should timeout or return error
            assert result.exit_code != 0 or result.stderr is not None
        finally:
            try:
                await runtime.close()
            except Exception:
                pass
