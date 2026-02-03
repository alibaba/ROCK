import asyncio
import os
from pathlib import Path

from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig


async def main() -> None:
    sandbox = Sandbox(SandboxConfig())

    await sandbox.start()

    await sandbox.agent.install()

    result = await sandbox.agent.run("Hello")

    print(f"[Agent] run finished, result={result}")

    await sandbox.stop()


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent)

    # Ensure admin server is running before executing
    print("IMPORTANT: Make sure the admin server is running before executing this demo!")
    print("Start the admin server with: rock admin start")
    asyncio.run(main())
