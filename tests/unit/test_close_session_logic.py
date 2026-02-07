"""
Unit test for close_session - tests the core logic without Docker
"""
import pytest
from rock.rocklet.local_sandbox import LocalSandboxRuntime, BashSession
from rock.admin.proto.request import SandboxBashAction as BashAction
from rock.admin.proto.request import SandboxCloseBashSessionRequest as CloseBashSessionRequest
from rock.admin.proto.request import SandboxCreateBashSessionRequest as CreateBashSessionRequest


@pytest.mark.asyncio
async def test_close_session_logic():
    """Test close_session logic directly with LocalSandboxRuntime"""
    
    # Create runtime (no extra parameters allowed)
    runtime = LocalSandboxRuntime()
    
    # 1. Create a session
    session_name = "test-session"
    create_req = CreateBashSessionRequest(session=session_name, session_type="bash")
    create_resp = await runtime.create_session(create_req)
    assert create_resp.session_type == "bash"
    assert session_name in runtime.sessions
    print(f"✅ Session '{session_name}' created successfully")
    
    # 2. Run a command in the session
    action = BashAction(command="echo 'hello'", session=session_name)
    obs = await runtime.run_in_session(action)
    assert "hello" in obs.output
    print(f"✅ Command executed in session: {obs.output.strip()}")
    
    # 3. Close the session
    close_req = CloseBashSessionRequest(session=session_name, session_type="bash")
    close_resp = await runtime.close_session(close_req)
    assert close_resp.session_type == "bash"
    assert session_name not in runtime.sessions
    print(f"✅ Session '{session_name}' closed successfully")
    
    # 4. Verify session is closed (should raise exception)
    with pytest.raises(Exception) as exc_info:
        await runtime.run_in_session(action)
    
    assert "does not exist" in str(exc_info.value)
    print(f"✅ Verified: Closed session cannot be used")
    
    print("\n" + "=" * 60)
    print("✅ All unit tests passed!")
    print("=" * 60)


@pytest.mark.asyncio
async def test_close_nonexistent_session():
    """Test closing a session that doesn't exist raises SessionDoesNotExistError"""
    from rock.rocklet.local_sandbox import SessionDoesNotExistError
    
    runtime = LocalSandboxRuntime()
    
    # Try to close a session that was never created
    close_req = CloseBashSessionRequest(session="nonexistent-session", session_type="bash")
    
    with pytest.raises(SessionDoesNotExistError) as exc_info:
        await runtime.close_session(close_req)
    
    assert "nonexistent-session" in str(exc_info.value)
    print("✅ Correctly raised SessionDoesNotExistError for non-existent session")


@pytest.mark.asyncio
async def test_close_session_twice():
    """Test that closing a session twice raises an error"""
    from rock.rocklet.local_sandbox import SessionDoesNotExistError
    
    runtime = LocalSandboxRuntime()
    
    # Create a session
    session_name = "test-double-close"
    create_req = CreateBashSessionRequest(session=session_name, session_type="bash")
    await runtime.create_session(create_req)
    
    # Close the session first time - should succeed
    close_req = CloseBashSessionRequest(session=session_name, session_type="bash")
    await runtime.close_session(close_req)
    
    # Close the session second time - should raise error
    with pytest.raises(SessionDoesNotExistError) as exc_info:
        await runtime.close_session(close_req)
    
    assert session_name in str(exc_info.value)
    print("✅ Correctly raised SessionDoesNotExistError when closing session twice")


@pytest.mark.asyncio
async def test_close_session_with_default_name():
    """Test closing the default session"""
    runtime = LocalSandboxRuntime()
    
    # Create a session with default name
    create_req = CreateBashSessionRequest(session_type="bash")  # uses default session name
    await runtime.create_session(create_req)
    assert "default" in runtime.sessions
    
    # Close the default session
    close_req = CloseBashSessionRequest(session_type="bash")  # uses default session name
    close_resp = await runtime.close_session(close_req)
    
    assert close_resp.session_type == "bash"
    assert "default" not in runtime.sessions
    print("✅ Successfully closed default session")


if __name__ == "__main__":
    import asyncio
    asyncio.run(test_close_session_logic())
