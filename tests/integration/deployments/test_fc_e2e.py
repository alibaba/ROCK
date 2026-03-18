"""
FC Runtime E2E Tests

End-to-end tests that call actual deployed FC functions.
These tests require a real FC function to be deployed.

FC (Function Compute) is Alibaba Cloud's serverless compute service.

Prerequisites:
1. Deploy FC function (choose one runtime mode):
   - Custom Runtime: cd rock/deployments/fc_rocklet/runtime && ./package.sh && s deploy
   - Adapter: cd rock/deployments/fc_rocklet/adapter && ./package.sh && s deploy
   - Container: Build image, push to ACR, update s.yaml, then s deploy
2. Set environment variables for credentials:
   FC_ACCOUNT_ID, FC_ACCESS_KEY_ID, FC_ACCESS_KEY_SECRET

Access Protocols:
- FC SDK: Use FCDeployment class with FC API (recommended)
- HTTP: Direct HTTP calls to FC function endpoints
- WebSocket: Stateful session via FC WebSocket API
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

E2E-WS (WebSocket Protocol):
- E2E-WS-01: WebSocket session operations
- E2E-WS-02: WebSocket reconnection

E2E-GRPC (gRPC Protocol - Not Supported):
- E2E-GRPC-01: Health check via gRPC (planned)
- E2E-GRPC-02: Create session via gRPC (planned)
- E2E-GRPC-03: Execute command via gRPC (planned)
"""

import asyncio
import os
import uuid
from io import BytesIO
from pathlib import Path
from typing import Optional

import httpx
import pytest

# E2E tests requiring deployed FC function
# Prerequisites:
#   1. Deploy FC function: cd rock/deployments/fc_rocklet/runtime && s deploy
#   2. Set environment variables: FC_ACCOUNT_ID, FC_ACCESS_KEY_ID, FC_ACCESS_KEY_SECRET
pytestmark = pytest.mark.need_fc

from rock.deployments.config import FCDeploymentConfig
from rock.deployments.fc import FCDeployment, FCSessionManager
from rock.logger import init_logger

logger = init_logger(__name__)


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
    """Get FC function URL."""
    config = get_fc_config()
    account_id = config["account_id"]
    region = config["region"]
    function_name = config["function_name"]
    return f"https://{account_id}.{region}.fc.aliyuncs.com/2016-08-15/proxy/{function_name}"


def skip_if_no_credentials():
    """Skip tests if FC credentials are not available."""
    config = get_fc_config()
    if not config["account_id"] or not config["access_key_id"]:
        pytest.skip(
            "FC credentials not set. Set FC_ACCOUNT_ID, FC_ACCESS_KEY_ID, FC_ACCESS_KEY_SECRET"
        )


