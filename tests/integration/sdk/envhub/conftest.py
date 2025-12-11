import asyncio
import subprocess

import pytest
import requests

from rock.utils import find_free_port, release_port


@pytest.fixture(name="envhub_server")
async def envhub_server_fixture():
    """Start EnvHub server using subprocess with random port"""
    import os
    import tempfile

    process = None
    db_file = None
    port = None

    try:
        # Get free port
        port = await find_free_port()

        # Create temporary database file
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_file:
            db_file = tmp_file.name

        # Start server process with random port
        cmd = ["envhub", "--db-url", f"sqlite:///{db_file}", "--port", str(port)]

        process = subprocess.Popen(cmd, stdout=None, stderr=None)

        # Health check
        server_url = f"http://127.0.0.1:{port}"
        max_retries = 10
        for i in range(max_retries):
            try:
                requests.get(f"{server_url}/health", timeout=2)
                break
            except requests.exceptions.RequestException:
                if i == max_retries - 1:
                    raise Exception(f"Server failed to start on port {port}")
                print(f"Waiting for server on port {port}... ({i + 1}/{max_retries})")
                await asyncio.sleep(1)  # 使用 async sleep

        print(f"Server started successfully on {server_url}")
        yield server_url

    finally:
        # Release port
        if port:
            release_port(port)

        # Terminate server process
        if process:
            try:
                process.terminate()
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

        # Clean up temporary database file
        if db_file and os.path.exists(db_file):
            try:
                os.unlink(db_file)
            except Exception as e:
                print(f"Failed to remove temporary database file {db_file}: {e}")