@pytest.fixture
def fc_deployment_config() -> FCDeploymentConfig:
    """Create FCDeploymentConfig for testing."""
    skip_if_no_credentials()

    config = get_fc_config()
    session_id = f"e2e-{uuid.uuid4().hex[:8]}"

    return FCDeploymentConfig(
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


@pytest.fixture
async def fc_deployment(fc_deployment_config: FCDeploymentConfig) -> FCDeployment:
    """Create and start FCDeployment for testing."""
    deployment = FCDeployment.from_config(fc_deployment_config)
    await deployment.start()
    yield deployment
    # Cleanup
    try:
        await deployment.close()
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
    """E2E tests via FC SDK (FCDeployment class)."""

    @pytest.mark.asyncio
    async def test_is_alive(self, fc_deployment: FCDeployment):
        """E2E-FC-SDK-01: Test is_alive via FC SDK."""
        result = await fc_deployment.is_alive()
        assert result.is_alive is True


class TestE2EFCSDKSessionLifecycle:
    """E2E tests for session lifecycle via FC SDK."""

    @pytest.mark.asyncio
    async def test_create_session(self, fc_deployment: FCDeployment):
        """E2E-FC-SDK-02a: Create session via FC SDK."""
        create_result = await fc_deployment.create_session(
            request={"session_type": "bash"}
        )
        assert create_result.success is True

    @pytest.mark.asyncio
    async def test_full_session_lifecycle(self, fc_deployment: FCDeployment):
        """E2E-FC-SDK-02b: Full session lifecycle via FC SDK."""
        # Create session
        create_result = await fc_deployment.create_session(
            request={"session_type": "bash"}
        )
        assert create_result.success is True

        # Run command
        run_result = await fc_deployment.run_in_session(
            action={"action_type": "bash", "command": "echo 'hello fc sdk'"}
        )
        assert run_result.exit_code == 0
        assert "hello fc sdk" in run_result.output

        # Close session
        close_result = await fc_deployment.close_session(
            request={"session_id": fc_deployment.sandbox_id}
        )
        assert close_result.success is True


class TestE2EFCSDKCommandExecution:
    """E2E tests for command execution via FC SDK."""

    @pytest.fixture(autouse=True)
    async def setup_session(self, fc_deployment: FCDeployment):
        """Create session before each test."""
        await fc_deployment.create_session(request={"session_type": "bash"})
        yield
        try:
            await fc_deployment.close_session(
                request={"session_id": fc_deployment.sandbox_id}
            )
        except Exception:
            pass

    @pytest.mark.asyncio
    async def test_echo_command(self, fc_deployment: FCDeployment):
        """E2E-FC-SDK-03a: Test echo command."""
        result = await fc_deployment.run_in_session(
            action={"action_type": "bash", "command": "echo 'test'"}
        )
        assert result.exit_code == 0
        assert "test" in result.output

    @pytest.mark.asyncio
    async def test_command_with_pipe(self, fc_deployment: FCDeployment):
        """E2E-FC-SDK-03b: Test command with pipe."""
        result = await fc_deployment.run_in_session(
            action={"action_type": "bash", "command": "echo 'hello world' | wc -w"}
        )
        assert result.exit_code == 0
        assert "2" in result.output

    @pytest.mark.asyncio
    async def test_environment_variable_persistence(self, fc_deployment: FCDeployment):
        """E2E-FC-SDK-03c: Test environment variable persists."""
        await fc_deployment.run_in_session(
            action={"action_type": "bash", "command": "export TEST_VAR='e2e_value'"}
        )
        result = await fc_deployment.run_in_session(
            action={"action_type": "bash", "command": "echo $TEST_VAR"}
        )
        assert "e2e_value" in result.output

    @pytest.mark.asyncio
    async def test_working_directory_persistence(self, fc_deployment: FCDeployment):
        """E2E-FC-SDK-03d: Test cd command persists."""
        await fc_deployment.run_in_session(
            action={"action_type": "bash", "command": "mkdir -p /tmp/e2e_test && cd /tmp/e2e_test"}
        )
        result = await fc_deployment.run_in_session(
            action={"action_type": "bash", "command": "pwd"}
        )
        assert "/tmp/e2e_test" in result.output


class TestE2EFCSDKFileOperations:
    """E2E tests for file operations via FC SDK."""

    @pytest.fixture(autouse=True)
    async def setup_session(self, fc_deployment: FCDeployment):
        """Create session before each test."""
        await fc_deployment.create_session(request={"session_type": "bash"})
        yield
        try:
            await fc_deployment.close_session(
                request={"session_id": fc_deployment.sandbox_id}
            )
        except Exception:
            pass

    @pytest.mark.asyncio
    async def test_write_and_read_file(self, fc_deployment: FCDeployment):
        """E2E-FC-SDK-04a: Test write and read file."""
        test_content = f"e2e test content {uuid.uuid4().hex}"
        test_file = f"/tmp/e2e_test_{uuid.uuid4().hex[:8]}.txt"

        # Write file
        write_result = await fc_deployment.write_file(
            request={"path": test_file, "content": test_content}
        )
        assert write_result.success is True

        # Read file
        read_result = await fc_deployment.read_file(request={"path": test_file})
        assert read_result.success is True
        assert test_content in read_result.content

    @pytest.mark.asyncio
    async def test_read_nonexistent_file(self, fc_deployment: FCDeployment):
        """E2E-FC-SDK-04b: Test reading nonexistent file."""
        result = await fc_deployment.read_file(
            request={"path": f"/tmp/nonexistent_{uuid.uuid4().hex}.txt"}
        )
        assert result.success is False or result.error is not None


class TestE2EFCSDKDirectExecute:
    """E2E tests for direct execute via FC SDK."""

    @pytest.mark.asyncio
    async def test_execute_command_directly(self, fc_deployment: FCDeployment):
        """E2E-FC-SDK-05: Test execute command without session."""
        result = await fc_deployment.execute(
            command={"command": "echo 'direct execute'", "timeout": 30, "shell": True}
        )
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
            json={"session_type": "bash"},
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
            json={"session_id": session_id, "session_type": "bash"},
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
# E2E-WS: WebSocket Protocol Tests
# ============================================================


class TestE2EWebSocketSession:
    """E2E tests for WebSocket session operations."""

    @pytest.mark.asyncio
    async def test_create_session(self, fc_deployment_config: FCDeploymentConfig):
        """E2E-WS-01a: Test creating session via WebSocket."""
        session_manager = FCSessionManager(config=fc_deployment_config)
        session_id = f"ws-{uuid.uuid4().hex[:8]}"

        try:
            result = await session_manager.create_session(session_id=session_id)
            assert result is not None
            assert session_id in session_manager.sessions
        finally:
            await session_manager.close_session(session_id=session_id)

    @pytest.mark.asyncio
    async def test_execute_command(self, fc_deployment_config: FCDeploymentConfig):
        """E2E-WS-01b: Test executing command via WebSocket."""
        session_manager = FCSessionManager(config=fc_deployment_config)
        session_id = f"ws-{uuid.uuid4().hex[:8]}"

        try:
            await session_manager.create_session(session_id=session_id)

            result = await session_manager.execute_command(
                session_id=session_id,
                command="echo 'websocket test'",
            )
            assert result is not None
            assert "websocket test" in result.get("output", "")
        finally:
            await session_manager.close_session(session_id=session_id)

    @pytest.mark.asyncio
    async def test_close_session(self, fc_deployment_config: FCDeploymentConfig):
        """E2E-WS-01c: Test closing WebSocket session."""
        session_manager = FCSessionManager(config=fc_deployment_config)
        session_id = f"ws-{uuid.uuid4().hex[:8]}"

        await session_manager.create_session(session_id=session_id)
        assert session_id in session_manager.sessions

        await session_manager.close_session(session_id=session_id)
        assert session_id not in session_manager.sessions

    @pytest.mark.asyncio
    async def test_session_stats(self, fc_deployment_config: FCDeploymentConfig):
        """E2E-WS-01d: Test getting session statistics."""
        session_manager = FCSessionManager(config=fc_deployment_config)
        session_id = f"ws-{uuid.uuid4().hex[:8]}"

        try:
            await session_manager.create_session(session_id=session_id)

            stats = await session_manager.get_session_stats(session_id=session_id)
            assert stats is not None
            assert stats.get("session_id") == session_id
        finally:
            await session_manager.close_session(session_id=session_id)


class TestE2EWebSocketReconnection:
    """E2E tests for WebSocket reconnection behavior."""

    @pytest.mark.asyncio
    async def test_session_state_tracking(self, fc_deployment_config: FCDeploymentConfig):
        """E2E-WS-02a: Test session state is tracked correctly."""
        session_manager = FCSessionManager(config=fc_deployment_config)
        session_id = f"ws-{uuid.uuid4().hex[:8]}"

        try:
            await session_manager.create_session(session_id=session_id)

            # Check session is alive
            is_alive = await session_manager.is_session_alive(session_id=session_id)
            assert is_alive is True

            # Get session state
            state = session_manager.sessions.get(session_id)
            assert state is not None
            assert state.session_id == session_id
        finally:
            await session_manager.close_session(session_id=session_id)

    @pytest.mark.asyncio
    async def test_multiple_sessions(self, fc_deployment_config: FCDeploymentConfig):
        """E2E-WS-02b: Test managing multiple WebSocket sessions."""
        session_manager = FCSessionManager(config=fc_deployment_config)
        session_ids = [f"ws-multi-{i}-{uuid.uuid4().hex[:6]}" for i in range(3)]

        try:
            # Create multiple sessions
            for sid in session_ids:
                await session_manager.create_session(session_id=sid)

            # Verify all sessions exist
            for sid in session_ids:
                assert sid in session_manager.sessions

            # Execute commands in different sessions
            for i, sid in enumerate(session_ids):
                result = await session_manager.execute_command(
                    session_id=sid,
                    command=f"echo 'session {i}'",
                )
                assert f"session {i}" in result.get("output", "")

        finally:
            for sid in session_ids:
                try:
                    await session_manager.close_session(session_id=sid)
                except Exception:
                    pass


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
    async def test_command_timeout(self, fc_deployment_config: FCDeploymentConfig):
        """E2E-ERR-01b: Test command timeout handling."""
        short_timeout_config = FCDeploymentConfig(
            type="fc",
            session_id=f"timeout-{uuid.uuid4().hex[:8]}",
            function_name=fc_deployment_config.function_name,
            region=fc_deployment_config.region,
            account_id=fc_deployment_config.account_id,
            access_key_id=fc_deployment_config.access_key_id,
            access_key_secret=fc_deployment_config.access_key_secret,
            function_timeout=2.0,
        )

        deployment = FCDeployment.from_config(short_timeout_config)
        try:
            await deployment.start()
            result = await deployment.execute(
                command={"command": "sleep 10", "timeout": 1, "shell": True}
            )
            # Should timeout or return error
            assert result.exit_code != 0 or result.error is not None
        finally:
            try:
                await deployment.close()
            except Exception:
                pass